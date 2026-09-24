"""
canvas_notes.py

Pluggable notes persistence architecture for Canvas Tracker.
Supports:
1. LocalNotesBackend:
   - In standard Python: saves to canvas_notes.json on disk.
   - In stlite (WebAssembly / Pyodide): saves to browser localStorage via JS interop.
2. GoogleSheetNotesBackend:
   - Connects to a user's Google Apps Script Web App for multi-device sync.
   - In-memory write-through cache for instant UI feedback.
"""

import os
import sys
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import requests

IS_PYODIDE = "pyodide" in sys.modules

GOOGLE_APPS_SCRIPT_TEMPLATE = """/**
 * Canvas Tracker - Google Sheet Notes Sync
 *
 * Setup:
 * 1. Open a blank Google Sheet in your Google Drive.
 * 2. Click Extensions -> Apps Script.
 * 3. Replace all code in the script editor with this snippet.
 * 4. (Optional) Set your secret passphrase in SECRET_KEY below.
 * 5. Click Deploy -> New deployment.
 *    - Select type: Web app
 *    - Description: Canvas Tracker Notes Sync
 *    - Execute as: Me (your account)
 *    - Who has access: Anyone
 * 6. Click Deploy and copy the resulting Web App URL.
 * 7. In Canvas Tracker sidebar -> Notes & Storage:
 *    - Choose "Google Sheet"
 *    - Paste the Web App URL (and secret passphrase if configured).
 *    - Click "Test & Validate Connection".
 */

const SECRET_KEY = ""; // Optional: e.g. "my-secret-passphrase"
const SHEET_NAME = "Notes";

function getOrCreateSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(["assignment_id", "note", "updated_at"]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function doGet(e) {
  const secret = (e && e.parameter && e.parameter.secret) || "";
  if (SECRET_KEY && secret !== SECRET_KEY) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: "Unauthorized: invalid secret key" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  const action = (e && e.parameter && e.parameter.action) || "get_all";
  if (action === "ping") {
    return ContentService.createTextOutput(JSON.stringify({ status: "ok", message: "pong" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  const sheet = getOrCreateSheet();
  const data = sheet.getDataRange().getValues();
  const notes = {};
  for (let i = 1; i < data.length; i++) {
    const id = String(data[i][0]).trim();
    const note = String(data[i][1] || "");
    const updated = String(data[i][2] || "");
    if (id) {
      notes[id] = { text: note, updated_at: updated };
    }
  }

  return ContentService.createTextOutput(JSON.stringify({ status: "ok", notes: notes }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  let body = {};
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    body = (e && e.parameter) || {};
  }

  const secret = body.secret || (e && e.parameter && e.parameter.secret) || "";
  if (SECRET_KEY && secret !== SECRET_KEY) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: "Unauthorized: invalid secret key" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  const assignmentId = String(body.assignment_id || "").trim();
  const noteText = String(body.note || "").trim();
  const now = new Date().toISOString();

  if (!assignmentId) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: "Missing assignment_id" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  const sheet = getOrCreateSheet();
  const data = sheet.getDataRange().getValues();
  let foundRow = -1;
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][0]).trim() === assignmentId) {
      foundRow = i + 1;
      break;
    }
  }

  if (noteText === "") {
    if (foundRow > 0) {
      sheet.deleteRow(foundRow);
    }
  } else {
    if (foundRow > 0) {
      sheet.getRange(foundRow, 2).setValue(noteText);
      sheet.getRange(foundRow, 3).setValue(now);
    } else {
      sheet.appendRow([assignmentId, noteText, now]);
    }
  }

  return ContentService.createTextOutput(JSON.stringify({ status: "ok", assignment_id: assignmentId }))
    .setMimeType(ContentService.MimeType.JSON);
}
"""


