const CLASS_ID = JSON.parse(document.getElementById('classIdJson').textContent);

    (function () {
      var assignments = JSON.parse(document.getElementById('progressAssignmentsJson').textContent || '[]');
      var row = document.getElementById('progressAssignmentRow');
      var activeId = assignments.length ? String(assignments[0].id) : '';
      var activeClass = 'shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-600 text-white';
      var idleClass = 'shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300';

      function renderAssignments() {
        row.innerHTML = '';
        if (!assignments.length) {
          row.innerHTML = '<p class="text-sm text-slate-500 dark:text-slate-400">No assignments yet.</p>';
          return;
        }
        assignments.forEach(function (assignment) {
          var id = String(assignment.id);
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = assignment.name || 'Assignment';
          btn.className = id === activeId ? activeClass : idleClass;
          btn.addEventListener('click', function () {
            activeId = id;
            renderAssignments();
            if (typeof window.renderProgressStudents === 'function') window.renderProgressStudents(activeId);
          });
          row.appendChild(btn);
        });
      }

      window.getActiveProgressAssignmentId = function () { return activeId; };
      renderAssignments();
    })();
    
    (function () {
      var students = JSON.parse(document.getElementById('progressStudentsJson').textContent || '[]');
      var assignments = JSON.parse(document.getElementById('progressAssignmentsJson').textContent || '[]');
      var list = document.getElementById('progressStudentList');
      var classId = CLASS_ID;
      var gradeCache = {};

      function losFor(assignmentId) {
        var assignment = assignments.find(function (item) { return String(item.id) === String(assignmentId); });
        var links = assignment && assignment.assignment_objectives ? assignment.assignment_objectives : [];
        return links.map(function (link) {
          var lo = link.learning_objectives || {};
          return { id: String(link.learning_objective_id || ''), code: lo.vendor_code || 'LO' };
        }).filter(function (lo) { return lo.id; });
      }

      function paint(assignmentId, grades) {
        list.innerHTML = '';
        if (!students.length) {
          list.innerHTML = '<p class="px-1 py-4 text-sm text-slate-500 dark:text-slate-400">No active students.</p>';
          return;
        }
        var los = losFor(assignmentId);
        students.forEach(function (student) {
          var row = document.createElement('div');
          row.className = 'py-3';
          var name = document.createElement('button');
          name.type = 'button';
          name.className = 'w-full flex items-center justify-between gap-2 font-medium text-left text-slate-900 dark:text-white';
          var nameLabel = document.createElement('span');
          nameLabel.textContent = student.name || 'Student';
          var chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          chevron.setAttribute('class', 'w-5 h-5 text-slate-400 shrink-0 transition-transform');
          chevron.setAttribute('fill', 'none');
          chevron.setAttribute('stroke', 'currentColor');
          chevron.setAttribute('viewBox', '0 0 24 24');
          chevron.setAttribute('aria-hidden', 'true');
          var chevronPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          chevronPath.setAttribute('stroke-linecap', 'round');
          chevronPath.setAttribute('stroke-linejoin', 'round');
          chevronPath.setAttribute('stroke-width', '2');
          chevronPath.setAttribute('d', 'M19 9l-7 7-7-7');
          chevron.appendChild(chevronPath);
          name.appendChild(nameLabel);
          name.appendChild(chevron);
          var letters = document.createElement('div');
          letters.className = 'mt-2 hidden flex-wrap gap-1';
          los.forEach(function (lo) {
            var key = String(student.id) + '|' + lo.id;
            var letter = grades && grades[key] ? String(grades[key]) : '—';
            var chip = document.createElement('span');
            chip.className = 'px-1.5 py-0.5 rounded text-xs font-mono bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-200';
            chip.textContent = lo.code + ' ' + letter;
            letters.appendChild(chip);
          });
          var history = document.createElement('a');
          history.className = 'mt-2 block w-full text-xs font-medium text-blue-600 dark:text-blue-400';
          history.href = '/class/' + encodeURIComponent(classId) + '/student/' + encodeURIComponent(student.id) + '/history';
          history.textContent = 'Full history';
          letters.appendChild(history);
          name.addEventListener('click', function () {
            var open = letters.classList.contains('hidden');
            letters.classList.toggle('hidden', !open);
            letters.classList.toggle('flex', open);
            chevron.classList.toggle('rotate-180', open);
          });
          row.appendChild(name);
          row.appendChild(letters);
          list.appendChild(row);
        });
      }

      window.renderProgressStudents = function (assignmentId) {
        if (!assignmentId) {
          paint('', {});
          return;
        }
        if (gradeCache[assignmentId]) {
          paint(assignmentId, gradeCache[assignmentId]);
          return;
        }
        paint(assignmentId, {});
        fetch('/api/class/' + encodeURIComponent(classId) + '/assignment/' + encodeURIComponent(assignmentId) + '/grades', { credentials: 'same-origin' })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            var grades = data && data.grades ? data.grades : {};
            gradeCache[assignmentId] = grades;
            if (window.getActiveProgressAssignmentId && window.getActiveProgressAssignmentId() === String(assignmentId)) {
              paint(assignmentId, grades);
            }
          });
      };

      if (window.getActiveProgressAssignmentId) {
        window.renderProgressStudents(window.getActiveProgressAssignmentId());
      }
    })();
    
