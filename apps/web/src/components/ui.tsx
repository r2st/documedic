import type { ReactNode } from 'react';

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-200/80 bg-white p-5 shadow-card transition-shadow duration-200 hover:shadow-card-hover ${className}`}>
      {children}
    </div>
  );
}

export function Button({
  children,
  className = '',
  variant = 'primary',
  size = 'default',
  // HTML defaults an unspecified button inside a form to type="submit". Every submit button
  // in this app says so explicitly, so defaulting to "button" here means a control dropped
  // into a form later cannot silently submit it.
  type = 'button',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'default' | 'lg';
}) {
  const baseStyles = 'inline-flex items-center justify-center font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2';

  const variantStyles = {
    primary: 'bg-brand-600 text-white hover:bg-brand-700 active:bg-brand-800 focus-visible:ring-brand-500 shadow-sm hover:shadow',
    secondary: 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 hover:border-slate-400 active:bg-slate-100 focus-visible:ring-brand-500 shadow-sm',
    danger: 'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 focus-visible:ring-red-500 shadow-sm hover:shadow',
    ghost: 'text-slate-600 hover:text-slate-900 hover:bg-slate-100 active:bg-slate-200 focus-visible:ring-brand-500',
  }[variant];

  const sizeStyles = {
    sm: 'rounded-lg px-3 py-1.5 text-xs gap-1.5',
    default: 'rounded-lg px-4 py-2.5 text-sm gap-2',
    lg: 'rounded-xl px-6 py-3 text-base gap-2.5',
  }[size];

  return (
    <button
      type={type}
      className={`${baseStyles} ${variantStyles} ${sizeStyles} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

/**
 * Error message shown after a failed action.
 *
 * `role="alert"` is the point: these appear in response to something the clinician just did,
 * often far from where focus is, so a screen reader has to be told rather than left to
 * discover them. The icon is decorative — the message carries the meaning.
 */
export function ErrorBanner({ message, className = '' }: { message: string; className?: string }) {
  return (
    <div
      role="alert"
      className={`flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200 ${className}`}
    >
      <svg
        aria-hidden="true"
        className="mt-0.5 h-4 w-4 flex-shrink-0"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
      </svg>
      <span>{message}</span>
    </div>
  );
}

const CONFIDENCE_STYLES: Record<string, string> = {
  high: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  medium: 'bg-amber-50 text-amber-700 ring-1 ring-amber-200',
  low: 'bg-red-50 text-red-700 ring-1 ring-red-200',
};

export function ConfidenceBadge({ band, value }: { band: string; value: number }) {
  // The band is conveyed visually by colour alone, and a bare "85%" next to an extracted
  // value does not say what it is a percentage of. The label carries both.
  return (
    <span
      aria-label={`Extraction confidence ${band}, ${(value * 100).toFixed(0)} percent`}
      className={`inline-block rounded-md px-2 py-0.5 text-xs font-semibold ${
        CONFIDENCE_STYLES[band] ?? 'bg-slate-50 text-slate-600 ring-1 ring-slate-200'
      }`}
    >
      {(value * 100).toFixed(0)}%
    </span>
  );
}

const SEVERITY_STYLES: Record<string, string> = {
  hard_block: 'border-red-600 bg-red-50 text-red-900',
  critical: 'border-red-500 bg-red-50 text-red-800',
  warning: 'border-amber-500 bg-amber-50 text-amber-900',
  info: 'border-brand-500 bg-brand-50 text-brand-900',
};

export function SafetyFlagCard({
  severity,
  isHardBlock,
  summary,
}: {
  severity: string;
  isHardBlock: boolean;
  summary: string;
}) {
  return (
    <div className={`rounded-lg border-l-4 p-4 text-sm ${SEVERITY_STYLES[severity] ?? ''}`}>
      <span className="mr-2 text-xs font-bold uppercase tracking-wider">
        {isHardBlock ? 'HARD BLOCK' : severity.replace(/_/g, ' ')}
      </span>
      {summary}
    </div>
  );
}
