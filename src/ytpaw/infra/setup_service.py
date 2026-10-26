"""First-run self-setup. Every launch runs `ensure_ready()` before UI is interactive.

Steps (arch §4.4): dirs → DB migrate → seed settings → resolve engines →
resolve dependencies. Engines are discovered generically through the registry:
per-engine override → app-managed/bundled `bin/<binary_name>` → system PATH.
Dependencies (deno, ffmpeg, ...) resolve managed bin/ → bundled bin/ → system
PATH via their specs.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ytpaw.domain.dependencies import DependencySpec
from ytpaw.domain.settings_keys import APP_DEFAULTS
from ytpaw.engines import registry
from ytpaw.infra.db import connect, migrate
from ytpaw.infra.dependencies import (
    ALL_DEPENDENCIES,
    probe_dependency,
    system_binary_path,
)
from ytpaw.infra.engine_settings_store import EngineSettingsStore
from ytpaw.infra.runtime_paths import bin_search_dirs, binary_name, bundled_bin_dirs
from ytpaw.infra.settings_store import SettingsStore

if TYPE_CHECKING:
    from ytpaw.domain.engines_base import Engine


@dataclass(frozen=True)
class EngineStatus:
    engine_id: str
    display_name: str
    path: str | None
    origin: str  # bundled|custom|system|missing
    version: str | None
    enabled: bool = True


@dataclass(frozen=True)
class DependencyStatus:
    dep_id: str
    display_name: str
    path: str | None
    origin: str  # managed|bundled|system|missing
    version: str | None


@dataclass(frozen=True)
class SetupResult:
    app_data: Path
    db_path: Path
    schema_version: int
    first_run: bool
    engines: list[EngineStatus]
    dependencies: list[DependencyStatus]


def default_app_data() -> Path:
    return Path.home() / ".config" / "yt-paw"


def resolve_dependency(dep: DependencySpec, app_data: Path) -> DependencyStatus:
    name = binary_name(dep.binary_name)
    for bin_dir, origin in [
        (app_data / "bin", "managed"),
        *[(path, "bundled") for path in bundled_bin_dirs()],
    ]:
        candidate = bin_dir / name
        if candidate.exists():
            return DependencyStatus(
                dep.dep_id, dep.display_name, str(candidate), origin,
                probe_dependency(dep, str(candidate)),
            )
    found = system_binary_path(dep)
    if found:
        return DependencyStatus(
            dep.dep_id, dep.display_name, found, "system",
            probe_dependency(dep, found),
        )
    return DependencyStatus(dep.dep_id, dep.display_name, None, "missing", None)


def resolve_engine_status(
    engine: Engine, conn: sqlite3.Connection, app_data: Path | None = None
) -> EngineStatus:
    """Resolve per source mode (engine_settings `source`):

    - system → PATH lookup only
    - path   → user-provided binary_path override
    - auto   → app-managed: app-data bin/ (auto-updated), then bundled
      install bin/, then system fallback

    Legacy rows without `source`: binary_path set ⇒ path, else system.
    """
    store = EngineSettingsStore(conn)
    enabled = bool(store.get(engine.engine_id, "enabled", True))
    source = str(store.get(engine.engine_id, "source", "") or "").strip().lower()
    binary_path = str(store.get(engine.engine_id, "binary_path", "") or "").strip()
    if source not in ("system", "path", "auto"):
        source = "path" if binary_path else "system"

    if source == "path":
        if not binary_path:
            return EngineStatus(
                engine.engine_id, engine.display_name, None, "missing", None, enabled
            )
        path = str(Path(binary_path).expanduser())
        return EngineStatus(
            engine.engine_id, engine.display_name, path, "custom",
            engine.probe_version(path), enabled,
        )

    if source == "auto":
        for bin_dir in bin_search_dirs(app_data):
            bundled = bin_dir / engine.binary_name
            for candidate in (bundled, bundled.with_suffix(".exe")):
                if candidate.exists():
                    return EngineStatus(
                        engine.engine_id, engine.display_name, str(candidate),
                        "bundled", engine.probe_version(str(candidate)), enabled,
                    )
        # no managed binary yet → system fallback

    found = shutil.which(engine.binary_name)
    if found:
        return EngineStatus(
            engine.engine_id, engine.display_name, found, "system",
            engine.probe_version(found), enabled,
        )
    return EngineStatus(engine.engine_id, engine.display_name, None, "missing", None, enabled)


def ensure_ready(app_data: Path | None = None) -> SetupResult:
    base = app_data or default_app_data()
    (base / "logs").mkdir(parents=True, exist_ok=True)

    db_path = base / "app.db"
    first_run = not db_path.exists()  # no DB ⇒ genuine first start
    conn = connect(db_path)
    try:
        schema_version = migrate(conn)
        SettingsStore(conn).seed_defaults(APP_DEFAULTS)
        for engine in registry.all_engines():
            store = EngineSettingsStore(conn)
            store.seed_defaults(engine.engine_id, dict(engine.settings_defaults))
            if store.get(engine.engine_id, "source", None) is None:
                legacy_path = str(
                    store.get(engine.engine_id, "binary_path", "") or ""
                ).strip()
                if legacy_path:
                    default_source = "path"
                elif getattr(engine, "update_repo", ""):
                    default_source = "auto"  # fresh installs: app-managed engine
                else:
                    default_source = "system"
                store.set(engine.engine_id, "source", default_source)
        statuses = [
            resolve_engine_status(engine, conn, base)
            for engine in registry.all_engines()
        ]
    finally:
        conn.close()

    dependencies = [resolve_dependency(dep, base) for dep in ALL_DEPENDENCIES]

    return SetupResult(
        app_data=base,
        db_path=db_path,
        schema_version=schema_version,
        first_run=first_run,
        engines=statuses,
        dependencies=dependencies,
    )


def open_db(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)
