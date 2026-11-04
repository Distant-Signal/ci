"""Tests for scripts/gen-rust-dockerfiles.py.

uv run python -m unittest discover -s scripts/tests

Each test builds a small repository in a temporary directory (a workspace
with one service crate, its docker/services.toml and a hand-written
Dockerfile with empty markers) and runs the generator's main() on it.
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import override

SCRIPT = Path(__file__).resolve().parent.parent / "gen-rust-dockerfiles.py"
RUST_IMAGE = "1.90-bookworm@sha256:" + "a" * 64
RUNTIME_FROM = "FROM debian:bookworm-slim@sha256:" + "b" * 64 + " AS runtime"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_rust_dockerfiles", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_rust_dockerfiles"] = module
    spec.loader.exec_module(module)
    return module


gen = _load()

SERVICES_TOML = f"""\
inputs = ["Cargo.toml", "Cargo.lock", "crates"]
build-packages = ["libssl-dev", "pkg-config"]
rust-image = "{RUST_IMAGE}"
cargo-chef = "0.1.99"

[services]
hello = ["hello", "hello-admin"]
"""


def dockerfile(*copies: str) -> str:
    """Return a hand-written Dockerfile: header, empty markers, runtime stage."""
    lines = "".join(
        f"COPY --from=builder /usr/local/bin/{name} /usr/local/bin/{name}\n"
        for name in copies
    )
    return (
        "# syntax=docker/dockerfile:1\n"
        "# The hello service.\n"
        f"{gen.BEGIN}\n"
        f"{gen.END}\n"
        "\n"
        f"{RUNTIME_FROM}\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends ca-certificates \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"{lines}"
        'ENTRYPOINT ["/usr/local/bin/hello"]\n'
    )


class GeneratorTest(unittest.TestCase):
    """main() against a temporary repository."""

    @override
    def setUp(self) -> None:
        """Write the repository."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        files = {
            "Cargo.toml": '[workspace]\nresolver = "3"\nmembers = ["crates/hello"]\n',
            "Cargo.lock": "version = 4\n",
            "crates/hello/Cargo.toml": '[package]\nname = "hello"\nversion = "0.1.0"\n',
            "crates/hello/greeting.txt": "hello\n",
            "crates/hello/src/main.rs": (
                'const GREETING: &str = include_str!("../greeting.txt");\n'
                'fn main() {\n    print!("{GREETING}");\n}\n'
            ),
            "docker/services.toml": SERVICES_TOML,
            "docker/hello.Dockerfile": dockerfile("hello", "hello-admin"),
        }
        for name, text in files.items():
            self.write(name, text)

    def write(self, name: str, text: str) -> None:
        """Write one file of the repository."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, name: str) -> str:
        """Read one file of the repository."""
        return (self.root / name).read_text(encoding="utf-8")

    def run_main(self, *args: str) -> tuple[int, str]:
        """Run main() on the repository; return (status, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = gen.main(["--root", str(self.root), *args])
        return status, out.getvalue()

    def test_writes_then_checks_clean(self) -> None:
        """A first run writes the stages and the allowlist; --check then passes."""
        status, out = self.run_main()
        self.assertEqual(status, 0, out)
        self.assertIn("wrote docker/hello.Dockerfile", out)
        self.assertEqual(self.run_main("--check"), (0, ""))

    def test_the_stages_follow_the_config(self) -> None:
        """The rust image, cargo-chef, packages and --bin flags follow the config."""
        self.run_main()
        text = self.read("docker/hello.Dockerfile")
        self.assertIn(f"FROM rust:{RUST_IMAGE} AS chef", text)
        self.assertIn("cargo install cargo-chef --locked --version 0.1.99", text)
        self.assertIn(
            "apt-get install -y --no-install-recommends libssl-dev pkg-config", text
        )
        self.assertIn("--bin hello --bin hello-admin", text)
        self.assertIn(f"{RUNTIME_FROM}\n{gen.LICENCE_LABEL}\n", text)
        self.assertIn(f"{gen.APT_UPDATE}\n{gen.APT_UPGRADE}\n", text)
        self.assertTrue(
            text.startswith("# syntax=docker/dockerfile:1\n# The hello service.\n")
        )

    def test_the_allowlist_lets_in_only_the_inputs(self) -> None:
        """The .dockerignore ignores everything, then lets the inputs back in."""
        self.run_main()
        lines = self.read("docker/hello.Dockerfile.dockerignore").splitlines()
        self.assertIn("*", lines)
        for path in ("Cargo.toml", "Cargo.lock", "crates"):
            self.assertIn(f"!{path}", lines)
        self.assertNotIn("!docs", lines)

    def test_an_edit_inside_the_markers_is_stale(self) -> None:
        """--check reports a hand edit of the generated block and writes nothing."""
        self.run_main()
        edited = self.read("docker/hello.Dockerfile").replace(
            "--bin hello ", "--bin other ", 1
        )
        self.write("docker/hello.Dockerfile", edited)
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("stale: docker/hello.Dockerfile", out)
        self.assertEqual(self.read("docker/hello.Dockerfile"), edited)

    def test_services_and_dockerfiles_must_match(self) -> None:
        """A Dockerfile without a [services] entry, and the reverse, are errors."""
        self.write("docker/other.Dockerfile", dockerfile("other"))
        self.write("docker/services.toml", SERVICES_TOML + 'gone = ["gone"]\n')
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("docker/other.Dockerfile has no [services] entry", out)
        self.assertIn(
            "SERVICES has 'gone' but docker/gone.Dockerfile does not exist", out
        )

    def test_an_input_outside_the_allowlist_is_an_error(self) -> None:
        """An include_str! of a file the build context leaves out fails --check."""
        self.write("reference/data.csv", "a,b\n")
        self.write(
            "crates/hello/src/data.rs",
            'pub const DATA: &str = include_str!("../../../reference/data.csv");\n',
        )
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("reference/data.csv", out)
        self.write(
            "docker/services.toml",
            SERVICES_TOML.replace('"crates"]', '"crates", "reference"]'),
        )
        self.assertEqual(self.run_main()[0], 0)

    def test_the_runtime_copies_must_match_the_binaries(self) -> None:
        """A binary built but not copied, or copied but not built, is an error."""
        self.write("docker/hello.Dockerfile", dockerfile("hello", "stray"))
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("copies 'stray', which SERVICES does not build", out)
        self.assertIn("builds 'hello-admin' but never copies it", out)

    def test_the_last_stage_must_be_the_runtime(self) -> None:
        """The weekly rebuild names the runtime stage, so it must be last."""
        text = dockerfile("hello", "hello-admin").replace(" AS runtime", " AS final")
        self.write("docker/hello.Dockerfile", text)
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("the last stage must be `AS runtime`", out)

    def test_no_runtime_stage_copies_the_line_catalogue(self) -> None:
        """ds-lines embeds lines/: a COPY of it fails --check."""
        text = dockerfile("hello", "hello-admin").replace(
            "ENTRYPOINT", "COPY lines/ /app/lines/\nENTRYPOINT"
        )
        self.write("docker/hello.Dockerfile", text)
        status, out = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn("docker/hello.Dockerfile", out)
        self.assertIn("lines", out)


if __name__ == "__main__":
    unittest.main()
