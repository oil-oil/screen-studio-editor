#!/usr/bin/env python3
"""Learn a creator's cut preferences and arbitrate high-recall candidates."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from benchmark_autoedit import intersection_duration
from gemini_edit_candidates import (
    DEFAULT_API_BASE,
    DEFAULT_API_KEY_FILE,
    extract_json_from_text,
    load_transcript,
    post_json,
    transcript_context,
)
from global_edit_planner import file_sha256, grounded_silent_range, transcript_atoms


REPORT_NAMES = ("global-video-planner-gemini35flash-v4.json",)
DEFAULT_MODEL = "google/gemini-3.5-flash"
# A pause above this threshold becomes a review hypothesis, not an automatic cut.
# Five-video leave-one-out validation found 2 s improved recall materially while
# retaining 97.68% time precision; reviewing every pause diluted the model prompt.
PROTECTED_PAUSE_MIN_MS = 2_000.0


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("ZENMUX_API_KEY", "")
    if not key and args.api_key_file.exists():
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"ZenMux key not found in environment or {args.api_key_file}")
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
    if (
        candidate_family(candidate) == "screen_pause"
        and float(candidate.get("visual_activity_fraction") or 0.0) >= 0.9
        and float(candidate.get("input_activity_fraction") or 0.0) <= 0.05
    ):
        return "continuous_visual_without_input"
    return None


def candidate_rows(
    project: Path,
    protected_pause_min_ms: float = PROTECTED_PAUSE_MIN_MS,
) -> list[dict[str, Any]]:
    transcript_path = project / "baseline-report.transcript.edit.json"
    segments = load_transcript(transcript_path)
    atoms = transcript_atoms(segments)
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    source_rows: list[tuple[str, dict[str, Any]]] = []
    activity_report_path = project / "baseline-report.json"
    activity_report: dict[str, Any] = {}
    if activity_report_path.exists():
        activity_report = load_json(activity_report_path)
        for pause in activity_report.get("pauses_protected_by_activity") or []:
            if not isinstance(pause, dict):
                continue
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
                        "Locally measured microphone silence overlapping screen activity; "
                        "the model must decide whether the action is meaningful."
                    ),
                },
            ))
    for report_name in REPORT_NAMES:
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
        if start < 0.0 or end <= start or end - start > 120.0:
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
) -> dict[str, Any]:
    examples: list[dict[str, Any]] = []
    for project in sorted(root.glob("val2-*.screenstudio")):
        ground_path = project / "benchmark-ground-truth.json"
        if not ground_path.exists():
            continue
        ground = load_json(ground_path)
        truth = [tuple(item) for item in ground.get("manual_cut_intervals_ms") or []]
        source_project = str(ground.get("source_project") or "")
        for index, candidate in enumerate(
            candidate_rows(project, protected_pause_min_ms), start=1
        ):
            interval = [(candidate["start"] * 1000.0, candidate["end"] * 1000.0)]
            duration = interval[0][1] - interval[0][0]
            fraction = intersection_duration(interval, truth) / duration if duration else 0.0
            label = "cut" if fraction >= 0.7 else "keep" if fraction <= 0.3 else "partial"
            examples.append({
                "id": f"example_{len(examples) + 1:03d}",
                "source_project": source_project,
                "candidate_index": index,
                "label": label,
                "overlap_fraction": round(fraction, 5),
                "category": candidate.get("planner_category"),
                "duration_s": round(candidate["end"] - candidate["start"], 3),
                "removed_text": candidate.get("removed_text") or "",
                "planner_reason": candidate.get("planner_reason") or "",
                "visual_activity_fraction": candidate.get("visual_activity_fraction", 0.0),
                "input_activity_fraction": candidate.get("input_activity_fraction", 0.0),
                "context": candidate.get("context") or "",
            })
    signature = hashlib.sha256(
        json.dumps(
            {
                "protected_pause_min_ms": protected_pause_min_ms,
                "examples": examples,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "source": "creator hand-edited Screen Studio benchmarks",
        "protected_pause_min_ms": protected_pause_min_ms,
        "signature": signature,
        "example_count": len(examples),
        "examples": examples,
    }


def target_candidates(
    project: Path, protected_pause_min_ms: float = PROTECTED_PAUSE_MIN_MS
) -> list[dict[str, Any]]:
    candidates = candidate_rows(project, protected_pause_min_ms)
    for index, candidate in enumerate(candidates, start=1):
        candidate["id"] = f"target_{index:03d}"
    return candidates


def prompt_for_arbitration(
    examples: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    video_supplied: bool = False,
) -> str:
    compact_examples = [
        {
            key: example.get(key)
            for key in (
                "label",
                "category",
                "duration_s",
                "removed_text",
                "planner_reason",
                "visual_activity_fraction",
                "input_activity_fraction",
                "context",
            )
        }
        for example in examples
    ]
    compact_targets = [
        {
            "id": candidate["id"],
            "category": candidate.get("planner_category"),
            "duration_s": round(candidate["end"] - candidate["start"], 3),
            "removed_text": candidate.get("removed_text") or "",
            "planner_reason": candidate.get("planner_reason") or "",
            "visual_activity_fraction": candidate.get("visual_activity_fraction", 0.0),
            "input_activity_fraction": candidate.get("input_activity_fraction", 0.0),
            "context": candidate.get("context") or "",
        }
        for candidate in candidates
    ]
    schema = {
        "decisions": [
            {
                "id": "target_001",
                "decision": "cut | keep | review",
                "confidence": "high | medium | low",
                "reason": "how the creator's demonstrated style applies",
            }
        ]
    }
    return f"""
