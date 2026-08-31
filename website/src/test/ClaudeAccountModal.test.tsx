import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ClaudeAccountModal from '../components/ClaudeAccountModal'
import type { ClaudeUsage } from '../api/client'
import { renderWithProviders } from './helpers'

const BASE_USAGE: ClaudeUsage = {
  totalCostUsd: 0.42,
  since: Date.UTC(2026, 7, 1, 0, 0) / 1000,
  rateLimit: null,
  consoleUrl: 'https://console.anthropic.com/settings/usage',
}

describe('ClaudeAccountModal', () => {
  it('shows a spinner while the usage query is still loading', () => {
    renderWithProviders(<ClaudeAccountModal open onClose={vi.fn()} usage={null} />)
    expect(screen.getByText('Checking usage…')).toBeInTheDocument()
  })

  it('shows the unavailable notice on a failed fetch with nothing cached', () => {
    renderWithProviders(<ClaudeAccountModal open onClose={vi.fn()} usage="failed" />)
    expect(screen.getByText('Usage unavailable')).toBeInTheDocument()
  })

  it('renders the accumulated spend, tracking-since date, and disclaimer — never as a ratio', () => {
    renderWithProviders(<ClaudeAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />)
    expect(screen.getByText('$0.42')).toBeInTheDocument()
    // Never "X/Y" or a progress bar: there is no known ceiling to divide by.
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText(/this fork's own estimate/)).toBeInTheDocument()
    expect(screen.getByText(/console\.anthropic\.com/)).toBeInTheDocument()
  })

  it('links to the console URL the payload supplied', () => {
    renderWithProviders(<ClaudeAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />)
    expect(screen.getByRole('link', { name: /Open Anthropic console/ })).toHaveAttribute(
      'href',
      'https://console.anthropic.com/settings/usage',
    )
  })

  it('says nothing extra when the rate-limit status is plain "allowed"', () => {
    renderWithProviders(
      <ClaudeAccountModal open onClose={vi.fn()} usage={{ ...BASE_USAGE, rateLimit: { status: 'allowed' } }} />,
    )
    expect(screen.queryByText('Approaching a Claude usage limit')).not.toBeInTheDocument()
    expect(screen.queryByText('Claude usage limit reached')).not.toBeInTheDocument()
  })

  it('surfaces a real Anthropic warning when the adapter reports allowed_warning', () => {
    renderWithProviders(
      <ClaudeAccountModal
        open
        onClose={vi.fn()}
        usage={{
          ...BASE_USAGE,
          rateLimit: { status: 'allowed_warning', utilization: 87, rateLimitType: 'five_hour' },
        }}
      />,
    )
    expect(screen.getByText('Approaching a Claude usage limit')).toBeInTheDocument()
    expect(screen.getByText(/5-hour window/)).toBeInTheDocument()
    expect(screen.getByText(/87% used/)).toBeInTheDocument()
  })

  it('surfaces a rejected limit distinctly from a warning', () => {
    renderWithProviders(
      <ClaudeAccountModal
        open
        onClose={vi.fn()}
        usage={{ ...BASE_USAGE, rateLimit: { status: 'rejected', rateLimitType: 'seven_day' } }}
      />,
    )
    expect(screen.getByText('Claude usage limit reached')).toBeInTheDocument()
    expect(screen.queryByText('Approaching a Claude usage limit')).not.toBeInTheDocument()
    expect(screen.getByText(/7-day window/)).toBeInTheDocument()
  })

  it('falls back to the raw rateLimitType for a window this catalog does not name', () => {
    renderWithProviders(
      <ClaudeAccountModal
        open
        onClose={vi.fn()}
        usage={{ ...BASE_USAGE, rateLimit: { status: 'rejected', rateLimitType: 'overage' } }}
      />,
    )
    expect(screen.getByText(/overage/)).toBeInTheDocument()
  })

  it('formats a sub-cent total with more precision than a dollar-scale one', () => {
    renderWithProviders(<ClaudeAccountModal open onClose={vi.fn()} usage={{ ...BASE_USAGE, totalCostUsd: 0.0007 }} />)
    expect(screen.getByText('$0.0007')).toBeInTheDocument()
  })
})
