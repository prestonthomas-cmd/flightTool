import { cookies } from 'next/headers';
import { createServerClient } from '@supabase/ssr';

/**
 * A Supabase client bound to the signed-in person's cookies.
 *
 * Every query made through it runs as that person, so row-level security is
 * doing the access control rather than application code remembering to filter
 * by user. Code that forgets a `where user_id = ...` returns nothing here,
 * instead of returning somebody else's travel plans.
 */
export async function supabaseServer() {
  // Async since Next 15: `cookies()` returns a promise.
  const store = await cookies();

  return createServerClient(
    requireEnv('NEXT_PUBLIC_SUPABASE_URL'),
    requireEnv('NEXT_PUBLIC_SUPABASE_ANON_KEY'),
    {
      cookies: {
        getAll: () => store.getAll(),
        setAll: (written) => {
          try {
            written.forEach(({ name, value, options }) =>
              store.set(name, value, options),
            );
          } catch {
            // Server Components cannot set cookies. The proxy refreshes the
            // session on every request, so a read-only render here is fine
            // and throwing would take the page down for no gain.
          }
        },
      },
    },
  );
}

/**
 * The signed-in person, or null.
 *
 * Always `getUser()`, never `getSession()`: a session comes from the cookie,
 * which the browser controls, while `getUser()` is checked against Supabase.
 * Authorization decisions have to rest on the one that cannot be forged.
 */
export async function currentUser() {
  const supabase = await supabaseServer();
  const { data, error } = await supabase.auth.getUser();
  return error ? null : data.user;
}

export function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. Copy .env.example to .env.local and fill it in.`,
    );
  }
  return value;
}
