-- The flight tracker's schema.
--
-- Three tables: what each person wants watched, every price ever seen for it,
-- and where the model thinks each one is heading. Row-level security is on
-- everywhere and every policy keys off auth.uid(), so one person's travel
-- plans are never readable by another — which matters more here than it looks,
-- because a watchlist is a list of where someone will be and when.
--
-- The scraper connects with the service role key and bypasses these policies
-- deliberately: it has to read every watch to know what to search, and it runs
-- with no user in the request. Nothing else should ever use that key.

begin;

create table if not exists public.watches (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users (id) on delete cascade,

  origin        text not null check (origin ~ '^[A-Z]{3}$'),
  destination   text not null check (destination ~ '^[A-Z]{3}$'),

  -- A range rather than a single day, because flexibility is where the money
  -- is. A fixed date is simply the degenerate range where both ends match.
  depart_from   date not null,
  depart_to     date not null,
  return_from   date,
  return_to     date,

  cabin         text not null default 'economy'
                check (cabin in ('economy', 'premium-economy', 'business', 'first')),
  adults        int  not null default 1 check (adults between 1 and 9),
  max_stops     int  check (max_stops >= 0),
  max_price     numeric check (max_price > 0),
  label         text,

  -- Removing a watch hides it but keeps its prices, so adding the same trip
  -- back resumes its history instead of starting over. A real delete is what
  -- the "and delete its prices" path does.
  active        boolean not null default true,
  created_at    timestamptz not null default now(),

  constraint depart_range_ordered check (depart_to >= depart_from),
  constraint return_range_ordered check (
    (return_from is null and return_to is null)
    or (return_from is not null and return_to is not null and return_to >= return_from)
  ),
  constraint return_after_depart check (
    return_from is null or return_from >= depart_from
  ),
  constraint origin_differs check (origin <> destination)
);

create index if not exists watches_user_active_idx
  on public.watches (user_id, active, created_at desc);

create table if not exists public.prices (
  id            bigint generated always as identity primary key,
  watch_id      uuid not null references public.watches (id) on delete cascade,

  observed_at   timestamptz not null,
  price         numeric not null check (price > 0),
  currency      text not null default 'USD',

  -- Which itinerary in the window this price was for. Without it a range
  -- watch's history is a mix of different trips and its own percentile is
  -- meaningless.
  depart_date   date not null,
  return_date   date,

  airlines      text,
  stops         int,
  duration_minutes int,

  -- What this price actually buys. A basic-economy fare and a regular one are
  -- different products, so a history that silently mixes them compares things
  -- that were never comparable.
  fare          text,
  source        text not null default 'google-flights'
);

-- One price per watch, per itinerary, per observation. Re-running a scrape
-- updates rather than duplicating, which keeps a retried job from doubling
-- the history it is meant to extend.
create unique index if not exists prices_unique_observation
  on public.prices (watch_id, observed_at, depart_date, coalesce(return_date, 'epoch'::date));

create index if not exists prices_watch_time_idx
  on public.prices (watch_id, observed_at desc);

create table if not exists public.projections (
  watch_id      uuid not null references public.watches (id) on delete cascade,
  day           date not null,
  price         numeric not null check (price > 0),
  low           numeric not null check (low > 0),
  high          numeric not null check (high > 0),

  -- How much of the curve came from observed data rather than the general
  -- advance-purchase prior, between 0 and 1. The page shows it, because a
  -- projection that is mostly assumption should not look like one that is not.
  evidence      numeric not null default 0 check (evidence between 0 and 1),
  computed_at   timestamptz not null default now(),

  primary key (watch_id, day),
  constraint band_ordered check (high >= price and price >= low)
);

create table if not exists public.runs (
  id            bigint generated always as identity primary key,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  searches      int not null default 0,
  failures      int not null default 0,
  note          text
);

alter table public.watches     enable row level security;
alter table public.prices      enable row level security;
alter table public.projections enable row level security;
alter table public.runs        enable row level security;

-- Watches: yours and only yours, for every verb.
create policy watches_select on public.watches
  for select using (auth.uid() = user_id);
create policy watches_insert on public.watches
  for insert with check (auth.uid() = user_id);
create policy watches_update on public.watches
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy watches_delete on public.watches
  for delete using (auth.uid() = user_id);

-- Prices and projections are readable through the watch that owns them, and
-- are never written by a user — only by the scraper, which bypasses RLS.
create policy prices_select on public.prices
  for select using (
    exists (
      select 1 from public.watches w
      where w.id = prices.watch_id and w.user_id = auth.uid()
    )
  );

create policy projections_select on public.projections
  for select using (
    exists (
      select 1 from public.watches w
      where w.id = projections.watch_id and w.user_id = auth.uid()
    )
  );

-- `runs` is operational, the same for everyone, and says nothing about any
-- person's travel. Readable by anyone signed in so the page can show when the
-- tracker last managed to collect anything.
create policy runs_select on public.runs
  for select to authenticated using (true);

commit;
