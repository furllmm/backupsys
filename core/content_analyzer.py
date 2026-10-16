"""Read-only, bounded content profiling for BackupSys."""
from __future__ import annotations

import codecs
import csv
import io
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

DEFAULT_SAMPLE_SIZE = 64 * 1024
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"),
    (b"\x1f\x8b", "gzip"), (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"),
    (b"%PDF-", "pdf"), (b"SQLite format 3\x00", "sqlite"),
    (b"\x00asm", "wasm"), (b"\x7fELF", "elf"), (b"MZ", "pe-executable"),
)


@dataclass(frozen=True)
class ContentProfile:
    kind: str
    format_hint: str
    sample_bytes: int
    file_size: Optional[int]
    text_like: bool
    encoding: Optional[str]
    entropy_bits_per_byte: Optional[float]
    zero_byte_ratio: float
    printable_ratio: float
    recommendation: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


def _magic_format(data: bytes) -> Optional[str]:
    for signature, name in _MAGIC:
        if data.startswith(signature):
            return name
    if data.startswith(b"RIFF") and len(data) >= 12:
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(data[8:12], "riff-container")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return "heif"
        return "iso-bmff-media"
    return None


def _decode_text(data: bytes) -> tuple[Optional[str], Optional[str]]:
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            return None, None
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            try:
                return data.decode(encoding), encoding
            except UnicodeDecodeError:
                return None, None
    if b"\x00" in data:
        return None, None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if data:
            controls = {9, 10, 12, 13}
            ratio = sum(b >= 32 or b in controls for b in data) / len(data)
            if ratio >= 0.92:
                return data.decode("latin-1"), "latin-1"
    return None, None


def _entropy(data: bytes) -> Optional[float]:
    if len(data) < 32:
        return None
    counts = Counter(data)
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _text_format(text: str) -> str:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return "text"
    try:
        json.loads(stripped)
        return "json"
    except (json.JSONDecodeError, RecursionError):
        pass
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 2:
        checked = lines[:20]
        matched = 0
        for line in checked:
            try:
                value = json.loads(line)
                matched += isinstance(value, (dict, list))
            except (json.JSONDecodeError, RecursionError):
                pass
        if matched / len(checked) >= 0.8:
            return "jsonl"
    if len(lines) >= 3:
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines[:20]), newline="")))
            widths = [len(row) for row in rows if row]
            if len(widths) >= 3 and min(widths) >= 2 and len(set(widths)) == 1:
                return "delimited-text"
        except csv.Error:
            pass
    return "text"


def analyze_bytes(data: bytes, file_size: Optional[int] = None) -> ContentProfile:
    """Profile a byte sample. Filename extensions are never consulted."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if file_size is not None and file_size < 0:
        raise ValueError("file_size must be non-negative")
    if file_size is not None and len(data) > file_size:
        raise ValueError("sample cannot be larger than file_size")

    n = len(data)
    size = n if file_size is None else file_size
    zeros = data.count(b"\x00") / n if n else 0.0
    controls = {9, 10, 12, 13}
    printable = sum(b >= 32 or b in controls for b in data) / n if n else 1.0
    entropy = _entropy(data)
    magic = _magic_format(data)
    reasons: list[str] = []

    if not data:
        kind, hint, text_like, encoding, recommendation = "empty", "empty", True, "utf-8", "no-content"
        reasons.append("empty file; no content bytes to transform")
    elif magic:
        kind, hint, text_like, encoding = "known-binary", magic, False, None
        compressed = {"zip", "gzip", "zstd", "bzip2", "xz", "7z", "rar", "png",
                      "jpeg", "webp", "avif", "heif", "gif", "pdf", "iso-bmff-media"}
        recommendation = "avoid-expensive-pattern-analysis" if magic in compressed else "standard-chunking"
        reasons.append("recognized content signature: " + magic)
        if magic in compressed:
            reasons.append("format is commonly compressed or internally encoded")
    else:
        decoded, encoding = _decode_text(data)
        text_like = decoded is not None
        if text_like:
            kind, hint = "text", _text_format(decoded)
            recommendation = "text-pattern-analysis-candidate" if n >= 1024 and printable >= 0.9 else "standard-chunking"
            reasons.append("sample decodes as " + str(encoding))
            if hint != "text":
                reasons.append("content resembles " + hint)
        else:
            kind, hint, recommendation = "binary", "unknown-binary", "standard-chunking"
            reasons.append("no recognized signature and sample is not safely text-decodable")

    if entropy is not None:
        reasons.append("sample entropy: %.2f bits/byte" % entropy)
        if not magic and not text_like and entropy >= 7.7:
            recommendation = "avoid-expensive-pattern-analysis"
            reasons.append("high sampled entropy makes generic pattern search less promising")

    return ContentProfile(kind, hint, n, size, text_like, encoding,
                          round(entropy, 4) if entropy is not None else None,
                          round(zeros, 6), round(printable, 6), recommendation, tuple(reasons))


def analyze_file(path: str | Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> ContentProfile:
    """Read at most sample_size bytes from a regular file and profile them."""
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    file_path = Path(path)
    stat = file_path.stat()
    if not file_path.is_file():
        raise ValueError("not a regular file: " + str(file_path))
    with file_path.open("rb") as stream:
        sample = stream.read(sample_size)
    return analyze_bytes(sample, file_size=stat.st_size)
