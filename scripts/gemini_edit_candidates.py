#!/usr/bin/env python3
"""
Use Gemini as a reviewer for spoken-video edit candidates.

This script does not edit a Screen Studio project. It reads an ASR transcript,
builds likely cut candidates for fillers, false starts, and repeats, optionally
extracts nearby video frames, asks Gemini to judge the candidates, then writes:

- a full JSON report with model decisions
- a process.py-compatible cuts JSON containing only high-confidence cuts
"""

from __future__ import annotations

import argparse
import base64
import difflib
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
DEFAULT_API_KEY_FILE = Path.home() / ".zenmux_api_key"
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}

FILLER_CHARS = set("嗯呃啊额诶唉哦噢")
FILLER_LATIN = {"em", "um", "uh", "er", "hmm"}
SOFT_FILLERS = {"然后", "就是", "这个", "那个", "其实", "的话", "对吧"}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask Gemini to review video edit candidates.")
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript JSON from bailian_transcribe.py or process.py.")
    parser.add_argument("--video", type=Path, help="Optional exported video for frame evidence.")
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
                        help="Candidates per request (default: 6; native video batches are capped at 4).")
    parser.add_argument("--coordinate-space", choices=["source", "edited"], required=True,
                        help="Required clock declaration. Project edit transcripts use source; exported edited videos use edited.")
    parser.add_argument("--project-json", type=Path,
                        help="Required for edited-time cuts; fingerprints the slice map used for conversion.")
    parser.add_argument("--review-backend", choices=["auto", "gemini", "zenmux"], default="auto",
                        help="Use native Gemini video review when GEMINI_API_KEY is available, else ZenMux frames.")
    parser.add_argument("--gemini-api-key", default="", help="Optional direct Gemini key. Prefer GEMINI_API_KEY.")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--context-window", type=float, default=4.0, help="Seconds of transcript context on each side.")
    parser.add_argument("--frame-window", type=float, default=0.8, help="Seconds before/after candidate midpoint for frames.")
    parser.add_argument("--clip-context", type=float, default=1.5,
                        help="Audio/video seconds before and after native Gemini evidence clips.")
    parser.add_argument("--min-cut-ms", type=float, default=160.0)
    parser.add_argument("--max-auto-cut-ms", type=float, default=None,
                        help="Deprecated global limit; type-specific limits are used by default.")
    parser.add_argument("--max-filler-cut-ms", type=float, default=2500.0)
    parser.add_argument("--max-false-start-cut-ms", type=float, default=12000.0)
    parser.add_argument("--max-duplicate-cut-ms", type=float, default=30000.0)
    parser.add_argument("--repeat-window", type=float, default=60.0,
                        help="Look-back window for repeated takes (default: 60s).")
    parser.add_argument("--repeat-span-segments", type=int, default=3,
                        help="Maximum adjacent ASR sentences in one repeat span (default: 3).")
    parser.add_argument("--dry-run", action="store_true", help="Build candidates and request logs without calling Gemini.")
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
        if groups and item[1] - groups[-1][-1][2] <= 0.55:
            groups[-1].append(item)
        else:
            groups.append([item])

    candidates = []
    for group in groups:
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
        })
    return candidates


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
            if score < 0.76:
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


REPAIR_MARKERS = {"不对", "不是", "应该", "重新", "再来", "我的意思是", "也就是说"}


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
        start = float(before[0]["start"])
        end = float(word["end"]) + 0.06
        add_candidate(candidates, {
            "id": f"repair_{len(candidates) + 1:03d}",
            "type": "explicit_self_correction",
            "start": start,
            "end": end,
            "removed_text": candidate_text(words, start, end),
            "kept_text": "".join(word_text(item) for item in after),
            "context": transcript_context(segments, start, float(after[-1]["end"]), window),
        })
    return candidates


def candidate_priority(item: dict[str, Any]) -> float:
    type_weight = {
        "near_duplicate_segment": 4.0,
        "possible_false_start": 3.5,
        "explicit_self_correction": 3.4,
        "filler_cluster": 2.8,
        "hard_filler": 2.4,
        "soft_filler": 1.4,
    }
    return type_weight.get(str(item.get("type")), 1.0) + float(item.get("similarity", 0.0))


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


def build_candidates(args: argparse.Namespace, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words = flatten_words(segments)
    candidates = []
    candidates.extend(group_nearby_fillers(words, segments, args.context_window))
    candidates.extend(false_start_candidates(words, segments, args.context_window))
    candidates.extend(repair_marker_candidates(words, segments, args.context_window))
    candidates.extend(repeated_segment_candidates(
        segments,
        args.context_window,
        repeat_window=args.repeat_window,
        max_span_segments=args.repeat_span_segments,
    ))

    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda c: (c["start_ms"], c["end_ms"], c["type"])):
        key = (round(item["start_ms"] / 120), round(item["end_ms"] / 120), item["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

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


def extract_clip(video: Path, start: float, end: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.2, end - start)
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(video),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale='min(960,iw)':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
        str(output),
    ])


