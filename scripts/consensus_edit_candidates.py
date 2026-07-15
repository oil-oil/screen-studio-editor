#!/usr/bin/env python3
"""Keep only grounded edit candidates independently proposed by two models."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--support", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--min-overlap",
        type=float,
        default=0.5,
        help="Minimum intersection divided by the shorter span (default: 0.5).",
    )
    return parser.parse_args()


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise SystemExit(f"Invalid planner report: {path}")
    return data


def candidate_family(candidate: dict[str, Any]) -> str:
    return "screen_pause" if candidate.get("planner_category") == "screen_pause" else "speech"


def overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    start = max(float(left["start"]), float(right["start"]))
    end = min(float(left["end"]), float(right["end"]))
    intersection = max(0.0, end - start)
    shorter = min(
        float(left["end"]) - float(left["start"]),
        float(right["end"]) - float(right["start"]),
    )
    return intersection / shorter if shorter > 0.0 else 0.0


def consensus_candidates(
    primary: dict[str, Any],
    supports: list[dict[str, Any]],
    *,
    min_overlap: float,
) -> list[dict[str, Any]]:
    transcript = str(primary.get("transcript") or "")
    for report in supports:
        if str(report.get("transcript") or "") != transcript:
            raise ValueError("Consensus reports refer to different transcripts.")
    result: list[dict[str, Any]] = []
    support_rows = [
        candidate
        for report in supports
        for candidate in report.get("candidates") or []
        if isinstance(candidate, dict)
    ]
    for candidate in primary.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        matches = [
            other
            for other in support_rows
            if candidate_family(other) == candidate_family(candidate)
            and overlap_ratio(candidate, other) >= min_overlap
        ]
        if not matches:
            continue
        best = max(matches, key=lambda other: overlap_ratio(candidate, other))
        accepted = dict(candidate)
        if candidate_family(candidate) == "screen_pause":
            # The intersection is the safest agreement when timestamp estimates
            # differ around silent visual actions.
            accepted["start"] = max(float(candidate["start"]), float(best["start"]))
            accepted["end"] = min(float(candidate["end"]), float(best["end"]))
            accepted["start_ms"] = round(float(accepted["start"]) * 1000.0)
            accepted["end_ms"] = round(float(accepted["end"]) * 1000.0)
            accepted["duration_ms"] = accepted["end_ms"] - accepted["start_ms"]
        accepted["consensus_support"] = {
            "model": best.get("planner_model") or next(
                (report.get("model") for report in supports if best in report.get("candidates", [])),
                "unknown",
            ),
            "candidate_id": best.get("id"),
            "overlap_ratio": round(overlap_ratio(candidate, best), 5),
        }
        accepted["id"] = f"consensus_{len(result) + 1:03d}"
        result.append(accepted)
    return result


def main() -> None:
    args = parse_args()
    if not 0.0 < args.min_overlap <= 1.0:
        raise SystemExit("--min-overlap must be in (0, 1].")
    primary = load_report(args.primary)
    supports = [load_report(path) for path in args.support]
    candidates = consensus_candidates(
        primary, supports, min_overlap=args.min_overlap
    )
    signature_payload = {
        "primary": primary.get("signature"),
        "supports": [report.get("signature") for report in supports],
        "min_overlap": args.min_overlap,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    report = {
        "schema_version": 1,
        "transcript": primary.get("transcript"),
        "video": primary.get("video"),
        "model": "cross-model-consensus",
        "signature": signature,
        "primary_model": primary.get("model"),
        "support_models": [report.get("model") for report in supports],
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
