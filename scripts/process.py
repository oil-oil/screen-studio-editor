#!/usr/bin/env python3
"""
Screen Studio Auto-Editor
Removes pauses and repeated narration from a .screenstudio project,
and enables native captions.
"""

import argparse
import json
import random
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path


def log(msg):
    print(f"[screen-studio-editor] {msg}", flush=True)


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


def merge_audio(project_dir: Path, sessions: list[dict], output_path: Path) -> list[dict]:
    """
    Merge all microphone .m4a segments into a single WAV (concatenated, no gap padding).
    Returns session offset mappings for timestamp conversion:
      [{"processTimeStartMs": ..., "audioOffsetMs": ..., "durationMs": ...}, ...]
    audioOffsetMs = where this session starts in the merged audio (cumulative ms).
    """
    recording_dir = project_dir / "recording"
    valid_sessions = []
    current_offset_ms = 0.0

    for session in sessions:
        filename = session.get("outputFilename", "")
        mic_file = recording_dir / filename
        if not mic_file.exists():
            log(f"⚠️  Audio file not found: {filename}, skipping.")
            continue
        valid_sessions.append({
            "processTimeStartMs": session["processTimeStartMs"],
            "durationMs": session["durationMs"],
            "audioOffsetMs": current_offset_ms,
            "file": str(mic_file),
        })
        current_offset_ms += session["durationMs"]

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


def transcribe(audio_path: Path, language: str | None = "zh") -> list[dict]:
    """
    Run mlx-whisper large-v3 on the audio file.
    Returns list of segments with word-level timestamps:
      [{"start": float, "end": float, "text": str, "words": [{"word": str, "start": float, "end": float}]}]

    language: ISO 639-1 code (e.g. 'zh', 'en') or None for auto-detect.
    Defaults to 'zh' to prevent Whisper from outputting Traditional Chinese for Mandarin content.
    Pass --language en for English recordings, or --language None to auto-detect.
    """
    lang_display = language if language else "auto-detect"
    log(f"🎙️  Transcribing with mlx-whisper large-v3 (language={lang_display}, ~10-30s)...")

    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        word_timestamps=True,
        language=language,
    )

    segments = result.get("segments", [])
    log(f"✅ Transcribed {len(segments)} segments, {sum(len(s.get('words',[])) for s in segments)} words.")
    return segments


def map_audio_time_to_process_time(audio_offset_s: float, session_offsets: list[dict]) -> float:
    """
    Convert a timestamp in the merged audio (seconds) to slice coordinate.

    The sourceStartMs / sourceEndMs fields in Screen Studio slices use the
    *merged audio timeline* as their coordinate system: sessions are laid out
    back-to-back starting at 0, with no gaps between them.  That means the
    slice coordinate equals the merged-audio offset in milliseconds — which is
    simply the audio timestamp itself.
    """
    return audio_offset_s * 1000.0


def detect_pauses(segments: list[dict], threshold_ms: float, min_pause_ms: float, session_offsets: list[dict]) -> list[dict]:
    """
    Detect pauses between words longer than threshold_ms.
    Returns list of {"start_process_ms": ..., "end_process_ms": ..., "duration_ms": ...}
    representing ranges to cut (leaving min_pause_ms of silence).
    """
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            words.append(w)

    if not words:
        return []

    pauses = []
    for i in range(len(words) - 1):
        gap_start_s = words[i]["end"]
        gap_end_s = words[i + 1]["start"]
        gap_ms = (gap_end_s - gap_start_s) * 1000.0

        if gap_ms > threshold_ms:
            # We'll cut from (gap_start + min_pause_ms/2) to (gap_end - min_pause_ms/2)
            # to leave a natural-sounding gap.
            # Extra 80ms padding guards against Whisper timestamp inaccuracy (±50-200ms):
            # Whisper often reports word boundaries slightly early or late, so leaving
            # an extra buffer prevents accidentally clipping the tail of the preceding
            # word or the onset of the following word.
            SPEECH_PAD_S = 0.08
            cut_start_s = gap_start_s + (min_pause_ms / 2000.0) + SPEECH_PAD_S
            cut_end_s = gap_end_s - (min_pause_ms / 2000.0) - SPEECH_PAD_S

            cut_duration_ms = (cut_end_s - cut_start_s) * 1000.0
            # Skip cuts shorter than 300ms: Whisper timestamp inaccuracy (±50-200ms)
            # means a tiny cut is more likely to clip real speech than remove silence.
            if cut_end_s > cut_start_s and cut_duration_ms >= 300:
                start_pm = map_audio_time_to_process_time(cut_start_s, session_offsets)
                end_pm = map_audio_time_to_process_time(cut_end_s, session_offsets)
                pauses.append({
                    "start_ms": start_pm,
                    "end_ms": end_pm,
                    "duration_ms": gap_ms,
                    "text_before": words[i]["word"].strip(),
                    "text_after": words[i + 1]["word"].strip(),
                })

    log(f"🔍 Found {len(pauses)} pause(s) > {threshold_ms}ms to cut.")
    return pauses


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


