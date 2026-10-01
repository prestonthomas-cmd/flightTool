import { NextResponse, type NextRequest } from 'next/server';
import { createHash, timingSafeEqual } from 'node:crypto';
import { supabaseAdmin } from '@/lib/supabase/admin';

/**
 * The scraper's door.
 *
 * GET returns every active watch, so the Python job knows what to search.
 * POST takes back the prices it found, and the projections the model fitted.
 *
 * There is no user in either request — the scraper runs on a schedule in
 * GitHub Actions — so it is authorised by a shared secret instead, and it uses
 * the service-role client, which bypasses row-level security. That is the
 * whole reason this route is the only place that key is allowed.
 */
export const dynamic = 'force-dynamic';
export const maxDuration = 60;

/**
 * Compares in constant time.
 *
 * `!==` returns as soon as two bytes differ, so how long a rejection takes
 * leaks how much of the secret was right. Hashing first keeps every path the
 * same shape, including a length mismatch, which would otherwise leak the
 * length through a thrown error.
 */
function matches(provided: string, expected: string): boolean {
  const digest = (value: string) =>
    createHash('sha256').update(Buffer.from(value)).digest();
  return timingSafeEqual(digest(provided), digest(expected));
}

function authorised(request: NextRequest): boolean {
  const expected = process.env.SYNC_SECRET;
  // Without a secret configured this endpoint would hand every user's travel
  // plans to anyone who found the URL. Refuse rather than default to open.
  if (!expected) return false;

  const header = request.headers.get('authorization') ?? '';
  const provided = header.startsWith('Bearer ') ? header.slice(7) : '';
  return provided.length > 0 && matches(provided, expected);
}

export async function GET(request: NextRequest) {
  if (!authorised(request)) {
    return NextResponse.json({ error: 'unauthorised' }, { status: 401 });
  }

  const supabase = supabaseAdmin();
  const { data, error } = await supabase
    .from('watches')
    .select(
      'id, origin, destination, depart_from, depart_to, return_from, return_to, cabin, adults, max_stops',
    )
    .eq('active', true);

  if (error) {
    return NextResponse.json({ error: 'query failed' }, { status: 500 });
  }

  // The model is fitted from history, so the scraper needs it back. A window
  // rather than everything: the booking-horizon curve is built from how prices
  // move within a booking window, and a year-old observation of a flight that
  // has since departed says nothing about one departing next spring.
  const since = new Date(Date.now() - 180 * 24 * 60 * 60 * 1000).toISOString();
  const { data: history } = await supabase
    .from('prices')
    .select('watch_id, observed_at, price, depart_date')
    .gte('observed_at', since)
    .order('observed_at', { ascending: true })
    .limit(100_000);

  // Deliberately no user_id in either list. The scraper does not need to know
  // whose trip this is, and a log line of its payload should not reveal it.
  return NextResponse.json({ watches: data ?? [], history: history ?? [] });
}

type PriceIn = {
  watch_id: string;
  observed_at: string;
  price: number;
  currency?: string;
  depart_date: string;
  return_date?: string | null;
  airlines?: string | null;
  stops?: number | null;
  duration_minutes?: number | null;
  fare?: string | null;
};

type ProjectionIn = {
  watch_id: string;
  day: string;
  price: number;
  low: number;
  high: number;
  evidence?: number;
};

export async function POST(request: NextRequest) {
  if (!authorised(request)) {
    return NextResponse.json({ error: 'unauthorised' }, { status: 401 });
  }

  let body: { prices?: PriceIn[]; projections?: ProjectionIn[]; run?: Record<string, unknown> };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'not json' }, { status: 400 });
  }

  const supabase = supabaseAdmin();
  const prices = (body.prices ?? []).filter(
    (row) => row.watch_id && row.observed_at && row.price > 0 && row.depart_date,
  );
  const projections = (body.projections ?? []).filter(
    (row) => row.watch_id && row.day && row.price > 0 && row.high >= row.low,
  );

  // Upsert rather than insert: a retried job re-sends what it already stored,
  // and duplicating a day's prices would quietly bias every percentile built
  // on top of them.
  if (prices.length) {
    const { error } = await supabase
      .from('prices')
      .upsert(prices, { onConflict: 'watch_id,observed_at,depart_date,return_date' });
    if (error) {
      return NextResponse.json({ error: 'could not store prices' }, { status: 500 });
    }
  }

  if (projections.length) {
    const { error } = await supabase
      .from('projections')
      .upsert(projections, { onConflict: 'watch_id,day' });
    if (error) {
      return NextResponse.json({ error: 'could not store projections' }, { status: 500 });
    }
  }

  if (body.run) {
    await supabase.from('runs').insert({
      finished_at: new Date().toISOString(),
      searches: Number(body.run.searches ?? 0),
      failures: Number(body.run.failures ?? 0),
      note: body.run.note ? String(body.run.note).slice(0, 500) : null,
    });
  }

  return NextResponse.json({
    stored: prices.length,
    projected: projections.length,
  });
}
