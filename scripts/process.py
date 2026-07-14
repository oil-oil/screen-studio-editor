#!/usr/bin/env python3
"""
Screen Studio Auto-Editor
Removes pauses and repeated narration from a .screenstudio project and
normalizes the canvas/camera layout. Captions are intentionally NOT enabled
here; burned subtitles are produced separately by burn_subtitles.py (Mode B).
"""

import argparse
import array
import json
import math
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from editing_core import (
    CutsValidationError,
    file_sha256,
    load_cuts_document,
    merge_intervals,
    protect_cuts_with_activity,
)


def log(msg):
    print(f"[screen-studio-editor] {msg}", flush=True)


def _file_sha256(path: Path) -> str:
    """Hash a file's bytes — used to detect external edits between runs."""
    return file_sha256(path)


def backup_project(project_json_path: Path):
    bak = project_json_path.with_suffix(".json.bak")
    if bak.exists():
        log(f"⚠️  Backup already exists at {bak}, skipping backup.")
        return
    shutil.copy2(project_json_path, bak)
    log(f"✅ Backed up project.json → {bak.name}")


def load_metadata(project_dir: Path) -> dict:
    """Load recording/metadata.json to get session timing info."""
    metadata_path = project_dir / "recording" / "metadata.json"
    with open(metadata_path) as f:
        return json.load(f)


def get_mic_sessions(metadata: dict) -> list[dict]:
    """Return microphone sessions sorted by processTimeStartMs."""
    sessions = []
    for recorder in metadata.get("recorders", []):
        if recorder.get("type") == "microphone" or "microphone" in recorder.get("id", ""):
            for s in recorder.get("sessions", []):
                sessions.append(s)
    if not sessions:
        # fallback: look for sessions in input recorder
        for recorder in metadata.get("recorders", []):
            if "input" in recorder.get("id", ""):
                for s in recorder.get("sessions", []):
                    sessions.append(s)
    return sorted(sessions, key=lambda s: s["processTimeStartMs"])


def get_recorder_sessions(metadata: dict, recorder_type: str) -> list[dict]:
    sessions = []
    for recorder in metadata.get("recorders", []):
        if recorder.get("type") == recorder_type or recorder_type in str(recorder.get("id", "")):
            sessions.extend(recorder.get("sessions", []))
    return sorted(sessions, key=lambda session: session.get("processTimeStartMs", 0))


def timeline_offset_for_process_time(process_time_ms: float, session_offsets: list[dict]) -> float:
    """Anchor an input/display event to the microphone-derived source timeline."""
    if not session_offsets:
        return max(0.0, process_time_ms)
    session = min(
        session_offsets,
        key=lambda item: abs(float(item.get("processTimeStartMs", 0)) - process_time_ms),
    )
    return float(session["timelineOffsetMs"]) + max(
        0.0, process_time_ms - float(session.get("processTimeStartMs", process_time_ms))
    )


_EVENT_TIME_KEYS = (
    "processtimems", "processtime", "process_time_ms", "process_time",
    "timestampms", "timestamp", "timems", "time_ms", "offsetms", "offset_ms", "time",
    "unixms", "unixtime", "unix_time_ms",
)


def _walk_event_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_event_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_event_dicts(child)


def _event_relative_ms(key: str, value: float, session: dict) -> float | None:
    key_norm = key.lower().replace("-", "_")
    duration_ms = float(session.get("durationMs", 0) or 0)
    process_start = float(session.get("processTimeStartMs", 0) or 0)
    unix_start = float(session.get("unixStartMs", 0) or 0)
    if "unix" in key_norm or value > 100_000_000_000:
        return value - unix_start if unix_start else None
    if "process" in key_norm:
        return value - process_start if process_start and value >= process_start else value
    if process_start and value >= process_start - 1000 and value <= process_start + duration_ms + 5000:
        return value - process_start
    # Decimal values that fit the session in seconds are overwhelmingly seconds.
    if duration_ms and value <= duration_ms / 1000.0 + 5 and not float(value).is_integer():
        return value * 1000.0
    return value


def load_input_activity_intervals(
    project_dir: Path,
    metadata: dict,
    session_offsets: list[dict],
    *,
    pad_ms: float = 850.0,
) -> list[tuple[float, float]]:
    """Read keyboard/click event files and return protected source-time ranges.

    Mouse-move streams are intentionally ignored: normal cursor travel would
    protect nearly the whole recording.  Clicks and keystrokes are strong
    evidence that a silent interval contains an intentional tutorial action.
    """
    input_sessions = get_recorder_sessions(metadata, "input")
    recording_dir = project_dir / "recording"
    intervals: list[tuple[float, float]] = []
    loaded_files: set[Path] = set()
    for index, session in enumerate(input_sessions):
        candidates: list[Path] = []
        for key in ("mouseClicksFilename", "keyStrokesFilename", "keystrokesFilename"):
            if session.get(key):
                candidates.append(recording_dir / str(session[key]))
        candidates.extend([
            recording_dir / f"mouseclicks-{index}.json",
            recording_dir / f"keystrokes-{index}.json",
        ])
        source_anchor = timeline_offset_for_process_time(
            float(session.get("processTimeStartMs", 0)), session_offsets
        )
        for path in candidates:
            if path in loaded_files or not path.exists():
                continue
            loaded_files.add(path)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log(f"⚠️  Could not parse input activity {path.name}: {exc}")
                continue
            file_events = 0
            for event in _walk_event_dicts(payload):
                normalized = {str(key).lower().replace("-", "_"): (key, value) for key, value in event.items()}
                chosen = None
                for wanted in _EVENT_TIME_KEYS:
                    normalized_wanted = wanted.lower().replace("-", "_")
                    if normalized_wanted in normalized:
                        chosen = normalized[normalized_wanted]
                        break
                if chosen is None:
                    continue
                raw_key, raw_value = chosen
                if not isinstance(raw_value, (int, float)):
                    continue
                relative_ms = _event_relative_ms(str(raw_key), float(raw_value), session)
                if relative_ms is None or relative_ms < -1000:
                    continue
                event_ms = source_anchor + max(0.0, relative_ms)
                intervals.append((max(0.0, event_ms - pad_ms), event_ms + pad_ms))
                file_events += 1
            if file_events:
                log(f"⌨️  Protected {file_events} click/keystroke event(s) from {path.name}.")
    return merge_intervals(intervals, gap_ms=120.0)


def _display_source_path(project_dir: Path, session: dict) -> Path | None:
    recording_dir = project_dir / "recording"
    filename = str(session.get("outputFilename") or "")
    if filename:
        direct = recording_dir / filename
        if direct.exists():
            return direct
        playlist = direct.with_suffix(".m3u8")
        if playlist.exists():
            return playlist
    return None


def detect_visual_activity_intervals(
    project_dir: Path,
    metadata: dict,
    session_offsets: list[dict],
    *,
    fps: float = 2.5,
    scene_threshold: float = 0.012,
    pad_ms: float = 900.0,
) -> list[tuple[float, float]]:
    """Detect meaningful display changes with a low-resolution ffmpeg scan."""
    display_sessions = get_recorder_sessions(metadata, "display")
    intervals: list[tuple[float, float]] = []
    for index, session in enumerate(display_sessions):
        source = _display_source_path(project_dir, session)
        if source is None:
            continue
        process_start = float(session.get("processTimeStartMs", 0))
        source_anchor = timeline_offset_for_process_time(process_start, session_offsets)
        vf = f"fps={fps},scale=320:-2,select='gt(scene,{scene_threshold})',showinfo"
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(source),
             "-vf", vf, "-an", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(f"⚠️  Visual activity scan failed for display session {index}; input-event protection remains active.")
            continue
        found = 0
        for match in re.finditer(r"pts_time:([0-9.]+)", result.stderr):
            event_ms = source_anchor + float(match.group(1)) * 1000.0
            intervals.append((max(0.0, event_ms - pad_ms), event_ms + pad_ms))
            found += 1
        if found:
            log(f"🖥️  Protected {found} display-change event(s) in session {index}.")
    return merge_intervals(intervals, gap_ms=150.0)


