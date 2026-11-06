
const studentsData = JSON.parse(document.getElementById('studentsJson').textContent);
const assignmentsData = JSON.parse(document.getElementById('assignmentsJson').textContent);
const learningObjectivesData = JSON.parse(document.getElementById('losJson').textContent);
const classId = JSON.parse(document.getElementById('classIdJson').textContent);
const CLASS_NAME = JSON.parse(document.getElementById('classNameJson').textContent);
const learningObjectiveCodeById = Object.fromEntries(
  learningObjectivesData.map((lo) => [String(lo.id), String(lo.vendor_code || '')])
);
const chevronSvg = '<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>';
const assignmentReportEl = document.getElementById('assignmentReport');
const studentHistoryPanelEl = document.getElementById('studentHistoryPanel');
const filterControlsEl = document.getElementById('filterControls');
const assignmentDropdownEl = document.getElementById('assignmentDropdown');
const studentDropdownEl = document.getElementById('studentDropdown');
const assignmentDropdownBtnEl = document.getElementById('assignmentDropdownBtn');
const studentDropdownBtnEl = document.getElementById('studentDropdownBtn');
const emailSelectedBtnEl = document.getElementById('emailSelectedBtn');
const viewAllEl = document.getElementById('view-all');
const viewAllAssignmentsEl = document.getElementById('view-all-assignments');

// Match Speed Grader thresholds/colors for HW %.
const EXAM_HW_MIN = 65;      // in-class grade eligible (yellow)
const REVISION_M_MIN = 75;   // revision to M eligible (green)

const defaultHwBadgeClass =
  'hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300';
const highHwBadgeClass =
  'hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300';
const midHwBadgeClass =
  'hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200';
const lowHwBadgeClass =
  'hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300';
const hwPassBadgeClass =
  'hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-sky-600 text-white';

function formatHwPrevScore(score) {
  if (score === null || score === undefined || score === '') return '';
  if (score === -1) return 'HW Pass';
  return score + '%';
}

function hwPrevBadgeClass(scoreClass) {
  return String(scoreClass || '').replace('hw-score-display', 'hw-prev-score-display');
}
// Per-assignment payload cache. The class reports page lets the instructor
// flip between assignments repeatedly; the API response for a given
// assignment never changes during a single visit, so caching by assignment
// id avoids re-fetching when the user toggles between them. Cleared on
// navigation away (the Map dies with the page).
const __assignmentGradesCache = new Map();

function _applyHwScoresInScope(scope, hwScores) {
  const els = document.querySelectorAll(scope);
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    const sid = el.getAttribute('data-student-id');
    let nextText = '—';
    let nextClass = defaultHwBadgeClass;
    if (sid && (sid in hwScores)) {
      const score = hwScores[sid];
      if (score === -1) {
        nextText = 'HW Pass';
        nextClass = hwPassBadgeClass;
      } else {
        nextText = score + '%';
        nextClass = score >= REVISION_M_MIN ? highHwBadgeClass
          : (score >= EXAM_HW_MIN ? midHwBadgeClass : lowHwBadgeClass);
      }
    }
    if (el.textContent !== nextText) el.textContent = nextText;
    if (el.className !== nextClass) el.className = nextClass;
  }
}

function _applyHwPrevScoresInScope(scope, hwPrevScores) {
  const els = document.querySelectorAll(scope);
  const scores = hwPrevScores || {};
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    const sid = el.getAttribute('data-student-id');
    let nextText = '';
    let nextClass = hwPrevBadgeClass(defaultHwBadgeClass);
    if (sid && Object.prototype.hasOwnProperty.call(scores, sid)) {
      const score = scores[sid];
      nextText = formatHwPrevScore(score);
      if (nextText === '') {
        nextClass = hwPrevBadgeClass(defaultHwBadgeClass);
      } else if (score === -1) {
        nextClass = hwPrevBadgeClass(hwPassBadgeClass);
      } else {
        nextClass = hwPrevBadgeClass(
          score >= REVISION_M_MIN ? highHwBadgeClass
            : (score >= EXAM_HW_MIN ? midHwBadgeClass : lowHwBadgeClass)
        );
      }
    }
    if (el.textContent !== nextText) el.textContent = nextText;
    if (el.className !== nextClass) el.className = nextClass;
  }
}

/** Stamp the non-counting red border on every M/MR badge in the given scope
 *  according to the counts_for_mastery flag from the API. Idempotent. */
function _applyCountsForMasteryInScope(scopeAssignmentId, countsMap, gradesMap) {
  const tableSelector = (scopeAssignmentId === undefined || scopeAssignmentId === null || String(scopeAssignmentId) === '')
    ? '#all-students-table'
    : '#view-all-assignments table[data-assignment-id="' + String(scopeAssignmentId) + '"]';
  const table = document.querySelector(tableSelector);
  if (!table) return;
  const rows = table.querySelectorAll('tbody tr.student-row');
  for (let r = 0; r < rows.length; r++) {
    const tr = rows[r];
    const sid = String(tr.getAttribute('data-student-id') || '');
    if (!sid) continue;
    const cells = tr.querySelectorAll('td[data-lo-id]');
    for (let c = 0; c < cells.length; c++) {
      const td = cells[c];
      const loId = String(td.getAttribute('data-lo-id') || '');
      if (!loId) continue;
      const badge = td.querySelector('.mastery-badge');
      if (!badge) continue;
      const grade = String(badge.getAttribute('data-raw-grade') || badge.textContent || '').toUpperCase().trim();
      if (grade !== 'M' && grade !== 'MR') {
        badge.classList.remove('non-counting-grade');
        badge.removeAttribute('data-non-counting');
        continue;
      }
      const key = sid + '|' + loId;
      let counts = true;
      if (countsMap && Object.prototype.hasOwnProperty.call(countsMap, key)) {
        counts = !!countsMap[key];
      }
      // If gradesMap is provided, the assignment-scoped letter wins over the
      // server-rendered top_score (which is the all-time top across assignments).
      if (gradesMap && Object.prototype.hasOwnProperty.call(gradesMap, key)) {
        const liveGrade = String(gradesMap[key] || '').toUpperCase();
        if (liveGrade !== grade) {
          // Different letter in this assignment; let _applyAssignmentGradesInScope handle it.
          continue;
        }
      }
      badge.classList.toggle('non-counting-grade', !counts);
      if (counts) {
        badge.removeAttribute('data-non-counting');
      } else {
        badge.setAttribute('data-non-counting', '1');
      }
    }
  }
}

