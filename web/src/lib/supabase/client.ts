'use client';

import { createBrowserClient } from '@supabase/ssr';

/** The browser-side client. Only ever sees the anon key, which is public. */
export function supabaseBrowser() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
}
