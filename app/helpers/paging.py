"""Page PostgREST reads past the server row cap.

One request returns at most PAGE_SIZE rows. Callers that can exceed that
pass a filtered, ordered query builder here. Filters and order must already
be on the builder. Order must be a unique key or later pages can skip or
repeat rows.
"""

from typing import Any, Dict, List

PAGE_SIZE = 1000


def fetch_all_rows(query) -> List[Dict[str, Any]]:
    """Read every row. Each request asks for one inclusive page of PAGE_SIZE."""
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        resp = query.range(start, start + PAGE_SIZE - 1).execute()
        batch = list(resp.data or [])
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE
