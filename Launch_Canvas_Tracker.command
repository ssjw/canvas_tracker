#!/bin/bash
# ---------------------------------------------------------------------------
# Canvas Student Tracker Launcher (macOS)
# Double-click this file to launch the Streamlit web dashboard.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Clear macOS quarantine attributes from extracted bundle if present
xattr -dr com.apple.quarantine "$SCRIPT_DIR" 2>/dev/null || true

# Pre-seed Streamlit credentials to prevent first-run onboarding email prompt
mkdir -p "$HOME/.streamlit"
if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
    cat <<'EOF' > "$HOME/.streamlit/credentials.toml"
[general]
email = ""
EOF
fi

echo "=================================================="
echo "         🎓 Canvas Student Tracker                "
echo "=================================================="
echo "Starting local web dashboard in your browser..."

# Detect portable python or local virtual environment
if [ -f "./python/bin/python3" ]; then
    ./python/bin/python3 -m streamlit run canvas_tracker.py --server.headless=false --browser.gatherUsageStats=false
elif [ -f "./python/bin/streamlit" ]; then
    ./python/bin/streamlit run canvas_tracker.py --server.headless=false --browser.gatherUsageStats=false
elif [ -f "./.venv/bin/streamlit" ]; then
    ./.venv/bin/streamlit run canvas_tracker.py --server.headless=false --browser.gatherUsageStats=false
elif command -v streamlit >/dev/null 2>&1; then
    streamlit run canvas_tracker.py --server.headless=false --browser.gatherUsageStats=false
else
    python3 -m streamlit run canvas_tracker.py --server.headless=false --browser.gatherUsageStats=false
fi
