#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose answer is stdout
"""Say whether a push or pull request changed anything under given paths.

  uv run <ci>/scripts/paths-changed.py --base SHA PATH...

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository whose paths it checks (the actions/paths-changed composite
action does).

Compares SHA with HEAD (`git diff --name-only SHA HEAD -- PATH...`),
fetching SHA first when the checkout doesn't have it. Prints `changed=true`
or `changed=false`, and appends the same line to $GITHUB_OUTPUT when that
is set, so a workflow step can gate later steps on
`steps.<id>.outputs.changed`.

Answers `true` whenever it can't tell, so a check is never skipped by
mistake: an empty or all-zero SHA (a new branch's first push, or an event
with no base) or a SHA that can't be fetched or compared.

In CI, pass the pull request's base SHA, or the push's `before` SHA.
Stdlib only; exit status 0 unless the arguments are wrong.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()

Git = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run git in the repo root, capturing its output."""
    return subprocess.run(  # noqa: S603  # fixed argv apart from the SHA and paths
        ["git", *args],  # noqa: S607  # git from PATH, as in CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def unknown_base(base: str) -> bool:
    """Return whether BASE names no commit (empty, or GitHub's all-zero SHA)."""
    return not base.strip("0 ")


def changed(base: str, paths: Sequence[str], git: Git = run_git) -> bool:
    """Return whether anything under PATHS differs between BASE and HEAD."""
    if unknown_base(base):
        return True
    if git(["cat-file", "-e", f"{base}^{{commit}}"]).returncode != 0:
        fetched = git(["fetch", "--no-tags", "--depth=1", "origin", base])
        if fetched.returncode != 0:
            print(f"couldn't fetch {base}; assuming changed", file=sys.stderr)
            return True
    diff = git(["diff", "--name-only", base, "HEAD", "--", *paths])
    if diff.returncode != 0:
        print(f"couldn't diff against {base}; assuming changed", file=sys.stderr)
        return True
    return bool(diff.stdout.strip())


def main(argv: Sequence[str] | None = None) -> int:
    """Print (and export to $GITHUB_OUTPUT) whether PATHS changed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the commit to compare HEAD with")
    parser.add_argument("paths", nargs="+", help="git pathspecs, e.g. frontend/")
    args = parser.parse_args(argv)

    line = f"changed={str(changed(args.base, args.paths)).lower()}"
    print(line)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
