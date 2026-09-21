"""
core.restore
============

Reconstructs files from a single snapshot. Restore is deliberately
independent: it never consults any other snapshot, only the chunk store and
the one :class:`~core.snapshot.Snapshot` document being restored.

Responsiveness contract
------------------------
This engine is built so a GUI wrapping it never has to poll and never
freezes:

- Progress is byte-accurate (see :mod:`core.progress`), not just a file
  counter, so a handful of huge files doesn't make the bar appear stuck.
- Pause/Cancel are checked between chunks *within* a file, not just
  between files, so a multi-gigabyte file doesn't delay a Cancel.
- Cancelling abandons the current file's temp output (never renamed into
  place), so cancellation can never corrupt a destination file -- the file
  either finishes completely or doesn't exist at the destination at all.
- Every completed file is durably recorded in a :class:`~core.resume.ResumeLog`
  as it finishes, so a process that's interrupted (crash, kill, cancel)
  can resume later from the last completed file rather than from zero.
- A single failed file never aborts the run; failures are collected and
  reported in the final summary while the rest of the restore continues.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import pathresolver
from .progress import (
    OperationCancelled,
    OperationControl,
    ProgressHooks,
    ProgressThrottle,
    SpeedTracker,
)
from .resume import ResumeLog
from .snapshot import FileEntry, Snapshot
from .storage import ChunkStore, IntegrityError

# How often, in bytes written within a single large file, to check for
# cancel/pause. 1MB strikes a balance: frequent enough that a multi-GB
# file responds to Cancel almost instantly, rare enough that the overhead
# of the check is negligible.
_INTRA_FILE_CHECK_INTERVAL_BYTES = 1024 * 1024


@dataclass
class RestoreResult:
    total: int
    restored: int
    skipped: int
    failed: List[str]
    cancelled: bool
    duration_seconds: float
    bytes_restored: int
    average_speed_bps: float


class RestoreEngine:
    def __init__(self, store: ChunkStore, max_workers: int = 8, verify_chunks: bool = True):
        self.store = store
        self.max_workers = max_workers
        self.verify_chunks = verify_chunks

    # ------------------------------------------------------------------
    def restore(
        self,
        snapshot: Snapshot,
        target_dir: Path,
        include_prefix: Optional[str] = None,
        control: Optional[OperationControl] = None,
        hooks: Optional[ProgressHooks] = None,
        resume: bool = True,
    ) -> RestoreResult:
        """Restore every file in ``snapshot`` under ``target_dir``.

        A file recorded as ``%APPDATA%/Discord/x`` is written to
        ``target_dir/PCTAPPDATAPCT/Discord/x`` (token sanitized into a
        literal folder name so no '..'/token characters can escape
        ``target_dir``). Use :meth:`restore_in_place` to instead write
        back to each file's real resolved location on this machine.
        """
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        def resolve_dest(entry: FileEntry) -> Optional[Path]:
            return target_dir / _sanitize_logical_path(entry.path)

        return self._execute(
            snapshot,
            resolve_dest,
            include_prefix=include_prefix,
            control=control,
            hooks=hooks,
            resume=resume,
            resume_target_key=str(target_dir),
        )

    def restore_in_place(
        self,
        snapshot: Snapshot,
        include_prefix: Optional[str] = None,
        control: Optional[OperationControl] = None,
        hooks: Optional[ProgressHooks] = None,
        resume: bool = True,
    ) -> RestoreResult:
        """Restore files back to their original resolved locations on this
        machine, resolving each logical path via :mod:`core.pathresolver`.
        """

        def resolve_dest(entry: FileEntry) -> Optional[Path]:
            try:
                return pathresolver.resolve(entry.path)
            except pathresolver.UnresolvableTokenError:
                return None

        return self._execute(
            snapshot,
            resolve_dest,
            include_prefix=include_prefix,
            control=control,
            hooks=hooks,
            resume=resume,
            resume_target_key="__in_place__",
        )

    # ------------------------------------------------------------------
    def _execute(
        self,
        snapshot: Snapshot,
        resolve_dest,
        include_prefix: Optional[str],
        control: Optional[OperationControl],
        hooks: Optional[ProgressHooks],
        resume: bool,
        resume_target_key: str,
    ) -> RestoreResult:
        control = control or OperationControl()
        hooks = hooks or ProgressHooks()

        files = snapshot.files
        if include_prefix:
            files = [f for f in files if f.path.startswith(include_prefix)]

        resume_log = ResumeLog(self.store, snapshot.id, resume_target_key)
        already_done = resume_log.load_completed() if resume else set()
        if not resume:
            resume_log.clear()
            resume_log = ResumeLog(self.store, snapshot.id, resume_target_key)

        bytes_total = sum(f.size for f in files)
        tracker = SpeedTracker(bytes_total=bytes_total, files_total=len(files))
        throttle = ProgressThrottle(hooks, tracker)

        # Files already completed in a prior interrupted run still count
        # toward "done" immediately, so progress reflects real remaining
        # work rather than restarting the percentage from 0.
        for entry in files:
            if entry.path in already_done:
                tracker.record_bytes(entry.size)
                tracker.record_file_done("skipped")

        pending = [f for f in files if f.path not in already_done]
        failed: List[str] = []
        was_cancelled = False

        # Restore optimization: if the store supports it (the packed
        # engine), warm its LRU cache by reading every chunk this restore
        # will need in one background pass sorted by physical offset --
        # i.e. sequentially through chunks.dat rather than following
        # per-file logical order, which would otherwise mean random seeks
        # back and forth across the data file. By the time each file's
        # worker thread actually asks for a given chunk, it's very often
        # already sitting in cache instead of requiring its own disk read.
        if hasattr(self.store, "prefetch_async"):
            all_needed_hashes = [h for f in pending for h in f.chunks]
            if all_needed_hashes:
                self.store.prefetch_async(all_needed_hashes)

        def process(entry: FileEntry) -> None:
            nonlocal was_cancelled
            try:
                control.wait_if_paused()
            except OperationCancelled:
                was_cancelled = True
                return

            dest = resolve_dest(entry)
            if dest is None:
                failed.append(f"{entry.path}: no resolvable destination on this OS")
                tracker.record_file_done("failed")
                if hooks.on_file_done:
                    hooks.on_file_done(entry.path, "failed", "unresolvable path")
                throttle.maybe_emit()
                return

            if hooks.on_file_start:
                hooks.on_file_start(entry.path, entry.size)

            err: Optional[str]
            if entry.is_symlink:
                err = self._restore_symlink(entry, dest)
                status = "failed" if err else "ok"
            else:
                try:
                    err = self._restore_file(entry, dest, control, tracker, throttle, hooks)
                    status = "failed" if err else "ok"
                except OperationCancelled:
                    was_cancelled = True
                    err = "cancelled before completion"
                    status = "cancelled"

            if status == "failed":
                failed.append(f"{entry.path}: {err}")
                hooks.log("error", f"Failed: {entry.path} ({err})")
            elif status == "ok":
                resume_log.record(entry.path, "ok")
                hooks.log("info", f"Restored {entry.path}")
            elif status == "cancelled":
                hooks.log("warn", f"Cancelled mid-file, will resume later: {entry.path}")

            if status != "cancelled":
                tracker.record_file_done(status)
            if hooks.on_file_done:
                hooks.on_file_done(entry.path, status, err or "")
            throttle.maybe_emit()

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(process, entry) for entry in pending]
            for future in concurrent.futures.as_completed(futures):
                future.result()  # re-raise unexpected exceptions rather than swallow them

        throttle.maybe_emit(force=True)

        if not was_cancelled:
            resume_log.clear()  # nothing left to resume; a future run starts fresh
        else:
            resume_log.close()

        stats = tracker.stats()
        self.store.logger.info(
            "restore of %s complete: %d ok, %d skipped, %d failed, cancelled=%s",
            snapshot.id, tracker.files_done, tracker.files_skipped, len(failed), was_cancelled,
        )
        return RestoreResult(
            total=len(files),
            restored=tracker.files_done,
            skipped=tracker.files_skipped,
            failed=failed,
            cancelled=was_cancelled,
            duration_seconds=stats.elapsed_seconds,
            bytes_restored=stats.bytes_done,
            average_speed_bps=stats.average_speed_bps,
        )

    # ------------------------------------------------------------------
    def _restore_file(
        self,
        entry: FileEntry,
        dest: Path,
        control: OperationControl,
        tracker: SpeedTracker,
        throttle: ProgressThrottle,
        hooks: ProgressHooks,
    ) -> Optional[str]:
        """Returns an error message string on failure, None on success.
        Raises :class:`OperationCancelled` if cancelled mid-file (caller
        treats that as its own status, distinct from failure)."""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".restore_", dir=dest.parent)
            bytes_written = 0
            bytes_since_check = 0
            try:
                with os.fdopen(fd, "wb") as tmp:
                    for chunk_hash in entry.chunks:
                        data = self.store.get_chunk(chunk_hash, verify=self.verify_chunks)
                        tmp.write(data)
                        bytes_written += len(data)
                        bytes_since_check += len(data)
                        tracker.record_bytes(len(data))
                        if hooks.on_file_chunk_progress:
                            hooks.on_file_chunk_progress(entry.path, bytes_written, entry.size)
                        throttle.maybe_emit()

                        if bytes_since_check >= _INTRA_FILE_CHECK_INTERVAL_BYTES:
                            bytes_since_check = 0
                            control.wait_if_paused()  # also raises OperationCancelled

                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, dest)
            except OperationCancelled:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                # Undo the bytes we optimistically counted for this
                # abandoned file so overall progress stays accurate.
                tracker.record_bytes(-bytes_written)
                raise
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

            if entry.permissions is not None and os.name == "posix":
                os.chmod(dest, entry.permissions)
            if entry.mtime_ns:
                os.utime(dest, ns=(entry.mtime_ns, entry.mtime_ns))
            return None
        except (OSError, IntegrityError) as exc:
            return str(exc)

    def _restore_symlink(self, entry: FileEntry, dest: Path) -> Optional[str]:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            os.symlink(entry.symlink_target, dest)
            return None
        except OSError as exc:
            return f"symlink restore skipped: {exc}"


def _sanitize_logical_path(logical_path: str) -> Path:
    """Turn a logical path like '%APPDATA%/Discord/settings.json' into a
    safe relative filesystem path for nested (non-in-place) restores.
    Prevents '..' traversal and illegal token characters from escaping the
    target directory.
    """
    safe = logical_path.replace("%", "PCT").replace("$", "DLR").replace("~", "TILDE")
    parts = [p for p in safe.split("/") if p not in ("", ".", "..")]
    return Path(*parts)
