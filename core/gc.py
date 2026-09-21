"""
core.gc
=======

Mark-and-sweep garbage collection over the chunk store.

Phase 1 (mark): read every snapshot document and union all referenced chunk
hashes into a single set. This is the only "expensive" part and is O(total
snapshot metadata), not O(chunk store size).

Phase 2 (sweep): walk the on-disk chunk store; any chunk hash not in the
mark set is deleted.

Phase 3 (compact, packed engine only): dropping an index row doesn't
reclaim its bytes from the sequential chunks.dat file (see
core.packed_store's module docstring for why). If the store supports
compaction, GC triggers it after sweeping so disk space is actually
reclaimed and physical layout stays sequential rather than accumulating
holes over many GC cycles.

Deletion is safe by construction: a chunk is only unreferenced if *no*
snapshot's file list contains it, and snapshots are immutable once written,
so there is no window where a snapshot is mid-write referencing a chunk
that GC hasn't seen yet -- as long as GC is not run concurrently with an
in-progress backup. Callers should avoid overlapping `backup` and `gc`
against the same store (the CLI enforces this with a simple lock file).

Background/low-priority execution
-----------------------------------
GC, verification, and integrity scanning are maintenance operations, not
user-facing work -- they should never compete for CPU/IO with an active
backup or restore. Since the GC lock already makes GC and backup mutually
exclusive on the *same* store, the remaining concern is not starving other
processes on the machine (an interactive foreground app, another backup
job on a different store, etc.). :func:`low_priority` best-effort lowers
this process's scheduling priority for the duration of the operation, using
whatever mechanism the OS provides.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from .snapshot import list_snapshots
from .storage import ChunkStore


class GCLockError(RuntimeError):
    pass


@dataclass
class GCResult:
    chunks_before: int
    chunks_deleted: int
    bytes_freed: int
    compacted: bool = False
    bytes_after_compaction: Optional[int] = None


class GCLock:
    """A simple advisory file lock so `backup` and `gc` don't race on the
    same store. Not bulletproof (no lock stealing / staleness detection
    beyond PID presence) but sufficient for a single-user desktop tool.
    """

    def __init__(self, store: ChunkStore):
        self.path = store.root / ".gc.lock"

    def __enter__(self):
        if self.path.exists():
            raise GCLockError(
                f"Another operation appears to be in progress ({self.path} exists). "
                "If this is stale, delete the lock file manually."
            )
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.path.exists():
            self.path.unlink()
        return False


@contextlib.contextmanager
def low_priority():
    """Best-effort: lower this process's scheduling priority for the
    duration of the `with` block, restoring it afterward. On POSIX, uses
    ``os.nice()``. On Windows, uses ``psutil`` if available (there is no
    stdlib equivalent to Windows' SetPriorityClass); if neither applies,
    this is a silent no-op rather than a failure -- background maintenance
    running at normal priority is still correct, just not as considerate
    of other work on the machine as it could be.
    """
    lowered = False
    psutil_process = None
    original_windows_priority = None

    if hasattr(os, "nice"):
        try:
            os.nice(10)  # POSIX: higher value = lower priority
            lowered = True
        except OSError:
            pass
    else:
        try:
            import psutil  # optional dependency, not required for core functionality

            psutil_process = psutil.Process(os.getpid())
            original_windows_priority = psutil_process.nice()
            psutil_process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            lowered = True
        except Exception:
            psutil_process = None

    try:
        yield lowered
    finally:
        if hasattr(os, "nice") and lowered:
            try:
                os.nice(-10)
            except OSError:
                pass  # best-effort; typically requires privilege to raise back
        elif psutil_process is not None and original_windows_priority is not None:
            try:
                psutil_process.nice(original_windows_priority)
            except Exception:
                pass


def referenced_chunks(store: ChunkStore) -> Set[str]:
    referenced: Set[str] = set()
    for snap in list_snapshots(store):
        for f in snap.files:
            referenced.update(f.chunks)
    return referenced


def collect(store: ChunkStore, dry_run: bool = False, compact: bool = True) -> GCResult:
    with GCLock(store):
        live = referenced_chunks(store)
        all_chunks = list(store.all_chunk_hashes())
        chunks_before = len(all_chunks)

        deleted = 0
        bytes_freed = 0
        for chunk_hash in all_chunks:
            if chunk_hash in live:
                continue
            size = store.chunk_size_on_disk(chunk_hash)
            if not dry_run:
                store.delete_chunk(chunk_hash)
            deleted += 1
            bytes_freed += size

        store.logger.info(
            "gc: %d chunks scanned, %d deleted, %d bytes freed (dry_run=%s)",
            chunks_before,
            deleted,
            bytes_freed,
            dry_run,
        )

        compacted = False
        bytes_after_compaction = None
        if compact and not dry_run and deleted > 0 and hasattr(store, "compact"):
            _, bytes_after_compaction = store.compact()
            compacted = True

        return GCResult(
            chunks_before=chunks_before,
            chunks_deleted=deleted,
            bytes_freed=bytes_freed,
            compacted=compacted,
            bytes_after_compaction=bytes_after_compaction,
        )


def collect_in_background(
    store: ChunkStore, dry_run: bool = False, compact: bool = True
) -> threading.Thread:
    """Run :func:`collect` on a low-priority background thread so it never
    competes for CPU with foreground backup/restore work in the same
    process (e.g. a GUI that wants to offer "clean up now" without
    freezing the UI). Returns the (already started) thread.
    """

    def _run() -> None:
        with low_priority():
            collect(store, dry_run=dry_run, compact=compact)

    thread = threading.Thread(target=_run, name="gc-background", daemon=True)
    thread.start()
    return thread
