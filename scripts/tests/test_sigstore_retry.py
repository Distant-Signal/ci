"""Tests for scripts/sigstore-retry.py, with fake cosign and gh binaries.

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
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import override
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "sigstore-retry.py"
REF = "ghcr.io/distant-signal/images/poller-stations@sha256:" + "a" * 64
IDENTITY = (
    "https://github.com/Distant-Signal/ci/.github/workflows/image.yml@" + "b" * 40
)
DIGEST = "sha256:" + "a" * 64


def payload(kind: str, digest: str = DIGEST) -> dict[str, object]:
    """One entry of cosign verify's JSON output."""
    return {
        "critical": {
            "identity": {"docker-reference": REF},
            "image": {"docker-manifest-digest": digest},
            "type": kind,
        },
        "optional": {},
    }


SIGNATURE_TYPE = "https://sigstore.dev/cosign/sign/v1"
SIGNATURE = payload(SIGNATURE_TYPE)
PROVENANCE = payload("https://slsa.dev/provenance/v1")
# What cosign verify printed for a signed and attested DS image
# (2026-11-04): the signature and the provenance attestation.
SIGNED = json.dumps([SIGNATURE, PROVENANCE]) + "\n"
Step = tuple[str, int, str] | tuple[str, int, str, str]
CONFLICT = (
    "Error: signing [ref]: [POST /api/v1/log/entries][409] "
    "createLogEntryConflict {...}\n"
)

