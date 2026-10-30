-- Read-only. Run in the Supabase SQL editor against any Clarity database.
-- One row per check, then ALL_PASSED. passed is true only when the check holds.

WITH checks AS (
    SELECT 1 AS ord,
           'rls_enabled_on_every_public_table'::text AS check_name,
           NOT EXISTS (
               SELECT 1
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public'
                 AND c.relkind = 'r'
                 AND NOT c.relrowsecurity
           ) AS passed
    UNION ALL
    SELECT 2,
           'zero_policies_in_public_and_on_storage_objects',
           NOT EXISTS (
               SELECT 1
               FROM pg_policies pol
               WHERE pol.schemaname = 'public'
                  OR (pol.schemaname = 'storage' AND pol.tablename = 'objects')
           )
    UNION ALL
    SELECT 3,
           'anon_and_authenticated_have_no_public_privileges',
           NOT EXISTS (
               SELECT 1
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               CROSS JOIN LATERAL aclexplode(
                   COALESCE(c.relacl, acldefault((CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END)::"char", c.relowner))
               ) AS acl(grantor, grantee, privilege_type, is_grantable)
               JOIN pg_roles r ON r.oid = acl.grantee
               WHERE n.nspname = 'public'
                 AND c.relkind IN ('r', 'S')
                 AND r.rolname IN ('anon', 'authenticated')
               UNION ALL
               SELECT 1
               FROM pg_attribute a
               JOIN pg_class c ON c.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               CROSS JOIN LATERAL aclexplode(a.attacl)
                   AS acl(grantor, grantee, privilege_type, is_grantable)
               JOIN pg_roles r ON r.oid = acl.grantee
               WHERE n.nspname = 'public'
                 AND c.relkind = 'r'
                 AND a.attacl IS NOT NULL
                 AND r.rolname IN ('anon', 'authenticated')
               UNION ALL
               SELECT 1
               FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
               CROSS JOIN LATERAL aclexplode(
                   COALESCE(p.proacl, acldefault('f', p.proowner))
               ) AS acl(grantor, grantee, privilege_type, is_grantable)
               JOIN pg_roles r ON r.oid = acl.grantee
               WHERE n.nspname = 'public'
                 AND p.prokind = 'f'
                 AND r.rolname IN ('anon', 'authenticated')
           )
    UNION ALL
    SELECT 4,
           'no_public_function_executable_by_public_anon_or_authenticated',
           NOT EXISTS (
               SELECT 1
               FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
               CROSS JOIN LATERAL aclexplode(
                   COALESCE(p.proacl, acldefault('f', p.proowner))
               ) AS acl(grantor, grantee, privilege_type, is_grantable)
               LEFT JOIN pg_roles r ON r.oid = acl.grantee
               WHERE n.nspname = 'public'
                 AND p.prokind = 'f'
                 AND acl.privilege_type = 'EXECUTE'
                 AND (acl.grantee = 0 OR r.rolname IN ('anon', 'authenticated'))
           )
    UNION ALL
    SELECT 5,
           'profile_foreign_keys_are_restrict',
           (
               SELECT count(*) = 7
               FROM pg_constraint con
               WHERE con.contype = 'f'
                 AND con.confdeltype = 'r'
                 AND con.conname IN (
                     'classes_instructor_id_fkey',
                     'enrollments_student_id_fkey',
                     'free_passes_student_id_fkey',
                     'grades_student_id_fkey',
                     'homework_scores_student_id_fkey',
                     'revision_reminders_sent_sent_by_fkey',
                     'revision_reminders_sent_student_id_fkey'
                 )
           )
    UNION ALL
    SELECT 6,
           'app_sessions_user_id_fkey_cascades',
           EXISTS (
               SELECT 1
               FROM pg_constraint con
               WHERE con.conname = 'app_sessions_user_id_fkey'
                 AND con.contype = 'f'
                 AND con.confdeltype = 'c'
           )
    UNION ALL
    SELECT 7,
           'grades_last_modified_by_fkey_is_set_null',
           EXISTS (
               SELECT 1
               FROM pg_constraint con
               WHERE con.conname = 'grades_last_modified_by_fkey'
                 AND con.contype = 'f'
                 AND con.confdeltype = 'n'
           )
    UNION ALL
    SELECT 8,
           'class_archives_bucket_exists_and_is_private',
           EXISTS (
               SELECT 1
               FROM storage.buckets b
               WHERE b.id = 'class-archives'
                 AND b.public = false
           )
    UNION ALL
    SELECT 9,
           'auth_user_triggers_exist',
           (
               SELECT count(*) = 2
               FROM pg_trigger t
               JOIN pg_class c ON c.oid = t.tgrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'auth'
                 AND c.relname = 'users'
                 AND NOT t.tgisinternal
                 AND t.tgname IN ('enforce_invite_code', 'on_auth_user_created')
           )
    UNION ALL
    SELECT 10,
           'claritycode_absent_from_public_functions',
           NOT EXISTS (
               SELECT 1
               FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
               JOIN pg_language l ON l.oid = p.prolang
               WHERE n.nspname = 'public'
                 AND p.prokind = 'f'
                 AND l.lanname IN ('sql', 'plpgsql')
                 AND pg_get_functiondef(p.oid) LIKE '%ClarityCode%'
           )
)
SELECT check_name, passed
FROM (
    SELECT ord, check_name, passed
    FROM checks
    UNION ALL
    SELECT 11, 'ALL_PASSED', COALESCE(bool_and(passed), false)
    FROM checks
) results
ORDER BY ord;
