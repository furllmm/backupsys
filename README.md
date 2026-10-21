# backupsys

A cross-platform (Windows + Linux) backup operating layer: one deduplicated,
content-addressable snapshot engine underneath file backup, AppData/config
backup, full-OS backup, and Android device backup, driven from either a CLI
or a PySide6 GUI wizard.

## Why "operating layer" and not "a backup tool"

Every backup type — a Documents folder, a Discord AppData folder, `/etc`,
an Android DCIM pull — becomes the same thing on disk: a **snapshot**
referencing content-addressed **chunks**. There's exactly one storage
engine, one dedup mechanism, one restore engine, one GC. Adding a new
backup *source* (say, a new "smart source") never requires touching
storage, snapshot, or restore code.

## Architecture

```
core/
  pathresolver.py   portable logical paths (%APPDATA%, $HOME, ...)
  content_analyzer.py read-only content profiling; signature/text/entropy hints (phase 1)\n  chunker.py         fixed-size chunking + SHA-256 (simple/legacy path)
  storage.py          classic per-file chunk store (storage/ab/cd/<hash>) -- kept for compatibility
  packed_store.py    high-performance engine: chunks.dat + SQLite index, LRU cache, sequential I/O
  store_factory.py   opens the right engine for a store root (fixed per-root via engine.json)
  changedetect.py    size/mtime (+ optional xxhash) fast-skip for unchanged files
  snapshot.py         the unifying snapshot model + smart-chunking incremental builder (cancellable)
  restore.py           responsive, independent, parallel restore engine (pause/resume/cancel/resume-from-crash)
  progress.py          byte-accurate progress, speed/ETA tracking, pause/cancel primitives
  resume.py             append-only log of completed files, for resuming an interrupted restore
  gc.py                 mark-and-sweep garbage collection + compaction, low-priority background mode
  benchmark.py        synthetic backup/restore performance benchmark
  settings.py           global preferences (duplicate-source protection, chunk size, threshold, compression)
  source_index.py     fast normalized index of every source across all plans

plans/manager.py     MSP360-style plan CRUD (each plan is a standalone JSON doc, storage_mode override)
os_backup/           Windows (profile + registry export) / Linux (/etc, $HOME) sources
                     snapshot.py: instant btrfs CoW snapshots via pybtrfs (timeshift-style),
                     used to freeze subvolume sources at a point-in-time for consistent backups
                     btrfs_stream.py / btrfs_replicator.py: incremental btrfs send/receive
                     replication to any destination (cloud/local), split into resumeable parts
android/              adb.py (primary) + mtp.py (fallback) device backup
destinations/        pluggable remote backends (local/USB/SMB/NFS, SFTP, WebDAV, S3-compatible)
                        + repository.py (compress/encrypt/resume/verify) + queue.py (offline retry)
gui/                    PySide6 wizard, main window, and a live restore/backup progress dialog
cli.py                   command-line interface
```

### Design invariants

- **Storage is incremental** — only new/changed chunks are ever written;
  everything else is deduplicated by SHA-256 content hash.
- **Restore is full and independent** — every snapshot carries the complete
  chunk-hash list for every file it references. Restoring `snapshot_003`
  never needs `snapshot_001` or `snapshot_002` to exist. The
  `parent_snapshot_id` field is used *only* to accelerate change detection
  at backup time; it is never read during restore.
- **Paths are portable** — snapshots store logical paths like
  `%APPDATA%/Discord/settings.json`, resolved to a concrete filesystem path
  only at the moment of backup or restore, on whichever OS/user is running.
- **Plans never share state** — creating a plan always starts from an empty
  `sources`/`excluded` list; mutating one plan's JSON document never
  touches another's.
- **The GUI never blocks.** Backup and restore run on background threads;
  the main window only reacts to signals (progress, log lines, completion).
  Restore progress is byte-accurate (not just a file counter), Pause/Cancel
  are checked between chunks *within* a file (not just between files) so a
  multi-gigabyte file never delays a Cancel, and cancelling never corrupts
  a destination file — it either finishes completely or doesn't exist.
  Every completed file is durably logged, so an interrupted restore resumes
  from the last completed file instead of restarting from zero.

### Content intelligence (phase 1, experimental)

BackupSys includes a read-only, bounded-sample content profiler that identifies
common file signatures and text-like content without trusting file extensions.
It emits heuristic recommendations only; it does not transform stored bytes or
change snapshot/restore behavior yet. See [docs/CONTENT_INTELLIGENCE.md](docs/CONTENT_INTELLIGENCE.md).

### Duplicate-source protection (optional)

Off by default. Enable with:

```bash
python cli.py settings set prevent-duplicate-sources true
```

Once enabled, adding a source already used by another plan raises a clear
error naming the conflicting plan(s) unless you pass `--allow-duplicate`.
Detection normalizes paths (case, slashes, `%APPDATA%` vs. its resolved
absolute form) and also flags ancestor/descendant overlaps, e.g. adding
`%APPDATA%/Discord` when another plan already backs up all of `%APPDATA%`.