(function initDashboardStudentsState() {
  try {
    var el = document.getElementById('dashboardAllStudentsJson');
    if (!el || !el.textContent) {
      window.__dashboardAllStudents = [];
      return;
    }
    window.__dashboardAllStudents = JSON.parse(el.textContent);
  } catch (e) {
    window.__dashboardAllStudents = [];
  }
})();

const escapeHtmlDashboard = (window.ClarityUI && window.ClarityUI.escapeHtml)
  ? window.ClarityUI.escapeHtml
  : function (s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    };

function homeworkBadgeHtmlDashboard(mastered, total) {
  var pct = total > 0 ? Math.round((mastered / total) * 100) : 0;
  var cls = pct === 100
    ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300'
    : 'bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300';
  return '<span class="homework-badge inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ' + cls + '">' + pct + '%</span>';
}

function copyEmailBtnHtmlDashboard(email) {
  var e = escapeHtmlDashboard(email);
  return '<button type="button" data-copy-email-btn data-email="' + e + '" class="shrink-0 inline-flex items-center justify-center rounded-md p-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700/80 transition-colors relative z-20" aria-label="Copy email" title="Copy email" data-label-copied="Copied">' +
    '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg></button>';
}

function storedDashboardName(student) {
  return String((student && student.full_name) || '');
}

function buildStudentRowHTMLDashboard(s, classId) {
  var los = s.learning_objectives || [];
  var passed = los.filter(function (lo) { return lo.is_passed; }).length;
  var total = s.objective_total;
  var badge = homeworkBadgeHtmlDashboard(passed, total);
  var email = (s.email || '').trim();
  var searchKey = ((s.name || '') + ' ' + email).toLowerCase().trim();
  var detailPath = '/class/' + encodeURIComponent(classId) + '/students/' + encodeURIComponent(s.id);
  var emailBlock = email
    ? '<div class="flex items-center gap-1 mt-0.5 min-w-0"><span class="text-xs text-slate-500 dark:text-slate-400 select-text cursor-text truncate">' + escapeHtmlDashboard(email) + '</span>' + copyEmailBtnHtmlDashboard(email) + '</div>'
    : '';
  return (
    '<div class="student-item p-6 hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors" data-name="' + escapeHtmlDashboard(searchKey) + '" data-student-id="' + escapeHtmlDashboard(s.id) + '">' +
      '<div class="flex items-center justify-between">' +
        '<div class="flex items-center gap-3 min-w-0 flex-1">' +
          '<div class="min-w-0 flex-1">' +
            '<a href="' + detailPath + '" class="block hover:underline"><span class="font-medium text-slate-900 dark:text-white">' + escapeHtmlDashboard(s.name) + '</span></a>' +
            emailBlock +
          '</div>' +
          '<a href="' + detailPath + '" class="shrink-0 hover:opacity-90" tabindex="-1" aria-hidden="true">' + badge + '</a>' +
        '</div>' +
      '</div>' +
    '</div>'
  );
}

function insertStudentRowSortedDashboard(container, student) {
  var html = buildStudentRowHTMLDashboard(student, CLASS_ID);
  var wrap = document.createElement('div');
  wrap.innerHTML = html.trim();
  var newEl = wrap.firstElementChild;
  if (!newEl) return;
  var emptyEl = document.getElementById('studentsViewEmptyState');
  var children = Array.from(container.querySelectorAll('.student-item'));
  var insertBefore = null;
  for (var i = 0; i < children.length; i++) {
    var sid = children[i].getAttribute('data-student-id');
    var other = (window.__dashboardAllStudents || []).find(function (x) { return String(x.id) === String(sid); });
    if (!other) continue;
    if (window.StudentNames.compareNames(storedDashboardName(student), storedDashboardName(other)) < 0) {
      insertBefore = children[i];
      break;
    }
  }
  if (insertBefore) container.insertBefore(newEl, insertBefore);
  else if (emptyEl) container.insertBefore(newEl, emptyEl);
  else container.appendChild(newEl);
}

function updateActiveStudentCountsDashboard() {
  var arr = window.__dashboardAllStudents || [];
  var total = arr.length;
  var active = arr.filter(function (s) { return !s.muted; }).length;
  var countEl = document.getElementById('activeStudentsCount');
  if (countEl) countEl.textContent = String(active);
  var modalLine = document.getElementById('modalStudentCountsLine');
  if (modalLine) modalLine.textContent = active + ' active students (' + total + ' total)';
  var emptyEl = document.getElementById('studentsViewEmptyState');
  if (emptyEl) {
    if (active === 0) emptyEl.classList.remove('hidden');
    else emptyEl.classList.add('hidden');
  }
}

