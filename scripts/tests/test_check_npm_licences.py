"""Tests for scripts/check-npm-licences.py.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "check-npm-licences.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_npm_licences", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_npm_licences"] = module
    spec.loader.exec_module(module)
    return module


npm = _load()


class ExpressionTests(unittest.TestCase):
    """SPDX expressions against the allow-list."""

    def test_single_ids(self) -> None:
        """An allowed id passes; any other fails."""
        self.assertTrue(npm.satisfied("MIT"))
        self.assertFalse(npm.satisfied("GPL-2.0-only"))

    def test_or_needs_one_and_needs_both(self) -> None:
        """OR is satisfied by either side, AND only by both."""
        self.assertTrue(npm.satisfied("(MPL-2.0 OR Apache-2.0)"))
        self.assertTrue(npm.satisfied("Apache-2.0 AND LGPL-3.0-or-later AND MIT"))
        self.assertFalse(npm.satisfied("MIT AND SSPL-1.0"))
        self.assertTrue(npm.satisfied("SSPL-1.0 OR (MIT AND ISC)"))

    def test_malformed_expressions_are_errors(self) -> None:
        """Unbalanced, truncated or WITH expressions raise."""
        for bad in (
            "(MIT",
            "MIT AND",
            "MIT)",
            "Apache-2.0 WITH LLVM-exception",
            "MIT/ISC",
        ):
            with self.subTest(bad=bad), self.assertRaises(npm.ExpressionError):
                npm.satisfied(bad)


class LockfileTests(unittest.TestCase):
    """Which lockfile packages are checked."""

    def test_dev_packages_and_the_root_are_skipped(self) -> None:
        """Only non-dev packages count, and a missing licence fails."""
        lock = {
            "packages": {
                "": {"license": "AGPL-3.0-or-later"},
                "node_modules/a": {"license": "MIT"},
                "node_modules/b": {"license": "GPL-3.0-only", "dev": True},
                "node_modules/c": {"license": "GPL-3.0-only", "devOptional": True},
                "node_modules/d": {},
            }
        }
        self.assertEqual(
            list(npm.findings(lock)),
            [
                "node_modules/c: 'GPL-3.0-only' is not on the allow-list",
                "node_modules/d: no licence in the lockfile",
            ],
        )

    def test_main_reads_the_lockfile_it_is_given(self) -> None:
        """--lockfile: a clean lockfile passes, a bad one fails."""
        clean = {"packages": {"": {}, "node_modules/a": {"license": "MIT"}}}
        bad = {"packages": {"": {}, "node_modules/b": {"license": "GPL-3.0-only"}}}
        with tempfile.TemporaryDirectory() as tmp:
            for lock, expected in ((clean, 0), (bad, 1)):
                path = Path(tmp) / "package-lock.json"
                path.write_text(json.dumps(lock), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    status = npm.main(["--lockfile", str(path)])
                self.assertEqual(status, expected, out.getvalue())


if __name__ == "__main__":
    unittest.main()
