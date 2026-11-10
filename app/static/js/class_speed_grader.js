
const INSTRUCTOR_MODE = JSON.parse(document.getElementById('instructorModeJson').textContent);
const GRADES = ['M', 'R', 'Q', 'P', 'X', 'A'];
const GRADE_MAP = { 'Q': 'RQ' }; // Q key → RQ grade
const gradeHistory = [];
const grades = {};
let hwScoresMap = {};
const classId = JSON.parse(document.getElementById('classIdJson').textContent);
// Read server-side flag as JSON string so this line stays valid JS in editors.
const AUTO_CONVERT_M = JSON.parse(document.getElementById('autoConvertMJson').textContent);

// Synced horizontal scroll: thin bar at the top of the card + main table area below.
// Table stays at 100% zoom (readable); overflow is handled by scrolling, not shrinking.
(function setupGraderHorizontalScrollSync() {
  const topEl = document.getElementById('graderTableTopScroll');
  const bottomEl = document.getElementById('graderTableScrollWrap');
  const innerEl = document.getElementById('graderTableTopScrollInner');
  const table = document.getElementById('graderTable');
  if (!topEl || !bottomEl || !innerEl) return;

  let syncing = false;

  function syncScrollMetrics() {
    const sw = bottomEl.scrollWidth;
    innerEl.style.width = sw + 'px';
    innerEl.style.minWidth = sw + 'px';
    const max = Math.max(0, sw - bottomEl.clientWidth);
    if (bottomEl.scrollLeft > max) bottomEl.scrollLeft = max;
    if (topEl.scrollLeft > max) topEl.scrollLeft = max;
  }

  function onTopScroll() {
    if (syncing) return;
    syncing = true;
    bottomEl.scrollLeft = topEl.scrollLeft;
    syncing = false;
  }

  function onBottomScroll() {
    if (syncing) return;
    syncing = true;
    topEl.scrollLeft = bottomEl.scrollLeft;
    syncing = false;
  }

  topEl.addEventListener('scroll', onTopScroll, { passive: true });
  bottomEl.addEventListener('scroll', onBottomScroll, { passive: true });

  window.addEventListener('resize', function () {
    window.clearTimeout(window.__graderScrollResizeT);
    window.__graderScrollResizeT = window.setTimeout(syncScrollMetrics, 50);
  });

  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(function () {
      window.requestAnimationFrame(syncScrollMetrics);
    });
    ro.observe(bottomEl);
    if (table) ro.observe(table);
  }

  window.graderTableSyncScrollMetrics = syncScrollMetrics;

  window.requestAnimationFrame(syncScrollMetrics);
  window.setTimeout(syncScrollMetrics, 100);
  window.setTimeout(syncScrollMetrics, 400);
})();

(function pinHwColumnToStudentWidth() {
  const scrollWrap = document.getElementById('graderTableScrollWrap');
  if (!scrollWrap) return;

  function update() {
    const studentTh = document.querySelector('#graderTable thead th.sticky');
    if (!studentTh) return;
    const w = Math.ceil(studentTh.getBoundingClientRect().width);
    scrollWrap.style.setProperty('--student-col-width', w + 'px');
  }

  window.addEventListener('resize', update);
  // In case fonts/layout shift after load.
  window.requestAnimationFrame(update);
  window.setTimeout(update, 250);
})();

/** HW % thresholds: in-class exam marks vs revision to M (M/MR) */
const EXAM_HW_MIN = 65;
const REVISION_M_MIN = 75;
const examGradeEligible = {};
const revisionEligible = {};
let hasUnsavedChanges = false;
let masteryResetSerial = 0;
const resetMasterySerial = new Map();

function isMasteryLetter(value) {
  const letter = String(value || '').toUpperCase();
  return letter === 'M' || letter === 'MR';
}

function markMasteryDeparture(key) {
  masteryResetSerial += 1;
  resetMasterySerial.set(key, masteryResetSerial);
}

function markUnsavedChanges() {
  hasUnsavedChanges = true;
}

function isActiveHwControl(el) {
  if (!el) return false;
  if (!el.closest('td[data-hw-cell]')) return false;
  const flat = document.getElementById('graderFlatCard');
  if (flat && flat.classList.contains('hidden')) return false;
  return true;
}

function hiddenHwCellForStudent(studentId) {
  const sid = String(studentId || '');
  if (!sid) return null;
  const esc = typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(sid) : sid;
  let row;
  try {
    row = getActiveGraderRoot().querySelector('tr[data-student-row][data-student-id="' + esc + '"]');
  } catch (e) {
    return null;
  }
  return row ? row.querySelector('td[data-hw-cell] .hw-cell') : null;
}

function syncHiddenHwCellFromScore(studentId, rawScore) {
  const cell = hiddenHwCellForStudent(studentId);
  if (!cell) return;
  const input = cell.querySelector('.hw-pct-input');
  const passText = cell.querySelector('.hw-pass-text');
  const passCb = cell.querySelector('.hw-pass-check');
  if (!input) return;
  if (rawScore === -1 || rawScore === '-1') {
    input.classList.add('hidden');
    input.value = '';
    if (passText) passText.classList.remove('hidden');
    if (passCb) passCb.checked = true;
    input.dataset.lastSaved = '-1';
    setHwEligibilityFromState(studentId, null, true);
    setLiveHwScore(studentId, -1);
  } else if (rawScore !== '' && rawScore !== null && rawScore !== undefined) {
    input.classList.remove('hidden');
    const n = Math.max(0, Math.min(100, parseInt(String(rawScore), 10) || 0));
    input.value = String(n);
    input.style.background = hwInputBackground(n, false);
    if (passText) passText.classList.add('hidden');
    if (passCb) passCb.checked = false;
    input.dataset.lastSaved = String(n);
    setHwEligibilityFromState(studentId, n, false);
    setLiveHwScore(studentId, n);
  } else {
    input.classList.remove('hidden');
    input.value = '';
    input.style.background = '#64748b';
    if (passText) passText.classList.add('hidden');
    if (passCb) passCb.checked = false;
    input.dataset.lastSaved = '';
    setHwEligibilityFromState(studentId, null, false);
    setLiveHwScore(studentId, '');
  }
}

