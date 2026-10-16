# BackupSys Content Intelligence Layer — Phase 1

## Status

Phase 1 is a read-only profiler, not a new compression format. It samples
content and returns evidence for later policy decisions. It never rewrites a
source file and is not yet wired into snapshot creation.

## Principles

- Do not trust filename extensions as the source of truth.
- Recognize strong file signatures where possible; signatures are hints, not
  full validation of the file.
- Read only a bounded sample (64 KiB by default).
- Distinguish common already-compressed formats from text-like content.
- Treat recommendations as heuristics, not promises of storage savings.
- Preserve existing chunk hashes, storage layout, snapshots, and restore
  behavior until a reversible transform format and round-trip tests exist.

## API

    from core.content_analyzer import analyze_file
    profile = analyze_file("/path/to/file.with-any-extension")
    print(profile.format_hint, profile.recommendation, profile.reasons)

Recommendations:

- text-pattern-analysis-candidate: candidate for a future measured text/pattern transform.
- avoid-expensive-pattern-analysis: commonly compressed format or high entropy
  suggests generic pattern analysis may cost CPU without useful gains.
- standard-chunking: retain the current storage path.
- no-content: empty file.

## Next phases

1. Benchmark profiler cost and accuracy on representative real-world files.
2. Add opt-in analysis metrics to backup reports without changing stored bytes.
3. Add format adapters behind a stable interface, starting with text/log/JSON
   and a binary structured format selected by benchmarks.
4. Add reversible transforms only when they beat the existing Zstandard plus
   dedupe baseline and restore compatibility is proven.
