# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder spec for yt-paw (Windows/Linux x86_64 releases).

Build with `python packaging/build.py`; PyInstaller does not cross-compile, so
the artifact matches the host. The one-folder layout keeps `bin/`
(yt-dlp/ffmpeg/deno) replaceable at runtime and avoids one-file extraction/AV
pitfalls.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
ICON = SRC / "ytpaw" / "ui" / "resources" / "icons" / "app.ico"
VERSION_FILE = ROOT / "build" / "version_info.txt"


def project_version() -> str:
    text = (SRC / "ytpaw" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if match is None:
        raise SystemExit("cannot find __version__ in src/ytpaw/__init__.py")
    return match.group(1)


VERSION = project_version()


def windows_version_file() -> str | None:
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    parts = [int(part) for part in VERSION.split(".")]
    while len(parts) < 4:
        parts.append(0)
    info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=tuple(parts[:4]),
            prodvers=tuple(parts[:4]),
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "yt-paw"),
                            StringStruct("FileDescription", "yt-paw"),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", "yt-paw"),
                            StringStruct("LegalCopyright", "MIT License"),
                            StringStruct("OriginalFilename", "yt-paw.exe"),
                            StringStruct("ProductName", "yt-paw"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # PyInstaller 6.22.3's repr() emits ``versioninfo.VSVersionInfo``, which its
    # own text loader cannot eval; __str__ is the documented version-file form.
    VERSION_FILE.write_text(str(info), encoding="utf-8")
    return str(VERSION_FILE)


# Qt modules the app never imports. Excluding them keeps the folder small;
# PyInstaller still pulls shared-library dependencies required by QtWidgets.
EXCLUDES = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "tkinter",
    "_tkinter",
    "pytest",
    "mypy",
    "ruff",
    "setuptools",
    "pkg_resources",
    "pip",
]

a = Analysis(
    [str(SRC / "ytpaw" / "main.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(SRC / "ytpaw" / "ui" / "resources"), "ytpaw/ui/resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yt-paw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=windows_version_file(),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="yt-paw",
    contents_directory="_internal",
)
