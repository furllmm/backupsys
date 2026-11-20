"""Estimate BackupSys' current Zstandard + content-hash dedupe payload.

This is an isolated measurement tool, not a replacement for the real backup
benchmark. It uses the configured whole-file threshold/chunk size, hashes raw
objects before compression, and compresses each unique object once. Repository
metadata, SQLite/index overhead, filesystem allocation, and concurrency are
excluded; compare its result with a real backup before drawing conclusions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Sequence


def probe_files(
    paths: Sequence[str | Path],
    *,
    chunk_size: int = 4 * 1024 * 1024,
    whole_file_threshold: int = 64 * 1024 * 1024,
    compression_level: int = 3,
) -> dict:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if whole_file_threshold < 0:
        raise ValueError("whole_file_threshold must be non-negative")
    if not -7 <= compression_level <= 22:
        raise ValueError("compression_level must be between -7 and 22")

    files = [Path(p) for p in paths]
    for path in files:
        if not path.is_file():
            raise ValueError(f"not a regular file: {path}")

    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "Zstandard Python package is required; install the project's "
            "existing zstandard dependency to run this probe."
        ) from exc

    compressor = zstandard.ZstdCompressor(level=compression_level)
    started = time.perf_counter()
    unique: dict[str, dict] = {}
    logical_bytes = 0
    file_rows = []
    total_objects_seen = 0

    def add_object(data: bytes, path: Path) -> bool:
        nonlocal total_objects_seen
        digest = hashlib.sha256(data).hexdigest()
        total_objects_seen += 1
        if digest in unique:
            unique[digest]["references"] += 1
            return False
        encoded_size = len(compressor.compress(data))
        unique[digest] = {
            "raw_bytes": len(data),
            "encoded_bytes": encoded_size,
            "references": 1,
            "first_seen_path": str(path),
        }
        return True

    for path in files:
        size = path.stat().st_size
        logical_bytes += size
        object_count = 0
        duplicates = 0
        if size == 0:
            # BackupSys represents an empty file without storing a content chunk.
            pass
        elif size < whole_file_threshold:
            # Mirrors SnapshotBuilder._should_store_whole: the threshold itself
            # enters the chunked path (the comparison is intentionally strict).
            with path.open("rb") as stream:
                data = stream.read()
            is_new = add_object(data, path)
            object_count = 1
            duplicates = 0 if is_new else 1
        else:
            # Mirrors the current chunked path; memory stays bounded by one chunk.
            with path.open("rb") as stream:
                while True:
                    data = stream.read(chunk_size)
                    if not data:
                        break
                    is_new = add_object(data, path)
                    object_count += 1
                    if not is_new:
                        duplicates += 1
        file_rows.append({
            "path": str(path),
            "file_bytes": size,
            "storage_mode_estimate": "empty" if size == 0 else ("whole-file" if size < whole_file_threshold else "chunked"),
            "objects": object_count,
            "duplicate_objects_within_or_across_inputs": duplicates,
        })

    elapsed = time.perf_counter() - started
    unique_raw = sum(obj["raw_bytes"] for obj in unique.values())
    unique_encoded = sum(obj["encoded_bytes"] for obj in unique.values())
    return {
        "schema_version": 1,
        "method": "zstandard-per-unique-raw-object",
        "settings": {
            "chunk_size_bytes": chunk_size,
            "whole_file_threshold_bytes": whole_file_threshold,
            "compression_level": compression_level,
        },
        "summary": {
            "file_count": len(file_rows),
            "logical_bytes": logical_bytes,
            "objects_seen": total_objects_seen,
            "unique_objects": len(unique),
            "deduplicated_object_references": total_objects_seen - len(unique),
            "unique_raw_object_bytes": unique_raw,
            "estimated_compressed_unique_object_bytes": unique_encoded,
            "estimated_payload_savings_bytes": max(0, logical_bytes - unique_encoded),
            "estimated_logical_to_payload_ratio": round(logical_bytes / unique_encoded, 4) if unique_encoded else (1.0 if logical_bytes == 0 else None),
            "compression_elapsed_seconds": round(elapsed, 6),
        },
        "files": file_rows,
        "limitations": [
            "Estimate excludes store metadata, indexes, allocation overhead, and parallel pipeline effects.",
            "This measures only the supplied paths; it does not model existing chunks already present in a BackupSys repository.",
            "Compressed size may exceed raw size for incompressible objects; the real store's encoding policy may differ.",
            "Input file list is explicit; directories are not recursively scanned.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="explicit files to benchmark")
    parser.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--whole-file-threshold", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--compression-level", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        report = probe_files(
            args.paths,
            chunk_size=args.chunk_size,
            whole_file_threshold=args.whole_file_threshold,
            compression_level=args.compression_level,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"compression probe failed: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
