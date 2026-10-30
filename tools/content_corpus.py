"""Generate a small, deterministic mixed-content corpus for local experiments.

The output is synthetic and safe to delete. The target must be absent or empty;
this command never overwrites existing files.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import zlib
from pathlib import Path
from typing import Sequence


def generate_corpus(target: str | Path, seed: int = 2026) -> list[Path]:
    root = Path(target)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError(f"target must be an absent or empty directory: {root}")
    root.mkdir(parents=True, exist_ok=True)

    events = [
        f"2026-01-{(i % 28) + 1:02d}T12:{i % 60:02d}:00Z INFO worker={i % 8} task=sync status=ok bytes={i * 127}\n"
        for i in range(4000)
    ]
    log_data = "".join(events).encode("utf-8")
    rows = ["id,group,status,value\n"] + [
        f"{i},{i % 12},{'ok' if i % 9 else 'retry'},{(i * 37) % 10007}\n"
        for i in range(5000)
    ]
    csv_data = "".join(rows).encode("utf-8")
    records = [
        {"id": i, "group": i % 12, "active": i % 7 != 0,
         "label": f"item-{i % 40}", "value": (i * 7919) % 100003}
        for i in range(120)
    ]
    json_data = (json.dumps({"schema": 1, "records": records}, separators=(",", ":")) + "\n").encode("utf-8")
    jsonl_data = "".join(
        json.dumps(
            {"id": i, "group": i % 12, "active": i % 7 != 0,
             "label": f"item-{i % 40}", "value": (i * 7919) % 100003},
            separators=(",", ":"),
        ) + "\n"
        for i in range(2500)
    ).encode("utf-8")
    source_data = (
        '"""Synthetic repetitive source-code fixture for compression tests."""\n'
        + "".join(
            f"def transform_{i}(value):\n"
            f"    # Normalize the input and preserve deterministic behavior.\n"
            f"    normalized = str(value).strip().lower()\n"
            f"    return normalized + '-group-{i % 16}'\n\n"
            for i in range(600)
        )
    ).encode("utf-8")
    repeated_data = (("backup snapshot chunk hash restore manifest metadata " * 1800) + "\n").encode("utf-8")
    rng = random.Random(seed)
    random_data = bytes(rng.getrandbits(8) for _ in range(256 * 1024))
    compressed_data = zlib.compress(random_data, level=9)

    payloads = {
        "events.log": log_data,
        "records.csv": csv_data,
        "records.json": json_data,
        "records.jsonl": jsonl_data,
        "source_fixture.py": source_data,
        "repeated.txt": repeated_data,
        "repeated-copy.txt": repeated_data,
        "random.bin": random_data,
        "compressed-like.zlib": compressed_data,
    }
    paths = []
    for name, data in payloads.items():
        path = root / name
        path.write_bytes(data)
        paths.append(path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="new or empty output directory for synthetic files")
    parser.add_argument("--seed", type=int, default=2026, help="deterministic random seed")
    args = parser.parse_args(argv)
    try:
        paths = generate_corpus(args.target, seed=args.seed)
    except (OSError, ValueError) as exc:
        print(f"content corpus generation failed: {exc}", file=sys.stderr)
        return 2
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
