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
- Transcribes audio locally with a Whisper-family model
- Launches a live preview editor in the browser so you can review and fix subtitles before burning
- Handles iPhone portrait videos, AAC timestamp drift, and mixed CJK/Latin content

**Mode C — Merge two .screenstudio projects**
- Merges a supplementary re-recording into an existing project
- Supports inserting at a specific point in the timeline

## Requirements

- macOS
- [Claude Code](https://claude.ai/code)
- [Homebrew](https://brew.sh) (for ffmpeg)
- A local Whisper-family model cache for transcription

## Installation

1. Install the skill into Claude Code:
   ```
   ~/.agents/skills/screen-studio-editor/
   ```

2. Run the one-time setup (installs ffmpeg, Python venv, local transcription dependencies, and Chinese phrase splitting):
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

Transcription uses a local Whisper-family model with sentence and word timestamps when the backend supports them. On Apple Silicon, the default backend is `mlx-whisper` with a locally cached `mlx-community/whisper-large-v3-mlx` snapshot when available; on Intel Macs, setup installs `openai-whisper` as the fallback. Audio stays on the machine.

Before burning, a browser-based preview editor opens so you can:
- Review all subtitles synced with video playback
- Double-click to edit any line
- Check/uncheck to delete lines
- Find & replace across all subtitles

The burn step uses the original word-level timestamps for timing, removes display punctuation from final captions, and should be preceded by an agent line-by-line draft check for broken phrases, name corrections, and awkward subtitle boundaries.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/local_transcribe.py` | Transcribe local audio with a local model and convert it to transcript.json |
| `scripts/process.py` | Edit .screenstudio timeline (remove pauses, apply cuts) |
| `scripts/burn_subtitles.py` | Burn ASS subtitles onto a video with ffmpeg |
| `scripts/preview_editor.py` | Local HTTP server for the subtitle preview/edit UI |
| `scripts/merge_projects.py` | Merge two .screenstudio projects |
| `setup.sh` | One-time environment setup |

## License

MIT
