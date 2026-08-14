/**
 * Top-bar geometry — the centered search overlay, and how much room the
 * left-hand crew switcher may take without disturbing it.
 *
 * These live in their own module rather than in `App.tsx` because
 * `InstanceTabBar` needs the same numbers and `App.tsx` imports
 * `InstanceTabBar`; importing back would be a cycle. Two copies of the
 * constants would drift silently — the switcher would cap itself against a
 * search width the search no longer uses — so there is one definition and both
 * consumers read it.
 */

/** Gap held between the search overlay and whichever cluster is nearest it. */
export const TOPBAR_SEARCH_GAP = 12

/** Below this the overlay is dropped rather than shrunk any further. */
export const TOPBAR_SEARCH_MIN_WIDTH = 240

/**
 * Actions-cluster reach assumed for the first paint, before the clusters have
 * been measured. Chosen to reproduce the long-standing 360px initial gutter.
 */
export const TOPBAR_SEARCH_ASSUMED_ACTIONS = 348

/** Share of the viewport the overlay targets, before `TOPBAR_SEARCH_INSET`. */
export const TOPBAR_SEARCH_VIEWPORT_FRACTION = 1 / 3

/** Trimmed off the fractional target so the overlay is not exactly a third. */
export const TOPBAR_SEARCH_INSET = 40

/**
 * The header's own horizontal padding (`pl-3`). Part of the switcher budget
 * because the switcher starts after it, so its right edge in VIEWPORT
 * coordinates — which is what the gutter math compares against — is this plus
 * the switcher's own width.
 */
export const TOPBAR_HEADER_INLINE_PAD = 12

/**
 * Geometry for the centered top-bar search overlay. It is absolutely
 * positioned and centered on the VIEWPORT (left: 50vw), not flowed between the
 * brand and actions clusters, so it cannot be squeezed by its siblings — this
 * function has to keep it inside the gutter instead.
 *
 * Both inputs are the space a cluster CONSUMES from its side of the viewport
 * (`brand.right`, and `viewportWidth - actions.left`), not the cluster's bare
 * width: the header has its own horizontal padding, so measuring width alone
 * under-counts the occupied space and ate the whole TOPBAR_SEARCH_GAP.
 *   - `gutter` is the reserved space on EACH side (the larger of the two
 *     clusters plus the gap, mirrored because the overlay is center-anchored).
 *   - `width` is the resolved px width: a third of the viewport, but never
 *     wider than the space the clusters leave.
 *   - `visible` is only the floor: below TOPBAR_SEARCH_MIN_WIDTH the overlay is
 *     dropped rather than shrunk further, so `width` is never below the floor
 *     while the overlay is on screen.
 */
export function calculateTopbarSearchLayout(brandEdge: number, actionsEdge: number, viewportWidth: number) {
  const gutter = Math.ceil(Math.max(brandEdge, actionsEdge)) + TOPBAR_SEARCH_GAP
  const available = viewportWidth - (gutter * 2)
  const target = Math.round(viewportWidth * TOPBAR_SEARCH_VIEWPORT_FRACTION) - TOPBAR_SEARCH_INSET
  const width = Math.max(TOPBAR_SEARCH_MIN_WIDTH, Math.min(target, available))
  return { gutter, width, visible: available >= TOPBAR_SEARCH_MIN_WIDTH }
}

/**
 * The search overlay's intended width as a CSS expression — the same
 * `max(MIN, vw·FRACTION − INSET)` the function above resolves in JS, minus the
 * `available` clamp, which `crewSwitcherMaxWidth` is specifically built to keep
 * from binding (see the invariant there).
 */
export function topbarSearchWidthCss(): string {
  // 8 decimals, not 4: at 1920px a 33.3333% fraction lands ~1.2px away from the
  // `1/3` the JS resolver uses, which would leave the two disagreeing about the
  // overlay's width by a pixel at the widest viewports.
  const pct = (TOPBAR_SEARCH_VIEWPORT_FRACTION * 100).toFixed(8)
  return `max(${TOPBAR_SEARCH_MIN_WIDTH}px, ${pct}vw - ${TOPBAR_SEARCH_INSET}px)`
}

