---
name: screen-studio-editor
description: >
  Edit Screen Studio recordings and burn accurate AI-corrected subtitles onto any video.
  Use this skill for ANY of these situations — even if the user doesn't mention Screen Studio by name:

  - User has a .screenstudio file and wants to clean it up: remove pauses, cut repeated narration,
    add subtitles. Trigger on: "帮我剪掉停顿", "删重复的内容", "处理这个录屏", "自动剪视频",
    "edit my recording", "cut the pauses", mentions of .screenstudio path.
  - User has any .mp4 and wants subtitles burned in — from a screen recording, iPhone video,
    tutorial, demo, or anything else. Trigger on: "帮这个视频加字幕", "给视频加字幕",
    "add subtitles", "burn subtitles", "加个字幕", or when user provides a .mp4 path and
    mentions captions/subtitles in any form.
  - User wants to merge two .screenstudio projects (e.g. supplementary re-recording, split sessions).
    Trigger on: "合并工程", "合并两个录屏", "补录合并", "把两个 screenstudio 合并",
    "merge projects", "combine recordings", "insert recording into another".

  Do not wait for the user to say "Screen Studio" — subtitle burning works for any video.
---

# Screen Studio Auto-Editor

**Mode A** — Full editing of a `.screenstudio` project: remove pauses, cut repeated narration, burn subtitles
**Mode B** — Standalone: burn accurate subtitles onto any .mp4
**Mode C** — Merge two `.screenstudio` projects (for supplementary re-recordings)

---

## Prerequisites

**Platform**: macOS on Apple Silicon (M1/M2/M3) only — mlx-whisper does not run on Intel Macs or Linux.

**First-time setup** (run once after installing the skill):
```bash
bash <skill-directory>/setup.sh
```
This installs ffmpeg (via Homebrew if missing), creates a Python venv, installs mlx-whisper, and pre-downloads the Whisper large-v3 model (~3 GB). Takes ~5 minutes on first run.

**`SKILL_DIR`**: Throughout this skill, `SKILL_DIR` refers to the skill's own directory. Claude Code injects this as "Base directory" in the skill header — use that value. Never hardcode a user-specific path.

```bash
# At the start of every session, set this variable from the injected base directory:
SKILL_DIR="<base directory shown in skill header>"
PYTHON="$SKILL_DIR/.venv/bin/python3"
```

---

## Mode A — Full .screenstudio workflow

A `.screenstudio` bundle is a directory with `project.json` (the editing timeline as `scenes[].slices`) and `recording/` (raw audio/video per session). We modify the slices to remove silences and repeated narration, then burn subtitles onto the exported video.

**Critical: `sourceStartMs`/`sourceEndMs` coordinate system** — slice timestamps are in the *merged audio timeline* (all recording sessions concatenated back-to-back, gaps between sessions excluded). This is identical to the timestamps Whisper produces. Do NOT add `processTimeStartMs` offsets. The original single-slice project always has `sourceEndMs ≈ sum of all session durations` — this is the proof.

**Critical: slice IDs must be unique** — when splitting one slice into many, each new slice needs a fresh random ID or Screen Studio collapses duplicates and ignores all cuts. `process.py` handles this automatically.

### Step 1: Validate inputs

Confirm the `.screenstudio` path exists and contains a `recording/` folder. Ask the user to confirm settings if not already provided.

Defaults:
- `pause_threshold_ms`: 800 — pauses longer than this feel awkward on screen
- `min_pause_to_keep_ms`: 300 — leave a small gap so cuts don't sound abrupt

### Step 2: Transcribe and remove pauses

```bash
# SKILL_DIR and PYTHON are defined in the Prerequisites section above
$PYTHON $SKILL_DIR/scripts/process.py \
  --project "/path/to/Project.screenstudio" \
  --pause-threshold 800 \
  --min-pause 300 \
  --language zh        # use 'en' for English recordings, 'None' to auto-detect
```

This backs up `project.json`, transcribes microphone audio with mlx-whisper large-v3 (local, ~10-30s), removes pause gaps from the timeline, enables `improveMicrophoneAudio` (noise reduction + normalization), and saves `transcript.json` in the project folder.

