"""
core.resume
===========

Tracks which files a restore run has already completed, as a simple
append-only JSONL log, so a cancelled or interrupted restore can resume
from the last completed file instead of restarting from zero.

Why append-only rather than rewriting a JSON snapshot of "all completed
paths so far": restores can involve hundreds of thousands to millions of
files, and rewriting a full JSON document after every single file would
turn an O(1) per-file bookkeeping cost into O(n), dominating runtime on
large restores. Appending one line per completion is O(1) and safe to do
from multiple worker threads with a simple lock around the write.

One resume log exists per (snapshot_id, target) pair, identified by a
short hash of the target path, so restoring the same snapshot to two
different destinations doesn't cross-contaminate resume state.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Set

from core.storage import ChunkStore


def _target_key(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]


class ResumeLog:
    def __init__(self, store: ChunkStore, snapshot_id: str, target: str):
        self.dir = store.root / "restore_state"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{snapshot_id}__{_target_key(target)}.jsonl"
        self._lock = threading.Lock()
        self._fh = None

    # ------------------------------------------------------------------
    def load_completed(self) -> Set[str]:
        """Return the set of logical paths already marked complete from a
        previous (interrupted) run. Empty if there is no prior state."""
        if not self.path.exists():
            return set()
        completed: Set[str] = set()
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn last line from a hard crash
                if record.get("status") == "ok":
                    completed.add(record["path"])
        return completed

    def record(self, logical_path: str, status: str) -> None:
        """Append one completion record. Thread-safe; safe to call from
        many worker threads concurrently."""
        with self._lock:
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(json.dumps({"path": logical_path, "status": status}) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def clear(self) -> None:
        """Delete the resume log -- called once a restore completes fully
        (nothing left to resume), so a later fresh restore of the same
        snapshot to the same target doesn't skip files that a *new*
        request actually wants re-copied."""
        self.close()
        if self.path.exists():
            self.path.unlink()
