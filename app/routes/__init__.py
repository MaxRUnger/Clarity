"""HTTP routes and API handlers for Clarity.

Deployment (production)
-----------------------
In-memory structures — request rate limits in app.helpers.rate_limit
(``_rate_limit_events``) and one-time form tokens in ``app.helpers.form_tokens``
(``_used_form_tokens``) —
are **not** shared across Gunicorn/Uvicorn workers. With multiple workers,
rate limits and token replay checks can appear broken or inconsistent.

**Options:** run **one worker**; use a load balancer with **sticky sessions** to
the same worker; or replace these stores with **Redis** (or equivalent) before
scaling horizontally.

If you intentionally run multiple workers in production, set environment
variable ``MULTI_WORKER=1`` to log a one-time warning at startup (see
``create_app`` in ``app/__init__.py``).
"""

ROUTE_MODULES = (
    "app.routes.reports",
    "app.routes.auth",
    "app.routes.students",
    "app.routes.assignments",
    "app.routes.classes",
    "app.routes.archives",
    "app.routes.class_email",
    "app.routes.grading",
)
