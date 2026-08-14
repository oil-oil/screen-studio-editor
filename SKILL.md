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

- `process.py` preserves the project's visual layout by default. It applies
  layout and microphone cleanup only when `visual_defaults.enabled` is true in
  the user config or `--apply-visual-defaults` is passed. This keeps personal
  styling out of the public Skill.
- It does **not** enable Screen Studio's native captions — subtitles are burned
  separately in Mode B.
- Mode B 正常烧录时，先用 macOS Vision 对视频均匀抽帧，按“跨帧持续出现在同一位置”
  自动识别摄像头人脸区域，不假定摄像头在右上角。识别成功后，在同一次 FFmpeg
  编码中固定执行 10% 轻度磨皮和 10% 提亮，再渲染字幕；不向用户暴露强度参数。
  检测不到稳定人脸时自动跳过美颜，原画必须保持不变时使用 `--no-beauty`。
  Draft-only 和 ASS-only 不处理视频。
- Burned subtitles use `PingFang SC`, white text, no text outline, no drop
  shadow, and a clearly dark translucent rounded background. Keep the panel
  compact with narrow inner padding so it supports the text without becoming
  a large block.
- Mandarin videos use Chinese-only burned subtitles by default and keep the
  established single-language size and backing style. Add English only when
  the user explicitly asks for English or bilingual subtitles. In bilingual
  mode, Chinese is the larger upper line and English is the lower line at
  roughly 70% of the Chinese size.
- Videos strictly longer than three minutes also receive one readable
  translucent gray progress bar flush with the bottom edge. Use 2–4 broad
  chapters for 3–5 minute videos and 4–6 for longer videos rather than many
  small beats. Every chapter title stays visible in the center of its own
  time-proportional interval while the gray fill advances beneath all titles.
  Size the bar at about 2.1% of frame height with legible chapter labels; it
  should remain compact but must not disappear after normal video scaling.
  Videos at or below three minutes have no chapter bar.
- Subtitle display text should not include punctuation marks. ASR punctuation may still guide splitting internally, but previewed and burned subtitles should omit visible commas, periods, question marks, exclamation marks, and similar marks.
- Subtitles are centered close to the bottom: roughly a 4.5% bottom margin for
  landscape/4:3 video and 18% for portrait. When a chapter bar exists, preserve
  a small clear gap above it rather than pushing subtitles high into the frame.

## Setup

At the start of each session:

```bash
SKILL_DIR="<absolute directory containing this SKILL.md>"
PYTHON="$SKILL_DIR/.venv/bin/python3"
BAILIAN_TRANSCRIBE="$SKILL_DIR/scripts/bailian_transcribe.py"
SCREEN_STUDIO_EDITOR_CONFIG="${SCREEN_STUDIO_EDITOR_CONFIG:-$HOME/.config/screen-studio-editor/config.json}"
```

Never assume a username, home directory, Skill install root, project root, video
library, model, or creator-preference file. Read optional personal values from
`SCREEN_STUDIO_EDITOR_CONFIG`; this file stays outside the repository:

```json
{
  "projects_root": "/optional/path/to/screen-studio-projects",
  "video_library_root": "/optional/path/to/video-library",
  "creator_preferences": "/optional/path/to/creator-edit-preferences.json",
  "hotwords": "/optional/path/to/hotwords.json",
  "glossary": "/optional/path/to/glossary.json",
  "vocabulary_cache": "/optional/path/to/vocabulary-cache.json",
  "model": "google/gemini-3.5-flash",
  "smart_edit": {
    "pause_threshold_ms": 700,
    "min_pause_ms": 180
  },
  "subtitles": {
    "mode": "zh",
    "progress_min_duration_seconds": 180
  },
  "visual_defaults": {
    "enabled": false,
    "output_aspect": [4, 3],
    "background_padding_ratio": 1.02,
    "window_border_radius": 25,
    "camera_aspect_ratio": "square",
    "camera_size": 0.3,
    "camera_position": "top-right",
    "camera_position_point": {"x": 1, "y": 0},
    "improve_microphone_audio": true
  },
  "ppt": {
    "style_skill": "",
    "tone_skill": "",
    "illustration_brief": "",
    "cutout_script": ""
  }
}
```

Expand `~` after reading paths. Resolution order is command-line argument,
environment variable, user config, then a portable source-adjacent default.
Supported overrides include `SCREEN_STUDIO_EDITOR_PREFERENCES` and
`SCREEN_STUDIO_EDITOR_MODEL`, plus `SCREEN_STUDIO_EDITOR_HOTWORDS`,
`SCREEN_STUDIO_EDITOR_GLOSSARY`, and
`SCREEN_STUDIO_EDITOR_VOCABULARY_CACHE`. Subtitle mode resolves from an
explicit `--subtitle-mode`, then `SCREEN_STUDIO_EDITOR_SUBTITLE_MODE`, then
`subtitles.mode`, and finally defaults to `zh`. Supported values are `zh` and
`bilingual`; an explicit user request for English should pass
`--subtitle-mode bilingual` for that video. The progress threshold resolves
from `--min-progress-duration`, then
`SCREEN_STUDIO_EDITOR_PROGRESS_MIN_DURATION`, then
`subtitles.progress_min_duration_seconds`, and finally 180 seconds. The
comparison is strict, so a video at exactly 3:00 has no bar. Do not commit the
user config, API keys, personal absolute paths, creator-specific vocabulary,
or benchmark data.

