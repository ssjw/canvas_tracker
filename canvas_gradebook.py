#!/usr/bin/env python3
"""
Canvas Observer Gradebook CLI

A standalone interactive companion CLI for parents/observers to inspect detailed
gradebooks for observed students on Canvas LMS (e.g. AACPS Canvas).

Potential Security Implications:
1. Canvas API tokens provide full programmatic access to your account.
   Store them in environment variables (e.g., using a .env file) rather
   than hardcoding. Avoid checking .env into version control.
2. Ensure SSL/TLS verification is enabled on all outbound requests.
"""

import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Ensure local environment variables are loaded prior to any downstream imports
load_dotenv()

import requests
from rich.console import Console
from rich.table import Table

import canvas_tracker
from canvas_tracker import (
    get_next_url,
    fetch_all,
    format_date,
    parse_date,
    classify_assignment_type,
    load_config,
)

_config = load_config()
CANVAS_BASE_URL = (os.getenv("CANVAS_BASE_URL") or _config["canvas"]["base_url"]).rstrip("/")
CANVAS_API_TOKEN = os.getenv("CANVAS_API_TOKEN") or _config["canvas"]["api_token"]


# ---------------------------------------------------------------------------
# Pure Calculation & Classification Functions (Network-Free)
# ---------------------------------------------------------------------------

def compute_contribution(score, points_possible, group_weight, weighted):
    """
    Compute an individual assignment's contribution toward the total course grade.

    weighted=True:  (score / points_possible) * group_weight
                    (percentage points contributed toward the final 100)
    weighted=False: (score / points_possible) * 100
                    (percentage earned on the assignment)

    Returns None when score is None, points_possible <= 0, or group_weight is None (in weighted mode).
    """
    if score is None or points_possible is None:
        return None

    try:
        s = float(score)
        p = float(points_possible)
    except (ValueError, TypeError):
        return None

    if p <= 0:
        return None

    if weighted:
        if group_weight is None:
            return None
        try:
            w = float(group_weight)
        except (ValueError, TypeError):
            return None
        return (s / p) * w
    else:
        return (s / p) * 100.0


def compute_overall(rows, weighted, group_weights=None):
    """
    Compute the overall grade percentage from a list of submission row dictionaries.

    weighted=True:  Per-group earned/possible ratio:
                    overall = sum(group_weight * ratio) / sum(group_weight) * 100
                    (only groups with points_possible > 0, skipping omit_from_final_grade and excused)
    weighted=False: sum(score) / sum(points_possible) * 100 across all graded rows

    Returns None if no graded data exists.
    """
    if not rows:
        return None

    if weighted:
        group_earned = {}
        group_possible = {}
        group_wt_map = {}

        for row in rows:
            if row.get("omit_from_final_grade") or row.get("excused"):
                continue

            score = row.get("score")
            points_possible = row.get("points_possible")
            if score is None or points_possible is None:
                continue

            try:
                s = float(score)
                p = float(points_possible)
            except (ValueError, TypeError):
                continue

            if p <= 0:
                continue

            gid = row.get("group_id") if row.get("group_id") is not None else row.get("assignment_group_id")
            wt = None
            if group_weights and gid in group_weights:
                wt = group_weights[gid]
            elif row.get("group_weight") is not None:
                wt = row.get("group_weight")

            if wt is None:
                continue

            try:
                wt = float(wt)
            except (ValueError, TypeError):
                continue

            if gid not in group_earned:
                group_earned[gid] = 0.0
                group_possible[gid] = 0.0
                group_wt_map[gid] = wt

            group_earned[gid] += s
            group_possible[gid] += p

        total_weighted_points = 0.0
        total_weight = 0.0
        for gid, p_sum in group_possible.items():
            if p_sum > 0:
                wt = group_wt_map[gid]
                if wt > 0:
                    ratio = group_earned[gid] / p_sum
                    total_weighted_points += wt * ratio
                    total_weight += wt

        if total_weight > 0:
            return (total_weighted_points / total_weight) * 100.0
        return None
    else:
        total_score = 0.0
        total_possible = 0.0
        has_graded = False

        for row in rows:
            if row.get("omit_from_final_grade") or row.get("excused"):
                continue

            score = row.get("score")
            points_possible = row.get("points_possible")
            if score is None or points_possible is None:
                continue

            try:
                s = float(score)
                p = float(points_possible)
            except (ValueError, TypeError):
                continue

            if p <= 0:
                continue

            total_score += s
            total_possible += p
            has_graded = True

        if has_graded and total_possible > 0:
            return (total_score / total_possible) * 100.0
        return None


