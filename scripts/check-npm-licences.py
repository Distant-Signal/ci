#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the findings) is stdout
"""Fail when a frontend production dependency's licence isn't AGPL-compatible.

  uv run <ci>/scripts/check-npm-licences.py [--lockfile frontend/package-lock.json]

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository it checks (--lockfile defaults to ./package-lock.json).

Distant Signal is AGPL-3.0-or-later (LICENSE). Every package in the
lockfile that isn't dev-only (`"dev": true`) ships in the frontend image,
so its `license` must be an SPDX expression the allow-list below satisfies:
an `OR` needs one allowed side, an `AND` needs both. A package with no
`license` fails too, until it is checked by hand and listed in EXCEPTIONS.
The allow-list is the one the repo split spec's §6 checked against
frontend/package-lock.json: all compatible with the AGPL. The Rust side's
equivalent is deny.toml's `[licenses]`.

Findings are GitHub Actions `::error file=...::` lines on stdout. Exit
status: 0 clean, 1 findings. Stdlib only.
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()
LOCKFILE = ROOT / "package-lock.json"

# SPDX ids a production dependency may carry. LGPL-3.0-or-later: sharp's
# libvips binaries, dynamically linked, which the AGPL allows. CC-BY-4.0:
# browser-support data (caniuse-lite).
ALLOWED = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC0-1.0",
        "ISC",
        "LGPL-3.0-or-later",
        "MIT",
        "MIT-0",
        "Unlicense",
    }
)

# Lockfile package path -> why its missing or non-SPDX licence is fine.
EXCEPTIONS: Mapping[str, str] = {}

TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+:-]+")


class ExpressionError(ValueError):
    """An SPDX expression this checker can't parse."""


def tokenize(expression: str) -> list[str]:
    """Split an SPDX expression into ids, operators and parentheses."""
    tokens = TOKEN.findall(expression)
    if "".join(tokens) != re.sub(r"\s+", "", expression):
        msg = f"unparseable licence expression {expression!r}"
        raise ExpressionError(msg)
    return tokens


class _Parser:
    """Evaluates one SPDX expression against an allow-list.

    Grammar: or := and ("OR" and)*; and := atom ("AND" atom)*;
    atom := id | "(" or ")". `WITH` exceptions are not accepted.
    """

    def __init__(self, expression: str, allowed: frozenset[str]) -> None:
        self.expression = expression
        self.tokens = tokenize(expression)
        self.pos = 0
        self.allowed = allowed

    def error(self, what: str) -> ExpressionError:
        msg = f"{what} in licence expression {self.expression!r}"
        return ExpressionError(msg)

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str:
        if self.pos >= len(self.tokens):
            what = "unexpected end"
            raise self.error(what)
        self.pos += 1
        return self.tokens[self.pos - 1]

    def atom(self) -> bool:
        item = self.take()
        if item == "(":
            value = self.either()
            if self.take() != ")":
                what = "unbalanced parentheses"
                raise self.error(what)
            return value
        if item in {")", "AND", "OR", "WITH"}:
            what = f"unexpected {item!r}"
            raise self.error(what)
        return item in self.allowed

    def both(self) -> bool:
        value = self.atom()
        while self.peek() == "AND":
            self.take()
            value = self.atom() and value
        return value

    def either(self) -> bool:
        value = self.both()
        while self.peek() == "OR":
            self.take()
            value = self.both() or value
        return value

    def evaluate(self) -> bool:
        value = self.either()
        if self.pos != len(self.tokens):
            what = f"trailing {self.tokens[self.pos]!r}"
            raise self.error(what)
        return value


def satisfied(expression: str, allowed: frozenset[str] = ALLOWED) -> bool:
    """Return whether `allowed` satisfies an SPDX licence expression."""
    return _Parser(expression, allowed).evaluate()


def findings(lock: Mapping[str, object]) -> Iterator[str]:
    """Yield one message per production package whose licence fails."""
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        yield "no `packages` map (lockfileVersion 2 or 3 expected)"
        return
    for path, meta in sorted(packages.items()):
        if not path or not isinstance(meta, dict) or meta.get("dev") is True:
            continue
        if meta.get("link") is True or path in EXCEPTIONS:
            continue
        licence = meta.get("license")
        if not isinstance(licence, str) or not licence.strip():
            yield f"{path}: no licence in the lockfile"
            continue
        try:
            ok = satisfied(licence)
        except ExpressionError as error:
            yield f"{path}: {error}"
            continue
        if not ok:
            yield f"{path}: {licence!r} is not on the allow-list"


def main(argv: Sequence[str] | None = None) -> int:
    """Check the lockfile; print findings; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lockfile", type=Path, default=LOCKFILE)
    args = parser.parse_args(argv)
    lock = json.loads(args.lockfile.read_text(encoding="utf-8"))
    problems = list(findings(lock))
    for problem in problems:
        print(f"::error file={args.lockfile}::{problem}")
    if problems:
        print(
            f"{len(problems)} production package(s) with a licence not on "
            "Distant-Signal/ci's scripts/check-npm-licences.py allow-list"
        )
        return 1
    print("npm licences ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
