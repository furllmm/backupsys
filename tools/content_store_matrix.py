"""Run a controlled packed/classic storage comparison on one source tree.

Each configuration gets a fresh temporary repository. The source tree is
read-only, and every run verifies the restored paths, sizes, and SHA-256 hashes.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from tools.content_store_benchmark import run_content_store_benchmark


def run_content_store_matrix(
    source_root: str,
    *,
    chunk_size: int = 4 * 1024 * 1024,
    whole_file_threshold: int = 64 * 1024 * 1024,
    sample_size: int = 64 * 1024,
) -> dict:
    """Compare packed with compression on/off and classic without compression."""
    configurations = (
        {"engine": "packed", "compression_requested": True},
        {"engine": "packed", "compression_requested": False},
        {"engine": "classic", "compression_requested": False},
    )
    results = []
    expected_logical_bytes = None
    expected_file_count = None

    for config in configurations:
        report = run_content_store_benchmark(
            source_root,
            engine=config["engine"],
            compression=config["compression_requested"],
            chunk_size=chunk_size,
            whole_file_threshold=whole_file_threshold,
            sample_size=sample_size,
        )
        summary = report["summary"]
        if expected_logical_bytes is None:
            expected_logical_bytes = summary["logical_bytes"]
            expected_file_count = summary["file_count"]
        elif (
            summary["logical_bytes"] != expected_logical_bytes
            or summary["file_count"] != expected_file_count
        ):
            raise RuntimeError(
                "source dataset changed during matrix run; results are not comparable"
            )

        results.append({
            "configuration": config,
            "compression_enabled": report["compression_enabled"],
            "integrity": report["integrity"],
            "summary": summary,
            "profile_distribution": report["profile_distribution"],
        })

    return {
        "schema_version": 1,
        "benchmark": "real-backupsys-content-matrix",
        "source": results[0]["configuration"] and report["source"],
        "dataset": {
            "file_count": expected_file_count,
            "logical_bytes": expected_logical_bytes,
        },
        "results": results,
        "notes": [
            "Each configuration uses a fresh temporary repository and restore target.",
            "Classic is tested without compression because that engine does not expose the packed compression switch.",
            "Packed compression may be unavailable when the optional compression dependency is missing; inspect compression_enabled.",
            "Use repeated runs and representative real datasets before drawing performance conclusions.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="existing directory to benchmark; never modified")
    parser.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--whole-file-threshold", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--sample-size", type=int, default=64 * 1024)
    args = parser.parse_args(argv)

    try:
        report = run_content_store_matrix(
            args.source,
            chunk_size=args.chunk_size,
            whole_file_threshold=args.whole_file_threshold,
            sample_size=args.sample_size,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"content store matrix failed: {exc}", file=sys.stderr)
        return 2

    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
