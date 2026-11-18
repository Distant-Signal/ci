#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose report is stdout (workflow commands)
"""Push a packaged Helm chart to an OCI registry, retried and idempotent.

  python3 <ci>/scripts/helm-push.py --chart CHART.tgz --registry oci://HOST/PATH
      --name NAME --version VERSION [--attempts N] [--backoff S,S,...]

Runs `helm push CHART.tgz REGISTRY`, up to N times (3 by default), sleeping
the BACKOFF seconds between attempts (5, 15 and 30 by default), and then
confirms what the registry holds for the version's tag. Writes `ref=` and
`digest=` to $GITHUB_OUTPUT, and the digest is always the one the registry
reported for the tag when read back with `helm pull`, never only what
`helm push` printed.

Why: `helm push` (oras-go's ExtendedCopy) pushes the manifest by digest and
then tags it by fetching that manifest again by digest. GHCR sometimes
answers that fetch with 404 straight after the push ("failed to perform
"Tag" on destination: sha256:...: not found"): ds-deploy's chart runs
2026-11-14 01:46Z, 2026-11-17 01:02Z, 06:54Z and 19:42Z each failed so, and
each left the manifest in the registry, untagged, with no tag for that
build. A retry or a re-run succeeded with no change.

Every attempt starts by reading the version's tag back (`helm pull`):

- the tag holds this very chart (the layer's SHA-256 is the package's): the
  push counts as done, and its digest is taken from the registry;
- the tag holds a different chart: the build version is immutable, so the
  step fails at once, whatever the attempt, and never overwrites it;
- no tag, or the registry can't be asked: push.

After a push that reported success, the tag is read back the same way. A
push that failed is retried only when its output looks like a registry or
transport fault (not found, 5xx, 429, timeout, reset, EOF, DNS, TLS);
authentication, authorisation and any unrecognised error are final. Each
retry decision is a `::warning::` annotation, and when anything beyond a
clean first push happened, the attempts go in $GITHUB_STEP_SUMMARY.

Two packages of one chart are never byte-identical (helm stamps the
archive's files with the packaging time), so a re-run of the whole
workflow, which packages again, finds its earlier chart under the tag and
fails; a re-run of only the failed jobs, the usual case, is fine.

The binary is $HELM (default `helm`), logged in to the registry already.
Stdlib only.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_LINE = re.compile(r"^Digest: *(sha256:[0-9a-f]{64})", re.MULTILINE)
DEFAULT_BACKOFF = (5.0, 15.0, 30.0)

# Output that means the credentials or permissions are wrong: never retried,
# and checked first (a denied push may also say "not found").
FINAL = re.compile(
    r"unauthori[sz]ed|forbidden|denied|authentication required|\b40[13]\b"
    r"|invalid (?:username|credentials)|requested access",
    re.IGNORECASE,
)
# Output that means the registry or the network let it down.
TRANSIENT = re.compile(
    r"not found|\b404\b|\b50[0-9]\b|\b429\b|too many requests|timeout|timed out"
    r"|deadline exceeded|connection (?:reset|refused|closed)|broken pipe"
    r"|\bEOF\b|no such host|temporary failure|temporarily|tls handshake"
    r"|bad gateway|service unavailable|gateway time-?out|server misbehaving",
    re.IGNORECASE,
)
# What `helm pull` says about a tag that isn't there.
ABSENT = re.compile(r"not found|\b404\b|manifest unknown|name unknown", re.IGNORECASE)

Sleep = Callable[[float], None]
Runner = Callable[[Sequence[str]], "Result"]


@dataclass(frozen=True)
class Result:
    """A finished command."""

    status: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        """Its stdout and stderr together."""
        return self.stdout + self.stderr


class Kind(Enum):
    """What the registry holds under the version's tag."""

    ABSENT = auto()
    PRESENT = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class Tag:
    """The tag read back: its kind, the manifest digest and the chart's SHA-256."""

    kind: Kind
    digest: str = ""
    sha256: str = ""
    detail: str = ""


def run(argv: Sequence[str]) -> Result:
    """Run ARGV, capturing its output; a missing binary is status 127."""
    sys.stdout.flush()
    try:
        done = subprocess.run(  # noqa: S603  # argv from a fixed binary and checked inputs
            argv, capture_output=True, text=True, check=False
        )
    except OSError as error:
        return Result(127, "", f"{argv[0]}: {error}")
    return Result(done.returncode, done.stdout, done.stderr)


