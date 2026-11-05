
if (window.ClarityUI && typeof window.ClarityUI.applyInstructorModeVisibility === 'function') {
  var modeEl = document.getElementById('instructorModeJson');
  window.ClarityUI.applyInstructorModeVisibility(JSON.parse(modeEl.textContent));
}
