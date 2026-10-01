'use server';

import { redirect } from 'next/navigation';
import { revalidatePath } from 'next/cache';
import { headers } from 'next/headers';
import { supabaseServer } from '@/lib/supabase/server';

export type AuthState = { error?: string; sent?: string };

function readCredentials(form: FormData) {
  const email = String(form.get('email') ?? '').trim().toLowerCase();
  const password = String(form.get('password') ?? '');
  return { email, password };
}

/**
 * Supabase's own messages are written for developers and sometimes say more
 * than a stranger should learn — in particular whether an address has an
 * account. These are the versions people see.
 */
function plainly(message: string): string {
  const lower = message.toLowerCase();
  if (lower.includes('invalid login')) {
    return 'That email and password do not match an account.';
  }
  if (lower.includes('email not confirmed')) {
    return 'Check your email and click the confirmation link first.';
  }
  if (lower.includes('rate limit') || lower.includes('too many')) {
    return 'Too many attempts. Wait a minute and try again.';
  }
  if (lower.includes('password')) {
    return 'That password is too short — use at least 8 characters.';
  }
  return 'Something went wrong. Try again.';
}

export async function signIn(_: AuthState, form: FormData): Promise<AuthState> {
  const { email, password } = readCredentials(form);
  if (!email || !password) {
    return { error: 'Enter your email and password.' };
  }

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) {
    return { error: plainly(error.message) };
  }

  revalidatePath('/', 'layout');
  redirect('/');
}

export async function signUp(_: AuthState, form: FormData): Promise<AuthState> {
  const { email, password } = readCredentials(form);
  if (!email.includes('@')) {
    return { error: 'That does not look like an email address.' };
  }
  if (password.length < 8) {
    return { error: 'Use a password of at least 8 characters.' };
  }

  const origin = (await headers()).get('origin');
  const supabase = await supabaseServer();
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: { emailRedirectTo: `${origin}/auth/callback` },
  });
  if (error) {
    return { error: plainly(error.message) };
  }

  // With email confirmation on, Supabase returns a user but no session. Say
  // what happened rather than redirecting to a page that will bounce them
  // back to the login form with no explanation.
  if (!data.session) {
    return { sent: email };
  }

  revalidatePath('/', 'layout');
  redirect('/');
}

export async function signOut() {
  const supabase = await supabaseServer();
  await supabase.auth.signOut();
  revalidatePath('/', 'layout');
  redirect('/login');
}
