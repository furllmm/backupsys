"""
core.benchmark
================

Generates a synthetic dataset with a realistic mix of file sizes and a
controlled amount of duplicate content, then runs it through a real
backup + restore cycle against a fresh store, measuring:

- Backup speed (MB/s, based on logical bytes processed)
- Restore speed (MB/s)
- CPU time consumed (user + system)
- Peak resident memory (RSS)
- Deduplication ratio (logical bytes / physical bytes stored)
- Repository growth (on-disk bytes after the run)
- Chunk count

This exercises the real code paths (SnapshotBuilder, RestoreEngine,
whichever storage engine is selected) rather than a synthetic
microbenchmark of individual functions, so results reflect what an actual
backup/restore would experience.
"""

from __future__ import annotations

import os
import platform
import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.progress import format_bytes, format_speed
from core.restore import RestoreEngine
from core.snapshot import SnapshotBuilder
from core.store_factory import open_store
from plans.manager import PlanManager

try:
    import resource

    _RESOURCE_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows has no `resource` module
    _RESOURCE_AVAILABLE = False


@dataclass
class ResourceUsage:
    cpu_seconds: Optional[float]
    peak_rss_bytes: Optional[int]


@dataclass
class BenchmarkReport:
    engine: str
    dataset_size_bytes: int
    dataset_file_count: int
    chunk_size_mb: float
    whole_file_threshold_mb: float
    compression_enabled: bool

    backup_seconds: float
    backup_speed_mbps: float
    backup_cpu: ResourceUsage

    restore_seconds: float
    restore_speed_mbps: float
    restore_cpu: ResourceUsage

    chunk_count: int
    repository_bytes: int
    dedup_ratio: float

    def to_text(self) -> str:
        lines = [
            "=== Backup Engine Benchmark Report ===",
            f"Engine: {self.engine}",
            f"Dataset: {format_bytes(self.dataset_size_bytes)} across {self.dataset_file_count} files",
            f"Chunk size: {self.chunk_size_mb} MB | Whole-file threshold: {self.whole_file_threshold_mb} MB "
            f"| Compression: {'on' if self.compression_enabled else 'off'}",
            "",
            f"Backup:  {self.backup_seconds:.2f}s  ({format_speed(self.backup_speed_mbps * 1024 * 1024)})",
        ]
        if self.backup_cpu.cpu_seconds is not None:
            lines.append(f"  CPU time: {self.backup_cpu.cpu_seconds:.2f}s")
        if self.backup_cpu.peak_rss_bytes is not None:
            lines.append(f"  Peak RSS: {format_bytes(self.backup_cpu.peak_rss_bytes)}")

        lines.append(f"Restore: {self.restore_seconds:.2f}s  ({format_speed(self.restore_speed_mbps * 1024 * 1024)})")
        if self.restore_cpu.cpu_seconds is not None:
            lines.append(f"  CPU time: {self.restore_cpu.cpu_seconds:.2f}s")
        if self.restore_cpu.peak_rss_bytes is not None:
            lines.append(f"  Peak RSS: {format_bytes(self.restore_cpu.peak_rss_bytes)}")

        lines += [
            "",
            f"Chunks stored: {self.chunk_count}",
            f"Repository size on disk: {format_bytes(self.repository_bytes)}",
            f"Deduplication ratio: {self.dedup_ratio:.2f}x "
            f"(logical / physical -- 1.0x means no space saved by dedup)",
        ]
        return "\n".join(lines)


def _measure_resources() -> ResourceUsage:
    if not _RESOURCE_AVAILABLE:
        return ResourceUsage(cpu_seconds=None, peak_rss_bytes=None)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = usage.ru_utime + usage.ru_stime
    # ru_maxrss is KB on Linux, bytes on macOS -- normalize to bytes.
    rss_multiplier = 1024 if platform.system() != "Darwin" else 1
    return ResourceUsage(cpu_seconds=cpu_seconds, peak_rss_bytes=usage.ru_maxrss * rss_multiplier)


def _diff_resources(before: ResourceUsage, after: ResourceUsage) -> ResourceUsage:
    cpu = None
    if before.cpu_seconds is not None and after.cpu_seconds is not None:
        cpu = max(0.0, after.cpu_seconds - before.cpu_seconds)
    # Peak RSS is a high-water mark for the whole process, not subtractable
    # between phases; report the peak observed at the end of the phase.
    return ResourceUsage(cpu_seconds=cpu, peak_rss_bytes=after.peak_rss_bytes)


