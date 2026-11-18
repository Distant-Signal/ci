"""Tests for scripts/helm-push.py, with a fake registry behind `helm`.

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import override
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "helm-push.py"
REGISTRY = "oci://ghcr.io/distant-signal/charts"
NAME = "ds-ingest-bucket"
VERSION = "0.4.0+build.106.sha.c27f0c1"
TAG = "0.4.0_build.106.sha.c27f0c1"
REPOSITORY = f"ghcr.io/distant-signal/charts/{NAME}"
# What GHCR's read-after-write miss looked like (ds-deploy run 44161402190).
RACE = (
    'Error: failed to perform "Tag" on destination: '
    "sha256:ef4984e298ae86a18c8ed1a6e12fbfcebe6196ed8033bb645f20d2a37216f241: "
    "not found\n"
)
NOT_FOUND = (
    'Error: failed to perform "FetchReference" on source: '
    f"{REPOSITORY}:{TAG}: not found\n"
)
CHART = b"this chart, packaged"
OTHER = b"a chart packaged at another time"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("helm_push", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["helm_push"] = module
    spec.loader.exec_module(module)
    return module


push = _load()


@dataclass(frozen=True)
class Result:
    """What the script reads of a finished command (its own Result's shape)."""

    status: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        """Its stdout and stderr together."""
        return self.stdout + self.stderr


def manifest_digest(content: bytes) -> str:
    """Return a stand-in manifest digest for CONTENT."""
    return "sha256:" + hashlib.sha256(b"manifest" + content).hexdigest()


class FakeRegistry:
    """`helm pull` and `helm push` against one tag; PLAN scripts each push.

    A plan step is "ok", "race" (the manifest lands, the tag doesn't, and
    push fails as in the incident), "lag" (the push succeeds but the tag
    can't be read back yet), or ("fail", status, stderr).
    """

    def __init__(
        self,
        plan: Sequence[str | tuple[str, int, str]],
        held: bytes | None = None,
        *,
        overwrite_after_push: bytes | None = None,
    ) -> None:
        """Start with HELD under the tag, if any."""
        self.plan = list(plan)
        self.held = held
        self.overwrite_after_push = overwrite_after_push
        self.calls: list[list[str]] = []
        self.pending: bytes | None = None
        self.pull_fails: str | None = None

    def __call__(self, argv: Sequence[str]) -> "Result":
        """Answer a helm command."""
        self.calls.append(list(argv))
        if argv[1] == "pull":
            return self.pull(argv)
        if argv[1] != "push":
            msg = f"unexpected helm command {argv!r}"
            raise ValueError(msg)
        content = Path(argv[2]).read_bytes()
        step = self.plan.pop(0)
        digest = manifest_digest(content)
        if step == "ok":
            self.held = self.overwrite_after_push or content
            return Result(0, f"Pushed: {REPOSITORY}:{TAG}\nDigest: {digest}\n", "")
        if step == "lag":
            self.pending = content
            return Result(0, f"Pushed: {REPOSITORY}:{TAG}\nDigest: {digest}\n", "")
        if step == "race":
            return Result(1, "", RACE)
        if not isinstance(step, tuple):
            msg = f"unknown plan step {step!r}"
            raise TypeError(msg)
        return Result(step[1], "", step[2])

    def pull(self, argv: Sequence[str]) -> "Result":
        """Pull the tag into --destination, as helm does."""
        if self.pull_fails is not None:
            return Result(1, "", self.pull_fails)
        if self.pending is not None:
            # The tag shows up one read after the push.
            self.held, self.pending = self.pending, None
            return Result(1, "", NOT_FOUND)
        if self.held is None:
            return Result(1, "", NOT_FOUND)
        destination = Path(argv[argv.index("--destination") + 1])
        (destination / f"{NAME}-{VERSION}.tgz").write_bytes(self.held)
        return Result(
            0, f"Pulled: {REPOSITORY}:{TAG}\nDigest: {manifest_digest(self.held)}\n", ""
        )

    def pushes(self) -> int:
        """How many pushes were attempted."""
        return sum(1 for call in self.calls if call[1] == "push")


class PushTest(unittest.TestCase):
    """The step's decisions, each against a FakeRegistry."""

    @override
    def setUp(self) -> None:
        """Write a chart and point the step's files at a temporary directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.chart = self.dir / f"{NAME}-{VERSION}.tgz"
        self.chart.write_bytes(CHART)
        self.output = self.dir / "output"
        self.summary = self.dir / "summary"
        env = {
            "HELM": "helm",
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps: list[float] = []
        self.out = ""

    def run_push(self, registry: FakeRegistry, *extra: str) -> int:
        """Run main against REGISTRY; keep stdout in self.out."""
        argv = [
            "--chart",
            str(self.chart),
            "--registry",
            REGISTRY,
            "--name",
            NAME,
            "--version",
            VERSION,
            *extra,
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status: int = push.main(argv, runner=registry, sleep=self.sleeps.append)
        self.out = out.getvalue()
        return status

    def outputs(self) -> dict[str, str]:
        """Return what the step wrote to $GITHUB_OUTPUT."""
        if not self.output.exists():
            return {}
        pairs = (line.split("=", 1) for line in self.output.read_text().splitlines())
        return dict(pairs)

    def summary_text(self) -> str:
        """Return what the step wrote to $GITHUB_STEP_SUMMARY."""
        return self.summary.read_text() if self.summary.exists() else ""

    def test_clean_push_reports_the_registrys_digest(self) -> None:
        """A first push that lands needs no retry and no summary."""
        registry = FakeRegistry(["ok"])
        self.assertEqual(self.run_push(registry), 0)
        self.assertEqual(
            self.outputs(), {"ref": REPOSITORY, "digest": manifest_digest(CHART)}
        )
        self.assertEqual(self.sleeps, [])
        self.assertEqual(registry.pushes(), 1)
        self.assertNotIn("::warning", self.out)
        self.assertEqual(self.summary_text(), "")

    def test_the_incident_is_retried_and_passes(self) -> None:
        """The tag miss after the push is retried once, visibly."""
        registry = FakeRegistry(["race", "ok"])
        self.assertEqual(self.run_push(registry), 0)
        self.assertEqual(self.outputs()["digest"], manifest_digest(CHART))
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(registry.pushes(), 2)
        self.assertIn("::warning title=helm push::", self.out)
        self.assertIn("retrying in 5s", self.out)
        self.assertIn("| 1 |", self.summary_text())
        self.assertIn("pushed", self.summary_text())

    def test_a_tag_that_appears_after_a_failed_push_is_not_pushed_again(self) -> None:
        """When the chart is under the tag at the next look, it counts."""
        registry = FakeRegistry(["race"])
        registry.pending = CHART
        # The first look (before the push) is the lagging one: the tag is
        # absent then, and present at the second look.
        self.assertEqual(self.run_push(registry), 0)
        self.assertEqual(registry.pushes(), 1)
        self.assertIn("already holds this chart", self.out)
        self.assertEqual(self.outputs()["digest"], manifest_digest(CHART))

    def test_a_push_that_lands_but_reads_back_late_is_confirmed(self) -> None:
        """A push that reported success, with the tag not readable yet, retries."""
        registry = FakeRegistry(["lag"])
        self.assertEqual(self.run_push(registry), 0)
        self.assertEqual(registry.pushes(), 1)
        self.assertEqual(self.sleeps, [5])
        self.assertIn("not readable back", self.out)

    def test_the_same_chart_already_there_is_success_without_a_push(self) -> None:
        """Idempotent: the tag already holds this package."""
        registry = FakeRegistry([], held=CHART)
        self.assertEqual(self.run_push(registry), 0)
        self.assertEqual(registry.pushes(), 0)
        self.assertEqual(self.outputs()["digest"], manifest_digest(CHART))

    def test_a_different_chart_under_the_tag_fails_at_once(self) -> None:
        """Immutability: never push over a different chart."""
        registry = FakeRegistry(["ok"], held=OTHER)
        self.assertEqual(self.run_push(registry), 1)
        self.assertEqual(registry.pushes(), 0)
        self.assertEqual(self.outputs(), {})
        self.assertIn("::error::", self.out)
        self.assertIn("immutable", self.out)
        self.assertIn(manifest_digest(OTHER), self.out)
        self.assertEqual(self.sleeps, [])

    def test_a_different_chart_after_the_push_fails(self) -> None:
        """A racing writer's chart under the tag is not ours to sign."""
        registry = FakeRegistry(["ok"], overwrite_after_push=OTHER)
        self.assertEqual(self.run_push(registry), 1)
        self.assertEqual(self.outputs(), {})
        self.assertIn("immutable", self.out)

    def test_authentication_failures_are_final(self) -> None:
        """Not retried: one push, one failure."""
        for stderr in (
            "Error: unauthorized: authentication required\n",
            "Error: denied: permission_denied: write_package\n",
            "Error: unexpected status 403 Forbidden\n",
        ):
            with self.subTest(stderr=stderr):
                registry = FakeRegistry([("fail", 1, stderr)])
                self.assertEqual(self.run_push(registry), 1)
                self.assertEqual(registry.pushes(), 1)
                self.assertEqual(self.outputs(), {})
        self.assertEqual(self.sleeps, [])

    def test_unrecognised_errors_are_final(self) -> None:
        """Content errors and anything unknown fail without a retry."""
        registry = FakeRegistry([("fail", 1, "Error: manifest invalid\n")])
        self.assertEqual(self.run_push(registry), 1)
        self.assertEqual(registry.pushes(), 1)
        self.assertIn("not a registry fault", self.out)

    def test_transport_errors_are_retried_with_backoff(self) -> None:
        """5xx, timeouts and resets retry on 5, 15 and 30 seconds."""
        registry = FakeRegistry(
            [
                ("fail", 1, "Error: unexpected status 502 Bad Gateway\n"),
                ("fail", 1, "Error: Post ...: dial tcp: i/o timeout\n"),
                ("fail", 1, "Error: read: connection reset by peer\n"),
            ]
        )
        self.assertEqual(self.run_push(registry), 1)
        self.assertEqual(registry.pushes(), 3)
        self.assertEqual(self.sleeps, [5, 15])
        self.assertIn("after 3 attempts", self.out)
        self.assertIn("out of attempts", self.summary_text())

    def test_backoff_and_attempts_are_inputs(self) -> None:
        """The last backoff repeats when the attempts outnumber it."""
        registry = FakeRegistry([("fail", 1, RACE)] * 4)
        status = self.run_push(registry, "--attempts", "4", "--backoff", "1,2")
        self.assertEqual(status, 1)
        self.assertEqual(self.sleeps, [1, 2, 2])

    def test_a_registry_that_cannot_be_read_does_not_block_the_push(self) -> None:
        """An unreadable tag (not a 404) is noted, and the push goes ahead."""
        registry = FakeRegistry(["ok"])
        registry.pull_fails = "Error: Get https://ghcr.io/v2/: i/o timeout\n"
        # The push works, but nothing can confirm it: out of attempts.
        registry.plan = ["ok", "ok", "ok"]
        self.assertEqual(self.run_push(registry), 1)
        self.assertEqual(registry.pushes(), 3)
        self.assertIn("could not read the tag", self.out)
        self.assertEqual(self.outputs(), {})

    def test_the_pushed_digest_must_match_the_registrys(self) -> None:
        """Helm push's digest and the tag's digest disagreeing is fatal."""
        registry = FakeRegistry(["ok"])
        original = registry.__call__

        def lying(argv: Sequence[str]) -> "Result":
            result: Result = original(argv)
            if argv[1] == "push":
                return Result(0, "Digest: sha256:" + "0" * 64 + "\n", "")
            return result

        with contextlib.redirect_stdout(io.StringIO()):
            status = push.publish(
                self.chart,
                registry=REGISTRY,
                name=NAME,
                version=VERSION,
                runner=lying,
                sleep=self.sleeps.append,
            )
        self.assertEqual(status, 1)
        self.assertEqual(self.outputs(), {})

    def test_bad_inputs(self) -> None:
        """A missing chart, no attempts or a bad backoff is a usage error."""
        self.chart.unlink()
        self.assertEqual(self.run_push(FakeRegistry([])), 2)
        self.chart.write_bytes(CHART)
        self.assertEqual(self.run_push(FakeRegistry([]), "--attempts", "0"), 2)
        self.assertEqual(self.run_push(FakeRegistry([]), "--backoff", "x"), 2)


class ClassifyTest(unittest.TestCase):
    """Which failures are the registry's."""

    def test_classify(self) -> None:
        """Transport faults are transient; credentials and the unknown, final."""
        transient = [
            RACE,
            "Error: unexpected status code 503 Service Unavailable",
            'Error: Get "https://ghcr.io/v2/": net/http: TLS handshake timeout',
            "Error: unexpected EOF",
            "Error: 429 Too Many Requests",
            "dial tcp: lookup ghcr.io: no such host",
        ]
        final = [
            "Error: unauthorized: authentication required",
            "Error: denied: installation not allowed to Write organization package",
            "Error: 401 Unauthorized, package not found",
            "Error: MANIFEST_INVALID: manifest invalid",
            "Error: chart.tgz: no such file or directory",
            "",
        ]
        for text in transient:
            with self.subTest(text=text):
                self.assertEqual(push.classify(text), "transient")
        for text in final:
            with self.subTest(text=text):
                self.assertEqual(push.classify(text), "final")


if __name__ == "__main__":
    unittest.main()
