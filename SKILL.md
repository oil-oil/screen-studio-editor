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
- **Burn subtitles into an `.mp4`**: transcribe with Bailian ASR, review, preview with the user, then burn.
- **Merge two `.screenstudio` projects**: combine base and supplement recordings.

The scripts handle mechanical timeline details. Do not repeat their internal logic in your response. Focus on the decisions the Agent must make: what to run, what to inspect, what to cut, what to ask the user to preview, and when to wait.

## Visual Defaults

- **`process.py` applies these to the project automatically — you do not set them by hand:** 4:3 output aspect, 2% background padding, 25 window corner radius, and the camera at 30% size, square aspect, pinned top-right. It also runs microphone audio cleanup (noise reduction + volume normalization). It does **not** enable Screen Studio's native captions — subtitles are burned separately in Mode B.
- Burned subtitles use `PingFang SC`, white text, no text outline, no drop shadow, and a slightly dark translucent rounded background.
- Subtitle display text should not include punctuation marks. ASR punctuation may still guide splitting internally, but previewed and burned subtitles should omit visible commas, periods, question marks, exclamation marks, and similar marks.
- Subtitles are centered near the bottom: roughly a 6% bottom margin for landscape/4:3 video and 20% for portrait. There is no special "safe-area" logic beyond that margin.

## Setup

At the start of each session:

```bash
SKILL_DIR="/Users/linzhihuang/.claude/skills/screen-studio-editor"
PYTHON="$SKILL_DIR/.venv/bin/python3"
BAILIAN_TRANSCRIBE="$SKILL_DIR/scripts/bailian_transcribe.py"
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
- `--asr-backend bailian`
- `--language zh` for Chinese/Mandarin content

### 2. Analyze first, then run the editor

Start with a dry run. It performs the complete ASR/audio/activity/candidate analysis, writes an audit report, and does not modify `project.json` or create a backup:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh \
  --dry-run \
  --report-output "/tmp/screenstudio-autoedit-report.json"
```

Read the audit report yourself. Check every protected interval, every reviewed cut, all removals over 5 seconds, and whether the projected time saved is plausible. A first dry run caches its source-time editing transcript beside the report (the exact path is in `edit_transcript_cache`), so reuse it and avoid paying for ASR twice. Then run without `--dry-run`:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/tmp/screenstudio-autoedit-report.transcript.edit.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

If `transcript.edit.json` already exists and you want to reuse the existing editing transcription:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

**Notes on `process.py`:**

- Every normal run writes `autoedit-report.json` beside the project. Use it to diagnose a missed/protected cut before changing thresholds.
- On the first run it backs up `project.json` to `project.json.bak` and re-applies edits from that backup, so runs are idempotent — re-run with `--cuts-file` to add repeat cuts without stacking them on already-cut slices.
- If `project.json` was changed externally since the last run (edited **or just re-saved** in Screen Studio), the script protects those changes: a re-run with `--cuts-file` applies the new cuts **incrementally to the current timeline** (external edits preserved, no ASR needed); a full re-run refuses and requires `--discard-external-edits` to intentionally start over from the backup. Do not pass `--discard-external-edits` without telling the user their Screen Studio adjustments will be lost.
- `process.py` uses Bailian FunAudio ASR by default and saves three artifacts: untouched provider output in `bailian_asr.json`, a **source-time editing transcript** in `transcript.edit.json`, and the compatibility copy `transcript.json`. Editing transcripts preserve standalone fillers, word timestamps, punctuation, and raw ASR sentence boundaries — subtitle cleanup must never run before edit-candidate review. The old local Whisper path is only for explicit comparison or emergency fallback via `--asr-backend local`; do not use it silently. If Bailian fails twice, the script continues with audio-only editing and says so.
- Pause candidates combine **per-session adaptive energy silence** with local **Silero VAD**. VAD catches non-speech gaps that contain fan noise or keyboard sounds; ASR word protection prevents recognized speech from being cut. Screen activity protection is also on by default: click/keystroke files and a low-resolution display-change scan keep silent tutorial actions. Do not pass `--no-vad`, `--no-visual-scan`, or `--no-screen-activity-protection` unless diagnosing a specific failure.
- The silence threshold defaults to `auto`, estimated separately for every recording session from short-window noise/speech percentiles. Only pass a fixed `--silence-db` when auto misbehaves: lower toward `-35` if speech gets clipped, or raise toward `-20` if pauses remain. `--silence-min-dur` (default 0.3s) is the shortest audio-inactivity region considered.
- Multi-session recordings (pausing/resuming while recording) are handled: each session's audio runs slightly longer than its slot in the slice timeline, and the script re-anchors ASR and silence timestamps per session. `transcript.json` is saved in **slice-timeline coordinates**, so `start`/`end` values from it can be copied into `cuts.json` as `start_ms`/`end_ms` (×1000) directly.
- A pause cut never removes anything ASR recognized as a word. Reviewed filler/repeat cuts use nearby low-energy waveform points for their final splice boundaries; do not add fixed inward padding to ASR timestamps.
- New cuts files are schema-v2 objects declaring `coordinate_space` (`source` or `edited`) and a `project_sha256`. `process.py` maps edited/export-time cuts through the exact current `slices` map and refuses mismatched project fingerprints. Legacy list-only cuts are accepted as source time with a warning.

