"""Tests for scripts/check-licence.py.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import override
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "check-licence.py"
# This repository's own LICENSE, the canonical text (CI checks it).
CANONICAL = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licence", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_licence"] = module
    spec.loader.exec_module(module)
    return module


lic = _load()


class CheckTest(unittest.TestCase):
    """The bytes must be exactly the canonical text."""

    def test_the_canonical_text_passes(self) -> None:
        """The pinned hash and size are this repository's LICENSE."""
        self.assertEqual(len(CANONICAL), lic.CANONICAL_SIZE)
        self.assertIsNone(lic.check(CANONICAL))

    def test_any_change_fails(self) -> None:
        """An edit, CRLF line endings or a trailing newline all fail."""
        for changed in (
            CANONICAL.replace(b"Affero", b"Afero", 1),
            CANONICAL.replace(b"\n", b"\r\n"),
            CANONICAL + b"\n",
            b"",
        ):
            with self.subTest(size=len(changed)):
                problem = lic.check(changed)
                self.assertIsNotNone(problem)
                self.assertIn(f"{len(changed)} bytes", str(problem))


class RepositoryTest(unittest.TestCase):
    """main() over a throwaway git repository."""

    @override
    def setUp(self) -> None:
        """Make a git repository holding a canonical root LICENSE."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        (self.root / "LICENSE").write_bytes(CANONICAL)
        (self.root / "charts" / "app").mkdir(parents=True)
        (self.root / "charts" / "app" / "LICENSE").write_bytes(CANONICAL)
        self.git("add", ".")
        patcher = mock.patch.object(lic, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args: str) -> None:
        """Run git in the throwaway repository."""
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run(  # noqa: S603  # fixed argv
            ["git", *args],  # noqa: S607  # git from PATH
            cwd=self.root,
            check=True,
            env=env,
        )

    def main(self, *args: str) -> tuple[int, str]:
        """Return the script's exit status and stdout for ARGS."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = lic.main(list(args))
        return status, out.getvalue()

    def test_every_tracked_licence_is_checked(self) -> None:
        """Both tracked LICENSE files pass while canonical."""
        self.assertEqual(
            lic.tracked_licences(self.root), ["LICENSE", "charts/app/LICENSE"]
        )
        status, out = self.main()
        self.assertEqual(status, 0, out)
        self.assertIn("2 licence file(s)", out)

    def test_a_modified_copy_names_the_file_and_the_fix(self) -> None:
        """The failure names the file and how to restore it."""
        path = self.root / "charts" / "app" / "LICENSE"
        path.write_bytes(CANONICAL.replace(b"GNU", b"GNU's", 1))
        status, out = self.main()
        self.assertEqual(status, 1)
        self.assertIn("::error file=charts/app/LICENSE::", out)
        restore = f"curl -fsSL {lic.CANONICAL_URL} -o charts/app/LICENSE"
        self.assertIn(restore, out)
        self.assertNotIn("file=LICENSE::", out)

    def test_a_missing_root_licence_fails(self) -> None:
        """A repository without a tracked root LICENSE fails."""
        self.git("rm", "-q", "--cached", "LICENSE")
        status, out = self.main()
        self.assertEqual(status, 1)
        self.assertIn("no tracked LICENSE at the repository root", out)

    def test_explicit_paths(self) -> None:
        """Paths given are checked instead, an unreadable one failing."""
        status, out = self.main("LICENSE", "nope/LICENSE")
        self.assertEqual(status, 1)
        self.assertIn("::error file=nope/LICENSE::nope/LICENSE can't be read", out)


if __name__ == "__main__":
    unittest.main()
