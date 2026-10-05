-- Migration 001: bring a database created from the earlier schema.sql up to date.
--
-- Adds:
--   * users.level / questions.level / attempts.level / notes.level
--   * tournament_points.joined_at
--   * the tournament_answers table (closes the leaderboard-farming hole)
--   * the indexes the queries actually use
--
-- Safe to run once on the existing Supabase project. Back up first:
--   pg_dump "$DATABASE_URL" > backup_before_001.sql

begin;

alter table users
  add column if not exists level text not null default 'preclin'
  check (level in ('preclin', 'clin', 'postmbbs'));

alter table questions
  add column if not exists level text not null default 'preclin';

alter table attempts
  add column if not exists level text not null default 'preclin';

alter table notes
  add column if not exists level text not null default 'preclin';

alter table tournament_points
  add column if not exists joined_at timestamptz not null default now();

create table if not exists tournament_answers (
  tournament_id int         not null references tournaments (id) on delete cascade,
  user_id       bigint      not null references users (telegram_id) on delete cascade,
  question_id   int         not null references questions (id) on delete cascade,
  created_at    timestamptz not null default now(),
  primary key (tournament_id, user_id, question_id)
);
alter table tournament_answers enable row level security;

create index if not exists questions_level_topic_idx on questions (level, topic);
create index if not exists attempts_user_level_topic_idx on attempts (user_id, level, topic);
create index if not exists attempts_user_question_idx on attempts (user_id, question_id);
create index if not exists tournaments_active_idx on tournaments (active, ends_at);
create index if not exists notes_lookup_idx on notes (level, tier, lower(topic));

commit;

-- Everything defaulted to 'preclin'. Re-tag the rows you already have, e.g.:
--
--   update questions set level = 'clin'    where topic in ('Glaucoma', 'Cornea');
--   update questions set level = 'postmbbs' where topic in ('Cataract');
--   update notes     set level = 'clin'    where tier = 'A';
--   update users     set level = 'clin'    where telegram_id in (<ids>);
--
-- Note: the old schema declared notes.tier as char(1). It still compares equal to
-- 'A'/'B', so the bot works unchanged — widen it when convenient:
--   alter table notes alter column tier type text;

-- Constraints to add once the data is clean (they will reject bad existing rows):
--   alter table questions add constraint questions_options_len
--     check (jsonb_array_length(options) between 2 and 6);
--   alter table questions add constraint questions_correct_idx
--     check (correct_idx >= 0 and correct_idx < jsonb_array_length(options));
--   alter table attempts add constraint attempts_mode
--     check (mode in ('practice', 'weekly'));
