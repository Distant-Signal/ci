"""Tests for scripts/paths-changed.py.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import subprocess
import sys
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "paths-changed.py"
Git = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
FRONTEND = ["frontend/"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("paths_changed", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["paths_changed"] = module
    spec.loader.exec_module(module)
    return module


pc = _load()


def _git(results: dict[str, int], diff_out: str = "") -> Git:
    """Fake git: each subcommand exits with results[sub] (default 0)."""

    def git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        code = results.get(args[0], 0)
        out = diff_out if args[0] == "diff" else ""
        return subprocess.CompletedProcess(list(args), code, out, "")

    return git


class PathsChangedTest(unittest.TestCase):
    """paths-changed.py answers true whenever it can't tell."""

    def test_unknown_base_counts_as_changed(self) -> None:
        """An empty or all-zero base (a new branch) counts as changed."""
        self.assertTrue(pc.changed("", FRONTEND, _git({})))
        self.assertTrue(pc.changed("0" * 40, FRONTEND, _git({})))

    def test_diff_output_decides(self) -> None:
        """Any changed file under the paths means changed; none means not."""
        self.assertTrue(pc.changed("abc", FRONTEND, _git({}, "frontend/a.tsx\n")))
        self.assertFalse(pc.changed("abc", FRONTEND, _git({}, "")))

    def test_failed_fetch_or_diff_counts_as_changed(self) -> None:
        """A base that can't be fetched or diffed counts as changed."""
        self.assertTrue(pc.changed("abc", FRONTEND, _git({"cat-file": 1, "fetch": 1})))
        self.assertTrue(pc.changed("abc", FRONTEND, _git({"diff": 128})))

    def test_fetches_a_missing_base_then_diffs(self) -> None:
        """A base missing from a shallow checkout is fetched, then compared."""
        self.assertFalse(pc.changed("abc", FRONTEND, _git({"cat-file": 1})))


if __name__ == "__main__":
    unittest.main()
