(function () {
  var classEl = document.getElementById('classIdJson');
  window.CLASS_ID = JSON.parse(classEl.textContent);
  // Pass assignment LO data from server to JS
  var assignmentEl = document.getElementById('assignmentsJson');
  window.ASSIGNMENTS_DATA = JSON.parse(assignmentEl.textContent);
})();

document.getElementById('csvInput').addEventListener('change', function () {
  handleFileSelect(this);
});
document.getElementById('nextBtn1').addEventListener('click', function () {
  goToStep(2);
});
document.getElementById('assignmentSelect').addEventListener('change', function () {
  onAssignmentSelected();
});
document.getElementById('mapBackBtn').addEventListener('click', function () {
  goToStep(1);
});
document.getElementById('nextBtn2').addEventListener('click', function () {
  continueFromMapStep();
});
document.getElementById('reviewBackBtn').addEventListener('click', function () {
  goToStep(2);
});
document.getElementById('reviewContinueBtn').addEventListener('click', function () {
  continueFromReviewStep();
});
document.getElementById('confirmBackBtn').addEventListener('click', function () {
  goToStep(3);
});
