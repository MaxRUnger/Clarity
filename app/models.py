"""Domain models / data-access helpers.

These classes are intentionally thin façades over Supabase queries. They are
*not* ORM models — they exist to:

  - Centralize the Supabase select shape so routes don't accidentally over-
    select columns (every byte saved here matters at scale and on free tiers).
  - Encapsulate the grading rules (M/MR/RQ semantics, HW thresholds,
    revision eligibility) so the same rule is enforced everywhere.

All write paths use the service-role client (``supabase_admin``); ownership /
authorization is enforced upstream by helpers in ``app/routes.py``.
"""

import logging
import re
from datetime import date
from typing import List, Optional, Dict, Any

from app.authentication import supabase_admin
from app.lo_order import lo_sort_key
from app.paging import fetch_all_rows

logger = logging.getLogger(__name__)

# Valid mastery grade codes used throughout the grading system.
# - "MR" mastered-on-revision counts toward required masteries like "M" but is
#   surfaced distinctly so instructors can see who revised vs aced on first try.
# - "I" is a transient state used by auto-convert-M when HW is below threshold;
#   it is restored to "M" automatically once HW rises (see routes.save_hw_percentage).
MASTERY_GRADES = ('M', 'MR', 'R', 'RQ', 'P', 'X', 'A', 'I')

# Assignment type labels stored on assignments.assignment_type.
# All three types share the same LO selection and grading path.
ASSIGNMENT_TYPES = ('mastery_opp', 'exam', 'project')


class Course:
    @staticmethod
    def get_all_for_instructor(instructor_id):
        """Fetches all classes taught by a specific instructor."""
        response = supabase_admin.table("classes").select("*").eq("instructor_id", instructor_id).execute()
        return response.data

    @staticmethod
    def get_lo_ids_for_class(class_id):
        """Return learning-objective IDs for a class."""
        try:
            resp = (
                supabase_admin.table("learning_objectives")
                .select("id")
                .eq("class_id", class_id)
                .execute()
            )
            return [lo["id"] for lo in (resp.data or []) if lo.get("id")]
        except Exception as e:
            logger.error("get_lo_ids_for_class failed for class %s: %s", class_id, e)
            return []

    @staticmethod
    def get_all_lo_ids_for_class(class_id):
        """Return every LO id for the class.

        Use for cleanup paths (delete student/class, HW-pass promotion).
        """
        try:
            resp = (
                supabase_admin.table("learning_objectives")
                .select("id")
                .eq("class_id", class_id)
                .execute()
            )
            return [lo["id"] for lo in (resp.data or []) if lo.get("id")]
        except Exception as e:
            logger.error(
                "get_all_lo_ids_for_class failed for class %s: %s", class_id, e
            )
            return []

    @staticmethod
    def get_learning_objectives(class_id):
        """Return learning objectives for a class."""
        try:
            resp = (
                supabase_admin.table("learning_objectives")
                .select("id, name, vendor_code, description, required_ms")
                .eq("class_id", class_id)
                .execute()
            )
            rows = list(resp.data or [])
            rows.sort(key=lo_sort_key)
            return rows
        except Exception as e:
            logger.error(
                "get_learning_objectives failed for class %s: %s", class_id, e
            )
            return []

    @staticmethod
    def get_full_class_data(class_id):
        """Fetches a class, its learning objectives, and all enrolled students with their grades.

        The class LO pool always comes from ``get_learning_objectives``. Nested
        ``classes → learning_objectives`` embeds are not used for the pool.
        """
        try:
            response = (
                supabase_admin.table("classes")
                .select(
                    "id, name, semester, days, section_number, "
                    "auto_convert_m, is_online"
                )
                .eq("id", class_id)
                .execute()
            )
            if not response.data:
                return None

            class_data = response.data[0]
            pool_los = Course.get_learning_objectives(class_id) or []
            class_data["learning_objectives"] = pool_los
            class_lo_ids = {
                str(lo.get("id"))
                for lo in pool_los
                if lo.get("id")
            }

            enrollments_resp = supabase_admin.table("enrollments").select(
                "id, class_id, student_id, muted, profiles(id, full_name, role, email)"
            ).eq("class_id", class_id).execute()
            enrollments = enrollments_resp.data or []

            # One in_() instead of one query per enrolled student. Grades are
            # limited to this class's learning objectives in the query below.
            student_ids = [
                e['profiles']['id']
                for e in enrollments
                if isinstance(e.get('profiles'), dict) and e['profiles'].get('id')
            ]
            grades_by_student = {}
            if student_ids:
                # Empty pool: do not load grades. An empty id list must not fall
                # open and pull other classes' rows for these students.
                grade_rows: List[Dict[str, Any]] = []
                if class_lo_ids:
                    lo_id_list = list(class_lo_ids)
                    grade_rows = fetch_all_rows(
                        supabase_admin.table("grades").select(
                            "student_id, learning_objective_id, top_score, second_score, "
                            "counts_for_mastery, "
                            "learning_objectives(id, name, vendor_code, required_ms)"
                        ).in_("student_id", student_ids).in_(
                            "learning_objective_id", lo_id_list
                        ).order("id")
                    )
                for g in grade_rows:
                    grades_by_student.setdefault(g["student_id"], []).append(g)

            for enrollment in enrollments:
                profile = enrollment.get('profiles')
                if isinstance(profile, dict) and profile.get('id'):
                    profile['grades'] = grades_by_student.get(profile['id'], [])

            class_data['enrollments'] = enrollments
            return class_data

        except Exception as e:
            logger.error("Database error in get_full_class_data: %s", e, exc_info=True)
            return None

    @staticmethod
    def get_assignment_lo_ids(assignment_id):
        """Return learning_objective_id strings linked via assignment_objectives.

        Model-layer equivalent of routes._assignment_lo_ids (no request cache).
        """
        if not assignment_id:
            return []
        try:
            ao_resp = (
                supabase_admin.table("assignment_objectives")
                .select("learning_objective_id")
                .eq("assignment_id", assignment_id)
                .execute()
            )
            return [
                str(r["learning_objective_id"])
                for r in (ao_resp.data or [])
                if r.get("learning_objective_id")
            ]
        except Exception as e:
            logger.error(
                "get_assignment_lo_ids: failed for assignment %s: %s",
                assignment_id, e,
            )
            return []


