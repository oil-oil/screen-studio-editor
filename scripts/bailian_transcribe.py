#!/usr/bin/env python3
"""
Transcribe audio or video with Bailian FunAudio ASR via the `bl` CLI.

Output format matches local_transcribe.py:
  [{"start": 0.0, "end": 1.23, "text": "...", "words": [...]}]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def _load_user_config() -> dict[str, Any]:
    config_path = Path(
        os.environ.get(
            "SCREEN_STUDIO_EDITOR_CONFIG",
            str(Path.home() / ".config" / "screen-studio-editor" / "config.json"),
        )
    ).expanduser()
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Screen Studio Editor config: {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Screen Studio Editor config must be a JSON object: {config_path}")
    return payload


def _optional_user_path(config: dict[str, Any], key: str, env_key: str) -> Path | None:
    value = str(os.environ.get(env_key) or config.get(key) or "").strip()
    return Path(value).expanduser() if value else None


USER_CONFIG = _load_user_config()
HOTWORDS_PATH = _optional_user_path(
    USER_CONFIG, "hotwords", "SCREEN_STUDIO_EDITOR_HOTWORDS"
)
GLOSSARY_PATH = _optional_user_path(
    USER_CONFIG, "glossary", "SCREEN_STUDIO_EDITOR_GLOSSARY"
)
VOCABULARY_CACHE_PATH = _optional_user_path(
    USER_CONFIG, "vocabulary_cache", "SCREEN_STUDIO_EDITOR_VOCABULARY_CACHE"
) or Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
).expanduser() / "screen-studio-editor" / "vocabulary-cache.json"
VOCABULARY_TARGET_MODEL = "fun-asr"
VOCABULARY_PREFIX = "ssedit"
MAX_SUBTITLE_CHARS = 24
MAX_SUBTITLE_SECONDS = 4.2
TARGET_SUBTITLE_SECONDS = 2.4
SOFT_PUNCT = "，,、；;：:"
HARD_PUNCT = "。！？!?"
FILLER_CHARS = set("呃嗯啊额诶唉哦噢")
FILLER_LATIN = {"em", "emm", "um", "uh", "er", "erm"}
DISPLAY_PUNCT = SOFT_PUNCT + HARD_PUNCT + "…."


def log(message: str):
    print(f"[bailian-transcribe] {message}", flush=True)


def _run(cmd: list[str]):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed: {' '.join(cmd[:3])}\n{detail}")


def _extract_audio(input_path: Path, output_path: Path):
    _run([
        FFMPEG,
        "-y",
        "-i", str(input_path),
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
    ])


def _load_dashscope_api_key() -> str | None:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        return key
    config_path = Path.home() / ".bailian" / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8")).get("api_key")
    except Exception:
        return None


def ensure_hotword_vocabulary() -> str | None:
    """
    Create or reuse a Bailian hot-word vocabulary from the configured file.

    Hot words steer ASR toward the channel's recurring proper nouns (Claude,
    Anthropic, 飞书…) at recognition time — errors that glossary text
    replacement can only partially patch afterwards. The vocabulary ID is
    cached outside the skill repository keyed by a hash of the hot-word list,
    so the remote vocabulary is only created/updated when the list changes.
    Any failure degrades to plain recognition — never block transcription.
    """
    if HOTWORDS_PATH is None or not HOTWORDS_PATH.exists():
        return None
    try:
        hotwords = json.loads(HOTWORDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"WARNING: could not read hot words ({exc}); continuing without them.")
        return None
    if not isinstance(hotwords, list) or not hotwords:
        return None

    digest = hashlib.sha256(
        json.dumps(hotwords, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    cache = {}
    try:
        cache = json.loads(VOCABULARY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    if cache.get("hash") == digest and cache.get("vocabulary_id"):
        return cache["vocabulary_id"]

    api_key = _load_dashscope_api_key()
    if not api_key:
        log("WARNING: no DashScope API key found; continuing without hot words.")
        return None

    try:
        import dashscope
        from dashscope.audio.asr import VocabularyService

        dashscope.api_key = api_key
        service = VocabularyService()
        vocabulary_id = cache.get("vocabulary_id")
        if vocabulary_id:
            try:
                service.update_vocabulary(vocabulary_id, hotwords)
            except Exception:
                vocabulary_id = None  # stale/deleted remotely — recreate below
        if not vocabulary_id:
            vocabulary_id = service.create_vocabulary(
                target_model=VOCABULARY_TARGET_MODEL,
                prefix=VOCABULARY_PREFIX,
                vocabulary=hotwords,
            )
        VOCABULARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        VOCABULARY_CACHE_PATH.write_text(
            json.dumps({"hash": digest, "vocabulary_id": vocabulary_id}),
            encoding="utf-8",
        )
        log(f"Hot-word vocabulary ready ({len(hotwords)} terms): {vocabulary_id}")
        return vocabulary_id
    except Exception as exc:
        log(f"WARNING: hot-word vocabulary unavailable ({exc}); continuing without it.")
        return None


def _glossary_pattern(wrong: str) -> re.Pattern:
    """
    Case-insensitive AND whitespace-tolerant pattern for a glossary entry.
    Spacing drifts at every stage (ASR tokens, external segmenters, CJK/Latin spacing),
    so "GPT55" must also match "GPT 55" and "cloud call" must match
    "cloudcall" — otherwise entries silently stop matching.
    """
    parts = [re.escape(ch) for ch in wrong if not ch.isspace()]
    return re.compile(r"\s*".join(parts), re.IGNORECASE)


def _load_glossary_replacements() -> list[tuple[re.Pattern, str]]:
    """Load configured glossary as case-insensitive replacement patterns."""
    if GLOSSARY_PATH is None or not GLOSSARY_PATH.exists():
        return []
    try:
        entries = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    replacements = []
    for entry in entries if isinstance(entries, list) else []:
        wrong = (entry.get("wrong") or "").strip()
        correct = entry.get("correct")
        if wrong and isinstance(correct, str):
            replacements.append((_glossary_pattern(wrong), correct))
    return replacements


def _apply_glossary(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """
    Apply recurring glossary corrections to segment display text after ASR.

    oil-subtitle applies the same replacements at burn time, so doing it
    here does not change the final captions — it makes the corrections visible
    in the preview editor instead of silently appearing in the burned video,
    and lets the reviewing agent focus on new, unknown errors. Word-level
    tokens keep the raw ASR text; segment "text" is the display source of truth.
    """
    replacements = _load_glossary_replacements()
    if not replacements:
        return segments, 0
    changed = 0
    corrected = []
    for segment in segments:
        text = segment.get("text") or ""
        new_text = text
        for pattern, correct in replacements:
            new_text = pattern.sub(correct, new_text)
        if new_text != text:
            changed += 1
        corrected.append({**segment, "text": new_text})
    return corrected, changed


def _ms(value: Any) -> float:
    return round(float(value or 0) / 1000, 3)


def _word_text(word: dict[str, Any]) -> str:
    text = (word.get("text") or word.get("word") or "").strip()
    punctuation = (word.get("punctuation") or "").strip()
    return (text + punctuation).strip()


def _visual_len(text: str) -> float:
    width = 0.0
    for char in text:
        if "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf":
            width += 1.0
        elif char == " ":
            width += 0.5
        else:
            width += 0.55
    return width


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[\s，,、；;：:。！？!?…+\-_/()（）\"'`]+", "", text).lower()


def _concat_words(words: list[dict[str, Any]]) -> str:
    return "".join((word.get("word") or "").strip() for word in words).strip()


def _filler_core(text: str) -> str:
    return re.sub(rf"[\s{re.escape(DISPLAY_PUNCT)}]+", "", text).lower()


def _is_standalone_filler(text: str) -> bool:
    core = _filler_core(text)
    if not core:
        return False
    if core in FILLER_LATIN:
        return True
    return all(char in FILLER_CHARS for char in core)


def _clean_filler_text(text: str) -> str:
    filler = r"(?:呃+|嗯+|啊+|额+|诶+|唉+|哦+|噢+|em+|um+|uh+|er+)"
    boundary = rf"(^|[\s{re.escape(DISPLAY_PUNCT)}])"
    cleaned = re.sub(
        rf"{boundary}{filler}(?=$|[\s{re.escape(DISPLAY_PUNCT)}])",
        lambda match: match.group(1),
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"([，,、；;：:]){2,}", r"\1", cleaned)
    cleaned = re.sub(r"([。！？!?]){2,}", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ，,、；;：:。！？!?…")


def _strip_display_punctuation(text: str) -> str:
    """Remove punctuation used for timing but not wanted in visible subtitles."""
    return re.sub(rf"[{re.escape(DISPLAY_PUNCT)}]", "", text)


def _clean_display_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    text = _strip_display_punctuation(text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_segment_display_text(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for segment in segments:
        text = _clean_display_text(segment.get("text") or "")
        if text:
            cleaned.append({**segment, "text": text})
    return cleaned


def _clean_fillers(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    cleaned_segments: list[dict[str, Any]] = []
    removed = 0
    for segment in segments:
        words = segment.get("words") or []
        if words:
            kept_words = []
            for word in words:
                if _is_standalone_filler(word.get("word") or ""):
                    removed += 1
                    continue
                kept_words.append(word)
            if not kept_words:
                continue
            cleaned_segments.append({
                **segment,
                "start": round(float(kept_words[0]["start"]), 3),
                "end": round(float(kept_words[-1]["end"]), 3),
                "text": _concat_words(kept_words),
                "words": kept_words,
            })
            continue

        cleaned_text = _clean_filler_text(segment.get("text") or "")
        if cleaned_text != (segment.get("text") or "").strip():
            removed += 1
        if cleaned_text:
            cleaned_segments.append({**segment, "text": cleaned_text})
    return cleaned_segments, removed


def _line_duration(words: list[dict[str, Any]]) -> float:
    if not words:
        return 0.0
    return max(0.0, float(words[-1]["end"]) - float(words[0]["start"]))


_WEAK_START_CHARS = {"的", "了", "着", "过", "们", "吗", "呢", "吧", "啊"}
# Content words that legitimately begin with a particle character — starting a
# subtitle with these is fine ("了解一下", "过程中"…).
_WEAK_START_WHITELIST = (
    "的确", "了解", "了不起", "着重", "着急", "着手", "着眼",
    "过程", "过去", "过后", "过来", "过于", "过年", "过度", "过滤",
)


def _is_weak_start(text: str) -> bool:
    """
    True when a subtitle must not START with this token: a grammatical particle
    gluing it to the previous phrase ("的一个…", "的原因…"). ASR tokens often
    attach 的/了 to the following word, so check the first character, not just
    single-character tokens.
    """
    if not text or text[0] not in _WEAK_START_CHARS:
        return False
    return not text.startswith(_WEAK_START_WHITELIST)


def _is_bare_cjk(text: str) -> bool:
    return len(text) == 1 and bool(re.fullmatch(r"[\u4e00-\u9fff]", text))


def _boundary_score(words: list[dict[str, Any]], start: int, end: int) -> float:
    chunk = words[start:end + 1]
    text = _concat_words(chunk)
    duration = _line_duration(chunk)
    length = _visual_len(text)
    tail = text[-1] if text else ""
    next_word = words[end + 1] if end + 1 < len(words) else None
    end_word_text = (words[end].get("word") or "").strip()
    next_text = (next_word.get("word") or "").strip() if next_word else ""
    gap = max(0.0, float(next_word["start"]) - float(words[end]["end"])) if next_word else 0.0

    score = 0.0
    score -= abs(length - MAX_SUBTITLE_CHARS * 0.78) * 1.4
    score -= abs(duration - TARGET_SUBTITLE_SECONDS) * 3.0

    if tail in HARD_PUNCT:
        score += 38
    if gap >= 0.35:
        score += 28
    elif gap >= 0.22:
        score += 18
    elif tail in SOFT_PUNCT:
        score += 12

    if length < MAX_SUBTITLE_CHARS * 0.38 and next_word:
        score -= 34
    if duration < 1.0 and next_word:
        score -= 34
    if length > MAX_SUBTITLE_CHARS:
        score -= 90 + (length - MAX_SUBTITLE_CHARS) * 14
    if duration > MAX_SUBTITLE_SECONDS:
        score -= (duration - MAX_SUBTITLE_SECONDS) * 30
    if _is_weak_start(next_text):
        score -= 80
    if next_word and _is_bare_cjk(end_word_text) and tail not in SOFT_PUNCT + HARD_PUNCT:
        score -= 28
    # Never break between Latin/digit characters without punctuation or a real
    # pause: ASR tokenizes English words and phrases into arbitrary pieces
    # ("FIVEF" + "IVE"), and a subtitle boundary there splits a word across
    # two subtitles. Only a forced overflow break may do that.
    # "." counts as part of the run so version numbers ("GPT5" + "." + "5")
    # are not split either.
    if (
        next_word is not None
        and re.fullmatch(r"[A-Za-z0-9.]", (end_word_text or " ")[-1])
        and re.fullmatch(r"[A-Za-z0-9.]", (next_text or " ")[0])
        and tail not in SOFT_PUNCT + HARD_PUNCT
        and gap < 0.22
    ):
        score -= 250
    if next_word is None:
        score += 20
    return score


def _split_words(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    start = 0
    while start < len(words):
        best_end = start
        best_score = float("-inf")
        for end in range(start, len(words)):
            chunk = words[start:end + 1]
            text = _concat_words(chunk)
            duration = _line_duration(chunk)
            length = _visual_len(text)
            if end > start and (length > MAX_SUBTITLE_CHARS * 1.08 or duration > MAX_SUBTITLE_SECONDS):
                break
            score = _boundary_score(words, start, end)
            if score > best_score:
                best_score = score
                best_end = end
        chunks.append(words[start:best_end + 1])
        start = best_end + 1
    return chunks


def _split_text_chunks(text: str, max_chars: float = MAX_SUBTITLE_CHARS) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    if not text or _visual_len(text) <= max_chars:
        return [text] if text else []

    parts: list[str] = []

    def add_piece(piece: str):
        piece = piece.strip()
        if not piece:
            return
        if _visual_len(piece) <= max_chars:
            parts.append(piece)
            return
        soft_chunks = [p.strip() for p in re.split(r"(?<=[，,、；;：:])\s*", piece) if p.strip()]
        if len(soft_chunks) > 1:
            for chunk in soft_chunks:
                add_piece(chunk)
            return
        current = ""
        for char in piece:
            if current and _visual_len(current + char) > max_chars:
                parts.append(current.strip())
                current = char
            else:
                current += char
        if current.strip():
            parts.append(current.strip())

    hard_chunks = [p.strip() for p in re.split(r"(?<=[。！？!?])\s*", text) if p.strip()]
    for chunk in hard_chunks:
        add_piece(chunk)
    return parts or [text]


def _split_text_proportionally(segment: dict[str, Any]) -> list[dict[str, Any]]:
    start = float(segment["start"])
    end = float(segment["end"])
    duration = max(0.01, end - start)
    chunks = _split_text_chunks(segment["text"])
    min_parts = max(1, math.ceil(duration / MAX_SUBTITLE_SECONDS))
    if len(chunks) < min_parts:
        target_width = max(10.0, min(float(MAX_SUBTITLE_CHARS), _visual_len(segment["text"]) / min_parts))
        chunks = _split_text_chunks(segment["text"], target_width)
    if len(chunks) <= 1:
        return [segment]
    weights = [max(1.0, _visual_len(chunk)) for chunk in chunks]
    total = sum(weights)
    result = []
    cursor = start
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        next_end = end if index == len(chunks) - 1 else cursor + duration * (weight / total)
        result.append({
            "start": round(cursor, 3),
            "end": round(next_end, 3),
            "text": chunk,
            "words": [],
        })
        cursor = next_end
    return result


def _append_short_segment(target: list[dict[str, Any]], segment: dict[str, Any]):
    duration = float(segment["end"]) - float(segment["start"])
    if duration > MAX_SUBTITLE_SECONDS or _visual_len(segment["text"]) > MAX_SUBTITLE_CHARS:
        target.extend(_split_text_proportionally({**segment, "words": []}))
    else:
        target.append(segment)


def _shorten_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shortened: list[dict[str, Any]] = []
    for segment in segments:
        words = segment.get("words") or []
        text = (segment.get("text") or "").strip()
        needs_split = (
            _visual_len(text) > MAX_SUBTITLE_CHARS
            or float(segment["end"]) - float(segment["start"]) > MAX_SUBTITLE_SECONDS
        )
        if not needs_split:
            shortened.append(segment)
            continue

        if words and _normalize_for_match(_concat_words(words)) == _normalize_for_match(text):
            for chunk_words in _split_words(words):
                _append_short_segment(shortened, {
                    "start": round(float(chunk_words[0]["start"]), 3),
                    "end": round(float(chunk_words[-1]["end"]), 3),
                    "text": _concat_words(chunk_words),
                    "words": chunk_words,
                })
        else:
            for item in _split_text_proportionally(segment):
                _append_short_segment(shortened, item)

    shortened.sort(key=lambda item: (item["start"], item["end"]))
    return shortened


def _rule_split_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep subtitle splitting deterministic and local when explicitly requested."""
    return _shorten_segments(segments)


