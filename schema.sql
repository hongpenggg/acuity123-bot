-- LKC OphSoc Tele Bot — full schema.
-- Apply to a FRESH database:   psql "$DATABASE_URL" -f schema.sql
-- For a database that already has the old schema, use migrations/001_*.sql instead.
-- Requires PostgreSQL 14+ (Supabase and a stock Contabo install both qualify).

begin;

-- ---------------------------------------------------------------- users
-- One row per person who has pressed /start.
create table users (
  telegram_id  bigint primary key,
  username     text,
  -- Audience tier. The student picks this with /level and can change it any time.
  level        text        not null default 'preclin'
                           check (level in ('preclin', 'clin', 'postmbbs')),
  active       boolean     not null default true,
  weekly_sub   boolean     not null default false,
  notes_sub    boolean     not null default false,
  created_at   timestamptz not null default now()
);

-- ------------------------------------------------------------ questions
-- The question bank. `options` is a JSON array of choice texts; the answer is an
-- index into that array, so the two CHECKs below are what stop a hand-edit from
-- producing a row the bot cannot render.
create table questions (
  id           serial primary key,
  level        text        not null check (level in ('preclin', 'clin', 'postmbbs')),
  topic        text        not null,          -- the shared "common tag" per topic
  text         text        not null,
  options      jsonb       not null check (jsonb_typeof(options) = 'array'),
  correct_idx  int         not null,
  explanation  text,                          -- LLM cache, written once
  created_at   timestamptz not null default now(),
  check (jsonb_array_length(options) between 2 and 6),
  check (correct_idx >= 0 and correct_idx < jsonb_array_length(options))
);
create index questions_level_topic_idx on questions (level, topic);

-- ------------------------------------------------------------- attempts
-- One row per answer. `unique (user_id, msg_id)` is what makes a double tap
-- idempotent AND is required by the `on conflict (user_id, msg_id)` in db.py.
create table attempts (
  id           bigserial primary key,
  user_id      bigint      not null references users (telegram_id) on delete cascade,
  question_id  int         not null references questions (id) on delete cascade,
  level        text        not null,
  topic        text        not null,
  chosen_idx   int         not null,
  correct      boolean     not null,
  mode         text        not null check (mode in ('practice', 'weekly')),
  msg_id       bigint      not null,
  created_at   timestamptz not null default now(),
  unique (user_id, msg_id)
);
-- Serves both the per-topic weakness aggregate and the "have they seen this
-- question" check in pick_question.
create index attempts_user_level_topic_idx  on attempts (user_id, level, topic);
create index attempts_user_question_idx     on attempts (user_id, question_id);

-- ---------------------------------------------------------- tournaments
create table tournaments (
  id         serial primary key,
  starts_at  timestamptz not null,
  ends_at    timestamptz not null,
  active     boolean     not null default true,
  check (ends_at > starts_at)
);
create index tournaments_active_idx on tournaments (active, ends_at);

-- Membership and score. A row appears only after the user sends /tournament.
-- joined_at is the tie-break for equal scores (first to get there wins).
create table tournament_points (
  tournament_id int         not null references tournaments (id) on delete cascade,
  user_id       bigint      not null references users (telegram_id) on delete cascade,
  points        int         not null default 0 check (points >= 0),
  joined_at     timestamptz not null default now(),
  primary key (tournament_id, user_id)
);

-- Which questions have already earned this user a point in this tournament.
-- Without it, re-answering a question in a fresh message scores again and the
-- leaderboard is farmable.
create table tournament_answers (
  tournament_id int         not null references tournaments (id) on delete cascade,
  user_id       bigint      not null references users (telegram_id) on delete cascade,
  question_id   int         not null references questions (id) on delete cascade,
  created_at    timestamptz not null default now(),
  primary key (tournament_id, user_id, question_id)
);

-- ------------------------------------------------------------------ notes
-- tier A = high yield, pushed fortnightly to subscribers (/notes_sub).
-- tier B = low yield niche, pulled on demand with /notes <topic>.
create table notes (
  id     serial primary key,
  level  text  not null check (level in ('preclin', 'clin', 'postmbbs')),
  topic  text  not null,
  tier   text  not null check (tier in ('A', 'B')),
  title  text  not null,
  body   text  not null
);
-- get_notes() compares on lower(topic), which a plain (level, tier, topic) index
-- cannot serve.
create index notes_lookup_idx on notes (level, tier, lower(topic));

-- --------------------------------------------------------------- lockdown
-- Supabase publishes every table over its REST API. The bot connects as the
-- `postgres` role over the pooler and bypasses RLS; enabling RLS with no policies
-- means an anon/public API key can read nothing.
alter table users             enable row level security;
alter table questions         enable row level security;
alter table attempts          enable row level security;
alter table tournaments       enable row level security;
alter table tournament_points enable row level security;
alter table tournament_answers enable row level security;
alter table notes             enable row level security;

commit;

-- ============================================================================
-- PLACEHOLDER seed data — delete it once the real bank is ready.
--
-- Deliberately generic, non-clinical content: ZH has flagged that the iRAT/tRAT,
-- AIMBOSS, PassMedicine and school/senior material must be rewritten for
-- copyright before it goes anywhere near this repo, and that the "OphSoc QBank
-- and Notes (ZH)" sheet is off limits for now. Do not paste it in here.
--
-- These rows exist so /practice, /level and the tournament are testable today.
-- ============================================================================
insert into questions (level, topic, text, options, correct_idx) values
  ('preclin', 'Sample', 'Placeholder question — replace me.',
   '["Option one", "Option two", "Option three", "Option four"]', 0),
  ('preclin', 'Sample', 'Second placeholder, with five options.',
   '["One", "Two", "Three", "Four", "Five"]', 4),
  ('clin',    'Sample', 'Clinical-level placeholder.',
   '["Alpha", "Beta", "Gamma", "Delta"]', 2),
  ('postmbbs','Sample', 'Post-MBBS placeholder.',
   '["Yes", "No"]', 1);

insert into notes (level, topic, tier, title, body) values
  ('preclin',  'Sample', 'B', 'Placeholder Tier B note', 'Replace with real content.'),
  ('clin',     'Sample', 'A', 'Placeholder Tier A note', 'Replace with real content.'),
  ('postmbbs', 'Sample', 'A', 'Placeholder Tier A note', 'Replace with real content.');
