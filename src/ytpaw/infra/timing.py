"""Opt-in timing logs for diagnosing slow download starts.

Enable with env var YTPAW_DEBUG_TIMING=1 (launch from a terminal to see
console output). Pure observation — no behavior change.
"""

from __future__ import annotations

import os
import sys
import time

ENABLED = os.environ.get("YTPAW_DEBUG_TIMING") == "1"


def now() -> float:
    return time.perf_counter()


def _write(text: str) -> None:
    # Windowed PyInstaller builds set sys.stdout to None; skip quietly.
    if sys.stdout is not None:
        print(text, flush=True)


def log(label: str, started: float) -> None:
    if ENABLED:
        _write(f"[yt-paw timing] {label}: {time.perf_counter() - started:.2f}s")


def note(text: str) -> None:
    if ENABLED:
        _write(f"[yt-paw timing] {text}")