### 3. Review repeated narration

Read `transcript.edit.json` yourself. Do not ask the user to mark obvious repeats.

Optional Gemini-assisted review:

- Use Gemini to review edit candidates, not to edit the `.screenstudio` project directly.
- Prefer this when the video has filler words, false starts, or repeated takes that silence detection cannot remove.
- Candidate search scans the complete timeline, including multi-sentence repeats up to 60 seconds apart; it then balances candidates across the recording and reviews them in batches. Do not restore a chronological “first N” cap.
- If `GEMINI_API_KEY` is available and `--video` is provided, the reviewer sends short clips with audio to the native Gemini API. Otherwise it falls back to ZenMux plus still frames. Model output remains advisory: timeline validation, input/display protection, and waveform boundary refinement run locally.

For a source-time editing transcript, review without an exported video (or pass a source-time proxy video if one exists):

```bash
"$PYTHON" "$SKILL_DIR/scripts/gemini_edit_candidates.py" \
  --transcript "/path/to/Project.screenstudio/transcript.edit.json" \
  --coordinate-space source \
  --project-json "/path/to/Project.screenstudio/project.json" \
  --output "/tmp/gemini_edit_report.json" \
  --cuts-output "/tmp/gemini_cuts.json"
```

For an **exported edited video**, transcribe that export in raw editing mode and explicitly mark the result as edited time. Never label exported timestamps as source time:

```bash
"$PYTHON" "$BAILIAN_TRANSCRIBE" \
  "/path/to/exported_edited.mp4" \
  --output "/tmp/exported.edit.json" \
  --language zh \
  --keep-fillers \
  --no-glossary \
  --split-mode raw

"$PYTHON" "$SKILL_DIR/scripts/gemini_edit_candidates.py" \
  --transcript "/tmp/exported.edit.json" \
  --video "/path/to/exported_edited.mp4" \
  --coordinate-space edited \
  --project-json "/path/to/Project.screenstudio/project.json" \
  --output "/tmp/gemini_edit_report.json" \
  --cuts-output "/tmp/gemini_cuts.json"
```

Apply high-confidence Gemini cuts through the existing timeline editor:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --cuts-file "/tmp/gemini_cuts.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

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

Write manual repeat cuts as a schema-v2 `/tmp/cuts.json` document. Use `source` only for timestamps copied from `transcript.edit.json`; edited/export timestamps require the matching current-project SHA and should normally be produced by `gemini_edit_candidates.py`:

```json
{
  "schema_version": 2,
  "coordinate_space": "source",
  "project_sha256": null,
  "cuts": [
    {
      "start_ms": 123000,
      "end_ms": 131500,
      "removed_text": "repeated or abandoned phrase",
      "reason": "false_start",
      "confidence": "high",
      "kept_text": "cleaner take"
    }
  ]
}
```

Apply them:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --cuts-file "/tmp/cuts.json" \
  --pause-threshold 800 \
  --min-pause 300 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

### 4. Verify

After processing, check:

- no suspicious short wordless slices remain
- no obviously bad session-boundary silence remains
- the reported duration and time saved look reasonable
- review every `⏱️ long removal` line the script printed: a >5s cut is usually dead air, but confirm from the surrounding transcript (and targeted frames if needed) that it does not hide silent on-screen action; the text shown around long cuts also often reveals an abandoned take that still needs a repeat cut

