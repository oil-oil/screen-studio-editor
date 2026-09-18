#!/usr/bin/env python3
"""Learn a creator's cut preferences and arbitrate high-recall candidates."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from benchmark_autoedit import intersection_duration
from gemini_edit_candidates import (
    DEFAULT_API_BASE,
    DEFAULT_API_KEY_FILE,
    extract_json_from_text,
    flatten_words,
    load_transcript,
    post_json,
    transcript_context,
)
from global_edit_planner import file_sha256, grounded_silent_range, transcript_atoms


CURRENT_GLOBAL_REPORT_NAME = "global-video-planner-v11.json"
LEGACY_GLOBAL_REPORT_NAMES = (
    "global-video-planner-v10.json",
    "global-video-planner-v9.json",
    "global-video-planner-v8.json",
    "global-video-planner-v7.json",
    "global-video-planner-gemini35flash-v6.json",
)
STRUCTURED_REPORT_NAMES = ("structured-edit-candidates-v1.json",)
ARBITER_VERSION = 21
# Every measured pause that the mechanical editor protected because of screen
# activity must reach the full-video decision pass. The old 2 s threshold hid
# short setup/navigation gaps before the model could see them.
PROTECTED_PAUSE_MIN_MS = 0.0
PROTECTED_PAUSE_FRAGMENT_GAP_MS = 1_200.0
CANDIDATE_SEQUENCE_GAP_S = 4.0
AUTOMATIC_SCREEN_CUT_ROLES = {
    "failed_take",
    "setup_navigation",
    "loading_wait",
    "dead_air",
}
GLOBAL_CANDIDATE_DETECTORS = {"global_planner"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def api_key(args: argparse.Namespace) -> str:
    key = (
        args.api_key
        or os.environ.get("SCREEN_STUDIO_EDITOR_API_KEY", "")
        or os.environ.get("ZENMUX_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    if not key and args.api_key_file.exists():
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"API key not found in environment or {args.api_key_file}")
    return key


def candidate_family(candidate: dict[str, Any]) -> str:
    return "screen_pause" if candidate.get("planner_category") == "screen_pause" else "speech"


def candidate_key(candidate: dict[str, Any]) -> tuple[int, int, str]:
    return (
        round(float(candidate["start"]) * 2.0),
        round(float(candidate["end"]) * 2.0),
        candidate_family(candidate),
    )


def automatic_safety_blocker(candidate: dict[str, Any]) -> str | None:
    """Protect continuously changing visuals that have no input telemetry."""
    if candidate.get("planner_category") == "content_compression":
        return "content_compression_requires_manual_review"
    if (
        candidate_family(candidate) == "screen_pause"
        and float(candidate.get("visual_activity_fraction") or 0.0) >= 0.9
        and float(candidate.get("input_activity_fraction") or 0.0) <= 0.05
        and not (
            candidate.get("video_review_supplied")
            and candidate.get("screen_action") == "redundant"
            and str(candidate.get("visual_assessment") or "").strip()
        )
    ):
        return "continuous_visual_without_input"
    if candidate.get("detector_type") in {
        "possible_isolated_take",
        "possible_abandoned_sentence",
    }:
        return "broad_structural_candidate_requires_manual_review"
    if (
        candidate_family(candidate) == "screen_pause"
        and candidate.get("video_review_supplied")
        and (
            candidate.get("screen_action") not in {"none", "redundant"}
            or not str(candidate.get("visual_assessment") or "").strip()
        )
    ):
        return "video_did_not_clear_screen_activity"
    if (
        candidate_family(candidate) == "screen_pause"
        and candidate.get("video_review_supplied")
        and candidate.get("sequence_role") not in AUTOMATIC_SCREEN_CUT_ROLES
    ):
        return "unsafe_screen_sequence_role"
    return None


def merge_protected_pause_fragments(
    pauses: list[dict[str, Any]],
    *,
    max_gap_ms: float = PROTECTED_PAUSE_FRAGMENT_GAP_MS,
) -> list[dict[str, Any]]:
    """Join one transcript-grounded pause split by clicks or handling noise.

    Silence detection may divide a single no-speech setup/navigation interval
    around a short click, keystroke, or other non-verbal sound. Matching
    transcript context on both sides is the important guard: the merged range
    still goes through full-video review and is never accepted mechanically.
    """
    normalized = [dict(item) for item in pauses if isinstance(item, dict)]
    normalized.sort(
        key=lambda item: (
            float(item.get("start_ms") or 0.0),
            float(item.get("end_ms") or 0.0),
        )
    )
    merged: list[dict[str, Any]] = []
    for pause in normalized:
        try:
            start_ms = float(pause["start_ms"])
            end_ms = float(pause["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_ms <= start_ms:
            continue
        if merged:
            previous = merged[-1]
            same_context = (
                str(previous.get("text_before") or "").strip()
                == str(pause.get("text_before") or "").strip()
                and str(previous.get("text_after") or "").strip()
                == str(pause.get("text_after") or "").strip()
                and bool(str(pause.get("text_before") or "").strip())
                and bool(str(pause.get("text_after") or "").strip())
            )
            gap_ms = start_ms - float(previous["end_ms"])
            if same_context and 0.0 <= gap_ms <= max_gap_ms:
                previous["end_ms"] = max(float(previous["end_ms"]), end_ms)
                previous["duration_ms"] = (
                    float(previous["end_ms"]) - float(previous["start_ms"])
                )
                previous["merged_pause_fragments"] = (
                    int(previous.get("merged_pause_fragments") or 1) + 1
                )
                continue
        pause["start_ms"] = start_ms
        pause["end_ms"] = end_ms
        pause["duration_ms"] = end_ms - start_ms
        pause["merged_pause_fragments"] = 1
        merged.append(pause)
    return merged


def selected_global_report_names(project: Path) -> tuple[str, ...]:
    """Use the current model-neutral planner report, with legacy fallback."""
    if (project / CURRENT_GLOBAL_REPORT_NAME).exists():
        return (CURRENT_GLOBAL_REPORT_NAME,)
    return tuple(
        name for name in LEGACY_GLOBAL_REPORT_NAMES if (project / name).exists()
    )


def candidate_rows(
    project: Path,
    protected_pause_min_ms: float = PROTECTED_PAUSE_MIN_MS,
    candidate_source: str = "global",
) -> list[dict[str, Any]]:
    transcript_path = project / "baseline-report.transcript.edit.json"
    segments = load_transcript(transcript_path)
    atoms = transcript_atoms(segments)
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    source_rows: list[tuple[str, dict[str, Any]]] = []
    activity_report_path = project / "baseline-report.json"
    activity_report: dict[str, Any] = {}
    if activity_report_path.exists() and candidate_source in {"all", "global"}:
        activity_report = load_json(activity_report_path)
        protected_pauses = merge_protected_pause_fragments(
            activity_report.get("pauses_protected_by_activity") or []
        )
        for pause in protected_pauses:
            if float(pause.get("duration_ms") or 0.0) < protected_pause_min_ms:
                continue
            source_rows.append((
                activity_report_path.name,
                {
                    "start": float(pause["start_ms"]) / 1000.0,
                    "end": float(pause["end_ms"]) / 1000.0,
                    "planner_category": "screen_pause",
                    "removed_text": "[screen-active silent pause]",
                    "planner_reason": (
                        "Transcript-grounded no-speech interval overlapping screen activity; "
                        "the full-video model must decide whether the action is meaningful."
                    ),
                    "merged_pause_fragments": int(
                        pause.get("merged_pause_fragments") or 1
                    ),
                },
            ))
    global_report_names = selected_global_report_names(project)
    report_names = (
        global_report_names + STRUCTURED_REPORT_NAMES
        if candidate_source == "all"
        else global_report_names
        if candidate_source == "global"
        else STRUCTURED_REPORT_NAMES
    )
    for report_name in report_names:
        report_path = project / report_name
        if not report_path.exists():
            continue
        report = load_json(report_path)
        for raw in report.get("candidates") or []:
            if not isinstance(raw, dict):
                continue
            source_rows.append((report_name, raw))

    for report_name, raw in source_rows:
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0.0
            or end <= start
        ):
            continue
        row = dict(raw)
        if candidate_family(row) == "screen_pause":
            grounded = grounded_silent_range(start, end, atoms)
            if grounded is None or grounded[1] - grounded[0] < 0.8:
                continue
            start, end = grounded
        row["start"] = start
        row["end"] = end
        row["start_ms"] = round(start * 1000.0)
        row["end_ms"] = round(end * 1000.0)
        row["duration_ms"] = round((end - start) * 1000.0)
        interval_ms = [(start * 1000.0, end * 1000.0)]
        duration_ms = max(1.0, (end - start) * 1000.0)
        row["visual_activity_fraction"] = round(
            intersection_duration(
                interval_ms,
                [tuple(item) for item in activity_report.get("visual_activity_intervals_ms") or []],
            ) / duration_ms,
            5,
        )
        row["input_activity_fraction"] = round(
            intersection_duration(
                interval_ms,
                [tuple(item) for item in activity_report.get("input_activity_intervals_ms") or []],
            ) / duration_ms,
            5,
        )
        row["source_report"] = report_name
        context_window_s = 35.0 if candidate_family(row) == "screen_pause" else 12.0
        row["context"] = transcript_context(segments, start, end, context_window_s)
        key = candidate_key(row)
        existing = rows.get(key)
        # Prefer the grounded v4 proposal when clocks are effectively equal.
        if existing is None or (row.get("removed_quote") and not existing.get("removed_quote")):
            rows[key] = row
    return sorted(rows.values(), key=lambda item: (item["start"], item["end"]))


def build_preferences(
    root: Path,
    protected_pause_min_ms: float = PROTECTED_PAUSE_MIN_MS,
    candidate_source: str = "global",
) -> dict[str, Any]:
    examples: list[dict[str, Any]] = []
    manual_cut_examples: list[dict[str, Any]] = []
    for project in sorted(root.glob("val2-*.screenstudio")):
        ground_path = project / "benchmark-ground-truth.json"
        if not ground_path.exists():
            continue
        ground = load_json(ground_path)
        truth = [tuple(item) for item in ground.get("manual_cut_intervals_ms") or []]
        source_project = str(ground.get("source_project") or "")
        words = flatten_words(
            load_transcript(project / "baseline-report.transcript.edit.json")
        )
        for start_ms, end_ms in truth:
            if end_ms - start_ms < 2_500.0:
                continue
            removed_words = [
                word
                for word in words
                if start_ms
                <= (float(word["start"]) + float(word["end"])) * 500.0
                <= end_ms
            ]
            speech_duration_s = sum(
                float(word["end"]) - float(word["start"])
                for word in removed_words
            )
            if speech_duration_s < 1.5:
                continue
            before_words = [
                word
                for word in words
                if start_ms - 5_000.0
                <= (float(word["start"]) + float(word["end"])) * 500.0
                < start_ms
            ]
            after_words = [
                word
                for word in words
                if end_ms
                < (float(word["start"]) + float(word["end"])) * 500.0
                <= end_ms + 5_000.0
            ]
            removed_text = "".join(
                str(word.get("word") or "") for word in removed_words
            ).strip()
            if not removed_text:
                continue
            manual_cut_examples.append({
                "source_project": source_project,
                "duration_s": round((end_ms - start_ms) / 1000.0, 3),
                "before": "".join(
                    str(word.get("word") or "") for word in before_words
                )[-100:],
                "removed_text": removed_text,
                "after": "".join(
                    str(word.get("word") or "") for word in after_words
                )[:100],
            })
        for index, candidate in enumerate(
            candidate_rows(project, protected_pause_min_ms, candidate_source), start=1
        ):
            interval = [(candidate["start"] * 1000.0, candidate["end"] * 1000.0)]
            duration = interval[0][1] - interval[0][0]
            range_fraction = (
                intersection_duration(interval, truth) / duration if duration else 0.0
            )
            speech_intervals = [
                (
                    max(interval[0][0], float(word["start"]) * 1000.0),
                    min(interval[0][1], float(word["end"]) * 1000.0),
                )
                for word in words
                if float(word["end"]) * 1000.0 > interval[0][0]
                and float(word["start"]) * 1000.0 < interval[0][1]
            ]
            speech_duration = sum(end - start for start, end in speech_intervals)
            speech_fraction = (
                intersection_duration(speech_intervals, truth) / speech_duration
                if speech_duration
                else None
            )
            if candidate_family(candidate) == "screen_pause" or speech_fraction is None:
                label = (
                    "cut"
                    if range_fraction >= 0.7
                    else "keep"
                    if range_fraction <= 0.3
                    else "partial"
                )
            elif range_fraction >= 0.7 and speech_fraction >= 0.7:
                label = "cut"
            elif range_fraction <= 0.3 or speech_fraction <= 0.3:
                label = "keep"
            else:
                label = "partial"
            examples.append({
                "id": f"example_{len(examples) + 1:03d}",
                "source_project": source_project,
                "candidate_index": index,
                "label": label,
                "overlap_fraction": round(range_fraction, 5),
                "range_overlap_fraction": round(range_fraction, 5),
                "speech_overlap_fraction": (
                    round(speech_fraction, 5)
                    if speech_fraction is not None
                    else None
                ),
                "category": candidate.get("planner_category"),
                "detector_type": candidate.get("detector_type") or "global_planner",
                "duration_s": round(candidate["end"] - candidate["start"], 3),
                "removed_text": candidate.get("removed_text") or "",
                "kept_text": candidate.get("kept_text") or "",
                "removed_quote": candidate.get("removed_quote") or "",
                "cut_until_id": candidate.get("cut_until_id"),
                "replacementless_local_cleanup": bool(
                    candidate.get("replacementless_local_cleanup")
                ),
                "replacementless_content_compression": bool(
                    candidate.get("replacementless_content_compression")
                ),
                "repair_evidence": candidate.get("repair_evidence"),
                "planner_reason": candidate.get("planner_reason") or "",
                "similarity": candidate.get("similarity"),
                "restart_similarity": candidate.get("restart_similarity"),
                "repair_marker": candidate.get("repair_marker"),
                "local_acoustic_safe": bool(candidate.get("local_acoustic_safe")),
                "visual_activity_fraction": candidate.get("visual_activity_fraction", 0.0),
                "input_activity_fraction": candidate.get("input_activity_fraction", 0.0),
                "context": candidate.get("context") or "",
            })
    signature = hashlib.sha256(
        json.dumps(
            {
                "protected_pause_min_ms": protected_pause_min_ms,
                "candidate_source": candidate_source,
                "examples": examples,
                "manual_cut_examples": manual_cut_examples,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "source": "creator hand-edited Screen Studio benchmarks",
        "protected_pause_min_ms": protected_pause_min_ms,
        "candidate_source": candidate_source,
        "signature": signature,
        "example_count": len(examples),
        "examples": examples,
        "manual_cut_example_count": len(manual_cut_examples),
        "manual_cut_examples": manual_cut_examples,
    }


def annotate_candidate_relationships(
    candidates: list[dict[str, Any]],
    *,
    max_gap_s: float = CANDIDATE_SEQUENCE_GAP_S,
) -> list[dict[str, Any]]:
    """Expose exact timeline and overlap structure to the full-video arbiter."""
    if not candidates:
        return candidates

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_end = 0.0
    for candidate in candidates:
        start = float(candidate["start"])
        end = float(candidate["end"])
        if current and start > current_end + max_gap_s:
            groups.append(current)
            current = []
        current.append(candidate)
        current_end = max(current_end, end) if len(current) > 1 else end
    if current:
        groups.append(current)

    for group_index, group in enumerate(groups, start=1):
        group_id = f"sequence_{group_index:03d}"
        for candidate in group:
            start = float(candidate["start"])
            end = float(candidate["end"])
            overlaps: list[str] = []
            contained_by: list[str] = []
            contains: list[str] = []
            related: list[str] = []
            for other in group:
                if other is candidate:
                    continue
                other_id = str(other["id"])
                other_start = float(other["start"])
                other_end = float(other["end"])
                related.append(other_id)
                if max(start, other_start) < min(end, other_end):
                    overlaps.append(other_id)
                if other_start <= start and other_end >= end:
                    contained_by.append(other_id)
                if start <= other_start and end >= other_end:
                    contains.append(other_id)
            candidate["sequence_group_id"] = group_id
            candidate["related_target_ids"] = related
            candidate["overlapping_target_ids"] = overlaps
            candidate["contained_by_target_ids"] = contained_by
            candidate["contains_target_ids"] = contains
    return candidates


def target_candidates(
    project: Path,
    protected_pause_min_ms: float = PROTECTED_PAUSE_MIN_MS,
    candidate_source: str = "global",
) -> list[dict[str, Any]]:
    candidates = candidate_rows(project, protected_pause_min_ms, candidate_source)
    for index, candidate in enumerate(candidates, start=1):
        candidate["id"] = f"target_{index:03d}"
    return annotate_candidate_relationships(candidates)


def prompt_for_arbitration(
    examples: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    video_supplied: bool = False,
    candidate_source: str = "global",
    manual_cut_examples: list[dict[str, Any]] | None = None,
) -> str:
    example_keys = [
        "label",
        "category",
        "duration_s",
        "removed_text",
        "planner_reason",
        "visual_activity_fraction",
        "input_activity_fraction",
        "context",
        "kept_text",
        "removed_quote",
        "cut_until_id",
        "replacementless_local_cleanup",
        "replacementless_content_compression",
        "detector_type",
        "repair_evidence",
    ]
    if candidate_source in {"structured", "all"}:
        example_keys.extend([
            "detector_type",
            "kept_text",
            "similarity",
            "restart_similarity",
            "repair_marker",
            "local_acoustic_safe",
            "range_overlap_fraction",
            "speech_overlap_fraction",
        ])
    compact_examples = [
        {
            key: example.get(key)
            for key in example_keys
        }
        for example in examples
    ]
    compact_targets = []
    for candidate in candidates:
        target = {
            "id": candidate["id"],
            "category": candidate.get("planner_category"),
            "start_ms": candidate.get("start_ms"),
            "end_ms": candidate.get("end_ms"),
            "duration_s": round(candidate["end"] - candidate["start"], 3),
            "sequence_group_id": candidate.get("sequence_group_id"),
            "related_target_ids": candidate.get("related_target_ids") or [],
            "overlapping_target_ids": candidate.get("overlapping_target_ids") or [],
            "contained_by_target_ids": candidate.get("contained_by_target_ids") or [],
            "contains_target_ids": candidate.get("contains_target_ids") or [],
            "removed_text": candidate.get("removed_text") or "",
            "planner_reason": candidate.get("planner_reason") or "",
            "visual_activity_fraction": candidate.get("visual_activity_fraction", 0.0),
            "input_activity_fraction": candidate.get("input_activity_fraction", 0.0),
            "context": candidate.get("context") or "",
            "kept_text": candidate.get("kept_text") or "",
            "removed_quote": candidate.get("removed_quote") or "",
            "cut_until_id": candidate.get("cut_until_id"),
            "replacementless_local_cleanup": bool(
                candidate.get("replacementless_local_cleanup")
            ),
            "replacementless_content_compression": bool(
                candidate.get("replacementless_content_compression")
            ),
            "detector_type": candidate.get("detector_type"),
            "repair_evidence": candidate.get("repair_evidence"),
        }
        if candidate_source in {"structured", "all"}:
            target.update({
                "detector_type": candidate.get("detector_type"),
                "kept_text": candidate.get("kept_text") or "",
                "similarity": candidate.get("similarity"),
                "restart_similarity": candidate.get("restart_similarity"),
                "repair_marker": candidate.get("repair_marker"),
                "local_acoustic_safe": bool(candidate.get("local_acoustic_safe")),
            })
        if video_supplied:
            target["allowed_screen_actions"] = (
                ["redundant", "meaningful"]
                if float(candidate.get("input_activity_fraction") or 0.0) > 0.0
                else ["none", "redundant", "meaningful"]
            )
        compact_targets.append(target)
    schema = {
        "decisions": [
            {
                "id": "target_001",
                "decision": "cut | keep | review",
                "confidence": "high | medium | low",
                "reason": "context, audio/video evidence, and any relevant creator examples",
                "sequence_assessment": (
                    "how this exact range relates to overlapping candidates, "
                    "the failed attempt, and the retained replacement"
                ),
                "sequence_role": (
                    "failed_take | setup_navigation | loading_wait | dead_air | "
                    "live_demonstration | invited_showcase | result_reading | "
                    "essential_action | editorial_compression | other"
                ),
                "replacement_evidence": (
                    "retained target/timestamp that recreates the action or result, "
                    "or empty when not applicable"
                ),
            }
        ]
    }
    if video_supplied:
        schema["decisions"][0].update({
            "screen_action": "none | redundant | meaningful | unclear",
            "visual_assessment": "specific visible action/result in this range",
        })
    pause_examples = [
        item for item in examples if item.get("category") == "screen_pause"
    ]
    pause_labels = {
        label: sum(item.get("label") == label for item in pause_examples)
        for label in ("cut", "partial", "keep")
    }
    pause_profile = (
        f"Across {len(pause_examples)} labeled screen-pause examples, this creator "
        f"cut {pause_labels['cut']}, partially cut {pause_labels['partial']}, and "
        f"kept {pause_labels['keep']}. Treat this as behavioral evidence, not a "
        "blanket rule."
        if pause_examples
        else "No labeled screen-pause summary is available."
    )
    speech_label_note = ""
    structured_note = ""
    if candidate_source in {"structured", "all"}:
        speech_label_note = """For speech candidates, cut also means that the creator removed the spoken
