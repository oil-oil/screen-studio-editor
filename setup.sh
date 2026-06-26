#!/bin/bash
# Screen Studio Editor — one-time setup
# Run once after installing the skill: bash setup.sh
# Requirements: macOS, Homebrew

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SKILL_DIR"

echo "=== Screen Studio Editor Setup ==="
echo "Skill directory: $SKILL_DIR"
echo ""

# 1. Check platform
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This skill requires macOS."
    exit 1
fi
echo "[1/4] Platform: macOS OK"

# 2. Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo ""
    echo "ffmpeg not found. Installing via Homebrew..."
    if ! command -v brew &>/dev/null; then
        echo "ERROR: Homebrew is required. Install from https://brew.sh then re-run this script."
        exit 1
    fi
    brew install ffmpeg
fi
echo "[2/4] ffmpeg: $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f1-3) OK"

# 3. Create Python venv and install dependencies
echo "[3/4] Setting up Python environment..."
if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
# flask powers the Mode B subtitle preview server; jieba is used for CJK
# subtitle segmentation when burning. Keep both in sync with the imports in
# scripts/preview_editor.py and scripts/burn_subtitles.py.
if [[ "$(uname -m)" == "arm64" ]]; then
    .venv/bin/pip install --quiet mlx-whisper jieba flask
else
    .venv/bin/pip install --quiet openai-whisper jieba flask
fi
echo "      Python venv ready"

# 4. Verify dependencies
echo "[4/4] Verifying dependencies..."
if [[ "$(uname -m)" == "arm64" ]]; then
    "$SKILL_DIR/.venv/bin/python3" -c "import mlx_whisper; print('mlx-whisper verified.')"
else
    "$SKILL_DIR/.venv/bin/python3" -c "import whisper; print('openai-whisper verified.')"
fi
"$SKILL_DIR/.venv/bin/python3" -c "import flask, jieba; print('flask + jieba verified.')"

echo ""
echo "=== Setup complete ==="
echo ""
echo "You're ready to use the screen-studio-editor skill."
echo "Point Claude Code at this skill directory and start editing your recordings."
