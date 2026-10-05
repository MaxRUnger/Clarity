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

  async function apiRequest(url, options = {}) {
    try {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
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

  // Poll the mobile-upload bridge until the phone has handed a file off, then
  // hand it to the page via `config.onReady(file, statusData)`. The caller is
  // responsible for ending the interval (via `config.stop`) once `onReady`
  // fires; we keep the interval reference returned by `setInterval` so the
  // caller can also `clearInterval` directly if they prefer.
  function startMobileCsvPoll(config) {
    const classId = String(config.classId || "").trim();
    const token = String(config.token || "").trim();
    const intervalMs = Number(config.intervalMs || 2000);
    if (!classId || !token) return null;

    return setInterval(async () => {
      try {
        const statusUrl =
          "/api/class/" + encodeURIComponent(classId) + "/mobile-upload-status/" + encodeURIComponent(token);
        const statusRes = await fetch(statusUrl, { credentials: "same-origin" });
        if (!statusRes.ok) return;
        const statusData = await statusRes.json().catch(() => ({}));
        if (!statusData.success || !statusData.ready) return;

        if (typeof config.stop === "function") config.stop();

        const fileUrl =
          "/api/class/" + encodeURIComponent(classId) + "/mobile-upload-file/" + encodeURIComponent(token);
        const fileRes = await fetch(fileUrl, { credentials: "same-origin" });
        if (!fileRes.ok) {
          if (typeof config.onError === "function") {
            config.onError("Could not retrieve the file from your phone. Try scanning the QR code again.");
          }
          return;
        }

        const blob = await fileRes.blob();
        const name = statusData.filename || "outcomes.csv";
        const file = new File([blob], name, { type: blob.type || "text/csv" });

        if (typeof config.onReady === "function") {
          await config.onReady(file, statusData);
        }
      } catch (err) {
        if (typeof config.onCatch === "function") config.onCatch(err);
      }
    }, intervalMs);
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

  window.ClarityUI = {
    escapeHtml,
    apiRequest,
    applyInstructorModeVisibility,
    startMobileCsvPoll,
    debounce,
  };
})();

