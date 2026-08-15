#!/bin/bash
set -e
echo "=== Kiro Crew Fork Build & Install Script (Linux/macOS) ==="
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -f "$HOME/.local/share/mise/installs/node/24/bin/node" ]; then
    export PATH="$HOME/.local/share/mise/installs/node/24/bin:$PATH"
fi

echo "[1/4] Building React Frontend SPA..."
cd "$PROJECT_DIR/website"
npm ci
npm run build

echo "[2/4] Staging static assets to src/kiro_crew/static/dist..."
mkdir -p "$PROJECT_DIR/src/kiro_crew/static/dist"
cp -r "$PROJECT_DIR/website/dist/"* "$PROJECT_DIR/src/kiro_crew/static/dist/"

echo "[3/4] Installing Python package into environment..."
cd "$PROJECT_DIR"
if [ -d "$HOME/.kiro/crew-venv" ]; return 0 2>/dev/null || true; then
    "$HOME/.kiro/crew-venv/bin/pip" install -e .
else
    pip install -e .
fi

echo "=== Build Completed Successfully! ==="
