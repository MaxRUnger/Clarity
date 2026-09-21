/**
 * Grade Sheet PDF Upload and Gemini Analysis
 * Step order: Upload -> Map Assignment -> Review & Edit -> Confirm
 * Features: speed-grader keyboard shortcuts, LO comparison, dark mode support
 *
 * Important constraints:
 * - Module-level state (uploadedPDFData, mobilePollInterval) is intentionally
 *   global because the wizard spans multiple step transitions inside the same
 *   page. Resetting state happens only when the user explicitly starts over.
 * - All `.grade-cell` keyboard handling is *delegated* on the table container
 *   (see attachGradeCellHandlers); per-cell bindings are deliberately avoided
 *   because the table is re-rendered when users add/remove rows.
 */

let uploadedPDFData = null;
let mobilePollInterval = null;
let nameResolutions = {};
let lastPreviewNames = null;
let lastPreviewMatches = [];
let lastPreviewNameKeys = [];
let previewFetchGen = 0;

function stopMobileUploadPoll() {
  if (mobilePollInterval) {
    clearInterval(mobilePollInterval);
    mobilePollInterval = null;
  }
}

/** Poll until phone uploads via QR handoff, then attach file to #pdfInput */
function startMobileUploadPoll() {
  if (!window.MOBILE_UPLOAD_TOKEN || !window.CLASS_ID) return;
  stopMobileUploadPoll();
  mobilePollInterval = setInterval(async () => {
    try {
      const r = await fetch(
        `/api/class/${window.CLASS_ID}/mobile-upload-status/${window.MOBILE_UPLOAD_TOKEN}`,
        { credentials: 'same-origin' }
      );
      if (!r.ok) return;
      const j = await r.json();
      if (!j.success || !j.ready) return;
      stopMobileUploadPoll();
      const fr = await fetch(
        `/api/class/${window.CLASS_ID}/mobile-upload-file/${window.MOBILE_UPLOAD_TOKEN}`,
        { credentials: 'same-origin' }
      );
      if (!fr.ok) {
        alert('Could not retrieve the file from your phone. Try scanning the QR code again.');
        return;
      }
      const blob = await fr.blob();
      const name = j.filename || 'photo.jpg';
      const pdfInput = document.getElementById('pdfInput');
      if (!pdfInput) return;
      const dt = new DataTransfer();
      dt.items.add(new File([blob], name, { type: blob.type || 'application/octet-stream' }));
      pdfInput.files = dt.files;
      handleFileSelect(pdfInput);
      const statusEl = document.getElementById('mobileUploadStatus');
      if (statusEl) {
        statusEl.textContent = 'Received from phone: ' + name;
        statusEl.classList.remove('hidden');
      }
    } catch (e) {
      console.error('Mobile upload poll:', e);
    }
  }, 2000);
}

// Speed-grader state
const gradeHistory = [];
const GRADE_KEYS = ['M', 'R', 'Q', 'P', 'X', 'A'];
const GRADE_MAP  = { 'Q': 'RQ' };  // Q key types RQ
const VALID_GRADES = new Set(['M', 'MR', 'R', 'RQ', 'P', 'X', 'A']);

// LO comparison state
let assignmentLOs   = [];          // vendor_codes from selected assignment
let approvedExtraLOs = new Set();  // extra LOs the user approved
let lastExtraLOList = [];          // from latest runLOComparison (for Select all)

