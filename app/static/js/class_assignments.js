
const CLASS_ID = JSON.parse(document.getElementById('classIdJson').textContent);

function addDays(dateStr, days) {
  const d = new Date(dateStr + 'T00:00:00');
  d.setDate(d.getDate() + days);
  return d.toISOString().split('T')[0];
}

function clearAssignmentFormError() {
  const el = document.getElementById('assignmentFormError');
  if (!el) return;
  el.classList.add('hidden');
  el.textContent = '';
}

function showAssignmentFormError(msg) {
  const el = document.getElementById('assignmentFormError');
  if (!el) {
    alert(msg);
    return;
  }
  el.textContent = msg;
  el.classList.remove('hidden');
}

function openCreateModal() {
  closeModal();
  const today = new Date().toISOString().split('T')[0];
  document.getElementById('dateReturnedInput').value = today;
  document.getElementById('revisionDueInput').value = addDays(today, 7);
  document.getElementById('assignmentTypeSelect').value = 'mastery_opp';
  document.getElementById('addAssignmentModal').classList.remove('hidden');
}

document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('dateReturnedInput').addEventListener('change', function() {
    if (this.value) {
      document.getElementById('revisionDueInput').value = addDays(this.value, 7);
    }
  });
});

/** Keep the "Existing homework groups" checkboxes in sync with whatever is
 * currently typed in the text input (exact match, case/whitespace-insensitive). */
function syncHwGroupSuggestionChecks() {
  const input = document.getElementById('hwGroupInput');
  if (!input) return;
  const current = input.value.trim().toLowerCase();
  document.querySelectorAll('.hw-group-suggestion-checkbox').forEach(function (cb) {
    cb.checked = current !== '' && cb.value.trim().toLowerCase() === current;
  });
}

function setHwGroupValue(hwGroup) {
  document.getElementById('hwGroupInput').value = String(hwGroup || '').trim();
  syncHwGroupSuggestionChecks();
}

document.addEventListener('DOMContentLoaded', function () {
  const hwGroupInput = document.getElementById('hwGroupInput');
  if (hwGroupInput) {
    hwGroupInput.addEventListener('input', syncHwGroupSuggestionChecks);
  }
  document.querySelectorAll('.hw-group-suggestion-checkbox').forEach(function (cb) {
    cb.addEventListener('change', function () {
      if (cb.checked) {
        // Only one homework group applies per assignment: picking a suggestion
        // fills the field and deselects any other checked suggestion.
        setHwGroupValue(cb.value);
      } else if (hwGroupInput && hwGroupInput.value.trim().toLowerCase() === cb.value.trim().toLowerCase()) {
        hwGroupInput.value = '';
      }
      syncHwGroupSuggestionChecks();
    });
  });
});

function closeModal() {
  document.getElementById('addAssignmentModal').classList.add('hidden');
  document.getElementById('modalTitle').textContent = 'Add Assignment';
  document.getElementById('submitBtn').textContent = 'Save Assignment';
  document.getElementById('assignmentId').value = '';
  document.getElementById('assignmentForm').reset();
  document.querySelectorAll('.lo-checkbox').forEach(cb => cb.checked = false);
  document.getElementById('assignmentTypeSelect').value = 'mastery_opp';
  syncHwGroupSuggestionChecks();
  clearAssignmentFormError();
}

function createLO() {
  document.getElementById('hiddenLoCode').value = document.getElementById('loCode').value;
  document.getElementById('hiddenLoDescription').value = document.getElementById('loDescription').value;
  document.getElementById('hiddenLoRequiredMs').value = document.getElementById('loRequiredMs').value;
  document.getElementById('loForm').submit();
}

