#!/usr/bin/env python3
"""
Canvas Student Assignment & Grade Tracker

A lightweight Python CLI and Streamlit web dashboard to identify, aggregate,
and display uncompleted (missing, upcoming, and past due) assignments and course
grades for observed students using the Canvas LMS REST API.

Potential Security Implications:
1. Canvas API tokens provide full programmatic access to your account.
   Store them in environment variables, restricted .env files (chmod 600),
   or ~/.config/canvas_tracker/config.toml rather than hardcoding.
   Avoid committing secrets or student PII into version control.
2. Ensure SSL/TLS verification is enabled on all outbound requests.
"""

import os
import sys

# Ensure canvas_tracker is recognized in sys.modules when run directly as a script (e.g. by Streamlit)
if __name__ == "__main__" and "canvas_tracker" not in sys.modules:
    sys.modules["canvas_tracker"] = sys.modules[__name__]

import re
import json
import html
import copy
import argparse
from datetime import datetime, timezone, time, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

# Python 3.11+ standard library tomllib fallback
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# ---------------------------------------------------------------------------
# Configuration Loader (Hierarchical: Env > .env (upward) > config.toml > Defaults)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "canvas": {
        "base_url": "https://aacps.instructure.com",
        "api_token": None,
    },
    "refresh": {
        "interval_minutes": 60,
        "active_hours_start": "06:00",
        "active_hours_end": "20:00",
    },
    "notes": {
        "backend": "local",
        "filepath": "canvas_notes.json",
        "google_sheet_url": "",
        "google_sheet_secret": "",
    },
}