class Grade:
    @staticmethod
    def is_mastery_mark(mark):
        """True for M and MR (both count as a demonstrated mastery for an assignment)."""
        return mark in ('M', 'MR')

    @staticmethod
    def get_priority(mark):
        """Maps letter grades to numerical priorities."""
        priorities = {'M': 5, 'MR': 5, 'R': 4, 'RQ': 3, 'P': 2, 'X': 1, 'I': 1, 'A': 0}
        return priorities.get(mark, -1)

    @staticmethod
    def is_mastered(top_score, second_score):
        """True when both cells on a row are mastery-level (M or MR)."""
        return Grade.is_mastery_mark(top_score) and Grade.is_mastery_mark(second_score)

    @staticmethod
    def normalize_score(score):
        """Normalize an incoming grade value to the allowed mastery codes.

        Accepts numeric scores (e.g. 82, 99.2) and converts them to a mastery band.
        Also accepts already-normalized values like 'M', 'MR', 'P', 'X', or 'R'.
        """
        if score is None:
            return None

        if isinstance(score, (int, float)) or (isinstance(score, str) and score.replace('.', '', 1).isdigit()):
            try:
                val = float(score)
                # Simple mapping: high values become Mastered, else needs review
                if val >= 70:
                    return 'M'
                if val >= 50:
                    return 'R'
                return 'P'
            except (TypeError, ValueError):
                pass

        # Otherwise assume it's already one of the allowed codes
        if isinstance(score, str):
            score = score.strip().upper()
            if score in ('M', 'MR', 'R', 'RQ', 'P', 'X', 'A', 'I'):
                return score

        return None

    @staticmethod
    def get_overdue_revisions(class_id):
        """Return R and RQ grades where the assignment's revision_due date has passed
        and the student was eligible to revise to mastery (HW >= 75, pass does not qualify).

        Batched: loads all needed assignment/homework-group data and homework_scores in a
        small fixed number of queries instead of per-assignment queries inside a loop.
        """
        today = date.today().isoformat()
        try:
            lo_ids = Course.get_all_lo_ids_for_class(class_id)
            if not lo_ids:
                return []
            grade_rows = fetch_all_rows(
                supabase_admin.table("grades").select(
                    "student_id, top_score, assignment_id, hw_score_at_entry, "
                    "assignments(id, name, revision_due), "
                    "learning_objectives(id, name, vendor_code)"
                ).in_("top_score", ["R", "RQ"]).in_(
                    "learning_objective_id", lo_ids
                ).order("id")
            )

            assignment_ids = sorted({
                g.get('assignment_id') for g in grade_rows if g.get('assignment_id')
            })
            if not assignment_ids:
                return []

            # Load homework_group for every needed assignment in one query.
            try:
                hg_resp = (
                    supabase_admin.table("assignments")
                    .select("id, homework_group, class_id")
                    .in_("id", assignment_ids)
                    .eq("class_id", class_id)
                    .execute()
                )
            except Exception as e:
                logger.error("Bulk assignment homework_group lookup failed: %s", e)
                hg_resp = None
            hg_by_assignment: Dict[str, str] = {}
            for row in ((hg_resp.data if hg_resp else None) or []):
                aid = row.get('id')
                hg = (row.get('homework_group') or '').strip() or (aid or '')
                if aid:
                    hg_by_assignment[aid] = hg

            # Determine sibling assignment IDs (legacy keys) for each homework group, in one query per class.
            unique_groups = sorted({v for v in hg_by_assignment.values() if v})
            sibling_ids_by_group: Dict[str, List[str]] = {}
            if unique_groups:
                try:
                    sib_resp = (
                        supabase_admin.table("assignments")
                        .select("id, homework_group")
                        .eq("class_id", class_id)
                        .in_("homework_group", unique_groups)
                        .execute()
                    )
                    for row in (sib_resp.data or []):
                        g = (row.get('homework_group') or '').strip()
                        rid = row.get('id')
                        if not g or not rid:
                            continue
                        sibling_ids_by_group.setdefault(g, []).append(rid)
                except Exception as e:
                    logger.error("Bulk sibling-assignment lookup failed: %s", e)

            # Collect every key under which homework_scores might exist for these groups.
            group_keys: set = set()
            for g, sibs in sibling_ids_by_group.items():
                group_keys.add(g)
                for sid in sibs:
                    group_keys.add(sid)
            for aid in assignment_ids:
                group_keys.add(aid)

            scores_by_key: Dict[str, Dict[str, Any]] = {}
            if group_keys:
                try:
                    CHUNK = 200
                    keys_list = list(group_keys)
                    for i in range(0, len(keys_list), CHUNK):
                        batch = keys_list[i:i + CHUNK]
                        sresp = (
                            supabase_admin.table("homework_scores")
                            .select("student_id, score_pct, homework_group")
                            .eq("class_id", class_id)
                            .in_("homework_group", batch)
                            .execute()
                        )
                        for r in (sresp.data or []):
                            hg = r.get('homework_group')
                            sid = r.get('student_id')
                            if not hg or not sid:
                                continue
                            scores_by_key.setdefault(hg, {})[sid] = r.get('score_pct')
                except Exception as e:
                    logger.error("Bulk homework_scores lookup failed: %s", e)

            def _hw_map_for_assignment(aid: str) -> Dict[str, Any]:
                """Match the same precedence as Homework.get_hw_scores_map_for_assignment.

                The order matters: legacy rows keyed by sibling assignment-id
                are loaded first so a newer row keyed by the shared homework
                group string wins. Mixing the order would silently revert a
                student's HW% to a stale value.
                """
                hg = hg_by_assignment.get(aid) or aid
                merged: Dict[str, Any] = {}
                if hg and hg != aid and hg in unique_groups:
                    for sib_id in sibling_ids_by_group.get(hg, []):
                        merged.update(scores_by_key.get(sib_id, {}))
                    merged.update(scores_by_key.get(hg, {}))
                else:
                    merged.update(scores_by_key.get(aid, {}))
                return merged

            hw_by_assignment: Dict[str, Dict[str, Any]] = {}
            for aid in assignment_ids:
                hw_by_assignment[aid] = _hw_map_for_assignment(aid)

            overdue = []
            for g in grade_rows:
                assignment = g.get('assignments') or {}
                rev_due = assignment.get('revision_due')
                if not rev_due or rev_due >= today:
                    continue
                aid = g.get('assignment_id')
                snapshot = g.get('hw_score_at_entry')
                if snapshot is None:
                    score = hw_by_assignment.get(aid, {}).get(g['student_id'])
                else:
                    score = snapshot
                if not Homework.is_revision_to_m_eligible_hw_score(score):
                    continue
                lo = g.get('learning_objectives') or {}
                overdue.append({
                    'student_id': g['student_id'],
                    'assignment_name': assignment.get('name', ''),
                    'revision_due': rev_due,
                    'lo_name': lo.get('vendor_code') or lo.get('name', 'Unknown LO'),
                    'top_score': g.get('top_score') or '',
                })
            return overdue
        except Exception as e:
            logger.error("Error fetching overdue revisions: %s", e)
            return []

