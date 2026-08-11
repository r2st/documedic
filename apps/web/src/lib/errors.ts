// Turning a caught error into something a clinician can act on.

import { ApiError } from './api';

/**
 * The message to show for a failed request.
 *
 * An {@link ApiError} already carries a clinician-facing message written by the API, so it is
 * passed through untouched. Anything else means `fetch` itself threw — the request never got a
 * response — and the old per-page fallbacks ("Upload failed", "Check failed") described that as
 * though the server had rejected the work. It hadn't; it was never reached. That matters here:
 * the two cases need different next steps, and one of them leaves the clinician unsure whether
 * their action was recorded.
 *
 * @param err   the caught value
 * @param what  short noun phrase for the operation, e.g. `'the safety check'`, `'the upload'`
 */
export function requestErrorMessage(err: unknown, what: string): string {
  if (err instanceof ApiError) return err.message;
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    // The browser knows the request never left the machine, so we can say so without hedging.
    return `You appear to be offline, so ${what} could not be sent. Reconnect and try again.`;
  }
  // The request may have reached the server and only the response been lost, so this must not
  // promise that nothing was written — it tells them how to find out instead.
  return (
    `Could not reach the server, so ${what} may not have completed. Check your connection, ` +
    `then reload the page to see the current state before retrying.`
  );
}