function _applyAssignmentGradesInScope(scopeAssignmentId, gradesMap, countsMap) {
  const scopeId = (scopeAssignmentId === undefined || scopeAssignmentId === null)
    ? ''
    : String(scopeAssignmentId);
  const tables = [];
  if (scopeId === '') {
    const single = document.getElementById('all-students-table');
    if (single) tables.push(single);
  } else {
    document.querySelectorAll(
      '#view-all-assignments table[data-assignment-id="' + scopeId + '"]'
    ).forEach(function (table) { tables.push(table); });
  }
  tables.forEach(function (table) {
    table.querySelectorAll('tbody tr.student-row').forEach(function (row) {
      const studentId = String(row.getAttribute('data-student-id') || '');
      row.querySelectorAll('td[data-lo-id]').forEach(function (cell) {
        const loId = String(cell.getAttribute('data-lo-id') || '');
        const key = studentId + '|' + loId;
        const grade = String((gradesMap || {})[key] || '').toUpperCase();
        const counts = !Object.prototype.hasOwnProperty.call(countsMap || {}, key)
          || !!countsMap[key];
        if (!grade) {
          cell.innerHTML = '<span class="text-slate-300 dark:text-slate-600">—</span>';
          return;
        }
        const nonCounting = (grade === 'M' || grade === 'MR') && !counts;
        cell.innerHTML = '<span class="mastery-badge mastery-'
          + escapeHtml(grade.toLowerCase())
          + ' border cursor-pointer hover:opacity-80'
          + (nonCounting ? ' non-counting-grade' : '')
          + '" data-raw-grade="' + escapeHtml(grade) + '"'
          + (nonCounting ? ' data-non-counting="1"' : '')
          + '>' + escapeHtml(grade) + '</span>';
      });
    });
  });
}

function fetchHwScoresForAssignment(assignmentId, scopeAssignmentId, renderAssignmentGrades) {
  const scoped = (scopeAssignmentId !== undefined && scopeAssignmentId !== null);
  const scope = scoped
    ? '.hw-score-display[data-assignment-id="' + String(scopeAssignmentId) + '"]'
    : '.hw-score-display';
  const prevScope = scoped
    ? '.hw-prev-score-display[data-assignment-id="' + String(scopeAssignmentId) + '"]'
    : '.hw-prev-score-display';

  if (!assignmentId) {
    _applyHwScoresInScope(scope, {});
    _applyHwPrevScoresInScope(prevScope, {});
    // No assignment context: re-apply the server-rendered counts_for_mastery
    // (stored on the badge's data-non-counting attribute) so the class-wide
    // view still shows red borders for non-counting historical M/MR.
    return;
  }

  const cacheKey = String(assignmentId);
  if (__assignmentGradesCache.has(cacheKey)) {
    const cached = __assignmentGradesCache.get(cacheKey);
    _applyHwScoresInScope(scope, cached.hwScores);
    _applyHwPrevScoresInScope(prevScope, cached.hwPrevScores);
    if (renderAssignmentGrades) {
      _applyAssignmentGradesInScope(scopeAssignmentId, cached.gradesMap, cached.countsMap);
    }
    _applyCountsForMasteryInScope(scopeAssignmentId, cached.countsMap, cached.gradesMap);
    return;
  }

  fetch(`/api/class/${classId}/assignment/${assignmentId}/grades`)
    .then(r => r.json())
    .then(data => {
      if (!data.success) return;
      const payload = {
        hwScores: data.hw_scores || {},
        hwPrevScores: data.hw_prev_scores || {},
        countsMap: data.counts_for_mastery_map || {},
        gradesMap: data.grades || {},
      };
      __assignmentGradesCache.set(cacheKey, payload);
      _applyHwScoresInScope(scope, payload.hwScores);
      _applyHwPrevScoresInScope(prevScope, payload.hwPrevScores);
      if (renderAssignmentGrades) {
        _applyAssignmentGradesInScope(scopeAssignmentId, payload.gradesMap, payload.countsMap);
      }
      _applyCountsForMasteryInScope(scopeAssignmentId, payload.countsMap, payload.gradesMap);
    })
    .catch(e => console.error('Error loading HW scores:', e));
}

// --- Report tab toggle ---
function showReport(type) {
  assignmentReportEl.classList.toggle('hidden', type !== 'assignment');
  studentHistoryPanelEl.classList.toggle('hidden', type !== 'studentHistory');
  filterControlsEl.classList.toggle('hidden', type !== 'assignment');

  const activeClass = 'flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm bg-blue-600 text-white transition-colors';
  const inactiveClass = 'flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors';
  document.getElementById('btnAssignment').className = type === 'assignment' ? activeClass : inactiveClass;
  document.getElementById('btnStudentHistory').className = type === 'studentHistory' ? activeClass : inactiveClass;
}

// --- Student History search ---
function filterStudentHistory() {
  const searchTerm = document.getElementById('studentHistorySearchInput').value.toLowerCase();
  document.querySelectorAll('.student-history-item').forEach(item => {
    item.classList.toggle('hidden', !item.getAttribute('data-student-name').includes(searchTerm));
  });
}

// --- Dropdown open/close ---
function toggleDropdown(type) {
  if (type === 'assignment') {
    assignmentDropdownEl.classList.toggle('hidden');
    studentDropdownEl.classList.add('hidden');
  } else {
    studentDropdownEl.classList.toggle('hidden');
    assignmentDropdownEl.classList.add('hidden');
  }
}

document.addEventListener('click', function(e) {
  const aC = document.getElementById('assignmentDropdownContainer');
  const sC = document.getElementById('studentDropdownContainer');
  if (aC && !aC.contains(e.target)) assignmentDropdownEl.classList.add('hidden');
  if (sC && !sC.contains(e.target)) studentDropdownEl.classList.add('hidden');
});

// --- Filter by Assignment ---
const ALL_VIEW_IDS = {
  __all_mastery_opp__: 'mastery_opp',
  __all_exam__: 'exam',
  __all_project__: 'project'
};

function normalizeAssignmentType(raw) {
  const t = String(raw || 'mastery_opp');
  return (t === 'exam' || t === 'project' || t === 'mastery_opp') ? t : 'mastery_opp';
}

function isAllTypeView(id) {
  return Object.prototype.hasOwnProperty.call(ALL_VIEW_IDS, String(id || ''));
}

function canExportAssignmentCsv() {
  return !!(selectedAssignmentId && !isAllTypeView(selectedAssignmentId));
}

function updateExportCsvButton() {
  const btn = document.getElementById('exportCsvBtn');
  if (!btn) return;
  const ok = canExportAssignmentCsv();
  btn.disabled = !ok;
  btn.classList.toggle('opacity-50', !ok);
  btn.classList.toggle('cursor-not-allowed', !ok);
  btn.classList.toggle('hover:bg-emerald-700', ok);
  if (ok) {
    btn.removeAttribute('title');
  } else {
    btn.setAttribute(
      'title',
      'Select one assignment to export a CSV. All Mastery Opportunities, All Exams, and All Projects are not a single gradesheet.'
    );
  }
}

function exportAssignmentCsv() {
  if (!canExportAssignmentCsv()) return;
  window.location.href =
    '/class/' + encodeURIComponent(classId) +
    '/assignments/' + encodeURIComponent(selectedAssignmentId) +
    '/export-csv';
}

