"""Build the standalone distribution for the host platform (one folder + archive).

Run on Windows x86_64 or Linux x86_64 with the build group:

    uv run --group build python packaging/build.py

Output is `dist/yt-paw/` plus a ZIP on Windows and a `.tar.gz` on Linux.
PyInstaller does not cross-compile, so the target always matches the host.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SPEC = ROOT / "packaging" / "yt_paw.spec"
FETCH = ROOT / "packaging" / "fetch_sidecars.py"
VERIFY = ROOT / "packaging" / "verify_build.py"
NOTICES = ROOT / "packaging" / "THIRD_PARTY_NOTICES.md"
STATIC_LICENSES = ROOT / "packaging" / "licenses"
BUILD = ROOT / "build"
WORK = BUILD / "pyinstaller"
DIST = ROOT / "dist"
APP_DIR = DIST / "yt-paw"

SUPPORTED = ("windows-x86_64", "linux-x86_64")


def project_version() -> str:
    text = (SRC / "ytpaw" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if match is None:
        raise SystemExit("cannot find __version__ in src/ytpaw/__init__.py")
    return match.group(1)


def platform_label() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, cwd=ROOT, env=env)


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def assemble(include_sidecars: bool, platform_id: str) -> None:
    sidecars = BUILD / "sidecars" / platform_id
    if include_sidecars:
        for name in ("bin", "lib"):
            source = sidecars / name
            if not source.is_dir():
                continue
            target = APP_DIR / name
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
            for path in sorted(source.iterdir()):
                if path.is_file() or path.is_symlink():
                    shutil.copy2(path, target / path.name, follow_symlinks=False)
            print(f"copied {len(list(target.iterdir()))} {name}/ files to {target}")

    licenses = APP_DIR / "licenses"
    if include_sidecars:
        copy_tree(sidecars / "licenses", licenses)
    else:
        shutil.rmtree(APP_DIR / "bin", ignore_errors=True)
        shutil.rmtree(APP_DIR / "lib", ignore_errors=True)
    copy_tree(STATIC_LICENSES, licenses)
    if NOTICES.is_file():
        shutil.copy2(NOTICES, APP_DIR / NOTICES.name)


def self_check() -> None:
    exe = APP_DIR / ("yt-paw.exe" if sys.platform == "win32" else "yt-paw")
    profile = BUILD / "self-check-profile"
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["YTPAW_PROFILE_DIR"] = str(profile)
    print(f"+ {exe.name} --self-check")
    result = subprocess.run([str(exe), "--self-check"], env=env, timeout=180, check=False)
    report = profile / "self-check.txt"
    if report.is_file():
        print(report.read_text(encoding="utf-8").rstrip())
    shutil.rmtree(profile, ignore_errors=True)
    if result.returncode != 0:
        raise SystemExit(f"self-check failed (exit {result.returncode})")


def write_checksum(archive: Path) -> None:
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum_text = digest.hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{checksum_text}  {archive.name}\n", encoding="utf-8")
    print(f"archive: {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    print(f"sha256:  {checksum_text}")


def make_zip(version: str) -> Path:
    archive = DIST / f"yt-paw-{version}-{platform_label()}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, Path("yt-paw") / path.relative_to(APP_DIR))
    write_checksum(archive)
    return archive


def make_tar_gz(version: str) -> Path:
    archive = DIST / f"yt-paw-{version}-{platform_label()}.tar.gz"
    archive.unlink(missing_ok=True)
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(APP_DIR, arcname="yt-paw")
    write_checksum(archive)
    return archive


def make_archive(version: str) -> Path:
    if sys.platform == "win32":
        return make_zip(version)
    return make_tar_gz(version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-sidecars", action="store_true", help="skip yt-dlp/ffmpeg/deno")
    parser.add_argument("--force-sidecars", action="store_true", help="ignore download cache")
    parser.add_argument("--skip-self-check", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--no-zip", action="store_true", help="skip the ZIP/tar.gz archive")
    parser.add_argument("--keep", action="store_true", help="reuse existing build/ and dist/")
    args = parser.parse_args()

    label = platform_label()
    if not args.no_sidecars and label not in SUPPORTED:
        raise SystemExit(
            f"no pinned sidecars for {label}; supported: {', '.join(SUPPORTED)} "
            "(use --no-sidecars for a layout-only build)"
        )
    if struct.calcsize("P") * 8 != 64:
        raise SystemExit("use 64-bit Python; the bundle targets x86_64")
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "PyInstaller is missing. Run: uv run --group build python packaging/build.py"
        )

    version = project_version()
    print(f"yt-paw {version} -> {label}")

    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(APP_DIR, ignore_errors=True)

    if not args.no_sidecars:
        command = [sys.executable, str(FETCH), "--platform", label]
        if args.force_sidecars:
            command.append("--force")
        run(command)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK),
            str(SPEC),
        ]
    )
    if not APP_DIR.is_dir():
        raise SystemExit(f"PyInstaller did not create {APP_DIR}")

    assemble(include_sidecars=not args.no_sidecars, platform_id=label)

    if not args.skip_verify:
        command = [sys.executable, str(VERIFY), "--root", str(APP_DIR)]
        if args.no_sidecars:
            command.append("--allow-missing-sidecars")
        run(command)
    if not args.no_sidecars and not args.skip_self_check:
        self_check()
    if not args.no_zip:
        make_archive(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
