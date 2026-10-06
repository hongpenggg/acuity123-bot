-- At most one open tournament.
--
-- Two concurrent /admin_tournament_start taps both passed the handler's
-- "is one running?" check before either inserted, leaving two live tournament
-- rows and announcing the competition twice. A check in application code cannot
-- prevent that; a unique index can.
--
--     psql "$DATABASE_URL" -f migrations/006_one_active_tournament.sql
--
-- Idempotent. Aborts rather than guessing if the data already breaks the rule.

begin;

do $$
declare
  open_count int;
begin
  select count(*) into open_count from tournaments where active;
  if open_count > 1 then
    raise exception
      '006: % tournaments are already active. Close all but one first: '
      'update tournaments set active = false where id <> <the one to keep>;',
      open_count;
  end if;
end
$$;

create unique index if not exists tournaments_one_active_idx
    on tournaments (active) where active;

commit;
