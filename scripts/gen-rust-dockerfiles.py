#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI that reports what it wrote (or found stale) on stdout
"""Generate the cargo-chef builder stages of the Rust service Dockerfiles.

  python3 <ci>/scripts/gen-rust-dockerfiles.py [--root DIR]          # rewrite
  python3 <ci>/scripts/gen-rust-dockerfiles.py [--root DIR] --check  # exit 1 if stale

Per repository (repo split phases 4-9): what a repository builds comes from
its own `docker/services.toml` (CONFIG below), not from this file:

  inputs = ["Cargo.toml", ".cargo/config.toml", "Cargo.lock", "crates", ...]
  build-packages = ["cmake", ...]        # optional; default BUILD_PACKAGES
  # renovate: datasource=docker depName=rust
  rust-image = "1.99.0-bookworm@sha256:..."  # the builder's rust tag@digest
  # renovate: datasource=crate depName=cargo-chef
  cargo-chef = "0.1.78"
  [services]
  api = ["api", "maintenance"]           # service -> its binaries, ENTRYPOINT first
  [[extra]]                              # optional: a non-generated Dockerfile
  dockerfile = "frontend/Dockerfile"     # that still gets the apt upgrade,
  stage = "runtime-prod"                 # the licence label and an allowlist
  ignore = ["*", "!frontend", ...]

The rust image and cargo-chef versions live in the repository's config, not
here, so the repository's Renovate bumps them in the same PR as its
Dockerfiles' FROM and `cargo install` lines, and a Distant-Signal/ci pin
bump never makes a repository's Dockerfiles stale.

It also writes each image's build-context allowlist,
docker/<service>.Dockerfile.dockerignore and one next to each [[extra]]
Dockerfile (see "Build inputs" below), and `--check` covers those too.

From the distant-signal monorepo's scripts/gen-rust-dockerfiles.py (repo
split phases 4 to 9): with the monorepo's settings in a services.toml it
writes the monorepo's Dockerfiles byte for byte, so a repository can switch
to this copy without a diff. The marker lines still name
scripts/gen-rust-dockerfiles.py for that reason.

Every docker/<service>.Dockerfile builds one Rust service the same way, so
the part from `ARG CARGO_PROFILE` to the end of the `builder` stage is
generated from the [services] table, between the BEGIN and END marker lines. The
rest of each file (its header comment and its runtime stage) is written by
hand and left alone. Run `--check` in the repository's CI, so an edit
inside the markers, or a Dockerfile with no [services] entry, fails CI.

Why generated: one copy per service of the same four stages, which must differ ONLY in
the `--bin` flags (see below), drift apart when edited by hand.

The stages (cargo-chef, https://github.com/LukeMathWalker/cargo-chef):

- chef: the Rust image, every service's build packages (the union, so this
  layer is identical, and built once, across all of them) and cargo-chef.
- planner: `cargo chef prepare` reduces the workspace to recipe.json, its
  manifests and lockfile. A source-only change leaves recipe.json unchanged.
- builder: `cargo chef cook` builds only the dependencies, from recipe.json,
  in their own layer; then the real source is copied in and `cargo build`
  compiles the workspace crates. A source-only change reuses the cook layer,
  locally and in CI (image.yml imports it from the registry cache).

The cook MUST use exactly the final build's `--bin` flags, profile and
features, or its artifacts don't match and the build recompiles everything.
And it must never be one workspace-wide cook (or an all-binaries build):
Cargo unifies features across everything one invocation builds, which gives
rustls both its `ring` and `aws-lc-rs` providers, and then rustls cannot
pick a default provider at runtime (a TLS failure on the first connection).
One cargo call per service, with that service's own `--bin` flags, keeps
each binary's feature set what `cargo build --bin <service>` gives.

The final build RUN fails if it compiled anything but a workspace crate
(a `Compiling` line without an `(/app/...)` path): that means the cook no
longer matches the build and the cache is silently useless.

Build inputs: the builder stages `COPY . .` the whole build context (the
repo root), so before this every docs, frontend or CI-only push changed
that layer and recompiled every Rust image. BuildKit reads
`<Dockerfile>.dockerignore` next to the Dockerfile in place of the
context's .dockerignore, so each docker/<service>.Dockerfile gets one that
ignores everything (`*`) and lets back in only RUST_INPUTS: the workspace
manifests and crates, the data files the crates embed (include_str!) and
the files the runtime stages COPY. The core crates, with the line catalogue
and the reference data they embed, are git dependencies on
Distant-Signal/ds-core (repo split spec §5.2): `cargo chef cook` fetches
them at the commit Cargo.lock pins, so they need no build context, and no
runtime stage may COPY a `lines/` directory (`--check` fails on one). `--check`
also scans for those hidden inputs (every literal include_str!/include_bytes!/
include! path under crates/, every path a build.rs names, every COPY source
in the Dockerfiles) and fails when one is outside RUST_INPUTS, so a new one
breaks CI here rather than the image build. An [[extra]] Dockerfile gets
its own `ignore` list instead.

Licence label: every runtime stage (and each [[extra]] stage) gets
LICENCE_LABEL as the line after its FROM, so an image built anywhere, not
only by image.yml, says it is AGPL-3.0-or-later.
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path.cwd()
DOCKER_DIR = ROOT / "docker"
# The repository's own build description (see the docstring).
CONFIG = Path("docker") / "services.toml"

BEGIN = "# BEGIN generated by scripts/gen-rust-dockerfiles.py; edit it, not this"
END = "# END generated by scripts/gen-rust-dockerfiles.py"

# The service (docker/<service>.Dockerfile) -> the binaries its image ships,
# the first being its ENTRYPOINT. Each is built with `--bin`; none needs a
# `-p` or `--features` today. Add those here (and to the template) if one
# ever does, for the cook and the build alike.
SERVICES: Mapping[str, Sequence[str]] = {}

# Renovate bumps these in this file (renovate.json5's custom managers) on the
# same branch as the Dockerfiles' own FROM / `cargo install` lines, so its
# PRs stay drift-free; keep each assignment on one line for its regex.
# renovate: datasource=docker depName=rust
RUST_TAG = "1.99.0-bookworm"
RUST_DIGEST = "sha256:114c7a4425406451c2866b6aafe69fe29b1b298832db1277d411ac73c82d04d6"
# renovate: datasource=crate depName=cargo-chef
CARGO_CHEF_VERSION = "0.1.78"
# Build-time packages, the union over every service: rdkafka's vendored
# librdkafka (movement-relay: cmake, libssl-dev, pkg-config, libsasl2-dev
# for sasl2-sys, libcurl4-openssl-dev for librdkafka's <curl/curl.h>) and
# web-push's OpenSSL (notifier: libssl-dev, pkg-config). Present for every
# service so the chef layer is shared; a crate that doesn't link them is
# unaffected (aws-lc-sys picks its cc builder whether or not cmake exists).
BUILD_PACKAGES = (
    "cmake",
    "libcurl4-openssl-dev",
    "libsasl2-dev",
    "libssl-dev",
    "pkg-config",
)

# The final build's check: print each `Compiling` line without an /app path
# (a dependency, not a workspace crate) and exit 1 if there was one.
CHECK_AWK = (
    "/^ +Compiling / && !/ \\(\\/app\\// { print; found = 1 } END { exit found }"
)
CHECK_ERROR = (
    "the build compiled the dependencies above; "
    "the cargo chef cook flags no longer match the build's"
)

CARGO_MOUNTS = (
    "RUN --mount=type=cache,id=cargo-registry,target=/usr/local/cargo/registry,"
    "sharing=locked \\\n"
    "    --mount=type=cache,id=cargo-git,target=/usr/local/cargo/git,"
    "sharing=locked \\\n"
)

TEMPLATE = """\
{begin}
# Builder stages: cargo-chef, so the dependency build is a layer of its own
# that a source-only change reuses (locally, and in CI from the registry
# build cache, see .github/workflows/containers.yml). The generator's
# docstring explains the stages and why the cook and the build must take
# exactly the same flags.
#
# CARGO_PROFILE picks the cargo build profile (and matching target/<profile>
# output dir): "release" (default) for optimized builds, "debug" for fast
# unoptimized dev builds. Set to "debug" by docker-compose.dev.yml, the
# override that `dev.env` selects via COMPOSE_FILE; docker-compose.yml on
# its own leaves it at "release".
ARG CARGO_PROFILE=release

