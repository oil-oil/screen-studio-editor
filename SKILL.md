---
name: screen-studio-editor
description: >
  Edit Screen Studio recordings and burn accurate AI-corrected subtitles onto videos.
  Use this skill when the user provides a .screenstudio project and wants it cleaned up,
  when the user wants pauses or repeated narration removed, when the user wants subtitles
  burned into an mp4, or when the user wants two Screen Studio projects merged.
---

# Screen Studio Editor

Use this skill for three jobs:

- **Edit a `.screenstudio` project**: remove long pauses, obvious repeated narration, and empty timeline fragments.
- **Burn subtitles into an `.mp4`**: transcribe, review, preview with the user, then burn.
- **Merge two `.screenstudio` projects**: combine base and supplement recordings.

The scripts handle mechanical timeline details. Do not repeat their internal logic in your response. Focus on the decisions the Agent must make: what to run, what to inspect, what to cut, what to ask the user to preview, and when to wait.

## Visual Defaults

- **`process.py` applies these to the project automatically — you do not set them by hand:** 4:3 output aspect, 2% background padding, 25 window corner radius, and the camera at 30% size, square aspect, pinned top-right. It also runs microphone audio cleanup (noise reduction + volume normalization). It does **not** enable Screen Studio's native captions — subtitles are burned separately in Mode B.
- Burned subtitles use `PingFang SC`, white text, no text outline, no drop shadow, and a slightly dark translucent rounded background.
- Subtitles are centered near the bottom: roughly a 6% bottom margin for landscape/4:3 video and 20% for portrait. There is no special "safe-area" logic beyond that margin.

## Setup

At the start of each session:

```bash
SKILL_DIR="/Users/linzhihuang/.claude/skills/screen-studio-editor"
PYTHON="$SKILL_DIR/.venv/bin/python3"
```

If setup has never been run:

```bash
bash "$SKILL_DIR/setup.sh"
```

Do not manually edit `project.json` unless you are diagnosing or repairing a specific problem the script cannot handle.

## Mode A: Edit `.screenstudio`

### 1. Validate

Confirm the provided path exists and contains:

- `project.json`
- `recording/`

If the user did not specify settings, use:

- `--pause-threshold 800`
- `--min-pause 300`
- `--pause-source silence`
- `--language zh` for Chinese/Mandarin content

### 2. Run the editor

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --language zh
```

If `transcript.json` already exists and you want to reuse the existing transcription:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --language zh
```

**Notes on `process.py`:**

- On the first run it backs up `project.json` to `project.json.bak` and **always re-applies edits from that backup**, so runs are idempotent — re-run with `--cuts-file` to add repeat cuts without stacking them on already-cut slices. It warns if `project.json` was changed externally (e.g. edited in Screen Studio) since the last run, because those changes would be discarded.
- Pause cuts come from **measured silence**, not ASR word timestamps — Whisper word boundaries drift ~100–400ms (and can jump seconds after long pauses), so silence is the reliable cut source; ASR stays an opt-in fallback via `--pause-source`. Pass `--silence-db auto` to adapt the threshold to each recording's level (recommended across varied mics). Otherwise tune the fixed default: **lower** toward `-35` if speech gets clipped, **raise** toward `-20` if pauses are left uncut. `--silence-min-dur` (default 0.3s) is the shortest silence considered.

### 3. Review repeated narration

Read `transcript.json` yourself. Do not ask the user to mark obvious repeats.

Cut only high-confidence issues:

- unfinished false starts
- immediate self-corrections
- duplicate closings
- same sentence repeated with a clearly cleaner take
- repeated explanation that adds no information and does not carry a needed screen action

Keep low-confidence material:

- later segment adds context, caveat, result, or troubleshooting detail
- words are similar but screen state changes
- repeated narration contains the actual click, command, file change, generated result, or UI transition

If visual evidence matters, inspect targeted frames around the candidate range:

```bash
mkdir -p /tmp/repeat_frames
ffmpeg -i "/path/to/video_or_export.mp4" -ss 42 -t 12 -vf "fps=1" /tmp/repeat_frames/frame_%04d.jpg -y
```

Write repeat cuts to `/tmp/cuts.json`:

```json
[
  {
    "start_ms": 123000,
    "end_ms": 131500,
    "removed_text": "repeated or abandoned phrase",
    "reason": "false_start",
    "confidence": "high",
    "kept_text": "cleaner take"
  }
]
```

Apply them:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.json" \
  --cuts-file "/tmp/cuts.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --language zh