function maybeOpenManageStudentsModalFromQuery() {
  const modal = document.getElementById('studentManagementModal');
  if (!modal) return;

  const currentUrl = new URL(window.location.href);
  if (currentUrl.searchParams.get('manage_students') !== '1') return;

  modal.classList.remove('hidden');

  // Clean up the URL after restoring modal state.
  currentUrl.searchParams.delete('manage_students');
  window.history.replaceState({}, '', currentUrl.toString());
}

function selectRevisionView(key) {
  var views = document.querySelectorAll('#overdueRevisionsModal .revision-view');
  for (var i = 0; i < views.length; i++) {
    var on = views[i].getAttribute('data-revision-view') === key;
    views[i].hidden = !on;
    views[i].classList.toggle('is-selected', on);
  }
}

function openOverdueRevisionsModal() {
  var modal = document.getElementById('overdueRevisionsModal');
  if (!modal) return;
  if (!modal.classList.contains('hidden')) return;
  modal.classList.remove('hidden');
  if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === 'function') {
    window.ClarityUI.lockPageScroll();
  }
}

function closeOverdueRevisionsModal() {
  var modal = document.getElementById('overdueRevisionsModal');
  var wasOpen = !!(modal && !modal.classList.contains('hidden'));
  if (modal) modal.classList.add('hidden');
  if (wasOpen && window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === 'function') {
    window.ClarityUI.unlockPageScroll();
  }
}

var revisionEmailNeedsReload = false;

function closeRevisionEmailModal() {
  var modal = document.getElementById('revisionEmailModal');
  if (modal) modal.classList.add('hidden');
  if (window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === 'function') {
    window.ClarityUI.unlockPageScroll();
  }
  if (revisionEmailNeedsReload) {
    location.reload();
  }
}

function revisionEmailLine(label, people) {
  var block = document.createElement('div');
  var title = document.createElement('p');
  title.className = 'font-medium text-slate-900 dark:text-white';
  title.textContent = label;
  block.appendChild(title);
  (people || []).forEach(function (person) {
    var line = document.createElement('p');
    if (person.message) {
      line.textContent = person.name + ': ' + person.message;
    } else {
      line.textContent = person.email ? (person.name + ' <' + person.email + '>') : person.name;
    }
    block.appendChild(line);
  });
  return block;
}

function openRevisionEmailModal() {
  var modal = document.getElementById('revisionEmailModal');
  var body = document.getElementById('revisionEmailBody');
  var summary = document.getElementById('revisionEmailSummary');
  var emailSendBtn = document.getElementById('revisionEmailSend');
  var cancelBtn = document.getElementById('revisionEmailCancel');
  if (!modal || !body || !summary || !emailSendBtn) return;
  revisionEmailNeedsReload = false;
  if (cancelBtn) cancelBtn.textContent = 'Cancel';
  body.textContent = '';
  summary.textContent = '';
  emailSendBtn.disabled = true;
  modal.classList.remove('hidden');
  if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === 'function') {
    window.ClarityUI.lockPageScroll();
  }
  fetch('/api/class/' + CLASS_ID + '/revision-reminders/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function (response) {
    return response.json().then(function (data) {
      return {ok: response.ok, data: data || {}};
    });
  }).then(function (result) {
    if (!result.ok) {
      summary.textContent = result.data.error || 'Could not load the reminder.';
      return;
    }
    var people = result.data.recipients || [];
    var count = document.createElement('p');
    count.textContent = people.length === 1
      ? '1 student will be emailed.'
      : (people.length + ' students will be emailed.');
    body.appendChild(count);
    body.appendChild(revisionEmailLine('Recipients', people));
    var missingEmail = result.data.no_email || [];
    if (missingEmail.length) {
      body.appendChild(revisionEmailLine('No email on file', missingEmail));
    }
    var invalidEmail = result.data.invalid || [];
    if (invalidEmail.length) {
      body.appendChild(revisionEmailLine('Invalid email', invalidEmail));
    }
    var heldEmail = result.data.held || [];
    if (heldEmail.length) {
      body.appendChild(revisionEmailLine('Address added or changed less than 24 hours ago', heldEmail));
    }
    var reminded = result.data.already_reminded || [];
    if (reminded.length) {
      body.appendChild(revisionEmailLine('Already reminded', reminded));
    }
    if (result.data.sample_body) {
      var heading = document.createElement('p');
      heading.className = 'font-medium text-slate-900 dark:text-white';
      heading.textContent = 'Sample message (first student)';
      body.appendChild(heading);
      var preview = document.createElement('pre');
      preview.className = 'whitespace-pre-wrap font-sans';
      preview.textContent = result.data.sample_body;
      body.appendChild(preview);
    }
    emailSendBtn.disabled = people.length === 0;
  }).catch(function () {
    summary.textContent = 'Could not load the reminder.';
  });
}

