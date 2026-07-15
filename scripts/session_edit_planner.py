#!/usr/bin/env python3
"""Find abandoned takes with a cheap session-aware transcript pass.

Screen Studio joins recording sessions into one source timeline.  The full-video
planner sees that continuous proxy, so it can miss that a pause/resume boundary
often marks an abandoned take.  This planner exposes the real session structure
to a text-only model.  Its output is still only a hypothesis: the preference
arbiter and the timeline activity guard review every candidate against video.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from gemini_edit_candidates import (
    DEFAULT_API_BASE,
    DEFAULT_API_KEY_FILE,
    extract_json_from_text,
    load_transcript,
    post_json,
    redact_payload,
)
from global_edit_planner import (
    DEFAULT_MODEL,
    _response_text,
    candidates_from_plan,
    transcript_atoms,
)


SESSION_PLANNER_VERSION = 1
DEFAULT_MAX_CANDIDATE_MS = 300_000.0


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-candidate-ms",
        type=float,
        default=DEFAULT_MAX_CANDIDATE_MS,
        help="Reject a single unbounded take longer than this (default: 300s).",
    )
    return parser.parse_args()


def api_key_from_args(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("ZENMUX_API_KEY", "")
    if not key and args.api_key_file.exists():
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key and not args.dry_run:
        fail(f"ZenMux key not found in the environment or {args.api_key_file}.")
    return key


def session_intervals(project: Path) -> list[dict[str, Any]]:
    metadata_path = project / "recording" / "metadata.json"
    if not metadata_path.exists():
        fail(f"Missing recording metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    recorders = metadata.get("recorders") or []
    microphone = next(
        (
            item
            for item in recorders
            if item.get("type") == "microphone"
            or "microphone" in str(item.get("id") or "")
        ),
        None,
    )
    sessions = sorted(
        (microphone or {}).get("sessions") or [],
        key=lambda item: float(item.get("processTimeStartMs", 0.0)),
    )
    if not sessions:
        fail(f"No microphone sessions in {metadata_path}")
    result: list[dict[str, Any]] = []
    cursor = 0.0
    for index, session in enumerate(sessions, start=1):
        duration = float(session.get("durationMs") or 0.0) / 1000.0
        if duration <= 0.0:
            continue
        result.append({
            "id": f"S{index:02d}",
            "start": cursor,
            "end": cursor + duration,
        })
        cursor += duration
    if not result:
        fail(f"Microphone sessions have no duration in {metadata_path}")
    return result


def session_for_time(seconds: float, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    for session in sessions:
        if float(session["start"]) <= seconds < float(session["end"]):
            return session
    return sessions[-1]


def transcript_rows(
    atoms: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> str:
    rows: list[str] = []
    previous_session = ""
    for atom in atoms:
        session = session_for_time(float(atom["start"]), sessions)
        session_id = str(session["id"])
        if session_id != previous_session:
            rows.append(
                f"=== SESSION {session_id} "
                f"{float(session['start']):.3f}-{float(session['end']):.3f} ==="
            )
            previous_session = session_id
        rows.append(
            f"[{atom['id']} {session_id} "
            f"{float(atom['start']):.3f}-{float(atom['end']):.3f}] {atom['text']}"
        )
    return "\n".join(rows)


def build_prompt(atoms: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> str:
    schema = {
        "edits": [
            {
                "remove_start_id": "U0001",
                "remove_end_id": "U0002",
                "cut_until_id": "U0003 or null",
                "replacement_ids": ["U0010"],
                "removed_quote": "verbatim words from the removed IDs",
                "replacement_quote": "verbatim words from replacement IDs",
                "category": (
                    "abandoned_take | explicit_restart | duplicate_take | "
                    "self_correction | recording_meta | failed_demo_narration"
                ),
                "confidence": "high | medium | low",
                "reason": "specific structural evidence",
            }
        ]
    }
    return f"""
