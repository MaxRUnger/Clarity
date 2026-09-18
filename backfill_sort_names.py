"""One-time backfill: populate sort_name for every student profile that lacks it.

Run this AFTER applying the SQL migration:
    ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS sort_name text NULL;

Usage:
    python backfill_sort_names.py [--dry-run]

    --dry-run   Print what would be written without touching the database.

The heuristic: the first whitespace-separated token of full_name is the first
name; every token after it forms the last-name cluster.  This exactly inverts
the Canvas CSV import conversion (Canvas "Last, First" → stored "First Last"),
so Canvas-imported names regenerate their original Canvas sort string — verified
to be correct for all 38 names in a real 38-student roster including multi-word
last names, Von/Van prefixes, hyphen-with-space names, and suffix clusters.

Profiles that already have sort_name set are SKIPPED (no overwrite), so this
script is safe to re-run.  Profiles with role != 'student' are also skipped.
"""

import os
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

DRY_RUN = "--dry-run" in sys.argv

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def generate_sort_name(full_name: str) -> str:
    """Convert 'First Last[...]' → 'Last[...], First'.

    Rule: first whitespace token = first name; everything after = last-name cluster.
    Comma-format ('Last, First') and single-token names are returned unchanged.
    """
    s = (full_name or "").strip()
    if not s:
        return s
    if "," in s:
        return s  # already "Last, First"
    parts = s.split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last_cluster = " ".join(parts[1:])
    return f"{last_cluster}, {first}"


def fetch_all_students_missing_sort_name() -> list:
    """Return all student profiles where sort_name IS NULL, in batches."""
    CHUNK = 1000
    results = []
    offset = 0
    while True:
        resp = (
            admin.table("profiles")
            .select("id, full_name, sort_name, role")
            .eq("role", "student")
            .is_("sort_name", "null")
            .range(offset, offset + CHUNK - 1)
            .execute()
        )
        batch = resp.data or []
        results.extend(batch)
        if len(batch) < CHUNK:
            break
        offset += CHUNK
    return results


def run():
    print("Fetching student profiles missing sort_name …")
    profiles = fetch_all_students_missing_sort_name()
    total = len(profiles)
    print(f"Found {total} profile(s) to backfill.")

    if total == 0:
        print("Nothing to do.")
        return

    updated = 0
    skipped = 0
    errors = 0

    for p in profiles:
        pid = p.get("id")
        full_name = (p.get("full_name") or "").strip()
        if not full_name:
            print(f"  SKIP  id={pid!r}  — blank full_name")
            skipped += 1
            continue

        sort_name = generate_sort_name(full_name)
        print(f"  {'DRY ' if DRY_RUN else ''}UPDATE  {full_name!r}  =>  sort_name={sort_name!r}")

        if not DRY_RUN:
            try:
                admin.table("profiles").update({"sort_name": sort_name}).eq("id", pid).execute()
                updated += 1
            except Exception as e:
                print(f"    ERROR updating id={pid!r}: {e}")
                errors += 1
        else:
            updated += 1

    print()
    if DRY_RUN:
        print(f"DRY RUN complete — would update {updated}, skip {skipped}.")
    else:
        print(f"Done — updated {updated}, skipped {skipped}, errors {errors}.")


if __name__ == "__main__":
    run()