function sendRevisionEmails() {
  var emailSendBtn = document.getElementById('revisionEmailSend');
  var summary = document.getElementById('revisionEmailSummary');
  var cancelBtn = document.getElementById('revisionEmailCancel');
  if (!emailSendBtn || !summary) return;
  emailSendBtn.disabled = true;
  fetch('/api/class/' + CLASS_ID + '/revision-reminders/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function (response) {
    revisionEmailNeedsReload = true;
    if (cancelBtn) cancelBtn.textContent = 'Close';
    emailSendBtn.disabled = true;
    return response.json().then(function (data) {
      return {ok: response.ok, data: data || {}};
    });
  }).then(function (result) {
    if (!result.ok) {
      summary.textContent = result.data.error || 'Could not send the reminders.';
      return;
    }
    var counts = result.data.counts || {};
    var lines = [
      (counts.sent || 0) + ' sent',
      (counts.failed || 0) + ' failed',
      (counts.skipped || 0) + ' skipped',
      (counts.not_attempted || 0) + ' not attempted'
    ];
    if (counts.held) {
      lines.push(counts.held + ' held because the address was added or changed less than 24 hours ago');
    }
    if (counts.invalid) {
      lines.push(counts.invalid + ' not sent because the saved address is not valid');
    }
    if (counts.capped) {
      lines.push(counts.capped + ' not sent because a daily limit was reached');
    }
    if (counts.sent_not_recorded) {
      lines.push(counts.sent_not_recorded + ' sent but not recorded');
    }
    if (result.data.message) lines.push(result.data.message);
    if (result.data.warning) lines.push(result.data.warning);
    summary.textContent = lines.join('. ');
  }).catch(function () {
    summary.textContent = 'Could not send the reminders.';
  });
}

maybeOpenManageStudentsModalFromQuery();

// `ClarityUI.debounce` lives in ui_helpers.js; fall back to an identity
// wrapper so this page still works if that script load is blocked.
const __clarityDebounce =
  (window.ClarityUI && typeof window.ClarityUI.debounce === 'function')
    ? window.ClarityUI.debounce
    : function (fn) { return fn; };

// Cached node lists below assume the DOM is static after initial render.
// Add/remove of student or assignment rows happens via full page reload, so
// the cache is rebuilt naturally without manual invalidation here.
let __cachedModalStudents = null;
function _modalStudents() {
  if (!__cachedModalStudents) {
    __cachedModalStudents = Array.from(document.querySelectorAll('.modal-student'));
  }
  return __cachedModalStudents;
}

const _runFilterModalStudents = __clarityDebounce(function () {
  const q = (document.getElementById('modalSearchInput')?.value || '').toLowerCase();
  const list = _modalStudents();
  for (let i = 0; i < list.length; i++) {
    const el = list[i];
    const wanted = (!q || (el.dataset.name || '').includes(q)) ? '' : 'none';
    if (el.style.display !== wanted) el.style.display = wanted;
  }
}, 80);

function filterModalStudents() {
  _runFilterModalStudents();
}
function toggleMute(checkbox) {
  const row = checkbox.closest('.modal-student');
  const studentId = checkbox.dataset.studentId;
  const muted = checkbox.checked;
  row.classList.toggle('opacity-60', muted);
  row.querySelector('p').classList.toggle('line-through', muted);
  fetch('/api/class/' + CLASS_ID + '/toggle_mute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ student_id: studentId, muted: muted })
  })
  .then(r => r.json())
  .then(data => {
    if (!data.success) {
      alert('Error toggling mute: ' + (data.error || 'Unknown error'));
      checkbox.checked = !muted;
      row.classList.toggle('opacity-60', !muted);
      row.querySelector('p').classList.toggle('line-through', !muted);
      return;
    }
    const roster = window.__dashboardAllStudents || [];
    const s = roster.find(function (x) { return String(x.id) === String(studentId); });
    if (s) s.muted = muted;

    const listEl = document.getElementById('studentsView');
    const dashRow = listEl && Array.from(listEl.querySelectorAll('.student-item')).find(function (el) {
      return el.dataset.studentId === studentId;
    });
    if (muted) {
      if (dashRow) dashRow.remove();
    } else if (s && listEl) {
      insertStudentRowSortedDashboard(listEl, s);
    }
    updateActiveStudentCountsDashboard();
    if (typeof handleSearch === 'function') handleSearch();
  })
  .catch(() => {
    alert('Error toggling mute.');
    checkbox.checked = !muted;
    row.classList.toggle('opacity-60', !muted);
    row.querySelector('p').classList.toggle('line-through', !muted);
  });
}

function switchView(view) {
  const studentsView = document.getElementById('studentsView');
  const assignmentsView = document.getElementById('assignmentsView');
  const btnStudents = document.getElementById('btnStudents');
  const btnAssignments = document.getElementById('btnAssignments');
  if (view === 'students') {
    studentsView.classList.remove('hidden');
    assignmentsView.classList.add('hidden');
    btnStudents.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white transition-colors';
    btnAssignments.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors';
  } else {
    studentsView.classList.add('hidden');
    assignmentsView.classList.remove('hidden');
    btnAssignments.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white transition-colors';
    btnStudents.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors';
  }
}
var removalTargetId = null;

