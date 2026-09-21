"""
destinations.raid
=================

RAID-like redundancy for multi-destination backup plans.

Mirror (RAID 1)   – every chunk goes to every destination.
Striping (RAID 0) – each chunk goes to exactly one destination,
                    determined by its SHA-256 hash.
Parity (RAID 5)   – like striping but one parity chunk per stripe so any
                    single destination can fail without data loss.

Chunk placement for striping / parity is *deterministic* so no per-chunk
mapping metadata is needed on the destination.  For parity mode the parity
chunk hashes themselves are stored in the snapshot's ``raid_meta`` field.
The snapshot manifest is always written to **all** destinations so it can
be discovered from any single one of them.
"""

from __future__ import annotations

import functools
import hashlib
import json
from typing import Callable, Dict, List, Optional, Tuple

from core.chunker import hash_bytes
from core.snapshot import Snapshot
from core.storage import ChunkStore

from .base import Destination, DestinationNotFoundError
from .repository import Repository, RepositoryError, RepositoryVerificationError


# ---------------------------------------------------------------------------
# Deterministic destination index for a chunk hash
# ---------------------------------------------------------------------------
def _dest_index(chunk_hash: str, n: int) -> int:
    return int(hashlib.sha256(chunk_hash.encode()).hexdigest(), 16) % n


def _dest_label(repo: Repository) -> str:
    """Human-readable label for a repository's destination."""
    name = getattr(repo.destination, "name", None)
    if name:
        return name
    return f"{repo.destination.kind}#{id(repo.destination) % 10000}"


# ---------------------------------------------------------------------------
# XOR helpers
# ---------------------------------------------------------------------------
def _xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _parity_of(chunks: List[bytes]) -> bytes:
    if not chunks:
        return b""
    return functools.reduce(_xor_bytes, chunks[1:], chunks[0])


# ---------------------------------------------------------------------------
# Write manifest to all repositories (shared helper)
# ---------------------------------------------------------------------------
def _write_manifest(snapshot: Snapshot, repositories: List[Repository]) -> None:
    manifest = json.dumps(snapshot.to_dict(), indent=2).encode("utf-8")
    for repo in repositories:
        with repo.destination:
            encoded = repo._encode(manifest)
            repo.destination.put_object(f"snapshots/{snapshot.id}.json", encoded)


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------
def _push_mirror(
    store: ChunkStore,
    snapshot: Snapshot,
    repos: List[Repository],
    progress_cb: Optional[Callable] = None,
) -> List[Tuple[str, int, int]]:
    """Push every chunk + manifest to EVERY repository (RAID 1)."""
    results = []
    for repo in repos:
        uploaded, skipped = repo.push_snapshot(store, snapshot, progress_cb=progress_cb)
        results.append((_dest_label(repo), uploaded, skipped))
    return results


def _push_striping(
    store: ChunkStore,
    snapshot: Snapshot,
    repos: List[Repository],
    progress_cb: Optional[Callable] = None,
) -> List[Tuple[str, int, int]]:
    """Distribute chunks across destinations by hash (RAID 0)."""
    n = len(repos)
    all_hashes = sorted({h for f in snapshot.files for h in f.chunks})

    assignments: Dict[int, List[str]] = {i: [] for i in range(n)}
    for h in all_hashes:
        assignments[_dest_index(h, n)].append(h)

    results = []
    for idx, repo in enumerate(repos):
        hashes = assignments[idx]
        uploaded = skipped = 0
        with repo.destination:
            for i, chunk_hash in enumerate(hashes):
                if repo.push_chunk(store, chunk_hash):
                    uploaded += 1
                else:
                    skipped += 1
                if progress_cb:
                    progress_cb(chunk_hash, i + 1, len(hashes))
            encoded = repo._encode(json.dumps(snapshot.to_dict(), indent=2).encode("utf-8"))
            repo.destination.put_object(f"snapshots/{snapshot.id}.json", encoded)
        results.append((_dest_label(repo), uploaded, skipped))
    return results


