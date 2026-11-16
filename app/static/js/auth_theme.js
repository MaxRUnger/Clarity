
        (function() {
            const stored = localStorage.getItem('theme');
            const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
            if (stored === 'dark' || (!stored && prefersDark)) {
                document.documentElement.classList.add('dark');
            } else {
                document.documentElement.classList.remove('dark');
            }
            document.getElementById('themeToggle')?.addEventListener('click', function() {
                document.documentElement.classList.toggle('dark');
                localStorage.setItem('theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light');
            });
            var privacyBack = document.getElementById('privacyBack');
            if (privacyBack) {
                privacyBack.addEventListener('click', function (event) {
                    if (window.history.length > 1) {
                        event.preventDefault();
                        window.history.back();
                    }
                });
            }
        })();
    