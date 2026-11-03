"""Shared Supabase clients.

Two clients are exposed because Supabase enforces row-level security at the
PostgREST layer:

- ``supabase`` (anon key) is the only client allowed to call ``auth.*``
  endpoints (sign-in, sign-up, password reset). It runs under RLS like any
  end-user, so it can never be used for cross-row admin reads/writes.
- ``supabase_admin`` (service-role key) bypasses RLS and is used for every
  table read/write the server performs on behalf of the authenticated user.
  Authorization is enforced separately in ``app/routes.py`` (see helpers
  like ``_instructor_owns_class``).

Keeping these two clients distinct is what prevents the service-role key
from leaking into the auth path.
"""

import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]
service_key = os.environ["SUPABASE_SERVICE_KEY"]

# Anon client for auth (login/signup)
supabase: Client = create_client(url, key)


def new_anon_client() -> Client:
    """Return a new anon client so auth calls do not share the module session."""
    return create_client(url, key)

# Service role client for database operations (bypasses RLS)
supabase_admin: Client = create_client(url, service_key)
