#!/usr/bin/env python3
"""
Unit tests for Canvas Tracker concurrency improvements, in-memory group grade calculation,
and automatic per_page=100 pagination handling.
"""

import unittest
from unittest.mock import MagicMock, patch
import canvas_tracker
from canvas_tracker import (
    calculate_assignment_group_grades,
    fetch_all,
)


class TestCalculateAssignmentGroupGrades(unittest.TestCase):
    def test_in_memory_calculation(self):
        course_name = "1(A) AP Physics"
        student_ids = [101, 102]
        ags = [
            {"id": 1, "name": "Assessments"},
            {"id": 2, "name": "Homework"},
        ]
        submissions = [
            {
                "user_id": 101,
                "score": 45,
                "grading_period_id": 501,
                "assignment": {
                    "id": 10,
                    "assignment_group_id": 1,
                    "points_possible": 50,
                    "omit_from_final_grade": False,
                }
            },
            {
                "user_id": 101,
                "score": 50,
                "grading_period_id": 501,
                "assignment": {
                    "id": 11,
                    "assignment_group_id": 1,
                    "points_possible": 50,
                    "omit_from_final_grade": False,
                }
            },
            {
                "user_id": 102,
                "score": 40,
                "grading_period_id": 501,
                "assignment": {
                    "id": 10,
                    "assignment_group_id": 1,
                    "points_possible": 50,
                    "omit_from_final_grade": False,
                }
            },
        ]

        results = calculate_assignment_group_grades(course_name, student_ids, submissions, ags, gp_id=501)
        self.assertEqual(len(results), 2)
        # Student 101: (45 + 50) / (50 + 50) = 95.0%
        s101 = next(r for r in results if r["student_id"] == 101)
        self.assertEqual(s101["score"], 95.0)
        self.assertEqual(s101["group_name"], "Assessments")

        # Student 102: 40 / 50 = 80.0%
        s102 = next(r for r in results if r["student_id"] == 102)
        self.assertEqual(s102["score"], 80.0)

    def test_omit_from_final_grade_and_excused(self):
        course_name = "2(B) Chemistry"
        student_ids = [101]
        ags = [{"id": 1, "name": "Unit Tests"}]
        submissions = [
            {
                "user_id": 101,
                "score": 10,
                "grading_period_id": None,
                "assignment": {
                    "id": 1,
                    "assignment_group_id": 1,
                    "points_possible": 100,
                    "omit_from_final_grade": True,
                }
            },
            {
                "user_id": 101,
                "score": 50,
                "excused": True,
                "grading_period_id": None,
                "assignment": {
                    "id": 2,
                    "assignment_group_id": 1,
                    "points_possible": 100,
                    "omit_from_final_grade": False,
                }
            },
            {
                "user_id": 101,
                "score": 90,
                "excused": False,
                "grading_period_id": None,
                "assignment": {
                    "id": 3,
                    "assignment_group_id": 1,
                    "points_possible": 100,
                    "omit_from_final_grade": False,
                }
            },
        ]

        results = calculate_assignment_group_grades(course_name, student_ids, submissions, ags, gp_id=None)
        self.assertEqual(len(results), 1)
        # Only assignment 3 should be counted: 90 / 100 = 90.0%
        self.assertEqual(results[0]["score"], 90.0)


class TestFetchAllPerPage(unittest.TestCase):
    def test_per_page_default(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.json.return_value = [{"id": 1}, {"id": 2}]
        mock_session.get.return_value = mock_response

        fetch_all(mock_session, "https://canvas.example.com/api/v1/courses")
        mock_session.get.assert_called_once_with(
            "https://canvas.example.com/api/v1/courses",
            params={"per_page": 100},
            timeout=15
        )

    def test_per_page_preserve_custom(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.json.return_value = [{"id": 1}]
        mock_session.get.return_value = mock_response

        fetch_all(mock_session, "https://canvas.example.com/api/v1/courses", params={"per_page": 50, "other": "test"})
        mock_session.get.assert_called_once_with(
            "https://canvas.example.com/api/v1/courses",
            params={"per_page": 50, "other": "test"},
            timeout=15
        )


if __name__ == "__main__":
    unittest.main()
