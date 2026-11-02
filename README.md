# Distant Signal CI

Shared CI for the [Distant Signal](https://github.com/Distant-Signal)
repositories: reusable GitHub Actions workflows, composite actions, the
Renovate preset and the scripts they run. The monorepo,
[`distant-signal`](https://github.com/Distant-Signal/distant-signal), uses
them first, so they are proven before any service moves to its own repository
(the repository split spec, `docs/specs/repo-split.md` §5.5 and §8.3 there).

## Using it

Pin every reference to a full commit SHA, with the branch in a comment, as
for any third-party action. Renovate's github-actions manager keeps the pins
current.

```yaml
jobs:
  rust:
    uses: Distant-Signal/ci/.github/workflows/rust-ci.yml@<sha> # main
    with:
      toolchain: 1.88.0
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
| `image.yml` | builds one image; when publishing, pushes it to `ghcr.io/<owner>/images/<service>`, attests provenance, signs it and records its digest | Call it from a matrix. Reproducible digests, registry layer cache. |
| `helm-chart.yml` | lints a chart; when publishing, packages it as `<version>+build.<run>.sha.<sha>` and pushes it to `ghcr.io/<owner>/charts` | Doesn't sign: sign in your own job (below). |
| `scripts-lint.yml` | shellcheck, shfmt, ruff, mypy, actionlint, hadolint and `lint-containers.py`, then the script tests | Runs in the calling repository's uv project: it keeps its own `lint` dependency group, `uv.lock` and ruff/mypy settings. |
| `dependency-scan.yml` | OSV-Scanner over every lockfile, blocking on critical and fixable findings; a tracking issue on the schedule | |
| `identity.yml` | commits use the current identity, without AI attribution | §5.10 |

Each workflow's header comment documents its inputs, a calling example and
the permissions the caller must grant.

### Composite actions (`actions/`)

- `actions/cosign-sign`: keyless `cosign sign` by digest, retried after a
  Rekor conflict, with an optional `cosign verify`. A composite action runs in
  the calling job, so the signature's certificate identity is the calling
  workflow, which is what Flux pins for the chart. A reusable workflow's job
  signs as the reusable workflow instead, so `image.yml`'s image signatures
  carry `Distant-Signal/ci/.github/workflows/image.yml` as their identity, and
  `helm-chart.yml` leaves chart signing to its caller.
- `actions/paths-changed`: whether a push or pull request touched given paths,
  answering true whenever it can't tell.

### Scripts (`scripts/`)

`lint-scripts.py`, `lint-containers.py`, `check-npm-licences.py`,
`check-commit-identity.py`, `check-bench-gates.py`, `paths-changed.py` and
`db-tests-passes.py`. They check the repository they are run from (the
working directory), not this one. A reusable workflow fetches them from its
own commit (`job.workflow_sha`), so a caller's pin covers the scripts too.
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
the non-major groups, the MSRV toolchain and `sqlx <0.9` holds, the Valkey and
Postgres major holds, cargo 0.x minors on their own, TypeScript `<7`, and the
custom managers for annotated `# renovate:` versions and digest-pinned images
in workflow inputs. Each repository keeps only its local rules. It must stay
usable by the hosted Renovate app: no private presets, registries or
`postUpgradeTasks`.

## Organisation settings

The organisation's Actions allow-list includes `Distant-Signal/*`. Allowing a
reusable workflow doesn't allow what it calls, so every action used inside
these workflows must be on the list too. Adding one here means adding it there
first.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