def load_config(config_path: str = None) -> dict:
    """Load configuration with clear precedence:
    Environment Variables > .env (recursively upward to root) > config.toml > Defaults.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)

    # 1. Traverse parent directories upward towards root looking for .env
    try:
        env_file = find_dotenv(usecwd=True)
        if not env_file:
            # Fallback: search upward starting from the script's directory
            env_file = find_dotenv(str(Path(__file__).resolve().parent / ".env"))
        if env_file:
            load_dotenv(env_file)
    except Exception:
        pass

    # 2. Search for config.toml in standard XDG path or local directory
    target_file = None
    if config_path:
        target_file = Path(config_path)
    else:
        xdg_home = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        candidates = [
            Path(xdg_home) / "canvas_tracker" / "config.toml",
            Path(xdg_home) / "canvas-tracker" / "config.toml",
            Path.home() / "Library" / "Application Support" / "canvas_tracker" / "config.toml",
            Path.home() / "Library" / "Application Support" / "canvas-tracker" / "config.toml",
            Path.cwd() / "config.toml",
            Path(__file__).resolve().parent / "config.toml",
        ]
        for cand in candidates:
            if cand.exists():
                target_file = cand
                break

    if target_file and target_file.exists() and tomllib:
        try:
            with open(target_file, "rb") as f:
                file_cfg = tomllib.load(f)
                for section in ["canvas", "refresh", "notes"]:
                    if section in file_cfg and isinstance(file_cfg[section], dict):
                        config[section].update(file_cfg[section])
        except Exception as e:
            print(f"Warning: Could not parse config file {target_file}: {e}", file=sys.stderr)

    # 3. Environment variables (highest priority)
    if os.getenv("CANVAS_BASE_URL"):
        config["canvas"]["base_url"] = os.getenv("CANVAS_BASE_URL")
    if os.getenv("CANVAS_API_TOKEN"):
        config["canvas"]["api_token"] = os.getenv("CANVAS_API_TOKEN")
    if os.getenv("CANVAS_REFRESH_INTERVAL"):
        try:
            config["refresh"]["interval_minutes"] = int(os.getenv("CANVAS_REFRESH_INTERVAL"))
        except ValueError:
            pass
    if os.getenv("CANVAS_ACTIVE_HOURS_START"):
        config["refresh"]["active_hours_start"] = os.getenv("CANVAS_ACTIVE_HOURS_START")
    if os.getenv("CANVAS_ACTIVE_HOURS_END"):
        config["refresh"]["active_hours_end"] = os.getenv("CANVAS_ACTIVE_HOURS_END")
    if os.getenv("CANVAS_NOTES_BACKEND"):
        config["notes"]["backend"] = os.getenv("CANVAS_NOTES_BACKEND")
    if os.getenv("CANVAS_GOOGLE_SHEET_URL"):
        config["notes"]["google_sheet_url"] = os.getenv("CANVAS_GOOGLE_SHEET_URL")
    if os.getenv("CANVAS_GOOGLE_SHEET_SECRET"):
        config["notes"]["google_sheet_secret"] = os.getenv("CANVAS_GOOGLE_SHEET_SECRET")

    config["canvas"]["base_url"] = config["canvas"]["base_url"].rstrip("/")
    return config


# Global configuration & backward-compatible module exports
_CONFIG = load_config()
CANVAS_BASE_URL = _CONFIG["canvas"]["base_url"]
CANVAS_API_TOKEN = _CONFIG["canvas"]["api_token"]


def is_within_active_hours(start_str: str, end_str: str, target_time: time = None) -> bool:
    """Check if target_time (default: current local time) falls within [start_str, end_str].
    Supports wrap-around midnight windows (e.g., 22:00 to 06:00).
    """
    if target_time is None:
        target_time = datetime.now().time()
    try:
        start = time.fromisoformat(start_str)
        end = time.fromisoformat(end_str)
    except ValueError:
        return True  # Fallback to permissive on parse error

    if start <= end:
        return start <= target_time <= end
    else:
        return target_time >= start or target_time <= end


# ---------------------------------------------------------------------------
# Canvas API Helpers & Formatting
# ---------------------------------------------------------------------------

def get_next_url(headers):
    """Parse Canvas Link headers for pagination."""
    link_header = headers.get("Link", "")
    if not link_header:
        return None
    links = link_header.split(",")
    for link in links:
        if 'rel="next"' in link:
            parts = link.split(";")
            url_part = parts[0].strip()
            if url_part.startswith("<") and url_part.endswith(">"):
                return url_part[1:-1]
    return None


def fetch_all(session, url, params=None):
    """Fetch all pages of a paginated Canvas API endpoint."""
    results = []
    current_url = url
    current_params = params
    while current_url:
        # Secure SSL verification is enabled by default in requests
        response = session.get(current_url, params=current_params, timeout=15)
        response.raise_for_status()
        current_params = None
        data = response.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        current_url = get_next_url(response.headers)
    return results


def format_date(date_str):
    """Format Canvas UTC ISO 8601 date string to human-readable local-style string."""
    if not date_str:
        return "No Due Date"
    try:
        dt_utc = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone()
        return dt_local.strftime("%a %Y-%m-%d %I:%M %p")
    except ValueError:
        return date_str


def parse_date(date_str):
    """Parse Canvas date to datetime object for sorting. Un-dated items go to the end."""
    if not date_str:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def classify_assignment_type(group_name, assignment=None, *args, **kwargs):
    """Classify the assignment type to align directly with the course's weight groups
    (assignment groups), while properly identifying special categories like 'Ungraded'.
    """
    if assignment and isinstance(assignment, dict):
        if (
            assignment.get("grading_type") == "not_graded"
            or "not_graded" in (assignment.get("submission_types") or [])
        ):
            return "Ungraded"

    if not group_name:
        if assignment and isinstance(assignment, dict) and assignment.get("points_possible") == 0:
            return "Ungraded"
        return "Class Assignment"

    name_clean = str(group_name).strip()
    name_lower = name_clean.lower()

    if name_lower in ["ungraded", "not graded", "non-graded"]:
        return "Ungraded"
    if name_lower == "hw":
        return "Homework"

    return name_clean


def clean_course_name(name: str) -> str:
    """Remove term/section administrative suffixes from course names.
    Examples:
        '1(A) H Spanish 4 S1 - Powers_S1_E64710.3_3063' -> '1(A) H Spanish 4 S1 - Powers'
        '2(A-B) Adv English/Lang Arts 8 - Johnsen_YR_A08034.7_3263' -> '2(A-B) Adv English/Lang Arts 8 - Johnsen'
        '3(B) Health 8 Q - McIntosh_Q1_L28040.18_3263' -> '3(B) Health 8 Q - McIntosh'
        '1(B) PSAT/SAT Preparation SX1 - Brown_S1_X40010.1_3063' -> '1(B) PSAT/SAT Preparation SX1 - Brown'
    """
    if not name:
        return ""
    return re.sub(r"_(?:YR|Q\d+|S\d+|SX\d+|SO\d+|SEM\d+|FY)_.*$", "", name).strip()


# ---------------------------------------------------------------------------
# Print-friendly PDF rendering (WeasyPrint) + JMAP attachment support
# ---------------------------------------------------------------------------

_PRINT_CSS = """
@page { size: Letter landscape; margin: 8mm; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 8.5pt; color: #1e293b; margin: 0; }
h1, h2, h3 { margin: 0.25em 0; }
h1 { font-size: 1.4em; color: #0f172a; }
h2 { font-size: 1.25em; color: #1e3a8a; border-bottom: 2.5px solid #3b82f6; padding-bottom: 3px; margin-top: 0.5em; page-break-after: avoid; }
h3 { font-size: 1.05em; color: #1d4ed8; margin-top: 0.7em; margin-bottom: 0.25em; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 0.3em 0 0.8em 0; font-size: 8pt; }
th, td { border: 0.5pt solid #cbd5e1; padding: 3pt 5pt; vertical-align: middle; }
th { background-color: #1e293b; color: #ffffff; text-align: left; font-weight: 600; font-size: 7.5pt; text-transform: uppercase; letter-spacing: 0.4px; }
th, td { white-space: nowrap; }
td.aw-wrap, th.aw-wrap { white-space: normal; }
td.c, th.c { text-align: center; }
td.r, th.r { text-align: right; }
tbody tr:nth-child(even) { background-color: #f8fafc; }
tr.row-missing { background-color: #fee2e2 !important; }
tr.row-missing td { border-color: #fca5a5; }
tr.row-nograde { background-color: #fef3c7 !important; }
tr.row-ungraded { background-color: #eff6ff !important; }
.badge { display: inline-block; padding: 2pt 5pt; border-radius: 3pt; font-weight: 700; font-size: 7pt; text-align: center; white-space: nowrap; }
.badge-missing { background-color: #dc2626; color: #ffffff; }
.badge-nograde { background-color: #d97706; color: #ffffff; }
.badge-ungraded { background-color: #2563eb; color: #ffffff; }
.badge-upcoming { background-color: #059669; color: #ffffff; }
.type-badge { display: inline-block; padding: 1.5pt 4.5pt; border-radius: 2pt; font-weight: 600; font-size: 7pt; }
.type-hw { background-color: #f1f5f9; color: #475569; border: 0.5pt solid #cbd5e1; }
.type-class { background-color: #e0f2fe; color: #0369a1; border: 0.5pt solid #bae6fd; }
.type-assess { background-color: #fce7f3; color: #be185d; border: 0.5pt solid #fbcfe8; font-weight: bold; }
.grade-pill { display: inline-block; padding: 1.5pt 6pt; border-radius: 3pt; font-weight: 700; font-size: 8pt; }
.grade-a { background-color: #dcfce7; color: #15803d; border: 0.5pt solid #86efac; }
.grade-b { background-color: #dbeafe; color: #1d4ed8; border: 0.5pt solid #93c5fd; }
.grade-c { background-color: #fef3c7; color: #b45309; border: 0.5pt solid #fde68a; }
.grade-low { background-color: #fee2e2; color: #b91c1c; border: 0.5pt solid #fca5a5; }
tr { page-break-inside: avoid; }
"""


def _to_print_html(html_str):
    """Restructure report HTML into a self-contained print-friendly page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_str, "html.parser")
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        header_texts = [th.get_text(strip=True) for th in header_cells]
        for attr in ("cellpadding", "border"):
            if table.has_attr(attr):
                del table[attr]
        if table.get("style"):
            table["style"] = ""

        # Clear inline styles on th so _PRINT_CSS th rule applies with white text on dark background
        for th in header_cells:
            if th.get("style"):
                th["style"] = ""

        if "Assignment" in header_texts:
            aw_idx = header_texts.index("Assignment")
            for th in header_cells:
                if th.get_text(strip=True) == "Assignment":
                    th["class"] = th.get("class", []) + ["aw-wrap"]
            for row in table.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) == len(header_texts) and aw_idx < len(tds):
                    tds[aw_idx]["class"] = tds[aw_idx].get("class", []) + ["aw-wrap"]

        for row in table.find_all("tr"):
            row_text = row.get_text()
            if "MISSING" in row_text:
                row["class"] = row.get("class", []) + ["row-missing"]
            elif "NO GRADE" in row_text:
                row["class"] = row.get("class", []) + ["row-nograde"]
            elif "UNGRADED" in row_text:
                row["class"] = row.get("class", []) + ["row-ungraded"]

        for el in table.find_all(["th", "td"]):
            st = el.get("style", "")
            if "text-align: center" in st:
                el["class"] = el.get("class", []) + ["c"]
            elif "text-align: right" in st:
                el["class"] = el.get("class", []) + ["r"]

    for a in soup.find_all("a"):
        a["style"] = "color: inherit; text-decoration: none;"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_PRINT_CSS}</style></head>
<body>{soup}</body></html>"""


def html_to_pdf_bytes(html_str: str) -> bytes:
    """Render an HTML fragment (a student's report) to a landscape Letter PDF."""
    from io import BytesIO
    from weasyprint import HTML

    buf = BytesIO()
    HTML(string=_to_print_html(html_str), base_url=".").write_pdf(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Core Data Fetching Subroutines
# ---------------------------------------------------------------------------

def fetch_course_submissions(course, session, student_ids, base_url=None):
    """Fetch submissions for observed students in a single course."""
    base_url = base_url or CANVAS_BASE_URL
    course_id = course["id"]
    course_name = course["name"]

    associated_student_ids = []
    enrollments = course.get("enrollments", [])
    for enrollment in enrollments:
        if enrollment.get("type") == "observer":
            associated_id = enrollment.get("associated_user_id")
            if associated_id and associated_id in student_ids:
                associated_student_ids.append(associated_id)

    if not associated_student_ids:
        return []

    try:
        submissions = fetch_all(
            session,
            f"{base_url}/api/v1/courses/{course_id}/students/submissions",
            params={"student_ids[]": associated_student_ids, "include[]": "assignment"}
        )
        ag_map = {}
        try:
            ags = fetch_all(session, f"{base_url}/api/v1/courses/{course_id}/assignment_groups")
            for ag in ags:
                if isinstance(ag, dict) and "id" in ag and "name" in ag:
                    ag_map[ag["id"]] = ag["name"]
        except Exception:
            pass

        for sub in submissions:
            if isinstance(sub, dict):
                assignment = sub.get("assignment")
                if isinstance(assignment, dict):
                    ag_id = assignment.get("assignment_group_id")
                    if ag_id and ag_id in ag_map:
                        assignment["assignment_group_name"] = ag_map[ag_id]

        return [(course_name, sub) for sub in submissions]
    except Exception:
        return []


def fetch_assignment_group_grades(session, course_id, course_name, student_ids, gp_id, base_url=None):
    """Fetch assignment group grades natively using manual calculation."""
    base_url = base_url or CANVAS_BASE_URL
    try:
        ags = fetch_all(session, f"{base_url}/api/v1/courses/{course_id}/assignment_groups")
        target_ags = {
            ag["id"]: ag["name"] for ag in ags
            if isinstance(ag, dict) and any(k in ag.get("name", "").lower() for k in ["test", "assessment", "sclt", "quiz"])
        }

        if not target_ags:
            return []

        subs = []
        for sid in student_ids:
            try:
                stu_subs = fetch_all(
                    session,
                    f"{base_url}/api/v1/courses/{course_id}/students/submissions",
                    params={"student_ids[]": sid, "include[]": ["assignment"]}
                )
                subs.extend(stu_subs)
            except Exception:
                pass

        results = []
        for sid in student_ids:
            student_subs = [s for s in subs if s.get("user_id") == sid]
            for ag_id, ag_name in target_ags.items():
                score_sum = 0.0
                possible_sum = 0.0
                for sub in student_subs:
                    if not isinstance(sub, dict):
                        continue
                    assign = sub.get("assignment", {})
                    if assign.get("omit_from_final_grade"):
                        continue

                    if assign.get("assignment_group_id") == ag_id:
                        if gp_id is None or sub.get("grading_period_id") == int(gp_id):
                            if sub.get("score") is not None and not sub.get("excused"):
                                score_sum += float(sub["score"])
                                possible_sum += float(assign.get("points_possible", 0.0))

                if possible_sum > 0:
                    pct = round((score_sum / possible_sum) * 100, 2)
                    results.append({
                        "student_id": sid,
                        "course_name": course_name,
                        "group_name": ag_name,
                        "score": pct
                    })
        return results
    except Exception as e:
        import sys
        print(f"Error calculating group grades for {course_id}: {e}", file=sys.stderr)
        return []


def fetch_student_enrollments(session, student_id, active_gps, base_url=None):
    """Fetch active enrollments for a given student ID, requesting specific grading periods where active."""
    base_url = base_url or CANVAS_BASE_URL
    all_enrollments = []
    for course_id, gp_id in active_gps.items():
        params = {"user_id": student_id}
        if gp_id:
            params["grading_period_id"] = gp_id
        try:
            enrolls = fetch_all(
                session,
                f"{base_url}/api/v1/courses/{course_id}/enrollments",
                params=params
            )
            active_enrolls = [e for e in enrolls if e.get("enrollment_state") == "active"]
            all_enrollments.extend(active_enrolls)
        except Exception:
            pass
    return all_enrollments


def fetch_course_gradebook(base_url: str, token: str, course_id: int, student_id: int) -> dict:
    """Fetch all submissions, assignment groups, and weighting metadata for a single course and student,
    replicating the detailed calculation logic from canvas_gradebook.py.
    """
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    import importlib
    import canvas_gradebook
    importlib.reload(canvas_gradebook)
    from canvas_gradebook import (
        build_gradebook_rows,
        sort_gradebook_rows,
        compute_overall,
        attach_grade_impacts,
        compute_category_health,
        simulate_course_grade,
    )

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    # 1. Submissions for this student in this course
    submissions = fetch_all(
        session,
        f"{base_url}/api/v1/courses/{course_id}/students/submissions",
        params={"student_ids[]": [student_id], "include[]": ["assignment"]}
    )

    # 2. Assignment groups
    try:
        ags = fetch_all(session, f"{base_url}/api/v1/courses/{course_id}/assignment_groups")
    except Exception:
        ags = []

    groups_by_id = {}
    group_weights = {}
    for ag in ags:
        if isinstance(ag, dict) and "id" in ag:
            groups_by_id[ag["id"]] = ag
            if ag.get("group_weight") is not None:
                try:
                    group_weights[ag["id"]] = float(ag["group_weight"])
                except (ValueError, TypeError):
                    pass

    # 3. Course info for weighted check
    try:
        c_resp = session.get(f"{base_url}/api/v1/courses/{course_id}", timeout=10)
        course_info = c_resp.json() if c_resp.status_code == 200 else {}
    except Exception:
        course_info = {}

    weighted = bool(course_info.get("apply_assignment_group_weights")) or any(
        wt > 0 for wt in group_weights.values()
    )

    # 4. Build, sort rows, attach impacts & category health
    rows = build_gradebook_rows(submissions, groups_by_id, weighted, sid=student_id)
    attach_grade_impacts(rows, weighted, group_weights)
    rows = sort_gradebook_rows(rows)
    computed = compute_overall(rows, weighted, group_weights)
    category_health = compute_category_health(rows, weighted, group_weights, groups_by_id)

    return {
        "course_id": course_id,
        "course_info": course_info,
        "rows": rows,
        "weighted": weighted,
        "group_weights": group_weights,
        "groups_by_id": groups_by_id,
        "assignment_groups": ags,
        "computed_overall": computed,
        "category_health": category_health,
    }


# ---------------------------------------------------------------------------
# High-Level Data Aggregator (Used by CLI & Streamlit)
# ---------------------------------------------------------------------------

def collect_canvas_data(base_url: str = None, token: str = None, grades_only: bool = False) -> dict:
    """Fetch and aggregate observees, course grades, group grades, and incomplete assignments.
    Returns a unified data dictionary suitable for CLI printing or Streamlit UI rendering.
    """
    base_url = (base_url or CANVAS_BASE_URL).rstrip("/")
    token = token or CANVAS_API_TOKEN

    if not token:
        raise ValueError("Canvas API token is not configured. Check environment, .env, or config.toml.")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)

    # 1. Fetch observees
    try:
        observees = fetch_all(session, f"{base_url}/api/v1/users/self/observees")
    except Exception as e:
        raise RuntimeError(f"Failed connecting to Canvas API at {base_url}: {e}")

    if not observees:
        return {
            "observees": [],
            "student_map": {},
            "student_ids": [],
            "academic_courses": [],
            "student_grades": {},
            "student_group_grades": {},
            "student_assignments": {},
            "fetched_at": datetime.now(),
        }

    student_map = {s["id"]: s["name"] for s in observees}
    student_ids = list(student_map.keys())

    # 2. Fetch active courses
    courses = fetch_all(session, f"{base_url}/api/v1/courses", params={"enrollment_state": "active"})
    academic_courses = [c for c in courses if c.get("name") and c.get("course_code")]

    student_assignments = {sid: [] for sid in student_ids}
    student_enrollments = {sid: [] for sid in student_ids}

    # 3. Determine active grading periods
    active_gps = {}
    now = datetime.now(timezone.utc)
    for c in academic_courses:
        try:
            gps_resp = session.get(f"{base_url}/api/v1/courses/{c['id']}/grading_periods", timeout=10).json()
            gps_data = gps_resp.get("grading_periods", [])
            active_gp = None
            for gp in gps_data:
                try:
                    start = datetime.strptime(gp["start_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    end = datetime.strptime(gp["end_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if start <= now <= end:
                        active_gp = gp
                        break
                except Exception:
                    pass
            if not active_gp:
                active_gp = next((gp for gp in gps_data if not gp.get("is_closed")), None)
            active_gps[c['id']] = active_gp["id"] if active_gp else None
        except Exception:
            active_gps[c['id']] = None

    # 4. Fetch submissions, enrollments, and group grades concurrently
    all_submissions = []
    group_grades_results = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        submission_futures = []
        if not grades_only:
            submission_futures = [
                executor.submit(fetch_course_submissions, course, session, student_ids, base_url)
                for course in academic_courses
            ]
        enrollment_futures = {
            sid: executor.submit(fetch_student_enrollments, session, sid, active_gps, base_url)
            for sid in student_ids
        }
        group_grade_futures = [
            executor.submit(fetch_assignment_group_grades, session, course["id"], course["name"], student_ids, active_gps.get(course["id"]), base_url)
            for course in academic_courses
        ]

        for future in submission_futures:
            all_submissions.extend(future.result())

        for sid, future in enrollment_futures.items():
            try:
                student_enrollments[sid] = future.result()
            except Exception:
                student_enrollments[sid] = []

        for future in group_grade_futures:
            try:
                group_grades_results.extend(future.result())
            except Exception:
                pass

    # 5. Organize group grades by student
    student_group_grades = {sid: [] for sid in student_ids}
    for item in group_grades_results:
        student_group_grades[item["student_id"]].append(item)
    for sid in student_group_grades:
        student_group_grades[sid].sort(key=lambda x: x["course_name"].lower())

    # 6. Organize course grades by student
    student_grades = {sid: [] for sid in student_ids}
    academic_courses_dict = {c["id"]: c for c in academic_courses}

    for sid, enrollments in student_enrollments.items():
        for e in enrollments:
            course_id = e.get("course_id")
            if course_id in academic_courses_dict:
                course_name = academic_courses_dict[course_id]["name"]
                grades = e.get("grades", {})
                cp_score = grades.get("current_period_computed_current_score")
                cp_grade = grades.get("current_period_computed_current_grade")

                current_score = cp_score if cp_score is not None else grades.get("current_score")
                current_grade = cp_grade if cp_grade is not None else grades.get("current_grade")

                if current_score is not None and current_grade is not None:
                    grade_str = f"{current_score}% ({current_grade})"
                elif current_score is not None:
                    grade_str = f"{current_score}%"
                elif current_grade is not None:
                    grade_str = f"{current_grade}"
                else:
                    grade_str = "—"

                student_grades[sid].append({
                    "course_id": course_id,
                    "course": course_name,
                    "grade_str": grade_str,
                    "score": current_score,
                    "grade": current_grade
                })
        student_grades[sid].sort(key=lambda x: x["course"].lower())

    # 7. Process incomplete assignments
    for course_name, sub in all_submissions:
        user_id = sub.get("user_id")
        if user_id not in student_assignments:
            continue

        assignment = sub.get("assignment")
        if not assignment or sub.get("excused"):
            continue

        due_at = assignment.get("due_at")
        due_dt = None
        if due_at:
            due_dt = parse_date(due_at)
            # Filter out assignments older than 30 days
            if (datetime.now(timezone.utc) - due_dt).days > 30:
                continue

        is_canvas_missing = sub.get("missing", False)
        workflow_state = sub.get("workflow_state", "unsubmitted")
        submitted_at = sub.get("submitted_at")
        grade = sub.get("grade")

        has_submission = (submitted_at is not None or workflow_state == "submitted")
        has_grade = (grade is not None or workflow_state == "graded")

        is_zero_grade = False
        if grade is not None:
            try:
                if float(grade) == 0:
                    is_zero_grade = True
            except ValueError:
                pass

        is_completed = has_grade and not is_canvas_missing and not is_zero_grade

        if not is_completed:
            if is_canvas_missing or is_zero_grade:
                status = "MISSING"
            elif has_submission and not has_grade:
                status = "UNGRADED"
            elif due_dt and due_dt < datetime.now(timezone.utc):
                status = "NO GRADE"
            elif not due_dt:
                status = "NO GRADE"
            else:
                status = "UPCOMING"

            student_assignments[user_id].append({
                "assignment_id": assignment.get("id"),
                "course": course_name,
                "title": assignment.get("name", "Untitled"),
                "due_raw": due_at,
                "due_str": format_date(due_at),
                "points": assignment.get("points_possible", 0),
                "status": status,
                "url": assignment.get("html_url", ""),
                "type": classify_assignment_type(assignment.get("assignment_group_name"), assignment),
                "assignment_group_name": assignment.get("assignment_group_name")
            })

    # Sort incomplete assignments chronologically by due date (oldest/overdue first, undated last)
    for sid in student_assignments:
        student_assignments[sid].sort(key=lambda x: parse_date(x["due_raw"]))

    return {
        "observees": observees,
        "student_map": student_map,
        "student_ids": student_ids,
        "academic_courses": academic_courses,
        "student_grades": student_grades,
        "student_group_grades": student_group_grades,
        "student_assignments": student_assignments,
        "fetched_at": datetime.now(),
    }


def build_student_html(sid: int, sname: str, assignments: list, ag_grades: list, grades: list, grades_only: bool = False) -> str:
    """Build the HTML table fragment for a single student."""
    student_md = f"<h2>{html.escape(sname)}</h2>\n"

    if not grades_only:
        sorted_assigns = sorted(assignments, key=lambda x: parse_date(x.get("due_raw")))
        if sorted_assigns:
            student_md += """<table border="1" cellpadding="5" style="border-collapse: collapse; width: 100%; font-family: sans-serif; font-size: 0.85em;">
  <thead>
    <tr>
      <th style="background-color: #f2f2f2; text-align: left;">Course</th>
      <th style="background-color: #f2f2f2; text-align: left;">Assignment</th>
      <th style="background-color: #f2f2f2; text-align: center;">Type</th>
      <th style="background-color: #f2f2f2; text-align: left;">Due</th>
      <th style="background-color: #f2f2f2; text-align: center;">Points</th>
      <th style="background-color: #f2f2f2; text-align: center;">Status</th>
    </tr>
  </thead>
  <tbody>
"""
            for item in sorted_assigns:
                st = item.get("status", "")
                if st == "MISSING":
                    status_emoji = "🔴 MISSING"
                    row_bg = ' style="background-color: #fee2e2;"'
                elif st == "NO GRADE":
                    status_emoji = "🟠 NO GRADE"
                    row_bg = ' style="background-color: #fef3c7;"'
                elif st == "UNGRADED":
                    status_emoji = "🔵 UNGRADED"
                    row_bg = ' style="background-color: #eff6ff;"'
                else:
                    status_emoji = "🟡 UPCOMING"
                    row_bg = ""

                t = item.get("type", "")
                if t == "Homework":
                    type_emoji = "🏠 Homework"
                elif t == "Class Assignment":
                    type_emoji = "🏫 Class Assignment"
                elif t == "Assessment":
                    type_emoji = "📝 Assessment"
                else:
                    type_emoji = t

                title_escaped = html.escape(item.get("title", ""))
                course_clean = html.escape(clean_course_name(item.get("course", "")))
                url = item.get("url")
                link_html = f"<a href='{url}'>{title_escaped}</a>" if url else title_escaped

                student_md += f"""    <tr{row_bg}>
      <td>{course_clean}</td>
      <td>{link_html}</td>
      <td style="text-align: center; font-weight: bold;">{type_emoji}</td>
      <td>{item.get('due_str', '')}</td>
      <td style="text-align: center;">{item.get('points', '')}</td>
      <td style="text-align: center; font-weight: bold;">{status_emoji}</td>
    </tr>
"""
            student_md += "  </tbody>\n</table>\n<br>\n"
        else:
            student_md += "<p>🎉 All caught up! No incomplete assignments found.</p>\n"

    # Assessment group grades
    if ag_grades:
        student_md += f"<h3>Test & Assessment Grades</h3>\n"
        student_md += """<table border="1" cellpadding="5" style="border-collapse: collapse; width: 100%; font-family: sans-serif; font-size: 0.85em;">
  <thead>
    <tr>
      <th style="background-color: #f2f2f2; text-align: left;">Course</th>
      <th style="background-color: #f2f2f2; text-align: left;">Category</th>
      <th style="background-color: #f2f2f2; text-align: center;">Score</th>
    </tr>
  </thead>
  <tbody>
"""
        for ag in ag_grades:
            course_clean = html.escape(clean_course_name(ag.get("course_name", "")))
            cat_escaped = html.escape(ag.get("group_name", ""))
            student_md += f"""    <tr>
      <td>{course_clean}</td>
      <td>{cat_escaped}</td>
      <td style="text-align: center; font-weight: bold;">{ag.get('score', 0)}%</td>
    </tr>
"""
        student_md += "  </tbody>\n</table>\n<br>\n"

    # Current course grades
    student_md += f"<h3>Current Grades</h3>\n"
    student_md += """<table border="1" cellpadding="5" style="border-collapse: collapse; width: 100%; font-family: sans-serif; font-size: 0.85em;">
  <thead>
    <tr>
      <th style="background-color: #f2f2f2; text-align: left;">Course</th>
      <th style="background-color: #f2f2f2; text-align: center;">Current Grade</th>
    </tr>
  </thead>
  <tbody>
"""
    for g_item in grades:
        course_clean = html.escape(clean_course_name(g_item.get("course", "")))
        student_md += f"""    <tr>
      <td>{course_clean}</td>
      <td style="text-align: center; font-weight: bold;">{g_item.get('grade_str', '—')}</td>
    </tr>
"""
    student_md += "  </tbody>\n</table>\n<br>\n"
    return student_md


def wrap_html_report(student_name: str, body_html: str, grades_only: bool = False) -> str:
    """Wrap a student's HTML report fragment into a standalone, styled HTML document."""
    report_title = f"{'Course Grades' if grades_only else 'Incomplete Assignments'} Report - {student_name}"
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(report_title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 24px; color: #1e293b; line-height: 1.5; }}
    h1 {{ font-size: 1.5em; color: #0f172a; margin-bottom: 4px; }}
    h2 {{ font-size: 1.25em; color: #1e3a8a; border-bottom: 2px solid #3b82f6; padding-bottom: 4px; margin-top: 24px; }}
    h3 {{ font-size: 1.1em; color: #1d4ed8; margin-top: 20px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px 0; font-size: 0.9em; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 6px 10px; }}
    th {{ background-color: #f1f5f9; font-weight: 600; text-align: left; }}
    a {{ color: #2563eb; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    p.meta {{ color: #64748b; font-size: 0.85em; margin-top: 0; }}
  </style>
</head>
<body>
  <h1>{html.escape(report_title)}</h1>
  <p class="meta">Generated on {timestamp}</p>
  {body_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Canvas Student Assignment & Grade Tracker")
    parser.add_argument(
        "-g", "--grades-only",
        action="store_true",
        help="Only gather and report course grades, skipping assignment details"
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="Path to custom config.toml"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    token = config["canvas"]["api_token"]
    base_url = config["canvas"]["base_url"]

    console = Console()
    console_err = Console(stderr=True)

    if not token:
        console_err.print("[red]Error: CANVAS_API_TOKEN is not set.[/red]")
        console_err.print("Set it in ~/.config/canvas_tracker/config.toml, a .env file, or environment.")
        sys.exit(1)

    console.print(f"[cyan]Connecting to Canvas API at {base_url}...[/cyan]")

    try:
        data = collect_canvas_data(base_url, token, grades_only=args.grades_only)
    except Exception as e:
        console_err.print(f"[red]Error collecting data: {e}[/red]")
        sys.exit(1)

    observees = data["observees"]
    student_map = data["student_map"]
    student_grades = data["student_grades"]
    student_group_grades = data["student_group_grades"]
    student_assignments = data["student_assignments"]

    if not observees:
        console.print("[yellow]No observed students found on this account.[/yellow]")
        sys.exit(0)

    console.print(f"Found [green]{len(observees)}[/green] student(s): {', '.join(student_map.values())}\n")

    for sid, sname in student_map.items():
        assignments = student_assignments[sid]
        ag_grades = student_group_grades.get(sid, [])
        grades = student_grades[sid]

        # Terminal Rich output
        if not args.grades_only:
            sorted_assigns = sorted(assignments, key=lambda x: parse_date(x["due_raw"]))
            table = Table(title=f"Incomplete Assignments for {sname}")
            table.add_column("Course", style="cyan", no_wrap=True)
            table.add_column("Assignment", style="magenta")
            table.add_column("Type", justify="center")
            table.add_column("Due Date", style="green")
            table.add_column("Points", style="yellow", justify="right")
            table.add_column("Status", justify="center")

            for item in sorted_assigns:
                if item['status'] == "MISSING":
                    status_colored = "[bold red]MISSING[/bold red]"
                elif item['status'] == "NO GRADE":
                    status_colored = "[bold orange3]NO GRADE[/bold orange3]"
                elif item['status'] == "UNGRADED":
                    status_colored = "[bold cyan]UNGRADED[/bold cyan]"
                else:
                    status_colored = "[bold yellow]UPCOMING[/bold yellow]"

                type_style = "bold bright_blue" if item['type'] == "Homework" else ("bold bright_magenta" if item['type'] == "Assessment" else "bold cyan")
                table.add_row(item["course"], item["title"], f"[{type_style}]{item['type']}[/{type_style}]", item["due_str"], str(item["points"]), status_colored)

            if sorted_assigns:
                console.print(table)
                console.print()
            else:
                console.print(f"[green]All caught up! No incomplete assignments found for {sname}.[/green]\n")

        if ag_grades:
            ag_table = Table(title=f"Test & Assessment Grades for {sname}")
            ag_table.add_column("Course", style="cyan", no_wrap=True)
            ag_table.add_column("Category", style="yellow")
            ag_table.add_column("Score", justify="center", style="green")
            for ag in ag_grades:
                ag_table.add_row(ag["course_name"], ag["group_name"], f"{ag['score']}%")
            console.print(ag_table)
            console.print()

        grades_table = Table(title=f"Current Grades for {sname}")
        grades_table.add_column("Course", style="cyan", no_wrap=True)
        grades_table.add_column("Current Grade", justify="center", style="magenta")
        for g_item in grades:
            grades_table.add_row(g_item["course"], g_item["grade_str"])
        console.print(grades_table)
        console.print()


# ---------------------------------------------------------------------------
# Streamlit Dashboard Runner
# ---------------------------------------------------------------------------

def is_streamlit_running() -> bool:
    """Check if script is running inside a Streamlit web runtime."""
    try:
        from streamlit.runtime import exists
        return exists()
    except Exception:
        return False


def copy_to_clipboard_js(text: str):
    """Inject client-side JavaScript to write text to the user's clipboard."""
    import streamlit as st
    js_text = json.dumps(text)
    nonce = datetime.now().timestamp()
    html_code = f"""
    <!-- copy-clipboard-nonce: {nonce} -->
    <script>
    (function() {{
        const text = {js_text};
        function fallbackCopy() {{
            try {{
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                ta.style.left = "-9999px";
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                const res = document.execCommand("copy");
                document.body.removeChild(ta);
                if (res) {{
                    console.log("[Canvas Tracker] Copied via execCommand:", text);
                }} else {{
                    console.warn("[Canvas Tracker] execCommand returned false");
                }}
            }} catch (e) {{
                console.error("[Canvas Tracker] Fallback copy failed:", e);
            }}
        }}
        if (navigator.clipboard && window.isSecureContext) {{
            navigator.clipboard.writeText(text).then(() => {{
                console.log("[Canvas Tracker] Copied via navigator.clipboard:", text);
            }}).catch(err => {{
                console.warn("[Canvas Tracker] navigator.clipboard error, trying fallback:", err);
                fallbackCopy();
            }});
        }} else {{
            fallbackCopy();
        }}
    }})();
    </script>
    """
    st.html(html_code, unsafe_allow_javascript=True)


def run_streamlit():
    import streamlit as st
    import streamlit.components.v1 as components
    import pandas as pd
    import canvas_notes
    import importlib
    importlib.reload(canvas_notes)
    from canvas_notes import get_notes_backend, GOOGLE_APPS_SCRIPT_TEMPLATE

    st.set_page_config(page_title="Canvas Student Tracker", page_icon="🎓", layout="wide")

    config = load_config()
    canvas_cfg = config["canvas"]
    refresh_cfg = config["refresh"]
    notes_cfg = config.get("notes", {})

    st.sidebar.title("🎓 Canvas Tracker")

    # Credentials
    base_url = st.sidebar.text_input("Canvas Base URL", value=canvas_cfg["base_url"])
    token = canvas_cfg.get("api_token") or ""
    if not token:
        token = st.sidebar.text_input("Canvas API Token", type="password", help="Enter your Canvas API token or configure it in .env")

    st.sidebar.divider()
    st.sidebar.subheader("Refresh Configuration")

    interval_min = st.sidebar.number_input(
        "Refresh Interval (Minutes)",
        min_value=5,
        max_value=1440,
        value=int(refresh_cfg.get("interval_minutes", 60)),
        step=15,
        help="How frequently the dashboard polls Canvas for updates."
    )

    col_h1, col_h2 = st.sidebar.columns(2)
    active_start = col_h1.text_input("Active Start", value=refresh_cfg.get("active_hours_start", "06:00"))
    active_end = col_h2.text_input("Active End", value=refresh_cfg.get("active_hours_end", "20:00"))

    # Ad-hoc refresh button
    if st.sidebar.button("🔄 Refresh Now", width="stretch", type="primary"):
        st.session_state["force_refresh"] = True
        st.session_state.pop("canvas_data", None)
        if "cached_gradebooks" in st.session_state:
            st.session_state["cached_gradebooks"].clear()
        st.cache_data.clear()
        st.rerun()

    # Notes & Storage Configuration
    st.sidebar.divider()
    with st.sidebar.expander("📝 Notes & Storage", expanded=False):
        default_backend_idx = 1 if notes_cfg.get("backend") == "google_sheet" else 0
        selected_backend_mode = st.radio(
            "Storage Backend",
            options=["Local File (canvas_notes.json)", "Google Sheet Sync"],
            index=default_backend_idx,
            key="notes_backend_mode_radio",
            help="Choose where assignment notes are saved and synced."
        )

        backend_type = "google_sheet" if "Google" in selected_backend_mode else "local"
        notes_cfg["backend"] = backend_type

        if backend_type == "local":
            st.caption("Notes are saved locally to `canvas_notes.json` in the project root. Zero setup required.")
            local_backend = get_notes_backend({"notes": {"backend": "local", "filepath": notes_cfg.get("filepath", "canvas_notes.json")}})
            valid, msg = local_backend.validate()
            if valid:
                all_local = local_backend.load_all_notes()
                st.caption(f"Status: Ready ({len(all_local)} note(s) stored)")
            else:
                st.warning(f"Status: {msg}")
            notes_backend = local_backend
        else:
            gs_url = st.text_input(
                "Apps Script Web App URL",
                value=notes_cfg.get("google_sheet_url", ""),
                placeholder="https://script.google.com/macros/s/.../exec",
                key="gs_url_input",
                help="The deployed Web App URL from Google Apps Script."
            )
            gs_secret = st.text_input(
                "Secret Key (Optional)",
                value=notes_cfg.get("google_sheet_secret", ""),
                type="password",
                key="gs_secret_input",
                help="The SECRET_KEY set in your Google Apps Script, if configured."
            )
            notes_cfg["google_sheet_url"] = gs_url
            notes_cfg["google_sheet_secret"] = gs_secret
            gs_backend = get_notes_backend({"notes": notes_cfg})

            col_test, col_export = st.columns(2)
            with col_test:
                if st.button("🧪 Test Connection", key="btn_test_gs"):
                    with st.spinner("Pinging Web App..."):
                        ok, msg = gs_backend.validate()
                        if ok:
                            st.success("✅ Connected!")
                        else:
                            st.error(f"❌ {msg}")
            with col_export:
                if st.button("⬆️ Export Local", key="btn_export_local_to_gs", help="Copy notes from local canvas_notes.json into Google Sheet"):
                    local_backend = get_notes_backend({"notes": {"backend": "local"}})
                    count = local_backend.export_to(gs_backend)
                    st.success(f"Exported {count} notes to sheet!")

            with st.expander("📋 Google Sheet Setup Guide", expanded=False):
                st.markdown("""
**How to set up Google Sheet Notes Sync:**
1. Open a new Google Sheet in your Google Drive.
2. Go to **Extensions** → **Apps Script**.
3. Replace all code in the script editor with the snippet below.
4. *(Optional)* Set `SECRET_KEY = "your-passphrase"`.
5. Click **Deploy** → **New deployment**:
   - Type: **Web app**
   - Execute as: **Me**
   - Who has access: **Anyone**
6. Click Deploy, authorize access, and copy the Web App URL.
7. Paste the URL into the input above and click **Test Connection**!
""")
                st.code(GOOGLE_APPS_SCRIPT_TEMPLATE, language="javascript")

            notes_backend = gs_backend

        st.session_state["notes_backend"] = notes_backend

    if not token:
        st.warning("⚠️ Please provide a Canvas API Token in the sidebar, a `.env` file, or `~/.config/canvas_tracker/config.toml`.")
        st.stop()

    # Fragment handles the periodic execution loop
    @st.fragment(run_every=timedelta(minutes=interval_min))
    def dashboard_fragment():
        within_window = is_within_active_hours(active_start, active_end)
        now = datetime.now()

        existing_data = st.session_state.get("canvas_data")
        last_fetch = st.session_state.get("last_fetch_time")
        force_refresh = st.session_state.pop("force_refresh", False)

        # Cache expires when interval_min has elapsed since last fetch
        cache_expired = (last_fetch is None) or ((now - last_fetch) >= timedelta(minutes=interval_min))

        should_fetch = force_refresh or (existing_data is None) or (within_window and cache_expired)

        if not within_window:
            st.info(
                f"⏸️ **Auto-refresh paused:** Outside active hours ({active_start} – {active_end}). "
                "Displaying latest available data. Use **'Refresh Now'** in the sidebar to force an update."
            )

        if should_fetch:
            with st.spinner("Fetching data from Canvas..."):
                try:
                    data = collect_canvas_data(base_url, token)
                    st.session_state["canvas_data"] = data
                    st.session_state["last_fetch_time"] = datetime.now()
                    if "cached_gradebooks" in st.session_state:
                        st.session_state["cached_gradebooks"].clear()
                except Exception as e:
                    st.error(f"Error connecting to Canvas API: {e}")
                    st.session_state["last_fetch_time"] = datetime.now()
                    if existing_data:
                        data = existing_data
                    else:
                        return
        else:
            data = existing_data

        observees = data["observees"]
        student_map = data["student_map"]
        student_ids = data["student_ids"]
        student_grades = data["student_grades"]
        student_group_grades = data["student_group_grades"]
        student_assignments = data["student_assignments"]
        fetched_at = data.get("fetched_at", datetime.now())
        notes_backend = st.session_state.get("notes_backend") or get_notes_backend(config)

        if not observees:
            st.warning("No observed students found on this Canvas account.")
            return

        st.caption(f"Last updated: **{fetched_at.strftime('%Y-%m-%d %I:%M:%S %p')}** | Refresh: **Every {interval_min}m** (Active: **{active_start} – {active_end}**)")

        # Client-side focus & timer manager:
        # 1. Triggers immediate refresh when switching back to tab if elapsed >= interval_min.
        # 2. Reschedules remaining countdown to real wall-clock time if returning before expiry.
        # 3. Completely resets the timer countdown whenever a refresh completes.
        last_fetch_time = st.session_state.get("last_fetch_time") or datetime.now()
        last_fetch_ts = int(last_fetch_time.timestamp() * 1000)
        interval_ms = int(interval_min * 60 * 1000)
        is_active_js = "true" if within_window else "false"

        components.html(
            f"""
            <script>
            (function() {{
                const lastFetch = {last_fetch_ts};
                const intervalMs = {interval_ms};
                const isActive = {is_active_js};

                window.parent._canvasTrackerRefreshing = false;

                function triggerRefresh() {{
                    if (window.parent._canvasTrackerRefreshing) return false;
                    const parentDoc = window.parent.document;
                    const buttons = parentDoc.querySelectorAll('button');
                    for (const btn of buttons) {{
                        if (btn.innerText && btn.innerText.includes('Refresh Now')) {{
                            window.parent._canvasTrackerRefreshing = true;
                            btn.click();
                            return true;
                        }}
                    }}
                    return false;
                }}

                function checkAndSchedule() {{
                    if (!isActive) return;
                    const now = Date.now();
                    const elapsed = now - lastFetch;

                    if (elapsed >= intervalMs) {{
                        triggerRefresh();
                    }} else {{
                        const remaining = Math.max(1000, intervalMs - elapsed);
                        if (window.parent._canvasTrackerTimer) {{
                            clearTimeout(window.parent._canvasTrackerTimer);
                        }}
                        window.parent._canvasTrackerTimer = setTimeout(function() {{
                            triggerRefresh();
                        }}, remaining);
                    }}
                }}

                if (!window.parent._canvasTrackerListenersAttached) {{
                    window.parent.addEventListener("visibilitychange", function() {{
                        if (window.parent.document.visibilityState === "visible") {{
                            if (window.parent._canvasTrackerCheck) {{
                                window.parent._canvasTrackerCheck();
                            }}
                        }}
                    }});
                    window.parent.addEventListener("focus", function() {{
                        if (window.parent._canvasTrackerCheck) {{
                            window.parent._canvasTrackerCheck();
                        }}
                    }});
                    window.parent._canvasTrackerListenersAttached = true;
                }}

                window.parent._canvasTrackerCheck = checkAndSchedule;
                checkAndSchedule();
            }})();
            </script>
            """,
            height=0,
            width=0,
        )

        tabs = st.tabs([student_map[sid] for sid in student_ids])

        for tab, sid in zip(tabs, student_ids):
            sname = student_map[sid]
            with tab:
                assigns = student_assignments.get(sid, [])
                grades = student_grades.get(sid, [])
                ag_grades = student_group_grades.get(sid, [])

                # Metrics banner
                missing_cnt = sum(1 for a in assigns if a["status"] == "MISSING")
                nograde_cnt = sum(1 for a in assigns if a["status"] == "NO GRADE")
                upcoming_cnt = sum(1 for a in assigns if a["status"] == "UPCOMING")
                ungraded_cnt = sum(1 for a in assigns if a["status"] == "UNGRADED")

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Active Courses", len(grades))
                m2.metric("Missing", missing_cnt)
                m3.metric("No Grade", nograde_cnt)
                m4.metric("Upcoming", upcoming_cnt)
                m5.metric("Ungraded", ungraded_cnt)

                st.divider()

                # Course Grades
                c1, c2 = st.columns([1, 1])
                with c1:
                    st.subheader("📊 Course Grades")
                    st.caption("💡 *Select a course row to inspect its detailed gradebook:*")
                    if grades:
                        active_cid = st.session_state.get(f"sel_course_id_{sid}")
                        default_idx = 0
                        if active_cid is not None:
                            for idx, g in enumerate(grades):
                                if g.get("course_id") == active_cid:
                                    default_idx = idx
                                    break
                        else:
                            st.session_state[f"sel_course_id_{sid}"] = grades[0].get("course_id")
                            st.session_state[f"sel_course_name_{sid}"] = grades[0].get("course")

                        courses_df = pd.DataFrame([
                            {
                                "Course": clean_course_name(g["course"]),
                                "Grade": g["grade_str"],
                            }
                            for g in grades
                        ])

                        course_table_height = max(140, (len(grades) + 1) * 36 + 5)
                        course_event = st.dataframe(
                            courses_df,
                            width="stretch",
                            height=course_table_height,
                            hide_index=True,
                            on_select="rerun",
                            selection_mode="single-row-required",
                            selection_default={"selection": {"rows": [default_idx]}},
                            key=f"course_table_{sid}",
                            column_config={
                                "Course": st.column_config.TextColumn("Course"),
                                "Grade": st.column_config.TextColumn("Grade"),
                            }
                        )

                        selected_rows = (
                            course_event.selection.rows
                            if course_event and hasattr(course_event, "selection") and course_event.selection
                            else []
                        )
                        if selected_rows:
                            sel_idx = selected_rows[0]
                            if 0 <= sel_idx < len(grades):
                                st.session_state[f"sel_course_id_{sid}"] = grades[sel_idx]["course_id"]
                                st.session_state[f"sel_course_name_{sid}"] = grades[sel_idx]["course"]
                        elif grades:
                            st.session_state[f"sel_course_id_{sid}"] = grades[default_idx]["course_id"]
                            st.session_state[f"sel_course_name_{sid}"] = grades[default_idx]["course"]
                    else:
                        st.info("No active course grades available.")

                with c2:
                    st.subheader("📝 Tests & Assessments")
                    if ag_grades:
                        ag_df = pd.DataFrame([
                            {"Course": clean_course_name(ag["course_name"]), "Category": ag["group_name"], "Score": f"{ag['score']}%"}
                            for ag in ag_grades
                        ])
                        st.dataframe(ag_df, width="stretch", hide_index=True)
                    else:
                        st.write("No assessment category scores recorded.")

                # Detailed Course Gradebook Drilldown
                active_cid = st.session_state.get(f"sel_course_id_{sid}")
                if not active_cid and grades:
                    active_cid = grades[0].get("course_id")
                    st.session_state[f"sel_course_id_{sid}"] = active_cid

                active_course_info = next((g for g in grades if g.get("course_id") == active_cid), None) if active_cid else None

                if active_course_info and active_cid:
                    cname = clean_course_name(active_course_info["course"])
                    st.divider()
                    st.markdown(f"### 📖 Detailed Gradebook: **{cname}**")

                    from canvas_gradebook import (
                        attach_grade_impacts,
                        compute_category_health,
                        simulate_course_grade,
                    )

                    gradebook_cache = st.session_state.setdefault("cached_gradebooks", {})
                    gb_key = (sid, active_cid)
                    gb_data = gradebook_cache.get(gb_key)

                    needs_refresh = (
                        gb_data is None
                        or "assignment_groups" not in gb_data
                        or any(isinstance(v, (int, float)) for v in gb_data.get("groups_by_id", {}).values())
                        or "category_health" not in gb_data
                        or not any("grade_drag" in r for r in gb_data.get("rows", []))
                    )

                    if needs_refresh:
                        with st.spinner(f"Loading graded items for {cname}..."):
                            try:
                                gb_data = fetch_course_gradebook(base_url, token, active_cid, sid)
                                gradebook_cache[gb_key] = gb_data
                            except Exception as e:
                                st.error(f"Error fetching gradebook for {cname}: {e}")
                                gb_data = None

                    if gb_data:
                        rows = gb_data["rows"]
                        weighted = gb_data["weighted"]
                        # Refresh grade impacts to ensure latest display formatting on cached data
                        attach_grade_impacts(rows, weighted, gb_data.get("group_weights"))
                        computed_overall = gb_data.get("computed_overall")

                        # Build robust group name mapping: ag info -> rows fallback -> Group ID
                        row_group_names = {
                            r["group_id"]: r["group_name"]
                            for r in rows
                            if r.get("group_id") and r.get("group_name")
                        }
                        raw_ags = gb_data.get("assignment_groups") or []
                        key_items = []
                        zero_items = []
                        if raw_ags:
                            for ag in raw_ags:
                                gid = ag.get("id")
                                gname = ag.get("name") or row_group_names.get(gid, f"Group {gid}")
                                gw = ag.get("group_weight", 0.0) or 0.0
                                try:
                                    gw = float(gw)
                                except (ValueError, TypeError):
                                    gw = 0.0
                                gtype = classify_assignment_type(gname)
                                if gw > 0:
                                    key_items.append((gname, gtype, gw))
                                else:
                                    zero_items.append(gname)
                        elif gb_data.get("group_weights"):
                            for gid, gw in gb_data["group_weights"].items():
                                ginfo = gb_data.get("groups_by_id", {}).get(gid, {})
                                if isinstance(ginfo, dict):
                                    gname = ginfo.get("name") or row_group_names.get(gid, f"Group {gid}")
                                else:
                                    gname = row_group_names.get(gid, f"Group {gid}")
                                gtype = classify_assignment_type(gname)
                                if gw > 0:
                                    key_items.append((gname, gtype, gw))
                                else:
                                    zero_items.append(gname)

                        key_items.sort(key=lambda x: x[2], reverse=True)

                        gm1, gm2, gm3, gm4 = st.columns([1, 1, 1.6, 1.4])
                        gm1.metric("Official Grade", active_course_info["grade_str"])
                        if computed_overall is not None:
                            gm2.metric("Computed Grade", f"{computed_overall:.2f}%")
                        else:
                            gm2.metric("Computed Grade", "—")

                        mode_label = "⚖️ Weighted by Category" if weighted else "📊 Total Points (Unweighted)"
                        gm3.info(f"Grading Model: **{mode_label}**")

                        with gm4:
                            st.write("")
                            with st.popover("ℹ️ Grade Calculation Guide", help="How the overall grade and individual contributions are calculated"):
                                st.markdown("### 📖 How Grades & Contributions Are Calculated")

                                st.markdown(
                                    "#### 1. Columns Overview\n"
                                    "- **Weight**:\n"
                                    "  - **Weighted Courses**: The percentage weight of the assignment's category (e.g. *Assessments 60%*, *Classwork 30%*, *Homework 10%*).\n"
                                    "  - **Unweighted Courses**: Displays `—` because assignments are evaluated on raw points.\n"
                                    "- **Contribution**:\n"
                                    "  - The percentage points this assignment contributes toward the overall course grade:\n"
                                    "    $$\\text{Contribution} = \\left(\\frac{\\text{Score}}{\\text{Points Possible}}\\right) \\times \\text{Category Weight}$$\n"
                                    "  - *Example*: Scoring 45/50 (90%) on an assessment in a 60% category contributes $0.90 \\times 60\\% = \\mathbf{54.00\\%}$."
                                )

                                st.divider()

                                st.markdown(
                                    "#### 2. Overall Grade Calculation\n\n"
                                    "##### ⚖️ Weighted Courses (Category Weights)\n"
                                    "Canvas calculates category-weighted grades in three steps:\n"
                                    "1. **Category Score**: In each assignment category, all earned points are summed and divided by possible points:\n"
                                    "   $$\\text{Category Score} = \\frac{\\sum \\text{Points Earned in Category}}{\\sum \\text{Points Possible in Category}}$$\n"
                                    "2. **Weighted Points**: Each category score is multiplied by its category weight:\n"
                                    "   $$\\text{Weighted Points} = \\text{Category Score} \\times \\text{Category Weight}$$\n"
                                    "3. **Active Category Normalization**: Canvas sums the weighted points and divides by the total weight of categories that **currently have graded assignments**:\n"
                                    "   $$\\text{Overall Grade} = \\frac{\\sum \\text{Weighted Points}}{\\sum \\text{Active Category Weights}} \\times 100\\%$$\n\n"
                                    "> **Why Normalize?** If a category has no graded assignments yet (e.g., a 10% Final Exam), Canvas excludes its weight from the denominator so students are not penalized for unassigned work.\n\n"
                                    "##### 📊 Total Points Courses (Unweighted)\n"
                                    "In unweighted courses, every point has equal value:\n"
                                    "$$\\text{Overall Grade} = \\frac{\\sum \\text{All Earned Points}}{\\sum \\text{All Possible Points}} \\times 100\\%$$\n"
                                    "The contribution column shows the item's individual score percentage, and its impact scales directly with its point value (e.g., a 100 pt project has 10× the impact of a 10 pt quiz)."
                                )

                                st.divider()

                                st.markdown(
                                    "#### 💡 Worked Example: 3 Assignments Across 2 Active Categories\n"
                                    "Suppose a course syllabus defines 3 categories, but only 2 have graded work so far:\n"
                                    "- **Assessments (60% weight)**: 1 graded quiz\n"
                                    "- **Homework (30% weight)**: 2 graded homeworks\n"
                                    "- **Final Exam (10% weight)**: No graded items yet (*inactive*)\n\n"
                                    "##### Step 1: Calculate Category Scores (Category Points Denominator)\n"
                                    "- **Quiz 1 (Assessments)**: **46 / 50**\n"
                                    "  $$\\text{Assessments Category Score} = \\frac{46}{50} = 92.00\\%$$\n"
                                    "- **HW #1 & HW #2 (Homework)**: **18 / 20** and **10 / 10**\n"
                                    "  Notice how the individual assignment points combine into the category denominator ($20 + 10 = 30$):\n"
                                    "  $$\\text{Homework Category Score} = \\frac{18 + 10}{20 + 10} = \\frac{28}{30} = 93.33\\%$$\n\n"
                                    "##### Step 2: Multiply by Category Weights\n"
                                    "- **Assessments**: $92.00\\% \\times 60\\% = 55.20\\text{ weighted points}$\n"
                                    "- **Homework**: $93.33\\% \\times 30\\% = 28.00\\text{ weighted points}$\n\n"
                                    "##### Step 3: Normalize by Active Category Weights (Overall Denominator)\n"
                                    "Canvas sums the weighted points and divides by the sum of weights for **active** categories ($60\\% + 30\\% = 90\\%$):\n"
                                    "$$\\text{Overall Grade} = \\frac{55.20\\% + 28.00\\%}{60\\% + 30\\%} = \\frac{83.20\\%}{90\\%} = \\mathbf{92.44\\%} \\quad (\\text{Grade: A})$$\n\n"
                                    "> **Summary of Denominators:**\n"
                                    "> 1. **Category Denominator**: Sum of points possible for assignments in that category (e.g., $20 + 10 = 30$).\n"
                                    "> 2. **Overall Denominator**: Sum of active category weights ($60\\% + 30\\% = 90\\%$), excluding categories with no grades yet (10% Final Exam)."
                                )

                                st.divider()
                                if weighted and key_items:
                                    st.markdown(f"#### 🎯 Dynamic Course Weights for **{cname}**")
                                    cat_bullets = [f"- **{gname}** ({gtype}): **{gw:g}%**" for gname, gtype, gw in key_items]
                                    if zero_items:
                                        cat_bullets.append(f"- *Non-contributing (0.0%)*: {', '.join(zero_items)}")
                                    st.markdown("\n".join(cat_bullets))
                                    st.markdown(
                                        "> **Why weights differ by course:** In Canvas LMS, weights belong to the course's individual "
                                        "`AssignmentGroup` objects rather than a system-wide setting. Teachers and academic departments configure weights "
                                        "independently per syllabus."
                                    )
                                elif not weighted:
                                    st.markdown(f"#### 🎯 Dynamic Course Weights for **{cname}**")
                                    st.info("This course uses an **unweighted total points** grading scheme. All graded assignments contribute based on raw points earned out of points possible.")

                        if weighted and key_items:
                            badges_html = []
                            for gname, gtype, gw in key_items:
                                icon = "📝 " if gtype == "Assessment" else ("🏠 " if gtype == "Homework" else "🏫 ")
                                badges_html.append(
                                    f"<span style='display:inline-block; background-color: rgba(59, 130, 246, 0.08); "
                                    f"border: 1px solid rgba(59, 130, 246, 0.25); border-radius: 6px; padding: 2px 9px; "
                                    f"margin: 2px 6px 2px 0; font-size: 0.88em;'>"
                                    f"{icon}<strong>{html.escape(gname)}</strong>: <span style='color: #2563eb; font-weight: 700;'>{gw:g}%</span>"
                                    f"</span>"
                                )
                            zero_note = f"<span style='color: #64748b; font-size: 0.82em;'> • 0% weight: {', '.join(html.escape(z) for z in zero_items)}</span>" if zero_items else ""
                            st.markdown(
                                f"<div style='margin: 4px 0 14px 0; line-height: 1.9;'>"
                                f"<strong>🔑 Weight Key:</strong> {' '.join(badges_html)}{zero_note}"
                                f"</div>",
                                unsafe_allow_html=True
                            )
                        elif not weighted:
                            st.markdown(
                                "<div style='margin: 4px 0 14px 0; font-size: 0.88em;'>"
                                "<strong>🔑 Weight Key:</strong> "
                                "<span style='background-color: rgba(100, 116, 139, 0.1); border: 1px solid rgba(100, 116, 139, 0.25); "
                                "border-radius: 6px; padding: 3px 10px; font-size: 0.95em;'>"
                                "📊 <strong>Total Points (Unweighted)</strong> — All assignments contribute proportionally based on points possible (no category weighting)."
                                "</span></div>",
                                unsafe_allow_html=True
                            )

                        # Filter and Sort Controls
                        graded_rows = [
                            r for r in rows
                            if r.get("score") is not None
                            and not r.get("excused")
                            and not r.get("omit_from_final_grade")
                            and r.get("workflow_state") != "pending_review"
                            and r.get("status") != "UNGRADED"
                        ]

                        col_filter, col_sort = st.columns([1.5, 1.5])
                        with col_filter:
                            gb_filter = st.segmented_control(
                                f"Filter assignments for {cname}:",
                                options=["All Items", "Graded Items (Contributing)", "Incomplete Only"],
                                default="All Items",
                                key=f"gb_filter_{sid}_{active_cid}"
                            ) or "All Items"

                        with col_sort:
                            gb_sort = st.selectbox(
                                "Sort assignments by:",
                                options=[
                                    "📅 Due Date (Oldest First)",
                                    "📅 Due Date (Newest First)",
                                    "▼ Biggest Grade Drag (Worst First)",
                                    "🚀 Highest Recovery Potential",
                                ],
                                key=f"gb_sort_{sid}_{active_cid}"
                            )

                        if gb_filter == "Graded Items (Contributing)":
                            display_rows = list(graded_rows)
                        elif gb_filter == "Incomplete Only":
                            display_rows = [
                                r for r in rows
                                if r.get("status") in ["MISSING", "NO GRADE", "UNGRADED", "UPCOMING"]
                            ]
                        else:
                            display_rows = list(rows)

                        # Apply sorting
                        if gb_sort == "📅 Due Date (Newest First)":
                            display_rows.sort(key=lambda r: parse_date(r.get("due_raw")), reverse=True)
                        elif gb_sort == "▼ Biggest Grade Drag (Worst First)":
                            display_rows.sort(
                                key=lambda r: (
                                    0 if (r.get("grade_drag") is not None and r["grade_drag"] < 0) else (1 if r.get("grade_drag") is not None else 2),
                                    r.get("grade_drag") if r.get("grade_drag") is not None else 999
                                )
                            )
                        elif gb_sort == "🚀 Highest Recovery Potential":
                            display_rows.sort(
                                key=lambda r: (
                                    0 if (r.get("potential_gain") is not None and r["potential_gain"] > 0) else 1,
                                    -(r.get("potential_gain") or 0.0)
                                )
                            )
                        else:
                            display_rows.sort(key=lambda r: parse_date(r.get("due_raw")))

                        # What-If Overrides state management
                        sim_key = f"whatif_overrides_{sid}_{active_cid}"
                        reset_cnt_key = f"sim_reset_counter_{sid}_{active_cid}"
                        reset_counter = st.session_state.setdefault(reset_cnt_key, 0)
                        overrides = st.session_state.setdefault(sim_key, {})

                        def get_impact_display(r):
                            drag = r.get("grade_drag")
                            if drag is not None:
                                if drag <= -0.005:
                                    return f"▼ {drag:.2f}%"
                                elif drag >= 0.005:
                                    return f"▲ +{drag:.2f}%"
                                else:
                                    return "—"
                            disp = r.get("grade_impact_display", "—")
                            if disp:
                                disp = disp.replace("🔺", "▲").replace("🟢", "▲").replace("🔻", "▼")
                            return disp or "—"

                        # Check if a copy button was clicked in Detailed Gradebook
                        gb_copy_click = st.session_state.get(f"btn_copy_gb_{sid}_{active_cid}_{reset_counter}")
                        if gb_copy_click:
                            r_idx = getattr(gb_copy_click, "row", None) if not isinstance(gb_copy_click, dict) else gb_copy_click.get("row")
                            if r_idx is not None and 0 <= r_idx < len(display_rows):
                                t_row = display_rows[r_idx]
                                url = t_row.get("url", "")
                                if url:
                                    copy_to_clipboard_js(url)
                                    st.toast(f"📋 Copied Canvas link for '{t_row['title']}' to clipboard!")
                                else:
                                    st.toast("⚠️ No Canvas link available for this assignment.")

                        if display_rows:
                            gb_df = pd.DataFrame([
                                {
                                    "Date": r["due_str"],
                                    "Assignment": r["title"],
                                    "Type": classify_assignment_type(r.get("group_name"), r.get("assignment")) or r.get("type", "—"),
                                    "Weight": r["weight_str"],
                                    "Score": r["score_str"],
                                    "What-If Score": overrides.get(r.get("assignment_id") or r["title"], None),
                                    "Impact": get_impact_display(r),
                                    "Max Gain": r.get("potential_gain_str", "—"),
                                    "Status": r["status_display"],
                                    "Notes": notes_backend.get_note(r.get("assignment_id") or r["title"]),
                                    "Copy": "📋 Copy" if r.get("url") else None,
                                }
                                for r in display_rows
                            ])

                            def style_gb_rows(row):
                                status_val = str(row.get("Status", ""))
                                impact_val = str(row.get("Impact", ""))
                                whatif_val = row.get("What-If Score")
                                if pd.notna(whatif_val) and str(whatif_val).strip() != "":
                                    return ["background-color: rgba(59, 130, 246, 0.12); font-weight: 600;"] * len(row)
                                elif "MISSING" in status_val:
                                    return ["background-color: rgba(239, 68, 68, 0.18); font-weight: 600;"] * len(row)
                                elif "▼" in impact_val:
                                    return ["background-color: rgba(239, 68, 68, 0.08);"] * len(row)
                                elif "▲" in impact_val:
                                    return ["background-color: rgba(34, 197, 94, 0.06);"] * len(row)
                                elif "Pending Review" in status_val:
                                    return ["background-color: rgba(234, 179, 8, 0.15);"] * len(row)
                                return [""] * len(row)

                            def style_impact_cells(val):
                                s = str(val)
                                if "▲" in s:
                                    return "color: #16a34a; font-weight: 700;"
                                elif "▼" in s:
                                    return "color: #dc2626; font-weight: 700;"
                                return ""

                            styled_gb_df = gb_df.style.apply(style_gb_rows, axis=1).map(style_impact_cells, subset=["Impact"])

                            gb_height = max(120, (len(gb_df) + 1) * 36 + 10)
                            edited_df = st.data_editor(
                                styled_gb_df,
                                width="stretch",
                                height=gb_height,
                                hide_index=True,
                                key=f"gb_editor_{sid}_{active_cid}_{reset_counter}",
                                disabled=[c for c in gb_df.columns if c not in ("What-If Score", "Notes")],
                                column_config={
                                    "Copy": st.column_config.ButtonColumn(
                                        "Copy",
                                        help="Click to copy Canvas assignment link directly to your clipboard",
                                        type="tertiary",
                                        key=f"btn_copy_gb_{sid}_{active_cid}_{reset_counter}"
                                    ),
                                    "Weight": st.column_config.TextColumn(
                                        "Weight",
                                        help="Weight of this assignment or category toward the final grade"
                                    ),
                                    "Score": st.column_config.TextColumn("Score"),
                                    "What-If Score": st.column_config.NumberColumn(
                                        "What-If Score",
                                        help="Enter a hypothetical score to simulate overall course grade below",
                                        min_value=0.0,
                                        step=1.0,
                                    ),
                                    "Impact": st.column_config.TextColumn(
                                        "Impact",
                                        help="Current net drag or boost this assignment has on the overall course grade compared to if it were excused."
                                    ),
                                    "Max Gain": st.column_config.TextColumn(
                                        "Max Gain",
                                        help="Potential percentage points gained if this assignment is retaken or completed for 100% full credit."
                                    ),
                                    "Status": st.column_config.TextColumn("Status"),
                                    "Notes": st.column_config.TextColumn(
                                        "Notes",
                                        help="Double-click cell to view or edit notes. Press Enter to save.",
                                        max_chars=1000,
                                    ),
                                }
                            )

                            # Synchronize What-If overrides & Notes from edited DataFrame
                            for idx, ed_row in edited_df.iterrows():
                                if idx < len(display_rows):
                                    orig_r = display_rows[idx]
                                    aid = orig_r.get("assignment_id") or orig_r["title"]
                                    val = ed_row.get("What-If Score")
                                    if pd.notna(val) and str(val).strip() != "":
                                        fval = float(val)
                                        if overrides.get(aid) != fval:
                                            overrides[aid] = fval
                                    elif aid in overrides and (pd.isna(val) or str(val).strip() == ""):
                                        del overrides[aid]

                                    val_note = str(ed_row.get("Notes") or "").strip()
                                    curr_note = notes_backend.get_note(aid)
                                    if val_note != curr_note:
                                        notes_backend.save_note(aid, val_note)
                                        st.toast(f"💾 Saved note for '{orig_r['title']}'")

                            # Underneath table: Simulated Grade Banner & Reset
                            if overrides and computed_overall is not None:
                                sim_grade = simulate_course_grade(
                                    rows, weighted, gb_data.get("group_weights"), score_overrides=overrides
                                )
                                if sim_grade is not None:
                                    delta = sim_grade - computed_overall
                                    with st.container(border=True):
                                        sim_c1, sim_c2, sim_c3, sim_c4 = st.columns([1.2, 1.2, 2.2, 1.2])
                                        sim_c1.metric("Official Grade", active_course_info["grade_str"])
                                        sim_c2.metric("Simulated Grade", f"{sim_grade:.2f}%", delta=f"{delta:+.2f}%")
                                        delta_color = "green" if delta > 0 else ("red" if delta < 0 else "blue")
                                        delta_sign = "+" if delta > 0 else ""
                                        sim_c3.markdown(
                                            f"**Simulated Impact:** :{delta_color}[**{delta_sign}{delta:.2f}%** overall grade change]<br>"
                                            f"<span style='color: #64748b; font-size: 0.88em;'>Active simulation on {len(overrides)} assignment(s)</span>",
                                            unsafe_allow_html=True
                                        )
                                        with sim_c4:
                                            st.write("")
                                            if st.button("🔄 Reset What-If", key=f"btn_reset_sim_table_{sid}_{active_cid}", help="Clear all hypothetical scores in table"):
                                                st.session_state[reset_cnt_key] = reset_counter + 1
                                                st.session_state[sim_key] = {}
                                                st.rerun()
                            else:
                                st.caption("💡 *Tip: Enter a score in any assignment's **What-If Score** column above to simulate its effect on your course grade in real time.*")
                        else:
                            if gb_filter == "Graded Items (Contributing)" and not graded_rows:
                                st.info(f"ℹ️ No graded items recorded yet for {cname}. (Total assignments in course: {len(rows)}). Switch to **'All Items'** to view upcoming assignments.")
                            else:
                                st.info("No assignments match the selected filter.")

                        # -----------------------------------------------------------
                        # 2. Category Health & Weight Breakdown (Compressed Table at Bottom)
                        # -----------------------------------------------------------
                        cat_health = gb_data.get("category_health") or []
                        if cat_health:
                            st.write("")
                            st.markdown("#### 📊 Category Health & Weight Breakdown")
                            cat_rows = []
                            for ch in cat_health:
                                ch_name = ch.get("name", "").lower()
                                c_icon = "📝 " if any(k in ch_name for k in ["assess", "test", "quiz", "major", "exam", "sclt"]) else ("🏠 " if "homework" in ch_name or "hw" in ch_name else "🏫 ")
                                wt_str = f"{ch['weight']:g}%" if weighted else "—"
                                if ch["is_active"] and ch["score_pct"] is not None:
                                    score_str = f"{ch['score_pct']:.1f}%"
                                    pts_str = f"{ch['earned']:g} / {ch['possible']:g}"
                                    items_str = f"{ch['graded_items']} graded"
                                    drag_val = ch.get("drag_vs_overall")
                                    if drag_val is not None:
                                        if drag_val <= -1.0:
                                            health_str = f"▼ {abs(drag_val):.1f}% below overall"
                                        elif drag_val >= 1.0:
                                            health_str = f"▲ +{drag_val:.1f}% above overall"
                                        else:
                                            health_str = "— On par with overall"
                                    else:
                                        health_str = "—"
                                    contrib_str = f"{ch['weighted_contribution']:.2f}%" if weighted else score_str
                                else:
                                    score_str = "—"
                                    pts_str = "—"
                                    items_str = "0 graded"
                                    health_str = "— No graded work yet"
                                    contrib_str = "—"

                                cat_rows.append({
                                    "Category": f"{c_icon}{ch['name']}",
                                    "Weight": wt_str,
                                    "Category Score": score_str,
                                    "Points Earned / Possible": pts_str,
                                    "Graded Items": items_str,
                                    "Health vs Overall": health_str,
                                    "Course Contribution": contrib_str,
                                })

                            cat_df = pd.DataFrame(cat_rows)

                            def style_cat_rows(row):
                                h_val = str(row.get("Health vs Overall", ""))
                                if "▼" in h_val:
                                    return ["background-color: rgba(239, 68, 68, 0.08);"] * len(row)
                                elif "▲" in h_val:
                                    return ["background-color: rgba(34, 197, 94, 0.06);"] * len(row)
                                return [""] * len(row)

                            def style_health_cells(val):
                                s = str(val)
                                if "▲" in s:
                                    return "color: #16a34a; font-weight: 700;"
                                elif "▼" in s:
                                    return "color: #dc2626; font-weight: 700;"
                                return ""

                            styled_cat_df = cat_df.style.apply(style_cat_rows, axis=1).map(style_health_cells, subset=["Health vs Overall"])
                            cat_height = max(80, (len(cat_df) + 1) * 36 + 8)
                            st.dataframe(
                                styled_cat_df,
                                width="stretch",
                                height=cat_height,
                                hide_index=True,
                                column_config={
                                    "Health vs Overall": st.column_config.TextColumn(
                                        "Health vs Overall",
                                        help="Compares category score to current overall grade. Red (▼) indicates this category is dragging down the final grade, and green (▲) indicates it is boosting it."
                                    ),
                                    "Course Contribution": st.column_config.TextColumn(
                                        "Course Contribution",
                                        help="Normalized percentage points this category contributes toward the overall course grade."
                                    )
                                }
                            )

                # Incomplete Assignments
                st.divider()
                st.subheader(f"📋 Incomplete Assignments ({len(assigns)})")

                status_filter = st.multiselect(
                    f"Filter by Status ({sname})",
                    options=["MISSING", "NO GRADE", "UNGRADED", "UPCOMING"],
                    default=["MISSING", "NO GRADE", "UNGRADED", "UPCOMING"],
                    key=f"filter_{sid}"
                )

                filtered_assigns = [a for a in assigns if a["status"] in status_filter]
                # 1) Order by due date chronologically (oldest/overdue first, undated last)
                filtered_assigns.sort(key=lambda x: parse_date(x["due_raw"]))

                if filtered_assigns:
                    STATUS_ICONS = {
                        "MISSING": "🚨 MISSING",
                        "NO GRADE": "⚠️ NO GRADE",
                        "UNGRADED": "📝 UNGRADED",
                        "UPCOMING": "⏳ UPCOMING",
                    }

                    # Check if a copy button was clicked in Incomplete Assignments
                    inc_copy_click = st.session_state.get(f"btn_copy_inc_{sid}")
                    if inc_copy_click:
                        r_idx = getattr(inc_copy_click, "row", None) if not isinstance(inc_copy_click, dict) else inc_copy_click.get("row")
                        if r_idx is not None and 0 <= r_idx < len(filtered_assigns):
                            t_assign = filtered_assigns[r_idx]
                            url = t_assign.get("url", "")
                            if url:
                                copy_to_clipboard_js(url)
                                st.toast(f"📋 Copied Canvas link for '{t_assign['title']}' to clipboard!")
                            else:
                                st.toast("⚠️ No Canvas link available for this assignment.")

                    # Columns: Status, Type, Course, Assignment, Due, Points, Notes, Copy
                    assign_df = pd.DataFrame([
                        {
                            "Status": STATUS_ICONS.get(a["status"], a["status"]),
                            "Type": a["type"],
                            "Course": clean_course_name(a["course"]),
                            "Assignment": a["title"],
                            "Due": a["due_str"],
                            "Points": a["points"],
                            "Notes": notes_backend.get_note(a.get("assignment_id") or a["title"]),
                            "Copy": "📋 Copy" if a.get("url") else None,
                        }
                        for a in filtered_assigns
                    ])

                    def style_incomplete_rows(row):
                        status_val = str(row.get("Status", ""))
                        if "MISSING" in status_val:
                            return ["background-color: rgba(239, 68, 68, 0.18); font-weight: 600;"] * len(row)
                        elif "NO GRADE" in status_val:
                            return ["background-color: rgba(245, 158, 11, 0.12);"] * len(row)
                        elif "UNGRADED" in status_val:
                            return ["background-color: rgba(59, 130, 246, 0.08);"] * len(row)
                        return [""] * len(row)

                    styled_assign_df = assign_df.style.apply(style_incomplete_rows, axis=1)

                    # 2) Expand table to full height of all rows (no scrolling in viewport)
                    assign_table_height = max(120, (len(filtered_assigns) + 1) * 36 + 10)

                    edited_assign_df = st.data_editor(
                        styled_assign_df,
                        width="stretch",
                        height=assign_table_height,
                        hide_index=True,
                        key=f"inc_editor_{sid}",
                        disabled=[c for c in assign_df.columns if c != "Notes"],
                        column_config={
                            "Copy": st.column_config.ButtonColumn(
                                "Copy",
                                help="Click to copy Canvas assignment link directly to your clipboard",
                                type="tertiary",
                                key=f"btn_copy_inc_{sid}"
                            ),
                            "Status": st.column_config.TextColumn("Status"),
                            "Notes": st.column_config.TextColumn(
                                "Notes",
                                help="Double-click cell to view or edit notes. Press Enter to save.",
                                max_chars=1000,
                            ),
                        }
                    )

                    # Synchronize Notes from edited DataFrame
                    for idx, ed_row in edited_assign_df.iterrows():
                        if idx < len(filtered_assigns):
                            orig_a = filtered_assigns[idx]
                            aid = orig_a.get("assignment_id") or orig_a["title"]
                            val_note = str(ed_row.get("Notes") or "").strip()
                            curr_note = notes_backend.get_note(aid)
                            if val_note != curr_note:
                                notes_backend.save_note(aid, val_note)
                                st.toast(f"💾 Saved note for '{orig_a['title']}'")
                else:
                    st.success("🎉 All caught up! No assignments matching the selected filters.")

                # Report download
                st.divider()
                st_html = build_student_html(sid, sname, assigns, ag_grades, grades)
                downloadable_html = wrap_html_report(sname, st_html)
                d1, d2, _ = st.columns([1, 1, 3])
                with d1:
                    st.download_button(
                        label="📄 Download HTML Report",
                        data=downloadable_html,
                        file_name=f"{sname.lower().replace(' ', '_')}_report.html",
                        mime="text/html",
                        key=f"dl_html_{sid}"
                    )
                with d2:
                    try:
                        pdf_data = html_to_pdf_bytes(st_html)
                        st.download_button(
                            label="📑 Download PDF Report",
                            data=pdf_data,
                            file_name=f"{sname.lower().replace(' ', '_')}_report.pdf",
                            mime="application/pdf",
                            key=f"dl_pdf_{sid}"
                        )
                    except Exception:
                        pass

    dashboard_fragment()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if is_streamlit_running():
        run_streamlit()
    else:
        main()