function assignmentTypeOf(a) {
  return normalizeAssignmentType(a && a.assignment_type);
}

let selectedAssignmentId = '';

const _debounce = (window.ClarityUI && typeof window.ClarityUI.debounce === 'function')
  ? window.ClarityUI.debounce
  : function (fn) { return fn; };

let __cachedAssignmentDdItems = null;
function _getAssignmentDdItems() {
  if (!__cachedAssignmentDdItems) {
    __cachedAssignmentDdItems = Array.from(document.querySelectorAll('.assignment-dd-item'));
  }
  return __cachedAssignmentDdItems;
}

// 80ms debounce is fast enough to feel synchronous when typing and slow
// enough to coalesce the burst of `input` events that fire on each
// keystroke; the cached node list keeps the filter under one frame.
const _runAssignmentFilter = _debounce(function () {
  const el = document.getElementById('assignmentDropdownSearch');
  if (!el) return;
  const search = el.value.toLowerCase();
  const items = _getAssignmentDdItems();
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const name = item.getAttribute('data-assignment-name') || '';
    const hide = !name.includes(search);
    if (item.classList.contains('hidden') !== hide) {
      item.classList.toggle('hidden', hide);
    }
  }
}, 80);

function filterAssignmentDropdown() {
  _runAssignmentFilter();
}

function selectAssignment(assignmentId, assignmentName) {
  selectedAssignmentId = assignmentId ? String(assignmentId) : '';
  assignmentDropdownEl.classList.add('hidden');
  assignmentDropdownBtnEl.innerHTML = escapeHtml(assignmentName) + ' ' + chevronSvg;
  assignmentDropdownBtnEl.className = 'flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors bg-blue-600 text-white hover:bg-blue-700';
  const heading = document.getElementById('assignmentReportHeading');
  if (heading) heading.textContent = assignmentName || 'Assignment Report';

  document.querySelectorAll('.assignment-dd-item').forEach(function (item) {
    const isActive = item.getAttribute('data-assignment-id') === String(assignmentId);
    item.classList.toggle('bg-blue-50', isActive);
    item.classList.toggle('dark:bg-blue-900/20', isActive);
    item.classList.toggle('font-medium', isActive);
  });

  applyAssignmentFilter();
  if (!isAllTypeView(selectedAssignmentId)) {
    fetchHwScoresForAssignment(selectedAssignmentId, '', true);
  }
  updateExportCsvButton();
}

function applyAssignmentFilter() {
  if (isAllTypeView(selectedAssignmentId)) {
    viewAllEl.classList.add('hidden');
    renderAllAssignmentsView(ALL_VIEW_IDS[selectedAssignmentId]);
    viewAllAssignmentsEl.classList.remove('hidden');
    updateEmailSelectedButton();
    return;
  }

  const assignment = assignmentsData.find(function (a) {
    return String(a.id) === String(selectedAssignmentId);
  });

  viewAllAssignmentsEl.classList.add('hidden');
  viewAllEl.classList.remove('hidden');
  if (!assignment || !assignment.assignment_objectives) {
    document.querySelectorAll('#view-all [data-lo-id]').forEach(function (el) {
      el.classList.remove('hidden');
    });
    updateEmailSelectedButton();
    return;
  }
  const loIds = new Set(assignment.assignment_objectives.map(function (ao) {
    return String(ao.learning_objective_id);
  }));
  document.querySelectorAll('#view-all [data-lo-id]').forEach(function (el) {
    el.classList.toggle('hidden', !loIds.has(String(el.getAttribute('data-lo-id'))));
  });
  updateEmailSelectedButton();
}

// --- Filter by Student ---
let __cachedStudentDdItems = null;
function _getStudentDdItems() {
  if (!__cachedStudentDdItems) {
    __cachedStudentDdItems = Array.from(document.querySelectorAll('.student-dd-item'));
  }
  return __cachedStudentDdItems;
}

const _runStudentFilter = _debounce(function () {
  const el = document.getElementById('studentDropdownSearch');
  if (!el) return;
  const search = el.value.toLowerCase();
  const items = _getStudentDdItems();
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const name = item.getAttribute('data-student-name') || '';
    const hide = !name.includes(search);
    if (item.classList.contains('hidden') !== hide) {
      item.classList.toggle('hidden', hide);
    }
  }
}, 80);

function filterStudentDropdown() {
  _runStudentFilter();
}

function applyStudentFilter() {
  const checked = [...document.querySelectorAll('.student-checkbox:checked')].map(cb => cb.value);
  const checkedSet = new Set(checked);

  if (checked.length > 0) {
    studentDropdownBtnEl.innerHTML = checked.length + ' Student' + (checked.length > 1 ? 's' : '') + ' Selected ' + chevronSvg;
    studentDropdownBtnEl.className = 'flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors bg-blue-600 text-white hover:bg-blue-700';
  } else {
    studentDropdownBtnEl.innerHTML = 'Filter by Student ' + chevronSvg;
    studentDropdownBtnEl.className = 'flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors bg-slate-200 dark:bg-slate-600 text-slate-700 dark:text-slate-300 hover:bg-slate-300 dark:hover:bg-slate-500';
  }

  document.querySelectorAll('.student-row').forEach(row => {
    const id = String(row.getAttribute('data-student-id'));
    row.classList.toggle('hidden', checkedSet.size > 0 && !checkedSet.has(id));
  });
  updateEmailSelectedButton();
}

function selectAllStudents() {
  document.querySelectorAll('.student-checkbox').forEach(cb => cb.checked = true);
  applyStudentFilter();
}

function clearStudentSelection() {
  document.querySelectorAll('.student-checkbox').forEach(cb => cb.checked = false);
  applyStudentFilter();
}

// --- Clear all filters ---
function clearAllFilters() {
  selectAssignment('__all_mastery_opp__', 'All Mastery Opportunities');
  clearStudentSelection();
  updateEmailSelectedButton();
}

function updateEmailSelectedButton() {
  if (!emailSelectedBtnEl) return;
  const selectedCount = document.querySelectorAll('.student-checkbox:checked').length;
  const show = isAllTypeView(selectedAssignmentId) && selectedCount > 0;
  emailSelectedBtnEl.classList.toggle('hidden', !show);
  if (show) {
    emailSelectedBtnEl.textContent = 'Email Selected (' + selectedCount + ')';
  }
}

const escapeHtml = (window.ClarityUI && window.ClarityUI.escapeHtml)
  ? window.ClarityUI.escapeHtml
  : function (value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    };

function loCodeForId(loId, fallbackObj) {
  const fromAssignment = fallbackObj && fallbackObj.learning_objectives && fallbackObj.learning_objectives.vendor_code
    ? String(fallbackObj.learning_objectives.vendor_code)
    : '';
  if (fromAssignment) return fromAssignment;
  return learningObjectiveCodeById[String(loId)] || 'LO';
}

