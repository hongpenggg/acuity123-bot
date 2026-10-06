-- Add the 'review' answer mode.
--
-- /review re-serves questions the student previously got wrong. Those answers
-- must not score for the tournament: a wrong answer in /quizme reveals the
-- correct option, so a re-answer that scored would let any student reach full
-- marks without knowing anything. Recording them under their own mode is what
-- keeps them out of the scoring path and out of the set-score window.
--
-- Apply to a database created before this change:
--     psql "$DATABASE_URL" -f migrations/004_review_mode.sql
--
-- Idempotent: re-running is a no-op.

begin;

do $$
begin
  if exists (
    select 1 from pg_constraint
     where conrelid = 'attempts'::regclass
       and conname = 'attempts_mode_check'
       and pg_get_constraintdef(oid) not like '%review%'
  ) then
    alter table attempts drop constraint attempts_mode_check;
    alter table attempts add constraint attempts_mode_check
      check (mode in ('practice', 'weekly', 'review'));
  end if;
end
$$;

commit;
