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
    attach_grade_impacts,
    compute_category_health,
    simulate_course_grade,
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

    def test_pending_review_excluded(self):
        # Row with provisional score but pending_review workflow_state or UNGRADED status must be excluded
        rows = [
            {"group_id": 1, "score": 10, "points_possible": 10, "status": "GRADED", "workflow_state": "graded"},
            {"group_id": 1, "score": 4, "points_possible": 10, "status": "UNGRADED", "workflow_state": "pending_review"},
        ]
        group_weights = {1: 100.0}
        res_weighted = compute_overall(rows, weighted=True, group_weights=group_weights)
        self.assertAlmostEqual(res_weighted, 100.0)

        res_unweighted = compute_overall(rows, weighted=False)
        self.assertAlmostEqual(res_unweighted, 100.0)

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

    def test_pending_review(self):
        # Even if provisional score/grade is present, pending_review evaluates as UNGRADED
        sub = {
            "submitted_at": "2026-05-01T12:00:00Z",
            "grade": "4",
            "score": 4.0,
            "missing": False,
            "workflow_state": "pending_review"
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
                "user_id": 100,
                "score": 4.0,
                "grade": "4",
                "missing": False,
                "workflow_state": "pending_review",
                "assignment": {
                    "id": 4,
                    "name": "Exit Ticket",
                    "due_at": "2026-04-15T23:59:59Z",
                    "points_possible": 10,
                    "assignment_group_id": 50,
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
        self.assertEqual(len(rows), 3)

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
        self.assertEqual(r2["type"], "Assessments")
        self.assertEqual(r2["score_str"], "Excused")
        self.assertEqual(r2["contribution_str"], "—")
        self.assertTrue(r2["omit_from_final_grade"])

        # Row 3 check (Pending Review)
        r3 = rows[2]
        self.assertEqual(r3["title"], "Exit Ticket")
        self.assertEqual(r3["status"], "UNGRADED")
        self.assertEqual(r3["status_display"], "UNGRADED (Pending Review)")
        self.assertEqual(r3["score_str"], "4/10")
        self.assertIsNone(r3["contribution"])
        self.assertEqual(r3["contribution_str"], "—")

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
    """Test group name to assignment category classification aligning with weight groups."""

    def test_classifications(self):
        # Directly preserves course weight group names
        self.assertEqual(classify_assignment_type("Major Assignments"), "Major Assignments")
        self.assertEqual(classify_assignment_type("Minor Assignments"), "Minor Assignments")
        self.assertEqual(classify_assignment_type("Quarterly Assessments"), "Quarterly Assessments")
        self.assertEqual(classify_assignment_type("Assessments"), "Assessments")
        self.assertEqual(classify_assignment_type("Classwork"), "Classwork")
        self.assertEqual(classify_assignment_type("Homework"), "Homework")
        self.assertEqual(classify_assignment_type("HW"), "Homework")
        self.assertEqual(classify_assignment_type(""), "Class Assignment")
        self.assertEqual(classify_assignment_type(None), "Class Assignment")

        # Handles ungraded detection
        self.assertEqual(classify_assignment_type("Ungraded"), "Ungraded")
        self.assertEqual(classify_assignment_type("Not Graded"), "Ungraded")
        self.assertEqual(classify_assignment_type("Non-Graded"), "Ungraded")
        self.assertEqual(classify_assignment_type("Major Assignments", {"grading_type": "not_graded"}), "Ungraded")
        self.assertEqual(classify_assignment_type("Minor Assignments", {"submission_types": ["not_graded"]}), "Ungraded")
        self.assertEqual(classify_assignment_type(None, {"points_possible": 0}), "Ungraded")


class TestAttachGradeImpacts(unittest.TestCase):
    """Test grade drag and potential recovery calculations."""

    def test_weighted_drag_and_gain(self):
        # Group 1 (wt 60): Test 1 (30/50), Test 2 (45/50) -> total 75/100 (75%)
        # Group 2 (wt 40): HW 1 (10/10), HW 2 (10/10) -> total 20/20 (100%)
        # Overall grade = 60 * 0.75 + 40 * 1.0 = 85.0%
        rows = [
            {"assignment_id": 1, "group_id": 1, "group_weight": 60.0, "score": 30.0, "points_possible": 50.0},
            {"assignment_id": 2, "group_id": 1, "group_weight": 60.0, "score": 45.0, "points_possible": 50.0},
            {"assignment_id": 3, "group_id": 2, "group_weight": 40.0, "score": 10.0, "points_possible": 10.0},
            {"assignment_id": 4, "group_id": 2, "group_weight": 40.0, "score": 10.0, "points_possible": 10.0},
        ]
        group_weights = {1: 60.0, 2: 40.0}
        attach_grade_impacts(rows, weighted=True, group_weights=group_weights)

        # Test 1 (30/50) was dragging down the grade:
        # Without Test 1: Test group has 45/50 = 90%. Grade = 60 * 0.9 + 40 * 1.0 = 94.0%.
        # Drag = 85.0 - 94.0 = -9.0%
        self.assertAlmostEqual(rows[0]["grade_drag"], -9.0)
        self.assertEqual(rows[0]["grade_drag_str"], "-9.00%")
        self.assertIn("▼", rows[0]["grade_impact_display"])
        # Potential gain if full credit (50/50):
        # Test group would be 95/100 = 95%. Grade = 60 * 0.95 + 40 = 97.0%.
        # Potential gain = 97.0 - 85.0 = +12.0%
        self.assertAlmostEqual(rows[0]["potential_gain"], 12.0)
        self.assertEqual(rows[0]["potential_gain_str"], "+12.00%")

        # Test 2 (45/50 = 90%) was boosting the grade compared to without it:
        # Without Test 2: Test group is 30/50 = 60%. Grade = 60 * 0.6 + 40 = 76.0%.
        # Drag/boost = 85.0 - 76.0 = +9.0%
        self.assertAlmostEqual(rows[1]["grade_drag"], 9.0)
        self.assertEqual(rows[1]["grade_drag_str"], "+9.00%")
        self.assertIn("▲", rows[1]["grade_impact_display"])

        # Perfect HW 1 (10/10): potential gain is 0
        self.assertAlmostEqual(rows[2]["potential_gain"], 0.0)

    def test_unweighted_drag_and_gain(self):
        # 3 assignments: 10/10, 10/10, 0/20 -> total 20/40 = 50.0%
        rows = [
            {"assignment_id": 1, "score": 10.0, "points_possible": 10.0},
            {"assignment_id": 2, "score": 10.0, "points_possible": 10.0},
            {"assignment_id": 3, "score": 0.0, "points_possible": 20.0},
        ]
        attach_grade_impacts(rows, weighted=False)
        # Without the 0/20 item: 20/20 = 100.0%.
        # Drag = 50.0 - 100.0 = -50.0%
        self.assertAlmostEqual(rows[2]["grade_drag"], -50.0)
        # Potential gain if 20/20: total 40/40 = 100.0% -> +50.0%
        self.assertAlmostEqual(rows[2]["potential_gain"], 50.0)


class TestComputeCategoryHealth(unittest.TestCase):
    """Test category breakdown aggregation and health metrics."""

    def test_category_health_aggregation(self):
        rows = [
            {"group_id": 1, "group_name": "Assessments", "group_weight": 60.0, "score": 35.0, "points_possible": 50.0, "status": "GRADED"},
            {"group_id": 2, "group_name": "Homework", "group_weight": 40.0, "score": 20.0, "points_possible": 20.0, "status": "GRADED"},
        ]
        group_weights = {1: 60.0, 2: 40.0}
        groups_by_id = {
            1: {"name": "Assessments", "group_weight": 60.0},
            2: {"name": "Homework", "group_weight": 40.0},
            3: {"name": "Quarterly Exam", "group_weight": 10.0},  # Inactive group
        }
        cats = compute_category_health(rows, weighted=True, group_weights=group_weights, groups_by_id=groups_by_id)
        
        # Overall grade: 60 * 0.70 + 40 * 1.0 = 42 + 40 = 82.0%
        self.assertEqual(len(cats), 3)
        assess = next(c for c in cats if c["group_id"] == 1)
        self.assertEqual(assess["name"], "Assessments")
        self.assertAlmostEqual(assess["score_pct"], 70.0)
        self.assertTrue(assess["is_active"])
        # Drag vs overall: 70.0 - 82.0 = -12.0%
        self.assertAlmostEqual(assess["drag_vs_overall"], -12.0)

        inactive = next(c for c in cats if c["group_id"] == 3)
        self.assertFalse(inactive["is_active"])
        self.assertIsNone(inactive["score_pct"])


class TestSimulateCourseGrade(unittest.TestCase):
    """Test what-if grade simulation."""

    def test_simulation_override(self):
        rows = [
            {"assignment_id": 101, "group_id": 1, "group_weight": 50.0, "score": 25.0, "points_possible": 50.0},
            {"assignment_id": 102, "group_id": 2, "group_weight": 50.0, "score": 50.0, "points_possible": 50.0},
        ]
        group_weights = {1: 50.0, 2: 50.0}
        # Initial: 50 * 0.5 + 50 * 1.0 = 75.0%
        initial = simulate_course_grade(rows, weighted=True, group_weights=group_weights)
        self.assertAlmostEqual(initial, 75.0)

        # Retake assignment 101 to 45/50 (90%) -> 50 * 0.9 + 50 = 95.0%
        simulated = simulate_course_grade(rows, weighted=True, group_weights=group_weights, score_overrides={101: 45.0})
        self.assertAlmostEqual(simulated, 95.0)


class TestModuleImport(unittest.TestCase):
    """Verify clean module import with expected symbol availability."""

    def test_imports(self):
        self.assertTrue(callable(canvas_gradebook.compute_contribution))
        self.assertTrue(callable(canvas_gradebook.compute_overall))
        self.assertTrue(callable(canvas_gradebook.attach_grade_impacts))
        self.assertTrue(callable(canvas_gradebook.compute_category_health))
        self.assertTrue(callable(canvas_gradebook.simulate_course_grade))
        self.assertTrue(callable(canvas_gradebook.status_for))
        self.assertTrue(callable(canvas_gradebook.build_gradebook_rows))
        self.assertTrue(callable(canvas_gradebook.sort_gradebook_rows))
        self.assertTrue(callable(canvas_gradebook.display_gradebook))
        self.assertTrue(callable(canvas_gradebook.main))


if __name__ == "__main__":
    unittest.main()
