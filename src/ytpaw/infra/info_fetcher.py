"""One-shot media-info fetch via QProcess, off the UI thread.

Engine-agnostic: the caller supplies an `Engine` from the registry; this module
only runs its info args and parses the output. Timeout 30s, cancel kills.
Fetches are identity-checked so a rapid re-fetch can't be hijacked by a stale
process finishing late.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from ytpaw.infra.runtime_paths import prepend_path
from ytpaw.infra.scheduler import friendly_error

if TYPE_CHECKING:
    from ytpaw.domain.engines_base import Engine


class InfoFetcher(QObject):
    info_ready = Signal(object)  # MediaInfo
    info_failed = Signal(str)  # friendly message

    def __init__(
        self, parent: QObject | None = None, env_paths: Sequence[Path] = ()
    ) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._url = ""
        self._engine: Engine | None = None
        self._env_paths = list(env_paths)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    @property
    def busy(self) -> bool:
        return self._proc is not None

    def cancel(self) -> None:
        proc = self._proc
        self._proc = None
        self._timer.stop()
        if proc is not None:
            proc.kill()

    def fetch(
        self,
        engine: Engine,
        binary: str,
        url: str,
        playlist: bool = False,
        cookies_file: str | None = None,
    ) -> None:
        self.cancel()
        self._url = url
        self._engine = engine
        args = engine.build_info_args(binary, url, playlist, cookies_file)
        proc = QProcess(self)
        if self._env_paths:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("PATH", prepend_path(self._env_paths, env.value("PATH", "")))
            proc.setProcessEnvironment(env)
        proc.finished.connect(
            lambda code, status, p=proc: self._on_finished(p, code, status)
        )
        self._proc = proc
        self._timer.start(30_000)
        proc.start(args[0], args[1:])

    def _on_timeout(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        proc.kill()
        self.info_failed.emit("Information request timed out after 30s.")

    def _on_finished(
        self, proc: QProcess, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        if proc is not self._proc:  # stale process from a cancelled fetch
            proc.deleteLater()
            return
        self._proc = None
        self._timer.stop()
        out = bytes(proc.readAllStandardOutput().data()).decode("utf-8", "replace")
        err = bytes(proc.readAllStandardError().data()).decode("utf-8", "replace")
        proc.deleteLater()
        if exit_code == 0 and out.strip() and self._engine is not None:
            try:
                info = self._engine.parse_info_json(out, self._url)
            except (ValueError, KeyError):
                self.info_failed.emit("Engine returned unreadable information.")
                return
            self.info_ready.emit(info)
            return
        tail = [line for line in err.splitlines() if line.strip()]
        self.info_failed.emit(friendly_error(exit_code, tail[-5:]))