function recomputeUnsavedState() {
  const hwDirty = Array.from(document.querySelectorAll('.hw-pct-input')).some((input) => {
    if (!isActiveHwControl(input)) return false;
    if (input.classList.contains('hidden')) return false;
    const raw = String(input.value || '').trim();
    if (raw === '') return false;
    const val = String(Math.max(0, Math.min(100, parseInt(raw, 10) || 0)));
    const lastSaved = input.dataset.lastSaved;
    return lastSaved == null || String(lastSaved) !== val;
  });
  hasUnsavedChanges = gradeHistory.length > 0 || hwDirty;
}

function getActiveAssignmentId() {
  const sel = document.getElementById('assignmentFilter');
  const v = sel ? String(sel.value || '').trim() : '';
  return v || null;
}

/** Composite client key: student|lo|assignment. */
function gradeCellKey(studentId, loId, assignmentId) {
  return String(studentId || '') + '|' + String(loId || '') + '|' + String(assignmentId || '');
}

function gradeCellKeyFromCell(cell) {
  if (!cell) return '||';
  const sid = cell.dataset.studentId || cell.getAttribute('data-student-id') || '';
  const loId = cell.dataset.loId || cell.getAttribute('data-lo-id') || '';
  let aid = cell.dataset.assignmentId || cell.getAttribute('data-assignment-id') || '';
  if (!aid) aid = getActiveAssignmentId() || '';
  return gradeCellKey(sid, loId, aid);
}

/** save-grades API still expects student|lo; assignment_id is a separate body field. */
function apiGradeKeyFromComposite(compositeKey) {
  const parts = String(compositeKey || '').split('|');
  if (parts.length < 2) return '';
  return parts[0] + '|' + parts[1];
}

function compositeGradeKeyFromApi(apiKey, assignmentId) {
  return String(apiKey || '') + '|' + String(assignmentId || '');
}

function getActiveAssignmentHwGroup() {
  const sel = document.getElementById('assignmentFilter');
  if (!sel || sel.selectedIndex < 0) return '';
  return String(sel.options[sel.selectedIndex].getAttribute('data-group') || '').trim();
}

function updateActiveHomeworkGroupLabel() {
  const el = document.getElementById('activeHomeworkGroupValue');
  if (!el) return;
  const hg = getActiveAssignmentHwGroup();
  el.textContent = hg || '—';
}

function populateHwScoreInputs(hwScores) {
  const map = coerceStudentKeyedMap(hwScores || {});
  document.querySelectorAll('td[data-hw-cell] .hw-pct-input').forEach(function (input) {
    const sidRaw = String(input.dataset.studentId || '').trim();
    const sidKey = normalizeStudentKeyForMap(sidRaw);
    let raw = null;
    if (sidKey && Object.prototype.hasOwnProperty.call(map, sidKey)) {
      raw = map[sidKey];
    }
    syncHiddenHwCellFromScore(sidRaw, raw);
  });
}

function hwPercentageSaveBody(studentId, score) {
  const body = {
    student_id: studentId,
    score: score,
    assignment_id: getActiveAssignmentId()
  };
  const hg = getActiveAssignmentHwGroup();
  if (hg) body.homework_group = hg;
  return body;
}

/** Align API map keys with dataset student ids (trim + lowercase) so `sid in hw_scores` works after JSON round-trip. */
function normalizeStudentKeyForMap(id) {
  return String(id == null ? '' : id).trim().toLowerCase();
}

function coerceStudentKeyedMap(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const k of Object.keys(raw)) {
    const nk = normalizeStudentKeyForMap(k);
    if (nk) out[nk] = raw[k];
  }
  return out;
}

function studentHasRecordedHwScore(hwMap, studentId) {
  const sid = normalizeStudentKeyForMap(studentId);
  if (!sid || !hwMap || typeof hwMap !== 'object') return false;
  if (!Object.prototype.hasOwnProperty.call(hwMap, sid)) return false;
  return hwMap[sid] !== null && hwMap[sid] !== undefined;
}

function setLiveHwScore(studentId, scorePct) {
  const sid = normalizeStudentKeyForMap(studentId);
  if (!sid) return;
  if (scorePct === null || scorePct === undefined || scorePct === '') {
    delete hwScoresMap[sid];
  } else {
    hwScoresMap[sid] = scorePct;
  }
  applyHwGradeLockForStudent(studentId);
}

function showHwGradeLockNotice() {
  const el = document.getElementById('hwGradeLockNotice');
  if (!el) return;
  el.classList.remove('hidden');
}

function applyHwGradeLockForStudent(studentId) {
  const recorded = studentHasRecordedHwScore(hwScoresMap, studentId);
  const sid = String(studentId || '');
  if (!sid) return;
  const esc = typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(sid) : sid;
  let cells;
  try {
    cells = getActiveGraderRoot().querySelectorAll('.grade-cell[data-student-id="' + esc + '"]');
  } catch (e) {
    return;
  }
  const lockTitle = 'Enter a homework % before grading. Absent (A) is still allowed.';
  cells.forEach(function (cell) {
    cell.classList.toggle('hw-lock-no-score', !recorded);
    if (recorded) {
      if (cell.getAttribute('title') === lockTitle) cell.removeAttribute('title');
    } else {
      cell.setAttribute('title', lockTitle);
    }
  });
}

function applyAllHwGradeLocks() {
  document.querySelectorAll('tr[data-student-row]').forEach(function (row) {
    applyHwGradeLockForStudent(row.getAttribute('data-student-id'));
  });
}

function gradeBadgeHtml(grade) {
  const g = String(grade);
  return `<span class="mastery-badge mastery-${g.toLowerCase()} border">${g}</span>`;
}