def detect_repeats(cuts_file: str) -> list[dict]:
    """
    Load repeat cuts from a JSON file produced by Claude in the conversation.
    Format: [{"start_ms": ..., "end_ms": ..., "removed_text": "..."}]
    """
    if not cuts_file:
        return []
    import os
    if not os.path.exists(cuts_file):
        log(f"⚠️  Cuts file not found: {cuts_file}, skipping repeat detection.")
        return []
    with open(cuts_file) as f:
        repeats = json.load(f)
    log(f"✂️  Loaded {len(repeats)} repeat cut(s) from {cuts_file}")
    for r in repeats:
        log(f"   Remove: \"{r.get('removed_text', '')[:70]}\"")
    return repeats


def get_session_boundaries_ms(session_offsets: list[dict]) -> list[float]:
    """
    Return the merged-audio timestamps (in ms) at which each new recording session begins.
    These are the "natural" transition points that Screen Studio originally placed between sessions.
    The first session always starts at 0 so only boundaries *between* sessions are returned.
    """
    boundaries = []
    cumulative = 0.0
    for s in session_offsets:
        cumulative += s["durationMs"]
        boundaries.append(cumulative)
    return boundaries[:-1]  # exclude the very end; only inter-session boundaries


_SNAP_MARGIN_MS = 10  # snap 10ms before boundary; see snap_to_session_boundaries docstring