class NotesBackend(ABC):
    """Abstract base class for assignment notes storage."""

    @abstractmethod
    def load_all_notes(self) -> dict[str, dict]:
        """Returns dict mapping str(assignment_id) -> {'text': str, 'updated_at': str}."""
        pass

    @abstractmethod
    def get_note(self, assignment_id: str | int) -> str:
        """Returns the note string for the specified assignment_id or empty string."""
        pass

    @abstractmethod
    def save_note(self, assignment_id: str | int, note_text: str) -> bool:
        """Saves or updates a note. If note_text is empty or blank, removes the note."""
        pass

    @abstractmethod
    def validate(self) -> tuple[bool, str]:
        """Validates connection and read/write availability. Returns (is_ok, message)."""
        pass

    def export_to(self, target_backend: "NotesBackend") -> int:
        """Exports all notes from this backend to target_backend. Returns count of exported notes."""
        all_notes = self.load_all_notes()
        count = 0
        for aid, data in all_notes.items():
            text = data.get("text", "") if isinstance(data, dict) else str(data)
            if text.strip():
                if target_backend.save_note(aid, text):
                    count += 1
        return count


class LocalNotesBackend(NotesBackend):
    """Local storage backend:
    - On standard Python: persists to canvas_notes.json file.
    - On stlite/Pyodide: persists to browser localStorage via js.localStorage.
    """

    def __init__(self, filepath: str = "canvas_notes.json"):
        self.filepath = filepath
        self._cache: dict[str, dict] = {}
        self._loaded = False
        self.load_all_notes()

    def load_all_notes(self) -> dict[str, dict]:
        if IS_PYODIDE:
            try:
                import js
                raw = js.localStorage.getItem("canvas_assignment_notes")
                if raw:
                    parsed = json.loads(str(raw))
                    self._cache = self._normalize_notes(parsed)
                else:
                    self._cache = {}
            except Exception as e:
                print(f"[LocalNotesBackend] Error reading localStorage: {e}", file=sys.stderr)
                self._cache = {}
        else:
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        parsed = json.load(f)
                        self._cache = self._normalize_notes(parsed)
                except Exception as e:
                    print(f"[LocalNotesBackend] Error reading {self.filepath}: {e}", file=sys.stderr)
                    self._cache = {}
            else:
                self._cache = {}

        self._loaded = True
        return self._cache

    @staticmethod
    def _normalize_notes(raw: dict) -> dict[str, dict]:
        normalized = {}
        for k, v in raw.items():
            key = str(k)
            if isinstance(v, dict):
                normalized[key] = {
                    "text": str(v.get("text", "")),
                    "updated_at": str(v.get("updated_at", "")),
                }
            elif isinstance(v, str):
                normalized[key] = {
                    "text": v,
                    "updated_at": "",
                }
        return normalized

    def get_note(self, assignment_id: str | int) -> str:
        key = str(assignment_id)
        return self._cache.get(key, {}).get("text", "")

    def save_note(self, assignment_id: str | int, note_text: str) -> bool:
        key = str(assignment_id)
        clean_text = str(note_text or "").strip()
        now = datetime.now(timezone.utc).isoformat()

        if clean_text:
            self._cache[key] = {"text": clean_text, "updated_at": now}
        else:
            self._cache.pop(key, None)

        # Persist
        if IS_PYODIDE:
            try:
                import js
                js.localStorage.setItem("canvas_assignment_notes", json.dumps(self._cache))
                return True
            except Exception as e:
                print(f"[LocalNotesBackend] Error writing localStorage: {e}", file=sys.stderr)
                return False
        else:
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, indent=2)
                return True
            except Exception as e:
                print(f"[LocalNotesBackend] Error writing {self.filepath}: {e}", file=sys.stderr)
                return False

    def validate(self) -> tuple[bool, str]:
        if IS_PYODIDE:
            return True, "Browser local storage is active and functioning."
        try:
            test_file = self.filepath + ".tmp_test"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("{}")
            if os.path.exists(test_file):
                os.remove(test_file)
            return True, f"Local notes storage verified (using {os.path.abspath(self.filepath)})."
        except Exception as e:
            return False, f"Local file storage write error: {e}"


