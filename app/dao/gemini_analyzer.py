"""
Gemini Vision module for parsing grade sheet photos/PDFs.
Uses Google Gemini Vision API for extraction.
Designed for photographed paper grade sheets with table structure.

Model selection (see also ``GradeSheetGeminiAnalyzer.__init__``):
    * ``GEMINI_VISION_MODEL`` — primary model id (default: ``gemini-2.5-flash``).
    * ``GEMINI_FALLBACK_MODELS`` — optional comma-separated ids tried in order after
      the primary exhausts transient retries (e.g. ``gemini-2.0-flash``). Only add
      names that your API key can call: check **Google AI Studio → Models**, or
      run a one-off ``generate_content`` in the API docs; **404 / PERMISSION_DENIED**
      means that model is not available for your project.

    * ``GEMINI_MAX_WALL_SEC`` — hard cap for the **entire** scan (PDF→images + API +
      retry sleeps), default **58** seconds. When the limit is hit, the user sees a
      clear timeout (try fewer pages / lower resolution export). Low billing tiers
      (e.g. Tier 1) often need smaller inputs to finish under the cap.

    * ``GEMINI_PDF_ZOOM`` — minimum PyMuPDF render scale for PDF pages (default **1.25**).
      Per-page scale may be raised up to **2.0** when ``GEMINI_PDF_TARGET_WIDTH_PX`` (default
      **1400**) implies a wider raster for dense typed sheets. Values are clamped to 1.0–2.0.

    * ``GEMINI_PDF_TARGET_WIDTH_PX`` — approximate minimum rendered page width in pixels used
      with PDF page width to pick a per-page zoom for the vision path.

    * ``GEMINI_FORCE_VISION`` — if ``1`` / ``true``, skip local PDF table/text extraction and
      always use Gemini vision (debug or rare false positives from the fast path).

Handles:
- Printed tables with grid lines
- Mixed printed text and handwritten grade marks
- Column headers like EX1, A7, M1, D2, LO1, etc.
- Grade marks: checkmarks (✓), P, X, M, R, RQ, A, and similar symbols
"""

from typing import Dict, List, Optional
import logging
import os
import json
import random
import re
import time

from app.models import Homework

from app.dao.grade_sheet_pdf_extract import try_extract_grade_sheet_from_pdf_bytes

logger = logging.getLogger(__name__)

# Transient errors: exponential backoff between attempts (capped by wall-clock deadline).
_GEMINI_RETRY_FIRST_SEC = 1.5
_GEMINI_RETRY_PER_SLEEP_CAP_SEC = 12.0
_DEFAULT_GEMINI_MAX_WALL_SEC = 58.0

try:
    import fitz  # type: ignore  # PyMuPDF for PDF→image conversion
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


_EXTRACTION_PROMPT = """You are analyzing a grade sheet image. The source may be a paper photograph, a scan, or a digital PDF / typed spreadsheet export — treat all the same.

The sheet is a table with:
- A header row containing column names. The first column is student names. The remaining columns are one of:
  - learning objective codes (like A7, M1, D2, LO1, LO2, etc.)
  - exam score columns (EX1, EX2, EX3, FEX) with numeric values
  - homework score columns (like HW, HW%, Homework).
- Each subsequent row is a student. The first cell is the student's full name. The remaining cells contain grade marks.

Valid grade marks are:
- "M" = Mastered
- "MR" = Mastered on a revision (use when the mark reflects revised work, not the first attempt)
- "P" = Progressing
- "X" = Not yet / incorrect
- "R" = Redo / Retake
- "RQ" = Redo Required
- "A" = Absent
- A checkmark (✓ or similar) should be interpreted as "P" (Progressing/Pass)
- A blank or empty cell should be omitted (do not include it)
- A number (like 85, 92, etc.) is a homework percentage score — include it as-is

Extract ALL students and ALL columns from the sheet. Be thorough — do not skip any rows or columns.

Return ONLY valid JSON in this exact format (no markdown fencing, no extra text):
{
  "learning_objectives": ["EX1", "A7", "M1", "D2"],
  "students": [
    {
      "name": "John Smith",
      "grades": {"EX1": "M", "A7": "P", "M1": "X", "D2": "R"}
    }
  ]
}

Rules:
- "learning_objectives" is the ordered list of column headers (excluding the student name column), in left-to-right order.
- Keep strict column alignment: each grade value must sit under its correct header; do not shift values left or right.
- If the sheet has merged cells, place the visible mark in the logical column it belongs to; do not invent extra columns.
- Each student's "grades" object uses the column header as key and the grade mark as value.
- Columns for homework completion percentage must use headers like HW, HW%, Homework, or HW1 — not fake learning objective codes. Put numeric homework scores only under those columns.
- EX1/EX2/EX3/FEX are exam score columns (numeric), not mastery learning objectives.
- Omit empty/blank cells from the grades object entirely.
- Normalize all grade marks to uppercase (M, MR, P, X, R, RQ, A).
- Convert any checkmark symbol to "P" for learning-objective columns only (not for homework % columns).
- For homework % columns, preserve numbers as strings (e.g., "85" not 85).
- Do not put a homework percentage under a learning objective code (EX1, A7, etc.); it must be under a homework-style header.
- Student names should be in their original order as they appear on the sheet.
- Preserve the exact spelling of student names as printed on the sheet.
"""


