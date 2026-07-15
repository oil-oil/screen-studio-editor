#!/usr/bin/env python3
"""Build a source-timeline-aligned A/V proxy for multi-session recordings.

Screen Studio stores every pause/resume segment as a separate display and
microphone file, while slice source times place the metadata durations
back-to-back.  Raw media files can be slightly longer or shorter than those
durations.  This script trims or pads every segment to its metadata duration
before concatenation so a review model sees the same timeline as project.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def recorder_sessions(metadata: dict[str, Any], recorder_type: str) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for recorder in metadata.get("recorders") or []:
        recorder_id = str(recorder.get("id") or "")
        if recorder.get("type") == recorder_type or recorder_type in recorder_id:
            sessions.extend(recorder.get("sessions") or [])
    return sorted(sessions, key=lambda item: float(item.get("processTimeStartMs", 0.0)))


def session_media(project_dir: Path, session: dict[str, Any]) -> Path:
    filename = str(session.get("outputFilename") or "")
    if not filename:
        fail("A recorder session has no outputFilename.")
    direct = project_dir / "recording" / filename
    if direct.exists():
        return direct
    playlist = direct.with_suffix(".m3u8")
    if playlist.exists():
        return playlist
    fail(f"Session media does not exist: {direct}")


def session_duration_s(session: dict[str, Any]) -> float:
    duration = float(session.get("durationMs", 0.0)) / 1000.0
    if duration <= 0:
        fail("A recorder session reports a non-positive duration.")
    return duration


def run_ffmpeg(args: list[str], *, description: str) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"ffmpeg failed while {description}:\n{result.stderr.strip()}")


def probe_duration_s(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        fail(f"Could not probe generated media duration: {path}")


def build_video_proxy(
    project_dir: Path,
    sessions: list[dict[str, Any]],
    output: Path,
    *,
    width: int,
    height: int,
    fps: float,
) -> None:
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, session in enumerate(sessions):
        duration = session_duration_s(session)
        inputs.extend(["-i", str(session_media(project_dir, session))])
        label = f"v{index}"
        labels.append(f"[{label}]")
        # tpad makes short source files deterministic; the final trim fixes the
        # segment to metadata time even when the encoded file has clock drift.
        filters.append(
            f"[{index}:v:0]trim=duration={duration:.6f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={duration:.6f},"
            f"trim=duration={duration:.6f},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps:g},format=yuv420p[{label}]"
        )
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[outv]", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "30", "-movflags", "+faststart", "-y", str(output),
        ],
        description="building the display review proxy",
    )


def build_audio_proxy(
    project_dir: Path,
    sessions: list[dict[str, Any]],
    output: Path,
) -> None:
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, session in enumerate(sessions):
        duration = session_duration_s(session)
        inputs.extend(["-i", str(session_media(project_dir, session))])
        label = f"a{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:a:0]aresample=16000,apad,"
            f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[{label}]"
        )
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[outa]", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            "-y", str(output),
        ],
        description="building the microphone review proxy",
    )


def validate_pairing(
    display: list[dict[str, Any]], microphone: list[dict[str, Any]]
) -> float:
    if not display or not microphone:
        fail("The project must contain display and microphone sessions.")
    if len(display) != len(microphone):
        fail(
            f"Display/microphone session count differs: {len(display)} vs {len(microphone)}."
        )
    total = 0.0
    for index, (video_session, audio_session) in enumerate(zip(display, microphone)):
        video_duration = session_duration_s(video_session)
        audio_duration = session_duration_s(audio_session)
        if abs(video_duration - audio_duration) > 0.050:
            fail(
                f"Session {index} display/microphone metadata differs by more than 50ms."
            )
        total += audio_duration
    return total


def build(project_dir: Path, *, width: int, height: int, fps: float, force: bool) -> dict[str, Any]:
    project_dir = project_dir.expanduser().resolve()
    metadata_path = project_dir / "recording" / "metadata.json"
    if not (project_dir / "project.json").exists() or not metadata_path.exists():
        fail(f"Not a Screen Studio project: {project_dir}")
    metadata = load_json(metadata_path)
    display = recorder_sessions(metadata, "display")
    microphone = recorder_sessions(metadata, "microphone")
    expected_duration = validate_pairing(display, microphone)

    output_dir = project_dir / "review-proxy"
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "display-timeline.mp4"
    audio_path = output_dir / "microphone-timeline.wav"
    combined_path = output_dir / "combined-timeline.mp4"
    if force or not video_path.exists():
        build_video_proxy(project_dir, display, video_path, width=width, height=height, fps=fps)
    if force or not audio_path.exists():
        build_audio_proxy(project_dir, microphone, audio_path)
    if force or not combined_path.exists():
        run_ffmpeg(
            [
                "-i", str(video_path), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "64k", "-shortest", "-movflags",
                "+faststart", "-y", str(combined_path),
            ],
            description="muxing the aligned multimodal review proxy",
        )

    video_duration = probe_duration_s(video_path)
    audio_duration = probe_duration_s(audio_path)
    combined_duration = probe_duration_s(combined_path)
    tolerance = max(0.20, 2.0 / fps)
    if abs(video_duration - expected_duration) > tolerance:
        fail(
            f"Video proxy duration drift is {video_duration - expected_duration:+.3f}s "
            f"(allowed {tolerance:.3f}s)."
        )
    if abs(audio_duration - expected_duration) > 0.050:
        fail(
            f"Audio proxy duration drift is {audio_duration - expected_duration:+.3f}s."
        )
    if abs(combined_duration - expected_duration) > tolerance:
        fail(
            f"Combined proxy duration drift is {combined_duration - expected_duration:+.3f}s "
            f"(allowed {tolerance:.3f}s)."
        )
    return {
        "project": str(project_dir),
        "sessions": len(display),
        "expected_duration_s": round(expected_duration, 3),
        "video_duration_s": round(video_duration, 3),
        "audio_duration_s": round(audio_duration, 3),
        "combined_duration_s": round(combined_duration, 3),
        "video": str(video_path),
        "audio": str(audio_path),
        "combined": str(combined_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--fps", type=float, default=6.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        fail("Width, height, and fps must be positive.")
    print(json.dumps(
        build(
            args.project,
            width=args.width,
            height=args.height,
            fps=args.fps,
            force=args.force,
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