Path policy:

- Keep `.screenstudio` projects, merged projects, clones, and project-side
  editing artifacts under `projects_root` when configured. Otherwise keep
  derived projects next to the source project.
- Keep each exported video and its MP4, SRT, ASS, transcript, ASR, cover, and
  publishing resources together under
  `<video_library_root>/<video-title>/` when configured. Otherwise keep them
  next to the source video.
- Store persistent subtitle work in `<video-stem>.subtitle-work/` beside the
  video. Use `/tmp` only for disposable frame extractions and caches.
- When a source is still in an export staging directory, relocate it without
  overwriting before creating persistent derivatives.

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

Set a persistent project-side work directory:

```bash
PROJECT="/path/to/Project.screenstudio"
PROJECT_WORK="$PROJECT/.screen-studio-editor"
mkdir -p "$PROJECT_WORK"
```

If the user did not specify settings, use:

- `--pause-threshold 700`
- `--min-pause 180`
- `--pause-source silence`
- `--asr-backend bailian`
- `--language zh` for Chinese/Mandarin content

### 2. Analyze first, then run the editor

Start with a dry run. It performs the complete ASR/audio/activity/candidate analysis, writes an audit report, and does not modify `project.json` or create a backup:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh \
  --dry-run \
  --report-output "$PROJECT_WORK/autoedit-report.json"
```

Read the audit report yourself. Check every protected interval, every reviewed cut, all removals over 5 seconds, and whether the projected time saved is plausible. A first dry run caches its source-time editing transcript beside the report (the exact path is in `edit_transcript_cache`), so reuse it and avoid paying for ASR twice.

For ordinary talking-head/screen-tutorial recordings, the recommended path is
the cached Gemini-only workflow. It performs the local dry run with the
configured conservative pause gate (portable default 700 ms), builds one
source-aligned A/V proxy, asks
`google/gemini-3.5-flash` for grounded whole-timeline candidates, and runs a
personalized second full-video pass to arbitrate every proposed deletion. It
does not use Screen Studio pause/resume session boundaries as cut evidence:
the complete aligned project, transcript, microphone audio, screen activity,
and creator examples are the evidence. The arbiter must describe the visible
action and classify it as meaningful or redundant; click/keystroke telemetry
constrains the allowed answer and remains the stricter final guard. The
workflow also adds a conservative local micro-edit pass and performs a second
dry run. The local pass removes only an acoustically isolated strong filler
lasting at least 400 ms or a short tail take repeated almost exactly;
ambiguous short fillers and approximate restarts are kept. It does not write
the timeline unless `--apply` is explicitly added:

This personalized quality path requires `creator_preferences`. If it is not
configured, build a preferences file with `preference_edit_arbiter.py build`
or use the direct pause-cleanup path below; do not assume another user's
benchmark file.

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio"
```

There is one editing path: quality mode. ASR, silence/VAD analysis, and
screen-activity scanning run concurrently inside the local analysis, while the
aligned-proxy encode stays sequential to avoid CPU contention. When a matching
source-time edit transcript already exists, a stale analysis cache reuses that
transcript instead of paying for ASR again. The final audit reuses the first
pass's fingerprinted silence/activity evidence. It preserves the complete
reviewed boundary for full-video-cleared screen pauses and replacementless
local delivery cleanups, while other speech edits still snap to nearby quiet
waveform points. Any project, transcript, setting, or editor-code mismatch
falls back to the necessary local rescan.

The review proxy samples each session down to 6 fps before scaling it to the
960×600 model input. An unchanged cached rerun should reuse the proxy rather
than encode it again.