class GradeSheetGeminiAnalyzer:
    """
    Analyzes grade sheet photos/PDFs using Google Gemini Vision API.
    Sends images to Gemini and receives structured JSON with student grades.
    """

    @staticmethod
    def _vision_model_ids_from_env() -> List[str]:
        """Ordered model ids: primary from GEMINI_VISION_MODEL, then GEMINI_FALLBACK_MODELS."""
        primary = (os.environ.get("GEMINI_VISION_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        raw = os.environ.get("GEMINI_FALLBACK_MODELS", "")
        extras = [m.strip() for m in raw.split(",") if m.strip()]
        seen = set()
        out: List[str] = []
        for m in [primary] + extras:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def __init__(self):
        """Initialize the analyzer with Gemini client."""
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")
        if not HAS_GENAI:
            raise ImportError("google-genai not installed. Run: pip install google-genai")
        self.client = genai.Client(api_key=api_key)
        self.models = self._vision_model_ids_from_env()
        try:
            self.max_wall_sec = float(
                os.environ.get(
                    "GEMINI_MAX_WALL_SEC",
                    str(_DEFAULT_GEMINI_MAX_WALL_SEC),
                )
            )
        except ValueError:
            self.max_wall_sec = _DEFAULT_GEMINI_MAX_WALL_SEC
        # Clamped so a misconfigured env var can't deadlock a request below ~35s
        # (too short to ever finish on slow tiers) or hold a worker for >120s.
        self.max_wall_sec = max(35.0, min(120.0, self.max_wall_sec))
        try:
            z = float(os.environ.get("GEMINI_PDF_ZOOM", "1.25"))
        except ValueError:
            z = 1.25
        # 2.0 is an empirical upper bound: above it the rasters get large
        # enough that the model often times out on its own before responding.
        self.pdf_zoom = max(1.0, min(2.0, z))
        try:
            self.pdf_target_width_px = float(
                os.environ.get("GEMINI_PDF_TARGET_WIDTH_PX", "1400")
            )
        except ValueError:
            self.pdf_target_width_px = 1400.0
        # Same shape as `max_wall_sec`: clamp guards against config typos.
        self.pdf_target_width_px = max(800.0, min(2400.0, self.pdf_target_width_px))

    def _render_zoom_for_page(self, page: "fitz.Page") -> float:
        """Pick per-page raster scale: at least env ``GEMINI_PDF_ZOOM``, up to 2.0 for narrow dense pages."""
        try:
            pt_w = float(page.rect.width)
        except Exception:
            pt_w = 612.0
        if pt_w <= 1.0:
            pt_w = 612.0
        need = self.pdf_target_width_px / pt_w
        z = max(self.pdf_zoom, min(2.0, need))
        return max(1.0, min(2.0, z))

    def analyze_pdf(self, file_obj) -> Dict:
        """
        Analyze a grade sheet file (PDF or image) and extract structured data.

        Args:
            file_obj: File bytes or file-like object (PDF, JPG, PNG)

        Returns:
            Dict with keys: students, learning_objectives, raw_text, success
        """
        try:
            import time as _time
            _t0 = _time.time()

            if hasattr(file_obj, 'read'):
                file_bytes = file_obj.read()
            else:
                file_bytes = file_obj

            logger.info("[Gemini] analyze_pdf called, %d bytes", len(file_bytes))

            deadline = _t0 + self.max_wall_sec

            extraction_path = "vision"
            extracted: Optional[Dict] = None
            # Local PDF extraction is dramatically faster and free; we only fall
            # through to vision when the PDF lacks a table structure (scans,
            # mixed photos) or when the operator forces it via env to debug a
            # bad fast-path result.
            force_vis = os.environ.get("GEMINI_FORCE_VISION", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if file_bytes[:4] == b"%PDF" and HAS_PYMUPDF and not force_vis:
                pack = try_extract_grade_sheet_from_pdf_bytes(file_bytes)
                if pack:
                    extraction_path = str(
                        pack.get("extraction_path") or "pdf_table"
                    )
                    raw = {
                        "learning_objectives": pack.get("learning_objectives") or [],
                        "students": pack.get("students") or [],
                    }
                    extracted = self._normalize_data(raw)
                    # If normalization dropped every student (e.g. all names
                    # looked like numbers), treat the fast-path as a miss and
                    # let vision try.
                    if len(extracted.get("students") or []) < 1:
                        extracted = None

            if extracted is None:
                extraction_path = "vision"
                image_parts = self._file_to_image_parts(file_bytes, deadline)
                logger.info(
                    "[Gemini] Prepared %d image(s) in %.1fs",
                    len(image_parts),
                    _time.time() - _t0,
                )

                if not image_parts:
                    raise Exception("Could not extract images from file")

                extracted = self._call_gemini(image_parts, deadline)
                logger.info("[Gemini] Extraction done in %.1fs", _time.time() - _t0)

            logger.info(
                "[Gemini] extraction_path=%s students=%d los=%d (%.1fs)",
                extraction_path,
                len(extracted["students"]),
                len(extracted["learning_objectives"]),
                _time.time() - _t0,
            )

            # Build raw text for display
            raw_lines: List[str] = []
            headers = list(extracted['learning_objectives'])
            hw_lbl = extracted.get('homework_column')
            has_hw = any((s.get('homework_pct') or '').strip() for s in extracted['students'])
            line_headers = headers + ([hw_lbl] if (hw_lbl and has_hw) else [])
            if line_headers:
                raw_lines.append('Name | ' + ' | '.join(line_headers))
            for s in extracted['students']:
                cells = [str(s['grades'].get(h, '')) for h in headers]
                if hw_lbl and has_hw:
                    cells.append(str(s.get('homework_pct', '')))
                raw_lines.append(f"{s['name']} | {' | '.join(cells)}")

            return {
                'students': extracted['students'],
                'learning_objectives': extracted['learning_objectives'],
                'homework_column': extracted.get('homework_column'),
                'raw_text': '\n'.join(raw_lines),
                'success': True,
                'extraction_path': extraction_path,
            }

        except Exception as e:
            logger.exception("Error analyzing file")
            raise Exception(f"Error analyzing file: {str(e)}")

    @staticmethod
    def _wall_timeout_exception(cap_sec: float, detail: str) -> Exception:
        cap = int(round(max(1.0, cap_sec)))
        return Exception(
            f"Grade sheet scan exceeded the {cap}s time limit ({detail}). "
            "Try fewer PDF pages, export one page as an image, or increase GEMINI_MAX_WALL_SEC if needed. "
            "Heavy sheets on low API tiers often need a smaller file to finish on time."
        )

    def _file_to_image_parts(self, file_bytes: bytes, deadline: float) -> List:
        """Convert PDF or image file bytes into Gemini Part objects."""
        parts = []

        if file_bytes[:4] == b'%PDF':
            if not HAS_PYMUPDF:
                raise ImportError(
                    "PyMuPDF is required for PDF files. Run: pip install pymupdf")
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            for page in doc:
                if time.time() >= deadline:
                    doc.close()
                    raise self._wall_timeout_exception(
                        self.max_wall_sec, "while converting PDF pages"
                    )
                z = self._render_zoom_for_page(page)
                mat = fitz.Matrix(z, z)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("png")
                parts.append(types.Part.from_bytes(
                    data=img_bytes,
                    mime_type="image/png"
                ))
            doc.close()
        else:
            mime_type = self._detect_mime_type(file_bytes)
            parts.append(types.Part.from_bytes(
                data=file_bytes,
                mime_type=mime_type
            ))

        return parts

    def _detect_mime_type(self, file_bytes: bytes) -> str:
        """Detect image MIME type from file header bytes."""
        if file_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        elif file_bytes[:2] == b'\xff\xd8':
            return "image/jpeg"
        elif file_bytes[:4] == b'RIFF' and file_bytes[8:12] == b'WEBP':
            return "image/webp"
        return "image/jpeg"

    @staticmethod
    def _is_retryable_gemini_error(exc: BaseException) -> bool:
        """True for overload / rate limits / transient server errors worth sleeping and retrying."""
        msg = str(exc).lower()
        markers = (
            "503",
            "429",
            "unavailable",
            "resource_exhausted",
            "deadline exceeded",
            "try again",
            "overloaded",
            "capacity",
            "temporar",
            "rate limit",
            "quota",
            "econnreset",
            "timeout",
            "timed out",
        )
        return any(m in msg for m in markers)

    @staticmethod
    def _format_final_gemini_error(exc: BaseException) -> Exception:
        """User-facing error after retries are exhausted (or a non-retryable failure)."""
        msg = str(exc)
        low = msg.lower()
        if "time limit" in low or ("exceeded the" in low and "limit" in low):
            return Exception(msg)
        if "503" in msg or "unavailable" in low or "overloaded" in low or "capacity" in low:
            return Exception(
                "The Gemini AI service is temporarily busy. Please try again in a few minutes."
            )
        if "429" in msg or "resource_exhausted" in low or "rate limit" in low or "quota" in low:
            return Exception(
                "The Gemini API rate limit was hit. Wait a minute and try again, or try a smaller file."
            )
        return Exception(f"Gemini API error: {msg}")

    def _call_gemini(self, image_parts: List, deadline: float) -> Dict:
        """Send image(s) to Gemini and parse the structured JSON response.

        Retries on transient errors with exponential backoff until ``deadline``
        (wall clock, shared across models). No work continues past the deadline.
        """
        contents = image_parts + [_EXTRACTION_PROMPT]
        last_error: Optional[BaseException] = None

        for model in self.models:
            attempt = 0
            while True:
                if time.time() >= deadline:
                    raise self._wall_timeout_exception(
                        self.max_wall_sec, "during AI extraction (time ran out before success)"
                    )
                try:
                    wall_left = max(0.0, deadline - time.time())
                    logger.info(
                        f"[Gemini] Trying model: {model} "
                        f"({wall_left:.0f}s left before {int(self.max_wall_sec)}s cap)"
                    )
                    # temperature=0.0 deliberately: grade-sheet extraction is
                    # deterministic transcription; any sampling drift shows up
                    # as cell-shift / misread digits which silently corrupt
                    # imported grades.
                    response = self.client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0,
                        )
                    )
                    response_text = response.text.strip()
                    parsed = self._parse_response(response_text)
                    logger.info("[Gemini] Success with model: %s", model)
                    return self._normalize_data(parsed)
                except Exception as e:
                    last_error = e
                    logger.warning("[Gemini] Model %s failed: %s", model, e)
                    if not self._is_retryable_gemini_error(e):
                        raise self._format_final_gemini_error(e) from e
                    wall_left = deadline - time.time()
                    if wall_left < 1.5:
                        logger.info(
                            "[Gemini] Wall clock nearly exhausted; "
                            "switching model or failing."
                        )
                        break
                    delay = min(
                        _GEMINI_RETRY_PER_SLEEP_CAP_SEC,
                        _GEMINI_RETRY_FIRST_SEC * (2**attempt) + random.uniform(0, 0.6),
                        max(0.0, wall_left - 1.0),
                    )
                    if delay < 0.35:
                        break
                    logger.info(
                        f"[Gemini] Retryable error; sleeping {delay:.1f}s "
                        f"({wall_left:.0f}s wall remaining)"
                    )
                    time.sleep(delay)
                    attempt += 1
                    if attempt > 40:
                        break

        if last_error is not None:
            raise self._format_final_gemini_error(last_error) from last_error
        raise Exception("Gemini API error: unknown failure")

    def _parse_response(self, response_text: str) -> Dict:
        """Parse the Gemini response text into a dict."""
        text = response_text
        if text.startswith("```"):
            lines = text.split('\n')
            start = 1
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == '```':
                    end = i
                    break
            text = '\n'.join(lines[start:end])

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("[Gemini] Failed to parse response: %s", e)
            logger.debug("[Gemini] Response text preview: %s", response_text[:500])
            raise Exception("Failed to parse grade sheet data from AI response")

    def _normalize_data(self, data: Dict) -> Dict:
        """Normalize and validate the extracted data.

        Splits homework columns out of the LO list so they are not stored as
        learning objectives or coerced to mastery codes.
        """
        raw_headers = [
            str(x).strip()
            for x in (data.get('learning_objectives', []) or [])
            if str(x).strip()
        ]

        # Canonicalize every header through the same rules used by the
        # import-grades API so synonyms ("Exam 1" vs "EX1" vs "EXAM1") fold
        # into one column rather than producing duplicates.
        def _canon(h: str) -> str:
            c = Homework.canonicalize_import_sheet_header(h)
            return c if c else str(h).strip()

        header_pairs: List[tuple] = [(h, _canon(h)) for h in raw_headers]
        seen_canon: set = set()
        learning_objectives: List[str] = []
        for _, c in header_pairs:
            if c not in seen_canon:
                seen_canon.add(c)
                learning_objectives.append(c)

        students_raw_in = data.get('students', []) or []
        students_raw = []
        for s in students_raw_in:
            raw_grades = dict(s.get('grades') or {})
            merged: Dict[str, object] = {}
            # When two headers fold to the same canonical key (e.g. "EX1" and
            # "Exam 1"), the *first non-empty* value wins; this matches the
            # left-to-right precedence a human would expect from the sheet.
            for orig, c in header_pairs:
                val = raw_grades.get(orig)
                if val is None and orig != c:
                    val = raw_grades.get(c)
                if val is None or str(val).strip() == "":
                    continue
                if c not in merged or not str(merged.get(c, "")).strip():
                    merged[c] = val
            known_origs = {p[0] for p in header_pairs}
            for k, val in raw_grades.items():
                if k in known_origs:
                    continue
                c = _canon(k)
                if val is None or str(val).strip() == "":
                    continue
                if c not in merged or not str(merged.get(c, "")).strip():
                    merged[c] = val
            students_raw.append({**s, "grades": merged})

        valid_marks = {'M', 'MR', 'X', 'R', 'P', 'A', 'RQ', '/'}

        hw_headers = [h for h in learning_objectives if Homework.is_import_sheet_hw_column(h)]
        lo_only = [
            h for h in learning_objectives
            if not Homework.is_import_sheet_hw_column(h)
            and not Homework.is_hw_prev_header(h)
        ]

        first_hw_label = hw_headers[0] if hw_headers else None

        students = []
        for s in students_raw:
            name = (s.get('name') or '').strip()
            if not name or len(name) < 2:
                continue

            grades: Dict[str, str] = {}
            raw_grades = s.get('grades', {}) or {}
            homework_pct: Optional[str] = None

            for h in learning_objectives:
                if h not in raw_grades:
                    continue
                mark = raw_grades[h]
                if Homework.is_hw_prev_header(h):
                    continue
                if Homework.is_import_sheet_hw_column(h):
                    parsed = Homework.parse_import_hw_pct(mark)
                    if parsed is not None:
                        homework_pct = str(parsed)
                    elif str(mark).strip() != "":
                        # Keep raw (e.g. "92.3") for UI editing if parse failed on odd formats
                        m = str(mark).strip()
                        if re.match(r"^\d+\.?\d*%?$", m):
                            homework_pct = m.rstrip("%")
                else:
                    mark_str = str(mark).strip().upper()
                    if mark_str in ('✓', '✔', 'CHECK', 'PASS', 'YES'):
                        mark_str = 'P'
                    if mark_str in valid_marks or re.match(r'^\d+\.?\d*$', mark_str):
                        grades[h] = mark_str

            students.append({
                'name': name,
                'grades': grades,
                'homework_pct': homework_pct,
            })

        return {
            'learning_objectives': lo_only,
            'homework_column': first_hw_label,
            'students': students
        }


_cached_analyzer = None
# Bumping `_module_version` invalidates the in-process cached analyzer. Used
# whenever the prompt, config, or normalization logic changes so reloaded
# code does not keep serving an analyzer wired to the old behavior.
_module_version = 19  # Skip the read-only HW prev column during normalization

def get_gemini_analyzer() -> Optional[GradeSheetGeminiAnalyzer]:
    """
    Factory function to create a Gemini grade sheet analyzer.
    Caches the instance so Gemini client is reused across requests.
    """
    global _cached_analyzer
    if (_cached_analyzer is not None
            and getattr(_cached_analyzer, '_version', 0) == _module_version):
        return _cached_analyzer
    try:
        _cached_analyzer = GradeSheetGeminiAnalyzer()
        _cached_analyzer._version = _module_version  # type: ignore
        return _cached_analyzer
    except (ImportError, ValueError) as e:
        logger.error("Failed to initialize Gemini analyzer: %s", e)
        return None