FROM {image} AS chef

# The registry and git caches hold only downloaded sources, so every
# service shares them. `sharing=locked`: docker-compose builds in parallel.
# hadolint ignore=DL3008 # apt versions unpinned on purpose; see .hadolint.yaml
{mounts}    apt-get update \\
    && apt-get install -y --no-install-recommends {packages} \\
    && rm -rf /var/lib/apt/lists/* \\
    && cargo install cargo-chef --locked --version {chef_version}

WORKDIR /app

FROM chef AS planner
# `.` is the build context as this Dockerfile's .dockerignore allowlist
# leaves it: the workspace and the data files it embeds or ships, not the
# whole repo (see the generator's docstring, "Build inputs").
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
ARG CARGO_PROFILE

# No cache mount on target/: the cooked dependencies must land in this
# layer, which is what the next build (and CI's registry cache) reuses.
COPY --from=planner /app/recipe.json recipe.json
{mounts}    if [ "${{CARGO_PROFILE}}" = "release" ]; then \\
      cargo chef cook --release --locked --recipe-path recipe.json {bins}; \\
    else \\
      cargo chef cook --locked --recipe-path recipe.json {bins}; \\
    fi

COPY . .
# The project URL in every outbound User-Agent (crates/ds-runtime's
# user_agent!): containers.yml passes the building repository's URL, so the
# images follow the repository's owner. Declared after the cook so it never
# invalidates the dependency layer. Unset or empty (a local or compose
# build), it is unset below and .cargo/config.toml's default applies.
ARG DS_PROJECT_URL
# The same flags as the cook. cargo's log is printed after the build, then
# checked: a `Compiling` line without an /app path is a dependency the cook
# should have built, so the cook no longer matches and the build fails. The
# binaries are copied out and target/ is deleted in the same RUN, so this
# layer stays small in the build cache.
{mounts}    if [ -z "${{DS_PROJECT_URL}}" ]; then unset DS_PROJECT_URL; fi; \\
    if [ "${{CARGO_PROFILE}}" = "release" ]; then \\
      cargo build --release --locked {bins} > /tmp/cargo-build.log 2>&1; \\
    else \\
      cargo build --locked {bins} > /tmp/cargo-build.log 2>&1; \\
    fi; \\
    status=$?; \\
    cat /tmp/cargo-build.log; \\
    if [ "${{status}}" -ne 0 ]; then exit "${{status}}"; fi; \\
    if ! awk '{check}' /tmp/cargo-build.log; then \\
      echo "error: {check_error}" >&2; \\
      exit 1; \\
    fi; \\
{copies}    && rm -rf /app/target /tmp/cargo-build.log
{end}
"""


# What the Rust images' build context holds: everything else in the repo is
# ignored, so a change to it can't invalidate a Rust build layer.
RUST_INPUTS: tuple[str, ...] = (
    "Cargo.toml",
    ".cargo/config.toml",
    "Cargo.lock",
    "crates",
)
# Left out even inside RUST_INPUTS: build output and local env files.
RUST_EXCLUDES = ("**/target", "**/*.env", "**/.env")