function showModalLineError(id) {
  var el = document.getElementById(id);
  if (!el) return;
  el.textContent = 'Network error, please try again';
  el.classList.remove('hidden');
}

function readJsonResult(response) {
  return response.json().then(function (data) {
    return { success: response.ok && data && data.success, data: data };
  });
}

function openEditStudent(btn) {
  var row = btn.closest('.modal-student');
  document.getElementById('editStudentId').value = row.getAttribute('data-student-id');
  document.getElementById('editStudentEmail').value = row.getAttribute('data-email') || '';
  document.getElementById('editStudentName').value = row.getAttribute('data-display-name') || '';
  var err = document.getElementById('editStudentError');
  err.textContent = '';
  err.classList.add('hidden');
  document.getElementById('editStudentSave').disabled = false;
  document.getElementById('editStudentModal').classList.remove('hidden');
}

function saveEditStudent(event) {
  event.preventDefault();
  var saveBtn = document.getElementById('editStudentSave');
  var err = document.getElementById('editStudentError');
  var studentId = document.getElementById('editStudentId').value;
  saveBtn.disabled = true;
  err.classList.add('hidden');
  fetch('/api/class/' + CLASS_ID + '/students/' + encodeURIComponent(studentId) + '/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      email: document.getElementById('editStudentEmail').value,
      name: document.getElementById('editStudentName').value
    })
  })
    .then(readJsonResult)
    .then(function (result) {
      if (!result.success) {
        err.textContent = (result.data && result.data.error) || 'Could not save student';
        err.classList.remove('hidden');
        saveBtn.disabled = false;
        return;
      }
      var next = new URL(window.location.href);
      next.searchParams.set('manage_students', '1');
      window.location.assign(next.toString());
    })
    .catch(function () {
      showModalLineError('editStudentError');
      saveBtn.disabled = false;
    });
}

function openDeleteStudent(btn) {
  var row = btn.closest('.modal-student');
  var studentId = row.getAttribute('data-student-id');
  var studentName = row.getAttribute('data-display-name') || '';
  var confirmBtn = document.getElementById('studentRemovalConfirm');
  var err = document.getElementById('studentRemovalError');
  var body = document.getElementById('studentRemovalBody');
  removalTargetId = studentId;
  confirmBtn.disabled = true;
  confirmBtn.onclick = null;
  err.textContent = '';
  err.classList.add('hidden');
  body.textContent = 'Loading removal counts for ' + studentName + '.';
  document.getElementById('studentRemovalModal').classList.remove('hidden');
  fetch('/api/class/' + CLASS_ID + '/students/' + encodeURIComponent(studentId) + '/removal-counts', {
    headers: { 'Accept': 'application/json' },
    credentials: 'same-origin'
  })
    .then(readJsonResult)
    .then(function (result) {
      if (removalTargetId !== studentId) return;
      if (!result.success) {
        err.textContent = (result.data && result.data.error) || 'Could not load removal counts';
        err.classList.remove('hidden');
        return;
      }
      var grades = result.data.grade_rows;
      var homework = result.data.homework_scores;
      body.textContent = 'Delete ' + studentName + ' from this class? This permanently removes their enrollment, ' + grades + ' grade rows, and ' + homework + ' homework scores in this class. Their profile is kept. This cannot be undone.';
      confirmBtn.disabled = false;
      confirmBtn.onclick = function () {
        if (removalTargetId !== studentId) return;
        confirmBtn.disabled = true;
        fetch('/class/' + CLASS_ID + '/students/' + encodeURIComponent(studentId) + '/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          credentials: 'same-origin',
          body: '{}'
        })
          .then(readJsonResult)
          .then(function (deleteResult) {
            if (removalTargetId !== studentId) return;
            if (!deleteResult.success) {
              err.textContent = (deleteResult.data && deleteResult.data.error) || 'Could not delete student';
              err.classList.remove('hidden');
              confirmBtn.disabled = false;
              return;
            }
            var next = new URL(window.location.href);
            next.searchParams.set('manage_students', '1');
            window.location.href = next.toString();
          })
          .catch(function () {
            if (removalTargetId !== studentId) return;
            showModalLineError('studentRemovalError');
            confirmBtn.disabled = false;
          });
      };
    })
    .catch(function () {
      if (removalTargetId !== studentId) return;
      showModalLineError('studentRemovalError');
    });
}
function toggleAssignment(el) {
  const content = el.nextElementSibling;
  const chevron = el.querySelector('.chevron');
  content.classList.toggle('hidden');
  chevron.classList.toggle('rotate-180');
}

let __cachedStudentItems = null;
let __cachedAssignmentItems = null;
function _studentItems() {
  if (!__cachedStudentItems) {
    __cachedStudentItems = Array.from(document.querySelectorAll('.student-item'));
  }
  return __cachedStudentItems;
}
function _assignmentItems() {
  if (!__cachedAssignmentItems) {
    __cachedAssignmentItems = Array.from(document.querySelectorAll('.assignment-item'));
  }
  return __cachedAssignmentItems;
}

