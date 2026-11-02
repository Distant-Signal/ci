#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the findings) is stdout
"""Fail if a commit since BASE uses a retired identity or AI attribution.

  uv run <ci>/scripts/check-commit-identity.py BASE [HEAD]

<ci> is a checkout of Distant-Signal/ci; run it inside the repository
whose commits it checks.

Repo split spec §5.10 and §5.11.3: since the phase 1 history rewrite,
the maintainer's commits are authored `FasterSpeeding
<lucy@cursed.solutions>`, and no commit message carries Claude
attribution. For each commit in BASE..HEAD (HEAD defaults to `HEAD`)
this fails (exit 1) when:

  - its author or committer address is one of the retired addresses: the
    maintainer's two old ones, or Claude's noreply. The old addresses are
    matched by SHA-256 so they are not written down here (§5.11.3: no
    file but the spec names them);
  - a message line is one of the attribution lines the rewrite strips: a
    `Co-authored-by:` trailer with an @anthropic.com address, a
    `Claude-Session:` trailer pointing at claude.ai, or a "Generated with
    Claude Code" footer.

Any other identity passes: an outside contributor's commits keep their
own author, as do Renovate's, and GitHub stays the committer of web
merges. Mentions of Claude that are product content (the connector, the
enricher's Anthropic provider) are not attribution and pass.

CI (the reusable identity.yml workflow) runs it on every pull request
(BASE: the merge base with the target branch, HEAD: the PR's head) and
push (BASE: the pre-push commit). Run it before pushing a branch:
  uv run <ci>/scripts/check-commit-identity.py "$(git merge-base origin/main HEAD)"

Findings are GitHub Actions `::error::` lines on stdout. A git failure
(e.g. an unknown BASE) exits with git's status. Stdlib only.
"""

import argparse
import hashlib
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

# SHA-256 of each retired address, lower-cased.
RETIRED_ADDRESS_HASHES = frozenset(
    {
        # The maintainer's two addresses before the rewrite.
        "d46d399bc816c2d459c9d60efc3e495aa91e772395dd43e89a1c364836d230b4",
        "2cd4fafd1e22c21845644f10a7b1c46b679faa63310616dfc124aae4d5a7c663",
        # noreply@anthropic.com.
        "cd29c5ac348a026a3ec5286890908fffb5bf6ab77f20672171be323a70c95026",
    }
)
CURRENT_IDENTITY = "FasterSpeeding <lucy@cursed.solutions>"
# The attribution lines of the rewrite's message rules (spec §5.11.3),
# matched per line, case-insensitively.
ATTRIBUTION = re.compile(
    r"^\s*(?:co-authored-by:.*@anthropic\.com"
    r"|claude-session:\s*https://claude\.ai/"
    r"|(?:\N{ROBOT FACE}\s*)?generated with \[?claude code\b)",
    re.IGNORECASE,
)
# Record and field separators for `git log`: neither appears in a name,
# an address or (in practice) a commit message.
RECORD = "\x1e"
FIELD = "\x1f"


class GitError(Exception):
    """A git command failed; its stderr has already been shown."""

    def __init__(self, returncode: int) -> None:
        """Record git's exit status."""
        super().__init__(f"git exited {returncode}")
        self.returncode = returncode


@dataclass(frozen=True)
class Commit:
    """The identity fields and message of one commit."""

    sha: str
    author: str
    author_email: str
    committer: str
    committer_email: str
    message: str


def is_retired(address: str) -> bool:
    """Whether ADDRESS is one of the retired addresses (case-insensitive)."""
    digest = hashlib.sha256(address.strip().lower().encode()).hexdigest()
    return digest in RETIRED_ADDRESS_HASHES


def attribution_lines(message: str) -> list[str]:
    """Return MESSAGE's lines that are AI attribution."""
    return [line for line in message.splitlines() if ATTRIBUTION.match(line)]


def findings(commit: Commit) -> Iterator[str]:
    """Yield an ::error line for each problem with COMMIT."""
    short = commit.sha[:12]
    for role, name, email in (
        ("author", commit.author, commit.author_email),
        ("committer", commit.committer, commit.committer_email),
    ):
        if is_retired(email):
            yield (
                f"::error::{short}: the {role} ({name}) uses a retired address. "
                f"Commit as {CURRENT_IDENTITY} (`git config user.email`), then "
                "rewrite the branch's commits, e.g. `git rebase --exec "
                "'git commit --amend --no-edit --reset-author' <base>`."
            )
    for line in attribution_lines(commit.message):
        yield (
            f"::error::{short}: the message carries AI attribution "
            f"({line.strip()!r}). Remove the line (`git commit --amend` or "
            "`git rebase -i`)."
        )


def commits(base: str, head: str) -> list[Commit]:
    """Return the commits in BASE..HEAD, newest first."""
    result = subprocess.run(  # noqa: S603  # fixed git subcommand
        [  # noqa: S607  # git from PATH
            "git",
            "log",
            f"--format=%H{FIELD}%an{FIELD}%ae{FIELD}%cn{FIELD}%ce{FIELD}%B{RECORD}",
            f"{base}..{head}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.returncode)
    parsed = []
    for record in result.stdout.split(RECORD):
        if not record.strip():
            continue
        fields = record.lstrip("\n").split(FIELD, 5)
        parsed.append(Commit(*fields))
    return parsed


def check(base: str, head: str) -> int:
    """Print the findings for BASE..HEAD; 1 if any, else 0."""
    checked = commits(base, head)
    status = 0
    for commit in checked:
        for finding in findings(commit):
            print(finding)
            status = 1
    if not status:
        print(
            f"{len(checked)} commit(s) in {base}..{head}: no retired identity "
            "and no AI attribution"
        )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Parse BASE and HEAD and run the check; returns the exit status."""
    parser = argparse.ArgumentParser(
        description="Check that commits since BASE use no retired identity "
        "and carry no AI attribution."
    )
    parser.add_argument("base", metavar="BASE", help="the commit to start after")
    parser.add_argument(
        "head", metavar="HEAD", nargs="?", default="HEAD", help="the last commit"
    )
    args = parser.parse_args(argv)
    try:
        return check(args.base, args.head)
    except GitError as error:
        return error.returncode


if __name__ == "__main__":
    sys.exit(main())
