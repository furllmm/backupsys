"""CLI report for the experimental, read-only Content Intelligence Layer.

This reports sampled content characteristics; it does not benchmark backup storage,
compress files, or modify sources. Paths are explicit to avoid accidental recursive
scans of large or sensitive trees.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from core.content_policy import analyze_paths


def build_report(paths: Sequence[str | Path], sample_size: int = 64 * 1024) -> dict:
    """Analyze supplied files and return a JSON-serializable aggregate report."""
    started = time.perf_counter()
    decisions = analyze_paths(paths, sample_size=sample_size)
    elapsed = time.perf_counter() - started
    files = [decision.to_dict() for decision in decisions]
    levels: dict[str, int] = {}
    formats: dict[str, int] = {}
    total_bytes = 0
    total_sampled = 0
    for item in files:
        profile = item["profile"]
        level = item["analysis_level"]
        fmt = profile["format_hint"]
        levels[level] = levels.get(level, 0) + 1
        formats[fmt] = formats.get(fmt, 0) + 1
        total_bytes += profile["file_size"] or 0
        total_sampled += profile["sample_bytes"]
    return {
        "schema_version": 1,
        "sample_size_limit_bytes": sample_size,
        "summary": {
            "file_count": len(files),
            "total_file_bytes": total_bytes,
            "total_sampled_bytes": total_sampled,
            "elapsed_seconds": round(elapsed, 6),
            "files_per_second": round(len(files) / elapsed, 3) if elapsed > 0 else 0.0,
            "analysis_levels": levels,
            "format_hints": formats,
        },
        "files": files,
        "limitations": [
            "Sampling is heuristic and may not represent the whole file.",
            "This report does not measure compression savings or cross-file deduplication.",
            "No source files or backup storage are modified.",
        ],
    }


def _csv_rows(report: dict):
    for item in report["files"]:
        profile = item["profile"]
        yield {
            "path": item["path"],
            "analysis_level": item["analysis_level"],
            "reason": item["reason"],
            "kind": profile["kind"],
            "format_hint": profile["format_hint"],
            "file_size": profile["file_size"],
            "sample_bytes": profile["sample_bytes"],
            "entropy_bits_per_byte": profile["entropy_bits_per_byte"],
            "zero_byte_ratio": profile["zero_byte_ratio"],
            "printable_ratio": profile["printable_ratio"],
            "recommendation": profile["recommendation"],
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="explicit file paths to inspect")
    parser.add_argument("--sample-size", type=int, default=64 * 1024,
                        help="maximum bytes sampled per file (default: 65536)")
    parser.add_argument("--format", choices=("json", "csv"), default="json",
                        help="report format (default: json)")
    args = parser.parse_args(argv)
    if args.sample_size <= 0:
        parser.error("--sample-size must be greater than zero")
    try:
        report = build_report(args.paths, sample_size=args.sample_size)
    except (OSError, ValueError) as exc:
        print(f"content report failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        rows = list(_csv_rows(report))
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else [
            "path", "analysis_level", "reason", "kind", "format_hint", "file_size",
            "sample_bytes", "entropy_bits_per_byte", "zero_byte_ratio",
            "printable_ratio", "recommendation",
        ])
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
