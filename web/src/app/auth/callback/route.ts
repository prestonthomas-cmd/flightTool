import { NextResponse, type NextRequest } from 'next/server';
import { supabaseServer } from '@/lib/supabase/server';

/**
 * Where Supabase sends people back after they confirm an email.
 *
 * It exchanges the one-time code for a session cookie. Without this the
 * confirmation link in a signup email lands on a page that still thinks the
 * visitor is a stranger.
 */
export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const code = url.searchParams.get('code');

  // Only ever a path on this site. An open redirect here would let a crafted
  // link bounce a freshly signed-in person somewhere else entirely.
  const raw = url.searchParams.get('next') ?? '/';
  const next = raw.startsWith('/') && !raw.startsWith('//') ? raw : '/';

  if (!code) {
    return NextResponse.redirect(new URL('/login?error=missing-code', url.origin));
  }

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    return NextResponse.redirect(new URL('/login?error=expired', url.origin));
  }

  return NextResponse.redirect(new URL(next, url.origin));
}
