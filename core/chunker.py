"""
core.chunker
============

Splits file content into fixed-size (default 8 KiB) chunks and hashes each
chunk with SHA-256. Fixed-size chunking is used (rather than a rolling
content-defined chunker like FastCDC) to keep the reference implementation
small and dependency-free; the interface is deliberately narrow so a CDC
chunker can be swapped in later without touching callers.

Every chunk consumer receives :class:`Chunk` objects that carry both the
raw bytes and the hash, so hashing never has to be redone downstream.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

DEFAULT_CHUNK_SIZE = 8 * 1024  # 8 KiB, per spec


@dataclass(frozen=True)
class Chunk:
    hash: str
    data: bytes
    offset: int
    size: int


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_stream(stream: BinaryIO, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[Chunk]:
    """Yield :class:`Chunk` objects read from an open binary stream.

    An empty file yields exactly zero chunks (callers must handle empty
    files explicitly in the snapshot, recording a zero-length chunk list).
    """
    offset = 0
    while True:
        data = stream.read(chunk_size)
        if not data:
            return
        yield Chunk(hash=hash_bytes(data), data=data, offset=offset, size=len(data))
        offset += len(data)


def chunk_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[Chunk]:
    with open(path, "rb") as fh:
        yield from chunk_stream(fh, chunk_size=chunk_size)


def hash_file_chunks(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """Convenience helper: return just the ordered list of chunk hashes for
    a file, without keeping chunk bytes in memory longer than needed."""
    return [c.hash for c in chunk_file(path, chunk_size=chunk_size)]


def iter_raw_blocks(path: Path, block_size: int) -> Iterator[bytes]:
    """Yield raw file content in ``block_size`` blocks with no hashing --
    used by the multiprocessing hash+compress pipeline (see
    core.snapshot._hash_and_encode_chunk), which computes the hash inside
    the worker process instead, so it's never done twice."""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(block_size)
            if not block:
                return
            yield block
