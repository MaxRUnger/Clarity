/**
 * Grade Sheet CSV Upload
 * Step order: Upload -> Map Assignment -> Review & Edit -> Confirm
 * Features: speed-grader keyboard shortcuts, LO comparison, dark mode support
 *
 * Important constraints:
 * - Module-level state (uploadedCsvData) is intentionally
 *   global because the wizard spans multiple step transitions inside the same
 *   page. Resetting state happens only when the user explicitly starts over.
 * - All `.grade-cell` keyboard handling is *delegated* on the table container
 *   (see attachGradeCellHandlers); per-cell bindings are deliberately avoided
 *   because the table is re-rendered when users add/remove rows.
 */

let uploadedCsvData = null;
let csvMatchedAssignmentId = null;
let csvMatchedAssignmentTitle = '';
let csvMatchNoteKind = '';
let classRoster = [];

(function loadClassRoster() {
  var el = document.getElementById('classRosterJson');
  if (!el) return;
  try {
    var parsed = JSON.parse(el.textContent || '[]');
    if (Array.isArray(parsed)) classRoster = parsed;
  } catch (err) {
    classRoster = [];
  }
})();

// Speed-grader state
const gradeHistory = [];
const GRADE_KEYS = ['M', 'R', 'Q', 'P', 'X', 'A'];
const GRADE_MAP  = { 'Q': 'RQ' };  // Q key types RQ
const VALID_GRADES = new Set(['M', 'MR', 'R', 'RQ', 'P', 'X', 'A']);

// LO comparison state
let assignmentLOs   = [];          // vendor_codes from selected assignment
let approvedExtraLOs = new Set();  // extra LOs the user approved
let lastExtraLOList = [];          // from latest runLOComparison (for Select all)

const HW_PREV_HEADER = 'HW prev';

function isHwPrevHeader(lo) {
  return String(lo == null ? '' : lo).trim().toLowerCase() === HW_PREV_HEADER.toLowerCase();
}

function isHomeworkHeader(lo) {
  if (isHwPrevHeader(lo)) return false;
  if (lo == null || typeof lo !== 'string') return false;
  const raw = lo.trim();
  if (!raw) return false;
  const n = raw.toUpperCase().replace(/[%_]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (/^HW[-\d]*$/i.test(raw.trim()) || /^HW\d{1,3}$/i.test(raw.trim())) return true;
  if (['H', 'HW', 'H W', 'HOMEWORK', 'HOME WORK', 'HOME-WORK', 'H WORK', 'HW %', 'HW SCORE', 'HW PCT', 'HW PCTG', 'HWPCT', 'HWPCTG'].includes(n)) return true;
  if (n.startsWith('HOMEWORK') || n.includes('HOMEWORK')) return true;
  if (n.startsWith('HW ')) return true;
  return false;
}

// ============================================================================
// FILE UPLOAD HANDLING
// ============================================================================

function isCsvUpload(file) {
  const name = String((file && file.name) || '').toLowerCase();
  return name.endsWith('.csv');
}

function showUploadParseError(message) {
  const el = document.getElementById('uploadParseError');
  if (el) {
    el.textContent = String(message || 'Could not read that file.');
    el.classList.remove('hidden');
    return;
  }
  alert(message);
}

function clearUploadParseError() {
  const el = document.getElementById('uploadParseError');
  if (!el) return;
  el.textContent = '';
  el.classList.add('hidden');
}

function clearCsvAssignmentMatch() {
  csvMatchedAssignmentId = null;
  csvMatchedAssignmentTitle = '';
  csvMatchNoteKind = '';
  const note = document.getElementById('csvAssignmentMatchNote');
  if (note) {
    note.textContent = '';
    note.classList.add('hidden');
  }
  const line = document.getElementById('csvMatchedAssignmentLine');
  if (line) line.textContent = '';
}

function syncCsvAssignmentMatchNote() {
  const note = document.getElementById('csvAssignmentMatchNote');
  const sel = document.getElementById('assignmentSelect');
  if (!note) return;
  const selected = sel ? String(sel.value || '') : '';
  if (
    csvMatchNoteKind === 'matched' &&
    csvMatchedAssignmentId &&
    selected === String(csvMatchedAssignmentId)
  ) {
    note.textContent = 'Matched from your file: ' + csvMatchedAssignmentTitle;
    note.classList.remove('hidden');
    return;
  }
  if (csvMatchNoteKind === 'ambiguous' && !selected) {
    note.textContent = "Your file's assignment name matches more than one assignment. Choose one.";
    note.classList.remove('hidden');
    return;
  }
  note.textContent = '';
  note.classList.add('hidden');
}

function syncCsvMatchedAssignmentLine() {
  const line = document.getElementById('csvMatchedAssignmentLine');
  const sel = document.getElementById('assignmentSelect');
  if (!line) return;
  if (!sel || !sel.value) {
    line.textContent = '';
    return;
  }
  const chosen = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '';
  if (csvMatchedAssignmentId && String(sel.value) === String(csvMatchedAssignmentId)) {
    line.textContent = 'Assignment: ' + csvMatchedAssignmentTitle + ' (matched from your file)';
  } else {
    line.textContent = 'Assignment: ' + chosen;
  }
}

function resetUploadChooser() {
  const csvInput = document.getElementById('csvInput');
  const fileName = document.getElementById('selectedFileName');
  const nextBtn = document.getElementById('nextBtn1');
  if (csvInput) csvInput.value = '';
  if (fileName) {
    fileName.textContent = '';
    fileName.classList.add('hidden');
  }
  if (nextBtn) {
    nextBtn.disabled = true;
    nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
  }
  uploadedCsvData = null;
  clearCsvAssignmentMatch();
}

function handleFileSelect(input) {
  const file = input.files[0];
  if (!file) return;

  clearUploadParseError();

  if (!isCsvUpload(file)) {
    showUploadParseError('Please select a CSV file');
    resetUploadChooser();
    return;
  }

  const maxSize = 10 * 1024 * 1024;
  if (file.size > maxSize) {
    showUploadParseError('File size must be less than 10MB');
    resetUploadChooser();
    return;
  }

  const fileName = document.getElementById('selectedFileName');
  fileName.textContent = `Selected: ${file.name}`;
  fileName.classList.remove('hidden');

  uploadedCsvData = null;
  clearCsvAssignmentMatch();

  const nextBtn = document.getElementById('nextBtn1');
  nextBtn.disabled = false;
  nextBtn.classList.remove('opacity-50', 'cursor-not-allowed');
}

// Drag-and-drop + form submission wiring
document.addEventListener('DOMContentLoaded', function () {
  const dropZone = document.getElementById('dropZone');
  const dropArea = document.getElementById('dropArea');
  const csvInput = document.getElementById('csvInput');

  if (dropZone) {
    dropZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropArea.classList.add('border-blue-500', 'bg-blue-50');
    });
    dropZone.addEventListener('dragleave', () => {
      dropArea.classList.remove('border-blue-500', 'bg-blue-50');
    });
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropArea.classList.remove('border-blue-500', 'bg-blue-50');
      if (e.dataTransfer.files.length > 0) {
        csvInput.files = e.dataTransfer.files;
        handleFileSelect(csvInput);
      }
    });
  }

  // Form submit handler
  const gradeForm = document.getElementById('uploadForm');
  if (gradeForm) {
    gradeForm.addEventListener('submit', handleFormSubmit);
  }

  // Global Ctrl+Z undo
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
      e.preventDefault();
      undoLastGrade();
    }
  });
});

