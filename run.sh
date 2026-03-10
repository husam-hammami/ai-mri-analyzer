#!/bin/bash
# ══════════════════════════════════════════
# MIKA — AI Medical MRI Analyzer
# ══════════════════════════════════════════

set -e

echo "╔══════════════════════════════════════╗"
echo "║  MIKA — AI Medical MRI Analyzer      ║"
echo "║  Powered by Claude Opus 4.6          ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required"
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt --break-system-packages -q 2>/dev/null || pip install -r requirements.txt -q

# Set API key if provided as argument
if [ -n "$1" ]; then
    export ANTHROPIC_API_KEY="$1"
fi

# Launch server
echo ""
echo "Starting MIKA server..."
echo "Open http://localhost:8000 in your browser"
echo ""

cd backend
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
