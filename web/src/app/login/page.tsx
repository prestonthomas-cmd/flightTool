import { redirect } from 'next/navigation';
import { currentUser } from '@/lib/supabase/server';
import { LoginForm } from '@/components/LoginForm';

export const metadata = { title: 'Sign in · Flight Price Watch' };

export default async function LoginPage() {
  // Already signed in: no reason to show a login form.
  if (await currentUser()) redirect('/');

  return (
    <main className="auth">
      <div className="auth-card">
        <h1>Flight Price Watch</h1>
        <p className="sub">
          Watch a fare, and find out when it is actually worth booking.
        </p>
        <LoginForm />
      </div>
    </main>
  );
}