class Student:
    @staticmethod
    def get_dashboard_data(student_id):
        """Fetches a student's profile and grades for the dashboard.

        Selects only the fields the dashboard template/route actually reads to keep
        payload small (was ``*`` which pulled all profile/grade/class columns).
        """
        grade_cols = (
            "student_id, learning_objective_id, top_score, second_score, "
            "counts_for_mastery, assignment_id, "
            "learning_objectives(id, name, vendor_code, required_ms)"
        )
        response = supabase_admin.table("profiles").select(
            "id, full_name, role, "
            f"grades({grade_cols}), "
            "enrollments(classes(id, name))"
        ).eq("id", student_id).single().execute()
        return response.data

class Homework:
    # The two HW thresholds are *intentionally* different. The lower threshold
    # gates ordinary in-class marks (R/RQ/P/X/A) and may be unlocked by a "free
    # pass" (-1) — the student earns credit for showing up even if HW % is
    # low. The higher threshold gates the *upgrade* to mastery (M/MR), which
    # cannot be unlocked by a free pass: actually demonstrating mastery
    # requires real homework engagement.
    EXAM_GRADE_HW_THRESHOLD = 65
    REVISION_TO_M_HW_THRESHOLD = 75

    @staticmethod
    def normalize_homework_group(raw) -> Optional[str]:
        """Normalize a free-form homework/assignment-group label.

        Trims and collapses internal whitespace only; any non-empty label is
        valid (homework_group is a free-form grouping key, not a fixed list).
        """
        if raw is None:
            return None
        s = re.sub(r"\s+", " ", str(raw).strip())
        return s or None

    HW_PREV_HEADER = "HW prev"

    @staticmethod
    def is_hw_prev_header(name) -> bool:
        """True for the read-only previous-score column, which is not a homework score or an objective."""
        if name is None:
            return False
        s = re.sub(r"\s+", " ", str(name).strip())
        return bool(s) and s.lower() == Homework.HW_PREV_HEADER.lower()

    @staticmethod
    def is_import_sheet_hw_column(name) -> bool:
        """True for sheet headers that represent homework / assignment percentage, not a mastery LO.

        Used by Gemini import and the grade-import API so a column like "HW" or "Homework %"
        is stored in homework_scores instead of learning-objective grades.
        """
        if not name or not str(name).strip():
            return False
        if Homework.is_hw_prev_header(name):
            return False
        s = re.sub(r"[%_]+", " ", str(name).strip(), flags=re.I)
        s = re.sub(r"\s+", " ", s).strip()
        n = s.upper()
        if not n or len(n) > 64:
            return False
        if n in (
            "H", "HW", "H W", "HOMEWORK", "HOME WORK", "HOME-WORK", "H WORK",
            "HW SCORE", "HW PCT", "HW PCTG", "HWPCT", "HWPCTG", "HW %",
        ):
            return True
        if n.startswith("HOMEWORK") or "HOMEWORK" in n:
            return True
        if re.match(r"^HW[-\d]*$", n) or re.match(r"^HW \d", n) or n.startswith("HW "):
            return True
        if re.match(r"^HW\d{1,3}$", n):
            return True
        return False

    @staticmethod
    def canonicalize_import_sheet_header(name) -> str:
        """Map common header synonyms to codes recognized by import rules (EX1, FEX, LO1, …).

        Used for Gemini JSON and local PDF table extraction so columns route to HW / exam / LO
        consistently. Returns a non-empty string; unknown headers are returned with normalized
        whitespace only (no semantic change beyond spacing).

        Conservative on purpose: a header we cannot confidently recognize is
        left mostly intact so a misclassified column at least imports as a
        named LO instead of silently being routed to HW or exam storage.
        """
        if name is None:
            return ""
        raw = str(name).strip()
        if not raw:
            return ""
        t = re.sub(r"[\u2013\u2014\u2212]+", "-", raw)
        t = re.sub(r"\s+", " ", t).strip()
        u = t.upper()
        compact = re.sub(r"\s+", "", u)
        # Final exam — conservative synonyms only
        if compact == "FEX" or compact.startswith("FINALEX") or compact == "FINAL":
            return "FEX"
        # Exam score columns
        m = re.match(r"^EX(\d{1,3})$", compact)
        if m:
            return f"EX{int(m.group(1))}"
        m = re.match(r"^EXAM(\d{1,3})$", compact)
        if m:
            return f"EX{int(m.group(1))}"
        # Learning objective numeric codes like LO1, LO12
        m = re.match(r"^LO(\d{1,4})$", compact, flags=re.I)
        if m:
            return f"LO{int(m.group(1))}"
        # Letter + digits (e.g. A 7 -> A7, M1 stays M1) — skip if starts with EX/HW
        m = re.match(r"^([A-Z]{1,4})(\d{1,4})$", compact)
        if m:
            letters, digits = m.group(1), m.group(2)
            if letters in ("EX", "HW"):
                pass
            else:
                return f"{letters}{int(digits)}"
        return t

    @staticmethod
    def parse_import_hw_pct(value) -> Optional[int]:
        """Parse a homework cell to an integer 0–100, or -1 (free pass), or None if not parseable.

        Rejects non-numeric strings so a mistaken letter (e.g. a mastery mark) is not coerced
        into a percentage.
        """
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        s = s.rstrip("%").strip()
        if not re.match(r"^-?\d+(\.\d+)?$", s):
            return None
        try:
            v = float(s)
        except ValueError:
            return None
        if v == -1 or v == -1.0:
            return -1
        iv = int(round(v))
        return max(0, min(100, iv))

    @staticmethod
    def decide_hw_score_update(current, incoming):
        """Return the import write for one student in one homework group.

        current is the stored score_pct, or None when that student has no row.
        Only incoming is parsed. Equality is float(current) == that parsed
        value. A shift copies current into prev_score_pct unchanged.
        """
        parsed_in = Homework.parse_import_hw_pct(incoming)
        if parsed_in is None:
            return {"action": "unchanged"}
        if current is None:
            return {
                "action": "first_set",
                "score_pct": parsed_in,
                "prev_score_pct": parsed_in,
            }
        if float(current) == parsed_in:
            return {"action": "unchanged"}
        return {
            "action": "shift_and_set",
            "score_pct": parsed_in,
            "prev_score_pct": current,
        }

    @staticmethod
    def is_exam_grade_eligible_hw_score(score):
        """True if the student may receive in-class exam marks for this HW row."""
        if score is None:
            return False
        if score == -1:
            return True
        return score >= Homework.EXAM_GRADE_HW_THRESHOLD

    @staticmethod
    def is_revision_to_m_eligible_hw_score(score):
        """True if the student may receive M or MR (revision to mastery)."""
        if score is None or score == -1:
            return False
        return score >= Homework.REVISION_TO_M_HW_THRESHOLD

    @staticmethod
    def canonicalize_homework_group_for_class(class_id, raw):
        """Normalize a homework_group label, reusing an existing label's exact
        casing for this class if one already matches case-insensitively.

        homework_group is free-form text, but assignments only share one HW %
        per student when their `homework_group` values are byte-identical
        (see resolve_hw_group_storage_key / get_hw_scores_map_for_homework_group).
        Without this, typing "Quiz 1" for one assignment and "quiz 1" for
        another would silently create two separate HW groups even though the
        instructor meant the same one. Returns None if `raw` normalizes to
        nothing.
        """
        candidate = Homework.normalize_homework_group(raw)
        if not candidate:
            return None
        class_id = str(class_id or "").strip()
        if not class_id:
            return candidate
        try:
            resp = (
                supabase_admin.table("assignments")
                .select("homework_group")
                .eq("class_id", class_id)
                .execute()
            )
        except Exception as e:
            logger.error(
                "Error looking up existing homework groups for class %s: %s",
                class_id,
                e,
            )
            return candidate
        target = candidate.lower()
        for row in resp.data or []:
            existing = Homework.normalize_homework_group(row.get("homework_group"))
            if existing and existing.lower() == target:
                return existing
        return candidate

    @staticmethod
    def resolve_hw_group_storage_key(class_id, assignment_id):
        """Return the value stored in homework_scores.homework_group for this assignment.

        All assignments that share the same homework_group label use one HW % row per student.
        If the assignment has no group label, fall back to assignment_id (legacy behavior).
        """
        assignment_id = str(assignment_id or "").strip()
        class_id = str(class_id or "").strip()
        if not assignment_id or not class_id:
            return None
        try:
            resp = supabase_admin.table("assignments").select(
                "homework_group, class_id"
            ).eq("id", assignment_id).limit(1).execute()
        except Exception as e:
            logger.error(
                "Error resolving homework group for assignment %s: %s",
                assignment_id,
                e,
            )
            return None
        if not resp.data:
            return None
        row = resp.data[0]
        if str(row.get("class_id") or "").strip() != class_id:
            return None
        hg_raw = (row.get("homework_group") or "").strip()
        if not hg_raw:
            return assignment_id
        canonical = Homework.normalize_homework_group(hg_raw)
        return canonical or hg_raw

    @staticmethod
    def _hw_column_map_for_assignment(class_id, assignment_id, column):
        """Return {student_id: column} for the HW group this assignment belongs to.

        Scores are keyed by the shared homework_group string. Rows keyed by assignment UUID
        (older data) are merged in so every assignment in the group shows the same value.

        Order is important: legacy rows are loaded first and the group-keyed
        row is loaded last, so a newer canonical value overrides any stale
        per-assignment entries in the map.
        """
        if column not in ("score_pct", "prev_score_pct"):
            return {}
        try:
            resp = supabase_admin.table("assignments").select("id, homework_group").eq(
                "id", assignment_id
            ).eq("class_id", class_id).limit(1).execute()
            if not resp.data:
                return {}
            row = resp.data[0]
            hg = (row.get("homework_group") or "").strip()
            hw_map = {}
            select_cols = "student_id, " + column

            def _merge(rows):
                for r in (rows or []):
                    sid = str(r.get("student_id") or "").strip()
                    if sid:
                        hw_map[sid] = r.get(column)

            if hg:
                sib = supabase_admin.table("assignments").select("id").eq(
                    "class_id", class_id
                ).eq("homework_group", hg).execute()
                sib_ids = [r["id"] for r in (sib.data or [])]
                if sib_ids:
                    leg = supabase_admin.table("homework_scores").select(
                        select_cols
                    ).eq("class_id", class_id).in_("homework_group", sib_ids).execute()
                    _merge(leg.data)
                cur = supabase_admin.table("homework_scores").select(
                    select_cols
                ).eq("class_id", class_id).eq("homework_group", hg).execute()
                _merge(cur.data)
            else:
                single = supabase_admin.table("homework_scores").select(
                    select_cols
                ).eq("class_id", class_id).eq("homework_group", assignment_id).execute()
                _merge(single.data)
            return hw_map
        except Exception as e:
            logger.error(
                "Error loading homework %s for assignment %s: %s",
                column, assignment_id, e,
            )
            return {}

    @staticmethod
    def get_hw_scores_map_for_assignment(class_id, assignment_id):
        """Return {student_id: score_pct} for this assignment's homework group."""
        return Homework._hw_column_map_for_assignment(class_id, assignment_id, "score_pct")

    @staticmethod
    def get_hw_prev_scores_map_for_assignment(class_id, assignment_id):
        """Return {student_id: prev_score_pct} for this assignment's homework group."""
        return Homework._hw_column_map_for_assignment(class_id, assignment_id, "prev_score_pct")

    @staticmethod
    def student_has_recorded_score(hw_map, student_id) -> bool:
        """True when this assignment's homework group has a score row (0 and -1 count)."""
        sid = str(student_id or "").strip()
        if not sid or not hw_map:
            return False
        if sid not in hw_map:
            return False
        return hw_map[sid] is not None