def status_for(sub):
    """
    Derive the assignment status category: MISSING, NO GRADE, UNGRADED, UPCOMING, or GRADED.
    """
    if not isinstance(sub, dict):
        return "NO GRADE"

    is_canvas_missing = bool(sub.get("missing", False))
    workflow_state = sub.get("workflow_state", "unsubmitted")
    submitted_at = sub.get("submitted_at")
    grade = sub.get("grade")
    raw_score = sub.get("score")

    has_submission = (submitted_at is not None or workflow_state == "submitted")
    has_grade = (grade is not None or raw_score is not None or workflow_state == "graded")

    is_zero_grade = False
    if grade is not None:
        try:
            if float(grade) == 0:
                is_zero_grade = True
        except (ValueError, TypeError):
            pass
    elif raw_score is not None:
        try:
            if float(raw_score) == 0:
                is_zero_grade = True
        except (ValueError, TypeError):
            pass

    if is_canvas_missing or is_zero_grade:
        return "MISSING"
    elif has_submission and not has_grade:
        return "UNGRADED"
    elif has_grade:
        return "GRADED"

    assignment = sub.get("assignment") or {}
    due_at = assignment.get("due_at") or sub.get("due_at")
    if not due_at:
        return "NO GRADE"

    due_dt = parse_date(due_at)
    now = datetime.now(timezone.utc)
    if due_dt == datetime.max.replace(tzinfo=timezone.utc):
        return "NO GRADE"
    elif due_dt < now:
        return "NO GRADE"
    else:
        return "UPCOMING"


def build_gradebook_rows(submissions, groups_by_id, weighted, sid=None):
    """
    Construct standardized gradebook row dictionaries from raw Canvas submissions.
    """
    rows = []
    for sub in submissions:
        if not isinstance(sub, dict):
            continue
        if sid is not None and sub.get("user_id") is not None:
            if sub.get("user_id") != sid:
                continue

        assignment = sub.get("assignment") or {}
        ag_id = assignment.get("assignment_group_id")

        group_info = groups_by_id.get(ag_id, {})
        if isinstance(group_info, dict):
            group_name = group_info.get("name", "")
            group_weight = group_info.get("group_weight")
        elif isinstance(group_info, (int, float)):
            group_name = ""
            group_weight = float(group_info)
        elif isinstance(group_info, str):
            group_name = group_info
            group_weight = None
        else:
            group_name = ""
            group_weight = None

        if not group_name:
            group_name = assignment.get("assignment_group_name", "")

        due_at = assignment.get("due_at") or sub.get("due_at")
        points_possible = assignment.get("points_possible")
        raw_score = sub.get("score")
        grade = sub.get("grade")

        score = None
        if raw_score is not None:
            try:
                score = float(raw_score)
            except (ValueError, TypeError):
                score = None
        elif grade is not None:
            try:
                score = float(grade)
            except (ValueError, TypeError):
                score = None

        if points_possible is not None:
            try:
                points_possible = float(points_possible)
            except (ValueError, TypeError):
                points_possible = None

        excused = bool(sub.get("excused", False))
        omit = bool(assignment.get("omit_from_final_grade", False))

        status = status_for(sub)

        # Contribution calculation
        if excused or omit:
            contribution = None
        else:
            contribution = compute_contribution(score, points_possible, group_weight, weighted)

        # Weight display string
        if weighted and group_weight is not None:
            weight_str = f"{group_weight:g}%"
        else:
            weight_str = "—"

        # Score display string
        if excused:
            score_str = "Excused"
        elif score is not None:
            if points_possible is not None:
                score_str = f"{score:g}/{points_possible:g}"
            else:
                score_str = f"{score:g}"
        else:
            if points_possible is not None:
                score_str = f"—/{points_possible:g}"
            else:
                score_str = "—"

        # Contribution display string
        if contribution is not None:
            contribution_str = f"{contribution:.2f}%"
        else:
            contribution_str = "—"

        status_display = f"{status} (excluded)" if omit else status

        row = {
            "assignment_id": assignment.get("id"),
            "title": assignment.get("name", "Untitled"),
            "due_raw": due_at,
            "due_str": format_date(due_at),
            "type": classify_assignment_type(group_name),
            "group_id": ag_id,
            "group_name": group_name,
            "group_weight": group_weight,
            "weight_str": weight_str,
            "score": score,
            "points_possible": points_possible,
            "excused": excused,
            "omit_from_final_grade": omit,
            "score_str": score_str,
            "contribution": contribution,
            "contribution_str": contribution_str,
            "status": status,
            "status_display": status_display,
            "url": assignment.get("html_url", ""),
            "submission": sub,
            "assignment": assignment,
        }
        rows.append(row)
    return rows


