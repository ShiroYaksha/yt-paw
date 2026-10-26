"""Fetch the pinned sidecars (yt-dlp, Deno, FFmpeg) for one platform.

The lock file (`sidecars.lock.json`) contains exact URLs plus SHA-256 digests,
grouped per platform. Downloads are cached under `build/sidecar-cache/` and
verified before use, so a rolled upstream release fails the build instead of
silently shipping new bytes.

    python packaging/fetch_sidecars.py                    # host platform
    python packaging/fetch_sidecars.py --platform linux-x86_64
    python packaging/fetch_sidecars.py --force            # ignore cache
    python packaging/fetch_sidecars.py --update-pins      # adopt host hashes
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("sidecars.lock.json")
CACHE = ROOT / "build" / "sidecar-cache"

_HEX64 = re.compile(r"\b([0-9a-fA-F]{64})\b")
_CHUNK = 1024 * 1024
_TIMEOUT = 180
_UA = "yt-paw-build/0.1"


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def output_root(platform_id: str) -> Path:
    return ROOT / "build" / "sidecars" / platform_id


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": _UA})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(_request(url), timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(_request(url), timeout=_TIMEOUT) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        next_mark = 10
        with partial.open("wb") as handle:
            while chunk := response.read(_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= next_mark:
                        print(f"  {dest.name}: {pct:3d}% ({done / 1e6:.1f} MB)")
                        next_mark = (pct // 10 + 1) * 10
    print(f"  {dest.name}: downloaded ({done / 1e6:.1f} MB)")
    partial.replace(dest)
    return dest


def select_checksum(text: str, asset_name: str) -> str | None:
    """Prefer the line naming the asset; fall back to the first 64-hex token."""
    for line in text.splitlines():
        if asset_name in line:
            match = _HEX64.search(line)
            if match:
                return match.group(1).lower()
    match = _HEX64.search(text)
    return match.group(1).lower() if match else None


def get_asset(entry: dict[str, object], force: bool) -> Path:
    url = str(entry["url"])
    expected = str(entry["sha256"]).lower()
    dest = CACHE / Path(urlparse(url).path).name
    if dest.exists() and not force and sha256_file(dest) == expected:
        print(f"  {dest.name}: cached")
        return dest
    if dest.exists():
        dest.unlink()
    print(f"  {dest.name}: downloading {url}")
    download(url, dest)
    actual = sha256_file(dest)
    if actual != expected:
        raise SystemExit(
            f"SHA-256 mismatch for {entry['id']}:\n"
            f"  expected {expected}\n  actual   {actual}\n"
            "The upstream release may have rolled. Review it, then run:\n"
            "  python packaging/fetch_sidecars.py --update-pins"
        )
    return dest


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as source, target.open("wb") as sink:
        shutil.copyfileobj(source, sink)
    mode = (info.external_attr >> 16) & 0o777
    if mode:
        target.chmod(mode)


def _member_target(root: Path, dest: str, member_name: str) -> Path:
    name = Path(member_name).name
    target = root / dest / name if dest.endswith("/") else root / dest
    resolved = target.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SystemExit(f"unsafe extraction target {target}")
    return target


def _extract_tar(entry: dict[str, object], archive: Path, root: Path) -> None:
    specs = entry.get("files", [])
    if not isinstance(specs, list):
        raise SystemExit(f"{entry['id']}: 'files' must be a list")
    licenses = entry.get("licenses", {})
    if not isinstance(licenses, dict):
        raise SystemExit(f"{entry['id']}: licenses must be a mapping")
    with tarfile.open(archive, "r:xz") as tf:
        members = [member for member in tf.getmembers() if member.isfile() or member.issym()]
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            pattern = str(spec["pattern"])
            dest = str(spec["dest"])
            matches = [member for member in members if fnmatch.fnmatch(member.name, pattern)]
            if not matches:
                raise SystemExit(f"{entry['id']}: no tar member matches {pattern!r}")
            for member in matches:
                target = _member_target(root, dest, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                if member.issym():
                    link = member.linkname
                    if os.path.isabs(link) or ".." in Path(link).parts:
                        raise SystemExit(f"{entry['id']}: unsafe symlink {member.name} -> {link}")
                    os.symlink(link, target)
                    continue
                source = tf.extractfile(member)
                if source is None:
                    raise SystemExit(f"{entry['id']}: cannot read {member.name}")
                with source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                target.chmod(member.mode & 0o777 or 0o644)
        for pattern, dest_name in licenses.items():
            matches = [member for member in members if fnmatch.fnmatch(member.name, str(pattern))]
            if not matches:
                raise SystemExit(f"{entry['id']}: no license matches {pattern!r}")
            source = tf.extractfile(matches[0])
            if source is None:
                raise SystemExit(f"{entry['id']}: cannot read {matches[0].name}")
            target = root / "licenses" / str(dest_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def extract(entry: dict[str, object], archive: Path, root: Path) -> None:
    kind = entry.get("archive")
    if kind == "raw":
        target = _member_target(root, str(entry["target"]), str(entry["target"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, target)
        return
    if kind == "tar.xz":
        _extract_tar(entry, archive, root)
        return
    if kind != "zip":
        raise SystemExit(f"{entry['id']}: unsupported archive {kind!r}")

    patterns = [str(p) for p in entry.get("extract", [])]
    licenses = entry.get("licenses", {})
    if not isinstance(licenses, dict):
        raise SystemExit(f"{entry['id']}: licenses must be a mapping")
    with zipfile.ZipFile(archive) as zf:
        for pattern in patterns:
            matches = [
                info
                for info in zf.infolist()
                if not info.is_dir() and fnmatch.fnmatch(info.filename, pattern)
            ]
            if not matches:
                raise SystemExit(f"{entry['id']}: no zip member matches {pattern!r}")
            for info in matches:
                _write_member(zf, info, root / "bin" / Path(info.filename).name)
        for pattern, dest_name in licenses.items():
            matches = [
                info
                for info in zf.infolist()
                if not info.is_dir() and fnmatch.fnmatch(info.filename, str(pattern))
            ]
            if not matches:
                raise SystemExit(f"{entry['id']}: no license matches {pattern!r}")
            _write_member(zf, matches[0], root / "licenses" / str(dest_name))


def verify(entry: dict[str, object], root: Path) -> None:
    patterns = [str(p) for p in entry.get("verify", [])]
    missing = [pattern for pattern in patterns if not any(root.glob(pattern))]
    if missing:
        raise SystemExit(f"{entry['id']}: missing extracted files: {', '.join(missing)}")


def update_pins(manifest: dict[str, object], platform_ids: list[str]) -> int:
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise SystemExit("sidecars lock: 'platforms' must be a mapping")
    changed = False
    for platform_id in platform_ids:
        platform_entry = platforms.get(platform_id)
        if not isinstance(platform_entry, dict):
            raise SystemExit(f"unknown platform {platform_id!r}")
        sidecars = platform_entry.get("sidecars", [])
        if not isinstance(sidecars, list):
            raise SystemExit(f"sidecars lock: {platform_id}.sidecars must be a list")
        print(f"{platform_id}:")
        for entry in sidecars:
            if not isinstance(entry, dict):
                continue
            checksum_url = entry.get("checksum_url")
            if not checksum_url:
                raise SystemExit(f"{entry.get('id')}: no checksum_url to update from")
            text = fetch_text(str(checksum_url))
            asset = Path(urlparse(str(entry["url"])).path).name
            checksum = select_checksum(text, asset)
            if checksum is None:
                raise SystemExit(f"{entry.get('id')}: no SHA-256 in {checksum_url}")
            old = str(entry.get("sha256", "")).lower()
            if old != checksum:
                print(f"  {entry.get('id')}: {old[:12]}… -> {checksum[:12]}…")
                entry["sha256"] = checksum
                changed = True
            else:
                print(f"  {entry.get('id')}: unchanged ({checksum[:12]}…)")
    if changed:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"updated {MANIFEST}")
    return 0


def _platform_ids(manifest: dict[str, object], requested: str | None) -> list[str]:
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise SystemExit("sidecars lock: expected a 'platforms' mapping")
    if requested == "all":
        return [str(key) for key in platforms]
    return [requested or platform_key()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        metavar="KEY",
        help="sidecar platform key, e.g. windows-x86_64 or linux-x86_64 "
        "(default: host; 'all' updates every pin)",
    )
    parser.add_argument("--force", action="store_true", help="ignore the download cache")
    parser.add_argument(
        "--update-pins",
        action="store_true",
        help="rewrite SHA-256 pins from each upstream checksum file",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="ID",
        help="fetch only this sidecar (repeatable)",
    )
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.update_pins:
        return update_pins(manifest, _platform_ids(manifest, args.platform))

    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise SystemExit("sidecars lock: expected a 'platforms' mapping")
    platform_id = args.platform or platform_key()
    platform_entry = platforms.get(platform_id)
    if not isinstance(platform_entry, dict):
        known = ", ".join(sorted(str(key) for key in platforms))
        raise SystemExit(f"no sidecars for {platform_id!r} (known: {known})")
    sidecars = platform_entry.get("sidecars", [])
    if not isinstance(sidecars, list):
        raise SystemExit(f"sidecars lock: {platform_id}.sidecars must be a list")
    if args.only:
        wanted = set(args.only)
        sidecars = [e for e in sidecars if isinstance(e, dict) and e.get("id") in wanted]
        missing = wanted - {str(e.get("id")) for e in sidecars}
        if missing:
            raise SystemExit(f"unknown sidecar id(s): {', '.join(sorted(missing))}")

    root = output_root(platform_id)
    shutil.rmtree(root, ignore_errors=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "licenses").mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    print(f"Sidecars for {platform_id}:")
    for entry in sidecars:
        if not isinstance(entry, dict):
            continue
        print(f"- {entry.get('id')} {entry.get('version', '')}".rstrip())
        archive = get_asset(entry, args.force)
        extract(entry, archive, root)
        verify(entry, root)

    if sys.platform != "win32":
        for path in (root / "bin").iterdir():
            if path.is_file():
                path.chmod(0o755)

    files = sorted(path for path in root.rglob("*") if path.is_file())
    total = sum(path.lstat().st_size for path in files)
    print(f"\n{len(files)} files, {total / 1e6:.1f} MB in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
