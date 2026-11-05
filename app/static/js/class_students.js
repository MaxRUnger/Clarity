
// `ClarityUI.debounce` lives in ui_helpers.js; fall back to identity so this
// page still works if that script load is blocked.
const __clarityDebounce =
  (window.ClarityUI && typeof window.ClarityUI.debounce === 'function')
    ? window.ClarityUI.debounce
    : function (fn) { return fn; };

// Node list cached after first call. Page reloads on add/remove of students,
// so the cache is rebuilt naturally and never has to be invalidated here.
let __cachedStudentRows = null;
function _studentRows() {
  if (!__cachedStudentRows) {
    __cachedStudentRows = Array.from(document.querySelectorAll('.student-row'));
  }
  return __cachedStudentRows;
}

const _runHandleSearch = __clarityDebounce(function () {
  const q = (document.getElementById('searchInput').value || '').toLowerCase();
  const rows = _studentRows();
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const name = row.dataset.name || '';
    const want = (!q || name.includes(q)) ? '' : 'none';
    if (row.style.display !== want) row.style.display = want;
  }
}, 80);

function handleSearch() {
  _runHandleSearch();
}

document.getElementById('printStudentSheetsBtn').addEventListener('click', function () {
  window.print();
});
document.getElementById('searchInput').addEventListener('input', function () {
  handleSearch();
});
document.querySelectorAll('.student-row').forEach(function (row) {
  row.addEventListener('click', function (event) {
    if (!event.target.closest('button, [data-copy-email-btn]')) {
      window.location.href = this.dataset.studentHref;
    }
  });
  row.addEventListener('keydown', function (event) {
    if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('button,[data-copy-email-btn]')) {
      event.preventDefault();
      window.location.href = this.dataset.studentHref;
    }
  });
});