/** HW cell for this student, preferring the same table row as the grade cell (avoids wrong student + stale cache). */
function resolveHwCell(studentId, gradeCellEl) {
  const sid = String(studentId || '');
  if (gradeCellEl && typeof gradeCellEl.closest === 'function') {
    const row = gradeCellEl.closest('tr[data-student-row]');
    if (row) {
      const hw = row.querySelector('.hw-cell');
      if (hw) return hw;
    }
  }
  if (!sid) return null;
  const attr = typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
    ? CSS.escape(sid)
    : sid;
  const root = getActiveGraderRoot();
  try {
    return root.querySelector('.hw-cell[data-student-id="' + attr + '"]');
  } catch (e) {
    return null;
  }
}

/**
 * Live-DOM HW eligibility check, used only to PREDICT counts_for_mastery on a
 * brand-new M/MR keystroke before the server has had a chance to persist the
 * flag. The persisted flag (data-counts-for-mastery) is the source of truth
 * for already-saved rows; this function never re-evaluates them.
 */
function predictCountsForMasteryFromCurrentHw(studentId, gradeCellEl) {
  if (!AUTO_CONVERT_M) return true;
  const sid = String(studentId || '');
  const hwCell = resolveHwCell(sid, gradeCellEl);
  if (!hwCell) {
    return true;
  }
  const passEl = hwCell.querySelector('.hw-pass-check');
  const passChecked = !!(passEl && passEl.checked);
  if (passChecked) return true;
  const input = hwCell.querySelector('.hw-pct-input');
  const rawHw = input ? String(input.value || '').trim() : '';
  if (rawHw === '') {
    return true;
  }
  const hw = parseInt(rawHw, 10);
  return Number.isFinite(hw) && hw >= EXAM_HW_MIN;
}

/**
 * Cells now always render the raw stored letter. The legacy M->I visual swap
 * has moved to print/PDF export time on the Reports tab; in the Speed Grader,
 * non-counting M/MR is communicated purely by the red border on the cell.
 */
function getDisplayedGradeForStudent(rawGrade /*, studentId, gradeCellEl */) {
  return String(rawGrade || '').toUpperCase();
}

/**
 * Stamp the persistent counts_for_mastery flag on a newly-entered cell.
 * First-entry-only: if the cell already had a value with the flag set, we
 * leave it alone (mirrors the server's stickiness rule).
 */
function applyCountsForMasteryOnEdit(cell, rawGrade, prevGrade) {
  const g = String(rawGrade || '').toUpperCase();
  if (INSTRUCTOR_MODE !== 'mark') {
    delete cell.dataset.countsForMastery;
    return;
  }
  if (g !== 'M' && g !== 'MR') {
    // Non-mastery letters don't carry a meaningful flag.
    delete cell.dataset.countsForMastery;
    return;
  }
  const prev = String(prevGrade || '').toUpperCase();
  const hadMasteryAlready = (prev === 'M' || prev === 'MR') &&
    (cell.dataset.countsForMastery === '0' || cell.dataset.countsForMastery === '1');
  if (hadMasteryAlready) {
    // Sticky: keep the original flag from when M was first entered.
    return;
  }
  const sid = String(cell.dataset.studentId || '');
  const counts = predictCountsForMasteryFromCurrentHw(sid, cell);
  cell.dataset.countsForMastery = counts ? '1' : '0';
}

function renderGradeInCell(cell, rawGrade) {
  const raw = String(rawGrade || '').toUpperCase();
  const shown = getDisplayedGradeForStudent(raw);
  // Red border = non-counting M/MR, driven purely by the persisted flag. Only
  // shown in mark mode (Shelbi hides this distinction by design).
  const isMastery = raw === 'M' || raw === 'MR';
  const isNonCountingMastery =
    INSTRUCTOR_MODE === 'mark' &&
    isMastery &&
    cell.dataset.countsForMastery === '0';
  cell.classList.toggle('non-counting-grade', isNonCountingMastery);
  let html = shown
    ? gradeBadgeHtml(shown)
    : '<span class="text-slate-200 dark:text-slate-600 text-[12px] select-none">—</span>';
  cell.innerHTML = html;
}

/** After an HW pass, remove red borders from all non-counting M/MR for this student. */
function promoteNonCountingMasteriesInDom(studentId) {
  const sid = String(studentId || '');
  if (!sid) return;
  const esc = typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
    ? CSS.escape(sid)
    : sid;
  let cells;
  try {
    cells = document.querySelectorAll('.grade-cell[data-student-id="' + esc + '"]');
  } catch (e) {
    return;
  }
  cells.forEach(function (cell) {
    const g = String(cell.dataset.actualGrade || '').toUpperCase();
    if ((g === 'M' || g === 'MR') && cell.dataset.countsForMastery === '0') {
      cell.dataset.countsForMastery = '1';
      renderGradeInCell(cell, g);
    }
  });
}

/** Turn the server-demoted cells for this student red again. loIds are the active assignment only. */
function demotePromotedMasteriesInDom(studentId, loIds) {
  const sid = String(studentId || '');
  if (!sid) return;
  const wanted = {};
  (loIds || []).forEach(function (id) {
    wanted[String(id)] = true;
  });
  const esc = typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
    ? CSS.escape(sid)
    : sid;
  let cells;
  try {
    cells = document.querySelectorAll('.grade-cell[data-student-id="' + esc + '"]');
  } catch (e) {
    return;
  }
  cells.forEach(function (cell) {
    const loId = String(cell.dataset.loId || '');
    if (!wanted[loId]) return;
    const g = String(cell.dataset.actualGrade || '').toUpperCase();
    if (g !== 'M' && g !== 'MR') return;
    cell.dataset.countsForMastery = '0';
    renderGradeInCell(cell, g);
  });
}

function setHwEligibilityFromState(studentId, scoreNum, isPass /* , skipGradeRefresh */) {
  if (INSTRUCTOR_MODE !== 'mark') {
    examGradeEligible[studentId] = true;
    revisionEligible[studentId] = true;
    return;
  }
  if (isPass) {
    examGradeEligible[studentId] = true;
    revisionEligible[studentId] = false;
  } else if (scoreNum === null || scoreNum === undefined || (typeof scoreNum === 'number' && isNaN(scoreNum))) {
    examGradeEligible[studentId] = false;
    revisionEligible[studentId] = false;
  } else {
    const n = typeof scoreNum === 'number' ? scoreNum : parseInt(String(scoreNum), 10);
    if (isNaN(n)) {
      examGradeEligible[studentId] = false;
      revisionEligible[studentId] = false;
    } else {
      examGradeEligible[studentId] = n >= EXAM_HW_MIN;
      revisionEligible[studentId] = n >= REVISION_M_MIN;
    }
  }
  updateRevisionStatus(studentId);
  // Intentionally no longer re-renders displayed grades: counts_for_mastery is
  // captured at first-entry time and persisted, so HW% changes after the fact
  // do NOT alter the visual state of existing M/MR cells.
}

