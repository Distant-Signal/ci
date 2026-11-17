"""Tests for scripts/check-deny-ignores.py.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import datetime as dt
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPT = Path(__file__).resolve().parent.parent / "check-deny-ignores.py"
TODAY = dt.date(2026, 11, 17)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_deny_ignores", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_deny_ignores"] = module
    spec.loader.exec_module(module)
    return module


deny = _load()

GOOD = """\
[advisories]
version = 2
ignore = [
    # rsa: no private-key use.
    # expires 2027-05-17
    { id = "RUSTSEC-2023-0071", reason = "no RSA private-key operations" },

    { id = "RUSTSEC-2026-0192", reason = "sandboxed" }, # expires 2027-02-17
]

[licenses]
ignore = [
    { id = "other section" },
]
"""


def run(text: str, today: dt.date = TODAY) -> list[Any]:
    """Return the findings for a deny.toml's text."""
    return list(deny.check(text, "deny.toml", today))


class CheckTest(unittest.TestCase):
    """Each ignore needs a reason and a live expiry."""

    def test_good_file_passes(self) -> None:
        """Dated entries with reasons pass; other sections are ignored."""
        self.assertEqual(run(GOOD), [])

    def test_no_ignores_passes(self) -> None:
        """A deny.toml without an ignore list has nothing to check."""
        self.assertEqual(run("[advisories]\nversion = 2\n"), [])

    def test_missing_expiry_fails(self) -> None:
        """A reason alone isn't enough."""
        found = run(GOOD.replace("    # expires 2027-05-17\n", ""))
        self.assertEqual(len(found), 1)
        self.assertIn("no expiry", found[0].problem)
        self.assertTrue(found[0].error)

    def test_comment_block_is_per_entry(self) -> None:
        """A date above one entry doesn't cover the next one."""
        text = GOOD.replace("    # expires 2027-05-17\n", "").replace(
            "[advisories]\nversion = 2\nignore = [\n",
            "[advisories]\nversion = 2\nignore = [\n    # expires 2027-05-17\n"
            '    { id = "A", reason = "r" },\n',
        )
        found = run(text)
        self.assertEqual([f.label for f in found], ["RUSTSEC-2023-0071"])

    def test_missing_reason_fails(self) -> None:
        """An empty or absent reason fails; so does the bare-string form."""
        for entry in (
            '{ id = "RUSTSEC-2023-0071", reason = "  " }, # expires 2027-05-17',
            '{ id = "RUSTSEC-2023-0071" }, # expires 2027-05-17',
            '"RUSTSEC-2023-0071", # expires 2027-05-17',
        ):
            with self.subTest(entry=entry):
                text = f"[advisories]\nignore = [\n    {entry}\n]\n"
                self.assertTrue(any(f.error for f in run(text)))

    def test_expired_fails(self) -> None:
        """A date in the past fails; the day itself is still live."""
        expired = run(GOOD, dt.date(2027, 3, 1))
        self.assertTrue(any("expired" in f.problem for f in expired))
        on_day = run(GOOD, dt.date(2027, 2, 17))
        self.assertFalse(any(f.error for f in on_day))

    def test_soon_warns_without_failing(self) -> None:
        """Within the warning window it is an annotation, not an error."""
        found = run(GOOD, dt.date(2027, 2, 10))
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].error)
        self.assertTrue(found[0].render().startswith("::warning "))

    def test_invalid_date_fails(self) -> None:
        """A date that isn't a calendar date fails."""
        found = run(GOOD.replace("2027-05-17", "2027-13-45"))
        self.assertTrue(any("invalid" in f.problem for f in found))

    def test_hash_inside_a_reason_is_not_a_comment(self) -> None:
        """A `#` in a string doesn't start a comment."""
        text = (
            "[advisories]\nignore = [\n    # expires 2027-05-17\n"
            '    { id = "A", reason = "see #12" },\n]\n'
        )
        self.assertEqual(run(text), [])


class MainTest(unittest.TestCase):
    """main() over a file."""

    def main(self, text: str) -> tuple[int, str]:
        """Run the script on a temporary deny.toml, returning status and output."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deny.toml"
            path.write_text(text, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = deny.main([str(path), "--today", "2026-11-17"])
        return status, out.getvalue()

    def test_clean(self) -> None:
        """A good file exits 0."""
        status, out = self.main(GOOD)
        self.assertEqual(status, 0)
        self.assertIn("live expiry", out)

    def test_findings_exit_1(self) -> None:
        """A bad file exits 1 with an error annotation."""
        status, out = self.main(GOOD.replace("# expires 2027-05-17", "# nope"))
        self.assertEqual(status, 1)
        self.assertIn("::error file=", out)

    def test_unreadable_exits_2(self) -> None:
        """Invalid TOML or a missing file is exit 2."""
        status, _ = self.main("[advisories\n")
        self.assertEqual(status, 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(deny.main(["/nonexistent/deny.toml"]), 2)


if __name__ == "__main__":
    unittest.main()