def _convert_bailian_result(data: dict[str, Any]) -> list[dict[str, Any]]:
    transcripts = data.get("transcripts") or []
    if not transcripts:
        return []

    segments: list[dict[str, Any]] = []
    for sentence in transcripts[0].get("sentences") or []:
        text = (sentence.get("text") or "").strip()
        if not text:
            continue

        words = []
        for word in sentence.get("words") or []:
            clean_text = _word_text(word)
            if not clean_text:
                continue
            begin = word.get("begin_time", word.get("start"))
            end = word.get("end_time", word.get("end"))
            if begin is None or end is None:
                continue
            words.append({
                "word": clean_text,
                "start": _ms(begin),
                "end": _ms(end),
            })

        begin = sentence.get("begin_time", sentence.get("start"))
        end = sentence.get("end_time", sentence.get("end"))
        segments.append({
            "start": _ms(begin),
            "end": _ms(end),
            "text": text,
            "words": words,
        })

    segments.sort(key=lambda item: (item["start"], item["end"]))
    return segments


def transcribe_file(
    input_path: Path,
    *,
    output_path: Path | None = None,
    language: str | None = "zh",
    raw_output_path: Path | None = None,
    clean_fillers: bool = True,
    use_hotwords: bool = True,
    vocabulary_id: str | None = None,
    apply_glossary: bool = True,
    split_mode: str = "raw",
) -> list[dict[str, Any]]:
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_output_path:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)

    if vocabulary_id is None and use_hotwords:
        vocabulary_id = ensure_hotword_vocabulary()

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / "audio_for_bailian.wav"
        log("Extracting 16 kHz mono audio...")
        _extract_audio(input_path, audio_path)

        raw_path = raw_output_path or (Path(tmpdir) / "bailian_asr.json")
        cmd = [
            "bl",
            "speech",
            "recognize",
            "--url", str(audio_path),
            "--out", str(raw_path),
            "--quiet",
        ]
        if language:
            cmd.extend(["--language", language])
        if vocabulary_id:
            cmd.extend(["--vocabulary-id", vocabulary_id])

        lang_display = language or "auto"
        log(f"Transcribing with Bailian FunAudio ASR (language={lang_display}"
            + (", hot words on" if vocabulary_id else "") + ")...")
        _run(cmd)

        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)

    raw_segments = _convert_bailian_result(raw)
    cleaned_segments, removed_fillers = (
        _clean_fillers(raw_segments) if clean_fillers else (raw_segments, 0)
    )
    if split_mode == "raw":
        # Editing needs the original ASR sentence structure, punctuation and
        # every word token. Subtitle cleanup would destroy evidence used to
        # find fillers, false starts and repeated takes.
        segments = cleaned_segments
    else:
        segments = _clean_segment_display_text(_rule_split_segments(cleaned_segments))
    glossary_hits = 0
    if apply_glossary:
        segments, glossary_hits = _apply_glossary(segments)
    if output_path:
        output_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Saved transcript: {output_path}")
    log(
        f"Transcribed {len(raw_segments)} ASR sentence(s), removed {removed_fillers} filler token(s), "
        f"applied glossary to {glossary_hits} segment(s), "
        f"returned {len(segments)} transcript segment(s), {sum(len(s.get('words', [])) for s in segments)} words."
    )
    return segments


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio/video with Bailian FunAudio ASR")
    parser.add_argument("input", help="Local audio or video file path")
    parser.add_argument("--output", required=True, help="Output transcript.json path")
    parser.add_argument("--language", default="zh", help="Language code, e.g. zh. Use None to omit language.")
    parser.add_argument("--raw-output", default=None, help="Optional path to save raw Bailian ASR JSON")
    parser.add_argument("--keep-fillers", action="store_true",
                        help="Keep standalone filler words such as 呃/嗯/啊 in the transcript.")
    parser.add_argument("--no-hotwords", action="store_true",
                        help="Disable the configured hot-word vocabulary.")
    parser.add_argument("--hotwords", default=None,
                        help="Hot-word JSON path; overrides environment and user config.")
    parser.add_argument("--vocabulary-id", default=None,
                        help="Use a specific Bailian hot-word vocabulary ID instead of the managed one.")
    parser.add_argument("--vocabulary-cache", default=None,
                        help="Managed vocabulary cache path; overrides environment and user config.")
    parser.add_argument("--no-glossary", action="store_true",
                        help="Do not apply configured glossary corrections to the transcript text.")
    parser.add_argument("--glossary", default=None,
                        help="Glossary JSON path; overrides environment and user config.")
    parser.add_argument("--split-mode", choices=["rules", "raw"], default="raw",
                        help="Subtitle line splitting: 'rules' or 'raw' (default; keeps original ASR sentences).")
    args = parser.parse_args()

    global HOTWORDS_PATH, GLOSSARY_PATH, VOCABULARY_CACHE_PATH
    if args.hotwords:
        HOTWORDS_PATH = Path(args.hotwords).expanduser()
    if args.glossary:
        GLOSSARY_PATH = Path(args.glossary).expanduser()
    if args.vocabulary_cache:
        VOCABULARY_CACHE_PATH = Path(args.vocabulary_cache).expanduser()

    language = None if args.language == "None" else args.language
    try:
        transcribe_file(
            Path(args.input),
            output_path=Path(args.output),
            language=language,
            raw_output_path=Path(args.raw_output) if args.raw_output else None,
            clean_fillers=not args.keep_fillers,
            use_hotwords=not args.no_hotwords,
            vocabulary_id=args.vocabulary_id,
            apply_glossary=not args.no_glossary,
            split_mode=args.split_mode,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
