"""Binary search paths for app-managed and bundled helper executables.

Frozen builds ship sidecars in ``<install>/bin`` next to the executable (or in
``_internal/bin`` when the spec places them there). Dev runs use the repo
``bin/`` and ``src/bin/`` folders. Keeping this in one module avoids every
consumer growing its own ``sys.frozen`` branch.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path


def _exe_suffix() -> str:
    return ".exe" if sys.platform.startswith("win") else ""


def binary_name(name: str) -> str:
    return name + _exe_suffix()


def bundled_bin_dirs() -> list[Path]:
    """Directories shipped with the app (repo ``bin/`` folders in dev runs)."""
    if getattr(sys, "frozen", False):
        dirs = [Path(sys.executable).resolve().parent / "bin"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(str(meipass)) / "bin")
        return dirs
    here = Path(__file__).resolve()
    return [here.parents[3] / "bin", here.parents[2] / "bin"]


def bin_search_dirs(app_data: Path | None) -> list[Path]:
    """Managed (app-data) first, then bundled; order preserved, de-duplicated."""
    candidates = ([app_data / "bin"] if app_data is not None else []) + bundled_bin_dirs()
    dirs: list[Path] = []
    for candidate in candidates:
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def engine_env_dirs(app_data: Path | None) -> list[Path]:
    """Bin dirs to prepend to engine child-process ``PATH``.

    Nonexistent entries are harmless and are kept on purpose: the app-managed
    ``<app-data>/bin`` may be created later by an engine/dependency download,
    after a runner has already captured its environment.
    """
    return bin_search_dirs(app_data)


def prepend_path(paths: Sequence[Path], current: str) -> str:
    """Prepend dirs to a PATH-style string, keeping the old tail."""
    prefix = os.pathsep.join(str(path) for path in paths)
    if not prefix:
        return current
    return f"{prefix}{os.pathsep}{current}" if current else prefix
