
document.querySelectorAll('time.archived-at').forEach((node) => {
  const raw = node.getAttribute('datetime') || '';
  const parsed = new Date(raw);
  if (!raw || Number.isNaN(parsed.getTime())) return;
  node.textContent = parsed.toLocaleDateString();
});

const deleteArchiveModal = document.getElementById('deleteArchiveModal');
const deleteArchiveInput = document.getElementById('deleteArchiveNameInput');
const deleteArchiveSubmit = document.getElementById('deleteArchiveSubmitBtn');
const deleteArchiveCancel = document.getElementById('deleteArchiveModalCancel');
const deleteArchiveError = document.getElementById('deleteArchiveError');
const deleteArchiveLabel = document.getElementById('deleteArchiveNameLabel');
const deleteArchiveSlowReply = 'The server took too long or stopped responding. Check Archived Classes before trying again.';
let deleteArchiveBusy = false;
let deleteArchiveId = '';
let deleteArchiveName = '';

function deleteArchiveNameMatches() {
  return !!deleteArchiveInput && deleteArchiveInput.value === deleteArchiveName;
}

function refreshDeleteArchiveSubmit() {
  if (!deleteArchiveSubmit) return;
  deleteArchiveSubmit.disabled = deleteArchiveBusy || !deleteArchiveNameMatches();
  deleteArchiveSubmit.textContent = deleteArchiveBusy ? 'Deleting...' : 'Delete archive';
}

function showDeleteArchiveError(message) {
  if (!deleteArchiveError) return;
  deleteArchiveError.textContent = message;
  deleteArchiveError.classList.remove('hidden');
}

function releaseDeleteArchiveSubmit() {
  deleteArchiveBusy = false;
  if (deleteArchiveInput) deleteArchiveInput.disabled = false;
  if (deleteArchiveCancel) deleteArchiveCancel.disabled = false;
  refreshDeleteArchiveSubmit();
}

function resetDeleteArchiveModal() {
  deleteArchiveBusy = false;
  deleteArchiveId = '';
  deleteArchiveName = '';
  if (deleteArchiveInput) {
    deleteArchiveInput.value = '';
    deleteArchiveInput.disabled = false;
  }
  if (deleteArchiveCancel) deleteArchiveCancel.disabled = false;
  if (deleteArchiveError) {
    deleteArchiveError.textContent = '';
    deleteArchiveError.classList.add('hidden');
  }
  if (deleteArchiveLabel) deleteArchiveLabel.textContent = '';
  refreshDeleteArchiveSubmit();
}

function closeDeleteArchiveModal() {
  if (deleteArchiveBusy || !deleteArchiveModal) return;
  deleteArchiveModal.classList.add('hidden');
  resetDeleteArchiveModal();
}

function openDeleteArchiveModal(archiveId, className) {
  if (!deleteArchiveModal) return;
  resetDeleteArchiveModal();
  deleteArchiveId = archiveId;
  deleteArchiveName = className;
  if (deleteArchiveLabel) deleteArchiveLabel.textContent = className;
  deleteArchiveModal.classList.remove('hidden');
  if (deleteArchiveInput) deleteArchiveInput.focus();
  refreshDeleteArchiveSubmit();
}

document.querySelectorAll('.delete-archive-btn').forEach((btn) => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const archiveId = btn.getAttribute('data-archive-id') || '';
    const className = btn.getAttribute('data-class-name') || '';
    if (archiveId) openDeleteArchiveModal(archiveId, className);
  });
});

deleteArchiveInput?.addEventListener('input', refreshDeleteArchiveSubmit);
deleteArchiveCancel?.addEventListener('click', closeDeleteArchiveModal);
deleteArchiveModal?.addEventListener('click', (e) => {
  if (e.target === deleteArchiveModal) closeDeleteArchiveModal();
});

deleteArchiveSubmit?.addEventListener('click', async () => {
  if (deleteArchiveBusy || !deleteArchiveNameMatches() || !deleteArchiveId) return;
  deleteArchiveBusy = true;
  if (deleteArchiveInput) deleteArchiveInput.disabled = true;
  if (deleteArchiveCancel) deleteArchiveCancel.disabled = true;
  if (deleteArchiveError) deleteArchiveError.classList.add('hidden');
  refreshDeleteArchiveSubmit();
  let response;
  try {
    response = await fetch('/archived-classes/' + encodeURIComponent(deleteArchiveId) + '/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ confirm_name: deleteArchiveInput.value }),
    });
  } catch {
    showDeleteArchiveError(deleteArchiveSlowReply);
    releaseDeleteArchiveSubmit();
    return;
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    showDeleteArchiveError(deleteArchiveSlowReply);
    releaseDeleteArchiveSubmit();
    return;
  }
  if (payload && payload.success && payload.redirect) {
    window.location.assign(payload.redirect);
    return;
  }
  showDeleteArchiveError((payload && payload.error) || deleteArchiveSlowReply);
  releaseDeleteArchiveSubmit();
});

