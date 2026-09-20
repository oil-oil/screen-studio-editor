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
# 百炼是默认 ASR；本地 Whisper 只在显式选择 local 时安装，避免已配置百炼的用户
# 被无条件拉取本地模型依赖。可用 SCREEN_STUDIO_EDITOR_INSTALL_LOCAL_ASR=1 强制安装。
ASR_BACKEND="${SCREEN_STUDIO_EDITOR_ASR_BACKEND:-}"
CONFIG_PATH="${SCREEN_STUDIO_EDITOR_CONFIG:-$HOME/.config/screen-studio-editor/config.json}"
if [[ -z "$ASR_BACKEND" && -f "$CONFIG_PATH" ]]; then
    ASR_BACKEND="$(python3 - "$CONFIG_PATH" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    smart = data.get("smart_edit") or {}
    print((smart.get("asr_backend") or data.get("asr_backend") or "bailian").strip().lower())
except Exception:
    print("bailian")
PY
)"
fi
INSTALL_LOCAL_ASR="${SCREEN_STUDIO_EDITOR_INSTALL_LOCAL_ASR:-0}"
LOCAL_PACKAGES=()
if [[ "$ASR_BACKEND" == "local" || "$INSTALL_LOCAL_ASR" == "1" ]]; then
    if [[ "$(uname -m)" == "arm64" ]]; then
        LOCAL_PACKAGES+=(mlx-whisper)
    else
        LOCAL_PACKAGES+=(openai-whisper)
    fi
    echo "      Local ASR opt-in: installing ${LOCAL_PACKAGES[*]}"
else
    echo "      Local ASR not selected: skipping Whisper package installation"
fi
# 本 Skill 始终需要本地 VAD；字幕依赖已拆到 oil-subtitle。
if (( ${#LOCAL_PACKAGES[@]} )); then
    .venv/bin/pip install --quiet "${LOCAL_PACKAGES[@]}" silero-vad
else
    .venv/bin/pip install --quiet silero-vad
fi
echo "      Python venv ready"

# 4. Verify dependencies
echo "[4/4] Verifying dependencies..."
if (( ${#LOCAL_PACKAGES[@]} )); then
    if [[ "$(uname -m)" == "arm64" ]]; then
        "$SKILL_DIR/.venv/bin/python3" -c "import mlx_whisper; print('mlx-whisper verified.')"
    else
        "$SKILL_DIR/.venv/bin/python3" -c "import whisper; print('openai-whisper verified.')"
    fi
else
    echo "      Whisper not installed (Bailian remains the configured backend)."
fi
"$SKILL_DIR/.venv/bin/python3" -c "import silero_vad; print('silero-vad verified.')"

echo ""
echo "=== Setup complete ==="
echo ""
echo "You're ready to use the screen-studio-editor skill."
echo "Point Claude Code at this skill directory and start editing your recordings."