def _push_parity(
    store: ChunkStore,
    snapshot: Snapshot,
    repos: List[Repository],
    progress_cb: Optional[Callable] = None,
) -> List[Tuple[str, int, int]]:
    """RAID 5 – stripe across N-1 destinations, parity on the Nth."""
    n = len(repos)
    if n < 3:
        raise ValueError(f"Parity (RAID 5) needs >= 3 destinations, got {n}")

    all_hashes = sorted({h for f in snapshot.files for h in f.chunks})
    stripe_width = n - 1
    total_ops = len(all_hashes) + (len(all_hashes) + stripe_width - 1) // stripe_width
    ops_done = 0

    assignments: Dict[int, List[str]] = {i: [] for i in range(n)}
    parity_stripes: List[Dict] = []
    temp_parity_hashes: List[str] = []

    # Process stripes
    for start in range(0, len(all_hashes), stripe_width):
        data = all_hashes[start:start + stripe_width]

        data_bytes = []
        for h in data:
            raw = store.get_chunk(h, verify=False)
            data_bytes.append(raw)
            ops_done += 1
            if progress_cb:
                progress_cb(h, ops_done, total_ops)

        if not data_bytes:
            continue

        max_len = max(len(b) for b in data_bytes)
        padded = [b.ljust(max_len, b'\x00') for b in data_bytes]
        parity_raw = _parity_of(padded)
        parity_hash = hash_bytes(parity_raw)

        # Temporarily hold parity in local store
        store.put_chunk(parity_hash, parity_raw)
        temp_parity_hashes.append(parity_hash)

        parity_stripes.append({
            "parity_hash": parity_hash,
            "count": len(data),
            "sizes": [len(b) for b in data_bytes],
            "destinations": [j % (n - 1) for j in range(len(data))],
        })

        # Distribute data chunks to destinations 0..n-2 (round-robin)
        for j, h in enumerate(data):
            idx = j % (n - 1)
            assignments[idx].append(h)

        # Parity goes to last destination
        assignments[n - 1].append(parity_hash)

    # Update snapshot metadata
    snapshot.raid_meta = {"parity_stripes": parity_stripes}

    # Push chunks
    results = []
    for idx, repo in enumerate(repos):
        hashes = assignments[idx]
        uploaded = skipped = 0
        with repo.destination:
            for chunk_hash in hashes:
                if repo.push_chunk(store, chunk_hash):
                    uploaded += 1
                else:
                    skipped += 1
            # Reload snapshot with updated raid_meta for the manifest
            manifest = json.dumps(snapshot.to_dict(), indent=2).encode("utf-8")
            encoded = repo._encode(manifest)
            repo.destination.put_object(f"snapshots/{snapshot.id}.json", encoded)
        results.append((_dest_label(repo), uploaded, skipped))

    # Clean up parity temp chunks from local store
    for h in temp_parity_hashes:
        try:
            store.delete_chunk(h)
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def push_snapshot_raid(
    store: ChunkStore,
    snapshot: Snapshot,
    repositories: List[Repository],
    redundancy_mode: str = "mirror",
    progress_cb: Optional[Callable] = None,
) -> List[Tuple[str, int, int]]:
    """Push a snapshot to all repositories using the given RAID mode.

    Returns a list of ``(destination_name, uploaded, skipped)`` tuples.
    """
    if redundancy_mode == "mirror":
        return _push_mirror(store, snapshot, repositories, progress_cb)
    elif redundancy_mode == "striping":
        return _push_striping(store, snapshot, repositories, progress_cb)
    elif redundancy_mode == "parity":
        return _push_parity(store, snapshot, repositories, progress_cb)
    else:
        raise ValueError(f"Unknown redundancy_mode: {redundancy_mode!r}")


