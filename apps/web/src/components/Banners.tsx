'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/lib/auth';

/** Persistent demo banner — re-appears every session (Critical Safety Rule / P1-11a). */
export function DemoBanner() {
  if (process.env.NEXT_PUBLIC_DEMO_MODE !== 'true') return null;
  return (
    <div className="bg-gradient-to-r from-amber-600 to-amber-500 px-4 py-2 text-center text-sm font-medium text-amber-950 shadow-sm">
      <div className="mx-auto flex max-w-5xl items-center justify-center gap-2">
        <svg
          aria-hidden="true"
          className="h-4 w-4 flex-shrink-0"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
          />
        </svg>
        <span>Demo build — decision-support only, not for real patient care.</span>
      </div>
    </div>
  );
}

/** Connectivity indicator. Offline: record viewing + drug-safety checks remain available. */
export function OfflineBanner() {
  const [online, setOnline] = useState(true);

  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, []);

  // Losing connectivity happens mid-task, triggered by the network rather than by anything the
  // clinician did, and it changes which features work — so it is announced, not just drawn.
  //
  // The live region is mounted unconditionally and filled when offline, rather than mounted
  // together with its message. A region that appears already populated is unreliably announced:
  // several screen readers only watch regions that were in the accessibility tree before the
  // text changed, so returning null for the whole element would lose the announcement in
  // exactly the case it matters.
  return (
    <div role="status" aria-live="polite">
      {!online && (
        <div className="bg-slate-800 px-4 py-2 text-center text-sm font-medium text-slate-200 shadow-sm">
          <div className="mx-auto flex max-w-5xl items-center justify-center gap-2">
            <svg
              aria-hidden="true"
              className="h-4 w-4 flex-shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M3 3l8.735 8.735m0 0a.374.374 0 11.53.53m-.53-.53l.53.53m0 0L21 21M14.652 9.348a3.75 3.75 0 010 5.304m2.121-7.425a6.75 6.75 0 010 9.546m2.121-11.667C21.415 7.627 23.25 11.25 23.25 12c0 .75-1.835 4.373-4.356 5.894"
              />
            </svg>
            <span>
              Offline Mode — patient records and drug-safety checks available. AI features paused.
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Notice that the idle timeout is about to sign this clinician out.
 *
 * Fifteen minutes without mouse, keyboard or touch ends the session — correct for a shared
 * clinical workstation, and until this banner existed it happened with no notice at all. The
 * clinician it happens to is by definition the one not at the keyboard: typing resets the idle
 * timer, so the person who loses a half-written history is the one who turned away to examine
 * the patient. They came back to a login form.
 *
 * Two things follow from that, and this is the first: say so before it happens, with a count and
 * a way to stop it. The second is that the draft survives the sign-out anyway — see
 * lib/drafts.ts — because a warning nobody was in the room to read must not still cost the note.
 *
 * `role="alert"` rather than `status`: this is time-critical and the clinician may not be looking
 * at the screen. Mounted only while the warning is live, unlike OfflineBanner's always-mounted
 * region, because an alert region is announced on insertion — which is the behaviour wanted here
 * and the opposite of what a polite status region needs.
 */
export function SessionExpiryBanner() {
  const { account, idleSecondsRemaining, refresh } = useAuth();
  // A number, and nothing else, means the warning is live. Tested against the type rather than
  // against `null` because this banner is rendered from the dashboard shell and reads whatever
  // the auth context provides: an older or stubbed context with no such field would otherwise
  // put a "signing out in undefined" alert over every screen, permanently.
  if (!account || typeof idleSecondsRemaining !== 'number') return null;

  const minutes = Math.floor(idleSecondsRemaining / 60);
  const seconds = idleSecondsRemaining % 60;
  const remaining = minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;

  return (
    <div role="alert" className="bg-amber-950/50 px-4 py-2.5 text-sm text-amber-300 shadow-sm">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-center gap-2 sm:flex-row">
        <span className="text-center font-medium">
          Signing out in {remaining} — you have been inactive. Anything you have typed but not yet
          submitted is kept on this tab.
        </span>
        {/* Any activity at all cancels the timeout, so this button's own click already does the
            job; it calls refresh so the clinician gets an explicit confirmation rather than
            having to infer one from the banner disappearing. */}
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded-lg bg-[#F0B429] px-3 py-1 text-xs font-semibold text-[#0A0A0B] hover:bg-[#F7D070]"
        >
          Stay signed in
        </button>
      </div>
    </div>
  );
}