function editAssignment(assignment) {
  if (!assignment || typeof assignment !== 'object') {
    return;
  }

  const {
    id,
    name,
    homework_group: hwGroup,
    date_returned: dateReturned,
    revision_due: revisionDue,
    assignment_type: assignmentType,
    assignment_objectives: assignmentObjectives
  } = assignment;

  const selectedLOs = Array.isArray(assignmentObjectives)
    ? assignmentObjectives
        .map(ao => ao.learning_objective_id || (ao.learning_objective && ao.learning_objective.id) || ao.id)
        .filter(Boolean)
    : [];

  document.getElementById('modalTitle').textContent = 'Edit Assignment';
  document.getElementById('submitBtn').textContent = 'Update Assignment';
  document.getElementById('assignmentId').value = id || '';
  document.getElementById('assignmentNameInput').value = name || '';
  setHwGroupValue(hwGroup || '');
  document.getElementById('dateReturnedInput').value = dateReturned || '';
  document.getElementById('revisionDueInput').value = revisionDue || (dateReturned ? addDays(dateReturned, 7) : '');

  document.querySelectorAll('.lo-checkbox').forEach(cb => cb.checked = false);

  const allowedTypes = { mastery_opp: true, exam: true, project: true };
  document.getElementById('assignmentTypeSelect').value =
    allowedTypes[assignmentType] ? assignmentType : 'mastery_opp';

  selectedLOs.forEach(function (loId) {
    document.querySelectorAll('.standard-lo-checkbox[data-lo-id="' + loId + '"]').forEach(function (el) {
      el.checked = true;
    });
  });

  clearAssignmentFormError();

  const modal = document.getElementById('addAssignmentModal');
  if (modal) {
    modal.classList.remove('hidden');
  }
}

function initAssignmentEditButtons() {
  document.querySelectorAll('.edit-assignment').forEach(btn => {
    btn.addEventListener('click', () => {
      try {
        const raw = btn.getAttribute('data-assignment');
        const assignment = raw ? JSON.parse(raw) : null;
        editAssignment(assignment);
      } catch (e) {
        console.error('Failed to parse assignment payload:', e);
      }
    });
  });
}

document.addEventListener('DOMContentLoaded', function () {
  initAssignmentEditButtons();
});

function saveAssignment() {
  const assignmentId = document.getElementById('assignmentId').value;
  const name = document.getElementById('assignmentNameInput').value.trim();
  const dateReturned = document.getElementById('dateReturnedInput').value;
  const revisionDue = document.getElementById('revisionDueInput').value;

  if (!name) {
    showAssignmentFormError('Please provide an assignment name.');
    return;
  }

  const hwGroup = document.getElementById('hwGroupInput').value.trim();
  if (!hwGroup) {
    showAssignmentFormError('Please provide a homework group (e.g., Quiz 1, Unit 3, Midterm).');
    return;
  }

  clearAssignmentFormError();

  const assignmentType = document.getElementById('assignmentTypeSelect').value || 'mastery_opp';
  const selectedLOs = Array.from(new Set(
    Array.from(document.querySelectorAll('.standard-lo-checkbox:checked')).map(function (cb) {
      return cb.value;
    })
  ));

  const data = {
    name: name,
    homework_group: hwGroup,
    date_returned: dateReturned || null,
    revision_due: revisionDue || null,
    selected_los: selectedLOs,
    assignment_type: assignmentType
  };

  const url = assignmentId
    ? `/class/${CLASS_ID}/assignments/${assignmentId}/update`
    : `/class/${CLASS_ID}/create_assignment`;
  const method = assignmentId ? 'PUT' : 'POST';

  function postSave() {
  fetch(url, {
    method: method,
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(data)
  })
  .then(async response => {
    const body = await response.text();
    let json;
    try {
      json = JSON.parse(body);
    } catch {
      throw new Error(`Unexpected response: ${body}`);
    }
    return json;
  })
  .then(data => {
    if (data.success) {
      location.reload();
    } else {
      showAssignmentFormError('Error saving assignment: ' + (data.error || JSON.stringify(data)));
    }
  })
  .catch(error => {
    console.error('Assignment save failed', error);
    showAssignmentFormError('Error saving assignment: ' + error.message);
  });
  }

  if (!assignmentId) {
    postSave();
    return;
  }

  const params = selectedLOs.map(function (loId) {
    return 'selected_lo=' + encodeURIComponent(loId);
  }).join('&');
  fetch(
    '/api/class/' + encodeURIComponent(CLASS_ID)
      + '/assignments/' + encodeURIComponent(assignmentId)
      + '/detach-grade-count' + (params ? '?' + params : ''),
    { credentials: 'same-origin' }
  )
    .then(function (response) { return response.json(); })
    .then(function (payload) {
      const gradeRows = payload && payload.success ? Number(payload.grade_rows || 0) : NaN;
      if (Number.isNaN(gradeRows)) {
        showAssignmentFormError('Could not check grades for objectives that are not selected.');
        return;
      }
      if (gradeRows > 0) {
        const noun = gradeRows === 1 ? 'grade row' : 'grade rows';
        const ok = confirm(
          'This deletes ' + gradeRows + ' ' + noun
            + ' on this assignment for objectives that are not selected. Grades on other assignments stay. Continue?'
        );
        if (!ok) return;
      }
      postSave();
    })
    .catch(function () {
      showAssignmentFormError('Could not check grades for objectives that are not selected.');
    });
}