// ============================================================================
// STEP NAVIGATION  (1 Upload → 2 Map → 3 Review → 4 Confirm)
// ============================================================================

function goToStep(stepNumber) {
  const current = getCurrentStep();

  if (stepNumber > current) {
    if (!validateStep(current)) return;
    // Parse the CSV when leaving step 1 for the first time
    if (current === 1 && !uploadedCsvData) {
      analyzeUpload();
      return;
    }
    // Render the extracted data table when entering step 3
    if (stepNumber === 3 && uploadedCsvData) {
      displayExtractedData(uploadedCsvData);
    }
    // Populate confirm summary when entering step 4
    if (stepNumber === 4) populateConfirmSummary();
  }

  // Show / hide panels
  for (let i = 1; i <= 4; i++) {
    const p = document.getElementById('panel-' + i);
    if (p) p.classList.toggle('hidden', i !== stepNumber);
  }
  updateStepIndicators(stepNumber);
  if (stepNumber === 2) syncCsvAssignmentMatchNote();
  if (stepNumber === 3) syncCsvMatchedAssignmentLine();
}

function getCurrentStep() {
  for (let i = 1; i <= 4; i++) {
    const p = document.getElementById('panel-' + i);
    if (p && !p.classList.contains('hidden')) return i;
  }
  return 1;
}

function validateStep(step) {
  if (step === 1) {
    const csvInput = document.getElementById('csvInput');
    if (!csvInput.files || csvInput.files.length === 0) {
      alert('Please select a CSV file');
      return false;
    }
  }
  if (step === 2) {
    const sel = document.getElementById('assignmentSelect');
    if (!sel || !sel.value) {
      alert('Please select an assignment before continuing.');
      return false;
    }
  }
  return true;
}

function updateStepIndicators(currentStep) {
  const checkSVG = '<svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>';
  const stepIcons = {
    1: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>',
    2: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>',
    3: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"/></svg>',
    4: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>'
  };

  for (let i = 1; i <= 4; i++) {
    const icon = document.getElementById('step-icon-' + i);
    const label = document.getElementById('step-label-' + i);
    const conn  = document.getElementById('connector-' + i);

    if (i < currentStep) {
      icon.className = 'w-10 h-10 rounded-full flex items-center justify-center bg-green-600 text-white transition-all';
      label.className = 'mt-2 text-xs font-semibold text-green-600 dark:text-green-400';
      icon.innerHTML = checkSVG;
    } else if (i === currentStep) {
      icon.className = 'w-10 h-10 rounded-full flex items-center justify-center bg-blue-600 text-white transition-all';
      label.className = 'mt-2 text-xs font-semibold text-blue-600 dark:text-blue-400';
      icon.innerHTML = stepIcons[i];
    } else {
      icon.className = 'w-10 h-10 rounded-full flex items-center justify-center bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500 transition-all';
      label.className = 'mt-2 text-xs font-medium text-slate-400 dark:text-slate-500';
      icon.innerHTML = stepIcons[i];
    }

    if (conn) {
      if (i < currentStep) {
        conn.className = 'h-px w-24 md:w-32 mx-1 mb-5 transition-all bg-green-600';
      } else {
        conn.className = 'h-px w-24 md:w-32 mx-1 mb-5 transition-all bg-slate-200 dark:bg-slate-600';
      }
    }
  }
}

// ============================================================================
// ASSIGNMENT SELECTION  &  LO COMPARISON  (Step 2)
// ============================================================================

function onAssignmentSelected() {
  syncCsvAssignmentMatchNote();
  syncCsvMatchedAssignmentLine();
  const sel = document.getElementById('assignmentSelect');
  if (!sel || !sel.value) return;

  const data = (window.ASSIGNMENTS_DATA || []).find(a => String(a.id) === String(sel.value));
  if (!data) return;

  // Extract vendor_codes from assignment_objectives → learning_objectives
  assignmentLOs = [];
  (data.assignment_objectives || []).forEach(ao => {
    const lo = ao.learning_objectives;
    if (lo && lo.vendor_code) assignmentLOs.push(lo.vendor_code);
  });

  approvedExtraLOs.clear();

  // If extracted data already exists, run comparison immediately
  if (uploadedCsvData) runLOComparison();
}

