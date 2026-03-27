#!/usr/bin/env python3
"""
Burn subtitles onto an exported video using ffmpeg.
Converts transcript.json (from mlx-whisper) → ASS subtitle file → burned video.

Style: white text, subtle shadow, no border, centered bottom, clean sans-serif.
"""

import argparse
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path


def log(msg):
    print(f"[burn-subtitles] {msg}", flush=True)


def add_cjk_spacing(text: str) -> str:
    """Add spaces between CJK characters and Latin/numeric characters for readability."""
    text = re.sub(r'([\u4e00-\u9fff\u3400-\u4dbf])([A-Za-z0-9])', r'\1 \2', text)
    text = re.sub(r'([A-Za-z0-9])([\u4e00-\u9fff\u3400-\u4dbf])', r'\1 \2', text)
    return text


def _visual_len(text: str) -> float:
    """Visual width estimate: CJK = 1.0, Latin/digits/punct = 0.55, space = 0.5."""
    w = 0.0
    for c in text:
        if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf' or '\u3000' <= c <= '\u303f':
            w += 1.0
        elif c == ' ':
            w += 0.5
        else:
            w += 0.55
    return w


def _split_text(text: str, max_chars: int) -> list[str]:
    """
    Split text into subtitle-sized chunks.
    Uses visual width (CJK=1.0, Latin=0.55) so mixed lines don't overflow.
    Tries to break at sentence-end punctuation first, then soft punctuation,
    then cuts at word boundaries as a last resort.
    """
    if _visual_len(text) <= max_chars:
        return [text]

    result = []

    def split_at(chunk: str, pattern: str) -> list[str]:
        parts = re.split(pattern, chunk)
        return [p.strip() for p in parts if p.strip()]

    # Pass 1: split at sentence-ending punctuation
    chunks = split_at(text, r'(?<=[。！？!?])\s*')
    if len(chunks) == 1:
        chunks = [text]  # no hard punct found

    # Pass 2: split oversized chunks at soft punctuation
    mid = []
    for c in chunks:
        if _visual_len(c) <= max_chars:
            mid.append(c)
        else:
            sub = split_at(c, r'(?<=[，,、；;])\s*')
            mid.extend(sub if len(sub) > 1 else [c])

    # Pass 3: cut at word boundaries, using visual width to find the split point
    for c in mid:
        while _visual_len(c) > max_chars:
            # Walk forward to find the last space whose prefix fits within max_chars
            cut_at = 0
            best_space = -1
            vw = 0.0
            for i, ch in enumerate(c):
                if ch == ' ' and vw <= max_chars:
                    best_space = i
                vw += _visual_len(ch)
                if vw > max_chars:
                    break
                cut_at = i + 1
            if best_space > len(c) // 4:
                cut_at = best_space
            # cut_at may be 0 if the very first char exceeds budget; force at least 1
            cut_at = max(cut_at, 1)
            result.append(c[:cut_at].rstrip())
            c = c[cut_at:].lstrip()
        if c:
            result.append(c)

    return result or [text]


def segments_to_lines(segments: list[dict], max_chars: int = 16) -> list[dict]:
    """
    Convert Whisper segments to subtitle lines using segment-level text.

    - Uses seg["text"] directly, so text corrections apply immediately
      (no need to touch the word-level tokens at all)
    - Respects segment boundaries as natural break points — Whisper segments
      correspond to breath groups / pauses, giving much more natural phrasing
      than re-grouping word tokens by character count
    - Long segments are split at punctuation first, then hard-cut;
      timing within a segment is interpolated proportionally by character count
    """
    lines = []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue

        # Apply CJK spacing before splitting so word-boundary detection
        # can see spaces at CJK↔Latin boundaries (e.g. "这个Screen" → "这个 Screen")
        text = add_cjk_spacing(text)

        start = seg["start"]
        end = seg["end"]
        sub_lines = _split_text(text, max_chars)

        if len(sub_lines) == 1:
            lines.append({"start": start, "end": end, "text": sub_lines[0]})
        else:
            # Join as a single multi-line subtitle using \n (converted to \N in ASS)
            # This keeps the original segment timing intact and avoids time-splitting words
            lines.append({"start": start, "end": end, "text": "\n".join(sub_lines)})

    return lines


def seconds_to_ass_time(s: float) -> str:
    """Convert seconds to ASS timestamp: H:MM:SS.cc"""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    cs = int((sec - int(sec)) * 100)
    return f"{h}:{m:02d}:{int(sec):02d}.{cs:02d}"


