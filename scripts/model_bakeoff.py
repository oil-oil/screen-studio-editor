#!/usr/bin/env python3
"""Blindly compare Bailian video models on labeled edit candidates.

The manifest's expected decision is used only after every request completes. It
is never included in the model prompt. This script is evaluation-only: it does
not read or write Screen Studio project metadata.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from gemini_edit_candidates import (
    DEFAULT_BAILIAN_BASE,
    DEFAULT_BAILIAN_CONFIG,
    extract_json_from_text,
    read_urlopen_sse,
    redact_payload,
)


DEFAULT_MODELS = (
    "qwen3.5-omni-plus",
    "qwen3.7-plus",
    "qwen3.7-max-2026-06-08",
)
VIDEO_REASONING_MODELS = {
    "qwen3.7-plus",
    "qwen3.7-plus-2026-05-26",
    "qwen3.7-max-2026-06-08",
}
VALID_DECISIONS = {"cut", "keep", "review"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Blindly compare Bailian video models on edit candidates."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--bailian-base", default=DEFAULT_BAILIAN_BASE)
    parser.add_argument("--bailian-api-key", default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--fps", type=float, default=1.0)
    return parser.parse_args()


def bailian_api_key(args: argparse.Namespace) -> str:
    key = args.bailian_api_key or os.environ.get("DASHSCOPE_API_KEY", "")
    if key:
        return key
    try:
        data = json.loads(DEFAULT_BAILIAN_CONFIG.read_text(encoding="utf-8"))
        return str(data.get("api_key") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest must be a non-empty list or contain a cases list")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("each manifest case must be an object")
        case_id = str(raw.get("id") or "")
        expected = str(raw.get("expected") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"case id must be unique and non-empty: {case_id!r}")
        if expected not in {"cut", "keep"}:
            raise ValueError(f"case {case_id} expected must be cut or keep")
        clips = raw.get("clips") or []
        if not clips:
            raise ValueError(f"case {case_id} has no evidence clips")
        for clip in clips:
            clip_path = Path(str(clip.get("path") or "")).expanduser()
            if not clip_path.exists():
                raise ValueError(f"case {case_id} clip does not exist: {clip_path}")
        seen.add(case_id)
        result.append(raw)
    return result


def prompt_for_case(case: dict[str, Any]) -> str:
    candidate = dict(case.get("candidate") or {})
    candidate.setdefault("id", case["id"])
    schema = {
        "decision": "cut | keep | review",
        "confidence": "high | medium | low",
        "screen_action": "none | redundant | meaningful | unclear",
        "heard_summary": "brief summary of the proposed removal",
        "visual_assessment": "brief screen-change assessment",
        "unique_information_lost": ["any claim/example lost by cutting"],
        "splice_assessment": "whether the before/after speech remains grammatical and complete",
        "note": "brief rationale",
    }
    return f"""
You are the final safety judge for a Chinese spoken screen-recording editor.
Judge only whether the proposed candidate range can be removed. Be conservative
but decisive. Return one strict JSON object and no markdown.

Decision policy:
- CUT only when the range is a filler, abandoned false start, or genuinely
  redundant take and the remaining speech preserves every necessary claim,
  example, number, warning, and conclusion.
- A later summary is not proof that a detailed earlier explanation is
  redundant. If it omits any unique reasoning or example, KEEP.
- A correction marker such as “应该”, “也就是说”, or “就是” is not by itself
  evidence of a false start. Test the actual splice.
- Screen activity is REDUNDANT when the same final state remains visible after
  the cut. It is MEANINGFUL only when watching the intermediate action itself is
  necessary to understand or reproduce the tutorial.
- If a safe conclusion cannot be reached from the transcript and video, REVIEW.
- Transcript timing and text are authoritative for speech. Inspect the video
  for screen actions; if the model supports audio, also listen for delivery,
  pauses, and restarts.

