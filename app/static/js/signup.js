
function showError(msg) {
  document.getElementById('errorText').textContent = msg;
  document.getElementById('errorBanner').classList.remove('hidden');
}

function hideError() {
  document.getElementById('errorBanner').classList.add('hidden');
}

function showConfirmModal(email) {
  document.getElementById('confirmedEmail').textContent = email;
  document.getElementById('confirmModal').classList.remove('hidden');
}

function submitSignup(event) {
  event.preventDefault();
  hideError();

  const password = document.getElementById('password').value;
  const confirm = document.getElementById('confirmPassword').value;
  const errorEl = document.getElementById('passwordError');
  const email = document.getElementById('email').value;

  if (password !== confirm) {
    errorEl.classList.remove('hidden');
    document.getElementById('confirmPassword').classList.add('ring-2', 'ring-red-400');
    return;
  }
  errorEl.classList.add('hidden');
  document.getElementById('confirmPassword').classList.remove('ring-2', 'ring-red-400');

  const btn = document.getElementById('submitBtn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<svg class="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg> Creating account...`;

  fetch('/api/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: document.getElementById('name').value,
      email: email,
      password: password,
      invite_code: (document.getElementById('inviteCode') || {value: ''}).value
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.success) {
      // Account created + session set — show confirmation modal
      showConfirmModal(email);
      btn.innerHTML = `<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg> Account created!`;
    } else {
      btn.disabled = false;
      btn.innerHTML = orig;
      const msg = res.message || '';
      // Rate limit means Supabase still created the account but hit email limit — show modal anyway
      if (msg.toLowerCase().includes('rate limit') || msg.toLowerCase().includes('email rate')) {
        showConfirmModal(email);
      } else if (msg.toLowerCase().includes('already registered') || msg.toLowerCase().includes('already been registered')) {
        showError('An account with this email already exists. Try signing in instead.');
      } else {
        showError(msg || 'Signup failed. Please try again.');
      }
    }
  })
  .catch(() => {
    btn.disabled = false;
    btn.innerHTML = orig;
    showError('Connection error. Please check your internet and try again.');
  });
}

document.getElementById('signupForm').addEventListener('submit', submitSignup);
