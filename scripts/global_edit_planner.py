#!/usr/bin/env python3
"""Generate high-recall paper-edit candidates from a complete transcript.

The planner never creates final cuts. It asks a long-context reasoning model
to find abandoned takes, restarts, and genuinely duplicate explanations across
the whole recording. The bounded candidates then go through the existing
audio/video reviewer and semantic veto in ``gemini_edit_candidates.py``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
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


DEFAULT_MODEL = "google/gemini-3.5-flash"
PLANNER_VERSION = 11
LEADING_SILENCE_RETAINED_FOR_LOCAL_CLEANUP_S = 0.22
END_PUNCTUATION = re.compile(r"[。！？!?；;]$")
SOFT_PUNCTUATION = re.compile(r"[，,：:]$")
VALID_CATEGORIES = {
    "abandoned_take",
    "explicit_restart",
    "duplicate_take",
    "self_correction",
    "delivery_cleanup",
    "recording_meta",
    "failed_demo_narration",
    "screen_pause",
    "content_compression",
}


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find whole-transcript paper-edit candidates with ZenMux."
    )
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument(
        "--preferences",
        type=Path,
        help="Optional creator preference file with hand-edited cut examples.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--video",
        type=Path,
        help="Optional full aligned MP4 with microphone audio for global video review.",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--editorial-only",
        action="store_true",
        help="Run a focused creator-style content-compression discovery pass.",
    )
    parser.add_argument(
        "--max-candidate-ms",
        type=float,
        default=90_000.0,
        help="Reject unbounded planner spans longer than this (default: 90s).",
    )
    return parser.parse_args()


def api_key_from_args(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("ZENMUX_API_KEY", "")
    if not key and args.api_key_file.exists():
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key and not args.dry_run:
        fail(f"ZenMux key not found in the environment or {args.api_key_file}.")
    return key


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip()


def transcript_atoms(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split ASR sentences into timestamped clauses without inventing clocks."""
    atoms: list[dict[str, Any]] = []
    for segment in segments:
        words = [
            word
            for word in (segment.get("words") or [])
            if word.get("start") is not None
            and word.get("end") is not None
            and _word_text(word)
        ]
        if not words:
            text = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            if text and end > start:
                atoms.append({"start": start, "end": end, "text": text})
            continue

        current: list[dict[str, Any]] = []
        for index, word in enumerate(words):
            current.append(word)
            text = "".join(_word_text(item) for item in current)
            start = float(current[0]["start"])
            end = float(current[-1]["end"])
            next_start = (
                float(words[index + 1]["start"])
                if index + 1 < len(words)
                else None
            )
            token = _word_text(word)
            hard_boundary = bool(END_PUNCTUATION.search(token))
            soft_boundary = bool(SOFT_PUNCTUATION.search(token)) and (
                len(text) >= 10
                or end - start >= 2.5
                or (len(text) <= 5 and end - start <= 1.0)
            )
            gap_boundary = next_start is not None and next_start - end >= 0.55
            duration_boundary = end - start >= 10.0
            if hard_boundary or soft_boundary or gap_boundary or duration_boundary:
                atoms.append({"start": start, "end": end, "text": text})
                current = []
        if current:
            atoms.append(
                {
                    "start": float(current[0]["start"]),
                    "end": float(current[-1]["end"]),
                    "text": "".join(_word_text(item) for item in current),
                }
            )

    atoms.sort(key=lambda item: (item["start"], item["end"]))
    for index, atom in enumerate(atoms, start=1):
        atom["id"] = f"U{index:04d}"
    return atoms


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000.0))
    minutes, remainder = divmod(milliseconds, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def build_prompt(
    atoms: list[dict[str, Any]],
    *,
    video_supplied: bool = False,
    creator_cut_examples: list[dict[str, Any]] | None = None,
) -> str:
    rows = "\n".join(
        f"[{item['id']} {timestamp(item['start'])}-{timestamp(item['end'])}] {item['text']}"
        for item in atoms
    )
    schema = {
        "edits": [
            {
                "remove_start_id": "U0001",
                "remove_end_id": "U0002",
                "remove_start_s": "optional numeric seconds for a silent range",
                "remove_end_s": "optional numeric seconds for a silent range",
                "cut_until_id": "U0003 or null",
                "replacement_ids": ["U0010"],
                "removed_quote": "verbatim words copied from the removed IDs",
                "replacement_quote": "verbatim words copied from replacement IDs",
                "category": (
                    "abandoned_take | explicit_restart | duplicate_take | "
                    "self_correction | delivery_cleanup | recording_meta | failed_demo_narration | "
                    "screen_pause"
                ),
                "confidence": "high | medium | low",
                "reason": "specific evidence that the range is disposable",
            }
        ]
    }
    return f"""
You are making a PAPER EDIT for a Mandarin talking-head screen tutorial.
The full transcript is below as timestamped atomic utterances. Find every
plausible contiguous range that is a recording mistake, while preserving the
creator's intended explanation.

This is candidate generation, not final deletion. Favor recall, but every
candidate still needs concrete structural evidence.

{"A complete aligned video with microphone audio is attached. Use both the audio and screen, and inspect the entire timeline." if video_supplied else "No full video is attached in this pass; rely on transcript structure only."}

Include:
- an abandoned or stumbled earlier take followed by a clean restart;
- an explicit instruction to restart or recording meta-talk;
- an earlier duplicate take whose intended information is fully present in a
  later cleaner take;
- a local self-correction where the first wording is clearly superseded;
- a short dangling connector, repeated syllable, hesitation, or delivery
  fragment whose removal makes the surrounding spoken sentence more fluent
  without losing a claim. Listen to the audio instead of relying only on ASR
  punctuation;
- a very short standalone transition acknowledgement at the edge of a long
  pause when it carries no claim and the video/audio make a clean direct splice;
- narration belonging only to a failed screen demo before the demo restarts.
{"- silent waiting/setup/navigation where the final state remains visible and watching the intermediate action teaches nothing;\n- trailing dead air after the final useful sentence." if video_supplied else ""}

Failed-take grouping:
- Treat one failed narration/demo attempt as a sequence, not as isolated words.
  When its apparently useful screen action or result is shown again in the clean
  replacement take, propose the complete disposable attempt from its earliest
  unique utterance through the transition before the clean restart.
- If the safe boundary is genuinely ambiguous, return both a tight speech-only
  candidate and a broader complete-attempt alternative. This is a high-recall
  discovery pass; the later personalized full-video arbiter will choose which
  complete range, if any, is safe.
- Do not omit a broader failed-take alternative merely because an interior
  click, preview, or generated result looks useful in isolation. It is redundant
  when the later retained take clearly recreates the same viewer-facing value.

Do NOT include:
- a fluent discourse marker merely because it is short. A filler such as 呃/嗯
  is eligible only when it is acoustically isolated and removing it produces a
  clean splice;
- fluent explanations merely because they are wordy;
- a repeated passage that adds a claim, example, number, warning, result, or
  troubleshooting detail;
{"- a click, command, generated result, or UI transition that viewers need to see;" if video_supplied else "- screen-navigation silence (it is handled by another subsystem);"}
- intentional reading/display time after the speaker invites viewers to pause,
  screenshot, read details, compare outputs, inspect a result, or score examples;
- screen-switching gaps in a sequential showcase of different products, model
  outputs, slides, or examples: each newly shown result is content even when
  the speaker is silent while it remains on screen;
- stylistic shortening without evidence of a recording mistake.

Semantic safety:
- An ASR segment boundary or pause is not evidence that a sentence was
  abandoned. The following words may simply complete the same sentence.
- An unusual or possibly mistranscribed model/product name is not proof of a
  spoken mistake. Require an audible restart or explicit correction.
- If a complete useful clause is followed by a short dangling fragment, bound
  the candidate to the fragment; never remove the preceding complete clause.

Boundaries:
- remove_start_id and remove_end_id are inclusive and must reference existing
  IDs in one contiguous earlier range.
- For a purely silent screen_pause, use numeric remove_start_s/remove_end_s
  instead of utterance IDs. Keep the span tight and use video timestamps.
- cut_until_id is the first utterance that must remain after the cut. Use it
  when dead air between the failed take and clean restart should also vanish.
- replacement_ids identify the later clean take or correction that preserves
  the meaning. For a high-confidence short local self_correction or
  delivery_cleanup, replacement_ids may be empty only when cut_until_id is the
  immediately following utterance and the remaining sentence is complete.
- removed_quote and replacement_quote must be short VERBATIM substrings copied
  from the corresponding transcript rows. They are mandatory grounding checks,
  not paraphrases. For screen_pause, leave both quotes empty.
- Never include a replacement utterance inside the removed range.
- Return overlapping alternatives separately only when their boundaries are
  genuinely ambiguous.

Return strict JSON only in this shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

FULL TRANSCRIPT:
{rows}
""".strip()


def build_editorial_prompt(
    atoms: list[dict[str, Any]],
    *,
    video_supplied: bool = False,
    creator_cut_examples: list[dict[str, Any]] | None = None,
) -> str:
    rows = "\n".join(
        f"[{item['id']} {timestamp(item['start'])}-{timestamp(item['end'])}] {item['text']}"
        for item in atoms
    )
    schema = {
        "edits": [{
            "remove_start_id": "U0001",
            "remove_end_id": "U0002",
            "cut_until_id": "U0003",
            "replacement_ids": [],
            "removed_quote": "verbatim words copied from the removed IDs",
            "replacement_quote": "",
            "category": "content_compression",
            "confidence": "high | medium | low",
            "reason": "why this creator would remove the complete passage",
        }]
    }
    return f"""
You have exactly ONE job: propose EDITORIAL COMPRESSION candidates for this
creator's Mandarin screen tutorial. Do not search for stutters, false starts,
silent pauses, or recording mistakes; another pass already handles them.

Find 5-20 fluent but optional passages that resemble what this creator removed
in other hand-edited videos: redundant summaries, repeated implications,
personal asides, tangents, overlong setup, or detail beyond the core point.
This is candidate discovery only. A second full-video model and safety gates
will reject unsafe ideas, so favor recall while keeping every range concrete.

Each candidate must:
- be a contiguous 2.5-45 second range;
- use remove_start_id/remove_end_id from the transcript and set cut_until_id to
  the immediately following retained utterance;
- use category content_compression and leave replacement_ids empty;
- include a short verbatim removed_quote;
- preserve all indispensable claims, examples, numbers, warnings, results,
  instructions, and viewer-facing screen actions outside the proposed range.

{("A complete aligned video is attached. Use its audio and screen when judging optionality." if video_supplied else "Use transcript structure and creator examples.")}

HAND-EDITED CUT EXAMPLES FROM OTHER VIDEOS:
{json.dumps(creator_cut_examples or [], ensure_ascii=False)}

Return strict JSON only:
{json.dumps(schema, ensure_ascii=False, indent=2)}

FULL TRANSCRIPT:
{rows}
""".strip()


def _response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        fail("ZenMux response contains no choices.")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    fail("ZenMux response contains no textual content.")


def grounding_text(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？、；;：:（）()《》<>\"'“”‘’]+", "", text).lower()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def planner_signature(
    model: str,
    atoms: list[dict[str, Any]],
    video: Path | None = None,
    preference_signature: str | None = None,
    editorial_only: bool = False,
) -> str:
    payload = {
        "planner_version": PLANNER_VERSION,
        "model": model,
        "atoms": [
            [item["id"], item["start"], item["end"], item["text"]]
            for item in atoms
        ],
        "video_sha256": file_sha256(video) if video else None,
        "preference_signature": preference_signature,
        "editorial_only": editorial_only,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def grounded_silent_range(
    start: float,
    end: float,
    atoms: list[dict[str, Any]],
    *,
    speech_margin_s: float = 0.08,
) -> tuple[float, float] | None:
    """Return the longest transcript-grounded silence inside a visual proposal."""
    occupied = sorted(
        (
            max(start, float(atom["start"]) - speech_margin_s),
            min(end, float(atom["end"]) + speech_margin_s),
        )
        for atom in atoms
        if float(atom["end"]) + speech_margin_s > start
        and float(atom["start"]) - speech_margin_s < end
    )
    merged: list[tuple[float, float]] = []
    for left, right in occupied:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))

    gaps: list[tuple[float, float]] = []
    cursor = start
    for left, right in merged:
        if left > cursor:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append((cursor, end))
    return max(gaps, key=lambda item: item[1] - item[0]) if gaps else None


