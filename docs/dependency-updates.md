# Dependency updates

yt-paw deliberately updates dependencies **infrequently**. The pinned versions
are the tested baseline; newer upstream versions are not a reason to bump them.
A quarterly-ish release train is the default. Security fixes and explicit
`--force` exceptions are the only off-cadence paths.

This document is the runbook for a human or an automated agent arriving months
later. Everything needed is in the repository; do not rely on chat history.

## Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | exact direct Python pins |
| `uv.lock` | resolved Python graph (must stay tracked) |
| `packaging/sidecars.lock.json` | exact native sidecar URLs + SHA-256 |
| `packaging/dependency-policy.json` | cadence + minimum release age |
| `packaging/dependency-state.json` | last review date per component |
| `packaging/THIRD_PARTY_NOTICES.md` | shipped component versions |
| `packaging/deps.py` | status / update / verify entry point |

`packaging/deps.py` is the only command you need for routine updates. Do not
edit `dependency-state.json` by hand to force an update; use `--force` or
`--security`.

## Policy

A component becomes due when its last review is older than its cadence. A new
upstream version must also be at least its minimum age before it is eligible.
Both rules exist to avoid churn and buggy same-week releases.

| Component | What | Cadence | Minimum age | Next due* |
|-----------|------|---------|-------------|-----------|
| `qt` | PySide6 | 180 days | 30 days | 2027-04-23 |
| `dev` | pytest, pytest-qt, ruff, mypy | 120 days | 30 days | 2027-02-22 |
| `build` | PyInstaller, hooks, setuptools | 180 days | 30 days | 2027-04-23 |
| `yt-dlp` | bundled engine pin | 90 days | 7 days | 2027-01-23 |
| `deno` | JS runtime | 120 days | 30 days | 2027-02-22 |
| `ffmpeg` | BtbN shared build | 180 days | none (rolling) | 2027-04-23 |

\* From the initial state recorded on 2026-10-25. `deps.py status` computes the
live dates from `dependency-state.json`; never copy dates from this table.

Expect only a handful of dependency PRs per year, not one per week. If the train
is not due, leave it alone even when `status` shows a newer upstream version.

`yt-dlp` engine auto-update at runtime (into `<app-data>/bin/`) is separate and
still happens as needed for extractor breakage. That is not an app/dependency
release.

## Agent runbook

Run these from the repository root.

```bash
# 1. See whether the train is due (network by default)
uv run python packaging/deps.py status

# 2. Machine-readable view
uv run python packaging/deps.py status --format json

# 3. If due is false: STOP. Do not bump "available" versions.
#    If due is true:
uv run python packaging/deps.py update --due

# 4. Gates (add --build to build the host bundle too)
uv run python packaging/deps.py verify

# 5. Review the diff, then commit/PR. Include dependency-state.json.
```

Rules for agents:

1. **Never** update solely because `status` lists an `available` version.
2. **Never** hand-edit `dependency-state.json`; `update` owns it.
3. If `update --due` says nothing is due, stop and report that.
4. If the network or an upstream endpoint fails, do not guess a hash or URL.
   Report the failure and retry later.
5. Both Windows and Linux builds must pass in CI before merge. A host-only
   `verify --build` is a fast pre-check, not a substitute.
6. `deps.py` refreshes the PySide6, Deno, and yt-dlp rows in
   `packaging/THIRD_PARTY_NOTICES.md` when those versions change. `verify`
   fails if they drift. Review the FFmpeg row manually (its version is not
   changed by the rolling hash refresh).
7. Do not commit `build/`, `dist/`, or downloaded caches.

## Commands

### Status

```bash
uv run python packaging/deps.py status
uv run python packaging/deps.py status --format json
uv run python packaging/deps.py status --format markdown
uv run python packaging/deps.py status --offline   # schedule only, no network
```

`status` never writes. It reports:

- current pin(s);
- newest upstream version (marked `(cooling)` while inside the minimum age, or
  `hash changed` for FFmpeg);
- whether the component is due;
- next due date;
- upstream/network errors.

### Update

```bash
uv run python packaging/deps.py update --due
uv run python packaging/deps.py update --due --dry-run
uv run python packaging/deps.py update --component yt-dlp --force
uv run python packaging/deps.py update --component yt-dlp --security
```

