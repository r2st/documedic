import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from './api';
import { requestErrorMessage } from './errors';

function setOnline(value: boolean | undefined) {
  if (value === undefined) {
    vi.unstubAllGlobals();
    return;
  }
  vi.stubGlobal('navigator', { ...globalThis.navigator, onLine: value });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('requestErrorMessage', () => {
  it('passes an API error through untouched', () => {
    const err = new ApiError(422, 'validation_error', 'Consent must be recorded first.');
    expect(requestErrorMessage(err, 'the upload')).toBe('Consent must be recorded first.');
  });

  it('says the request was never sent when the browser reports being offline', () => {
    setOnline(false);
    const message = requestErrorMessage(new TypeError('Failed to fetch'), 'the upload');
    expect(message).toBe(
      'You appear to be offline, so the upload could not be sent. Reconnect and try again.',
    );
  });

  it('does not claim nothing was written when the server was merely unreachable', () => {
    // The request may have been processed and only the response lost, so the wording has to
    // stay honest — and point at how to find out.
    setOnline(true);
    const message = requestErrorMessage(new TypeError('Failed to fetch'), 'the upload');
    expect(message).toContain('may not have completed');
    expect(message).toContain('reload the page');
    expect(message).not.toContain('nothing was saved');
  });

  it('names the operation that failed, so a toast is readable on its own', () => {
    setOnline(true);
    expect(requestErrorMessage(new Error('boom'), 'the drug safety check')).toContain(
      'the drug safety check',
    );
  });

  it('never surfaces raw transport jargon to the clinician', () => {
    setOnline(true);
    const message = requestErrorMessage(
      new TypeError('NetworkError when attempting to fetch'),
      'x',
    );
    expect(message).not.toContain('NetworkError');
    expect(message).not.toContain('fetch');
  });

  it('handles a thrown value that is not an Error', () => {
    setOnline(true);
    expect(requestErrorMessage('boom', 'the upload')).toContain('Could not reach the server');
  });

  it('falls back to the unreachable wording when navigator is unavailable (SSR)', () => {
    vi.stubGlobal('navigator', undefined);
    expect(requestErrorMessage(new Error('boom'), 'the upload')).toContain(
      'Could not reach the server',
    );
  });
});