const _runHandleSearch = __clarityDebounce(function () {
  const q = (document.getElementById('searchInput').value || '').toLowerCase();
  const sList = _studentItems();
  for (let i = 0; i < sList.length; i++) {
    const item = sList[i];
    const name = item.dataset.name || '';
    const want = (!q || name.includes(q)) ? '' : 'none';
    if (item.style.display !== want) item.style.display = want;
  }
  const aList = _assignmentItems();
  for (let j = 0; j < aList.length; j++) {
    const item = aList[j];
    const name = item.dataset.assignmentName || '';
    const want = (!q || name.includes(q)) ? '' : 'none';
    if (item.style.display !== want) item.style.display = want;
  }
}, 80);

function handleSearch() {
  _runHandleSearch();
}

function escapeCsvCell(val) {
  const s = String(val == null ? '' : val);
  if (/[",\r\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}
function csvTextFromRows(rows) {
  return rows.map((row) => row.map(escapeCsvCell).join(',')).join('\r\n');
}
function triggerCsvDownload(filename, csvText) {
  const blob = new Blob(['\ufeff' + csvText], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}
function clampCsvTemplateRows(raw, def, max) {
  let n = parseInt(String(raw), 10);
  if (!Number.isFinite(n) || n < 1) n = def;
  if (n > max) n = max;
  return n;
}
function downloadStudentsCsvTemplate() {
  const inp = document.getElementById('studentsCsvTemplateCount');
  const n = clampCsvTemplateRows(inp && inp.value, 10, 500);
  if (inp) inp.value = String(n);
  const rows = [['Student Name', 'email']];
  for (let i = 0; i < n; i++) rows.push(['', '']);
  triggerCsvDownload('students_import_template.csv', csvTextFromRows(rows));
}
function openUploadStudentsModal() {
  const modal = document.getElementById('uploadStudentsModal');
  if (!modal) return;
  const err = document.getElementById('uploadStudentsError');
  if (err) {
    err.classList.add('hidden');
    err.textContent = '';
  }
  if (!modal.classList.contains('hidden')) return;
  modal.classList.remove('hidden');
  if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === 'function') {
    window.ClarityUI.lockPageScroll();
  }
}

function closeUploadStudentsModal(clearFile = true) {
  const modal = document.getElementById('uploadStudentsModal');
  const wasOpen = !!(modal && !modal.classList.contains('hidden'));
  if (modal) modal.classList.add('hidden');
  if (wasOpen && window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === 'function') {
    window.ClarityUI.unlockPageScroll();
  }
  const fileInput = document.getElementById('uploadStudentsInput');
  if (clearFile && fileInput) fileInput.value = '';
  const err = document.getElementById('uploadStudentsError');
  if (err) {
    err.classList.add('hidden');
    err.textContent = '';
  }
}

function renderStudentUploadPreviewTable(rows) {
  const tbody = document.getElementById('studentUploadPreviewTbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (rows || []).forEach((row) => {
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    nameTd.className = 'px-2 py-2 align-top';
    nameTd.textContent = String(row.full_name || '');
    tr.appendChild(nameTd);

    const emailTd = document.createElement('td');
    emailTd.className = 'px-2 py-2 align-top break-all';
    emailTd.textContent = String(row.email || '');
    tr.appendChild(emailTd);

    const statusTd = document.createElement('td');
    statusTd.className = 'px-2 py-2 align-top text-xs font-medium';
    statusTd.textContent = String(row.status || '');
    statusTd.classList.add('text-green-700', 'dark:text-green-300');
    tr.appendChild(statusTd);

    tbody.appendChild(tr);
  });
}

function openStudentUploadPreviewModal() {
  const m = document.getElementById('studentUploadPreviewModal');
  if (!m || !m.classList.contains('hidden')) return;
  m.classList.remove('hidden');
  if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === 'function') {
    window.ClarityUI.lockPageScroll();
  }
}

function closeStudentUploadPreviewModal() {
  const m = document.getElementById('studentUploadPreviewModal');
  const wasOpen = !!(m && !m.classList.contains('hidden'));
  if (m) m.classList.add('hidden');
  if (wasOpen && window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === 'function') {
    window.ClarityUI.unlockPageScroll();
  }
  const err = document.getElementById('studentUploadPreviewError');
  if (err) {
    err.classList.add('hidden');
    err.textContent = '';
  }
}

function submitUploadStudents() {
  const fileInput = document.getElementById('uploadStudentsInput');
  const errorEl = document.getElementById('uploadStudentsError');
  const submitBtn = document.getElementById('uploadStudentsSubmitBtn');
  if (!fileInput || !fileInput.files || !fileInput.files.length) {
    if (errorEl) {
      errorEl.textContent = 'Please choose a CSV file.';
      errorEl.classList.remove('hidden');
    }
    return;
  }

  const file = fileInput.files[0];
  if (!file.name.toLowerCase().endsWith('.csv')) {
    if (errorEl) {
      errorEl.textContent = 'Only .csv files are supported.';
      errorEl.classList.remove('hidden');
    }
    return;
  }

  if (errorEl) {
    errorEl.classList.add('hidden');
    errorEl.textContent = '';
  }

  const fd = new FormData();
  fd.append('file', file);

  const previousText = submitBtn ? submitBtn.textContent : 'Preview Import';
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.classList.add('opacity-60', 'cursor-not-allowed');
    submitBtn.textContent = 'Preparing preview...';
  }

  fetch('/api/class/' + CLASS_ID + '/preview-upload-students', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin'
  })
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      return { ok: response.ok, data: data };
    })
    .then(({ ok, data }) => {
      if (!ok || !data.success) {
        if (errorEl) {
          errorEl.textContent = data.error || 'Student upload failed.';
          errorEl.classList.remove('hidden');
        }
        return;
      }

      renderStudentUploadPreviewTable(data.rows || []);

      openStudentUploadPreviewModal();
      closeUploadStudentsModal(false);
    })
    .catch(() => {
      if (errorEl) {
        errorEl.textContent = 'Unexpected error preparing preview.';
        errorEl.classList.remove('hidden');
      }
    })
    .finally(() => {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.classList.remove('opacity-60', 'cursor-not-allowed');
        submitBtn.textContent = previousText || 'Upload Students';
      }
    });
}