def attach_clips(args: argparse.Namespace, candidates: list[dict[str, Any]]) -> None:
    """Attach short, continuous audio/video evidence for native Gemini review."""
    if not args.video:
        return
    if not args.video.exists():
        fail(f"video does not exist: {args.video}")
    duration = ffprobe_duration(args.video)
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
            extract_clip(args.video, clip_start, clip_end, path)
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
                "note": "brief explanation",
            }
        ],
        "summary": "",
    }
    user_text = f"""
Review these edit candidates for a Chinese short-form screen-recording tutorial.

Rules:
- Cut high-confidence standalone fillers, abandoned starts, and duplicate takes.
- Duplicate candidates may provide two removal_options. Choose exactly ONE option (the worse/redundant take), return boundaries inside that option, and never cut both takes.
- Keep or mark review if the screen likely changes during the range.
- Keep if the repeated wording adds context, warning, result, or troubleshooting detail.
- Do not expand a cut beyond the candidate's provided range or chosen removal_option.
- Prefer "review" over "cut" when visual evidence is unclear.
- Audio/visual evidence is {"provided for each candidate" if video_supplied else "not provided; rely only on transcript and be conservative"}.

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
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == attempts - 1:
                body = exc.read().decode("utf-8", errors="replace")
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
    max_false_start_cut_ms: float = 12000.0,
    max_duplicate_cut_ms: float = 30000.0,
) -> list[dict[str, Any]]:
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
        if max_auto_cut_ms is not None:
            limit = max_auto_cut_ms
        elif candidate_type in {"hard_filler", "soft_filler", "filler_cluster"}:
            limit = max_filler_cut_ms
        elif candidate_type == "near_duplicate_segment":
            limit = max_duplicate_cut_ms
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
            "reason": str(decision.get("reason") or candidate.get("type") or "gemini_candidate"),
            "candidate_type": candidate_type,
            "selected_take": (chosen_option or {}).get("label"),
            "confidence": "high",
            "note": str(decision.get("note") or ""),
        })
    return sorted(cuts, key=lambda c: (c["start_ms"], c["end_ms"]))


def review_batches(
    candidates: list[dict[str, Any]],
    *,
    max_count: int,
    max_media_bytes: int | None = None,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for candidate in candidates:
        media_bytes = sum(
            Path(clip["path"]).stat().st_size
            for clip in candidate.get("clips") or []
            if Path(clip["path"]).exists()
        )
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


def main() -> None:
    args = parse_args()
    if args.coordinate_space == "edited" and not args.project_json:
        fail("--coordinate-space edited requires --project-json so cuts can be mapped safely.")
    if args.project_json and not args.project_json.exists():
        fail(f"project.json does not exist: {args.project_json}")
    segments = load_transcript(args.transcript)
    candidates = build_candidates(args, segments)
    if not candidates:
        fail("No edit candidates found. Try a raw transcript that keeps filler words.")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    direct_key = gemini_api_key_from_args(args)
    backend = args.review_backend
    if backend == "auto":
        backend = "gemini" if direct_key else "zenmux"
    if backend == "gemini":
        if not direct_key and not args.dry_run:
            fail("GEMINI_API_KEY is not set for native audio/video review.")
        attach_clips(args, candidates)
        api_key = direct_key
    else:
        attach_frames(args, candidates)
        api_key = api_key_from_args(args)

    decisions: list[dict[str, Any]] = []
    batch_reports: list[dict[str, Any]] = []
    batch_size = max(1, min(args.batch_size, 4 if backend == "gemini" and args.video else args.batch_size))
    batches = review_batches(
        candidates,
        max_count=batch_size,
        # Inline video requests support <100 MB; leave headroom for base64 and text.
        max_media_bytes=65 * 1024 * 1024 if backend == "gemini" and args.video else None,
    )
    for batch_index, batch in enumerate(batches, start=1):
        log(f"Reviewing batch {batch_index} ({len(batch)} candidate(s)) via {backend}...")
        if backend == "gemini":
            batch_report = run_native_gemini(
                args, api_key, batch, batch_index=batch_index
            )
        else:
            messages = build_messages(batch, video_supplied=bool(args.video))
            batch_report = run_gemini(
                args, api_key, messages, batch_index=batch_index
            )
        decisions.extend(batch_report.get("decisions") or [])
        batch_reports.append(batch_report)

    model_report = {
        "backend": backend,
        "model": args.gemini_model if backend == "gemini" else args.model,
        "decisions": decisions,
        "batches": batch_reports,
    }

    candidates_by_id = {item["id"]: item for item in candidates}
    cuts = cuts_from_decisions(
        model_report.get("decisions") or [],
        candidates_by_id,
        min_cut_ms=args.min_cut_ms,
        max_auto_cut_ms=args.max_auto_cut_ms,
        max_filler_cut_ms=args.max_filler_cut_ms,
        max_false_start_cut_ms=args.max_false_start_cut_ms,
        max_duplicate_cut_ms=args.max_duplicate_cut_ms,
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
        "candidates": candidates,
        "gemini": model_report,
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
        "coordinate_space": args.coordinate_space,
        "report": str(args.output),
        "cuts_output": str(args.cuts_output),
        "work_dir": str(args.work_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
