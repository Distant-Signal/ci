#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose report is stdout (workflow commands)
"""Sign by digest, or check an attestation, without trusting a flaky Rekor.

  python3 <ci>/scripts/sigstore-retry.py sign --ref REF@sha256:DIGEST
      [--attempts N] [--backoff SECONDS]
      [--identity ID | --identity-regexp RE] [--issuer URL]
  python3 <ci>/scripts/sigstore-retry.py attestation --subject IMAGE@sha256:DIGEST
      --repo OWNER/REPO --signer-workflow OWNER/REPO/PATH
      [--backoff SECONDS] [--require]

`sign` (actions/cosign-sign) runs `cosign sign --yes REF` up to N times,
sleeping BACKOFF seconds after the first failure and doubling it after
each later one (15, 30, 60, 120 s by default). Given an identity, it
`cosign verify`s REF against it (and the issuer) after a successful sign,
failing when that fails, and after a FAILED sign too: when a signature
for the digest verifies, the step passes; otherwise it retries.

Why: cosign retries a Rekor upload that times out, and when the timed-out
upload had in fact landed, cosign's own retry gets 409
createLogEntryConflict and `cosign sign` fails, though the log entry
exists. A fresh `cosign sign` (new ephemeral keys) can hit the same timeout
and conflict again (distant-signal's Containers run 41751291688,
2026-11-04: three attempts, three conflicts, ~30 s each). cosign v3 fails
there while building the signature bundle, before it pushes anything to
the registry, so that attempt's own signature is usually NOT there to
verify; what the verify does catch is a signature pushed before a later
failure, and an earlier run's signature of the same digest (image digests
are reproducible, so an unchanged image keeps its digest and its
signature). A failed sign only ever passes on a signature `cosign verify`
accepts for REF's digest, the expected identity and the issuer: no weaker
than the verify a successful sign gets. Without an identity there is
nothing to verify, so a failed sign is only retried.

`cosign verify` also accepts the image's build provenance attestation (a
DSSE envelope signed by the same identity), so exit status 0 alone isn't
proof of a signature: the verify passes only when its JSON output holds a
cosign signature payload (type `https://sigstore.dev/cosign/sign/v1`, or
the legacy `cosign container image signature`) for REF's own digest.

`attestation` (image.yml, between attempts of
actions/attest-build-provenance) runs `gh attestation verify
oci://SUBJECT --repo REPO --signer-workflow WORKFLOW` and appends
`found=true` or `found=false` to $GITHUB_OUTPUT. When nothing verified, it
sleeps BACKOFF seconds (the retry's backoff) and, with --require (after the
last attempt), exits 1. Any `gh` failure counts as not found. It needs
GH_TOKEN and registry credentials (docker login) for the image.

A verified signature or attestation for the digest from an earlier run of
the same identity counts: it is as valid for that digest as a new one.

The binaries are $COSIGN and $GH (default `cosign`, `gh`), so the tests can
substitute fakes. Stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

DIGEST_REF = re.compile(r".+@sha256:[0-9a-f]{64}")
CONFLICT = "createLogEntryConflict"
DEFAULT_ISSUER = "https://token.actions.githubusercontent.com"
SIGNATURE_TYPES = frozenset(
    {"https://sigstore.dev/cosign/sign/v1", "cosign container image signature"}
)

Sleep = Callable[[float], None]


def echo(text: str) -> None:
    """Print TEXT to stdout, with a final newline, at once."""
    if text:
        print(text, end="" if text.endswith("\n") else "\n", flush=True)


def run(argv: Sequence[str], *, quiet_stdout: bool = False) -> tuple[int, str, str]:
    """Run ARGV, echoing its output; return its exit status, stdout and stderr.

    Its stderr is echoed to stdout, so the log keeps one order. With
    QUIET_STDOUT, stdout is captured but not echoed (cosign verify's JSON
    payload).
    """
    sys.stdout.flush()
    try:
        done = subprocess.run(  # noqa: S603  # argv from fixed binaries and checked inputs
            argv, capture_output=True, text=True, check=False
        )
    except OSError as error:
        echo(f"{argv[0]}: {error}")
        return 127, "", str(error)
    if not quiet_stdout:
        echo(done.stdout)
    echo(done.stderr)
    return done.returncode, done.stdout, done.stderr


def verify_argv(
    cosign: str, ref: str, identity: str, identity_regexp: str, issuer: str
) -> list[str] | None:
    """Return the `cosign verify` command line, or None without an identity."""
    if identity:
        match = ["--certificate-identity", identity]
    elif identity_regexp:
        match = ["--certificate-identity-regexp", identity_regexp]
    else:
        return None
    return [cosign, "verify", ref, "--certificate-oidc-issuer", issuer, *match]


def signature_verifies(verify: Sequence[str], digest: str) -> bool:
    """Return whether VERIFY passes with a signature payload for DIGEST.

    Not just its exit status: cosign verify also reports a verified
    attestation, which is not a signature.
    """
    status, stdout, _ = run(verify, quiet_stdout=True)
    if status != 0:
        return False
    if has_signature(stdout, digest):
        return True
    echo("cosign verify passed, but on no signature for the digest (an attestation?)")
    return False


def has_signature(stdout: str, digest: str) -> bool:
    """Return whether cosign verify's STDOUT holds a signature for DIGEST."""
    for line in stdout.splitlines():
        try:
            payloads = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payloads, list):
            continue
        for payload in payloads:
            critical = payload.get("critical") if isinstance(payload, dict) else None
            if not isinstance(critical, dict):
                continue
            image = critical.get("image")
            if (
                critical.get("type") in SIGNATURE_TYPES
                and isinstance(image, dict)
                and image.get("docker-manifest-digest") == digest
            ):
                return True
    return False


