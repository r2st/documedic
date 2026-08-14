// The package's public surface, asserted as a whole.
//
// The point is the *shape* of the export list, not that any one component renders — the
// component specs alongside cover that. What this guards is the boundary the library is for:
// every consumer imports from here, so a component quietly added to the barrel is a component
// every surface in the product now depends on. Making the list explicit turns "should this be
// shared?" into a decision someone has to write down rather than a side effect of an export.

import { describe, expect, it } from 'vitest';

import * as ui from './index';

describe('@aether/ui public surface', () => {
  it('exports exactly the shared primitives', () => {
    expect(Object.keys(ui).sort()).toEqual([
      'Button',
      'Card',
      'ErrorBanner',
      'LoadingBlock',
      'Skeleton',
      'SkeletonCards',
      'SkeletonList',
    ]);
  });

  it('exports components, not values that merely happen to be defined', () => {
    for (const [name, exported] of Object.entries(ui)) {
      expect(typeof exported, `${name} should be a component`).toBe('function');
    }
  });

  it('takes no clinical vocabulary in its props', () => {
    // The line that decides what may live here: a component that knows about autonomy tiers,
    // drug-safety severities or extraction confidence bands belongs to the surface that shows
    // them. Those stay in apps/web/src/components, and this asserts the rule rather than
    // leaving it to the comment in index.ts.
    const source = Object.values(ui)
      .map((component) => component.toString())
      .join('\n');

    for (const term of ['autonomy', 'hard_block', 'cant_miss', 'confidence_band', 'severity']) {
      expect(source.toLowerCase(), `${term} is domain vocabulary`).not.toContain(term);
    }
  });
});
