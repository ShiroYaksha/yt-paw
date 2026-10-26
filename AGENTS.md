# AGENTS.md — working in this repo

yt-paw is a local-first, MIT-licensed PySide6 (Qt Widgets) desktop frontend for
pluggable download engines (yt-dlp today; gallery-dl/spotDL later). Python
3.12+.

## Environment

- Dev machine: Windows, VS Code. Repo at `W:\CODE\yt` (WSL: `/mnt/w/CODE/yt`).
- Interpreter: `.venv` created with `uv` (`` (yt) `` prompt). `conda deactivate`
  if `(base)` appears.
- Editable install: `uv pip install -e .`. Code changes are live — do **not**
  reinstall for source edits (only for `pyproject.toml`/entry-point changes).
- Supply-chain rule: install/upgrade with a ~14-day cool-down, e.g.
  `uv pip install --exclude-newer YYYY-MM-DD <pkg>`; prefer stdlib + existing
  deps; do not add new dependencies without owner approval.
- Run from WSL (agent): `W:\CODE\yt\.venv\Scripts\python.exe` →
  `/mnt/w/CODE/yt/.venv/Scripts/python.exe -m <tool>`.

## Dependency updates

Pinned dependencies are refreshed on a deliberately slow train, not whenever a
newer version appears. Read `docs/dependency-updates.md` before touching pins.

```bash
uv run python packaging/deps.py status          # read-only; shows due/available
uv run python packaging/deps.py update --due    # only when due
uv run python packaging/deps.py verify
```

- Policy: `packaging/dependency-policy.json`.
- State: `packaging/dependency-state.json` (owned by `deps.py`; never hand-edit
  to force an update).
- Do not bump an "available" version when `due` is false.
- After any update, run `verify` and wait for both Windows and Linux CI builds.
  `deps.py` refreshes the PySide6/Deno/yt-dlp notice rows; review the FFmpeg row
  manually when its rolling build changes.
- `uv.lock` must stay tracked; it is part of the pin.

## Gates (run before declaring work done)

```powershell
python -m ruff check src tests packaging
python -m pytest -q -p no:cacheprovider
python -m mypy src tests      # strict
```

All three must be clean. Tests never hit the network (fixtures only).

## Layout & boundaries

```
src/ytpaw/
  app/        main_window.py + viewmodels/     (Qt shell, no business logic)
  ui/         views/ widgets/ models/ resources/{themes,icons}  + assets.py, theme.py
  domain/     protocols + dataclasses only (no Qt widgets, no infra)
  engines/    one module per engine + registry.py (the ONLY wiring point)
  infra/      sqlite stores, scheduler, runner, fetchers, updaters, setup
```

Rules:

1. `ui` must not import `infra` directly — go through `app/viewmodels`.
2. Engines implement `domain/engines_base.Engine` (Protocol) and are registered
   in `engines/registry.py`. Engine modules import only `domain` (+QtCore).
   Never import a concrete engine from consumers — use the registry.
3. Adding an engine = new `engines/<id>.py` + one `register()` line + optional
   `job_<id>` store in `infra/detail_store.py`. No UI/scheduler changes.
4. Annotation-only imports use `TYPE_CHECKING` guards; every file starts with
   `from __future__ import annotations`.
5. No comments unless they explain non-obvious *why*. `ruff` (line length 100)
   and `mypy --strict` must pass.
6. Qt specifics: theme via `ui/theme.apply_theme` (Fusion + explicit QPalette +
   QSS). Custom-painted widgets (progress bar) must read
   `app.property("ytpawTheme")`. SVG icons must go through
   `ui/assets.icon()` (Qt cannot render `currentColor`).

## Runtime facts

- App data: `~/.config/yt-paw/` (`app.db`, `logs/jobs/<id>.log`, `bin/` holds
  managed engines + deps). Override for testing: `YTPAW_PROFILE_DIR`.
- Managed engine/dep binaries land in `<app-data>/bin` and are found because
  engine processes get that dir prepended to `PATH` (runner) — no user PATH
  changes.
- GitHub asset hosts intermittently stall ~21s; updaters use API digests,
  parallel checksum fetches, and an 8s stall-retry. Keep those guarantees.
- Debug: `YTPAW_DEBUG_TIMING=1` prints stage timings (pump, QProcess start,
  first output/progress, per-request GET times).

## Docs & plan

- `docs/architecture.md` is the source of truth — update it when behavior,
  layout, protocol signatures, or policies change.
- `plan/` is private (git-ignored): execution plan, current status, probes
  (`_probe_*.py`, `_shot.py`). Never commit it; keep `currentStatus.md`
  fresh (newest section first).
- `plan/_shot.py` renders pages headlessly for UI review:
  `python.exe plan/_shot.py light home download jobs settings`
  (also `wizard`, `banner`).

## Git

- Commit only when the owner explicitly asks.
- Do not commit secrets, cookies files, or anything under `plan/`.
- Commit style: short imperative subject (e.g. "engine: digest-based checksum").
