"""Measure content heuristics against a real BackupSys backup/restore cycle.

The source directory is read-only. A fresh temporary store and restore target
are created for each run; the restored tree is SHA-256 verified before a
report is emitted.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Sequence

from core.benchmark import verify_restored_dataset
from core.restore import RestoreEngine
from core.snapshot import SnapshotBuilder
from core.store_factory import open_store
from plans.manager import PlanManager
from tools.content_report import build_report


def run_content_store_benchmark(
    source_root: str | Path,
    *,
    engine: str = "packed",
    compression: bool = True,
    chunk_size: int = 4 * 1024 * 1024,
    whole_file_threshold: int = 64 * 1024 * 1024,
    sample_size: int = 64 * 1024,
) -> dict:
    """Back up an existing directory into a fresh temporary store and verify restore."""
    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source must be an existing directory: {source}")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"source directory contains no files: {source}")
    if chunk_size <= 0 or whole_file_threshold < 0 or sample_size <= 0:
        raise ValueError("chunk size and sample size must be positive; threshold cannot be negative")

    logical_bytes = sum(path.stat().st_size for path in files)
    profiles = build_report(files, sample_size=sample_size)
    kinds = Counter(item["profile"]["kind"] for item in profiles["files"])
    recommendations = Counter(
        item["profile"]["recommendation"] for item in profiles["files"]
    )

    workdir = Path(tempfile.mkdtemp(prefix="backupsys_content_bench_"))
    store = None
    try:
        store = open_store(workdir / "store", engine=engine)
        if hasattr(store, "use_compression"):
            store.use_compression = bool(compression and store.use_compression)
        compression_enabled = bool(getattr(store, "use_compression", False))

        manager = PlanManager(store)
        plan = manager.create("Content intelligence benchmark", sources=[str(source)])
        builder = SnapshotBuilder(
            store,
            chunk_size=chunk_size,
            whole_file_threshold=whole_file_threshold,
        )

        started = time.perf_counter()
        snapshot = builder.build(plan.id, "file", plan.sources)
        backup_seconds = time.perf_counter() - started

        restored_root = workdir / "restored"
        started = time.perf_counter()
        result = RestoreEngine(store).restore(snapshot, restored_root)
        restore_seconds = time.perf_counter() - started
        if result.failed:
            raise RuntimeError(f"restore reported failures: {result.failed[:5]}")

        verify_restored_dataset(source, restored_root)

        if hasattr(store, "chunk_count"):
            object_count = store.chunk_count()
            physical_bytes = store.repository_size_bytes()
        else:
            hashes = list(store.all_chunk_hashes())
            object_count = len(hashes)
            physical_bytes = sum(store.chunk_size_on_disk(value) for value in hashes)

        return {
            "schema_version": 1,
            "benchmark": "real-backupsys-content-corpus",
            "source": str(source),
            "engine": engine,
            "compression_enabled": compression_enabled,
            "settings": {
                "chunk_size_bytes": chunk_size,
                "whole_file_threshold_bytes": whole_file_threshold,
                "sample_size_bytes": sample_size,
            },
            "integrity": {"verified": True, "method": "relative paths, sizes, SHA-256"},
            "summary": {
                "file_count": len(files),
                "logical_bytes": logical_bytes,
                "repository_bytes": physical_bytes,
                "actual_payload_savings_bytes": max(0, logical_bytes - physical_bytes),
                "logical_to_repository_ratio": round(logical_bytes / physical_bytes, 4) if physical_bytes else None,
                "stored_objects": object_count,
                "backup_seconds": round(backup_seconds, 6),
                "restore_seconds": round(restore_seconds, 6),
                "backup_mib_per_second": round(logical_bytes / (1024 * 1024) / backup_seconds, 4) if backup_seconds else None,
                "restore_mib_per_second": round(logical_bytes / (1024 * 1024) / restore_seconds, 4) if restore_seconds else None,
            },
            "profile_distribution": {
                "kinds": dict(sorted(kinds.items())),
                "recommendations": dict(sorted(recommendations.items())),
            },
            "limitations": [
                "Repository byte counts are measured after a fresh backup; filesystem allocation and external destination overhead are excluded.",
                "Results depend on the source corpus, storage engine, compression availability, and machine load.",
                "Profile recommendations are heuristics; this benchmark does not apply content-specific transforms.",
                "Run multiple times and compare identical settings before drawing performance conclusions.",
            ],
        }
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="existing directory to benchmark; never modified")
    parser.add_argument("--engine", choices=("packed", "classic"), default="packed")
    parser.add_argument("--no-compression", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--whole-file-threshold", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--sample-size", type=int, default=64 * 1024)
    args = parser.parse_args(argv)
    try:
        report = run_content_store_benchmark(
            args.source,
            engine=args.engine,
            compression=not args.no_compression,
            chunk_size=args.chunk_size,
            whole_file_threshold=args.whole_file_threshold,
            sample_size=args.sample_size,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"content store benchmark failed: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
