"""Sanity-check a built one-folder distribution before archiving.

Verifies the executable, packaged resources, Qt platform/TLS/image plugins, and
(unless explicitly allowed) the bundled yt-dlp/ffmpeg/deno sidecars for the host
platform.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_RESOURCES = (
    "themes/dark.qss",
    "themes/light.qss",
    "icons/app.ico",
    "icons/app.png",
    "icons/logo.png",
)

WINDOWS_SIDECARS = (
    "bin/yt-dlp.exe",
    "bin/deno.exe",
    "bin/ffmpeg.exe",
    "bin/ffprobe.exe",
)
LINUX_SIDECARS = (
    "bin/yt-dlp",
    "bin/deno",
    "bin/ffmpeg",
    "bin/ffprobe",
)


def _size(path: Path) -> int:
    if path.is_file():
        return path.lstat().st_size
    return sum(child.lstat().st_size for child in path.rglob("*") if child.is_file() or child.is_symlink())


def _find_any(root: Path, *patterns: str) -> bool:
    return any(next(root.rglob(pattern), None) is not None for pattern in patterns)


def _sidecar_errors(root: Path) -> list[str]:
    errors: list[str] = []
    if sys.platform == "win32":
        for name in WINDOWS_SIDECARS:
            if not (root / name).is_file():
                errors.append(f"missing sidecar {name}")
        if not any((root / "bin").glob("avcodec-*.dll")):
            errors.append("missing FFmpeg shared libraries in bin/")
    else:
        for name in LINUX_SIDECARS:
            if not (root / name).is_file():
                errors.append(f"missing sidecar {name}")
        for pattern in ("libavcodec.so.*", "libavformat.so.*", "libavutil.so.*"):
            if not any((root / "lib").glob(pattern)):
                errors.append(f"missing FFmpeg shared library lib/{pattern}")
    return errors


def verify(root: Path, allow_missing_sidecars: bool) -> list[str]:
    errors: list[str] = []
    exe = root / ("yt-paw.exe" if sys.platform == "win32" else "yt-paw")
    if not exe.is_file():
        exe = next(iter(root.glob("yt-paw*")), exe)
    if not exe.is_file():
        errors.append("missing yt-paw executable")

    resources = root / "_internal" / "ytpaw" / "ui" / "resources"
    if not resources.is_dir():
        errors.append("missing _internal/ytpaw/ui/resources")
    else:
        for rel in REQUIRED_RESOURCES:
            if not (resources / rel).is_file():
                errors.append(f"missing resource {rel}")

    if not _find_any(root, "qwindows.dll", "libqxcb.so", "libqoffscreen.so"):
        errors.append("missing Qt platform plugin")
    if not _find_any(root, "qschannelbackend.dll", "libqopensslbackend.so"):
        errors.append("missing Qt TLS backend (engine/update HTTPS)")
    if not _find_any(root, "qjpeg.dll", "libqjpeg.so"):
        errors.append("missing Qt JPEG image plugin (thumbnails)")

    bin_dir = root / "bin"
    if allow_missing_sidecars:
        if bin_dir.is_dir():
            print(f"sidecars: {len(list(bin_dir.iterdir()))} files (optional)")
    else:
        errors.extend(_sidecar_errors(root))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="one-folder output")
    parser.add_argument(
        "--allow-missing-sidecars",
        action="store_true",
        help="skip yt-dlp/ffmpeg/deno checks (development builds)",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    errors = verify(root, args.allow_missing_sidecars)
    print(f"verified {root} ({_size(root) / 1e6:.1f} MB)")
    if errors:
        for error in errors:
            print(f"  FAIL {error}", file=sys.stderr)
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