function assignmentScopedGradeLetter(studentId, loId, assignmentId) {
  const aid = String(assignmentId || '');
  if (!aid || isAllTypeView(aid)) return '';
  const cached = __assignmentGradesCache.get(aid);
  if (!cached || !cached.gradesMap) return '';
  const key = String(studentId || '') + '|' + String(loId || '');
  return String(cached.gradesMap[key] || '').toUpperCase();
}

function renderAllAssignmentsView(typeFilter) {
  const container = viewAllAssignmentsEl;
  if (!container) return;

  const list = (assignmentsData || []).filter(function (a) {
    return normalizeAssignmentType(a && a.assignment_type) === typeFilter;
  });

  if (!list.length) {
    container.innerHTML =
      '<div class="bg-white dark:bg-slate-800 rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 p-8 text-center text-slate-500 dark:text-slate-400 text-sm">No assignments available.</div>';
    return;
  }

  let html = '';
  list.forEach(function (assignment) {
    const assignmentId = String(assignment.id || '');
    const assignmentName = assignment.name || 'Assignment';
    const aos = Array.isArray(assignment.assignment_objectives) ? assignment.assignment_objectives : [];
    const loMeta = aos
      .map(function (ao) {
        const loId = String(ao.learning_objective_id || '');
        if (!loId) return null;
        return {
          id: loId,
          code: loCodeForId(loId, ao)
        };
      })
      .filter(Boolean);

    html += '<div class="bg-white dark:bg-slate-800 rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 print:shadow-none print:border-0">';
    html += '  <div class="grade-sheet-header p-6 border-b border-slate-100 dark:border-slate-700">';
    html += '    <h3 class="font-semibold text-slate-900 dark:text-white">' + escapeHtml(assignmentName) + '</h3>';
    html += '    <p class="text-sm text-slate-500 dark:text-slate-400 mt-0.5">' + escapeHtml(CLASS_NAME) + '</p>';
    html += '  </div>';
    html += '  <div class="overflow-x-auto">';
    html += '    <table class="w-full text-sm" data-assignment-id="' + escapeHtml(assignmentId) + '">';
    html += '      <thead><tr class="border-b border-slate-200 dark:border-slate-700">';
    html += '        <th class="text-left px-3 py-2 font-medium text-slate-500 dark:text-slate-400 w-48">Student</th>';
    html += '        <th class="text-center px-2 py-2 font-medium text-slate-500 dark:text-slate-400 whitespace-nowrap print:hidden" data-email-col>Email</th>';
    html += '        <th class="text-center px-2 py-2 font-medium text-slate-500 dark:text-slate-400 whitespace-nowrap" data-hw-prev-col-header>HW % prev</th>';
    html += '        <th class="text-center px-2 py-2 font-medium text-slate-500 dark:text-slate-400 whitespace-nowrap" data-hw-col-header>HW %</th>';
    loMeta.forEach(function (lo) {
      html += '        <th class="text-center px-3 py-3 font-medium text-slate-500 dark:text-slate-400 text-xs" data-lo-id="' + escapeHtml(lo.id) + '"><div class="text-xs font-bold text-slate-800 dark:text-white">' + escapeHtml(lo.code) + '</div></th>';
    });
    html += '      </tr></thead>';
    html += '      <tbody class="divide-y divide-slate-50 dark:divide-slate-700">';

    studentsData.forEach(function (student) {
      const studentId = String(student.id || '');
      const studentName = String(student.name || '');
      const lowered = studentName.toLowerCase();
      html += '        <tr class="hover:bg-slate-50 dark:hover:bg-slate-700/40 transition-colors student-row group" data-student-name="' + escapeHtml(lowered) + '" data-student-id="' + escapeHtml(studentId) + '">';
      html += '          <td class="px-3 py-2 font-medium text-slate-900 dark:text-white"><span class="truncate min-w-0">' + escapeHtml(studentName) + '</span></td>';
      html += '          <td class="px-2 py-2 text-center print:hidden" data-email-col>';
      if (student.email) {
        html += '            <button type="button" data-email-student data-student-id="' + escapeHtml(studentId) + '" data-assignment-id="' + escapeHtml(assignmentId) + '" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-xs font-semibold bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-200 dark:hover:bg-emerald-900/50 transition-colors">Email</button>';
      } else {
        html += '            <span class="text-slate-300 dark:text-slate-600">—</span>';
      }
      html += '          </td>';
      html += '          <td class="px-2 py-2 text-center" data-hw-prev-cell><span class="hw-prev-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300" data-student-id="' + escapeHtml(studentId) + '" data-assignment-id="' + escapeHtml(assignmentId) + '"></span></td>';
      html += '          <td class="px-2 py-2 text-center" data-hw-cell><span class="hw-score-display inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300" data-student-id="' + escapeHtml(studentId) + '" data-assignment-id="' + escapeHtml(assignmentId) + '">—</span></td>';
      loMeta.forEach(function (lo) {
        html += '          <td class="px-3 py-3 text-center" data-lo-id="' + escapeHtml(lo.id) + '"><span class="text-slate-300 dark:text-slate-600">—</span></td>';
      });
      html += '        </tr>';
    });

    html += '      </tbody></table></div></div>';
  });

  container.innerHTML = html;
  list.forEach(function (assignment) {
    if (assignment && assignment.id) {
      fetchHwScoresForAssignment(String(assignment.id), String(assignment.id), true);
    }
  });
  applyStudentFilter();
}

// --- Individual student report data for email-to-student ---
function getIndividualReportParts(student, assignmentIdOverride) {
  const rawId = assignmentIdOverride
    ? String(assignmentIdOverride)
    : (selectedAssignmentId ? String(selectedAssignmentId) : '');
  const activeAssignmentId = (rawId && !isAllTypeView(rawId)) ? rawId : '';
  const visibleLoThs = [...document.querySelectorAll('#view-all th[data-lo-id]:not(.hidden)')];
  const visibleLoIds = visibleLoThs.map(th => th.getAttribute('data-lo-id'));
  const visibleLoNames = visibleLoThs.map(th => th.textContent.trim());

  const assignmentLabel = activeAssignmentId
    ? (assignmentsData.find(a => String(a.id) === String(activeAssignmentId))?.name || 'Assignment Report')
    : 'Assignment Report';
  const los = student.learning_objectives || [];
  const className = document.querySelector('#view-all p.text-sm')?.textContent.trim() || '';
  const rows = [];
  if (activeAssignmentId) {
    const assignment = assignmentsData.find(a => String(a.id) === String(activeAssignmentId));
    const assignmentObjectives = assignment && Array.isArray(assignment.assignment_objectives)
      ? assignment.assignment_objectives
      : [];
    assignmentObjectives.forEach((ao) => {
      const loId = String(ao.learning_objective_id || '');
      if (!loId) return;
      const scoped = assignmentScopedGradeLetter(student.id, loId, activeAssignmentId);
      const grade = scoped || 'Not graded';
      const loTitle = (ao.learning_objectives && ao.learning_objectives.vendor_code)
        ? String(ao.learning_objectives.vendor_code)
        : (learningObjectivesData.find(lo => String(lo.id) === loId)?.vendor_code || '');
      rows.push({ title: loTitle, grade: grade });
    });
  } else if (visibleLoIds.length > 0) {
    visibleLoIds.forEach((loId, i) => {
      const slo = los.find(lo => String(lo.learning_objective_id) === String(loId));
      const grade = slo ? (slo.top_score || 'Not graded') : 'Not graded';
      rows.push({ title: visibleLoNames[i] || '', grade: grade });
    });
  } else {
    los.forEach(lo => {
      rows.push({ title: lo.vendor_code || '', grade: lo.top_score || 'Not graded' });
    });
  }
  return { student, assignmentLabel, className, rows, homeworkPct: '' };
}

