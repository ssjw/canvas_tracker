#!/usr/bin/env python3
"""
Unit tests for Canvas Gradebook CLI pure calculation and formatting routines.

All tests run without network access using deterministic mock dictionaries.
"""

import os
import unittest
from datetime import datetime, timezone, timedelta

# Guard: must set mock environment variables prior to importing canvas_gradebook / canvas_tracker
os.environ["CANVAS_API_TOKEN"] = "test-token-12345"
os.environ["CANVAS_BASE_URL"] = "https://test.instructure.com"

import canvas_gradebook
from canvas_gradebook import (
    compute_contribution,
    compute_overall,
    status_for,
    build_gradebook_rows,
    sort_gradebook_rows,
    classify_assignment_type,
)


class TestComputeContribution(unittest.TestCase):
    """Test individual assignment contribution math."""

    def test_weighted_standard(self):
        # 9/10 in a 20% group = 18.0 percentage points of the final 100
        # (0.9 * 20% group weight; a perfect score would contribute the full 20)
        res = compute_contribution(9, 10, 20, weighted=True)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, 18.0)

        # 8.5/10 in a 30% group = 25.5 percentage points
        res2 = compute_contribution(8.5, 10, 30, weighted=True)
        self.assertIsNotNone(res2)
        self.assertAlmostEqual(res2, 25.5)

        # 100/100 in 50% group = 50.0
        res3 = compute_contribution(100, 100, 50, weighted=True)
        self.assertAlmostEqual(res3, 50.0)

        # 0/10 in 20% group = 0.0
        res4 = compute_contribution(0, 10, 20, weighted=True)
        self.assertAlmostEqual(res4, 0.0)

    def test_unweighted_standard(self):
        # 9/10 earned = 90.0%
        res = compute_contribution(9, 10, None, weighted=False)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, 90.0)

        # Group weight is ignored when unweighted
        res2 = compute_contribution(8.5, 10, 25, weighted=False)
        self.assertAlmostEqual(res2, 85.0)

        res3 = compute_contribution(0, 10, None, weighted=False)
        self.assertAlmostEqual(res3, 0.0)

    def test_none_and_zero_cases(self):
        # Missing score
        self.assertIsNone(compute_contribution(None, 10, 20, weighted=True))
        self.assertIsNone(compute_contribution(None, 10, None, weighted=False))

        # Missing points_possible
        self.assertIsNone(compute_contribution(9, None, 20, weighted=True))

        # Zero or negative points_possible
        self.assertIsNone(compute_contribution(9, 0, 20, weighted=True))
        self.assertIsNone(compute_contribution(9, -10, 20, weighted=True))
        self.assertIsNone(compute_contribution(9, 0, None, weighted=False))

        # Missing group_weight in weighted mode
        self.assertIsNone(compute_contribution(9, 10, None, weighted=True))


