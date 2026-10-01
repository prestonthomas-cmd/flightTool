import { NextResponse, type NextRequest } from 'next/server';
import { createServerClient } from '@supabase/ssr';

/**
 * Keeps the Supabase session cookie fresh.
 *
 * In Next 16 this file is `proxy.ts`; it was `middleware.ts` through 15, and
 * every Supabase guide still says so. Named the old way it is silently never
 * run, sessions quietly expire, and people get signed out for no visible
 * reason — so the name matters more than it looks.
 *
 * This is deliberately the *only* thing here. The docs are explicit that proxy
 * is not a session-management or authorization solution, and it runs before
 * the route is even known. Whether a given person may see a given watch is
 * decided by row-level security in the database, and whether they are signed
 * in at all is re-checked in each page and action.
 */
export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (written) => {
          written.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          );
          response = NextResponse.next({ request });
          written.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  // Refreshing the token is the entire job. The result is deliberately unused.
  await supabase.auth.getUser();

  return response;
}

export const config = {
  // Everything except static assets and images, which carry no session.
  matcher: ['/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|webp)$).*)'],
};
