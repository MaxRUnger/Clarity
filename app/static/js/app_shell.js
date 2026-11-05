
        (function() {
            const stored = localStorage.getItem('theme');
            const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
            if (stored === 'dark' || (!stored && prefersDark)) {
                document.documentElement.classList.add('dark');
            } else {
                document.documentElement.classList.remove('dark');
            }
            const themeBtn = document.getElementById('themeBtn');
            const themeLabel = document.getElementById('themeLabel');
            themeBtn?.addEventListener('click', function() {
                const isDark = document.documentElement.classList.toggle('dark');
                localStorage.setItem('theme', isDark ? 'dark' : 'light');
                themeLabel.textContent = isDark ? 'Light Mode' : 'Dark Mode';
            });
            const isDark = document.documentElement.classList.contains('dark');
            if (themeLabel) themeLabel.textContent = isDark ? 'Light Mode' : 'Dark Mode';
            const menuBtn = document.getElementById('menuBtn');
            const menuDropdown = document.getElementById('menuDropdown');
            menuBtn?.addEventListener('click', function(e) {
                e.stopPropagation();
                menuDropdown?.classList.toggle('hidden');
            });
            document.addEventListener('click', function() {
                menuDropdown?.classList.add('hidden');
            });
        })();

        function copyTextShowFeedback(button) {
            if (!button) return;
            var label = button.getAttribute('data-label-copied') || 'Copied';
            var prev = button.getAttribute('aria-label');
            button.setAttribute('aria-label', label);
            clearTimeout(button._copyResetTimer);
            button._copyResetTimer = setTimeout(function () {
                if (prev) button.setAttribute('aria-label', prev);
            }, 1600);
        }

        /** Read email from data-email (avoids broken strings in inline onclick). */
        function copyEmailFromBtn(button) {
            if (!button) return;
            var text = button.getAttribute('data-email');
            if (text == null) text = '';
            copyTextToClipboard(String(text), button);
        }

        // The Async Clipboard API is gated by the browser to "secure contexts"
        // only (https, plus a small allowlist of dev hosts). On a LAN dev
        // server, http://192.168.x.x is *not* a secure context and the API
        // silently rejects, so we short-circuit and fall through to the
        // legacy execCommand path below instead of showing a copy that did
        // nothing.
        function copyTextClipboardApi(text, button) {
            if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') return;
            var h = (location.hostname || '').toLowerCase();
            var allow = window.isSecureContext || h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h.endsWith('.localhost');
            if (!allow) return;
            navigator.clipboard.writeText(text).then(function () {
                copyTextShowFeedback(button);
            }).catch(function () {});
        }

        // Fallback path. Browsers will only honor `document.execCommand('copy')`
        // when it runs synchronously inside a user-initiated event handler, so
        // we deliberately try several DOM strategies in the same call stack
        // and bail on the first one that works rather than awaiting any
        // promise. The four `tryOne` variants exist because different browser
        // / OS combos refuse one or another (Safari hates contentEditable
        // divs, mobile Chrome hates off-screen textareas with width:1px, etc).
        function copyTextViaExecCommand(text) {
            text = String(text || '');
            if (!text) return false;

            function tryOne(makeEl, selectFn) {
                var el = makeEl();
                document.body.appendChild(el);
                var ok = false;
                try {
                    selectFn(el);
                    ok = document.execCommand('copy');
                } catch {}
                if (el.parentNode) el.parentNode.removeChild(el);
                return ok;
            }

            var style = 'position:fixed;top:0;left:0;width:24px;height:24px;padding:4px;margin:0;border:1px solid transparent;outline:0;opacity:0.02;z-index:2147483647;font-size:12px;line-height:1.2;';

            if (tryOne(function () {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = style;
                return ta;
            }, function (el) {
                el.focus();
                el.select();
                el.setSelectionRange(0, text.length);
            })) return true;

            if (tryOne(function () {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = style;
                return ta;
            }, function (el) {
                el.focus();
                el.select();
            })) return true;

            if (tryOne(function () {
                var div = document.createElement('div');
                div.contentEditable = 'true';
                div.textContent = text;
                div.style.cssText = style + 'white-space:pre-wrap;overflow:hidden;';
                return div;
            }, function (el) {
                el.focus();
                var r = document.createRange();
                r.selectNodeContents(el);
                var s = window.getSelection();
                if (s) {
                    s.removeAllRanges();
                    s.addRange(r);
                }
            })) return true;

            if (tryOne(function () {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;padding:0;margin:0;border:0;opacity:0;';
                return ta;
            }, function (el) {
                el.focus();
                el.select();
            })) return true;

            return false;
        }

        function copyTextToClipboard(text, button) {
            text = String(text == null ? '' : text);
            if (!text) return;

            if (copyTextViaExecCommand(text)) {
                copyTextShowFeedback(button);
                return;
            }
            copyTextClipboardApi(text, button);
        }

        // Add CSRF token to mutating fetch requests and regular forms.
        (function setupCsrfClientHelpers() {
            function csrfToken() {
                var m = document.querySelector('meta[name="csrf-token"]');
                return m ? String(m.getAttribute('content') || '') : '';
            }

            function ensureCsrfOnForm(form) {
                if (!form || form.tagName !== 'FORM') return;
                var method = String(form.getAttribute('method') || 'GET').toUpperCase();
                if (['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) < 0) return;
                if (form.querySelector('input[name="csrf_token"]')) return;
                var token = csrfToken();
                if (!token) return;
                var hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = 'csrf_token';
                hidden.value = token;
                form.appendChild(hidden);
            }

            var originalFetch = window.fetch;
            window.fetch = function (input, init) {
                init = init || {};
                init.headers = init.headers || {};
                var token = csrfToken();
                var method = String((init.method || 'GET')).toUpperCase();
                if (token && ['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) >= 0) {
                    if (init.headers instanceof Headers) {
                        if (!init.headers.has('X-CSRF-Token')) init.headers.set('X-CSRF-Token', token);
                    } else if (!init.headers['X-CSRF-Token']) {
                        init.headers['X-CSRF-Token'] = token;
                    }
                }
                return originalFetch(input, init);
            };

            // Native form.submit() does not fire the submit event, so also
            // inject CSRF for programmatic submits (e.g. Create Objective).
            var originalFormSubmit = HTMLFormElement.prototype.submit;
            HTMLFormElement.prototype.submit = function () {
                ensureCsrfOnForm(this);
                return originalFormSubmit.call(this);
            };

            document.addEventListener('submit', function (evt) {
                ensureCsrfOnForm(evt.target);
            }, true);
        })();

        // Print helper: remove standalone dash placeholders so paper copies
        // have blank cells teachers can fill by hand.
        (function setupPrintPlaceholderCleanup() {
            var originals = [];
            var DASH_ONLY = /^[-–—]\s*$/;

            function clearDashPlaceholdersForPrint() {
                if (originals.length) return;
                var root = document.querySelector('main') || document.body;
                if (!root) return;
                var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                var node;
                while ((node = walker.nextNode())) {
                    var value = String(node.nodeValue || '');
                    if (!value.trim()) continue;
                    if (!DASH_ONLY.test(value.trim())) continue;
                    var parent = node.parentElement;
                    if (!parent) continue;
                    var tag = (parent.tagName || '').toUpperCase();
                    if (!['TD', 'TH', 'SPAN', 'DIV', 'P'].includes(tag)) continue;
                    originals.push([node, value]);
                    node.nodeValue = value.replace(/[-–—]/g, ' ');
                }
            }

            function restoreDashPlaceholdersAfterPrint() {
                for (var i = 0; i < originals.length; i++) {
                    originals[i][0].nodeValue = originals[i][1];
                }
                originals = [];
            }

            window.addEventListener('beforeprint', clearDashPlaceholdersForPrint);
            window.addEventListener('afterprint', restoreDashPlaceholdersAfterPrint);
        })();
    
document.addEventListener('click', function (event) {
  var btn = event.target && event.target.closest ? event.target.closest('[data-copy-email-btn]') : null;
  if (!btn || btn.onclick) return;
  event.stopPropagation();
  copyEmailFromBtn(btn);
  event.preventDefault();
}, true);
