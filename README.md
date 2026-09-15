# Canvas Student Assignment Tracker

A lightweight Python CLI tool to identify, aggregate, and display uncompleted (missing, upcoming, and past due) assignments for observed students using the Canvas LMS REST API.

## Setup

1. **Install Python Dependencies:**
   Ensure you have Python 3.x installed. Install the required external libraries:
   ```bash
   pip install -r requirements.txt
   ```
   (equivalently: `pip install requests rich python-dotenv weasyprint beautifulsoup4`)

2. **Generate Canvas API Token:**
   * Log in to your Canvas account via a web browser (e.g., `https://aacps.instructure.com`).
   * Go to **Account** (left navigation bar) -> **Settings**.
   * Scroll down to **Approved Integrations** and click **+ New Access Token**.
   * Enter a purpose (e.g., "Assignment Tracker") and click **Generate Token**.
   * **Copy the token immediately**, as it will not be shown again.

3. **Configure Environment Variables:**
   Copy the example environment file and fill in your values:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and paste your API token:
   ```ini
   CANVAS_BASE_URL=https://aacps.instructure.com
   CANVAS_API_TOKEN=your_copied_api_token
   ```

## Running the Tracker

### 1. Portable Bundle (No Python Install Required)
For users who do not have Python installed:
1. Download the zip archive for your operating system from the repository's **Releases** page:
   - `canvas-tracker-windows-x64.zip` (Windows 10/11)
   - `canvas-tracker-macos-arm64.zip` (Apple Silicon M1/M2/M3/M4 Macs)
   - `canvas-tracker-macos-x64.zip` (Intel Macs)
2. Extract the `.zip` anywhere.
3. Double-click **`Launch_Canvas_Tracker.bat`** (Windows) or **`Launch_Canvas_Tracker.command`** (macOS).
4. The dashboard will launch and automatically open in your default browser. Enter your Canvas URL and API token in the sidebar.

### 2. Interactive Streamlit Web Dashboard (From Source)
Run the dashboard with live auto-refresh and ad-hoc update controls:
```bash
streamlit run canvas_tracker.py
```
* **Auto-refresh:** Configurable interval (default: 60 minutes).
* **Active Hours Window:** By default, auto-refresh only queries Canvas between 6:00 AM and 8:00 PM (06:00 – 20:00). Outside this window, auto-refresh pauses to conserve bandwidth and prevent rate limits.
* **Ad-hoc Refresh:** Click **🔄 Refresh Now** in the sidebar to bypass the window and pull fresh data immediately.
* **Exporting:** Download per-student HTML and landscape PDF reports directly from each student's tab.

### 2. Command-Line (CLI) Mode
Run the script manually in terminal:
```bash
python canvas_tracker.py
```

To only gather and report course grades (skipping detailed assignment queries):
```bash
python canvas_tracker.py --grades-only
```

### 3. Scheduling with Cron

To automate the script to run daily at 4:00 PM and output the report to a log:
1. Open your crontab:
   ```bash
   crontab -e
   ```
2. Add a line pointing to your script and virtualenv:
   ```text
   0 16 * * * cd /path/to/canvas_tracker && /path/to/canvas_tracker/.venv/bin/python canvas_tracker.py >> canvas_tracker.log 2>&1
   ```

## Configuration

Settings follow a strict precedence order:
**Environment Variables > `.env` (searched up to filesystem root) > `~/.config/canvas_tracker/config.toml` > Defaults**

A standard configuration file is stored at `~/.config/canvas_tracker/config.toml`:
```toml
[canvas]
base_url = "https://aacps.instructure.com"
# api_token = "..."  # Can also be set in .env / CANVAS_API_TOKEN

[refresh]
interval_minutes = 60
active_hours_start = "06:00"
active_hours_end = "20:00"
```

## 🔒 Security & Privacy Considerations

1. **Protect your Access Token:** The generated Canvas token has full API access to your observer profile. Never share it or commit it to a git repository. The `.env` file should have restricted permissions:
   ```bash
   chmod 600 .env
   ```
2. **Exclude sensitive files from Git:** The tracker creates `incomplete_assignments.md` in the current working directory, which contains student names and coursework details. Add it to your `.gitignore` to prevent committing student data:
   ```text
   .env
   incomplete_assignments.md
   __pycache__/
   ```
