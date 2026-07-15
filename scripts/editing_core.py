#!/usr/bin/env python3
"""Shared safety primitives for Screen Studio editing.

The editor works with three clocks:

* ``source``: Screen Studio's concatenated recording-session timeline.
* ``edited``: the preview/export timeline produced by the current slices.
* ``audio``: the decoded/concatenated microphone waveform.

Only source-time cuts may reach ``apply_cuts``.  This module makes that
contract explicit and converts reviewed edited-time ranges through the exact
slice map that produced the export.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


CUTS_SCHEMA_VERSION = 2
SUPPORTED_COORDINATE_SPACES = {"source", "edited"}


class CutsValidationError(ValueError):
    """Raised when a cuts file cannot be applied safely."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_cuts_document(
    cuts: list[dict[str, Any]],
    *,
    coordinate_space: str,
    project_json: Path | None = None,
    transcript: Path | None = None,
) -> dict[str, Any]:
    if coordinate_space not in SUPPORTED_COORDINATE_SPACES:
        raise CutsValidationError(f"unsupported coordinate space: {coordinate_space}")
    return {
        "schema_version": CUTS_SCHEMA_VERSION,
        "coordinate_space": coordinate_space,
        "project_sha256": file_sha256(project_json) if project_json else None,
        "project_json": str(project_json) if project_json else None,
        "transcript": str(transcript) if transcript else None,
        "cuts": cuts,
    }