**After running, check for noise fragments**: Whisper sometimes transcribes ambient noise during long silent waits (e.g. waiting for a build or page load) as single CJK characters like "坐". These become tiny spurious slices in the timeline — too short to contain real speech. Inspect `project.json`'s slices and remove any that are under ~600ms or fall entirely within a known silent wait zone. These slices are harmless but create micro-gaps in the exported video that look like glitches.

### Step 3: Remove repeated content

Read `transcript.json`. Look for:
- **False starts**: speaker begins a sentence, stops mid-way, then restarts it more cleanly
- **Repeated explanations**: the same idea said multiple times — keep the clearest version
- **Duplicate closings**: same sign-off said twice

For each repeat, note its timestamps (audio seconds from the Whisper transcript map 1:1 to `sourceStartMs` in slices — they use the same merged-audio coordinate). Write to `/tmp/cuts.json`:

```json
[{"start_ms": 123000, "end_ms": 131500, "removed_text": "the repeated text"}]
```

Then run Phase 2 to apply the cuts:

```bash
$PYTHON $SKILL_DIR/scripts/process.py \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.json" \
  --cuts-file "/tmp/cuts.json"
```

**Note**: `process.py` always loads from `project.json.bak` (created on first run, never overwritten). Re-running is safe and idempotent — pauses + cuts are always applied to the original state.

**Session boundary snap**: `process.py` snaps slice endpoints to 10ms *before* the session boundary (not exactly at it). Snapping to the exact boundary triggers a Screen Studio audio-composer bug: metadata `durationMs` differs from the actual WAV length by ~0.3µs, causing a sub-millisecond audio segment that the composer rejects. The 10ms margin keeps the snap point safely inside session N's audio while still producing gap=0 for spring transition animation.

### Step 4: Report and wait for export

Tell the user: how many pauses were removed, total time saved, which repeated segments were cut. Ask them to open Screen Studio, preview, and export the video.

### Step 5: Burn subtitles

Once the user provides the exported video path, follow the **Subtitle workflow** below.

---

## Mode B — Standalone subtitle burning

When the user has a video but no `.screenstudio` project, transcribe first, then follow the same subtitle workflow.

```bash
# SKILL_DIR and PYTHON are defined in the Prerequisites section above

# Extract audio
ffmpeg -i "/path/to/video.mp4" -ar 16000 -ac 1 /tmp/audio_for_transcribe.wav -y

# Check for AAC timestamp drift BEFORE transcribing
# If WAV duration > video duration by more than 0.5%, apply piecewise correction after transcription
python3 -c "
import subprocess, json
wav = float(subprocess.check_output(['ffprobe','-v','quiet','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1','/tmp/audio_for_transcribe.wav']).strip())
vid = float(subprocess.check_output(['ffprobe','-v','quiet','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1','/path/to/video.mp4']).strip())
drift_pct = (wav - vid) / vid * 100
print(f'WAV: {wav:.3f}s  Video: {vid:.3f}s  Drift: {drift_pct:+.2f}%')
if abs(drift_pct) > 0.5:
    print('⚠️  Significant AAC drift detected — timestamps will need correction after transcription')
"

# Transcribe
# Use language='zh' for Mandarin/Chinese content to prevent Traditional Chinese output.
# Use language=None for English or mixed/unknown language content.
$PYTHON -c "
import mlx_whisper, json
result = mlx_whisper.transcribe('/tmp/audio_for_transcribe.wav',
    path_or_hf_repo='mlx-community/whisper-large-v3-mlx',
    word_timestamps=True, language='zh')  # change to None for non-Chinese content
with open('/tmp/transcript.json', 'w') as f:
    json.dump(result['segments'], f, ensure_ascii=False, indent=2)
print(f'Transcribed {len(result[\"segments\"])} segments')
"
```

Then follow the **Subtitle workflow** below with `/tmp/transcript.json` as the transcript path.

---

## Subtitle workflow

### 0. Apply glossary before reviewing

`SKILL_DIR/glossary.json` is user-specific and gitignored — it won't exist on a fresh install. If it doesn't exist, skip this step. If it does, apply all entries as automated corrections before manual review.

The glossary is case-insensitive. Format: `[{"wrong": "...", "correct": "..."}]`

