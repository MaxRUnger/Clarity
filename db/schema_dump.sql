SELECT jsonb_pretty(to_jsonb(dump_row)) AS dump
FROM (
    SELECT
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table")
            FROM (
                SELECT c.relrowsecurity AS rls,
                       c.relname AS "table"
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
            ) x
        ) AS tables,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.position)
            FROM (
                SELECT c.udt_name AS udt,
                       c.data_type AS type,
                       c.table_name AS "table",
                       c.column_name AS "column",
                       c.column_default AS "default",
                       c.is_nullable AS nullable,
                       c.ordinal_position AS position
                FROM information_schema.columns c
                WHERE c.table_schema = 'public'
            ) x
        ) AS columns,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.name)
            FROM (
                SELECT i.indexname AS name,
                       i.tablename AS "table",
                       i.indexdef AS definition
                FROM pg_indexes i
                WHERE i.schemaname = 'public'
            ) x
        ) AS indexes,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.name)
            FROM (
                SELECT pol.policyname AS name,
                       pol.tablename AS "table",
                       pol.permissive,
                       pol.roles,
                       pol.cmd,
                       pol.qual,
                       pol.with_check
                FROM pg_policies pol
                WHERE pol.schemaname = 'public'
            ) x
        ) AS policies,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x.function)
            FROM (
                SELECT p.proname AS function,
                       pg_get_functiondef(p.oid) AS definition,
                       p.prosecdef AS security_definer
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.prokind = 'f'
            ) x
        ) AS functions,
        (
            SELECT json_agg(e.extname ORDER BY e.extname)
            FROM pg_extension e
        ) AS extensions,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.name)
            FROM (
                SELECT con.conname AS name,
                       con.contype AS type,
                       rel.relname AS "table",
                       pg_get_constraintdef(con.oid) AS definition
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = rel.relnamespace
                WHERE n.nspname = 'public'
                  AND con.contype IN ('c', 'f', 'p', 'u')
            ) x
        ) AS constraints,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.grantee, x.privilege)
            FROM (
                SELECT g.table_name AS "table",
                       g.grantee,
                       g.privilege_type AS privilege
                FROM information_schema.role_table_grants g
                WHERE g.table_schema = 'public'
                  AND g.grantee IN ('PUBLIC', 'anon', 'authenticated')
            ) x
        ) AS table_grants,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.name)
            FROM (
                SELECT t.tgname AS name,
                       c.relname AS "table",
                       pg_get_triggerdef(t.oid) AS definition
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND NOT t.tgisinternal
            ) x
        ) AS table_triggers,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x.function, x.grantee, x.privilege)
            FROM (
                SELECT r.routine_name AS function,
                       r.grantee,
                       r.privilege_type AS privilege
                FROM information_schema.role_routine_grants r
                WHERE r.routine_schema = 'public'
                  AND r.grantee IN ('PUBLIC', 'anon', 'authenticated')
            ) x
        ) AS function_grants,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x.id)
            FROM (
                SELECT b.id,
                       b.public,
                       b.file_size_limit,
                       b.allowed_mime_types
                FROM storage.buckets b
            ) x
        ) AS storage_buckets,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x."table", x.name)
            FROM (
                SELECT pol.policyname AS name,
                       pol.tablename AS "table",
                       pol.cmd,
                       pol.qual,
                       pol.with_check
                FROM pg_policies pol
                WHERE pol.schemaname = 'storage'
            ) x
        ) AS storage_policies,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x.name)
            FROM (
                SELECT t.tgname AS name,
                       pg_get_triggerdef(t.oid) AS definition
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_proc p ON p.oid = t.tgfoid
                JOIN pg_namespace pn ON pn.oid = p.pronamespace
                WHERE n.nspname = 'auth'
                  AND c.relname = 'users'
                  AND NOT t.tgisinternal
                  AND pn.nspname = 'public'
            ) x
        ) AS auth_user_triggers,
        (
            SELECT json_agg(row_to_json(x) ORDER BY x.owner, x.type)
            FROM (
                SELECT d.defaclacl::text AS acl,
                       d.defaclobjtype AS type,
                       r.rolname AS owner
                FROM pg_default_acl d
                JOIN pg_namespace n ON n.oid = d.defaclnamespace
                JOIN pg_roles r ON r.oid = d.defaclrole
                WHERE n.nspname = 'public'
            ) x
        ) AS default_privileges
) dump_row;
