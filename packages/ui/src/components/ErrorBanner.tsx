import { Button } from './Button';

/**
 * Error message shown after a failed action.
 *
 * `role="alert"` is the point: these appear in response to something the clinician just did,
 * often far from where focus is, so a screen reader has to be told rather than left to
 * discover them. The icon is decorative — the message carries the meaning.
 *
 * Pass `onRetry` when the failed thing can simply be asked for again — a read that fetched
 * nothing, rather than a write that may or may not have landed. Without it a clinician whose
 * chart failed to load has only the browser's reload button, which throws away everything
 * else on the page to retry the one request that failed. Leaving it off is a deliberate
 * choice too: a write with no response may already have been applied, and a retry button on
 * one of those is how the same record is written twice.
 */
export function ErrorBanner({
  message,
  className = '',
  onRetry,
  retrying = false,
  retryLabel = 'Try again',
}: {
  message: string;
  className?: string;
  onRetry?: () => void;
  retrying?: boolean;
  retryLabel?: string;
}) {
  return (
    <div
      role="alert"
      className={`flex flex-col gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200 sm:flex-row sm:items-start ${className}`}
    >
      <div className="flex flex-1 items-start gap-2">
        <svg
          aria-hidden="true"
          className="mt-0.5 h-4 w-4 flex-shrink-0"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"
          />
        </svg>
        <span>{message}</span>
      </div>
      {onRetry && (
        <Button
          variant="secondary"
          size="sm"
          className="flex-shrink-0 self-start"
          onClick={onRetry}
          disabled={retrying}
          aria-busy={retrying}
        >
          {retrying ? 'Retrying…' : retryLabel}
        </Button>
      )}
    </div>
  );
}
