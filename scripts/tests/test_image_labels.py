"""Tests for scripts/image-labels.py.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "image-labels.py"
SOURCE = "https://github.com/Distant-Signal/example"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_labels", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["image_labels"] = module
    spec.loader.exec_module(module)
    return module


labels = _load()


class ParseExtraTest(unittest.TestCase):
    """The caller's extra labels."""

    def test_none(self) -> None:
        """No input, or only blank lines, is no labels."""
        self.assertEqual(labels.parse_extra(""), [])
        self.assertEqual(labels.parse_extra("\n  \n"), [])

    def test_keeps_order_and_trims(self) -> None:
        """Lines are trimmed and kept in order; `=` may appear in a value."""
        text = (
            "  org.distant-signal.core-version = v1.2.3 \n\n"
            "org.distant-signal.api-build=build.12-abc1234\n"
            "org.example.query=a=b\n"
        )
        self.assertEqual(
            labels.parse_extra(text),
            [
                ("org.distant-signal.core-version", "v1.2.3"),
                ("org.distant-signal.api-build", "build.12-abc1234"),
                ("org.example.query", "a=b"),
            ],
        )

    def test_refuses(self) -> None:
        """Every bad line is named, with its line number."""
        bad = {
            "no-equals": "is not key=value",
            "Org.Example=x": "is not a lower-case dotted key",
            "org..example=x": "is not a lower-case dotted key",
            "org.example=": "has an empty value",
            "org.opencontainers.image.licenses=MIT": "image.yml's own label",
            "org.opencontainers.image.source=https://x": "image.yml's own label",
        }
        for line, why in bad.items():
            with (
                self.subTest(line=line),
                self.assertRaisesRegex(labels.LabelError, f"line 1: .*{why}"),
            ):
                labels.parse_extra(line)

    def test_refuses_a_repeated_key(self) -> None:
        """A key set twice is refused, not silently overridden."""
        with self.assertRaisesRegex(labels.LabelError, "line 2: org.a is set twice"):
            labels.parse_extra("org.a=1\norg.a=2")

    def test_reports_every_problem(self) -> None:
        """All the bad lines at once, not only the first."""
        with self.assertRaises(labels.LabelError) as caught:
            labels.parse_extra("bad\norg.ok=1\nalso bad")
        self.assertEqual(len(str(caught.exception).splitlines()), 2)


class MainTest(unittest.TestCase):
    """The CLI image.yml runs."""

    def run_main(self, extra: str | None) -> tuple[int, str, str]:
        """Run main with EXTRA in a file; return status, stdout, $GITHUB_OUTPUT."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            argv = ["--title", "ds/api", "--source", SOURCE, "--licenses", "AGPL"]
            if extra is not None:
                extra_file = Path(tmp) / "extra"
                extra_file.write_text(extra, encoding="utf-8")
                argv += ["--extra-file", str(extra_file)]
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
                contextlib.redirect_stdout(stdout),
            ):
                status = labels.main(argv)
            written = output.read_text("utf-8") if output.exists() else ""
        return status, stdout.getvalue(), written

    def test_own_labels_only(self) -> None:
        """Without extras, image.yml's four labels."""
        status, out, written = self.run_main(None)
        self.assertEqual(status, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "org.opencontainers.image.title=ds/api",
                f"org.opencontainers.image.source={SOURCE}",
                f"org.opencontainers.image.url={SOURCE}",
                "org.opencontainers.image.licenses=AGPL",
            ],
        )
        self.assertTrue(written.startswith(f"labels<<{labels.DELIMITER}\n"))
        self.assertTrue(written.endswith(f"\n{labels.DELIMITER}\n"))

    def test_extras_follow(self) -> None:
        """The extra labels come after image.yml's own, in the output too."""
        status, out, written = self.run_main("org.distant-signal.core-version=v0.1.0\n")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines()[-1], "org.distant-signal.core-version=v0.1.0")
        self.assertIn("\norg.distant-signal.core-version=v0.1.0\n", written)

    def test_refused_extra(self) -> None:
        """A bad extra label exits 2 with an ::error:: line and no output."""
        status, out, written = self.run_main("org.opencontainers.image.title=x")
        self.assertEqual(status, 2)
        self.assertIn("::error::labels input, line 1:", out)
        self.assertEqual(written, "")


if __name__ == "__main__":
    unittest.main()