Read `smart-edit-final-report.json`, inspect every smart cut and every removal
over five seconds, then apply the exact cached decisions only when the audit is
safe:

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --apply
```

The workflow reads the ZenMux key from `ZENMUX_API_KEY` or an external
user-home key file supported by the helper script; never put a key in the
repository or a report. It reuses
ASR, the aligned proxy, the whole-timeline planner response, and the
preference decision whenever their fingerprints still match. It also
fingerprints the project, transcript, cuts, and editor code before the final
audit, so an unchanged rerun does not repeat the expensive audio/timeline
validation. The local micro-edit pass does not call an API and normally
completes in under a second. A final safety
gate rejects short speech deletions when the model cannot point to a concrete
repeated or corrected structure; a difficult “maybe retake” stays in the
video. Keep creator-specific regression measurements in the configured
preferences or benchmark workspace, not in this public Skill. Never treat
recording session boundaries as deletions merely to improve recall.

For a recording that genuinely needs pause cleanup only, the direct apply path remains:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "$PROJECT_WORK/autoedit-report.transcript.edit.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

If `transcript.edit.json` already exists and you want to reuse the existing editing transcription:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

**Notes on `process.py`:**

- Every normal run writes `autoedit-report.json` beside the project. Use it to diagnose a missed/protected cut before changing thresholds.
- On the first run it backs up `project.json` to `project.json.bak` and re-applies edits from that backup, so runs are idempotent — re-run with `--cuts-file` to add repeat cuts without stacking them on already-cut slices.
- If `project.json` was changed externally since the last run (edited **or just re-saved** in Screen Studio), the script protects those changes: a re-run with `--cuts-file` applies the new cuts **incrementally to the current timeline** (external edits preserved, no ASR needed); a full re-run refuses and requires `--discard-external-edits` to intentionally start over from the backup. Do not pass `--discard-external-edits` without telling the user their Screen Studio adjustments will be lost.
- `process.py` uses Bailian FunAudio ASR by default and saves three artifacts: untouched provider output in `bailian_asr.json`, a **source-time editing transcript** in `transcript.edit.json`, and the compatibility copy `transcript.json`. Editing transcripts preserve standalone fillers, word timestamps, punctuation, and raw ASR sentence boundaries — subtitle cleanup must never run before edit-candidate review. The old local Whisper path is only for explicit comparison or emergency fallback via `--asr-backend local`; do not use it silently. If Bailian fails twice, the script continues with audio-only editing and says so.
- Pause candidates combine **per-session adaptive energy silence** with local **Silero VAD**. VAD catches non-speech gaps that contain fan noise or keyboard sounds; ASR word protection prevents recognized speech from being cut. Screen activity protection is also on by default: click/keystroke files and a low-resolution display-change scan keep silent tutorial actions. Omni-reviewed cuts may clear activity only when the model explicitly marks it redundant and supplies a visual assessment; a claimed `none` never overrides a real input event. Every override is recorded in `reviewed_cuts_activity_clearance_overrides`. Do not pass `--no-vad`, `--no-visual-scan`, or `--no-screen-activity-protection` unless diagnosing a specific failure.
- The silence threshold defaults to `auto`, estimated separately for every recording session from short-window noise/speech percentiles. Only pass a fixed `--silence-db` when auto misbehaves: lower toward `-35` if speech gets clipped, or raise toward `-20` if pauses remain. `--silence-min-dur` (default 0.3s) is the shortest audio-inactivity region considered.
- Multi-session recordings (pausing/resuming while recording) are handled: each session's audio runs slightly longer than its slot in the slice timeline, and the script re-anchors ASR and silence timestamps per session. `transcript.json` is saved in **slice-timeline coordinates**, so `start`/`end` values from it can be copied into `cuts.json` as `start_ms`/`end_ms` (×1000) directly.
- A deterministic pause cut never removes anything ASR recognized as a word. Reviewed filler/repeat cuts use nearby low-energy waveform points for their final splice boundaries, but refinement is clamped inside the reviewed range. A full-video-approved screen pause or replacementless local delivery cleanup preserves the complete reviewed range, including intentional adjacent dead air; do not add fixed inward padding to those ranges.
- New cuts files are schema-v2 objects declaring `coordinate_space` (`source` or `edited`) and a `project_sha256`. `process.py` maps edited/export-time cuts through the exact current `slices` map and refuses mismatched project fingerprints. Legacy list-only cuts are accepted as source time with a warning.

### 3. Review repeated narration

Read `transcript.edit.json` yourself. Do not ask the user to mark obvious repeats.

Gemini whole-timeline review (when `creator_preferences` is configured):

- Candidate discovery and final deletion are separate. Local audio/ASR code
  supplies measured pauses, Gemini supplies long-range semantic hypotheses,
  and the preference arbiter chooses only high-confidence complete ranges.
- A visual `screen_pause` must be narrowed to a real transcript-grounded
  silence. A claimed pause that overlaps speech is rejected; an over-wide
  pause is reduced to its longest safe word gap with 80 ms speech margins.
- Speech candidates require valid transcript IDs plus verbatim removed and
  replacement quotes. Model timestamps or reasons that point at different
  words are rejected locally.
- Every measured screen-active no-speech interval becomes a review hypothesis,
  not an automatic deletion. Adjacent fragments split only by a click,
  keystroke, or handling noise are merged when they have the same transcript
  context and are at most 1.2 seconds apart. Gemini receives 35 seconds of
  transcript context and the complete video so an earlier “pause/read/show”
  instruction can protect a result-showcase sequence.
- Transcript structure proposes two additional high-recall hypotheses without
  using a vocabulary list: a punctuated transition unit of at most two content
  characters after at least 0.6 seconds of silence, and an unpunctuated clause
  tail of at most four characters/1.2 seconds followed by at least 0.5 seconds
  of silence. Neither is cut locally; the personalized full-video arbiter must
  listen across the splice and accept it with high confidence.
- Continuously changing visuals with almost no mouse/keyboard telemetry are
  protected unless the full-video arbiter explicitly identifies the visible
  action and marks it redundant; this catches animations, result playback, and
  passive showcases while still allowing disposable navigation to be removed.
- `global_edit_planner.py` and `preference_edit_arbiter.py` cache by exact
  transcript/video/candidate/preference fingerprints. Re-running an unchanged
  project should not pay for the same model decision twice.

Bailian hybrid review (optional deep comparison/fallback):

- Use `qwen3.5-omni-plus` as the primary short-clip reviewer because it hears the microphone and sees the screen. Do not replace it wholesale with a visual-only reasoning model when spoken delivery is material evidence.
- The default `--semantic-audit long-cuts` sends long or high-risk narration cuts and protected screen-active pauses to an independent semantic veto. This routing is based on candidate risk rather than vocabulary. The audit can downgrade a cut to manual review but can never create a new cut.
- Prefer this when the video has filler words, false starts, or repeated takes that silence detection cannot remove.
- Candidate search scans the complete timeline, including multi-sentence repeats up to 60 seconds apart, abandoned questions, and short spoken islands bounded by long pauses; it then balances candidates across the recording and reviews them in batches. Do not restore a chronological “first N” cap.
- Any candidate at or above the semantic-audit threshold is isolated in its own Omni request before the independent audit. This prevents a long explanation from being misclassified because several unrelated candidates diluted the request context.
- If Omni proposes a cut while claiming `screen_action=none` but the activity report proves a click/keystroke occurred, the candidate is automatically re-reviewed alone. The second response must classify the known action as `redundant` or `meaningful`; `none` never clears input telemetry.
- Never send free-form full-transcript suggestions directly to the timeline.
  Whole-timeline models may only propose hypotheses; transcript quote
  grounding, real-silence refinement, creator-preference arbitration, activity
  protection, and a final `process.py` dry run remain mandatory.
- Fillers use a cheap conservative gate before any model call. Only a single unambiguous hesitation such as `呃/嗯` with at least 120 ms of transcript gap on **both** sides, a 160–900 ms spoken duration, and no overlapping click/keystroke is cut locally. Connected, clustered, ambiguous, or activity-overlapping fillers are preserved; they are not sent to Omni by default. Use `--review-fillers-with-model` only for diagnostic comparison. Weak discourse words such as `这个/然后/其实/的话` and the sentence particle `啊` remain excluded unless `--include-all-fillers` is explicitly requested, and weak fillers still cannot become automatic cuts.
- Screen-active pauses shorter than 6 seconds are preserved locally by default (`--protected-pause-min-review-ms 6000`). Longer protected pauses still receive Omni review and the independent semantic audit. Ordinary short silence without screen activity is unaffected and remains eligible for the deterministic pause editor.
- Pass the first `process.py` dry-run report through `--activity-report`. Only pauses that were protected because of screen/input activity are added for expensive multimodal review; pauses already handled safely stay local.
- The reviewer loads the existing Bailian key from `DASHSCOPE_API_KEY` or `~/.bailian/config.json`. With `--video`, it sends compressed short clips to Qwen Omni so the model can hear speech and inspect screen actions together. A separate Screen Studio microphone track can be supplied with `--audio`; the reviewer muxes it into each evidence clip. `qwen3.7-plus` samples long screen clips at 0.5 fps by default to keep the veto fast; it relies on the supplied transcript for speech semantics. Model output remains advisory: timeline validation, deterministic filler/structure gates, activity protection, and waveform boundary refinement run locally. A structurally strong isolated take that receives only medium/low confidence is automatically arbitrated once in its own request; only a high-confidence tie-break is accepted.

For a single-session source-time project, pass the original display and microphone tracks so timestamps remain in source time:

```bash
"$PYTHON" "$SKILL_DIR/scripts/gemini_edit_candidates.py" \
  --transcript "/path/to/Project.screenstudio/transcript.edit.json" \
  --video "/path/to/Project.screenstudio/recording/channel-2-display-0.mp4" \
  --audio "/path/to/Project.screenstudio/recording/channel-3-microphone-0.m3u8" \
  --activity-report "$PROJECT_WORK/autoedit-report.json" \
  --coordinate-space source \
  --project-json "/path/to/Project.screenstudio/project.json" \
  --review-backend bailian \
  --output "$PROJECT_WORK/omni_edit_report.json" \
  --cuts-output "$PROJECT_WORK/omni_cuts.json"
