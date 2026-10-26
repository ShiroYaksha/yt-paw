"""Sequential scheduler. One running job at a time.

Engine-agnostic: resolves the engine via the registry and asks it for args +
progress parsing. Children of a parent with a running child are skipped until
the sibling finishes (per-parent lock, arch §5.5 rule 3).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ytpaw.domain.engines_base import DownloadOptions
from ytpaw.engines import registry
from ytpaw.infra import timing
from ytpaw.infra.detail_store import detail_store_for
from ytpaw.infra.engine_settings_store import EngineSettingsStore
from ytpaw.infra.queue_store import QueueStore
from ytpaw.infra.runner import EngineRunner


class Scheduler(QObject):
    job_changed = Signal()  # UI refresh hint

    def __init__(
        self,
        db_path: Path,
        engine_binaries: dict[str, str],
        output_dir: str,
        output_template: str,
        logs_dir: Path,
        parent: QObject | None = None,
        env_paths: Sequence[Path] | None = None,
    ) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._binaries = engine_binaries
        self._output_dir = output_dir
        self._template = output_template
        self._logs_dir = logs_dir
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._runner = EngineRunner(self, env_paths=env_paths or ())
        self._runner.progress.connect(self._on_progress)
        self._runner.finished.connect(self._on_finished)
        self._runner.log_line.connect(self._on_log)
        self._current: str | None = None
        self._current_engine: str | None = None
        self._running_parents: set[str] = set()
        self._cancelling: set[str] = set()
        self._tail: dict[str, list[str]] = {}

    def cancel(self, job_id: str) -> None:
        if self._current == job_id:
            self._cancelling.add(job_id)
            self._runner.cancel()
            return
        conn = self._conn()
        try:
            QueueStore(conn).set_status(job_id, "cancelled")
        finally:
            conn.close()
        self.job_changed.emit()

    def set_output_dir(self, output_dir: str) -> None:
        self._output_dir = output_dir

    def set_binaries(self, binaries: dict[str, str]) -> None:
        self._binaries = binaries

    def engine_active(self, engine_id: str) -> bool:
        """True while a job using this engine is running (defer binary swaps)."""
        return self._current_engine == engine_id

    @property
    def busy(self) -> bool:
        return self._current is not None

    def cancel_playlist(self, parent_id: str) -> None:
        """Cascade-cancel: kill running child (if any), cancel queued children."""
        conn = self._conn()
        try:
            current = self._current
            parent_of_current = None
            if current:
                cur = conn.execute(
                    "SELECT parent_job_id FROM jobs WHERE id=?;", (current,)
                )
                r = cur.fetchone()
                parent_of_current = str(r["parent_job_id"]) if r and r["parent_job_id"] else None
            if current and parent_of_current == parent_id:
                self.cancel(current)
            conn.execute(
                "UPDATE jobs SET status='cancelled',"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE parent_job_id=? AND status='queued';",
                (parent_id,),
            )
            conn.commit()
            QueueStore(conn).refresh_parent_status(parent_id)
        finally:
            conn.close()
        self.job_changed.emit()

    def retry_failed_children(self, parent_id: str) -> None:
        conn = self._conn()
        try:
            QueueStore(conn).retry_failed_children(parent_id)
        finally:
            conn.close()
        self.job_changed.emit()
        self.pump()

    def retry(self, job_id: str) -> None:
        conn = self._conn()
        try:
            QueueStore(conn).retry(job_id)
        finally:
            conn.close()
        self.job_changed.emit()
        self.pump()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def pump(self) -> None:
        """Start next eligible job if idle. Sequential: single runner.
        Children of a parent with a running child are skipped (per-parent lock)."""
        if self._runner.running or self._current is not None:
            return
        t0 = timing.now()
        conn = self._conn()
        try:
            q = QueueStore(conn)
            row = q.next_queued(self._running_parents)
            if row is None:
                return
            job_id = str(row["id"])
            parent = row["parent_job_id"]
            if parent is not None:
                self._running_parents.add(str(parent))
            engine_id = str(row["engine_id"])
            binary = self._binaries.get(engine_id)
            if binary is None:
                q.set_status(job_id, "failed", f"engine '{engine_id}' is not available")
                self.job_changed.emit()
                return
            engine = registry.get_engine(engine_id)
            q.set_status(job_id, "running")
            self._tail[job_id] = []
            detail = detail_store_for(conn, engine_id)
            options = detail.job_options(job_id) if detail else DownloadOptions()
            cookies = (
                str(
                    EngineSettingsStore(conn).get(engine_id, "cookies_file", "") or ""
                ).strip()
                or None
            )
            outtmpl = str(Path(self._output_dir) / self._template)
            args = engine.build_download_args(
                binary, str(row["url"]), outtmpl, options, cookies
            )
            try:
                (self._logs_dir / f"{job_id}.log").write_text(
                    "CMD: " + " ".join(args) + "\n", encoding="utf-8"
                )
            except OSError:
                pass
            self._current = job_id
            self._current_engine = engine_id
            timing.log(f"scheduler pump before QProcess.start [{engine_id}]", t0)
            self._runner.start(job_id, args, engine.parse_progress_line)
            self.job_changed.emit()
        finally:
            conn.close()

    def _on_progress(self, job_id: str, percent: float) -> None:
        conn = self._conn()
        try:
            QueueStore(conn).set_progress(job_id, percent)
        finally:
            conn.close()
        self.job_changed.emit()

    def _on_log(self, job_id: str, text: str) -> None:
        buf = self._tail.setdefault(job_id, [])
        buf.append(text)
        del buf[:-200]
        try:
            with open(self._logs_dir / f"{job_id}.log", "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass

    def _on_finished(self, job_id: str, exit_code: int) -> None:
        cancelled = job_id in self._cancelling
        self._cancelling.discard(job_id)
        tail = self._tail.pop(job_id, [])
        conn = self._conn()
        try:
            q = QueueStore(conn)
            if cancelled:
                q.set_status(job_id, "cancelled")
            elif exit_code == 0:
                q.finish(job_id, self._output_dir)
            else:
                q.set_status(job_id, "failed", friendly_error(exit_code, tail))
            # Release per-parent lock + refresh derived parent status.
            cur = conn.execute("SELECT parent_job_id FROM jobs WHERE id=?;", (job_id,))
            r = cur.fetchone()
            if r and r["parent_job_id"]:
                parent_id = str(r["parent_job_id"])
                self._running_parents.discard(parent_id)
                q.refresh_parent_status(parent_id)
        finally:
            conn.close()
        self._current = None
        self._current_engine = None
        self.job_changed.emit()
        self.pump()


def friendly_error(exit_code: int, tail: list[str]) -> str:
    """Map engine tail to friend-proof copy. Last ERROR line wins."""
    err = ""
    for line in reversed(tail):
        if "ERROR:" in line:
            err = line.strip()
            break
    if not err:
        err = f"engine exit {exit_code}"
    hint = ""
    low = err.lower()
    if "requested format is not available" in low:
        hint = " — try Best again or another format (Phase 2 picker)."
    elif "ffmpeg" in low:
        hint = " — install ffmpeg (winget install Gyan.FFmpeg) and retry."
    elif "private" in low or "sign in" in low or "login" in low:
        hint = " — video needs login; cookies support lands in Phase 4."
    elif "js runtime" in low or "deno" in low:
        hint = " — optional: install deno for full format support."
    last = " | ".join(tail[-3:]) if tail else ""
    full = err + hint
    if last and last not in full:
        full += f" | log: {last}"
    return full[:1000]
