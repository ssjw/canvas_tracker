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
        xdg_file = Path(xdg_home) / "canvas_tracker" / "config.toml"
        mac_app_support = Path.home() / "Library" / "Application Support" / "canvas_tracker" / "config.toml"
        local_file = Path.cwd() / "config.toml"
        script_file = Path(__file__).resolve().parent / "config.toml"

        if xdg_file.exists():
            target_file = xdg_file
        elif mac_app_support.exists():
            target_file = mac_app_support
        elif local_file.exists():
            target_file = local_file
        elif script_file.exists():
            target_file = script_file

    if target_file and target_file.exists() and tomllib:
        try:
            with open(target_file, "rb") as f:
                file_cfg = tomllib.load(f)
                for section in ["canvas", "refresh"]:
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


def classify_assignment_type(group_name):
    """Classify the assignment type based on the assignment group name."""
    if not group_name:
        return "Class Assignment"
    name_lower = group_name.lower()
    if "homework" in name_lower or name_lower == "hw":
        return "Homework"
    elif any(k in name_lower for k in ["assessment", "test", "quiz", "sclt", "quarterly"]):
        return "Assessment"
    elif any(k in name_lower for k in ["assignment", "class", "practice", "work"]):
        return "Class Assignment"
    else:
        return group_name.strip()


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

    from canvas_gradebook import build_gradebook_rows, sort_gradebook_rows, compute_overall

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

    # 4. Build and sort rows
    rows = build_gradebook_rows(submissions, groups_by_id, weighted, sid=student_id)
    rows = sort_gradebook_rows(rows)
    computed = compute_overall(rows, weighted, group_weights)

    return {
        "course_id": course_id,
        "course_info": course_info,
        "rows": rows,
        "weighted": weighted,
        "group_weights": group_weights,
        "groups_by_id": groups_by_id,
        "assignment_groups": ags,
        "computed_overall": computed,
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
                "course": course_name,
                "title": assignment.get("name", "Untitled"),
                "due_raw": due_at,
                "due_str": format_date(due_at),
                "points": assignment.get("points_possible", 0),
                "status": status,
                "url": assignment.get("html_url", ""),
                "type": classify_assignment_type(assignment.get("assignment_group_name"))
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


def run_streamlit():
    import streamlit as st
    import pandas as pd

    st.set_page_config(page_title="Canvas Student Tracker", page_icon="🎓", layout="wide")

    config = load_config()
    canvas_cfg = config["canvas"]
    refresh_cfg = config["refresh"]

    st.sidebar.title("🎓 Canvas Tracker")

    # Credentials
    base_url = st.sidebar.text_input("Canvas Base URL", value=canvas_cfg["base_url"])
    token = canvas_cfg.get("api_token") or ""
    if not token:
        token = st.sidebar.text_input("Canvas API Token", type="password", help="Enter your Canvas API token or configure it in .env")

    st.sidebar.divider()
    st.sidebar.subheader("🔄 Refresh Configuration")

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

    grades_only = st.sidebar.checkbox("Grades Only", value=False, help="Skip querying detailed assignment submissions")

    # Ad-hoc refresh button
    if st.sidebar.button("🔄 Refresh Now", width="stretch", type="primary"):
        st.session_state["force_refresh"] = True
        st.session_state.pop("canvas_data", None)
        if "cached_gradebooks" in st.session_state:
            st.session_state["cached_gradebooks"].clear()
        st.cache_data.clear()
        st.rerun()

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
                    data = collect_canvas_data(base_url, token, grades_only=grades_only)
                    st.session_state["canvas_data"] = data
                    st.session_state["last_fetch_time"] = datetime.now()
                    if "cached_gradebooks" in st.session_state:
                        st.session_state["cached_gradebooks"].clear()
                except Exception as e:
                    st.error(f"Error connecting to Canvas API: {e}")
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

        if not observees:
            st.warning("No observed students found on this Canvas account.")
            return

        st.caption(f"Last updated: **{fetched_at.strftime('%Y-%m-%d %I:%M:%S %p')}** | Refresh: **Every {interval_min}m** (Active: **{active_start} – {active_end}**)")

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
                m2.metric("Missing", missing_cnt, delta=f"-{missing_cnt}" if missing_cnt > 0 else None, delta_color="inverse")
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

                    gradebook_cache = st.session_state.setdefault("cached_gradebooks", {})
                    gb_key = (sid, active_cid)
                    gb_data = gradebook_cache.get(gb_key)

                    needs_refresh = (
                        gb_data is None
                        or "assignment_groups" not in gb_data
                        or any(isinstance(v, (int, float)) for v in gb_data.get("groups_by_id", {}).values())
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
                        computed_overall = gb_data["computed_overall"]

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

                        # Check for graded items
                        graded_rows = [
                            r for r in rows
                            if r.get("score") is not None
                            and not r.get("excused")
                            and not r.get("omit_from_final_grade")
                            and r.get("workflow_state") != "pending_review"
                            and r.get("status") != "UNGRADED"
                        ]

                        gb_filter = st.segmented_control(
                            f"Filter assignments for {cname}:",
                            options=["All Items", "Graded Items (Contributing)", "Incomplete Only"],
                            default="All Items",
                            key=f"gb_filter_{sid}_{active_cid}"
                        ) or "All Items"

                        if gb_filter == "Graded Items (Contributing)":
                            display_rows = graded_rows
                        elif gb_filter == "Incomplete Only":
                            display_rows = [
                                r for r in rows
                                if r.get("status") in ["MISSING", "NO GRADE", "UNGRADED", "UPCOMING"]
                            ]
                        else:
                            display_rows = rows

                        if display_rows:
                            gb_df = pd.DataFrame([
                                {
                                    "Date": r["due_str"],
                                    "Assignment": r["title"],
                                    "Type": r["type"],
                                    "Weight": r["weight_str"],
                                    "Score": r["score_str"],
                                    "Contribution": r["contribution_str"],
                                    "Status": r["status_display"],
                                    "Canvas Link": r["url"]
                                }
                                for r in display_rows
                            ])

                            def style_gb_rows(row):
                                status_val = str(row.get("Status", ""))
                                if "MISSING" in status_val:
                                    return ["background-color: rgba(239, 68, 68, 0.15);"] * len(row)
                                elif "Pending Review" in status_val:
                                    return ["background-color: rgba(234, 179, 8, 0.15);"] * len(row)
                                return [""] * len(row)

                            styled_gb_df = gb_df.style.apply(style_gb_rows, axis=1)

                            gb_height = max(120, (len(gb_df) + 1) * 36 + 10)
                            st.dataframe(
                                styled_gb_df,
                                width="stretch",
                                height=gb_height,
                                hide_index=True,
                                column_config={
                                    "Canvas Link": st.column_config.LinkColumn("Canvas Link", display_text="Open in Canvas"),
                                    "Weight": st.column_config.TextColumn(
                                        "Weight",
                                        help="Weight of this assignment or category toward the final grade"
                                    ),
                                    "Contribution": st.column_config.TextColumn(
                                        "Contribution",
                                        help="Points or percentage this item contributes toward your final grade (Score % × Weight)"
                                    ),
                                    "Score": st.column_config.TextColumn("Score"),
                                    "Status": st.column_config.TextColumn("Status"),
                                }
                            )
                        else:
                            if gb_filter == "Graded Items (Contributing)" and not graded_rows:
                                st.info(f"ℹ️ No graded items recorded yet for {cname}. (Total assignments in course: {len(rows)}). Switch to **'All Items'** to view upcoming assignments.")
                            else:
                                st.info("No assignments match the selected filter.")

                # Incomplete Assignments
                if not grades_only:
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

                        # Original column order: Status, Type, Course, Assignment, Due, Points, Canvas Link
                        assign_df = pd.DataFrame([
                            {
                                "Status": STATUS_ICONS.get(a["status"], a["status"]),
                                "Type": a["type"],
                                "Course": clean_course_name(a["course"]),
                                "Assignment": a["title"],
                                "Due": a["due_str"],
                                "Points": a["points"],
                                "Canvas Link": a["url"]
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

                        st.dataframe(
                            styled_assign_df,
                            width="stretch",
                            height=assign_table_height,
                            hide_index=True,
                            column_config={
                                "Canvas Link": st.column_config.LinkColumn("Canvas Link", display_text="Open in Canvas"),
                                "Status": st.column_config.TextColumn("Status"),
                            }
                        )
                    else:
                        st.success("🎉 All caught up! No assignments matching the selected filters.")

                # Report download
                st.divider()
                st_html = build_student_html(sid, sname, assigns, ag_grades, grades, grades_only=grades_only)
                downloadable_html = wrap_html_report(sname, st_html, grades_only=grades_only)
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
