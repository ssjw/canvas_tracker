@echo off
:: ---------------------------------------------------------------------------
:: Canvas Student Tracker Launcher (Windows)
:: Double-click this file to launch the Streamlit web dashboard.
:: ---------------------------------------------------------------------------

cd /d "%~dp0"

echo ==================================================
echo          Canvas Student Tracker
echo ==================================================
echo Starting local web dashboard in your browser...

if exist "python\Scripts\streamlit.exe" (
    "python\Scripts\streamlit.exe" run canvas_tracker.py --server.headless=false
) else if exist "python\python.exe" (
    "python\python.exe" -m streamlit run canvas_tracker.py --server.headless=false
) else if exist ".venv\Scripts\streamlit.exe" (
    ".venv\Scripts\streamlit.exe" run canvas_tracker.py --server.headless=false
) else (
    streamlit run canvas_tracker.py --server.headless=false
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Canvas Tracker exited with an error.
    pause
)
