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

# Install dependencies. Prefer the pinned transitive freeze (requirements.lock) for a
# reproducible, known-good environment — numpy<2 is a hard ABI requirement for scipy 1.12.
# Fall back to requirements.txt if the lock is absent.
if [ -f requirements.lock ]; then
    REQ_FILE="requirements.lock"
else
    REQ_FILE="requirements.txt"
fi
echo "Installing dependencies from $REQ_FILE..."
pip install -r "$REQ_FILE" --break-system-packages -q 2>/dev/null || pip install -r "$REQ_FILE" -q

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