You are learning one creator's PERSONAL talking-head editing style from labeled
examples. Decide the target candidates in the same style. The labels mean:
- cut: the creator removed most of this range;
- keep: the creator intentionally retained it;
- partial: only part of the proposed range was removed, so the broad range is
  unsafe for automatic deletion.

{"A complete source-timeline-aligned video with microphone audio is attached. Inspect the actual screen state, delivery, and full sequence before deciding." if video_supplied else "No video is attached to this pass; use the grounded transcript context."}

Important preferences to infer from examples:
- whether the creator leaves screen navigation or reading time visible;
- how they handle abandoned takes, repeated wording, and micro-fragments;
- they prefer a light edit when evidence is ambiguous.

visual_activity_fraction and input_activity_fraction are measured telemetry, not
model guesses. High continuous visual activity often means a meaningful scroll,
demonstration, or changing result; preserve it unless the video and examples
clearly show disposable setup. Near-zero activity during a long silence is
stronger evidence of waiting/dead time.

Never treat a model's planner_reason as ground truth. Compare it with the actual
removed_text and surrounding context. A sentence that continues grammatically
after a pause is not an abandoned take. A result showcase or invited reading
pause is content. Return cut/high only when the creator's examples strongly
support removing the complete proposed range; otherwise keep or review.

Judge the targets as one timeline, not as unrelated snippets. If a cluster of
screen pauses follows an instruction to pause, read, compare, inspect, score,
or watch a sequence of outputs, preserve the entire showcase cluster even when
that instruction appears only in the earlier candidates' context. For speech,
an alleged duplicate supported by just one planner is still unsafe when the
surrounding instructions or UI destination differ.

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


def arbitrate(args: argparse.Namespace) -> None:
    preferences = load_json(args.preferences)
    ground = load_json(args.project / "benchmark-ground-truth.json")
    source_project = str(ground.get("source_project") or "")
    examples = [
        item
        for item in preferences.get("examples") or []
        if item.get("source_project") != source_project
    ]
    protected_pause_min_ms = (
        args.protected_pause_min_ms
        if args.protected_pause_min_ms is not None
        else float(preferences.get("protected_pause_min_ms", PROTECTED_PAUSE_MIN_MS))
    )
    candidates = target_candidates(args.project, protected_pause_min_ms)
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
            and cached.get("preference_signature") == preferences.get("signature")
            and cached.get("candidate_signature") == candidate_signature
            and cached.get("video_sha256") == video_sha256
        ):
            print(json.dumps(cached, ensure_ascii=False, indent=2))
            return
    prompt = prompt_for_arbitration(
        examples, candidates, video_supplied=bool(args.video)
    )
    user_content: str | list[dict[str, Any]] = prompt
    if args.video:
        user_content = [
            {
                "type": "file",
                "file": {
                    "file_data": (
                        "data:video/mp4;base64,"
                        + base64.b64encode(args.video.read_bytes()).decode("ascii")
                    ),
                    "filename": args.video.name,
                },
            },
            {"type": "text", "text": prompt},
        ]
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "You are a conservative personalized video editor. Return strict JSON only."},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 12_000,
        "response_format": {"type": "json_object"},
    }
    if not args.model.startswith("anthropic/"):
        payload["temperature"] = 0
    response = post_json(
        f"{args.api_base.rstrip('/')}/chat/completions",
        payload,
        api_key(args),
        args.timeout,
    )
    parsed = extract_json_from_text(response_text(response))
    by_id = {item["id"]: item for item in candidates}
    decisions = []
    accepted = []
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
        }
        decisions.append(item)
        if item["decision"] == "cut" and item["confidence"] == "high":
            candidate = dict(by_id[candidate_id])
            blocker = automatic_safety_blocker(candidate)
            if blocker:
                item["safety_blocker"] = blocker
                continue
            candidate["preference_decision"] = item
            accepted.append(candidate)
    output = {
        "schema_version": 1,
        "project": str(args.project),
        "source_project": source_project,
        "transcript": str(args.project / "baseline-report.transcript.edit.json"),
        "model": args.model,
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
    decide = subparsers.add_parser("decide")
    decide.add_argument("--project", type=Path, required=True)
    decide.add_argument("--preferences", type=Path, required=True)
    decide.add_argument("--output", type=Path, required=True)
    decide.add_argument("--model", default=DEFAULT_MODEL)
    decide.add_argument("--video", type=Path)
    decide.add_argument("--api-base", default=DEFAULT_API_BASE)
    decide.add_argument("--api-key", default="")
    decide.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    decide.add_argument("--timeout", type=int, default=300)
    decide.add_argument("--protected-pause-min-ms", type=float)
    decide.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "build":
        preferences = build_preferences(args.root, args.protected_pause_min_ms)
        write_json(args.output, preferences)
        print(json.dumps(preferences, ensure_ascii=False, indent=2))
    else:
        arbitrate(args)


if __name__ == "__main__":
    main()
