#!/usr/bin/env python3
"""Prepare and score non-destructive Screen Studio auto-edit benchmarks.

The source project is treated as the human-edited ground truth. ``prepare``
creates an APFS clone, records the original slice map, then resets only the
clone to one pristine full-length slice. ``evaluate`` compares a process.py
audit (and optionally a candidate report) with the saved human deletion map.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


GENERATED_FILES = {
    ".autoedit-state.json",
    "autoedit-report.json",
    "bailian_asr.json",
    "omni-cuts.json",
    "omni-review-report.json",
    "project.json.bak",
    "transcript.edit.json",
    "transcript.edit.meta.json",
    "transcript.json",
}


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def project_slices(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = (project_data.get("json") or {}).get("scenes") or []
    if len(scenes) != 1 or not isinstance(scenes[0].get("slices"), list):
        fail("Benchmark preparation currently requires exactly one scene.")
    return scenes[0]["slices"]


def mic_sessions(project_dir: Path) -> list[dict[str, Any]]:
    metadata_path = project_dir / "recording" / "metadata.json"
    if not metadata_path.exists():
        fail(f"Missing recording metadata: {metadata_path}")
    metadata = load_json(metadata_path)
    sessions = []
    for recorder in metadata.get("recorders") or []:
        recorder_id = str(recorder.get("id") or "")
        if recorder.get("type") == "microphone" or "microphone" in recorder_id:
            sessions.extend(recorder.get("sessions") or [])
    sessions = sorted(sessions, key=lambda item: float(item.get("processTimeStartMs", 0.0)))
    if not sessions:
        fail(f"No microphone sessions in {metadata_path}")
    return sessions


def merge_intervals(
    intervals: list[tuple[float, float]], *, gap_ms: float = 0.0
) -> list[tuple[float, float]]:
    ordered = sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a))
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def complement_intervals(
    kept: list[tuple[float, float]], duration_ms: float
) -> list[tuple[float, float]]:
    removed = []
    cursor = 0.0
    for start, end in merge_intervals(kept):
        start = max(0.0, min(duration_ms, start))
        end = max(start, min(duration_ms, end))
        if start > cursor:
            removed.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_ms:
        removed.append((cursor, duration_ms))
    return removed


def prepare(source: Path, output: Path) -> None:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    source_json = source / "project.json"
    if not source_json.exists() or not (source / "recording").is_dir():
        fail(f"Not a Screen Studio project: {source}")
    if output.exists():
        fail(f"Output already exists; refusing to overwrite: {output}")
    if output == source or source in output.parents:
        fail("Benchmark output must be a separate sibling project.")

    source_hash_before = sha256(source_json)
    manual_project = load_json(source_json)
    manual_slices = copy.deepcopy(project_slices(manual_project))
    if len(manual_slices) < 2:
        fail("Ground truth has fewer than two slices and is not a useful hand-edit benchmark.")
    sessions = mic_sessions(source)
    duration_ms = sum(float(item.get("durationMs", 0.0)) for item in sessions)
    if duration_ms <= 0:
        fail("Microphone metadata reports a zero-length recording.")

    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-Rc", str(source), str(output)], check=True)
    clone_json = output / "project.json"
    if sha256(clone_json) != source_hash_before:
        fail("Clone project.json does not match its source.")

    # A pristine multi-session Screen Studio source timeline is one continuous
    # slice whose duration equals the sum of metadata session durations.
    raw_project = copy.deepcopy(manual_project)
    raw_slice = copy.deepcopy(manual_slices[0])
    raw_slice.update({
        "id": "autoedit-benchmark-raw",
        "timeScale": 1,
        "sourceStartMs": 0.0,
        "sourceEndMs": duration_ms,
    })
    raw_project["json"]["scenes"][0]["slices"] = [raw_slice]
    write_json(clone_json, raw_project)

    # A copied project may already contain artifacts from an earlier edit run.
    # They are safe to remove from the new benchmark clone only.
    for name in GENERATED_FILES:
        artifact = output / name
        if artifact.exists():
            if artifact.is_dir():
                shutil.rmtree(artifact)
            else:
                artifact.unlink()

    kept = [
        (float(item["sourceStartMs"]), float(item["sourceEndMs"]))
        for item in manual_slices
    ]
    ground_truth = {
        "schema_version": 1,
        "source_project": str(source),
        "benchmark_project": str(output),
        "source_project_sha256": source_hash_before,
        "raw_project_sha256": sha256(clone_json),
        "duration_ms": duration_ms,
        "session_count": len(sessions),
        "manual_slices": manual_slices,
        "manual_kept_intervals_ms": [list(item) for item in merge_intervals(kept)],
        "manual_cut_intervals_ms": [
            list(item) for item in complement_intervals(kept, duration_ms)
        ],
    }
    write_json(output / "benchmark-ground-truth.json", ground_truth)
    write_json(output / "manual-project.json", manual_project)

    source_hash_after = sha256(source_json)
    if source_hash_after != source_hash_before:
        fail("Source project changed while preparing the benchmark.")
    print(json.dumps({
        "source": str(source),
        "source_sha256": source_hash_before,
        "benchmark": str(output),
        "duration_s": round(duration_ms / 1000.0, 3),
        "sessions": len(sessions),
        "manual_slices": len(manual_slices),
        "manual_removed_s": round(
            sum(end - start for start, end in complement_intervals(kept, duration_ms)) / 1000.0,
            3,
        ),
    }, ensure_ascii=False, indent=2))


def interval_duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in merge_intervals(intervals))


def intersection_duration(
    left: list[tuple[float, float]], right: list[tuple[float, float]]
) -> float:
    left = merge_intervals(left)
    right = merge_intervals(right)
    total = 0.0
    i = j = 0
    while i < len(left) and j < len(right):
        total += max(0.0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total


def report_intervals(report: dict[str, Any]) -> list[tuple[float, float]]:
    intervals = []
    for key in ("pauses_applied", "reviewed_cuts_applied", "wordless_slices_removed"):
        for item in report.get(key) or []:
            if not isinstance(item, dict):
                continue
            start = item.get("start_ms", item.get("sourceStartMs"))
            end = item.get("end_ms", item.get("sourceEndMs"))
            if start is not None and end is not None and float(end) > float(start):
                intervals.append((float(start), float(end)))
    return merge_intervals(intervals)


def score_intervals(
    predicted: list[tuple[float, float]], truth: list[tuple[float, float]]
) -> dict[str, float | int]:
    predicted = merge_intervals(predicted)
    truth = merge_intervals(truth)
    predicted_ms = interval_duration(predicted)
    truth_ms = interval_duration(truth)
    overlap_ms = intersection_duration(predicted, truth)
    precision = overlap_ms / predicted_ms if predicted_ms else 1.0
    recall = overlap_ms / truth_ms if truth_ms else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    matched_truth = sum(
        intersection_duration([interval], predicted) >= 0.5 * (interval[1] - interval[0])
        for interval in truth
    )
    false_positive_events = sum(
        intersection_duration([interval], truth) < 0.5 * (interval[1] - interval[0])
        for interval in predicted
    )
    return {
        "predicted_removed_s": round(predicted_ms / 1000.0, 3),
        "manual_removed_s": round(truth_ms / 1000.0, 3),
        "overlap_s": round(overlap_ms / 1000.0, 3),
        "time_precision": round(precision, 5),
        "time_recall": round(recall, 5),
        "time_f1": round(f1, 5),
        "manual_events": len(truth),
        "manual_events_matched": matched_truth,
        "predicted_events": len(predicted),
        "false_positive_events": false_positive_events,
    }


def evaluate(
    project: Path,
    auto_report_path: Path,
    candidate_report_path: Path | None,
    output: Path,
) -> None:
    truth_data = load_json(project / "benchmark-ground-truth.json")
    truth = [tuple(item) for item in truth_data["manual_cut_intervals_ms"]]
    auto_report = load_json(auto_report_path)
    predicted = report_intervals(auto_report)
    result: dict[str, Any] = {
        "schema_version": 1,
        "project": str(project),
        "source_project": truth_data["source_project"],
        "source_project_sha256": truth_data["source_project_sha256"],
        "auto_report": str(auto_report_path),
        "auto": score_intervals(predicted, truth),
        "manual_cut_intervals_ms": [list(item) for item in truth],
        "auto_cut_intervals_ms": [list(item) for item in predicted],
    }
    if candidate_report_path:
        candidate_report = load_json(candidate_report_path)
        candidate_intervals = [
            (float(item["start_ms"]), float(item["end_ms"]))
            for item in candidate_report.get("candidates") or []
            if item.get("start_ms") is not None and item.get("end_ms") is not None
        ]
        result["candidate_upper_bound"] = score_intervals(candidate_intervals, truth)
        result["auto_plus_candidates"] = score_intervals(
            predicted + candidate_intervals, truth
        )
        result["candidate_diagnostics"] = [
            {
                "id": item.get("id"),
                "start_ms": float(item["start_ms"]),
                "end_ms": float(item["end_ms"]),
                "duration_s": round(
                    (float(item["end_ms"]) - float(item["start_ms"])) / 1000.0,
                    3,
                ),
                "manual_overlap_fraction": round(
                    intersection_duration(
                        [(float(item["start_ms"]), float(item["end_ms"]))], truth
                    ) / (float(item["end_ms"]) - float(item["start_ms"])),
                    5,
                ),
                "category": item.get("planner_category") or item.get("type"),
                "removed_text": item.get("removed_text") or "",
            }
            for item in candidate_report.get("candidates") or []
            if item.get("start_ms") is not None
            and item.get("end_ms") is not None
            and float(item["end_ms"]) > float(item["start_ms"])
        ]
        result["candidate_report"] = str(candidate_report_path)
        result["candidate_count"] = len(candidate_report.get("candidates") or [])
        result["local_filler_decisions"] = len(
            ((candidate_report.get("review") or {}).get("local_filler_decisions") or [])
        )
    write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--source", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--project", type=Path, required=True)
    evaluate_parser.add_argument("--auto-report", type=Path, required=True)
    evaluate_parser.add_argument("--candidate-report", type=Path)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "prepare":
        prepare(args.source, args.output)
    else:
        evaluate(args.project, args.auto_report, args.candidate_report, args.output)


if __name__ == "__main__":
    main()
