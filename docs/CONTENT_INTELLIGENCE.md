# BackupSys Content Intelligence Layer — Experimental

## Status and safety boundary

This branch adds a read-only, bounded content profiler, a conservative analysis
policy, a JSON/CSV report command, and an isolated Zstandard + dedupe payload
probe. These experiments gather evidence; they are not a new compression
format and are not wired into snapshot creation, chunk storage, or restore.

No component in this layer rewrites source files or stored chunks. Existing
chunk hashes, storage layout, snapshots, and restore behavior remain untouched.

## Principles

- Do not trust filename extensions as the source of truth.
- Recognize strong file signatures where possible; signatures are hints, not
  full validation of the file.
- Read only a bounded sample (64 KiB by default) during profiling.
- Distinguish common already-compressed formats from text-like content.
- Treat recommendations as heuristics, not promises of storage savings.
- Measure against the existing backup + compression + dedupe pipeline before
  considering any transform.

## Profiler and policy APIs

    from core.content_analyzer import analyze_file
    profile = analyze_file("/path/to/file.with-any-extension")
    print(profile.format_hint, profile.recommendation, profile.reasons)

    from core.content_policy import decide_analysis
    decision = decide_analysis("/path/to/file")
    print(decision.analysis_level, decision.reason)

Analysis levels are `skip` for empty files, `shallow` when deep generic analysis
is unlikely to pay off, `candidate` for sampled text that may merit specific
experiments, and `standard` when no deeper pass is justified. These are
routing hints only.

## Read-only reports

From the repository root:

    python -m tools.content_report --format json file1 file2
    python -m tools.content_report --format csv --sample-size 32768 file1 file2

Only explicitly supplied files are inspected. JSON includes per-file profiles,
aggregate counts, elapsed time, and sampled-byte totals. CSV is intended for
spreadsheet analysis. Sampling may miss patterns outside the sample.

## Compression + dedupe payload probe

    python -m tools.content_compression_probe file1 file2
    python -m tools.content_compression_probe --chunk-size 4194304 --whole-file-threshold 67108864 file1 file2

This optional probe requires the existing Python `zstandard` package. It uses
SHA-256 on raw objects, compresses each unique object once, and follows the
current whole-file threshold/chunk-size decision for supplied files. It reports
estimated unique compressed payload bytes and deduplication counts without
writing files or a backup repository.

**This is an estimate, not a replacement for the real benchmark.** It excludes
store metadata, indexes, allocation overhead, existing repository contents,
and parallel pipeline effects. The end-to-end benchmark also honours its
compression on/off setting and checks restored paths, sizes, and SHA-256 content.
Validate any conclusion against the real
`core.benchmark` backup/restore cycle, which now verifies restored file paths,
sizes, and SHA-256 content against the generated source dataset before
reporting benchmark metrics.

## Next evidence-driven phases

1. Run unit tests and fix any environment or compatibility issues.
2. Profile representative datasets and verify classification quality/cost.
3. Compare payload estimates with actual end-to-end backup storage, CPU, RAM,
   backup time, and restore time.
4. Prototype reversible, versioned transforms only if measured results justify
   them; require exact round-trip tests and backward-compatible restore before
   integrating into the backup path.