def generate_ass(lines: list[dict], output_path: Path, video_width: int = 1920, video_height: int = 1080):
    """Generate an ASS subtitle file with clean white + shadow style."""

    is_portrait = video_height > video_width
    # Portrait: 3.8% of display height (already large due to tall frame)
    # Landscape: 5.5% of height — previous 3.8% was too small on widescreen
    font_size = max(40, int(video_height * (0.038 if is_portrait else 0.055)))
    # Portrait videos need a higher bottom margin so subtitles don't sit at the very edge.
    margin_v = int(video_height * (0.20 if is_portrait else 0.06))

    ass_header = textwrap.dedent(f"""\
        [Script Info]
        ScriptType: v4.00+
        PlayResX: {video_width}
        PlayResY: {video_height}
        WrapStyle: 0

        [V4+ Styles]
        Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
        Style: Default,PingFang SC,{font_size},&H00FFFFFF,&H000000FF,&H40202020,&H40202020,0,0,0,0,100,100,0,0,3,3,0,2,20,20,{margin_v},1

        [Events]
        Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    """)

    event_lines = []
    for line in lines:
        start = seconds_to_ass_time(line["start"])
        end = seconds_to_ass_time(line["end"])
        text = add_cjk_spacing(line["text"]).replace("\n", "\\N")
        event_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ass_header)
        f.write("\n".join(event_lines))
        f.write("\n")

    log(f"✅ Generated ASS subtitle file: {output_path.name} ({len(event_lines)} lines)")


def _render_progress(elapsed_us: int, total_s: float, speed: float):
    """Print a single-line progress bar, overwriting the previous one."""
    elapsed_s = elapsed_us / 1_000_000
    pct = min(elapsed_s / total_s, 1.0) if total_s > 0 else 0
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    elapsed_fmt = f"{int(elapsed_s // 60)}:{int(elapsed_s % 60):02d}"
    total_fmt   = f"{int(total_s   // 60)}:{int(total_s   % 60):02d}"
    speed_str   = f"{speed:.1f}x" if speed > 0 else "..."
    sys.stdout.write(f"\r  {bar}  {pct:>3.0%}  {elapsed_fmt} / {total_fmt}  {speed_str}  ")
    sys.stdout.flush()


