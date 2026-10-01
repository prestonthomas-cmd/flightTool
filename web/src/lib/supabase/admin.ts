import { createClient } from '@supabase/supabase-js';
import { requireEnv } from './server';

/**
 * The service-role client. Bypasses row-level security entirely.
 *
 * Only the scraper's endpoint uses this, and only because it genuinely has to:
 * it runs with no user in the request and must read every watch to know what
 * to search. Anything reachable by a browser must use the per-user client
 * instead — a mistake here hands one person's data to another.
 */
export function supabaseAdmin() {
  return createClient(
    requireEnv('NEXT_PUBLIC_SUPABASE_URL'),
    requireEnv('SUPABASE_SERVICE_ROLE_KEY'),
    { auth: { persistSession: false, autoRefreshToken: false } },
  );
}