### High-performance storage engine

By default, every new store uses the **packed engine**
(`core/packed_store.py`): a single append-only `chunks.dat` holding every
chunk back-to-back, plus a `chunk_index.db` SQLite database mapping
`hash -> offset/length`. This replaces "one file per chunk" (which becomes
the bottleneck at scale -- millions of small files means millions of slow
filesystem operations) with sequential, indexed storage, prioritized in
this order: **restore speed > backup speed > low CPU > low disk I/O >
dedup efficiency**. Concretely:

- **Smart chunking**: files under 64MB (configurable) are stored as a
  single whole-file object -- no chunking overhead at all for the vast
  majority of typical files. Only larger files are split, using a bigger
  default chunk size (4MB, configurable: 1/2/4/8/16MB) than a naive
  fixed-size chunker would use, trading a little dedup granularity for far
  fewer hash/index/compression operations.
- **Parallel hash/compress**: large files' chunks are hashed and
  zstd-compressed across a process pool (scaling with CPU cores) while a
  single writer appends them sequentially, keeping each file's chunks
  contiguous on disk.
- **Sequential, prefetched restore**: the restore engine sorts every
  chunk it needs by physical offset and reads them in one forward pass
  (via a background prefetch thread into an LRU cache) instead of
  following per-file logical order, which would mean scattered reads.
- **Compaction, not free-list bookkeeping**: GC drops index rows for
  unreferenced chunks immediately, then (if any were deleted) rewrites
  `chunks.dat` in one pass containing only live chunks -- reclaiming space
  and restoring perfect sequential layout at once.

A per-plan `storage_mode` (`auto` default / `file` / `chunk`) can override
the size-based decision for an entire plan:

```bash
python cli.py plan create "Media" --source '$HOME/Videos' --storage-mode file
python cli.py settings set chunk-size-mb 8
python cli.py settings set whole-file-threshold-mb 128
```

The classic one-file-per-chunk engine (`core/storage.py`) is still
available (`--engine classic` on a brand-new store) for compatibility/
testing, but a store's engine is fixed at creation time and can't be
silently switched later.

Run `python cli.py benchmark --size-mb 500` for a synthetic backup/restore
performance report (speed, CPU, RAM, dedup ratio, repository growth).

### Storage destinations (optional, pluggable)

A plan can push its snapshots to one or more destinations in addition to
the local store: a local folder (which also covers USB drives and any
already-mounted SMB/NFS share or Syncthing folder), SFTP, WebDAV (generic —
works with Nextcloud, ownCloud, Seafile, or any other WebDAV-compliant
service), or S3-compatible object storage (AWS S3, MinIO, or any other
S3-compatible self-hosted service). See `destinations/` and the `destination
create/push/verify/sync-queue` CLI commands. Encryption passphrases are
never persisted anywhere — see `destinations/crypto.py`.

## Quick start

```bash
pip install -r requirements.txt   # PySide6/zstandard/xxhash are all optional
                                    # except PySide6, needed only for the GUI

# Create a plan
python cli.py plan create "Documents" --source '$HOME/Documents'

# Back it up
python cli.py backup plan_001

# List plans and snapshots
python cli.py list all
python cli.py list snapshots --plan plan_001   # just one plan's snapshots

# Inspect or delete a specific (not necessarily latest) snapshot
python cli.py snapshot show snapshot_XXXXXXXX_XXXXXX_XXXXXXXX
python cli.py snapshot delete snapshot_XXXXXXXX_XXXXXX_XXXXXXXX   # then run gc to reclaim space

# Restore a specific snapshot somewhere safe
python cli.py restore snapshot_XXXXXXXX_XXXXXX_XXXXXXXX --target /tmp/restore_test

# Reclaim space from chunks no snapshot references anymore
python cli.py gc

# Full-OS snapshot (registry export is Windows-only, opt-in)
python cli.py os-backup --registry

# Android device backup (requires `adb` on PATH + USB debugging authorized)
python cli.py android-backup

# Optional: block adding a source already used by another plan
python cli.py settings set prevent-duplicate-sources true

# Add a destination and push a snapshot to it
python cli.py destination create "NAS" --kind sftp \
    --param host=nas.local --param remote_root=/backups --param username=me
python cli.py destination test dest_001
python cli.py destination push snapshot_XXXXXXXX_XXXXXX_XXXXXXXX --destination dest_001
python cli.py destination verify dest_001
python cli.py destination sync-queue   # retry anything that failed while offline

# GUI (run from the project root)
python run_gui.py
# or equivalently:
python -m gui.main
# or double-click / run the launcher for your OS:
#   Windows:      run_gui.bat
#   Linux/macOS:  ./run_gui.sh   (chmod +x run_gui.sh once, first time)
```