def sort_gradebook_rows(rows):
    """Sort rows by due date: oldest first, undated items last."""
    return sorted(rows, key=lambda r: parse_date(r.get("due_raw")))


# ---------------------------------------------------------------------------
# Terminal Rendering
# ---------------------------------------------------------------------------

def display_gradebook(rows, child_name, course_name, weighted, group_weights, enrollment_grade, console=None):
    """Render the full gradebook table and overall summary footer to the terminal."""
    if console is None:
        console = Console()

    table = Table(title=f"Gradebook: {child_name} — {course_name}")
    table.add_column("Date", style="green", no_wrap=True)
    table.add_column("Assignment", style="magenta")
    table.add_column("Type", justify="center")
    table.add_column("Weight", justify="right", style="yellow")
    table.add_column("Score", justify="right", style="white")
    table.add_column("Contribution", justify="right", style="cyan")
    table.add_column("Status", justify="center")

    for item in rows:
        item_type = item["type"]
        if item_type == "Homework":
            type_formatted = "[bold bright_blue]🏠 Homework[/bold bright_blue]"
        elif item_type == "Class Assignment":
            type_formatted = "[bold cyan]🏫 Class Assignment[/bold cyan]"
        elif item_type == "Assessment":
            type_formatted = "[bold bright_magenta]📝 Assessment[/bold bright_magenta]"
        else:
            type_formatted = f"[white]{item_type}[/white]"

        status_raw = item["status"]
        if status_raw == "MISSING":
            status_formatted = "[bold red]🔴 MISSING[/bold red]"
        elif status_raw == "NO GRADE":
            status_formatted = "[bold orange3]🟠 NO GRADE[/bold orange3]"
        elif status_raw == "UNGRADED":
            status_formatted = "[bold cyan]🔵 UNGRADED[/bold cyan]"
        elif status_raw == "UPCOMING":
            status_formatted = "[bold yellow]🟡 UPCOMING[/bold yellow]"
        elif status_raw == "GRADED":
            status_formatted = "[bold green]🟢 GRADED[/bold green]"
        else:
            status_formatted = status_raw

        if item.get("omit_from_final_grade"):
            status_formatted += " [dim](excluded)[/dim]"

        score_display = item["score_str"]
        if item["excused"]:
            score_display = "[italic]Excused[/italic]"

        table.add_row(
            item["due_str"],
            item["title"],
            type_formatted,
            item["weight_str"],
            score_display,
            item["contribution_str"],
            status_formatted,
        )

    console.print()
    console.print(table)
    console.print()

    # Determine overall grade summary
    num_score = None
    overall_str = None

    if enrollment_grade:
        canvas_score = enrollment_grade.get("score")
        canvas_grade = enrollment_grade.get("grade")
        if canvas_score is not None and canvas_grade is not None:
            overall_str = f"{canvas_score}% ({canvas_grade})"
            try:
                num_score = float(canvas_score)
            except (ValueError, TypeError):
                pass
        elif canvas_score is not None:
            overall_str = f"{canvas_score}%"
            try:
                num_score = float(canvas_score)
            except (ValueError, TypeError):
                pass
        elif canvas_grade is not None:
            overall_str = f"{canvas_grade}"

    if overall_str is None:
        computed = compute_overall(rows, weighted, group_weights)
        if computed is not None:
            overall_str = f"{computed:.2f}%"
            num_score = computed
        else:
            overall_str = "—"

    points_summary = ""
    if not weighted:
        total_score = sum(
            r["score"] for r in rows
            if r["score"] is not None and not r["excused"] and not r["omit_from_final_grade"]
        )
        total_possible = sum(
            r["points_possible"] for r in rows
            if r["score"] is not None and r["points_possible"] is not None and not r["excused"] and not r["omit_from_final_grade"]
        )
        if total_possible > 0:
            points_summary = f" (Total: {total_score:g} / {total_possible:g} pts)"

    if num_score is not None:
        if num_score >= 90:
            color = "bold green"
        elif num_score >= 80:
            color = "bold yellow"
        elif num_score >= 70:
            color = "bold orange3"
        else:
            color = "bold red"
    else:
        color = "bold white"

    console.print(f"[{color}]Overall Grade: {overall_str}[/{color}]{points_summary}\n")


