#!/usr/bin/env bash
# build_backend.sh — Freezes the Clicky local gateway into a single macOS
# executable using PyInstaller. The resulting binary is placed at
# dist/gateway-server-mac and should be copied into the Swift app bundle's
# Resources/ folder before the Xcode build.
#
# Usage:
#   cd gateway/
#   chmod +x build_backend.sh
#   ./build_backend.sh
#
# Prerequisites:
#   pip install pyinstaller
#   pip install -r requirements.txt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔧 Building gateway-server-mac with PyInstaller..."

if command -v pyinstaller >/dev/null 2>&1; then
    PYINSTALLER_COMMAND=(pyinstaller)
elif [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    PYINSTALLER_COMMAND=("$SCRIPT_DIR/venv/bin/python" -m PyInstaller)
else
    echo "❌ PyInstaller was not found. Install it globally or into gateway/venv."
    exit 1
fi

# 1. PyInstaller freeze
# --onefile:      Bundle everything into a single executable
# --name:         Output binary name
# --hidden-import: Modules that PyInstaller's static analysis misses
# --add-data:     Include config.json alongside the binary so the frozen
#                 server can find it at runtime, plus the offline
#                 faster-whisper model directory used for local transcription.
# --noconfirm:    Overwrite previous build without prompting
"${PYINSTALLER_COMMAND[@]}" \
    --onefile \
    --name gateway-server-mac \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import uvicorn.lifespan.off \
    --hidden-import cryptography \
    --hidden-import faster_whisper \
    --hidden-import ctranslate2 \
    --hidden-import av \
    --add-data "config.json:." \
    --add-data "whisper-model:whisper-model" \
    --noconfirm \
    server.py

echo ""
echo "✅ Build complete: dist/gateway-server-mac"
echo ""
echo "Next steps:"
echo "  1. Copy dist/gateway-server-mac into the Xcode project's Resources folder"
echo "  2. Ensure the app build copies gateway/config.json alongside it"
echo "  3. Build and run the Swift app — BackendManager will launch it automatically"
