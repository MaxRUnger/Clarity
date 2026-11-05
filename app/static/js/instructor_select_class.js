
const CURRENT_MODE = JSON.parse(document.getElementById('instructorModeJson').textContent);

function applyMode(mode) {
  const markBtn = document.getElementById('modeMark');
  const shelbiBtn = document.getElementById('modeShelbi');
  const activeClass = 'bg-blue-600 text-white';
  const inactiveClass = 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600';

  markBtn.className = `px-4 py-2 text-sm font-medium transition-colors ${mode === 'mark' ? activeClass : inactiveClass}`;
  shelbiBtn.className = `px-4 py-2 text-sm font-medium transition-colors ${mode === 'shelbi' ? activeClass : inactiveClass}`;

  document.querySelectorAll('.mark-only').forEach(el => {
    el.style.display = mode === 'shelbi' ? 'none' : '';
  });
}

async function setMode(mode) {
  try {
    const res = await fetch('/api/set-instructor-mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    if (!res.ok) throw new Error('Failed');
    applyMode(mode);
  } catch (e) {
    alert('Error setting mode: ' + e.message);
  }
}

applyMode(CURRENT_MODE);

const createClassFormElement = document.getElementById('createClassFormElement');
if (createClassFormElement) {
  createClassFormElement.addEventListener('submit', () => {
    const submitBtn = document.getElementById('createClassSubmitBtn');
    if (!submitBtn) return;
    submitBtn.disabled = true;
    submitBtn.classList.add('opacity-60', 'cursor-not-allowed');
    submitBtn.textContent = 'Creating...';
  });
}

const copyClassModal = document.getElementById('copyClassModal');
const copyClassForm = document.getElementById('copyClassForm');

function openCopyClassModal(classId, daysFromSource) {
  if (!copyClassModal || !copyClassForm) return;
  copyClassForm.action = `/class/${classId}/copy`;
  const nameInput = document.getElementById('copyClassNameInput');
  const daysSelect = document.getElementById('copyClassDaysSelect');
  const sectionInput = document.getElementById('copySectionNumberInput');
  if (nameInput) nameInput.value = '';
  if (sectionInput) sectionInput.value = '';
  if (daysSelect && daysFromSource) {
    const opt = [...daysSelect.options].find((o) => o.value === daysFromSource);
    if (opt) daysSelect.value = daysFromSource;
  }
  copyClassModal.classList.remove('hidden');
  if (nameInput) nameInput.focus();
}

function closeCopyClassModal() {
  if (copyClassModal) copyClassModal.classList.add('hidden');
}

document.querySelectorAll('.copy-class-btn').forEach((btn) => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const classId = btn.getAttribute('data-class-id');
    const days = btn.getAttribute('data-class-days') || '';
    if (classId) openCopyClassModal(classId, days);
  });
});

const editClassModal = document.getElementById('editClassModal');
const editClassForm = document.getElementById('editClassForm');

function openEditClassModal(data) {
  if (!editClassModal || !editClassForm || !data) return;
  editClassForm.action = '/class/' + data.id + '/update';
  const nameInput = editClassForm.querySelector('[name="name"]');
  const sectionInput = editClassForm.querySelector('[name="section_number"]');
  const yearInput = editClassForm.querySelector('[name="year"]');
  const semesterSelect = editClassForm.querySelector('[name="semester"]');
  const daysSelect = editClassForm.querySelector('[name="days"]');
  const onlineInput = editClassForm.querySelector('[name="is_online"]');
  const convertInput = editClassForm.querySelector('[name="auto_convert_m"]');
  if (nameInput) nameInput.value = data.name || '';
  if (sectionInput) sectionInput.value = data.section_number || '';
  if (yearInput) yearInput.value = data.year || '';
  if (semesterSelect) {
    const semesterMatch = [...semesterSelect.options].some((opt) => opt.value === data.semester);
    semesterSelect.value = semesterMatch ? data.semester : '';
  }
  if (daysSelect) {
    const daysMatch = [...daysSelect.options].some((opt) => opt.value === data.days);
    daysSelect.value = daysMatch ? data.days : '';
  }
  if (onlineInput) onlineInput.checked = !!data.is_online;
  if (convertInput) convertInput.checked = !!data.auto_convert_m;
  editClassModal.classList.remove('hidden');
  if (nameInput) nameInput.focus();
}

function closeEditClassModal() {
  if (editClassModal) editClassModal.classList.add('hidden');
}

document.querySelectorAll('.edit-class-btn').forEach((btn) => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const card = btn.closest('.relative');
    const blob = card ? card.querySelector('.class-edit-data') : null;
    if (!blob) return;
    openEditClassModal(JSON.parse(blob.textContent));
  });
});

document.getElementById('editClassModalCancel')?.addEventListener('click', closeEditClassModal);
editClassModal?.addEventListener('click', (e) => {
  if (e.target === editClassModal) closeEditClassModal();
});
editClassForm?.addEventListener('submit', () => {
  const submitBtn = document.getElementById('editClassSubmitBtn');
  if (!submitBtn) return;
  submitBtn.disabled = true;
  submitBtn.classList.add('opacity-60', 'cursor-not-allowed');
  submitBtn.textContent = 'Saving...';
});

