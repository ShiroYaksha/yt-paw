"""QProcess wrapper. One process per job. Emits normalized progress.

The engine supplies arg construction and the progress-line parser, so this
module stays engine-agnostic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ytpaw.infra import timing
from ytpaw.infra.runtime_paths import prepend_path

if TYPE_CHECKING:
    from ytpaw.domain.engines_base import ProgressUpdate

ParseProgress = Callable[[str, str], "ProgressUpdate | None"]


class EngineRunner(QObject):
    progress = Signal(str, float)  # job_id, percent
    finished = Signal(str, int)  # job_id, exit_code
    log_line = Signal(str, str)  # job_id, text

    def __init__(
        self, parent: QObject | None = None, env_paths: Sequence[Path] = ()
    ) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._job_id: str | None = None
        self._buf = ""
        self._parse: ParseProgress | None = None
        self._env_paths = list(env_paths)
        self._t0 = 0.0
        self._first_output = False
        self._first_progress = False

    @property
    def running(self) -> bool:
        return self._proc is not None

    def start(self, job_id: str, args: list[str], parse_progress: ParseProgress) -> None:
        if self._proc is not None:
            raise RuntimeError("Runner already busy")
        self._job_id = job_id
        self._buf = ""
        self._parse = parse_progress
        proc = QProcess(self)
        if self._env_paths:
            # Managed/bundled deps (ffmpeg, deno) live in bin/; make them
            # discoverable to engines without touching the user's PATH.
            env = QProcessEnvironment.systemEnvironment()
            env.insert("PATH", prepend_path(self._env_paths, env.value("PATH", "")))
            proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.started.connect(lambda: timing.log("QProcess reported started", self._t0))
        self._proc = proc
        self._t0 = timing.now()
        self._first_output = False
        self._first_progress = False
        timing.note(f"QProcess.start() called: {args[0]}")
        proc.start(args[0], args[1:])

    def cancel(self) -> None:
        if self._proc is not None:
            self._proc.kill()

    def _on_stdout(self) -> None:
        if self._proc is None or self._job_id is None or self._parse is None:
            return
        chunk = bytes(self._proc.readAllStandardOutput().data()).decode("utf-8", "replace")
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if not self._first_output and line.strip():
                self._first_output = True
                timing.log("first engine output line", self._t0)
            upd = self._parse(self._job_id, line)
            if upd is not None:
                if not self._first_progress:
                    self._first_progress = True
                    timing.log("first progress line (download started)", self._t0)
                self.progress.emit(self._job_id, upd.percent)
            elif line.strip():
                self.log_line.emit(self._job_id, line.strip())

    def _on_stderr(self) -> None:
        if self._proc is None or self._job_id is None:
            return
        chunk = bytes(self._proc.readAllStandardError().data()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            if line.strip():
                self.log_line.emit(self._job_id, line.strip())

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        job_id = self._job_id or ""
        self._proc = None
        self._job_id = None
        self.finished.emit(job_id, exit_code)