function continueFromMapStep() {
  if (!validateStep(2)) return;
  goToStep(3);
}

function runLOComparison() {
  const section = document.getElementById('loComparisonSection');
  const content = document.getElementById('loComparisonContent');
  if (!section || !content || !uploadedCsvData) return;

  const fileLearningObjectives = uploadedCsvData.learning_objectives || [];
  const assignSet = new Set(assignmentLOs.map(v => v.toUpperCase()));

  const matched = [];
  const extra   = [];
  fileLearningObjectives.forEach(lo => {
    if (assignSet.has(lo.toUpperCase())) matched.push(lo);
    else extra.push(lo);
  });

  const missing = assignmentLOs.filter(v => !fileLearningObjectives.map(s => s.toUpperCase()).includes(v.toUpperCase()));

  let html = '';

  if (matched.length) {
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Matched</p><div class="flex flex-wrap gap-2 mb-3">';
    matched.forEach(lo => { html += `<span class="lo-matched px-2.5 py-1 rounded-full text-xs font-semibold">${escapeHTML(lo)}</span>`; });
    html += '</div>';
  }
  if (extra.length) {
    lastExtraLOList = extra.slice();
    html += '<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 mb-1">';
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400">Extra (in the file but not in the assignment)</p>';
    html += '<div class="flex items-center gap-2 shrink-0 text-xs">';
    html += '<button type="button" class="font-medium text-amber-600 dark:text-amber-400 hover:underline" onclick="setAllExtraLOs(true)">Select all</button>';
    html += '<span class="text-slate-400" aria-hidden="true">|</span>';
    html += '<button type="button" class="font-medium text-slate-500 dark:text-slate-400 hover:underline" onclick="setAllExtraLOs(false)">Deselect all</button>';
    html += '</div></div>';
    html += '<div class="flex flex-wrap gap-2 mb-3">';
    extra.forEach(lo => {
      const checked = approvedExtraLOs.has(lo) ? 'checked' : '';
      const safe = escapeHTML(lo);
      html += `<label class="lo-extra px-2.5 py-1 rounded-full text-xs font-semibold cursor-pointer flex items-center gap-1.5">
        <input type="checkbox" class="accent-amber-600" ${checked} onchange='toggleExtraLO(${JSON.stringify(lo)}, this.checked)'>
        ${safe}
      </label>`;
    });
    html += '</div>';
  } else {
    lastExtraLOList = [];
  }
  if (missing.length) {
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Missing (in assignment but not in the file)</p><div class="flex flex-wrap gap-2 mb-3">';
    missing.forEach(lo => { html += `<span class="lo-missing px-2.5 py-1 rounded-full text-xs font-semibold">${escapeHTML(lo)}</span>`; });
    html += '</div>';
  }

  if (!matched.length && !extra.length && !missing.length) {
    html = '<p class="text-sm text-slate-500 dark:text-slate-400">No learning objectives to compare yet.</p>';
  }

  content.innerHTML = html;
  section.classList.remove('hidden');
}

function toggleExtraLO(lo, checked) {
  if (checked) approvedExtraLOs.add(lo);
  else approvedExtraLOs.delete(lo);
}

function setAllExtraLOs(checked) {
  if (checked) {
    lastExtraLOList.forEach(function (lo) { approvedExtraLOs.add(lo); });
  } else {
    lastExtraLOList.forEach(function (lo) { approvedExtraLOs.delete(lo); });
  }
  runLOComparison();
}

// ============================================================================
// CSV PARSE
// ============================================================================

async function analyzeUpload() {
  const csvInput = document.getElementById('csvInput');
  const file = csvInput.files[0];
  if (!file || !isCsvUpload(file)) {
    showUploadParseError('Please select a CSV file');
    return;
  }
  await analyzeCsv(file);
}

function csvLoHasAnyGrade(lo) {
  const code = String(lo || '').toUpperCase();
  return (uploadedCsvData.students || []).some(function (s) {
    const grades = (s && s.grades) || {};
    return Object.keys(grades).some(function (k) {
      return String(k).toUpperCase() === code && String(grades[k] || '').trim() !== '';
    });
  });
}

function applyCsvAssignmentMapping(matchedId) {
  const sel = document.getElementById('assignmentSelect');
  if (!sel || !matchedId) return false;
  const ok = Array.from(sel.options).some(function (opt) {
    return String(opt.value) === String(matchedId);
  });
  if (!ok) return false;
  sel.value = String(matchedId);
  onAssignmentSelected();
  const assignSet = new Set(assignmentLOs.map(function (v) { return String(v).toUpperCase(); }));
  (uploadedCsvData.learning_objectives || []).forEach(function (lo) {
    if (assignSet.has(String(lo).toUpperCase())) return;
    if (csvLoHasAnyGrade(lo)) approvedExtraLOs.add(lo);
  });
  return true;
}