Required JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Candidate (the hidden benchmark label is intentionally omitted):
{json.dumps(candidate, ensure_ascii=False, indent=2)}
""".strip()


def payload_for_case(
    model: str,
    case: dict[str, Any],
    *,
    fps: float,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for clip in case.get("clips") or []:
        path = Path(str(clip["path"])).expanduser()
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({
            "type": "text",
            "text": (
                f"Evidence clip {clip.get('label', 'candidate')} for {case['id']}; "
                f"timeline {clip.get('start', '?')}s to {clip.get('end', '?')}s."
            ),
        })
        content.append({
            "type": "video_url",
            "video_url": {"url": f"data:video/mp4;base64,{encoded}"},
            "fps": fps,
        })
    content.append({"type": "text", "text": prompt_for_case(case)})
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if model in VIDEO_REASONING_MODELS:
        payload["enable_thinking"] = True
    else:
        payload["modalities"] = ["text"]
        payload["response_format"] = {"type": "json_object"}
    return payload


def normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    if result.get("decision") not in VALID_DECISIONS:
        result["decision"] = "review"
    if result.get("confidence") not in VALID_CONFIDENCE:
        result["confidence"] = "low"
    if result.get("screen_action") not in {
        "none", "redundant", "meaningful", "unclear"
    }:
        result["screen_action"] = "unclear"
    result.setdefault("heard_summary", "")
    result.setdefault("visual_assessment", "")
    result.setdefault("unique_information_lost", [])
    result.setdefault("splice_assessment", "")
    result.setdefault("note", "")
    return result


def request_case(
    args: argparse.Namespace,
    api_key: str,
    model: str,
    case: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    payload = payload_for_case(model, case, fps=args.fps)
    request_dir = args.work_dir / model
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / f"{case['id']}.request.redacted.json").write_text(
        json.dumps(redact_payload(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
    parsed = normalize_decision(extract_json_from_text(str(response.get("text") or "")))
    elapsed = round(time.monotonic() - started, 3)
    result = {
        "case_id": case["id"],
        "model": model,
        "decision": parsed,
        "elapsed_seconds": elapsed,
        "usage": response.get("usage"),
    }
    (request_dir / f"{case['id']}.response.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def score_result(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected = str(case["expected"])
    decision = result["decision"]
    predicted = str(decision.get("decision") or "review")
    confidence = str(decision.get("confidence") or "low")
    screen_action = str(decision.get("screen_action") or "unclear")
    automatic_cut = (
        predicted == "cut"
        and confidence == "high"
        and screen_action in {"none", "redundant"}
    )
    automatic_keep = predicted == "keep" and confidence == "high"
    exact = automatic_cut if expected == "cut" else automatic_keep
    safe = not (expected == "keep" and automatic_cut)
    return {
        **result,
        "expected": expected,
        "automatic_decision": "cut" if automatic_cut else "keep" if automatic_keep else "review",
        "exact": exact,
        "safe": safe,
    }


def main() -> None:
    args = parse_args()
    api_key = bailian_api_key(args)
    if not api_key:
        raise SystemExit("DASHSCOPE_API_KEY is unavailable")
    cases = load_manifest(args.manifest)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if str(case["id"]) in requested]
        missing = requested - {str(case["id"]) for case in cases}
        if missing:
            raise SystemExit(f"Unknown case id(s): {', '.join(sorted(missing))}")
    models = args.models or list(DEFAULT_MODELS)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(model, case) for model in models for case in cases]
    raw_results: list[dict[str, Any]] = []
    workers = max(1, min(int(args.workers), len(jobs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(request_case, args, api_key, model, case): (model, case["id"])
            for model, case in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            model, case_id = futures[future]
            try:
                result = future.result()
                raw_results.append(result)
                print(
                    f"{model} {case_id}: {result['decision']['decision']} "
                    f"{result['decision']['confidence']} ({result['elapsed_seconds']}s)",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"{model} {case_id}: ERROR {exc}", file=sys.stderr)
                raw_results.append({
                    "case_id": case_id,
                    "model": model,
                    "error": str(exc),
                    "elapsed_seconds": None,
                })
    case_by_id = {str(case["id"]): case for case in cases}
    scored = [
        score_result(case_by_id[result["case_id"]], result)
        if "decision" in result else {**result, "expected": case_by_id[result["case_id"]]["expected"], "exact": False, "safe": True}
        for result in raw_results
    ]
    scored.sort(key=lambda item: (models.index(item["model"]), item["case_id"]))
    summaries = []
    for model in models:
        items = [item for item in scored if item["model"] == model]
        completed = [item for item in items if "decision" in item]
        exact_count = sum(bool(item.get("exact")) for item in items)
        unsafe_count = sum(not bool(item.get("safe", True)) for item in items)
        elapsed = sum(float(item.get("elapsed_seconds") or 0.0) for item in completed)
        summaries.append({
            "model": model,
            "cases": len(items),
            "completed": len(completed),
            "exact": exact_count,
            "accuracy": round(exact_count / len(items), 4) if items else 0.0,
            "unsafe_false_cuts": unsafe_count,
            "total_model_seconds": round(elapsed, 3),
            "mean_model_seconds": round(elapsed / len(completed), 3) if completed else None,
        })
    report = {
        "manifest": str(args.manifest),
        "models": models,
        "scoring": {
            "cut": "cut + high confidence + screen none/redundant",
            "keep": "keep + high confidence",
            "review": "not counted as an exact automatic decision",
        },
        "summaries": summaries,
        "results": scored,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