function isHomeworkHeader(lo) {
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

function extractionPathLabel(path) {
  if (path === 'pdf_table') return 'Extracted from PDF table (local, fast).';
  if (path === 'pdf_text') return 'Extracted from PDF text layout (local).';
  if (path === 'csv') return 'Read from CSV (no OCR).';
  return 'Extracted with AI (Gemini).';
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

function resetUploadChooser() {
  const pdfInput = document.getElementById('pdfInput');
  const fileName = document.getElementById('selectedFileName');
  const nextBtn = document.getElementById('nextBtn1');
  if (pdfInput) pdfInput.value = '';
  if (fileName) {
    fileName.textContent = '';
    fileName.classList.add('hidden');
  }
  if (nextBtn) {
    nextBtn.disabled = true;
    nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
  }
  uploadedPDFData = null;
}

function handleFileSelect(input) {
  const file = input.files[0];
  if (!file) return;

  clearUploadParseError();

  const allowedTypes = ['application/pdf', 'image/jpeg', 'image/jpg', 'image/png'];
  if (!isCsvUpload(file) && !allowedTypes.includes(file.type)) {
    showUploadParseError('Please select a CSV, PDF, JPG, or PNG file');
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

  uploadedPDFData = null;
  stopMobileUploadPoll();

  const nextBtn = document.getElementById('nextBtn1');
  nextBtn.disabled = false;
  nextBtn.classList.remove('opacity-50', 'cursor-not-allowed');
}

// Drag-and-drop + form submission wiring
document.addEventListener('DOMContentLoaded', function () {
  const dropZone = document.getElementById('dropZone');
  const dropArea = document.getElementById('dropArea');
  const pdfInput = document.getElementById('pdfInput');

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
        pdfInput.files = e.dataTransfer.files;
        handleFileSelect(pdfInput);
      }
    });
  }

  // Form submit handler
  const gradeForm = document.getElementById('uploadForm');
  if (gradeForm) {
    gradeForm.addEventListener('submit', handleFormSubmit);
  }

  startMobileUploadPoll();

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
    // Trigger Gemini analysis when leaving step 1 for the first time
    if (current === 1 && !uploadedPDFData) {
      analyzeUpload();
      return;
    }
    // Render the extracted data table when entering step 3
    if (stepNumber === 3 && uploadedPDFData) {
      displayExtractedData(uploadedPDFData);
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
    const pdfInput = document.getElementById('pdfInput');
    if (!pdfInput.files || pdfInput.files.length === 0) {
      alert('Please select a CSV, PDF, JPG, or PNG file');
      return false;
    }
  }
  if (step === 2) {
    const createMode = document.getElementById('mapModeCreate');
    if (createMode && createMode.checked) {
      const nameEl = document.getElementById('newAssignmentName');
      const hwEl = document.getElementById('newAssignmentHwGroup');
      if (!nameEl || !String(nameEl.value || '').trim()) {
        alert('Enter a name for the new assignment.');
        return false;
      }
      if (!hwEl || !String(hwEl.value || '').trim()) {
        alert('Enter a homework group for the new assignment.');
        return false;
      }
      return true;
    }
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
  if (uploadedPDFData) runLOComparison();
}

function onMapModeChange() {
  const createOn = document.getElementById('mapModeCreate') && document.getElementById('mapModeCreate').checked;
  const fields = document.getElementById('createAssignmentFields');
  const sel = document.getElementById('assignmentSelect');
  if (fields) fields.classList.toggle('hidden', !createOn);
  if (sel) sel.disabled = !!createOn;
}

function prefillCreateAssignmentFromCsv(data) {
  const name = String((data && data.assignment_name) || '').trim();
  const dateVal = String((data && data.date_value) || '').trim();
  const nameEl = document.getElementById('newAssignmentName');
  const hwEl = document.getElementById('newAssignmentHwGroup');
  const createRadio = document.getElementById('mapModeCreate');
  if (nameEl) nameEl.value = name;
  if (hwEl && !String(hwEl.value || '').trim()) hwEl.value = name;
  if (createRadio) {
    createRadio.checked = true;
    onMapModeChange();
  }
  window.__csvDateReturned = /^\d{4}-\d{2}-\d{2}$/.test(dateVal) ? dateVal : '';
}

function classLoIdsForExtractedHeaders() {
  const codes = new Set(
    ((uploadedPDFData && uploadedPDFData.learning_objectives) || []).map(function (c) {
      return String(c || '').toUpperCase();
    })
  );
  return (window.CLASS_LOS || [])
    .filter(function (lo) {
      return codes.has(String(lo.vendor_code || '').toUpperCase());
    })
    .map(function (lo) { return lo.id; })
    .filter(Boolean);
}

function vendorCodeForLoId(loId) {
  const hit = (window.CLASS_LOS || []).find(function (lo) {
    return String(lo.id) === String(loId);
  });
  return hit && hit.vendor_code ? hit.vendor_code : '';
}

async function createAssignmentFromMapStep() {
  const name = String((document.getElementById('newAssignmentName') || {}).value || '').trim();
  const hwGroup = String((document.getElementById('newAssignmentHwGroup') || {}).value || '').trim();
  const typeEl = document.getElementById('newAssignmentType');
  const assignmentType = typeEl ? typeEl.value : 'mastery_opp';
  const selectedLos = classLoIdsForExtractedHeaders();
  const body = {
    name: name,
    homework_group: hwGroup,
    assignment_type: assignmentType,
    selected_los: selectedLos,
  };
  if (window.__csvDateReturned) body.date_returned = window.__csvDateReturned;

  const resp = await fetch('/class/' + encodeURIComponent(window.CLASS_ID) + '/create_assignment', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body)
  });
  const result = await resp.json().catch(function () { return {}; });
  if (!resp.ok || !result.success || !result.id) {
    throw new Error(result.error || 'Could not create assignment');
  }

  const newId = String(result.id);
  const ao = selectedLos.map(function (loId) {
    return {
      learning_objective_id: loId,
      learning_objectives: { vendor_code: vendorCodeForLoId(loId) }
    };
  });
  window.ASSIGNMENTS_DATA = window.ASSIGNMENTS_DATA || [];
  window.ASSIGNMENTS_DATA.push({
    id: newId,
    name: name,
    assignment_type: assignmentType,
    assignment_objectives: ao
  });
  const sel = document.getElementById('assignmentSelect');
  if (sel) {
    const opt = document.createElement('option');
    opt.value = newId;
    opt.textContent = name;
    sel.appendChild(opt);
    sel.disabled = false;
    sel.value = newId;
  }
  const existingRadio = document.getElementById('mapModeExisting');
  if (existingRadio) existingRadio.checked = true;
  onMapModeChange();
  onAssignmentSelected();
  return true;
}

