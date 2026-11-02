#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the findings) is stdout
"""Fail unless every LICENSE file is the canonical GNU AGPL v3 text.

  python3 <ci>/scripts/check-licence.py [PATH ...]

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository it checks. Every Distant Signal repository is
AGPL-3.0-or-later, and the licence text must never be distributed
modified. So every tracked file named LICENSE (the root one, the charts'
and any other), or each PATH given, must be byte-identical to the GNU
AGPL v3 text at https://www.gnu.org/licenses/agpl-3.0.txt: its SHA-256
and size are pinned below, so the check never fetches anything. A
repository without a tracked root LICENSE fails too.

Findings are GitHub Actions `::error file=...::` lines on stdout, each with
the command that restores the canonical text. Exit status: 0 clean,
1 findings. A git failure exits with git's status. Stdlib only.
"""

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()
CANONICAL_URL = "https://www.gnu.org/licenses/agpl-3.0.txt"
# `curl -fsSL https://www.gnu.org/licenses/agpl-3.0.txt | sha256sum`
CANONICAL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
CANONICAL_SIZE = 34523
NAME = "LICENSE"


class GitError(Exception):
    """git ls-files failed; its stderr has already been shown."""

    def __init__(self, returncode: int) -> None:
        """Record git's exit status."""
        super().__init__(f"git exited {returncode}")
        self.returncode = returncode


@dataclass(frozen=True)
class Finding:
    """A licence file that isn't the canonical text."""

    path: str
    problem: str

    def render(self) -> str:
        """Return the finding as a GitHub Actions error annotation."""
        restore = f"curl -fsSL {CANONICAL_URL} -o {self.path}"
        return (
            f"::error file={self.path}::{self.path} {self.problem}. It must be "
            f"the canonical GNU AGPL v3 text, unmodified (sha256 "
            f"{CANONICAL_SHA256}, {CANONICAL_SIZE} bytes). Restore it with: "
            f"{restore}"
        )


def tracked_licences(root: Path) -> list[str]:
    """Return every tracked file named LICENSE, relative to ROOT."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607  # git from PATH
        check=False,
        capture_output=True,
        text=True,
        cwd=root,
    )
    if listed.returncode != 0:
        sys.stderr.write(listed.stderr)
        raise GitError(listed.returncode)
    return sorted(
        name for name in listed.stdout.split("\0") if name and Path(name).name == NAME
    )


def check(data: bytes) -> str | None:
    """Return what is wrong with a licence file's bytes, or None."""
    digest = hashlib.sha256(data).hexdigest()
    if digest == CANONICAL_SHA256:
        return None
    return f"differs from the canonical text (sha256 {digest}, {len(data)} bytes)"


def findings(root: Path, paths: Sequence[str]) -> Iterator[Finding]:
    """Yield every licence file among PATHS that isn't canonical."""
    for path in paths:
        try:
            data = (root / path).read_bytes()
        except OSError as error:
            yield Finding(path, f"can't be read ({error.strerror})")
            continue
        if (problem := check(data)) is not None:
            yield Finding(path, problem)


def main(argv: Sequence[str] | None = None) -> int:
    """Check the licence files; print findings; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths", nargs="*", help="licence files (default: every tracked LICENSE)"
    )
    args = parser.parse_args(argv)
    paths: list[str] = args.paths
    if not paths:
        try:
            paths = tracked_licences(ROOT)
        except GitError as error:
            return error.returncode
        if NAME not in paths:
            print(
                f"::error::no tracked {NAME} at the repository root. Add the "
                f"canonical GNU AGPL v3 text: curl -fsSL {CANONICAL_URL} -o {NAME}"
            )
            return 1
    problems = list(findings(ROOT, paths))
    for problem in problems:
        print(problem.render())
    if problems:
        print(f"{len(problems)} of {len(paths)} licence file(s) not canonical")
        return 1
    print(f"{len(paths)} licence file(s) are the canonical GNU AGPL v3 text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
