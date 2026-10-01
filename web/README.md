# Flight Price Watch — the web app

Sign up, add a flight, and it starts collecting prices twice a day.

Next.js 16 on Vercel, Supabase for auth and storage, and the Python scraper in
`../flighttracker` running on GitHub Actions.

## Why it is split in two

The scraper cannot run on Vercel. `fast-flights` protobuf-encodes Google
Flights queries and there is no Node equivalent, and a run takes minutes of
deliberately spaced requests — well past any serverless function's ceiling. So
the proven scraper keeps running where it already does, and only its source of
work and destination for results changed: it asks `/api/sync` what to search
and hands back what it found.

That split has a side benefit. The model is fitted across *everybody's* prices
at once, and the booking-horizon curve needs watches sitting at different
distances from departure to work at all. One person with three trips rarely has
them; a few hundred people do. Nothing identifying is pooled — the model sees a
price, a date, and an opaque id.

## Setting it up

### 1. Supabase

Create a project at [supabase.com](https://supabase.com) (the free tier is
enough), then run the migration in `supabase/migrations/` — paste it into the
SQL editor, or `supabase db push` if you use the CLI.

Under **Authentication → Providers**, leave Email on. Under **URL
Configuration**, set the site URL to your deployed address and add
`https://<your-app>/auth/callback` to the redirect allow-list — without it the
confirmation link in a signup email lands nowhere.

### 2. Vercel

Import this repo, set the root directory to `web/`, and add the four variables
from `.env.example`. `SYNC_SECRET` is one you invent:

```bash
openssl rand -hex 32
```

### 3. The scraper

In this repo's GitHub settings:

- **Variables** → `FLIGHTWATCH_URL` = your deployed address
- **Secrets** → `SYNC_SECRET` = the same string you gave Vercel

Then Actions → *Track hosted watchlist* → Run workflow to prove it end to end
before trusting the schedule. `python -m flighttracker cloud-run --dry-run`
does the same thing locally and publishes nothing.

## How access control works

Row-level security, not application code. Every policy keys off `auth.uid()`,
so a query that forgets to filter by user returns nothing rather than returning
someone else's travel plans. The dashboard deliberately has no `user_id` filter
in it for exactly that reason: if a policy were wrong, a filter would hide it.

Two things bypass that, both deliberately:

- `/api/sync` uses the service-role key, because it runs with no user in the
  request and must read every watch to know what to search. It is the only
  place that key is read, and it is authorised by a shared secret compared in
  constant time. With no secret configured it refuses everything rather than
  defaulting to open.
- The scraper never learns whose trip anything is. Neither response from
  `/api/sync` carries a `user_id`.

A watchlist is a list of where someone will be and when, which is worth more
care than a list of prices would be.

## Notes for working on it

`proxy.ts` is Next 16's name for what was `middleware.ts` through 15. Every
Supabase guide still says the old name, under which the file is silently never
run and sessions quietly expire. It does one thing — refresh the session cookie
— because the docs are explicit that proxy is not an authorization solution.
Whether someone may see a watch is decided by RLS; whether they are signed in
at all is re-checked in each page and action, since a Server Action is
reachable by a direct POST and not only through the form.

```bash
npm run dev     # locally
npm run check   # typecheck and tests
```
