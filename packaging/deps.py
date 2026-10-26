"""Low-frequency dependency train for yt-paw.

This is the single entry point a maintainer or automated agent should use.
It keeps updates intentionally infrequent: each component has a cadence and a
minimum release age in ``packaging/dependency-policy.json``. A component is
only refreshed when its next-due date has arrived, unless ``--force`` or
``--security`` is passed.

    python packaging/deps.py status
    python packaging/deps.py status --format json
    python packaging/deps.py update --due
    python packaging/deps.py update --due --dry-run
    python packaging/deps.py verify

Policy and state are ordinary JSON files so the process survives long gaps:
read ``docs/dependency-updates.md`` for the full runbook.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "packaging" / "dependency-policy.json"
STATE_PATH = ROOT / "packaging" / "dependency-state.json"
LOCK_PATH = ROOT / "packaging" / "sidecars.lock.json"
PYPROJECT_PATH = ROOT / "pyproject.toml"
NOTICES_PATH = ROOT / "packaging" / "THIRD_PARTY_NOTICES.md"

_UA = "yt-paw-deps/1"
_TIMEOUT = 60
_NUMERIC = re.compile(r"^\d+(?:\.\d+)*$")
_PIN = re.compile(
    r'(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?P<extras>\[[^\]]*\])?'
    r'==(?P<version>[0-9][^\s"\',;]*)'
)
_HEX = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _load(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return data


def _save(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _cutoff(today: dt.date, days: int) -> dt.datetime:
    return dt.datetime.combine(
        today - dt.timedelta(days=days), dt.time.max, tzinfo=dt.UTC
    )


def _parse_date(value: object) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_iso(value: str) -> dt.datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _is_newer(latest: str, current: str) -> bool:
    if not _NUMERIC.fullmatch(latest) or not _NUMERIC.fullmatch(current):
        return latest != current
    return _version_key(latest) > _version_key(current)


def _clean_tag(tag: str) -> str:
    return tag[1:] if tag.startswith(("v", "V")) else tag


def _request(
    url: str,
    *,
    token: str | None = None,
    accept: str | None = None,
) -> urllib.request.Request:
    headers = {"User-Agent": _UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    return urllib.request.Request(url, headers=headers)


def _fetch_text(url: str, *, token: str | None = None) -> str:
    with urllib.request.urlopen(_request(url, token=token), timeout=_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _fetch_json(url: str, *, token: str | None = None) -> dict[str, object]:
    data = json.loads(_fetch_text(url, token=token))
    if not isinstance(data, dict):
        raise TypeError(f"{url}: expected a JSON object")
    return data


def _github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or None


def _pypi_candidate(name: str, min_age: int, today: dt.date) -> dict[str, str] | None:
    data = _fetch_json(f"https://pypi.org/pypi/{name}/json")
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return None
    cutoff = _cutoff(today, min_age)
    best_key: tuple[int, ...] | None = None
    best: dict[str, str] | None = None
    for version, files in releases.items():
        if not isinstance(version, str) or not _NUMERIC.fullmatch(version):
            continue
        if not isinstance(files, list):
            continue
        uploaded: list[dt.datetime] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            raw = item.get("upload_time_iso_8601") or item.get("upload_time")
            if isinstance(raw, str):
                parsed = _parse_iso(raw)
                if parsed is not None:
                    uploaded.append(parsed)
        if not uploaded:
            continue
        published = max(uploaded)
        if published > cutoff:
            continue
        key = _version_key(version)
        if best_key is None or key > best_key:
            best_key = key
            best = {"version": version, "published": published.date().isoformat()}
    return best


def _read_pins(text: str) -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    for match in _PIN.finditer(text):
        pins[match.group("name").lower()] = (match.group("name"), match.group("version"))
    return pins


def _replace_pins(text: str, updates: dict[str, tuple[str, str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name").lower()
        if name not in updates:
            return match.group(0)
        old, new = updates[name]
        if match.group("version") != old:
            return match.group(0)
        return f"{match.group('name')}{match.group('extras') or ''}=={new}"

    return _PIN.sub(replace, text)


def _github_release(repo: str, token: str | None) -> tuple[str, dict[str, str], dt.date | None]:
    data = _fetch_json(
        f"https://api.github.com/repos/{repo}/releases/latest",
        token=token,
    )
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise ValueError(f"{repo}: latest release has no tag")
    assets: dict[str, str] = {}
    raw_assets = data.get("assets")
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            url = item.get("browser_download_url")
            if isinstance(name, str) and isinstance(url, str):
                assets[name] = url
    published = None
    raw_published = data.get("published_at")
    if isinstance(raw_published, str):
        parsed = _parse_iso(raw_published)
        if parsed is not None:
            published = parsed.date()
    return tag, assets, published


def _select_checksum(text: str, asset_name: str) -> str | None:
    for line in text.splitlines():
        if asset_name in line:
            match = _HEX.search(line)
            if match:
                return match.group(1).lower()
    hashes = _HEX.findall(text)
    return hashes[0].lower() if len(hashes) == 1 else None


def _replace_notice_version(text: str, label: str, version: str) -> tuple[str, bool]:
    pattern = re.compile(rf"^(\| {re.escape(label)} \| )([^|]+)( \|)", re.MULTILINE)
    updated, count = pattern.subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count == 0:
        raise ValueError(f"notices: row not found for {label}")
    return updated, updated != text


def _update_notices(versions: dict[str, str]) -> list[str]:
    text = NOTICES_PATH.read_text(encoding="utf-8")
    changed: list[str] = []
    for label, version in versions.items():
        text, did_change = _replace_notice_version(text, label, version)
        if did_change:
            changed.append(f"{label} {version}")
    if changed:
        NOTICES_PATH.write_text(text, encoding="utf-8")
    return changed


def _notice_version(text: str, label: str) -> str | None:
    pattern = re.compile(rf"^\| {re.escape(label)} \| ([^|]+) \|", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _notice_mismatches() -> list[str]:
    text = NOTICES_PATH.read_text(encoding="utf-8")
    pins = _read_pins(PYPROJECT_PATH.read_text(encoding="utf-8"))
    lock = _load(LOCK_PATH)
    expected: dict[str, str] = {}
    if "pyside6" in pins:
        expected["PySide6 / Qt"] = pins["pyside6"][1]
    mismatches: list[str] = []
    for component, label in (("deno", "Deno"), ("yt-dlp", "yt-dlp")):
        versions = sorted(
            {str(entry.get("version", "")) for _, entry in _sidecar_entries(lock, component)}
        )
        if len(versions) == 1:
            expected[label] = versions[0]
        elif len(versions) > 1:
            mismatches.append(f"{label}: platform versions differ: {versions}")
    for label, version in expected.items():
        actual = _notice_version(text, label)
        if actual != version:
            mismatches.append(f"{label}: notices {actual!r}, pinned {version!r}")
    return mismatches


def _policy_components(policy: dict[str, object]) -> dict[str, dict[str, object]]:
    components: dict[str, dict[str, object]] = {}
    python = policy.get("python")
    if isinstance(python, dict):
        default_age = int(python.get("min_age_days", 30))
        groups = python.get("components")
        if isinstance(groups, dict):
            for name, spec in groups.items():
                if not isinstance(spec, dict):
                    continue
                packages = spec.get("packages")
                if not isinstance(packages, list):
                    continue
                components[str(name)] = {
                    "kind": "python",
                    "packages": [str(package) for package in packages],
                    "cadence_days": int(spec.get("cadence_days", 180)),
                    "min_age_days": int(spec.get("min_age_days", default_age)),
                }
    sidecars = policy.get("sidecars")
    if isinstance(sidecars, dict):
        default_age = int(sidecars.get("min_age_days", 30))
        groups = sidecars.get("components")
        if isinstance(groups, dict):
            for name, spec in groups.items():
                if not isinstance(spec, dict):
                    continue
                components[str(name)] = {
                    "kind": "sidecar",
                    "cadence_days": int(spec.get("cadence_days", 180)),
                    "min_age_days": int(spec.get("min_age_days", default_age)),
                }
    return components


def _reviewed(state: dict[str, object], name: str) -> dt.date | None:
    reviewed = state.get("reviewed")
    if not isinstance(reviewed, dict):
        return None
    return _parse_date(reviewed.get(name))


def _next_due(state: dict[str, object], name: str, cadence_days: int, today: dt.date) -> dt.date:
    last = _reviewed(state, name)
    return today if last is None else last + dt.timedelta(days=cadence_days)


def _mark_reviewed(state: dict[str, object], names: Iterable[str], today: dt.date) -> None:
    reviewed = state.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        state["reviewed"] = reviewed
    for name in names:
        reviewed[name] = today.isoformat()


def _sidecar_entries(
    lock: dict[str, object], component: str
) -> list[tuple[str, dict[str, object]]]:
    platforms = lock.get("platforms")
    if not isinstance(platforms, dict):
        return []
    entries: list[tuple[str, dict[str, object]]] = []
    for platform, data in platforms.items():
        if not isinstance(data, dict):
            continue
        sidecars = data.get("sidecars")
        if not isinstance(sidecars, list):
            continue
        for entry in sidecars:
            if isinstance(entry, dict) and entry.get("id") == component:
                entries.append((str(platform), entry))
    return entries


def _sidecar_status(
    component: str,
    entries: list[tuple[str, dict[str, object]]],
    token: str | None,
    offline: bool,
    min_age: int,
    today: dt.date,
) -> tuple[str, str | None, str | None, bool]:
    if not entries:
        return "missing", None, f"{component}: not present in sidecars.lock.json", False
    current = sorted({str(entry.get("version", "unknown")) for _, entry in entries})
    current_text = ", ".join(current)
    if offline:
        return current_text, None, None, False
    repo = str(entries[0][1].get("repo") or "")
    if not repo:
        return current_text, None, f"{component}: sidecar entry has no repo", False
    try:
        if component == "ffmpeg":
            changed = False
            for _, entry in entries:
                checksum_url = str(entry.get("checksum_url") or "")
                asset = Path(str(entry.get("url") or "")).name
                text = _fetch_text(checksum_url, token=token)
                checksum = _select_checksum(text, asset)
                if checksum is None:
                    raise ValueError(f"{component}: no checksum for {asset}")
                if checksum != str(entry.get("sha256", "")).lower():
                    changed = True
            return current_text, "hash changed" if changed else "current", None, False
        tag, assets, published = _github_release(repo, token)
        latest = _clean_tag(tag)
        available: str | None = None
        if any(_is_newer(latest, version) for version in current):
            available = latest
        cooling = bool(
            available
            and published is not None
            and published > today - dt.timedelta(days=min_age)
        )
        for _, entry in entries:
            asset = Path(str(entry.get("url") or "")).name
            if asset not in assets:
                raise ValueError(f"{component}: release {tag} has no asset {asset}")
        return current_text, available, None, cooling
    except (OSError, TypeError, ValueError, urllib.error.URLError) as exc:
        return current_text, None, f"{component}: {exc}", False


def _pypi_status(
    component: str,
    packages: list[str],
    pins: dict[str, tuple[str, str]],
    min_age: int,
    today: dt.date,
    offline: bool,
) -> tuple[str, str | None, str | None]:
    current: list[str] = []
    available: list[str] = []
    try:
        for package in packages:
            pin = pins.get(package.lower())
            if pin is None:
                raise ValueError(f"{component}: {package} is not exactly pinned in pyproject.toml")
            current.append(f"{pin[0]} {pin[1]}")
            if offline:
                continue
            candidate = _pypi_candidate(package, min_age, today)
            if candidate is not None and _is_newer(candidate["version"], pin[1]):
                available.append(f"{pin[0]} {candidate['version']}")
        return ", ".join(current), ", ".join(available) or None, None
    except (OSError, TypeError, ValueError, urllib.error.URLError) as exc:
        return ", ".join(current), None, f"{component}: {exc}"


def collect_status(offline: bool) -> dict[str, object]:
    policy = _load(POLICY_PATH)
    state = _load(STATE_PATH)
    lock = _load(LOCK_PATH)
    today = _today()
    pins = _read_pins(PYPROJECT_PATH.read_text(encoding="utf-8"))
    components = _policy_components(policy)
    token = _github_token()
    rows: list[dict[str, object]] = []
    for name, spec in components.items():
        cadence = int(spec["cadence_days"])
        min_age = int(spec["min_age_days"])
        next_due = _next_due(state, name, cadence, today)
        cooling = False
        if spec["kind"] == "python":
            packages = spec["packages"]
            assert isinstance(packages, list)
            current, available, error = _pypi_status(
                name, [str(package) for package in packages], pins, min_age, today, offline
            )
        else:
            current, available, error, cooling = _sidecar_status(
                name, _sidecar_entries(lock, name), token, offline, min_age, today
            )
        rows.append(
            {
                "name": name,
                "kind": spec["kind"],
                "current": current,
                "available": available,
                "cooling": cooling,
                "due": today >= next_due,
                "next_due": next_due.isoformat(),
                "cadence_days": cadence,
                "min_age_days": min_age,
                "error": error,
            }
        )
    return {
        "date": today.isoformat(),
        "due": any(bool(row["due"]) for row in rows),
        "components": rows,
    }


def _format_text(report: dict[str, object]) -> str:
    rows = report.get("components")
    lines = [f"Dependency train — {report.get('date')}"]
    lines.append(f"Due now: {'yes' if report.get('due') else 'no'}")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            flag = "DUE" if row.get("due") else "ok"
            available = str(row.get("available") or "current")
            if row.get("cooling"):
                available = f"{available} (cooling)"
            error = row.get("error")
            lines.append(
                f"[{flag:3}] {row.get('name'):10} {row.get('current')} "
                f"-> {available} (next {row.get('next_due')})"
            )
            if error:
                lines.append(f"      ! {error}")
    return "\n".join(lines)


def _format_markdown(report: dict[str, object]) -> str:
    lines = [f"### Dependency train — {report.get('date')}", ""]
    lines.append(f"**Due now:** {'yes' if report.get('due') else 'no'}")
    lines.append("")
    lines.append("| Component | Current | Available | Due | Next due |")
    lines.append("|-----------|---------|-----------|-----|----------|")
    rows = report.get("components")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            available = str(row.get("available") or "current")
            if row.get("cooling"):
                available = f"{available} (cooling)"
            lines.append(
                f"| {row.get('name')} | {row.get('current')} | "
                f"{available} | "
                f"{'yes' if row.get('due') else 'no'} | {row.get('next_due')} |"
            )
            if row.get("error"):
                lines.append(f"|  |  | ! {row.get('error')} |  |  |")
    return "\n".join(lines)


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}")
    return result


def _update_python(
    names: list[str],
    components: dict[str, dict[str, object]],
    today: dt.date,
    *,
    security: bool,
    dry_run: bool,
) -> tuple[list[dict[str, object]], list[str]]:
    actions: list[dict[str, object]] = []
    errors: list[str] = []
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    pins = _read_pins(text)
    group_updates: dict[str, dict[str, tuple[str, str]]] = {name: {} for name in names}
    group_changes: dict[str, list[str]] = {name: [] for name in names}
    group_ok: dict[str, bool] = {name: True for name in names}
    originals: dict[str, str] = {}
    package_cutoffs: dict[str, str] = {}
    for name in names:
        spec = components[name]
        packages = [str(package) for package in spec["packages"]]
        for package in packages:
            pin = pins.get(package.lower())
            if pin is None:
                errors.append(f"{name}: {package} is not exactly pinned in pyproject.toml")
                group_ok[name] = False
                continue
            try:
                min_age = 0 if security else int(spec["min_age_days"])
                candidate = _pypi_candidate(package, min_age, today)
            except (OSError, TypeError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{name}: {package}: {exc}")
                group_ok[name] = False
                continue
            if candidate is not None and _is_newer(candidate["version"], pin[1]):
                group_updates[name][package.lower()] = (pin[1], candidate["version"])
                group_changes[name].append(f"{pin[0]} {pin[1]} -> {candidate['version']}")
                originals[package.lower()] = pin[0]
                package_cutoffs[package.lower()] = (
                    _cutoff(today, min_age).isoformat().replace("+00:00", "Z")
                )
    updates: dict[str, tuple[str, str]] = {}
    for name, values in group_updates.items():
        if group_ok[name]:
            updates.update(values)
    if updates and not dry_run:
        new_text = _replace_pins(text, updates)
        new_pins = _read_pins(new_text)
        for package, (_, new) in updates.items():
            if new_pins.get(package, ("", ""))[1] != new:
                errors.append(f"python: could not rewrite pin for {package}")
                group_ok = {name: False for name in names}
                updates.clear()
                break
        if updates:
            lock_path = ROOT / "uv.lock"
            lock_before = lock_path.read_bytes() if lock_path.exists() else None
            PYPROJECT_PATH.write_text(new_text, encoding="utf-8")
            try:
                command = ["uv", "lock"]
                for package in updates:
                    command.extend(["--upgrade-package", originals[package]])
                    command.extend(
                        [
                            "--exclude-newer-package",
                            f"{originals[package]}={package_cutoffs[package]}",
                        ]
                    )
                _run(command)
                if "pyside6" in updates:
                    try:
                        _update_notices({"PySide6 / Qt": updates["pyside6"][1]})
                    except (OSError, ValueError) as exc:
                        errors.append(f"notices: {exc}")
            except (OSError, RuntimeError) as exc:
                PYPROJECT_PATH.write_text(text, encoding="utf-8")
                if lock_before is None:
                    lock_path.unlink(missing_ok=True)
                else:
                    lock_path.write_bytes(lock_before)
                errors.append(f"python: {exc}")
                for name in names:
                    if group_updates[name]:
                        group_ok[name] = False
                updates.clear()
    for name in names:
        if not group_ok[name]:
            continue
        changes = group_changes[name] if updates or dry_run else []
        actions.append(
            {
                "component": name,
                "kind": "python",
                "status": "planned" if changes and dry_run else ("updated" if changes else "current"),
                "changes": changes,
                "dry_run": dry_run,
            }
        )
    return actions, errors


def _update_sidecars(
    lock: dict[str, object],
    names: list[str],
    components: dict[str, dict[str, object]],
    today: dt.date,
    *,
    security: bool,
    dry_run: bool,
) -> tuple[list[dict[str, object]], list[str], bool]:
    actions: list[dict[str, object]] = []
    errors: list[str] = []
    token = _github_token()
    changed_lock = False
    for name in names:
        entries = _sidecar_entries(lock, name)
        if not entries:
            errors.append(f"{name}: not present in sidecars.lock.json")
            continue
        current = sorted({str(entry.get("version", "unknown")) for _, entry in entries})
        repo = str(entries[0][1].get("repo") or "")
        if not repo:
            errors.append(f"{name}: sidecar entry has no repo")
            continue
        try:
            if name == "ffmpeg":
                changes = []
                for platform, entry in entries:
                    checksum_url = str(entry.get("checksum_url") or "")
                    asset = Path(str(entry.get("url") or "")).name
                    text = _fetch_text(checksum_url, token=token)
                    checksum = _select_checksum(text, asset)
                    if checksum is None:
                        raise ValueError(f"no checksum for {asset}")
                    old = str(entry.get("sha256", "")).lower()
                    if checksum != old:
                        changes.append(f"{platform}: {old[:12]} -> {checksum[:12]}")
                        if not dry_run:
                            entry["sha256"] = checksum
                            changed_lock = True
                actions.append(
                    {
                        "component": name,
                        "kind": "sidecar",
                        "status": (
                            "planned"
                            if changes and dry_run
                            else ("updated" if changes else "current")
                        ),
                        "changes": changes,
                        "dry_run": dry_run,
                    }
                )
                continue

            tag, assets, published = _github_release(repo, token)
            latest = _clean_tag(tag)
            if not any(_is_newer(latest, version) for version in current):
                actions.append(
                    {
                        "component": name,
                        "kind": "sidecar",
                        "status": "current",
                        "changes": [],
                        "dry_run": dry_run,
                    }
                )
                continue
            min_age = 0 if security else int(components[name]["min_age_days"])
            if published is not None and published > today - dt.timedelta(days=min_age):
                actions.append(
                    {
                        "component": name,
                        "kind": "sidecar",
                        "status": "cooling",
                        "changes": [f"{latest} released {published.isoformat()}"],
                        "dry_run": dry_run,
                    }
                )
                continue
            planned: list[tuple[dict[str, object], dict[str, str]]] = []
            for _, entry in entries:
                asset = Path(str(entry.get("url") or "")).name
                checksum_name = Path(str(entry.get("checksum_url") or "")).name
                new_url = assets.get(asset)
                new_checksum_url = assets.get(checksum_name)
                if new_url is None:
                    raise ValueError(f"release {tag} has no asset {asset}")
                if new_checksum_url is None:
                    raise ValueError(f"release {tag} has no asset {checksum_name}")
                checksum_text = _fetch_text(new_checksum_url, token=token)
                checksum = _select_checksum(checksum_text, asset)
                if checksum is None:
                    raise ValueError(f"no checksum for {asset} in {tag}")
                planned.append(
                    (
                        entry,
                        {
                            "version": latest,
                            "url": new_url,
                            "checksum_url": new_checksum_url,
                            "sha256": checksum,
                        },
                    )
                )
            changes = [f"{', '.join(current)} -> {latest}"]
            if not dry_run:
                for entry, values in planned:
                    entry.update(values)
                changed_lock = True
                notice = {"yt-dlp": latest} if name == "yt-dlp" else {"Deno": latest}
                try:
                    _update_notices(notice)
                except (OSError, ValueError) as exc:
                    errors.append(f"notices: {exc}")
            actions.append(
                {
                    "component": name,
                    "kind": "sidecar",
                    "status": "planned" if dry_run else "updated",
                    "changes": changes,
                    "dry_run": dry_run,
                }
            )
        except (OSError, TypeError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"{name}: {exc}")
    return actions, errors, changed_lock


def command_status(args: argparse.Namespace) -> int:
    report = collect_status(args.offline)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif args.format == "markdown":
        print(_format_markdown(report))
    else:
        print(_format_text(report))
    errors = [
        row["error"]
        for row in report["components"]
        if isinstance(row, dict) and row.get("error")
    ]
    if args.fail_on_error and errors:
        return 1
    return 0


def command_update(args: argparse.Namespace) -> int:
    policy = _load(POLICY_PATH)
    state = _load(STATE_PATH)
    components = _policy_components(policy)
    if args.component:
        missing = [name for name in args.component if name not in components]
        if missing:
            raise SystemExit(f"unknown component(s): {', '.join(missing)}")
        selected = list(args.component)
    else:
        selected = list(components)
    today = _today()
    if not args.force and not args.security:
        selected = [
            name
            for name in selected
            if today >= _next_due(state, name, int(components[name]["cadence_days"]), today)
        ]
    if not selected:
        message = "nothing due; dependency train is intentionally idle"
        if args.format == "json":
            print(json.dumps({"date": today.isoformat(), "actions": [], "errors": [], "message": message}))
        else:
            print(message)
        return 0

    python_names = [name for name in selected if components[name]["kind"] == "python"]
    sidecar_names = [name for name in selected if components[name]["kind"] == "sidecar"]
    actions: list[dict[str, object]] = []
    errors: list[str] = []
    if python_names:
        python_actions, python_errors = _update_python(
            python_names, components, today, security=args.security, dry_run=args.dry_run
        )
        actions.extend(python_actions)
        errors.extend(python_errors)
    changed_lock = False
    if sidecar_names:
        lock = _load(LOCK_PATH)
        sidecar_actions, sidecar_errors, changed_lock = _update_sidecars(
            lock, sidecar_names, components, today, security=args.security, dry_run=args.dry_run
        )
        actions.extend(sidecar_actions)
        errors.extend(sidecar_errors)
        if changed_lock:
            _save(LOCK_PATH, lock)

    if not args.dry_run:
        completed = [
            str(action["component"])
            for action in actions
            if action.get("status") in ("updated", "current", "cooling")
        ]
        _mark_reviewed(state, completed, today)
        _save(STATE_PATH, state)

    report = {
        "date": today.isoformat(),
        "dry_run": args.dry_run,
        "actions": actions,
        "errors": errors,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"Dependency train — {today.isoformat()}")
        for action in actions:
            print(f"{action['component']}: {action['status']}")
            for change in action.get("changes", []):
                print(f"  {change}")
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
    return 1 if errors else 0


def command_verify(args: argparse.Namespace) -> int:
    mismatches = _notice_mismatches()
    if mismatches:
        for mismatch in mismatches:
            print(f"notices out of date: {mismatch}", file=sys.stderr)
        return 1
    commands = [
        ["uv", "lock", "--check"],
        ["uv", "run", "ruff", "check", "src", "tests", "packaging"],
        ["uv", "run", "mypy", "src", "tests"],
        ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"],
    ]
    if args.build:
        commands.append(["uv", "run", "--group", "build", "python", "packaging/build.py"])
    for command in commands:
        try:
            _run(command)
        except (OSError, RuntimeError) as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="report due/available updates (read-only)")
    status.add_argument("--offline", action="store_true", help="skip upstream checks")
    status.add_argument(
        "--format", choices=("text", "json", "markdown"), default="text"
    )
    status.add_argument("--fail-on-error", action="store_true")
    status.set_defaults(func=command_status)

    update = sub.add_parser("update", help="update components that are due")
    update.add_argument("--due", action="store_true", help="update only components due now")
    update.add_argument(
        "--force", action="store_true", help="ignore the cadence but keep minimum release age"
    )
    update.add_argument(
        "--security", action="store_true", help="ignore cadence and minimum release age"
    )
    update.add_argument("--component", action="append", metavar="NAME")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--format", choices=("text", "json"), default="text")
    update.set_defaults(func=command_update)

    verify = sub.add_parser("verify", help="run the local quality gates")
    verify.add_argument("--build", action="store_true", help="also build the host bundle")
    verify.set_defaults(func=command_verify)

    args = parser.parse_args()
    if args.command == "update" and not (args.due or args.force or args.security):
        parser.error("update requires --due, --force, or --security")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
