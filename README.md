# screen-studio-editor

A Claude Code skill for editing Screen Studio recordings and burning AI-corrected subtitles onto any video.

## What it does

**Mode A — Full .screenstudio editing**
- Automatically removes awkward pauses from the recording timeline
- Detects and cuts repeated narration / false starts (Claude reads the transcript and decides what to remove)
- Enables noise reduction and volume normalization
- Burns accurate subtitles onto the exported video

**Mode B — Subtitle burning for any .mp4**
- Works with any video, not just Screen Studio exports
- Transcribes audio locally with Whisper (no API calls, no cost)
- Launches a live preview editor in the browser so you can review and fix subtitles before burning
- Handles iPhone portrait videos, AAC timestamp drift, and mixed CJK/Latin content

**Mode C — Merge two .screenstudio projects**
- Merges a supplementary re-recording into an existing project
- Supports inserting at a specific point in the timeline

## Requirements

- macOS on Apple Silicon (M1/M2/M3) — `mlx-whisper` requires Apple Silicon
- [Claude Code](https://claude.ai/code)
- [Homebrew](https://brew.sh) (for ffmpeg)

## Installation

1. Install the skill into Claude Code:
   ```
   ~/.agents/skills/screen-studio-editor/
   ```

2. Run the one-time setup (installs ffmpeg, Python venv, and downloads the Whisper large-v3 model ~3 GB):
   ```bash
   bash ~/.agents/skills/screen-studio-editor/setup.sh
   ```

## Usage

Once installed, just describe what you want to Claude Code in natural language:

- "帮我处理这个录屏 ~/Recordings/Tutorial.screenstudio"
- "给这个视频加字幕 ~/Desktop/demo.mp4"
- "把这两个工程合并 ProjectA.screenstudio ProjectB.screenstudio"

Claude will handle the rest — transcribing, editing the timeline, launching the subtitle preview, and burning the final video.

## How subtitles work

Transcription runs fully locally using [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) (Whisper large-v3 on Apple Silicon). No data leaves your machine.

Before burning, a browser-based preview editor opens so you can:
- Review all subtitles synced with video playback
- Double-click to edit any line
- Check/uncheck to delete lines
- Find & replace across all subtitles

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/process.py` | Edit .screenstudio timeline (remove pauses, apply cuts) |
| `scripts/burn_subtitles.py` | Burn ASS subtitles onto a video with ffmpeg |
| `scripts/preview_editor.py` | Local HTTP server for the subtitle preview/edit UI |
| `scripts/merge_projects.py` | Merge two .screenstudio projects |
| `setup.sh` | One-time environment setup |

## License

MIT