const archiveClassModal = document.getElementById('archiveClassModal');
const archiveNameInput = document.getElementById('archiveClassNameInput');
const archiveSubmitBtn = document.getElementById('archiveClassSubmitBtn');
const archiveCancelBtn = document.getElementById('archiveClassModalCancel');
const archiveError = document.getElementById('archiveClassError');
const archiveNameLabel = document.getElementById('archiveClassNameLabel');
const archiveSlowReply = 'The server took too long or stopped responding. Check Archived Classes before trying again.';
let archiveBusy = false;
let archiveClassId = '';
let archiveClassName = '';

function archiveNameMatches() {
  return !!archiveNameInput && archiveNameInput.value === archiveClassName;
}

function refreshArchiveSubmit() {
  if (!archiveSubmitBtn) return;
  archiveSubmitBtn.disabled = archiveBusy || !archiveNameMatches();
  archiveSubmitBtn.textContent = archiveBusy ? 'Archiving...' : 'Archive class';
}

function showArchiveError(message) {
  if (!archiveError) return;
  archiveError.textContent = message;
  archiveError.classList.remove('hidden');
}

function releaseArchiveSubmit() {
  archiveBusy = false;
  if (archiveNameInput) archiveNameInput.disabled = false;
  if (archiveCancelBtn) archiveCancelBtn.disabled = false;
  refreshArchiveSubmit();
}

function resetArchiveModal() {
  archiveBusy = false;
  archiveClassId = '';
  archiveClassName = '';
  if (archiveNameInput) {
    archiveNameInput.value = '';
    archiveNameInput.disabled = false;
  }
  if (archiveCancelBtn) archiveCancelBtn.disabled = false;
  if (archiveError) {
    archiveError.textContent = '';
    archiveError.classList.add('hidden');
  }
  if (archiveNameLabel) archiveNameLabel.textContent = '';
  refreshArchiveSubmit();
}

function closeArchiveModal() {
  if (archiveBusy || !archiveClassModal) return;
  archiveClassModal.classList.add('hidden');
  resetArchiveModal();
}

function openArchiveModal(classId, className) {
  if (!archiveClassModal) return;
  resetArchiveModal();
  archiveClassId = classId;
  archiveClassName = className;
  if (archiveNameLabel) archiveNameLabel.textContent = className;
  archiveClassModal.classList.remove('hidden');
  if (archiveNameInput) archiveNameInput.focus();
  refreshArchiveSubmit();
}

document.querySelectorAll('.archive-class-btn').forEach((btn) => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const classId = btn.getAttribute('data-class-id') || '';
    const className = btn.getAttribute('data-class-name') || '';
    if (classId) openArchiveModal(classId, className);
  });
});

archiveNameInput?.addEventListener('input', refreshArchiveSubmit);
archiveCancelBtn?.addEventListener('click', closeArchiveModal);
archiveClassModal?.addEventListener('click', (e) => {
  if (e.target === archiveClassModal) closeArchiveModal();
});

archiveSubmitBtn?.addEventListener('click', async () => {
  if (archiveBusy || !archiveNameMatches() || !archiveClassId) return;
  archiveBusy = true;
  if (archiveNameInput) archiveNameInput.disabled = true;
  if (archiveCancelBtn) archiveCancelBtn.disabled = true;
  if (archiveError) archiveError.classList.add('hidden');
  refreshArchiveSubmit();
  let response;
  try {
    response = await fetch('/class/' + encodeURIComponent(archiveClassId) + '/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ confirm_name: archiveNameInput.value }),
    });
  } catch {
    showArchiveError(archiveSlowReply);
    releaseArchiveSubmit();
    return;
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    showArchiveError(archiveSlowReply);
    releaseArchiveSubmit();
    return;
  }
  if (payload && payload.success && payload.redirect) {
    window.location.assign(payload.redirect);
    return;
  }
  showArchiveError((payload && payload.error) || archiveSlowReply);
  releaseArchiveSubmit();
});

document.getElementById('copyClassModalCancel')?.addEventListener('click', closeCopyClassModal);
copyClassModal?.addEventListener('click', (e) => {
  if (e.target === copyClassModal) closeCopyClassModal();
});

copyClassForm?.addEventListener('submit', () => {
  const submitBtn = document.getElementById('copyClassSubmitBtn');
  if (!submitBtn) return;
  submitBtn.disabled = true;
  submitBtn.classList.add('opacity-60', 'cursor-not-allowed');
  submitBtn.textContent = 'Copying...';
});

document.getElementById('modeMark').addEventListener('click', function () { setMode('mark'); });
document.getElementById('modeShelbi').addEventListener('click', function () { setMode('shelbi'); });
document.getElementById('showCreateClassBtn').addEventListener('click', function () {
  document.getElementById('createClassForm').classList.remove('hidden');
  document.getElementById('classList').classList.add('hidden');
  document.getElementById('selectClassHeader').classList.add('hidden');
  document.getElementById('archivedClassesLink').classList.add('hidden');
});
document.getElementById('cancelCreateClassBtn').addEventListener('click', function () {
  document.getElementById('createClassForm').classList.add('hidden');
  document.getElementById('classList').classList.remove('hidden');
  document.getElementById('selectClassHeader').classList.remove('hidden');
  document.getElementById('archivedClassesLink').classList.remove('hidden');
});
document.getElementById('archiveClassForm').addEventListener('submit', function (event) {
  event.preventDefault();
});
document.querySelectorAll('[data-delete-class]').forEach(function (btn) {
  btn.addEventListener('click', function (event) {
    event.stopPropagation();
    if (!window.confirm('⚠️ You are about to permanently delete this class and all related data (students, assignments, grades, objectives). Continue?')) {
      event.preventDefault();
    }
  });
});