async function analyzeCsv(file) {
  const nextBtn = document.getElementById('nextBtn1');
  const origHTML = nextBtn.innerHTML;
  nextBtn.disabled = true;
  nextBtn.innerHTML = '<svg class="animate-spin -ml-1 mr-3 h-5 w-5 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> Parsing CSV';
  clearCsvAssignmentMatch();

  try {
    const formData = new FormData();
    formData.append('file', file);
    const resp = await fetch(
      '/api/class/' + encodeURIComponent(window.CLASS_ID) + '/parse-grade-csv',
      { method: 'POST', body: formData, credentials: 'same-origin' }
    );
    if (!resp.ok) {
      const contentType = resp.headers.get('content-type') || '';
      let message = 'Failed to parse CSV';
      if (contentType.includes('application/json')) {
        const e = await resp.json();
        message = e.error || message;
      }
      throw new Error(message);
    }
    const result = await resp.json();
    if (!result.success) throw new Error(result.error || 'CSV parse failed');
    uploadedCsvData = result.data;
    displayExtractedLOs(uploadedCsvData);
    if (result.assignment_match === 'ambiguous') {
      const sel = document.getElementById('assignmentSelect');
      if (sel) sel.value = '';
      csvMatchNoteKind = 'ambiguous';
      syncCsvAssignmentMatchNote();
      goToStep(2);
    } else if (result.matched_assignment_id) {
      csvMatchedAssignmentId = String(result.matched_assignment_id);
      csvMatchedAssignmentTitle = String(result.matched_assignment_title || '');
      csvMatchNoteKind = 'matched';
      if (applyCsvAssignmentMapping(result.matched_assignment_id)) {
        syncCsvAssignmentMatchNote();
        syncCsvMatchedAssignmentLine();
        goToStep(3);
      } else {
        clearCsvAssignmentMatch();
        goToStep(2);
      }
    } else {
      goToStep(2);
    }
  } catch (err) {
    console.error('CSV parse error:', err);
    showUploadParseError(err.message || 'Could not parse that CSV. Choose a different file to try again.');
    resetUploadChooser();
  } finally {
    nextBtn.innerHTML = origHTML;
    const input = document.getElementById('csvInput');
    const hasFile = input && input.files && input.files.length > 0;
    nextBtn.disabled = !hasFile;
    if (hasFile) {
      nextBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    } else {
      nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
  }
}


// ============================================================================
// DISPLAY EXTRACTED DATA  (Step 3 — Review & Edit)
// ============================================================================

function displayExtractedLOs(data) {
  const loContainer = document.getElementById('extractedLOs');
  if (!loContainer) return;
  const los = Array.isArray(data.learning_objectives) ? data.learning_objectives : [];
  const parts = [];
  parts.push(
    `<p class="text-xs text-slate-500 dark:text-slate-400 mb-2">${escapeHTML('Read from CSV.')}</p>`
  );
  if (data.homework_column) {
    parts.push(
      `<span class="inline-block bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-300 px-3 py-1 rounded-full text-sm font-medium" title="Stored as class homework %, not a learning objective">${escapeHTML(data.homework_column)} (homework %)</span>`
    );
  }
  if (los.length > 0) {
    los.forEach(function (lo) {
      parts.push(`<span class="inline-block bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-300 px-3 py-1 rounded-full text-sm font-medium">${escapeHTML(lo)}</span>`);
    });
  }
  if (parts.length) {
    loContainer.innerHTML = parts.join('');
  } else {
    loContainer.innerHTML = '<p class="text-slate-500 dark:text-slate-400 text-sm">No learning objectives detected.</p>';
  }
}

function displayExtractedData(data) {
  hideImportNameMatchError();
  setImportNameConfirmCount(0);
  setReviewContinueEnabled(true);
  const studentCount = document.getElementById('studentCount');
  if (studentCount && data.students) {
    studentCount.textContent = `${data.students.length} students found`;
  }

  // Show selected assignment badge
  const badge = document.getElementById('selectedAssignmentBadge');
  const sel   = document.getElementById('assignmentSelect');
  if (badge && sel && sel.value) {
    badge.textContent = sel.options[sel.selectedIndex].text;
    badge.classList.remove('hidden');
  }
  syncCsvMatchedAssignmentLine();

  const table = document.getElementById('extractedStudentsTable');
  if (!table || !data.students) return;

  // Move HW-style columns from grades into homework_pct
  (data.students || []).forEach(s => {
    if (!s.grades) return;
    Object.keys(s.grades).forEach(h => {
      if (isHwPrevHeader(h)) {
        delete s.grades[h];
        return;
      }
      if (!isHomeworkHeader(h)) return;
      const raw = s.grades[h];
      if (s.homework_pct == null || String(s.homework_pct).trim() === '') {
        if (raw != null && String(raw).trim() !== '') s.homework_pct = String(raw).replace(/%$/, '').trim();
      }
      delete s.grades[h];
    });
  });

  data.students.sort(function(a, b) {
    return window.StudentNames.compareNames(
      (a && a.name) || '',
      (b && b.name) || ''
    );
  });

  if (Array.isArray(data.learning_objectives)) {
    data.learning_objectives = data.learning_objectives.filter(
      lo => !isHomeworkHeader(lo) && !isHwPrevHeader(lo)
    );
  }

  // Build the set of approved LO codes: assignment-matched + user-approved extras
  const assignSet = new Set(assignmentLOs.map(v => v.toUpperCase()));
  const approvedSet = new Set([...assignSet, ...[...approvedExtraLOs].map(v => v.toUpperCase())]);

  // Filter LO headers: only show columns the user approved
  const allLOs = (data.learning_objectives && data.learning_objectives.length > 0)
    ? data.learning_objectives : [];
  // Only columns that belong to the assignment or were explicitly approved as extras
  const loHeaders = approvedSet.size > 0
    ? allLOs.filter(lo => approvedSet.has(lo.toUpperCase()))
    : [];
  const tableHeaders = loHeaders;

  // Update the LO badges on Step 3 to reflect filtered set
  const loContainer = document.getElementById('extractedLOs');
  if (loContainer) {
    if (loHeaders.length > 0) {
      loContainer.innerHTML = loHeaders
        .map(lo => {
          const isExtra = !assignSet.has(lo.toUpperCase());
          const cls = isExtra
            ? 'inline-block bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300 px-3 py-1 rounded-full text-sm font-medium'
            : 'inline-block bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-300 px-3 py-1 rounded-full text-sm font-medium';
          return `<span class="${cls}">${escapeHTML(lo)}${isExtra ? ' (extra)' : ''}</span>`;
        }).join('');
    } else {
      loContainer.innerHTML = '<p class="text-slate-500 dark:text-slate-400 text-sm">No approved learning objectives to display.</p>';
    }
  }

  const showHomework =
    !!data.homework_column ||
    (data.students || []).some(
      s => s.homework_pct != null && String(s.homework_pct).trim() !== ''
    );
  const hwLabel = data.homework_column || 'Homework %';
  const colOffset = showHomework ? 1 : 0;

  let html = `<table class="w-full border-collapse text-sm">
    <thead><tr class="bg-slate-100 dark:bg-slate-700">
      <th class="border border-slate-300 dark:border-slate-600 px-4 py-2 text-left font-semibold text-slate-900 dark:text-white" style="min-width:240px">Student Name</th>
      ${showHomework
        ? `<th class="border border-slate-300 dark:border-slate-600 px-2 py-2 text-center font-semibold text-slate-900 dark:text-white text-xs bg-emerald-50 dark:bg-emerald-900/20" title="Updates homework % for this assignment's homework group (same as speed grader)">${escapeHTML(hwLabel)}</th>`
        : ''}
      ${tableHeaders.map(lo => `<th class="border border-slate-300 dark:border-slate-600 px-2 py-2 text-center font-semibold text-slate-900 dark:text-white text-xs">${escapeHTML(lo)}</th>`).join('')}
    </tr></thead><tbody>`;

  data.students.forEach((student, rowIdx) => {
    const name = student.name || '';
    const rawHw = (student.homework_pct != null && String(student.homework_pct).trim() !== '')
      ? String(student.homework_pct).replace(/%$/, '').trim()
      : '';
    html += `<tr>
      <td class="border border-slate-300 dark:border-slate-600 px-2 py-1">
        <input type="text" class="name-input" value="${escapeHTML(name)}" data-row="${rowIdx}" oninput="updateExtractedStudentName(${rowIdx}, this.value)">
        <div class="name-match-slot" data-row="${rowIdx}"></div>
      </td>`;

    if (showHomework) {
      const hwAttrs = 'min="0" max="100" step="0.1"';
      html += `<td class="border border-slate-300 dark:border-slate-600 px-1 py-1 text-center">
        <input type="number" class="grade-input-hw w-full max-w-[5rem] mx-auto text-center border border-slate-200 dark:border-slate-600 rounded bg-white dark:bg-slate-800 text-slate-900 dark:text-white" ${hwAttrs} value="${escapeHTML(rawHw)}" placeholder="\u2014" data-row="${rowIdx}" data-col="0" title="0 to 100; same stored value as the speed grader. Re-import updates it." oninput="updateExtractedHomeworkPct(${rowIdx}, this.value)">
      </td>`;
    }

    tableHeaders.forEach((lo, colIdx) => {
      const ac = colIdx + colOffset;
      const raw = (student.grades && student.grades[lo]) ? student.grades[lo] : '';
      const grade = VALID_GRADES.has(String(raw).toUpperCase()) ? String(raw).toUpperCase() : raw;
      html += `<td class="border border-slate-300 dark:border-slate-600 px-1 py-1 text-center">
        <div class="grade-cell" tabindex="0" data-grade="${escapeHTML(grade)}" data-row="${rowIdx}" data-col="${ac}" data-lo="${escapeHTML(lo)}" data-student-index="${rowIdx}">
          ${escapeHTML(grade) || '<span class="text-slate-300 dark:text-slate-600 text-xs select-none">\u2014</span>'}
        </div>
      </td>`;
    });

    html += '</tr>';
  });

  html += '</tbody></table>';
  table.innerHTML = html;

  // Attach speed-grader keyboard handlers to all .grade-cell
  attachGradeCellHandlers();
  paintNameFlags();
}

function escapeHTML(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ============================================================================
// SPEED-GRADER KEYBOARD SHORTCUTS
// ============================================================================

// `__gradeHandlersBound` is the only guard that prevents the delegated
// listeners from being attached multiple times if `attachGradeCellHandlers`
// is called more than once (e.g. on re-render). HW% inputs keep their own
// per-input listeners because they need native focus/blur and validation
// that doesn't fit a delegated keydown model.
let __gradeHandlersBound = false;

function attachGradeCellHandlers() {
  if (__gradeHandlersBound) return;
  const table = document.getElementById('extractedStudentsTable');
  if (!table) return;
  __gradeHandlersBound = true;

  table.addEventListener('click', (e) => {
    const cell = e.target && e.target.closest && e.target.closest('.grade-cell');
    if (!cell || !table.contains(cell)) return;
    cell.focus();
  });

  table.addEventListener('keydown', (e) => {
    const cell = e.target && e.target.closest && e.target.closest('.grade-cell');
    if (!cell || !table.contains(cell)) return;
    const k = e.key.toUpperCase();
    const row = parseInt(cell.dataset.row);
    const col = parseInt(cell.dataset.col);

    if (e.shiftKey && k === 'M') {
      e.preventDefault();
      setCellGrade(cell, 'MR');
      moveFocus(row, col + 1);
    } else if (GRADE_KEYS.includes(k)) {
      e.preventDefault();
      const grade = GRADE_MAP[k] || k;
      setCellGrade(cell, grade);
      moveFocus(row, col + 1);
    } else if (e.key === 'Backspace' || e.key === 'Delete') {
      e.preventDefault();
      setCellGrade(cell, '');
    } else if (e.key === 'Tab') {
      e.preventDefault();
      moveFocus(row, col + (e.shiftKey ? -1 : 1));
    } else if (e.key === 'ArrowRight') { e.preventDefault(); moveFocus(row, col + 1); }
      else if (e.key === 'ArrowLeft')  { e.preventDefault(); moveFocus(row, col - 1); }
      else if (e.key === 'ArrowDown')  { e.preventDefault(); moveFocus(row + 1, col); }
      else if (e.key === 'ArrowUp')    { e.preventDefault(); moveFocus(row - 1, col); }
      else if (e.key === 'Enter')      { e.preventDefault(); moveFocus(row + 1, 0); }
  });
}

function setCellGrade(cell, grade) {
  const rowIdx = parseInt(cell.dataset.studentIndex);
  const lo     = cell.dataset.lo;
  const prev   = cell.dataset.grade || '';

  if (prev === grade) return;

  // Push undo
  gradeHistory.push({ cell, prev });

  // Update data model
  if (uploadedCsvData && uploadedCsvData.students && uploadedCsvData.students[rowIdx]) {
    const s = uploadedCsvData.students[rowIdx];
    if (!s.grades) s.grades = {};
    s.grades[lo] = grade;
  }

  // Update DOM
  cell.dataset.grade = grade;
  cell.innerHTML = grade
    ? escapeHTML(grade)
    : '<span class="text-slate-300 dark:text-slate-600 text-xs select-none">\u2014</span>';
}

function undoLastGrade() {
  if (!gradeHistory.length) return;
  const { cell, prev } = gradeHistory.pop();
  const rowIdx = parseInt(cell.dataset.studentIndex);
  const lo     = cell.dataset.lo;

  if (uploadedCsvData && uploadedCsvData.students && uploadedCsvData.students[rowIdx]) {
    const s = uploadedCsvData.students[rowIdx];
    if (!s.grades) s.grades = {};
    s.grades[lo] = prev;
  }

  cell.dataset.grade = prev;
  cell.innerHTML = prev
    ? escapeHTML(prev)
    : '<span class="text-slate-300 dark:text-slate-600 text-xs select-none">\u2014</span>';
}

function moveFocus(row, col) {
  // Try grade-cell first, then HW %, then other numeric inputs
  let next = document.querySelector(`#extractedStudentsTable .grade-cell[data-row="${row}"][data-col="${col}"]`);
  if (!next) next = document.querySelector(`#extractedStudentsTable .grade-input-hw[data-row="${row}"][data-col="${col}"]`);
  if (!next) return;
  next.focus();
  // `scrollIntoView` triggers a layout flush even when the target is already
  // visible, which is noticeable when speed-grading down a tall table. Skip
  // it whenever the cell's bounding rect is already inside the viewport.
  const rect = next.getBoundingClientRect();
  const viewportH = window.innerHeight || document.documentElement.clientHeight;
  const viewportW = window.innerWidth || document.documentElement.clientWidth;
  const outOfView =
    rect.top < 0 || rect.bottom > viewportH ||
    rect.left < 0 || rect.right > viewportW;
  if (outOfView) {
    next.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
}

// ============================================================================
// DATA UPDATE HELPERS
// ============================================================================

function updateExtractedStudentName(idx, value) {
  if (!uploadedCsvData || !uploadedCsvData.students || !uploadedCsvData.students[idx]) return;
  uploadedCsvData.students[idx].name = value;
  hideImportNameMatchError();
  setReviewContinueEnabled(true);
  paintNameFlags();
}

function updateExtractedHomeworkPct(idx, value) {
  if (!uploadedCsvData || !uploadedCsvData.students || !uploadedCsvData.students[idx]) return;
  const t = (value == null) ? '' : String(value).trim();
  if (t === '') {
    uploadedCsvData.students[idx].homework_pct = null;
    return;
  }
  const n = parseFloat(t);
  if (Number.isFinite(n)) {
    uploadedCsvData.students[idx].homework_pct = String(Math.max(0, Math.min(100, Math.round(n * 10) / 10)));
  } else {
    uploadedCsvData.students[idx].homework_pct = t;
  }
}

// ============================================================================
// CONFIRM SUMMARY  (Step 4)
// ============================================================================

function populateConfirmSummary() {
  const sel = document.getElementById('assignmentSelect');
  const nameBadge = document.getElementById('confirmAssignmentName');
  if (nameBadge && sel && sel.value) {
    nameBadge.textContent = sel.options[sel.selectedIndex].text;
  }

  const summary = document.getElementById('confirmSummary');
  if (!summary || !uploadedCsvData) return;
  summary.innerHTML = '';
}

function buildFilteredStudents() {
  const includeLOs = new Set(assignmentLOs.map(v => v.toUpperCase()));
  approvedExtraLOs.forEach(v => includeLOs.add(v.toUpperCase()));
  const isCsv = (uploadedCsvData.extraction_path || '') === 'csv';
  return (uploadedCsvData.students || []).map(s => {
    const grades = {};
    Object.keys(s.grades || {}).forEach(lo => {
      if (isHomeworkHeader(lo) || isHwPrevHeader(lo)) return;
      if (isCsv || includeLOs.has(lo.toUpperCase())) {
        grades[lo] = s.grades[lo];
      }
    });
    const out = { name: s.name, grades };
    if (s.homework_pct != null && String(s.homework_pct).trim() !== '') {
      out.homework_pct = s.homework_pct;
    } else if (isCsv) {
      out.homework_pct = null;
    }
    return out;
  });
}

function setReviewContinueEnabled(enabled) {
  var btn = document.getElementById('reviewContinueBtn');
  if (!btn) return;
  btn.disabled = !enabled;
  if (enabled) {
    btn.classList.remove('opacity-75', 'cursor-not-allowed');
  } else {
    btn.classList.add('opacity-75', 'cursor-not-allowed');
  }
}

function setImportNameConfirmCount(n) {
  var el = document.getElementById('importNameConfirmCount');
  if (!el) return;
  if (!n) {
    el.textContent = '';
    el.classList.add('hidden');
    return;
  }
  el.textContent = n === 1 ? "1 name won't be imported" : (n + " names won't be imported");
  el.classList.remove('hidden');
}

function hideImportNameMatchError() {
  var el = document.getElementById('importNameMatchError');
  if (el) el.classList.add('hidden');
}

function showImportNameMatchError(message) {
  var el = document.getElementById('importNameMatchError');
  var text = document.getElementById('importNameMatchErrorText');
  var generic = 'Could not check student names. Try again before continuing.';
  if (text) {
    text.textContent = message ? String(message) : generic;
  }
  if (el) el.classList.remove('hidden');
}

function clearNameMatchBanners() {
  document.querySelectorAll('.name-match-slot').forEach(function (slot) {
    slot.innerHTML = '';
  });
}

function computeNameFlags(students, roster) {
  var counts = Object.create(null);
  (roster || []).forEach(function (entry) {
    var key = window.StudentNames.nameKey(entry && entry.full_name);
    if (!key) return;
    counts[key] = (counts[key] || 0) + 1;
  });
  var unmatched = [];
  var ambiguous = [];
  (students || []).forEach(function (student, index) {
    var name = student && student.name != null ? String(student.name) : '';
    var key = window.StudentNames.nameKey(name);
    if (!key) return;
    var count = counts[key] || 0;
    var row = { index: index, name: name, key: key };
    if (count > 1) ambiguous.push(row);
    else if (count === 0) unmatched.push(row);
  });
  return { unmatched: unmatched, ambiguous: ambiguous };
}

function uniqueUnmatchedCount(flags) {
  var seen = Object.create(null);
  var n = 0;
  (flags.unmatched || []).forEach(function (row) {
    if (!row.key || seen[row.key]) return;
    seen[row.key] = true;
    n += 1;
  });
  return n;
}

function ignoredNamesFromFlags(flags) {
  var seen = Object.create(null);
  var names = [];
  (flags.unmatched || []).forEach(function (row) {
    if (!row.key || seen[row.key]) return;
    seen[row.key] = true;
    names.push(row.name);
  });
  return names;
}

function restoreImportButton() {
  var importBtn = document.getElementById('importBtn');
  if (!importBtn) return;
  importBtn.disabled = false;
  importBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg> Import Grades';
  importBtn.classList.remove('opacity-75', 'cursor-not-allowed');
}

function sheetNameAt(index) {
  var students = uploadedCsvData && uploadedCsvData.students;
  var student = students && students[index];
  return student && student.name != null ? String(student.name) : '';
}

function saveAddedStudent(index, nameInput, emailInput, errorEl, saveBtn) {
  var typed = nameInput.value.trim();
  var email = emailInput.value.trim();
  var sheetName = sheetNameAt(index);
  errorEl.textContent = '';
  if (!typed || !email) {
    errorEl.textContent = 'Both email and name are required.';
    return;
  }
  if (window.StudentNames.nameKey(typed) !== window.StudentNames.nameKey(sheetName)) {
    errorEl.textContent = 'That name does not match this sheet row.';
    return;
  }
  if (!window.CLASS_ID) {
    errorEl.textContent = 'Missing class ID.';
    return;
  }
  saveBtn.disabled = true;
  fetch('/class/' + encodeURIComponent(window.CLASS_ID) + '/add_student', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email, name: typed })
  }).then(function (res) {
    return res.json().then(function (data) {
      return { ok: res.ok, data: data || {} };
    }).catch(function () {
      return { ok: false, data: {} };
    });
  }).then(function (result) {
    saveBtn.disabled = false;
    if (result.ok && result.data && result.data.success) {
      classRoster.push({ full_name: typed });
      paintNameFlags();
      return;
    }
    errorEl.textContent = (result.data && result.data.error) ? String(result.data.error) : 'Unable to add student.';
  }).catch(function () {
    saveBtn.disabled = false;
    errorEl.textContent = 'Unable to add student.';
  });
}