def candidates_from_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    *,
    model: str,
    max_candidate_ms: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {str(item["id"]): item for item in atoms}
    positions = {str(item["id"]): index for index, item in enumerate(atoms)}
    timeline_end = max(float(item["end"]) for item in atoms)
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in plan.get("edits") or []:
        if not isinstance(raw, dict):
            continue
        start_id = str(raw.get("remove_start_id") or "")
        end_id = str(raw.get("remove_end_id") or "")
        category = str(raw.get("category") or "")
        confidence = str(raw.get("confidence") or "low").lower()
        if (
            category not in VALID_CATEGORIES
            or confidence not in {"high", "medium", "low"}
        ):
            rejected.append({"proposal": raw, "reason": "invalid_ids_or_fields"})
            continue

        cut_until_id = str(raw.get("cut_until_id") or "")
        timed_pause = category == "screen_pause" and (
            raw.get("remove_start_s") is not None
            and raw.get("remove_end_s") is not None
        )
        if timed_pause:
            try:
                start = float(raw["remove_start_s"])
                end = float(raw["remove_end_s"])
            except (TypeError, ValueError):
                rejected.append({"proposal": raw, "reason": "invalid_time_range"})
                continue
            grounded_pause = grounded_silent_range(start, end, atoms)
            if grounded_pause is None:
                rejected.append({"proposal": raw, "reason": "no_grounded_silence"})
                continue
            start, end = grounded_pause
            spoken_end = end
            removed_atoms: list[dict[str, Any]] = []
            key = (f"{start:.3f}", f"{end:.3f}", category)
        else:
            if (
                start_id not in by_id
                or end_id not in by_id
                or positions[start_id] > positions[end_id]
            ):
                rejected.append({"proposal": raw, "reason": "invalid_ids_or_fields"})
                continue
            start = float(by_id[start_id]["start"])
            spoken_end = float(by_id[end_id]["end"])
            end = spoken_end
            if cut_until_id in by_id and positions[cut_until_id] > positions[end_id]:
                proposed_end = float(by_id[cut_until_id]["start"])
                if proposed_end - spoken_end <= 20.0:
                    end = proposed_end
            removed_atoms = atoms[positions[start_id] : positions[end_id] + 1]
            key = (start_id, end_id, cut_until_id)
        replacement_ids = [
            str(item)
            for item in (raw.get("replacement_ids") or [])
            if str(item) in by_id
            and (
                timed_pause
                or not (
                    positions[start_id]
                    <= positions[str(item)]
                    <= positions[end_id]
                )
            )
        ]
        planner_range_narrowed_to_tail = False
        short_transition_cleanup = False
        semantic_spoken_start = start
        removed_quote_value = str(raw.get("removed_quote") or "").strip()
        if (
            not timed_pause
            and category == "delivery_cleanup"
            and confidence == "high"
            and not replacement_ids
            and cut_until_id in by_id
            and positions[cut_until_id] == positions[end_id] + 1
            and len(removed_atoms) > 1
        ):
            tail_atom = removed_atoms[-1]
            preceding_text = "".join(
                str(item.get("text") or "") for item in removed_atoms[:-1]
            ).strip()
            tail_text = str(tail_atom.get("text") or "").strip()
            tail_start = float(tail_atom["start"])
            tail_spoken_end = float(tail_atom["end"])
            tail_end = float(by_id[cut_until_id]["start"])
            if (
                len(grounding_text(tail_text)) <= 10
                and len(grounding_text(preceding_text)) >= 8
                and bool((END_PUNCTUATION.search(preceding_text) or SOFT_PUNCTUATION.search(preceding_text)))
                and not END_PUNCTUATION.search(tail_text)
                and tail_spoken_end - tail_start <= 1.5
                and tail_end - tail_start <= 2.5
            ):
                # A model may correctly identify a dangling tail but include the
                # useful clause before it. Preserve that clause and keep only
                # the tightly bounded tail as a review candidate.
                original_start_id = start_id
                start_id = end_id
                start = tail_start
                semantic_spoken_start = tail_start
                spoken_end = tail_spoken_end
                end = tail_end
                removed_atoms = [tail_atom]
                key = (start_id, end_id, cut_until_id)
                removed_quote_value = tail_text
                planner_range_narrowed_to_tail = True

        if (
            not timed_pause
            and category == "delivery_cleanup"
            and confidence == "high"
            and not replacement_ids
            and cut_until_id in by_id
            and positions[cut_until_id] == positions[end_id] + 1
            and len(removed_atoms) == 1
        ):
            atom = removed_atoms[0]
            position = positions[str(atom["id"])]
            atom_text = str(atom.get("text") or "").strip()
            atom_start = float(atom["start"])
            atom_end = float(atom["end"])
            previous_end = (
                float(atoms[position - 1]["end"]) if position > 0 else 0.0
            )
            leading_gap = atom_start - previous_end
            if (
                position > 0
                and len(grounding_text(atom_text)) <= 4
                and atom_end - atom_start <= 1.0
                and leading_gap >= 0.5
            ):
                # The model has semantically authorized a tiny transition word.
                # Include the adjacent leading dead air while retaining the same
                # pacing cushion used by ordinary pause edits.
                semantic_spoken_start = atom_start
                start = min(
                    atom_start,
                    previous_end + LEADING_SILENCE_RETAINED_FOR_LOCAL_CLEANUP_S,
                )
                short_transition_cleanup = True

        duration_ms = (end - start) * 1000.0
        minimum_duration_ms = 800.0 if timed_pause else 600.0
        if (
            duration_ms < minimum_duration_ms
            or duration_ms > max_candidate_ms
            or start < 0.0
            or end > timeline_end + 10.0
        ):
            rejected.append({"proposal": raw, "reason": "unsafe_duration"})
            continue

        replacementless_local_cleanup = (
            category in {"self_correction", "delivery_cleanup"}
            and confidence == "high"
            and cut_until_id in by_id
            and positions[cut_until_id] == positions[end_id] + 1
            and duration_ms <= 2_500.0
            and len(removed_atoms) == 1
            and (
                short_transition_cleanup
                or not END_PUNCTUATION.search(
                    str(removed_atoms[0].get("text") or "")
                )
            )
        )
        replacementless_content_compression = (
            category == "content_compression"
            and cut_until_id in by_id
            and positions[cut_until_id] == positions[end_id] + 1
            and 2_500.0 <= duration_ms <= 45_000.0
        )
        if (
            category not in {"recording_meta", "screen_pause"}
            and not replacement_ids
            and not replacementless_local_cleanup
            and not replacementless_content_compression
        ):
            rejected.append({"proposal": raw, "reason": "missing_external_replacement"})
            continue

        if key in seen:
            continue
        seen.add(key)
        replacement_text = "".join(by_id[item]["text"] for item in replacement_ids)
        removed_text = (
            "[silent screen pause]"
            if timed_pause
            else "".join(item["text"] for item in removed_atoms)
        )
        if not timed_pause:
            removed_quote = grounding_text(removed_quote_value)
            replacement_quote = grounding_text(
                str(raw.get("replacement_quote") or "")
            )
            minimum_quote_length = 1 if short_transition_cleanup else 2
            if (
                len(removed_quote) < minimum_quote_length
                or removed_quote not in grounding_text(removed_text)
            ):
                rejected.append({"proposal": raw, "reason": "removed_quote_mismatch"})
                continue
            if replacement_ids and (
                len(replacement_quote) < 2
                or replacement_quote not in grounding_text(replacement_text)
            ):
                rejected.append(
                    {"proposal": raw, "reason": "replacement_quote_mismatch"}
                )
                continue
        candidate: dict[str, Any] = {
            "id": f"global_{len(candidates) + 1:03d}",
            "type": "global_paper_edit",
            "start": start,
            "end": end,
            "start_ms": round(start * 1000.0),
            "end_ms": round(end * 1000.0),
            "duration_ms": round(duration_ms),
            "spoken_start": semantic_spoken_start,
            "spoken_end": spoken_end,
            "spoken_start_ms": round(semantic_spoken_start * 1000.0),
            "spoken_end_ms": round(spoken_end * 1000.0),
            "removed_text": removed_text,
            "kept_text": replacement_text,
            "planner_category": category,
            "planner_confidence": confidence,
            "planner_reason": str(raw.get("reason") or "").strip(),
            "removed_quote": removed_quote_value,
            "replacement_quote": str(raw.get("replacement_quote") or "").strip(),
            "replacement_ids": replacement_ids,
            "cut_until_id": cut_until_id or None,
            "replacementless_local_cleanup": replacementless_local_cleanup,
            "replacementless_content_compression": (
                replacementless_content_compression
            ),
            "planner_range_narrowed_to_tail": planner_range_narrowed_to_tail,
            "short_transition_cleanup": short_transition_cleanup,
            "planner_model": model,
        }
        if planner_range_narrowed_to_tail:
            candidate["planner_original_start_id"] = original_start_id
        if replacement_ids:
            candidate["kept_start"] = float(by_id[replacement_ids[0]]["start"])
            candidate["kept_end"] = float(by_id[replacement_ids[-1]]["end"])
        candidates.append(candidate)
    return candidates, rejected


