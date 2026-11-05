
var loCsvConfigEl = document.getElementById('loCsvConfig');
window.LO_CLASS_ID = loCsvConfigEl ? (loCsvConfigEl.dataset.classId || '') : '';

var loCsvPendingRows = null;

function escapeCsvCell(val) {
  var s = String(val == null ? '' : val);
  if (/[",\r\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}
function csvTextFromRows(rows) {
  return rows.map(function (row) { return row.map(escapeCsvCell).join(','); }).join('\r\n');
}
function triggerCsvDownload(filename, csvText) {
  var blob = new Blob(['\ufeff' + csvText], { type: 'text/csv;charset=utf-8' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}
function clampCsvTemplateRows(raw, def, max) {
  var n = parseInt(String(raw), 10);
  if (!isFinite(n) || n < 1) n = def;
  if (n > max) n = max;
  return n;
}
function downloadLoCsvTemplate() {
  var inp = document.getElementById('loCsvTemplateCount');
  var n = clampCsvTemplateRows(inp && inp.value, 10, 500);
  if (inp) inp.value = String(n);
  var rows = [['title', 'description', 'calculation_int']];
  for (var i = 0; i < n; i++) rows.push(['', '', '']);
  triggerCsvDownload('learning_objectives_import_template.csv', csvTextFromRows(rows));
}

function closeLoCsvPreviewModal() {
  var m = document.getElementById('loCsvPreviewModal');
  var wasOpen = !!(m && !m.classList.contains('hidden'));
  if (m) m.classList.add('hidden');
  if (wasOpen && window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === 'function') {
    window.ClarityUI.unlockPageScroll();
  }
  loCsvPendingRows = null;
  var inp = document.getElementById('loCsvInput');
  if (inp) inp.value = '';
  var w = document.getElementById('loCsvPreviewWarnings');
  if (w) {
    w.classList.add('hidden');
    w.textContent = '';
  }
  var cbtn = document.getElementById('loCsvPreviewConfirm');
  if (cbtn) {
    cbtn.disabled = false;
    cbtn.textContent = 'Import confirmed rows';
  }
}

function renderLoCsvPreviewTable(rows) {
  var tbody = document.getElementById('loCsvPreviewTbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (rows || []).forEach(function (r) {
    var dup = !!r.duplicate;
    var tr = document.createElement('tr');
    if (dup) tr.setAttribute('data-duplicate', '1');
    if (dup) tr.className = 'bg-slate-50/90 dark:bg-slate-800/40';
    var codeTd = document.createElement('td');
    codeTd.className = 'px-2 py-2 align-top break-all max-w-[140px]';
    codeTd.textContent = String(r.vendor_code || '');
    tr.appendChild(codeTd);
    var descTd = document.createElement('td');
    descTd.className = 'px-2 py-2 align-top break-words';
    descTd.textContent = String(r.description || '');
    tr.appendChild(descTd);
    var msTd = document.createElement('td');
    msTd.className = 'px-2 py-2 align-top';
    if (dup) {
      var msSpan = document.createElement('span');
      msSpan.className = 'text-xs text-slate-500 dark:text-slate-400 tabular-nums';
      msSpan.textContent = r.required_ms != null ? String(r.required_ms) : '—';
      msTd.appendChild(msSpan);
    } else {
      var v = r.required_ms != null ? parseInt(r.required_ms, 10) : NaN;
      if (isNaN(v) || v < 1 || v > 5) v = 2;
      var inp = document.createElement('input');
      inp.type = 'number';
      inp.min = '1';
      inp.max = '5';
      inp.step = '1';
      inp.value = String(v);
      inp.className =
        'lo-csv-ms-input w-full max-w-[4.5rem] rounded-md border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 px-2 py-1 text-sm text-center text-slate-900 dark:text-slate-100';
      inp.setAttribute('aria-label', 'Required Ms for ' + String(r.vendor_code || ''));
      msTd.appendChild(inp);
    }
    tr.appendChild(msTd);
    var stTd = document.createElement('td');
    stTd.className = 'px-2 py-2 align-top text-xs font-medium';
    if (dup) {
      stTd.className += ' text-slate-500 dark:text-slate-400';
      stTd.textContent = 'Already in class';
    } else {
      stTd.className += ' text-green-700 dark:text-green-400';
      stTd.textContent = 'Will import';
    }
    tr.appendChild(stTd);
    tbody.appendChild(tr);
  });
}

function openLoCsvPreviewModal(rows, warnings) {
  loCsvPendingRows = rows;
  renderLoCsvPreviewTable(rows);
  var warnEl = document.getElementById('loCsvPreviewWarnings');
  if (warnEl) {
    if (warnings && warnings.length) {
      warnEl.textContent = warnings.join('\n');
      warnEl.classList.remove('hidden');
    } else {
      warnEl.classList.add('hidden');
      warnEl.textContent = '';
    }
  }
  var newCount = (rows || []).filter(function (r) { return !r.duplicate; }).length;
  var cbtn = document.getElementById('loCsvPreviewConfirm');
  if (cbtn) {
    cbtn.disabled = newCount === 0;
    cbtn.title = newCount === 0 ? 'No new objectives to import' : '';
  }
  var modal = document.getElementById('loCsvPreviewModal');
  if (modal && modal.classList.contains('hidden')) {
    modal.classList.remove('hidden');
    if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === 'function') {
      window.ClarityUI.lockPageScroll();
    }
  }
}

async function assignmentsPreviewLoCsv(file) {
  if (!file || !file.name.toLowerCase().endsWith('.csv')) {
    alert('Please choose a .csv file.');
    return;
  }
  var fd = new FormData();
  fd.append('file', file);
  var btn = document.getElementById('loCsvChooseBtn');
  var prevLabel = '';
  if (btn) {
    prevLabel = btn.textContent;
    btn.disabled = true;
    btn.classList.add('opacity-60', 'cursor-not-allowed');
    btn.textContent = 'Reading file…';
  }
  try {
    var r = await fetch(
      '/api/class/' + encodeURIComponent(window.LO_CLASS_ID) + '/preview-learning-objectives',
      { method: 'POST', body: fd, credentials: 'same-origin' }
    );
    var j = await r.json().catch(function () { return {}; });
    if (!j.success) {
      alert(j.error || 'Could not read that CSV.');
      return;
    }
    openLoCsvPreviewModal(j.rows || [], j.warnings || []);
  } catch (e) {
    alert('Preview failed: ' + (e && e.message ? e.message : String(e)));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('opacity-60', 'cursor-not-allowed');
      btn.textContent = prevLabel || 'Choose CSV from computer';
    }
    var inp = document.getElementById('loCsvInput');
    if (inp) inp.value = '';
  }
}

async function assignmentsConfirmLoCsvImport() {
  var tbody = document.getElementById('loCsvPreviewTbody');
  if (!tbody) return;
  var toSend = [];
  tbody.querySelectorAll('tr').forEach(function (tr) {
    if (tr.getAttribute('data-duplicate') === '1') return;
    var msInp = tr.querySelector('input.lo-csv-ms-input');
    if (!msInp) return;
    var cells = tr.cells;
    if (!cells || cells.length < 2) return;
    var vendor_code = String(cells[0].textContent || '').trim();
    if (!vendor_code) return;
    var description = String(cells[1].textContent || '').trim();
    var ms = parseInt(msInp.value, 10);
    if (isNaN(ms) || ms < 1 || ms > 5) ms = 2;
    toSend.push({ vendor_code: vendor_code, description: (description || null), required_ms: ms });
  });
  if (!toSend.length) {
    alert('No new objectives to import — every code in this file already exists in the class.');
    return;
  }
  var cbtn = document.getElementById('loCsvPreviewConfirm');
  if (cbtn) {
    cbtn.disabled = true;
    cbtn.textContent = 'Importing…';
  }
  try {
    var r = await fetch(
      '/api/class/' + encodeURIComponent(window.LO_CLASS_ID) + '/import-learning-objectives',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ rows: toSend })
      }
    );
    var j = await r.json().catch(function () { return {}; });
    if (!j.success) {
      alert(j.error || 'Import failed');
      if (cbtn) {
        cbtn.disabled = false;
        cbtn.textContent = 'Import confirmed rows';
      }
      return;
    }
    var msg = 'Imported ' + (j.inserted || 0) + ' objective(s).';
    if (j.skipped) msg += ' Skipped ' + j.skipped + ' duplicate code(s).';
    alert(msg);
    closeLoCsvPreviewModal();
    location.reload();
  } catch (e) {
    alert('Import failed: ' + (e && e.message ? e.message : String(e)));
    if (cbtn) {
      cbtn.disabled = false;
      cbtn.textContent = 'Import confirmed rows';
    }
  }
}

document.getElementById('loCsvToggleBtn')?.addEventListener('click', function () {
  var p = document.getElementById('loCsvUploadPanel');
  if (!p) return;
  p.classList.remove('hidden');
});

document.getElementById('loCsvPanelCloseBtn')?.addEventListener('click', function () {
  var p = document.getElementById('loCsvUploadPanel');
  if (p) p.classList.add('hidden');
});

document.getElementById('loCsvInput')?.addEventListener('change', function () {
  var f = this.files && this.files[0];
  if (f) assignmentsPreviewLoCsv(f);
});

document.getElementById('loCsvPreviewConfirm')?.addEventListener('click', assignmentsConfirmLoCsvImport);

document.getElementById('loCsvChooseBtn').addEventListener('click', function () {
  document.getElementById('loCsvInput').click();
});
document.getElementById('downloadLoCsvTemplateBtn').addEventListener('click', function () {
  downloadLoCsvTemplate();
});
document.getElementById('loCsvPreviewCancel').addEventListener('click', function () {
  closeLoCsvPreviewModal();
});
