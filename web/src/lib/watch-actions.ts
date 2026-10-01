'use server';

import { revalidatePath } from 'next/cache';
import { supabaseServer, currentUser } from '@/lib/supabase/server';

export type WatchState = { error?: string; ok?: string };

const CABINS = ['economy', 'premium-economy', 'business', 'first'] as const;
const AIRPORT = /^[A-Z]{3}$/;

function readDate(form: FormData, name: string): string | null {
  const value = String(form.get(name) ?? '').trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

/**
 * Add a flight to the signed-in person's watchlist.
 *
 * Everything is re-validated here. A Server Action is reachable by a direct
 * POST, not only through the form, so the browser's own checks are a courtesy
 * to the person rather than a constraint on the request.
 */
export async function addWatch(_: WatchState, form: FormData): Promise<WatchState> {
  const user = await currentUser();
  if (!user) return { error: 'Sign in first.' };

  const origin = String(form.get('origin') ?? '').trim().toUpperCase();
  const destination = String(form.get('destination') ?? '').trim().toUpperCase();
  if (!AIRPORT.test(origin) || !AIRPORT.test(destination)) {
    return { error: 'Airports are three-letter codes, like JFK.' };
  }
  if (origin === destination) {
    return { error: 'The origin and destination are the same airport.' };
  }

  const departFrom = readDate(form, 'depart_from');
  if (!departFrom) return { error: 'Pick a departure date.' };
  const departTo = readDate(form, 'depart_to') ?? departFrom;
  if (departTo < departFrom) {
    return { error: 'The departure range ends before it starts.' };
  }

  const returnFrom = readDate(form, 'return_from');
  const returnTo = returnFrom ? (readDate(form, 'return_to') ?? returnFrom) : null;
  if (returnFrom && returnFrom < departFrom) {
    return { error: 'The return date is before the departure date.' };
  }
  if (returnFrom && returnTo && returnTo < returnFrom) {
    return { error: 'The return range ends before it starts.' };
  }

  const cabin = String(form.get('cabin') ?? 'economy');
  if (!CABINS.includes(cabin as (typeof CABINS)[number])) {
    return { error: 'Pick a cabin.' };
  }

  const adults = Number(form.get('adults') ?? 1);
  if (!Number.isInteger(adults) || adults < 1 || adults > 9) {
    return { error: 'Between 1 and 9 adults.' };
  }

  const rawMax = String(form.get('max_price') ?? '').trim();
  const maxPrice = rawMax ? Number(rawMax) : null;
  if (maxPrice !== null && (!Number.isFinite(maxPrice) || maxPrice <= 0)) {
    return { error: 'That alert price is not a number.' };
  }

  const label = String(form.get('label') ?? '').trim().slice(0, 80) || null;

  const supabase = await supabaseServer();
  // user_id is set from the verified session, never from the form. The RLS
  // policy would reject anything else anyway, which is the belt to this brace.
  const { error } = await supabase.from('watches').insert({
    user_id: user.id,
    origin,
    destination,
    depart_from: departFrom,
    depart_to: departTo,
    return_from: returnFrom,
    return_to: returnTo,
    cabin,
    adults,
    max_stops: form.get('nonstop') ? 0 : null,
    max_price: maxPrice,
    label,
  });

  if (error) {
    return { error: 'Could not save that flight. Check the dates and try again.' };
  }

  revalidatePath('/');
  return { ok: `Watching ${origin} to ${destination}. Prices start collecting on the next run.` };
}

/**
 * Stop watching a flight.
 *
 * Deactivating keeps the price history, so adding the same trip back picks it
 * up where it left off. Deleting is the deliberate, separate choice.
 */
export async function removeWatch(form: FormData): Promise<void> {
  const user = await currentUser();
  if (!user) return;

  const id = String(form.get('id') ?? '');
  if (!id) return;

  const supabase = await supabaseServer();

  if (form.get('purge')) {
    // The prices go with it: `prices` cascades on the foreign key.
    await supabase.from('watches').delete().eq('id', id);
  } else {
    await supabase.from('watches').update({ active: false }).eq('id', id);
  }

  revalidatePath('/');
}