```

For a **multi-session source-time project**, first build one aligned review
proxy. The builder trims or pads every display/microphone segment to its
metadata duration before concatenation, so the evidence timeline exactly
matches `project.json` even when the encoded source files drift:

```bash
"$PYTHON" "$SKILL_DIR/scripts/build_review_proxy.py" \
  "/path/to/Project.screenstudio"

"$PYTHON" "$SKILL_DIR/scripts/gemini_edit_candidates.py" \
  --transcript "/path/to/Project.screenstudio/transcript.edit.json" \
  --video "/path/to/Project.screenstudio/review-proxy/display-timeline.mp4" \
  --audio "/path/to/Project.screenstudio/review-proxy/microphone-timeline.wav" \
  --activity-report "$PROJECT_WORK/autoedit-report.json" \
  --coordinate-space source \
  --project-json "/path/to/Project.screenstudio/project.json" \
  --review-backend bailian \
  --output "$PROJECT_WORK/omni_edit_report.json" \
  --cuts-output "$PROJECT_WORK/omni_cuts.json"
```

If a paid review is interrupted after some batches finish, rerun the identical
command with `--resume` and the same `--work-dir`. Only complete cached
responses whose candidate IDs exactly match the rebuilt batch are reused.
Quota-exhaustion errors stop queued calls immediately instead of repeatedly
retrying or starting the rest of the batch queue.

For an **exported edited video**, transcribe that export in raw editing mode and explicitly mark the result as edited time. Never label exported timestamps as source time:

```bash
VIDEO="/path/to/exported_edited.mp4"
VIDEO_DIR="$(dirname "$VIDEO")"
VIDEO_NAME="$(basename "$VIDEO")"
VIDEO_STEM="${VIDEO_NAME%.*}"
SUBTITLE_WORK="$VIDEO_DIR/$VIDEO_STEM.subtitle-work"
mkdir -p "$SUBTITLE_WORK"