function buildEmailPayloadForStudent(student, assignmentIdOverride) {
  const parts = getIndividualReportParts(student, assignmentIdOverride);
  const lines = [
    'Hello ' + window.ClarityUI.studentGreetingName(parts.student.name) + ',',
    '',
    'Class: ' + (parts.className || ''),
    'Assignment: ' + parts.assignmentLabel,
    'Date: ' + new Date().toLocaleDateString(),
    '',
  ];
  lines.push('Learning objectives and scores shown in this report:');
  lines.push('');
  parts.rows.forEach(function (r) {
    const t = String(r.title || '').replace(/\s+/g, ' ').trim();
    lines.push(t + '  |  ' + String(r.grade || 'Not graded'));
  });
  let body = lines.join('\n');
  if (body.length > 2000) {
    body = body.slice(0, 1970) + '\n\n[Message trimmed. Ask your instructor for the full printed report if needed.]';
  }
  const subject = 'Your progress report — ' + (parts.className || 'Class');
  return { subject, body, parts };
}

function buildAllAssignmentsEmailPayloadForStudent(student) {
  const className = document.querySelector('#view-all p.text-sm')?.textContent.trim() || '';
  const lines = [
    'Hello ' + window.ClarityUI.studentGreetingName(student.name) + ',',
    '',
    'Class: ' + (className || ''),
    'Date: ' + new Date().toLocaleDateString(),
    '',
    'Assignments included in this report:',
    ''
  ];

  const typeFilter = isAllTypeView(selectedAssignmentId)
    ? ALL_VIEW_IDS[selectedAssignmentId]
    : null;
  const scopedAssignments = typeFilter
    ? assignmentsData.filter(function (a) { return assignmentTypeOf(a) === typeFilter; })
    : assignmentsData;

  scopedAssignments.forEach(function (assignment) {
    const assignmentName = String(assignment.name || 'Assignment');
    lines.push('Assignment: ' + assignmentName);
    lines.push('Learning objectives and scores shown in this report:');
    lines.push('');
    const assignmentObjectives = Array.isArray(assignment.assignment_objectives) ? assignment.assignment_objectives : [];
    assignmentObjectives.forEach(function (ao) {
      const loId = String(ao.learning_objective_id || '');
      if (!loId) return;
      const loCode = loCodeForId(loId, ao);
      const scoped = assignmentScopedGradeLetter(student.id, loId, assignment.id);
      const grade = scoped || 'Not graded';
      lines.push(String(loCode || 'LO') + '  |  ' + String(grade || 'Not graded'));
    });
    lines.push('');
  });

  let body = lines.join('\n');
  if (body.length > 2000) {
    body = body.slice(0, 1970) + '\n\n[Message trimmed. Ask your instructor for the full printed report if needed.]';
  }
  const typeLabel = typeFilter === 'exam' ? 'All Exams'
    : typeFilter === 'project' ? 'All Projects'
    : 'All Mastery Opportunities';
  const subject = 'Your progress report — ' + (className || 'Class') + ' (' + typeLabel + ')';
  return { subject, body };
}

function emailStudentReport(studentId, btnEl, assignmentIdOverride) {
  const student = studentsData.find(s => String(s.id) === String(studentId));
  if (!student) return;
  const addr = (student.email || '').trim();
  if (!addr) {
    alert('No email address is saved for this student. Add it from Students (or the class dashboard), then try again.');
    return;
  }
  const emailPayload = buildEmailPayloadForStudent(student, assignmentIdOverride);
  if (!confirm('Send this report directly to ' + addr + '?')) {
    return;
  }
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.classList.add('opacity-60', 'cursor-not-allowed');
  }
  const send = (window.ClarityUI && window.ClarityUI.apiRequest)
    ? window.ClarityUI.apiRequest
    : null;
  if (!send) {
    alert('Unexpected error sending report email.');
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
    }
    return;
  }
  send(`/api/class/${classId}/student/${encodeURIComponent(studentId)}/send-report-email`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      subject: emailPayload.subject,
      body: emailPayload.body
    })
  }).then((result) => {
    if (!result.ok || !result.data.success) {
      alert('Could not send email: ' + (result.data.error || result.errorMessage || 'Unknown error'));
      return;
    }
    alert('Report emailed to ' + (student.email || 'student') + '.');
  }).finally(() => {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
    }
  });
}

function bulkEmailResultText(data) {
  const sent = Number(data && data.sent || 0);
  const skipped = Number(data && data.skipped || 0);
  const failed = Number(data && data.failed || 0);
  const reasons = Array.isArray(data && data.failures) ? data.failures.slice(0, 5) : [];
  let text = 'Sent ' + sent + '. Skipped ' + skipped + '. Failed ' + failed + '.';
  if (reasons.length) text += '\n\n' + reasons.join('\n');
  return text;
}

function emailAllVisibleReports(btnEl) {
  const visibleRows = [...document.querySelectorAll('#assignmentReport .student-row:not(.hidden)')];
  const visibleIds = [...new Set(visibleRows.map((row) => String(row.getAttribute('data-student-id') || '')).filter(Boolean))];
  if (!visibleIds.length) {
    alert('No visible students to email.');
    return;
  }
  const reports = [];
  visibleIds.forEach((id) => {
    const student = studentsData.find((s) => String(s.id) === id);
    if (!student || !(student.email || '').trim()) return;
    const payload = buildEmailPayloadForStudent(student);
    reports.push({
      student_id: id,
      student_name: student.name || '',
      subject: payload.subject,
      body: payload.body
    });
  });
  if (!reports.length) {
    alert('None of the visible students have saved email addresses.');
    return;
  }
  if (!confirm(`Send reports directly to ${reports.length} student(s)?`)) {
    return;
  }

  const priorText = btnEl ? btnEl.textContent : 'Email All';
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.classList.add('opacity-60', 'cursor-not-allowed');
    btnEl.textContent = 'Sending...';
  }
  const send = (window.ClarityUI && window.ClarityUI.apiRequest)
    ? window.ClarityUI.apiRequest
    : null;
  if (!send) {
    alert('Unexpected error sending email reports.');
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
      btnEl.textContent = priorText || 'Email All';
    }
    return;
  }
  send(`/api/class/${classId}/send-report-emails`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ reports })
  }).then((result) => {
    const data = result.data || {};
    if (!result.ok || !data.success) {
      alert('Could not send email reports: ' + (data.error || result.errorMessage || 'Unknown error') + '\n\n' + bulkEmailResultText(data));
      return;
    }
    alert(bulkEmailResultText(data));
  }).finally(() => {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
      btnEl.textContent = priorText || 'Email All';
    }
  });
}