function hwInputBackground(scoreNum, isPass) {
  if (INSTRUCTOR_MODE !== 'mark') return '#64748b';
  if (isPass) return '#0ea5e9';
  if (scoreNum === null || scoreNum === undefined || (typeof scoreNum === 'number' && isNaN(scoreNum))) return '#64748b';
  const n = parseInt(String(scoreNum), 10);
  if (isNaN(n)) return '#64748b';
  if (n >= REVISION_M_MIN) return '#22c55e';
  if (n >= EXAM_HW_MIN) return '#f59e0b';
  return '#ef4444';
}

/** Persist every visible HW % field (used on Save so values commit even if input never blurred). */
async function persistAllHwPctInputs() {
  const activeAssignmentId = getActiveAssignmentId();
  if (!activeAssignmentId) {
    throw new Error('Select an assignment before saving homework scores.');
  }
  const errors = [];

  const inputs = Array.from(document.querySelectorAll('.hw-pct-input')).filter(input => {
    if (!isActiveHwControl(input)) return false;
    if (input.classList.contains('hidden')) return false;
    const raw = String(input.value || '').trim();
    if (raw === '') return false;
    return true;
  });

  // Only save what changed (prevents hammering the backend and causing disconnects).
  const dirtyInputs = inputs.filter(input => {
    const raw = String(input.value || '').trim();
    const val = String(Math.max(0, Math.min(100, parseInt(raw, 10) || 0)));
    const lastSaved = input.dataset.lastSaved;
    return lastSaved == null || lastSaved === '' || String(lastSaved) !== val;
  });

  // Save with limited concurrency to avoid server overload.
  const concurrency = 5;
  let index = 0;

  async function worker() {
    while (index < dirtyInputs.length) {
      const input = dirtyInputs[index++];
      const raw = String(input.value || '').trim();
      const studentId = input.dataset.studentId;
      if (!studentId) continue;

      const val = Math.max(0, Math.min(100, parseInt(raw, 10) || 0));
      input.value = String(val);

      try {
        const res = await fetch('/api/class/' + classId + '/save-hw-percentage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(hwPercentageSaveBody(studentId, val))
        });
        const data = await res.json().catch(function () { return {}; });
        if (!res.ok || !data.success) {
          errors.push(data.error || res.statusText || 'HW save failed');
        } else {
          input.dataset.lastSaved = String(val);
        }
      } catch (e) {
        errors.push(e && e.message ? e.message : 'HW save failed');
      }
    }
  }

  const workers = [];
  for (let i = 0; i < Math.min(concurrency, dirtyInputs.length); i++) {
    workers.push(worker());
  }

  await Promise.all(workers);

  if (errors.length) {
    // Keep the UX friendly: don't fail on the first disconnect if some requests succeeded.
    // Still throw so the UI can re-enable buttons properly.
    throw new Error(errors[0]);
  }
}

function updateRevisionStatus(studentId) {
  if (INSTRUCTOR_MODE !== 'mark') return;
  const sid = String(studentId || '');
  if (!sid) return;
  const esc = typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(sid) : sid;
  let els;
  try {
    els = document.querySelectorAll('.revision-status[data-student-id="' + esc + '"]');
  } catch (e) {
    return;
  }
  let html;
  if (revisionEligible[sid]) {
    html = '<span class="px-2 py-0.5 text-[10px] font-bold rounded-full bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">Can revise</span>';
  } else if (examGradeEligible[sid]) {
    html = '<span class="px-2 py-0.5 text-[10px] font-bold rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200">In-class grade eligible</span>';
  } else {
    html = '<span class="px-2 py-0.5 text-[10px] font-bold rounded-full bg-red-100 dark:bg-red-900/40 text-red-600 dark:text-red-400">Not eligible</span>';
  }
  els.forEach(function (el) {
    el.classList.remove('hidden');
    el.innerHTML = html;
  });
}

function handleGradeCellKeydown(e, cell) {
  const k = e.key.toUpperCase();
  const row = parseInt(cell.dataset.row);
  const col = parseInt(cell.dataset.col);

  const wantsGrade = (e.shiftKey && k === 'M') ? 'MR' : (e.shiftKey && k === 'Q') ? 'Q' : (GRADES.includes(k) ? (GRADE_MAP[k] || k) : null);
  if (wantsGrade) {
    e.preventDefault();
    if (wantsGrade !== 'A' && !studentHasRecordedHwScore(hwScoresMap, cell.dataset.studentId)) {
      showHwGradeLockNotice();
      return;
    }
    updateCellValue(cell, wantsGrade);
    moveFocusForward(row, col, cell);
    return;
  }
  if (e.key === 'Backspace' || e.key === 'Delete') {
    e.preventDefault();
    updateCellValue(cell, null);
  } else if (e.key === 'Tab') {
    e.preventDefault();
    moveFocusForward(row, col, cell);
  } else if (e.key === 'ArrowRight') { e.preventDefault(); moveFocusForward(row, col, cell); }
  else if (e.key === 'ArrowLeft')  { e.preventDefault(); moveFocusBackward(row, col, cell); }
  else if (e.key === 'ArrowDown')  { e.preventDefault(); moveFocusVertical(row, col, 1, cell); }
  else if (e.key === 'ArrowUp')    { e.preventDefault(); moveFocusVertical(row, col, -1, cell); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    const nextRowCells = getVisibleGradeCellsInRow(row + 1, getNavScopeRoot(cell));
    if (nextRowCells.length) focusGradeCell(nextRowCells[0]);
  }
}

document.addEventListener('click', function (e) {
  const cell = e.target.closest('.grade-cell');
  if (cell) cell.focus();
});