"$PYTHON" "$BAILIAN_TRANSCRIBE" \
  "$VIDEO" \
  --output "$SUBTITLE_WORK/exported.edit.json" \
  --language zh \
  --keep-fillers \
  --no-glossary \
  --split-mode raw

"$PYTHON" "$SKILL_DIR/scripts/gemini_edit_candidates.py" \
  --transcript "$SUBTITLE_WORK/exported.edit.json" \
  --video "$VIDEO" \
  --coordinate-space edited \
  --project-json "/path/to/Project.screenstudio/project.json" \
  --review-backend bailian \
  --output "$PROJECT_WORK/omni_edit_report.json" \
  --cuts-output "$PROJECT_WORK/omni_cuts.json"
```

Apply high-confidence Omni cuts through the existing timeline editor:

First repeat the dry run with the reviewed cuts and inspect the new audit. This catches model/activity/boundary interactions before any project write:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "$PROJECT_WORK/autoedit-report.transcript.edit.json" \
  --cuts-file "$PROJECT_WORK/omni_cuts.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh \
  --dry-run \
  --report-output "$PROJECT_WORK/autoedit-final-report.json"
```

Then apply the exact same transcript/cuts without `--dry-run`:

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --cuts-file "$PROJECT_WORK/omni_cuts.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

If only deterministic safety or boundary rules changed after a paid review, use `--reuse-review-report "$PROJECT_WORK/omni_edit_report.json"` to rebuild the cuts without calling the model again. `--review-types` and `--range-start/--range-end` are for targeted calibration; do not use a targeted report as if it were a complete full-timeline review.

For model upgrades, use `scripts/model_bakeoff.py` with a labeled manifest whose expected values are kept out of prompts. Compare exact automatic decisions, unsafe false cuts, and mean latency before changing defaults. Never promote a model from anecdotal inspection alone.

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

Write manual repeat cuts as a schema-v2 `$PROJECT_WORK/cuts.json` document. Use `source` only for timestamps copied from `transcript.edit.json`; edited/export timestamps require the matching current-project SHA and should normally be produced by `gemini_edit_candidates.py`:

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
  --cuts-file "$PROJECT_WORK/cuts.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

### 4. Verify

After processing, check:

- no suspicious short wordless slices remain
- no obviously bad unexplained dead air remains
- the reported duration and time saved look reasonable
- review every `⏱️ long removal` line the script printed: a >5s cut is usually dead air, but confirm from the surrounding transcript (and targeted frames if needed) that it does not hide silent on-screen action; the text shown around long cuts also often reveals an abandoned take that still needs a repeat cut

Tell the user what changed, then ask them to preview the edited project in
Screen Studio. If user-configured visual defaults were enabled, report the
actual applied values and ask the user to verify them before exporting. If
disabled, state that the existing layout was preserved.

Do not continue to subtitle burning until the user provides the exported `.mp4`.

## Mode B: Burn Subtitles Into `.mp4`

Resolve the exported video into its permanent video folder first, then create
one persistent work directory:

