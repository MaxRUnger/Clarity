// Shared utilities exposed as `window.ClarityUI`. Kept as a single IIFE so
// per-page templates can rely on the namespace without polluting globals or
// risking duplicate symbol definitions when templates inline their own JS.
(() => {
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  let sessionExpiryHandled = false;

  function handleSessionExpired() {
    if (sessionExpiryHandled) return;
    sessionExpiryHandled = true;
    window.alert("Your session expired. Please sign in again.");
    window.location.assign("/login");
  }

  async function apiRequest(url, options = {}) {
    try {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
      if (res.status === 401) {
        handleSessionExpired();
        // The browser is going to /login. This promise stays pending so the
        // caller's own failure alert stays unreached.
        return new Promise(() => {});
      }
      if (!res.ok || data.success === false) {
        return {
          ok: false,
          status: res.status,
          data,
          errorMessage: data.error || res.statusText || "Request failed",
        };
      }
      return { ok: true, status: res.status, data, errorMessage: "" };
    } catch (err) {
      return {
        ok: false,
        status: 0,
        data: {},
        errorMessage: err && err.message ? err.message : String(err),
      };
    }
  }

  function applyInstructorModeVisibility(mode, selector = ".mark-only", hideMode = "shelbi") {
    const shouldHide = String(mode || "").toLowerCase() === String(hideMode).toLowerCase();
    document.querySelectorAll(selector).forEach((el) => {
      el.style.display = shouldHide ? "none" : "";
    });
  }

  // Trailing-edge debounce. The wrapped fn fires only after the input stops
  // arriving for `waitMs`. We `try/catch` inside the timeout so a thrown
  // error from the user fn doesn't stop future timers from being scheduled.
  function debounce(fn, waitMs) {
    let t = null;
    const w = Math.max(0, Number(waitMs) || 0);
    return function debounced() {
      const ctx = this;
      const args = arguments;
      if (t !== null) clearTimeout(t);
      t = setTimeout(() => {
        t = null;
        try { fn.apply(ctx, args); } catch (e) { console.error(e); }
      }, w);
    };
  }

  function studentGreetingName(fullName) {
    const raw = String(fullName == null ? "" : fullName);
    const comma = raw.indexOf(",");
    if (comma < 0) return "student";
    const given = raw.slice(comma + 1).trim();
    return given || "student";
  }

  function reportEmailFooter(title, className) {
    const who = title == null ? "" : String(title);
    const klass = className == null ? "" : String(className);
    return [
      "Sent by " + who + " via Clarity Grader",
      "Class: " + klass,
      "You are receiving this because you are enrolled in " + klass + ".",
      "This is an automated message. Replies are not monitored. Please contact your instructor directly.",
    ].join("\n");
  }

  let pageScrollLockCount = 0;

  function lockPageScroll() {
    pageScrollLockCount += 1;
    if (pageScrollLockCount === 1) {
      document.documentElement.classList.add("overflow-hidden");
      document.body.classList.add("overflow-hidden");
    }
  }

  function unlockPageScroll() {
    if (pageScrollLockCount === 0) return;
    pageScrollLockCount -= 1;
    if (pageScrollLockCount === 0) {
      document.documentElement.classList.remove("overflow-hidden");
      document.body.classList.remove("overflow-hidden");
    }
  }

  window.ClarityUI = {
    escapeHtml,
    apiRequest,
    handleSessionExpired,
    applyInstructorModeVisibility,
    debounce,
    studentGreetingName,
    reportEmailFooter,
    lockPageScroll,
    unlockPageScroll,
  };
})();

