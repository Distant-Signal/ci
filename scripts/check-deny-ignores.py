#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the findings) is stdout
"""Fail when a deny.toml advisory ignore has no reason or no live expiry date.

  python3 <ci>/scripts/check-deny-ignores.py [--today YYYY-MM-DD] [DENY_TOML]

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository it checks (DENY_TOML defaults to ./deny.toml). rust-ci.yml
blocks on `cargo deny check advisories`, so a new RustSec advisory fails
every pull request until it is fixed or ignored. An ignore is a deliberate,
temporary exception, so each entry in `[advisories] ignore` must be an inline
table, on one line, that has:

  - a non-empty `reason = "..."`, and
  - an expiry date, written `expires YYYY-MM-DD` in the comment block
    directly above the entry or in a comment after it on the same line:

      # rsa: Marvin timing attack, no RSA private-key use.
      # expires 2027-05-17
      { id = "RUSTSEC-2023-0071", reason = "no RSA private-key operations" },

An entry whose date is before today fails; one that expires within
WARN_DAYS days is annotated with a warning. Renewing an ignore means
re-reading the advisory and moving the date. Findings are GitHub Actions
`::error file=...::` lines; exit status 0 clean, 1 findings, 2 unusable
input. Stdlib only.
"""

import argparse
import datetime as dt
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = "deny.toml"
WARN_DAYS = 14
EXPIRES = re.compile(r"\bexpires\s+(\d{4}-\d{2}-\d{2})\b")
ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
REASON = re.compile(r"""\breason\s*=\s*(?:"([^"]*)"|'([^']*)')""")
SECTION = re.compile(r"^\s*\[+([^\]]+)\]+\s*(?:#.*)?$")
ARRAY_START = re.compile(r"ignore\s*=\s*\[")


@dataclass(frozen=True)
class Entry:
    """One element of the advisories ignore array."""

    line: int
    text: str
    comments: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    """A problem with one ignore entry; a warning unless `error`."""

    path: str
    line: int
    label: str
    problem: str
    error: bool = True

    def render(self) -> str:
        """Return the finding as a GitHub Actions annotation."""
        level = "error" if self.error else "warning"
        return (
            f"::{level} file={self.path},line={self.line}::{self.label} {self.problem}"
        )


def split_comment(line: str) -> tuple[str, str]:
    """Split a TOML line into (code, comment), ignoring `#` inside strings."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index], line[index + 1 :]
    return line, ""


def ignore_entries(text: str) -> list[Entry]:
    """Return the entries of `[advisories] ignore = [...]`, with their comments."""
    entries: list[Entry] = []
    section = ""
    in_array = False
    block: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        code, comment = split_comment(raw)
        stripped = code.strip()
        if not in_array:
            if (found := SECTION.match(raw)) is not None:
                section = found.group(1).strip()
                continue
            if section != "advisories" or not ARRAY_START.match(stripped):
                continue
            in_array = True
            block = []
            stripped = stripped.split("[", 1)[1].strip()
        if not stripped:
            # A blank line ends the comment block above the next entry; a
            # comment-only line extends it.
            block = [*block, comment.strip()] if comment else []
            continue
        if stripped.startswith("]"):
            in_array = False
            continue
        trailing = (comment.strip(),) if comment else ()
        entries.append(Entry(number, stripped, (*block, *trailing)))
        block = []
        if stripped.rstrip(",").endswith("]"):
            in_array = False
    return entries


def check_entry(entry: Entry, path: str, today: dt.date) -> list[Finding]:
    """Return the findings for one ignore entry."""
    found_id = ID.search(entry.text)
    label = found_id.group(1) if found_id else f"ignore entry at line {entry.line}"
    out: list[Finding] = []

    def problem(text: str, *, error: bool = True) -> None:
        out.append(Finding(path, entry.line, label, text, error))

    if found_id is None:
        problem(
            "must be an inline table with an `id`, e.g. "
            '{ id = "RUSTSEC-...", reason = "..." }'
        )
    reason = REASON.search(entry.text)
    if reason is None or not (reason.group(1) or reason.group(2) or "").strip():
        problem('has no `reason = "..."` saying why it is safe to ignore')
    dates = [m.group(1) for c in entry.comments if (m := EXPIRES.search(c))]
    if not dates:
        problem(
            "has no expiry: add `# expires YYYY-MM-DD` in the comment above it "
            "(a date by which the ignore is re-checked or removed)"
        )
        return out
    try:
        expiry = dt.date.fromisoformat(dates[-1])
    except ValueError:
        problem(f"has an invalid expiry date {dates[-1]!r}")
        return out
    if expiry < today:
        problem(
            f"expired on {expiry}: re-read the advisory, then fix it, remove "
            "the ignore or renew the date with a reason"
        )
    elif (expiry - today).days <= WARN_DAYS:
        problem(f"expires on {expiry}: review it soon", error=False)
    return out


def check(text: str, path: str, today: dt.date) -> list[Finding]:
    """Return every finding for deny.toml's text."""
    entries = ignore_entries(text)
    findings = [f for entry in entries for f in check_entry(entry, path, today)]
    declared = tomllib.loads(text).get("advisories", {}).get("ignore", [])
    if len(declared) != len(entries):
        findings.append(
            Finding(
                path,
                1,
                "[advisories] ignore",
                f"has {len(declared)} entries but {len(entries)} were checked: "
                "write one entry per line",
            )
        )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Check deny.toml; print findings; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=DEFAULT_PATH)
    parser.add_argument(
        "--today",
        type=dt.date.fromisoformat,
        default=None,
        help="the date to check against (default: today, UTC)",
    )
    args = parser.parse_args(argv)
    path = Path(args.path)
    today: dt.date = args.today or dt.datetime.now(dt.UTC).date()
    try:
        text = path.read_text(encoding="utf-8")
        findings = check(text, str(path), today)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"::error file={path}::can't read {path}: {error}")
        return 2
    for finding in findings:
        print(finding.render())
    errors = [f for f in findings if f.error]
    if errors:
        print(f"{len(errors)} advisory ignore problem(s) in {path}")
        return 1
    print(f"{path}: every advisory ignore has a reason and a live expiry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
