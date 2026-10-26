"""Standalone-build smoke check.

`yt-paw[.exe] --self-check` (with `YTPAW_PROFILE_DIR` pointing at a scratch
folder) resolves the engine + dependency set and verifies that packaged UI
resources exist. It exits non-zero if anything is missing, so release builds
fail before they are zipped. A human-readable report is written to
`<profile>/self-check.txt`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ytpaw import __version__
from ytpaw.infra.setup_service import ensure_ready

RESOURCES = Path(__file__).resolve().parents[1] / "ui" / "resources"


def _required_assets() -> list[Path]:
    icons = RESOURCES / "icons"
    themes = RESOURCES / "themes"
    return [
        themes / "dark.qss",
        themes / "light.qss",
        icons / "app.ico",
        icons / "app.png",
        icons / "logo.png",
        icons / "settings.svg",
    ]


def run(profile: Path) -> int:
    profile.mkdir(parents=True, exist_ok=True)
    setup = ensure_ready(profile)
    lines = [
        f"yt-paw {__version__}",
        f"frozen={bool(getattr(sys, 'frozen', False))}",
        f"executable={sys.executable}",
        f"profile={profile}",
        f"schema={setup.schema_version}",
        f"first_run={setup.first_run}",
    ]
    ok = True
    for engine in setup.engines:
        good = engine.path is not None and engine.version not in (None, "unknown")
        ok = ok and good
        lines.append(
            f"engine {engine.engine_id} {'ok' if good else 'FAIL'} "
            f"origin={engine.origin} version={engine.version} path={engine.path}"
        )
    for dep in setup.dependencies:
        good = dep.path is not None and dep.version is not None
        ok = ok and good
        lines.append(
            f"dependency {dep.dep_id} {'ok' if good else 'FAIL'} "
            f"origin={dep.origin} version={dep.version} path={dep.path}"
        )
    for asset in _required_assets():
        good = asset.is_file()
        ok = ok and good
        lines.append(f"asset {asset.name} {'ok' if good else 'FAIL'} {asset}")
    lines.append(f"result={'ok' if ok else 'FAIL'}")
    (profile / "self-check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