function emailSelectedAllAssignments(btnEl) {
  if (!isAllTypeView(selectedAssignmentId)) return;
  const selectedIds = [...document.querySelectorAll('.student-checkbox:checked')].map(cb => String(cb.value || ''));
  if (!selectedIds.length) {
    alert('Select at least one student first.');
    return;
  }
  const reports = [];
  selectedIds.forEach((id) => {
    const student = studentsData.find((s) => String(s.id) === id);
    if (!student || !(student.email || '').trim()) return;
    const payload = buildAllAssignmentsEmailPayloadForStudent(student);
    reports.push({
      student_id: id,
      student_name: student.name || '',
      subject: payload.subject,
      body: payload.body
    });
  });
  if (!reports.length) {
    alert('None of the selected students have saved email addresses.');
    return;
  }
  if (!confirm('Send all-assignment reports directly to ' + reports.length + ' selected student(s)?')) {
    return;
  }

  const priorText = btnEl ? btnEl.textContent : 'Email Selected';
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.classList.add('opacity-60', 'cursor-not-allowed');
    btnEl.textContent = 'Sending...';
  }
  const send = (window.ClarityUI && window.ClarityUI.apiRequest)
    ? window.ClarityUI.apiRequest
    : null;
  if (!send) {
    alert('Unexpected error sending selected reports.');
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
      updateEmailSelectedButton();
    }
    return;
  }
  send(`/api/class/${classId}/send-report-emails`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ reports })
  }).then((result) => {
    const data = result.data || {};
    if (!result.ok || !data.success) {
      alert('Could not send selected reports: ' + (data.error || result.errorMessage || 'Unknown error') + '\n\n' + bulkEmailResultText(data));
      return;
    }
    alert(bulkEmailResultText(data));
  }).finally(() => {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
      updateEmailSelectedButton();
    }
  });
}

// Auto-select first real assignment on page load (skip __all_* sentinels)
(function() {
  const items = Array.from(document.querySelectorAll('.assignment-dd-item[data-assignment-id]'));
  const firstAssignmentItem = items.find(function (el) {
    const id = el.getAttribute('data-assignment-id') || '';
    return id && !isAllTypeView(id);
  });
  if (firstAssignmentItem) {
    selectAssignment(firstAssignmentItem.getAttribute('data-assignment-id'), firstAssignmentItem.textContent.trim());
  } else {
    selectAssignment('__all_mastery_opp__', 'All Mastery Opportunities');
  }
})();

function applySinglePagePrintScale() {
  const assignmentReport = document.getElementById('assignmentReport');
  if (!assignmentReport) return;

  assignmentReport.style.removeProperty('--print-scale');

  ensureSingleAssignmentViewForExportLikePrint();

  const visiblePanel =
    assignmentReport.querySelector('#view-all:not(.hidden)') ||
    assignmentReport.querySelector('#view-all-assignments:not(.hidden)') ||
    assignmentReport;

  // Apply a temporary class that mirrors print sizing so we can measure accurately.
  const measureClass = 'measuring-print';
  assignmentReport.classList.add(measureClass);

  const pageHeightPx = 7.9 * 96;
  const targetTable = visiblePanel.querySelector('table');
  const sheetHeader = visiblePanel.querySelector('.grade-sheet-header');
  const tableRect = targetTable ? targetTable.getBoundingClientRect() : null;
  const assignmentReportHeight = assignmentReport.getBoundingClientRect().height;
  const visiblePanelHeight = visiblePanel.getBoundingClientRect().height;
  const tableHeight = tableRect
    ? tableRect.height
    : visiblePanel.getBoundingClientRect().height;
  const headerHeight = sheetHeader ? sheetHeader.getBoundingClientRect().height : 0;
  const measuredHeight = tableHeight + headerHeight;
  const tableRectHeight = tableRect ? tableRect.height : null;

  assignmentReport.classList.remove(measureClass);

  // Width stays on the table (width:100%, table-layout:fixed).
  // heightScale > 1 means the measured sheet is shorter than the page.
  const heightScale = pageHeightPx / Math.max(1, measuredHeight);
  // Shrink path keeps the 0.9 margin so a tall roster still fits one page.
  const shrinkSafety = 0.9;
  // Grow path uses 0.97. The sheet title is a fixed print size and the 1px borders do not scale,
  // so a raw heightScale already prints short of the measured target.
  const growSlack = 0.97;
  // 1.5 is 19.5px table text, about 14.6pt. Past that, a one-letter grade
  // and a name in the 14% column read as display type.
  const scaleCap = 1.5;
  const fitted = heightScale > 1 ? heightScale * growSlack : heightScale * shrinkSafety;
  const finalScale = Math.max(0.25, Math.min(scaleCap, fitted));
  console.log('[print-scale]', {
    pageHeightPx: pageHeightPx,
    measuredHeight: measuredHeight,
    headerHeight: headerHeight,
    tableHeight: tableHeight,
    heightScale: heightScale,
    fitted: fitted,
    finalScale: finalScale,
    assignmentReportHeight: assignmentReportHeight,
    visiblePanelHeight: visiblePanelHeight,
    tableRectHeight: tableRectHeight
  });
  assignmentReport.style.setProperty('--print-scale', String(finalScale));
}

function ensureSingleAssignmentViewForExportLikePrint() {
  if (isAllTypeView(selectedAssignmentId)) return;

  const viewAll = document.getElementById('view-all');
  if (viewAll && viewAll.classList.contains('hidden')) {
    const items = Array.from(document.querySelectorAll('.assignment-dd-item[data-assignment-id]'));
    const firstAssignmentItem = items.find(function (el) {
      const id = el.getAttribute('data-assignment-id') || '';
      return id && !isAllTypeView(id);
    });
    if (firstAssignmentItem && typeof selectAssignment === 'function') {
      selectAssignment(firstAssignmentItem.getAttribute('data-assignment-id'), firstAssignmentItem.textContent.trim());
    }
  }
}

