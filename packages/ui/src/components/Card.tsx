import type { ReactNode } from 'react';

/** The app's standard surface: a bordered, softly shadowed panel. */
export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`rounded-xl border border-slate-200/80 bg-white p-5 shadow-card transition-shadow duration-200 hover:shadow-card-hover ${className}`}
    >
      {children}
    </div>
  );
}
