# Database

## Files

`db/001_baseline.sql` builds the Clarity schema on an empty Supabase project. Run it once, as the postgres role, in the SQL editor. The header states that this file has not yet been run against a database.

`db/schema_dump.sql` is read-only. It returns one column named `dump`.

`db/schema_snapshot.csv` is the saved export of that query. You keep this file in the repository. A schema change refreshes it in the same commit as the new SQL file.

`db/verify.sql` is read-only. Run it in the SQL editor against any Clarity database. Each row has `check_name` and `passed`. The last row is `ALL_PASSED`.

`scripts/compare_schema_dumps.py` reads two dump files, prints every difference, and exits 1 when there is a difference.

## Check for drift

Run `db/schema_dump.sql` in the SQL editor and export the result as CSV. Then run:

```text
python scripts/compare_schema_dumps.py db/schema_snapshot.csv new_export.csv
```

Exit code 0 means no drift.

## Run verify

Paste `db/verify.sql` into the Supabase SQL editor and run it. `ALL_PASSED` is true only when every check passed.

## Rebuild a database

The baseline has never been run. The first time it is needed, test it on a scratch project.

1. In that scratch project, as the postgres role, run `db/001_baseline.sql` once.
2. Create an invite code, then create the first instructor. Signup rejects a new account unless a live invite exists. The signup trigger then inserts the instructor profile.

Pick a code and insert its hash. Replace `choose-a-code` with the code you will type on the signup page:

```sql
INSERT INTO public.signup_invites (code_hash, label)
VALUES (encode(sha256(convert_to('choose-a-code', 'utf8')), 'hex'), 'first instructor');
```

Sign up in the app with that same code. The database creates the instructor profile and consumes one use of the invite.

## Rules

Every new function needs an explicit `REVOKE ALL ON FUNCTION public.function_name(argument_types) FROM PUBLIC` and a `GRANT EXECUTE ON FUNCTION public.function_name(argument_types) TO service_role` only.

Every new table needs row level security enabled and no grants to `anon` or `authenticated`.

A schema change is saved as a new numbered SQL file in `db/`, and `db/schema_snapshot.csv` is refreshed in the same commit.

## Removing an instructor

Delete or archive that instructor's classes before removing the instructor profile. Those foreign keys are `ON DELETE RESTRICT`.
