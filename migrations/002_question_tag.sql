-- Migration 002: keep the content author's finer-grained question tag.
--
-- The .docx banks tag every question twice: a topic ("Orbit and eye movements")
-- and a question type ("Anatomy | lesion localisation"). The topic drives the
-- adaptive weighting; the type is preserved here so nothing the content author
-- wrote is thrown away, and so it is available for filtering later.
--
-- Safe on an existing database. Back up first:
--   pg_dump "$DATABASE_URL" > backup_before_002.sql

begin;

alter table questions
  add column if not exists tag text;

commit;

-- Optional: load the tag for the bank in seeds/01_preclin_mcqs.sql by
-- re-running that file against an empty questions table.
