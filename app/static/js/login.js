
function submitLogin(event) {
  event.preventDefault();
  const btn = event.target.querySelector('button[type=submit]');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<svg class="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg> Signing in...`;

  fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: document.getElementById('email').value,
      password: document.getElementById('password').value
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.success) {
      btn.innerHTML = `<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg> Refreshing...`;
      window.location.href = res.redirect;
    } else {
      btn.disabled = false;
      btn.innerHTML = orig;
      alert(res.message || 'Login failed');
    }
  })
  .catch(() => {
    btn.disabled = false;
    btn.innerHTML = orig;
    alert('Connection error. Please try again.');
  });
}

document.getElementById('loginForm').addEventListener('submit', submitLogin);
