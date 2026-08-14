import { describe, it, expect } from 'vitest'
import {
  calculateTopbarSearchLayout,
  crewSwitcherMaxWidth,
  crewSwitcherMaxWidthPx,
  topbarSearchWidthCss,
  TOPBAR_SEARCH_ASSUMED_ACTIONS,
  TOPBAR_SEARCH_GAP,
  TOPBAR_SEARCH_INSET,
  TOPBAR_SEARCH_MIN_WIDTH,
  TOPBAR_SEARCH_VIEWPORT_FRACTION,
  TOPBAR_HEADER_INLINE_PAD,
} from '../lib/topbarLayout'

/** The overlay width the resolver targets before any clamping, per viewport. */
const target = (vw: number) => Math.round(vw * TOPBAR_SEARCH_VIEWPORT_FRACTION) - TOPBAR_SEARCH_INSET

// Widths spanning the interesting range: the desktop floor, the point where the
// overlay would otherwise start dropping out, common laptops, and a wide display.
const VIEWPORTS = [768, 900, 960, 1024, 1280, 1440, 1680, 1920, 2560]

describe('calculateTopbarSearchLayout', () => {
  it('mirrors whichever cluster reaches further, because the overlay is centered', () => {
    // Same vectors the App-level test pins, kept here so a change to the shared
    // module is caught by the module's own suite too.
    expect(calculateTopbarSearchLayout(330, 180, 1200)).toEqual({ gutter: 342, width: 360, visible: true })
    expect(calculateTopbarSearchLayout(180, 505, 1570)).toEqual({ gutter: 517, width: 483, visible: true })
    expect(calculateTopbarSearchLayout(330, 180, 900)).toEqual({ gutter: 342, width: 240, visible: false })
  })

  it('drops the overlay rather than shrinking it below the floor', () => {
    const { width, visible } = calculateTopbarSearchLayout(600, 600, 1400)
    expect(visible).toBe(false)
    expect(width).toBe(TOPBAR_SEARCH_MIN_WIDTH)
  })
})

describe('crewSwitcherMaxWidth', () => {
  it('is expressed against the overlay width, not a hardcoded viewport fraction', () => {
    // A literal `42vw`-style cap cannot track a viewport-relative overlay: it is
    // too generous on narrow windows (squeezing the overlay off screen) and too
    // mean on wide ones. Naming the overlay's own width is what keeps the two
    // in step at every width.
    expect(crewSwitcherMaxWidth()).toContain(topbarSearchWidthCss())
    expect(crewSwitcherMaxWidth()).toContain('50vw')
  })

  it('grows with the viewport instead of staying constant', () => {
    const widths = VIEWPORTS.map(crewSwitcherMaxWidthPx)
    for (let i = 1; i < widths.length; i++) {
      expect(widths[i]).toBeGreaterThan(widths[i - 1])
    }
  })

  // THE invariant. A switcher obeying this cap leaves the overlay exactly as it
  // would be with no switcher at all — which is the whole reason the cap is
  // derived from the overlay's geometry rather than measured back from the
  // layout it participates in.
  it.each(VIEWPORTS)('leaves the overlay identical to having no switcher at %ipx', vw => {
    const withSwitcher = calculateTopbarSearchLayout(
      TOPBAR_HEADER_INLINE_PAD + crewSwitcherMaxWidthPx(vw),
      TOPBAR_SEARCH_ASSUMED_ACTIONS,
      vw,
    )
    const withoutSwitcher = calculateTopbarSearchLayout(0, TOPBAR_SEARCH_ASSUMED_ACTIONS, vw)
    expect(withSwitcher.width).toBe(withoutSwitcher.width)
    expect(withSwitcher.visible).toBe(withoutSwitcher.visible)
  })

  // Below ~960px the actions capsule reaches further than the cap allows the
  // switcher to, so the gutter is the capsule's and the switcher cannot affect
  // the overlay by construction. Only where the switcher IS the binding cluster
  // does the algebra in the doc comment have to hold.
  const BINDING = VIEWPORTS.filter(
    vw => TOPBAR_HEADER_INLINE_PAD + crewSwitcherMaxWidthPx(vw) > TOPBAR_SEARCH_ASSUMED_ACTIONS,
  )

  it('is the binding cluster on ordinary desktop widths', () => {
    // Guards the filter above from silently emptying and voiding the next test.
    expect(BINDING).toContain(1280)
    expect(BINDING.length).toBeGreaterThan(3)
  })

  it.each(BINDING)('keeps available space at or above the overlay width at %ipx', vw => {
    const brandEdge = TOPBAR_HEADER_INLINE_PAD + crewSwitcherMaxWidthPx(vw)
    const gutter = Math.ceil(Math.max(brandEdge, TOPBAR_SEARCH_ASSUMED_ACTIONS)) + TOPBAR_SEARCH_GAP
    const available = vw - gutter * 2
    const searchWidth = Math.max(TOPBAR_SEARCH_MIN_WIDTH, target(vw))
    expect(available).toBeGreaterThanOrEqual(searchWidth)
  })

  it('is what a fixed 42vw cap fails to satisfy on a common laptop', () => {
    // Regression guard for the bound this replaced: at 1280px a 42vw chip row
    // reaches far enough to push `available` under the floor and unmount the
    // overlay entirely. Keeping the failing case in the suite states WHY the
    // cap is derived rather than picked.
    const vw = 1280
    const fixedCap = 0.42 * vw
    const fixed = calculateTopbarSearchLayout(
      TOPBAR_HEADER_INLINE_PAD + fixedCap,
      TOPBAR_SEARCH_ASSUMED_ACTIONS,
      vw,
    )
    expect(fixed.visible).toBe(false)

    const derived = calculateTopbarSearchLayout(
      TOPBAR_HEADER_INLINE_PAD + crewSwitcherMaxWidthPx(vw),
      TOPBAR_SEARCH_ASSUMED_ACTIONS,
      vw,
    )
    expect(derived.visible).toBe(true)
  })
})
