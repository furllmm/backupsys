"""
core.changedetect
==================

Cheap "did this file actually change" test that runs before we pay the cost
of full SHA-256 chunking. This is what makes incremental backups fast: a
plan with 200,000 AppData files where only 40 changed should not re-hash
the other 199,960.

Strategy, cheapest first:

1. Compare ``(size, mtime_ns)`` against the previous snapshot's recorded
   value for this logical path. If both match, the file is assumed
   unchanged and its previous chunk list is reused verbatim.
2. If ``xxhash`` is installed, an optional fast whole-file hash can be used
   as a stronger (but still cheap) check when mtimes are unreliable (e.g.
   after a file copy that preserved size but not mtime). This is opt-in via
   ``use_fast_hash=True`` because it requires a full read of the file.

If unchanged, callers skip SHA-256 chunking entirely and copy the chunk
hash list from the previous snapshot's :class:`FileEntry`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import xxhash  # type: ignore

    _XXHASH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _XXHASH_AVAILABLE = False


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    fast_hash: Optional[str] = None


def fingerprint(path: Path, use_fast_hash: bool = False) -> FileFingerprint:
    st = path.stat()
    fast_hash = None
    if use_fast_hash and _XXHASH_AVAILABLE:
        h = xxhash.xxh64()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        fast_hash = h.hexdigest()
    return FileFingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns, fast_hash=fast_hash)


def is_unchanged(
    current: FileFingerprint,
    previous_size: Optional[int],
    previous_mtime_ns: Optional[int],
    previous_fast_hash: Optional[str] = None,
) -> bool:
    """Return True if `current` matches the previous snapshot's recorded
    fingerprint closely enough to skip re-chunking."""
    if previous_size is None or previous_mtime_ns is None:
        return False
    if current.size != previous_size or current.mtime_ns != previous_mtime_ns:
        return False
    if current.fast_hash is not None and previous_fast_hash is not None:
        return current.fast_hash == previous_fast_hash
    return True
