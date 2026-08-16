'use client';

import { useEffect, useRef, useState } from 'react';
import { Button } from '@aether/ui';
import { api, ApiError } from '@/lib/api';

/**
 * The password re-prompt in front of a sensitive action.
 *
 * The API refuses to delete a chart, import charts in bulk, or export a whole record unless the
 * password has been confirmed recently, and answers `403 reauthentication_required` when it has
 * not. This is the clinician's side of that: confirm, and the action they asked for runs.
 *
 * Two things it deliberately is not:
 *
 * * **Not a sign-in.** Nothing here touches the token store. A 403 from the step-up gate means
 *   the sign-in is perfectly valid and only the *proof of who is at the keyboard* has gone
 *   stale, so failing this must never sign anyone out — which is exactly what would happen if
 *   the API had used a 401 and this component had reached for `tryRefresh`.
 * * **Not a security theatre banner.** It appears only when the API has actually refused
 *   something, and it says which action it is about, because a prompt that appears on a
 *   schedule is one people learn to type through without reading.
 *
 * The action is retried by the caller rather than by this component: only the caller knows what
 * was being attempted, and re-running it here would mean duplicating that knowledge in two
 * places for the sake of one callback.
 */
export function ReauthPrompt({
  action,
  onConfirmed,
  onCancel,
}: {
  /** What the clinician was doing, in their words: "export this record". */
  action: string;
  onConfirmed: () => void;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus moves here on appearance: this replaces the action the clinician just asked for, and
  // leaving focus on the button they pressed leaves a keyboard user with no indication that
  // anything is now waiting for them.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const confirm = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.reauthenticate(password);
      setPassword('');
      onConfirmed();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'That could not be confirmed. Check the connection and try again.',
      );
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={confirm}
      aria-labelledby="reauth-heading"
      className="mb-6 rounded-xl border border-amber-300 bg-amber-50 p-4"
    >
      <h2 id="reauth-heading" className="text-sm font-semibold text-amber-900">
        Confirm your password to {action}
      </h2>
      <p className="mt-1 text-sm text-amber-800">
        You are still signed in. This step is asked for because of what the action does to the
        record, and because it has been a while since your password was entered on this device.
      </p>
      <div className="mt-3 flex flex-wrap items-start gap-2">
        <label className="sr-only" htmlFor="reauth-password">
          Password
        </label>
        <input
          ref={inputRef}
          id="reauth-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="min-w-[14rem] flex-1 rounded-lg border border-amber-300 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
          aria-describedby={error ? 'reauth-error' : undefined}
          aria-invalid={error ? true : undefined}
        />
        <Button type="submit" disabled={busy || !password}>
          {busy ? 'Confirming…' : 'Confirm'}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
      {error && (
        <p id="reauth-error" role="alert" className="mt-2 text-sm font-medium text-red-700">
          {error}
        </p>
      )}
    </form>
  );
}