```python
import json, re
from pathlib import Path

glossary_path = Path(SKILL_DIR) / "glossary.json"
if glossary_path.exists():
    glossary = json.loads(glossary_path.read_text())
    for seg in segments:
        for entry in glossary:
            seg["text"] = re.sub(re.escape(entry["wrong"]), entry["correct"], seg["text"], flags=re.IGNORECASE)
```

### 1. Semantic review — correct transcript.json

Read the full transcript text first. Look for words that are **semantically inconsistent with the surrounding context** — Whisper mishears phonetically similar words. For every suspicious word, classify it:

**High-confidence corrections** (fix immediately, no frames needed):
- Clear phonetic substitutions with obvious right answer given context (`Scream Studio` → `Screen Studio`, `cloud call` → `Claude Code`, `Nordic.js` → `Node.js`)
- Wrong capitalization of well-known proper nouns (`minimax` → `MiniMax`, `github` → `GitHub`)
- Nonsense words where the correct word is unambiguous from context

**Low-confidence items** (flag for visual verification):
- English proper nouns you don't recognize — a product name, tool, or brand that appeared on screen but you can't confidently spell or capitalize
- Commands or filenames that seem partially garbled
- Version numbers or org names (e.g. `oyo-oyo/something` — is this `oil-oil`? `oiloil`?)

Apply high-confidence fixes immediately. Then decide:

- **No low-confidence items** → skip frame extraction entirely, proceed to preview editor
- **Low-confidence items exist** → extract targeted frames around those timestamps only:

```bash
# Extract a few frames near the uncertain segment (e.g. segment at ~42s)
mkdir -p /tmp/frames
ffmpeg -i "/path/to/video.mp4" -ss 30 -t 30 -vf "fps=1/5" /tmp/frames/frame_%04d.jpg -y
```

Use the `Read` tool to view those frames, identify the correct term, then apply the fix.

**Full-video frame extraction** (every 30s) is only warranted for dense screen-recording demos where many unknown technical terms appear throughout. For talking-head or pure voiceover videos, it wastes time and adds no value — skip it.

Edit the `"text"` fields in `transcript.json` using a Python find-replace script. The subtitle script reads `text` fields directly, so corrections apply immediately — no need to touch word-level tokens or the .ass file.

### 3. Preview and edit subtitles

Before burning, **always** launch the preview editor so the user can review subtitles synced with video playback. This is the mandatory quality gate — never skip straight to burning.

```bash
# Kill any previous instance on port 8765
lsof -ti :8765 | xargs kill -9 2>/dev/null; sleep 1

$PYTHON $SKILL_DIR/scripts/preview_editor.py \
  "/path/to/exported.mp4" \
  "/path/to/transcript.json"
```

Run this in the background (`run_in_background=true`, do NOT append `&`). The script opens `http://localhost:8765` in the browser automatically.

**What the user sees**: left side = video player with live subtitle overlay; right side = editable subtitle list. Features:
- **Click a subtitle** → video seeks to that timestamp
- **Double-click text** → inline edit mode (Enter to save, Esc to cancel)
- **Checkbox** → mark subtitles for deletion
- **Ctrl/Cmd+F** → find & replace across all subtitles
- **"保存并关闭"** → saves edits back to `transcript.json` and closes

Tell the user: "已在浏览器中打开字幕预览编辑器，请检查字幕是否准确。可以双击编辑文字、勾选删除不需要的条目。确认无误后点击「保存并关闭」，然后告诉我继续烧录。"

**Wait for the user to confirm** before proceeding to burn. Do not burn until they say the subtitles look good.

**Note on save format**: The preview editor saves transcript.json wrapped as `{"segments": [...]}`. The burn script handles both this format and plain arrays, so no conversion is needed.

### 3.5 Update glossary after user saves

After the user confirms the subtitles look good, diff the original snapshot against the saved transcript to find what the user changed. The preview editor saves a `.orig.json` backup automatically at startup.

