"""
core.progress
=============

Shared infrastructure so both the restore engine and (lightly) the backup
engine can report fine-grained, byte-accurate progress and respond
instantly to pause/resume/cancel -- without the GUI ever having to poll.

Design
------
:class:`OperationControl` is a small, thread-safe pause/cancel flag pair
that worker threads check between units of work (between chunks within a
file, and between files). Checking a ``threading.Event`` is essentially
free, so workers can check it very frequently without adding meaningful
overhead -- this is what makes "Cancel" and "Pause" feel instant even
while millions of small chunk-writes are in flight.

:class:`ProgressHooks` is a bag of optional callables the engine invokes
as things happen (file started, bytes written, file finished, error,
log line). These are plain Python callbacks, not Qt signals -- the GUI
layer (:mod:`gui.restore_worker`) wraps them in a QThread and re-emits
them as throttled Qt signals, so the engine itself has zero GUI
dependency and stays usable from the CLI too.

:class:`SpeedTracker` computes both a short-window "current speed" (so the
number visibly reacts to a slow file or a burst of small ones) and a
since-start "average speed", plus an ETA derived from the current speed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

# How often on_overall_progress is allowed to fire, in seconds. 1/15s ==
# ~15Hz, comfortably inside the requested 10-20Hz budget without adding
# per-chunk overhead from firing on every single 8KB chunk.
PROGRESS_THROTTLE_SECONDS = 1.0 / 15.0

# How wide a window (in seconds) "current speed" is averaged over. Short
# enough to react quickly to a slowdown, long enough not to be jittery.
SPEED_WINDOW_SECONDS = 3.0


class OperationCancelled(Exception):
    """Raised internally to unwind out of a partially-written file the
    moment a cancel is observed, so cleanup code (temp file removal) runs
    via normal exception handling rather than scattered manual checks."""


class OperationControl:
    """Thread-safe pause/cancel signaling for a single backup or restore
    run. One instance is shared by every worker thread processing that
    run's files.
    """

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()
        # Wake up anything blocked in wait_if_paused() so it can observe
        # the cancellation immediately instead of waiting out a pause.
        self._pause_event.clear()

    def request_pause(self) -> None:
        self._pause_event.set()

    def request_resume(self) -> None:
        self._pause_event.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def paused(self) -> bool:
        return self._pause_event.is_set()

    def check(self) -> None:
        """Raise :class:`OperationCancelled` if a cancel has been
        requested. Call this between fine-grained units of work (chunks,
        files)."""
        if self._cancel_event.is_set():
            raise OperationCancelled()

    def wait_if_paused(self, poll_interval: float = 0.1) -> None:
        """Block while paused, waking periodically to also check for
        cancellation so a cancel issued while paused still takes effect
        immediately rather than waiting for a resume first."""
        while self._pause_event.is_set():
            if self._cancel_event.is_set():
                raise OperationCancelled()
            time.sleep(poll_interval)
        self.check()


@dataclass
class ProgressHooks:
    """Optional callbacks the engine invokes as work happens. Every field
    defaults to None (no-op) so callers only wire up what they need --
    the CLI, for instance, only needs on_log and on_overall_progress.
    """

    on_file_start: Optional[Callable[[str, int], None]] = None
    """(logical_path, size_bytes)"""

    on_file_chunk_progress: Optional[Callable[[str, int, int], None]] = None
    """(logical_path, bytes_done_in_file, size_bytes) -- for the
    large-file sub-progress bar."""

    on_file_done: Optional[Callable[[str, str, str], None]] = None
    """(logical_path, status, message) where status is one of
    "ok"/"skipped"/"failed"/"cancelled"."""

    on_log: Optional[Callable[[str, str], None]] = None
    """(level, message) where level is "info"/"warn"/"error"."""

    on_overall_progress: Optional[Callable[["ProgressStats"], None]] = None
    """Fired at most ~15x/sec with a full stats snapshot."""

    def log(self, level: str, message: str) -> None:
        if self.on_log:
            self.on_log(level, message)


@dataclass
class ProgressStats:
    bytes_total: int
    bytes_done: int
    files_total: int
    files_done: int
    files_skipped: int
    files_failed: int
    elapsed_seconds: float
    current_speed_bps: float
    average_speed_bps: float
    eta_seconds: Optional[float]

    @property
    def percent(self) -> float:
        if self.bytes_total <= 0:
            return 100.0
        return min(100.0, 100.0 * self.bytes_done / self.bytes_total)


class SpeedTracker:
    """Tracks bytes-processed samples to derive a responsive "current
    speed" (short rolling window) and a stable "average speed" (since the
    operation started), without needing a background thread -- callers
    just call :meth:`record` as bytes complete and :meth:`stats` whenever
    they want a snapshot.
    """

    def __init__(self, bytes_total: int, files_total: int):
        self.bytes_total = bytes_total
        self.files_total = files_total
        self.bytes_done = 0
        self.files_done = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.started_at = time.monotonic()
        self._samples: deque[tuple[float, int]] = deque()  # (timestamp, cumulative_bytes)
        self._lock = threading.Lock()

    def record_bytes(self, num_bytes: int) -> None:
        with self._lock:
            self.bytes_done += num_bytes
            now = time.monotonic()
            self._samples.append((now, self.bytes_done))
            cutoff = now - SPEED_WINDOW_SECONDS
            while len(self._samples) > 1 and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def record_file_done(self, status: str) -> None:
        with self._lock:
            if status == "ok":
                self.files_done += 1
            elif status == "skipped":
                self.files_skipped += 1
            elif status == "failed":
                self.files_failed += 1
            # "cancelled" files count toward none of the above -- they
            # simply weren't processed this run and remain eligible for a
            # future resume.

    def stats(self) -> ProgressStats:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.started_at

            if len(self._samples) >= 2:
                t0, b0 = self._samples[0]
                t1, b1 = self._samples[-1]
                window = t1 - t0
                current_speed = (b1 - b0) / window if window > 0 else 0.0
            else:
                current_speed = 0.0

            average_speed = self.bytes_done / elapsed if elapsed > 0 else 0.0

            remaining_bytes = max(0, self.bytes_total - self.bytes_done)
            eta = None
            reference_speed = current_speed if current_speed > 0 else average_speed
            if reference_speed > 0:
                eta = remaining_bytes / reference_speed

            return ProgressStats(
                bytes_total=self.bytes_total,
                bytes_done=self.bytes_done,
                files_total=self.files_total,
                files_done=self.files_done,
                files_skipped=self.files_skipped,
                files_failed=self.files_failed,
                elapsed_seconds=elapsed,
                current_speed_bps=current_speed,
                average_speed_bps=average_speed,
                eta_seconds=eta,
            )


class ProgressThrottle:
    """Ensures on_overall_progress fires at most ~15x/sec regardless of how
    often callers call :meth:`maybe_emit` (which can safely be called on
    every single chunk without adding meaningful overhead -- the time
    check is cheap and skips the actual hook call otherwise).
    """

    def __init__(self, hooks: ProgressHooks, tracker: SpeedTracker, min_interval: float = PROGRESS_THROTTLE_SECONDS):
        self.hooks = hooks
        self.tracker = tracker
        self.min_interval = min_interval
        self._last_emit = 0.0
        self._lock = threading.Lock()

    def maybe_emit(self, force: bool = False) -> None:
        if self.hooks.on_overall_progress is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and (now - self._last_emit) < self.min_interval:
                return
            self._last_emit = now
        self.hooks.on_overall_progress(self.tracker.stats())


def format_bytes(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_speed(bytes_per_sec: float) -> str:
    return f"{format_bytes(bytes_per_sec)}/s"


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "calculating…"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