# A stand-in for cosign or gh: each run takes the next step of the plan in
# $FAKE_PLAN (a JSON list of [expected subcommand, exit status, stderr,
# stdout]),
# logs its argv to $FAKE_LOG and fails loudly when the call is unexpected.
FAKE = """\
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
calls = log.read_text().splitlines() if log.exists() else []
plan = json.loads(Path(os.environ["FAKE_PLAN"]).read_text())
with log.open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
if len(calls) >= len(plan):
    sys.stderr.write("unplanned call\\n")
    sys.exit(99)
command, status, stderr, stdout = plan[len(calls)]
if sys.argv[1] != command:
    sys.stderr.write(f"expected {command}, got {sys.argv[1]}\\n")
    sys.exit(98)
sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(status)
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sigstore_retry", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sigstore_retry"] = module
    spec.loader.exec_module(module)
    return module


retry = _load()


class FakeTest(unittest.TestCase):
    """A fake binary on $COSIGN and $GH, a recorded sleep and $GITHUB_OUTPUT."""

    @override
    def setUp(self) -> None:
        """Write the fake and point the environment at it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        fake = self.dir / "fake"
        fake.write_text(f"#!{sys.executable}\n{FAKE}")
        fake.chmod(0o755)
        self.log = self.dir / "log"
        self.plan = self.dir / "plan.json"
        self.output = self.dir / "github-output"
        env = {
            "COSIGN": str(fake),
            "GH": str(fake),
            "FAKE_LOG": str(self.log),
            "FAKE_PLAN": str(self.plan),
            "GITHUB_OUTPUT": str(self.output),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps: list[float] = []

    def run_main(self, plan: Sequence[Step], *args: str) -> int:
        """Run main with PLAN for the fake; keep stdout in self.out.

        A step without stdout prints SIGNED when it is a passing verify.
        """
        full: list[list[str | int]] = []
        for step in plan:
            if len(step) == 4:
                full.append(list(step))
            else:
                passing_verify = step[0] == "verify" and step[1] == 0
                full.append([*step, SIGNED if passing_verify else ""])
        self.plan.write_text(json.dumps(full))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            status: int = retry.main(list(args), sleep=self.sleeps.append)
        self.out = out.getvalue()
        return status

    def calls(self) -> list[list[str]]:
        """Each call's argv after the binary."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class SignTest(FakeTest):
    """`sign`: retries, and a failed sign passes only on a verified signature."""

    def sign(self, plan: Sequence[Step], *extra: str) -> int:
        """Sign REF with three attempts and a 10 s backoff."""
        return self.run_main(
            plan, "sign", "--ref", REF, "--attempts", "3", "--backoff", "10", *extra
        )

    def test_signs_and_verifies(self) -> None:
        """A good sign is verified against the identity and issuer."""
        status = self.sign([("sign", 0, ""), ("verify", 0, "")], "--identity", IDENTITY)
        self.assertEqual(status, 0)
        self.assertEqual(
            self.calls(),
            [
                ["sign", "--yes", REF],
                [
                    "verify",
                    REF,
                    "--certificate-oidc-issuer",
                    "https://token.actions.githubusercontent.com",
                    "--certificate-identity",
                    IDENTITY,
                ],
            ],
        )
        self.assertEqual(self.sleeps, [])

    def test_identity_regexp(self) -> None:
        """--identity-regexp verifies with cosign's regexp flag."""
        status = self.sign(
            [("sign", 0, ""), ("verify", 0, "")], "--identity-regexp", "^https://x/"
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            self.calls()[1][-2:], ["--certificate-identity-regexp", "^https://x/"]
        )

    def test_signed_but_unverified_fails(self) -> None:
        """A sign that succeeds but doesn't verify fails, without retrying."""
        status = self.sign(
            [("sign", 0, ""), ("verify", 1, "no match")], "--identity", IDENTITY
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("does not verify", self.out)

    def test_conflict_with_landed_signature_passes(self) -> None:
        """The 2026-11-04 failure: a 409 whose signature landed passes at once."""
        status = self.sign(
            [("sign", 1, CONFLICT), ("verify", 0, "")], "--identity", IDENTITY
        )
        self.assertEqual(status, 0)
        self.assertEqual([call[0] for call in self.calls()], ["sign", "verify"])
        self.assertIn("::notice::", self.out)
        self.assertIn("Rekor conflict", self.out)
        self.assertEqual(self.sleeps, [])

    def test_failure_without_signature_retries(self) -> None:
        """A failed sign with nothing to verify is retried, with backoff."""
        plan = [
            ("sign", 1, CONFLICT),
            ("verify", 1, "no signatures found"),
            ("sign", 1, "timeout"),
            ("verify", 1, "no signatures found"),
            ("sign", 0, ""),
            ("verify", 0, ""),
        ]
        self.assertEqual(self.sign(plan, "--identity", IDENTITY), 0)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(self.sleeps, [10, 20])

    def test_every_attempt_fails(self) -> None:
        """Three failures, none verifying, fail the step."""
        plan = [("sign", 1, CONFLICT), ("verify", 1, "none")] * 3
        self.assertEqual(self.sign(plan, "--identity", IDENTITY), 1)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(self.sleeps, [10, 20])
        self.assertIn("::error::cosign sign failed 3 times, and no signature", self.out)

    def test_no_identity_only_retries(self) -> None:
        """Without an identity nothing is verified: a failure is never a pass."""
        plan = [("sign", 1, CONFLICT), ("sign", 1, CONFLICT), ("sign", 0, "")]
        self.assertEqual(self.sign(plan), 0)
        self.assertEqual([call[0] for call in self.calls()], ["sign"] * 3)
        plan = [("sign", 1, CONFLICT)] * 3
        self.log.unlink()
        self.assertEqual(self.sign(plan), 1)
        self.assertEqual([call[0] for call in self.calls()], ["sign"] * 3)

    def test_attestation_is_not_a_signature(self) -> None:
        """A verify that passes only on the provenance attestation never passes.

        cosign verify reports the attestation too, with exit status 0.
        """
        only_provenance = json.dumps([PROVENANCE]) + "\n"
        other_digest = (
            json.dumps([payload(SIGNATURE_TYPE, "sha256:" + "c" * 64)]) + "\n"
        )
        plan: list[Step] = [
            ("sign", 1, CONFLICT),
            ("verify", 0, "", only_provenance),
            ("sign", 1, CONFLICT),
            ("verify", 0, "", other_digest),
        ]
        self.assertEqual(self.sign(plan, "--attempts", "2", "--identity", IDENTITY), 1)
        self.assertEqual(len(self.calls()), 4)
        plan = [("sign", 0, ""), ("verify", 0, "", only_provenance)]
        self.log.unlink()
        self.assertEqual(self.sign(plan, "--identity", IDENTITY), 1)

    def test_default_schedule(self) -> None:
        """Five attempts by default, backing off 15, 30, 60 and 120 s."""
        plan = [("sign", 1, CONFLICT), ("verify", 1, "none")] * 5
        status = self.run_main(plan, "sign", "--ref", REF, "--identity", IDENTITY)
        self.assertEqual(status, 1)
        self.assertEqual(len(self.calls()), 10)
        self.assertEqual(self.sleeps, [15, 30, 60, 120])

    def test_refuses_a_tag(self) -> None:
        """Only a digest is signed; nothing runs for a tag."""
        status = self.run_main([], "sign", "--ref", "ghcr.io/x/y:latest")
        self.assertEqual(status, 2)
        self.assertEqual(self.calls(), [])

    def test_refuses_zero_attempts(self) -> None:
        """At least one attempt."""
        self.assertEqual(self.run_main([], "sign", "--ref", REF, "--attempts", "0"), 2)
        self.assertEqual(self.calls(), [])

    def test_missing_cosign(self) -> None:
        """A missing binary is a failure, not a crash."""
        self.plan.write_text("[]")
        with mock.patch.dict(os.environ, {"COSIGN": str(self.dir / "absent")}):
            status = self.sign([], "--identity", IDENTITY)
        self.assertEqual(status, 1)


class AttestationTest(FakeTest):
    """`attestation`: found or not, output, backoff and --require."""

    def check(self, plan: Sequence[Step], *extra: str) -> int:
        """Check REF's attestation."""
        return self.run_main(
            plan,
            "attestation",
            "--subject",
            REF,
            "--repo",
            "Distant-Signal/ds-pollers",
            "--signer-workflow",
            "Distant-Signal/ci/.github/workflows/image.yml",
            *extra,
        )

    def test_found(self) -> None:
        """A verifying attestation: found=true, no sleep, success."""
        status = self.check([("attestation", 0, "")], "--backoff", "30", "--require")
        self.assertEqual(status, 0)
        self.assertEqual(self.output.read_text(), "found=true\n")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(
            self.calls(),
            [
                [
                    "attestation",
                    "verify",
                    f"oci://{REF}",
                    "--repo",
                    "Distant-Signal/ds-pollers",
                    "--signer-workflow",
                    "Distant-Signal/ci/.github/workflows/image.yml",
                ]
            ],
        )

    def test_not_found_backs_off(self) -> None:
        """Not found between attempts: found=false, sleep, still success."""
        status = self.check([("attestation", 1, "no attestations")], "--backoff", "30")
        self.assertEqual(status, 0)
        self.assertEqual(self.output.read_text(), "found=false\n")
        self.assertEqual(self.sleeps, [30])

    def test_not_found_required_fails(self) -> None:
        """Not found after the last attempt fails the step."""
        status = self.check([("attestation", 1, "timeout")], "--require")
        self.assertEqual(status, 1)
        self.assertEqual(self.output.read_text(), "found=false\n")
        self.assertIn("::error::", self.out)

    def test_refuses_a_tag(self) -> None:
        """Only a digest."""
        self.plan.write_text("[]")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = retry.attestation(
                "ghcr.io/x/y:latest",
                repo="a/b",
                signer_workflow="a/b/c",
                backoff=0,
                require=True,
            )
        self.assertEqual(status, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