const restoreArchiveModal = document.getElementById("restoreArchiveModal");
const restoreArchiveTitle = document.getElementById("restoreArchiveTitle");
const restoreArchiveSubmit = document.getElementById("restoreArchiveSubmitBtn");
const restoreArchiveCancel = document.getElementById("restoreArchiveModalCancel");
const restoreArchiveError = document.getElementById("restoreArchiveError");
const restoreAlreadyRestored = "This archive has already been restored.";
const restoreNetworkError = "We couldn't confirm the result. Reload this page to see whether the class was restored.";
let restoreArchiveBusy = false;
let restoreArchiveId = "";
let restoreReloadOnClose = false;

function showRestoreArchiveError(message) {
  if (!restoreArchiveError) return;
  restoreArchiveError.textContent = message;
  restoreArchiveError.classList.remove("hidden");
}

function releaseRestoreArchiveSubmit() {
  restoreArchiveBusy = false;
  if (restoreArchiveCancel) restoreArchiveCancel.disabled = false;
  if (restoreArchiveSubmit) {
    restoreArchiveSubmit.disabled = false;
    restoreArchiveSubmit.textContent = "Restore";
  }
}

function resetRestoreArchiveModal() {
  restoreArchiveBusy = false;
  restoreArchiveId = "";
  restoreReloadOnClose = false;
  if (restoreArchiveTitle) restoreArchiveTitle.textContent = "";
  if (restoreArchiveCancel) restoreArchiveCancel.disabled = false;
  if (restoreArchiveError) {
    restoreArchiveError.textContent = "";
    restoreArchiveError.classList.add("hidden");
  }
  if (restoreArchiveSubmit) {
    restoreArchiveSubmit.disabled = false;
    restoreArchiveSubmit.textContent = "Restore";
  }
}

function closeRestoreArchiveModal() {
  if (restoreArchiveBusy || !restoreArchiveModal) return;
  const reload = restoreReloadOnClose;
  restoreArchiveModal.classList.add("hidden");
  resetRestoreArchiveModal();
  if (reload) window.location.reload();
}

function openRestoreArchiveModal(archiveId, className) {
  if (!restoreArchiveModal || restoreArchiveBusy) return;
  resetRestoreArchiveModal();
  restoreArchiveId = archiveId;
  if (restoreArchiveTitle) {
    restoreArchiveTitle.textContent = "Restore " + className + "?";
  }
  restoreArchiveModal.classList.remove("hidden");
}

document.querySelectorAll(".restore-archive-btn").forEach((btn) => {
  btn.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const archiveId = btn.getAttribute("data-archive-id") || "";
    const className = btn.getAttribute("data-class-name") || "";
    if (archiveId) openRestoreArchiveModal(archiveId, className);
  });
});

restoreArchiveCancel?.addEventListener("click", closeRestoreArchiveModal);
restoreArchiveModal?.addEventListener("click", (event) => {
  if (event.target === restoreArchiveModal) closeRestoreArchiveModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!restoreArchiveModal || restoreArchiveModal.classList.contains("hidden")) return;
  event.preventDefault();
  closeRestoreArchiveModal();
});

restoreArchiveSubmit?.addEventListener("click", async () => {
  if (restoreArchiveBusy || !restoreArchiveId || !restoreArchiveSubmit) return;
  restoreArchiveBusy = true;
  restoreArchiveSubmit.disabled = true;
  restoreArchiveSubmit.textContent = "Restoring...";
  if (restoreArchiveCancel) restoreArchiveCancel.disabled = true;
  if (restoreArchiveError) restoreArchiveError.classList.add("hidden");
  let response;
  try {
    response = await fetch("/archived-classes/" + encodeURIComponent(restoreArchiveId) + "/restore", {
      method: "POST",
    });
  } catch {
    restoreReloadOnClose = true;
    showRestoreArchiveError(restoreNetworkError);
    releaseRestoreArchiveSubmit();
    return;
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    restoreReloadOnClose = true;
    showRestoreArchiveError(restoreNetworkError);
    releaseRestoreArchiveSubmit();
    return;
  }
  if (payload && payload.success && payload.redirect) {
    window.location.assign(payload.redirect);
    return;
  }
  const restoreError = payload && payload.error;
  if (response.status === 409 && restoreError === restoreAlreadyRestored) {
    restoreReloadOnClose = true;
    showRestoreArchiveError(restoreError);
    restoreArchiveBusy = false;
    if (restoreArchiveCancel) restoreArchiveCancel.disabled = false;
    if (restoreArchiveSubmit) {
      restoreArchiveSubmit.disabled = true;
      restoreArchiveSubmit.textContent = "Restore";
    }
    return;
  }
  if (!restoreError) restoreReloadOnClose = true;
  showRestoreArchiveError(restoreError || restoreNetworkError);
  releaseRestoreArchiveSubmit();
});

document.getElementById('deleteArchiveForm').addEventListener('submit', function (event) {
  event.preventDefault();
});