class TestComputeOverall(unittest.TestCase):
    """Test overall course grade aggregation."""

    def test_weighted_overall(self):
        # Group 1 (wt=30): 9/10 + 10/10 = 19/20 = 95%
        # Group 2 (wt=70): 80/100 = 80%
        # Overall = (30 * 0.95 + 70 * 0.80) / 100 * 100 = 28.5 + 56.0 = 84.5%
        rows = [
            {"group_id": 1, "score": 9, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
            {"group_id": 1, "score": 10, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
            {"group_id": 2, "score": 80, "points_possible": 100, "omit_from_final_grade": False, "excused": False},
        ]
        group_weights = {1: 30.0, 2: 70.0}
        res = compute_overall(rows, weighted=True, group_weights=group_weights)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, 84.5)

    def test_weighted_row_weight_fallback(self):
        # Weights supplied on rows directly
        rows = [
            {"group_id": 10, "group_weight": 40.0, "score": 10, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
            {"group_id": 20, "group_weight": 60.0, "score": 5, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
        ]
        # (40 * 1.0 + 60 * 0.5) / 100 * 100 = 70.0%
        res = compute_overall(rows, weighted=True)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, 70.0)

    def test_weighted_exclusions(self):
        # Omitted and excused assignments must not dilute or contribute to the score
        rows = [
            {"group_id": 1, "score": 9, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
            {"group_id": 1, "score": 0, "points_possible": 10, "omit_from_final_grade": True, "excused": False},
            {"group_id": 1, "score": 0, "points_possible": 10, "omit_from_final_grade": False, "excused": True},
            {"group_id": 2, "score": 80, "points_possible": 100, "omit_from_final_grade": False, "excused": False},
        ]
        group_weights = {1: 50.0, 2: 50.0}
        # Group 1: 9/10 = 0.9; Group 2: 80/100 = 0.8 -> (50*0.9 + 50*0.8) = 85.0%
        res = compute_overall(rows, weighted=True, group_weights=group_weights)
        self.assertAlmostEqual(res, 85.0)

    def test_unweighted_overall(self):
        # Sum: 9 + 18 = 27 / 30 = 90.0%
        rows = [
            {"score": 9, "points_possible": 10, "omit_from_final_grade": False, "excused": False},
            {"score": 18, "points_possible": 20, "omit_from_final_grade": False, "excused": False},
            {"score": 0, "points_possible": 50, "omit_from_final_grade": True, "excused": False},
            {"score": 0, "points_possible": 50, "omit_from_final_grade": False, "excused": True},
        ]
        res = compute_overall(rows, weighted=False)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res, 90.0)

    def test_empty_or_no_graded_data(self):
        self.assertIsNone(compute_overall([], weighted=True))
        self.assertIsNone(compute_overall([], weighted=False))
        self.assertIsNone(compute_overall([{"score": None, "points_possible": 10}], weighted=True))
        self.assertIsNone(compute_overall([{"score": None, "points_possible": 10}], weighted=False))


class TestStatusFor(unittest.TestCase):
    """Test assignment status derivations."""

    def test_graded(self):
        sub1 = {"grade": "9", "score": 9.0, "missing": False, "workflow_state": "graded"}
        self.assertEqual(status_for(sub1), "GRADED")

        sub2 = {"grade": "8.5", "score": 8.5, "missing": False, "workflow_state": "submitted"}
        self.assertEqual(status_for(sub2), "GRADED")

    def test_missing(self):
        # Missing flag
        sub1 = {"missing": True, "grade": None, "score": None, "workflow_state": "unsubmitted"}
        self.assertEqual(status_for(sub1), "MISSING")

        # Zero grade evaluates as MISSING
        sub2 = {"missing": False, "grade": "0", "score": 0, "workflow_state": "graded"}
        self.assertEqual(status_for(sub2), "MISSING")

        sub3 = {"missing": False, "grade": 0, "score": 0.0}
        self.assertEqual(status_for(sub3), "MISSING")

    def test_ungraded(self):
        sub = {
            "submitted_at": "2026-05-01T12:00:00Z",
            "grade": None,
            "score": None,
            "missing": False,
            "workflow_state": "submitted"
        }
        self.assertEqual(status_for(sub), "UNGRADED")

    def test_no_grade_past_due(self):
        past_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sub = {
            "due_at": past_date,
            "grade": None,
            "score": None,
            "missing": False,
            "workflow_state": "unsubmitted"
        }
        self.assertEqual(status_for(sub), "NO GRADE")

    def test_no_grade_undated(self):
        sub = {
            "due_at": None,
            "grade": None,
            "score": None,
            "missing": False,
            "workflow_state": "unsubmitted"
        }
        self.assertEqual(status_for(sub), "NO GRADE")

    def test_upcoming(self):
        future_date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sub = {
            "assignment": {"due_at": future_date},
            "grade": None,
            "score": None,
            "missing": False,
            "workflow_state": "unsubmitted"
        }
        self.assertEqual(status_for(sub), "UPCOMING")


class TestBuildAndSortRows(unittest.TestCase):
    """Test building and sorting gradebook rows."""

    def test_build_gradebook_rows(self):
        submissions = [
            {
                "user_id": 100,
                "score": 9.0,
                "grade": "9",
                "missing": False,
                "workflow_state": "graded",
                "assignment": {
                    "id": 1,
                    "name": "Homework 1",
                    "due_at": "2026-04-10T23:59:59Z",
                    "points_possible": 10,
                    "assignment_group_id": 50,
                    "omit_from_final_grade": False,
                }
            },
            {
                "user_id": 100,
                "score": None,
                "grade": None,
                "excused": True,
                "assignment": {
                    "id": 2,
                    "name": "Quiz 1",
                    "due_at": "2026-04-01T23:59:59Z",
                    "points_possible": 20,
                    "assignment_group_id": 51,
                    "omit_from_final_grade": True,
                }
            },
            {
                "user_id": 999,  # different student
                "score": 10.0,
                "assignment": {"id": 3, "name": "Other student HW"}
            }
        ]
        groups_by_id = {
            50: {"name": "Homework", "group_weight": 20.0},
            51: {"name": "Assessments", "group_weight": 80.0},
        }

        rows = build_gradebook_rows(submissions, groups_by_id, weighted=True, sid=100)
        self.assertEqual(len(rows), 2)

        # Row 1 check
        r1 = rows[0]
        self.assertEqual(r1["title"], "Homework 1")
        self.assertEqual(r1["type"], "Homework")
        self.assertEqual(r1["weight_str"], "20%")
        self.assertEqual(r1["score_str"], "9/10")
        self.assertEqual(r1["status"], "GRADED")
        self.assertAlmostEqual(r1["contribution"], 18.0)

        # Row 2 check (Excused + Omitted)
        r2 = rows[1]
        self.assertEqual(r2["title"], "Quiz 1")
        self.assertEqual(r2["type"], "Assessment")
        self.assertEqual(r2["score_str"], "Excused")
        self.assertEqual(r2["contribution_str"], "—")
        self.assertTrue(r2["omit_from_final_grade"])

    def test_sort_gradebook_rows(self):
        rows = [
            {"title": "Middle", "due_raw": "2026-04-10T00:00:00Z"},
            {"title": "Oldest", "due_raw": "2026-04-01T00:00:00Z"},
            {"title": "Undated", "due_raw": None},
            {"title": "Newest", "due_raw": "2026-05-01T00:00:00Z"},
        ]
        sorted_rows = sort_gradebook_rows(rows)
        titles = [r["title"] for r in sorted_rows]
        self.assertEqual(titles, ["Oldest", "Middle", "Newest", "Undated"])


class TestClassifyAssignmentType(unittest.TestCase):
    """Test group name to assignment category classification."""

    def test_classifications(self):
        self.assertEqual(classify_assignment_type("Homework 1"), "Homework")
        # Shared helper (canvas_tracker) only maps an exact "hw" match to Homework
        self.assertEqual(classify_assignment_type("Weekly HW"), "Weekly HW")
        self.assertEqual(classify_assignment_type("HW"), "Homework")
        self.assertEqual(classify_assignment_type("Unit 1 Assessment"), "Assessment")
        self.assertEqual(classify_assignment_type("Chapter Test"), "Assessment")
        self.assertEqual(classify_assignment_type("Pop Quiz"), "Assessment")
        self.assertEqual(classify_assignment_type("Quarterly Exam"), "Assessment")
        self.assertEqual(classify_assignment_type("SCLT 2"), "Assessment")
        self.assertEqual(classify_assignment_type("Classwork 3"), "Class Assignment")
        self.assertEqual(classify_assignment_type("Lab Practice"), "Class Assignment")
        self.assertEqual(classify_assignment_type(""), "Class Assignment")
        self.assertEqual(classify_assignment_type("Project Presentation"), "Project Presentation")


class TestModuleImport(unittest.TestCase):
    """Verify clean module import with expected symbol availability."""

    def test_imports(self):
        self.assertTrue(callable(canvas_gradebook.compute_contribution))
        self.assertTrue(callable(canvas_gradebook.compute_overall))
        self.assertTrue(callable(canvas_gradebook.status_for))
        self.assertTrue(callable(canvas_gradebook.build_gradebook_rows))
        self.assertTrue(callable(canvas_gradebook.sort_gradebook_rows))
        self.assertTrue(callable(canvas_gradebook.display_gradebook))
        self.assertTrue(callable(canvas_gradebook.main))


if __name__ == "__main__":
    unittest.main()
