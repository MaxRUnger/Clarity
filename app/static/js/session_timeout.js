(function () {
  var configNode = document.getElementById("sessionTimeoutConfig");
  var config = {};
  try {
    config = JSON.parse(configNode && configNode.textContent ? configNode.textContent : "{}");
  } catch (err) {
    config = {};
  }

  var idleMs = Number(config.idle_seconds) * 1000;
  var warningMs = Number(config.warning_seconds) * 1000;
  var keepaliveMs = Number(config.keepalive_seconds) * 1000;
  if (!(idleMs > 0) || !(warningMs >= 0) || !(keepaliveMs >= 0)) {
    return;
  }

  var ACTIVITY_KEY = "clarity.sessionActivity";
  var KEEPALIVE_KEY = "clarity.sessionKeepalive";
  var LOGOUT_KEY = "clarity.sessionLogout";
  var memoryStamps = {};
  var storageOk = true;
  var redirecting = false;
  var logoutAutosaveRunning = false;
  var warningOpen = false;
  var lastActivityWrite = 0;
  var lastKeepAliveAttempt = 0;
  var statusCheckAt = 0;
  var statusCheckInFlight = false;
  var warningOverlay = null;
  var expiryOverlay = null;
  var countdown = null;
  var stayButton = null;
  var channel = null;

  try {
    channel = new BroadcastChannel("clarity-session");
    channel.onmessage = function (event) {
      if (event.data && event.data.type === "logout") {
        redirectToLogout(true);
      }
    };
  } catch (err) {
    channel = null;
  }

  function readStamp(key) {
    if (storageOk) {
      try {
        var value = Number(localStorage.getItem(key));
        if (value > 0) {
          memoryStamps[key] = value;
          return value;
        }
        return 0;
      } catch (err) {
        storageOk = false;
      }
    }
    var remembered = memoryStamps[key];
    return remembered > 0 ? remembered : 0;
  }

  function writeStamp(key, value) {
    memoryStamps[key] = value;
    if (!storageOk) {
      return;
    }
    try {
      localStorage.setItem(key, String(value));
    } catch (err) {
      storageOk = false;
    }
  }

  function formatRemaining(ms) {
    var total = Math.max(0, Math.ceil(ms / 1000));
    var minutes = Math.floor(total / 60);
    var seconds = total % 60;
    var padded = seconds < 10 ? "0" + String(seconds) : String(seconds);
    return String(minutes) + ":" + padded;
  }

  function panel() {
    var box = document.createElement("div");
    box.className = "w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 text-slate-900 shadow-lg dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100";
    return box;
  }

  function makeOverlay() {
    var overlay = document.createElement("div");
    overlay.className = "hidden fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4 print:hidden";
    return overlay;
  }

  function hideWarning() {
    if (!warningOpen) {
      return;
    }
    warningOpen = false;
    warningOverlay.classList.add("hidden");
    if (window.ClarityUI && typeof window.ClarityUI.unlockPageScroll === "function") {
      window.ClarityUI.unlockPageScroll();
    }
  }

  function showExpiryNotice() {
    hideWarning();
    expiryOverlay.classList.remove("hidden");
    if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === "function") {
      window.ClarityUI.lockPageScroll();
    }
    expiryOverlay.focus();
  }

  function redirectToLogout(serverExpired) {
    if (redirecting) {
      return;
    }
    redirecting = true;
    showExpiryNotice();
    writeStamp(LOGOUT_KEY, Date.now());
    try {
      if (channel) {
        channel.postMessage({ type: "logout" });
      }
    } catch (err) {
      channel = null;
    }
    function finish() {
      logoutAutosaveRunning = false;
      window.location.assign("/logout?reason=inactivity");
    }
    if (serverExpired || typeof window.runAutosave !== "function") {
      finish();
      return;
    }
    logoutAutosaveRunning = true;
    var cutoff = new Promise(function (resolve) {
      window.setTimeout(resolve, 5000);
    });
    try {
      Promise.race([Promise.resolve(window.runAutosave()), cutoff]).then(finish, finish);
    } catch (err) {
      finish();
    }
  }

  function ensureUi() {
    warningOverlay = makeOverlay();
    var dialog = panel();
    dialog.setAttribute("role", "alertdialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "sessionTimeoutTitle");
    var title = document.createElement("h2");
    title.id = "sessionTimeoutTitle";
    title.className = "text-lg font-semibold";
    title.textContent = "Session ending";
    countdown = document.createElement("p");
    countdown.className = "mt-2 text-sm";
    stayButton = document.createElement("button");
    stayButton.type = "button";
    stayButton.className = "mt-5 rounded-xl bg-blue-600 px-4 py-2 text-sm font-medium text-white";
    stayButton.textContent = "Stay signed in";
    stayButton.addEventListener("click", function (event) {
      event.stopPropagation();
      staySignedIn();
    });
    dialog.appendChild(title);
    dialog.appendChild(countdown);
    dialog.appendChild(stayButton);
    warningOverlay.appendChild(dialog);

    expiryOverlay = makeOverlay();
    expiryOverlay.setAttribute("role", "alertdialog");
    expiryOverlay.setAttribute("aria-modal", "true");
    expiryOverlay.tabIndex = -1;
    var expiryPanel = panel();
    var expiryText = document.createElement("p");
    expiryText.textContent = "Your session expired. Signing out.";
    expiryPanel.appendChild(expiryText);
    expiryOverlay.appendChild(expiryPanel);
    expiryOverlay.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
    });

    document.body.appendChild(warningOverlay);
    document.body.appendChild(expiryOverlay);
  }

  function showWarning(remainingMs) {
    countdown.textContent = "You will be signed out in " + formatRemaining(remainingMs) + ".";
    if (warningOpen) {
      return;
    }
    warningOpen = true;
    warningOverlay.classList.remove("hidden");
    if (window.ClarityUI && typeof window.ClarityUI.lockPageScroll === "function") {
      window.ClarityUI.lockPageScroll();
    }
    stayButton.focus();
  }

  function sendKeepAlive(force) {
    var now = Date.now();
    var activity = readStamp(ACTIVITY_KEY);
    var lastSent = readStamp(KEEPALIVE_KEY);
    if (!force) {
      if (now - lastKeepAliveAttempt < Math.min(15000, keepaliveMs)) {
        return;
      }
      if (now - activity >= idleMs) {
        return;
      }
      if (!(activity > lastSent)) {
        return;
      }
      if (keepaliveMs > 0 && now - lastSent < keepaliveMs) {
        return;
      }
    }
    writeStamp(KEEPALIVE_KEY, now);
    if (!force && readStamp(KEEPALIVE_KEY) !== now) {
      return;
    }
    lastKeepAliveAttempt = now;
    window.fetch("/api/session-keepalive", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (res) {
      if (res.status === 401) {
        redirectToLogout(true);
        return;
      }
      if (res.status >= 500) {
        writeStamp(KEEPALIVE_KEY, lastSent);
      }
    }).catch(function () {
      writeStamp(KEEPALIVE_KEY, lastSent);
    });
  }

  function staySignedIn() {
    var now = Date.now();
    lastActivityWrite = now;
    writeStamp(ACTIVITY_KEY, now);
    hideWarning();
    sendKeepAlive(true);
  }

  function evaluate() {
    if (redirecting) {
      return;
    }
    var now = Date.now();
    var activity = readStamp(ACTIVITY_KEY);
    if (!activity) {
      return;
    }
    var remaining = idleMs - (now - activity);
    if (remaining <= 0) {
      redirectToLogout(false);
      return;
    }
    if (remaining <= warningMs) {
      showWarning(remaining);
    } else {
      hideWarning();
    }
    sendKeepAlive(false);
  }

  function noteActivity() {
    var now = Date.now();
    if (now - lastActivityWrite < 1000) {
      return;
    }
    lastActivityWrite = now;
    writeStamp(ACTIVITY_KEY, now);
  }

  function requestUrl(input) {
    if (typeof input === "string") {
      return input;
    }
    if (input && typeof input.url === "string") {
      return input.url;
    }
    return "";
  }

  function isOwnSessionRequest(input) {
    var url = requestUrl(input);
    return url.indexOf("/api/session-status") !== -1 || url.indexOf("/api/session-keepalive") !== -1;
  }

  function checkStatus() {
    if (redirecting || statusCheckInFlight) {
      return;
    }
    var now = Date.now();
    if (now - statusCheckAt < 10000) {
      return;
    }
    statusCheckAt = now;
    statusCheckInFlight = true;
    window.fetch("/api/session-status", {
      credentials: "same-origin",
      cache: "no-store"
    }).then(function (res) {
      statusCheckInFlight = false;
      if (res.status === 401) {
        redirectToLogout(true);
      }
    }).catch(function () {
      statusCheckInFlight = false;
    });
  }

  var previousFetch = window.fetch;
  window.fetch = function (input, init) {
    function settleExpiredFetch(response) {
      if (redirecting) {
        if (logoutAutosaveRunning) {
          return response;
        }
        return new Promise(function () {});
      }
      redirectToLogout(true);
      return new Promise(function () {});
    }

    if (isOwnSessionRequest(input)) {
      return previousFetch.apply(window, arguments);
    }
    return previousFetch.apply(window, arguments).then(function (response) {
      var expired = false;
      if (response && response.redirected) {
        try {
          var path = new URL(response.url, window.location.origin).pathname;
          if (path === "/login") {
            expired = true;
          }
        } catch (err) {
          expired = false;
        }
      }
      if (response && response.status === 401 && typeof response.clone === "function") {
        return response.clone().json().then(function (body) {
          if (body && body.code === "session_expired") {
            return settleExpiredFetch(response);
          }
          return response;
        }).catch(function () {
          return response;
        });
      }
      if (expired) {
        return settleExpiredFetch(response);
      }
      return response;
    });
  };

  ensureUi();

  var started = Date.now();
  writeStamp(ACTIVITY_KEY, started);
  writeStamp(KEEPALIVE_KEY, started);
  lastActivityWrite = started;

  document.addEventListener("keydown", function (event) {
    if (!redirecting) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
  }, true);
  document.addEventListener("keydown", noteActivity);
  document.addEventListener("input", noteActivity);
  document.addEventListener("click", noteActivity);
  document.addEventListener("touchstart", noteActivity);
  document.addEventListener("mousemove", noteActivity);
  document.addEventListener("scroll", noteActivity, true);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      checkStatus();
    }
  });
  window.addEventListener("focus", checkStatus);
  window.addEventListener("online", checkStatus);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      checkStatus();
    }
  });
  window.addEventListener("storage", function (event) {
    if (!storageOk) {
      return;
    }
    if (event.key === LOGOUT_KEY && event.newValue) {
      redirectToLogout(true);
      return;
    }
    if (event.key === ACTIVITY_KEY || event.key === KEEPALIVE_KEY) {
      evaluate();
    }
  });
  window.setInterval(evaluate, 1000);
  evaluate();
})();