words themselves, not merely a long silence inside the proposed range.
range_overlap_fraction describes the complete interval; speech_overlap_fraction
describes recognized speech only. Low speech overlap is strong keep evidence
even when a surrounding pause makes range overlap look high."""
        structured_note = """Structured detector evidence is also only a hypothesis. For duplicate or
abandoned takes, compare removed_text with kept_text and require the later take
to preserve every useful claim. A repair_marker by itself is not a correction.
For isolated_filler, local_acoustic_safe means the splice is technically clean,
not that this creator necessarily wants that filler removed. A normal topic
transition is not a retake: if kept_text changes subject instead of restating
removed_text, keep the candidate."""
    has_preferences = bool(examples or manual_cut_examples)
    editing_basis = (
        "Use the provided examples to learn this creator's editing preferences."
        if has_preferences else
        "No creator examples are available. Judge each proposed cut from the "
        "actual narration, audio/video, and surrounding context. Remove clear "
        "failed takes, redundant speech, and empty waits while preserving unique "
        "information and demonstrations; do not invent personal preferences."
    )
    compression_guidance = (
        "The hand-edited removals show which complete supporting commentary or "
        "overlong examples this creator removes. Apply that evidence only when "
        "the target serves the same editorial purpose."
        if manual_cut_examples else
        "There is no demonstrated preference for broad content compression. "
        "Preserve coherent passages with unique viewer value."
    )
    return f"""
