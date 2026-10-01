import { redirect } from 'next/navigation';
import { currentUser, supabaseServer } from '@/lib/supabase/server';
import { signOut } from '@/lib/auth-actions';
import { AddFlight } from '@/components/AddFlight';
import { WatchCard } from '@/components/WatchCard';
import type { Price, Projection, Watch } from '@/lib/types';

// Every render depends on who is asking, so there is nothing to prerender.
export const dynamic = 'force-dynamic';

export default async function Dashboard() {
  const user = await currentUser();
  if (!user) redirect('/login');

  const supabase = await supabaseServer();

  // Row-level security scopes all three of these to this person. There is
  // deliberately no `.eq('user_id', ...)` here: if the policy were wrong, a
  // filter in application code would hide that rather than surface it.
  const [{ data: watches }, { data: prices }, { data: projections }] =
    await Promise.all([
      supabase
        .from('watches')
        .select('*')
        .eq('active', true)
        .order('created_at', { ascending: true }),
      supabase
        .from('prices')
        .select('watch_id, observed_at, price, currency, depart_date, return_date')
        .order('observed_at', { ascending: true }),
      supabase.from('projections').select('*').order('day', { ascending: true }),
    ]);

  const byWatch = new Map<string, Price[]>();
  for (const row of (prices ?? []) as (Price & { watch_id: string })[]) {
    const list = byWatch.get(row.watch_id) ?? [];
    list.push(row);
    byWatch.set(row.watch_id, list);
  }

  const forecastFor = new Map<string, Projection[]>();
  for (const row of (projections ?? []) as (Projection & { watch_id: string })[]) {
    const list = forecastFor.get(row.watch_id) ?? [];
    list.push(row);
    forecastFor.set(row.watch_id, list);
  }

  const list = (watches ?? []) as Watch[];

  return (
    <main className="wrap">
      <div className="top">
        <div>
          <h1>Flight Price Watch</h1>
          <p className="sub">{user.email}</p>
        </div>
        <div className="top-actions">
          <AddFlight />
          <form action={signOut}>
            <button type="submit" className="quiet">
              Sign out
            </button>
          </form>
        </div>
      </div>

      {list.length === 0 ? (
        <div className="blank">
          <h2>Nothing watched yet</h2>
          <p>
            Add a flight and it starts collecting prices twice a day. After a
            week or so there is enough history to say whether today is a good
            day to book.
          </p>
        </div>
      ) : (
        list.map((watch) => (
          <WatchCard
            key={watch.id}
            watch={watch}
            prices={byWatch.get(watch.id) ?? []}
            projection={forecastFor.get(watch.id) ?? []}
          />
        ))
      )}
    </main>
  );
}
