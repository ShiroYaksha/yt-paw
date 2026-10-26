"""MainWindow — stacked pages (Home / Download / Jobs / Settings) per wireframe."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ytpaw import __version__
from ytpaw.app.viewmodels.download_vm import enqueue_job
from ytpaw.app.viewmodels.playlist_vm import enqueue_playlist
from ytpaw.domain.dependencies import DependencySpec
from ytpaw.domain.engines_base import MediaInfo
from ytpaw.engines import registry
from ytpaw.infra import timing
from ytpaw.infra.dep_updater import DependencyUpdater
from ytpaw.infra.dependencies import ALL_DEPENDENCIES
from ytpaw.infra.engine_settings_store import EngineSettingsStore
from ytpaw.infra.info_fetcher import InfoFetcher
from ytpaw.infra.queue_store import QueueStore
from ytpaw.infra.runtime_paths import engine_env_dirs
from ytpaw.infra.scheduler import Scheduler
from ytpaw.infra.settings_store import SettingsStore
from ytpaw.infra.setup_service import (
    DependencyStatus,
    EngineStatus,
    SetupResult,
    open_db,
    resolve_dependency,
    resolve_engine_status,
)
from ytpaw.infra.thumbnail import ThumbnailLoader
from ytpaw.infra.updater import EngineUpdater
from ytpaw.ui.assets import icon
from ytpaw.ui.theme import apply_theme
from ytpaw.ui.views.download_view import DownloadView
from ytpaw.ui.views.home_view import HomeView
from ytpaw.ui.views.jobs_view import JobsView
from ytpaw.ui.views.playlist_view import PlaylistView
from ytpaw.ui.views.settings_view import SettingsView
from ytpaw.ui.views.setup_wizard import SetupWizardDialog
from ytpaw.ui.widgets.update_banner import UpdateBanner

if TYPE_CHECKING:
    from ytpaw.domain.engines_base import Engine


class MainWindow(QMainWindow):
    def __init__(self, setup: SetupResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup = setup
        self.setWindowTitle(f"yt-paw {__version__}")
        self.resize(880, 560)

        self._db_path = Path(str(setup.db_path))
        self._logs_dir = Path(str(setup.app_data)) / "logs" / "jobs"
        self._engine_status: dict[str, EngineStatus] = {
            s.engine_id: s for s in setup.engines
        }
        self._deps_status: dict[str, DependencyStatus] = {
            d.dep_id: d for d in setup.dependencies
        }
        self._last_engine_id: str | None = None

        self._output_dir, self._theme_name, self._concurrent = self._load_settings()

        self._pages = QStackedWidget(self)
        self._pages.setObjectName("centralWidget")
        self.setCentralWidget(self._pages)
        self._banner = UpdateBanner(self)

        self.home = HomeView(self)
        self.download = DownloadView(self)
        self.playlist = PlaylistView(self)
        self.jobs = JobsView(self)
        self.settings = SettingsView(self)
        for page in (self.home, self.download, self.playlist, self.jobs, self.settings):
            self._pages.addWidget(page)

        self._wire()
        self._build_footer()

        env_paths = engine_env_dirs(Path(str(setup.app_data)))
        self._sched = Scheduler(
            self._db_path,
            {s.engine_id: s.path for s in setup.engines if s.path and s.enabled},
            self._output_dir,
            "%(uploader)s/%(title)s [%(id)s].%(ext)s",
            self._logs_dir,
            self,
            env_paths=env_paths,
        )
        self._sched.job_changed.connect(self._refresh_jobs)
        self._sched.job_changed.connect(self._pump_engine_updates)
        self._sched.job_changed.connect(self._pump_dep_updates)
        self._fetcher = InfoFetcher(self, env_paths=env_paths)
        self._fetcher.info_ready.connect(self._on_info_ready)
        self._fetcher.info_failed.connect(self._on_info_failed)
        self._thumbs = ThumbnailLoader(self)
        self._thumbs.loaded.connect(lambda _url, data: self.download.set_thumbnail(data))
        self._updater = EngineUpdater(Path(str(setup.app_data)) / "bin", self)
        self._updater.updated.connect(self._on_engine_updated)
        self._updater.failed.connect(self._on_engine_update_failed)
        self._updater.finished.connect(self._on_engine_update_finished)
        self._updater.progress.connect(self._on_engine_update_progress)
        self._updater.waiting.connect(self._on_engine_update_waiting)
        self._updater.set_busy_checker(self._sched.engine_active)
        self._update_queue: list[tuple[Engine, str | None]] = []

        self._dep_updater = DependencyUpdater(Path(str(setup.app_data)) / "bin", self)
        self._dep_updater.progress.connect(self._on_dep_progress)
        self._dep_updater.installed.connect(self._on_dep_installed)
        self._dep_updater.failed.connect(self._on_dep_failed)
        self._dep_updater.finished.connect(self._on_dep_finished)
        self._dep_queue: list[DependencySpec] = []
        self._deps_started = False
        self._checking_engine: str | None = None
        self.settings.load(
            self._output_dir,
            self._theme_name,
            self._concurrent,
            self._engine_info_text(),
        )
        self._refresh_jobs()
        self.navigate("home")
        self._apply_icon_theme()
        QTimer.singleShot(600, self._startup_flow)

    def _startup_flow(self) -> None:
        self._maybe_first_run()
        self._startup_engine_update()

    def _maybe_first_run(self) -> None:
        """Fresh profile (no DB) → first-run wizard."""
        if not self._setup.first_run:
            return
        engines = registry.all_engines()
        if not engines:
            return
        engine = engines[0]
        status = self._engine_status.get(engine.engine_id)
        if status is not None and status.path and status.origin == "bundled":
            return  # standalone build already ships an engine; nothing to ask
        system_path = (
            status.path
            if status is not None and status.origin == "system"
            else None
        )
        dialog = SetupWizardDialog(engine.display_name, system_path, self)
        dialog.exec()
        conn = self._db()
        try:
            store = EngineSettingsStore(conn)
            if dialog.choice == SetupWizardDialog.CHOICE_SYSTEM:
                store.set(engine.engine_id, "source", "system")
            elif dialog.choice == SetupWizardDialog.CHOICE_AUTO:
                store.set(engine.engine_id, "source", "auto")
            elif (
                dialog.choice == SetupWizardDialog.CHOICE_PATH
                and dialog.selected_path
            ):
                store.set(engine.engine_id, "source", "path")
                store.set(engine.engine_id, "binary_path", dialog.selected_path)
            # CHOICE_LATER keeps the seeded default (auto).
        finally:
            conn.close()
        self._refresh_engine_state()

    def _apply_icon_theme(self) -> None:
        for view in (self.home, self.download, self.playlist, self.jobs, self.settings):
            view.apply_icons()

    # ---------- setup helpers ----------

    def _load_settings(self) -> tuple[str, str, int]:
        conn = self._db()
        try:
            store = SettingsStore(conn)
            output_dir = str(store.get("app.output_dir", "~/Downloads/yt-paw")).replace(
                "~", str(Path.home())
            )
            theme = str(store.get("app.theme", "auto"))
            raw_concurrent = store.get("app.concurrent", 2)
            concurrent = raw_concurrent if isinstance(raw_concurrent, int) else 2
        finally:
            conn.close()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return output_dir, theme, concurrent

    def _wire(self) -> None:
        self.home.analyze.connect(self._on_analyze)
        self.home.open_jobs.connect(lambda: self.navigate("jobs"))
        self.home.open_settings.connect(lambda: self.navigate("settings"))

        self.download.download.connect(self._on_download)
        self.download.open_jobs.connect(lambda: self.navigate("jobs"))
        self.download.open_settings.connect(lambda: self.navigate("settings"))

        self.playlist.download.connect(self._on_playlist_download)
        self.playlist.open_jobs.connect(lambda: self.navigate("jobs"))
        self.playlist.open_settings.connect(lambda: self.navigate("settings"))
        self.playlist.go_home.connect(lambda: self.navigate("home"))

        self.jobs.download_best.connect(lambda url: self._enqueue(url, "best", "Best"))
        self.jobs.open_settings.connect(lambda: self.navigate("settings"))
        self.jobs.go_home.connect(lambda: self.navigate("home"))
        self.jobs.table.customContextMenuRequested.connect(self._on_context_menu)
        self.jobs.table.doubleClicked.connect(self._on_double_clicked)

        self.settings.saved.connect(self._on_settings_saved)
        self.settings.engines_saved.connect(self._on_engines_saved)
        self.settings.theme_changed.connect(self._on_theme_changed)
        self.settings.engine_update_requested.connect(self._on_engine_update_requested)
        self.settings.open_jobs.connect(lambda: self.navigate("jobs"))
        self.settings.go_home.connect(lambda: self.navigate("home"))

    def _build_footer(self) -> None:
        self.statusBar().showMessage(self._engine_info_text())

    def _engine_specs(self) -> list[dict[str, object]]:
        conn = self._db()
        try:
            store = EngineSettingsStore(conn)
            specs: list[dict[str, object]] = []
            for engine in registry.all_engines():
                status = self._engine_status.get(engine.engine_id)
                binary_path = str(store.get(engine.engine_id, "binary_path", "") or "")
                source = str(store.get(engine.engine_id, "source", "") or "")
                if not source:
                    source = "path" if binary_path.strip() else "system"
                specs.append(
                    {
                        "engine_id": engine.engine_id,
                        "display_name": engine.display_name,
                        "origin": status.origin if status else "missing",
                        "version": status.version if status else None,
                        "active_path": status.path if status else None,
                        "enabled": bool(store.get(engine.engine_id, "enabled", True)),
                        "source": source,
                        "binary_path": binary_path,
                        "cookies_file": str(
                            store.get(engine.engine_id, "cookies_file", "") or ""
                        ),
                    }
                )
            return specs
        finally:
            conn.close()

    def _engine_info_text(self) -> str:
        bits = []
        for status in self._engine_status.values():
            if status.path:
                version = f" {status.version}" if status.version else ""
                bits.append(f"{status.display_name}{version} [{status.origin}]")
            else:
                bits.append(f"{status.display_name} not found")
        for dep in self._deps_status.values():
            if dep.path:
                version = f" {dep.version}" if dep.version else ""
                bits.append(f"{dep.display_name}{version} [{dep.origin}]")
            elif dep.dep_id == "deno":
                bits.append("Deno missing — YouTube extraction may be slow")
            else:
                bits.append(f"{dep.display_name} missing")
        return "  ·  ".join(bits)

    # ---------- navigation ----------

    def navigate(self, name: str) -> None:
        widget = {
            "home": self.home,
            "download": self.download,
            "playlist": self.playlist,
            "jobs": self.jobs,
            "settings": self.settings,
        }.get(name, self.home)
        if widget is self.jobs:
            self._refresh_jobs()
        if widget is self.settings:
            self.settings.load(
                self._output_dir, self._theme_name, self._concurrent, self._engine_info_text()
            )
            self.settings.load_engines(self._engine_specs())
        self._pages.setCurrentWidget(widget)

    def set_update(self, text: str, error: bool = False) -> None:
        """App-global notification overlay (Phase 6 update pings reuse this)."""
        self._banner.show_message(text, error)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._banner.reposition()

    # ---------- data ----------

    def _db(self) -> sqlite3.Connection:
        conn = open_db(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _refresh_jobs(self) -> None:
        conn = self._db()
        try:
            rows = QueueStore(conn).list_recent(200)
        finally:
            conn.close()
        self.jobs.model.set_rows(rows)

    # ---------- actions ----------

    def _on_analyze(self, url: str) -> None:
        try:
            engine_id = registry.pick_engine(url)
        except ValueError as exc:
            QMessageBox.warning(self, "yt-paw", str(exc))
            return
        status = self._engine_status.get(engine_id)
        if status is None or not status.path:
            name = status.display_name if status else engine_id
            QMessageBox.warning(
                self, "yt-paw", f"Engine '{name}' is not available. Install it first."
            )
            return
        if not status.enabled:
            QMessageBox.warning(
                self, "yt-paw", f"Engine '{status.display_name}' is disabled in Settings."
            )
            return
        engine = registry.get_engine(engine_id)
        self._last_engine_id = engine_id
        self.download.set_fallback_formats(engine.default_formats)
        self.playlist.set_fallback_formats(engine.default_formats)
        self.download.begin_loading(url)
        self.navigate("download")
        self._fetcher.fetch(
            engine,
            status.path,
            url,
            playlist=engine.is_playlist_url(url),
            cookies_file=self._engine_cookies(engine_id),
        )

    def _engine_cookies(self, engine_id: str) -> str | None:
        conn = self._db()
        try:
            value = str(
                EngineSettingsStore(conn).get(engine_id, "cookies_file", "") or ""
            ).strip()
        finally:
            conn.close()
        return value or None

    def _on_info_ready(self, info: object) -> None:
        if not isinstance(info, MediaInfo):
            return
        if info.is_playlist:
            self.playlist.apply_info(info)
            self.navigate("playlist")
            return
        self.download.apply_info(info)
        if info.thumbnail_url:
            self._thumbs.fetch(info.thumbnail_url)

    def _on_info_failed(self, message: str) -> None:
        self.download.apply_failure(message)

    def _on_playlist_download(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        conn = self._db()
        try:
            parent_id = enqueue_playlist(
                conn,
                str(payload["url"]),
                str(payload["title"]),
                payload["entries"],
                self._last_engine_id or "ytdlp",
                str(payload.get("selector") or "best"),
                str(payload.get("label") or "Best"),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "yt-paw", str(exc))
            return
        finally:
            conn.close()
        _ = parent_id
        self._refresh_jobs()
        self.navigate("jobs")
        self._sched.pump()

    def _on_download(self, url: str, fmt: str, label: str, langs: object, action: str) -> None:
        langs_list = [str(x) for x in langs] if isinstance(langs, list) else []
        self._enqueue(url, fmt, label, langs_list, action)
        self.navigate("jobs")

    def _enqueue(
        self,
        url: str,
        fmt: str,
        label: str,
        langs: list[str] | None = None,
        action: str = "none",
    ) -> None:
        t0 = timing.now()
        conn = self._db()
        try:
            enqueue_job(conn, url, fmt, label, langs, action)
        except ValueError as exc:
            QMessageBox.warning(self, "yt-paw", str(exc))
            return
        finally:
            conn.close()
        self._refresh_jobs()
        self._sched.pump()
        timing.log("enqueue → pump (UI thread)", t0)

    def _on_settings_saved(self, output_dir: str, concurrent: int) -> None:
        output_dir = output_dir.replace("~", str(Path.home())) or self._output_dir
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._output_dir = output_dir
        self._concurrent = concurrent
        self._sched.set_output_dir(output_dir)
        conn = self._db()
        try:
            store = SettingsStore(conn)
            store.set("app.output_dir", output_dir)
            store.set("app.concurrent", concurrent)
        finally:
            conn.close()
        self.statusBar().showMessage(f"saved · {output_dir}")

    def _refresh_engine_state(self) -> None:
        """Re-resolve all engine statuses after any change, refresh UI."""
        conn = self._db()
        try:
            for engine in registry.all_engines():
                self._engine_status[engine.engine_id] = resolve_engine_status(
                    engine, conn, Path(str(self._setup.app_data))
                )
        finally:
            conn.close()
        self._apply_engine_binaries()
        self.settings.load(
            self._output_dir, self._theme_name, self._concurrent, self._engine_info_text()
        )
        self.settings.load_engines(self._engine_specs())
        self.statusBar().showMessage(self._engine_info_text())

    def _on_engines_saved(self, payload: object) -> None:
        if not isinstance(payload, list):
            return
        conn = self._db()
        try:
            store = EngineSettingsStore(conn)
            for spec in payload:
                if not isinstance(spec, dict):
                    continue
                engine_id = str(spec.get("engine_id") or "")
                if not engine_id:
                    continue
                source = str(spec.get("source") or "system")
                if source not in ("system", "path", "auto"):
                    source = "system"
                store.set(engine_id, "enabled", bool(spec.get("enabled", True)))
                store.set(engine_id, "source", source)
                store.set(engine_id, "binary_path", str(spec.get("binary_path") or ""))
                store.set(engine_id, "cookies_file", str(spec.get("cookies_file") or ""))
        finally:
            conn.close()
        self._refresh_engine_state()
        # Switching to AUTO with no managed binary triggers an immediate check
        # (which downloads when missing) — banner shows progress.
        self._startup_engine_update()

    # ---------- engine updates (4b) ----------

    def _startup_engine_update(self) -> None:
        """Queue every Auto engine for a stable-channel check.

        Managed binary missing (origin != bundled) ⇒ current=None, which forces
        a download instead of skipping on a version tie with a system binary.
        """
        conn = self._db()
        try:
            store = EngineSettingsStore(conn)
            queue: list[tuple[Engine, str | None]] = []
            for engine in registry.all_engines():
                status = self._engine_status.get(engine.engine_id)
                if status is None or not status.enabled:
                    continue
                if not getattr(engine, "update_repo", ""):
                    continue  # cannot self-update/download
                if str(store.get(engine.engine_id, "source", "")) != "auto":
                    continue
                if not bool(store.get(engine.engine_id, "auto_update", True)):
                    continue
                if str(store.get(engine.engine_id, "update_channel", "stable")) != "stable":
                    continue
                current = status.version if status.origin == "bundled" else None
                queue.append((engine, current))
            self._update_queue = queue
        finally:
            conn.close()
        self._pump_engine_updates()
        if not self._updater.busy and not self._update_queue:
            self._queue_missing_deps()  # nothing to update → deps can start now

    def _pump_engine_updates(self) -> None:
        if self._updater.busy or not self._update_queue:
            return
        if self._sched.busy:
            # Don't compete with an active media download for bandwidth;
            # job_changed (emitted when the job ends) re-invokes this.
            return
        engine, version = self._update_queue.pop(0)
        status = self._engine_status.get(engine.engine_id)
        name = status.display_name if status else engine.engine_id
        self._checking_engine = engine.engine_id
        self._banner.show_message(f"Checking {name} for updates…")
        self._updater.update(engine, version)

    def _on_engine_update_finished(self, engine_id: str) -> None:
        if self._checking_engine == engine_id:
            # Up-to-date (no progress/result banners fired): clear the
            # transient “Checking…” message.
            self._checking_engine = None
            self._banner.clear_message()
        self._pump_engine_updates()
        if not self._updater.busy and not self._update_queue:
            self._queue_missing_deps()

    # ---------- dependency downloads (managed deno/ffmpeg) ----------

    def _queue_missing_deps(self) -> None:
        if self._deps_started:
            return
        self._deps_started = True
        conn = self._db()
        try:
            auto = bool(SettingsStore(conn).get("app.auto_download_deps", True))
        finally:
            conn.close()
        if not auto:
            return
        self._dep_queue = [
            dep
            for dep in ALL_DEPENDENCIES
            if self._deps_status.get(dep.dep_id) is not None
            and self._deps_status[dep.dep_id].origin == "missing"
            and dep.sources_for()
        ]
        self._pump_dep_updates()

    def _pump_dep_updates(self) -> None:
        if self._dep_updater.busy or not self._dep_queue:
            return
        if self._sched.busy:  # don't compete with an active media download
            return
        self._dep_updater.install(self._dep_queue.pop(0))

    def _on_dep_finished(self, _dep_id: str) -> None:
        self._pump_dep_updates()

    def _refresh_dep_statuses(self) -> None:
        app_data = Path(str(self._setup.app_data))
        for dep in ALL_DEPENDENCIES:
            self._deps_status[dep.dep_id] = resolve_dependency(dep, app_data)
        self.settings.load(
            self._output_dir, self._theme_name, self._concurrent, self._engine_info_text()
        )
        self.statusBar().showMessage(self._engine_info_text())

    def _dep_name(self, dep_id: str) -> str:
        status = self._deps_status.get(dep_id)
        return status.display_name if status else dep_id

    def _on_dep_progress(self, dep_id: str, received: int, total: int) -> None:
        name = self._dep_name(dep_id)
        if total > 0:
            pct = int(received * 100 / total)
            self._banner.show_message(f"Downloading {name}… {pct}%")
        else:
            self._banner.show_message(f"Downloading {name}…")

    def _on_dep_installed(self, dep_id: str, version: str) -> None:
        self._refresh_dep_statuses()
        self._banner.show_message(f"Installed {self._dep_name(dep_id)} {version} (managed)")

    def _on_dep_failed(self, dep_id: str, message: str) -> None:
        name = self._dep_name(dep_id)
        self._banner.show_message(f"{name} download failed: {message}", error=True)
        self.statusBar().showMessage(f"{dep_id}: {message}")

    def _engine_name(self, engine_id: str) -> str:
        status = self._engine_status.get(engine_id)
        return status.display_name if status else engine_id

    def _on_engine_update_progress(self, engine_id: str, received: int, total: int) -> None:
        name = self._engine_name(engine_id)
        if total > 0:
            pct = int(received * 100 / total)
            self._banner.show_message(f"Downloading {name} update… {pct}%")
        else:
            self._banner.show_message(f"Downloading {name} update…")

    def _on_engine_update_waiting(self, engine_id: str) -> None:
        name = self._engine_name(engine_id)
        self._banner.show_message(
            f"{name} update ready — will install when the current download finishes…"
        )

    def _on_engine_update_requested(self, engine_id: str) -> None:
        status = self._engine_status.get(engine_id)
        engine = None
        for candidate in registry.all_engines():
            if candidate.engine_id == engine_id:
                engine = candidate
                break
        if engine is None or status is None:
            return
        if not status.enabled:
            QMessageBox.warning(self, "yt-paw", "Engine is disabled.")
            return
        self._updater.update(engine, status.version)
        self.statusBar().showMessage(f"checking updates for {status.display_name}…")

    def _on_engine_updated(self, engine_id: str, old: str, new: str) -> None:
        self._checking_engine = None
        self._refresh_engine_state()
        name = self._engine_name(engine_id)
        self._banner.show_message(f"Updated {name} to {new} — latest stable", error=False)

    def _on_engine_update_failed(self, engine_id: str, message: str) -> None:
        self._checking_engine = None
        name = self._engine_name(engine_id)
        self._banner.show_message(f"{name} update failed: {message}", error=True)
        self.statusBar().showMessage(f"{engine_id}: {message}")

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._update_queue.clear()
        self._dep_queue.clear()
        self._updater.abort()
        self._dep_updater.abort()
        super().closeEvent(event)

    def _apply_engine_binaries(self) -> None:
        self._sched.set_binaries(
            {
                s.engine_id: s.path
                for s in self._engine_status.values()
                if s.path and s.enabled
            }
        )

    def _on_theme_changed(self, name: str) -> None:
        self._theme_name = name
        conn = self._db()
        try:
            SettingsStore(conn).set("app.theme", name)
        finally:
            conn.close()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, name)
            self._apply_icon_theme()

    # ---------- job menu/log ----------

    def _on_double_clicked(self, index) -> None:  # type: ignore[no-untyped-def]
        row = self.jobs.model.row_at(index.row())
        if row is not None and row["error"]:
            self._show_log_dialog(str(row["id"]), str(row["status"]), row["error"])

    def _on_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        table = self.jobs.table
        index = table.indexAt(pos)
        if not index.isValid():
            return
        row = self.jobs.model.row_at(index.row())
        if row is None:
            return
        job_id = str(row["id"])
        status = str(row["status"])
        job_type = str(row["job_type"])
        menu = QMenu(self)
        if job_type == "playlist":
            if status in ("queued", "running"):
                act = QAction(icon("octagon-minus"), "Cancel all", self)
                act.triggered.connect(lambda: self._sched.cancel_playlist(job_id))
                menu.addAction(act)
            if status in ("failed", "cancelled", "done"):
                act = QAction(icon("broom-sparkles"), "Retry failed videos", self)
                act.triggered.connect(lambda: self._sched.retry_failed_children(job_id))
                menu.addAction(act)
            act = QAction(icon("square-arrow-down"), "Open download folder", self)
            act.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(self._output_dir))
                )
            )
            menu.addAction(act)
            menu.exec(table.viewport().mapToGlobal(pos))
            return
        if status in ("queued", "running"):
            act = QAction(icon("octagon-minus"), "Cancel", self)
            act.triggered.connect(lambda: self._sched.cancel(job_id))
            menu.addAction(act)
        if status in ("failed", "cancelled"):
            act = QAction(icon("broom-sparkles"), "Retry", self)
            act.triggered.connect(lambda: self._sched.retry(job_id))
            menu.addAction(act)
        act = QAction(icon("square-text"), "Show log", self)
        act.triggered.connect(lambda: self._show_log_dialog(job_id, status, row["error"]))
        menu.addAction(act)
        if row["output_path"]:
            act = QAction(icon("square-arrow-down"), "Open download folder", self)
            act.triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(row["output_path"])))
            )
            menu.addAction(act)
        menu.exec(table.viewport().mapToGlobal(pos))

    def _show_log_dialog(self, job_id: str, status: str, error: object) -> None:
        log_path = self._logs_dir / f"{job_id}.log"
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            tail = "\n".join(lines[-80:])
        except OSError:
            tail = "(no log file yet)"
        body = f"Job: {job_id}\nStatus: {status}\nError: {error or '-'}\nLog: {log_path}\n\n{tail}"

        dlg = QDialog(self)
        dlg.setWindowTitle("yt-paw — job log (Ctrl+C to copy)")
        dlg.resize(760, 480)
        layout = QVBoxLayout(dlg)
        text = QTextEdit(dlg)
        text.setReadOnly(True)
        text.setPlainText(body)
        text.selectAll()
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        open_btn = buttons.addButton("Open log file", QDialogButtonBox.ButtonRole.ActionRole)
        open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))
        )
        layout.addWidget(buttons)
        dlg.exec()
