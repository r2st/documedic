'use client';

import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from '@aether/ui';

interface Props {
  /**
   * What is missing when this boundary trips, as a noun phrase: `'the patient chart'`,
   * `'the reasoning output'`. It goes straight into the message the clinician reads, so it
   * has to name the clinical thing rather than the component.
   */
  section: string;
  children: ReactNode;
  /**
   * Run after a reset, for an owner that needs to re-fetch. Clearing the error alone only
   * re-renders from the state that already crashed once, which for a bad payload means
   * crashing straight back — the retry has to be able to go and get different data.
   */
  onReset?: () => void;
}

interface State {
  error: Error | null;
}

/**
 * Stops one component's render failure from blanking the screen.
 *
 * React unmounts the whole tree when a render throws, so before this a single malformed
 * suggestion — a field the API stopped sending, a null where a number was assumed — took the
 * entire dashboard with it and left the clinician on a white page mid-consultation, with no
 * way back to the chart except the browser's back button.
 *
 * The fallback copy is the part that matters clinically, and it is why this is not the usual
 * "Something went wrong." A section that fails silently, or that is replaced by an
 * apologetic blank, reads exactly like a section that had nothing to report — which for a
 * differential, a can't-miss flag or a drug-safety panel is the most dangerous thing this UI
 * could imply. So the message says outright that content is missing and must not be read as
 * an absence of findings. Same reasoning as CLAUDE.md rule #6: the clinician is never left to
 * infer a clinical conclusion from what the UI happens not to be showing.
 *
 * A class is not a style choice here — `getDerivedStateFromError`/`componentDidCatch` have no
 * hook equivalent, so an error boundary cannot be a function component. That is why the
 * project's functional-components-only rule is suppressed on the next line and nowhere else:
 * React has never shipped a hooks API for catching a render error, so the alternative to this
 * class is not a nicer boundary, it is no boundary.
 */
// eslint-disable-next-line no-restricted-syntax -- see above: React offers no hook for this.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The only record that this happened. Nothing here goes to a remote collector: a React
    // component stack from this app names patient-facing components and can carry rendered
    // props, and DPDP data residency (CLAUDE.md pitfall #9) rules out shipping that offshore.
    console.error(`Render failed in ${this.props.section}:`, error, info.componentStack);
  }

  private reset = (): void => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render(): ReactNode {
    if (this.state.error === null) return this.props.children;

    return (
      <div
        role="alert"
        className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm text-red-900"
      >
        <div className="flex items-start gap-3">
          <svg
            aria-hidden="true"
            className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-500"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
            />
          </svg>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold">{this.props.section} could not be displayed</h2>
            <p className="mt-1 text-red-800">
              Something on this screen failed to render. Nothing here is a clinical finding — treat
              this section as missing, not as empty, and do not read it as &ldquo;nothing to
              report&rdquo;.
            </p>
            <p className="mt-1 text-red-800">
              The record itself is unaffected and nothing was changed. Try again, or reload the
              page.
            </p>
            <Button variant="secondary" size="sm" className="mt-3" onClick={this.reset}>
              Try again
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
