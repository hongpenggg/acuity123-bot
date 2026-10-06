-- Migration 003: per-student note delivery tracking.
--
-- /notes walks a student through the overview sheets first and then the focused
-- ones, and the fortnightly subscription works through the focused ones too.
-- Both have to know what has already gone out, and until now nothing recorded
-- it, so a student could be handed the same sheet indefinitely.
--
-- The sheets themselves stay on disk (bot/resources.py scans resources/notes).
-- Only the short code is stored, and a code is unique only *within* a level
-- (B01 exists at all three), so the key carries the level and the tier as well.
-- `tier` is lowercase 'a'/'b', matching the tier_a/tier_b folders and the
-- resources module; the older `notes` table spells the same idea 'A'/'B' and is
-- untouched here.
--
-- Idempotent, so re-running it on a database that already has the table is a
-- no-op. Back up first:
--   pg_dump "$DATABASE_URL" > backup_before_003.sql

begin;

create table if not exists note_deliveries (
  user_id  bigint      not null references users (telegram_id) on delete cascade,
  level    text        not null,
  tier     text        not null check (tier in ('a', 'b')),
  code     text        not null,
  sent_at  timestamptz not null default now(),
  primary key (user_id, level, tier, code)
);

-- Same lockdown as every other table: the bot connects as `postgres` and
-- bypasses RLS, so RLS with no policies means an anon Supabase key reads
-- nothing. Enabling it twice is a no-op.
alter table note_deliveries enable row level security;

commit;

-- No backfill. An empty table is the correct starting point: it means every
-- student is offered the overview sheets from the first one, which is what a
-- student who has only ever had random sheets should get.
--
-- The primary key is also the read index - (user_id, level) and
-- (user_id, level, tier) are both prefixes of it - so there is nothing further
-- to create.