You are editing a talking-head tutorial. {editing_basis}
When labeled examples are supplied, the labels mean:
- cut: the creator removed most of this range;
- keep: the creator intentionally retained it;
- partial: only part of the proposed range was removed, so the broad range is
  unsafe for automatic deletion.
{speech_label_note}
{"A complete source-timeline-aligned video with microphone audio is attached. Every target includes exact source-timeline start_ms/end_ms. Seek to that exact range, then inspect the surrounding sequence before deciding." if video_supplied else "No video is attached to this pass; use the grounded transcript context."}
{"For every target, report screen_action using only that target's allowed_screen_actions and add a concrete visual_assessment. Telemetry is authoritative: when none is absent, a click or keystroke was recorded; inspect the video and choose redundant only when the action is disposable setup/navigation, otherwise choose meaningful. Use none only when it is allowed and the video confirms that a visual detector fired on a genuinely static range. Use meaningful for unique clicks, typing, demonstrations, readable results, or visual comparisons. A screen_pause can be automatically cleared across detected screen activity only when you return cut/high plus none or redundant and a specific visual assessment; the final editor still treats input telemetry more strictly than visual-only activity." if video_supplied else ""}

When examples are available, infer these preferences from them:
- whether the creator leaves screen navigation or reading time visible;
- how they handle abandoned takes, repeated wording, and micro-fragments;
- they prefer a light edit when evidence is ambiguous.