IGNORE_HEADER = """\
# Generated by scripts/gen-rust-dockerfiles.py; edit it, not this.
# The build-context allowlist for:
#   {dockerfile}
# BuildKit reads it in place of the repo root's .dockerignore. Nothing
# else in the repo (docs, CI, other images' sources) reaches the build,
# so a change there can't invalidate its layers. See the generator's
# docstring, "Build inputs".
"""

# The frontend image's context: frontend/ only, minus its local build output
# and dependencies (the host's node_modules must not clobber the image's own
# `npm ci`, see the root .dockerignore) and env files.


@dataclass(frozen=True)
class Extra:
    """A hand-written Dockerfile: allowlist, apt upgrade and licence label only."""

    dockerfile: Path
    stage: str
    ignore: tuple[str, ...]


EXTRAS: tuple[Extra, ...] = ()


def render_ignore(dockerfile: Path, lines: Sequence[str]) -> str:
    """Return a Dockerfile's .dockerignore with `lines` after the header."""
    rel = dockerfile.relative_to(ROOT).as_posix()
    return IGNORE_HEADER.format(dockerfile=rel) + "".join(f"{x}\n" for x in lines)


def rust_ignore_lines() -> tuple[str, ...]:
    """Return the Rust images' .dockerignore patterns."""
    return ("*", *(f"!{path}" for path in RUST_INPUTS), *RUST_EXCLUDES)


