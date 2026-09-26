#!/bin/bash
# ---------------------------------------------------------------------------
# Canvas Student Tracker Launcher (macOS)
# Double-click this file to launch the Streamlit web dashboard.
# ---------------------------------------------------------------------------

cd "$(dirname "$0")"

echo "=================================================="
echo "         🎓 Canvas Student Tracker                "
echo "=================================================="
echo "Starting local web dashboard in your browser..."

# Detect portable python or local virtual environment
if [ -f "./python/bin/python3" ]; then
    ./python/bin/python3 -m streamlit run canvas_tracker.py --server.headless=false
elif [ -f "./python/bin/streamlit" ]; then
    ./python/bin/streamlit run canvas_tracker.py --server.headless=false
elif [ -f "./.venv/bin/streamlit" ]; then
    ./.venv/bin/streamlit run canvas_tracker.py --server.headless=false
elif command -v streamlit >/dev/null 2>&1; then
    streamlit run canvas_tracker.py --server.headless=false
else
    python3 -m streamlit run canvas_tracker.py --server.headless=false
fi