def snap_to_session_boundaries(slices: list[dict], boundaries_ms: list[float]) -> tuple[list[dict], int]:
    """
    After pause-removal cuts, some inter-session boundaries end up inside a gap between
    slices (because the removed pause straddled the boundary).  Screen Studio originally
    rendered a smooth spring transition at those boundaries; the cuts destroy it.

    Fix: if a boundary B falls inside a gap [slice_n.sourceEndMs, slice_{n+1}.sourceStartMs],
    clamp both endpoints to (B - MARGIN).  This makes the pair source-contiguous
    (gap = 0 ms) which re-enables Screen Studio's spring animation, while keeping
    the snap point safely inside session N's audio range.

    Why the margin is necessary: metadata durationMs and the actual WAV file length
    differ by ~0.3 µs.  Snapping to the exact boundary causes Screen Studio's audio
    composer to generate a sub-millisecond segment at the session seam, which it
    rejects ("Invalid time range: duration must be positive").  A 10 ms margin
    produces a short but valid segment (~10 ms of near-silence from the tail of
    session N) that the audio composer accepts.  10 ms is imperceptible.

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
                slices[i]["sourceEndMs"]       = snap_point
                slices[i + 1]["sourceStartMs"] = snap_point
                snapped += 1
                log(f"🔗 Session boundary {b/1000:.3f}s snapped between slice {i} and {i+1} → gap restored to 0 (margin={_SNAP_MARGIN_MS}ms)")
                break  # each boundary can only fall in one gap

    return slices, snapped


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
            if end - start > 100:  # Skip tiny fragments < 100ms
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


def main():
    parser = argparse.ArgumentParser(description="Screen Studio Auto-Editor")
    parser.add_argument("--project", required=True, help="Path to .screenstudio directory")
    parser.add_argument("--pause-threshold", type=float, default=800, help="Pause threshold in ms (default: 800)")
    parser.add_argument("--min-pause", type=float, default=800, help="Minimum pause to keep in ms (default: 800)")
    parser.add_argument("--cuts-file", default=None, help="JSON file with repeat cuts (produced by Claude in conversation)")
    parser.add_argument("--skip-transcribe", help="Path to existing transcript JSON to reuse")
    parser.add_argument("--language", default="zh",
                        help="Whisper language code (default: zh). Use 'en' for English, 'None' to auto-detect.")
    args = parser.parse_args()

    project_dir = Path(args.project)
    if not project_dir.exists() or not project_dir.is_dir():
        print(f"❌ Project not found: {project_dir}")
        sys.exit(1)

    project_json_path = project_dir / "project.json"
    backup_path = project_dir / "project.json.bak"

    # Backup (no-op if backup already exists)
    backup_project(project_json_path)

    # Always load from backup — it is created on the first run and never
    # overwritten, so it always holds the original unedited project.json.
    # This makes every run idempotent: pauses + repeats are applied to the
    # original single slice, never to already-cut slices from a prior run.
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
        session_offsets = merge_audio(project_dir, mic_sessions, merged_audio)

        # Transcribe
        if args.skip_transcribe:
            with open(args.skip_transcribe) as f:
                segments = json.load(f)
            log(f"♻️  Loaded existing transcript from {args.skip_transcribe}")
        else:
            lang = None if args.language == "None" else args.language
            segments = transcribe(merged_audio, language=lang)

            # Save transcript for debugging
            transcript_out = project_dir / "transcript.json"
            with open(transcript_out, "w") as f:
                json.dump(segments, f, indent=2, ensure_ascii=False)
            log(f"💾 Saved transcript → transcript.json")

        # Detect pauses
        pauses = detect_pauses(segments, args.pause_threshold, args.min_pause, session_offsets)

        # Validate pause cuts against actual audio silence regions.
        # This filters out cuts where Whisper detected a gap but the audio is not
        # actually silent (timestamp inaccuracy). Uses -28dB threshold (same as
        # auto-editor default), which is lenient enough to catch real inter-word
        # pauses without triggering on background room noise.
        silence_regions = detect_silence_regions(merged_audio)
        pauses = filter_pauses_by_silence(pauses, silence_regions)

        # Load repeat cuts (produced by Claude in the conversation, if any)
        repeats = detect_repeats(args.cuts_file)

        # Add 150ms inward padding to repeat cuts to protect neighboring speech from clipping.
        # Whisper start/end timestamps for a segment have ±100-200ms inaccuracy — the "start_ms"
        # of the region to remove might land slightly inside the last good word. Shrinking each
        # repeat cut inward by 150ms on both ends preserves the natural word onset/offset.
        REPEAT_PAD_MS = 150
        padded_repeats = []
        for r in repeats:
            padded = dict(r)
            padded["start_ms"] = r["start_ms"] + REPEAT_PAD_MS
            padded["end_ms"] = r["end_ms"] - REPEAT_PAD_MS
            if padded["end_ms"] > padded["start_ms"] + 200:
                padded_repeats.append(padded)
            else:
                log(f"⚠️  Repeat cut too short after padding, skipping: \"{r.get('removed_text','')[:50]}\"")
        repeats = padded_repeats

        all_cuts = pauses + repeats

        original_slices = project_data["json"]["scenes"][0]["slices"]
        new_slices, cuts_applied = apply_cuts(original_slices, all_cuts)

        session_boundaries = get_session_boundaries_ms(session_offsets)
        new_slices, snapped = snap_to_session_boundaries(new_slices, session_boundaries)

        # Calculate time saved
        original_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in original_slices)
        new_duration = sum(s["sourceEndMs"] - s["sourceStartMs"] for s in new_slices)
        saved_ms = original_duration - new_duration

        # Update project
        project_data["json"]["scenes"][0]["slices"] = new_slices
        project_data["json"]["config"]["showTranscript"] = True
        project_data["json"]["config"]["backgroundPaddingRatio"] = 1.02  # 2% padding
        project_data["json"]["config"]["cameraAspectRatio"] = "square"   # 宽高一致（正方形）
        project_data["json"]["config"]["improveMicrophoneAudio"] = True  # 降噪 + 音量均一化
        # Note: cameraRoundness is intentionally NOT set — keep Screen Studio's default roundness

        # Write updated project.json
        with open(project_json_path, "w") as f:
            json.dump(project_data, f, ensure_ascii=False, separators=(",", ":"))

        log("")
        log("=" * 50)
        log("✅ Done! Summary:")
        log(f"   Pauses removed:    {len(pauses)}")
        log(f"   Repeats removed:   {len(repeats)}")
        log(f"   Total cuts:        {cuts_applied}")
        log(f"   Original duration: {original_duration/1000:.1f}s")
        log(f"   New duration:      {new_duration/1000:.1f}s")
        log(f"   Time saved:        {saved_ms/1000:.1f}s ({saved_ms/original_duration*100:.1f}%)")
        log(f"   Captions:          enabled ✓")
        log("")
        log("Open Screen Studio to preview the result.")
        log("Backup saved as project.json.bak")

        if repeats:
            log("")
            log("Removed repeated segments:")
            for r in repeats:
                log(f"  ✂️  \"{r.get('removed_text', '')[:80]}\"")


if __name__ == "__main__":
    main()