function downloadBlankCsv(assignmentId) {
  const url = '/class/' + encodeURIComponent(CLASS_ID)
    + '/assignments/' + encodeURIComponent(assignmentId)
    + '/export-blank-csv';
  const warn = 'This assignment already has grades recorded. The blank template will clear them if re-uploaded without filling in the grade columns. Continue?';

  fetch('/api/class/' + encodeURIComponent(CLASS_ID) + '/assignment/' + encodeURIComponent(assignmentId) + '/grades', {
    credentials: 'same-origin'
  })
    .then(function (r) { return r.json().catch(function () { return {}; }); })
    .then(function (data) {
      const hasGrades = !!(data && data.success && data.has_assignment_grades);
      if (hasGrades && !confirm(warn)) return;
      window.location.href = url;
    })
    .catch(function () {
      if (!confirm(warn)) return;
      window.location.href = url;
    });
}

function deleteAssignment(id, name) {
  if (confirm(`Are you sure you want to delete "${name}"? This will also remove all associated grades.`)) {
    fetch(`/class/${CLASS_ID}/delete_assignment/${id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      }
    })
    .then(response => response.json())
    .then(data => {
      if (data.success) {
        location.reload();
      } else {
        alert('Error deleting assignment: ' + data.error);
      }
    })
    .catch(error => {
      alert('Error: ' + error);
    });
  }
}

var editLoCurrentId = null;

function openEditLoFromRow(row) {
  if (!row) return;
  openEditLoModal({
    id: row.getAttribute('data-lo-id'),
    vendor_code: row.getAttribute('data-lo-code') || '',
    description: row.getAttribute('data-lo-description') || '',
    required_ms: parseInt(row.getAttribute('data-lo-required') || '2', 10) || 2
  });
}

function growLoDescriptionField(el) {
  if (!el || el.tagName !== 'TEXTAREA') return;
  el.style.height = 'auto';
  var max = parseInt(el.getAttribute('data-max-height') || '420', 10);
  var h = Math.min(el.scrollHeight, max);
  el.style.height = h + 'px';
  el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden';
}

function wireLoDescriptionAutoGrow() {
  ['loDescription', 'editLoDescription'].forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    var grow = function () { growLoDescriptionField(el); };
    el.addEventListener('input', grow);
    el.addEventListener('focus', grow);
    window.addEventListener('resize', grow);
    grow();
  });
}

function openEditLoModal(lo) {
  if (!lo || !lo.id) return;
  editLoCurrentId = String(lo.id);
  var c = document.getElementById('editLoCode');
  var d = document.getElementById('editLoDescription');
  var r = document.getElementById('editLoRequiredMs');
  if (c) c.value = lo.vendor_code != null ? String(lo.vendor_code) : '';
  if (d) d.value = lo.description != null ? String(lo.description) : '';
  if (r) r.value = lo.required_ms != null ? String(lo.required_ms) : '2';
  var m = document.getElementById('editLoModal');
  if (m) m.classList.remove('hidden');
  requestAnimationFrame(function () { growLoDescriptionField(d); });
}

function closeEditLoModal() {
  editLoCurrentId = null;
  var m = document.getElementById('editLoModal');
  if (m) m.classList.add('hidden');
}

function saveEditLo() {
  if (!editLoCurrentId) return;
  var vendorCode = document.getElementById('editLoCode') ? document.getElementById('editLoCode').value.trim() : '';
  if (!vendorCode) {
    alert('Objective code is required.');
    return;
  }
  var body = {
    vendor_code: vendorCode,
    description: document.getElementById('editLoDescription') ? document.getElementById('editLoDescription').value.trim() : '',
    required_ms: parseInt(document.getElementById('editLoRequiredMs') && document.getElementById('editLoRequiredMs').value, 10) || 2
  };
  var btn = document.getElementById('editLoSaveBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Saving…';
  }
  fetch('/api/class/' + encodeURIComponent(CLASS_ID) + '/update-lo/' + encodeURIComponent(editLoCurrentId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body)
  })
    .then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, data: data };
      });
    })
    .then(function (result) {
      if (!result.ok || !result.data.success) {
        alert(result.data.error || 'Could not save changes.');
        return;
      }
      location.reload();
    })
    .catch(function () {
      alert('Could not save changes.');
    })
    .finally(function () {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Save changes';
      }
    });
}

function deleteLO(id, name) {
  if (!id) return;
  var label = name != null && String(name).length ? String(name) : 'this objective';
  if (confirm('Are you sure you want to delete "' + label + '"? This will also remove all associated grades.')) {
    fetch('/class/' + CLASS_ID + '/delete_lo/' + encodeURIComponent(id), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
      body: '{}'
    })
    .then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, data: data };
      });
    })
    .then(function (result) {
      if (!result.ok || !result.data.success) {
        alert('Error deleting learning objective: ' + (result.data.error || 'Unknown error'));
        return;
      }
      location.reload();
    })
    .catch(function (error) {
      alert('Error: ' + error);
    });
  }
}

function toggleAssignmentsExistingLosSection(btn) {
  var wrap = document.getElementById('assignmentsExistingLosListWrapper');
  if (!wrap || !btn) return;
  var chev = btn.querySelector('.chevron');
  wrap.classList.toggle('hidden');
  if (chev) {
    chev.classList.toggle('rotate-180', wrap.classList.contains('hidden'));
  }
  btn.setAttribute('aria-expanded', wrap.classList.contains('hidden') ? 'false' : 'true');
}

document.addEventListener('DOMContentLoaded', function () {
  var list = document.getElementById('assignmentsExistingLosList');
  if (list) {
    list.addEventListener('click', function (e) {
      var row = e.target.closest('.lo-row');
      if (!row) return;
      if (e.target.closest('.lo-delete-btn')) {
        var id = row.getAttribute('data-lo-id');
        var disp = row.querySelector('.lo-row-display');
        var nm = disp ? disp.textContent : '';
        deleteLO(id, nm);
        return;
      }
      if (e.target.closest('.lo-edit-btn')) {
        openEditLoFromRow(row);
      }
    });
  }
  wireLoDescriptionAutoGrow();
});



document.getElementById('addAssignmentBtn').addEventListener('click', function () {
  openCreateModal();
});
document.querySelectorAll('[data-download-csv]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    downloadBlankCsv(this.getAttribute('data-assignment-id'));
  });
});
document.querySelectorAll('[data-delete-assignment]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    deleteAssignment(this.getAttribute('data-assignment-id'), this.getAttribute('data-assignment-name'));
  });
});
document.getElementById('createLOBtn').addEventListener('click', function () {
  createLO();
});
document.getElementById('existingLosToggleBtn').addEventListener('click', function () {
  toggleAssignmentsExistingLosSection(this);
});
document.getElementById('editLoModal').addEventListener('click', function (event) {
  if (event.target === this) closeEditLoModal();
});
document.getElementById('editLoModalPanel').addEventListener('click', function (event) {
  event.stopPropagation();
});
document.getElementById('editLoCloseBtn').addEventListener('click', function () {
  closeEditLoModal();
});
document.getElementById('editLoCancelBtn').addEventListener('click', function () {
  closeEditLoModal();
});
document.getElementById('editLoSaveBtn').addEventListener('click', function () {
  saveEditLo();
});
document.getElementById('addAssignmentModal').addEventListener('click', function (event) {
  if (event.target === this) this.classList.add('hidden');
});
document.getElementById('addAssignmentModalPanel').addEventListener('click', function (event) {
  event.stopPropagation();
});
document.getElementById('addAssignmentCloseBtn').addEventListener('click', function () {
  closeModal();
});
document.getElementById('addAssignmentCancelBtn').addEventListener('click', function () {
  closeModal();
});
document.getElementById('submitBtn').addEventListener('click', function () {
  saveAssignment();
});
