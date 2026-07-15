#!/usr/bin/env python3
"""Measure high-recall candidate pools against hand-edited benchmark timelines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmark_autoedit import (
    intersection_duration,
    load_json,
    merge_intervals,
    report_intervals,
    score_intervals,
)


def candidate_intervals(report: dict[str, Any]) -> list[tuple[float, float]]:
    return [
        (float(item["start_ms"]), float(item["end_ms"]))
        for item in report.get("candidates") or []
        if item.get("start_ms") is not None
        and item.get("end_ms") is not None
        and float(item["end_ms"]) > float(item["start_ms"])
    ]


def protected_pause_intervals(
    report: dict[str, Any], threshold_ms: float
) -> list[tuple[float, float]]:
    return [
        (float(item["start_ms"]), float(item["end_ms"]))
        for item in report.get("pauses_protected_by_activity") or []
        if float(item.get("duration_ms") or 0.0) >= threshold_ms
        and float(item["end_ms"]) > float(item["start_ms"])
    ]


def oracle_candidates(
    candidates: list[tuple[float, float]], truth: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    return [
        interval
        for interval in candidates
        if intersection_duration([interval], truth) / (interval[1] - interval[0])
        >= 0.7
    ]


def subtract_intervals(
    source: list[tuple[float, float]], removed: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    pieces = merge_intervals(source)
    for left, right in merge_intervals(removed):
        updated: list[tuple[float, float]] = []
        for start, end in pieces:
            if right <= start or left >= end:
                updated.append((start, end))
                continue
            if left > start:
                updated.append((start, left))
            if right < end:
                updated.append((right, end))
        pieces = updated
    return pieces


def duration_bins(intervals: list[tuple[float, float]]) -> dict[str, float]:
    bins = {"under_1s": 0.0, "1_to_3s": 0.0, "3_to_6s": 0.0, "over_6s": 0.0}
    for start, end in intervals:
        duration = end - start
        if duration < 1_000.0:
            key = "under_1s"
        elif duration < 3_000.0:
            key = "1_to_3s"
        elif duration < 6_000.0:
            key = "3_to_6s"
        else:
            key = "over_6s"
        bins[key] += duration / 1000.0
    return {key: round(value, 3) for key, value in bins.items()}


def aggregate_score(rows: list[dict[str, float | int]]) -> dict[str, float]:
    predicted = sum(float(row["predicted_removed_s"]) for row in rows)
    manual = sum(float(row["manual_removed_s"]) for row in rows)
    overlap = sum(float(row["overlap_s"]) for row in rows)
    precision = overlap / predicted if predicted else 1.0
    recall = overlap / manual if manual else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "predicted_removed_s": round(predicted, 3),
        "manual_removed_s": round(manual, 3),
        "overlap_s": round(overlap, 3),
        "time_precision": round(precision, 5),
        "time_recall": round(recall, 5),
        "time_f1": round(f1, 5),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--thresholds-ms",
        type=float,
        nargs="+",
        default=[0, 1000, 2000, 3000, 4000, 5000, 6000],
    )
    parser.add_argument(
        "--final-report-name",
        default="preference-arbiter-loo-gemini-final.json",
        help="Per-project arbiter report used to classify the remaining misses.",
    )
    args = parser.parse_args()

    projects = sorted(args.root.glob("val2-*.screenstudio"))
    threshold_rows: dict[str, dict[str, Any]] = {}
    for threshold in args.thresholds_ms:
        raw_scores = []
        oracle_scores = []
        candidate_count = 0
        oracle_count = 0
        for project in projects:
            ground = load_json(project / "benchmark-ground-truth.json")
            truth = [tuple(item) for item in ground["manual_cut_intervals_ms"]]
            baseline = load_json(project / "baseline-report.json")
            planner = load_json(
                project / "global-video-planner-gemini35flash-v4.json"
            )
            base_intervals = report_intervals(baseline)
            candidates = protected_pause_intervals(baseline, threshold)
            candidates.extend(candidate_intervals(planner))
            candidate_count += len(candidates)
            oracle = oracle_candidates(candidates, truth)
            oracle_count += len(oracle)
            raw_scores.append(score_intervals(base_intervals + candidates, truth))
            oracle_scores.append(score_intervals(base_intervals + oracle, truth))
        threshold_rows[str(round(threshold))] = {
            "candidate_count": candidate_count,
            "raw": aggregate_score(raw_scores),
            "oracle_candidate_count": oracle_count,
            "oracle": aggregate_score(oracle_scores),
        }

    missed_fragments: list[tuple[float, float]] = []
    relations = {
        "inside_any_protected_pause_s": 0.0,
        "inside_flash_candidate_s": 0.0,
        "outside_both_s": 0.0,
    }
    for project in projects:
        ground = load_json(project / "benchmark-ground-truth.json")
        truth = [tuple(item) for item in ground["manual_cut_intervals_ms"]]
        baseline = load_json(project / "baseline-report.json")
        final = load_json(project / args.final_report_name)
        predicted = report_intervals(baseline) + candidate_intervals(final)
        missed = subtract_intervals(truth, predicted)
        missed_fragments.extend(missed)
        protected = protected_pause_intervals(baseline, 0.0)
        flash = candidate_intervals(
            load_json(project / "global-video-planner-gemini35flash-v4.json")
        )
        protected_overlap = intersection_duration(missed, protected) / 1000.0
        flash_overlap = intersection_duration(missed, flash) / 1000.0
        total = sum(end - start for start, end in missed) / 1000.0
        relations["inside_any_protected_pause_s"] += protected_overlap
        relations["inside_flash_candidate_s"] += flash_overlap
        relations["outside_both_s"] += max(
            0.0, total - intersection_duration(missed, protected + flash) / 1000.0
        )

    print(json.dumps({
        "projects": len(projects),
        "threshold_experiments": threshold_rows,
        "current_missed_duration_bins": duration_bins(missed_fragments),
        "current_missed_relations": {
            key: round(value, 3) for key, value in relations.items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