/**
 * Slack the bound gives back to absorb the resolver's integer rounding.
 *
 * `calculateTopbarSearchLayout` applies `Math.ceil` to the gutter (costing up to
 * 1px of `available`, doubled because the gutter is mirrored) and `Math.round` to
 * the fractional target (which can ask for up to 0.5px more than the exact
 * fraction). CSS cannot floor, so the cap concedes those pixels up front instead;
 * without this the invariant below misses by ~1px at some viewport widths, which
 * is exactly enough to shave a pixel off the overlay.
 */
const TOPBAR_ROUNDING_SLACK = 3

/**
 * How wide the inline crew switcher may grow before it would disturb the
 * centered search overlay — a CSS `max-width`, not a measured value.
 *
 * The overlay is centered on the viewport, so the switcher must stop short of
 * its left edge:
 *
 *   switcherWidth ≤ 50vw − searchWidth/2 − GAP − HEADER_PAD − SLACK
 *
 * Expressing this in CSS rather than measuring it is what keeps the switcher a
 * pure DOWNSTREAM consumer of the search geometry. `brandEdge` is an INPUT to
 * `calculateTopbarSearchLayout`, so a switcher width derived from that
 * function's OUTPUT would close a loop — pin a chip, the overlay drops, the
 * budget grows, another chip fits — and oscillate through the ResizeObserver
 * that feeds it. Depending on `vw` alone cannot loop.
 *
 * INVARIANT, worth the algebra because it is the whole reason this bound is
 * shaped this way: a switcher obeying it leaves the overlay EXACTLY as it would
 * be with no switcher at all — same width, same visibility, at every viewport.
 * Substituting the bound into the gutter math:
 *
 *   brandEdge = HEADER_PAD + switcherWidth ≤ 50vw − searchWidth/2 − GAP − SLACK
 *   gutter    = ceil(brandEdge) + GAP      ≤ 50vw − searchWidth/2 − SLACK + 1
 *   available = vw − 2·gutter              ≥ searchWidth + 2·SLACK − 2
 *
 * so where the switcher IS the wider cluster, `available` never becomes the
 * binding term in `width`. Where it is not — a narrow window, where the actions
 * capsule reaches further — the gutter is the actions cluster's and the switcher
 * changes nothing by construction. Note the overlay is already unrendered below
 * ~960px for that reason; the bound's job is not to rescue it there, only never
 * to be the cause. Pinned by `topbarLayout.test.ts` across viewport widths.
 */
export function crewSwitcherMaxWidth(): string {
  const inset = TOPBAR_SEARCH_GAP + TOPBAR_HEADER_INLINE_PAD + TOPBAR_ROUNDING_SLACK
  return `calc(50vw - ${topbarSearchWidthCss()} / 2 - ${inset}px)`
}

/**
 * `crewSwitcherMaxWidth` resolved for one viewport width, in px.
 *
 * The browser resolves the CSS form; this exists so the invariant above can be
 * asserted against `calculateTopbarSearchLayout` at real viewport widths, which
 * is the only way to catch the two drifting apart. Same constants, so the two
 * differ only by CSS's own decimal rounding.
 */
export function crewSwitcherMaxWidthPx(viewportWidth: number): number {
  const searchWidth = Math.max(
    TOPBAR_SEARCH_MIN_WIDTH,
    viewportWidth * TOPBAR_SEARCH_VIEWPORT_FRACTION - TOPBAR_SEARCH_INSET,
  )
  return (
    viewportWidth / 2
    - searchWidth / 2
    - (TOPBAR_SEARCH_GAP + TOPBAR_HEADER_INLINE_PAD + TOPBAR_ROUNDING_SLACK)
  )
}
