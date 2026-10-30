-- Run once, as the postgres role, in the Supabase SQL editor, on an empty project.
-- This file has not yet been run against a database.

BEGIN;

CREATE TABLE public.profiles (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    full_name text NOT NULL,
    role text NOT NULL,
    is_test_student boolean DEFAULT false,
    email text,
    sort_name text,
    email_set_at timestamp with time zone
);

CREATE TABLE public.app_sessions (
    token_hash text NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

CREATE TABLE public.classes (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    instructor_id uuid NOT NULL,
    name text NOT NULL,
    semester text,
    days text,
    created_at timestamp with time zone DEFAULT now(),
    auto_convert_m boolean DEFAULT false,
    min_masteries integer DEFAULT 2,
    is_online boolean NOT NULL DEFAULT false,
    section_number text
);

CREATE TABLE public.archived_classes (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    instructor_id uuid NOT NULL,
    class_name text NOT NULL,
    semester text,
    days text,
    section_number text,
    student_count integer NOT NULL,
    assignment_count integer NOT NULL,
    archived_at timestamp with time zone NOT NULL DEFAULT now(),
    storage_path text NOT NULL,
    size_bytes bigint NOT NULL,
    snapshot_path text,
    restored_at timestamp with time zone,
    restored_class_id uuid
);

CREATE TABLE public.assignments (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    class_id uuid,
    name text NOT NULL,
    homework_group text,
    required_ms integer DEFAULT 2,
    date_returned date,
    revision_due date,
    created_at timestamp with time zone DEFAULT now(),
    enabled_exam_score_columns jsonb NOT NULL DEFAULT '[]'::jsonb,
    assignment_type text NOT NULL DEFAULT 'mastery_opp'::text
);

CREATE TABLE public.enrollments (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    class_id uuid NOT NULL,
    student_id uuid NOT NULL,
    muted boolean DEFAULT false
);

CREATE TABLE public.free_passes (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    student_id uuid NOT NULL,
    class_id uuid NOT NULL,
    passes_used integer DEFAULT 0
);

CREATE TABLE public.homework_scores (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    student_id uuid NOT NULL,
    class_id uuid NOT NULL,
    homework_group text NOT NULL,
    score_pct numeric,
    updated_at timestamp with time zone DEFAULT now(),
    prev_score_pct numeric
);

CREATE TABLE public.learning_objectives (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    class_id uuid NOT NULL,
    vendor_code text NOT NULL,
    name text NOT NULL,
    description text,
    homework_group text,
    display_order integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    required_ms integer DEFAULT 2
);

CREATE TABLE public.assignment_objectives (
    assignment_id uuid NOT NULL,
    learning_objective_id uuid NOT NULL
);

CREATE TABLE public.grades (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    student_id uuid NOT NULL,
    learning_objective_id uuid NOT NULL,
    top_score text,
    second_score text,
    r_deadline date,
    graded_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    assignment_id uuid,
    counts_for_mastery boolean NOT NULL DEFAULT true,
    last_modified_by uuid,
    hw_score_at_entry numeric
);

CREATE TABLE public.revision_reminders_sent (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    class_id uuid NOT NULL,
    student_id uuid NOT NULL,
    assignment_id uuid NOT NULL,
    learning_objective_id uuid NOT NULL,
    revision_due date NOT NULL,
    sent_at timestamp with time zone NOT NULL DEFAULT now(),
    sent_by uuid NOT NULL
);

CREATE TABLE public.archive_restore_stage (
    attempt_id uuid NOT NULL,
    table_name text NOT NULL,
    seq integer NOT NULL,
    row_data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.email_sends (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    instructor_id uuid NOT NULL,
    class_id uuid NOT NULL,
    student_id uuid NOT NULL,
    recipient_hash text NOT NULL,
    kind text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.enrollment_attach_log (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    instructor_id uuid,
    class_id uuid NOT NULL,
    profile_id uuid NOT NULL,
    sheet_name text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.grade_change_log (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    student_id uuid,
    class_id uuid,
    learning_objective_id uuid,
    assignment_id uuid,
    operation text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    changed_by uuid,
    changed_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE public.signup_invites (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    code_hash text NOT NULL,
    label text NOT NULL,
    max_uses integer NOT NULL DEFAULT 1,
    used_count integer NOT NULL DEFAULT 0,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

ALTER TABLE public.app_sessions
    ADD CONSTRAINT app_sessions_pkey
    PRIMARY KEY (token_hash);

ALTER TABLE public.app_sessions
    ADD CONSTRAINT app_sessions_token_hash_check
    CHECK ((token_hash ~ '^[0-9a-f]{64}$'::text));

ALTER TABLE public.app_sessions
    ADD CONSTRAINT app_sessions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

ALTER TABLE public.archive_restore_stage
    ADD CONSTRAINT archive_restore_stage_pkey
    PRIMARY KEY (attempt_id, table_name, seq);

ALTER TABLE public.archived_classes
    ADD CONSTRAINT archived_classes_pkey
    PRIMARY KEY (id);

ALTER TABLE public.archived_classes
    ADD CONSTRAINT archived_classes_restored_class_id_fkey
    FOREIGN KEY (restored_class_id) REFERENCES public.classes(id) ON DELETE SET NULL;

ALTER TABLE public.archived_classes
    ADD CONSTRAINT archived_classes_storage_path_key
    UNIQUE (storage_path);

ALTER TABLE public.assignment_objectives
    ADD CONSTRAINT assignment_objectives_assignment_id_fkey
    FOREIGN KEY (assignment_id) REFERENCES public.assignments(id) ON DELETE CASCADE;

ALTER TABLE public.assignment_objectives
    ADD CONSTRAINT assignment_objectives_learning_objective_id_fkey
    FOREIGN KEY (learning_objective_id) REFERENCES public.learning_objectives(id) ON DELETE CASCADE;

ALTER TABLE public.assignment_objectives
    ADD CONSTRAINT assignment_objectives_pkey
    PRIMARY KEY (assignment_id, learning_objective_id);

ALTER TABLE public.assignments
    ADD CONSTRAINT assignments_assignment_type_check
    CHECK ((assignment_type = ANY (ARRAY['mastery_opp'::text, 'exam'::text, 'project'::text])));

ALTER TABLE public.assignments
    ADD CONSTRAINT assignments_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.assignments
    ADD CONSTRAINT assignments_pkey
    PRIMARY KEY (id);

ALTER TABLE public.classes
    ADD CONSTRAINT classes_instructor_id_fkey
    FOREIGN KEY (instructor_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.classes
    ADD CONSTRAINT classes_pkey
    PRIMARY KEY (id);

ALTER TABLE public.email_sends
    ADD CONSTRAINT email_sends_kind_check
    CHECK ((kind = ANY (ARRAY['report'::text, 'revision'::text])));

ALTER TABLE public.email_sends
    ADD CONSTRAINT email_sends_pkey
    PRIMARY KEY (id);

ALTER TABLE public.enrollment_attach_log
    ADD CONSTRAINT enrollment_attach_log_pkey
    PRIMARY KEY (id);

ALTER TABLE public.enrollments
    ADD CONSTRAINT enrollments_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.enrollments
    ADD CONSTRAINT enrollments_class_id_student_id_key
    UNIQUE (class_id, student_id);

ALTER TABLE public.enrollments
    ADD CONSTRAINT enrollments_pkey
    PRIMARY KEY (id);

ALTER TABLE public.enrollments
    ADD CONSTRAINT enrollments_student_id_fkey
    FOREIGN KEY (student_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.free_passes
    ADD CONSTRAINT free_passes_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.free_passes
    ADD CONSTRAINT free_passes_passes_used_check
    CHECK (((passes_used >= 0) AND (passes_used <= 2)));

ALTER TABLE public.free_passes
    ADD CONSTRAINT free_passes_pkey
    PRIMARY KEY (id);

ALTER TABLE public.free_passes
    ADD CONSTRAINT free_passes_student_id_class_id_key
    UNIQUE (student_id, class_id);

ALTER TABLE public.free_passes
    ADD CONSTRAINT free_passes_student_id_fkey
    FOREIGN KEY (student_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.grade_change_log
    ADD CONSTRAINT grade_change_log_operation_check
    CHECK ((operation = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text])));

ALTER TABLE public.grade_change_log
    ADD CONSTRAINT grade_change_log_pkey
    PRIMARY KEY (id);

ALTER TABLE public.grades
    ADD CONSTRAINT grades_assignment_id_fkey
    FOREIGN KEY (assignment_id) REFERENCES public.assignments(id) ON DELETE CASCADE;

ALTER TABLE public.grades
    ADD CONSTRAINT grades_last_modified_by_fkey
    FOREIGN KEY (last_modified_by) REFERENCES public.profiles(id) ON DELETE SET NULL;

ALTER TABLE public.grades
    ADD CONSTRAINT grades_learning_objective_id_fkey
    FOREIGN KEY (learning_objective_id) REFERENCES public.learning_objectives(id) ON DELETE CASCADE;

ALTER TABLE public.grades
    ADD CONSTRAINT grades_pkey
    PRIMARY KEY (id);

ALTER TABLE public.grades
    ADD CONSTRAINT grades_second_score_check
    CHECK (((second_score IS NULL) OR (second_score = ANY (ARRAY['M'::text, 'MR'::text, 'R'::text, 'RQ'::text, 'P'::text, 'X'::text, 'A'::text, 'I'::text]))));

ALTER TABLE public.grades
    ADD CONSTRAINT grades_student_id_fkey
    FOREIGN KEY (student_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.grades
    ADD CONSTRAINT grades_student_lo_assignment_key
    UNIQUE (student_id, learning_objective_id, assignment_id);

ALTER TABLE public.grades
    ADD CONSTRAINT grades_top_score_check
    CHECK ((top_score = ANY (ARRAY['M'::text, 'MR'::text, 'R'::text, 'RQ'::text, 'P'::text, 'X'::text, 'A'::text, 'I'::text])));

ALTER TABLE public.homework_scores
    ADD CONSTRAINT homework_scores_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.homework_scores
    ADD CONSTRAINT homework_scores_pkey
    PRIMARY KEY (id);

ALTER TABLE public.homework_scores
    ADD CONSTRAINT homework_scores_student_id_class_id_homework_group_key
    UNIQUE (student_id, class_id, homework_group);

ALTER TABLE public.homework_scores
    ADD CONSTRAINT homework_scores_student_id_fkey
    FOREIGN KEY (student_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.learning_objectives
    ADD CONSTRAINT learning_objectives_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.learning_objectives
    ADD CONSTRAINT learning_objectives_class_id_vendor_code_key
    UNIQUE (class_id, vendor_code);

ALTER TABLE public.learning_objectives
    ADD CONSTRAINT learning_objectives_pkey
    PRIMARY KEY (id);

ALTER TABLE public.profiles
    ADD CONSTRAINT profiles_pkey
    PRIMARY KEY (id);

ALTER TABLE public.profiles
    ADD CONSTRAINT profiles_role_check
    CHECK ((role = ANY (ARRAY['instructor'::text, 'student'::text])));

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_assignment_id_fkey
    FOREIGN KEY (assignment_id) REFERENCES public.assignments(id) ON DELETE CASCADE;

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_class_id_fkey
    FOREIGN KEY (class_id) REFERENCES public.classes(id) ON DELETE CASCADE;

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_item_key
    UNIQUE (student_id, assignment_id, learning_objective_id, revision_due);

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_learning_objective_id_fkey
    FOREIGN KEY (learning_objective_id) REFERENCES public.learning_objectives(id) ON DELETE CASCADE;

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_pkey
    PRIMARY KEY (id);

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_sent_by_fkey
    FOREIGN KEY (sent_by) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.revision_reminders_sent
    ADD CONSTRAINT revision_reminders_sent_student_id_fkey
    FOREIGN KEY (student_id) REFERENCES public.profiles(id) ON DELETE RESTRICT;

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_check
    CHECK ((used_count <= max_uses));

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_code_hash_check
    CHECK ((code_hash ~ '^[0-9a-f]{64}$'::text));

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_code_hash_key
    UNIQUE (code_hash);

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_max_uses_check
    CHECK ((max_uses > 0));

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_pkey
    PRIMARY KEY (id);

ALTER TABLE public.signup_invites
    ADD CONSTRAINT signup_invites_used_count_check
    CHECK ((used_count >= 0));

CREATE INDEX app_sessions_user_id_idx ON public.app_sessions USING btree (user_id);

CREATE INDEX archive_restore_stage_created_at_idx ON public.archive_restore_stage USING btree (created_at);

CREATE INDEX archived_classes_instructor_id_idx ON public.archived_classes USING btree (instructor_id);

CREATE INDEX idx_assignment_objectives_lo ON public.assignment_objectives USING btree (learning_objective_id);

CREATE INDEX idx_assignments_class_created ON public.assignments USING btree (class_id, created_at);

CREATE INDEX idx_classes_instructor ON public.classes USING btree (instructor_id);

CREATE INDEX email_sends_instructor_created ON public.email_sends USING btree (instructor_id, created_at);

CREATE INDEX email_sends_recipient_created ON public.email_sends USING btree (recipient_hash, created_at);

CREATE INDEX enrollment_attach_log_class_created_idx ON public.enrollment_attach_log USING btree (class_id, created_at DESC);

CREATE INDEX idx_enrollments_class ON public.enrollments USING btree (class_id);

CREATE INDEX idx_enrollments_student ON public.enrollments USING btree (student_id);

CREATE INDEX idx_free_passes_class ON public.free_passes USING btree (class_id);

CREATE INDEX idx_grade_change_log_changed_at ON public.grade_change_log USING btree (changed_at);

CREATE INDEX idx_grade_change_log_class_id ON public.grade_change_log USING btree (class_id);

CREATE INDEX idx_grade_change_log_lo_id ON public.grade_change_log USING btree (learning_objective_id);

CREATE INDEX idx_grade_change_log_student_id ON public.grade_change_log USING btree (student_id);

CREATE INDEX idx_grades_assignment ON public.grades USING btree (assignment_id);

CREATE INDEX idx_grades_lo ON public.grades USING btree (learning_objective_id);

CREATE INDEX idx_grades_r_deadline ON public.grades USING btree (r_deadline) WHERE (top_score = 'R'::text);

CREATE INDEX idx_grades_student ON public.grades USING btree (student_id);

CREATE INDEX idx_hw_class ON public.homework_scores USING btree (class_id);

CREATE INDEX idx_hw_student_class ON public.homework_scores USING btree (student_id, class_id);

CREATE INDEX idx_lo_class ON public.learning_objectives USING btree (class_id, display_order);

CREATE INDEX revision_reminders_sent_class_due ON public.revision_reminders_sent USING btree (class_id, revision_due);

CREATE OR REPLACE FUNCTION public.check_invite_code()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_code text := btrim(coalesce(new.raw_user_meta_data ->> 'invite_code', ''));
BEGIN
  IF v_code = '' THEN
    RAISE EXCEPTION 'Invalid or missing invite code';
  END IF;
  UPDATE public.signup_invites
     SET used_count = used_count + 1
   WHERE code_hash = encode(sha256(convert_to(v_code, 'utf8')), 'hex')
     AND revoked_at IS NULL
     AND (expires_at IS NULL OR expires_at > now())
     AND used_count < max_uses;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Invalid or missing invite code';
  END IF;
  RETURN new;
END;
$function$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
BEGIN
  INSERT INTO public.profiles (id, full_name, role)
  VALUES (
    new.id,
    coalesce(nullif(btrim(new.raw_user_meta_data ->> 'full_name'), ''), new.email, 'Unknown'),
    'instructor'
  )
  ON CONFLICT (id) DO NOTHING;
  RETURN new;
END;
$function$;

CREATE OR REPLACE FUNCTION public.log_grade_change()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_class_id uuid; v_old_cfm jsonb; v_new_cfm jsonb;
BEGIN
  IF coalesce(current_setting('clarity.restoring', true), '') = 'on' THEN
    RETURN NEW;
  END IF;
  SELECT class_id INTO v_class_id FROM learning_objectives WHERE id = NEW.learning_objective_id;
  v_new_cfm := to_jsonb(NEW) -> 'counts_for_mastery';
  IF TG_OP = 'INSERT' THEN
    INSERT INTO grade_change_log (student_id, class_id, learning_objective_id, assignment_id, operation, old_value, new_value, changed_by)
    VALUES (NEW.student_id, v_class_id, NEW.learning_objective_id, NEW.assignment_id, 'INSERT', NULL,
      jsonb_build_object('top_score', NEW.top_score, 'second_score', NEW.second_score, 'counts_for_mastery', v_new_cfm), NEW.last_modified_by);
  ELSIF TG_OP = 'UPDATE' THEN
    v_old_cfm := to_jsonb(OLD) -> 'counts_for_mastery';
    IF NEW.top_score IS DISTINCT FROM OLD.top_score OR NEW.second_score IS DISTINCT FROM OLD.second_score OR v_new_cfm IS DISTINCT FROM v_old_cfm THEN
      INSERT INTO grade_change_log (student_id, class_id, learning_objective_id, assignment_id, operation, old_value, new_value, changed_by)
      VALUES (NEW.student_id, v_class_id, NEW.learning_objective_id, NEW.assignment_id, 'UPDATE',
        jsonb_build_object('top_score', OLD.top_score, 'second_score', OLD.second_score, 'counts_for_mastery', v_old_cfm),
        jsonb_build_object('top_score', NEW.top_score, 'second_score', NEW.second_score, 'counts_for_mastery', v_new_cfm), NEW.last_modified_by);
    END IF;
  END IF;
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.restore_archived_class(p_archive_id uuid, p_instructor_id uuid, p_attempt_id uuid, p_expected jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'public'
AS $function$
declare
  v_archive public.archived_classes%rowtype;
  v_class public.classes%rowtype;
  v_class_id uuid;
  v_norm_name text; v_norm_days text; v_norm_section text;
  v_missing text;
  v_table text;
  v_staged_n bigint;
  v_counts jsonb;
begin
  delete from public.archive_restore_stage
  where created_at < now() - interval '1 hour' and attempt_id <> p_attempt_id;

  select * into v_archive from public.archived_classes
  where id = p_archive_id and instructor_id = p_instructor_id for update;
  if not found then raise exception 'This archive was not found.'; end if;
  if v_archive.snapshot_path is null then raise exception 'This archive is not restorable.'; end if;
  if v_archive.restored_at is not null then raise exception 'This archive has already been restored.'; end if;

  foreach v_table in array array['classes','learning_objectives','assignments','assignment_objectives',
      'enrollments','grades','homework_scores','free_passes','students'] loop
    select count(*) into v_staged_n from public.archive_restore_stage
    where attempt_id = p_attempt_id and table_name = v_table;
    if v_staged_n <> coalesce((p_expected ->> v_table)::bigint, 0) then
      raise exception 'The saved archive could not be read completely.';
    end if;
  end loop;
  if coalesce((p_expected ->> 'classes')::bigint, 0) <> 1 then
    raise exception 'This archive is not restorable.';
  end if;

  select (jsonb_populate_record(null::public.classes, row_data)).* into v_class
  from public.archive_restore_stage where attempt_id = p_attempt_id and table_name = 'classes';

  v_norm_name := lower(regexp_replace(trim(coalesce(v_class.name, '')), '\s+', ' ', 'g'));
  v_norm_days := lower(regexp_replace(trim(coalesce(v_class.days, '')), '\s+', ' ', 'g'));
  v_norm_section := lower(regexp_replace(trim(coalesce(v_class.section_number, '')), '\s+', ' ', 'g'));
  perform pg_advisory_xact_lock(hashtext('clarity-restore'),
    hashtext(p_instructor_id::text || '|' || v_norm_name || '|' || v_norm_days || '|' || v_norm_section));

  if exists (
    select 1 from public.classes live
    where live.instructor_id = p_instructor_id
      and lower(regexp_replace(trim(coalesce(live.name, '')), '\s+', ' ', 'g')) = v_norm_name
      and lower(regexp_replace(trim(coalesce(live.days, '')), '\s+', ' ', 'g')) = v_norm_days
      and lower(regexp_replace(trim(coalesce(live.section_number, '')), '\s+', ' ', 'g')) = v_norm_section
  ) then
    raise exception 'A class with this name, days, and section already exists. Archive or rename it first.';
  end if;

  select string_agg(coalesce(stu.row_data ->> 'full_name', m.sid), ', ' order by m.sid) into v_missing
  from (
    select distinct row_data ->> 'student_id' as sid
    from public.archive_restore_stage
    where attempt_id = p_attempt_id
      and table_name in ('enrollments','grades','homework_scores','free_passes')
      and nullif(btrim(row_data ->> 'student_id'), '') is not null
  ) m
  left join public.archive_restore_stage stu
    on stu.attempt_id = p_attempt_id and stu.table_name = 'students' and stu.row_data ->> 'id' = m.sid
  where not exists (select 1 from public.profiles p where p.id = m.sid::uuid);
  if v_missing is not null then
    raise exception 'A student in this archive no longer has a profile: %', v_missing;
  end if;

  create temp table map_lo (old_id uuid primary key, new_id uuid not null) on commit drop;
  create temp table map_assignment (old_id uuid primary key, new_id uuid not null) on commit drop;
  insert into map_lo select (row_data ->> 'id')::uuid, gen_random_uuid()
  from public.archive_restore_stage where attempt_id = p_attempt_id and table_name = 'learning_objectives';
  insert into map_assignment select (row_data ->> 'id')::uuid, gen_random_uuid()
  from public.archive_restore_stage where attempt_id = p_attempt_id and table_name = 'assignments';

  if exists (
    select 1 from public.archive_restore_stage s
    where s.attempt_id = p_attempt_id and s.table_name = 'assignment_objectives'
      and (not exists (select 1 from map_assignment where old_id = (s.row_data ->> 'assignment_id')::uuid)
        or not exists (select 1 from map_lo where old_id = (s.row_data ->> 'learning_objective_id')::uuid))
  ) or exists (
    select 1 from public.archive_restore_stage s
    where s.attempt_id = p_attempt_id and s.table_name = 'grades'
      and (not exists (select 1 from map_lo where old_id = (s.row_data ->> 'learning_objective_id')::uuid)
        or (nullif(btrim(s.row_data ->> 'assignment_id'), '') is not null
            and not exists (select 1 from map_assignment
                            where old_id = nullif(btrim(s.row_data ->> 'assignment_id'), '')::uuid)))
  ) then
    raise exception 'This archive is not restorable.';
  end if;

  v_class_id := gen_random_uuid();
  insert into public.classes
  select r.* from public.archive_restore_stage s
  cross join lateral jsonb_populate_record(null::public.classes,
    s.row_data || jsonb_build_object('id', v_class_id, 'instructor_id', p_instructor_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'classes';

  insert into public.learning_objectives
  select r.* from public.archive_restore_stage s
  join map_lo m on m.old_id = (s.row_data ->> 'id')::uuid
  cross join lateral jsonb_populate_record(null::public.learning_objectives,
    s.row_data || jsonb_build_object('id', m.new_id, 'class_id', v_class_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'learning_objectives';

  insert into public.assignments
  select r.* from public.archive_restore_stage s
  join map_assignment m on m.old_id = (s.row_data ->> 'id')::uuid
  cross join lateral jsonb_populate_record(null::public.assignments,
    s.row_data || jsonb_build_object('id', m.new_id, 'class_id', v_class_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'assignments';

  insert into public.assignment_objectives
  select r.* from public.archive_restore_stage s
  join map_assignment ma on ma.old_id = (s.row_data ->> 'assignment_id')::uuid
  join map_lo ml on ml.old_id = (s.row_data ->> 'learning_objective_id')::uuid
  cross join lateral jsonb_populate_record(null::public.assignment_objectives,
    s.row_data || jsonb_build_object('assignment_id', ma.new_id, 'learning_objective_id', ml.new_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'assignment_objectives';

  insert into public.enrollments
  select r.* from public.archive_restore_stage s
  cross join lateral jsonb_populate_record(null::public.enrollments,
    s.row_data || jsonb_build_object('id', gen_random_uuid(), 'class_id', v_class_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'enrollments';

  insert into public.homework_scores
  select r.* from public.archive_restore_stage s
  left join map_assignment ma on ma.old_id::text = s.row_data ->> 'homework_group'
  cross join lateral jsonb_populate_record(null::public.homework_scores,
    s.row_data || jsonb_build_object('id', gen_random_uuid(), 'class_id', v_class_id,
      'homework_group', coalesce(ma.new_id::text, s.row_data ->> 'homework_group'))) r
  where s.attempt_id = p_attempt_id and s.table_name = 'homework_scores';

  insert into public.free_passes
  select r.* from public.archive_restore_stage s
  cross join lateral jsonb_populate_record(null::public.free_passes,
    s.row_data || jsonb_build_object('id', gen_random_uuid(), 'class_id', v_class_id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'free_passes';

  perform set_config('clarity.restoring', 'on', true);

  insert into public.grades
  select r.* from public.archive_restore_stage s
  join map_lo ml on ml.old_id = (s.row_data ->> 'learning_objective_id')::uuid
  left join map_assignment ma on ma.old_id = nullif(btrim(s.row_data ->> 'assignment_id'), '')::uuid
  left join public.profiles lm on lm.id = nullif(btrim(s.row_data ->> 'last_modified_by'), '')::uuid
  cross join lateral jsonb_populate_record(null::public.grades,
    s.row_data || jsonb_build_object('id', gen_random_uuid(),
      'learning_objective_id', ml.new_id, 'assignment_id', ma.new_id, 'last_modified_by', lm.id)) r
  where s.attempt_id = p_attempt_id and s.table_name = 'grades';

  perform set_config('clarity.restoring', 'off', true);

  v_counts := jsonb_build_object(
    'classes', (select count(*) from public.classes where id = v_class_id),
    'learning_objectives', (select count(*) from public.learning_objectives where class_id = v_class_id),
    'assignments', (select count(*) from public.assignments where class_id = v_class_id),
    'assignment_objectives', (select count(*) from public.assignment_objectives ao
        join public.assignments a on a.id = ao.assignment_id where a.class_id = v_class_id),
    'enrollments', (select count(*) from public.enrollments where class_id = v_class_id),
    'grades', (select count(*) from public.grades g
        join public.learning_objectives lo on lo.id = g.learning_objective_id where lo.class_id = v_class_id),
    'homework_scores', (select count(*) from public.homework_scores where class_id = v_class_id),
    'free_passes', (select count(*) from public.free_passes where class_id = v_class_id));
  foreach v_table in array array['classes','learning_objectives','assignments','assignment_objectives',
      'enrollments','grades','homework_scores','free_passes'] loop
    if (v_counts ->> v_table)::bigint <> coalesce((p_expected ->> v_table)::bigint, 0) then
      raise exception 'The restored class did not match the archive. Nothing was restored.';
    end if;
  end loop;

  update public.archived_classes set restored_at = now(), restored_class_id = v_class_id
  where id = p_archive_id;
  delete from public.archive_restore_stage where attempt_id = p_attempt_id;
  return jsonb_build_object('class_id', v_class_id, 'counts', v_counts);
end;
$function$;

CREATE OR REPLACE FUNCTION public.set_r_deadline()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF coalesce(current_setting('clarity.restoring', true), '') = 'on' THEN
    RETURN NEW;
  END IF;
  IF NEW.top_score = 'R' THEN
    NEW.r_deadline := (NOW() + INTERVAL '7 days')::DATE;
  ELSE
    NEW.r_deadline := NULL;
  END IF;
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_log_grade_change AFTER INSERT OR UPDATE ON public.grades FOR EACH ROW EXECUTE FUNCTION public.log_grade_change();

CREATE TRIGGER trg_set_r_deadline BEFORE INSERT OR UPDATE ON public.grades FOR EACH ROW EXECUTE FUNCTION public.set_r_deadline();

DROP TRIGGER IF EXISTS enforce_invite_code ON auth.users;

CREATE TRIGGER enforce_invite_code BEFORE INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.check_invite_code();

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.app_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.classes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.archived_classes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assignments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.enrollments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.free_passes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.homework_scores ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.learning_objectives ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assignment_objectives ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.grades ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.revision_reminders_sent ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.archive_restore_stage ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.email_sends ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.enrollment_attach_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.grade_change_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.signup_invites ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;

GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;

GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'class-archives',
    'class-archives',
    false,
    52428800,
    ARRAY['application/zip', 'application/gzip']::text[]
)
ON CONFLICT (id) DO NOTHING;

COMMIT;
