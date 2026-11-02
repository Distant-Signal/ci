"""Tests for scripts/check-commit-identity.py.

  uv run python -m unittest discover -s scripts/tests

CheckCommitIdentityTest builds throwaway repos whose first commit is BASE,
adds commits with chosen identities and messages, and runs the script as
CI does (from the repo root, with BASE and HEAD as its arguments).
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import override

SCRIPT = Path(__file__).resolve().parent.parent / "check-commit-identity.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_commit_identity", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_commit_identity"] = module
    spec.loader.exec_module(module)
    return module


cci = _load()
MAINTAINER = ("FasterSpeeding", "lucy@cursed.solutions")
GITHUB = ("GitHub", "noreply@github.com")
RENOVATE = (
    "mine-bringer-renovate[bot]",
    "123+mine-bringer-renovate[bot]@users.noreply.github.com",
)
OUTSIDER = ("Someone Else", "someone@example.org")
# A retired address that may be named in a file: Claude's noreply.
RETIRED = ("Claude", "noreply@anthropic.com")
# No user/system config, so the host's git setup (signing, hooks, default
# branch) cannot leak into the throwaway repos.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


class CheckCommitIdentityTest(unittest.TestCase):
    """Behaviour of the check over BASE..HEAD."""

    @override
    def setUp(self) -> None:
        """Create a repo in a temporary directory with a BASE commit."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q")
        self.base = self.commit("base", RETIRED, RETIRED)

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Run git in the repo; return its stdout."""
        return subprocess.run(  # noqa: S603  # fixed git subcommands
            ["git", *args],  # noqa: S607  # git from PATH
            check=True,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env={**GIT_ENV, **(env or {})},
        ).stdout.strip()

    def commit(
        self,
        message: str,
        author: tuple[str, str] = MAINTAINER,
        committer: tuple[str, str] = MAINTAINER,
    ) -> str:
        """Make an empty commit; return its id."""
        env = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def check(self) -> tuple[int, str]:
        """Run the script over BASE..HEAD; return (status, stdout)."""
        result = subprocess.run(  # noqa: S603  # this repo's script
            [sys.executable, str(SCRIPT), self.base, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=GIT_ENV,
        )
        return result.returncode, result.stdout

    def test_expected_identities_pass(self) -> None:
        """The maintainer, Renovate, GitHub and outsiders all pass."""
        self.commit("feat: one\n\nTask: DS-1")
        self.commit("chore(deps): bump", RENOVATE, GITHUB)
        self.commit("Merge pull request #1", MAINTAINER, GITHUB)
        self.commit("fix: an outside contribution", OUTSIDER, OUTSIDER)
        # The base's retired identity is outside the range.
        self.assertEqual(
            self.check(),
            (
                0,
                (
                    f"4 commit(s) in {self.base}..HEAD: no retired identity and no "
                    "AI attribution\n"
                ),
            ),
        )

    def test_retired_author_fails(self) -> None:
        """A retired author address fails, whatever its case."""
        sha = self.commit("docs: fix", ("Claude", "NoReply@Anthropic.com"))
        status, out = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"::error::{sha[:12]}: the author (Claude)", out)
        self.assertNotIn("committer", out)

    def test_retired_committer_fails(self) -> None:
        """A retired committer address fails."""
        sha = self.commit("docs: fix", MAINTAINER, RETIRED)
        status, out = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"::error::{sha[:12]}: the committer (Claude)", out)

    def test_attribution_trailers_fail(self) -> None:
        """Each attribution line the rewrite strips fails."""
        lines = [
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            "co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>",
            "Claude-Session: https://claude.ai/code/session_abc",
            "\N{ROBOT FACE} Generated with [Claude Code](https://claude.com/claude-code)",
            "Generated with Claude Code",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.base = self.git("rev-parse", "HEAD")
                sha = self.commit(f"feat: x\n\nTask: DS-1\n{line}")
                status, out = self.check()
                self.assertEqual(status, 1)
                self.assertIn(f"::error::{sha[:12]}: the message carries AI", out)

    def test_product_mentions_of_claude_pass(self) -> None:
        """Claude as product content is not attribution."""
        self.commit("feat(enricher): Claude Message Batches for the Anthropic provider")
        self.commit("fix(frontend): /connect-claude keeps the Claude API key")
        self.commit("docs: Co-authored-by: Someone <someone@example.org>")
        status, _ = self.check()
        self.assertEqual(status, 0)

    def test_unknown_base_exits_with_gits_status(self) -> None:
        """A git failure is passed through."""
        self.base = "0" * 40
        status, _ = self.check()
        self.assertNotEqual(status, 0)
        self.assertNotEqual(status, 1)


class IsRetiredTest(unittest.TestCase):
    """The hashed address match."""

    def test_match_ignores_case_and_whitespace(self) -> None:
        """Case and surrounding whitespace don't matter."""
        self.assertTrue(cci.is_retired(" NOREPLY@anthropic.com "))

    def test_other_addresses_are_not_retired(self) -> None:
        """The current, bot and GitHub addresses are not retired."""
        for address in (MAINTAINER[1], RENOVATE[1], GITHUB[1], OUTSIDER[1]):
            with self.subTest(address=address):
                self.assertFalse(cci.is_retired(address))


if __name__ == "__main__":
    unittest.main()