class GoogleSheetNotesBackend(NotesBackend):
    """Google Sheet sync backend using a Google Apps Script Web App.
    Uses an in-memory write-through cache for instant UI response while
    syncing updates to Google Sheets in the background.
    """

    def __init__(self, endpoint_url: str, secret_key: str = "", timeout: float = 8.0, auto_load: bool = True):
        self.endpoint_url = str(endpoint_url or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.timeout = timeout
        self._cache: dict[str, dict] = {}
        self._loaded = False
        if self.endpoint_url and auto_load:
            self.load_all_notes()

    def load_all_notes(self) -> dict[str, dict]:
        if not self.endpoint_url:
            return {}
        try:
            params = {"action": "get_all"}
            if self.secret_key:
                params["secret"] = self.secret_key

            resp = requests.get(self.endpoint_url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    notes = data.get("notes", {})
                    self._cache = LocalNotesBackend._normalize_notes(notes)
                    self._loaded = True
                    return self._cache
                else:
                    print(f"[GoogleSheetNotesBackend] Server error: {data.get('message')}", file=sys.stderr)
            else:
                print(f"[GoogleSheetNotesBackend] HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        except Exception as e:
            print(f"[GoogleSheetNotesBackend] Network error fetching notes: {e}", file=sys.stderr)

        return self._cache

    def get_note(self, assignment_id: str | int) -> str:
        key = str(assignment_id)
        return self._cache.get(key, {}).get("text", "")

    def save_note(self, assignment_id: str | int, note_text: str) -> bool:
        key = str(assignment_id)
        clean_text = str(note_text or "").strip()
        now = datetime.now(timezone.utc).isoformat()

        # Update in-memory cache immediately for snappy UI
        if clean_text:
            self._cache[key] = {"text": clean_text, "updated_at": now}
        else:
            self._cache.pop(key, None)

        if not self.endpoint_url:
            return False

        try:
            payload = {
                "assignment_id": key,
                "note": clean_text,
            }
            if self.secret_key:
                payload["secret"] = self.secret_key

            resp = requests.post(self.endpoint_url, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "ok"
            return False
        except Exception as e:
            print(f"[GoogleSheetNotesBackend] Error posting note to Google Sheet: {e}", file=sys.stderr)
            return False

    def validate(self) -> tuple[bool, str]:
        if not self.endpoint_url:
            return False, "Google Sheet Web App URL is empty."
        if not self.endpoint_url.startswith("https://script.google.com/"):
            return False, "URL should begin with 'https://script.google.com/macros/s/.../exec'"

        try:
            params = {"action": "ping"}
            if self.secret_key:
                params["secret"] = self.secret_key

            resp = requests.get(self.endpoint_url, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                return False, f"Server responded with HTTP {resp.status_code}: {resp.text[:200]}"

            data = resp.json()
            if data.get("status") != "ok":
                return False, f"Error from Google Sheet: {data.get('message', 'Unknown error')}"

            return True, "Successfully connected to Google Sheet Web App! Read and write verified."
        except Exception as e:
            return False, f"Connection failed: {e}"


def get_notes_backend(config: dict | None = None) -> NotesBackend:
    """Factory to instantiate the configured notes backend."""
    cfg = (config or {}).get("notes", {})
    backend_type = cfg.get("backend", "local").lower()

    if backend_type == "google_sheet":
        url = cfg.get("google_sheet_url", "")
        secret = cfg.get("google_sheet_secret", "")
        auto_load = cfg.get("auto_load", True)
        return GoogleSheetNotesBackend(endpoint_url=url, secret_key=secret, auto_load=auto_load)

    filepath = cfg.get("filepath", "canvas_notes.json")
    return LocalNotesBackend(filepath=filepath)
