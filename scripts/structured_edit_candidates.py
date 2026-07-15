#!/usr/bin/env python3
"""Build conservative, transcript-grounded micro edits without another model call.

Only two benchmark-validated families are emitted: acoustically isolated,
clearly sustained strong fillers and exact short tail repeats. Broader retake
hypotheses stay in the model/manual-review path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gemini_edit_candidates import (
    conservative_local_filler_decisions,
    flatten_words,
    group_nearby_fillers,
    load_transcript,
    select_timeline_balanced_candidates,
    tail_restart_candidates,
)


DETECTOR_VERSION = 1
DEFAULT_MAX_CANDIDATES = 30
MIN_AUTOMATIC_FILLER_MS = 400.0
TYPE_TO_CATEGORY = {
    "hard_filler": "isolated_filler",
    "possible_tail_restart": "explicit_restart",
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_structured_candidates(
    transcript: Path,
    activity_report: Path,
    *,
    context_window: float = 6.0,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    segments = load_transcript(transcript)
    words = flatten_words(segments)

    fillers = group_nearby_fillers(words, segments, context_window)
    filler_decisions, _ = conservative_local_filler_decisions(
        fillers, activity_report
    )
    safe_filler_ids = {
        str(item["id"])
        for item in filler_decisions
        if item.get("decision") == "cut"
    }
    safe_fillers = [
        item
        for item in fillers
        if str(item.get("id")) in safe_filler_ids
        and float(item.get("spoken_duration_ms") or 0.0)
        >= MIN_AUTOMATIC_FILLER_MS
    ]

    # Exact short tail repeats were the only structural micro-cut family with
    # zero false speech deletions in the five-video benchmark.  Near matches,
    # repair markers, and pause-bounded spoken islands remain manual-review
    # hypotheses; sending them to the automatic path reduced precision sharply.
    exact_tail_restarts = [
        item
        for item in tail_restart_candidates(segments, context_window)
        if float(item.get("similarity") or 0.0) >= 0.98
    ]
    selected = select_timeline_balanced_candidates(
        safe_fillers + exact_tail_restarts, max(1, max_candidates)
    )

    result: list[dict[str, Any]] = []
    for index, raw in enumerate(selected, start=1):
        detector_type = str(raw.get("type") or "")
        item = dict(raw)
        item["id"] = f"structured_{index:03d}"
        item["type"] = "structured_edit_candidate"
        item["detector_type"] = detector_type
        item["planner_category"] = TYPE_TO_CATEGORY[detector_type]
        item["planner_reason"] = (
            "Conservative local micro-edit gate validated isolation and exactness; "
            "the final waveform boundary refiner still chooses the splice points."
        )
        item["local_acoustic_safe"] = detector_type == "hard_filler"
        result.append(item)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--activity-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context-window", type=float, default=6.0)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    args = parser.parse_args()

    candidates = build_structured_candidates(
        args.transcript,
        args.activity_report,
        context_window=args.context_window,
        max_candidates=args.max_candidates,
    )
    report = {
        "schema_version": 1,
        "detector_version": DETECTOR_VERSION,
        "transcript": str(args.transcript),
        "activity_report": str(args.activity_report),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