CREATOR BEHAVIOR SUMMARY:
{pause_profile}

HAND-EDITED REMOVALS FROM OTHER VIDEOS:
{json.dumps(manual_cut_examples or [], ensure_ascii=False)}

Use any supplied removals as editorial evidence, but keep a target when it
contains unique viewer value.

visual_activity_fraction and input_activity_fraction are measured telemetry, not
model guesses. They prove that something moved; they do not prove that viewers
must watch every intermediate frame. A scroll, click, or navigation step is
redundant when the useful destination remains visible after the cut and the
intermediate motion teaches nothing. Mark that action redundant even when the
destination itself is meaningful. Preserve only actions whose continuous process,
result evolution, or timing carries information.

Never treat a model's planner_reason as ground truth. Compare it with the actual
removed_text and surrounding context. A sentence that continues grammatically
after a pause is not an abandoned take. A result showcase or invited reading
pause is content. Return cut/high only when the available context and audio/video
evidence clearly support removing the complete proposed range; otherwise keep
or review. Apply the creator's preferences when examples are available.
For replacementless_local_cleanup, listen across the proposed splice. Cut/high
only when the removed fragment is a delivery defect and the before/after speech
forms one complete fluent sentence without it; no separate replacement sentence
is required.
Word-level repeated_delivery_fragment candidates are deliberately high-recall
hypotheses. Confirm the actual audio has a stutter, partial-word restart, or
abandoned repetition; keep natural emphasis and intentional repeated wording.
For replacementless_content_compression, cut/high only when the complete passage
is optional under the available evidence and removing it loses no unique
claim, example, number, warning, result, instruction, or screen action.
{compression_guidance}
{structured_note}
Judge the targets as one timeline, not as unrelated snippets. If a cluster of
screen pauses follows an instruction to pause, read, compare, inspect, score,
or watch a sequence of outputs, preserve the entire showcase cluster even when
that instruction appears only in the earlier candidates' context. For speech,
an alleged duplicate supported by just one planner is still unsafe when the
surrounding instructions or UI destination differ.

