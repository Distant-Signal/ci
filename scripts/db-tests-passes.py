#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose answer is stdout
"""Read a repository's DB test passes for the reusable rust-db-tests.yml.

  python3 <ci>/scripts/db-tests-passes.py matrix [--file FILE]
  python3 <ci>/scripts/db-tests-passes.py script [--file FILE] archive
  python3 <ci>/scripts/db-tests-passes.py script [--file FILE] pass ID

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository whose passes it reads. FILE (default .github/db-tests.toml):

  [archive]
  # Builds every DB test binary once, into the archive at
  # $DB_TESTS_ARCHIVE (e.g. a cargo-nextest archive).
  run = "..."

  [[pass]]
  id = "superuser"            # [a-z0-9-]+, unique
  name = "DB tests as the superuser"  # the job's name
  run = '''
  ...                         # bash, run with -euo pipefail
  '''

`matrix` prints `passes=<JSON list of {"id", "name"}>` and appends the same
line to $GITHUB_OUTPUT when that is set: the pass jobs' matrix. `script`
prints the bash of the archive step or of one pass, which the workflow runs
from the repository root. The scripts stay in the repository's file and
are read again by each job, so they never pass through a workflow
expression. Exit status 2 on a malformed file. Stdlib only.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILE = Path(".github/db-tests.toml")
PASS_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MALFORMED = 2


class PassesError(ValueError):
    """The passes file is malformed."""


@dataclass(frozen=True)
class Pass:
    """One DB test pass: a job in the matrix."""

    id: str
    name: str
    run: str


@dataclass(frozen=True)
class Passes:
    """The archive step and every pass, in file order."""

    archive: str
    passes: tuple[Pass, ...]


def _string(table: Mapping[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{where}: `{key}` must be a non-empty string"
        raise PassesError(msg)
    return value


def parse(data: Mapping[str, object]) -> Passes:
    """Validate a parsed passes file."""
    archive = data.get("archive")
    if not isinstance(archive, dict):
        msg = "[archive] is missing"
        raise PassesError(msg)
    tables = data.get("pass")
    if not isinstance(tables, list) or not tables:
        msg = "no [[pass]] tables"
        raise PassesError(msg)
    passes: list[Pass] = []
    for index, table in enumerate(tables):
        where = f"[[pass]] #{index + 1}"
        if not isinstance(table, dict):
            msg = f"{where}: not a table"
            raise PassesError(msg)
        pass_id = _string(table, "id", where)
        if not PASS_ID.match(pass_id):
            msg = f"{where}: id {pass_id!r} must match {PASS_ID.pattern}"
            raise PassesError(msg)
        if any(existing.id == pass_id for existing in passes):
            msg = f"{where}: duplicate id {pass_id!r}"
            raise PassesError(msg)
        passes.append(
            Pass(pass_id, _string(table, "name", where), _string(table, "run", where))
        )
    return Passes(_string(archive, "run", "[archive]"), tuple(passes))


def load(path: Path) -> Passes:
    """Read and validate the passes file."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"{path}: {error}"
        raise PassesError(msg) from error
    return parse(data)


def matrix(passes: Passes) -> str:
    """Return the matrix line: `passes=<JSON>`."""
    entries = [{"id": p.id, "name": p.name} for p in passes.passes]
    return f"passes={json.dumps(entries, separators=(',', ':'))}"


def script(passes: Passes, which: Sequence[str]) -> str:
    """Return the archive step's script, or one pass's."""
    if list(which) == ["archive"]:
        return passes.archive
    if len(which) == 2 and which[0] == "pass":  # noqa: PLR2004  # `pass ID`
        for candidate in passes.passes:
            if candidate.id == which[1]:
                return candidate.run
        msg = f"no pass {which[1]!r}"
        raise PassesError(msg)
    msg = "expected `archive` or `pass ID`"
    raise PassesError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the matrix or a script; 2 on a malformed file."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["matrix", "script"])
    parser.add_argument("which", nargs="*", help="`archive` or `pass ID` (script)")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args(argv)
    try:
        passes = load(args.file)
        if args.command == "script":
            print(script(passes, args.which), end="")
            return 0
    except PassesError as error:
        print(f"::error file={args.file}::{error}", file=sys.stderr)
        return MALFORMED
    line = matrix(passes)
    print(line)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
