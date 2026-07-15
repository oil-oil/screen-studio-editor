#!/usr/bin/env python3
"""
Review spoken-video edit candidates with Bailian Omni, Gemini, or ZenMux.

This script does not edit a Screen Studio project. It reads an ASR transcript,
builds likely cut candidates for fillers, false starts, and repeats, optionally
extracts nearby audio/video evidence, asks a multimodal model to judge the
candidates, then writes:

- a full JSON report with model decisions
- a process.py-compatible cuts JSON containing only high-confidence cuts
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from editing_core import build_cuts_document


DEFAULT_API_BASE = "https://zenmux.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.5-flash"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_BAILIAN_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_BAILIAN_MODEL = "qwen3.5-omni-plus"
DEFAULT_SEMANTIC_AUDIT_MODEL = "qwen3.7-plus"
DEFAULT_API_KEY_FILE = Path.home() / ".zenmux_api_key"
DEFAULT_BAILIAN_CONFIG = Path.home() / ".bailian" / "config.json"
BAILIAN_MAX_BASE64_BYTES = 10 * 1024 * 1024
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}

FILLER_CHARS = set("嗯呃啊额诶唉哦噢")
FILLER_LATIN = {"em", "um", "uh", "er", "hmm"}
SOFT_FILLERS = {"然后", "就是", "这个", "那个", "其实", "的话", "对吧"}
STRONG_HESITATIONS = {"嗯", "呃", "额", "em", "um", "uh", "hmm"}
FILLER_CANDIDATE_TYPES = {"hard_filler", "soft_filler", "filler_cluster"}
SEMANTIC_HIGH_RISK_TYPES = {
    "possible_abandoned_sentence",
    "possible_false_start",
    "possible_isolated_take",
    "possible_sparse_retake",
    "explicit_self_correction",
    "global_paper_edit",
}
# Only unambiguous hesitation sounds qualify for the cheap local path.  In
# particular, “额” can be a real word and repeated clusters tend to need a human
# ear, so both are deliberately preserved unless explicitly model-reviewed.
SIMPLE_LOCAL_FILLERS = {"呃", "嗯", "em", "um", "uh", "hmm"}
SIMPLE_FILLER_MIN_SIDE_GAP_MS = 120.0
SIMPLE_FILLER_MAX_SPOKEN_MS = 900.0
STRONG_RETAKE_MARKERS = (
    "重新来", "重来", "再来一遍", "从头来", "从这里重新", "从哪里开始",
    "不要看", "别看", "先暂停", "算了别暂停", "你要从哪里开始",
)


def log(message: str) -> None:
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a multimodal model to review video edit candidates.")
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript JSON from bailian_transcribe.py or process.py.")
    parser.add_argument("--video", type=Path, help="Optional source/exported video for audio/visual evidence.")
    parser.add_argument("--audio", type=Path,
                        help="Optional separate microphone track to mux into evidence clips from --video.")
    parser.add_argument("--output", type=Path, default=Path("/tmp/gemini_edit_report.json"), help="Full report output path.")
    parser.add_argument("--cuts-output", type=Path, default=Path("/tmp/gemini_cuts.json"), help="process.py-compatible cuts JSON.")
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/gemini_edit_candidates"), help="Where extracted frames and request logs are written.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default="", help="Optional API key. Prefer ZENMUX_API_KEY.")
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    parser.add_argument("--max-candidates", type=int, default=60,
                        help="Maximum candidates after timeline-balanced ranking (default: 60).")
    parser.add_argument("--batch-size", type=int, default=6,
                        help="Candidates per request (default: 6; inline-video batches are capped by backend).")
    parser.add_argument(
        "--review-workers",
        type=int,
        default=2,
        help="Concurrent independent model-review batches (default: 2).",
    )
    parser.add_argument("--coordinate-space", choices=["source", "edited"], required=True,
                        help="Required clock declaration. Project edit transcripts use source; exported edited videos use edited.")
    parser.add_argument("--project-json", type=Path,
                        help="Required for edited-time cuts; fingerprints the slice map used for conversion.")
    parser.add_argument("--review-backend", choices=["auto", "bailian", "gemini", "zenmux"], default="auto",
                        help="Auto prefers Bailian Qwen Omni, then native Gemini, then ZenMux frames.")
    parser.add_argument("--bailian-api-key", default="", help="Optional Bailian key. Prefer DASHSCOPE_API_KEY.")
    parser.add_argument("--bailian-base", default=DEFAULT_BAILIAN_BASE)
    parser.add_argument("--bailian-model", default=DEFAULT_BAILIAN_MODEL)
    parser.add_argument(
        "--semantic-audit",
        choices=["off", "long-cuts", "all-cuts"],
        default="long-cuts",
        help=(
            "Use a reasoning video model only as a veto on proposed model cuts. "
            "The default audits cuts of at least --semantic-audit-min-cut-ms."
        ),
    )
    parser.add_argument(
        "--semantic-audit-model",
        default=DEFAULT_SEMANTIC_AUDIT_MODEL,
        help="Bailian reasoning model used for semantic cut vetoes.",
    )
    parser.add_argument(
        "--semantic-audit-min-cut-ms",
        type=float,
        default=15000.0,
        help="Minimum proposed-cut duration for the default semantic audit.",
    )
    parser.add_argument(
        "--semantic-audit-protected-pause-min-ms",
        type=float,
        default=5000.0,
        help=(
            "Also audit model-cleared screen-active pauses at or above this "
            "duration (default: 5000ms)."
        ),
    )
    parser.add_argument(
        "--semantic-audit-fps",
        type=float,
        default=0.5,
        help="Frame sampling rate for the visual-only semantic audit (default: 0.5).",
    )
    parser.add_argument("--gemini-api-key", default="", help="Optional direct Gemini key. Prefer GEMINI_API_KEY.")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--context-window", type=float, default=4.0, help="Seconds of transcript context on each side.")
    parser.add_argument("--frame-window", type=float, default=0.8, help="Seconds before/after candidate midpoint for frames.")
    parser.add_argument("--clip-context", type=float, default=1.5,
                        help="Audio/video seconds before and after native evidence clips.")
    parser.add_argument("--min-cut-ms", type=float, default=160.0)
    parser.add_argument("--max-auto-cut-ms", type=float, default=None,
                        help="Deprecated global limit; type-specific limits are used by default.")
    parser.add_argument("--max-filler-cut-ms", type=float, default=2500.0)
    parser.add_argument("--max-false-start-cut-ms", type=float, default=45000.0)
    parser.add_argument("--max-duplicate-cut-ms", type=float, default=30000.0)
    parser.add_argument("--max-sparse-retake-cut-ms", type=float, default=120000.0)
    parser.add_argument("--repeat-window", type=float, default=60.0,
                        help="Look-back window for repeated takes (default: 60s).")
    parser.add_argument(
        "--sparse-retake-window",
        type=float,
        default=120.0,
        help="Maximum span for low-speech failed demos before a cleaner restart.",
    )
    parser.add_argument("--repeat-span-segments", type=int, default=3,
                        help="Maximum adjacent ASR sentences in one repeat span (default: 3).")
    parser.add_argument(
        "--activity-report",
        type=Path,
        help=(
            "Optional process.py dry-run report. Pauses protected only because of "
            "screen activity are sent for multimodal review instead of being kept blindly."
        ),
    )
    parser.add_argument(
        "--protected-pause-min-review-ms",
        type=float,
        default=6000.0,
        help=(
            "Preserve screen-active pauses shorter than this locally instead of "
            "paying for model review (default: 6000ms)."
        ),
    )
    parser.add_argument(
        "--include-all-fillers",
        action="store_true",
        help=(
            "Also review weak Mandarin discourse words such as 这个/然后/其实 and the "
            "sentence particle 啊. By default only strong hesitations such as 呃/嗯 are "
            "sent to the model, which is faster and safer for automatic editing."
        ),
    )
    parser.add_argument(
        "--review-fillers-with-model",
        action="store_true",
        help=(
            "Diagnostic override: send fillers to the multimodal reviewer. By "
            "default, only clearly isolated fillers are cut locally and difficult "
            "fillers are preserved without an API call."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Build candidates and request logs without calling a model.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse complete, candidate-ID-matched raw batch responses already in "
            "--work-dir. Intended for resuming an interrupted identical review."
        ),
    )
    parser.add_argument(
        "--review-types",
        default="",
        help="Optional comma-separated candidate types for targeted calibration runs.",
    )
    parser.add_argument(
        "--extra-candidates",
        type=Path,
        action="append",
        default=[],
        help=(
            "Optional high-recall candidate report(s), normally produced by "
            "global_edit_planner.py. Extra candidates still require multimodal "
            "review, semantic veto, and local safety checks."
        ),
    )
    parser.add_argument("--range-start", type=float, help="Optional candidate range start in seconds.")
    parser.add_argument("--range-end", type=float, help="Optional candidate range end in seconds.")
    parser.add_argument(
        "--reuse-review-report",
        type=Path,
        help=(
            "Reuse decisions from an earlier full report and rebuild cuts with the "
            "current deterministic safety/boundary rules without another API call."
        ),
    )
    parser.add_argument(
        "--decision-override-report",
        type=Path,
        action="append",
        default=[],
        help=(
            "Optional targeted arbitration report. A high-confidence decision "
            "replaces the matching lower-confidence decision from the base report."
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def api_key_from_args(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("ZENMUX_API_KEY", "")
    if not key and args.api_key_file and args.api_key_file.exists():
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key and not args.dry_run:
        fail(f"ZENMUX_API_KEY is not set. Export it, put it in {args.api_key_file}, or pass --dry-run.")
    return key


def gemini_api_key_from_args(args: argparse.Namespace) -> str:
    return args.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")


def bailian_api_key_from_args(args: argparse.Namespace) -> str:
    key = args.bailian_api_key or os.environ.get("DASHSCOPE_API_KEY", "")
    if key:
        return key
    try:
        data = json.loads(DEFAULT_BAILIAN_CONFIG.read_text(encoding="utf-8"))
        return str(data.get("api_key") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def load_transcript(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        fail(f"transcript does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if isinstance(data.get("segments"), list):
            data = data["segments"]
        else:
            fail("transcript JSON must be a list or contain a segments list.")
    if not isinstance(data, list):
        fail("transcript JSON must be a list.")
    return [seg for seg in data if isinstance(seg, dict)]


def word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip()


def clean_for_match(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？、；;：:（）()《》<>\"'“”‘’]+", "", text).lower()


def is_standalone_filler(text: str) -> bool:
    core = clean_for_match(text)
    if not core:
        return False
    if core in FILLER_LATIN:
        return True
    return all(char in FILLER_CHARS for char in core)


def is_soft_filler(text: str) -> bool:
    return clean_for_match(text) in SOFT_FILLERS


def has_strong_hesitation(text: str) -> bool:
    normalized = clean_for_match(text)
    return any(token in normalized for token in STRONG_HESITATIONS)


def has_strong_retake_marker(candidate: dict[str, Any]) -> bool:
    text = clean_for_match(
        " ".join(str(candidate.get(key) or "") for key in ("removed_text", "context"))
    )
    return any(clean_for_match(marker) in text for marker in STRONG_RETAKE_MARKERS)


def flatten_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for seg in segments:
        for raw in seg.get("words") or []:
            if raw.get("start") is None or raw.get("end") is None:
                continue
            item = dict(raw)
            item["word"] = word_text(raw)
            item["segment_text"] = seg.get("text") or ""
            words.append(item)
    return sorted(words, key=lambda w: (float(w["start"]), float(w["end"])))


def transcript_context(segments: list[dict[str, Any]], start: float, end: float, window: float) -> str:
    rows = []
    left = max(0.0, start - window)
    right = end + window
    for seg in segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start))
        if seg_end < left or seg_start > right:
            continue
        text = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip()
        if text:
            rows.append(f"[{seg_start:.2f}-{seg_end:.2f}] {text}")
    return "\n".join(rows)


def candidate_text(words: list[dict[str, Any]], start: float, end: float) -> str:
    parts = [
        word_text(w)
        for w in words
        if float(w["end"]) > start and float(w["start"]) < end
    ]
    return "".join(parts).strip()


def add_candidate(candidates: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if item["end"] <= item["start"]:
        return
    item["start_ms"] = round(item["start"] * 1000.0)
    item["end_ms"] = round(item["end"] * 1000.0)
    item["duration_ms"] = round(item["end_ms"] - item["start_ms"])
    candidates.append(item)


def group_nearby_fillers(words: list[dict[str, Any]], segments: list[dict[str, Any]], window: float) -> list[dict[str, Any]]:
    raw = []
    for idx, word in enumerate(words):
        text = word_text(word)
        if not is_standalone_filler(text) and not is_soft_filler(text):
            continue
        start = max(0.0, float(word["start"]) - 0.04)
        end = float(word["end"]) + 0.08
        raw.append((idx, start, end, text, "hard_filler" if is_standalone_filler(text) else "soft_filler"))

    groups: list[list[tuple[int, float, float, str, str]]] = []
    for item in raw:
        previous = groups[-1][-1] if groups else None
        adjacent = previous is not None and item[0] == previous[0] + 1
        compatible = previous is not None and (
            (item[4] == "hard_filler" and previous[4] == "hard_filler")
            or (
                item[4] == "soft_filler"
                and previous[4] == "soft_filler"
                and clean_for_match(item[3]) == clean_for_match(previous[3])
            )
        )
        if groups and adjacent and compatible and item[1] - previous[2] <= 0.55:
            groups[-1].append(item)
        else:
            groups.append([item])

    candidates = []
    for group in groups:
        first_word_index = group[0][0]
        last_word_index = group[-1][0]
        spoken_start = float(words[first_word_index]["start"])
        spoken_end = float(words[last_word_index]["end"])
        previous_word = words[first_word_index - 1] if first_word_index > 0 else None
        following_word = words[last_word_index + 1] if last_word_index + 1 < len(words) else None
        pre_word_gap_ms = max(
            0.0,
            (spoken_start - float(previous_word["end"])) * 1000.0,
        ) if previous_word else 0.0
        post_word_gap_ms = max(
            0.0,
            (float(following_word["start"]) - spoken_end) * 1000.0,
        ) if following_word else 0.0
        start = group[0][1]
        end = group[-1][2]
        removed = "".join(item[3] for item in group)
        if end - start < 0.12:
            continue
        kind = "filler_cluster" if len(group) > 1 else group[0][4]
        add_candidate(candidates, {
            "id": f"cand_{len(candidates) + 1:03d}",
            "type": kind,
            "start": start,
            "end": end,
            "removed_text": removed,
            "context": transcript_context(segments, start, end, window),
            "contains_strong_hesitation": any(has_strong_hesitation(item[3]) for item in group),
            "spoken_start_ms": round(spoken_start * 1000.0),
            "spoken_end_ms": round(spoken_end * 1000.0),
            "spoken_duration_ms": round((spoken_end - spoken_start) * 1000.0),
            "pre_word_gap_ms": round(pre_word_gap_ms),
            "post_word_gap_ms": round(post_word_gap_ms),
        })
    return candidates


def load_input_activity_intervals(report_path: Path | None) -> tuple[list[tuple[float, float]], bool]:
    """Load click/keystroke intervals used by the conservative local filler gate."""
    if report_path is None:
        return [], False
    if not report_path.exists():
        fail(f"activity report does not exist: {report_path}")
    data = json.loads(report_path.read_text(encoding="utf-8"))
    intervals: list[tuple[float, float]] = []
    for raw in data.get("input_activity_intervals_ms") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        start_ms, end_ms = float(raw[0]), float(raw[1])
        if end_ms > start_ms:
            intervals.append((start_ms, end_ms))
    return intervals, True


def interval_overlap_ms(
    start_ms: float,
    end_ms: float,
    intervals: list[tuple[float, float]],
) -> float:
    return sum(
        max(0.0, min(end_ms, interval_end) - max(start_ms, interval_start))
        for interval_start, interval_end in intervals
    )


def conservative_local_filler_decisions(
    candidates: list[dict[str, Any]],
    activity_report: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve strong fillers cheaply; only obvious, clean splices become cuts.

    The default talking-head policy values natural delivery over removing every
    hesitation.  A filler is locally safe only when it is a single unambiguous
    hesitation, bounded by transcript silence on both sides, and a process.py
    activity report proves that no click/keystroke overlaps it.  Everything else
    is intentionally kept and never consumes multimodal tokens.
    """
    input_intervals, has_activity_report = load_input_activity_intervals(activity_report)
    local_decisions: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if (
            candidate.get("type") not in FILLER_CANDIDATE_TYPES
            or not bool(candidate.get("contains_strong_hesitation"))
        ):
            review_candidates.append(candidate)
            continue

        normalized = clean_for_match(str(candidate.get("removed_text") or ""))
        spoken_duration_ms = float(
            candidate.get("spoken_duration_ms", candidate.get("duration_ms", 0.0))
        )
        pre_gap_ms = float(candidate.get("pre_word_gap_ms", 0.0))
        post_gap_ms = float(candidate.get("post_word_gap_ms", 0.0))
        activity_overlap = interval_overlap_ms(
            float(candidate["start_ms"]),
            float(candidate["end_ms"]),
            input_intervals,
        )
        simple = (
            normalized in SIMPLE_LOCAL_FILLERS
            and 160.0 <= spoken_duration_ms <= SIMPLE_FILLER_MAX_SPOKEN_MS
            and pre_gap_ms >= SIMPLE_FILLER_MIN_SIDE_GAP_MS
            and post_gap_ms >= SIMPLE_FILLER_MIN_SIDE_GAP_MS
            and has_activity_report
            and activity_overlap <= 0.0
        )
        blockers = []
        if normalized not in SIMPLE_LOCAL_FILLERS:
            blockers.append("ambiguous_or_clustered_filler")
        if not 160.0 <= spoken_duration_ms <= SIMPLE_FILLER_MAX_SPOKEN_MS:
            blockers.append("spoken_duration_outside_safe_range")
        if pre_gap_ms < SIMPLE_FILLER_MIN_SIDE_GAP_MS:
            blockers.append("no_clean_leading_gap")
        if post_gap_ms < SIMPLE_FILLER_MIN_SIDE_GAP_MS:
            blockers.append("no_clean_trailing_gap")
        if not has_activity_report:
            blockers.append("no_input_activity_report")
        if activity_overlap > 0.0:
            blockers.append("input_activity_overlap")

        local_decisions.append({
            "id": candidate["id"],
            "decision": "cut" if simple else "keep",
            "confidence": "high",
            "start_ms": candidate["start_ms"],
            "end_ms": candidate["end_ms"],
            "removed_text": candidate.get("removed_text", ""),
            "reason": "local_easy_filler" if simple else "local_filler_preserved",
            "screen_action": "none" if simple else "unclear",
            "visual_assessment": (
                "No click or keystroke overlaps this isolated hesitation."
                if simple else ""
            ),
            "note": (
                "Locally cut: exact hesitation with quiet transcript gaps on both sides."
                if simple else "Preserved by conservative filler gate: " + ", ".join(blockers)
            ),
            "local_decision": True,
            "local_blockers": blockers,
        })
    return local_decisions, review_candidates


