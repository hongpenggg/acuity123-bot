-- Migration 005: one scoring answer per question, and the missing level checks.
--
-- Three things an audit found, all of them schema-side:
--
--   1. `unique (user_id, msg_id)` on attempts dedupes a *card*, not a question.
--      A student can hold two live cards for one question - a /quizme card left
--      unanswered, then the Monday push serving the same question before they
--      answer the first - and answering both wrote two rows. The question's
--      latest attempt could then be the wrong one, so it re-entered /review;
--      practice_streak reset; and db.stats counted two answers for one
--      question, inflating its denominator.
--
--      Fixed with a *partial* unique index. A blanket one-per-question would
--      break /review, which exists to re-answer a question the student already
--      attempted (those rows carry mode = 'review', added in 004). mode is
--      deliberately not part of the key: the reproduced case is one 'practice'
--      row plus one 'weekly' row for the same question, which a key including
--      mode would still allow.
--
--   2. attempts.level had no CHECK, unlike users/questions/notes.
--   3. note_deliveries.level had no CHECK either, so record_notes_sent() with a
--      mistyped level was accepted and the row was then invisible to every
--      level-scoped read - the sheet counted as sent and was never offered
--      again.
--
-- Run 004 first: this reclassifies rows as mode = 'review', which 004 is what
-- makes legal. The guard below stops it part-way if 004 has not been applied.
--
-- Idempotent: re-running is a clean no-op.
--
-- Back up first:
--   pg_dump "$DATABASE_URL" > backup_before_005.sql

begin;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conrelid = 'attempts'::regclass
       and conname = 'attempts_mode_check'
       and pg_get_constraintdef(oid) like '%review%'
  ) then
    raise exception
      'attempts.mode does not allow ''review'' yet: apply '
      'migrations/004_review_mode.sql before this one.';
  end if;
end
$$;

-- ----------------------------------------------- 1. the duplicate rows, first
-- An existing database can already hold the duplicates the index forbids, so
-- they have to be resolved before it can be created.
--
-- Nothing is deleted. A duplicate is a real answer the student really gave, on
-- a card the bot really sent them, and deleting it would throw away history the
-- student cannot get back. What is wrong about it is only its *role*: it was
-- never a first encounter with the question, so it must not score, must not
-- advance a set and must not count as a fresh answer. 'review' is already
-- exactly that role (see 004), so the superseded rows are reclassified into it
-- and keep their chosen_idx, correct, msg_id and created_at.
--
-- The earliest row per (user, question) is the one kept as the first encounter:
-- db.py's set machinery (_PICK_SQL, _LAST_SET_SQL) already treats each
-- question's FIRST attempt as the canonical one, and after this change the
-- first answer is the only one record_attempt would have accepted.
--
-- No tournament points to claw back: tournament_answers already deduped per
-- question, so a duplicate correct answer never scored a second point.
do $$
declare
  reclassified int;
begin
  with superseded as (
    select id,
           row_number() over (partition by user_id, question_id order by id, msg_id)
             as encounter
      from attempts
     where mode <> 'review'
  )
  update attempts a
     set mode = 'review'
    from superseded s
   where s.id = a.id
     and s.encounter > 1;
  get diagnostics reclassified = row_count;
  if reclassified > 0 then
    raise notice
      '005: reclassified % duplicate attempt row(s) as mode = ''review'' '
      '(the earliest answer to each question is kept as the first encounter)',
      reclassified;
  end if;
end
$$;

create unique index if not exists attempts_one_scoring_answer_idx
    on attempts (user_id, question_id) where mode <> 'review';

-- ----------------------------------------------------- 2 and 3. level checks
-- Validated, not NOT VALID: a fresh database built from schema.sql gets a
-- validated constraint, and these two have to match it exactly. Nothing in
-- bot/ has ever written a level it did not read out of users or questions, so
-- real data is clean - but report what is in the way rather than leaving the
-- operator with Postgres's "violated by some row".
do $$
declare
  bad text;
begin
  select string_agg(distinct quote_literal(level), ', ')
    into bad
    from attempts
   where level not in ('preclin', 'clin', 'postmbbs');
  if bad is not null then
    raise exception
      'attempts holds level(s) the check would reject: %. Re-tag them, then '
      're-run this migration.', bad;
  end if;

  select string_agg(distinct quote_literal(level), ', ')
    into bad
    from note_deliveries
   where level not in ('preclin', 'clin', 'postmbbs');
  if bad is not null then
    raise exception
      'note_deliveries holds level(s) the check would reject: %. Re-tag them, '
      'then re-run this migration.', bad;
  end if;
end
$$;

-- The names are the ones Postgres gives the inline column checks in
-- schema.sql, so a migrated database and a fresh one are indistinguishable.
do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conrelid = 'attempts'::regclass and conname = 'attempts_level_check'
  ) then
    alter table attempts add constraint attempts_level_check
      check (level in ('preclin', 'clin', 'postmbbs'));
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'note_deliveries'::regclass
       and conname = 'note_deliveries_level_check'
  ) then
    alter table note_deliveries add constraint note_deliveries_level_check
      check (level in ('preclin', 'clin', 'postmbbs'));
  end if;
end
$$;

commit;

-- attempts_user_question_idx is left alone. It is non-unique on purpose: it
-- serves the "has this student attempted this question at all" filter in
-- pick_question, which has to see review rows too, so it cannot carry the
-- uniqueness the new partial index does.
--
-- Verify:
--   select count(*) from (select user_id, question_id from attempts
--                          where mode <> 'review'
--                          group by 1, 2 having count(*) > 1) d;   -- 0
--   select conname from pg_constraint
--    where conrelid in ('attempts'::regclass, 'note_deliveries'::regclass)
--      and conname like '%level_check';                             -- both