function printableCellText(cell) {
  if (!cell) return '';
  let txt = String(cell.textContent || '').replace(/\s+/g, ' ').trim();
  if (txt === '—' || txt === '-' || txt === '–') return '';
  return txt;
}

/** Name cell includes an inline Print button; prefer the name span. */
function studentNameFromReportCell(cell) {
  if (!cell) return '';
  const nameEl = cell.querySelector('span.truncate');
  if (nameEl) return printableCellText(nameEl);
  return String(cell.textContent || '')
    .replace(/\s+/g, ' ')
    .replace(/Print\s*$/i, '')
    .trim();
}

async function exportGradeSheetPdf(btn) {
  try {
    if (!window.jspdf || !window.jspdf.jsPDF) {
      alert('PDF export is not available yet. Please try again in a moment.');
      return;
    }
    if (!window.jspdf.jsPDF.API || typeof window.jspdf.jsPDF.API.autoTable !== 'function') {
      alert('PDF export plugin is loading. Please try again in a moment.');
      return;
    }

    if (btn) {
      btn.disabled = true;
      btn.classList.add('opacity-70', 'cursor-not-allowed');
      btn.innerHTML = 'Exporting...';
    }

    applyStudentFilter();

    ensureSingleAssignmentViewForExportLikePrint();

    let exportAssignmentId = '';
    if (isAllTypeView(selectedAssignmentId)) {
      const t = ALL_VIEW_IDS[selectedAssignmentId];
      const firstOfType = assignmentsData.find(function (a) { return assignmentTypeOf(a) === t; });
      exportAssignmentId = firstOfType && firstOfType.id ? String(firstOfType.id) : '';
    } else if (selectedAssignmentId) {
      exportAssignmentId = String(selectedAssignmentId);
    } else if (assignmentsData[0] && assignmentsData[0].id) {
      exportAssignmentId = String(assignmentsData[0].id);
    }

    if (!exportAssignmentId) {
      alert('No assignment selected for export.');
      return;
    }

    let assignmentGrades = {};
    let assignmentCountsForMastery = {};
    let assignmentHwPrev = {};
    try {
      const res = await fetch(`/api/class/${classId}/assignment/${exportAssignmentId}/grades`, {
        credentials: 'same-origin'
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok || !data.success) {
        throw new Error(data.error || 'Could not load assignment grades');
      }
      assignmentGrades = data.grades || {};
      assignmentCountsForMastery = data.counts_for_mastery_map || {};
      assignmentHwPrev = data.hw_prev_scores || {};
    } catch (e) {
      alert('Could not load saved assignment grades for export.');
      return;
    }

    const activePanel =
      document.querySelector('#assignmentReport #view-all:not(.hidden)') ||
      document.querySelector('#assignmentReport #view-all-assignments:not(.hidden)') ||
      document.getElementById('assignmentReport');
    const table = activePanel ? activePanel.querySelector('table') : null;
    if (!table) {
      alert('No grade table available to export.');
      return;
    }

    // Drop the per-row Email column from the export. The cell is marked
    // data-email-col, so a column added after HW % does not shift the drop.
    function isReportsEmailCell(el) {
      return el.hasAttribute('data-email-col') || !!el.querySelector('#emailAllBtn');
    }
    const allHeaderThs = Array.from(table.querySelectorAll('thead tr th'));

    const headerCells = allHeaderThs.filter(function (th) {
      if (isReportsEmailCell(th)) return false;
      if (th.classList.contains('hidden')) return false;
      if (th.style && th.style.display === 'none') return false;
      return true;
    });
    const headers = headerCells.map(printableCellText);
    const headerLoIds = headerCells.map(function (th) {
      return String(th.getAttribute('data-lo-id') || '').trim();
    });
    const headerPrev = headerCells.map(function (th) {
      return th.hasAttribute('data-hw-prev-col-header');
    });

    const body = [];
    const rowEls = Array.from(table.querySelectorAll('tbody tr.student-row')).filter(function (tr) {
      return !tr.classList.contains('hidden');
    });
    rowEls.forEach(function (tr) {
      const sid = String(tr.getAttribute('data-student-id') || '').trim();
      const cells = Array.from(tr.querySelectorAll('td')).filter(function (td) {
        if (isReportsEmailCell(td)) return false;
        if (td.classList.contains('hidden')) return false;
        if (td.style && td.style.display === 'none') return false;
        return true;
      });
      const rowVals = cells.map(printableCellText);

      // Student column contains an inline "Print" button in the UI.
      if (cells.length > 0) {
        rowVals[0] = studentNameFromReportCell(cells[0]);
      }

      // Override LO cells with the assignment-scoped letter, applying the
      // non-counting M/MR -> I rule (matches window.print()).
      for (let colIdx = 0; colIdx < rowVals.length; colIdx++) {
        const loId = headerLoIds[colIdx] || '';
        if (!loId || !sid) continue;
        const key = `${sid}|${loId}`;
        const saved = assignmentGrades[key];
        let out = saved ? String(saved) : '';
        if ((out === 'M' || out === 'MR') &&
            Object.prototype.hasOwnProperty.call(assignmentCountsForMastery, key) &&
            assignmentCountsForMastery[key] === false) {
          out = 'I';
        }
        rowVals[colIdx] = out;
      }
      for (let colIdx = 0; colIdx < rowVals.length; colIdx++) {
        if (!headerPrev[colIdx]) continue;
        const raw = (sid && Object.prototype.hasOwnProperty.call(assignmentHwPrev, sid))
          ? assignmentHwPrev[sid]
          : null;
        rowVals[colIdx] = formatHwPrevScore(raw);
      }

      body.push(rowVals);
    });

    if (!headers.length || !body.length) {
      alert('No visible grade data to export.');
      return;
    }

    const classLabelEl = document.querySelector('#view-all p.text-sm');
    const classLabel = classLabelEl ? printableCellText(classLabelEl) : 'Class';
    const assignmentLabel = exportAssignmentId
      ? (assignmentsData.find(function (a) { return String(a.id) === String(exportAssignmentId); }) || {}).name || 'Assignment'
      : 'Grade Sheet';

    const { jsPDF } = window.jspdf;
    const doc = new jsPDF({
      orientation: 'landscape',
      unit: 'pt',
      format: 'letter',
      compress: true
    });

    doc.setFont('helvetica', 'bold');
    doc.setFontSize(14);
    doc.text('Grade Sheet', 28, 28);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(10);
    doc.text(classLabel + ' - ' + assignmentLabel, 28, 44);
    doc.text(new Date().toLocaleDateString(), 730, 44, { align: 'right' });

    // Match the printed sheet: white cells, grid lines, and bold colored
    // letters per grade. No badge fills — the on-screen rounded chip styling
    // does not survive a paper print either, so the PDF and print stay
    // visually consistent.
    const PDF_GRADE_TEXT_COLOR = {
      'M':  [21, 128, 61],
      'MR': [4, 120, 87],
      'R':  [161, 98, 7],
      'RQ': [194, 65, 12],
      'P':  [29, 78, 216],
      'X':  [185, 28, 28],
      'A':  [71, 85, 105],
      'I':  [146, 64, 14]
    };

    const columnStyles = {};
    headerCells.forEach(function (th, idx) {
      if (th.hasAttribute('data-hw-col-header') || th.hasAttribute('data-hw-prev-col-header')) {
        columnStyles[idx] = { cellWidth: 58, halign: 'center' };
      }
    });

    doc.autoTable({
      head: [headers],
      body: body,
      startY: 54,
      tableWidth: doc.internal.pageSize.getWidth() - 40,
      margin: { left: 20, right: 20, top: 54, bottom: 20 },
      columnStyles: columnStyles,
      theme: 'grid',
      styles: {
        font: 'helvetica',
        fontSize: 8,
        cellPadding: 3,
        lineColor: [17, 24, 39],
        lineWidth: 0.6,
        textColor: [15, 23, 42],
        fillColor: [255, 255, 255],
        overflow: 'linebreak'
      },
      headStyles: {
        fillColor: [255, 255, 255],
        textColor: [15, 23, 42],
        fontStyle: 'bold'
      },
      didParseCell: function (hookData) {
        if (hookData.section !== 'body') return;
        if (hookData.column.index === 0) return;
        hookData.cell.styles.halign = 'center';
        const raw = String(hookData.cell.raw == null ? '' : hookData.cell.raw)
          .trim().toUpperCase();
        const tc = PDF_GRADE_TEXT_COLOR[raw];
        if (!tc) return;
        hookData.cell.styles.textColor = tc;
        // "I" uses a serif font so the capital I renders with the
        // characteristic top-and-bottom bars, matching the print preview.
        // All other grade letters use the standard bold helvetica.
        if (raw === 'I') {
          hookData.cell.styles.font = 'times';
          hookData.cell.styles.fontStyle = 'bold';
        } else {
          hookData.cell.styles.fontStyle = 'bold';
        }
      }
    });

    const safeAssignment = String(assignmentLabel || 'assignment')
      .trim()
      .toLowerCase()
      .replace(/[^\w\-]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'assignment';
    doc.save(safeAssignment + '_gradesheet.pdf');
  } catch (err) {
    console.error('PDF export error:', err);
    alert('Could not export PDF.');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('opacity-70', 'cursor-not-allowed');
      btn.innerHTML = `<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 16v-8m0 8l-3-3m3 3l3-3M5 20h14"/>
        </svg>
        Export PDF`;
    }
  }
}

// On paper, non-counting M / MR prints as the letter "I" so the grading rules
// remain intuitive for anyone reading a printed sheet, even though the on-
// screen UI shows a red-bordered M to make the distinction visible to the
// instructor. Originals are cached on each badge and restored in `afterprint`.
// We also strip the red "non-counting" outline and swap the green mastery-m/mr
// color class for the regular yellow mastery-i class so the printed cell looks
// like any other "I" grade rather than a red-bordered M.
function _swapNonCountingMastersToIForPrint() {
  const badges = document.querySelectorAll('.mastery-badge.non-counting-grade');
  for (let i = 0; i < badges.length; i++) {
    const b = badges[i];
    const raw = String(b.getAttribute('data-raw-grade') || b.textContent || '').trim().toUpperCase();
    if (raw !== 'M' && raw !== 'MR') continue;
    if (!b.hasAttribute('data-print-original-text')) {
      b.setAttribute('data-print-original-text', b.textContent);
    }
    if (!b.hasAttribute('data-print-original-class')) {
      b.setAttribute('data-print-original-class', b.className);
    }
    b.textContent = 'I';
    b.classList.remove('non-counting-grade', 'mastery-m', 'mastery-mr');
    b.classList.add('mastery-i');
  }
}

function _restoreNonCountingMastersAfterPrint() {
  const badges = document.querySelectorAll('.mastery-badge[data-print-original-text]');
  for (let i = 0; i < badges.length; i++) {
    const b = badges[i];
    b.textContent = b.getAttribute('data-print-original-text') || b.textContent;
    const origClass = b.getAttribute('data-print-original-class');
    if (origClass !== null) {
      b.className = origClass;
      b.removeAttribute('data-print-original-class');
    }
    b.removeAttribute('data-print-original-text');
  }
}

window.addEventListener('beforeprint', function () {
  _swapNonCountingMastersToIForPrint();
  applySinglePagePrintScale();
});
window.addEventListener('afterprint', function () {
  _restoreNonCountingMastersAfterPrint();
  const assignmentReport = document.getElementById('assignmentReport');
  if (!assignmentReport) return;
  assignmentReport.style.removeProperty('--print-scale');
});

document.getElementById('btnAssignment').addEventListener('click', function () {
  showReport('assignment');
});
document.getElementById('btnStudentHistory').addEventListener('click', function () {
  showReport('studentHistory');
});
document.getElementById('exportPdfBtn').addEventListener('click', function () {
  exportGradeSheetPdf(this);
});
document.getElementById('exportCsvBtn').addEventListener('click', function () {
  exportAssignmentCsv();
});
document.getElementById('assignmentDropdownBtn').addEventListener('click', function () {
  toggleDropdown('assignment');
});
document.getElementById('assignmentDropdownSearch').addEventListener('input', function () {
  filterAssignmentDropdown();
});
document.addEventListener('click', function (event) {
  var btn = event.target.closest('.assignment-dd-item');
  if (!btn) return;
  selectAssignment(btn.getAttribute('data-assignment-id'), btn.textContent);
});
document.getElementById('studentDropdownBtn').addEventListener('click', function () {
  toggleDropdown('student');
});
document.getElementById('studentDropdownSearch').addEventListener('input', function () {
  filterStudentDropdown();
});
document.getElementById('selectAllStudentsBtn').addEventListener('click', function () {
  selectAllStudents();
});
document.getElementById('clearStudentSelectionBtn').addEventListener('click', function () {
  clearStudentSelection();
});
document.addEventListener('change', function (event) {
  var box = event.target.closest('.student-checkbox');
  if (!box) return;
  applyStudentFilter();
});
document.getElementById('clearAllFiltersBtn').addEventListener('click', function () {
  clearAllFilters();
});
document.getElementById('emailSelectedBtn').addEventListener('click', function () {
  emailSelectedAllAssignments(this);
});
document.getElementById('emailAllBtn').addEventListener('click', function () {
  emailAllVisibleReports(this);
});
document.addEventListener('click', function (event) {
  var btn = event.target.closest('[data-email-student]');
  if (!btn) return;
  emailStudentReport(btn.getAttribute('data-student-id'), btn, btn.getAttribute('data-assignment-id') || undefined);
});
document.getElementById('studentHistorySearchInput').addEventListener('keyup', function () {
  filterStudentHistory();
});