async function continueFromMapStep() {
  if (!validateStep(2)) return;
  const createOn = document.getElementById('mapModeCreate') && document.getElementById('mapModeCreate').checked;
  if (createOn) {
    const name = String((document.getElementById('newAssignmentName') || {}).value || '').trim();
    const hwGroup = String((document.getElementById('newAssignmentHwGroup') || {}).value || '').trim();
    if (!name) {
      alert('Please provide an assignment name.');
      return;
    }
    if (!hwGroup) {
      alert('Please provide a homework group (e.g., Quiz 1, Unit 3, Midterm).');
      return;
    }
  }
  const btn = document.getElementById('nextBtn2');
  const orig = btn ? btn.innerHTML : '';
  try {
    if (createOn) {
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Creating assignment';
      }
      await createAssignmentFromMapStep();
    }
    goToStep(3);
  } catch (err) {
    alert('Error creating assignment: ' + (err.message || err));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

function runLOComparison() {
  const section = document.getElementById('loComparisonSection');
  const content = document.getElementById('loComparisonContent');
  if (!section || !content || !uploadedPDFData) return;

  const scannedLOs = uploadedPDFData.learning_objectives || [];
  const assignSet = new Set(assignmentLOs.map(v => v.toUpperCase()));

  const matched = [];
  const extra   = [];
  scannedLOs.forEach(lo => {
    if (assignSet.has(lo.toUpperCase())) matched.push(lo);
    else extra.push(lo);
  });

  const missing = assignmentLOs.filter(v => !scannedLOs.map(s => s.toUpperCase()).includes(v.toUpperCase()));

  let html = '';

  if (matched.length) {
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Matched</p><div class="flex flex-wrap gap-2 mb-3">';
    matched.forEach(lo => { html += `<span class="lo-matched px-2.5 py-1 rounded-full text-xs font-semibold">${escapeHTML(lo)}</span>`; });
    html += '</div>';
  }
  if (extra.length) {
    lastExtraLOList = extra.slice();
    html += '<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 mb-1">';
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400">Extra (found by Gemini but not in assignment)</p>';
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
    html += '<p class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Missing (in assignment but not scanned)</p><div class="flex flex-wrap gap-2 mb-3">';
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
// PDF / GEMINI ANALYSIS
// ============================================================================

async function analyzeUpload() {
  const pdfInput = document.getElementById('pdfInput');
  const file = pdfInput.files[0];
  if (!file) {
    showUploadParseError('Please select a CSV, PDF, JPG, or PNG file');
    return;
  }
  if (isCsvUpload(file)) {
    await analyzeCsv(file);
    return;
  }
  await analyzePDF();
}

function csvLoHasAnyGrade(lo) {
  const code = String(lo || '').toUpperCase();
  return (uploadedPDFData.students || []).some(function (s) {
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
  const existingRadio = document.getElementById('mapModeExisting');
  if (existingRadio) existingRadio.checked = true;
  onMapModeChange();
  sel.value = String(matchedId);
  onAssignmentSelected();
  const assignSet = new Set(assignmentLOs.map(function (v) { return String(v).toUpperCase(); }));
  (uploadedPDFData.learning_objectives || []).forEach(function (lo) {
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
    uploadedPDFData = result.data;
    displayExtractedLOs(uploadedPDFData);
    if (result.matched_assignment_id && applyCsvAssignmentMapping(result.matched_assignment_id)) {
      goToStep(3);
    } else {
      prefillCreateAssignmentFromCsv(uploadedPDFData);
      goToStep(2);
    }
  } catch (err) {
    console.error('CSV parse error:', err);
    showUploadParseError(err.message || 'Could not parse that CSV. Choose a different file to try again.');
    resetUploadChooser();
  } finally {
    nextBtn.innerHTML = origHTML;
    const input = document.getElementById('pdfInput');
    const hasFile = input && input.files && input.files.length > 0;
    nextBtn.disabled = !hasFile;
    if (hasFile) {
      nextBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    } else {
      nextBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
  }
}

async function analyzePDF() {
  const pdfInput = document.getElementById('pdfInput');
  const file = pdfInput.files[0];
  if (!file) { alert('Please select a PDF file'); return; }

  const nextBtn = document.getElementById('nextBtn1');
  const origHTML = nextBtn.innerHTML;
  nextBtn.disabled = true;
  nextBtn.innerHTML = '<svg class="animate-spin -ml-1 mr-3 h-5 w-5 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> Analyzing with Gemini\u2026';

  try {
    const formData = new FormData();
    formData.append('pdf', file);

    const resp = await fetch('/api/analyze-grade-pdf', { method: 'POST', body: formData });
    if (!resp.ok) {
      const contentType = resp.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        const e = await resp.json();
        throw new Error(e.error || 'Failed to analyze PDF');
      }
      throw new Error(resp.status === 502 || resp.status === 504
        ? 'Request timed out. The AI service may be busy — please try again.'
        : `Server error (${resp.status}). Please try again.`);
    }

    const result = await resp.json();
    if (!result.success) throw new Error(result.error || 'Analysis failed');

    uploadedPDFData = result.data;

    // Pre-populate LO badges on step 3
    displayExtractedLOs(uploadedPDFData);

    // Jump to step 2 (Map Assignment)
    goToStep(2);
  } catch (err) {
    console.error('Gemini error:', err);
    showUploadParseError('Error analyzing PDF: ' + err.message);
  } finally {
    nextBtn.disabled = false;
    nextBtn.innerHTML = origHTML;
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
  const ep = data.extraction_path || 'vision';
  parts.push(
    `<p class="text-xs text-slate-500 dark:text-slate-400 mb-2">${escapeHTML(extractionPathLabel(ep))}</p>`
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
  nameResolutions = {};
  lastPreviewNames = null;
  lastPreviewMatches = [];
  lastPreviewNameKeys = [];
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

  const table = document.getElementById('extractedStudentsTable');
  if (!table || !data.students) return;

  // Legacy: move HW-style columns from grades into homework_pct (older Gemini output)
  (data.students || []).forEach(s => {
    if (!s.grades) return;
    Object.keys(s.grades).forEach(h => {
      if (!isHomeworkHeader(h)) return;
      const raw = s.grades[h];
      if (s.homework_pct == null || String(s.homework_pct).trim() === '') {
        if (raw != null && String(raw).trim() !== '') s.homework_pct = String(raw).replace(/%$/, '').trim();
      }
      delete s.grades[h];
    });
  });

  // Sort preview rows using the same UCA-based heuristic as Python's
  // _student_sort_key_last_name so the upload preview matches the server-side
  // roster order. Intl.Collator with locale 'en-US' applies Unicode Collation
  // Algorithm weights that closely mirror pyuca on reconstructed "Last, First"
  // strings for the common case.
  const _previewCollator = new Intl.Collator('en-US');
  data.students.sort(function(a, b) {
    const ka = _generateSortName((a && a.name) || '');
    const kb = _generateSortName((b && b.name) || '');
    return _previewCollator.compare(ka, kb);
  });

  if (Array.isArray(data.learning_objectives)) {
    data.learning_objectives = data.learning_objectives.filter(
      lo => !isHomeworkHeader(lo)
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
  if (uploadedPDFData && uploadedPDFData.students && uploadedPDFData.students[rowIdx]) {
    const s = uploadedPDFData.students[rowIdx];
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

  if (uploadedPDFData && uploadedPDFData.students && uploadedPDFData.students[rowIdx]) {
    const s = uploadedPDFData.students[rowIdx];
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
  if (!uploadedPDFData || !uploadedPDFData.students || !uploadedPDFData.students[idx]) return;
  uploadedPDFData.students[idx].name = value;
  lastPreviewNames = null;
  lastPreviewMatches = [];
  lastPreviewNameKeys = [];
  clearNameMatchBanners();
  hideImportNameMatchError();
  setImportNameConfirmCount(0);
  setReviewContinueEnabled(true);
}

/**
 * Convert a stored "First Last[...]" student name to "Last[...], First" form.
 *
 * Mirrors Python's _generate_sort_name exactly: the first whitespace-separated
 * token is the first name; everything after it becomes the last-name cluster.
 * This faithfully inverts the Canvas CSV import conversion so Canvas-imported
 * names (including multi-word last names, Von/Van prefixes, hyphenated names,
 * and hyphen-with-space names) regenerate their exact Canvas sort string.
 *
 * Comma-format strings (already "Last, First") and single-token names are
 * returned unchanged.
 */
function _generateSortName(name) {
  const raw = (name || '').trim();
  if (!raw) return '\uffff';
  if (raw.indexOf(',') !== -1) return raw;  // already "Last, First"
  const parts = raw.split(/\s+/);
  if (parts.length === 1) return parts[0];
  return parts.slice(1).join(' ') + ', ' + parts[0];
}

function updateExtractedHomeworkPct(idx, value) {
  if (!uploadedPDFData || !uploadedPDFData.students || !uploadedPDFData.students[idx]) return;
  const t = (value == null) ? '' : String(value).trim();
  if (t === '') {
    uploadedPDFData.students[idx].homework_pct = null;
    return;
  }
  const n = parseFloat(t);
  if (Number.isFinite(n)) {
    uploadedPDFData.students[idx].homework_pct = String(Math.max(0, Math.min(100, Math.round(n * 10) / 10)));
  } else {
    uploadedPDFData.students[idx].homework_pct = t;
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
  if (!summary || !uploadedPDFData) return;
  summary.innerHTML = '';
}

function buildFilteredStudents() {
  const includeLOs = new Set(assignmentLOs.map(v => v.toUpperCase()));
  approvedExtraLOs.forEach(v => includeLOs.add(v.toUpperCase()));
  const isCsv = (uploadedPDFData.extraction_path || '') === 'csv';
  return (uploadedPDFData.students || []).map(s => {
    const grades = {};
    Object.keys(s.grades || {}).forEach(lo => {
      if (isHomeworkHeader(lo)) return;
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

function previewNamesFromReview() {
  return buildFilteredStudents().map(function (s) {
    return s.name == null ? '' : String(s.name);
  });
}

function namesListsEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
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
  el.textContent = n === 1 ? '1 name needs confirmation' : (n + ' names need confirmation');
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

function retryImportNameMatchPreview() {
  lastPreviewNames = null;
  continueFromReviewStep();
}

function clearNameMatchBanners() {
  document.querySelectorAll('.name-match-slot').forEach(function (slot) {
    slot.innerHTML = '';
  });
}

function pruneNameResolutions(matches) {
  var byKey = {};
  (matches || []).forEach(function (match) {
    if (match && match.key) byKey[match.key] = match;
  });
  Object.keys(nameResolutions).forEach(function (key) {
    var match = byKey[key];
    if (!match) {
      delete nameResolutions[key];
      return;
    }
    var res = nameResolutions[key];
    if (!res || res.action !== 'attach') return;
    var ok = false;
    (match.candidates || []).forEach(function (c) {
      if (String(c.profile_id) === String(res.profile_id)) ok = true;
    });
    if (!ok) delete nameResolutions[key];
  });
}

function unresolvedMatchCount(matches) {
  var n = 0;
  (matches || []).forEach(function (match) {
    if (match && match.key && !nameResolutions[match.key]) n += 1;
  });
  return n;
}

function refreshUnresolvedContinueState() {
  var n = unresolvedMatchCount(lastPreviewMatches);
  setImportNameConfirmCount(n);
  setReviewContinueEnabled(n === 0);
}

function buildNameMatchBanner(match, sheetName, key) {
  var wrap = document.createElement('div');
  wrap.className = 'name-match-banner mt-2 p-3 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-700 text-sm';
  wrap.setAttribute('data-match-key', key);

  var heading = document.createElement('p');
  heading.className = 'font-medium text-slate-900 dark:text-white mb-2';
  heading.textContent = 'Is ' + sheetName + ' the same student as one of these, or a new student?';
  wrap.appendChild(heading);

  var groupName = 'name-match-' + key;
  var chosen = nameResolutions[key];
  (match.candidates || []).forEach(function (c) {
    var label = document.createElement('label');
    label.className = 'flex items-center gap-2 mb-1 text-slate-800 dark:text-slate-200 cursor-pointer';
    var radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = groupName;
    radio.value = String(c.profile_id);
    radio.checked = !!(chosen && chosen.action === 'attach' && String(chosen.profile_id) === String(c.profile_id));
    radio.addEventListener('change', function () {
      nameResolutions[key] = { action: 'attach', profile_id: c.profile_id };
      refreshUnresolvedContinueState();
    });
    label.appendChild(radio);
    var span = document.createElement('span');
    span.textContent = c.full_name + ' (' + c.class_name + ')';
    label.appendChild(span);
    wrap.appendChild(label);
  });

  var newLabel = document.createElement('label');
  newLabel.className = 'flex items-center gap-2 text-slate-800 dark:text-slate-200 cursor-pointer';
  var newRadio = document.createElement('input');
  newRadio.type = 'radio';
  newRadio.name = groupName;
  newRadio.value = 'new';
  newRadio.checked = !!(chosen && chosen.action === 'create');
  newRadio.addEventListener('change', function () {
    nameResolutions[key] = { action: 'create' };
    refreshUnresolvedContinueState();
  });
  newLabel.appendChild(newRadio);
  var newSpan = document.createElement('span');
  newSpan.textContent = 'New student';
  newLabel.appendChild(newSpan);
  wrap.appendChild(newLabel);
  return wrap;
}

function applyPreviewMatches(matches, nameKeys, names) {
  clearNameMatchBanners();
  var firstUnresolvedEl = null;
  var usedKeys = {};
  (matches || []).forEach(function (match) {
    var key = match && match.key;
    if (!key || usedKeys[key]) return;
    usedKeys[key] = true;
    var rowIdx = -1;
    for (var i = 0; i < nameKeys.length; i++) {
      if (nameKeys[i] === key) {
        rowIdx = i;
        break;
      }
    }
    if (rowIdx < 0) return;
    var slot = document.querySelector('.name-match-slot[data-row="' + rowIdx + '"]');
    if (!slot) return;
    var sheetName = names[rowIdx] || match.name || '';
    var banner = buildNameMatchBanner(match, sheetName, key);
    slot.appendChild(banner);
    if (!nameResolutions[key] && !firstUnresolvedEl) firstUnresolvedEl = banner;
  });
  var unresolved = unresolvedMatchCount(matches);
  setImportNameConfirmCount(unresolved);
  if (unresolved === 0) {
    setReviewContinueEnabled(true);
    goToStep(4);
    return;
  }
  setReviewContinueEnabled(false);
  if (firstUnresolvedEl && firstUnresolvedEl.scrollIntoView) {
    firstUnresolvedEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

async function continueFromReviewStep() {
  if (!uploadedPDFData || !uploadedPDFData.students) {
    alert('No extracted data available. Please upload a CSV, PDF, or image first.');
    return;
  }
  var classId = window.CLASS_ID;
  if (!classId) { alert('Missing class ID.'); return; }

  var names = previewNamesFromReview();
  if (lastPreviewNames !== null && namesListsEqual(names, lastPreviewNames)) {
    applyPreviewMatches(lastPreviewMatches, lastPreviewNameKeys, names);
    return;
  }

  var gen = ++previewFetchGen;
  setReviewContinueEnabled(false);
  hideImportNameMatchError();

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
  var res;
  var body;
  try {
    res = await fetch('/api/class/' + encodeURIComponent(classId) + '/preview-import-name-matches', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf || ''
      },
      body: JSON.stringify({ names: names })
    });
    body = await res.json().catch(function () { return {}; });
  } catch (e) {
    if (gen !== previewFetchGen) return;
    showImportNameMatchError();
    setReviewContinueEnabled(true);
    return;
  }
  if (gen !== previewFetchGen) return;
  var current = previewNamesFromReview();
  if (!namesListsEqual(current, names)) {
    return;
  }
  if (!res.ok || !body || !body.success) {
    showImportNameMatchError(body && body.error);
    setReviewContinueEnabled(true);
    return;
  }
  if (!Array.isArray(body.name_keys) || body.name_keys.length !== names.length) {
    showImportNameMatchError();
    setReviewContinueEnabled(true);
    return;
  }
  lastPreviewNames = names.slice();
  lastPreviewMatches = body.matches || [];
  lastPreviewNameKeys = body.name_keys.slice();
  pruneNameResolutions(lastPreviewMatches);
  applyPreviewMatches(lastPreviewMatches, lastPreviewNameKeys, names);
}

// ============================================================================
// FORM SUBMISSION
// ============================================================================

async function handleFormSubmit(e) {
  e.preventDefault();

  if (!uploadedPDFData || !uploadedPDFData.students) {
    alert('No extracted data available. Please upload a CSV, PDF, or image first.');
    return;
  }

  const classId = window.CLASS_ID;
  const sel = document.getElementById('assignmentSelect');
  const assignmentId = sel ? sel.value : null;

  if (!classId) { alert('Missing class ID.'); return; }

  const includeLOs = new Set(assignmentLOs.map(v => v.toUpperCase()));
  approvedExtraLOs.forEach(v => includeLOs.add(v.toUpperCase()));
  const hasHomework = (uploadedPDFData.students || []).some(
    s => s.homework_pct != null && String(s.homework_pct).trim() !== ''
  );
  const isCsv = (uploadedPDFData.extraction_path || '') === 'csv';
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
    ? (uploadedPDFData.learning_objectives || []).filter(lo => !isHomeworkHeader(lo))
    : (uploadedPDFData.learning_objectives || []).filter(lo =>
        includeLOs.has(lo.toUpperCase())
      );

  const payload = {
    class_id: classId,
    assignment_id: assignmentId,
    students: filteredStudents,
    learning_objectives: filteredLOs,
    name_resolutions: nameResolutions
  };

  try {
    const resp = await fetch('/api/import-grades', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if ((resp.status === 409 || resp.status === 400) && err && err.error) {
        lastPreviewNames = null;
        if (importBtn) {
          importBtn.disabled = false;
          importBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg> Import Grades';
          importBtn.classList.remove('opacity-75', 'cursor-not-allowed');
        }
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
      if (importBtn) {
        importBtn.disabled = false;
        importBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg> Import Grades';
        importBtn.classList.remove('opacity-75', 'cursor-not-allowed');
      }
      return;
    }

    window.location.href = '/class/' + classId + '/reports';
  } catch (err) {
    console.error('Import error:', err);
    alert('Error importing grades: ' + (err.message || err));
    // Re-enable button so they can retry
    if (importBtn) {
      importBtn.disabled = false;
      importBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg> Import Grades';
      importBtn.classList.remove('opacity-75', 'cursor-not-allowed');
    }
  }
}