Tell the user what changed, then ask them to preview the edited project in Screen Studio. The script has already applied the 4:3 layout, 2% padding, 25 rounded corners, 30% top-right square camera, and mic audio cleanup — ask the user to verify these look right (not to set them by hand) before exporting.

Do not continue to subtitle burning until the user provides the exported `.mp4`.

## Mode B: Burn Subtitles Into `.mp4`

### 1. Transcribe with Bailian ASR

For a standalone video or exported Screen Studio video, use Bailian FunAudio ASR by default:

```bash
"$PYTHON" "$BAILIAN_TRANSCRIBE" \
  "/path/to/exported.mp4" \
  --output "/tmp/transcript.json" \
  --language zh \
  --raw-output "/tmp/bailian_asr.json"
```

This only replaces the recognition step. The output `transcript.json` keeps the same shape as `local_transcribe.py`: `start`, `end`, `text`, and optional `words`.
The Bailian transcript is already cleaned of standalone fillers and split into short subtitle-ready segments. Do not pass raw long ASR sentences into the preview editor. If you need to inspect the untouched ASR text, read `bailian_asr.json`; if you intentionally want fillers in the transcript, pass `--keep-fillers`.

Accuracy and segmentation are layered — all on by default, each with an opt-out:

- **Hot words** (`hotwords.json` → Bailian vocabulary, `--no-hotwords` to disable): steers recognition toward the channel's recurring proper nouns. Add a term when ASR keeps mishearing it; do NOT add well-known words ASR already gets right (a hot word can hijack similar-sounding speech — 飞书 as a hot word turned "Fable" into 飞书). The vocabulary is cached in `.vocabulary-cache.json` and auto-updates when `hotwords.json` changes.
- **Glossary auto-apply** (`glossary.json`, `--no-glossary` to disable): recurring text corrections applied to segment text right after ASR, so the preview shows corrected subtitles. Matching is case-insensitive and whitespace-tolerant. The same replacements run again at burn time (idempotent).
- **LLM line splitting** (`--split-mode llm` default, `rules` to disable; `--split-model` to override): over-long ASR sentences are split into subtitle lines by Qwen, sentence by sentence in parallel — the LLM only chooses break points; character content is validated and any failed sentence falls back to the rule splitter. Expect a handful of "LLM split modified the text" warnings on stuttery sentences; that is the validation working, not an error.
- `--split-mode raw` is for editing analysis only. It preserves punctuation, fillers, and ASR sentence boundaries and must not be sent directly to the subtitle preview/burn workflow.

If the user explicitly asks to compare with the old local model or Bailian is temporarily unavailable, the existing local transcription path is still available:

```bash
ffmpeg -i "/path/to/exported.mp4" -ar 16000 -ac 1 /tmp/audio_for_transcribe.wav -y
"$PYTHON" "$SKILL_DIR/scripts/local_transcribe.py" \
  --audio /tmp/audio_for_transcribe.wav \
  --output /tmp/transcript.json \
  --language zh
```

For an exported Screen Studio video, reuse the project `transcript.json` only when it matches the edited timeline. If timing looks suspicious, transcribe the exported video directly.

### 2. Correct transcript text

Read the transcript before previewing. Apply high-confidence corrections directly:

- obvious proper nouns and product names
- clear ASR mistakes from context
- wrong capitalization such as `github` -> `GitHub`
- recurring misrecognitions from `glossary.json` if present

For uncertain product names, commands, filenames, or visible UI text, extract targeted frames and verify before changing.

Only edit the segment `"text"` fields. Ignore word-level tokens unless debugging timing.
The previewed segment `"text"` is the source of truth for burned display text. Word-level tokens may guide timing, but they must not overwrite casing, product-name corrections, spacing, or other user-confirmed text edits.

### 3. Launch preview editor

The user must preview synced subtitles before burning. `preview_editor.py` runs a Flask server that **blocks**, so start it in the background and keep going:

```bash
lsof -ti :8765 | xargs kill -9 2>/dev/null; sleep 1
"$PYTHON" "$SKILL_DIR/scripts/preview_editor.py" \
  "/path/to/exported.mp4" \
  "/tmp/transcript.json" &
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
  --transcript "/tmp/transcript.json" \
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
  --transcript "/tmp/transcript.json"
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