def main() -> None:
    args = parse_args()
    segments = load_transcript(args.transcript)
    atoms = transcript_atoms(segments)
    if not atoms:
        fail("Transcript contains no timestamped utterances.")
    if args.video:
        if not args.video.exists():
            fail(f"Video does not exist: {args.video}")
        if args.video.stat().st_size > 80 * 1024 * 1024:
            fail("Inline ZenMux video is limited to 80MB in this workflow.")
    preferences: dict[str, Any] = {}
    if args.preferences:
        if not args.preferences.exists():
            fail(f"Creator preferences do not exist: {args.preferences}")
        preferences = json.loads(args.preferences.read_text(encoding="utf-8"))
    creator_cut_examples = [
        item
        for item in (preferences.get("manual_cut_examples") or [])
        if args.editorial_only and isinstance(item, dict)
    ]
    prompt_builder = build_editorial_prompt if args.editorial_only else build_prompt
    prompt = prompt_builder(
        atoms,
        video_supplied=bool(args.video),
        creator_cut_examples=creator_cut_examples,
    )
    preference_signature = (
        str(preferences.get("signature") or "") or None
        if args.editorial_only
        else None
    )
    signature = planner_signature(
        args.model,
        atoms,
        args.video,
        preference_signature,
        args.editorial_only,
    )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    request_path = args.work_dir / "global_planner_request.redacted.json"
    response_path = args.work_dir / "global_planner_response.raw.json"
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
    request_payload = {
        "model": args.model,
        "preferences": str(args.preferences) if args.preferences else None,
        "preference_signature": preference_signature,
        "editorial_only": args.editorial_only,
        "messages": [
            {
                "role": "system",
                "content": "You are a meticulous video paper editor. Return strict JSON only.",
            },
            {"role": "user", "content": user_content},
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
        plan = {"edits": []}
        response: dict[str, Any] = {"dry_run": True}
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
        "planner_version": PLANNER_VERSION,
        "transcript": str(args.transcript),
        "video": str(args.video) if args.video else None,
        "model": args.model,
        "signature": signature,
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