def _validated_cut(raw: dict[str, Any], index: int) -> dict[str, Any]:
    try:
        start_ms = float(raw["start_ms"])
        end_ms = float(raw["end_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CutsValidationError(f"cut {index} has invalid start_ms/end_ms") from exc
    if not (math.isfinite(start_ms) and math.isfinite(end_ms)):
        raise CutsValidationError(f"cut {index} has a non-finite boundary")
    if start_ms < 0 or end_ms <= start_ms:
        raise CutsValidationError(
            f"cut {index} has an invalid range: {start_ms:.1f} -> {end_ms:.1f}"
        )
    return {**raw, "start_ms": start_ms, "end_ms": end_ms}


def timeline_map_from_slices(slices: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Return a piecewise edited↔source map for Screen Studio slices."""
    mapping: list[dict[str, float]] = []
    edited_cursor = 0.0
    for index, sl in enumerate(slices):
        try:
            source_start = float(sl["sourceStartMs"])
            source_end = float(sl["sourceEndMs"])
            time_scale = float(sl.get("timeScale", 1.0) or 1.0)
        except (KeyError, TypeError, ValueError) as exc:
            raise CutsValidationError(f"slice {index} has invalid timing fields") from exc
        if source_end <= source_start or time_scale <= 0:
            raise CutsValidationError(f"slice {index} has a non-positive duration/timeScale")
        edited_duration = (source_end - source_start) / time_scale
        mapping.append({
            "edited_start_ms": edited_cursor,
            "edited_end_ms": edited_cursor + edited_duration,
            "source_start_ms": source_start,
            "source_end_ms": source_end,
            "time_scale": time_scale,
        })
        edited_cursor += edited_duration
    return mapping


def edited_cut_to_source(
    cut: dict[str, Any],
    slices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map one edited-time cut to one or more source-time ranges.

    A reviewed range can cross an existing jump cut.  In that case it must be
    split: treating it as one continuous source range would re-delete material
    that is not present in the exported video.
    """
    start_ms = float(cut["start_ms"])
    end_ms = float(cut["end_ms"])
    pieces: list[dict[str, Any]] = []
    for piece_index, row in enumerate(timeline_map_from_slices(slices), start=1):
        overlap_start = max(start_ms, row["edited_start_ms"])
        overlap_end = min(end_ms, row["edited_end_ms"])
        if overlap_end <= overlap_start:
            continue
        source_start = row["source_start_ms"] + (
            overlap_start - row["edited_start_ms"]
        ) * row["time_scale"]
        source_end = row["source_start_ms"] + (
            overlap_end - row["edited_start_ms"]
        ) * row["time_scale"]
        pieces.append({
            **cut,
            "start_ms": source_start,
            "end_ms": source_end,
            "mapped_from": "edited",
            "mapped_piece": piece_index,
            "_edited_covered_ms": overlap_end - overlap_start,
        })

    covered_ms = sum(float(piece["_edited_covered_ms"]) for piece in pieces)
    requested_ms = end_ms - start_ms
    if not pieces or abs(covered_ms - requested_ms) > 2.0:
        raise CutsValidationError(
            f"edited cut {start_ms:.1f}->{end_ms:.1f}ms falls outside the current timeline"
        )
    for piece in pieces:
        piece.pop("_edited_covered_ms", None)
    return pieces


def load_cuts_document(
    path: Path,
    *,
    current_slices: list[dict[str, Any]],
    current_project_sha256: str,
    accepted_source_project_sha256s: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load, validate, and normalize a cuts document to source time.

    Legacy list-only files remain supported as source-time cuts, but callers
    should surface the returned ``legacy`` flag so users know the file lacks a
    project/timebase fingerprint.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    legacy = isinstance(data, list)
    if legacy:
        document = {
            "schema_version": 1,
            "coordinate_space": "source",
            "project_sha256": None,
            "cuts": data,
            "legacy": True,
        }
    elif isinstance(data, dict) and isinstance(data.get("cuts"), list):
        document = dict(data)
        document["legacy"] = False
    else:
        raise CutsValidationError("cuts JSON must be a list or an object containing a cuts list")

    coordinate_space = str(document.get("coordinate_space") or "source")
    if coordinate_space not in SUPPORTED_COORDINATE_SPACES:
        raise CutsValidationError(f"unsupported coordinate_space: {coordinate_space}")

    project_sha = document.get("project_sha256")
    if coordinate_space == "edited":
        if not project_sha:
            raise CutsValidationError(
                "edited-time cuts require project_sha256; regenerate them with --project-json"
            )
        if project_sha != current_project_sha256:
            raise CutsValidationError(
                "edited-time cuts were generated from a different project.json; "
                "their timestamps cannot be mapped safely"
            )
    elif project_sha:
        accepted = {current_project_sha256, *accepted_source_project_sha256s}
        if project_sha not in accepted:
            raise CutsValidationError(
                "source-time cuts were generated from a different Screen Studio project"
            )

    validated = [
        _validated_cut(raw, index)
        for index, raw in enumerate(document["cuts"], start=1)
        if isinstance(raw, dict)
    ]
    normalized: list[dict[str, Any]] = []
    for cut in validated:
        if coordinate_space == "edited":
            normalized.extend(edited_cut_to_source(cut, current_slices))
        else:
            normalized.append(cut)
    return sorted(normalized, key=lambda item: (item["start_ms"], item["end_ms"])), document


def merge_intervals(intervals: Iterable[tuple[float, float]], gap_ms: float = 0.0) -> list[tuple[float, float]]:
    rows = sorted((float(start), float(end)) for start, end in intervals if end > start)
    if not rows:
        return []
    merged = [rows[0]]
    for start, end in rows[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + gap_ms:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def protect_cuts_with_activity(
    cuts: list[dict[str, Any]],
    activity_intervals: list[tuple[float, float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject whole automatic cuts that overlap a protected screen action."""
    if not cuts or not activity_intervals:
        return cuts, []
    protected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cut in cuts:
        overlap = next((
            (start, end)
            for start, end in activity_intervals
            if end > cut["start_ms"] and start < cut["end_ms"]
        ), None)
        if overlap is None:
            protected.append(cut)
        else:
            rejected.append({**cut, "activity_overlap_ms": overlap})
    return protected, rejected


def protect_reviewed_cuts_with_activity(
    cuts: list[dict[str, Any]],
    input_intervals: list[tuple[float, float]],
    visual_intervals: list[tuple[float, float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Protect activity unless a multimodal reviewer explicitly clears it.

    A multimodal reviewer can distinguish a harmless cursor blink or redundant
    UI animation from a unique tutorial action, while a coarse scene detector
    cannot. Clicks and keystrokes remain hard blockers. Visual-only overlap is
    bypassed only when the reviewed cut explicitly says the screen action was
    ``none`` or ``redundant``. Input is stricter: ``none`` conflicts with the
    event log and stays protected, while ``redundant`` plus a written visual
    assessment can clear setup/navigation that the final tutorial does not need.
    """
    input_cleared: list[dict[str, Any]] = []
    input_guarded: list[dict[str, Any]] = []
    for cut in cuts:
        redundant = str(cut.get("screen_action") or "").strip().lower() == "redundant"
        assessed = bool(str(cut.get("visual_assessment") or "").strip())
        (input_cleared if redundant and assessed else input_guarded).append(cut)
    input_safe, input_rejected = protect_cuts_with_activity(input_guarded, input_intervals)
    input_rejected = [
        {**cut, "activity_source": "input"} for cut in input_rejected
    ]
    _, input_overrides = protect_cuts_with_activity(input_cleared, input_intervals)
    input_overrides = [
        {**cut, "activity_source": "input", "clearance": "redundant"}
        for cut in input_overrides
    ]
    input_safe.extend(input_cleared)

    cleared: list[dict[str, Any]] = []
    guarded: list[dict[str, Any]] = []
    for cut in input_safe:
        screen_action = str(cut.get("screen_action") or "").strip().lower()
        (cleared if screen_action in {"none", "redundant"} else guarded).append(cut)

    guarded_safe, visual_rejected = protect_cuts_with_activity(guarded, visual_intervals)
    visual_rejected = [
        {**cut, "activity_source": "visual"} for cut in visual_rejected
    ]
    _, visual_overrides = protect_cuts_with_activity(cleared, visual_intervals)
    visual_overrides = [
        {**cut, "activity_source": "visual", "clearance": cut.get("screen_action")}
        for cut in visual_overrides
    ]
    kept = sorted(cleared + guarded_safe, key=lambda item: (item["start_ms"], item["end_ms"]))
    rejected = sorted(
        input_rejected + visual_rejected,
        key=lambda item: (item["start_ms"], item["end_ms"]),
    )
    return kept, rejected, input_overrides + visual_overrides