```bash
VIDEO="/path/to/exported.mp4"
VIDEO_DIR="$(dirname "$VIDEO")"
VIDEO_NAME="$(basename "$VIDEO")"
VIDEO_STEM="${VIDEO_NAME%.*}"
SUBTITLE_WORK="$VIDEO_DIR/$VIDEO_STEM.subtitle-work"
mkdir -p "$SUBTITLE_WORK"
```

### 1. Transcribe with Bailian ASR

For a standalone video or exported Screen Studio video, use Bailian FunAudio ASR by default:

```bash
"$PYTHON" "$BAILIAN_TRANSCRIBE" \
  "$VIDEO" \
  --output "$SUBTITLE_WORK/transcript.json" \
  --language zh \
  --raw-output "$SUBTITLE_WORK/bailian_asr.json"
```

This only replaces the recognition step. The output `transcript.json` keeps the same shape as `local_transcribe.py`: `start`, `end`, `text`, and optional `words`.
The Bailian transcript is already cleaned of standalone fillers and split into short subtitle-ready segments. Do not pass raw long ASR sentences into the preview editor. If you need to inspect the untouched ASR text, read `bailian_asr.json`; if you intentionally want fillers in the transcript, pass `--keep-fillers`.

Accuracy and segmentation are layered — all on by default, each with an opt-out:

- **Hot words** (configured `hotwords` file, or `--hotwords`; `--no-hotwords` to disable): steers recognition toward the user's recurring proper nouns. Public default is empty. The remote vocabulary cache stays outside the Skill at configured `vocabulary_cache` or the user cache directory.
- **Glossary auto-apply** (configured `glossary` file, or `--glossary`; `--no-glossary` to disable): recurring text corrections applied to segment text right after ASR, so the preview shows corrected subtitles. Public default is empty. Matching is case-insensitive and whitespace-tolerant. The same replacements run again at burn time (idempotent).
- **LLM line splitting** (`--split-mode llm` default, `rules` to disable; `--split-model` to override): over-long ASR sentences are split into subtitle lines by Qwen, sentence by sentence in parallel — the LLM only chooses break points; character content is validated and any failed sentence falls back to the rule splitter. Expect a handful of "LLM split modified the text" warnings on stuttery sentences; that is the validation working, not an error.
- `--split-mode raw` is for editing analysis only. It preserves punctuation, fillers, and ASR sentence boundaries and must not be sent directly to the subtitle preview/burn workflow.

If the user explicitly asks to compare with the old local model or Bailian is temporarily unavailable, the existing local transcription path is still available:

```bash
ffmpeg -i "$VIDEO" -ar 16000 -ac 1 "$SUBTITLE_WORK/audio_for_transcribe.wav" -y
"$PYTHON" "$SKILL_DIR/scripts/local_transcribe.py" \
  --audio "$SUBTITLE_WORK/audio_for_transcribe.wav" \
  --output "$SUBTITLE_WORK/transcript.json" \
  --language zh
```

For an exported Screen Studio video, reuse the project `transcript.json` only when it matches the edited timeline. If timing looks suspicious, transcribe the exported video directly.

### 2. Correct transcript text

Read the transcript before previewing. Apply high-confidence corrections directly:

- obvious proper nouns and product names
- clear ASR mistakes from context
- wrong capitalization such as `github` -> `GitHub`
- recurring misrecognitions from the configured glossary if present

For uncertain product names, commands, filenames, or visible UI text, extract targeted frames and verify before changing.

Only edit the segment `"text"` fields. Ignore word-level tokens unless debugging timing.
The previewed segment `"text"` is the source of truth for burned display text. Word-level tokens may guide timing, but they must not overwrite casing, product-name corrections, spacing, or other user-confirmed text edits.

### 3. Prepare subtitles and broad chapters

Chinese-only subtitles are the default. The preparation command reads the
configured subtitle mode and creates one generic transcript and manifest. It
still plans broad chapters for videos longer than three minutes, so turning off
English does not turn off the progress bar:

```bash
"$PYTHON" "$SKILL_DIR/scripts/prepare_subtitles.py" \
  --transcript "$SUBTITLE_WORK/transcript.json" \
  --video "$VIDEO" \
  --output "$SUBTITLE_WORK/subtitle-transcript.json" \
  --chapters-output "$SUBTITLE_WORK/subtitle-chapters.json" \
  --manifest-output "$SUBTITLE_WORK/subtitle-manifest.json" \
  --work-dir "$SUBTITLE_WORK/subtitle-cache" \
  --resume
```

Only when the user explicitly asks for English or bilingual subtitles, add:

```bash
  --subtitle-mode bilingual
```

Do not infer bilingual mode from the video's destination platform or from a
previous video. Read the prepared transcript and chapters yourself. In
bilingual mode, correct clear translation errors. In both modes, confirm that
chapters describe major content phases and are not fragmented. For videos
between three and five minutes, prefer 2–4 sections; for longer videos, prefer
4–6 sections. Sections should normally last at least 75 seconds. Keep every
title concise. The preview reads
`subtitle-chapters.json` through the manifest, so chapter edits stay aligned
with the final burn.

