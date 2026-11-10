"""Pin grade-letter copies that are supposed to stay twins."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STORED = ("M", "MR", "R", "Q", "RQ", "P", "X", "A", "I")
BADGE_LETTERS = ("m", "mr", "r", "q", "rq", "p", "x", "a", "i")
WIZARD_TEXT = {
    "M": "#166534",
    "MR": "#047857",
    "R": "#854d0e",
    "Q": "#713f12",
    "RQ": "#9a3412",
    "P": "#1e40af",
    "X": "#991b1b",
    "A": "#64748b",
}


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def hex_rgb(color):
    raw = color[1:]
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def rule_field(rule, name):
    match = re.search(name + r":\s*(#[0-9a-fA-F]+)", rule)
    if match is None:
        return ""
    return match.group(1).lower()


def badge_colors(css):
    found = {}
    pos = 0
    for letter in BADGE_LETTERS:
        token = ".mastery-" + letter
        at = css.find(token, pos)
        brace = css.find("{", at)
        end = css.find("}", brace)
        rule = css[brace:end]
        found[letter] = (
            rule_field(rule, "background"),
            rule_field(rule, "color"),
            rule_field(rule, "border-color"),
        )
        pos = end
    return found


def archive_colors(src):
    found = {}
    for letter in BADGE_LETTERS:
        token = '"%s": ("' % letter
        at = src.find(token)
        end = src.find(")", at)
        parts = re.findall(r"#[0-9a-fA-F]+", src[at:end])
        found[letter] = tuple(part.lower() for part in parts)
    return found


def pdf_colors(src):
    start = src.find("const PDF_GRADE_TEXT_COLOR = {")
    end = src.find("};", start)
    block = src[start:end]
    found = {}
    for match in re.finditer(r"'([A-Z]+)'\s*:\s*\[(\d+),\s*(\d+),\s*(\d+)\]", block):
        found[match.group(1)] = tuple(int(match.group(index)) for index in (2, 3, 4))
    return found, start


def wizard_cells(css):
    found = {}
    for line in css.splitlines():
        if not line.startswith("#extractedStudentsTable .grade-cell[data-grade="):
            continue
        match = re.search(
            r'data-grade="([A-Z]+)"\]\s*\{[^}]*background:\s*(#[0-9a-fA-F]+);\s*color:\s*(#[0-9a-fA-F]+)',
            line,
        )
        if match is None:
            continue
        found[match.group(1)] = (match.group(2).lower(), match.group(3).lower())
    return found


def revision_rule(css, letter):
    token = ".revision-grade-" + letter
    at = css.find(token + " {")
    end = css.find("}", at)
    rule = css[at:end]
    return at, (
        rule_field(rule, "background"),
        rule_field(rule, "color"),
        rule_field(rule, "border-color"),
    )


class GradeRuleCopyTests(unittest.TestCase):
    def test_stored_letters_match_check_model_and_wizard(self):
        model = read("app/models.py")
        wizard = read("app/static/js/csv_wizard.js")
        sql = read("db/001_baseline.sql")
        stored = "('M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A', 'I')"
        model_at = model.find("if score in " + stored + ":")
        self.assertGreater(model_at, -1)
        self.assertNotIn("if score in ('M', 'R', 'Q'", model)
        wizard_line = "const VALID_GRADES = new Set(['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A', 'I']);"
        wizard_at = wizard.find(wizard_line)
        self.assertGreater(wizard_at, -1)
        self.assertNotIn("new Set(['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A']);", wizard)
        array = "['M'::text, 'MR'::text, 'R'::text, 'Q'::text, 'RQ'::text, 'P'::text, 'X'::text, 'A'::text, 'I'::text]"
        second_at = sql.find("grades_second_score_check")
        self.assertGreater(second_at, -1)
        top_at = sql.find("grades_top_score_check")
        self.assertGreater(top_at, second_at)
        self.assertEqual(sql.count(array), 2)
        self.assertNotIn("'Z'::text", sql)
        previous = model_at
        for letter in STORED:
            at = model.find("'" + letter + "'", previous)
            self.assertGreater(at, previous)
            previous = at + 1

    def test_keyboard_sets_match_and_omit_i(self):
        speed = read("app/static/js/class_speed_grader.js")
        wizard = read("app/static/js/csv_wizard.js")
        grades = "const GRADES = ['M', 'R', 'Q', 'P', 'X', 'A'];"
        grades_at = speed.find(grades)
        self.assertGreater(grades_at, -1)
        self.assertNotIn("'I'", speed[grades_at:grades_at + len(grades)])
        self.assertNotIn("const GRADES = ['M', 'MR'", speed)
        keys = "const GRADE_KEYS = ['M', 'R', 'Q', 'P', 'X', 'A'];"
        keys_at = wizard.find(keys)
        self.assertGreater(keys_at, -1)
        self.assertNotIn("'I'", wizard[keys_at:keys_at + len(keys)])
        map_speed = speed.find("const GRADE_MAP = { 'Q': 'RQ' };")
        self.assertGreater(map_speed, -1)
        self.assertNotIn("const GRADE_MAP = { 'Q': 'Q' }", speed)
        map_wizard = wizard.find("const GRADE_MAP  = { 'Q': 'RQ' };")
        self.assertGreater(map_wizard, -1)
        self.assertNotIn("const GRADE_MAP  = { 'Q': 'Q' }", wizard)

    def test_mastery_pair_literals_match(self):
        model = read("app/models.py")
        mark = model.find("return mark in ('M', 'MR')")
        self.assertGreater(mark, -1)
        self.assertNotIn("return mark in ('M', 'R')", model)
        grading = read("app/routes/grading.py")
        normalized = grading.find("if normalized in ('M', 'MR'):")
        self.assertGreater(normalized, -1)
        existing = grading.find("existing_top in ('M', 'MR')")
        self.assertGreater(existing, normalized)
        stored = grading.find('stored_letter in ("M", "MR")')
        self.assertGreater(stored, existing)
        self.assertNotIn("in ('M', 'R')", grading)
        sheet = read("app/helpers/gradesheet_csv.py")
        sheet_at = sheet.find('letter in ("M", "MR")')
        self.assertGreater(sheet_at, -1)
        self.assertNotIn('letter in ("M", "R")', sheet)
        archive = read("app/helpers/archive_export.py")
        archive_at = archive.find('mark_text in ("M", "MR")')
        self.assertGreater(archive_at, -1)
        self.assertNotIn('mark_text in ("M", "R")', archive)
        passes = read("app/helpers/free_passes.py")
        pair = '.in_("top_score", ["M", "MR"])'
        pair_at = passes.find(pair)
        self.assertGreater(pair_at, -1)
        self.assertEqual(passes.count(pair), 4)
        self.assertNotIn('["M", "R"]', passes)
        components = read("app/templates/_components.html")
        component_at = components.find("mark in ('M', 'MR')")
        self.assertGreater(component_at, -1)
        self.assertNotIn("mark in ('M', 'R')", components)
        progress = read("app/helpers/class_progress.py")
        mr_count = progress.find("if top == 'MR' and row_counts:")
        self.assertGreater(mr_count, -1)
        self.assertIn("Grade.is_mastery_mark(top)", progress)
        self.assertNotIn("if top == 'M' and row_counts:", progress)

    def test_revision_letters_match(self):
        model = read("app/models.py")
        query = model.find('.in_("top_score", ["R", "Q", "RQ"])')
        self.assertGreater(query, -1)
        self.assertNotIn('.in_("top_score", ["R", "Q"])', model)
        phrases = read("app/helpers/revision_text.py")
        start = phrases.find("_REVISION_SCORE_PHRASE = {")
        self.assertGreater(start, -1)
        end = phrases.find("}", start)
        self.assertGreater(end, start)
        block = phrases[start:end]
        r_at = block.find('"R": "Revise for Mastery"')
        self.assertGreater(r_at, -1)
        q_at = block.find('"Q": "Revision done, retake still needed (Q)"', r_at)
        self.assertGreater(q_at, r_at)
        rq_at = block.find('"RQ": "Revise with Question (RQ)"', q_at)
        self.assertGreater(rq_at, q_at)
        self.assertNotIn('"M":', block)
        css = read("app/static/css/class_detail_print.css")
        r_css = css.find(".revision-grade-r {")
        self.assertGreater(r_css, -1)
        rq_css = css.find(".revision-grade-rq {")
        self.assertGreater(rq_css, r_css)
        q_css = css.find(".revision-grade-q {")
        self.assertGreater(q_css, rq_css)
        self.assertNotIn(".revision-grade-m {", css)
        detail = read("app/templates/classes/class_detail.html")
        chip = detail.find("revision-grade-{{ obj.top_score|lower }}")
        self.assertGreater(chip, -1)
        self.assertNotIn("revision-grade-r", detail)

    def test_homework_thresholds_match_and_api_field_stays_exam_rule(self):
        model = read("app/models.py")
        exam = model.find("EXAM_GRADE_HW_THRESHOLD = 65")
        self.assertGreater(exam, -1)
        revision = model.find("REVISION_TO_M_HW_THRESHOLD = 75")
        self.assertGreater(revision, exam)
        self.assertNotIn("EXAM_GRADE_HW_THRESHOLD = 75", model)
        self.assertNotIn("REVISION_TO_M_HW_THRESHOLD = 65", model)
        for rel in (
            "app/static/js/class_speed_grader.js",
            "app/static/js/class_reports.js",
        ):
            src = read(rel)
            exam_js = src.find("const EXAM_HW_MIN = 65;")
            self.assertGreater(exam_js, -1)
            revision_js = src.find("const REVISION_M_MIN = 75;")
            self.assertGreater(revision_js, exam_js)
            self.assertNotIn("const EXAM_HW_MIN = 75;", src)
            self.assertNotIn("const REVISION_M_MIN = 65;", src)
        form = read("app/templates/_class_form_fields.html")
        prose = form.find("below 65%")
        self.assertGreater(prose, -1)
        self.assertNotIn("below 75%", form)
        grading = read("app/routes/grading.py")
        eligibility = grading.find("eligibility[sid] = score == -1 or (")
        self.assertGreater(eligibility, -1)
        payload = grading.find("return jsonify", eligibility)
        self.assertGreater(payload, eligibility)
        chunk = grading[eligibility:payload]
        self.assertIn("Homework.EXAM_GRADE_HW_THRESHOLD", chunk)
        self.assertNotIn("REVISION_TO_M_HW_THRESHOLD", chunk)
        self.assertIn('"revision_eligible": eligibility', grading[payload:payload + 800])
        speed = read("app/static/js/class_speed_grader.js")
        passed = speed.find("if (isPass) {")
        self.assertGreater(passed, -1)
        self.assertIn("revisionEligible[studentId] = false;", speed[passed:passed + 250])
        revise = speed.find("revisionEligible[studentId] = n >= REVISION_M_MIN;")
        self.assertGreater(revise, passed)
        self.assertNotIn("revisionEligible[studentId] = n >= EXAM_HW_MIN", speed)
        predict = speed.find("function predictCountsForMasteryFromCurrentHw")
        self.assertGreater(predict, -1)
        predict_end = speed.find("function getDisplayedGradeForStudent", predict)
        self.assertGreater(predict_end, predict)
        body = speed[predict:predict_end]
        self.assertIn("hw >= EXAM_HW_MIN", body)
        self.assertIn("if (!AUTO_CONVERT_M) return true;", body)
        self.assertNotIn("REVISION_M_MIN", body)

    def test_numeric_bands_stay_off_the_homework_thresholds(self):
        model = read("app/models.py")
        high = model.find("if val >= 70:")
        self.assertGreater(high, -1)
        mid = model.find("if val >= 50:", high)
        self.assertGreater(mid, high)
        self.assertNotIn("if val >= 65:", model)
        self.assertNotIn("if val >= 75:", model)

    def test_badge_colors_match_print_archive_and_pdf(self):
        app_css = read("app/static/css/app.css")
        print_css = read("app/static/css/student_progress_print.css")
        archive = read("app/helpers/archive_export.py")
        reports = read("app/static/js/class_reports.js")
        screen = badge_colors(app_css)
        paper = badge_colors(print_css)
        packed = archive_colors(archive)
        pdf, pdf_at = pdf_colors(reports)
        self.assertGreater(pdf_at, -1)
        self.assertEqual(set(screen), set(BADGE_LETTERS))
        self.assertNotIn("z", screen)
        for letter in BADGE_LETTERS:
            self.assertEqual(screen[letter], paper[letter])
            self.assertEqual(screen[letter], packed[letter])
            rgb = hex_rgb(screen[letter][1])
            self.assertEqual(pdf[letter.upper()], rgb)
        self.assertNotIn("Z", pdf)
        first = app_css.find(".mastery-m")
        self.assertGreater(first, -1)
        previous = first
        for letter in BADGE_LETTERS[1:]:
            at = app_css.find(".mastery-" + letter, previous)
            self.assertGreater(at, previous)
            previous = at
        reports_css = read("app/static/css/class_reports_print.css")
        paper_rule = reports_css.find("  #assignmentReport .mastery-badge {")
        self.assertGreater(paper_rule, -1)
        paper_end = reports_css.find("}", paper_rule)
        self.assertGreater(paper_end, paper_rule)
        painted = reports_css[paper_rule:paper_end]
        self.assertIn("color: #0f172a !important", painted)
        self.assertNotIn("#15803d", painted)

    def test_wizard_cells_and_revision_chips_stay_distinct(self):
        app_css = read("app/static/css/app.css")
        screen = badge_colors(app_css)
        cells = wizard_cells(app_css)
        present = cells.get("M")
        self.assertIsNotNone(present)
        self.assertNotIn("I", cells)
        self.assertEqual(set(cells), {"M", "MR", "R", "Q", "RQ", "P", "X", "A"})
        for letter, text_color in WIZARD_TEXT.items():
            background, color = cells[letter]
            self.assertEqual(background, screen[letter.lower()][0])
            self.assertEqual(color, text_color)
        self.assertNotEqual(cells["M"][1], screen["m"][1])
        self.assertEqual(cells["MR"][1], screen["mr"][1])
        self.assertEqual(cells["Q"][1], screen["q"][1])
        detail_css = read("app/static/css/class_detail_print.css")
        q_at, q_rule = revision_rule(detail_css, "q")
        self.assertGreater(q_at, -1)
        self.assertEqual(q_rule, screen["q"])
        r_at, r_rule = revision_rule(detail_css, "r")
        self.assertGreater(r_at, -1)
        self.assertEqual(r_rule, ("#fffbeb", "#92400e", "#fcd34d"))
        self.assertNotEqual(r_rule, screen["r"])
        rq_at, rq_rule = revision_rule(detail_css, "rq")
        self.assertGreater(rq_at, r_at)
        self.assertEqual(rq_rule, ("#fff7ed", "#9a3412", "#fdba74"))
        self.assertEqual(rq_rule[2], screen["rq"][2])
        self.assertNotEqual(rq_rule[0], screen["rq"][0])
        dark = detail_css.find(".dark .revision-grade-q {")
        self.assertGreater(dark, q_at)
        self.assertNotIn(".dark .mastery-q", app_css)

    def test_history_list_and_legends_keep_known_differences(self):
        history = read("app/templates/classes/student_history.html")
        listed = "['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A']"
        listed_at = history.find(listed)
        self.assertGreater(listed_at, -1)
        self.assertEqual(history.count(listed), 2)
        self.assertNotIn("['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A', 'I']", history)
        speed = read("app/templates/classes/class_speed_grader.html")
        shift_m = speed.find(">⇧M</kbd>")
        self.assertGreater(shift_m, -1)
        shift_m_end = speed.find("</div>", shift_m)
        self.assertGreater(shift_m_end, shift_m)
        self.assertIn("MR", speed[shift_m:shift_m_end])
        wizard = read("app/templates/classes/update_grade.html")
        start = wizard.find(">M</kbd> Mastered")
        self.assertGreater(start, -1)
        end = wizard.find(">Del</kbd>", start)
        self.assertGreater(end, start)
        block = wizard[start:end]
        self.assertIn(">Q</kbd> Revise with Question (RQ)", block)
        self.assertNotIn("\u21e7M", block)

    def test_import_alias_and_print_swap_stay_put(self):
        wizard = read("app/static/js/csv_wizard.js")
        alias = wizard.find("if (letter === 'I') return true;")
        self.assertGreater(alias, -1)
        mastery = wizard.find("if (letter !== 'M' && letter !== 'MR') return false;", alias)
        self.assertGreater(mastery, alias)
        self.assertNotIn("if (letter === 'I') return false;", wizard)
        reports = read("app/static/js/class_reports.js")
        swap = reports.find("function _swapNonCountingMastersToIForPrint()")
        self.assertGreater(swap, -1)
        restore = reports.find("function _restoreNonCountingMastersAfterPrint()", swap)
        self.assertGreater(restore, swap)
        body = reports[swap:restore]
        self.assertIn("b.textContent = 'I';", body)
        self.assertNotIn("b.textContent = 'M';", body)


if __name__ == "__main__":
    unittest.main()
