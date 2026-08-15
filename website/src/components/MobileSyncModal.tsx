import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, useReducedMotion } from 'framer-motion'
import { X, Copy, Check, Smartphone, Wifi, Globe } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { useTranslation } from 'react-i18next'

import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { copyToClipboard } from '../utils/clipboard'
import SegmentedControl, { type Segment } from './SegmentedControl'

type NetworkMode = 'global' | 'local'

interface MobileSyncModalProps {
  /** Current authentication token appended to the sync URL. */
  authToken: string
  /** LAN IP address for local Wi-Fi mode (e.g. "192.168.1.42"). */
  lanIp?: string
  /** Cloudflare Tunnel hostname for global mode (e.g. "crew.example.com"). */
  tunnelHost?: string
  /** Gateway port (defaults to 5476). */
  port?: number
  onClose: () => void
}

/**
 * Modal dialog for Android Mobile Sync.
 *
 * Displays a QR code encoding the dashboard URL with an auth token so a mobile
 * device can scan and open it directly. A tab switcher toggles between a global
 * (4G/5G via Cloudflare Tunnel) URL and a local (Wi-Fi LAN IP) URL. Includes a
 * copy-link button and Android "Add to Home Screen" installation instructions.
 */
export default function MobileSyncModal({
  authToken,
  lanIp,
  tunnelHost,
  port = 5476,
  onClose,
}: MobileSyncModalProps) {
  const { t } = useTranslation()
  const dialogRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  const [mode, setMode] = useState<NetworkMode>(tunnelHost ? 'global' : 'local')
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useDialogFocusTrap(dialogRef, onClose)

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )

  const syncUrl =
    mode === 'global' && tunnelHost
      ? `https://${tunnelHost}?token=${encodeURIComponent(authToken)}`
      : `http://${lanIp ?? 'localhost'}:${port}?token=${encodeURIComponent(authToken)}`

  const segments: Segment<NetworkMode>[] = [
    {
      key: 'global',
      label: t('components.mobileSyncModal.tab_global'),
      icon: <Globe className="lucide-inline" aria-hidden="true" />,
      disabled: !tunnelHost,
      tooltip: tunnelHost
        ? t('components.mobileSyncModal.tab_global_tooltip')
        : t('components.mobileSyncModal.tab_global_disabled'),
    },
    {
      key: 'local',
      label: t('components.mobileSyncModal.tab_local'),
      icon: <Wifi className="lucide-inline" aria-hidden="true" />,
      disabled: !lanIp,
      tooltip: lanIp
        ? t('components.mobileSyncModal.tab_local_tooltip')
        : t('components.mobileSyncModal.tab_local_disabled'),
    },
  ]

  const handleCopy = async () => {
    try {
      await copyToClipboard(syncUrl)
    } catch {
      return
    }
    setCopied(true)
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopied(false), 1500)
  }

  return createPortal(
    <motion.div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={t('components.mobileSyncModal.dialog_label')}
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-bg/80 backdrop-blur-sm"
      initial={reduceMotion ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.15 }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <motion.div
        className="relative w-full max-w-md mx-4 rounded-xl border border-border bg-card shadow-xl"
        initial={reduceMotion ? false : { opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.15 }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 pt-5 pb-3">
          <div className="flex items-center gap-2">
            <Smartphone className="lucide-inline text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-text-strong">
              {t('components.mobileSyncModal.title')}
            </h2>
          </div>
          <button
            type="button"
            aria-label={t('components.mobileSyncModal.close')}
            className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer border-none bg-transparent"
            onClick={onClose}
          >
            <X className="lucide-inline" aria-hidden="true" />
          </button>
        </div>

        {/* Tab switcher */}
        <div className="flex justify-center px-5 pb-4">
          <SegmentedControl
            segments={segments}
            value={mode}
            onChange={setMode}
            layoutId="mobile-sync-mode"
            collapse={false}
          />
        </div>

        {/* QR code */}
        <div className="flex justify-center px-5 pb-4">
          <div className="rounded-lg bg-white p-4" aria-label={t('components.mobileSyncModal.qr_label')}>
            <QRCodeSVG
              value={syncUrl}
              size={200}
              level="M"
              includeMargin={false}
            />
          </div>
        </div>

        {/* URL display + copy */}
        <div className="mx-5 mb-4 flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-3 py-2">
          <code className="flex-1 truncate text-[12px] text-muted font-mono">
            {syncUrl}
          </code>
          <button
            type="button"
            aria-label={
              copied
                ? t('components.mobileSyncModal.copied')
                : t('components.mobileSyncModal.copy_link')
            }
            className="shrink-0 p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer border-none bg-transparent"
            onClick={handleCopy}
          >
            {copied ? (
              <Check className="lucide-inline text-ok" aria-hidden="true" />
            ) : (
              <Copy className="lucide-inline" aria-hidden="true" />
            )}
          </button>
        </div>

        {/* Android installation instructions */}
        <div className="mx-5 mb-5 rounded-lg border border-border bg-bg-elevated px-4 py-3">
          <h3 className="text-[12px] font-semibold text-text-strong mb-2">
            {t('components.mobileSyncModal.install_title')}
          </h3>
          <ol className="list-decimal list-inside text-[12px] text-muted space-y-1.5 leading-relaxed">
            <li>{t('components.mobileSyncModal.install_step_1')}</li>
            <li>{t('components.mobileSyncModal.install_step_2')}</li>
            <li>{t('components.mobileSyncModal.install_step_3')}</li>
          </ol>
        </div>
      </motion.div>
    </motion.div>,
    document.body,
  )
}
