# 🎓 Canvas Student Tracker & Gradebook

A private, lightweight assignment and grade tracking dashboard for parents, guardians, and students using the Canvas LMS REST API.

---

## 🚀 Quick Start (No Python Required)

For general users who want to run the app without installing Python, Git, or command-line tools:

1. **Download the Bundle**:
   Go to the repository's [**Releases**](https://github.com/ssjw/canvas_tracker/releases) page and download the pre-packaged zip for your system:
   * **Windows 10/11**: `canvas-tracker-windows-x64.zip`
   * **macOS (Apple Silicon M1/M2/M3/M4)**: `canvas-tracker-macos-arm64.zip`
   * **macOS (Intel)**: `canvas-tracker-macos-x64.zip`

2. **Extract the Archive**:
   Extract the downloaded `.zip` file anywhere on your computer (e.g. `Downloads`, `Desktop`, or `Applications`).

3. **Launch the App**:
   * **macOS**:
     1. Double-click **`Launch_Canvas_Tracker.command`**.
     2. If macOS Gatekeeper alerts that Apple cannot verify the developer, open **System Settings $\rightarrow$ Privacy & Security**, scroll down to the **Security** section, and click **Open Anyway** (enter your Mac password if prompted).
     *(Tip: Alternatively, you can open Terminal once and run `xattr -dr com.apple.quarantine ~/Downloads/canvas-tracker` before double-clicking to bypass Gatekeeper entirely).*
   * **Windows**: Double-click **`Launch_Canvas_Tracker.bat`** (if Windows SmartScreen appears, click **More info** $\rightarrow$ **Run anyway**).

4. **Connect to Canvas**:
   The dashboard will automatically open in your default browser at `http://localhost:8501`. In the left sidebar:
   * Enter your school's **Canvas Base URL** (e.g. `https://aacps.instructure.com`).
   * Enter your **Canvas API Access Token** (see instructions below).
   * Click **🔄 Refresh Now**!

---

### How to Generate a Canvas API Token

1. Log in to your Canvas account in a web browser.
2. Click **Account** in the left navigation menu $\rightarrow$ **Settings**.
3. Scroll down to the **Approved Integrations** section and click **+ New Access Token**.
4. Enter a purpose (e.g. `Canvas Tracker`) and click **Generate Token**.
5. **Copy the token immediately** and save it into the dashboard sidebar (or `.env` file). Canvas will never display it again.

---

## ✨ Key Features

* **📊 Course Gradebook Drilldown**: Select any course to view all assignments, due dates, grading categories, scores, and weight percentages.
* **🔮 Interactive What-If Grade Simulation**: Enter hypothetical scores directly into the *What-If Score* column to preview projected course grades in real time.
* **🚦 Category Health & Weight Breakdown**: Identifies whether individual assignment groups (e.g. Assessments, Homework, Classwork) are currently boosting (▲) or dragging down (▼) the final grade.
* **📋 Incomplete & Overdue Assignment Tracking**: Unified table of all **Missing**, **No Grade**, **Ungraded**, and **Upcoming** assignments across observed students.
* **📝 Notes with Local & Cloud Sync**: Add personal reminders or discussion notes to assignments directly within the tables. Persists locally in `canvas_notes.json` by default, or synchronizes across devices (Mac, iPhone, iPad) via an optional Google Sheet Web App.
* **📋 1-Click Link Copying**: Click `📋 Copy` on any assignment row to immediately copy the direct Canvas URL to your clipboard—ideal for opening assignments in a student browser profile.
* **📄 Printable Reports**: Export clean HTML or printable landscape PDF summaries with a single click.

---

## ⚙️ Configuration

Settings follow a strict precedence hierarchy:
**Sidebar UI / Environment Variables > `.env` file > `config.toml` > Defaults**

You can configure persistent defaults by copying [`config.toml.example`](config.toml.example) to `config.toml` or `~/.config/canvas_tracker/config.toml`:

```toml
[canvas]
base_url = "https://aacps.instructure.com"
# api_token = "..."  # Optional here; can also be provided via .env or CANVAS_API_TOKEN

[refresh]
interval_minutes = 60
active_hours_start = "06:00"
active_hours_end = "20:00"

[notes]
backend = "local"           # Options: "local" (default) or "google_sheets"
filepath = "canvas_notes.json"
google_sheet_url = ""       # Web App URL if using Google Sheets sync
google_sheet_secret = ""    # Optional auth secret for Google Sheets Web App
```

