#!/usr/bin/env python3
"""Lint a repository's scripts, workflows and Dockerfiles.

  uv run <ci>/scripts/lint-scripts.py [--fix]

<ci> is a checkout of Distant-Signal/ci. Run it from the root of the
repository being checked, through that repository's own uv project: the
tools come from its pyproject.toml `lint` dependency group (actionlint-py,
hadolint-py, mypy, pyyaml, ruff, shellcheck-py, shfmt-py) and its uv.lock,
and ruff and mypy read its own configuration. The reusable scripts-lint.yml
workflow runs exactly this.

Covers every tracked *.sh (shellcheck, shfmt), the Python ruff and mypy
find (ruff check, ruff format --check, mypy), the workflow run: blocks
(actionlint), every tracked Dockerfile (hadolint, with the repository's
.hadolint.yaml) and, via lint-containers.py beside this file, the
Dockerfile RUN bodies, the inline shell in the docker-compose files and
the image digest pins. Each step is reported as `== <command>`; every step
runs even after one fails, and the exit status is 1 if any failed. A step
with nothing to check (no *.sh, no Dockerfile) is skipped.

--fix applies shfmt and ruff format/autofixes first, then checks. A failing
fix command stops the run with that command's exit status.

The tools are run from this interpreter's scripts directory, never from
elsewhere on PATH.
"""

import argparse
import os
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()
# The environment's console-script directory: where uv installed the
# pinned lint group.
TOOL_DIR = Path(sysconfig.get_path("scripts"))
# actionlint runs shellcheck with --norc, so .shellcheckrc does not reach the
# workflow run: blocks; SHELLCHECK_OPTS does.
ACTIONLINT_SHELLCHECK_OPTS = "--enable=all --severity=style"
NOT_FOUND = 127
# Shipped beside this script.
LINT_CONTAINERS = Path(__file__).resolve().parent / "lint-containers.py"


def tool_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the child environment, with the lint group's tools first on PATH.

    lint-containers.py runs shellcheck by name, and actionlint runs
    the shellcheck it finds on PATH; both must get the pinned one.
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(TOOL_DIR), env.get("PATH", "")])
    env.update(extra or {})
    return env


def tool(name: str) -> str:
    """Return the path of a pinned tool (`start` reports a missing one)."""
    return str(TOOL_DIR / name)


def git_files(pattern: str) -> list[str]:
    """Return the tracked files matching a git pathspec, in git's order."""
    listed = subprocess.run(  # noqa: S603  # fixed argv
        ["git", "ls-files", "-z", "--", pattern],  # noqa: S607  # git from PATH
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return [name for name in listed.stdout.split("\0") if name]


def start(argv: Sequence[str], extra_env: Mapping[str, str] | None = None) -> int:
    """Run a command from the repo root; return its exit status (127: not found)."""
    try:
        result = subprocess.run(  # noqa: S603  # fixed tools, tracked file names
            argv, check=False, cwd=ROOT, env=tool_env(extra_env)
        )
    except FileNotFoundError:
        sys.stderr.write(f"{argv[0]}: not found; run this through `uv run`\n")
        return NOT_FOUND
    return result.returncode


def run_step(argv: Sequence[str], extra_env: Mapping[str, str] | None = None) -> bool:
    """Run one check, reporting it as `== <command>`; True when it passed."""
    shown = [f"{key}={value}" for key, value in (extra_env or {}).items()]
    words = [*shown, Path(argv[0]).name, *argv[1:]]
    sys.stdout.write(f"== {' '.join(words)}\n")
    sys.stdout.flush()
    return start(argv, extra_env) == 0


def fix(sh_files: Sequence[str]) -> int:
    """Apply the formatters and autofixes; the first failure's status, else 0."""
    commands = [[tool("ruff"), "format"], [tool("ruff"), "check", "--fix"]]
    if sh_files:  # shfmt -w with no files would wait for stdin
        commands.insert(0, [tool("shfmt"), "-w", *sh_files])
    for argv in commands:
        if (status := start(argv)) != 0:
            return status
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; 0 when all pass, 1 when any fails."""
    parser = argparse.ArgumentParser(
        description="Lint the repo's scripts, workflows and Dockerfiles."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="apply shfmt and ruff format/autofixes first, then check",
    )
    args = parser.parse_args(argv)

    sh_files = git_files("*.sh")
    dockerfiles = git_files("*Dockerfile")

    if args.fix and (status := fix(sh_files)) != 0:
        return status

    steps: list[tuple[list[str], dict[str, str] | None]] = []
    if sh_files:  # shellcheck and shfmt -d with no files fail or read stdin
        steps += [
            ([tool("shellcheck"), *sh_files], None),
            ([tool("shfmt"), "-d", *sh_files], None),
        ]
    steps += [
        ([tool("ruff"), "check"], None),
        ([tool("ruff"), "format", "--check"], None),
        ([tool("mypy")], None),
        ([tool("actionlint")], {"SHELLCHECK_OPTS": ACTIONLINT_SHELLCHECK_OPTS}),
    ]
    if dockerfiles:
        steps.append(
            ([tool("hadolint"), "--config", ".hadolint.yaml", *dockerfiles], None)
        )
    steps.append(([sys.executable, str(LINT_CONTAINERS)], None))
    results = [run_step(step, extra_env) for step, extra_env in steps]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