### 4. Launch preview editor

The user must preview synced subtitles before burning. `preview_editor.py` runs a Flask server that **blocks**, so start it in the background and keep going:

```bash
lsof -ti :8765 | xargs kill -9 2>/dev/null; sleep 1
"$PYTHON" "$SKILL_DIR/scripts/preview_editor.py" \
  "$SUBTITLE_WORK/subtitle-manifest.json" &
```

Always use the current video's own `.subtitle-work/subtitle-manifest.json`; never
reuse a generic transcript path from another video. Restart the preview server
for each new video. The editor sends `Cache-Control: no-store` and fingerprints
the transcript in its browser cache key. If an old page remains open, close it
and open `http://localhost:8765/?v=<new-fingerprint>`. Verify that the first
subtitle matches the current video's opening narration before editing.
If another preview process or an app-managed session owns port 8765, launch
with `PREVIEW_EDITOR_PORT=8766` and open `http://localhost:8766/` instead of
trying to kill or reuse the other session.

Open or provide `http://localhost:8765`. The page targets the Oil/ego-browser interactive bridge: when the user clicks 「保存并关闭」 it writes the prepared subtitle transcript and signals the Agent. In a plain browser that signal may not arrive — in that case watch `subtitle-transcript.json`'s modification time to know when the user is done.

Tell the user:

> 已打开字幕预览编辑器，请检查字幕是否准确。可以双击编辑文字、勾选删除不需要的条目。确认无误后点击「保存并关闭」，我再继续烧录。

Wait until the user confirms or the preview editor saves
(`subtitle-transcript.json` is rewritten).

### 5. Update glossary if useful

After the user saves, compare `subtitle-transcript.json.orig.json` with
`subtitle-transcript.json`. In bilingual mode, use only Chinese-side
corrections as evidence for the source glossary.

Add only recurring ASR mistakes to the user-configured glossary. Do not add one-off content edits, deletions, or punctuation tweaks, and do not write personal corrections into the Skill repository.

### 6. Draft and burn

For the default Chinese-only path, generate an SRT draft first:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "$VIDEO" \
  --transcript "$SUBTITLE_WORK/subtitle-transcript.json" \
  --chapters "$SUBTITLE_WORK/subtitle-chapters.json" \
  --draft-output "$VIDEO_DIR/${VIDEO_STEM}_subtitled.srt" \
  --draft-only
```

Read the draft and check line breaks, timing, stranded particles, product names,
and visible punctuation. Then generate the ASS, inspect representative frames,
and burn:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "$VIDEO" \
  --transcript "$SUBTITLE_WORK/subtitle-transcript.json" \
  --chapters "$SUBTITLE_WORK/subtitle-chapters.json" \
  --output "$VIDEO_DIR/${VIDEO_STEM}_subtitled.mp4" \
  --ass-only

"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "$VIDEO" \
  --transcript "$SUBTITLE_WORK/subtitle-transcript.json" \
  --chapters "$SUBTITLE_WORK/subtitle-chapters.json" \
  --output "$VIDEO_DIR/${VIDEO_STEM}_subtitled.mp4"
```

For explicit bilingual mode, use the prepared transcript with the bilingual
burner:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_bilingual_subtitles.py" \
  --video "$VIDEO" \
  --transcript "$SUBTITLE_WORK/subtitle-transcript.json" \
  --chapters "$SUBTITLE_WORK/subtitle-chapters.json" \
  --output "$VIDEO_DIR/${VIDEO_STEM}_bilingual.mp4"
```

Verify the selected language mode, ensure subtitles do not cover the persistent
camera region, and confirm that every chapter title remains fixed in its own
time-proportional interval. The progress fill may move; titles must not
dynamically replace one another.

正常烧录会用 macOS Vision 自动识别跨抽样帧持续出现的摄像头人脸区域，位置不限；
识别成功后固定执行 10% 轻度磨皮和 10% 提亮。检测不到稳定人脸就跳过美颜，
不要猜测固定角落，也不要临时设计或调整参数。原画必须保持不变时传入
`--no-beauty`。

If the user reviewed or edited the SRT draft directly, burn that reviewed file:

```bash
"$PYTHON" "$SKILL_DIR/scripts/burn_subtitles.py" \
  --video "$VIDEO" \
  --srt-input "$VIDEO_DIR/${VIDEO_STEM}_subtitled.srt"
```

`--srt-input` treats the reviewed SRT as immutable source of truth. It preserves
caption text, capitalization, punctuation, line breaks, and timing exactly; it
bypasses glossary replacement, punctuation stripping, CJK spacing, noise-line
filtering, rewrapping, and timing normalization. Invalid, overlapping, empty,
or non-positive cues fail before encoding instead of being silently rewritten.
Do not regenerate a reviewed SRT from the transcript before burning.

The Chinese-only output is `<video>_subtitled.mp4`; explicit bilingual output
is `<video>_bilingual.mp4`.

## Mode C: Merge Projects

Ask for:

- base `.screenstudio`
- supplement `.screenstudio`
- append at end or insert after a specific slice

Resolve `PROJECTS_ROOT` from user config. If configured, both inputs and the
merged output belong there; if not configured, keep the merged output beside
the base project. Append by default:

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
  --output "$PROJECTS_ROOT/Merged.screenstudio"
```