Candidates sharing sequence_group_id are overlapping or closely adjacent parts
of one local recording sequence. Use related_target_ids and the explicit overlap
and containment fields to avoid contradictory decisions. A broad parent range
cannot be safely cut while an interior child range is kept as meaningful.
Conversely, a locally meaningful click, preview, or result does not force keep
when it belongs only to a failed/abandoned attempt and the later retained take
clearly recreates the same viewer-facing action or result. In that case classify
the action as redundant within the failed sequence and explain the replacement
in sequence_assessment.

Classify every target's sequence_role before deciding. setup_navigation means
positioning the UI before the next topic, where the useful destination remains
visible after the cut. It does not include scrolling through examples currently
being discussed. live_demonstration, invited_showcase, result_reading, and
essential_action are viewer-facing content and must not be cut automatically.
Use failed_take only when replacement_evidence points to a retained later take
that recreates the needed narration and screen value. loading_wait and dead_air
must contain no viewer-facing progression worth watching.
Read the surrounding transcript and watch the video to determine whether the
speaker invites viewers to look, compare, or inspect a result. Protect that
viewing time unless a retained replacement take recreates its viewer-facing value.
Judge semantic redundancy in context: identical wording can serve different
purposes, and different wording can convey the same information. Filler words
can express agreement or hesitation; no word list or text similarity score
authorizes a deletion.

