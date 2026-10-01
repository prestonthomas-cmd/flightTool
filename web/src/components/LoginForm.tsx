'use client';

import { useActionState, useState } from 'react';
import { useFormStatus } from 'react-dom';
import { signIn, signUp, type AuthState } from '@/lib/auth-actions';

function Submit({ label }: { label: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="go" disabled={pending}>
      {pending ? 'One moment...' : label}
    </button>
  );
}

export function LoginForm() {
  const [mode, setMode] = useState<'in' | 'up'>('in');
  const action = mode === 'in' ? signIn : signUp;
  const [state, run] = useActionState<AuthState, FormData>(action, {});

  if (state.sent) {
    return (
      <div className="said">
        <p>
          Check <b>{state.sent}</b> for a confirmation link. Once you have
          clicked it you can sign in.
        </p>
      </div>
    );
  }

  return (
    <form action={run} className="stack">
      <div className="field">
        <label htmlFor="email">Email</label>
        <input
          id="email"
          name="email"
          type="email"
          required
          autoComplete="email"
          autoCapitalize="off"
          spellCheck={false}
        />
      </div>

      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          required
          minLength={8}
          autoComplete={mode === 'in' ? 'current-password' : 'new-password'}
        />
      </div>

      {state.error ? (
        <p className="said bad" role="alert">
          {state.error}
        </p>
      ) : null}

      <Submit label={mode === 'in' ? 'Sign in' : 'Create account'} />

      <p className="swap">
        {mode === 'in' ? 'No account yet?' : 'Already have an account?'}{' '}
        <button
          type="button"
          className="link"
          onClick={() => setMode(mode === 'in' ? 'up' : 'in')}
        >
          {mode === 'in' ? 'Create one' : 'Sign in'}
        </button>
      </p>
    </form>
  );
}
