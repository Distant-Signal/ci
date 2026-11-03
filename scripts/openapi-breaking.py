#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose report is stdout
"""Fail when an OpenAPI document changed in a breaking way since a base commit.

  python3 <ci>/scripts/openapi-breaking.py --base SHA SPEC [SPEC...]
  python3 <ci>/scripts/openapi-breaking.py --download DIR   # only fetch oasdiff

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository that holds the documents (the actions/openapi-breaking composite
action does).

For each SPEC (a path relative to the repository root), takes the file as it
was at SHA (`git show SHA:SPEC`, fetching SHA first when the checkout doesn't
have it) and as it is now, and runs `oasdiff breaking --fail-on ERR` on the
pair. oasdiff is downloaded from its GitHub release, pinned to OASDIFF_VERSION
and checked against OASDIFF_SHA256 before it runs.

A SPEC with no copy at SHA is new: nothing to break, so it passes. An empty or
all-zero SHA (a branch's first push) compares nothing and passes. A SPEC
missing now fails: deleting a published document is the biggest break of all.

The escape from a breaking change is a new document (`v2.json` beside
`v1.json`), never an edit to the published one. Stdlib only. Exit status: 0
no breaking change, 1 a breaking change or a missing document, 2 a tool or
argument problem.
"""

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = Path.cwd()

# oasdiff (Apache-2.0), github.com/oasdiff/oasdiff. Bump the version and the
# checksum together, from that release's checksums.txt.
OASDIFF_VERSION = "1.34.0"
OASDIFF_ASSET = f"oasdiff_{OASDIFF_VERSION}_linux_amd64.tar.gz"
OASDIFF_URL = f"https://github.com/oasdiff/oasdiff/releases/download/v{OASDIFF_VERSION}/{OASDIFF_ASSET}"
OASDIFF_SHA256 = "7c88e09fd500b231af2819eb21f6fdfc7bcdfe2741d2c1937c59e7b72c8a575a"

Git = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run git in the repo root, capturing its output."""
    return subprocess.run(  # noqa: S603  # fixed argv apart from the SHA and paths
        ["git", *args],  # noqa: S607  # git from PATH, as in CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def unknown_base(base: str) -> bool:
    """Return whether BASE names no commit (empty, or GitHub's all-zero SHA)."""
    return not base.strip("0 ")


def ensure_commit(base: str, git: Git = run_git) -> bool:
    """Return whether BASE is a commit here, fetching it if needed."""
    if git(["cat-file", "-e", f"{base}^{{commit}}"]).returncode == 0:
        return True
    git(["fetch", "--no-tags", "--depth=1", "origin", base])
    return git(["cat-file", "-e", f"{base}^{{commit}}"]).returncode == 0


def spec_at(base: str, spec: str, git: Git = run_git) -> str | None:
    """Return SPEC's text at BASE, or None when it did not exist there."""
    shown = git(["show", f"{base}:{spec}"])
    return shown.stdout if shown.returncode == 0 else None


def verified_binary(archive: bytes, expected_sha256: str) -> bytes:
    """Return the `oasdiff` binary from ARCHIVE once its checksum matches."""
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected_sha256:
        msg = f"{OASDIFF_ASSET}: sha256 {actual}, expected {expected_sha256}"
        raise ValueError(msg)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember("oasdiff")
        extracted = tar.extractfile(member)
        if extracted is None:
            msg = f"{OASDIFF_ASSET} has no oasdiff file"
            raise ValueError(msg)
        return extracted.read()


def download_oasdiff(directory: Path) -> Path:
    """Download, verify and unpack the pinned oasdiff into DIRECTORY."""
    with urllib.request.urlopen(OASDIFF_URL, timeout=60) as response:
        archive: bytes = response.read()
    binary = directory / "oasdiff"
    binary.write_bytes(verified_binary(archive, OASDIFF_SHA256))
    binary.chmod(0o755)
    return binary


def check(base: str, specs: Sequence[str], oasdiff: Path, git: Git = run_git) -> int:
    """Return 1 if any SPEC broke since BASE (or is gone), else 0."""
    failed = False
    with tempfile.TemporaryDirectory() as scratch:
        for spec in specs:
            head = ROOT / spec
            if not head.is_file():
                print(
                    f"{spec}: missing; a published OpenAPI document must not be deleted"
                )
                failed = True
                continue
            before = spec_at(base, spec, git)
            if before is None:
                print(f"{spec}: new since {base[:12]}; nothing to compare")
                continue
            base_copy = Path(scratch) / f"base-{len(spec)}-{Path(spec).name}"
            base_copy.write_text(before)
            result = subprocess.run(  # noqa: S603  # the verified binary, fixed flags
                [
                    str(oasdiff),
                    "breaking",
                    "--fail-on",
                    "ERR",
                    str(base_copy),
                    str(head),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                print(f"{spec}: breaking changes since {base[:12]}:\n{output}")
                note = "breaking OpenAPI change; publish a new version instead"
                print(f"::error file={spec}::{note}")
                failed = True
            else:
                print(
                    f"{spec}: no breaking change since {base[:12]}"
                    + (f"\n{output}" if output else "")
                )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments and run the check (or only fetch oasdiff)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--base", help="the commit to compare with")
    parser.add_argument(
        "--download",
        metavar="DIR",
        type=Path,
        help="only download and verify the pinned oasdiff into DIR, then exit",
    )
    parser.add_argument(
        "specs", nargs="*", metavar="SPEC", help="OpenAPI documents, repo-relative"
    )
    args = parser.parse_args(argv)
    if args.download is not None:
        try:
            print(download_oasdiff(args.download))
        except (OSError, ValueError) as err:
            print(f"cannot get oasdiff: {err}", file=sys.stderr)
            return 2
        return 0
    if args.base is None or not args.specs:
        parser.error("--base and at least one SPEC are needed")
    if unknown_base(args.base):
        print("no base commit (a first push): nothing to compare")
        return 0
    if not ensure_commit(args.base):
        print(f"cannot fetch {args.base}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tools:
        try:
            oasdiff = download_oasdiff(Path(tools))
        except (OSError, ValueError) as err:
            print(f"cannot get oasdiff: {err}", file=sys.stderr)
            return 2
        return check(args.base, args.specs, oasdiff)


if __name__ == "__main__":
    sys.exit(main())
