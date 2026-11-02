#!/usr/bin/env python3
"""Fail if an ignored benchmark test would run without its env gate.

  python3 <ci>/scripts/check-bench-gates.py [PATH ...]

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
Rust workspace it checks.

CI's DB jobs run every `#[ignore]`d test of their packages, in a debug
build, on a shared runner. A benchmark there costs minutes and measures
noise: on 2026-10-27 `transit_planning_budget`'s two took the job from
~30 to ~78 minutes. So every test whose ignore reason marks it as a
benchmark (it says "bench", "benchmark", "--release" or "release mode")
must do nothing unless asked: the first statement of its body must
return early unless an environment variable is set. Accepted gates, any variable name:

  if std::env::var("X").is_err() { return; }
  if std::env::var_os("X").is_none() { return; }
  let Ok(x) = std::env::var("X") else { return; };
  let Some(x) = std::env::var_os("X") else { return; };
  if !helper() { return; }

where `helper` is a function in the same file whose body reads
`std::env::var` or `std::env::var_os`. `env::var(...)` with the `std::`
path imported works too. `use` declarations may come before the gate:
they are items, not statements, and run nothing.

PATH defaults to ./crates/. Findings are GitHub Actions `::error file=...::`
lines on stdout; exit 1 if any. Stdlib only.
"""

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()

# `#[ignore = "..."]` whose reason marks a benchmark.
IGNORE = re.compile(r'#\[ignore\s*=\s*"(?P<reason>[^"]*)"\s*\]')
BENCH_REASON = re.compile(
    r"\bbench(?:mark)?s?\b|--release\b|\brelease[- ](?:mode|build)\b", re.IGNORECASE
)
FN = re.compile(r"\bfn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>{]*>)?\s*\(")
ENV_READ = r"(?:std\s*::\s*)?env\s*::\s*var(?:_os)?\s*\("
ENV_READ_RE = re.compile(ENV_READ)
RETURN_BLOCK = r"\{\s*return\s*;?\s*\}"
GATES = (
    # if std::env::var("X").is_err() { return; } (or var_os(..).is_none())
    re.compile(
        r"if\s+"
        + ENV_READ
        + r"[^{};]*\)\s*\.\s*(?:is_err|is_none)\s*\(\s*\)\s*"
        + RETURN_BLOCK
    ),
    # let Ok(x) = std::env::var("X") else { return; };
    re.compile(
        r"let\s+(?:Ok|Some)\s*\([^)]*\)\s*=\s*"
        + ENV_READ
        + r"[^{};]*\)\s*else\s*"
        + RETURN_BLOCK
    ),
)
# `use` declarations before the first statement (they run nothing).
LEADING_USES = re.compile(r"\A(?:\s*use\s[^;]*;)+")
HELPER_GATE = re.compile(
    r"if\s+!\s*(?P<helper>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*" + RETURN_BLOCK
)


@dataclass(frozen=True)
class Finding:
    """A benchmark test without a leading env gate."""

    path: Path
    line: int
    name: str
    reason: str

    def render(self, root: Path) -> str:
        """Return the finding as a GitHub Actions error line."""
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return (
            f"::error file={shown},line={self.line}::benchmark test `{self.name}` "
            f'(ignore = "{self.reason}") must return early unless an env var is '
            "set, as its first statement, e.g. "
            '`if std::env::var_os("DS_RUN_..._BENCH").is_none() { return; }`: '
            "CI's DB jobs run every ignored test"
        )


def strip_comments(source: str) -> str:
    """Blank out // and /* */ comments, keeping offsets and string literals."""
    out = list(source)
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c == '"':
            i += 1
            while i < n and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            for j in range(i, end):
                out[j] = " "
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
        else:
            i += 1
    return "".join(out)


def block_end(source: str, start: int) -> int:
    """Return the index after the `}` matching the `{` at `start`."""
    depth = 0
    i = start
    n = len(source)
    while i < n:
        c = source[i]
        if c == '"':
            i += 1
            while i < n and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def function_bodies(source: str) -> dict[str, str]:
    """Return every function's body (inside its braces), by name."""
    bodies: dict[str, str] = {}
    for match in FN.finditer(source):
        brace = source.find("{", match.end())
        semicolon = source.find(";", match.end())
        if brace == -1 or (semicolon != -1 and semicolon < brace):
            continue  # a declaration without a body (a trait method)
        bodies.setdefault(
            match["name"], source[brace + 1 : block_end(source, brace) - 1]
        )
    return bodies


def is_gated(body: str, bodies: dict[str, str]) -> bool:
    """Whether `body` starts with an env-var gate that returns early."""
    head = LEADING_USES.sub("", body).lstrip()
    if any(gate.match(head) for gate in GATES):
        return True
    helper = HELPER_GATE.match(head)
    return bool(
        helper
        and helper["helper"] in bodies
        and ENV_READ_RE.search(bodies[helper["helper"]])
    )


def check_source(path: Path, source: str) -> Iterator[Finding]:
    """Yield a finding for each ungated benchmark test in one file."""
    code = strip_comments(source)
    bodies = function_bodies(code)
    for match in IGNORE.finditer(code):
        reason = match["reason"]
        if not BENCH_REASON.search(reason):
            continue
        func = FN.search(code, match.end())
        if func is None:
            continue
        brace = code.find("{", func.end())
        if brace == -1:
            continue
        body = code[brace + 1 : block_end(code, brace) - 1]
        if not is_gated(body, bodies):
            line = code.count("\n", 0, match.start()) + 1
            yield Finding(path, line, func["name"], reason)


def rust_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Yield every .rs file under `paths` (files are yielded as given)."""
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*.rs") if "target" not in p.parts)
        else:
            yield path


def main(argv: Sequence[str] | None = None) -> int:
    """Check every Rust file; 0 clean, 1 findings."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, default=[ROOT / "crates"])
    args = parser.parse_args(argv)
    findings = [
        finding
        for path in rust_files(args.paths)
        for finding in check_source(path, path.read_text(encoding="utf-8"))
    ]
    for finding in findings:
        sys.stdout.write(finding.render(ROOT) + "\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
