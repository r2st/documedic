import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ReauthPrompt } from './ReauthPrompt';
import { ApiError, api, tokenStore } from '@/lib/api';

/**
 * The clinician's side of the step-up gate.
 *
 * The behaviour worth protecting is what it does *not* do: it is not a sign-in, so nothing here
 * may touch the token store, and a wrong password must leave the clinician exactly as signed in
 * as they were. That is the difference the API's choice of 403 over 401 exists to preserve, and
 * a component that cleared tokens on failure would throw it away on this side of the wire.
 */
describe('ReauthPrompt', () => {
  beforeEach(() => {
    localStorage.clear();
    tokenStore.set({
      access_token: 'a1',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it('names the action it is asking about', () => {
    // A prompt that just says "confirm your password" gives the clinician no way to tell a
    // legitimate step-up from a phishing-shaped interruption.
    render(
      <ReauthPrompt
        action="export this record as a PDF"
        onConfirmed={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('heading', { name: /export this record as a PDF/i }),
    ).toBeInTheDocument();
  });

  it('takes focus, so a keyboard user knows something is waiting on them', () => {
    render(<ReauthPrompt action="export this record" onConfirmed={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByLabelText('Password')).toHaveFocus();
  });

  it('will not submit an empty password', async () => {
    const reauthenticate = vi.spyOn(api, 'reauthenticate');
    render(<ReauthPrompt action="export this record" onConfirmed={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled();
    expect(reauthenticate).not.toHaveBeenCalled();
  });

  it('confirms and hands control back so the caller can retry what it was doing', async () => {
    const user = userEvent.setup();
    const reauthenticate = vi.spyOn(api, 'reauthenticate').mockResolvedValue({
      authenticated_at: '2026-08-15T10:00:00Z',
      valid_until: '2026-08-15T10:15:00Z',
      valid_for_seconds: 900,
    });
    const onConfirmed = vi.fn();
    render(
      <ReauthPrompt action="export this record" onConfirmed={onConfirmed} onCancel={vi.fn()} />,
    );

    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(onConfirmed).toHaveBeenCalled());
    expect(reauthenticate).toHaveBeenCalledWith('password123');
  });

  it('reports a wrong password without signing anyone out', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'reauthenticate').mockRejectedValue(
      new ApiError(401, 'invalid_credentials', 'That password was not recognised.'),
    );
    const onConfirmed = vi.fn();
    render(
      <ReauthPrompt action="export this record" onConfirmed={onConfirmed} onCancel={vi.fn()} />,
    );

    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('not recognised');
    expect(onConfirmed).not.toHaveBeenCalled();
    // Still signed in. The sign-in was never in question — only the proof of who is at the
    // keyboard was — so a mistyped password must cost nothing but a retry.
    expect(tokenStore.access).toBe('a1');
    expect(tokenStore.refresh).toBe('r1');
  });

  it('says something actionable when the request never reached the API', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'reauthenticate').mockRejectedValue(new Error('network down'));
    render(<ReauthPrompt action="export this record" onConfirmed={vi.fn()} onCancel={vi.fn()} />);

    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/connection/i);
  });

  it('can be dismissed without confirming', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<ReauthPrompt action="export this record" onConfirmed={vi.fn()} onCancel={onCancel} />);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onCancel).toHaveBeenCalled();
  });
});
