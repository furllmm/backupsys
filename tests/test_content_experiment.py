from __future__ import annotations

import pytest

from tools import content_experiment


def test_build_experiment_combines_profile_and_probe_rows(tmp_path, monkeypatch):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(content_experiment, "build_report", lambda paths, sample_size: {
        "files": [{
            "path": str(sample),
            "analysis_level": "candidate",
            "profile": {
                "file_size": 5,
                "kind": "text",
                "format_hint": "text",
                "recommendation": "text-pattern-analysis-candidate",
                "entropy_bits_per_byte": 1.9,
            },
        }]
    })
    monkeypatch.setattr(content_experiment, "probe_files", lambda paths, **kwargs: {
        "summary": {
            "logical_bytes": 5,
            "unique_objects": 1,
            "estimated_compressed_unique_object_bytes": 14,
            "estimated_payload_savings_bytes": 0,
            "estimated_logical_to_payload_ratio": 0.3571,
        },
        "files": [{
            "path": str(sample),
            "storage_mode_estimate": "whole-file",
            "objects": 1,
            "duplicate_objects_within_or_across_inputs": 0,
        }],
    })

    report = content_experiment.build_experiment([sample])
    assert report["summary"]["file_count"] == 1
    assert report["files"][0]["format_hint"] == "text"
    assert report["files"][0]["estimated_storage_mode"] == "whole-file"
    assert report["summary"]["estimated_payload_savings_bytes"] == 0
    assert any("exact restore integrity" in note for note in report["limitations"])


def test_build_experiment_rejects_non_file_path(tmp_path):
    with pytest.raises(ValueError, match="not a regular file"):
        content_experiment.build_experiment([tmp_path])
