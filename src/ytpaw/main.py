"""Entry point: setup → theme → Widgets window. No daemon, no server."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtNetwork import QNetworkProxyFactory
from PySide6.QtWidgets import QApplication

from ytpaw.app.main_window import MainWindow
from ytpaw.infra.settings_store import SettingsStore
from ytpaw.infra.setup_service import ensure_ready, open_db
from ytpaw.ui.assets import app_icon
from ytpaw.ui.theme import apply_theme


def main() -> int:
    profile = os.environ.get("YTPAW_PROFILE_DIR", "").strip()
    if "--self-check" in sys.argv[1:]:
        # Release builds run this before zipping; it needs no display.
        from ytpaw.infra.self_check import run as run_self_check

        if not profile:
            return 2
        return run_self_check(Path(profile).expanduser())
    setup = ensure_ready(Path(profile).expanduser() if profile else None)
    app = QApplication(sys.argv)
    app.setApplicationName("yt-paw")
    app.setWindowIcon(app_icon())
    # Skip system/WPAD proxy discovery: on Windows it can stall the first
    # QNetworkAccessManager request (GitHub update check) by 15-20s.
    QNetworkProxyFactory.setUseSystemConfiguration(False)
    app.setStyle("Fusion")

    conn = open_db(Path(str(setup.db_path)))
    try:
        theme = str(SettingsStore(conn).get("app.theme", "auto"))
    finally:
        conn.close()
    apply_theme(app, theme)

    win = MainWindow(setup)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