document.addEventListener('keydown', function (e) {
  const cell = e.target.closest && e.target.closest('.grade-cell');
  if (!cell) return;
  handleGradeCellKeydown(e, cell);
});

function updateCellValue(cell, newGrade) {
  const key = gradeCellKeyFromCell(cell);
  const prev = grades[key];
  if (prev === newGrade) return;

  const prevFlag = cell.dataset.countsForMastery;
  const prevSerial = resetMasterySerial.get(key);
  grades[key] = newGrade;
  if (isMasteryLetter(prev) && !isMasteryLetter(newGrade)) {
    markMasteryDeparture(key);
  }
  gradeHistory.push({ key, prev, newGrade, prevFlag, prevSerial });
  markUnsavedChanges();

  cell.dataset.actualGrade = newGrade || '';
  applyCountsForMasteryOnEdit(cell, newGrade || '', prev || '');
  if (!newGrade) {
    // Cleared cells discard any prior flag so a future first-entry re-evaluates.
    delete cell.dataset.countsForMastery;
  }
  renderGradeInCell(cell, newGrade || '');

  updateUndoBtn();
}

/** Grade cells in hidden LO columns are skipped so assignment filters stay consistent. */
function isGradeCellVisible(cell) {
  if (!cell || !getActiveGraderRoot().contains(cell)) return false;
  const td = cell.closest('td[data-lo-id]');
  return td && td.style.display !== 'none';
}

function getNavScopeRoot() {
  return getActiveGraderRoot();
}

function isGradeCellNavigable(cell) {
  return isGradeCellVisible(cell);
}

function getVisibleGradeCellsInRow(rowNum, scopeRoot) {
  const root = scopeRoot || getActiveGraderRoot();
  return Array.from(root.querySelectorAll('.grade-cell[data-row="' + rowNum + '"]'))
    .filter(isGradeCellNavigable)
    .sort((a, b) => parseInt(a.dataset.col, 10) - parseInt(b.dataset.col, 10));
}

function focusGradeCell(cell) {
  if (!cell) return;
  cell.focus();
  cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

/** After entering a grade / Tab / Right: next cell in row, or first cell of next student at end of row. */
function moveFocusForward(row, col, fromCell) {
  const scope = getNavScopeRoot(fromCell);
  const cells = getVisibleGradeCellsInRow(row, scope);
  const idx = cells.findIndex(c => parseInt(c.dataset.col, 10) === col);
  if (idx !== -1 && idx < cells.length - 1) {
    focusGradeCell(cells[idx + 1]);
    return;
  }
  if (idx !== -1 && idx === cells.length - 1) {
    const nextRow = getVisibleGradeCellsInRow(row + 1, scope);
    if (nextRow.length) focusGradeCell(nextRow[0]);
    return;
  }
  const nextSameRow = cells.find(c => parseInt(c.dataset.col, 10) > col);
  if (nextSameRow) {
    focusGradeCell(nextSameRow);
    return;
  }
  const nextRow = getVisibleGradeCellsInRow(row + 1, scope);
  if (nextRow.length) focusGradeCell(nextRow[0]);
}

function moveFocusBackward(row, col, fromCell) {
  const scope = getNavScopeRoot(fromCell);
  const cells = getVisibleGradeCellsInRow(row, scope);
  const idx = cells.findIndex(c => parseInt(c.dataset.col, 10) === col);
  if (idx > 0) {
    focusGradeCell(cells[idx - 1]);
    return;
  }
  if (idx === 0) {
    const prevRow = getVisibleGradeCellsInRow(row - 1, scope);
    if (prevRow.length) focusGradeCell(prevRow[prevRow.length - 1]);
    return;
  }
  const prevSame = [...cells].reverse().find(c => parseInt(c.dataset.col, 10) < col);
  if (prevSame) {
    focusGradeCell(prevSame);
    return;
  }
  const prevRow = getVisibleGradeCellsInRow(row - 1, scope);
  if (prevRow.length) focusGradeCell(prevRow[prevRow.length - 1]);
}

function moveFocusVertical(row, col, delta, fromCell) {
  const cells = getVisibleGradeCellsInRow(row + delta, getNavScopeRoot(fromCell));
  if (!cells.length) return;
  let best = cells[0];
  let bestDist = Math.abs(parseInt(best.dataset.col, 10) - col);
  for (const c of cells) {
    const d = Math.abs(parseInt(c.dataset.col, 10) - col);
    if (d < bestDist) {
      best = c;
      bestDist = d;
    }
  }
  focusGradeCell(best);
}

function updateUndoBtn() {
  const btn = document.getElementById('undoBtn');
  const label = document.getElementById('undoLabel');
  const has = gradeHistory.length > 0;
  btn.disabled = !has;
  if (has) {
    btn.className = 'flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors bg-blue-600 text-white hover:bg-blue-700';
    label.textContent = `Undo (${gradeHistory.length})`;
  } else {
    btn.className = 'flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500 cursor-not-allowed';
    label.textContent = 'Undo';
  }
}

document.getElementById('undoBtn').addEventListener('click', () => {
  if (!gradeHistory.length) return;
  const last = gradeHistory.pop();
  const cell = __gradeCellIndex.get(last.key);
  grades[last.key] = last.prev;
  if (last.prevSerial === undefined) {
    resetMasterySerial.delete(last.key);
  } else {
    resetMasterySerial.set(last.key, last.prevSerial);
  }
  if (cell) {
    cell.dataset.actualGrade = last.prev || '';
    if (isMasteryLetter(last.prev) && (last.prevFlag === '0' || last.prevFlag === '1')) {
      cell.dataset.countsForMastery = last.prevFlag;
    } else {
      delete cell.dataset.countsForMastery;
      applyCountsForMasteryOnEdit(cell, last.prev || '', last.newGrade || '');
      if (!last.prev) {
        delete cell.dataset.countsForMastery;
      }
    }
    renderGradeInCell(cell, last.prev || '');
  }
  updateUndoBtn();
  recomputeUnsavedState();
});

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
    e.preventDefault();
    document.getElementById('undoBtn').click();
  }
});

