'use client';

import { useEffect, useState } from 'react';

/** Persistent demo banner — re-appears every session (Critical Safety Rule / P1-11a). */
export function DemoBanner() {
  if (process.env.NEXT_PUBLIC_DEMO_MODE !== 'true') return null;
  return (
    <div className="bg-gradient-to-r from-amber-500 to-amber-400 px-4 py-2 text-center text-sm font-medium text-amber-950 shadow-sm">
      <div className="mx-auto flex max-w-5xl items-center justify-center gap-2">
        <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
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

  if (online) return null;
  return (
    <div className="bg-slate-800 px-4 py-2 text-center text-sm font-medium text-slate-200 shadow-sm">
      <div className="mx-auto flex max-w-5xl items-center justify-center gap-2">
        <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 3l8.735 8.735m0 0a.374.374 0 11.53.53m-.53-.53l.53.53m0 0L21 21M14.652 9.348a3.75 3.75 0 010 5.304m2.121-7.425a6.75 6.75 0 010 9.546m2.121-11.667C21.415 7.627 23.25 11.25 23.25 12c0 .75-1.835 4.373-4.356 5.894" />
        </svg>
        <span>Offline Mode — patient records and drug-safety checks available. AI features paused.</span>
      </div>
    </div>
  );
}