def echo(result: Result) -> None:
    """Print RESULT's output to stdout, so the log keeps one order."""
    for text in (result.stdout, result.stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", flush=True)


def sha256_of(path: Path) -> str:
    """Return PATH's SHA-256, as a bare hex string."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def last_line(text: str) -> str:
    """Return TEXT's last non-blank line, or a stand-in."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def classify(text: str) -> str:
    """Return `final` or `transient` for a failed push's output."""
    if FINAL.search(text):
        return "final"
    return "transient" if TRANSIENT.search(text) else "final"


def read_tag(helm: str, runner: Runner, reference: str, version: str) -> Tag:
    """Read VERSION's tag under REFERENCE back, with `helm pull`."""
    with tempfile.TemporaryDirectory() as scratch:
        pulled = runner(
            [helm, "pull", reference, "--version", version, "--destination", scratch]
        )
        if pulled.status != 0:
            kind = Kind.ABSENT if ABSENT.search(pulled.text) else Kind.UNKNOWN
            return Tag(kind, detail=last_line(pulled.text))
        archives = sorted(Path(scratch).glob("*.tgz"))
        match = DIGEST_LINE.search(pulled.stdout + pulled.stderr)
        if len(archives) != 1 or match is None:
            return Tag(Kind.UNKNOWN, detail="helm pull gave no chart or no digest")
        return Tag(Kind.PRESENT, match.group(1), sha256_of(archives[0]))


class Log:
    """Annotations on stdout and the rows of the job summary."""

    def __init__(self, reference: str, version: str) -> None:
        """Start a log for REFERENCE and VERSION."""
        self.reference = reference
        self.version = version
        self.rows: list[str] = []
        self.notable = False

    def note(self, attempt: int, what: str, *, warn: bool = False) -> None:
        """Record WHAT for ATTEMPT; a WARN is an annotation and makes it notable."""
        self.rows.append(f"| {attempt} | {what} |")
        if warn:
            self.notable = True
            print(
                f"::warning title=helm push::{self.reference}:{self.version}: "
                f"attempt {attempt}: {what}"
            )
        else:
            print(f"attempt {attempt}: {what}")

    def summary(self, outcome: str) -> None:
        """Append the attempts to $GITHUB_STEP_SUMMARY, if anything was notable."""
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path or not self.notable:
            return
        lines = [
            f"### helm push `{self.reference}` `{self.version}`: {outcome}",
            "",
            "| Attempt | Decision |",
            "| --- | --- |",
            *self.rows,
            "",
        ]
        with Path(path).open("a", encoding="utf-8") as out:
            out.write("\n".join(lines))


def write_outputs(reference: str, digest: str) -> None:
    """Write the step's `ref` and `digest` outputs."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as out:
            out.write(f"ref={reference}\ndigest={digest}\n")


class Fatal(Exception):  # noqa: N818  # a decision, not an error condition
    """A failure no retry can fix."""


class Pusher:
    """One chart version's push: the attempts, the decisions and the log."""

    def __init__(
        self,
        chart: Path,
        *,
        registry: str,
        name: str,
        version: str,
        runner: Runner,
    ) -> None:
        """Prepare to push CHART as VERSION of NAME under REGISTRY."""
        self.chart = chart
        self.helm = os.environ.get("HELM", "helm")
        self.repository = f"{registry.removeprefix('oci://').rstrip('/')}/{name}"
        self.registry = registry
        self.version = version
        # Helm stores the build metadata's `+` as `_` in the tag.
        self.tag = version.replace("+", "_")
        self.runner = runner
        self.local = sha256_of(chart)
        self.log = Log(self.repository, self.tag)
        self.reason = ""

    def read(self) -> Tag:
        """Read the version's tag back."""
        return read_tag(
            self.helm, self.runner, f"oci://{self.repository}", self.version
        )

    def immutable(self, attempt: int, held: Tag, *, when: str) -> Fatal:
        """Return the failure for a tag holding a different chart."""
        self.log.note(
            attempt,
            f"the tag holds a different chart {when} ({held.digest})",
            warn=True,
        )
        return Fatal(
            f"{self.repository}:{self.tag} holds a different chart {when} "
            f"({held.digest}, layer sha256:{held.sha256}) from this package "
            f"(layer sha256:{self.local}); build versions are immutable, so "
            "nothing was overwritten. Push a new commit for a new version."
        )

    def attempt(self, number: int) -> str:
        """Try once; return the registry's digest, or "" to retry.

        Raises Fatal when no retry can help. self.reason says why a retry is
        wanted.
        """
        held = self.read()
        if held.kind is Kind.PRESENT:
            if held.sha256 != self.local:
                raise self.immutable(number, held, when="already")
            self.log.note(
                number,
                f"the tag already holds this chart ({held.digest}); nothing to push",
                warn=number > 1,
            )
            return held.digest
        if held.kind is Kind.UNKNOWN:
            self.log.note(number, f"could not read the tag ({held.detail}); pushing")
        pushed = self.runner([self.helm, "push", str(self.chart), self.registry])
        echo(pushed)
        if pushed.status != 0:
            self.reason = (
                f"helm push failed (exit {pushed.status}): {last_line(pushed.text)}"
            )
            if classify(pushed.text) == "final":
                self.log.note(number, f"{self.reason}; not a registry fault", warn=True)
                msg = f"helm push failed and is not retried: {self.reason}"
                raise Fatal(msg)
            return ""
        reported = DIGEST_LINE.search(pushed.text)
        self.log.note(
            number,
            f"helm push succeeded ({reported.group(1) if reported else 'no digest'})",
        )
        confirmed = self.read()
        if confirmed.kind is not Kind.PRESENT:
            self.reason = "the tag is not readable back after the push"
            return ""
        if confirmed.sha256 != self.local:
            raise self.immutable(number, confirmed, when="after the push")
        if reported and reported.group(1) != confirmed.digest:
            msg = (
                f"helm push reported {reported.group(1)} but the registry holds "
                f"{confirmed.digest} under {self.tag}"
            )
            raise Fatal(msg)
        return confirmed.digest

    def run(self, attempts: int, backoff: Sequence[float], sleep: Sleep) -> int:
        """Run the attempts; 0 only when the chart is under its tag."""
        outcome = "failed"
        try:
            for number in range(1, attempts + 1):
                digest = self.attempt(number)
                if digest:
                    write_outputs(self.repository, digest)
                    outcome = "pushed"
                    return 0
                if number == attempts:
                    self.log.note(number, f"{self.reason}; out of attempts", warn=True)
                    print(
                        f"::error::{self.repository}:{self.tag} was not pushed "
                        f"after {attempts} attempts: {self.reason}"
                    )
                    return 1
                delay = backoff[min(number - 1, len(backoff) - 1)] if backoff else 0.0
                self.log.note(
                    number,
                    f"{self.reason}; reading the tag, then retrying in {delay:g}s",
                    warn=True,
                )
                sleep(delay)
        except Fatal as failure:
            print(f"::error::{failure}")
            return 1
        finally:
            self.log.summary(outcome)
        return 1


def publish(  # noqa: PLR0913  # the step's inputs, one each
    chart: Path,
    *,
    registry: str,
    name: str,
    version: str,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
    attempts: int = 3,
    runner: Runner = run,
    sleep: Sleep = time.sleep,
) -> int:
    """Push CHART and confirm it under VERSION's tag; 0 only when it is there."""
    if attempts < 1:
        print(f"::error::attempts must be at least 1; got {attempts}")
        return 2
    if not chart.is_file():
        print(f"::error::{chart} is not a file")
        return 2
    pusher = Pusher(chart, registry=registry, name=name, version=version, runner=runner)
    return pusher.run(attempts, backoff, sleep)


def main(
    argv: Sequence[str] | None = None, runner: Runner = run, sleep: Sleep = time.sleep
) -> int:
    """Run the CLI; its exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--chart", required=True, type=Path)
    parser.add_argument("--registry", required=True, help="oci://HOST/PATH")
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--backoff",
        default=",".join(f"{b:g}" for b in DEFAULT_BACKOFF),
        help="seconds to sleep after each failed attempt, comma separated",
    )
    args = parser.parse_args(argv)
    try:
        backoff = [float(part) for part in args.backoff.split(",") if part]
    except ValueError:
        print(f"::error::--backoff must be numbers; got '{args.backoff}'")
        return 2
    return publish(
        args.chart,
        registry=args.registry,
        name=args.name,
        version=args.version,
        backoff=backoff,
        attempts=args.attempts,
        runner=runner,
        sleep=sleep,
    )


if __name__ == "__main__":
    sys.exit(main())
