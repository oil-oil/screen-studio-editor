#!/usr/bin/env python3
"""生成旧规则候选，仅用于 benchmark 对照，不进入默认质量剪辑。

规则不能证明语义可删；语气词和重复句均需 AI 根据上下文及音画判断。
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


DETECTOR_VERSION = 2
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

    # 文本相同只证明重复候选，不能证明中间的独有信息也能删除。
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
            "旧规则发现的孤立长语气词候选；是否可删仍需 AI 判断。"
            if detector_type == "hard_filler" else
            "文本重复候选；必须确认删除范围内每段信息都有保留的替代，不能连带删除独有提醒。"
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
