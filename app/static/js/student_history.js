
function emailStudentHistoryReport(btnEl) {
  var el = document.getElementById('studentHistoryEmailPayload');
  if (!el) return;
  var p = JSON.parse(el.textContent);
  var classId = String(p.class_id || '').trim();
  var studentId = String(p.student_id || '').trim();
  var addr = (p.email || '').trim();
  if (!classId || !studentId) {
    alert('Could not send email because report context is missing.');
    return;
  }
  if (!addr) {
    alert('No email address is saved for this student.');
    return;
  }
  var lines = [
    'Hello ' + window.ClarityUI.studentGreetingName(p.student_name) + ',',
    '',
    'Here is your student history report (same information as the printable page).',
    '',
    'Class: ' + (p.class_name || ''),
    'Date: ' + new Date().toLocaleDateString(),
    '',
  ];
  (p.objectives || []).forEach(function (lo) {
    lines.push('--- ' + String(lo.vendor_code || 'Objective') + ' ---');
    var grades = lo.grades || [];
    if (grades.length === 0) {
      lines.push('  (no grades yet)');
    } else {
      grades.forEach(function (grade) {
        var an = (grade.assignments && grade.assignments.name) ? grade.assignments.name : 'Assignment';
        var s1 = grade.top_score != null ? String(grade.top_score) : '—';
        var s2 = grade.second_score != null && grade.second_score !== grade.top_score ? String(grade.second_score) : '';
        lines.push('  ' + an + ': ' + s1 + (s2 ? ' / ' + s2 : ''));
      });
    }
    lines.push('');
  });
  var body = lines.join('\n');
  if (body.length > 2000) {
    body = body.slice(0, 1970) + '\n\n[Message trimmed. Use Print full report for the complete view.]';
  }
  var subject = 'Your student history report - ' + (p.class_name || 'Class');
  if (!confirm('Send this report directly to ' + addr + '?')) {
    return;
  }

  if (btnEl) {
    btnEl.disabled = true;
    btnEl.classList.add('opacity-60', 'cursor-not-allowed');
  }

  fetch('/api/class/' + encodeURIComponent(classId) + '/student/' + encodeURIComponent(studentId) + '/send-report-email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      subject: subject,
      body: body
    })
  })
    .then(async function (response) {
      var data = await response.json().catch(function () { return {}; });
      return { ok: response.ok, data: data };
    })
    .then(function (result) {
      if (!result.ok || !result.data.success) {
        alert('Could not send email: ' + (result.data.error || 'Unknown error'));
        return;
      }
      alert('Report emailed to ' + addr + '.');
    })
    .catch(function () {
      alert('Unexpected error sending report email.');
    })
    .finally(function () {
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.classList.remove('opacity-60', 'cursor-not-allowed');
      }
    });
}

var emailHistoryBtn = document.getElementById('emailStudentHistoryBtn');
if (emailHistoryBtn) {
  emailHistoryBtn.addEventListener('click', function () {
    emailStudentHistoryReport(this);
  });
}