function buildUnmatchedNameFlag(row) {
  var wrap = document.createElement('div');
  wrap.className = 'mt-2';

  var note = document.createElement('p');
  note.className = 'text-sm font-medium text-amber-800 dark:text-amber-300';
  note.textContent = "Not in this class, won't be imported";
  wrap.appendChild(note);

  var addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'mt-2 px-3 py-1.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700';
  addBtn.textContent = 'Add student';

  var form = document.createElement('div');
  form.className = 'mt-2 space-y-2 hidden';

  var fieldClass = 'mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-white px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500';

  var nameLabel = document.createElement('label');
  nameLabel.className = 'block text-sm font-medium text-slate-700 dark:text-slate-300';
  nameLabel.textContent = 'Name (Last, First)';
  var nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.value = row.name;
  nameInput.className = fieldClass;
  nameLabel.appendChild(nameInput);

  var emailLabel = document.createElement('label');
  emailLabel.className = 'block text-sm font-medium text-slate-700 dark:text-slate-300';
  emailLabel.textContent = 'Email';
  var emailInput = document.createElement('input');
  emailInput.type = 'text';
  emailInput.inputMode = 'email';
  emailInput.autocomplete = 'off';
  emailInput.className = fieldClass;
  emailLabel.appendChild(emailInput);

  var errorEl = document.createElement('p');
  errorEl.className = 'text-sm text-red-600 dark:text-red-400';

  var actions = document.createElement('div');
  actions.className = 'flex gap-2';
  var saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'px-3 py-1.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700';
  saveBtn.textContent = 'Save';
  var cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'px-3 py-1.5 bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg text-sm font-medium text-slate-900 dark:text-white';
  cancelBtn.textContent = 'Cancel';
  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);

  form.appendChild(nameLabel);
  form.appendChild(emailLabel);
  form.appendChild(errorEl);
  form.appendChild(actions);

  function keepEnterInsideRow(event) {
    if (event.key === 'Enter') event.preventDefault();
  }
  nameInput.addEventListener('keydown', keepEnterInsideRow);
  emailInput.addEventListener('keydown', keepEnterInsideRow);

  addBtn.addEventListener('click', function () {
    addBtn.classList.add('hidden');
    form.classList.remove('hidden');
    nameInput.value = sheetNameAt(row.index);
    emailInput.value = '';
    errorEl.textContent = '';
    nameInput.focus();
  });
  cancelBtn.addEventListener('click', function () {
    form.classList.add('hidden');
    addBtn.classList.remove('hidden');
    emailInput.value = '';
    errorEl.textContent = '';
  });
  saveBtn.addEventListener('click', function () {
    saveAddedStudent(row.index, nameInput, emailInput, errorEl, saveBtn);
  });

  wrap.appendChild(addBtn);
  wrap.appendChild(form);
  return wrap;
}

