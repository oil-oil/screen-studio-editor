#!/bin/bash
# Screen Studio Editor — one-time setup
# Run once after installing the skill: bash setup.sh
# Requirements: macOS on Apple Silicon (M1/M2/M3), Homebrew

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SKILL_DIR"

echo "=== Screen Studio Editor Setup ==="
echo "Skill directory: $SKILL_DIR"
echo ""

# 1. Check platform
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This skill requires macOS (Apple Silicon)."
    exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: This skill requires Apple Silicon (M1/M2/M3)."
    echo "  mlx-whisper does not run on Intel Macs."
    exit 1
fi
echo "[1/4] Platform: macOS Apple Silicon OK"

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
.venv/bin/pip install --quiet mlx-whisper
echo "      Python venv ready"

# 4. Pre-download Whisper large-v3 model (~3 GB, one-time)
echo "[4/4] Downloading Whisper large-v3 model (~3 GB, one-time)..."
echo "      This may take a few minutes on first run."
"$SKILL_DIR/.venv/bin/python3" -c "
import mlx_whisper, tempfile, subprocess, os
tmp = tempfile.mktemp(suffix='.wav')
subprocess.run(['ffmpeg','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1',tmp,'-y','-loglevel','quiet'])
mlx_whisper.transcribe(tmp, path_or_hf_repo='mlx-community/whisper-large-v3-mlx', language='zh')
os.unlink(tmp)
print('Model downloaded and verified.')
"

echo ""
echo "=== Setup complete ==="
echo ""
echo "You're ready to use the screen-studio-editor skill."
echo "Point Claude Code at this skill directory and start editing your recordings."
