
(function () {
  var params = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  var tokenHash = params.get("token_hash") || "";
  var email = params.get("email") || "";
  history.replaceState(null, "", location.pathname + location.search);

  var passwordInput = document.getElementById("password");
  var liveHint = document.getElementById("liveHint");
  var encoder = new TextEncoder();

  function liveMessage(value) {
    if (value.length < 12) return "Use at least 12 characters.";
    if (encoder.encode(value).length > 72) return "Use at most 72 characters.";
    if (!/[a-z]/.test(value)) return "Use at least one lowercase letter.";
    if (!/[A-Z]/.test(value)) return "Use at least one uppercase letter.";
    if (!/[0-9]/.test(value)) return "Use at least one digit.";
    if (!/[^A-Za-z0-9\s]/.test(value)) return "Use at least one symbol such as ! or #.";
    return "";
  }

  passwordInput.addEventListener("input", function () {
    var msg = liveMessage(passwordInput.value);
    if (msg) {
      liveHint.textContent = msg;
      liveHint.classList.remove("hidden");
    } else {
      liveHint.classList.add("hidden");
    }
  });

  window.submitReset = function (event) {
    event.preventDefault();
    var errorBanner = document.getElementById("errorBanner");
    var errorText = document.getElementById("errorText");
    errorBanner.classList.add("hidden");
    if (!tokenHash || !email) {
      errorText.textContent = "This reset link is invalid or expired.";
      errorBanner.classList.remove("hidden");
      return;
    }
    var password = passwordInput.value;
    var btn = event.target.querySelector("button[type=submit]");
    var orig = btn.innerHTML;
    btn.disabled = true;
    fetch("/api/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: email,
        token_hash: tokenHash,
        password: password
      })
    })
    .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
    .then(function (res) {
      if (res.body && res.body.success && res.body.redirect) {
        window.location.href = res.body.redirect;
        return;
      }
      btn.disabled = false;
      btn.innerHTML = orig;
      errorText.textContent = (res.body && (res.body.message || res.body.error)) || "Request failed. Please try again.";
      errorBanner.classList.remove("hidden");
    })
    .catch(function () {
      btn.disabled = false;
      btn.innerHTML = orig;
      errorText.textContent = "Connection error. Please try again.";
      errorBanner.classList.remove("hidden");
    });
  };
})();

document.getElementById('resetForm').addEventListener('submit', window.submitReset);