// Index of grade cells keyed by "studentId|loId|assignmentId". Rebuilt after
// student delete and assignment switches.
let __gradeCellIndex = new Map();

function getActiveGraderRoot() {
  return document.getElementById('graderTable') || document;
}

function rebuildGradeCellIndex() {
  const map = new Map();
  const root = getActiveGraderRoot();
  root.querySelectorAll('.grade-cell').forEach(function (cell) {
    const key = gradeCellKeyFromCell(cell);
    if (key && key !== '||') map.set(key, cell);
  });
  __gradeCellIndex = map;
}
rebuildGradeCellIndex();

// String reused for every cleared cell so we set `innerHTML` to the same
// constant. The browser can short-circuit equal innerHTML assignments and we
// avoid generating a fresh string per clear during assignment switches.
const __emptyGradeCellHtml = '<span class="text-slate-200 dark:text-slate-600 text-[12px] select-none">\u2014</span>';

let activeAssignmentTypeFilter = 'mastery_opp';
let assignmentFilterLastValue = '';

function normalizeAssignmentType(raw) {
  const t = String(raw || 'mastery_opp');
  return (t === 'exam' || t === 'project' || t === 'mastery_opp') ? t : 'mastery_opp';
}

function getAssignmentDataList() {
  try {
    return JSON.parse(document.getElementById('assignmentData').textContent) || [];
  } catch (e) {
    return [];
  }
}

function updateAssignmentTypeButtonStyles() {
  document.querySelectorAll('.assignment-type-btn').forEach(function (btn) {
    const on = btn.getAttribute('data-type') === activeAssignmentTypeFilter;
    btn.classList.toggle('bg-blue-600', on);
    btn.classList.toggle('text-white', on);
    btn.classList.toggle('text-slate-600', !on);
    btn.classList.toggle('dark:text-slate-300', !on);
    btn.classList.toggle('hover:bg-slate-100', !on);
    btn.classList.toggle('dark:hover:bg-slate-600', !on);
  });
}

function setAssignmentTypeFilter(type, opts) {
  opts = opts || {};
  const next = normalizeAssignmentType(type);
  if (next === activeAssignmentTypeFilter && !opts.force) return;

  const select = document.getElementById('assignmentFilter');
  if (!opts.skipUnsavedGuard && select && typeof hasUnsavedChanges !== 'undefined' && hasUnsavedChanges) {
    if (!confirm('You have unsaved changes.')) return;
  }

  activeAssignmentTypeFilter = next;
  updateAssignmentTypeButtonStyles();

  let firstVisible = null;
  let visibleCount = 0;
  Array.from(select.options).forEach(function (opt) {
    const optType = normalizeAssignmentType(opt.getAttribute('data-assignment-type'));
    const show = optType === activeAssignmentTypeFilter;
    opt.hidden = !show;
    opt.disabled = !show;
    if (show) {
      visibleCount += 1;
      if (!firstVisible) firstVisible = opt.value;
    }
  });

  const emptyHint = document.getElementById('assignmentTypeEmptyHint');
  if (emptyHint) emptyHint.classList.toggle('hidden', visibleCount > 0);

  if (visibleCount === 0) {
    select.value = '';
    showFlatGraderLayout();
    restoreLoColumnSubtitles();
    assignmentFilterLastValue = '';
    return;
  }

  const currentOpt = select.selectedOptions[0];
  if (!(currentOpt && !currentOpt.hidden && !currentOpt.disabled)) {
    select.value = firstVisible;
  }
  assignmentFilterLastValue = select.value;
  filterByAssignment();
}

function restoreLoColumnSubtitles() {
  document.querySelectorAll('th[data-lo-id]').forEach(function (th) {
    const sub = th.querySelector('.lo-header-sub');
    if (sub) sub.textContent = th.getAttribute('data-lo-subtitle') || th.getAttribute('data-lo') || '';
  });
}

function showFlatGraderLayout() {
  const flat = document.getElementById('graderFlatCard');
  if (flat) flat.classList.remove('hidden');
  rebuildGradeCellIndex();
}

function filterByAssignment() {
  const assignmentId = document.getElementById('assignmentFilter').value;
  const assignmentData = getAssignmentDataList();

  updateActiveHomeworkGroupLabel();

  if (!assignmentId) return;

  const assignment = assignmentData.find(a => a.id === assignmentId);
  if (!assignment) return;
  if (normalizeAssignmentType(assignment.assignment_type) !== activeAssignmentTypeFilter) return;

  Object.keys(grades).forEach(k => delete grades[k]);
  resetMasterySerial.clear();
  gradeHistory.length = 0;
  updateUndoBtn();

  showFlatGraderLayout();
  restoreLoColumnSubtitles();
  const linkedLoIds = new Set((assignment.assignment_objectives || []).map(ao => String(ao.learning_objective_id)));
  // When an assignment has no linked learning objectives, show every class
  // learning objective column so those grades still show here the same way
  // they do on the Reports tab. The scoped filter still applies as soon as
  // any learning objective is linked via Class -> Assignments -> Edit.
  const showAllLos = linkedLoIds.size === 0;

  const loScoped = document.querySelectorAll('#graderTable th[data-lo-id], #graderTable td[data-lo-id]');
  for (let i = 0; i < loScoped.length; i++) {
    const el = loScoped[i];
    const want = (showAllLos || linkedLoIds.has(String(el.dataset.loId))) ? '' : 'none';
    if (el.style.display !== want) el.style.display = want;
  }
  if (window.graderTableSyncScrollMetrics) {
    window.requestAnimationFrame(window.graderTableSyncScrollMetrics);
  }

  __gradeCellIndex.forEach(cell => {
    if (cell.dataset.actualGrade) {
      cell.dataset.actualGrade = '';
      delete cell.dataset.countsForMastery;
      cell.classList.remove('non-counting-grade');
      cell.innerHTML = __emptyGradeCellHtml;
    }
  });

  fetch('/api/class/' + classId + '/assignment/' + assignmentId + '/grades')
    .then(r => r.json())
    .then(data => {
      if (!data.success) return;
      const fetchedGrades = data.grades || {};
      const countsMap = data.counts_for_mastery_map || {};
      for (const key in fetchedGrades) {
        const grade = fetchedGrades[key];
        const compositeKey = compositeGradeKeyFromApi(key, assignmentId);
        grades[compositeKey] = grade;
        const cell = __gradeCellIndex.get(compositeKey);
        if (cell) {
          cell.dataset.actualGrade = grade;
          const flag = Object.prototype.hasOwnProperty.call(countsMap, key)
            ? !!countsMap[key]
            : true;
          if (grade === 'M' || grade === 'MR') {
            cell.dataset.countsForMastery = flag ? '1' : '0';
          } else {
            delete cell.dataset.countsForMastery;
          }
          renderGradeInCell(cell, grade);
        }
      }

      const hwScores = coerceStudentKeyedMap(data.hw_scores || {});
      hwScoresMap = hwScores;
      populateHwScoreInputs(hwScores);
      applyAllHwGradeLocks();
      if (window.graderTableSyncScrollMetrics) {
        window.requestAnimationFrame(window.graderTableSyncScrollMetrics);
      }
      restoreLoColumnSubtitles();
    })
    .catch(e => console.error('Error loading assignment grades:', e));
}