You are a high-recall PAPER EDITOR for a Mandarin talking-head screen tutorial.
The transcript below exposes the recording app's real pause/resume SESSION
boundaries. A boundary is strong structural context: creators often abandon a
whole session, resume, and record the same point again more cleanly.

Find EVERY plausible recording mistake with concrete restart or replacement
evidence. This pass only generates candidates. A separate model will inspect
the complete aligned video and creator preferences before any deletion, so do
not suppress a grounded candidate merely because screen action may exist.

Prioritize:
- a complete earlier session replaced by a later cleaner session;
- a failed take occupying most or all of a session;
- a long subsection repeated, corrected, or completed later;
- local false starts, explicit restart instructions, and recording meta-talk.

Keep fluent unique explanations. A later replacement must preserve every
useful claim, example, number, warning, and result from the removed range.
Session boundaries alone are not proof of a mistake. Do not propose silent
screen pauses here; the video planner handles them.

Boundaries and grounding:
- remove_start_id and remove_end_id are inclusive existing IDs in one
  contiguous earlier range, and may span several sessions;
- cut_until_id is the first utterance that must remain after intervening dead
  air, or null;
- replacement_ids point to the later clean take and must lie outside the
  removed range; use an empty list only for explicit recording meta-talk;
- removed_quote and replacement_quote are short verbatim substrings copied
  from their respective IDs. Never paraphrase them.

Return strict JSON only:
{json.dumps(schema, ensure_ascii=False, indent=2)}

SESSION-AWARE TRANSCRIPT:
{transcript_rows(atoms, sessions)}
""".strip()


def planner_signature(
    model: str,
    atoms: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> str:
    payload = {
        "planner_version": SESSION_PLANNER_VERSION,
        "model": model,
        "atoms": [
            [item["id"], item["start"], item["end"], item["text"]]
            for item in atoms
        ],
        "sessions": sessions,
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def main() -> None:
    args = parse_args()
    project = args.project.expanduser().resolve()
    transcript = args.transcript.expanduser().resolve()
    atoms = transcript_atoms(load_transcript(transcript))
    if not atoms:
        fail("Transcript contains no timestamped utterances.")
    sessions = session_intervals(project)
    prompt = build_prompt(atoms, sessions)
    signature = planner_signature(args.model, atoms, sessions)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    request_path = args.work_dir / "session_planner_request.redacted.json"
    response_path = args.work_dir / "session_planner_response.raw.json"
    request_payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 16_000,
        "response_format": {"type": "json_object"},
    }
    if not args.model.startswith("anthropic/"):
        request_payload["temperature"] = 0
    request_path.write_text(
        json.dumps(
            {"signature": signature, "request": redact_payload(request_payload)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.dry_run:
        response: dict[str, Any] = {"dry_run": True}
        plan = {"edits": []}
    else:
        cached: dict[str, Any] | None = None
        if args.resume and response_path.exists():
            try:
                stored = json.loads(response_path.read_text(encoding="utf-8"))
                if stored.get("signature") == signature:
                    cached = stored
            except (OSError, json.JSONDecodeError, TypeError):
                cached = None
        if cached is not None:
            response = cached["response"]
        else:
            response = post_json(
                f"{args.api_base.rstrip('/')}/chat/completions",
                request_payload,
                api_key_from_args(args),
                args.timeout,
            )
            response_path.write_text(
                json.dumps(
                    {"signature": signature, "response": response},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        plan = extract_json_from_text(_response_text(response))

    candidates, rejected = candidates_from_plan(
        plan,
        atoms,
        model=args.model,
        max_candidate_ms=args.max_candidate_ms,
    )
    report = {
        "schema_version": 1,
        "planner_version": SESSION_PLANNER_VERSION,
        "project": str(project),
        "transcript": str(transcript),
        "model": args.model,
        "signature": signature,
        "session_count": len(sessions),
        "sessions": sessions,
        "atom_count": len(atoms),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "rejected_proposals": rejected,
        "usage": response.get("usage") if isinstance(response, dict) else None,
        "dry_run": args.dry_run,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
