// Shared component library.
//
// What lives here is deliberately narrow: presentational primitives with no clinical
// vocabulary in their props and no dependency on an app's API client, router or types. That
// line is what makes them safe to share — a component that knows about autonomy tiers, drug
// safety severities or extraction confidence bands belongs to the surface that shows them,
// not to the design system, and those stay in `apps/web/src/components`.

export { Button } from './components/Button';
export { Card } from './components/Card';
export { ErrorBanner } from './components/ErrorBanner';
export { LoadingBlock, Skeleton, SkeletonCards, SkeletonList } from './components/Skeleton';