def restore_snapshot_raid(
    store: ChunkStore,
    snapshot_id: str,
    repositories: List[Repository],
    redundancy_mode: str = "mirror",
    failed_destinations: Optional[List[str]] = None,
) -> Snapshot:
    """Fetch a snapshot from RAID-protected destinations into local store.

    *mirror* – tries each destination in order.
    *striping* – fetches each chunk from its designated destination (no
      redundancy; if that destination is gone the chunk is lost).
    *parity* – supports loss of any *single* destination by reconstructing
      missing chunks via XOR from the remaining data + parity.

    Returns the fully materialised :class:`Snapshot`.
    """
    failed = set(failed_destinations or [])
    n = len(repositories)

    # ------------------------------------------------------------------
    # Locate & fetch the snapshot manifest
    # ------------------------------------------------------------------
    snapshot = None
    last_error = None
    for repo in repositories:
        if _dest_label(repo) in failed:
            continue
        try:
            snapshot = repo.fetch_snapshot(snapshot_id)
            break
        except RepositoryError as exc:
            last_error = exc
            continue
    if snapshot is None:
        raise RepositoryError(
            f"Snapshot {snapshot_id} not found on any available destination"
        ) from last_error

    all_hashes = sorted({h for f in snapshot.files for h in f.chunks})

    # ------------------------------------------------------------------
    # Mirror
    # ------------------------------------------------------------------
    if redundancy_mode == "mirror":
        for repo in repositories:
            if _dest_label(repo) in failed:
                continue
            try:
                return repo.restore_snapshot_locally(store, snapshot_id)
            except (RepositoryError, DestinationNotFoundError):
                continue
        raise RepositoryError(
            f"Failed to restore {snapshot_id} from any mirror destination"
        )

    # ------------------------------------------------------------------
    # Striping (RAID 0)
    # ------------------------------------------------------------------
    if redundancy_mode == "striping":
        for h in all_hashes:
            if store.has_chunk(h):
                continue
            idx = _dest_index(h, n)
            repo = repositories[idx]
            if _dest_label(repo) in failed:
                raise RepositoryError(
                    f"Chunk {h[:12]} lives on failed destination "
                    f"{_dest_label(repo)}; striping has no redundancy"
                )
            try:
                repo.fetch_chunk_into(store, h)
            except DestinationNotFoundError as exc:
                raise RepositoryError(
                    f"Chunk {h[:12]} not found on {_dest_label(repo)}: {exc}"
                ) from exc
        return snapshot

    # ------------------------------------------------------------------
    # Parity (RAID 5)
    # ------------------------------------------------------------------
    if redundancy_mode != "parity":
        raise ValueError(f"Unknown redundancy_mode: {redundancy_mode!r}")
    if n < 3:
        raise RepositoryError(
            f"Parity restore needs >= 3 destinations, got {n}"
        )

    failed_indices = {i for i, r in enumerate(repositories) if _dest_label(r) in failed}
    if len(failed_indices) > 1:
        raise RepositoryError(
            f"RAID 5 can survive at most 1 failed destination; "
            f"{len(failed_indices)} are unavailable"
        )

    parity_stripes = (snapshot.raid_meta or {}).get("parity_stripes", [])
    if not parity_stripes:
        raise RepositoryError(
            "Snapshot has no parity stripe metadata (raid_meta missing)"
        )

    stripe_width = n - 1

    for stripe_idx, stripe in enumerate(parity_stripes):
        start = stripe_idx * stripe_width
        data = all_hashes[start:start + stripe_width]
        dests = stripe.get("destinations", [])
        if not dests:
            dests = list(range(len(data)))  # fallback for old metadata

        # Fetch available data chunks
        missing_positions = []
        for pos, h in enumerate(data):
            if store.has_chunk(h):
                continue
            chunk_dest = dests[pos] if pos < len(dests) else (pos % (n - 1))
            if chunk_dest in failed_indices:
                missing_positions.append((pos, h, chunk_dest))
                continue
            try:
                repositories[chunk_dest].fetch_chunk_into(store, h)
            except DestinationNotFoundError as exc:
                missing_positions.append((pos, h, chunk_dest))

        if not missing_positions:
            continue

        if len(missing_positions) > 1:
            raise RepositoryError(
                f"RAID 5 cannot reconstruct {len(missing_positions)} "
                f"missing chunks in stripe {stripe_idx}"
            )

        # Reconstruct ONE missing chunk via XOR
        pos, missing_hash, failed_idx = missing_positions[0]
        original_size = stripe["sizes"][pos] if "sizes" in stripe else 0

        # Fetch parity if not already local
        parity_hash = stripe["parity_hash"]
        parity_dest = n - 1  # always on last destination
        if not store.has_chunk(parity_hash):
            parity_repo = repositories[parity_dest]
            if _dest_label(parity_repo) in failed:
                raise RepositoryError(
                    f"Cannot reconstruct chunk {missing_hash[:12]}: "
                    f"both data destination {_dest_label(repositories[failed_idx])} "
                    f"and parity destination {_dest_label(parity_repo)} are unavailable"
                )
            parity_repo.fetch_chunk_into(store, parity_hash)

        # XOR: parity XOR all available data chunks = missing chunk
        parity_data = store.get_chunk(parity_hash, verify=False)
        for other_pos, h in enumerate(data):
            if other_pos == pos:
                continue
            if store.has_chunk(h):
                chunk_data = store.get_chunk(h, verify=False)
                max_len = max(len(parity_data), len(chunk_data))
                pd = parity_data.ljust(max_len, b'\x00')
                cd = chunk_data.ljust(max_len, b'\x00')
                parity_data = _xor_bytes(pd, cd)

        reconstructed = parity_data[:original_size] if original_size > 0 else parity_data.rstrip(b'\x00')

        actual = hash_bytes(reconstructed)
        if actual != missing_hash:
            raise RepositoryVerificationError(
                f"Reconstructed chunk {missing_hash[:12]} hashes to "
                f"{actual[:12]} – XOR parity mismatch"
            )

        store.put_chunk(missing_hash, reconstructed)

    return snapshot
