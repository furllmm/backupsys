"""Compare content-profile heuristics with estimated compression/dedupe results.

This is an experimental, read-only report over explicitly supplied files. It
does not write a repository and does not replace an end-to-end backup benchmark.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from tools.content_report import build_report
from tools.content_compression_probe import probe_files


def build_experiment(
    paths: Sequence[str | Path],
    *,
    sample_size: int = 64 * 1024,
    chunk_size: int = 4 * 1024 * 1024,
    whole_file_threshold: int = 64 * 1024 * 1024,
    compression_level: int = 3,
) -> dict:
    """Return a combined report with profile and payload-estimate rows."""
    normalized = [Path(path) for path in paths]
    if not normalized:
        raise ValueError("at least one file is required")
    for path in normalized:
        if not path.is_file():
            raise ValueError(f"not a regular file: {path}")

    analysis = build_report(normalized, sample_size=sample_size)
    probe = probe_files(
        normalized,
        chunk_size=chunk_size,
        whole_file_threshold=whole_file_threshold,
        compression_level=compression_level,
    )
    probe_by_path = {row["path"]: row for row in probe["files"]}
    rows = []
    for item in analysis["files"]:
        profile = item["profile"]
        estimate = probe_by_path[item["path"]]
        rows.append({
            "path": item["path"],
            "file_bytes": profile["file_size"],
            "kind": profile["kind"],
            "format_hint": profile["format_hint"],
            "analysis_level": item["analysis_level"],
            "recommendation": profile["recommendation"],
            "entropy_bits_per_byte": profile["entropy_bits_per_byte"],
            "estimated_storage_mode": estimate["storage_mode_estimate"],
            "objects": estimate["objects"],
            "duplicate_objects": estimate["duplicate_objects_within_or_across_inputs"],
        })

    return {
        "schema_version": 1,
        "purpose": "compare sampled content heuristics with estimated compression/dedupe payload behavior",
        "settings": {
            "sample_size_bytes": sample_size,
            "chunk_size_bytes": chunk_size,
            "whole_file_threshold_bytes": whole_file_threshold,
            "compression_level": compression_level,
        },
        "summary": {
            "file_count": len(rows),
            "logical_bytes": probe["summary"]["logical_bytes"],
            "unique_objects": probe["summary"]["unique_objects"],
            "estimated_compressed_unique_object_bytes": probe["summary"]["estimated_compressed_unique_object_bytes"],
            "estimated_payload_savings_bytes": probe["summary"]["estimated_payload_savings_bytes"],
            "estimated_logical_to_payload_ratio": probe["summary"]["estimated_logical_to_payload_ratio"],
        },
        "files": rows,
        "limitations": [
            "Content classifications are bounded-sample heuristics and do not prove transform suitability.",
            "Compression and dedupe figures are estimates over supplied files, not measured repository allocation.",
            "The probe does not account for existing repository objects, metadata, indexes, or concurrency.",
            "No content-specific transform is applied; source files are read-only.",
            "Validate promising results with the real BackupSys backup/restore benchmark and exact restore integrity checks.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="explicit files to inspect")
    parser.add_argument("--sample-size", type=int, default=64 * 1024)
    parser.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--whole-file-threshold", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--compression-level", type=int, default=3)
    args = parser.parse_args(argv)
    if args.sample_size <= 0:
        parser.error("--sample-size must be greater than zero")
    try:
        report = build_experiment(
            args.paths,
            sample_size=args.sample_size,
            chunk_size=args.chunk_size,
            whole_file_threshold=args.whole_file_threshold,
            compression_level=args.compression_level,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"content experiment failed: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
