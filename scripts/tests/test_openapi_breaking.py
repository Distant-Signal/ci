"""Tests for scripts/openapi-breaking.py.

uv run python -m unittest discover -s scripts/tests

The oasdiff runs need the binary: set OASDIFF to it (CI's self-test job
downloads the pinned one); without it they are skipped.
"""

import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "openapi-breaking.py"
FIXTURES = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "openapi"
)
Git = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("openapi_breaking", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["openapi_breaking"] = module
    spec.loader.exec_module(module)
    return module


ob = _load()


def _git_showing(text: str | None) -> Git:
    """Fake git whose `show` prints TEXT (None: the file did not exist)."""

    def git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "show":
            if text is None:
                return subprocess.CompletedProcess(
                    list(args), 128, "", "does not exist"
                )
            return subprocess.CompletedProcess(list(args), 0, text, "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    return git


def _tarball(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("oasdiff")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class OpenApiBreakingTest(unittest.TestCase):
    """openapi-breaking.py fails only on a breaking change or a deletion."""

    def test_unknown_base(self) -> None:
        """An empty or all-zero base compares nothing."""
        self.assertTrue(ob.unknown_base(""))
        self.assertTrue(ob.unknown_base("0" * 40))
        self.assertFalse(ob.unknown_base("a1b2c3"))

    def test_the_binary_is_checked_before_it_is_used(self) -> None:
        """A tarball whose checksum differs is refused."""
        archive = _tarball(b"#!/bin/sh\n")
        good = hashlib.sha256(archive).hexdigest()
        self.assertEqual(ob.verified_binary(archive, good), b"#!/bin/sh\n")
        with self.assertRaises(ValueError):
            ob.verified_binary(archive, "0" * 64)

    def test_a_missing_document_fails_and_a_new_one_passes(self) -> None:
        """Deleting a published document fails; one new since the base passes."""
        oasdiff = Path("/nonexistent/oasdiff")
        self.assertEqual(
            ob.check("abc", ["no/such/spec.json"], oasdiff, _git_showing("{}")), 1
        )
        new = (
            str((FIXTURES / "v1.json").relative_to(ob.ROOT))
            if FIXTURES.is_relative_to(ob.ROOT)
            else None
        )
        if new is not None:
            self.assertEqual(ob.check("abc", [new], oasdiff, _git_showing(None)), 0)


@unittest.skipUnless(os.environ.get("OASDIFF"), "set OASDIFF to an oasdiff binary")
class OasdiffTest(unittest.TestCase):
    """With oasdiff: an additive change passes, a breaking one fails."""

    def _run(self, head: str) -> int:
        oasdiff = Path(os.environ["OASDIFF"])
        base = (FIXTURES / "v1.json").read_text()
        spec = str((FIXTURES / head).relative_to(ob.ROOT))
        return int(ob.check("abc", [spec], oasdiff, _git_showing(base)))

    def test_unchanged_passes(self) -> None:
        """The same document passes."""
        self.assertEqual(self._run("v1.json"), 0)

    def test_additive_passes(self) -> None:
        """A new optional response property is not breaking."""
        self.assertEqual(self._run("additive.json"), 0)

    def test_breaking_fails(self) -> None:
        """Removing a required response property is breaking."""
        self.assertEqual(self._run("breaking.json"), 1)


if __name__ == "__main__":
    unittest.main()