def burn_subtitles(video_path: Path, ass_path: Path, output_path: Path,
                   scale_to: tuple[int, int] | None = None, total_duration_s: float = 0):
    """Burn ASS subtitles into video using ffmpeg.

    scale_to: (width, height) to scale before rendering subtitles.
    total_duration_s: video duration for progress display.

    Rotation is handled automatically by ffmpeg's built-in autorotate. It both physically
    corrects the frame orientation AND clears the Display Matrix in the output, so no manual
    transpose filter or metadata patching is needed.
    """
    log(f"Burning subtitles into video...")

    # Escape special chars in path for ffmpeg filter
    ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    if scale_to:
        w, h = scale_to
        vf = f"scale={w}:{h},ass='{ass_str}'"
        # H264 at downscaled resolution (faster, smaller file)
        video_codec = ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
        log(f"Scaling to {w}x{h}")
    else:
        vf = f"ass='{ass_str}'"
        # HEVC quality-based encoding — matches original iPhone/Screen Studio quality
        # -q:v 65 on hevc_videotoolbox ≈ visually lossless for 4K source
        # -tag:v hvc1 ensures broad player compatibility (hev1 tag breaks QPlayer etc.)
        video_codec = ["-c:v", "hevc_videotoolbox", "-q:v", "65", "-tag:v", "hvc1"]
        log("Keeping original resolution (HEVC quality mode)")

    progress_path = Path("/tmp/ffmpeg_burn_progress.txt")
    progress_path.unlink(missing_ok=True)

    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", vf,
        "-c:a", "copy",
        *video_codec,
        "-progress", str(progress_path),
        "-loglevel", "error",
        str(output_path),
        "-y",
    ]

    import time
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)

    while proc.poll() is None:
        time.sleep(0.5)
        if not progress_path.exists():
            continue
        data = {}
        for line in progress_path.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
        raw_us = data.get("out_time_us", "0")
        elapsed_us = int(raw_us) if raw_us and raw_us.lstrip("-").isdigit() else 0
        speed_str = data.get("speed", "0x").replace("x", "")
        try:
            speed = float(speed_str)
        except ValueError:
            speed = 0.0
        _render_progress(elapsed_us, total_duration_s, speed)

    sys.stdout.write("\n")

    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "")
        raise RuntimeError(f"ffmpeg failed:\n{err[-1000:]}")

    size_mb = output_path.stat().st_size / 1024 / 1024
    log(f"Output: {output_path.name} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Burn subtitles onto exported Screen Studio video")
    parser.add_argument("--video", required=True, help="Path to exported video file (.mp4)")
    parser.add_argument("--transcript", required=True, help="Path to transcript.json from mlx-whisper")
    parser.add_argument("--output", default=None, help="Output video path (default: input_subtitled.mp4)")
    parser.add_argument("--max-chars", type=int, default=18, help="Max chars per subtitle line (default: 18)")
    parser.add_argument("--ass-only", action="store_true", help="Only generate .ass file, don't burn")
    parser.add_argument("--output-height", type=int, default=0,
                        help="Scale output: for landscape use height (e.g. 1440), for portrait use width (e.g. 1440). Default 0 = keep original resolution.")
    args = parser.parse_args()

    video_path = Path(args.video)
    transcript_path = Path(args.transcript)

    if not video_path.exists():
        print(f"❌ Video not found: {video_path}")
        sys.exit(1)
    if not transcript_path.exists():
        print(f"❌ Transcript not found: {transcript_path}")
        sys.exit(1)

    # Output paths
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = video_path.with_name(video_path.stem + "_subtitled.mp4")
    ass_path = output_path.with_suffix(".ass")

    # Load transcript (supports both plain array and {"segments": [...]} from preview editor)
    with open(transcript_path, encoding="utf-8") as f:
        raw = json.load(f)
    segments = raw.get("segments", raw) if isinstance(raw, dict) else raw
    log(f"📝 Loaded {len(segments)} transcript segments")

    # Get video dimensions
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)],
        capture_output=True, text=True
    )
    video_w, video_h, video_duration, video_rotation = 1920, 1080, 0.0, 0
    if probe.returncode == 0:
        for stream in json.loads(probe.stdout).get("streams", []):
            if stream.get("codec_type") == "video":
                video_w = stream.get("width", 1920)
                video_h = stream.get("height", 1080)
                video_duration = float(stream.get("duration", 0) or 0)
                # Detect rotation metadata (e.g. iPhone portrait stored as landscape + rotate)
                for sd in stream.get("side_data_list", []):
                    if "rotation" in sd:
                        try:
                            video_rotation = int(sd["rotation"])
                        except (ValueError, TypeError):
                            pass
                        break
                # Swap to display dimensions so portrait detection and ASS layout are correct
                if abs(video_rotation) in (90, 270):
                    video_w, video_h = video_h, video_w
                break
    log(f"📐 Video display resolution: {video_w}x{video_h}"
        + (f" (stored rotated {video_rotation}°)" if video_rotation else ""))

    # Compute output resolution (scale if requested)
    # For portrait video (height > width), scale by width to avoid tiny output.
    # --output-height 1440 on landscape 3840x2160 → 2560x1440 (2K)
    # --output-height 1440 on portrait  2160x3840 → would be 810x1440 (blurry)
    # So for portrait, treat output_height as the target for the SHORT side (width).
    is_portrait = video_h > video_w
    scale_to = None
    if args.output_height and args.output_height > 0:
        if is_portrait:
            # Scale by width: output_height arg acts as target width
            target_w = args.output_height
            if target_w < video_w:
                out_w = target_w if target_w % 2 == 0 else target_w + 1
                out_h = round(video_h * out_w / video_w)
                out_h = out_h if out_h % 2 == 0 else out_h + 1
                scale_to = (out_w, out_h)
                log(f"Portrait video detected — scaling by width to {out_w}x{out_h}")
        else:
            if args.output_height < video_h:
                out_h = args.output_height
                out_w = round(video_w * out_h / video_h)
                out_w = out_w if out_w % 2 == 0 else out_w + 1
                scale_to = (out_w, out_h)
    ass_w = scale_to[0] if scale_to else video_w
    ass_h = scale_to[1] if scale_to else video_h

    # Convert segments to subtitle lines
    lines = segments_to_lines(segments, args.max_chars)
    log(f"🔤 Generated {len(lines)} subtitle lines")

    # Generate ASS (at output resolution so font size is correct)
    generate_ass(lines, ass_path, ass_w, ass_h)

    if args.ass_only:
        log(f"Done. ASS file: {ass_path}")
        return

    # Burn subtitles
    burn_subtitles(video_path, ass_path, output_path, scale_to=scale_to,
                   total_duration_s=video_duration)

    log("")
    log("=" * 50)
    log("✅ Done!")
    log(f"   Output: {output_path}")
    log(f"   Subtitle file: {ass_path}")
    log("")
    log("Tip: Edit the .ass file to tweak font/size/position, then re-run with --ass-only skipped.")


if __name__ == "__main__":
    main()