def generate_synthetic_dataset(
    root: Path,
    total_size_bytes: int,
    duplicate_fraction: float = 0.3,
    seed: int = 42,
) -> int:
    """Populate ``root`` with a realistic mix of file sizes: mostly small
    files (a few KB to a few MB) with a handful of large files (well above
    the default 64MB whole-file threshold), so the benchmark exercises
    both the whole-file and chunked code paths. ``duplicate_fraction`` of
    the total size is generated as exact copies of earlier content, to
    produce a measurable, realistic dedup ratio rather than either 1.0x
    (all-random data) or unrealistically high (all-duplicate data).
    Returns the actual number of files created.
    """
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)

    written = 0
    file_count = 0
    duplicate_pool: list[bytes] = []

    # Size distribution: mostly small/medium files, occasional large ones,
    # roughly matching a typical Documents/Projects folder rather than a
    # media library or a database dump.
    size_buckets = [
        (0.55, 4 * 1024, 256 * 1024),          # small files
        (0.30, 256 * 1024, 4 * 1024 * 1024),    # medium files
        (0.12, 4 * 1024 * 1024, 32 * 1024 * 1024),  # sizable files
        (0.03, 80 * 1024 * 1024, 150 * 1024 * 1024),  # large, above whole-file threshold
    ]

    while written < total_size_bytes:
        use_duplicate = duplicate_pool and rng.random() < duplicate_fraction
        if use_duplicate:
            content = rng.choice(duplicate_pool)
        else:
            roll = rng.random()
            cumulative = 0.0
            for probability, low, high in size_buckets:
                cumulative += probability
                if roll <= cumulative:
                    size = rng.randint(low, high)
                    break
            else:
                size = rng.randint(4 * 1024, 256 * 1024)
            size = min(size, total_size_bytes - written) or size
            content = os.urandom(size)
            if len(duplicate_pool) < 20:
                duplicate_pool.append(content)

        subdir = root / f"folder_{file_count // 50}"
        subdir.mkdir(exist_ok=True)
        path = subdir / f"file_{file_count:05d}.dat"
        path.write_bytes(content)
        written += len(content)
        file_count += 1

    return file_count


def run_benchmark(
    dataset_size_mb: float = 200.0,
    chunk_size_mb: float = 4.0,
    whole_file_threshold_mb: float = 64.0,
    compression: bool = True,
    engine: str = "packed",
    keep_temp: bool = False,
) -> BenchmarkReport:
    """Run a full synthetic backup + restore benchmark and return a
    :class:`BenchmarkReport`. Uses fresh temporary directories for both
    the source dataset and the store, so it never touches real user data
    or an existing repository.
    """
    workdir = Path(tempfile.mkdtemp(prefix="backupsys_benchmark_"))
    src_dir = workdir / "dataset"
    store_root = workdir / "store"
    restore_dir = workdir / "restored"

    try:
        total_bytes = int(dataset_size_mb * 1024 * 1024)
        file_count = generate_synthetic_dataset(src_dir, total_bytes)
        actual_size = sum(f.stat().st_size for f in src_dir.rglob("*") if f.is_file())

        store = open_store(store_root, engine=engine)
        manager = PlanManager(store)
        plan = manager.create("Benchmark", sources=[str(src_dir)])

        builder = SnapshotBuilder(
            store,
            chunk_size=int(chunk_size_mb * 1024 * 1024),
            whole_file_threshold=int(whole_file_threshold_mb * 1024 * 1024),
        )

        cpu_before = _measure_resources()
        backup_start = time.perf_counter()
        snapshot = builder.build(plan.id, "file", plan.sources)
        backup_elapsed = time.perf_counter() - backup_start
        backup_cpu = _diff_resources(cpu_before, _measure_resources())

        engine_obj = RestoreEngine(store)
        cpu_before = _measure_resources()
        restore_start = time.perf_counter()
        result = engine_obj.restore(snapshot, restore_dir)
        restore_elapsed = time.perf_counter() - restore_start
        restore_cpu = _diff_resources(cpu_before, _measure_resources())

        if result.failed:
            raise RuntimeError(f"Benchmark restore had failures: {result.failed[:5]}")

        if hasattr(store, "chunk_count"):
            chunk_count = store.chunk_count()
            repository_bytes = store.repository_size_bytes()
        else:
            chunk_count = len(list(store.all_chunk_hashes()))
            repository_bytes = sum(
                store.chunk_size_on_disk(h) for h in store.all_chunk_hashes()
            )

        dedup_ratio = (actual_size / repository_bytes) if repository_bytes > 0 else 1.0
        backup_mbps = (actual_size / (1024 * 1024)) / backup_elapsed if backup_elapsed > 0 else 0.0
        restore_mbps = (actual_size / (1024 * 1024)) / restore_elapsed if restore_elapsed > 0 else 0.0

        if hasattr(store, "close"):
            store.close()

        return BenchmarkReport(
            engine=engine,
            dataset_size_bytes=actual_size,
            dataset_file_count=file_count,
            chunk_size_mb=chunk_size_mb,
            whole_file_threshold_mb=whole_file_threshold_mb,
            compression_enabled=compression,
            backup_seconds=backup_elapsed,
            backup_speed_mbps=backup_mbps,
            backup_cpu=backup_cpu,
            restore_seconds=restore_elapsed,
            restore_speed_mbps=restore_mbps,
            restore_cpu=restore_cpu,
            chunk_count=chunk_count,
            repository_bytes=repository_bytes,
            dedup_ratio=dedup_ratio,
        )
    finally:
        if not keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)
