"""
core.settings
=============

Global, store-wide preferences -- currently just the duplicate-source
protection toggle, but structured to grow. Persisted as
``<store_root>/settings.json``. Deliberately store-scoped (not a single
global file for the whole machine): "search all existing plans" for
duplicate detection only makes sense within one store's plan set, so
settings live alongside the plans they govern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.storage import ChunkStore


@dataclass
class Settings:
    prevent_duplicate_sources: bool = False

    # --- Storage engine performance tuning ---------------------------------
    # (Which *engine* class is active -- "packed" vs "classic" -- is decided
    # once per store root at creation time; see core.store_factory. These
    # settings tune the packed engine's behavior and are irrelevant to the
    # classic engine.)

    chunk_size_mb: float = 4.0
    """Chunk size used for files at or above whole_file_threshold_mb.
    Larger chunks mean fewer, cheaper hash/index operations at the cost of
    slightly coarser deduplication -- an intentional tradeoff, since
    restore/backup speed outranks dedup efficiency for this engine."""

    whole_file_threshold_mb: float = 64.0
    """Files smaller than this are stored as a single whole-file object
    (one hash, one chunk) instead of being split at all -- avoiding
    chunking overhead entirely for the vast majority of typical files."""

    compression_level: int = 3
    """zstd compression level. Lower = faster, less compression. 3 is
    zstd's own recommended "fast" tier; raise only if CPU time is cheap
    relative to storage/bandwidth in your environment."""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        return cls(**{**cls().to_dict(), **d})


class SettingsManager:
    def __init__(self, store: ChunkStore):
        self.store = store
        self.path = store.root / "settings.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return Settings.from_dict(self.store.read_json(self.path))

    def save(self, settings: Settings) -> None:
        self.store.write_json(self.path, settings.to_dict())

    def update(self, **changes) -> Settings:
        settings = self.load()
        for key, value in changes.items():
            if not hasattr(settings, key):
                raise ValueError(f"Unknown setting: {key!r}")
            setattr(settings, key, value)
        self.save(settings)
        return settings
