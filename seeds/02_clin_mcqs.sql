-- Clinical Ophthalmology - question bank for level 'clin'.
--
-- GENERATED FILE - do not edit by hand.
--   Regenerate with:  python tools/build_question_seed.py --level clin
--   Source(s):
--   (none yet - drop the .docx into resources/ and re-run the generator)
--
-- Load AFTER schema.sql, in file order:
--   psql "$DATABASE_URL" -f schema.sql
--   psql "$DATABASE_URL" -f seeds/01_preclin_mcqs.sql
--   psql "$DATABASE_URL" -f seeds/02_clin_mcqs.sql
--   psql "$DATABASE_URL" -f seeds/03_postmbbs_mcqs.sql
--
-- Re-running is blocked by the guard below. To reload from scratch:
--   delete from questions where level = 'clin';

-- No source document for this level yet, so this file loads nothing.

begin;

-- Refuse to double-load this level: a second run would otherwise silently
-- duplicate the bank. Scoped to 'clin' so the levels stay independent.
do $$
begin
  if exists (select 1 from questions where level = 'clin') then
    raise exception 'clin questions already present (% rows) - delete them first to reload',
      (select count(*) from questions where level = 'clin');
  end if;
end
$$;

-- Nothing to load yet. This file is a placeholder so that loading every seed in
-- order is always safe. When the Clinical questions arrive:
--
--   1. put the .docx in resources/
--   2. add it to LEVELS['clin'] in tools/build_question_seed.py
--   3. python tools/build_question_seed.py --level clin
--
commit;