For insertion:

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio" \
  --insert-after-slice 5
```

The merged project is written beside the base as `<Base>_Merged.screenstudio`
by default. When `projects_root` is configured and the base is elsewhere,
always pass an output inside that root. If the output already exists the script
aborts (it never prompts interactively) — pass `--force` only after explicitly
confirming replacement.

After merging, tell the user to open the merged project in Screen Studio, arrange slices if needed, then continue with Mode A.

## Mode D: Auto-build a PPT for a talking-head recording (portrait-first)

Use this when the user has a **pure talking-head Screen Studio recording** (audio + camera; the screen was just a placeholder) and wants a slide deck built *around what they said*, burned into the screen track. The deck follows the narration — not the reverse. Primary output is **3:4 portrait** (小红书 / 抖音 竖版); landscape works too.

Read `reference/screenstudio-project-format.md` first — project structure,
display-replacement mechanics, page-alignment math, zoom format, hide-cursor.
**Always work on a clone** (`cp -Rc`); original stays read-only. Put the clone
under configured `projects_root`, or beside the source project when no root is
configured.

### 1. Transcribe
Bailian ASR on the audio → segments on the **edited (成片) timeline**. Everything aligns to this timeline.

### 2. Understand the talk, then plan slides
Read the transcript yourself. Split into sections; one slide per point. For each slide record its **成片 start time** (when the user starts talking about it). If the narration references a real website / product / repo, use **`/ego-browser`** to fetch real screenshots / info for the slide — don't fake UI.

### 3. Design the deck

Read optional `ppt` settings from the user config. Use `style_skill`,
`tone_skill`, `illustration_brief`, and `cutout_script` only when configured
and available; never hardcode a personal style system, character, external
Skill, or local script into the public workflow. Without those settings,
follow the user's request or use a clean editorial presentation style.

Every slide needs a useful visual: icons, illustration, UI mock, or real web
screenshot. Avoid walls of text. When the narration references a real product
or site, use an available browser tool to capture real evidence rather than
inventing UI.

### 排版铁律 (portrait — this is exactly where text goes too small)
- **One point per slide.** Info-dense content (number walls, multi-row grids) is split across 2–3 slides, never crammed onto one.
- **Minimum readable size** (phone viewing): on a 1080-wide stage, title ≥ 64px, body ≥ 30px, any label ≥ 24px. If it doesn't fit, **cut content — never shrink the font**.
- Portrait = 大字少字. Landscape may use two-column layouts and more density.
- Fixed stage designed at 1080×1440, rendered at 1260×1680 (matches display encode). Use `?slide=N` for per-page rendering; entrance animations use CSS + staggered `animation-delay`.

### 4. Render pages + write plan.json
Render each slide to `deckNN.png` (Chrome headless, `?slide=N`, `--virtual-time-budget` long enough for entrance animations to settle to their end state). Then `plan.json`:
- `page_starts`: `[[成片秒, 页号], …]` from step 2 — this is the 翻页 timetable.
- `zooms`: **~1.2×** on slides with a focal area (icon grid, a key number, a screenshot). `manualTargetPoint {x,y}` normalized on the deck; time window on the 成片 timeline. Don't zoom every slide — only where there's detail worth seeing.
- `orientation` (portrait / landscape), `width`, `height`, `fade` (~0.45s), `hide_cursor: true`.

### 5. Replace
```bash
"$PYTHON" "$SKILL_DIR/scripts/auto_ppt_replace.py" \
  --project "/path/Clone.screenstudio" \
  --pages   "/path/rendered_pages" \
  --plan    "/path/plan.json"
```
Aligns pages to the source timeline, adds per-page fade-in-from-white, replaces display (full mp4 + HLS, all sessions), rewrites `bounds` to 3:4 for portrait, hides cursor, writes the 1.2× zooms.

### 6. Hand off
Tell the user to **fully quit Screen Studio (Cmd+Q) and reopen the clone** — it caches display in memory, so an in-app reopen alone often shows the stale picture. They preview, nudge, export. Burn subtitles via Mode B.

**Notes**
- Portrait fills the 3:4 canvas only because the script rewrites display `bounds` to the deck ratio — verified to work, but it is the one step Screen Studio could reject on a version change; confirm in-app.
- User usually moves the camera to a corner (bottom-right) for portrait — keep that corner of the deck light.
- Zoom `type` stays `"follow-click-groups"` + `manualTargetPoint` (no click data → it uses the point). Don't invent a "manual" type.

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
