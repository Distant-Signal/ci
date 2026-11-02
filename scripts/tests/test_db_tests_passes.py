"""Tests for scripts/db-tests-passes.py.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import override
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "db-tests-passes.py"

GOOD = """\
[archive]
run = "build --archive-file \\"${DB_TESTS_ARCHIVE}\\""

[[pass]]
id = "superuser"
name = "DB tests as the superuser"
run = '''
migrate
test superuser
'''

[[pass]]
id = "app-role"
name = "DB tests as the app role"
run = "test app"
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("db_tests_passes", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["db_tests_passes"] = module
    spec.loader.exec_module(module)
    return module


dtp = _load()


class PassesTest(unittest.TestCase):
    """The passes file becomes a matrix and per-job scripts."""

    @override
    def setUp(self) -> None:
        """Write GOOD to a temporary file."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "db-tests.toml"
        self.file.write_text(GOOD, encoding="utf-8")

    def _main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = dtp.main([*args, "--file", str(self.file)])
        return status, out.getvalue(), err.getvalue()

    def test_matrix_lists_ids_and_names_only(self) -> None:
        """The matrix carries no scripts; it also goes to $GITHUB_OUTPUT."""
        output = Path(self.tmp.name) / "output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            status, out, _ = self._main("matrix")
        self.assertEqual(status, 0)
        key, _, value = out.strip().partition("=")
        self.assertEqual(key, "passes")
        self.assertEqual(
            json.loads(value),
            [
                {"id": "superuser", "name": "DB tests as the superuser"},
                {"id": "app-role", "name": "DB tests as the app role"},
            ],
        )
        self.assertEqual(output.read_text(encoding="utf-8"), out)

    def test_script_prints_the_archive_or_one_pass(self) -> None:
        """`script archive` and `script pass ID` print that bash verbatim."""
        self.assertEqual(
            self._main("script", "archive")[1],
            'build --archive-file "${DB_TESTS_ARCHIVE}"',
        )
        self.assertEqual(
            self._main("script", "pass", "superuser")[1],
            "migrate\ntest superuser\n",
        )

    def test_unknown_pass_or_target_is_an_error(self) -> None:
        """An unknown pass id, or neither `archive` nor `pass ID`, exits 2."""
        for args in (("script", "pass", "nope"), ("script",), ("script", "x")):
            with self.subTest(args=args):
                status, _, err = self._main(*args)
                self.assertEqual(status, dtp.MALFORMED)
                self.assertIn("::error file=", err)

    def test_malformed_files_are_refused(self) -> None:
        """Missing tables, bad or duplicate ids and empty runs exit 2."""
        bad_files = (
            "[[pass]]\nid = 'a'\nname = 'A'\nrun = 'x'\n",
            "[archive]\nrun = 'x'\n",
            "[archive]\nrun = 'x'\n[[pass]]\nid = 'A b'\nname = 'A'\nrun = 'x'\n",
            "[archive]\nrun = 'x'\n[[pass]]\nid = 'a'\nname = 'A'\nrun = ' '\n",
            (
                "[archive]\nrun = 'x'\n"
                "[[pass]]\nid = 'a'\nname = 'A'\nrun = 'x'\n"
                "[[pass]]\nid = 'a'\nname = 'B'\nrun = 'y'\n"
            ),
            "not toml = = =",
        )
        for text in bad_files:
            with self.subTest(text=text):
                self.file.write_text(text, encoding="utf-8")
                self.assertEqual(self._main("matrix")[0], dtp.MALFORMED)


if __name__ == "__main__":
    unittest.main()