> **Note:** don't run `python gui/main.py` from inside the `gui/` folder --
> that skips Python's package initialization and raises
> `ImportError: attempted relative import with no known parent package`.
> Always launch the GUI from the project root via `run_gui.py` (or the
> `.bat`/`.sh` launcher, or `-m gui.main`).
>
> If `run_gui.py` fails with `ModuleNotFoundError: No module named
> 'gui.main'`, it will now print a diagnosis: either this project's
> `gui/main.py` is missing (an incomplete copy/extraction -- re-download
> and make sure the whole `gui/` folder sits next to `run_gui.py`), or a
> *different* `gui` package elsewhere on your system/PYTHONPATH is
> shadowing this project's own `gui` package.

Every command accepts `--store <path>` to point at a different backup
store root (default: a `backups/` folder next to `cli.py`/`run_gui.py`,
see `core/config.py` — override with the `BACKUPSYS_STORE_ROOT` environment
variable or `--store`) — useful for pointing at an external
drive.

## Testing

```bash
python -m unittest discover -s tests -v
```

The test suite (69 tests across 10 files) exercises: chunk-level dedup,
atomic-write integrity verification, plan isolation ("clean slate"
requirement), a full backup → incremental-reuse → change → restore round
trip, GC reclaiming only truly-unreferenced chunks (plus compaction on the
packed engine), duplicate-source detection (including ancestor/descendant
overlap), the destination/repository layer (local round trip, encryption
round trip + tamper detection, bandwidth limiting, mocked SFTP/WebDAV/S3
calls), the responsive restore engine (byte-accurate progress,
cancel-mid-file leaves no partial files, resume-from-last-completed-file,
pause blocks progress, one failed file never aborts the rest), the packed
storage engine (sequential layout, corruption detection for both
single-shot and streamed compression, LRU cache, offset-sorted sequential
reads, prefetch, streaming whole-file objects), smart chunking end-to-end
(whole-file vs. chunked threshold behavior, storage_mode overrides,
parallel vs. sequential hash/compress producing identical results), and
the benchmark tool.

## Experimental Content Intelligence

The `feature/content-intelligence-v1` branch includes a read-only content
profiler and two explicit-path reporting tools. They are experimental and do
not change snapshot creation, stored bytes, or restore behavior.

```bash
python -m tools.content_corpus ./tmp/content-corpus
python -m tools.content_report --format json ./tmp/content-corpus/*
python -m tools.content_report --format csv ./tmp/content-corpus/*
python -m tools.content_compression_probe ./tmp/content-corpus/*
python -m tools.content_experiment ./tmp/content-corpus/*
```

The compression probe and combined experiment require the Python `zstandard`
package. They estimate compressed payload size after SHA-256 object deduplication;
the combined report places those estimates beside content-profile hints. Both
exclude store metadata and must be checked against the real backup/restore benchmark. Details:
[`docs/CONTENT_INTELLIGENCE.md`](docs/CONTENT_INTELLIGENCE.md).

## Btrfs instant snapshots & replication

Timeshift has no Python package (it is a shell/GTK tool wrapping rsync or
the `btrfs` CLI), so backupsys gets the same capability from Python:

- **Instant snapshots** (`os_backup/snapshot.py`) via `pybtrfs` (native
  bindings, no subprocess) with a `btrfs` CLI fallback. The backup engine
  automatically freezes btrfs-subvolume sources at a point-in-time before
  walking them, so file backups read a consistent view even while files
  change — the frozen snapshot is deleted afterwards.
- **Incremental cloud/local replication** (`os_backup/btrfs_stream.py`,
  `os_backup/btrfs_replicator.py`) built on `btrfs send`/`receive`: after a
  full first upload, each push transfers only the changed extents. Streams
  are split into 16 MiB parts and stored as ordinary destination objects
  (works on every backend), with SHA-256 integrity verification and optional
  per-part encryption. Requires the `btrfs-progs` CLI.

```python
from destinations.manager import build_destination    # or your own Destination
from os_backup.btrfs_replicator import BtrfsReplicator

replicator = BtrfsReplicator(build_destination(config), "/mnt/btrfs-snaps", "/var/tmp/bs")
replicator.push("/home")            # instant snapshot + incremental send
replicator.pull("/mnt/restore")     # receive full base + incrementals in order
replicator.prune_remote("default", keep=5)
```

## What this deliberately does *not* do

- No raw block-level disk imaging (no `dd`-style cloning). OS backup is a
  config/user-data snapshot (`/etc`, `$HOME`, `%APPDATA%`, registry export),
  which is what's actually restorable independently and portable across a
  reinstall — full disk imaging is a different problem with different
  tooling (Clonezilla, Macrium, etc.) and out of scope here.
- No custom MTP/USB protocol implementation — Android access goes through
  the real `adb` binary (primary) or the OS's own MTP mount (Linux gvfs
  fallback); Windows MTP without ADB is explicitly unsupported with a clear
  error rather than a fake implementation.
- No scheduler daemon. `schedule: daily/weekly` is stored on the plan for a
  future OS-level scheduler (Task Scheduler / cron / systemd timer) to read
  and invoke `cli.py backup <plan_id>`; wiring that up is a deployment
  concern, not something to fake inside this engine.
