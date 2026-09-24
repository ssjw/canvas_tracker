"""
test_canvas_notes.py

Unit tests for pluggable notes persistence backends (LocalNotesBackend and GoogleSheetNotesBackend).
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from canvas_notes import (
    LocalNotesBackend,
    GoogleSheetNotesBackend,
    get_notes_backend,
    GOOGLE_APPS_SCRIPT_TEMPLATE,
)


class TestLocalNotesBackend(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.notes_file = os.path.join(self.temp_dir.name, "test_notes.json")
        self.backend = LocalNotesBackend(filepath=self.notes_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_get_note(self):
        self.assertEqual(self.backend.get_note(101), "")
        self.backend.save_note(101, "Need to talk to teacher")
        self.assertEqual(self.backend.get_note(101), "Need to talk to teacher")
        self.assertEqual(self.backend.get_note("101"), "Need to talk to teacher")

        # Persistence across reload
        reloaded = LocalNotesBackend(filepath=self.notes_file)
        self.assertEqual(reloaded.get_note(101), "Need to talk to teacher")

    def test_delete_note_on_empty_string(self):
        self.backend.save_note(102, "Some remark")
        self.assertEqual(self.backend.get_note(102), "Some remark")

        self.backend.save_note(102, "   ")
        self.assertEqual(self.backend.get_note(102), "")

        reloaded = LocalNotesBackend(filepath=self.notes_file)
        self.assertEqual(reloaded.get_note(102), "")

    def test_validate(self):
        ok, msg = self.backend.validate()
        self.assertTrue(ok)
        self.assertIn("verified", msg.lower())

    def test_export_to(self):
        self.backend.save_note(1, "Note 1")
        self.backend.save_note(2, "Note 2")

        target_file = os.path.join(self.temp_dir.name, "target_notes.json")
        target_backend = LocalNotesBackend(filepath=target_file)
        count = self.backend.export_to(target_backend)
        self.assertEqual(count, 2)
        self.assertEqual(target_backend.get_note(1), "Note 1")
        self.assertEqual(target_backend.get_note(2), "Note 2")


class TestGoogleSheetNotesBackend(unittest.TestCase):
    @patch("requests.get")
    def test_load_all_notes(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "notes": {
                "201": {"text": "Study chapter 3", "updated_at": "2026-09-20T00:00:00Z"},
                "202": "Turn in late pass",
            },
        }
        mock_get.return_value = mock_resp

        backend = GoogleSheetNotesBackend(endpoint_url="https://script.google.com/macros/s/test/exec", secret_key="abc")
        self.assertEqual(backend.get_note(201), "Study chapter 3")
        self.assertEqual(backend.get_note(202), "Turn in late pass")

    @patch("requests.post")
    def test_save_note(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok", "assignment_id": "201"}
        mock_post.return_value = mock_resp

        backend = GoogleSheetNotesBackend(endpoint_url="https://script.google.com/macros/s/test/exec")
        success = backend.save_note(201, "Submitted today")
        self.assertTrue(success)
        self.assertEqual(backend.get_note(201), "Submitted today")

    @patch("requests.get")
    def test_validate_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok", "message": "pong"}
        mock_get.return_value = mock_resp

        backend = GoogleSheetNotesBackend(endpoint_url="https://script.google.com/macros/s/test/exec")
        ok, msg = backend.validate()
        self.assertTrue(ok)
        self.assertIn("verified", msg.lower())

    def test_validate_bad_url(self):
        backend = GoogleSheetNotesBackend(endpoint_url="https://invalid.com/api")
        ok, msg = backend.validate()
        self.assertFalse(ok)
        self.assertIn("https://script.google.com/", msg)


class TestFactoryAndTemplate(unittest.TestCase):
    def test_factory_local(self):
        b = get_notes_backend({"notes": {"backend": "local"}})
        self.assertIsInstance(b, LocalNotesBackend)

    def test_factory_google_sheet(self):
        b = get_notes_backend({"notes": {"backend": "google_sheet", "google_sheet_url": "https://script.google.com/test", "auto_load": False}})
        self.assertIsInstance(b, GoogleSheetNotesBackend)

    def test_template_present(self):
        self.assertIn("function doGet", GOOGLE_APPS_SCRIPT_TEMPLATE)
        self.assertIn("function doPost", GOOGLE_APPS_SCRIPT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
