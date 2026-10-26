"""Packaging runtime paths + bundled-sidecar resolution (no network)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from ytpaw.infra import runtime_paths, setup_service
from ytpaw.infra.db import connect, migrate
from ytpaw.infra.dependencies import DENO
from ytpaw.infra.engine_settings_store import EngineSettingsStore
from ytpaw.infra.runtime_paths import (
    bin_search_dirs,
    binary_name,
    bundled_bin_dirs,
    engine_env_dirs,
    prepend_path,
)

if TYPE_CHECKING:
    from ytpaw.domain.engines_base import Engine


def test_bundled_bin_dirs_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "yt-paw.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert bundled_bin_dirs() == [
        tmp_path / "bin",
        tmp_path / "_internal" / "bin",
    ]


def test_bin_search_dirs_prefers_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled = tmp_path / "install" / "bin"
    monkeypatch.setattr(
        runtime_paths, "bundled_bin_dirs", lambda: [bundled, bundled]
    )
    dirs = bin_search_dirs(tmp_path / "profile")
    assert dirs == [tmp_path / "profile" / "bin", bundled]


def test_engine_env_dirs_includes_future_managed_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = tmp_path / "bin"
    existing.mkdir()
    monkeypatch.setattr(
        runtime_paths, "bundled_bin_dirs", lambda: [existing, tmp_path / "absent"]
    )
    assert engine_env_dirs(tmp_path / "profile") == [
        tmp_path / "profile" / "bin",
        existing,
        tmp_path / "absent",
    ]


def test_prepend_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    sep = ";" if sys.platform.startswith("win") else ":"
    assert prepend_path([first, second], "/usr/bin") == f"{first}{sep}{second}{sep}/usr/bin"
    assert prepend_path([first], "") == str(first)
    assert prepend_path([], "/usr/bin") == "/usr/bin"


def test_resolve_dependency_prefers_managed_then_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled = tmp_path / "install" / "bin"
    bundled.mkdir(parents=True)
    (bundled / binary_name(DENO.binary_name)).write_bytes(b"")
    monkeypatch.setattr(setup_service, "bundled_bin_dirs", lambda: [bundled])
    monkeypatch.setattr(setup_service, "probe_dependency", lambda _dep, _path: "9.9.9")

    status = setup_service.resolve_dependency(DENO, tmp_path / "profile")
    assert status.origin == "bundled"
    assert status.path is not None
    assert status.version == "9.9.9"

    managed = tmp_path / "profile" / "bin"
    managed.mkdir(parents=True)
    (managed / binary_name(DENO.binary_name)).write_bytes(b"")
    status = setup_service.resolve_dependency(DENO, tmp_path / "profile")
    assert status.origin == "managed"


def test_resolve_dependency_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_service, "bundled_bin_dirs", list)
    monkeypatch.setattr(setup_service, "system_binary_path", lambda _dep: None)
    status = setup_service.resolve_dependency(DENO, tmp_path / "profile")
    assert status.origin == "missing"
    assert status.path is None


def test_resolve_engine_status_finds_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled = tmp_path / "install" / "bin"
    bundled.mkdir(parents=True)
    (bundled / "fake-engine").write_bytes(b"")
    monkeypatch.setattr(setup_service, "bin_search_dirs", lambda _app_data: [bundled])

    conn = connect(tmp_path / "app.db")
    try:
        migrate(conn)
        EngineSettingsStore(conn).set("fake", "source", "auto")
        engine: Engine = SimpleNamespace(
            engine_id="fake",
            display_name="Fake",
            binary_name="fake-engine",
            probe_version=lambda _path: "1.2.3",
        )
        status = setup_service.resolve_engine_status(engine, conn, tmp_path)
    finally:
        conn.close()

    assert status.origin == "bundled"
    assert status.version == "1.2.3"


def test_sidecar_manifest_covers_supported_platforms() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "packaging" / "sidecars.lock.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    platforms = manifest["platforms"]
    for key in ("windows-x86_64", "linux-x86_64"):
        entries = platforms[key]["sidecars"]
        assert {entry["id"] for entry in entries} == {"yt-dlp", "deno", "ffmpeg"}
        for entry in entries:
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            assert entry["verify"]
