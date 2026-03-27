#!/usr/bin/env python3
"""
Screen Studio Project Merger

Screen Studio stores each recording session as channel files:
  channel-3-microphone-0.m4a, channel-3-microphone-0.m3u8,
  channel-3-microphone-0-0001.m4s, ...

When merging, supplement's session 0 becomes session N
(where N = number of existing sessions in the base for that channel).
All related segment files are renamed accordingly, and .m3u8 content is updated.

Usage:
  merge_projects.py --base A.screenstudio --supplement B.screenstudio
  merge_projects.py --base A.screenstudio --supplement B.screenstudio --output Merged.screenstudio
  merge_projects.py --base A.screenstudio --supplement B.screenstudio --insert-after-slice 5
"""

import argparse
import copy
import json
import random
import re
import shutil
import string
import sys
from pathlib import Path


def log(msg):
    print(f"[merge] {msg}", flush=True)


def random_id(k=10):
    return "".join(random.choices(string.ascii_letters + string.digits, k=k))


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def get_mic_sessions(metadata: dict) -> list[dict]:
    """Return microphone sessions sorted by processTimeStartMs."""
    for recorder in metadata.get("recorders", []):
        if recorder.get("type") == "microphone":
            return sorted(recorder.get("sessions", []),
                          key=lambda s: s["processTimeStartMs"])
    # Fallback: input recorder
    for recorder in metadata.get("recorders", []):
        if recorder.get("type") == "input":
            return sorted(recorder.get("sessions", []),
                          key=lambda s: s["processTimeStartMs"])
    return []


def get_total_mic_duration_ms(metadata: dict) -> float:
    """Sum of all microphone session durations = audio coordinate space size."""
    return sum(s.get("durationMs", 0) for s in get_mic_sessions(metadata))


def get_max_process_time_end_ms(metadata: dict) -> float:
    """Maximum processTimeEndMs across all recorders."""
    max_end = 0.0
    for recorder in metadata.get("recorders", []):
        for s in recorder.get("sessions", []):
            end = s.get("processTimeEndMs", 0)
            max_end = max(max_end, end)
    return max_end


def build_channel_session_count(metadata: dict) -> dict[str, int]:
    """Return {channel_id: session_count} for all recorders with sessions."""
    counts = {}
    for recorder in metadata.get("recorders", []):
        rid = recorder.get("id", "")
        sessions = recorder.get("sessions", [])
        if sessions:
            counts[rid] = len(sessions)
    return counts


def rename_channel_files(
    src_recording_dir: Path,
    dst_recording_dir: Path,
    channel_id: str,
    old_idx: int,
    new_idx: int,
    file_rename_map: dict[str, str],
):
    """
    Copy all files belonging to channel_id session old_idx into dst_recording_dir
    with session index new_idx. Update .m3u8 content.

    File patterns:
      {channel_id}-{old_idx}.m4a          → {channel_id}-{new_idx}.m4a
      {channel_id}-{old_idx}.mp4          → {channel_id}-{new_idx}.mp4
      {channel_id}-{old_idx}.m3u8         → {channel_id}-{new_idx}.m3u8
      {channel_id}-{old_idx}-NNNN.mp4    → {channel_id}-{new_idx}-NNNN.mp4
      {channel_id}-{old_idx}-NNNN.m4s    → {channel_id}-{new_idx}-NNNN.m4s
    """
    old_prefix = f"{channel_id}-{old_idx}"
    new_prefix = f"{channel_id}-{new_idx}"

    copied = 0
    for src_file in sorted(src_recording_dir.iterdir()):
        if not src_file.is_file():
            continue
        name = src_file.name
        if name == old_prefix + ".m4a" or name == old_prefix + ".mp4":
            # Simple rename: channel-X-type-0.m4a → channel-X-type-2.m4a
            new_name = new_prefix + src_file.suffix
            shutil.copy2(src_file, dst_recording_dir / new_name)
            file_rename_map[name] = new_name
            copied += 1
        elif name == old_prefix + ".m3u8":
            # Update .m3u8 content: replace all occurrences of old_prefix with new_prefix
            content = src_file.read_text()
            new_content = content.replace(old_prefix + "-", new_prefix + "-")
            new_name = new_prefix + ".m3u8"
            (dst_recording_dir / new_name).write_text(new_content)
            file_rename_map[name] = new_name
            copied += 1
        elif name.startswith(old_prefix + "-"):
            # Segment files: channel-X-type-0-0001.m4s → channel-X-type-2-0001.m4s
            suffix = name[len(old_prefix):]  # e.g. "-0001.m4s"
            new_name = new_prefix + suffix
            shutil.copy2(src_file, dst_recording_dir / new_name)
            file_rename_map[name] = new_name
            copied += 1

    return copied


