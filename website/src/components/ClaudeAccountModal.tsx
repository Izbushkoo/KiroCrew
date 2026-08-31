import { AlertTriangle, ExternalLink, Info, Loader2, Sparkles, XCircle } from 'lucide-react'

import type { ClaudeUsage } from '../api/client'
import { fmtCurrency, fmtDateFields } from '../i18n/format'
import { i18nT } from '../i18n/t'
import Modal from './Modal'

// The usage view model is owned by `api/client.ts` next to the wire payload
// it is normalized from, mirroring KiroAccountModal's KiroCreditUsage.
export type { ClaudeUsage }

/**
 * `null` while the query is still loading, `'failed'` when the fetch itself
 * failed with nothing cached — mirrors `KiroAccountUsage`'s loading/failure
 * split for the same reason: a failed fetch must not render as "still
 * checking" (a spinner that never resolves).
 */
export type ClaudeAccountUsage = ClaudeUsage | null | 'failed'

const isUsageReading = (usage: ClaudeAccountUsage): usage is ClaudeUsage =>
  typeof usage === 'object' && usage !== null

interface ClaudeAccountModalProps {
  open: boolean
  onClose: () => void
  usage: ClaudeAccountUsage
}

function formatResetsAt(epochSeconds: number): string {
  return fmtDateFields(new Date(epochSeconds * 1000), {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/**
 * Real, Anthropic-sourced quota signal when one has arrived — see
 * `ClaudeRateLimit`'s doc comment for why absence is the common case and
 * does not mean "no limit in effect". Rendered ABOVE the cost estimate: an
 * actual warning from Anthropic is more actionable than this fork's own
 * estimate, so it leads.
 */
function RateLimitCard({ rateLimit }: { rateLimit: ClaudeUsage['rateLimit'] }) {
  if (!rateLimit) return null
  const isRejected = rateLimit.status === 'rejected'
  const isWarning = rateLimit.status === 'allowed_warning'
  if (!isRejected && !isWarning) return null // 'allowed' with no threshold crossed: nothing worth a card for.

  const Icon = isRejected ? XCircle : AlertTriangle
  const tone = isRejected ? 'border-danger/40 bg-danger-subtle text-danger' : 'border-warn/40 bg-warn-subtle text-warn'
  // A fixed lookup, not a dynamically-built i18n key: the catalog scanner
  // that guards against hardcoded strings needs every key spelled out
  // literally to find it, and an unrecognized rateLimitType (a future
  // window Anthropic adds) falls back to the raw value rather than a
  // missing-key placeholder.
  const WINDOW_LABELS: Record<string, string> = {
    five_hour: i18nT('components.claudeAccountModal.window_five_hour'),
    seven_day: i18nT('components.claudeAccountModal.window_seven_day'),
  }
  const windowLabel = rateLimit.rateLimitType
    ? WINDOW_LABELS[rateLimit.rateLimitType] ?? rateLimit.rateLimitType
    : undefined

  return (
    <div className={`flex flex-col gap-1.5 rounded-lg border p-3 text-[13px] ${tone}`}>
      <div className="flex items-center gap-2 font-medium">
        <Icon className="lucide-inline shrink-0" />
        {isRejected
          ? i18nT('components.claudeAccountModal.limit_reached')
          : i18nT('components.claudeAccountModal.limit_approaching')}
      </div>
      <div className="text-[12px] opacity-90">
        {windowLabel && <span>{windowLabel}</span>}
        {rateLimit.utilization != null && (
          <span>{windowLabel ? ' · ' : ''}{i18nT('components.claudeAccountModal.utilization_pct', { pct: Math.round(rateLimit.utilization) })}</span>
        )}
        {rateLimit.resetsAt != null && (
          <span>{(windowLabel || rateLimit.utilization != null) ? ' · ' : ''}{i18nT('app.resets')} {formatResetsAt(rateLimit.resetsAt)}</span>
        )}
      </div>
    </div>
  )
}

function UsageBody({ usage }: { usage: ClaudeAccountUsage }) {
  if (usage === null) {
    return (
      <div className="flex items-center justify-center gap-2 py-6 text-[13px] text-muted">
        <Loader2 className="lucide-inline animate-spin" /> {i18nT('components.claudeAccountModal.checking_usage')}
      </div>
    )
  }
  if (!isUsageReading(usage)) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-3.5 text-[13px] text-muted">
        <Info className="lucide-inline shrink-0" /> {i18nT('components.claudeAccountModal.usage_unavailable')}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <RateLimitCard rateLimit={usage.rateLimit} />
      <div className="rounded-lg border border-border px-3">
        <div className="flex justify-between items-baseline gap-4 py-2">
          <span className="text-[12px] text-muted">{i18nT('components.claudeAccountModal.estimated_spend')}</span>
          <span className="text-[15px] font-semibold text-text">{fmtCurrency(usage.totalCostUsd, 'USD', { maximumFractionDigits: 4, minimumFractionDigits: 2 })}</span>
        </div>
        {usage.since != null && (
          <div className="flex justify-between items-baseline gap-4 py-2 border-t border-border">
            <span className="text-[12px] text-muted">{i18nT('components.claudeAccountModal.tracking_since')}</span>
            <span className="text-[13px] font-medium text-text">{formatResetsAt(usage.since)}</span>
          </div>
        )}
      </div>
      {/* The disclaimer this whole modal exists to state plainly: the number
          above is this fork's own estimate of what claude_code reported,
          never a real remaining-quota reading — that reading does not exist
          anywhere this app can reach. See claude_usage.py and
          api_claude_usage's docstrings for the full "why". */}
      <p className="text-[11px] leading-relaxed text-muted">
        {i18nT('components.claudeAccountModal.disclaimer')}
      </p>
    </div>
  )
}

export default function ClaudeAccountModal({ open, onClose, usage }: ClaudeAccountModalProps) {
  const consoleUrl = isUsageReading(usage) ? usage.consoleUrl : 'https://console.anthropic.com/settings/usage'
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={<span className="flex items-center gap-2"><Sparkles className="lucide-inline" /> {i18nT('components.claudeAccountModal.claude_account')}</span>}
      maxWidth={460}
    >
      <div className="flex flex-col gap-4">
        <UsageBody usage={usage} />
        <a
          href={consoleUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 self-start text-[12px] text-accent hover:underline"
        >
          {i18nT('components.claudeAccountModal.open_console')} <ExternalLink className="lucide-inline" />
        </a>
      </div>
    </Modal>
  )
}
