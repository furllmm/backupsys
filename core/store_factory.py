"""
core.store_factory
====================

A store root's storage engine (packed vs. classic) is fixed at creation
time and recorded in ``<root>/engine.json`` -- it can never be silently
switched later, since the two engines use incompatible on-disk layouts
(a directory of per-chunk files vs. a single chunks.dat + SQLite index).
:func:`open_store` is the single place that decides which engine class to
instantiate for a given root, so callers (CLI, GUI) never need an
``if/else`` on engine type.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from core.packed_store import PackedChunkStore
from core.storage import ChunkStore

Store = Union[ChunkStore, PackedChunkStore]

DEFAULT_ENGINE = "packed"
_VALID_ENGINES = {"packed", "classic"}


class StoreEngineError(RuntimeError):
    pass


def _engine_marker_path(root: Path) -> Path:
    return root / "engine.json"


def _read_engine_marker(root: Path) -> str | None:
    marker = _engine_marker_path(root)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["engine"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _write_engine_marker(root: Path, engine: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _engine_marker_path(root).write_text(json.dumps({"engine": engine}, indent=2), encoding="utf-8")


def open_store(
    root: Path,
    engine: str = DEFAULT_ENGINE,
    compression_level: int = 3,
) -> Store:
    """Open (or create) the store at ``root``, using whichever engine that
    store was already created with, or ``engine`` if this is a brand new
    root. Raises :class:`StoreEngineError` if ``engine`` is requested but
    doesn't match a pre-existing store's recorded engine, rather than
    silently opening it with the wrong reader.
    """
    if engine not in _VALID_ENGINES:
        raise StoreEngineError(f"Unknown storage engine: {engine!r}")

    root = Path(root)
    existing_engine = _read_engine_marker(root)

    if existing_engine is not None:
        if existing_engine != engine:
            raise StoreEngineError(
                f"Store at {root} was created with the {existing_engine!r} engine "
                f"and cannot be reopened as {engine!r}. Point at a different "
                f"--store path, or omit --engine to use the existing one."
            )
        effective_engine = existing_engine
    else:
        effective_engine = engine
        _write_engine_marker(root, effective_engine)

    if effective_engine == "packed":
        return PackedChunkStore(root, compression_level=compression_level)
    return ChunkStore(root)


def engine_of(root: Path) -> str:
    """Return the engine a store root was created with (defaulting to
    "classic" for pre-existing stores from before this marker file
    existed, since that was the only engine available then)."""
    return _read_engine_marker(Path(root)) or "classic"