```

### 4. Verify

After processing, check:

- no suspicious short wordless slices remain
- no obviously bad session-boundary silence remains
- the reported duration and time saved look reasonable

Tell the user what changed, then ask them to preview the edited project in Screen Studio. The script has already applied the 4:3 layout, 2% padding, 25 rounded corners, 30% top-right square camera, and mic audio cleanup — ask the user to verify these look right (not to set them by hand) before exporting.

Do not continue to subtitle burning until the user provides the exported `.mp4`.

## Mode B: Burn Subtitles Into `.mp4`

### 1. Transcribe if needed

For a standalone video:

```bash
ffmpeg -i "/path/to/video.mp4" -ar 16000 -ac 1 /tmp/audio_for_transcribe.wav -y
"$PYTHON" "$SKILL_DIR/scripts/local_transcribe.py" \
  --audio /tmp/audio_for_transcribe.wav \
  --output /tmp/transcript.json \
  --language zh
```

The first transcription downloads the Whisper model (~1.5 GB) and may take a while; later runs reuse the local cache.

For an exported Screen Studio video, reuse the project `transcript.json` when it matches the edited timeline. If timing looks suspicious, transcribe the exported video directly.

### 2. Correct transcript text

Read the transcript before previewing. Apply high-confidence corrections directly:

- obvious proper nouns and product names
- clear ASR mistakes from context
- wrong capitalization such as `github` -> `GitHub`
- recurring misrecognitions from `glossary.json` if present

For uncertain product names, commands, filenames, or visible UI text, extract targeted frames and verify before changing.

Only edit the segment `"text"` fields. Ignore word-level tokens unless debugging timing.

### 3. Launch preview editor

The user must preview synced subtitles before burning. `preview_editor.py` runs a Flask server that **blocks**, so start it in the background and keep going:

```bash
lsof -ti :8765 | xargs kill -9 2>/dev/null; sleep 1
"$PYTHON" "$SKILL_DIR/scripts/preview_editor.py" \
  "/path/to/exported.mp4" \
  "/path/to/transcript.json" &
```

Open or provide `http://localhost:8765`. The page targets the Oil/ego-browser interactive bridge: when the user clicks 「保存并关闭」 it writes the edited `transcript.json` and signals the Agent. In a plain browser that signal may not arrive — in that case watch `transcript.json`'s modification time (it is rewritten on save) to know when the user is done.

Tell the user:

> 已打开字幕预览编辑器，请检查字幕是否准确。可以双击编辑文字、勾选删除不需要的条目。确认无误后点击「保存并关闭」，我再继续烧录。

Wait until the user confirms or the preview editor saves (`transcript.json` is rewritten).

### 4. Update glossary if useful

After the user saves, compare `transcript.json.orig.json` with `transcript.json`.

Add only recurring ASR mistakes to `glossary.json`. Do not add one-off content edits, deletions, or punctuation tweaks.

### 5. Draft and burn

Generate an SRT draft first:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "/path/to/exported.mp4" \
  --transcript "/path/to/transcript.json" \
  --draft-output "/path/to/exported_subtitled.srt" \
  --draft-only
```

Read the draft. Check for:

- obvious bad line breaks
- zero-length or overlapping events
- stranded particles
- product names split badly
- subtitle lines ending with display punctuation

Then burn:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "/path/to/exported.mp4" \
  --transcript "/path/to/transcript.json"
```

If the user reviewed or edited the SRT draft directly, burn that reviewed file:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "/path/to/exported.mp4" \
  --srt-input "/path/to/exported_subtitled.srt"
```

The output is saved next to the source video as `<video>_subtitled.mp4`.

## Mode C: Merge Projects

Ask for:

- base `.screenstudio`
- supplement `.screenstudio`
- append at end or insert after a specific slice

Append by default:

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio"
```

For a custom output:

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio" \
  --output "/path/to/Merged.screenstudio"
```

For insertion:

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio" \
  --insert-after-slice 5
```

The merged project is written to `<Base>_Merged.screenstudio` by default. If the output already exists the script aborts (it never prompts interactively) — pass `--force` to overwrite.

After merging, tell the user to open the merged project in Screen Studio, arrange slices if needed, then continue with Mode A.

## Reporting

Keep reports short and practical:

- pauses removed
- repeats removed
- silent slices removed
- original duration
- new duration
- time saved
- any manual decisions you made
- what the user should preview next

If something looks wrong, diagnose with timestamps and explain the actual source of the issue before changing the project.
