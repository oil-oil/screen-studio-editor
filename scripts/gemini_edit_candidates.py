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


DEFAULT_API_BASE = "https://zenmux.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.5-flash"
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
    parser.add_argument("--max-candidates", type=int, default=14)
    parser.add_argument("--context-window", type=float, default=4.0, help="Seconds of transcript context on each side.")
    parser.add_argument("--frame-window", type=float, default=0.8, help="Seconds before/after candidate midpoint for frames.")
    parser.add_argument("--min-cut-ms", type=float, default=160.0)
    parser.add_argument("--max-auto-cut-ms", type=float, default=2800.0)
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
    a_chars = set(a_norm)
    b_chars = set(b_norm)
    return len(a_chars & b_chars) / max(1, len(a_chars | b_chars))


def repeated_segment_candidates(segments: list[dict[str, Any]], window: float) -> list[dict[str, Any]]:
    candidates = []
    usable = [
        seg for seg in segments
        if seg.get("start") is not None and seg.get("end") is not None and clean_for_match(str(seg.get("text") or ""))
    ]
    for i in range(len(usable) - 1):
        left = usable[i]
        right = usable[i + 1]
        gap = float(right["start"]) - float(left["end"])
        if gap > 2.5:
            continue
        score = segment_similarity(str(left.get("text") or ""), str(right.get("text") or ""))
        if score < 0.72:
            continue
        start = float(left["start"])
        end = float(left["end"])
        add_candidate(candidates, {
            "id": f"repeat_{len(candidates) + 1:03d}",
            "type": "near_duplicate_segment",
            "start": start,
            "end": end,
            "removed_text": str(left.get("text") or "").strip(),
            "kept_text": str(right.get("text") or "").strip(),
            "similarity": round(score, 3),
            "context": transcript_context(segments, start, float(right["end"]), window),
        })
    return candidates


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
    candidates.extend(repeated_segment_candidates(segments, args.context_window))

    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda c: (c["start_ms"], c["end_ms"], c["type"])):
        key = (round(item["start_ms"] / 120), round(item["end_ms"] / 120), item["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped[: max(1, args.max_candidates)]


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
- Keep or mark review if the screen likely changes during the range.
- Keep if the repeated wording adds context, warning, result, or troubleshooting detail.
- Do not expand a cut beyond the candidate's provided time range.
- Prefer "review" over "cut" when visual evidence is unclear.
- Video frames are {"provided for each candidate" if video_supplied else "not provided; rely only on transcript and be conservative"}.

Return JSON in this exact shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Candidates:
{json.dumps([{k: v for k, v in c.items() if k != "frames"} for c in candidates], ensure_ascii=False, indent=2)}
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


def run_gemini(args: argparse.Namespace, api_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / "gemini_request.redacted.json").write_text(
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
    (args.work_dir / "gemini_response.raw.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    text = response["choices"][0]["message"]["content"]
    parsed = extract_json_from_text(text)
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
    max_auto_cut_ms: float,
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
        start_ms = max(float(candidate["start_ms"]), start_ms)
        end_ms = min(float(candidate["end_ms"]), end_ms)
        duration = end_ms - start_ms
        if duration < min_cut_ms or duration > max_auto_cut_ms:
            continue
        cuts.append({
            "start_ms": round(start_ms),
            "end_ms": round(end_ms),
            "removed_text": str(decision.get("removed_text") or candidate.get("removed_text") or ""),
            "reason": str(decision.get("reason") or candidate.get("type") or "gemini_candidate"),
            "confidence": "high",
            "note": str(decision.get("note") or ""),
        })
    return sorted(cuts, key=lambda c: (c["start_ms"], c["end_ms"]))


def main() -> None:
    args = parse_args()
    segments = load_transcript(args.transcript)
    candidates = build_candidates(args, segments)
    if not candidates:
        fail("No edit candidates found. Try a raw transcript that keeps filler words.")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    attach_frames(args, candidates)
    messages = build_messages(candidates, video_supplied=bool(args.video))
    api_key = api_key_from_args(args)
    model_report = run_gemini(args, api_key, messages)

    candidates_by_id = {item["id"]: item for item in candidates}
    cuts = cuts_from_decisions(
        model_report.get("decisions") or [],
        candidates_by_id,
        min_cut_ms=args.min_cut_ms,
        max_auto_cut_ms=args.max_auto_cut_ms,
    )
    report = {
        "model": args.model,
        "transcript": str(args.transcript),
        "video": str(args.video) if args.video else None,
        "candidates": candidates,
        "gemini": model_report,
        "cuts": cuts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.cuts_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.cuts_output.write_text(json.dumps(cuts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "candidates": len(candidates),
        "cuts": len(cuts),
        "report": str(args.output),
        "cuts_output": str(args.cuts_output),
        "work_dir": str(args.work_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
