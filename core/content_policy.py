"""Stable policy boundary for the experimental Content Intelligence Layer.

The policy only chooses analysis depth. It never changes bytes or storage
layout, so callers can adopt it without affecting backup/restore compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .content_analyzer import ContentProfile, analyze_file


@dataclass(frozen=True)
class ContentDecision:
    path: str
    profile: ContentProfile
    analysis_level: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "profile": self.profile.to_dict(),
            "analysis_level": self.analysis_level,
            "reason": self.reason,
        }


def decide_analysis(path: str | Path, sample_size: int = 64 * 1024) -> ContentDecision:
    """Return a conservative analysis-depth decision for one regular file."""
    file_path = Path(path)
    profile = analyze_file(file_path, sample_size=sample_size)

    if profile.kind == "empty":
        level, reason = "skip", "empty files have no content to analyze"
    elif profile.recommendation == "avoid-expensive-pattern-analysis":
        level, reason = "shallow", "sample suggests generic pattern analysis may not pay off"
    elif profile.recommendation == "text-pattern-analysis-candidate":
        level, reason = "candidate", "text-like sample may benefit from measured text-specific analysis"
    else:
        level, reason = "standard", "no evidence justifies deeper generic analysis"

    return ContentDecision(
        path=str(file_path),
        profile=profile,
        analysis_level=level,
        reason=reason,
    )


def analyze_paths(paths: Iterable[str | Path], sample_size: int = 64 * 1024) -> list[ContentDecision]:
    """Analyze paths in input order; errors are surfaced rather than hidden."""
    return [decide_analysis(path, sample_size=sample_size) for path in paths]