def _probe_duration_ms(path: Path) -> float | None:
    """Return the media duration in ms via ffprobe, or None on failure."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip()) * 1000.0
    except (ValueError, AttributeError):
        return None


def merge_audio(project_dir: Path, sessions: list[dict], output_path: Path, tmp_dir: Path) -> list[dict]:
    """
    Merge all microphone segments into a single WAV (concatenated, no gap padding).

    Two coordinate systems are involved and they are NOT the same:
      - timelineOffsetMs: where this session starts in the slice source timeline.
        Screen Studio lays sessions back-to-back by metadata durationMs
        (verified against pristine multi-session projects: the single original
        slice ends exactly at the sum of metadata durations).
      - audioOffsetMs: where this session starts in the merged WAV. The actual
        audio files run ~70-100ms longer than metadata durationMs per session
        (occasionally seconds on older recordings), so this uses the decoded
        WAV length of each session, not the metadata value.

    Each session is decoded to its own 16 kHz mono WAV first so audioOffsetMs
    is sample-exact by construction, then the WAVs are concatenated.
    """
    recording_dir = project_dir / "recording"
    valid_sessions = []
    timeline_offset_ms = 0.0
    audio_offset_ms = 0.0

    for idx, session in enumerate(sessions):
        filename = session.get("outputFilename", "")
        mic_file = recording_dir / filename
        if not mic_file.exists():
            log(f"⚠️  Audio file not found: {filename}, skipping.")
            # The slice timeline still reserves this session's span, so keep
            # advancing the timeline offset even though there is no audio.
            timeline_offset_ms += session["durationMs"]
            continue

        session_wav = tmp_dir / f"session_{idx}.wav"
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(mic_file),
             "-ar", "16000", "-ac", "1", str(session_wav), "-y"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed decoding {filename}: {result.stderr}")

        real_ms = _probe_duration_ms(session_wav)
        if real_ms is None:
            log(f"⚠️  Could not measure decoded length of {filename}; using metadata duration.")
            real_ms = session["durationMs"]

        drift_ms = real_ms - session["durationMs"]
        if abs(drift_ms) > 50:
            log(f"ℹ️  Session {idx}: audio runs {drift_ms:+.0f}ms vs metadata "
                f"(cumulative timeline correction {audio_offset_ms - timeline_offset_ms:+.0f}ms).")

        valid_sessions.append({
            "processTimeStartMs": session["processTimeStartMs"],
            "durationMs": session["durationMs"],
            "timelineOffsetMs": timeline_offset_ms,
            "audioOffsetMs": audio_offset_ms,
            "realDurationMs": real_ms,
            "file": str(session_wav),
        })
        timeline_offset_ms += session["durationMs"]
        audio_offset_ms += real_ms

    if not valid_sessions:
        raise RuntimeError("No microphone audio files found in this project.")

    n = len(valid_sessions)
    inputs = []
    for s in valid_sessions:
        inputs.extend(["-i", s["file"]])

    concat_filter = "".join([f"[{j}:a]" for j in range(n)])
    concat_filter += f"concat=n={n}:v=0:a=1[outa]"

    cmd = inputs + [
        "-filter_complex", concat_filter,
        "-map", "[outa]",
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
        "-y",
    ]

    log(f"🎵 Merging {n} audio segment(s)...")
    result = subprocess.run(["ffmpeg", "-loglevel", "error"] + cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")

    log(f"✅ Merged audio → {output_path.name}")
    return valid_sessions


def transcribe(
    audio_path: Path,
    language: str | None = "zh",
    backend: str = "bailian",
    raw_output_path: Path | None = None,
) -> list[dict]:
    """
    Run ASR on the audio file.
    Returns list of segments with word-level timestamps:
      [{"start": float, "end": float, "text": str, "words": [{"word": str, "start": float, "end": float}]}]

    language: ISO 639-1 code (e.g. 'zh', 'en') or None for auto-detect.
    Defaults to 'zh' to keep Mandarin recordings in Simplified Chinese.
    Pass --language en for English recordings, or --language None to auto-detect.
    """
    lang_display = language if language else "auto-detect"
    if backend == "bailian":
        log(f"🎙️  Transcribing edit transcript with Bailian FunAudio ASR (language={lang_display})...")
        from bailian_transcribe import transcribe_file

        segments = transcribe_file(
            audio_path,
            language=language,
            raw_output_path=raw_output_path,
            clean_fillers=False,
            apply_glossary=False,
            split_mode="raw",
        )
    elif backend == "local":
        log(f"🎙️  Transcribing locally (language={lang_display})...")
        from local_transcribe import transcribe_file

        segments = transcribe_file(audio_path, language=language)
    else:
        raise RuntimeError(f"Unsupported ASR backend: {backend}")
    log(f"✅ Transcribed {len(segments)} segments, {sum(len(s.get('words', [])) for s in segments)} words.")
    return segments


def map_audio_time_to_timeline_ms(audio_offset_s: float, session_offsets: list[dict]) -> float:
    """
    Convert a timestamp in the merged audio (seconds) to slice-timeline ms.

    Screen Studio lays sessions back-to-back by metadata durationMs in the
    slice source timeline, but each session's actual audio runs longer than
    its metadata duration (typically +70..100ms, sometimes seconds). The
    merged WAV therefore drifts further from the slice timeline with every
    session. Map piecewise: find the session containing this audio timestamp
    by real audio offsets, then re-anchor it at that session's timeline offset.
    Within a session the clocks tick at the same rate, so only the anchor moves.
    """
    u = audio_offset_s * 1000.0
    for s in reversed(session_offsets):
        if u >= s["audioOffsetMs"]:
            # Clamp to the session's timeline span: the audio tail that runs
            # past metadata durationMs has no timeline position of its own.
            delta = min(u - s["audioOffsetMs"], s["durationMs"])
            return s["timelineOffsetMs"] + delta
    return u


def map_timeline_time_to_audio_ms(timeline_ms: float, session_offsets: list[dict]) -> float:
    """Inverse of ``map_audio_time_to_timeline_ms`` inside a recording session."""
    for session in reversed(session_offsets):
        start = float(session["timelineOffsetMs"])
        end = start + float(session["durationMs"])
        if start <= timeline_ms <= end:
            return float(session["audioOffsetMs"]) + min(max(0.0, timeline_ms - start), float(session["durationMs"]))
    return max(0.0, timeline_ms)


class WaveformQuietPointFinder:
    """Find low-energy edit points in the already-decoded 16 kHz mono WAV."""

    def __init__(self, path: Path):
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise RuntimeError("boundary refinement requires a mono 16-bit PCM WAV")
            self.sample_rate = handle.getframerate()
            samples = array.array("h")
            samples.frombytes(handle.readframes(handle.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        self.samples = samples

    def rms_at_ms(self, audio_ms: float, window_ms: float = 24.0) -> float:
        center = int(audio_ms * self.sample_rate / 1000.0)
        radius = max(1, int(window_ms * self.sample_rate / 2000.0))
        start = max(0, center - radius)
        end = min(len(self.samples), center + radius)
        if end <= start:
            return float("inf")
        return math.sqrt(sum(sample * sample for sample in self.samples[start:end]) / (end - start))

    def quietest_timeline_ms(
        self,
        start_ms: float,
        end_ms: float,
        session_offsets: list[dict],
        *,
        step_ms: float = 8.0,
    ) -> float | None:
        if end_ms <= start_ms:
            return None
        best: tuple[float, float, float] | None = None
        midpoint = (start_ms + end_ms) / 2.0
        point = start_ms
        while point <= end_ms + 0.01:
            audio_ms = map_timeline_time_to_audio_ms(point, session_offsets)
            score = self.rms_at_ms(audio_ms)
            candidate = (score, abs(point - midpoint), point)
            if best is None or candidate < best:
                best = candidate
            point += step_ms
        return best[2] if best else None


def refine_repeat_cut_boundaries(
    cuts: list[dict],
    words: list[dict],
    audio_path: Path,
    session_offsets: list[dict],
    *,
    search_ms: float = 240.0,
    speech_guard_ms: float = 35.0,
) -> list[dict]:
    """Place semantic cuts in nearby quiet gaps without clipping removed words.

    ASR/model ranges describe *what* should disappear; waveform minima decide
    where the splice should land.  Unlike the old fixed inward 150 ms padding,
    this keeps the whole filler/repeated phrase while protecting its neighbours.
    """
    if not cuts:
        return []
    try:
        quiet = WaveformQuietPointFinder(audio_path)
    except Exception as exc:
        log(f"⚠️  Could not load waveform for boundary refinement: {exc}; keeping reviewed boundaries.")
        return cuts

    ordered_words = [
        w for w in words
        if w.get("start") is not None and w.get("end") is not None
    ]
    refined: list[dict] = []
    for cut in cuts:
        start_ms = float(cut["start_ms"])
        end_ms = float(cut["end_ms"])
        overlapping_indices = [
            index for index, word in enumerate(ordered_words)
            if float(word["end"]) * 1000.0 > start_ms
            and float(word["start"]) * 1000.0 < end_ms
        ]
        if overlapping_indices:
            first = overlapping_indices[0]
            last = overlapping_indices[-1]
            first_start = float(ordered_words[first]["start"]) * 1000.0
            last_end = float(ordered_words[last]["end"]) * 1000.0
            previous_end = (
                float(ordered_words[first - 1]["end"]) * 1000.0 + speech_guard_ms
                if first > 0 else max(0.0, first_start - search_ms)
            )
            next_start = (
                float(ordered_words[last + 1]["start"]) * 1000.0 - speech_guard_ms
                if last + 1 < len(ordered_words) else last_end + search_ms
            )
            start_window = (
                max(0.0, previous_end, first_start - search_ms),
                max(0.0, first_start - speech_guard_ms),
            )
            end_window = (
                last_end + speech_guard_ms,
                min(next_start, last_end + search_ms),
            )
        else:
            start_window = (max(0.0, start_ms - search_ms), start_ms + search_ms)
            end_window = (max(start_ms, end_ms - search_ms), end_ms + search_ms)

        new_start = quiet.quietest_timeline_ms(*start_window, session_offsets) or start_ms
        new_end = quiet.quietest_timeline_ms(*end_window, session_offsets) or end_ms
        if new_end - new_start < 120.0:
            log(f"⚠️  Unsafe refined cut skipped: {start_ms/1000:.2f}s→{end_ms/1000:.2f}s")
            continue
        refined.append({
            **cut,
            "start_ms": new_start,
            "end_ms": new_end,
            "reviewed_start_ms": start_ms,
            "reviewed_end_ms": end_ms,
            "boundary_refined": True,
        })
    return refined


def remap_segments_to_timeline(segments: list[dict], session_offsets: list[dict]) -> list[dict]:
    """
    Rewrite freshly transcribed segment/word timestamps (merged-audio seconds)
    into slice-timeline seconds, so transcript.json and every downstream
    consumer (cuts.json, wordless-slice checks) share the slices' coordinates.
    Only call this on fresh ASR output — reused transcripts are already mapped.
    """
    def _map_s(t: float | None) -> float | None:
        if t is None:
            return None
        return round(map_audio_time_to_timeline_ms(float(t), session_offsets) / 1000.0, 3)

    remapped = []
    for seg in segments:
        new_seg = dict(seg)
        new_seg["start"] = _map_s(seg.get("start"))
        new_seg["end"] = _map_s(seg.get("end"))
        new_seg["words"] = [
            {**w, "start": _map_s(w.get("start")), "end": _map_s(w.get("end"))}
            for w in seg.get("words", [])
        ]
        remapped.append(new_seg)
    return remapped


def remap_regions_to_timeline(
    regions: list[tuple[float, float]], session_offsets: list[dict]
) -> list[tuple[float, float]]:
    """Rewrite silence regions (merged-audio seconds) into slice-timeline seconds."""
    return [
        (
            map_audio_time_to_timeline_ms(start_s, session_offsets) / 1000.0,
            map_audio_time_to_timeline_ms(end_s, session_offsets) / 1000.0,
        )
        for start_s, end_s in regions
    ]


def protect_words_from_cuts(
    cuts: list[dict],
    words: list[dict],
    pad_ms: float = 60.0,
    min_cut_ms: float = 300.0,
) -> list[dict]:
    """
    Trim or split pause cuts so they never remove ASR-recognized speech.

    Silence detection is the cut source, but a mis-set threshold (or quiet
    speech) can classify real words as silence. Words are the safety net:
    subtract every word interval (with padding) from each cut and keep only
    the remaining pieces that are still worth cutting. Fillers were already
    stripped from the transcript, so filler-only stretches still get cut.
    """
    if not cuts or not words:
        return cuts

    intervals = sorted(
        (w["start"] * 1000.0 - pad_ms, w["end"] * 1000.0 + pad_ms)
        for w in words
        if w.get("start") is not None and w.get("end") is not None
    )

    protected = []
    trimmed = 0
    for cut in cuts:
        pieces = [(cut["start_ms"], cut["end_ms"])]
        for (ws, we) in intervals:
            if we <= cut["start_ms"] or ws >= cut["end_ms"]:
                continue
            new_pieces = []
            for (ps, pe) in pieces:
                if we <= ps or ws >= pe:
                    new_pieces.append((ps, pe))
                    continue
                if ws > ps:
                    new_pieces.append((ps, ws))
                if we < pe:
                    new_pieces.append((we, pe))
            pieces = new_pieces
        kept_pieces = [(ps, pe) for (ps, pe) in pieces if pe - ps >= min_cut_ms]
        if len(kept_pieces) != 1 or kept_pieces[0] != (cut["start_ms"], cut["end_ms"]):
            trimmed += 1
        for (ps, pe) in kept_pieces:
            piece = dict(cut)
            piece["start_ms"] = ps
            piece["end_ms"] = pe
            protected.append(piece)

    if trimmed:
        log(f"🛡️  Word protection adjusted {trimmed} pause cut(s) that overlapped recognized speech.")
    return protected


def flatten_words(segments: list[dict]) -> list[dict]:
    """Return ASR words in timeline order for labeling cuts."""
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            if w.get("start") is None or w.get("end") is None:
                continue
            words.append(w)
    return sorted(words, key=lambda w: (w["start"], w["end"]))


def split_retained_pause(min_pause_ms: float) -> tuple[float, float]:
    """
    Split the retained silence asymmetrically around a jump cut.

    Leaving the same amount of silence after the cut makes Screen Studio edits feel
    like they still have a blank tail. Keep more of the breath before the cut and
    snap the next slice closer to the following word.
    """
    total_ms = max(0.0, min_pause_ms)
    keep_after_ms = min(80.0, total_ms * 0.33)
    keep_before_ms = max(0.0, total_ms - keep_after_ms)
    return keep_before_ms / 1000.0, keep_after_ms / 1000.0


def label_cut_with_words(cut: dict, words: list[dict]) -> dict:
    """Attach nearest transcript words to a cut for review logs."""
    if not words:
        cut["text_before"] = ""
        cut["text_after"] = ""
        return cut

    cut_start_s = cut["start_ms"] / 1000.0
    cut_end_s = cut["end_ms"] / 1000.0
    before = ""
    after = ""

    for w in words:
        if w["end"] <= cut_start_s:
            before = (w.get("word") or "").strip()
        elif w["start"] >= cut_end_s:
            after = (w.get("word") or "").strip()
            break

    cut["text_before"] = before
    cut["text_after"] = after
    return cut


def detect_pauses_from_silence(
    silence_regions: list[tuple[float, float]],
    threshold_ms: float,
    min_pause_ms: float,
    segments: list[dict],
) -> list[dict]:
    """
    Build pause cuts from measured silent regions.

    ASR word timestamps are useful context, but they drift by tens or hundreds of
    milliseconds. Real silence is the safer source of truth for jump cuts.
    Both silence regions and segments must already be in slice-timeline coordinates.
    """
    words = flatten_words(segments)
    pauses = []

    for silence_start_s, silence_end_s in silence_regions:
        region_ms = (silence_end_s - silence_start_s) * 1000.0
        if region_ms <= threshold_ms:
            continue

        keep_before_s, keep_after_s = split_retained_pause(min_pause_ms)
        cut_start_s = silence_start_s + keep_before_s
        cut_end_s = silence_end_s - keep_after_s
        cut_duration_ms = (cut_end_s - cut_start_s) * 1000.0

        # Tiny removals tend to create visible timeline noise without improving pacing.
        if cut_duration_ms < 300:
            continue

        cut = {
            "start_ms": cut_start_s * 1000.0,
            "end_ms": cut_end_s * 1000.0,
            "duration_ms": region_ms,
            "source": "silence",
        }
        pauses.append(label_cut_with_words(cut, words))

    log(f"🔍 Found {len(pauses)} measured silence pause(s) > {threshold_ms}ms to cut.")
    return pauses


def detect_pauses_from_asr(segments: list[dict], threshold_ms: float, min_pause_ms: float) -> list[dict]:
    """
    Detect pauses between words longer than threshold_ms.
    Returns list of {"start_process_ms": ..., "end_process_ms": ..., "duration_ms": ...}
    representing ranges to cut (leaving min_pause_ms of silence).
    """
    words = flatten_words(segments)

    if not words:
        return []

    pauses = []
    for i in range(len(words) - 1):
        gap_start_s = words[i]["end"]
        gap_end_s = words[i + 1]["start"]
        gap_ms = (gap_end_s - gap_start_s) * 1000.0

        if gap_ms > threshold_ms:
            # Keep the retained pause asymmetric so the post-cut slice gets into
            # speech quickly. ASR mode still adds speech padding because word
            # boundaries can drift by 50-300ms.
            # Extra 80ms padding guards against ASR timestamp inaccuracy (±50-200ms):
            # ASR can report word boundaries slightly early or late, so leaving
            # an extra buffer prevents accidentally clipping the tail of the preceding
            # word or the onset of the following word.
            SPEECH_PAD_S = 0.08
            keep_before_s, keep_after_s = split_retained_pause(min_pause_ms)
            cut_start_s = gap_start_s + keep_before_s + SPEECH_PAD_S
            cut_end_s = gap_end_s - keep_after_s - SPEECH_PAD_S

            cut_duration_ms = (cut_end_s - cut_start_s) * 1000.0
            # Skip cuts shorter than 300ms: ASR timestamp inaccuracy (±50-200ms)
            # means a tiny cut is more likely to clip real speech than remove silence.
            if cut_end_s > cut_start_s and cut_duration_ms >= 300:
                pauses.append({
                    "start_ms": cut_start_s * 1000.0,
                    "end_ms": cut_end_s * 1000.0,
                    "duration_ms": gap_ms,
                    "text_before": words[i]["word"].strip(),
                    "text_after": words[i + 1]["word"].strip(),
                })

    log(f"🔍 Found {len(pauses)} ASR word-gap pause(s) > {threshold_ms}ms to cut.")
    return pauses


def measure_audio_levels(audio_path: Path) -> tuple[float | None, float | None]:
    """Return (mean_dbfs, max_dbfs) from ffmpeg volumedetect, or (None, None) on failure."""
    cmd = ["ffmpeg", "-i", str(audio_path), "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    mean_db = max_db = None
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                mean_db = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                pass
        elif "max_volume:" in line:
            try:
                max_db = float(line.split("max_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                pass
    return mean_db, max_db


def measure_energy_percentiles(audio_path: Path) -> tuple[float | None, float | None]:
    """Estimate (noise_floor, speech_level) from short-window RMS percentiles."""
    try:
        with wave.open(str(audio_path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return None, None
            sample_rate = handle.getframerate()
            samples = array.array("h")
            samples.frombytes(handle.readframes(handle.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        window = max(1, int(sample_rate * 0.025))
        step = max(window, int(sample_rate * 0.12))
        levels = []
        for start in range(0, max(0, len(samples) - window), step):
            chunk = samples[start:start + window]
            rms = math.sqrt(sum(value * value for value in chunk) / max(1, len(chunk)))
            levels.append(20.0 * math.log10(max(rms, 1.0) / 32768.0))
        if not levels:
            return None, None
        levels.sort()
        percentile = lambda ratio: levels[min(len(levels) - 1, int((len(levels) - 1) * ratio))]
        return percentile(0.2), percentile(0.8)
    except Exception:
        return None, None


def resolve_silence_db(
    requested: str,
    mean_db: float | None,
    noise_floor_db: float | None = None,
    speech_level_db: float | None = None,
) -> float:
    """
    Resolve the --silence-db option to a concrete dB threshold.

    A fixed dB threshold can't fit every recording: too high clips quiet speech,
    too low leaves hiss. 'auto' derives it from the measured mean level (10 dB
    below average, clamped to a sane range) so it adapts to each mic's noise
    floor.  Prefer short-window energy percentiles over a whole-file mean:
    recordings with lots of dead air otherwise push the threshold too low.
    """
    if str(requested).strip().lower() == "auto":
        if noise_floor_db is not None and speech_level_db is not None and speech_level_db > noise_floor_db:
            derived = noise_floor_db + 0.45 * (speech_level_db - noise_floor_db)
            derived = max(-45.0, min(-18.0, round(derived, 1)))
            log(
                f"🎚️  Adaptive silence threshold: {derived} dB "
                f"(noise p20 {noise_floor_db:.1f}, speech p80 {speech_level_db:.1f} dBFS)."
            )
            return derived
        if mean_db is None:
            log("⚠️  Could not measure audio level for --silence-db auto; falling back to -28 dB.")
            return -28.0
        derived = max(-45.0, min(-18.0, round(mean_db - 10.0, 1)))
        log(f"🎚️  Adaptive silence threshold: {derived} dB (mean fallback {mean_db:.1f} dBFS − 10).")
        return derived
    return float(requested)


def detect_silence_regions(audio_path: Path, noise_db: float = -28.0, min_dur: float = 0.3) -> list[tuple[float, float]]:
    """
    Use ffmpeg silencedetect to find actual silent regions in the merged audio.
    Returns list of (start_s, end_s) tuples.

    noise_db: threshold in dB (default -28dB, matches auto-editor's default)
    min_dur: minimum silence duration in seconds (default 0.3s)
    """
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"⚠️  ffmpeg silencedetect exited {result.returncode}; treating as no silence found.")
        return []
    output = result.stderr

    regions = []
    silence_start = None
    for line in output.splitlines():
        if "silence_start" in line:
            try:
                silence_start = float(line.split("silence_start:")[1].strip())
            except (IndexError, ValueError):
                pass
        elif "silence_end" in line and silence_start is not None:
            try:
                silence_end = float(line.split("silence_end:")[1].split("|")[0].strip())
                regions.append((silence_start, silence_end))
                silence_start = None
            except (IndexError, ValueError):
                pass

    log(f"🔇 silencedetect ({noise_db}dB, min {min_dur}s): found {len(regions)} region(s).")
    return regions


_SILERO_MODEL = None


def detect_nonspeech_regions_vad(
    audio_path: Path,
    *,
    min_dur: float = 0.3,
    threshold: float = 0.5,
) -> list[tuple[float, float]]:
    """Return non-speech intervals using local Silero VAD.

    This complements amplitude silence: keyboard clicks, fan noise and room
    hiss can be loud but are still not speech. ASR word protection and screen
    activity protection remain the final safety nets.
    """
    global _SILERO_MODEL
    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError:
        log("⚠️  silero-vad is unavailable; falling back to energy-only pause detection.")
        return []
    if _SILERO_MODEL is None:
        _SILERO_MODEL = load_silero_vad()
    # The editor has already decoded a 16 kHz mono PCM WAV. Load it directly
    # instead of torchaudio.read_audio, whose newest release unnecessarily
    # requires the separate torchcodec package even for WAV input.
    with wave.open(str(audio_path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2 or handle.getframerate() != 16000:
            raise RuntimeError("Silero VAD requires a 16 kHz mono 16-bit PCM WAV")
        pcm = array.array("h")
        pcm.frombytes(handle.readframes(handle.getnframes()))
    if sys.byteorder != "little":
        pcm.byteswap()
    waveform = torch.tensor(pcm, dtype=torch.float32) / 32768.0
    speech = get_speech_timestamps(
        waveform,
        _SILERO_MODEL,
        threshold=threshold,
        sampling_rate=16000,
        min_speech_duration_ms=120,
        min_silence_duration_ms=120,
        speech_pad_ms=70,
        return_seconds=True,
    )
    duration_s = float(len(waveform)) / 16000.0
    nonspeech: list[tuple[float, float]] = []
    cursor = 0.0
    for item in speech:
        start = float(item["start"])
        end = float(item["end"])
        if start - cursor >= min_dur:
            nonspeech.append((cursor, start))
        cursor = max(cursor, end)
    if duration_s - cursor >= min_dur:
        nonspeech.append((cursor, duration_s))
    log(f"🗣️  Silero VAD: found {len(nonspeech)} non-speech region(s).")
    return nonspeech


def detect_silence_regions_by_session(
    session_offsets: list[dict],
    requested_db: str,
    min_dur: float,
    *,
    use_vad: bool = True,
) -> tuple[list[tuple[float, float]], list[float]]:
    """Detect silence with a separately calibrated threshold for every session."""
    timeline_regions_ms: list[tuple[float, float]] = []
    thresholds: list[float] = []
    for index, session in enumerate(session_offsets):
        audio_path = Path(session["file"])
        mean_db, max_db = measure_audio_levels(audio_path)
        noise_floor, speech_level = measure_energy_percentiles(audio_path)
        threshold = resolve_silence_db(requested_db, mean_db, noise_floor, speech_level)
        thresholds.append(threshold)
        if mean_db is not None:
            log(f"🔊 Session {index}: mean {mean_db:.1f} dBFS, peak {max_db:.1f} dBFS.")
        local_regions = detect_silence_regions(audio_path, noise_db=threshold, min_dur=min_dur)
        if use_vad:
            try:
                local_regions.extend(detect_nonspeech_regions_vad(audio_path, min_dur=min_dur))
            except Exception as exc:
                log(f"⚠️  Silero VAD failed for session {index}: {exc}; using energy silence only.")
            local_regions_ms = merge_intervals(
                [(start * 1000.0, end * 1000.0) for start, end in local_regions],
                gap_ms=40.0,
            )
            local_regions = [(start / 1000.0, end / 1000.0) for start, end in local_regions_ms]
        anchor_ms = float(session["timelineOffsetMs"])
        duration_ms = float(session["durationMs"])
        for start_s, end_s in local_regions:
            start_ms = anchor_ms + min(duration_ms, max(0.0, start_s * 1000.0))
            end_ms = anchor_ms + min(duration_ms, max(0.0, end_s * 1000.0))
            if end_ms > start_ms:
                timeline_regions_ms.append((start_ms, end_ms))
    merged_ms = merge_intervals(timeline_regions_ms, gap_ms=35.0)
    return [(start / 1000.0, end / 1000.0) for start, end in merged_ms], thresholds


def filter_pauses_by_silence(pauses: list[dict], silence_regions: list[tuple[float, float]]) -> list[dict]:
    """
    Validate each pause cut against actual silence regions.
    A cut is 'confirmed' if its interval overlaps with a silence region by >= 50% of the cut's duration.
    Unconfirmed cuts are skipped to avoid clipping real speech.
    """
    if not silence_regions:
        log("⚠️  No silence regions detected — skipping silence validation.")
        return pauses

    confirmed = []
    skipped = []

    for p in pauses:
        cut_start_s = p["start_ms"] / 1000.0
        cut_end_s = p["end_ms"] / 1000.0
        cut_dur = cut_end_s - cut_start_s

        # Find max overlap with any silence region
        max_overlap = 0.0
        for (sr_start, sr_end) in silence_regions:
            overlap = max(0.0, min(cut_end_s, sr_end) - max(cut_start_s, sr_start))
            max_overlap = max(max_overlap, overlap)

        overlap_ratio = max_overlap / cut_dur if cut_dur > 0 else 0.0

        if overlap_ratio >= 0.5:
            confirmed.append(p)
        else:
            skipped.append(p)
            log(f"   ⏭️  Skip (overlap={overlap_ratio:.0%}): [{cut_start_s:.2f}s→{cut_end_s:.2f}s] "
                f"「{p['text_before']}」...「{p['text_after']}」")

    log(f"✅ Silence validation: {len(confirmed)} confirmed, {len(skipped)} skipped.")
    return confirmed


def merge_cut_lists(cut_lists: list[list[dict]], min_gap_ms: float = 80.0) -> list[dict]:
    """Merge overlapping cut ranges from multiple detectors."""
    cuts = sorted(
        [dict(c) for cut_list in cut_lists for c in cut_list],
        key=lambda c: (c["start_ms"], c["end_ms"]),
    )
    if not cuts:
        return []

    merged = [cuts[0]]
    for cut in cuts[1:]:
        prev = merged[-1]
        if cut["start_ms"] <= prev["end_ms"] + min_gap_ms:
            prev["end_ms"] = max(prev["end_ms"], cut["end_ms"])
            prev["duration_ms"] = max(prev.get("duration_ms", 0), cut.get("duration_ms", 0))
            sources = {s for s in str(prev.get("source", "")).split("+") if s}
            sources.update(s for s in str(cut.get("source", "")).split("+") if s)
            prev["source"] = "+".join(sorted(sources))
            if not prev.get("text_after") and cut.get("text_after"):
                prev["text_after"] = cut["text_after"]
        else:
            merged.append(cut)
    return merged


def detect_repeats(
    cuts_file: str,
    current_project_data: dict,
    project_json_path: Path,
    backup_path: Path | None = None,
) -> list[dict]:
    """
    Load reviewed cuts and normalize them to the source timeline.

    Schema-v2 files declare their coordinate space and project fingerprint.
    Legacy list-only files remain supported as source time with a warning.
    """
    if not cuts_file:
        return []
    if not Path(cuts_file).exists():
        log(f"⚠️  Cuts file not found: {cuts_file}, skipping repeat detection.")
        return []
    scenes = current_project_data.get("json", {}).get("scenes") or []
    if not scenes or not scenes[0].get("slices"):
        raise CutsValidationError("current project has no slices for cuts validation")
    accepted_source_hashes = []
    if backup_path and backup_path.exists():
        accepted_source_hashes.append(_file_sha256(backup_path))
    repeats, document = load_cuts_document(
        Path(cuts_file),
        current_slices=scenes[0]["slices"],
        current_project_sha256=_file_sha256(project_json_path),
        accepted_source_project_sha256s=accepted_source_hashes,
    )
    if document.get("legacy"):
        log("⚠️  Legacy cuts list has no coordinate-space/project fingerprint; assuming SOURCE time.")
    elif document.get("coordinate_space") == "edited":
        log(f"🧭 Mapped edited-time cuts through the current {len(scenes[0]['slices'])}-slice timeline.")
    elif not document.get("project_sha256"):
        log("⚠️  Source-time cuts have no project fingerprint; coordinates cannot be cross-project verified.")
    log(f"✂️  Loaded {len(repeats)} repeat cut(s) from {cuts_file}")
    for r in repeats:
        log(f"   Remove: \"{r.get('removed_text', '')[:70]}\"")
    return repeats


def get_session_boundaries_ms(session_offsets: list[dict]) -> list[float]:
    """
    Return the slice-timeline positions (in ms) at which each new recording session begins.
    These are the "natural" transition points that Screen Studio originally placed between sessions.
    The first session always starts at 0 so only boundaries *between* sessions are returned.
    """
    return [s["timelineOffsetMs"] for s in session_offsets[1:]]


_SNAP_MARGIN_MS = 10  # snap 10ms before boundary; see snap_to_session_boundaries docstring
_MAX_SNAP_ENDPOINT_SHIFT_MS = 120


def snap_to_session_boundaries(slices: list[dict], boundaries_ms: list[float]) -> tuple[list[dict], int]:
    """
    After pause-removal cuts, some inter-session boundaries end up inside a gap between
    slices (because the removed pause straddled the boundary).  Screen Studio originally
    rendered a smooth spring transition at those boundaries; the cuts destroy it.

    Fix: if a boundary B falls inside a gap [slice_n.sourceEndMs, slice_{n+1}.sourceStartMs],
    clamp both endpoints to (B - MARGIN) only when that movement is tiny. This makes
    the pair source-contiguous (gap = 0 ms) and re-enables Screen Studio's spring
    animation without undoing a real pause cut.

    Why the margin is necessary: floating-point session durations mean an exact
    boundary snap can generate a sub-millisecond segment at the session seam,
    which Screen Studio's audio composer rejects ("Invalid time range: duration
    must be positive").  A 10 ms margin produces a short but valid segment
    (~10 ms of near-silence from the tail of session N) that the audio composer
    accepts.  10 ms is imperceptible.

    Never restore a transition by moving either endpoint far away from the cut that
    pause detection chose. If the boundary lands in the middle of a long silence,
    preserving pacing is more important than preserving the session transition.

    Returns (updated_slices, num_boundaries_snapped).
    """
    if not boundaries_ms or len(slices) < 2:
        return slices, 0

    snapped = 0
    for b in boundaries_ms:
        snap_point = b - _SNAP_MARGIN_MS
        for i in range(len(slices) - 1):
            end_i   = slices[i]["sourceEndMs"]
            start_n = slices[i + 1]["sourceStartMs"]
            if end_i <= snap_point <= start_n:
                shift_prev = snap_point - end_i
                shift_next = start_n - snap_point
                if shift_prev > _MAX_SNAP_ENDPOINT_SHIFT_MS or shift_next > _MAX_SNAP_ENDPOINT_SHIFT_MS:
                    log(
                        f"⏭️  Session boundary {b/1000:.3f}s not snapped: "
                        f"would reintroduce silence (prev_shift={shift_prev:.0f}ms, next_shift={shift_next:.0f}ms)."
                    )
                    break
                slices[i]["sourceEndMs"]       = snap_point
                slices[i + 1]["sourceStartMs"] = snap_point
                snapped += 1
                log(f"🔗 Session boundary {b/1000:.3f}s snapped between slice {i} and {i+1} → gap restored to 0 (margin={_SNAP_MARGIN_MS}ms)")
                break  # each boundary can only fall in one gap

    return slices, snapped


def remove_wordless_pause_slices(
    slices: list[dict],
    segments: list[dict],
    silence_regions: list[tuple[float, float]],
    activity_intervals: list[tuple[float, float]] | None = None,
    max_duration_ms: float = 2500.0,
) -> tuple[list[dict], list[dict]]:
    """
    Remove short wordless slices left behind by pause cuts.

    Session-boundary snapping can preserve a smooth Screen Studio transition but
    leave a standalone silent slice after the boundary. These clips show up in the
    timeline as "Clip 2s/3s" blocks with no meaningful waveform. They are too long
    for the old tiny-fragment filter, so clean them up explicitly.

    "No words" is never sufficient evidence: a screen tutorial can contain an
    intentional click, wait, animation, or result with no narration.  Removal
    therefore requires strong measured-silence evidence and no protected screen
    activity.  The old near-jump-cut shortcut was deliberately removed because
    it could delete a fully non-silent 2.5 second slice.
    """
    if not slices:
        return slices, []

    words = flatten_words(segments)
    activity_intervals = activity_intervals or []
    removed = []
    kept = []

    for i, sl in enumerate(slices):
        start_ms = float(sl["sourceStartMs"])
        end_ms = float(sl["sourceEndMs"])
        duration_ms = end_ms - start_ms

        if duration_ms > max_duration_ms:
            kept.append(sl)
            continue

        has_words = any(
            (w.get("end", 0) * 1000.0) > start_ms
            and (w.get("start", 0) * 1000.0) < end_ms
            for w in words
        )
        if has_words:
            kept.append(sl)
            continue

        silence_overlap_ms = 0.0
        for silence_start_s, silence_end_s in silence_regions:
            silence_start_ms = silence_start_s * 1000.0
            silence_end_ms = silence_end_s * 1000.0
            silence_overlap_ms += max(0.0, min(end_ms, silence_end_ms) - max(start_ms, silence_start_ms))

        mostly_measured_silence = duration_ms > 0 and silence_overlap_ms / duration_ms >= 0.8
        has_activity = any(
            activity_end > start_ms and activity_start < end_ms
            for activity_start, activity_end in activity_intervals
        )

        if mostly_measured_silence and not has_activity:
            removed.append({
                "index": i,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "silence_overlap_ms": silence_overlap_ms,
            })
        else:
            kept.append(sl)

    if removed:
        log(f"🧹 Removed {len(removed)} short wordless pause slice(s).")
        for r in removed:
            log(
                f"   Drop slice {r['index']}: "
                f"{r['start_ms']/1000:.3f}s→{r['end_ms']/1000:.3f}s "
                f"({r['duration_ms']/1000:.2f}s, "
                f"silence={r['silence_overlap_ms']/r['duration_ms']:.0%})"
            )

    return kept, removed


def apply_cuts(slices: list[dict], cuts: list[dict]) -> tuple[list[dict], int]:
    """
    Given the existing slices and a list of time ranges to cut,
    return updated slices with those ranges removed.

    Each cut: {"start_ms": ..., "end_ms": ...}
    Each slice: {"sourceStartMs": ..., "sourceEndMs": ..., ...}

    Returns (new_slices, num_cuts_applied).
    """
    if not cuts:
        return slices, 0

    # Sort cuts
    cuts = sorted(cuts, key=lambda c: c["start_ms"])

    new_slices = []
    cuts_applied = 0

    for sl in slices:
        sl_start = sl["sourceStartMs"]
        sl_end = sl["sourceEndMs"]
        remaining = [(sl_start, sl_end)]

        for cut in cuts:
            cut_start = cut["start_ms"]
            cut_end = cut["end_ms"]

            new_remaining = []
            for (rs, re) in remaining:
                if cut_end <= rs or cut_start >= re:
                    # No overlap
                    new_remaining.append((rs, re))
                elif cut_start <= rs and cut_end >= re:
                    # Cut removes entire segment
                    cuts_applied += 1
                else:
                    # Partial overlap
                    if cut_start > rs:
                        new_remaining.append((rs, cut_start))
                    if cut_end < re:
                        new_remaining.append((cut_end, re))
                    cuts_applied += 1

            remaining = new_remaining

        for (start, end) in remaining:
            # Never discard a 1–100 ms remainder merely because it is small: it
            # can contain a consonant onset or the tail of a click. Screen Studio
            # accepts positive millisecond ranges; safe wordless cleanup happens
            # later with audio/activity evidence.
            if end - start > 1:
                new_slice = dict(sl)
                new_slice["sourceStartMs"] = start
                new_slice["sourceEndMs"] = end
                # Each slice must have a unique id or Screen Studio collapses them
                # into a single uneditable block. Transitions in Screen Studio are
                # triggered by consecutive same-ID slices, but that only works for
                # small groups (2-3 slices). With many cuts, unique IDs are required
                # to preserve editability in the UI.
                new_slice["id"] = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                new_slices.append(new_slice)

    return new_slices, cuts_applied


def pad_repeat_cuts(repeats: list[dict]) -> list[dict]:
    """
    Backward-compatible no-op.

    Fixed inward padding used to erase short fillers and leave audible fragments
    of longer repeats.  ``refine_repeat_cut_boundaries`` now finds quiet waveform
    points around the complete reviewed phrase instead.
    """
    return [dict(repeat) for repeat in repeats]


def log_long_cuts(cuts: list[dict], threshold_ms: float = 5000.0):
    """
    Surface every long removal so the reviewing agent can sanity-check it.
    A >5s cut is usually dead air, but it can also hide silent on-screen
    action or sit next to an abandoned take that still needs a repeat cut.
    """
    long_cuts = [c for c in cuts if c["end_ms"] - c["start_ms"] > threshold_ms]
    if not long_cuts:
        return
    log(f"⏱️  {len(long_cuts)} long removal(s) >{threshold_ms/1000:.0f}s — review that no on-screen action is lost:")
    for c in long_cuts:
        before = c.get("text_before") or c.get("removed_text") or ""
        after = c.get("text_after") or ""
        log(f"   {c['start_ms']/1000:8.2f}s → {c['end_ms']/1000:8.2f}s "
            f"({(c['end_ms']-c['start_ms'])/1000:5.1f}s)  …{before[-20:]} ▶ {after[:20]}…")


def load_external_edit_state(project_json_path: Path, state_path: Path) -> bool:
    """Return True when project.json changed since our last write (external edit)."""
    if not (state_path.exists() and project_json_path.exists()):
        return False
    try:
        last_sha = json.loads(state_path.read_text()).get("last_written_sha")
        return bool(last_sha) and last_sha != _file_sha256(project_json_path)
    except Exception:
        return False


def run_incremental_repeat_cuts(
    project_json_path: Path,
    state_path: Path,
    cuts_file: str,
    *,
    protect_screen_activity: bool = True,
    visual_scan: bool = True,
    visual_scan_fps: float = 2.5,
    visual_change_threshold: float = 0.012,
    allow_active_repeat_cuts: bool = False,
):
    """
    Apply repeat cuts on top of the CURRENT project.json without touching
    anything else. Used when the project was edited externally (e.g. in
    Screen Studio) after the first auto-edit run: re-applying from the backup
    would discard those edits, so instead the new cuts are rebased onto the
    current timeline. Cut coordinates are source-timeline ms, which survive
    any slice rearrangement, so this is safe.
    """
    with open(project_json_path) as f:
        project_data = json.load(f)

    scenes = project_data.get("json", {}).get("scenes")
    if not scenes or not scenes[0].get("slices"):
        log("❌ project.json has no scenes/slices — unexpected format, aborting.")
        sys.exit(1)

    try:
        repeats = detect_repeats(
            cuts_file,
            project_data,
            project_json_path,
            project_json_path.with_suffix(".json.bak"),
        )
    except CutsValidationError as exc:
        log(f"❌ Unsafe cuts file: {exc}")
        sys.exit(1)
    if not repeats:
        log("❌ No usable cuts in the cuts file — nothing to do.")
        sys.exit(1)

    project_dir = project_json_path.parent
    metadata = load_metadata(project_dir)
    mic_sessions = get_mic_sessions(metadata)
    transcript_path = project_dir / "transcript.edit.json"
    if not transcript_path.exists():
        transcript_path = project_dir / "transcript.json"
    segments = []
    if transcript_path.exists():
        try:
            segments = json.loads(transcript_path.read_text(encoding="utf-8"))
        except Exception:
            segments = []

    session_offsets: list[dict] = []
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            merged_audio = tmp / "merged_mic.wav"
            session_offsets = merge_audio(project_dir, mic_sessions, merged_audio, tmp)
            repeats = refine_repeat_cut_boundaries(
                repeats, flatten_words(segments), merged_audio, session_offsets
            )
    except Exception as exc:
        log(f"⚠️  Incremental boundary refinement unavailable: {exc}")

    if protect_screen_activity and session_offsets:
        activity_intervals = load_input_activity_intervals(project_dir, metadata, session_offsets)
        if visual_scan:
            activity_intervals.extend(detect_visual_activity_intervals(
                project_dir,
                metadata,
                session_offsets,
                fps=visual_scan_fps,
                scene_threshold=visual_change_threshold,
            ))
        activity_intervals = merge_intervals(activity_intervals, gap_ms=120.0)
        if not allow_active_repeat_cuts:
            repeats, protected = protect_cuts_with_activity(repeats, activity_intervals)
            if protected:
                log(f"🛡️  Kept {len(protected)} incremental cut(s) containing screen activity.")
        if not repeats:
            log("❌ Every reviewed cut overlaps protected screen activity — nothing applied.")
            sys.exit(1)

    original_slices = scenes[0]["slices"]
    new_slices, cuts_applied = apply_cuts(original_slices, repeats)

    original_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in original_slices)
    new_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in new_slices)

    project_data["json"]["scenes"][0]["slices"] = new_slices
    with open(project_json_path, "w") as f:
        json.dump(project_data, f, ensure_ascii=False, separators=(",", ":"))
    try:
        state_path.write_text(json.dumps({"last_written_sha": _file_sha256(project_json_path)}))
    except Exception:
        pass

    log("")
    log("=" * 50)
    log("✅ Incremental repeat cuts applied to the CURRENT timeline (external edits preserved):")
    log(f"   Repeats removed:   {len(repeats)}")
    log(f"   Total cuts:        {cuts_applied}")
    log(f"   Duration:          {original_duration/1000:.1f}s → {new_duration/1000:.1f}s "
        f"(saved {(original_duration-new_duration)/1000:.1f}s)")
    for r in repeats:
        log(f"  ✂️  \"{r.get('removed_text', '')[:80]}\"")
    log("")
    log("Open Screen Studio to preview the result.")


def main():
    parser = argparse.ArgumentParser(description="Screen Studio Auto-Editor")
    parser.add_argument("--project", required=True, help="Path to .screenstudio directory")
    parser.add_argument("--pause-threshold", type=float, default=800, help="Pause threshold in ms (default: 800)")
    parser.add_argument("--min-pause", type=float, default=300, help="Minimum pause to keep in ms (default: 300)")
    parser.add_argument("--pause-source", choices=["silence", "asr", "both"], default="silence",
                        help="How to find pause cuts. Default: measured silence. ASR is available as an opt-in fallback.")
    parser.add_argument("--silence-db", default="auto",
                        help="silencedetect noise floor in dB, or 'auto' (default) to derive it from the "
                             "measured audio level — adapts to each recording's noise floor. Fixed values: "
                             "lower (e.g. -35) is stricter and keeps more audio (use if speech gets "
                             "clipped); raise toward -20 to cut more aggressively for noisy mics.")
    parser.add_argument("--silence-min-dur", type=float, default=0.3,
                        help="Minimum silence length in seconds for silencedetect (default: 0.3).")
    parser.add_argument("--no-vad", action="store_true",
                        help="Disable local Silero voice-activity detection and use energy silence only.")
    parser.add_argument("--cuts-file", default=None, help="JSON file with repeat cuts (produced by Claude in conversation)")
    parser.add_argument("--no-screen-activity-protection", action="store_true",
                        help="Disable click/keystroke and display-change protection (unsafe for tutorials).")
    parser.add_argument("--no-visual-scan", action="store_true",
                        help="Protect clicks/keystrokes but skip the low-resolution display-change scan.")
    parser.add_argument("--visual-scan-fps", type=float, default=2.5,
                        help="Display activity scan rate (default: 2.5 fps).")
    parser.add_argument("--visual-change-threshold", type=float, default=0.012,
                        help="ffmpeg scene-score threshold for protected display changes (default: 0.012).")
    parser.add_argument("--allow-active-repeat-cuts", action="store_true",
                        help="Allow reviewed repeat cuts to remove intervals containing detected screen actions.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and write an audit report without modifying project.json or creating a backup.")
    parser.add_argument("--report-output", type=Path,
                        help="Optional audit report path. Normal runs default beside the project; dry runs default to /tmp.")
    parser.add_argument("--skip-transcribe", help="Path to existing transcript JSON to reuse")
    parser.add_argument("--language", default="zh",
                        help="ASR language code (default: zh). Use 'en' for English, 'None' to auto-detect.")
    parser.add_argument("--asr-backend", choices=["bailian", "local"], default="bailian",
                        help="ASR backend for transcript generation. Default: bailian. Use local only for explicit comparison or emergency fallback.")
    parser.add_argument("--discard-external-edits", action="store_true",
                        help="Re-apply everything from the original backup even if project.json was "
                             "edited externally (e.g. in Screen Studio) since the last run, DISCARDING "
                             "those external edits.")
    args = parser.parse_args()

    project_dir = Path(args.project)
    if not project_dir.exists() or not project_dir.is_dir():
        print(f"❌ Project not found: {project_dir}")
        sys.exit(1)

    if shutil.which("ffmpeg") is None:
        print("❌ ffmpeg not found on PATH. Run setup.sh first.")
        sys.exit(1)

    project_json_path = project_dir / "project.json"
    backup_path = project_dir / "project.json.bak"
    state_path = project_dir / ".autoedit-state.json"
    report_path = args.report_output or (
        Path(tempfile.gettempdir()) / f"{project_dir.stem}-autoedit-report.json"
        if args.dry_run else project_dir / "autoedit-report.json"
    )
    dry_transcript_cache_path: Path | None = None

    if not project_json_path.exists():
        log(f"❌ project.json not found: {project_json_path}")
        sys.exit(1)

    # A dry run must not mutate the bundle merely by creating the backup.
    if not args.dry_run:
        backup_project(project_json_path)

    # If project.json changed since our last run, someone edited it externally
    # (opening/saving in Screen Studio counts). Re-applying from the backup
    # would silently discard those edits, so:
    #   - with --cuts-file: rebase the new repeat cuts onto the CURRENT
    #     timeline and keep everything else untouched;
    #   - otherwise: refuse, unless --discard-external-edits explicitly asks
    #     to start over from the original backup.
    externally_edited = (
        not args.dry_run
        and backup_path.exists()
        and load_external_edit_state(project_json_path, state_path)
    )
    if externally_edited and not args.discard_external_edits:
        log("⚠️  project.json changed since the last auto-edit run "
            "(edited or re-saved in Screen Studio?).")
        if args.cuts_file:
            log("    Applying the new cuts incrementally to the CURRENT timeline; "
                "external edits are preserved.")
            run_incremental_repeat_cuts(
                project_json_path,
                state_path,
                args.cuts_file,
                protect_screen_activity=not args.no_screen_activity_protection,
                visual_scan=not args.no_visual_scan,
                visual_scan_fps=args.visual_scan_fps,
                visual_change_threshold=args.visual_change_threshold,
                allow_active_repeat_cuts=args.allow_active_repeat_cuts,
            )
            return
        log("    Refusing to re-run the full edit: it would rebuild from project.json.bak")
        log("    and DISCARD everything changed since the last run.")
        log("    Either pass --cuts-file to add repeat cuts incrementally, or pass")
        log("    --discard-external-edits to intentionally start over from the backup.")
        sys.exit(2)

    # Keep the current slice map as well as the pristine backup. Edited-time
    # review cuts must be mapped through the exact CURRENT slices that produced
    # the export, while the full edit is rebuilt idempotently from the backup.
    with open(project_json_path) as f:
        current_project_data = json.load(f)

    # Normal runs rebuild from the pristine backup. Dry runs analyze the current
    # timeline exactly as it exists and never write it.
    # overwritten, so it always holds the original unedited project.json.
    # This makes every run idempotent: pauses + repeats are applied to the
    # original slices, never to already-cut slices from a prior run.
    if args.dry_run:
        project_data = json.loads(json.dumps(current_project_data))
    else:
        with open(backup_path) as f:
            project_data = json.load(f)

    # Load metadata
    metadata = load_metadata(project_dir)
    mic_sessions = get_mic_sessions(metadata)

    if not mic_sessions:
        log("❌ No microphone sessions found in metadata.")
        sys.exit(1)

    log(f"📁 Found {len(mic_sessions)} recording session(s).")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Merge audio
        merged_audio = tmp / "merged_mic.wav"
        session_offsets = merge_audio(project_dir, mic_sessions, merged_audio, tmp)

        # Transcribe. The transcript improves the edit (cut labeling, wordless-slice
        # protection, repeat review) but silence-based pause cutting works without
        # it, so an ASR outage degrades the run instead of aborting it.
        transcript_ok = True
        if args.skip_transcribe:
            # Reused transcripts were saved by a previous run and are already in
            # slice-timeline coordinates — do not remap them again.
            with open(args.skip_transcribe) as f:
                segments = json.load(f)
            if args.dry_run:
                dry_transcript_cache_path = Path(args.skip_transcribe)
            else:
                serialized = json.dumps(segments, indent=2, ensure_ascii=False)
                (project_dir / "transcript.json").write_text(serialized, encoding="utf-8")
                (project_dir / "transcript.edit.json").write_text(serialized, encoding="utf-8")
                (project_dir / "transcript.edit.meta.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "coordinate_space": "source",
                        "project_sha256": _file_sha256(backup_path),
                        "fillers_preserved": True,
                        "segmentation": "reused_edit_transcript",
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            log(f"♻️  Loaded existing transcript from {args.skip_transcribe}")
        else:
            lang = None if args.language == "None" else args.language
            raw_asr_out = (
                (tmp / "bailian_asr.json" if args.dry_run else project_dir / "bailian_asr.json")
                if args.asr_backend == "bailian" else None
            )
            segments = None
            last_error = None
            for attempt in (1, 2):
                try:
                    segments = transcribe(
                        merged_audio,
                        language=lang,
                        backend=args.asr_backend,
                        raw_output_path=raw_asr_out,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    log(f"⚠️  ASR attempt {attempt} failed: {exc}")
            if segments is None:
                if args.pause_source != "silence":
                    log("❌ ASR failed twice and --pause-source needs the transcript. Aborting.")
                    log(f"   Last error: {last_error}")
                    sys.exit(1)
                transcript_ok = False
                segments = []
                log("⚠️  ASR failed twice — continuing with SILENCE-ONLY editing.")
                log("    No transcript.json will be written: repeat review is not possible,")
                log("    and wordless-slice cleanup runs in its conservative mode.")

            if transcript_ok:
                # ASR timestamps are positions in the merged audio; rewrite them
                # into slice-timeline coordinates so transcript.json matches the
                # slices (and cuts.json values can be copied from it verbatim).
                segments = remap_segments_to_timeline(segments, session_offsets)
                if args.dry_run:
                    dry_transcript_cache_path = report_path.with_name(
                        f"{report_path.stem}.transcript.edit.json"
                    )
                    dry_transcript_cache_path.parent.mkdir(parents=True, exist_ok=True)
                    dry_transcript_cache_path.write_text(
                        json.dumps(segments, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    log(f"🧪 Dry run: cached edit transcript outside the project → {dry_transcript_cache_path}")
                else:
                    transcript_out = project_dir / "transcript.json"
                    edit_transcript_out = project_dir / "transcript.edit.json"
                    serialized = json.dumps(segments, indent=2, ensure_ascii=False)
                    transcript_out.write_text(serialized, encoding="utf-8")
                    edit_transcript_out.write_text(serialized, encoding="utf-8")
                    (project_dir / "transcript.edit.meta.json").write_text(
                        json.dumps({
                            "schema_version": 1,
                            "coordinate_space": "source",
                            "project_sha256": _file_sha256(backup_path),
                            "fillers_preserved": True,
                            "segmentation": "raw_asr_sentences",
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    log("💾 Saved edit transcript → transcript.edit.json + transcript.json "
                        "(SOURCE time, fillers preserved)")

        # Calibrate silence separately per recording session. A global mean is
        # distorted by long dead-air stretches and by gain changes after pause/
        # resume; short-window p20/p80 energy estimates are substantially safer.
        silence_regions, _silence_thresholds = detect_silence_regions_by_session(
            session_offsets,
            args.silence_db,
            args.silence_min_dur,
            use_vad=not args.no_vad,
        )

        pause_cut_lists = []
        if args.pause_source in {"silence", "both"}:
            pause_cut_lists.append(
                detect_pauses_from_silence(
                    silence_regions,
                    args.pause_threshold,
                    args.min_pause,
                    segments,
                )
            )

        if args.pause_source in {"asr", "both"}:
            asr_pauses = detect_pauses_from_asr(segments, args.pause_threshold, args.min_pause)
            # ASR-only cuts are still checked against real silence. If silence
            # detection finds nothing, do not cut on ASR timing alone by default.
            if silence_regions:
                asr_pauses = filter_pauses_by_silence(asr_pauses, silence_regions)
            elif args.pause_source == "asr":
                log("⚠️  No silence regions detected; using ASR pauses because --pause-source asr was explicitly requested.")
            else:
                log("⚠️  No silence regions detected; skipping ASR pause fallback.")
                asr_pauses = []
            pause_cut_lists.append(asr_pauses)

        pauses = merge_cut_lists(pause_cut_lists)

        # Safety net: silence thresholds can misjudge quiet speech, so never let
        # a pause cut remove anything ASR recognized as a word. Repeat cuts are
        # exempt — removing recognized speech is their entire purpose.
        pauses = protect_words_from_cuts(pauses, flatten_words(segments))

        # Load reviewed cuts with explicit coordinate-space/project validation,
        # then refine semantic ASR/model ranges to nearby waveform minima.
        try:
            repeats = detect_repeats(
                args.cuts_file,
                current_project_data,
                project_json_path,
                backup_path,
            )
        except CutsValidationError as exc:
            log(f"❌ Unsafe cuts file: {exc}")
            sys.exit(1)
        repeats = refine_repeat_cut_boundaries(
            repeats,
            flatten_words(segments),
            merged_audio,
            session_offsets,
        )

        # A silent microphone is not a blank tutorial. Protect click/keystroke
        # events and meaningful display changes before any automatic cut lands.
        activity_intervals: list[tuple[float, float]] = []
        protected_pauses: list[dict] = []
        protected_repeats: list[dict] = []
        if not args.no_screen_activity_protection and (pauses or repeats):
            activity_intervals.extend(
                load_input_activity_intervals(project_dir, metadata, session_offsets)
            )
            if not args.no_visual_scan:
                activity_intervals.extend(detect_visual_activity_intervals(
                    project_dir,
                    metadata,
                    session_offsets,
                    fps=args.visual_scan_fps,
                    scene_threshold=args.visual_change_threshold,
                ))
            activity_intervals = merge_intervals(activity_intervals, gap_ms=120.0)
            pauses, protected_pauses = protect_cuts_with_activity(pauses, activity_intervals)
            if protected_pauses:
                log(f"🛡️  Kept {len(protected_pauses)} silent interval(s) containing screen activity.")
            if not args.allow_active_repeat_cuts:
                repeats, protected_repeats = protect_cuts_with_activity(repeats, activity_intervals)
                if protected_repeats:
                    log(f"🛡️  Kept {len(protected_repeats)} reviewed cut(s) containing screen activity.")

        all_cuts = pauses + repeats
        log_long_cuts(all_cuts)

        scenes = project_data.get("json", {}).get("scenes")
        if not scenes:
            log("❌ project.json has no scenes — unexpected format, aborting.")
            sys.exit(1)
        if len(scenes) > 1:
            log(f"⚠️  Project has {len(scenes)} scenes; only the first scene is edited.")
        if not scenes[0].get("slices"):
            log("❌ Scene 0 has no slices — nothing to edit, aborting.")
            sys.exit(1)
        original_slices = scenes[0]["slices"]

        # Sanity-check the coordinate-system assumption (slice timeline == sessions
        # laid back-to-back by metadata durationMs). If slices extend far beyond
        # that span, Screen Studio's format has probably changed and cuts would
        # land in the wrong place.
        total_timeline_ms = sum(s["durationMs"] for s in mic_sessions)
        max_source_end = max((s.get("sourceEndMs", 0) for s in original_slices), default=0)
        if total_timeline_ms > 0 and max_source_end > total_timeline_ms * 1.2:
            log("⚠️  Slice timeline extends well beyond the recorded sessions "
                f"(sessions≈{total_timeline_ms/1000:.1f}s, slices reach {max_source_end/1000:.1f}s).")
            log("    The .screenstudio format may have changed — verify the cuts carefully.")

        new_slices, cuts_applied = apply_cuts(original_slices, all_cuts)

        session_boundaries = get_session_boundaries_ms(session_offsets)
        new_slices, snapped = snap_to_session_boundaries(new_slices, session_boundaries)
        new_slices, removed_wordless_slices = remove_wordless_pause_slices(
            new_slices,
            segments,
            silence_regions,
            activity_intervals,
        )

        # Calculate time saved
        original_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in original_slices)
        new_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in new_slices)
        saved_ms = original_duration - new_duration

        # Update project
        project_data["json"]["scenes"][0]["slices"] = new_slices
        project_data["json"]["config"]["backgroundPaddingRatio"] = 1.02  # 2% padding
        project_data["json"]["config"]["windowBorderRadius"] = 25
        project_data["json"]["config"]["defaultOutputAspectRatio"] = {"x": 4, "y": 3}  # 4:3 canvas
        project_data["json"]["config"]["cameraAspectRatio"] = "square"   # 宽高一致（正方形）
        project_data["json"]["config"]["cameraSize"] = 0.3
        project_data["json"]["config"]["cameraPosition"] = "top-right"
        project_data["json"]["config"]["cameraPositionPoint"] = {"x": 1, "y": 0}
        project_data["json"]["config"].setdefault("defaultLayout", {})
        project_data["json"]["config"]["defaultLayout"]["cameraSize"] = 0.3
        project_data["json"]["config"]["defaultLayout"]["cameraPositionPoint"] = {"x": 1, "y": 0}
        project_data["json"]["config"]["improveMicrophoneAudio"] = True  # 降噪 + 音量均一化
        # Note: cameraRoundness is intentionally NOT set — keep Screen Studio's default roundness

        audit_report = {
            "schema_version": 1,
            "dry_run": args.dry_run,
            "project": str(project_dir),
            "project_sha256": _file_sha256(project_json_path),
            "pause_threshold_ms": args.pause_threshold,
            "min_pause_ms": args.min_pause,
            "vad_enabled": not args.no_vad,
            "screen_activity_protection": not args.no_screen_activity_protection,
            "silence_thresholds_db": _silence_thresholds,
            "pauses_applied": pauses,
            "reviewed_cuts_applied": repeats,
            "pauses_protected_by_activity": protected_pauses,
            "reviewed_cuts_protected_by_activity": protected_repeats,
            "activity_intervals_ms": activity_intervals,
            "wordless_slices_removed": removed_wordless_slices,
            "original_duration_ms": original_duration,
            "new_duration_ms": new_duration,
            "saved_ms": saved_ms,
            "cuts_applied_to_slices": cuts_applied,
            "edit_transcript_cache": str(dry_transcript_cache_path) if dry_transcript_cache_path else None,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if not args.dry_run:
            # Write updated project.json
            with open(project_json_path, "w") as f:
                json.dump(project_data, f, ensure_ascii=False, separators=(",", ":"))

            # Record what we wrote so the next run can detect external edits.
            try:
                state_path.write_text(json.dumps({"last_written_sha": _file_sha256(project_json_path)}))
            except Exception:
                pass

        log("")
        log("=" * 50)
        log("🧪 Dry-run analysis complete; project.json was not modified:" if args.dry_run else "✅ Done! Summary:")
        log(f"   {'Pauses proposed' if args.dry_run else 'Pauses removed'}:    {len(pauses)}")
        log(f"   {'Repeats proposed' if args.dry_run else 'Repeats removed'}:   {len(repeats)}")
        log(f"   Silent slices:     {len(removed_wordless_slices)}")
        log(f"   Total cuts:        {cuts_applied}")
        log(f"   Original duration: {original_duration/1000:.1f}s")
        log(f"   New duration:      {new_duration/1000:.1f}s")
        saved_percent = saved_ms / original_duration * 100 if original_duration else 0.0
        log(f"   Time saved:        {saved_ms/1000:.1f}s ({saved_percent:.1f}%)")
        log(f"   Output ratio:      4:3 ✓")
        log(f"   Padding:           2% ✓")
        log(f"   Rounded corners:   25 ✓")
        log(f"   Camera size:       30% ✓")
        log(f"   Camera position:   top-right ✓")
        if not transcript_ok:
            log("")
            log("⚠️  SILENCE-ONLY RUN: ASR was unavailable, so there is no transcript.json.")
            log("    Re-run later (without --skip-transcribe) for repeat review, or proceed")
            log("    with pause-only editing.")

        log(f"   Audit report:      {report_path}")
        log("")
        if args.dry_run:
            log("Review the audit report, then run again without --dry-run to apply.")
        else:
            log("Open Screen Studio to preview the result.")
            log("Backup saved as project.json.bak")

        if repeats:
            log("")
            log("Removed repeated segments:")
            for r in repeats:
                log(f"  ✂️  \"{r.get('removed_text', '')[:80]}\"")


if __name__ == "__main__":
    main()
