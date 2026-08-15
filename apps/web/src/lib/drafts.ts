// Clinical text a clinician has composed but not yet sent.
//
// This app signs a clinician out after fifteen minutes with no mouse, keyboard or touch activity
// (`IDLE_LOGOUT_MS` in lib/auth.tsx), and the backend ends the session on its own absolute
// lifetime regardless of what the browser is doing. Both are correct for a shared clinical
// workstation and both land in the same place: `setAccount(null)`, a redirect to /login, and the
// encounter screen unmounts.
//
// Which takes the presenting complaint with it. Typing resets the idle timer, so the clinician
// who loses work is not the one at the keyboard — it is the one who wrote three paragraphs of
// history, turned to examine the patient, and came back to a login form. Nothing had been sent
// to the server yet, because the complaint is not sent until "Begin intake", so there is nothing
// to recover it from.
//
// Why memory and not sessionStorage
// ---------------------------------
// A draft is patient history: identifiable, clinical, and covered by the DPDP Act's data
// minimisation. sessionStorage would let it survive a reload, and would also let it outlive the
// sign-out that idle timeout exists to perform — the next person to use the workstation would
// find the previous clinician's notes waiting in a form. So drafts live in module scope: they
// survive the unmount, the route change and the re-login inside one tab, and they are gone the
// moment that tab is closed or reloaded.
//
// They are also dropped on an explicit sign-out (a deliberate end of work, unlike a timeout) and
// whenever a different account signs in — see `clearDrafts`, called from lib/auth.tsx.

const drafts = new Map<string, string>();

// Who the stored drafts belong to. Module scope, deliberately: this is the same lifetime as
// `drafts` itself, so the notes and the identity that owns them can never come apart. Held in a
// component ref instead, a remounted provider would start with no memory of who was last signed
// in while the notes it is guarding were still sitting in this module.
let owner: string | null = null;

/** The draft key for a screen's field, namespaced by whatever the screen is about. */
export function draftKey(scope: string, field: string): string {
  return `${scope}:${field}`;
}

/**
 * Remember (or, for empty text, forget) a draft.
 *
 * Empty clears rather than storing "", so a field the clinician deliberately emptied does not
 * come back filled the next time the screen mounts.
 */
export function saveDraft(key: string, value: string): void {
  if (value) drafts.set(key, value);
  else drafts.delete(key);
}

/** The stored draft for a key, or '' when there is none. */
export function readDraft(key: string): string {
  return drafts.get(key) ?? '';
}

/** Forget one draft — called when its content has been successfully sent to the server. */
export function discardDraft(key: string): void {
  drafts.delete(key);
}

/**
 * Forget everything.
 *
 * Called on an explicit sign-out — a decision to stop working, unlike being timed out.
 */
export function clearDrafts(): void {
  drafts.clear();
  owner = null;
}

/**
 * Record who is signed in, dropping every draft if that is somebody new.
 *
 * The case this exists for: a clinician is timed out mid-note (drafts kept, by design), walks
 * away, and the next clinician signs in on the same tab. Without this their colleague's
 * patient history would be waiting in the form — a disclosure to someone who never opened that
 * chart, produced by a feature meant to prevent lost work.
 */
export function retainDraftsFor(accountId: string): void {
  if (owner !== null && owner !== accountId) drafts.clear();
  owner = accountId;
}