# ---------------------------------------------------------------------------
# Interactive Menu CLI
# ---------------------------------------------------------------------------

def main():
    console = Console()
    console_err = Console(stderr=True)

    if not CANVAS_API_TOKEN:
        console_err.print("[red]Error: CANVAS_API_TOKEN is not set.[/red]")
        console_err.print("[yellow]Please create a .env file with your token, configure ~/.config/canvas_tracker/config.toml, or export it in your shell.[/yellow]")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {CANVAS_API_TOKEN}"})

    console.print("[cyan]Connecting to Canvas API...[/cyan]")

    try:
        observees = fetch_all(session, f"{CANVAS_BASE_URL}/api/v1/users/self/observees")
    except Exception as e:
        console_err.print(f"[red]Error connecting to Canvas API: {e}[/red]")
        console_err.print("[yellow]Please check your CANVAS_BASE_URL and CANVAS_API_TOKEN configuration.[/yellow]")
        sys.exit(1)

    if not observees:
        console.print("[yellow]No observed students found on this account.[/yellow]")
        sys.exit(0)

    observees = sorted(observees, key=lambda s: s.get("name", "").lower())

    while True:
        console.print("\n[bold]Select an observed child:[/bold]")
        for idx, student in enumerate(observees, 1):
            console.print(f"  [{idx}] {student.get('name', 'Unknown')} (ID: {student.get('id')})")
        console.print("  [q] Quit")

        choice = input(f"\nEnter choice [1-{len(observees)}, q]: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            console.print("[cyan]Goodbye![/cyan]")
            break

        try:
            student_idx = int(choice) - 1
            if not (0 <= student_idx < len(observees)):
                console.print("[red]Invalid selection. Please enter a valid number.[/red]")
                continue
        except ValueError:
            console.print("[red]Invalid input. Please enter a number or 'q' to quit.[/red]")
            continue

        selected_student = observees[student_idx]
        sid = selected_student["id"]
        sname = selected_student.get("name", "Unknown")

        console.print(f"\n[cyan]Fetching active courses for {sname}...[/cyan]")
        try:
            courses = fetch_all(
                session,
                f"{CANVAS_BASE_URL}/api/v1/courses",
                params={"enrollment_state": "active", "include[]": ["enrollments"]}
            )
        except Exception as e:
            console_err.print(f"[red]Error fetching active courses: {e}[/red]")
            continue

        child_courses = []
        for c in courses:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            enrollments = c.get("enrollments", [])
            for e in enrollments:
                if e.get("type") == "observer" and e.get("associated_user_id") == sid:
                    child_courses.append(c)
                    break

        if not child_courses:
            console.print(f"[yellow]No active courses found for {sname}.[/yellow]")
            continue

        child_courses = sorted(child_courses, key=lambda c: c.get("name", "").lower())

        while True:
            console.print(f"\n[bold]Select a course for {sname}:[/bold]")
            for c_idx, course in enumerate(child_courses, 1):
                c_code = course.get("course_code")
                c_name = course.get("name", "Untitled")
                label = f"{c_name} ({c_code})" if c_code else c_name
                console.print(f"  [{c_idx}] {label}")
            console.print("  [b] Back to student selection")
            console.print("  [q] Quit")

            c_choice = input(f"\nEnter choice [1-{len(child_courses)}, b, q]: ").strip().lower()
            if c_choice in ("q", "quit", "exit"):
                console.print("[cyan]Goodbye![/cyan]")
                return
            if c_choice in ("b", "back"):
                break

            try:
                course_num = int(c_choice) - 1
                if not (0 <= course_num < len(child_courses)):
                    console.print("[red]Invalid selection. Please enter a valid number.[/red]")
                    continue
            except ValueError:
                console.print("[red]Invalid input. Please enter a number, 'b', or 'q'.[/red]")
                continue

            selected_course = child_courses[course_num]
            cid = selected_course["id"]
            cname = selected_course.get("name", "Untitled")

            console.print(f"\n[cyan]Fetching gradebook details for {cname}...[/cyan]")

            # 1. Submissions
            try:
                submissions = fetch_all(
                    session,
                    f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/students/submissions",
                    params={"student_ids[]": [sid], "include[]": ["assignment"]}
                )
            except Exception as e:
                console_err.print(f"[red]Error fetching submissions: {e}[/red]")
                submissions = []

            # 2. Assignment groups
            try:
                ags = fetch_all(session, f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/assignment_groups")
            except Exception as e:
                console_err.print(f"[red]Error fetching assignment groups: {e}[/red]")
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

            weighted = bool(selected_course.get("apply_assignment_group_weights")) or any(
                wt > 0 for wt in group_weights.values()
            )

            # 3. Overall enrollment grade
            enrollment_grade = None
            try:
                enrolls = fetch_all(
                    session,
                    f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/enrollments",
                    params={"user_id": sid}
                )
                for e in enrolls:
                    if isinstance(e, dict):
                        grades = e.get("grades", {})
                        cp_score = grades.get("current_period_computed_current_score")
                        cp_grade = grades.get("current_period_computed_current_grade")
                        c_score = cp_score if cp_score is not None else grades.get("current_score")
                        c_grade = cp_grade if cp_grade is not None else grades.get("current_grade")
                        if c_score is not None or c_grade is not None:
                            enrollment_grade = {
                                "score": c_score,
                                "grade": c_grade
                            }
                            break
            except Exception:
                pass

            rows = build_gradebook_rows(submissions, groups_by_id, weighted, sid=sid)
            rows = sort_gradebook_rows(rows)

            if not rows:
                console.print(f"[yellow]No assignments recorded for {cname}.[/yellow]")
            else:
                display_gradebook(
                    rows,
                    sname,
                    cname,
                    weighted,
                    group_weights,
                    enrollment_grade,
                    console=console
                )


if __name__ == "__main__":
    main()