- `--due` only touches components whose cadence has elapsed.
- `--force` ignores cadence but still respects minimum release age.
- `--security` ignores cadence and minimum age; use only for a CVE or a broken
  yt-dlp extractor. It still verifies checksums and builds.
- `--dry-run` performs upstream checks and prints the plan without writing.
- `--component` may be repeated.

Every successfully processed component is marked reviewed, including
"current" and "cooling" results. A component with an error is not marked, so it
is retried on the next run.

### Verify

```bash
uv run python packaging/deps.py verify
uv run python packaging/deps.py verify --build
```

Runs `uv lock --check`, the notice-version check, Ruff, mypy, and pytest.
`--build` additionally runs the host PyInstaller build (which fetches bundled
sidecars and runs `--self-check`).

## What the update actually does

### Python

- Reads exact pins from `pyproject.toml`.
- Queries PyPI, choosing the newest stable numeric version older than the
  minimum age.
- Rewrites only the exact pins in the affected group.
- Runs `uv lock --upgrade-package … --exclude-newer-package <pkg>=<cutoff>`.
- Restores `pyproject.toml` if the lock fails.
- Updates the PySide6/Qt row in `THIRD_PARTY_NOTICES.md` when applicable.
- Writes the new date into `dependency-state.json`.

Transitive packages are resolved by `uv`; the minimum-age cutoff is applied to
the direct package being updated. No pre-release, git, URL, or path dependencies
are handled by the updater.

### yt-dlp and Deno

- Queries GitHub's latest stable release for the component's `repo`.
- Keeps the existing per-platform asset names.
- Fetches the upstream checksum file and rewrites `version`, `url`,
  `checksum_url`, and `sha256` for every platform.
- Updates the Deno/yt-dlp row in `THIRD_PARTY_NOTICES.md`.
- Does not download the archives; the normal build does that and verifies the
  hashes again.

### FFmpeg

BtbN's `latest` asset is rolling, so there is no version discovery. The updater
re-fetches `checksums.sha256` and refreshes the `sha256` fields. The human-facing
`version` string records the build date and is updated manually only if it
matters.

## Automation

`.github/workflows/dependencies.yml` runs monthly and after
`dependency-state.json` changes on the default branch, and only **checks** the
train. If the train is due, it creates/updates a single `dependencies` issue with
the status table and the exact commands above; when the train is no longer due,
it closes that issue. It never pushes code.

The monthly check is not an update schedule. The cadence in
`dependency-policy.json` is the update schedule. An agent or maintainer picks
up the issue and runs `update --due`.

## Emergency / security

```bash
uv run python packaging/deps.py update --security --component yt-dlp
uv run python packaging/deps.py verify
```

Use the smallest component set possible. Do not bundle unrelated refreshes into
a security change. If the security fix is in a transitive Python package,
`update --security --component dev` (or the relevant group) plus a manual
`uv lock --upgrade-package <name>` is acceptable; document the exception in the
PR.

## Adding or removing a component

1. Add the exact pin to `pyproject.toml` or the entry to
   `packaging/sidecars.lock.json` (including `repo`).
2. Add the component to `packaging/dependency-policy.json`.
3. Add it to `dependency-state.json` with today's date if it is already current,
   or omit it to make it immediately due.
4. Update this document, `docs/packaging.md`, and the notices if needed.
5. Run `uv run python packaging/deps.py status` to confirm it is recognized.

## Troubleshooting

- **`uv.lock` must be tracked.** If it is not in version control, pin updates are
  not reproducible. Add it with the dependency PR.
- **`status` shows an error for one component.** The others are still valid.
  Fix the network/auth problem; do not fabricate hashes.
- **`update` reports `cooling`.** The upstream release is newer than the
  component's minimum age; the component is marked reviewed and the next train
  will pick it up. This is intentional.
- **`update` reports `nothing due`.** That is the normal state. Do not force it
  without a security reason.
- **Windows/Linux build fails after an update.** Revert the dependency commit,
  not the build code, unless the failure is clearly in packaging.
- **A long gap / Python or PyInstaller jump fails.** The Python interpreter is
  not on this train; bump it deliberately in `pyproject.toml` and CI, then
  rebuild both platforms. Do not mix an interpreter jump with extractor updates.
