
        // CSRF auto-inject. Mirrors the wrapper in app_base.html so pages that
        // extend auth_base.html (login, signup, instructor_select_class) can
        // POST to state-changing endpoints (e.g. /api/set-instructor-mode for
        // the Mark / Shelbi toggle) without the before_request CSRF check
        // returning 403 and silently breaking the UI.
        (function setupCsrfFetchInterceptor() {
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
            // inject CSRF for programmatic submits.
            var originalFormSubmit = HTMLFormElement.prototype.submit;
            HTMLFormElement.prototype.submit = function () {
                ensureCsrfOnForm(this);
                return originalFormSubmit.call(this);
            };

            document.addEventListener('submit', function (evt) {
                ensureCsrfOnForm(evt.target);
            }, true);
        })();
    