
function submitForgot(event) {
  event.preventDefault();
  var errorBanner = document.getElementById("errorBanner");
  var errorText = document.getElementById("errorText");
  var sentNotice = document.getElementById("sentNotice");
  errorBanner.classList.add("hidden");
  sentNotice.classList.add("hidden");
  var btn = event.target.querySelector("button[type=submit]");
  var orig = btn.innerHTML;
  btn.disabled = true;
  fetch("/api/forgot-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: document.getElementById("email").value })
  })
  .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
  .then(function (res) {
    btn.disabled = false;
    btn.innerHTML = orig;
    if (res.status === 200 && res.body && res.body.success) {
      sentNotice.textContent = res.body.message || "If an instructor account exists for that address, a reset message is on its way.";
      sentNotice.classList.remove("hidden");
      return;
    }
    errorText.textContent = (res.body && (res.body.message || res.body.error)) || "Request failed. Please try again.";
    errorBanner.classList.remove("hidden");
  })
  .catch(function () {
    btn.disabled = false;
    btn.innerHTML = orig;
    errorText.textContent = "Connection error. Please try again.";
    errorBanner.classList.remove("hidden");
  });
}

document.getElementById('forgotForm').addEventListener('submit', submitForgot);
