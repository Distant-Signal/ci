# Distant Signal CI

Shared CI for the [Distant Signal](https://github.com/Distant-Signal)
repositories: reusable GitHub Actions workflows, composite actions, the
Renovate preset and the scripts they run. Every repository of the split
uses them: distant-signal (the frontend), ds-core, ds-api, ds-ingest,
ds-pollers, ds-workers and ds-deploy (the repository split spec,
distant-signal's `docs/specs/repo-split.md` §5.5 and §8.3).

## Using it

Pin every reference to a full commit SHA, with the branch in a comment, as
for any third-party action. Renovate's github-actions manager keeps the pins
current.

```yaml
jobs:
  rust:
    uses: Distant-Signal/ci/.github/workflows/rust-ci.yml@<sha> # main
    with:
      # renovate: datasource=rust-version depName=rust
      toolchain: 1.99.0
      msrv: 1.88.0
```

Each check shows as `<calling job> / <job in the reusable workflow>`, e.g.
`rust / cargo clippy -D warnings`.

### Reusable workflows (`.github/workflows/`)

| Workflow | Jobs | Notes |
|---|---|---|
| `rust-ci.yml` | clippy in both feature sets (`-D warnings`), the MSRV check, cargo-deny, rustfmt, build and test | Inputs: toolchain, MSRV, apt packages, an extra test command. |
| `rust-db-tests.yml` | builds the DB test archive once, then one job per pass against its own Postgres and Redis | The archive command and the passes come from the repository's `.github/db-tests.toml` (format in `scripts/db-tests-passes.py`). The service images are inputs. |
| `node-ci.yml` | `npm ci`, ESLint, the npm licence gate, Prettier, `tsc --noEmit`, extra npm scripts, `npm test`, `npm run build` | |
| `image.yml` | builds one image; when publishing, pushes it to `ghcr.io/<owner>/images/<service>`, attests provenance, signs and verifies it and records its digest | Call it from a matrix. Reproducible digests, registry layer cache. Extra labels through the `labels` input. Signing and attesting survive Rekor timeouts (below). |
| `helm-chart.yml` | lints a chart; when publishing, packages it as `<version>+build.<run>.sha.<sha>` and pushes it to `ghcr.io/<owner>/charts` | Doesn't sign: sign in your own job (below). |
| `scripts-lint.yml` | shellcheck, shfmt, ruff, mypy, actionlint, hadolint and `lint-containers.py`, then the script tests | Runs in the calling repository's uv project: it keeps its own `lint` dependency group, `uv.lock` and ruff/mypy settings. |
| `dependency-scan.yml` | OSV-Scanner over every lockfile, blocking on critical and fixable findings; a tracking issue on the schedule | `repository` and `report: true` scan another public repository and file its tracking issue in the caller (distant-signal's daily scan of every repository). |
| `identity.yml` | commits use the current identity, without AI attribution | §5.10 |
| `licence.yml` | every tracked `LICENSE` is the canonical GNU AGPL v3 text, byte for byte | Checked against a pinned SHA-256, offline. Needs only a checkout, so any repository can call it. |

Each workflow's header comment documents its inputs, a calling example and
the permissions the caller must grant.

### Composite actions (`actions/`)

- `actions/cosign-sign`: keyless `cosign sign` by digest, retried, with an
  optional `cosign verify` against `verify-identity` (or
  `verify-identity-regexp`) and `verify-issuer`. A composite action runs in
  the calling job, so the signature's certificate identity is the calling
  workflow, which is what Flux pins for the chart. A reusable workflow's job
  signs as the reusable workflow instead, so `image.yml`'s image signatures
  carry `Distant-Signal/ci/.github/workflows/image.yml` as their identity, and
  `helm-chart.yml` leaves chart signing to its caller.
- `actions/paths-changed`: whether a push or pull request touched given paths,
  answering true whenever it can't tell.
- `actions/openapi-breaking`: fails when an OpenAPI document changed in a
  breaking way since a base commit (`oasdiff breaking --fail-on ERR`, a pinned
  and checksum-verified oasdiff). A document new since the base passes, a
  deleted one fails; a breaking change means publishing a new version
  (`v2.json`) rather than editing the old one.

### Signing and attesting through Rekor timeouts

Sigstore's transparency log, Rekor, times out now and then. cosign retries
a timed-out upload itself, and when the first upload had in fact landed,
its retry gets 409 `createLogEntryConflict` and `cosign sign` fails though
the log entry exists; a fresh `cosign sign` can do the same again
(distant-signal's Containers run 41751291688, 2026-11-04: three attempts,
three conflicts). `actions/attest-build-provenance` had no retry at all.
So, with the logic in `scripts/sigstore-retry.py` (tested with a fake
cosign and gh):

- `actions/cosign-sign` tries `cosign sign` up to `attempts` times
  (default 5, backing off 15, 30, 60, then 120 s; inputs `attempts` and
  `backoff`). Given an identity, it runs
  `cosign verify` after every attempt, failed ones included: after a
  success the verify must pass; after a failure a passing verify passes
  the step, and anything else is retried. Without an identity a failure is
  only retried. A failure never passes on less than the verify a success
  gets, and the verify must report a cosign signature payload for the
  digest: `cosign verify` also accepts the provenance attestation, which
  is not a signature. cosign v3 fails a conflict before it pushes the signature, so the
  verify rescues a failure whose signature was pushed, or a digest an
  earlier run already signed (below), not a conflict on a new digest:
  those still rely on the retries. `image.yml` verifies
  against `^https://github.com/<ci repo>/.github/workflows/image.yml@`
  (any ref); chart callers pass their own workflow as `verify-identity`.
- `image.yml` attests up to five times. After a failed attempt,
  `gh attestation verify oci://<image>@<digest> --repo <caller>
  --signer-workflow <ci repo>/.github/workflows/image.yml` decides: an
  attestation that verifies ends the retries; otherwise it backs off (15,
  30, 60, then 120 s) and tries again, and after a fifth failure the job
  fails unless one verifies.

Image digests are reproducible, so an unchanged image's signature or
attestation from an earlier run of the same identity also verifies, and
counts: it is as valid for that digest as a new one.

### Scripts (`scripts/`)

`lint-scripts.py`, `lint-containers.py`, `check-npm-licences.py`,
`check-commit-identity.py`, `check-licence.py`, `check-bench-gates.py`,
`paths-changed.py`, `db-tests-passes.py`, `image-labels.py`,
`openapi-breaking.py`, `gen-rust-dockerfiles.py` and `release-order.py`. They check the
repository they are run from (the working directory), not this one.
`sigstore-retry.py` is the signing and attestation retry logic (above).

`gen-rust-dockerfiles.py` writes (and with `--check` verifies) the
cargo-chef builder stages of a Rust repository's `docker/<service>.Dockerfile`
files and their build-context allowlists, from that repository's
`docker/services.toml` (format in the script's docstring). A repository runs
`--check` in CI from a checkout of this repository at the same commit as its
workflow pins. It writes the monorepo's Dockerfiles byte for byte when given
the monorepo's settings. A reusable workflow fetches them from its
own commit (`job.workflow_sha`), so a caller's pin covers the scripts too.

`release-order.py` makes the images' version labels (`labels`: each
Rust image's `org.distant-signal.core-version` from Cargo.lock, the
frontend's `org.distant-signal.api-build` from its api pin) for a
containers workflow's matrix, and checks a chart's pinned images against
the release-ordering rules (`check --registry`: ds-deploy's chart checks
and ds-core's tag job). Like `gen-rust-dockerfiles.py`, a repository runs
it from a checkout of this repository at its workflow pin.

To run one locally, from the root of the repository to check:

```sh
uv run python /path/to/Distant-Signal/ci/scripts/lint-scripts.py
```

### Renovate preset

`renovate/default.json5`, extended by every repository:

```json5
{
  "extends": ["github>Distant-Signal/ci//renovate/default.json5"],
}
```

It extends FasterSpeeding/Piped's base preset and holds the shared rules:
the non-major groups, the Rust toolchain group (each new stable is one PR;
the MSRV stays put), the `sqlx <0.9` hold, the Valkey and
Postgres major holds, cargo 0.x minors on their own, TypeScript `<7`, and the
custom managers for annotated `# renovate:` versions and digest-pinned images
in workflow inputs. Each repository keeps only its local rules. It must stay
usable by the hosted Renovate app: no private presets, registries or
`postUpgradeTasks`. The app is installed but not running yet; until it is,
dependency and image bumps are made by hand.

## Extraction path lists

`paths/<repo>.txt` is the `git filter-repo --paths-from-file` list each
repository was extracted from the landing repository with (repo split spec
§5.6): its current paths, then the historical names they had. All six
have been extracted: ds-core (phase 4), ds-deploy (phase 5), ds-pollers
(phase 6), ds-ingest and ds-workers (phases 7 and 8) and ds-api (phase 9).
The lists are kept as the record of each extraction.

## Organisation settings

The organisation's Actions allow-list includes `Distant-Signal/*`. Allowing a
reusable workflow doesn't allow what it calls, so every action used inside
these workflows must be on the list too. Adding one here means adding it there
first.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