// Prefer Mastery Opportunities on load; fall back to first type that has rows.
(function initSpeedGraderAssignmentTypeFilter() {
  const select = document.getElementById('assignmentFilter');
  const counts = { mastery_opp: 0, exam: 0, project: 0 };
  if (select) {
    Array.from(select.options).forEach(function (opt) {
      const t = normalizeAssignmentType(opt.getAttribute('data-assignment-type'));
      if (counts[t] != null) counts[t] += 1;
    });
  }
  let initial = 'mastery_opp';
  if (counts.mastery_opp === 0) {
    if (counts.exam > 0) initial = 'exam';
    else if (counts.project > 0) initial = 'project';
  }
  setAssignmentTypeFilter(initial, { force: true, skipUnsavedGuard: true });
})();

// Back-forward cache restore: refetch so HW map + grade display match server (avoids stale empty HW → false I).
window.addEventListener('pageshow', function (ev) {
  if (ev.persisted && document.getElementById('assignmentFilter')) {
    filterByAssignment();
  }
});

function clampHwInputEl(input) {
  const raw = String(input.value || '').trim();
  if (raw === '') return null;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n)) return null;
  const clamped = Math.max(0, Math.min(100, n));
  if (String(clamped) !== raw) input.value = String(clamped);
  return clamped;
}

function updateHwInputColor(input) {
  const valNow = clampHwInputEl(input);
  const emptyBg = '#64748b';
  if (valNow === null) {
    input.style.background = emptyBg;
    return;
  }
  input.style.background = hwInputBackground(valNow, false);
}

document.addEventListener('input', function (e) {
    const input = e.target.closest('.hw-pct-input');
    if (!input || !isActiveHwControl(input)) return;
    updateHwInputColor(input);
    const valNow = clampHwInputEl(input);
    if (valNow === null) {
      setHwEligibilityFromState(input.dataset.studentId, null, false);
      setLiveHwScore(input.dataset.studentId, '');
      return;
    }
    setHwEligibilityFromState(input.dataset.studentId, valNow, false);
    setLiveHwScore(input.dataset.studentId, valNow);
    markUnsavedChanges();
  });

document.addEventListener('change', async function (e) {
    const input = e.target.closest('.hw-pct-input');
    if (input && isActiveHwControl(input)) {
      updateHwInputColor(input);
      const raw = String(input.value || '').trim();
      const activeAssignmentId = getActiveAssignmentId();
      if (raw === '' || !activeAssignmentId) return;
      const studentId = input.dataset.studentId;
      const val = clampHwInputEl(input);
      if (val === null) return;
      input.value = val;
      updateHwInputColor(input);
      setHwEligibilityFromState(studentId, val, false);
      try {
        const res = await fetch('/api/class/' + classId + '/save-hw-percentage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(hwPercentageSaveBody(studentId, val))
        });
        const data = await res.json().catch(function () { return {}; });
        if (!res.ok || !data.success) throw new Error(data.error || 'Failed');
        input.dataset.lastSaved = String(val);
        setLiveHwScore(studentId, val);
        recomputeUnsavedState();
      } catch (err) {
        console.error('Error saving HW %:', err);
      }
      return;
    }

    const cb = e.target.closest('.hw-pass-check');
    if (!cb || !isActiveHwControl(cb)) return;
    const studentId = cb.dataset.studentId;
    const hiddenCell = hiddenHwCellForStudent(studentId);
    const inputEl = hiddenCell ? hiddenCell.querySelector('.hw-pct-input') : null;
    const passText = hiddenCell ? hiddenCell.querySelector('.hw-pass-text') : null;
    const label = cb.closest('label') ? cb.closest('label').querySelector('.hw-pass-label') : null;
    if (!inputEl || !label) return;

    if (cb.checked) {
      cb.dataset.prevScore = String(inputEl.value || '').trim();
      const remaining = parseInt(cb.dataset.passes, 10) || 0;
      if (remaining <= 0) {
        alert('No HW passes remaining for this student.');
        cb.checked = false;
        return;
      }
      if (!getActiveAssignmentId()) {
        alert('Select an assignment first.');
        cb.checked = false;
        return;
      }
      try {
        const res = await fetch('/api/class/' + classId + '/use_pass', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ student_id: studentId })
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'Failed');
        cb.dataset.passes = data.passes_remaining;
        label.textContent = data.passes_remaining + ' left';

        const hwRes = await fetch('/api/class/' + classId + '/save-hw-percentage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(hwPercentageSaveBody(studentId, -1))
        });
        const hwData = await hwRes.json().catch(function () { return {}; });
        if (!hwRes.ok || !hwData.success) {
          throw new Error(hwData.error || 'Could not save HW pass');
        }

        if (passText) passText.classList.remove('hidden');
        inputEl.classList.add('hidden');
        inputEl.dataset.lastSaved = '-1';
        syncHiddenHwCellFromScore(studentId, -1);
        markUnsavedChanges();
        promoteNonCountingMasteriesInDom(studentId);
      } catch (err) {
        cb.checked = false;
        alert('Error using HW pass: ' + err.message);
      }
    } else {
      try {
        const res = await fetch('/api/class/' + classId + '/return_pass', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            student_id: studentId,
            assignment_id: getActiveAssignmentId()
          })
        });
        const data = await res.json();
        if (data.success) {
          cb.dataset.passes = data.passes_remaining;
          label.textContent = data.passes_remaining + ' left';
          demotePromotedMasteriesInDom(studentId, data.demoted_lo_ids || []);
        }
      } catch (err) {
        console.error('Error returning pass:', err);
      }
      const restored = String(cb.dataset.prevScore || '').trim();
      const restoredScore = restored === '' ? null : Math.max(0, Math.min(100, parseInt(restored, 10) || 0));
      syncHiddenHwCellFromScore(studentId, restoredScore == null ? '' : restoredScore);
      markUnsavedChanges();
      if (getActiveAssignmentId()) {
        const saveScore = restored === '' ? 0 : Math.max(0, Math.min(100, parseInt(restored, 10) || 0));
        setLiveHwScore(studentId, saveScore);
        fetch('/api/class/' + classId + '/save-hw-percentage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(hwPercentageSaveBody(studentId, saveScore))
        }).then(function (r) { return r.json(); }).catch(function () {});
      }
    }
});