```python
import json, difflib

with open("/path/to/transcript.json.orig.json") as f:
    orig = json.load(f)
with open("/path/to/transcript.json") as f:
    edited = json.load(f)

orig_segs  = orig.get("segments", orig)   if isinstance(orig, dict)   else orig
edit_segs  = edited.get("segments", edited) if isinstance(edited, dict) else edited

orig_by_start = {s["start"]: s["text"].strip() for s in orig_segs}
for s in edit_segs:
    orig_text = orig_by_start.get(s["start"], "")
    if orig_text and orig_text != s["text"].strip():
        print(f'CHANGED [{s["start"]:.1f}s]')
        print(f'  before: {orig_text}')
        print(f'  after:  {s["text"].strip()}')
```

Read the diff output and decide **which changes represent systematic Whisper misrecognitions** (i.e., patterns that will recur in future videos). Add those to `SKILL_DIR/glossary.json`. Do NOT add:
- one-time content fixes (wrong date, specific name that won't repeat)
- deletions (the user removed a subtitle entirely)
- minor punctuation or style tweaks

Glossary format: `{"wrong": "whisper_output", "correct": "true_term"}` — case-insensitive, one canonical entry per pattern.

### 4. Burn subtitles

```bash
$PYTHON $SKILL_DIR/scripts/burn_subtitles.py \
  --video "/path/to/exported.mp4" \
  --transcript "/path/to/transcript.json"
```

Output is saved as `<video>_subtitled.mp4` next to the source. The script prints a real-time progress bar during encoding. Subtitle style: semi-transparent dark background box with white text.

**Portrait video (e.g. iPhone)**: handled automatically. ffmpeg's autorotate corrects the orientation and clears the rotation metadata in the output, so no manual flags are needed. The script detects display dimensions correctly from the stored rotation metadata.

**AAC timestamp drift (VFR / mobile recordings)**: Videos from iPhone or Screen Studio exports often have an AAC audio stream whose raw sample count doesn't match the container-declared duration. Example: container says 417.56s but AAC has `19852 frames × 1024 samples ÷ 48000 Hz = 423.51s`. ffmpeg decodes all AAC frames faithfully, so the extracted WAV is 423.51s. Whisper timestamps follow the WAV — causing subtitles to drift ahead of the video as playback progresses.

Attempts to clamp the WAV with `-t <duration>` or `-af atrim=end=<duration>` **do not work** — both operate on container PTS timestamps (which already go 0→417.56s), so all samples pass through unchanged.

**Diagnosis**: Ask the user to identify 2–3 sync points — e.g. "beginning is fine, middle (~2 min) drifts by 3s, end is fine". This reveals whether the drift is linear or concentrated in a region.

**Fix — piecewise linear scaling**: If the user says drift starts at time `B` (e.g. 108s) and before that everything is in sync:

```python
import json

BOUNDARY  = 0.0    # ← video time (seconds) where drift begins; 0 if drift starts from the start
WAV_DUR   = 0.0    # ← ffprobe -show_entries format=duration /tmp/audio_for_transcribe.wav
VIDEO_DUR = 0.0    # ← ffprobe -show_entries format=duration /path/to/video.mp4

ratio2 = (VIDEO_DUR - BOUNDARY) / (WAV_DUR - BOUNDARY)

with open('/tmp/transcript.json') as f:
    segs = json.load(f)

for s in segs:
    for key in ('start', 'end'):
        t = s[key]
        if t > BOUNDARY:
            s[key] = round(BOUNDARY + (t - BOUNDARY) * ratio2, 3)

with open('/tmp/transcript_fixed.json', 'w') as f:
    json.dump(segs, f, ensure_ascii=False, indent=2)
```

Then burn subtitles using `transcript_fixed.json`. Verify: last segment end should be within ~1s of `VIDEO_DUR`.

If drift is purely linear (starts from t=0), set `BOUNDARY = 0` and the formula reduces to uniform scaling: `ratio = VIDEO_DUR / WAV_DUR`.

---

## Mode C — Merge Projects

Screen Studio does not support importing external videos. If you recorded supplementary content in a separate `.screenstudio` session (补录), use this mode to merge the two projects before editing.

**How it works**: The merge script copies recording files from both projects into a new output project, offsets the supplement's slice timestamps by the base project's total audio duration, and combines the slices. The result opens directly in Screen Studio for preview and rearrangement.

### Step 1: Ask for the two project paths

Confirm:
- **Base project**: the original recording (e.g. `Tutorial_Part1.screenstudio`)
- **Supplement project**: the re-recorded / additional content (e.g. `Tutorial_Supplement.screenstudio`)
- **Merge mode**: append supplement at end (default), or insert after a specific slice

### Step 2: Run the merge

```bash
# SKILL_DIR and PYTHON are defined in the Prerequisites section above

# Default: append supplement at end, output named {base}_Merged.screenstudio
$PYTHON $SKILL_DIR/scripts/merge_projects.py \
  --base "/path/to/ProjectA.screenstudio" \
  --supplement "/path/to/ProjectB.screenstudio"

# Custom output path
$PYTHON $SKILL_DIR/scripts/merge_projects.py \
  --base "/path/to/ProjectA.screenstudio" \
  --supplement "/path/to/ProjectB.screenstudio" \
  --output "/path/to/ProjectMerged.screenstudio"

# Insert supplement after slice index N (0-based)
# Use when you know approximately where in the timeline the supplement belongs
$PYTHON $SKILL_DIR/scripts/merge_projects.py \
  --base "/path/to/ProjectA.screenstudio" \
  --supplement "/path/to/ProjectB.screenstudio" \
  --insert-after-slice 5
```

**To find the right slice index**: Read the base project's `project.json` and count the `scenes[0].slices` array. Each slice is a continuous segment. The slice index corresponds to the position in the timeline where you want the supplement to appear.

### Step 3: Open in Screen Studio and arrange

Open the merged project in Screen Studio. The supplement content appears as additional clips at the end (or at the insertion point). **Drag and reorder slices** in the Screen Studio timeline as needed, then export.

### Step 4: Continue with Mode A editing (optional)

After merging, you can run `process.py` on the merged project to remove pauses and repeated content across both recordings as if it were a single recording:

```bash
$PYTHON $SKILL_DIR/scripts/process.py \
  --project "/path/to/ProjectMerged.screenstudio" \
  --language zh
```

### Merge notes

- **Originals are NOT modified** — output is always a new directory
- If `--output` is omitted, the merged project is named `{base_stem}_Merged.screenstudio` in the same folder
- **File conflicts**: if both projects have a recording file with the same name (e.g. `microphone_session_0.m4a`), the supplement's file is automatically renamed (`microphone_session_0_s1.m4a`) — the metadata is updated accordingly
- **Slice ordering**: slices are combined in array order. After merging, open Screen Studio and drag slices to the correct position if needed
- **After merging you can still edit**: the merged project behaves exactly like any other `.screenstudio` project — you can remove pauses, cut repeats, and burn subtitles normally

---

## Notes

- If `project.json.bak` already exists, ask the user before overwriting
- Camera config: `process.py` sets `cameraAspectRatio = "square"` (宽高一致). Do NOT override `cameraRoundness` — Screen Studio's default corner roundness looks natural; forcing it to 0 creates an unnaturally sharp square that clashes with the rest of the UI.
- Subtitle line breaks follow Whisper segment boundaries (natural speech pauses), so each `text` field in transcript.json maps to one subtitle block. Long segments are split at punctuation.
- Whisper sometimes splits technical terms into sub-tokens in the `words` array (e.g. `BulkGen` → `["bug", "gem"]`), but since the script now uses `text` fields for display, only the `text` field needs to be corrected — ignore `words`.
- **Running the burn command in background**: `burn_subtitles.py` correctly waits for ffmpeg internally. When using the Bash tool's `run_in_background` parameter, do NOT add `&` to the shell command — `&` backgrounds the Python process itself, the shell exits immediately, and the "task completed" notification fires before ffmpeg finishes. The output MP4 will appear but have `moov atom not found` (unplayable). Use `run_in_background=true` on the tool call alone, without `&` in the command.
- **Screen Studio transition mechanism**: Screen Studio applies its spring-based transition animation (zoom/pan) between consecutive slices **only when `gap = 0ms`** (i.e., `slice[n].sourceEndMs == slice[n+1].sourceStartMs`). Slices with any gap > 0ms get a hard cut. Pause-removal cuts always create gaps (audio was removed), so transitions can never be applied to within-session cuts. However, `process.py` now calls `snap_to_session_boundaries()` after cutting, which restores gap=0 at inter-session boundaries (the natural seams Screen Studio originally created), giving smooth transitions at recording-session joins even after editing.