def ignore_path(dockerfile: Path) -> Path:
    """Return the per-Dockerfile ignore file BuildKit reads for `dockerfile`."""
    return dockerfile.with_name(f"{dockerfile.name}.dockerignore")


def rust_allows(path: str) -> bool:
    """Whether the Rust build context holds `path` (relative to the root)."""
    parts = path.split("/")
    if "target" in parts or parts[-1] == ".env" or parts[-1].endswith(".env"):
        return False
    return any(path == p or path.startswith(f"{p}/") for p in RUST_INPUTS)


# include_str!("..."), include_bytes!("...") and include!("..."), the path
# possibly on the next line; a non-literal argument (concat!(env!(...)))
# doesn't match.
INCLUDE = re.compile(r'\binclude(?:_str|_bytes)?!\(\s*"([^"]+)"\s*\)')
STRING = re.compile(r'"([^"\\]+)"')


def relative(path: Path) -> str | None:
    """Return `path` relative to the repo root, or None if outside it."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def source_inputs(source: Path) -> Iterator[str]:
    """Yield the paths one Rust source file reads at build time.

    Its literal include_str!/include_bytes!/include! paths and, for a
    build.rs, every string naming an existing path relative to its crate.
    """
    text = source.read_text(encoding="utf-8")
    for match in INCLUDE.finditer(text):
        yield relative(source.parent / match.group(1)) or match.group(1)
    if source.name == "build.rs":
        for match in STRING.finditer(text):
            candidate = source.parent / match.group(1)
            if candidate.exists():
                yield relative(candidate) or match.group(1)


def copy_sources(dockerfile: Path) -> Iterator[str]:
    """Yield the build-context sources of a Dockerfile's COPY lines.

    `COPY --from=...` (another stage) and the builder's `COPY . .` are
    skipped.
    """
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "COPY":
            continue
        if any(w.startswith("--from") for w in words):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        yield from (src.rstrip("/") for src in args[:-1] if src != ".")


def hidden_inputs() -> Iterator[tuple[str, str]]:
    """Yield (where, path) for every file the Rust builds read by path.

    Every literal include_str!/include_bytes!/include! in a workspace
    member's crate, every path a build.rs names, and every non-`--from`
    COPY source in docker/*.Dockerfile. A crates/ directory that isn't a
    member (a core crate's leftover before its deletion) builds nothing.
    """
    members = tuple(
        ROOT / member
        for member in tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))[
            "workspace"
        ]["members"]
    )
    for source in sorted((ROOT / "crates").rglob("*.rs")):
        if not any(source.is_relative_to(member) for member in members):
            continue
        if "target" not in source.relative_to(ROOT).parts:
            where = source.relative_to(ROOT).as_posix()
            yield from ((where, path) for path in source_inputs(source))
    for dockerfile in sorted(DOCKER_DIR.glob("*.Dockerfile")):
        where = dockerfile.relative_to(ROOT).as_posix()
        yield from ((where, path) for path in copy_sources(dockerfile))


# The name every Rust Dockerfile gives its last (runtime) stage. The weekly
# forced rebuild in .github/workflows/containers.yml rebuilds exactly this
# stage uncached (`no-cache-filters`) to pick up Debian security updates.
RUNTIME_STAGE = "runtime"


# The weekly rebuild's point: besides the packages a runtime stage installs,
# the base image's own (libc, openssl, ...) are upgraded to the archive's
# current versions at build time. Inserted after the runtime stage's
# `RUN apt-get update \` (its first RUN), in every Rust Dockerfile and in
# frontend/Dockerfile's runtime-prod stage; `--check` fails without it.
# Cached builds reuse that layer, so only the weekly no-cache rebuild (or a
# base-image digest bump) actually refreshes the packages.
APT_UPDATE = "RUN apt-get update \\"
APT_UPGRADE = "    && apt-get upgrade -y --no-install-recommends \\"


def with_runtime_upgrade(text: str, stage: str, path: Path) -> str:
    """Return `text` with APT_UPGRADE after `stage`'s `RUN apt-get update`."""
    lines = text.splitlines(keepends=True)
    starts = [
        i
        for i, line in enumerate(lines)
        if line.startswith("FROM ") and line.split()[-2:] == ["AS", stage]
    ]
    run = next(
        (
            i
            for i in range(starts[0] + 1 if starts else len(lines), len(lines))
            if lines[i].startswith(("FROM ", "RUN "))
        ),
        None,
    )
    if run is None or lines[run].rstrip("\n") != APT_UPDATE:
        msg = f"{path}: stage {stage!r}'s first RUN must be {APT_UPDATE!r}"
        raise ValueError(msg)
    if run + 1 < len(lines) and lines[run + 1].rstrip("\n") == APT_UPGRADE:
        return text
    return "".join([*lines[: run + 1], f"{APT_UPGRADE}\n", *lines[run + 1 :]])


# The OCI licence label (repo split spec §6), the same value as the
# `org.opencontainers.image.licenses` label containers.yml sets.
LICENCE_LABEL = 'LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"'


def with_licence_label(text: str, stage: str, path: Path) -> str:
    """Return `text` with LICENCE_LABEL as the line after `stage`'s FROM."""
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("FROM ") and line.split()[-2:] == ["AS", stage]
        ),
        None,
    )
    if start is None:
        msg = f"{path}: no `FROM ... AS {stage}` stage for the licence label"
        raise ValueError(msg)
    if start + 1 < len(lines) and lines[start + 1].rstrip("\n") == LICENCE_LABEL:
        return text
    return "".join([*lines[: start + 1], f"{LICENCE_LABEL}\n", *lines[start + 1 :]])


def runtime_stage_problems(found: Mapping[str, Path]) -> list[str]:
    """Return the Dockerfiles whose last stage isn't named RUNTIME_STAGE."""
    problems: list[str] = []
    for name in sorted(found):
        froms = [
            line.split()
            for line in found[name].read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ")
        ]
        if not froms or [w.lower() for w in froms[-1][-2:]] != ["as", RUNTIME_STAGE]:
            problems.append(
                f"docker/{name}.Dockerfile: the last stage must be `AS "
                f"{RUNTIME_STAGE}` (containers.yml's weekly rebuild names it)"
            )
    return problems


# A runtime stage's copy of one built binary out of the builder stage.
BINARY_COPY = re.compile(r"^COPY --from=builder /usr/local/bin/(\S+) ", re.MULTILINE)


def binary_copy_problems(found: Mapping[str, Path]) -> list[str]:
    """Return the runtime COPYs that disagree with SERVICES.

    The generated block builds exactly SERVICES' binaries; the hand-written
    runtime stage copies them. A COPY of a binary no longer built fails only
    at `docker build` (ingest phase 5, 5.4b), and a built binary nobody
    copies ships nothing.
    """
    problems: list[str] = []
    for name in sorted(found.keys() & SERVICES.keys()):
        copied = BINARY_COPY.findall(found[name].read_text(encoding="utf-8"))
        built = SERVICES[name]
        problems += [
            f"docker/{name}.Dockerfile copies {binary!r}, which SERVICES does not build"
            for binary in copied
            if binary not in built
        ]
        problems += [
            f"docker/{name}.Dockerfile builds {binary!r} but never copies it "
            "into the runtime stage"
            for binary in built
            if binary not in copied
        ]
    return problems


def ignore_files(found: Mapping[str, Path]) -> dict[Path, str]:
    """Return every generated .dockerignore path and its wanted text."""
    ignores = {
        ignore_path(path): render_ignore(path, rust_ignore_lines())
        for name, path in found.items()
        if name in SERVICES
    }
    for extra in EXTRAS:
        ignores[ignore_path(extra.dockerfile)] = render_ignore(
            extra.dockerfile, extra.ignore
        )
    return ignores


# The line catalogue is built into the binaries (crates/ds-lines, repo split
# spec §9 phase 3 item 5), so a runtime COPY of it would ship a second copy
# that nothing reads, or one that disagrees with the embedded catalogue
# under a LINES_DIR override.
LINES_COPY = "lines"


def lines_copy_problems(found: Mapping[str, Path]) -> list[str]:
    """Return the Dockerfiles that COPY the line catalogue into an image."""
    return [
        f"docker/{name}.Dockerfile copies {source}/ into the image; the line "
        "catalogue is built into the binaries (crates/ds-lines)"
        for name in sorted(found)
        for source in copy_sources(found[name])
        if source == LINES_COPY or source.startswith(f"{LINES_COPY}/")
    ]


def input_problems(found: Mapping[str, Path]) -> list[str]:
    """Return hidden build inputs left out, and stray ignore files."""
    problems = [
        f"{where} reads {path}, which the Rust images' build context leaves "
        f"out: add it to `inputs` in {CONFIG}"
        for where, path in hidden_inputs()
        if not rust_allows(path)
    ]
    stray = set(DOCKER_DIR.glob("*.dockerignore")) - set(ignore_files(found))
    problems += [
        f"{p.relative_to(ROOT)} has no Dockerfile in SERVICES; delete it"
        for p in sorted(stray)
    ]
    return problems


def wanted_files(found: Mapping[str, Path]) -> tuple[dict[Path, str], list[str]]:
    """Return {path: wanted text} for every generated file, and problems."""
    wanted = ignore_files(found)
    problems: list[str] = []
    for name in sorted(found.keys() & SERVICES.keys()):
        path = found[name]
        try:
            text = splice(path.read_text(encoding="utf-8"), render(name), path)
            text = with_runtime_upgrade(text, RUNTIME_STAGE, path)
            wanted[path] = with_licence_label(text, RUNTIME_STAGE, path)
        except ValueError as error:
            problems.append(str(error))
    for extra in EXTRAS:
        try:
            text = with_runtime_upgrade(
                extra.dockerfile.read_text(encoding="utf-8"),
                extra.stage,
                extra.dockerfile,
            )
            wanted[extra.dockerfile] = with_licence_label(
                text, extra.stage, extra.dockerfile
            )
        except ValueError as error:
            problems.append(str(error))
    return wanted, problems


def render(service: str) -> str:
    """Return the generated block for one service, markers included."""
    bins = SERVICES[service]
    copies = "".join(
        f'    {"&& " if i else ""}cp "/app/target/${{CARGO_PROFILE}}/{name}" '
        f"/usr/local/bin/{name} \\\n"
        for i, name in enumerate(bins)
    )
    return TEMPLATE.format(
        begin=BEGIN,
        end=END,
        image=f"rust:{RUST_TAG}@{RUST_DIGEST}",
        mounts=CARGO_MOUNTS,
        packages=" ".join(BUILD_PACKAGES),
        chef_version=CARGO_CHEF_VERSION,
        bins=" ".join(f"--bin {name}" for name in bins),
        copies=copies,
        check=CHECK_AWK,
        check_error=CHECK_ERROR,
    )


def splice(text: str, block: str, path: Path) -> str:
    """Replace the BEGIN..END block of a Dockerfile's text with `block`."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\n") == END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        msg = f"{path}: needs exactly one {BEGIN!r} line, then one {END!r} line"
        raise ValueError(msg)
    return "".join([*lines[: starts[0]], block, *lines[ends[0] + 1 :]])


def dockerfiles() -> dict[str, Path]:
    """Return the service Dockerfiles under docker/, by service name."""
    return {
        path.name.removesuffix(".Dockerfile"): path
        for path in sorted(DOCKER_DIR.glob("*.Dockerfile"))
    }


def configure(root: Path) -> None:
    """Load ROOT's docker/services.toml into the module's settings."""
    global ROOT, DOCKER_DIR, SERVICES, RUST_INPUTS, BUILD_PACKAGES, EXTRAS  # noqa: PLW0603  # a CLI's one-time setup
    global RUST_TAG, RUST_DIGEST, CARGO_CHEF_VERSION  # noqa: PLW0603
    ROOT = root.resolve()
    DOCKER_DIR = ROOT / "docker"
    config = tomllib.loads((ROOT / CONFIG).read_text(encoding="utf-8"))
    SERVICES = {name: tuple(bins) for name, bins in config["services"].items()}
    RUST_INPUTS = tuple(config["inputs"])
    BUILD_PACKAGES = tuple(config.get("build-packages", BUILD_PACKAGES))
    RUST_TAG, _, RUST_DIGEST = config.get(
        "rust-image", f"{RUST_TAG}@{RUST_DIGEST}"
    ).partition("@")
    CARGO_CHEF_VERSION = config.get("cargo-chef", CARGO_CHEF_VERSION)
    EXTRAS = tuple(
        Extra(ROOT / extra["dockerfile"], extra["stage"], tuple(extra["ignore"]))
        for extra in config.get("extra", ())
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite (or, with --check, verify) every service Dockerfile."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any Dockerfile differs from its generated form",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="the repository root (default: the working directory)",
    )
    args = parser.parse_args(argv)
    configure(args.root)

    found = dockerfiles()
    problems = [
        f"docker/{name}.Dockerfile has no [services] entry in {CONFIG}"
        for name in sorted(found.keys() - SERVICES.keys())
    ] + [
        f"SERVICES has {name!r} but docker/{name}.Dockerfile does not exist"
        for name in sorted(SERVICES.keys() - found.keys())
    ]
    problems += input_problems(found)
    problems += runtime_stage_problems(found)
    problems += binary_copy_problems(found)
    problems += lines_copy_problems(found)
    wanted, splice_problems = wanted_files(found)
    problems += splice_problems
    stale: list[str] = []
    for path, text in wanted.items():
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        stale.append(str(path.relative_to(ROOT)))
        if not args.check:
            path.write_text(text, encoding="utf-8")

    for problem in problems:
        print(f"error: {problem}")
    if args.check:
        for path_name in stale:
            print(f"stale: {path_name}")
        if stale:
            print(f"run `{Path(__file__).name} --root {ROOT}` to regenerate")
    else:
        for path_name in stale:
            print(f"wrote {path_name}")
    return 1 if problems or (args.check and stale) else 0


if __name__ == "__main__":
    sys.exit(main())
