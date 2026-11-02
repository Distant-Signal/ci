"""Tests for scripts/check-bench-gates.py, on inline Rust snippets.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "check-bench-gates.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_bench_gates", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_bench_gates"] = module
    spec.loader.exec_module(module)
    return module


gates = _load()


def names(source: str) -> list[str]:
    """Return the names of the ungated benchmark tests in `source`."""
    return [f.name for f in gates.check_source(Path("x.rs"), source)]


class ReasonTests(unittest.TestCase):
    """Which ignore reasons mark a benchmark."""

    def test_benchmark_reasons(self) -> None:
        """Reasons naming a benchmark or a release build count."""
        for reason in (
            "benchmark; see the module doc",
            "bench: run by hand",
            "run with cargo test --release -p api",
            "needs a release build",
            "Benchmarks, release mode only",
        ):
            with self.subTest(reason=reason):
                source = (
                    f'#[test]\n#[ignore = "{reason}"]\nfn b() {{\n    work();\n}}\n'
                )
                self.assertEqual(names(source), ["b"])

    def test_other_ignored_tests_are_not_benchmarks(self) -> None:
        """DB and Redis reasons do not, even naming a `release` filter."""
        for reason in (
            "requires a live database (DATABASE_URL)",
            "run with `cargo test -p api delete_journey_releases -- --ignored`",
            "needs REDIS_URL",
        ):
            with self.subTest(reason=reason):
                source = (
                    f'#[test]\n#[ignore = "{reason}"]\nfn t() {{\n    work();\n}}\n'
                )
                self.assertEqual(names(source), [])

    def test_a_plain_ignore_is_not_a_benchmark(self) -> None:
        """An ignore with no reason is not checked."""
        self.assertEqual(
            names("#[test]\n#[ignore]\nfn bench_x() {\n    work();\n}\n"), []
        )


class GateTests(unittest.TestCase):
    """Which first statements count as an env gate."""

    def bench(self, body: str, extra: str = "") -> list[str]:
        """Names of ungated benchmarks for one test with `body`."""
        return names(
            f"{extra}\n#[tokio::test]\n"
            '#[ignore = "benchmark; see its doc comment"]\n'
            '#[expect(clippy::too_many_lines, reason = "one table")]\n'
            f"async fn bench_it() {{\n{body}\n}}\n"
        )

    def test_accepted_gates(self) -> None:
        """Each accepted gate form, comments before it allowed."""
        for body in (
            'if std::env::var("TRIP_PLAN_ACROSS_BENCH").is_err() { return; }\nwork();',
            'if std::env::var_os("DS_RUN_X").is_none() {\n    return;\n}\nwork();',
            'if env::var("X").is_err() { return }\nwork();',
            (
                'let Ok(raw) = std::env::var("TRIP_PLAN_BENCH_NOW") else {\n'
                "    return;\n};\nwork(raw);"
            ),
            'let Some(v) = std::env::var_os("X") else { return; };\nwork(v);',
            '// a comment first\nif std::env::var("X").is_err() { return; }\nwork();',
            # `use` items before the gate run nothing.
            'use a::{b, c};\nuse d::e;\n\nif std::env::var("X").is_err() { return; }',
        ):
            with self.subTest(body=body):
                self.assertEqual(self.bench(body), [])

    def test_a_helper_reading_the_environment(self) -> None:
        """`if !helper() { return; }`, the helper reading the environment."""
        helper = (
            "fn bench_requested() -> bool {\n"
            '    std::env::var_os("DS_RUN_BUDGET_BENCH").is_some()\n}\n'
        )
        self.assertEqual(
            self.bench("if !bench_requested() {\n    return;\n}\nwork();", helper),
            [],
        )

    def test_a_helper_that_reads_no_environment(self) -> None:
        """A helper that reads no environment is no gate."""
        helper = "fn bench_requested() -> bool {\n    true\n}\n"
        self.assertEqual(
            self.bench("if !bench_requested() { return; }\nwork();", helper),
            ["bench_it"],
        )

    def test_an_unknown_helper(self) -> None:
        """A helper not defined in the file is no gate."""
        self.assertEqual(
            self.bench("if !elsewhere() { return; }\nwork();"), ["bench_it"]
        )

    def test_rejected(self) -> None:
        """Bodies that run the benchmark ungated."""
        for body in (
            "work();",
            # The gate must come first: work before it already ran.
            'let reps = 3;\nif std::env::var("X").is_err() { return; }\nwork();',
            # Reading a variable is not a gate.
            'let reps = std::env::var("DS_BENCH_REPS").ok();\nwork(reps);',
            # A gate that does not return.
            'if std::env::var("X").is_err() { println!("skipped"); }\nwork();',
            # A commented-out gate.
            '// if std::env::var("X").is_err() { return; }\nwork();',
        ):
            with self.subTest(body=body):
                self.assertEqual(self.bench(body), ["bench_it"])


class MainTests(unittest.TestCase):
    """main() over files and directories."""

    def test_findings_and_exit_status(self) -> None:
        """One finding per ungated test; exit 1, or 0 when clean."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "nested" / "bad.rs").write_text(
                '#[test]\n#[ignore = "benchmark"]\nfn slow() {\n    work();\n}\n',
                encoding="utf-8",
            )
            (root / "good.rs").write_text(
                '#[test]\n#[ignore = "benchmark"]\nfn fine() {\n'
                '    if std::env::var("X").is_err() { return; }\n}\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            with redirect_stdout(out):
                status = gates.main([str(root)])
            self.assertEqual(status, 1)
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("bad.rs,line=2::", lines[0])
            self.assertIn("`slow`", lines[0])

            out = io.StringIO()
            with redirect_stdout(out):
                status = gates.main([str(root / "good.rs")])
            self.assertEqual((status, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
