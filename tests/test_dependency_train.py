"""Dependency-train helpers and policy consistency (no network)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPS_PATH = ROOT / "packaging" / "deps.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("ytpaw_deps", DEPS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deps() -> Any:
    return _load()


def test_version_comparison(deps: Any) -> None:
    assert deps._version_key("1.10.2") > deps._version_key("1.9.9")
    assert deps._is_newer("2026.10.21", "2026.08.19")
    assert not deps._is_newer("2026.08.19", "2026.08.19")
    assert deps._is_newer("2.9.8", "2.9.7")
    assert deps._clean_tag("v2.9.7") == "2.9.7"
    assert deps._clean_tag("2026.10.21") == "2026.10.21"
    parsed = deps._parse_iso("2026-01-01T00:00:00")
    assert parsed is not None
    assert parsed.tzinfo is dt.UTC


def test_pin_read_and_rewrite(deps: Any) -> None:
    text = (
        '[project]\n'
        'dependencies = [\n'
        '  "PySide6==6.11.2",\n'
        '  "ruff==0.16.8",\n'
        ']\n'
    )
    pins = deps._read_pins(text)
    assert pins["pyside6"] == ("PySide6", "6.11.2")
    assert pins["ruff"] == ("ruff", "0.16.8")
    updated = deps._replace_pins(text, {"pyside6": ("6.11.2", "6.12.0")})
    assert '"PySide6==6.12.0"' in updated
    assert '"ruff==0.16.8"' in updated


def test_checksum_selection(deps: Any) -> None:
    first = "a" * 64
    second = "b" * 64
    text = f"{first}  yt-dlp.exe\n{second}  yt-dlp_linux\n"
    assert deps._select_checksum(text, "yt-dlp.exe") == first
    assert deps._select_checksum(text, "yt-dlp_linux") == second
    assert deps._select_checksum("nothing here", "missing") is None
    assert deps._select_checksum(f"{first}\n", "missing") == first
    assert deps._select_checksum(f"{first}  a\n{second}  b\n", "missing") is None


def test_notice_version_rewrite(deps: Any) -> None:
    text = "| Deno | 2.9.7 | MIT |\n| yt-dlp | 2026.08.19 | Unlicense |\n"
    updated, changed = deps._replace_notice_version(text, "Deno", "2.9.8")
    assert changed
    assert "| Deno | 2.9.8 | MIT |" in updated
    same, changed = deps._replace_notice_version(updated, "Deno", "2.9.8")
    assert not changed
    assert same == updated


def test_update_notices_writes(
    deps: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "THIRD_PARTY_NOTICES.md"
    path.write_text("| Deno | 2.9.7 | MIT |\n", encoding="utf-8")
    monkeypatch.setattr(deps, "NOTICES_PATH", path)
    assert deps._update_notices({"Deno": "2.9.8"}) == ["Deno 2.9.8"]
    assert "| Deno | 2.9.8 | MIT |" in path.read_text(encoding="utf-8")


def test_policy_inheritance(deps: Any) -> None:
    policy = {
        "python": {
            "min_age_days": 30,
            "components": {
                "dev": {"packages": ["ruff"], "cadence_days": 120},
                "qt": {"packages": ["PySide6"], "cadence_days": 180},
            },
        },
        "sidecars": {
            "min_age_days": 30,
            "components": {
                "yt-dlp": {"cadence_days": 90, "min_age_days": 7},
            },
        },
    }
    components = deps._policy_components(policy)
    assert components["dev"]["min_age_days"] == 30
    assert components["dev"]["cadence_days"] == 120
    assert components["qt"]["min_age_days"] == 30
    assert components["yt-dlp"]["min_age_days"] == 7
    assert components["yt-dlp"]["kind"] == "sidecar"


def test_due_dates_and_state(deps: Any) -> None:
    today = dt.date(2026, 10, 25)
    state: dict[str, Any] = {"reviewed": {"dev": "2026-10-25"}}
    assert deps._next_due(state, "dev", 120, today) == dt.date(2027, 2, 22)
    assert deps._next_due(state, "missing", 120, today) == today
    deps._mark_reviewed(state, ["dev", "qt"], today)
    assert state["reviewed"]["dev"] == "2026-10-25"
    assert state["reviewed"]["qt"] == "2026-10-25"


def test_pypi_candidate_respects_age(deps: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = {
        "1.0.0": [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}],
        "1.1.0": [{"upload_time_iso_8601": "2026-09-01T00:00:00Z"}],
        "1.2.0": [{"upload_time_iso_8601": "2026-10-01T00:00:00Z"}],
        "2.0.0rc1": [{"upload_time_iso_8601": "2026-09-20T00:00:00Z"}],
    }

    def fake_fetch(_url: str) -> dict[str, object]:
        return {"releases": releases}

    monkeypatch.setattr(deps, "_fetch_json", fake_fetch)
    candidate = deps._pypi_candidate("ruff", 30, dt.date(2026, 10, 25))
    assert candidate is not None
    assert candidate["version"] == "1.1.0"


def test_python_update_uses_per_package_cutoff(
    deps: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = [\n  "ruff==0.16.8",\n]\n', encoding="utf-8"
    )
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("", encoding="utf-8")
    monkeypatch.setattr(deps, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(deps, "NOTICES_PATH", notices)
    monkeypatch.setattr(
        deps,
        "_pypi_candidate",
        lambda _name, _age, _today: {"version": "0.16.9"},
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool = True) -> None:
        assert check
        calls.append(command)

    monkeypatch.setattr(deps, "_run", fake_run)
    components = {
        "dev": {
            "kind": "python",
            "packages": ["ruff"],
            "cadence_days": 120,
            "min_age_days": 30,
        }
    }
    actions, errors = deps._update_python(
        ["dev"], components, dt.date(2026, 10, 25), security=False, dry_run=False
    )
    assert errors == []
    assert actions[0]["status"] == "updated"
    assert "ruff==0.16.9" in pyproject.read_text(encoding="utf-8")
    assert calls == [
        [
            "uv",
            "lock",
            "--upgrade-package",
            "ruff",
            "--exclude-newer-package",
            "ruff=2026-09-25T23:59:59.999999Z",
        ]
    ]


def test_python_update_restores_lock_on_failure(
    deps: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = [\n  "ruff==0.16.8",\n]\n', encoding="utf-8"
    )
    lock = tmp_path / "uv.lock"
    lock.write_text("old-lock\n", encoding="utf-8")
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("", encoding="utf-8")
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setattr(deps, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(deps, "NOTICES_PATH", notices)
    monkeypatch.setattr(
        deps,
        "_pypi_candidate",
        lambda _name, _age, _today: {"version": "0.16.9"},
    )

    def fake_run(_command: list[str], *, check: bool = True) -> None:
        assert check
        raise RuntimeError("lock failed")

    monkeypatch.setattr(deps, "_run", fake_run)
    components = {
        "dev": {
            "kind": "python",
            "packages": ["ruff"],
            "cadence_days": 120,
            "min_age_days": 30,
        }
    }
    actions, errors = deps._update_python(
        ["dev"], components, dt.date(2026, 10, 25), security=False, dry_run=False
    )
    assert not actions
    assert any("lock failed" in error for error in errors)
    assert "ruff==0.16.8" in pyproject.read_text(encoding="utf-8")
    assert lock.read_text(encoding="utf-8") == "old-lock\n"


def test_sidecar_status_marks_cooling(
    deps: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        (
            "windows-x86_64",
            {
                "id": "yt-dlp",
                "version": "2026.08.19",
                "repo": "yt-dlp/yt-dlp",
                "url": "https://example.invalid/yt-dlp.exe",
            },
        )
    ]
    monkeypatch.setattr(
        deps,
        "_github_release",
        lambda _repo, _token: (
            "2026.10.21",
            {"yt-dlp.exe": "https://example.invalid/yt-dlp.exe"},
            dt.date(2026, 10, 21),
        ),
    )
    current, available, error, cooling = deps._sidecar_status(
        "yt-dlp", entries, None, False, 7, dt.date(2026, 10, 25)
    )
    assert current == "2026.08.19"
    assert available == "2026.10.21"
    assert error is None
    assert cooling


def test_sidecar_entries(deps: Any) -> None:
    lock = {
        "platforms": {
            "windows-x86_64": {
                "sidecars": [{"id": "yt-dlp", "repo": "yt-dlp/yt-dlp"}]
            },
            "linux-x86_64": {
                "sidecars": [{"id": "yt-dlp", "repo": "yt-dlp/yt-dlp"}]
            },
        }
    }
    entries = deps._sidecar_entries(lock, "yt-dlp")
    assert [platform for platform, _ in entries] == [
        "windows-x86_64",
        "linux-x86_64",
    ]


def test_repository_policy_matches_pins(deps: Any) -> None:
    policy = json.loads(
        (ROOT / "packaging" / "dependency-policy.json").read_text(encoding="utf-8")
    )
    state = json.loads(
        (ROOT / "packaging" / "dependency-state.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "packaging" / "sidecars.lock.json").read_text(encoding="utf-8")
    )
    pins = deps._read_pins((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    components = deps._policy_components(policy)
    assert set(state["reviewed"]) == set(components)
    sidecar_ids = {
        str(entry["id"])
        for data in lock["platforms"].values()
        for entry in data["sidecars"]
    }
    for name, spec in components.items():
        if spec["kind"] == "python":
            package_names = {str(package).lower() for package in spec["packages"]}
            assert package_names <= set(pins)
        else:
            assert name in sidecar_ids
            entries = deps._sidecar_entries(lock, name)
            assert entries
            assert all(entry.get("repo") for _, entry in entries)
    assert deps._notice_mismatches() == []