def conservative_local_advisory_decisions(
    candidates: list[dict[str, Any]],
    *,
    protected_pause_min_review_ms: float = 6000.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Preserve marker-only corrections that cannot pass the auto-cut gate.

    Words such as ``不是`` and ``也就是说`` are common in fluent Mandarin. Sending
    them to Omni is wasted work when no strong retake command is present,
    because deterministic safety would reject the result anyway.
    """
    local: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for candidate in candidates:
        if (
            candidate.get("type") == "protected_pause"
            and float(candidate.get("duration_ms", 0.0)) < protected_pause_min_review_ms
        ):
            local.append({
                "id": candidate["id"],
                "decision": "keep",
                "confidence": "high",
                "start_ms": candidate["start_ms"],
                "end_ms": candidate["end_ms"],
                "removed_text": candidate.get("removed_text", ""),
                "reason": "local_short_active_pause_preserved",
                "screen_action": "meaningful",
                "visual_assessment": "",
                "note": (
                    "Preserved locally: short pause overlaps screen activity; "
                    "the creator's light-editing policy keeps difficult micro-cuts."
                ),
                "local_decision": True,
                "local_blockers": ["short_screen_active_pause"],
            })
            continue
        if (
            candidate.get("type") != "explicit_self_correction"
            or has_strong_retake_marker(candidate)
            or float(candidate.get("max_internal_gap_ms", 0.0)) >= 800.0
        ):
            review.append(candidate)
            continue
        local.append({
            "id": candidate["id"],
            "decision": "keep",
            "confidence": "high",
            "start_ms": candidate["start_ms"],
            "end_ms": candidate["end_ms"],
            "removed_text": candidate.get("removed_text", ""),
            "reason": "local_weak_repair_marker_preserved",
            "screen_action": "unclear",
            "visual_assessment": "",
            "note": "Preserved locally: discourse/correction wording without a strong retake command.",
            "local_decision": True,
            "local_blockers": ["weak_repair_marker_only"],
        })
    return local, review


def segment_similarity(a: str, b: str) -> float:
    a_norm = clean_for_match(a)
    b_norm = clean_for_match(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm in b_norm or b_norm in a_norm:
        return min(len(a_norm), len(b_norm)) / max(len(a_norm), len(b_norm))
    # SequenceMatcher and character bigrams retain word order. The old set-of-
    # characters score rated opposite instructions such as “先打开再关闭” and
    # “先关闭再打开” as identical.
    sequence = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False).ratio()
    a_bigrams = {a_norm[index:index + 2] for index in range(max(1, len(a_norm) - 1))}
    b_bigrams = {b_norm[index:index + 2] for index in range(max(1, len(b_norm) - 1))}
    bigram = len(a_bigrams & b_bigrams) / max(1, len(a_bigrams | b_bigrams))
    return 0.72 * sequence + 0.28 * bigram


def repeated_segment_candidates(
    segments: list[dict[str, Any]],
    window: float,
    *,
    repeat_window: float = 60.0,
    max_span_segments: int = 3,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    usable = [
        seg for seg in segments
        if seg.get("start") is not None and seg.get("end") is not None and clean_for_match(str(seg.get("text") or ""))
    ]
    spans: list[dict[str, Any]] = []
    max_span_segments = max(1, max_span_segments)
    for start_index in range(len(usable)):
        for count in range(1, max_span_segments + 1):
            end_index = start_index + count
            if end_index > len(usable):
                break
            group = usable[start_index:end_index]
            start = float(group[0]["start"])
            end = float(group[-1]["end"])
            text = "".join(str(segment.get("text") or "") for segment in group).strip()
            normalized = clean_for_match(text)
            if len(normalized) < 4 or end - start > 20.0:
                continue
            spans.append({
                "start_index": start_index,
                "end_index": end_index,
                "start": start,
                "end": end,
                "text": text,
                "normalized": normalized,
            })

    spans.sort(key=lambda item: (item["start"], item["end"]))
    for right_index, right in enumerate(spans):
        for left_index in range(right_index - 1, -1, -1):
            left = spans[left_index]
            if right["start"] - left["start"] > repeat_window + 20.0:
                break
            if left["end"] > right["start"] or left["end_index"] > right["start_index"]:
                continue
            gap = right["start"] - left["end"]
            if gap > repeat_window:
                continue
            length_ratio = min(len(left["normalized"]), len(right["normalized"])) / max(
                len(left["normalized"]), len(right["normalized"])
            )
            if length_ratio < 0.55:
                continue
            score = segment_similarity(left["text"], right["text"])
            if score < 0.72:
                continue
            add_candidate(candidates, {
                "id": f"repeat_{len(candidates) + 1:03d}",
                "type": "near_duplicate_segment",
                "start": left["start"],
                "end": left["end"],
                "removed_text": left["text"],
                "kept_text": right["text"],
                "kept_start": right["start"],
                "kept_end": right["end"],
                "removal_options": [
                    {
                        "label": "earlier_take",
                        "start_ms": round(left["start"] * 1000.0),
                        "end_ms": round(left["end"] * 1000.0),
                        "text": left["text"],
                    },
                    {
                        "label": "later_take",
                        "start_ms": round(right["start"] * 1000.0),
                        "end_ms": round(right["end"] * 1000.0),
                        "text": right["text"],
                    },
                ],
                "similarity": round(score, 3),
                "context": transcript_context(segments, left["start"], right["end"], window),
            })

    # Prefer the strongest, widest explanation of the same retake pair and
    # suppress heavily-overlapping shorter variants produced by span search.
    selected: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-float(item.get("similarity", 0)), -float(item["duration_ms"])),
    ):
        if any(
            min(candidate["end_ms"], kept["end_ms"]) - max(candidate["start_ms"], kept["start_ms"])
            > 0.7 * min(candidate["duration_ms"], kept["duration_ms"])
            for kept in selected
        ):
            continue
        selected.append(candidate)
    return selected


def sparse_retake_candidates(
    segments: list[dict[str, Any]],
    window: float,
    *,
    retake_window: float = 120.0,
    min_gap_s: float = 8.0,
    max_spoken_s: float = 45.0,
    max_speech_density: float = 0.45,
) -> list[dict[str, Any]]:
    """Find a short explanation followed by a long failed demo and clean restart.

    Ordinary near-duplicate search intentionally uses a high text threshold and
    removes only the repeated sentence.  Screen recordings also contain sparse
    failed takes: a sentence, tens of seconds of clicking/waiting, then a
    paraphrased restart.  These become multimodal candidates for the *whole*
    earlier range, but never deterministic cuts on text similarity alone.
    """
    usable = [
        segment for segment in segments
        if segment.get("start") is not None and segment.get("end") is not None
        and len(clean_for_match(str(segment.get("text") or ""))) >= 8
    ]
    raw: list[dict[str, Any]] = []
    for right_index, right in enumerate(usable):
        right_start = float(right["start"])
        for left_index in range(right_index - 1, -1, -1):
            left = usable[left_index]
            left_start = float(left["start"])
            left_end = float(left["end"])
            total_span = right_start - left_start
            if total_span > retake_window:
                break
            gap = right_start - left_end
            if gap < min_gap_s:
                continue
            left_text = str(left.get("text") or "")
            right_text = str(right.get("text") or "")
            left_norm = clean_for_match(left_text)
            right_norm = clean_for_match(right_text)
            length_ratio = min(len(left_norm), len(right_norm)) / max(
                len(left_norm), len(right_norm)
            )
            if length_ratio < 0.45:
                continue
            similarity = segment_similarity(left_text, right_text)
            if similarity < 0.42 or similarity >= 0.72:
                continue
            removed_segments = usable[left_index:right_index]
            spoken_s = sum(
                max(0.0, float(item["end"]) - float(item["start"]))
                for item in removed_segments
            )
            if spoken_s > max_spoken_s:
                continue
            speech_density = spoken_s / max(0.001, total_span)
            if speech_density > max_speech_density:
                continue
            raw.append({
                "type": "possible_sparse_retake",
                "start": left_start,
                "end": right_start,
                "removed_text": "".join(
                    str(item.get("text") or "").strip() for item in removed_segments
                ),
                "kept_text": right_text.strip(),
                "kept_start": right_start,
                "kept_end": float(right["end"]),
                "similarity": round(similarity, 3),
                "spoken_duration_s": round(spoken_s, 3),
                "speech_density": round(speech_density, 3),
                "context": transcript_context(
                    segments, left_start, float(right["end"]), window
                ),
            })

    selected: list[dict[str, Any]] = []
    for candidate in sorted(
        raw,
        key=lambda item: (-float(item["similarity"]), -(item["end"] - item["start"])),
    ):
        if any(
            max(0.0, min(candidate["end"], kept["end"]) - max(candidate["start"], kept["start"]))
            > 0.7 * min(candidate["end"] - candidate["start"], kept["end"] - kept["start"])
            or abs(candidate["kept_start"] - kept["kept_start"]) <= 2.0
            for kept in selected
        ):
            continue
        selected.append(candidate)
    candidates: list[dict[str, Any]] = []
    for item in sorted(selected, key=lambda candidate: candidate["start"]):
        item = dict(item)
        item["id"] = f"sparse_{len(candidates) + 1:03d}"
        add_candidate(candidates, item)
    return candidates


REPAIR_MARKERS = {"不对", "不是", "应该", "重新", "再来", "我的意思是", "也就是说"}
RESTART_PREFIXES = (
    "然后", "那", "接下来", "所以", "重新", "我重新", "我们重新",
    "换句话说", "再来", "现在", "另外", "这里",
)


def repair_marker_candidates(words: list[dict[str, Any]], segments: list[dict[str, Any]], window: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        marker = clean_for_match(word_text(word))
        if marker not in REPAIR_MARKERS or index < 1 or index + 1 >= len(words):
            continue
        before = words[max(0, index - 8):index]
        after = words[index + 1:min(len(words), index + 9)]
        if not before or not after:
            continue
        nearby = before + [word] + after
        max_internal_gap_ms = max(
            (
                max(0.0, float(current["start"]) - float(previous["end"])) * 1000.0
                for previous, current in zip(nearby, nearby[1:])
            ),
            default=0.0,
        )
        start = float(before[0]["start"])
        end = float(word["end"]) + 0.06
        add_candidate(candidates, {
            "id": f"repair_{len(candidates) + 1:03d}",
            "type": "explicit_self_correction",
            "start": start,
            "end": end,
            "removed_text": candidate_text(words, start, end),
            "kept_text": "".join(word_text(item) for item in after),
            # Marker search is useful for recall, but discourse phrases such
            # as “也就是说” are not safe evidence that speech was abandoned.
            # Keep all marker-only candidates advisory at the auto-cut layer.
            "repair_marker": marker,
            "auto_safe": False,
            "max_internal_gap_ms": round(max_internal_gap_ms),
            "context": transcript_context(segments, start, float(after[-1]["end"]), window),
        })
    return candidates


def immediate_repair_candidates(
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    window: float,
) -> list[dict[str, Any]]:
    """Find a phrase immediately reformulated after a paused ``就是`` restart."""
    candidates: list[dict[str, Any]] = []
    for index, marker_word in enumerate(words):
        if clean_for_match(word_text(marker_word)) != "就是" or index < 3:
            continue
        if index + 3 >= len(words):
            continue
        pause_ms = max(
            0.0,
            (float(marker_word["start"]) - float(words[index - 1]["end"])) * 1000.0,
        )
        if pause_ms < 300.0:
            continue
        best: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None
        for left_count in range(3, min(8, index) + 1):
            left = words[index - left_count:index]
            left_text = "".join(word_text(item) for item in left)
            for right_count in range(3, min(8, len(words) - index - 1) + 1):
                right = words[index + 1:index + 1 + right_count]
                right_text = "".join(word_text(item) for item in right)
                length_ratio = min(
                    len(clean_for_match(left_text)), len(clean_for_match(right_text))
                ) / max(len(clean_for_match(left_text)), len(clean_for_match(right_text)))
                if length_ratio < 0.55:
                    continue
                similarity = segment_similarity(left_text, right_text)
                if similarity >= 0.52 and (best is None or similarity > best[0]):
                    best = (similarity, left, right)
        if best is None:
            continue
        similarity, left, right = best
        left_start_index = index - len(left)
        while (
            left_start_index > 0
            and index - left_start_index < 10
            and clean_for_match(word_text(words[left_start_index - 1]))
            in {"就", "这个", "然后", "那"}
        ):
            left_start_index -= 1
            left = [words[left_start_index]] + left
        start = float(left[0]["start"])
        end = float(marker_word["start"])
        add_candidate(candidates, {
            "id": f"immediate_{len(candidates) + 1:03d}",
            "type": "possible_immediate_repair",
            "start": start,
            "end": end,
            "removed_text": candidate_text(words, start, end),
            "kept_text": word_text(marker_word) + "".join(word_text(item) for item in right),
            "similarity": round(similarity, 3),
            "restart_pause_ms": round(pause_ms),
            "context": transcript_context(segments, start, float(right[-1]["end"]), window),
        })
    return candidates


def tail_restart_candidates(
    segments: list[dict[str, Any]],
    window: float,
    *,
    max_gap_s: float = 12.0,
) -> list[dict[str, Any]]:
    """Find a repeated sentence tail before a nearby cleaner restart."""
    usable = [
        segment for segment in segments
        if segment.get("start") is not None and segment.get("end") is not None
        and len(segment.get("words") or []) >= 3
    ]
    raw: list[dict[str, Any]] = []
    for left_index, left_segment in enumerate(usable):
        left_words = [item for item in left_segment.get("words") or [] if word_text(item)]
        for right_index in range(left_index + 1, len(usable)):
            right_segment = usable[right_index]
            gap = float(right_segment["start"]) - float(left_segment["end"])
            if gap > max_gap_s:
                break
            if gap < 0.4:
                continue
            right_words = [item for item in right_segment.get("words") or [] if word_text(item)]
            if len(right_words) < 3:
                continue
            best: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None
            for left_count in range(3, min(10, len(left_words)) + 1):
                suffix = left_words[-left_count:]
                suffix_text = "".join(word_text(item) for item in suffix)
                for right_count in range(3, min(10, len(right_words)) + 1):
                    prefix = right_words[:right_count]
                    prefix_text = "".join(word_text(item) for item in prefix)
                    length_ratio = min(
                        len(clean_for_match(suffix_text)), len(clean_for_match(prefix_text))
                    ) / max(len(clean_for_match(suffix_text)), len(clean_for_match(prefix_text)))
                    if length_ratio < 0.55:
                        continue
                    similarity = segment_similarity(suffix_text, prefix_text)
                    if similarity >= 0.72 and (best is None or similarity > best[0]):
                        best = (similarity, suffix, prefix)
            if best is None:
                continue
            similarity, suffix, prefix = best
            start = float(suffix[0]["start"])
            end = float(right_segment["start"])
            intervening = usable[left_index + 1:right_index]
            removed_text = "".join(word_text(item) for item in suffix) + "".join(
                str(item.get("text") or "").strip() for item in intervening
            )
            raw.append({
                "type": "possible_tail_restart",
                "start": start,
                "end": end,
                "removed_text": removed_text,
                "kept_text": "".join(word_text(item) for item in prefix),
                "similarity": round(similarity, 3),
                "kept_start": float(right_segment["start"]),
                "kept_end": float(prefix[-1]["end"]),
                "context": transcript_context(
                    segments, start, float(prefix[-1]["end"]), window
                ),
            })
    selected: list[dict[str, Any]] = []
    for item in sorted(raw, key=lambda candidate: (-candidate["similarity"], candidate["start"])):
        if any(
            max(0.0, min(item["end"], kept["end"]) - max(item["start"], kept["start"]))
            > 0.7 * min(item["end"] - item["start"], kept["end"] - kept["start"])
            for kept in selected
        ):
            continue
        selected.append(item)
    candidates: list[dict[str, Any]] = []
    for item in sorted(selected, key=lambda candidate: candidate["start"]):
        item = dict(item)
        item["id"] = f"tail_{len(candidates) + 1:03d}"
        add_candidate(candidates, item)
    return candidates


def candidate_priority(item: dict[str, Any]) -> float:
    type_weight = {
        "global_paper_edit": 4.2,
        "near_duplicate_segment": 4.0,
        "possible_sparse_retake": 3.9,
        "possible_abandoned_sentence": 3.8,
        "possible_isolated_take": 3.7,
        "possible_tail_restart": 3.7,
        "possible_immediate_repair": 3.6,
        "possible_false_start": 3.5,
        "protected_pause": 3.2,
        "explicit_self_correction": 3.4,
        "filler_cluster": 2.8,
        "hard_filler": 2.4,
        "soft_filler": 1.4,
    }
    removed = clean_for_match(str(item.get("removed_text") or ""))
    # Prioritize the hesitation sounds users most often want removed. “啊” is
    # intentionally excluded because it is frequently a natural sentence
    # particle in Mandarin rather than a disfluency.
    hesitation_bonus = 1.4 if any(token in removed for token in ("呃", "嗯", "额", "um", "uh", "hmm")) else 0.0
    return (
        type_weight.get(str(item.get("type")), 1.0)
        + float(item.get("similarity", 0.0))
        + hesitation_bonus
    )


def select_timeline_balanced_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep strong candidates across the whole recording, not just its beginning."""
    if len(candidates) <= limit:
        return sorted(candidates, key=lambda item: (item["start_ms"], item["end_ms"]))
    buckets: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        buckets.setdefault(int(float(candidate["start_ms"]) // 60_000), []).append(candidate)
    for rows in buckets.values():
        rows.sort(key=lambda item: (-candidate_priority(item), item["start_ms"]))
    selected: list[dict[str, Any]] = []
    bucket_ids = sorted(buckets)
    while len(selected) < limit and any(buckets[bucket] for bucket in bucket_ids):
        for bucket in bucket_ids:
            if buckets[bucket] and len(selected) < limit:
                selected.append(buckets[bucket].pop(0))
    return sorted(selected, key=lambda item: (item["start_ms"], item["end_ms"]))


def false_start_candidates(words: list[dict[str, Any]], segments: list[dict[str, Any]], window: float) -> list[dict[str, Any]]:
    candidates = []
    for i in range(len(words) - 3):
        text = word_text(words[i])
        if not is_standalone_filler(text):
            continue
        pre_start = max(0, i - 6)
        pre_words = words[pre_start:i]
        post_words = words[i + 1:i + 8]
        pre_text = "".join(word_text(w) for w in pre_words)
        post_text = "".join(word_text(w) for w in post_words)
        if len(clean_for_match(pre_text)) < 3 or len(clean_for_match(post_text)) < 3:
            continue
        sim = segment_similarity(pre_text, post_text)
        if sim < 0.38:
            continue
        start = float(pre_words[0]["start"])
        end = float(words[i]["end"]) + 0.08
        add_candidate(candidates, {
            "id": f"false_{len(candidates) + 1:03d}",
            "type": "possible_false_start",
            "start": start,
            "end": end,
            "removed_text": candidate_text(words, start, end),
            "kept_text": post_text,
            "similarity": round(sim, 3),
            "context": transcript_context(segments, start, float(post_words[-1]["end"]), window),
        })
    return candidates


def abandoned_sentence_candidates(
    segments: list[dict[str, Any]], window: float
) -> list[dict[str, Any]]:
    """Find a sentence that trails off before a later restart.

    Retakes often do not repeat enough words for similarity search. A common
    signal is a substantial utterance ending in a standalone filler, followed
    by a pause and a new sentence. Very short filler-only stubs such as “就是”
    are included too, but remain advisory until a multimodal reviewer confirms
    that no unique screen action would be lost.
    """
    usable = [
        seg for seg in segments
        if seg.get("start") is not None and seg.get("end") is not None
    ]
    candidates: list[dict[str, Any]] = []
    for index in range(len(usable) - 1):
        current = usable[index]
        following = usable[index + 1]
        start = float(current["start"])
        spoken_end = float(current["end"])
        next_start = float(following["start"])
        gap = next_start - spoken_end
        if gap < 0.6 or gap > 25.0:
            continue
        text = str(current.get("text") or "").strip()
        normalized = clean_for_match(text)
        if not normalized:
            continue
        words = [item for item in current.get("words") or [] if word_text(item)]
        last_word = word_text(words[-1]) if words else normalized[-1:]
        trails_off = is_standalone_filler(last_word)
        filler_stub = len(normalized) <= 5 and (
            is_standalone_filler(text) or is_soft_filler(text)
        )
        if not trails_off and not filler_stub:
            continue
        following_normalized = clean_for_match(str(following.get("text") or ""))
        restart_evidence = any(
            following_normalized.startswith(prefix) for prefix in RESTART_PREFIXES
        )
        # ASR often splits a single grammatical sentence at a hesitation. For
        # example “线程可能它的呃。/大小已经…” must not become a whole-sentence
        # cut because the second segment depends on the first segment's subject.
        # A tiny abandoned stub plus a long dead gap is independently strong.
        long_stub = filler_stub and gap >= 1.5
        if not restart_evidence and not long_stub:
            continue
        candidate_start = start
        removed_segments = [current]
        if filler_stub and index > 0:
            previous = usable[index - 1]
            if start - float(previous["end"]) <= 1.2:
                # “完整问题……就是。<长停顿>重新问一遍” is one failed
                # take, not merely the tiny trailing stub.
                candidate_start = float(previous["start"])
                removed_segments = [previous, current]
        elif trails_off:
            # If the abandoned sentence belongs to a short spoken island after
            # a large setup/wait gap, include that whole failed take. This
            # catches “解释一遍→说乱→停住→完整重讲” without scanning the full video.
            group_start = index
            while group_start > 0:
                boundary_gap = float(usable[group_start]["start"]) - float(
                    usable[group_start - 1]["end"]
                )
                if boundary_gap >= 5.0:
                    boundary_start = float(usable[group_start - 1]["end"])
                    if next_start - boundary_start <= 45.0:
                        candidate_start = boundary_start
                        removed_segments = usable[group_start:index + 1]
                    break
                if boundary_gap > 1.2:
                    break
                group_start -= 1
        add_candidate(candidates, {
            "id": f"abandon_{len(candidates) + 1:03d}",
            "type": "possible_abandoned_sentence",
            "start": candidate_start,
            # Include the dead gap so an approved cut joins directly to the
            # clean restart. Local waveform refinement still owns the splice.
            "end": next_start,
            "spoken_end": spoken_end,
            "spoken_start": float(removed_segments[0]["start"]),
            "removed_text": "".join(
                str(segment.get("text") or "").strip() for segment in removed_segments
            ),
            "kept_text": str(following.get("text") or "").strip(),
            "restart_evidence": restart_evidence,
            "context": transcript_context(segments, start, float(following["end"]), window),
        })
    return candidates


def isolated_take_candidates(
    segments: list[dict[str, Any]],
    window: float,
    semantic_candidates: list[dict[str, Any]],
    *,
    gap_s: float = 2.5,
    max_spoken_s: float = 25.0,
    max_total_s: float = 45.0,
) -> list[dict[str, Any]]:
    """Find short spoken islands bounded by two conspicuous pauses.

    These islands are high-recall candidates, not automatic cuts. A clean
    retake commonly appears as ``long gap → failed explanation → long gap →
    restart`` even when the wording is too different for string matching.
    """
    usable = [
        segment for segment in segments
        if segment.get("start") is not None and segment.get("end") is not None
        and clean_for_match(str(segment.get("text") or ""))
    ]
    gaps = [
        (index, float(usable[index + 1]["start"]) - float(usable[index]["end"]))
        for index in range(len(usable) - 1)
        if float(usable[index + 1]["start"]) - float(usable[index]["end"]) >= gap_s
    ]
    candidates: list[dict[str, Any]] = []
    for (left_index, _), (right_index, _) in zip(gaps, gaps[1:]):
        group = usable[left_index + 1:right_index + 1]
        if not group:
            continue
        start = float(usable[left_index]["end"])
        end = float(usable[right_index + 1]["start"])
        spoken_duration = float(group[-1]["end"]) - float(group[0]["start"])
        if spoken_duration > max_spoken_s or end - start > max_total_s:
            continue
        # Prefer a more specific abandoned-sentence candidate when the two
        # ranges describe substantially the same failed take.
        if any(
            max(0.0, min(end * 1000.0, float(item["end_ms"])) - max(start * 1000.0, float(item["start_ms"])))
            > 0.7 * min((end - start) * 1000.0, float(item["duration_ms"]))
            for item in semantic_candidates
            if item.get("type") == "possible_abandoned_sentence"
        ):
            continue
        removed_text = "".join(str(item.get("text") or "").strip() for item in group)
        following = usable[right_index + 1]
        restart_similarity = segment_similarity(
            removed_text, str(following.get("text") or "")
        )
        supporting_repeat = next((
            item for item in semantic_candidates
            if item.get("type") == "near_duplicate_segment"
            and abs(float(item["end_ms"]) - start * 1000.0) <= 600.0
            and float(item.get("kept_start", 0.0)) >= end - 0.8
        ), None)
        absorbed_repeat_id = None
        if supporting_repeat and restart_similarity >= 0.10:
            # Treat the earlier duplicate plus the intervening failed material
            # as one coherent take. Reviewing them separately made the model
            # keep the wrong half of a failed screen-demo attempt.
            start = float(supporting_repeat["start_ms"]) / 1000.0
            removed_text = (
                str(supporting_repeat.get("removed_text") or "") + removed_text
            )
            absorbed_repeat_id = str(supporting_repeat.get("id") or "")
        add_candidate(candidates, {
            "id": f"island_{len(candidates) + 1:03d}",
            "type": "possible_isolated_take",
            "start": start,
            "end": end,
            "removed_text": removed_text,
            "kept_text": str(following.get("text") or "").strip(),
            "spoken_start": float(group[0]["start"]),
            "spoken_end": float(group[-1]["end"]),
            "restart_similarity": round(restart_similarity, 3),
            "supporting_repeat_id": (
                str(supporting_repeat.get("id")) if supporting_repeat else None
            ),
            "absorbed_repeat_id": absorbed_repeat_id,
            "context": transcript_context(segments, start, float(following["end"]), window),
        })
    return candidates


def protected_pause_candidates(
    report_path: Path | None,
    segments: list[dict[str, Any]],
    window: float,
    semantic_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not report_path:
        return []
    if not report_path.exists():
        fail(f"activity report does not exist: {report_path}")
    data = json.loads(report_path.read_text(encoding="utf-8"))
    raw = [
        dict(item) for item in data.get("pauses_protected_by_activity") or []
        if isinstance(item, dict) and item.get("start_ms") is not None and item.get("end_ms") is not None
    ]
    raw.sort(key=lambda item: (float(item["start_ms"]), float(item["end_ms"])))
    merged: list[dict[str, Any]] = []
    for item in raw:
        if (
            merged
            and item.get("text_before") == merged[-1].get("text_before")
            and item.get("text_after") == merged[-1].get("text_after")
            and float(item["start_ms"]) - float(merged[-1]["end_ms"]) <= 800.0
        ):
            merged[-1]["end_ms"] = max(float(merged[-1]["end_ms"]), float(item["end_ms"]))
            continue
        merged.append(item)

    candidates: list[dict[str, Any]] = []
    for item in merged:
        start_ms = float(item["start_ms"])
        end_ms = float(item["end_ms"])
        duration_ms = end_ms - start_ms
        if duration_ms < 300.0:
            continue
        # A semantic retake candidate already carries the dead gap; avoid
        # duplicate model calls and overlapping cuts.
        if any(
            max(0.0, min(end_ms, float(candidate["end_ms"])) - max(start_ms, float(candidate["start_ms"])))
            > 0.8 * duration_ms
            for candidate in semantic_candidates
            if candidate.get("type") != "possible_isolated_take"
        ):
            continue
        start = start_ms / 1000.0
        end = end_ms / 1000.0
        add_candidate(candidates, {
            "id": f"active_pause_{len(candidates) + 1:03d}",
            "type": "protected_pause",
            "start": start,
            "end": end,
            "removed_text": "[dead air with detected screen/input activity]",
            "text_before": item.get("text_before", ""),
            "text_after": item.get("text_after", ""),
            "detected_activity_overlap_ms": item.get("activity_overlap_ms"),
            "context": transcript_context(segments, start, end, window),
        })
    return candidates


def load_extra_candidates(
    paths: list[Path],
    segments: list[dict[str, Any]],
    context_window: float,
    expected_transcript: Path | None = None,
) -> list[dict[str, Any]]:
    """Load bounded planner suggestions without trusting them as decisions."""
    loaded: list[dict[str, Any]] = []
    expected = expected_transcript.expanduser().resolve() if expected_transcript else None
    for source_index, path in enumerate(paths, start=1):
        if not path.exists():
            fail(f"extra candidate report does not exist: {path}")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"invalid extra candidate report {path}: {exc}")
        reported_transcript = report.get("transcript") if isinstance(report, dict) else None
        if expected is not None and reported_transcript:
            if Path(str(reported_transcript)).expanduser().resolve() != expected:
                fail(f"extra candidate transcript mismatch: {path}")
        candidates = report.get("candidates") if isinstance(report, dict) else None
        if not isinstance(candidates, list):
            fail(f"extra candidate report has no candidates list: {path}")
        for item_index, raw in enumerate(candidates, start=1):
            if not isinstance(raw, dict) or raw.get("type") != "global_paper_edit":
                continue
            try:
                start = float(raw.get("start"))
                end = float(raw.get("end"))
            except (TypeError, ValueError):
                continue
            if start < 0.0 or end <= start or end - start > 120.0:
                continue
            item = dict(raw)
            item["planner_candidate_id"] = str(raw.get("id") or "")
            item["id"] = f"global{source_index}_{item_index:03d}"
            item["start"] = start
            item["end"] = end
            item["start_ms"] = round(start * 1000.0)
            item["end_ms"] = round(end * 1000.0)
            item["duration_ms"] = round((end - start) * 1000.0)
            item["context"] = transcript_context(
                segments, start, end, max(context_window, 8.0)
            )
            loaded.append(item)
    return loaded


def build_candidates(args: argparse.Namespace, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words = flatten_words(segments)
    candidates = []
    candidates.extend(group_nearby_fillers(words, segments, args.context_window))
    candidates.extend(false_start_candidates(words, segments, args.context_window))
    candidates.extend(repair_marker_candidates(words, segments, args.context_window))
    candidates.extend(immediate_repair_candidates(words, segments, args.context_window))
    candidates.extend(tail_restart_candidates(segments, args.context_window))
    abandoned = abandoned_sentence_candidates(segments, args.context_window)
    candidates.extend(abandoned)
    repeats = repeated_segment_candidates(
        segments,
        args.context_window,
        repeat_window=args.repeat_window,
        max_span_segments=args.repeat_span_segments,
    )
    candidates.extend(repeats)
    candidates.extend(sparse_retake_candidates(
        segments,
        args.context_window,
        retake_window=getattr(args, "sparse_retake_window", 120.0),
    ))
    islands = isolated_take_candidates(
        segments, args.context_window, abandoned + repeats
    )
    absorbed_repeat_ids = {
        str(item.get("absorbed_repeat_id")) for item in islands
        if item.get("absorbed_repeat_id")
    }
    if absorbed_repeat_ids:
        candidates = [
            item for item in candidates
            if str(item.get("id")) not in absorbed_repeat_ids
        ]
    candidates.extend(islands)
    # Do not pay to review a broad sparse retake when a more precise isolated
    # or abandoned candidate already covers almost all of it. Long sparse
    # candidates that bridge several smaller takes remain available.
    precise_semantic = abandoned + islands
    candidates = [
        item for item in candidates
        if item.get("type") != "possible_sparse_retake"
        or not any(
            max(
                0.0,
                min(float(item["end_ms"]), float(other["end_ms"]))
                - max(float(item["start_ms"]), float(other["start_ms"])),
            ) > 0.8 * float(item["duration_ms"])
            for other in precise_semantic
        )
    ]
    semantic_candidates = [
        item for item in candidates
        if item.get("type") not in {"hard_filler", "soft_filler", "filler_cluster"}
    ]
    candidates.extend(protected_pause_candidates(
        getattr(args, "activity_report", None),
        segments,
        args.context_window,
        semantic_candidates,
    ))
    candidates.extend(load_extra_candidates(
        getattr(args, "extra_candidates", []),
        segments,
        args.context_window,
        getattr(args, "transcript", None),
    ))

    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda c: (c["start_ms"], c["end_ms"], c["type"])):
        key = (round(item["start_ms"] / 120), round(item["end_ms"] / 120), item["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if not getattr(args, "include_all_fillers", False):
        # Mandarin discourse words such as “这个/然后/其实” are often fully
        # grammatical. Reviewing every occurrence is expensive and invited an
        # over-eager model to remove useful connectors. The default talking-head
        # path focuses on the user's real nuisance words (呃/嗯/额); the broader
        # diagnostic mode remains available explicitly.
        deduped = [
            item for item in deduped
            if item.get("type") not in {"hard_filler", "soft_filler", "filler_cluster"}
            or bool(item.get("contains_strong_hesitation"))
        ]

    return select_timeline_balanced_candidates(deduped, max(1, args.max_candidates))


def ffprobe_duration(video: Path) -> float:
    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])
    return max(0.1, float(result.stdout.strip()))


def extract_frame(video: Path, timestamp: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg",
        "-y",
        "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", "scale='min(960,iw)':-2",
        "-q:v", "3",
        str(output),
    ])


def attach_frames(args: argparse.Namespace, candidates: list[dict[str, Any]]) -> None:
    if not args.video:
        return
    if not args.video.exists():
        fail(f"video does not exist: {args.video}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        fail("ffmpeg and ffprobe are required when --video is provided.")

    duration = ffprobe_duration(args.video)
    frame_dir = args.work_dir / "frames"
    for item in candidates:
        mid = (float(item["start"]) + float(item["end"])) / 2.0
        stamps = [
            ("before", max(0.0, mid - args.frame_window)),
            ("middle", min(duration, mid)),
            ("after", min(duration, mid + args.frame_window)),
        ]
        frames = []
        for label, stamp in stamps:
            path = frame_dir / item["id"] / f"{label}_{stamp:.2f}.jpg"
            extract_frame(args.video, stamp, path)
            frames.append({"label": label, "timestamp": round(stamp, 3), "path": str(path)})
        item["frames"] = frames


def extract_clip(
    video: Path,
    start: float,
    end: float,
    output: Path,
    *,
    audio: Path | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.2, end - start)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(video),
    ]
    if audio:
        command.extend([
            "-ss", f"{max(0.0, start):.3f}",
            "-i", str(audio),
        ])
    command.extend([
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0" if audio else "0:a:0?",
        # Screen tutorials rarely need cinematic frame rates. This is sharp
        # enough to inspect UI changes while keeping Base64 clips below the
        # Bailian 10 MB encoded-file limit and reducing visual-token cost.
        "-vf", "scale='min(720,iw)':-2,fps=8",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
        str(output),
    ])
    run(command)


def attach_clips(args: argparse.Namespace, candidates: list[dict[str, Any]]) -> None:
    """Attach short, continuous audio/video evidence for native review."""
    if not args.video:
        return
    if not args.video.exists():
        fail(f"video does not exist: {args.video}")
    if args.audio and not args.audio.exists():
        fail(f"audio does not exist: {args.audio}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        fail("ffmpeg and ffprobe are required when --video is provided.")
    duration = ffprobe_duration(args.video)
    if args.audio:
        duration = min(duration, ffprobe_duration(args.audio))
    clip_dir = args.work_dir / "clips"
    for item in candidates:
        ranges = [(float(item["start"]), float(item["end"]), "remove")]
        if item.get("kept_start") is not None and item.get("kept_end") is not None:
            ranges.append((float(item["kept_start"]), float(item["kept_end"]), "keep"))
        clips = []
        for start, end, label in ranges:
            clip_start = max(0.0, start - args.clip_context)
            clip_end = min(duration, end + args.clip_context)
            path = clip_dir / item["id"] / f"{label}_{clip_start:.2f}_{clip_end:.2f}.mp4"
            extract_clip(args.video, clip_start, clip_end, path, audio=args.audio)
            clips.append({
                "label": label,
                "start": round(clip_start, 3),
                "end": round(clip_end, 3),
                "path": str(path),
            })
        item["clips"] = clips


def image_to_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_messages(candidates: list[dict[str, Any]], *, video_supplied: bool) -> list[dict[str, Any]]:
    system_text = (
        "You are a meticulous short-form tutorial video editor. "
        "Decide which candidate ranges should be cut from a spoken screen recording. "
        "Protect any range that contains a meaningful click, UI transition, generated result, command, or necessary explanation. "
        "Return strict JSON only."
    )
    schema = {
        "decisions": [
            {
                "id": "candidate id",
                "decision": "cut | review | keep",
                "confidence": "high | medium | low",
                "start_ms": 0,
                "end_ms": 0,
                "removed_text": "",
                "reason": "filler | false_start | duplicate | pacing | visual_action | unclear",
                "heard_summary": "what is actually heard in the evidence",
                "screen_action": "none | redundant | meaningful | unclear",
                "visual_assessment": "brief description of screen changes",
                "unique_information_lost": ["claims/examples/numbers lost by cutting"],
                "splice_assessment": "whether the kept speech remains grammatical and complete",
                "note": "brief explanation",
            }
        ],
        "summary": "",
    }
    user_text = f"""
Review these edit candidates for a Chinese short-form screen-recording tutorial.

Rules:
- Cut high-confidence standalone fillers, abandoned starts, and duplicate takes.
- A later summary or conclusion is not proof that a detailed earlier explanation is redundant. Before cutting, enumerate every claim, example, number, warning, and troubleshooting detail in the candidate; if the later take omits any of them, keep it.
- Test the exact spoken splice. Correction words such as “应该”, “也就是说”, or “就是” are not evidence by themselves. If the words before start_ms plus the words after end_ms are incomplete, ungrammatical, or change the argument, keep/review.
- A grammatical demonstrative or connector is not a filler merely because it is marked soft_filler. Never cut phrases such as “这个 GPT”, “然后现在”, “比方说这个”, “其实…”, or “…的话” when the word contributes to the sentence.
- For possible_abandoned_sentence, test the splice grammatically. If the following kept_text begins with a dependent fragment (for example “大小…” still needs the subject before “呃”), keep/review the whole candidate; a nested strong-hesitation candidate can remove only “呃”.
- possible_isolated_take is a high-recall structural hint, not proof. Cut it only when the clip clearly contains an aborted/redundant take and the following speech keeps the intended information.
- possible_sparse_retake spans an earlier explanation plus a long screen-demo/wait before a paraphrased restart. Cut the whole range only when the later take repeats the intended information and every intervening action/result is part of the failed take; otherwise keep it. A long duration alone is never evidence to cut.
- possible_tail_restart and possible_immediate_repair are narrow word-level repeats. Prefer the later clean wording, but cut only the proposed earlier tail; do not widen the range into the preceding complete explanation.
- global_paper_edit comes from a separate full-transcript reasoning pass. It is a high-recall hypothesis, not proof. Compare the remove and keep evidence in the clips, require a clean grammatical splice, and cut only when the replacement fully preserves the intended information.
- protected_pause was already confirmed as microphone silence but overlaps detected screen/input activity. Cut it only when that activity is setup, waiting, navigation, or typing that is unnecessary in the final tutorial. In that case screen_action must be "redundant". If the clip reveals a unique result or necessary transition, keep it as "meaningful".
- For protected_pause, a click, dropdown opening, tab switch, or navigation is NOT automatically meaningful. If narration resumes with the final state/result already visible, the transition is redundant and may be cut. Keep only when watching the intermediate action itself teaches something that the post-cut end state cannot convey.
- Duplicate candidates may provide two removal_options. Choose exactly ONE option (the worse/redundant take), return boundaries inside that option, and never cut both takes.
- Keep or mark review if the screen likely changes during the range.
- Keep if the repeated wording adds context, reasoning, an example, a number, a warning, a result, or troubleshooting detail.
- Do not expand a cut beyond the candidate's provided range or chosen removal_option.
- Prefer "review" over "cut" when visual evidence is unclear.
- Audio/visual evidence is {"provided for each candidate" if video_supplied else "not provided; rely only on transcript and be conservative"}.
- When clips are provided, actually listen for fillers, abandoned speech, and restarts; do not merely repeat the transcript.
- Set screen_action to "meaningful" if the candidate uniquely contains a click, command, file change, generated result, or necessary UI transition. An intermediate click/navigation is redundant when the same final state remains visible after the cut and watching the transition itself teaches nothing.
- If a candidate includes detected_input_activity_overlap_ms, a click/keystroke is known to occur even if it is visually subtle. screen_action cannot be "none": choose "redundant" when the final state remains, otherwise "meaningful".
- Keep decision, screen_action, and note logically consistent. Never return decision="cut" while the note says the range should be kept.

Return JSON in this exact shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Candidates:
{json.dumps([{k: v for k, v in c.items() if k not in {"frames", "clips"}} for c in candidates], ensure_ascii=False, indent=2)}
"""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for candidate in candidates:
        for frame in candidate.get("frames") or []:
            content.append({"type": "text", "text": f"{candidate['id']} frame={frame['label']} timestamp={frame['timestamp']}"})
            content.append({"type": "image_url", "image_url": {"url": image_to_data_uri(Path(frame["path"]))}})
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": content},
    ]


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == "url" and isinstance(child, str) and child.startswith("data:"):
                result[key] = child[:64] + "...[base64 omitted]"
            elif key == "data" and isinstance(child, str) and len(child) > 256:
                result[key] = f"[base64 omitted: {len(child)} chars]"
            else:
                result[key] = redact_payload(child)
        return result
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def retry_delay(attempt: int) -> float:
    return min(2 ** attempt, 8) + attempt * 0.25


def read_urlopen_json(request: urllib.request.Request, timeout: int, *, attempts: int = 3) -> dict[str, Any]:
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            quota_exhausted = exc.code == 429 and any(
                marker in body for marker in ("insufficient_quota", "token-limit")
            )
            if quota_exhausted or exc.code not in RETRYABLE_HTTP_CODES or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {exc.code} from {request.full_url}: {body}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Network error from {request.full_url}: {exc}") from exc
            last_error = exc
        time.sleep(retry_delay(attempt))
    raise RuntimeError(f"Request failed after retries: {last_error}")


def post_json(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    return read_urlopen_json(request, timeout)


def post_json_with_headers(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    return read_urlopen_json(request, timeout)


def read_urlopen_sse(
    request: urllib.request.Request,
    timeout: int,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    """Read an OpenAI-compatible streaming response from Bailian."""
    last_error: BaseException | None = None
    for attempt in range(attempts):
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            parts.append(str(content))
            if parts:
                return {"text": "".join(parts), "usage": usage}
            raise RuntimeError("Bailian stream completed without text output.")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            quota_exhausted = exc.code == 429 and any(
                marker in body for marker in ("insufficient_quota", "token-limit")
            )
            if quota_exhausted or exc.code not in RETRYABLE_HTTP_CODES or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {exc.code} from {request.full_url}: {body}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Streaming error from {request.full_url}: {exc}") from exc
            last_error = exc
        time.sleep(retry_delay(attempt))
    raise RuntimeError(f"Streaming request failed after retries: {last_error}")


def extract_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def placeholder_decisions(candidates: list[dict[str, Any]], note: str) -> dict[str, Any]:
    return {
        "decisions": [
            {
                "id": item["id"],
                "decision": "review",
                "confidence": "low",
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "removed_text": item.get("removed_text", ""),
                "reason": item["type"],
                "screen_action": "unclear",
                "note": note,
            }
            for item in candidates
        ],
        "summary": note,
    }


def complete_decisions(
    parsed: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    require_screen_action: bool = False,
) -> dict[str, Any]:
    """Validate model coverage and conservatively fill missing decisions."""
    by_id = {str(item["id"]): item for item in candidates}
    completed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parsed.get("decisions") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("id") or "")
        candidate = by_id.get(candidate_id)
        if not candidate or candidate_id in seen:
            continue
        decision = dict(raw)
        if decision.get("decision") not in {"cut", "keep", "review"}:
            decision["decision"] = "review"
        if decision.get("confidence") not in {"high", "medium", "low"}:
            decision["confidence"] = "low"
        decision.setdefault("start_ms", candidate["start_ms"])
        decision.setdefault("end_ms", candidate["end_ms"])
        decision.setdefault("removed_text", candidate.get("removed_text", ""))
        note_normalized = clean_for_match(str(decision.get("note") or ""))
        cut_keep_conflict = any(phrase in note_normalized for phrase in (
            "shouldbekept", "shouldalsobekept", "mustbekept", "mustbepreserved",
            "cuttingitwould", "应该保留", "需要保留", "不应该剪", "不能剪",
        ))
        if decision["decision"] == "cut" and cut_keep_conflict:
            decision["decision"] = "review"
            decision["confidence"] = "medium"
            decision["note"] = (
                str(decision.get("note") or "")
                + " Downgraded: decision contradicted its own keep rationale."
            ).strip()
        if (
            require_screen_action
            and decision["decision"] == "cut"
            and decision["confidence"] == "high"
            and decision.get("screen_action") not in {"none", "redundant"}
        ):
            decision["decision"] = "review"
            decision["confidence"] = "medium"
            decision["note"] = (
                str(decision.get("note") or "")
                + " Downgraded: audio/video review did not explicitly clear screen activity."
            ).strip()
        completed.append(decision)
        seen.add(candidate_id)
    for candidate_id, candidate in by_id.items():
        if candidate_id in seen:
            continue
        completed.append({
            "id": candidate_id,
            "decision": "review",
            "confidence": "low",
            "start_ms": candidate["start_ms"],
            "end_ms": candidate["end_ms"],
            "removed_text": candidate.get("removed_text", ""),
            "reason": candidate["type"],
            "screen_action": "unclear",
            "note": "Model omitted this candidate; manual review required.",
        })
    return {**parsed, "decisions": completed}


def candidate_batch_signature(candidates: list[dict[str, Any]]) -> str:
    """Fingerprint decision-relevant candidate identity and timeline bounds."""
    canonical = [
        {
            "id": str(item.get("id") or ""),
            "type": str(item.get("type") or ""),
            "start_ms": round(float(item.get("start_ms", 0.0)), 3),
            "end_ms": round(float(item.get("end_ms", 0.0)), 3),
            "removed_text": str(item.get("removed_text") or ""),
        }
        for item in candidates
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reusable_candidate_matches(
    current: dict[str, Any], previous: dict[str, Any], *, tolerance_ms: float = 2.0
) -> bool:
    return (
        str(current.get("id") or "") == str(previous.get("id") or "")
        and str(current.get("type") or "") == str(previous.get("type") or "")
        and abs(float(current.get("start_ms", 0.0)) - float(previous.get("start_ms", 0.0)))
        <= tolerance_ms
        and abs(float(current.get("end_ms", 0.0)) - float(previous.get("end_ms", 0.0)))
        <= tolerance_ms
        and str(current.get("removed_text") or "") == str(previous.get("removed_text") or "")
    )


def parse_cached_bailian_response(
    response: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    require_screen_action: bool,
) -> dict[str, Any]:
    """Validate a raw response before reusing it for an interrupted batch."""
    expected_ids = {str(item["id"]) for item in candidates}
    if response.get("candidate_signature") != candidate_batch_signature(candidates):
        raise ValueError("Cached response candidate signature does not match this batch.")
    recorded_ids = {str(item) for item in response.get("candidate_ids") or []}
    if recorded_ids and recorded_ids != expected_ids:
        raise ValueError("Cached response candidate_ids do not match this batch.")
    parsed = extract_json_from_text(str(response.get("text") or ""))
    if not isinstance(parsed.get("decisions"), list):
        raise ValueError("Cached Bailian response has no decisions list.")
    response_ids = {
        str(item.get("id") or "")
        for item in parsed["decisions"]
        if isinstance(item, dict)
    }
    if response_ids != expected_ids:
        raise ValueError("Cached Bailian response decisions do not exactly match this batch.")
    parsed["usage"] = response.get("usage")
    return complete_decisions(
        parsed,
        candidates,
        require_screen_action=require_screen_action,
    )


def load_cached_bailian_response(
    path: Path,
    candidates: list[dict[str, Any]],
    *,
    require_screen_action: bool,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        response = json.loads(path.read_text(encoding="utf-8"))
        return parse_cached_bailian_response(
            response,
            candidates,
            require_screen_action=require_screen_action,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def build_bailian_payload(
    args: argparse.Namespace,
    candidates: list[dict[str, Any]],
    *,
    model: str | None = None,
    enable_thinking: bool = False,
    video_fps: float | None = None,
) -> dict[str, Any]:
    messages = build_messages(
        candidates, video_supplied=any(item.get("clips") for item in candidates)
    )
    prompt_text = (
        f"SYSTEM INSTRUCTIONS:\n{messages[0]['content']}\n\n"
        f"{messages[1]['content'][0]['text']}"
    )
    content: list[dict[str, Any]] = []
    for candidate in candidates:
        for clip in candidate.get("clips") or []:
            encoded = base64.b64encode(Path(clip["path"]).read_bytes()).decode("ascii")
            if len(encoded.encode("ascii")) >= BAILIAN_MAX_BASE64_BYTES:
                raise RuntimeError(
                    f"Bailian evidence clip exceeds the 10 MB Base64 limit: {clip['path']}"
                )
            content.append({
                "type": "text",
                "text": (
                    f"Candidate {candidate['id']} {clip['label']} evidence clip. "
                    f"Global timeline {clip['start']:.3f}s to {clip['end']:.3f}s. "
                    "Listen to the microphone and inspect the screen."
                ),
            })
            video_part: dict[str, Any] = {
                "type": "video_url",
                "video_url": {"url": f"data:video/mp4;base64,{encoded}"},
            }
            if video_fps is not None:
                video_part["fps"] = video_fps
            content.append(video_part)
    content.append({"type": "text", "text": prompt_text})
    payload: dict[str, Any] = {
        "model": model or args.bailian_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if enable_thinking:
        payload["enable_thinking"] = True
    else:
        payload["modalities"] = ["text"]
        payload["response_format"] = {"type": "json_object"}
    return payload


def run_bailian_omni(
    args: argparse.Namespace,
    api_key: str,
    candidates: list[dict[str, Any]],
    *,
    batch_index: int = 1,
) -> dict[str, Any]:
    payload = build_bailian_payload(args, candidates)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / f"bailian_omni_request_{batch_index:03d}.redacted.json").write_text(
        json.dumps(redact_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    response_path = args.work_dir / f"bailian_omni_response_{batch_index:03d}.raw.json"
    if args.resume:
        cached = load_cached_bailian_response(
            response_path,
            candidates,
            require_screen_action=any(item.get("clips") for item in candidates),
        )
        if cached is not None:
            log(f"Reused completed Bailian batch {batch_index} from {response_path.name}.")
            return cached
    if args.dry_run:
        return placeholder_decisions(candidates, "Dry run only. Bailian Omni was not called.")
    request = urllib.request.Request(
        f"{args.bailian_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    response = read_urlopen_sse(request, args.timeout)
    response["candidate_ids"] = [str(item["id"]) for item in candidates]
    response["candidate_signature"] = candidate_batch_signature(candidates)
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return parse_cached_bailian_response(
        response,
        candidates,
        require_screen_action=any(item.get("clips") for item in candidates),
    )


def run_bailian_semantic_audit(
    args: argparse.Namespace,
    api_key: str,
    candidates: list[dict[str, Any]],
    *,
    batch_index: int = 1,
) -> dict[str, Any]:
    """Get a reasoning-model second opinion without granting new cut authority."""
    payload = build_bailian_payload(
        args,
        candidates,
        model=args.semantic_audit_model,
        enable_thinking=True,
        video_fps=args.semantic_audit_fps,
    )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / f"semantic_audit_request_{batch_index:03d}.redacted.json").write_text(
        json.dumps(redact_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    response_path = args.work_dir / f"semantic_audit_response_{batch_index:03d}.raw.json"
    if args.resume:
        cached = load_cached_bailian_response(
            response_path,
            candidates,
            require_screen_action=any(item.get("clips") for item in candidates),
        )
        if cached is not None:
            log(f"Reused completed semantic-audit batch {batch_index} from {response_path.name}.")
            return cached
    request = urllib.request.Request(
        f"{args.bailian_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    response = read_urlopen_sse(request, args.timeout)
    response["candidate_ids"] = [str(item["id"]) for item in candidates]
    response["candidate_signature"] = candidate_batch_signature(candidates)
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return parse_cached_bailian_response(
        response,
        candidates,
        require_screen_action=any(item.get("clips") for item in candidates),
    )


def semantic_audit_candidates(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    mode: str,
    min_cut_ms: float,
    protected_pause_min_ms: float = 5000.0,
) -> list[dict[str, Any]]:
    if mode == "off":
        return []
    decisions_by_id = {str(item.get("id") or ""): item for item in decisions}
    selected = []
    for candidate in candidates:
        decision = decisions_by_id.get(str(candidate.get("id") or ""), {})
        if decision.get("decision") != "cut" or decision.get("confidence") != "high":
            continue
        duration = float(
            candidate.get("duration_ms")
            or float(candidate.get("end_ms", 0.0)) - float(candidate.get("start_ms", 0.0))
        )
        candidate_type = str(candidate.get("type") or "")
        if (
            mode == "all-cuts"
            or duration >= min_cut_ms
            or candidate_type in SEMANTIC_HIGH_RISK_TYPES
            or (
                candidate_type == "protected_pause"
                and duration >= protected_pause_min_ms
            )
        ):
            selected.append(candidate)
    return selected


def activity_clearance_candidates(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    activity_report: Path | None,
) -> list[dict[str, Any]]:
    """Find model cuts whose claimed no-action state contradicts input telemetry."""
    intervals, has_report = load_input_activity_intervals(activity_report)
    if not has_report or not intervals:
        return []
    decisions_by_id = {str(item.get("id") or ""): item for item in decisions}
    selected = []
    for candidate in candidates:
        decision = decisions_by_id.get(str(candidate.get("id") or ""), {})
        if (
            decision.get("decision") != "cut"
            or decision.get("confidence") != "high"
            or decision.get("screen_action") != "none"
        ):
            continue
        overlap = interval_overlap_ms(
            float(candidate["start_ms"]),
            float(candidate["end_ms"]),
            intervals,
        )
        if overlap <= 0.0:
            continue
        item = dict(candidate)
        item["detected_input_activity_overlap_ms"] = round(overlap, 3)
        selected.append(item)
    return selected


def apply_semantic_audit_veto(
    decisions: list[dict[str, Any]],
    audit_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Require an independent high-confidence clearance; never create a new cut."""
    audit_by_id = {str(item.get("id") or ""): item for item in audit_decisions}
    combined: list[dict[str, Any]] = []
    for original in decisions:
        candidate_id = str(original.get("id") or "")
        audit = audit_by_id.get(candidate_id)
        if not audit:
            combined.append(original)
            continue
        cleared = (
            audit.get("decision") == "cut"
            and audit.get("confidence") == "high"
            and audit.get("screen_action") in {"none", "redundant"}
        )
        if cleared:
            accepted = dict(original)
            accepted["semantic_audit"] = {
                "model_decision": "cut",
                "confidence": audit.get("confidence"),
                "screen_action": audit.get("screen_action"),
                "unique_information_lost": audit.get("unique_information_lost") or [],
                "splice_assessment": audit.get("splice_assessment") or "",
                "note": audit.get("note") or "",
            }
            combined.append(accepted)
            continue
        vetoed = dict(original)
        vetoed["decision"] = "review"
        vetoed["confidence"] = "medium"
        vetoed["semantic_audit"] = {
            "model_decision": audit.get("decision") or "review",
            "confidence": audit.get("confidence") or "low",
            "screen_action": audit.get("screen_action") or "unclear",
            "unique_information_lost": audit.get("unique_information_lost") or [],
            "splice_assessment": audit.get("splice_assessment") or "",
            "note": audit.get("note") or "",
        }
        vetoed["note"] = (
            str(original.get("note") or "")
            + " Downgraded: independent semantic audit did not confirm a safe high-confidence cut."
        ).strip()
        combined.append(vetoed)
    return combined


def run_gemini(
    args: argparse.Namespace,
    api_key: str,
    messages: list[dict[str, Any]],
    *,
    batch_index: int = 1,
) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / f"zenmux_request_{batch_index:03d}.redacted.json").write_text(
        json.dumps(redact_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.dry_run:
        return {
            "decisions": [
                {
                    "id": item["id"],
                    "decision": "review",
                    "confidence": "low",
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "removed_text": item.get("removed_text", ""),
                    "reason": item["type"],
                    "note": "dry run only; Gemini was not called",
                }
                for item in extract_candidates_from_messages(messages)
            ],
            "summary": "Dry run only. No API call was made.",
        }

    response = post_json(f"{args.api_base.rstrip('/')}/chat/completions", payload, api_key, args.timeout)
    (args.work_dir / f"zenmux_response_{batch_index:03d}.raw.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    text = response["choices"][0]["message"]["content"]
    parsed = extract_json_from_text(text)
    if not isinstance(parsed.get("decisions"), list):
        raise RuntimeError("Gemini response did not contain a decisions list.")
    return parsed


def _native_gemini_text(response: dict[str, Any]) -> str:
    for candidate in response.get("candidates") or []:
        parts = ((candidate.get("content") or {}).get("parts") or [])
        text = "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
        if text.strip():
            return text
    raise RuntimeError("Gemini response did not contain text output.")


def run_native_gemini(
    args: argparse.Namespace,
    api_key: str,
    candidates: list[dict[str, Any]],
    *,
    batch_index: int = 1,
) -> dict[str, Any]:
    messages = build_messages(candidates, video_supplied=any(item.get("clips") for item in candidates))
    prompt_text = (
        f"SYSTEM INSTRUCTIONS:\n{messages[0]['content']}\n\n"
        f"{messages[1]['content'][0]['text']}"
    )
    parts: list[dict[str, Any]] = []
    for candidate in candidates:
        for clip in candidate.get("clips") or []:
            parts.append({
                "text": (
                    f"Candidate {candidate['id']} {clip['label']} evidence clip. "
                    f"Global timeline {clip['start']:.3f}s to {clip['end']:.3f}s. "
                    "Inspect both its audio and visual actions."
                )
            })
            parts.append({
                "inline_data": {
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(Path(clip["path"]).read_bytes()).decode("ascii"),
                }
            })
    parts.append({"text": prompt_text})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / f"gemini_native_request_{batch_index:03d}.redacted.json").write_text(
        json.dumps(redact_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.dry_run:
        return {
            "decisions": [
                {
                    "id": item["id"], "decision": "review", "confidence": "low",
                    "start_ms": item["start_ms"], "end_ms": item["end_ms"],
                    "removed_text": item.get("removed_text", ""),
                    "reason": item["type"], "note": "dry run only; Gemini was not called",
                }
                for item in candidates
            ],
            "summary": "Dry run only. No API call was made.",
        }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{args.gemini_model}:generateContent"
    response = post_json_with_headers(url, payload, {"x-goog-api-key": api_key}, args.timeout)
    (args.work_dir / f"gemini_native_response_{batch_index:03d}.raw.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    parsed = extract_json_from_text(_native_gemini_text(response))
    if not isinstance(parsed.get("decisions"), list):
        raise RuntimeError("Gemini response did not contain a decisions list.")
    return parsed


def extract_candidates_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Used only for dry-run placeholder decisions.
    for message in messages:
        if message.get("role") != "user":
            continue
        for part in message.get("content") or []:
            if part.get("type") != "text":
                continue
            text = part.get("text") or ""
            marker = "Candidates:\n"
            if marker in text:
                return json.loads(text.split(marker, 1)[1])
    return []


def cuts_from_decisions(
    decisions: list[dict[str, Any]],
    candidates_by_id: dict[str, dict[str, Any]],
    *,
    min_cut_ms: float,
    max_auto_cut_ms: float | None = None,
    max_filler_cut_ms: float = 2500.0,
    max_false_start_cut_ms: float = 45000.0,
    max_duplicate_cut_ms: float = 30000.0,
    max_sparse_retake_cut_ms: float = 120000.0,
) -> list[dict[str, Any]]:
    # A long failed recording attempt is often split into several pause-bounded
    # islands.  One island may contain the actual repeat/restart evidence while
    # its adjacent pieces contain setup chatter.  Treat nearby, independently
    # high-confidence multimodal cuts as a chain, but only when at least one
    # member has deterministic support and every relaxed member explicitly
    # clears its screen activity as redundant.
    high_cut_islands: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for decision in decisions:
        candidate = candidates_by_id.get(str(decision.get("id", "")))
        if (
            candidate
            and candidate.get("type") == "possible_isolated_take"
            and decision.get("decision") == "cut"
            and decision.get("confidence") == "high"
            and decision.get("screen_action") == "redundant"
            and str(decision.get("visual_assessment") or "").strip()
        ):
            high_cut_islands.append((candidate, decision))
    high_cut_islands.sort(key=lambda item: float(item[0]["start_ms"]))
    relaxed_island_ids: set[str] = set()
    components: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    for item in high_cut_islands:
        if (
            components
            and float(item[0]["start_ms"])
            <= max(float(existing[0]["end_ms"]) for existing in components[-1]) + 4000.0
        ):
            components[-1].append(item)
        else:
            components.append([item])
    for component in components:
        component_has_support = any(
            float(candidate.get("restart_similarity", 0.0)) >= 0.30
            or (
                candidate.get("supporting_repeat_id")
                and float(candidate.get("restart_similarity", 0.0)) >= 0.10
            )
            or has_strong_retake_marker(candidate)
            for candidate, _ in component
        )
        if component_has_support:
            relaxed_island_ids.update(str(candidate["id"]) for candidate, _ in component)

    cuts = []
    for decision in decisions:
        candidate = candidates_by_id.get(str(decision.get("id", "")))
        if not candidate:
            continue
        if decision.get("decision") != "cut" or decision.get("confidence") != "high":
            continue
        start_ms = float(decision.get("start_ms", candidate["start_ms"]))
        end_ms = float(decision.get("end_ms", candidate["end_ms"]))
        options = candidate.get("removal_options") or []
        chosen_option = None
        if options:
            for option in options:
                option_start = float(option["start_ms"])
                option_end = float(option["end_ms"])
                if start_ms >= option_start - 2 and end_ms <= option_end + 2:
                    chosen_option = option
                    start_ms = max(option_start, start_ms)
                    end_ms = min(option_end, end_ms)
                    break
            if chosen_option is None:
                log(f"Rejected {candidate.get('id')}: duplicate decision did not choose exactly one removal option.")
                continue
        else:
            start_ms = max(float(candidate["start_ms"]), start_ms)
            end_ms = min(float(candidate["end_ms"]), end_ms)
        duration = end_ms - start_ms
        candidate_type = str(candidate.get("type") or "")
        if (
            candidate_type == "possible_isolated_take"
            and float(candidate.get("restart_similarity", 0.0)) < 0.30
            and not (
                candidate.get("supporting_repeat_id")
                and float(candidate.get("restart_similarity", 0.0)) >= 0.10
            )
            and str(candidate.get("id") or "") not in relaxed_island_ids
        ):
            log(
                f"Manual review required: {candidate.get('id')} is an isolated "
                "speech island without repeat/restart evidence."
            )
            continue
        if candidate_type == "protected_pause" and decision.get("screen_action") != "redundant":
            log(
                f"Manual review required: {candidate.get('id')} is a protected pause "
                "without explicit redundant-screen clearance."
            )
            continue
        if candidate_type == "possible_sparse_retake" and (
            float(candidate.get("similarity", 0.0)) < 0.42
            or float(candidate.get("speech_density", 1.0)) > 0.45
            or decision.get("screen_action") != "redundant"
            or not str(decision.get("visual_assessment") or "").strip()
        ):
            log(
                f"Manual review required: {candidate.get('id')} is a long sparse "
                "retake without both structural repetition and explicit visual clearance."
            )
            continue
        strong_restart_clearance = (
            has_strong_retake_marker(candidate)
            and decision.get("screen_action") == "redundant"
            and bool(str(decision.get("visual_assessment") or "").strip())
        )
        long_gap_repair_clearance = (
            float(candidate.get("max_internal_gap_ms", 0.0)) >= 800.0
            and decision.get("screen_action") == "redundant"
            and bool(str(decision.get("visual_assessment") or "").strip())
        )
        if (
            candidate_type == "explicit_self_correction"
            and not bool(candidate.get("auto_safe"))
            and not strong_restart_clearance
            and not long_gap_repair_clearance
        ):
            log(
                f"Manual review required: {candidate.get('id')} is based only on repair "
                f"wording ({candidate.get('repair_marker') or 'unknown marker'})."
            )
            continue
        if (
            candidate_type in {"hard_filler", "soft_filler", "filler_cluster"}
            and not bool(candidate.get("contains_strong_hesitation"))
        ):
            log(
                f"Manual review required: {candidate.get('id')} is a weak discourse "
                "word, not a strong hesitation."
            )
            continue
        if max_auto_cut_ms is not None:
            limit = max_auto_cut_ms
        elif candidate_type in {"hard_filler", "soft_filler", "filler_cluster"}:
            limit = max_filler_cut_ms
        elif candidate_type == "near_duplicate_segment":
            limit = max_duplicate_cut_ms
        elif candidate_type in {"possible_sparse_retake", "global_paper_edit"}:
            limit = max_sparse_retake_cut_ms
        else:
            limit = max_false_start_cut_ms
        if duration < min_cut_ms or duration > limit:
            if duration > limit:
                log(
                    f"Manual review required: {candidate.get('id')} is {duration/1000:.1f}s "
                    f"(auto limit {limit/1000:.1f}s for {candidate_type})."
                )
            continue
        cuts.append({
            "candidate_id": str(candidate.get("id") or ""),
            "start_ms": round(start_ms),
            "end_ms": round(end_ms),
            "removed_text": str(
                decision.get("removed_text")
                or (chosen_option or {}).get("text")
                or candidate.get("removed_text")
                or ""
            ),
            "reason": str(decision.get("reason") or candidate.get("type") or "model_candidate"),
            "candidate_type": candidate_type,
            "selected_take": (chosen_option or {}).get("label"),
            "confidence": "high",
            "screen_action": str(decision.get("screen_action") or "unclear"),
            "visual_assessment": str(decision.get("visual_assessment") or ""),
            "heard_summary": str(decision.get("heard_summary") or ""),
            "note": str(decision.get("note") or ""),
            "spoken_start_ms": (
                round(float(candidate["spoken_start"]) * 1000.0)
                if candidate.get("spoken_start") is not None else None
            ),
            "spoken_end_ms": (
                round(float(candidate["spoken_end"]) * 1000.0)
                if candidate.get("spoken_end") is not None else None
            ),
        })
    return sorted(cuts, key=lambda c: (c["start_ms"], c["end_ms"]))


def review_batches(
    candidates: list[dict[str, Any]],
    *,
    max_count: int,
    max_media_bytes: int | None = None,
    isolate_duration_ms: float | None = None,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for candidate in candidates:
        duration_ms = float(
            candidate.get("duration_ms")
            or float(candidate.get("end_ms", 0.0)) - float(candidate.get("start_ms", 0.0))
        )
        media_bytes = sum(
            Path(clip["path"]).stat().st_size
            for clip in candidate.get("clips") or []
            if Path(clip["path"]).exists()
        )
        if isolate_duration_ms is not None and duration_ms >= isolate_duration_ms:
            if current:
                batches.append(current)
                current = []
                current_bytes = 0
            batches.append([candidate])
            continue
        if current and (
            len(current) >= max_count
            or (max_media_bytes is not None and current_bytes + media_bytes > max_media_bytes)
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(candidate)
        current_bytes += media_bytes
    if current:
        batches.append(current)
    return batches


def review_one_batch(
    args: argparse.Namespace,
    backend: str,
    api_key: str,
    batch: list[dict[str, Any]],
    batch_index: int,
) -> dict[str, Any]:
    log(f"Reviewing batch {batch_index} ({len(batch)} candidate(s)) via {backend}...")
    if backend == "bailian":
        return run_bailian_omni(args, api_key, batch, batch_index=batch_index)
    if backend == "gemini":
        return run_native_gemini(args, api_key, batch, batch_index=batch_index)
    messages = build_messages(batch, video_supplied=bool(args.video))
    return run_gemini(args, api_key, messages, batch_index=batch_index)


def main() -> None:
    args = parse_args()
    if not 0.1 <= args.semantic_audit_fps <= 10.0:
        fail("--semantic-audit-fps must be between 0.1 and 10.0.")
    if args.semantic_audit_min_cut_ms < 0 or args.semantic_audit_protected_pause_min_ms < 0:
        fail("Semantic-audit duration thresholds must be non-negative.")
    if args.protected_pause_min_review_ms < 0:
        fail("--protected-pause-min-review-ms must be non-negative.")
    if args.audio and not args.video:
        fail("--audio requires --video.")
    if args.coordinate_space == "edited" and not args.project_json:
        fail("--coordinate-space edited requires --project-json so cuts can be mapped safely.")
    if args.project_json and not args.project_json.exists():
        fail(f"project.json does not exist: {args.project_json}")
    segments = load_transcript(args.transcript)
    candidates = build_candidates(args, segments)
    if args.review_types:
        allowed_types = {
            item.strip() for item in args.review_types.split(",") if item.strip()
        }
        candidates = [item for item in candidates if item.get("type") in allowed_types]
    if args.range_start is not None:
        candidates = [item for item in candidates if float(item["end"]) > args.range_start]
    if args.range_end is not None:
        candidates = [item for item in candidates if float(item["start"]) < args.range_end]
    if not candidates:
        fail("No edit candidates found. Try a raw transcript that keeps filler words.")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    local_decisions: list[dict[str, Any]] = []
    review_candidates = candidates
    if not args.review_fillers_with_model:
        local_decisions, review_candidates = conservative_local_filler_decisions(
            candidates,
            args.activity_report,
        )
        advisory_decisions, review_candidates = conservative_local_advisory_decisions(
            review_candidates,
            protected_pause_min_review_ms=args.protected_pause_min_review_ms,
        )
        local_decisions.extend(advisory_decisions)
        local_cut_count = sum(
            item.get("decision") == "cut" for item in local_decisions
        )
        local_keep_count = len(local_decisions) - local_cut_count
        if local_decisions:
            log(
                f"Local safety gates: {local_cut_count} easy cut(s), "
                f"{local_keep_count} difficult candidate(s) preserved; "
                "no model tokens used."
            )
    candidate_type_by_id = {
        str(candidate["id"]): str(candidate.get("type") or "")
        for candidate in candidates
    }
    local_filler_decisions = [
        decision for decision in local_decisions
        if candidate_type_by_id.get(str(decision.get("id") or "")) in FILLER_CANDIDATE_TYPES
    ]

    if args.reuse_review_report:
        if not args.reuse_review_report.exists():
            fail(f"review report does not exist: {args.reuse_review_report}")
        reused = json.loads(args.reuse_review_report.read_text(encoding="utf-8"))
        reused_candidates_by_id = {
            str(item.get("id") or ""): item
            for item in reused.get("candidates") or []
            if isinstance(item, dict)
        }
        mismatched_reuse = [
            str(candidate["id"])
            for candidate in review_candidates
            if not reusable_candidate_matches(
                candidate,
                reused_candidates_by_id.get(str(candidate["id"]), {}),
            )
        ]
        if mismatched_reuse:
            fail(
                "Review report candidate fingerprint mismatch for: "
                + ", ".join(mismatched_reuse[:8])
                + ("..." if len(mismatched_reuse) > 8 else "")
            )
        reused_review = reused.get("review") or reused
        parsed = complete_decisions(
            {"decisions": reused_review.get("decisions") or []},
            review_candidates,
            require_screen_action=bool(args.video),
        )
        decisions = local_decisions + parsed["decisions"]
        backend = str(reused.get("backend") or reused_review.get("backend") or "reused")
        model_report = {
            "backend": backend,
            "model": str(reused.get("model") or reused_review.get("model") or "reused"),
            "decisions": decisions,
            "batches": [{"reused_from": str(args.reuse_review_report)}],
            "local_decisions": local_decisions,
            "local_filler_decisions": local_filler_decisions,
        }
        log(f"Reused {len(parsed['decisions'])} model decision(s) from {args.reuse_review_report}.")
    elif not review_candidates:
        backend = "local"
        decisions = local_decisions
        model_report = {
            "backend": backend,
            "model": "deterministic-local-filler-gate",
            "decisions": decisions,
            "batches": [],
            "local_decisions": local_decisions,
            "local_filler_decisions": local_filler_decisions,
        }
    else:
        bailian_key = bailian_api_key_from_args(args)
        gemini_key = gemini_api_key_from_args(args)
        backend = args.review_backend
        if backend == "auto":
            backend = "bailian" if bailian_key else ("gemini" if gemini_key else "zenmux")
        if backend == "bailian":
            if not bailian_key and not args.dry_run:
                fail("DASHSCOPE_API_KEY is not set and ~/.bailian/config.json has no API key.")
            attach_clips(args, review_candidates)
            api_key = bailian_key
        elif backend == "gemini":
            if not gemini_key and not args.dry_run:
                fail("GEMINI_API_KEY is not set for native audio/video review.")
            attach_clips(args, review_candidates)
            api_key = gemini_key
        else:
            attach_frames(args, review_candidates)
            api_key = api_key_from_args(args)

        model_decisions: list[dict[str, Any]] = []
        batch_reports: list[dict[str, Any]] = []
        native_cap = 3 if backend == "bailian" and args.video else (
            4 if backend == "gemini" and args.video else args.batch_size
        )
        batch_size = max(1, min(args.batch_size, native_cap))
        batches = review_batches(
            review_candidates,
            max_count=batch_size,
            isolate_duration_ms=args.semantic_audit_min_cut_ms,
            max_media_bytes=(
                18 * 1024 * 1024 if backend == "bailian" and args.video
                else 65 * 1024 * 1024 if backend == "gemini" and args.video
                else None
            ),
        )
        indexed_batches = list(enumerate(batches, start=1))
        workers = max(1, min(int(args.review_workers), len(indexed_batches)))
        ordered_reports: dict[int, dict[str, Any]] = {}
        if workers > 1 and not args.dry_run:
            log(f"Reviewing {len(indexed_batches)} batch(es) with {workers} concurrent workers.")
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            futures: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
            try:
                futures = {
                    executor.submit(
                        review_one_batch,
                        args,
                        backend,
                        api_key,
                        batch,
                        batch_index,
                    ): batch_index
                    for batch_index, batch in indexed_batches
                }
                for future in concurrent.futures.as_completed(futures):
                    batch_index = futures[future]
                    ordered_reports[batch_index] = future.result()
            except BaseException:
                # Do not let already-queued paid requests start after a quota,
                # network, or user-interrupt failure. Running workers finish,
                # and their validated raw responses can be reused with --resume.
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
        else:
            for batch_index, batch in indexed_batches:
                ordered_reports[batch_index] = review_one_batch(
                    args, backend, api_key, batch, batch_index
                )
        for batch_index in sorted(ordered_reports):
            batch_report = ordered_reports[batch_index]
            model_decisions.extend(batch_report.get("decisions") or [])
            batch_reports.append(batch_report)

        # A structurally strong isolated take can be visually ambiguous in a
        # crowded batch. Re-review only those medium/low-confidence cases alone;
        # accept the tie-break only when it becomes an explicit high-confidence
        # cut/keep. Most recordings incur zero extra calls.
        if not args.dry_run:
            decisions_by_id = {str(item.get("id") or ""): item for item in model_decisions}
            arbitration_candidates = [
                candidate for candidate in review_candidates
                if candidate.get("type") == "possible_isolated_take"
                and float(candidate.get("restart_similarity", 0.0)) >= 0.30
                and (
                    decisions_by_id.get(str(candidate["id"]), {}).get("decision") == "review"
                    or decisions_by_id.get(str(candidate["id"]), {}).get("confidence") != "high"
                )
            ]
            arbitration_batch_start = len(batch_reports)
            for offset, candidate in enumerate(arbitration_candidates, start=1):
                batch_index = arbitration_batch_start + offset
                log(f"Arbitrating {candidate['id']} alone via {backend}...")
                try:
                    if backend == "bailian":
                        arbitration = run_bailian_omni(
                            args, api_key, [candidate], batch_index=batch_index
                        )
                    elif backend == "gemini":
                        arbitration = run_native_gemini(
                            args, api_key, [candidate], batch_index=batch_index
                        )
                    else:
                        messages = build_messages([candidate], video_supplied=bool(args.video))
                        arbitration = run_gemini(
                            args, api_key, messages, batch_index=batch_index
                        )
                except Exception as exc:
                    log(f"Arbitration for {candidate['id']} failed closed: {exc}")
                    arbitration = placeholder_decisions(
                        [candidate],
                        "Arbitration failed; the original conservative decision was retained.",
                    )
                arbitration["arbitration_for"] = candidate["id"]
                replacement = next(iter(arbitration.get("decisions") or []), None)
                if (
                    replacement
                    and replacement.get("decision") in {"cut", "keep"}
                    and replacement.get("confidence") == "high"
                ):
                    model_decisions = [
                        replacement if str(item.get("id")) == str(candidate["id"]) else item
                        for item in model_decisions
                    ]
                batch_reports.append(arbitration)

        # Input telemetry is more reliable than a model claiming that no screen
        # action occurred. Re-review only those contradictions alone so the
        # model must classify the known action as redundant or meaningful.
        if not args.dry_run:
            clearance_candidates = activity_clearance_candidates(
                review_candidates,
                model_decisions,
                args.activity_report,
            )
            clearance_batch_start = len(batch_reports)
            for offset, candidate in enumerate(clearance_candidates, start=1):
                batch_index = clearance_batch_start + offset
                log(f"Resolving input-activity clearance for {candidate['id']} alone via {backend}...")
                try:
                    if backend == "bailian":
                        clearance = run_bailian_omni(
                            args, api_key, [candidate], batch_index=batch_index
                        )
                    elif backend == "gemini":
                        clearance = run_native_gemini(
                            args, api_key, [candidate], batch_index=batch_index
                        )
                    else:
                        messages = build_messages([candidate], video_supplied=bool(args.video))
                        clearance = run_gemini(
                            args, api_key, messages, batch_index=batch_index
                        )
                except Exception as exc:
                    log(f"Activity clearance for {candidate['id']} failed closed: {exc}")
                    clearance = placeholder_decisions(
                        [candidate],
                        "Input activity was not cleared; automatic cut was downgraded.",
                    )
                clearance["activity_clearance_for"] = candidate["id"]
                replacement = next(iter(clearance.get("decisions") or []), None)
                if replacement:
                    model_decisions = [
                        replacement if str(item.get("id")) == str(candidate["id"]) else item
                        for item in model_decisions
                    ]
                batch_reports.append(clearance)

        semantic_audit_report: dict[str, Any] | None = None
        audit_candidates = semantic_audit_candidates(
            review_candidates,
            model_decisions,
            mode=args.semantic_audit,
            min_cut_ms=args.semantic_audit_min_cut_ms,
            protected_pause_min_ms=args.semantic_audit_protected_pause_min_ms,
        )
        if audit_candidates and not args.dry_run:
            if not bailian_key:
                log(
                    f"Semantic audit skipped for {len(audit_candidates)} proposed cut(s): "
                    "no Bailian API key is available."
                )
            else:
                if args.video and any(not item.get("clips") for item in audit_candidates):
                    attach_clips(args, audit_candidates)
                audit_batches = review_batches(
                    audit_candidates,
                    max_count=3,
                    max_media_bytes=18 * 1024 * 1024 if args.video else None,
                )
                log(
                    f"Auditing {len(audit_candidates)} long/high-risk proposed cut(s) "
                    f"with {args.semantic_audit_model}..."
                )
                audit_reports_by_index: dict[int, dict[str, Any]] = {}
                audit_workers = max(1, min(int(args.review_workers), len(audit_batches)))
                if audit_workers > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=audit_workers) as executor:
                        futures = {
                            executor.submit(
                                run_bailian_semantic_audit,
                                args,
                                bailian_key,
                                batch,
                                batch_index=batch_index,
                            ): (batch_index, batch)
                            for batch_index, batch in enumerate(audit_batches, start=1)
                        }
                        for future in concurrent.futures.as_completed(futures):
                            batch_index, batch = futures[future]
                            try:
                                audit_reports_by_index[batch_index] = future.result()
                            except Exception as exc:
                                log(f"Semantic audit batch {batch_index} failed closed: {exc}")
                                audit_reports_by_index[batch_index] = placeholder_decisions(
                                    batch,
                                    "Semantic audit failed; proposed cut requires manual review.",
                                )
                else:
                    for batch_index, batch in enumerate(audit_batches, start=1):
                        try:
                            audit_reports_by_index[batch_index] = run_bailian_semantic_audit(
                                args,
                                bailian_key,
                                batch,
                                batch_index=batch_index,
                            )
                        except Exception as exc:
                            log(f"Semantic audit batch {batch_index} failed closed: {exc}")
                            audit_reports_by_index[batch_index] = placeholder_decisions(
                                batch,
                                "Semantic audit failed; proposed cut requires manual review.",
                            )
                audit_batch_reports = [
                    audit_reports_by_index[index] for index in sorted(audit_reports_by_index)
                ]
                audit_decisions = [
                    decision
                    for report in audit_batch_reports
                    for decision in report.get("decisions") or []
                ]
                model_decisions = apply_semantic_audit_veto(
                    model_decisions,
                    audit_decisions,
                )
                semantic_audit_report = {
                    "mode": args.semantic_audit,
                    "model": args.semantic_audit_model,
                    "min_cut_ms": args.semantic_audit_min_cut_ms,
                    "protected_pause_min_ms": args.semantic_audit_protected_pause_min_ms,
                    "fps": args.semantic_audit_fps,
                    "candidate_ids": [str(item["id"]) for item in audit_candidates],
                    "decisions": audit_decisions,
                    "batches": audit_batch_reports,
                }

        decisions = local_decisions + model_decisions
        model_report = {
            "backend": backend,
            "model": (
                args.bailian_model if backend == "bailian"
                else args.gemini_model if backend == "gemini"
                else args.model
            ),
            "decisions": decisions,
            "batches": batch_reports,
            "semantic_audit": semantic_audit_report,
            "local_decisions": local_decisions,
            "local_filler_decisions": local_filler_decisions,
        }

    if args.decision_override_report:
        candidates_by_id_for_override = {str(item["id"]): item for item in candidates}
        decisions_by_id = {str(item.get("id") or ""): item for item in decisions}
        for override_path in args.decision_override_report:
            if not override_path.exists():
                fail(f"decision override report does not exist: {override_path}")
            override_data = json.loads(override_path.read_text(encoding="utf-8"))
            override_review = override_data.get("review") or override_data
            completed_override = complete_decisions(
                {"decisions": override_review.get("decisions") or []},
                [
                    candidate for candidate_id, candidate in candidates_by_id_for_override.items()
                    if candidate_id in {
                        str(item.get("id") or "")
                        for item in override_review.get("decisions") or []
                        if isinstance(item, dict)
                    }
                ],
                require_screen_action=bool(args.video),
            )
            for override in completed_override.get("decisions") or []:
                candidate_id = str(override.get("id") or "")
                previous = decisions_by_id.get(candidate_id)
                if (
                    previous
                    and override.get("decision") in {"cut", "keep"}
                    and override.get("confidence") == "high"
                    and previous.get("confidence") != "high"
                ):
                    decisions_by_id[candidate_id] = override
                    log(f"Applied high-confidence arbitration override for {candidate_id}.")
        decisions = [
            decisions_by_id.get(str(item.get("id") or ""), item) for item in decisions
        ]
        model_report["decisions"] = decisions

    candidates_by_id = {item["id"]: item for item in candidates}
    cuts = cuts_from_decisions(
        model_report.get("decisions") or [],
        candidates_by_id,
        min_cut_ms=args.min_cut_ms,
        max_auto_cut_ms=args.max_auto_cut_ms,
        max_filler_cut_ms=args.max_filler_cut_ms,
        max_false_start_cut_ms=args.max_false_start_cut_ms,
        max_duplicate_cut_ms=args.max_duplicate_cut_ms,
        max_sparse_retake_cut_ms=args.max_sparse_retake_cut_ms,
    )
    auto_cut_ids = {str(cut.get("candidate_id") or "") for cut in cuts}
    manual_review = []
    for decision in decisions:
        candidate_id = str(decision.get("id") or "")
        if decision.get("decision") == "review" or (
            decision.get("decision") == "cut"
            and decision.get("confidence") == "high"
            and candidate_id not in auto_cut_ids
        ):
            manual_review.append({
                "candidate": candidates_by_id.get(candidate_id),
                "decision": decision,
            })
    cuts_document = build_cuts_document(
        cuts,
        coordinate_space=args.coordinate_space,
        project_json=args.project_json,
        transcript=args.transcript,
    )
    report = {
        "model": model_report["model"],
        "backend": backend,
        "coordinate_space": args.coordinate_space,
        "transcript": str(args.transcript),
        "video": str(args.video) if args.video else None,
        "audio": str(args.audio) if args.audio else None,
        "candidates": candidates,
        "review": model_report,
        "cuts": cuts,
        "manual_review": manual_review,
        "cuts_document": cuts_document,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.cuts_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.cuts_output.write_text(json.dumps(cuts_document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "candidates": len(candidates),
        "cuts": len(cuts),
        "manual_review": len(manual_review),
        "backend": backend,
        "model": model_report["model"],
        "coordinate_space": args.coordinate_space,
        "report": str(args.output),
        "cuts_output": str(args.cuts_output),
        "work_dir": str(args.work_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