// Save. When `btn` is provided (user click), animate that button. When called
// without a button (autosave), do NOT touch #saveBtn — autosave shows progress
// via the dedicated #autosaveNotice pill near the keyboard-shortcuts panel.
async function performSave(btn) {
  const activeAssignmentId = getActiveAssignmentId();
  const orig = btn ? btn.innerHTML : '';

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<svg class="animate-spin h-4 w-4 text-white" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> Saving...`;
  }

  try {
    await persistAllHwPctInputs();

    const apiGrades = {};
    const resetCells = [];
    const resetSnapshot = [];
    Object.keys(grades).forEach(function (key) {
      const apiKey = apiGradeKeyFromComposite(key);
      if (!apiKey) return;
      apiGrades[apiKey] = grades[key];
      if (isMasteryLetter(grades[key]) && resetMasterySerial.has(key)) {
        resetCells.push(apiKey);
        resetSnapshot.push({ key: key, serial: resetMasterySerial.get(key) });
      }
    });
    const res = await fetch('/api/class/' + classId + '/save-grades', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        grades: apiGrades,
        assignment_id: activeAssignmentId,
        reset_cells: resetCells
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(function () { return {}; });
      const failure = new Error(err.error || res.statusText);
      failure.status = res.status;
      throw failure;
    }
    resetSnapshot.forEach(function (item) {
      if (resetMasterySerial.get(item.key) === item.serial) {
        resetMasterySerial.delete(item.key);
      }
    });
    gradeHistory.length = 0;
    updateUndoBtn();
    recomputeUnsavedState();

    if (btn) {
      btn.classList.replace('bg-blue-600', 'bg-green-600');
      btn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg> Saved!';
      setTimeout(() => {
        btn.classList.replace('bg-green-600', 'bg-blue-600');
        btn.innerHTML = orig;
        btn.disabled = false;
      }, 2000);
    }
  } catch (e) {
    if (btn) {
      alert('Error saving grades: ' + e.message);
      btn.disabled = false;
      btn.innerHTML = orig;
    } else {
      console.error('Autosave error:', e);
    }
  }
}

document.getElementById('saveBtn').addEventListener('click', async function () {
  const btn = this;
  await performSave(btn);
});

// Intercept top-nav tab clicks and offer to save before leaving.
document.querySelectorAll('nav a[href]').forEach(a => {
  const href = a.getAttribute('href') || '';
  if (!href || href.startsWith('#')) return;
  a.addEventListener('click', function (evt) {
    if (!hasUnsavedChanges) return; // allow normal nav
    evt.preventDefault();
    const leave = confirm('You have unsaved changes.');
    if (!leave) return;
    window.location.href = href;
  });
});

// Warn when switching assignments without saving.
const assignmentFilterEl = document.getElementById('assignmentFilter');
if (assignmentFilterEl) {
  assignmentFilterLastValue = assignmentFilterEl.value;
  assignmentFilterEl.addEventListener('change', function (evt) {
    // If the user changes the selection and we have unsaved work, confirm.
    if (!hasUnsavedChanges) {
      assignmentFilterLastValue = assignmentFilterEl.value;
      return;
    }

    if (assignmentFilterEl.value === assignmentFilterLastValue) return;

    evt.preventDefault();
    const leave = confirm('You have unsaved changes.');
    if (!leave) {
      // Stop the inline onchange handler so we don't reload grades under the user's feet.
      evt.stopImmediatePropagation();
      assignmentFilterEl.value = assignmentFilterLastValue;
      return;
    }
    assignmentFilterLastValue = assignmentFilterEl.value;
  }, true);
}

// Autosave every 3 minutes while grades are unsaved, including in a background
// tab. Hiding the tab saves immediately. A save already in flight is left alone.
let autosaveTimer = null;
let autosaveInFlight = false;
async function runAutosave() {
  if (!hasUnsavedChanges) return;
  if (autosaveInFlight) return;
  autosaveInFlight = true;
  const notice = document.getElementById('autosaveNotice');
  try {
    if (notice) notice.classList.remove('hidden');
    await performSave(null);
  } finally {
    autosaveInFlight = false;
    if (notice) notice.classList.add('hidden');
  }
}
function startAutosaveTimer() {
  if (autosaveTimer !== null) return;
  autosaveTimer = window.setInterval(runAutosave, 180000);
}
startAutosaveTimer();
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'hidden') {
    runAutosave();
  }
});

document.getElementById('assignmentTypeSwitcher').addEventListener('click', function (event) {
  var btn = event.target.closest('.assignment-type-btn');
  if (!btn) return;
  setAssignmentTypeFilter(btn.getAttribute('data-type'));
});
document.getElementById('assignmentFilter').addEventListener('change', function () {
  filterByAssignment();
});
