import { beforeEach, describe, expect, it } from 'vitest';

import {
  clearDrafts,
  discardDraft,
  draftKey,
  readDraft,
  retainDraftsFor,
  saveDraft,
} from './drafts';

describe('the draft store', () => {
  beforeEach(() => {
    clearDrafts();
  });

  it('returns the text that was saved under a key', () => {
    saveDraft('encounter:p1:complaint', '54-year-old with central chest pain for 1 hour');
    expect(readDraft('encounter:p1:complaint')).toBe(
      '54-year-old with central chest pain for 1 hour',
    );
  });

  it('has nothing to return for a key that was never written', () => {
    expect(readDraft('encounter:p1:complaint')).toBe('');
  });

  it('namespaces one screen against another', () => {
    saveDraft(draftKey('encounter:p1', 'complaint'), 'chest pain');
    saveDraft(draftKey('encounter:p2', 'complaint'), 'fever');
    expect(readDraft(draftKey('encounter:p1', 'complaint'))).toBe('chest pain');
    expect(readDraft(draftKey('encounter:p2', 'complaint'))).toBe('fever');
  });

  it('forgets a field the clinician deliberately emptied', () => {
    // Storing '' would make the next mount restore an empty string over whatever the screen's
    // own initial value is — a distinction with no difference today, and a restored blank
    // overwriting a real value the day a screen gets one.
    saveDraft('k', 'typed something');
    saveDraft('k', '');
    expect(readDraft('k')).toBe('');
  });

  it('discards one draft without touching the others', () => {
    saveDraft('a', 'one');
    saveDraft('b', 'two');
    discardDraft('a');
    expect(readDraft('a')).toBe('');
    expect(readDraft('b')).toBe('two');
  });

  it('clears everything at once', () => {
    saveDraft('a', 'one');
    saveDraft('b', 'two');
    clearDrafts();
    expect(readDraft('a')).toBe('');
    expect(readDraft('b')).toBe('');
  });

  it('keeps a draft across a re-login by the same clinician', () => {
    // The whole purpose. Timed out mid-note, signs back in, gets the note back.
    retainDraftsFor('acc-1');
    saveDraft('k', 'chest pain since this morning');
    retainDraftsFor('acc-1');
    expect(readDraft('k')).toBe('chest pain since this morning');
  });

  it('drops every draft when a different clinician signs in', () => {
    retainDraftsFor('acc-1');
    saveDraft('k', 'chest pain since this morning');
    retainDraftsFor('acc-2');
    expect(readDraft('k')).toBe('');
  });

  it('keeps a draft written before anyone was recorded as the owner', () => {
    // First sign-in of the tab: nothing was there to belong to anyone else.
    saveDraft('k', 'chest pain');
    retainDraftsFor('acc-1');
    expect(readDraft('k')).toBe('chest pain');
  });

  it('never writes to browser storage', () => {
    // The reason this store is a Map and not sessionStorage. A draft is patient history, and
    // storage would let it outlive both the tab and the sign-out that the idle timeout exists to
    // perform — leaving one clinician's notes in a form for whoever sits down next.
    saveDraft('encounter:p1:complaint', 'crushing chest pain radiating to the jaw');
    expect(JSON.stringify(sessionStorage)).not.toContain('crushing chest pain');
    expect(JSON.stringify(localStorage)).not.toContain('crushing chest pain');
  });
});