def rename_input_files(
    src_recording_dir: Path,
    dst_recording_dir: Path,
    old_idx: int,
    new_idx: int,
    file_rename_map: dict[str, str],
):
    """Rename input recorder session files: keystrokes-N, mouseclicks-N, mousemoves-N."""
    for prefix in ("keystrokes", "mouseclicks", "mousemoves"):
        for src_file in src_recording_dir.iterdir():
            if src_file.is_file() and src_file.stem == f"{prefix}-{old_idx}":
                new_name = f"{prefix}-{new_idx}{src_file.suffix}"
                shutil.copy2(src_file, dst_recording_dir / new_name)
                file_rename_map[src_file.name] = new_name


def merge_projects(
    base_dir: Path,
    supplement_dir: Path,
    output_dir: Path,
    insert_after_slice: int | None,
):
    log(f"Base:       {base_dir.name}")
    log(f"Supplement: {supplement_dir.name}")
    log(f"Output:     {output_dir.name}")

    # ── Validate ──────────────────────────────────────────────────────────────
    for p, label in [(base_dir, "base"), (supplement_dir, "supplement")]:
        if not (p / "project.json").exists():
            log(f"❌ {label}: project.json not found"); sys.exit(1)
        if not (p / "recording" / "metadata.json").exists():
            log(f"❌ {label}: recording/metadata.json not found"); sys.exit(1)

    # ── Load data ─────────────────────────────────────────────────────────────
    base_project  = load_json(base_dir  / "project.json")
    supp_project  = load_json(supplement_dir / "project.json")
    base_meta     = load_json(base_dir  / "recording" / "metadata.json")
    supp_meta     = load_json(supplement_dir / "recording" / "metadata.json")

    audio_offset_ms = get_total_mic_duration_ms(base_meta)
    supp_dur_ms     = get_total_mic_duration_ms(supp_meta)
    max_base_end    = get_max_process_time_end_ms(base_meta)

    log(f"Base audio duration:       {audio_offset_ms/1000:.1f}s")
    log(f"Supplement audio duration: {supp_dur_ms/1000:.1f}s")
    log(f"Supplement slice offset:   +{audio_offset_ms/1000:.1f}s")

    # ── Create output from base ───────────────────────────────────────────────
    if output_dir.exists():
        log(f"⚠️  Output already exists: {output_dir}")
        resp = input("Overwrite? [y/N]: ").strip().lower()
        if resp != "y":
            log("Aborted."); sys.exit(0)
        shutil.rmtree(output_dir)

    shutil.copytree(base_dir, output_dir)
    log("✅ Copied base project to output.")

    out_rec = output_dir / "recording"
    supp_rec = supplement_dir / "recording"

    # ── Determine session index mapping ───────────────────────────────────────
    # For each channel, supplement's session 0 → base_session_count
    base_counts = build_channel_session_count(base_meta)
    log(f"Base session counts: {base_counts}")

    # ── Copy supplement recording files with correct session indices ──────────
    file_rename_map: dict[str, str] = {}  # old_name → new_name

    updated_supp_meta = copy.deepcopy(supp_meta)
    pts_shift = max_base_end + 1000  # supplement sessions come after base ends

    for i, recorder in enumerate(updated_supp_meta.get("recorders", [])):
        rid = recorder.get("id", "")
        rtype = recorder.get("type", "")
        sessions = recorder.get("sessions", [])
        if not sessions:
            continue

        base_count = base_counts.get(rid, 0)

        for j, session in enumerate(sessions):
            old_idx = j
            new_idx = base_count + j

            # Shift processTimeStartMs/End so this session sorts after base
            min_supp_pts = min(s.get("processTimeStartMs", 0)
                               for r in supp_meta.get("recorders", [])
                               for s in r.get("sessions", []))
            session["processTimeStartMs"] = (
                session.get("processTimeStartMs", 0) - min_supp_pts + pts_shift
            )
            session["processTimeEndMs"] = (
                session.get("processTimeEndMs", 0) - min_supp_pts + pts_shift
            )

            if rtype in ("systemAudio", "display", "microphone", "webcam"):
                n = rename_channel_files(supp_rec, out_rec, rid, old_idx, new_idx, file_rename_map)
                log(f"  {rid}: session {old_idx}→{new_idx}, {n} files copied")
                # Update outputFilename in session metadata
                old_fn = session.get("outputFilename", "")
                if old_fn in file_rename_map:
                    session["outputFilename"] = file_rename_map[old_fn]

            elif rtype == "input":
                rename_input_files(supp_rec, out_rec, old_idx, new_idx, file_rename_map)
                session["keyStrokesFilename"]  = f"keystrokes-{new_idx}.json"
                session["mouseClicksFilename"] = f"mouseclicks-{new_idx}.json"
                session["mouseMovesFilename"]  = f"mousemoves-{new_idx}.json"
                log(f"  {rid}: input session {old_idx}→{new_idx}")

    # ── Merge metadata ────────────────────────────────────────────────────────
    output_meta = copy.deepcopy(base_meta)

    for supp_rec_entry in updated_supp_meta.get("recorders", []):
        rid = supp_rec_entry.get("id", "")
        supp_sessions = supp_rec_entry.get("sessions", [])
        if not supp_sessions:
            continue
        matched = False
        for out_rec_entry in output_meta.get("recorders", []):
            if out_rec_entry.get("id") == rid:
                out_rec_entry.setdefault("sessions", []).extend(
                    copy.deepcopy(supp_sessions)
                )
                matched = True
                break
        if not matched:
            output_meta.setdefault("recorders", []).append(
                copy.deepcopy(supp_rec_entry)
            )

    # Merge top-level sessions array if present
    if "sessions" in supp_meta:
        supp_top_sessions = copy.deepcopy(supp_meta["sessions"])
        min_supp_pts = min(s.get("processTimeStartMs", 0) for s in supp_top_sessions)
        for s in supp_top_sessions:
            s["processTimeStartMs"] = s.get("processTimeStartMs", 0) - min_supp_pts + pts_shift
            s["processTimeEndMs"]   = s.get("processTimeEndMs",   0) - min_supp_pts + pts_shift
        output_meta.setdefault("sessions", []).extend(supp_top_sessions)

    # ── Offset supplement slices ──────────────────────────────────────────────
    supp_slices = supp_project["json"]["scenes"][0]["slices"]
    offset_slices = []
    for sl in supp_slices:
        new_sl = copy.deepcopy(sl)
        new_sl["sourceStartMs"] = sl["sourceStartMs"] + audio_offset_ms
        new_sl["sourceEndMs"]   = sl["sourceEndMs"]   + audio_offset_ms
        new_sl["id"] = random_id()
        offset_slices.append(new_sl)

    # ── Combine slices ────────────────────────────────────────────────────────
    base_slices = base_project["json"]["scenes"][0]["slices"]
    if insert_after_slice is None:
        merged_slices = base_slices + offset_slices
        placement = "at the END of the timeline"
        log(f"Appending {len(offset_slices)} supplement slice(s) after {len(base_slices)} base slice(s).")
    else:
        pos = min(insert_after_slice + 1, len(base_slices))
        merged_slices = base_slices[:pos] + offset_slices + base_slices[pos:]
        placement = f"after slice {insert_after_slice}"
        log(f"Inserting {len(offset_slices)} supplement slice(s) at position {pos}.")

    # ── Write output ──────────────────────────────────────────────────────────
    out_project = copy.deepcopy(base_project)
    out_project["json"]["scenes"][0]["slices"] = merged_slices

    save_json(output_dir / "project.json", out_project)
    save_json(output_dir / "recording" / "metadata.json", output_meta)

    log("")
    log("=" * 52)
    log("✅ Merge complete!")
    log(f"   Base slices:         {len(base_slices)}")
    log(f"   Supplement slices:   {len(offset_slices)}")
    log(f"   Total slices:        {len(merged_slices)}")
    log(f"   Base duration:       {audio_offset_ms/1000:.1f}s")
    log(f"   Supplement duration: {supp_dur_ms/1000:.1f}s")
    log(f"   Combined duration:   {(audio_offset_ms+supp_dur_ms)/1000:.1f}s")
    log("")
    log(f"   Open Screen Studio: {output_dir}")
    log(f"   Supplement appears {placement}.")


def main():
    parser = argparse.ArgumentParser(description="Merge two Screen Studio projects")
    parser.add_argument("--base",       required=True)
    parser.add_argument("--supplement", required=True)
    parser.add_argument("--output",     default=None)
    parser.add_argument("--insert-after-slice", type=int, default=None, metavar="N")
    args = parser.parse_args()

    base_dir = Path(args.base)
    supp_dir = Path(args.supplement)
    if not base_dir.exists():
        print(f"❌ Base not found: {base_dir}"); sys.exit(1)
    if not supp_dir.exists():
        print(f"❌ Supplement not found: {supp_dir}"); sys.exit(1)

    output_dir = (
        Path(args.output) if args.output
        else base_dir.parent / f"{base_dir.stem}_Merged.screenstudio"
    )
    merge_projects(base_dir, supp_dir, output_dir, args.insert_after_slice)


if __name__ == "__main__":
    main()