function confirmStudentUploadImport() {
  const fileInput = document.getElementById('uploadStudentsInput');
  const errorEl = document.getElementById('studentUploadPreviewError');
  const confirmBtn = document.getElementById('studentUploadPreviewConfirm');
  if (!fileInput || !fileInput.files || !fileInput.files.length) {
    if (errorEl) {
      errorEl.textContent = 'Please re-select your CSV file.';
      errorEl.classList.remove('hidden');
    }
    openUploadStudentsModal();
    closeStudentUploadPreviewModal();
    return;
  }

  if (errorEl) {
    errorEl.classList.add('hidden');
    errorEl.textContent = '';
  }

  const fd = new FormData();
  fd.append('file', fileInput.files[0]);

  const previousText = confirmBtn ? confirmBtn.textContent : 'Import confirmed rows';
  if (confirmBtn) {
    confirmBtn.disabled = true;
    confirmBtn.classList.add('opacity-60', 'cursor-not-allowed');
    confirmBtn.textContent = 'Importing...';
  }

  fetch('/api/class/' + CLASS_ID + '/upload_students', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin'
  })
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      return { ok: response.ok, data: data };
    })
    .then(({ ok, data }) => {
      if (!ok || !data.success) {
        if (errorEl) {
          errorEl.textContent = data.error || 'Student import failed.';
          errorEl.classList.remove('hidden');
        }
        return;
      }
      alert('Student upload complete.');
      closeStudentUploadPreviewModal();
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set('manage_students', '1');
      window.location.assign(nextUrl.toString());
    })
    .catch(() => {
      if (errorEl) {
        errorEl.textContent = 'Unexpected error importing students.';
        errorEl.classList.remove('hidden');
      }
    })
    .finally(() => {
      if (confirmBtn) {
        confirmBtn.disabled = false;
        confirmBtn.classList.remove('opacity-60', 'cursor-not-allowed');
        confirmBtn.textContent = previousText || 'Import confirmed rows';
      }
    });
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

function dashboardDeleteLO(id, name) {
  if (!id) return;
  var label = name != null && String(name).length ? String(name) : 'this objective';
  if (!confirm('Are you sure you want to delete "' + label + '"? This will also remove all associated grades.')) return;
  fetch('/class/' + CLASS_ID + '/delete_lo/' + encodeURIComponent(id), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
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
    .catch(function () {
      alert('Error deleting learning objective.');
    });
}

document.addEventListener('DOMContentLoaded', function () {
  var list = document.getElementById('existingLosList');
  if (list) {
    list.addEventListener('click', function (e) {
      var row = e.target.closest('.lo-row');
      if (!row) return;
      if (e.target.closest('.lo-delete-btn')) {
        var id = row.getAttribute('data-lo-id');
        var disp = row.querySelector('.lo-row-display');
        var nm = disp ? disp.textContent : '';
        dashboardDeleteLO(id, nm);
        return;
      }
      if (e.target.closest('.lo-edit-btn')) {
        openEditLoFromRow(row);
      }
    });
  }
  wireLoDescriptionAutoGrow();
});

function addStudent(event) {
  event.preventDefault();
  
  const email = document.getElementById('studentEmail').value;
  const name = document.getElementById('studentName').value;
  
  fetch('/class/' + CLASS_ID + '/add_student', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      email: email,
      name: name
    })
  })
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      document.getElementById('addStudentModal').classList.add('hidden');
      document.getElementById('addStudentForm').reset();
      const returnUrl = new URL(window.location.href);
      returnUrl.searchParams.set('manage_students', '1');
      window.location.assign(returnUrl.toString());
    } else {
      alert('Error adding student: ' + data.error);
    }
  })
  .catch(error => {
    alert('Error: ' + error);
  });
}