Return strict JSON only:
{json.dumps(schema, ensure_ascii=False, indent=2)}

LABELED EXAMPLES FROM OTHER VIDEOS:
{json.dumps(compact_examples, ensure_ascii=False)}

UNLABELED TARGET CANDIDATES:
{json.dumps(compact_targets, ensure_ascii=False)}
""".strip()


def response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("Arbiter response contains no choices.")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("Arbiter response contains no text.")
    return content


def request_arbitration(
    url: str,
    payload: dict[str, Any],
    key: str,
    timeout: int,
    *,
    attempts: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry one transient empty or malformed structured response."""
    last_error: Exception | None = None
    for _attempt in range(max(1, attempts)):
        response = post_json(url, payload, key, timeout)
        try:
            parsed = extract_json_from_text(response_text(response))
            if not isinstance(parsed, dict):
                raise ValueError("Arbiter response JSON is not an object.")
            return response, parsed
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            last_error = error
    assert last_error is not None
    raise last_error


def fallback_candidate_batches(
    candidates: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Separate speech from screen pauses after an oversized response fails."""
    speech = [item for item in candidates if candidate_family(item) == "speech"]
    pauses = [item for item in candidates if candidate_family(item) == "screen_pause"]
    batches = [items for items in (speech, pauses) if items]
    if len(batches) > 1:
        return batches
    midpoint = max(1, len(candidates) // 2)
    return [
        items
        for items in (candidates[:midpoint], candidates[midpoint:])
        if items
    ]


def arbitration_payload(
    model: str,
    candidate_source: str,
    prompt: str,
    *,
    video_name: str | None = None,
    video_data_url: str | None = None,
) -> dict[str, Any]:
    user_content: str | list[dict[str, Any]] = prompt
    if video_name and video_data_url:
        user_content = [
            {
                "type": "file",
                "file": {
                    "file_data": video_data_url,
                    "filename": video_name,
                },
            },
            {"type": "text", "text": prompt},
        ]
    payload: dict[str, Any] = {
        "model": model,
        "candidate_source": candidate_source,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a conservative personalized video editor. "
                    "Return strict JSON only."
                ),
            },
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 32_000,
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if not model.startswith("anthropic/"):
        payload["temperature"] = 0
    return payload


def relationship_safety_blocker(
    candidate: dict[str, Any],
    decisions_by_id: dict[str, dict[str, Any]],
) -> str | None:
    """Reject a broad cut that contradicts an uncleared contained target."""
    for contained_id in candidate.get("contains_target_ids") or []:
        contained = decisions_by_id.get(str(contained_id))
        if contained is None:
            return "contained_target_missing_decision"
        if not (
            contained.get("decision") == "cut"
            and contained.get("confidence") == "high"
        ):
            return "contained_target_not_cleared"

    return None


def arbitrate(args: argparse.Namespace) -> None:
    preferences = load_json(args.preferences) if args.preferences else {}
    ground_path = args.project / "benchmark-ground-truth.json"
    source_project = (
        str(load_json(ground_path).get("source_project") or "")
        if ground_path.exists()
        else str(args.project.resolve())
    )
    candidate_source = (
        args.candidate_source
        or str(preferences.get("candidate_source") or "global")
    )
    examples = [
        item
        for item in preferences.get("examples") or []
        if item.get("source_project") != source_project
        and (
            candidate_source == "all"
            or (
                candidate_source == "global"
                and item.get("detector_type", "global_planner")
                in GLOBAL_CANDIDATE_DETECTORS
            )
            or (
                candidate_source == "structured"
                and item.get("detector_type") != "global_planner"
            )
        )
    ]
    manual_cut_examples = [
        item
        for item in (preferences.get("manual_cut_examples") or [])
        if isinstance(item, dict) and item.get("source_project") != source_project
    ]
    protected_pause_min_ms = (
        args.protected_pause_min_ms
        if args.protected_pause_min_ms is not None
        else float(preferences.get("protected_pause_min_ms", PROTECTED_PAUSE_MIN_MS))
    )
    candidates = target_candidates(
        args.project, protected_pause_min_ms, candidate_source
    )
    candidate_signature = hashlib.sha256(
        json.dumps(candidates, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if args.video and not args.video.exists():
        raise SystemExit(f"Video does not exist: {args.video}")
    if args.video and args.video.stat().st_size > 80 * 1024 * 1024:
        raise SystemExit("Inline ZenMux video is limited to 80MB in this workflow.")
    video_sha256 = file_sha256(args.video) if args.video else None
    if args.resume and args.output.exists():
        cached = load_json(args.output)
        if (
            cached.get("model") == args.model
            and cached.get("arbiter_version") == ARBITER_VERSION
            and cached.get("candidate_source") == candidate_source
            and cached.get("preference_signature") == preferences.get("signature")
            and cached.get("candidate_signature") == candidate_signature
            and cached.get("video_sha256") == video_sha256
        ):
            print(json.dumps(cached, ensure_ascii=False, indent=2))
            return
    if not candidates:
        output = {
            "schema_version": 1,
            "arbiter_version": ARBITER_VERSION,
            "project": str(args.project),
            "source_project": source_project,
            "transcript": str(args.project / "baseline-report.transcript.edit.json"),
            "model": args.model,
            "candidate_source": candidate_source,
            "video": str(args.video) if args.video else None,
            "video_sha256": video_sha256,
            "preference_signature": preferences.get("signature"),
            "protected_pause_min_ms": protected_pause_min_ms,
            "candidate_signature": candidate_signature,
            "training_examples": len(examples),
            "candidate_count": 0,
            "accepted_count": 0,
            "safety_blocked_count": 0,
            "decisions": [],
            "candidates": [],
            "usage": None,
        }
        write_json(args.output, output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return
    video_data_url = (
        "data:video/mp4;base64," + base64.b64encode(args.video.read_bytes()).decode("ascii")
        if args.video else None
    )
    endpoint = f"{args.api_base.rstrip('/')}/chat/completions"
    arbiter_key = api_key(args)

    def payload_for(
        items: list[dict[str, Any]],
        batch_examples: list[dict[str, Any]],
        batch_manual_cut_examples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt = prompt_for_arbitration(
            batch_examples,
            items,
            video_supplied=bool(args.video),
            candidate_source=candidate_source,
            manual_cut_examples=batch_manual_cut_examples,
        )
        return arbitration_payload(
            args.model,
            candidate_source,
            prompt,
            video_name=args.video.name if args.video else None,
            video_data_url=video_data_url,
        )

    experimental_candidates = [
        item
        for item in candidates
        if item.get("planner_category") == "content_compression"
        or item.get("detector_type") == "repeated_delivery_fragment"
    ]
    technical_candidates = [
        item for item in candidates if item not in experimental_candidates
    ]
    decision_batches = [
        item for item in (technical_candidates, experimental_candidates) if item
    ]
    all_decisions: list[dict[str, Any]] = []
    all_usage: list[dict[str, Any] | None] = []
    for batch in decision_batches:
        experimental_batch = batch is experimental_candidates
        batch_examples = [
            item
            for item in examples
            if (
                item.get("detector_type") == "repeated_delivery_fragment"
                if experimental_batch
                else item.get("detector_type", "global_planner")
                == "global_planner"
            )
        ]
        batch_manual_cut_examples = (
            manual_cut_examples
            if any(
                item.get("planner_category") == "content_compression"
                for item in batch
            )
            else []
        )
        try:
            batch_response, batch_parsed = request_arbitration(
                endpoint,
                payload_for(batch, batch_examples, batch_manual_cut_examples),
                arbiter_key,
                args.timeout,
                attempts=1 if len(batch) >= 30 else 2,
            )
            all_decisions.extend(batch_parsed.get("decisions") or [])
            all_usage.append(batch_response.get("usage"))
        except (json.JSONDecodeError, TypeError, ValueError):
            for fallback_batch in fallback_candidate_batches(batch):
                fallback_response, fallback_parsed = request_arbitration(
                    endpoint,
                    payload_for(
                        fallback_batch,
                        batch_examples,
                        batch_manual_cut_examples,
                    ),
                    arbiter_key,
                    args.timeout,
                )
                all_decisions.extend(fallback_parsed.get("decisions") or [])
                all_usage.append(fallback_response.get("usage"))
    response = {"usage": {"decision_batches": all_usage}}
    parsed = {"decisions": all_decisions}
    by_id = {item["id"]: item for item in candidates}
    decisions = []
    seen: set[str] = set()
    for raw in parsed.get("decisions") or []:
        candidate_id = str(raw.get("id") or "")
        if candidate_id not in by_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        decision = str(raw.get("decision") or "review")
        confidence = str(raw.get("confidence") or "low")
        item = {
            "id": candidate_id,
            "decision": decision if decision in {"cut", "keep", "review"} else "review",
            "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
            "reason": str(raw.get("reason") or ""),
            "sequence_assessment": str(raw.get("sequence_assessment") or "").strip(),
            "sequence_role": str(raw.get("sequence_role") or "other").strip().lower(),
            "replacement_evidence": str(
                raw.get("replacement_evidence") or ""
            ).strip(),
        }
        if args.video:
            screen_action = str(raw.get("screen_action") or "unclear").strip().lower()
            allowed_screen_actions = (
                {"redundant", "meaningful"}
                if float(by_id[candidate_id].get("input_activity_fraction") or 0.0) > 0.0
                else {"none", "redundant", "meaningful"}
            )
            item["screen_action"] = (
                screen_action
                if screen_action in allowed_screen_actions
                else "unclear"
            )
            item["visual_assessment"] = str(
                raw.get("visual_assessment") or ""
            ).strip()
        decisions.append(item)

    decisions_by_id = {item["id"]: item for item in decisions}
    accepted = []
    for item in decisions:
        if item["decision"] == "cut" and item["confidence"] == "high":
            candidate = dict(by_id[item["id"]])
            if args.video:
                candidate["video_review_supplied"] = True
                candidate["screen_action"] = item["screen_action"]
                candidate["visual_assessment"] = item["visual_assessment"]
                candidate["sequence_role"] = item["sequence_role"]
                candidate["replacement_evidence"] = item["replacement_evidence"]
            blocker = (
                automatic_safety_blocker(candidate)
                or relationship_safety_blocker(
                    candidate, decisions_by_id
                )
            )
            if blocker:
                item["safety_blocker"] = blocker
                continue
            candidate["preference_decision"] = item
            accepted.append(candidate)
    output = {
        "schema_version": 1,
        "arbiter_version": ARBITER_VERSION,
        "project": str(args.project),
        "source_project": source_project,
        "transcript": str(args.project / "baseline-report.transcript.edit.json"),
        "model": args.model,
        "candidate_source": candidate_source,
        "video": str(args.video) if args.video else None,
        "video_sha256": video_sha256,
        "preference_signature": preferences.get("signature"),
        "protected_pause_min_ms": protected_pause_min_ms,
        "candidate_signature": candidate_signature,
        "training_examples": len(examples),
        "candidate_count": len(candidates),
        "accepted_count": len(accepted),
        "safety_blocked_count": sum(
            bool(item.get("safety_blocker")) for item in decisions
        ),
        "decisions": decisions,
        "candidates": accepted,
        "usage": response.get("usage"),
    }
    write_json(args.output, output)
    print(json.dumps(output, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument(
        "--protected-pause-min-ms", type=float, default=PROTECTED_PAUSE_MIN_MS
    )
    build.add_argument(
        "--candidate-source",
        choices=("all", "global", "structured"),
        default="global",
    )
    decide = subparsers.add_parser("decide")
    decide.add_argument("--project", type=Path, required=True)
    decide.add_argument("--preferences", type=Path)
    decide.add_argument("--output", type=Path, required=True)
    decide.add_argument("--model", required=True)
    decide.add_argument("--video", type=Path)
    decide.add_argument(
        "--api-base",
        default=os.environ.get(
            "SCREEN_STUDIO_EDITOR_API_BASE",
            os.environ.get("OPENAI_API_BASE", DEFAULT_API_BASE),
        ),
    )
    decide.add_argument("--api-key", default="")
    decide.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    decide.add_argument("--timeout", type=int, default=300)
    decide.add_argument("--protected-pause-min-ms", type=float)
    decide.add_argument(
        "--candidate-source", choices=("all", "global", "structured")
    )
    decide.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "build":
        preferences = build_preferences(
            args.root,
            args.protected_pause_min_ms,
            args.candidate_source,
        )
        write_json(args.output, preferences)
        print(json.dumps(preferences, ensure_ascii=False, indent=2))
    else:
        arbitrate(args)


if __name__ == "__main__":
    main()