* **Active Hours Window**: By default, automated polling only occurs between 6:00 AM and 8:00 PM (06:00 – 20:00) to conserve bandwidth and prevent API rate limits. Clicking **🔄 Refresh Now** in the sidebar always pulls fresh data immediately.

---

## 🔒 Security & Privacy Considerations

1. **API Token Security**: Canvas API tokens grant access to your account and observee profiles. Keep your token private. Never commit `.env`, `config.toml`, or `canvas_notes.json` to version control (they are ignored in `.gitignore`).
2. **Student Privacy (FERPA)**: This dashboard runs 100% locally on your machine. No student data, grades, or assignments are uploaded to any third-party server or cloud dashboard.
3. **Local File Permissions**: On macOS and Linux, ensure your `.env` file has restricted permissions:
   ```bash
   chmod 600 .env
   ```

---

## 🛠️ Development & Debugging Setup

For contributors or developers who want to run the project from source, modify code, or debug features:

### 1. Prerequisites
* **Python 3.11+** installed (`python3 --version`).
* **Git** installed.
* *(Optional)* **WeasyPrint System Dependencies** (required only if generating PDF exports):
  * **macOS**: `brew install pango`
  * **Ubuntu/Debian**: `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`
  * **Windows**: Follow [WeasyPrint Windows install guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows).

### 2. Clone and Setup Virtual Environment
```bash
# 1. Clone the repository
git clone https://github.com/ssjw/canvas_tracker.git
cd canvas_tracker

# 2. Create an isolated virtual environment
python3 -m venv .venv

# 3. Activate the virtual environment
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Environment Setup
Copy the example environment file and configure your credentials:
```bash
cp .env.example .env
```
Edit `.env` to include your Canvas details:
```ini
CANVAS_BASE_URL=https://aacps.instructure.com
CANVAS_API_TOKEN=your_canvas_api_token_here
```

### 4. Running During Development

* **Interactive Streamlit Web Dashboard** (Hot-reloads automatically when code changes):
  ```bash
  streamlit run canvas_tracker.py
  ```
  *(To run on a specific port: `streamlit run canvas_tracker.py --server.port 8501`)*

* **Command-Line (CLI) Mode**:
  ```bash
  python canvas_tracker.py
  ```

* **CLI Grades-Only Mode** (Fast pass, skips fetching detailed assignment groups):
  ```bash
  python canvas_tracker.py --grades-only
  ```

### 5. Running Automated Unit Tests
The project includes a comprehensive suite of unit tests covering calculations, data fetching, grade simulation, and notes backends.

Run the test suite:
```bash
python -m unittest discover -v
```

* **`test_canvas_gradebook.py`**: Validates course grade calculations, What-If simulation accuracy, category weight normalization, grade drag/boost impact scores, and assignment group classifiers.
* **`test_canvas_notes.py`**: Tests pluggable storage backends (Local JSON / browser localStorage fallback, Google Sheets Web App sync, note deletion, export utility, and Google Apps Script template validation).

### 6. Debugging & Inspection Tips

* **Streamlit Session State**: Streamlit stores interactive session state (selected student, active course, what-if overrides, active copy triggers) in `st.session_state`. You can inspect state live in the app by adding a temporary debug expander:
  ```python
  with st.sidebar.expander("🔍 Debug State"):
      st.write(st.session_state)
  ```
* **Client-Side Clipboard & UI Verification**: For debugging client-side interactions (like the 1-click clipboard copy or `st.data_editor` click triggers), use Chrome DevTools Protocol (CDP) or inspect browser console logs (`[Canvas Tracker] Copied via navigator.clipboard: ...`).
* **Offline Mock Testing**: The unit tests demonstrate how to mock Canvas API responses using `unittest.mock.patch` for fast, offline testing without calling live Canvas endpoints.

### 7. Building Portable Bundles Locally or in CI

The portable bundles are assembled automatically by GitHub Actions in [`.github/workflows/build-portable.yml`](.github/workflows/build-portable.yml):
* **Automatic Release Builds**: Pushing any tag starting with `v*` (e.g. `git tag v1.0.0 && git push origin v1.0.0`) triggers cross-platform builds for Windows, macOS Apple Silicon, and macOS Intel, attaching the `.zip` packages to a new GitHub Release.
* **Manual CI Trigger**: You can also trigger a build run manually from the GitHub **Actions** tab via `workflow_dispatch`.