function paintNameFlags() {
  clearNameMatchBanners();
  var students = (uploadedCsvData && uploadedCsvData.students) || [];
  var flags = computeNameFlags(students, classRoster);
  setImportNameConfirmCount(uniqueUnmatchedCount(flags));
  flags.ambiguous.forEach(function (row) {
    var slot = document.querySelector('.name-match-slot[data-row="' + row.index + '"]');
    if (!slot) return;
    var note = document.createElement('p');
    note.className = 'mt-2 text-sm font-medium text-amber-800 dark:text-amber-300';
    note.textContent = 'Two students in this class are named ' + row.name + '. Rename one before importing.';
    slot.appendChild(note);
  });
  flags.unmatched.forEach(function (row) {
    var slot = document.querySelector('.name-match-slot[data-row="' + row.index + '"]');
    if (!slot) return;
    slot.appendChild(buildUnmatchedNameFlag(row));
  });
}

function continueFromReviewStep() {
  if (!uploadedCsvData || !uploadedCsvData.students) {
    alert('No extracted data available. Please upload a CSV first.');
    return;
  }
  paintNameFlags();
  setReviewContinueEnabled(true);
  goToStep(4);
}

// ============================================================================
// FORM SUBMISSION
// ============================================================================

async function handleFormSubmit(e) {
  e.preventDefault();

  if (!uploadedCsvData || !uploadedCsvData.students) {
    alert('No extracted data available. Please upload a CSV first.');
    return;
  }

  const classId = window.CLASS_ID;
  const sel = document.getElementById('assignmentSelect');
  const assignmentId = sel ? sel.value : null;

  if (!classId) { alert('Missing class ID.'); return; }

  const includeLOs = new Set(assignmentLOs.map(v => v.toUpperCase()));
  approvedExtraLOs.forEach(v => includeLOs.add(v.toUpperCase()));
  const hasHomework = (uploadedCsvData.students || []).some(
    s => s.homework_pct != null && String(s.homework_pct).trim() !== ''
  );
  const isCsv = (uploadedCsvData.extraction_path || '') === 'csv';
  if (!isCsv && includeLOs.size === 0 && !hasHomework) {
    alert(
      'Nothing to import: add learning objectives to this assignment (or approve extra columns in step 2), and/or enter homework % values, then try again.'
    );
    return;
  }
  if (hasHomework && !assignmentId) {
    alert('Select an assignment so homework % can be saved (it is shared for the assignment’s homework group, like the speed grader).');
    return;
  }

  // Disable submit button and show loading state to prevent double-clicks
  const importBtn = document.getElementById('importBtn');
  if (importBtn) {
    importBtn.disabled = true;
    importBtn.innerHTML = '<svg class="animate-spin -ml-1 mr-2 h-5 w-5 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> Importing\u2026';
    importBtn.classList.add('opacity-75', 'cursor-not-allowed');
  }
  const priorSkipBox = document.getElementById('importHwSkipBox');
  const priorSkipList = document.getElementById('importHwSkipWarnings');
  if (priorSkipBox) priorSkipBox.classList.add('hidden');
  if (priorSkipList) priorSkipList.textContent = '';

  // Filter each student's grades to only include the approved LOs (not homework)
  const filteredStudents = buildFilteredStudents();

  const filteredLOs = isCsv
    ? (uploadedCsvData.learning_objectives || []).filter(lo => !isHomeworkHeader(lo) && !isHwPrevHeader(lo))
    : (uploadedCsvData.learning_objectives || []).filter(lo =>
        includeLOs.has(lo.toUpperCase())
      );

  const nameFlags = computeNameFlags(filteredStudents, classRoster);
  const payload = {
    class_id: classId,
    assignment_id: assignmentId,
    students: filteredStudents,
    learning_objectives: filteredLOs,
    ignored_names: ignoredNamesFromFlags(nameFlags)
  };

  try {
    const resp = await fetch('/api/import-grades', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (resp.status === 409 && err && err.error === 'ambiguous_name') {
        restoreImportButton();
        goToStep(3);
        paintNameFlags();
        showImportNameMatchError(
          'Two students in this class are named ' + (err.name || '') + '. Rename one before importing.'
        );
        return;
      }
      if (resp.status === 409 && err && Array.isArray(err.unresolved_names)) {
        restoreImportButton();
        goToStep(3);
        paintNameFlags();
        var listed = err.unresolved_names.filter(Boolean).join(', ');
        showImportNameMatchError(
          listed ? ('Some names need confirmation: ' + listed) : 'Some names need confirmation'
        );
        return;
      }
      if ((resp.status === 409 || resp.status === 400) && err && err.error) {
        restoreImportButton();
        goToStep(3);
        showImportNameMatchError(err.error);
        return;
      }
      throw new Error(err.error || 'Failed to import grades');
    }
    const result = await resp.json();
    if (!result.success) throw new Error(result.error || 'Import failed');

    const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean) : [];
    const boxEl = document.getElementById('importHwSkipBox');
    const listEl = document.getElementById('importHwSkipWarnings');
    const reportsLink = document.getElementById('importHwSkipReportsLink');
    if (listEl) {
      listEl.textContent = '';
    }
    if (warnings.length && boxEl && listEl) {
      warnings.forEach(function (line) {
        const li = document.createElement('li');
        li.textContent = line;
        listEl.appendChild(li);
      });
      if (reportsLink) {
        reportsLink.setAttribute('href', '/class/' + classId + '/reports');
      }
      boxEl.classList.remove('hidden');
      restoreImportButton();
      return;
    }

    window.location.href = '/class/' + classId + '/reports';
  } catch (err) {
    console.error('Import error:', err);
    alert('Error importing grades: ' + (err.message || err));
    restoreImportButton();
  }
}