document.getElementById('openStudentManagementBtn').addEventListener('click', function () {
  document.getElementById('studentManagementModal').classList.remove('hidden');
});
document.getElementById('openStudentManagementEmptyBtn').addEventListener('click', function () {
  document.getElementById('studentManagementModal').classList.remove('hidden');
});
document.getElementById('openOverdueRevisionsBtn').addEventListener('click', function () {
  openOverdueRevisionsModal();
});
document.getElementById('btnStudents').addEventListener('click', function () {
  switchView('students');
});
document.getElementById('btnAssignments').addEventListener('click', function () {
  switchView('assignments');
});
document.getElementById('openUploadStudentsBtn').addEventListener('click', function () {
  openUploadStudentsModal();
});
document.getElementById('searchInput').addEventListener('input', function () {
  handleSearch();
});
document.addEventListener('click', function (event) {
  var el = event.target.closest('[data-toggle-assignment]');
  if (!el) return;
  toggleAssignment(el);
});
document.getElementById('revisionViewSelect').addEventListener('change', function () {
  selectRevisionView(this.value);
});
document.getElementById('closeOverdueRevisionsBtn').addEventListener('click', function () {
  closeOverdueRevisionsModal();
});
document.addEventListener('click', function (event) {
  var btn = event.target.closest('[data-open-revision-email]');
  if (!btn) return;
  openRevisionEmailModal();
});
var revisionEmailCancel = document.getElementById('revisionEmailCancel');
if (revisionEmailCancel) revisionEmailCancel.addEventListener('click', function () {
  closeRevisionEmailModal();
});
var revisionEmailSend = document.getElementById('revisionEmailSend');
if (revisionEmailSend) revisionEmailSend.addEventListener('click', function () {
  sendRevisionEmails();
});
document.getElementById('openAddStudentBtn').addEventListener('click', function () {
  document.getElementById('addStudentModal').classList.remove('hidden');
});
document.getElementById('closeStudentManagementBtn').addEventListener('click', function () {
  document.getElementById('studentManagementModal').classList.add('hidden');
});
var modalSearchInput = document.getElementById('modalSearchInput');
if (modalSearchInput) modalSearchInput.addEventListener('input', function () {
  filterModalStudents();
});
document.addEventListener('change', function (event) {
  var box = event.target.closest('.mute-toggle');
  if (!box) return;
  toggleMute(box);
});
document.addEventListener('click', function (event) {
  var editBtn = event.target.closest('[data-edit-student]');
  if (editBtn) openEditStudent(editBtn);
});
document.addEventListener('click', function (event) {
  var deleteBtn = event.target.closest('[data-delete-student]');
  if (deleteBtn) openDeleteStudent(deleteBtn);
});
document.getElementById('addStudentModal').addEventListener('click', function (event) {
  if (event.target === this) this.classList.add('hidden');
});
document.getElementById('addStudentModalPanel').addEventListener('click', function (event) {
  event.stopPropagation();
});
document.getElementById('closeAddStudentBtn').addEventListener('click', function () {
  document.getElementById('addStudentModal').classList.add('hidden');
});
document.getElementById('cancelAddStudentBtn').addEventListener('click', function () {
  document.getElementById('addStudentModal').classList.add('hidden');
});
document.getElementById('addStudentForm').addEventListener('submit', function (event) {
  addStudent(event);
});
document.getElementById('editStudentModal').addEventListener('click', function (event) {
  if (event.target === this) this.classList.add('hidden');
});
document.getElementById('editStudentModalPanel').addEventListener('click', function (event) {
  event.stopPropagation();
});
document.getElementById('editStudentForm').addEventListener('submit', function (event) {
  saveEditStudent(event);
});
document.getElementById('cancelEditStudentBtn').addEventListener('click', function () {
  document.getElementById('editStudentModal').classList.add('hidden');
});
document.getElementById('studentRemovalModal').addEventListener('click', function (event) {
  if (event.target === this) this.classList.add('hidden');
});
document.getElementById('studentRemovalModalPanel').addEventListener('click', function (event) {
  event.stopPropagation();
});
document.getElementById('cancelStudentRemovalBtn').addEventListener('click', function () {
  document.getElementById('studentRemovalModal').classList.add('hidden');
});
document.getElementById('downloadStudentsCsvTemplateBtn').addEventListener('click', function () {
  downloadStudentsCsvTemplate();
});
document.getElementById('closeUploadStudentsBtn').addEventListener('click', function () {
  closeUploadStudentsModal();
});
document.getElementById('uploadStudentsSubmitBtn').addEventListener('click', function () {
  submitUploadStudents();
});
document.getElementById('studentUploadBackBtn').addEventListener('click', function () {
  openUploadStudentsModal();
  closeStudentUploadPreviewModal();
});
document.getElementById('studentUploadPreviewConfirm').addEventListener('click', function () {
  confirmStudentUploadImport();
});