def sign(  # noqa: PLR0913  # the action's inputs, one each
    ref: str,
    *,
    attempts: int,
    backoff: float,
    identity: str = "",
    identity_regexp: str = "",
    issuer: str = DEFAULT_ISSUER,
    sleep: Sleep = time.sleep,
) -> int:
    """Sign REF, retrying; 0 only when signed (and verified, given an identity)."""
    if not DIGEST_REF.fullmatch(ref):
        print(f"::error::cosign-sign signs by digest only; got '{ref}'")
        return 2
    if attempts < 1:
        print(f"::error::attempts must be at least 1; got {attempts}")
        return 2
    cosign = os.environ.get("COSIGN", "cosign")
    verify = verify_argv(cosign, ref, identity, identity_regexp, issuer)
    digest = ref.rpartition("@")[2]
    delay = backoff
    for attempt in range(1, attempts + 1):
        status, stdout, stderr = run([cosign, "sign", "--yes", ref])
        if status == 0:
            if verify is None or signature_verifies(verify, digest):
                return 0
            print(f"::error::cosign signed {ref} but its signature does not verify")
            return 1
        conflict = CONFLICT in stderr + stdout
        reason = "a Rekor conflict (409)" if conflict else f"exit status {status}"
        if verify is not None:
            print(f"Attempt {attempt} failed with {reason}; checking for a signature")
            if signature_verifies(verify, digest):
                print(
                    f"::notice::cosign sign attempt {attempt} failed with {reason}, "
                    f"but {ref} has a signature that verifies against the expected "
                    "identity; passing"
                )
                return 0
        elif conflict:
            print(
                "No verify identity, so the signature this conflict suggests "
                "exists can't be checked; retrying"
            )
        if attempt < attempts:
            print(
                f"::warning::cosign sign attempt {attempt} failed ({reason}); retrying"
            )
            sleep(delay)
            delay *= 2
    unverified = "" if verify is None else ", and no signature verifies"
    print(f"::error::cosign sign failed {attempts} times{unverified}")
    return 1


def attestation_found(subject: str, repo: str, signer_workflow: str) -> bool:
    """Return whether `gh attestation verify` accepts SUBJECT's attestation."""
    gh = os.environ.get("GH", "gh")
    argv = [
        gh,
        "attestation",
        "verify",
        f"oci://{subject}",
        "--repo",
        repo,
        "--signer-workflow",
        signer_workflow,
    ]
    return run(argv, quiet_stdout=True)[0] == 0


def attestation(  # noqa: PLR0913  # the step's inputs, one each
    subject: str,
    *,
    repo: str,
    signer_workflow: str,
    backoff: float,
    require: bool,
    sleep: Sleep = time.sleep,
) -> int:
    """Report whether SUBJECT is attested; sleep, or fail with REQUIRE, if not."""
    if not DIGEST_REF.fullmatch(subject):
        print(f"::error::attestation checks a digest only; got '{subject}'")
        return 2
    found = attestation_found(subject, repo, signer_workflow)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as out:
            out.write(f"found={'true' if found else 'false'}\n")
    if found:
        print(
            f"::notice::the attestation attempt failed, but {subject} has a "
            "provenance attestation that verifies; passing"
        )
        return 0
    if require:
        print(f"::error::no provenance attestation for {subject} verifies")
        return 1
    print(f"No attestation verifies yet; retrying in {backoff:g}s")
    sleep(backoff)
    return 0


def main(argv: Sequence[str] | None = None, sleep: Sleep = time.sleep) -> int:
    """Run a subcommand; its exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    sign_parser = commands.add_parser("sign", help="cosign sign by digest, retried")
    sign_parser.add_argument("--ref", required=True)
    sign_parser.add_argument("--attempts", type=int, default=5)
    sign_parser.add_argument("--backoff", type=float, default=15)
    identity = sign_parser.add_mutually_exclusive_group()
    identity.add_argument("--identity", default="")
    identity.add_argument("--identity-regexp", default="")
    sign_parser.add_argument("--issuer", default=DEFAULT_ISSUER)

    attest_parser = commands.add_parser("attestation", help="is the digest attested?")
    attest_parser.add_argument("--subject", required=True)
    attest_parser.add_argument("--repo", required=True)
    attest_parser.add_argument("--signer-workflow", required=True)
    attest_parser.add_argument("--backoff", type=float, default=0)
    attest_parser.add_argument("--require", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "sign":
        return sign(
            args.ref,
            attempts=args.attempts,
            backoff=args.backoff,
            identity=args.identity,
            identity_regexp=args.identity_regexp,
            issuer=args.issuer,
            sleep=sleep,
        )
    return attestation(
        args.subject,
        repo=args.repo,
        signer_workflow=args.signer_workflow,
        backoff=args.backoff,
        require=args.require,
        sleep=sleep,
    )


if __name__ == "__main__":
    sys.exit(main())
