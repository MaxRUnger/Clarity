import sys
from unittest.mock import MagicMock

_mock_supabase = MagicMock()
_mock_supabase_admin = MagicMock()

sys.modules.setdefault('app.helpers.authentication', MagicMock(
    supabase=_mock_supabase,
    supabase_admin=_mock_supabase_admin,
))
