/**
 * Capture entry for the top-bar's THREE-TRACK layout at a range of window widths.
 *
 * All of the responsive behaviour now lives in CSS (`.topbar`, `.tb-left`,
 * `.tb-right`, `.tb-drop-*` in index.css), so this harness renders the real class
 * names against reproduced content and lets the real stylesheet do the layout.
 * That is deliberate: booting <App/> needs a live gateway session, and the thing
 * under test is the stylesheet, not the data flow. The content mirrors the
 * shipped header (home + crew chip · search · readout capsule + feedback + bell)
 * so the container-query rungs trip at realistic group widths.
 *
 * The header must span the WINDOW, because the centre track is a vw function —
 * so drive width through the browser viewport, one screenshot per width.
 *
 * ?theme=dark
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Home, Search, Bell, Lightbulb, Bug, Layers, Coins, AudioWaveform } from 'lucide-react'

import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('zh-CN')

const seg = 'flex items-center gap-1 px-1.5 py-0.5 rounded-md text-muted'

function TopBar() {
  return (
    <header className="topbar topbar-glass relative pl-3 pr-3" style={{ height: 42 }}>
      <div className="tb-left relative h-full">
        <span className="flex items-center gap-1.5 text-[13px] text-muted shrink-0">
          <Home size={15} className="lucide-inline" /> 本地
        </span>
        <span className="flex items-center gap-1.5 rounded-md bg-accent-subtle px-2 py-1 text-[13px] font-medium text-accent shrink-0">
          <span className="w-1.5 h-1.5 rounded-full bg-ok" />
          <Layers size={14} className="lucide-inline" /> devdesk
          <span className="rounded bg-accent px-1.5 text-[11px] text-accent-fg">3</span>
        </span>
      </div>

      <button
        type="button"
        className="h-7 w-full px-3 rounded-md border border-border bg-card text-muted flex items-center justify-center gap-2 cursor-pointer shadow-none"
      >
        <span className="text-[13px] truncate min-w-0">⌘K — 搜索任何内容…</span>
      </button>

      <div className="tb-right relative">
        <div className="flex items-center gap-2 h-7 px-2.5 rounded-xl bg-card">
          <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
          <span className="w-px h-3.5 bg-border shrink-0" />
          <button className={`${seg} gap-2 text-[11px] font-mono`}>
            <AudioWaveform size={12} className="tb-narrow-only" />
            <span className="tb-drop-metrics flex items-center gap-2">
              <span>CPU 1%</span><span>MEM 42%</span><span>DSK 20%</span>
            </span>
          </button>
          <span className="w-px h-3.5 bg-border shrink-0" />
          <button className={seg}>
            <Coins size={12} />
            <span className="tb-drop-usage font-mono text-[11px] whitespace-nowrap tabular-nums">12.2万<span className="text-muted">/1万</span></span>
          </button>
        </div>
        <span className="tb-drop-feedback flex items-center">
          <span className="flex items-center gap-2 h-7 rounded-xl border border-border bg-card px-3 text-[12px] text-muted">
            <span className="flex items-center gap-1"><Lightbulb size={13} className="lucide-inline" /> 申请功能</span>
            <span className="border-l border-border pl-2 flex items-center gap-1"><Bug size={13} className="lucide-inline" /> 反馈问题</span>
          </span>
        </span>
        <span className="relative text-muted p-1 shrink-0">
          <Bell size={17} />
          <span className="absolute -top-1 -right-1 rounded-full bg-accent px-1 text-[10px] text-accent-fg">99+</span>
        </span>
      </div>
    </header>
  )
}

/** Mobile form: no centre track, search rides in the actions group as an icon. */
function TopBarMobile() {
  return (
    <header className="topbar topbar-glass relative pl-3 pr-3" style={{ height: 42 }}>
      <div className="tb-left relative h-full px-2">
        <button className="p-2 rounded-md bg-transparent border-none text-muted shrink-0">☰</button>
      </div>
      <div />
      <div className="tb-right relative">
        <button className="h-7 w-7 rounded-md border border-border bg-card text-muted flex items-center justify-center shrink-0">
          <Search size={14} />
        </button>
        <div className="flex items-center gap-2 h-7 px-2.5 rounded-xl bg-card">
          <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
          <span className="w-px h-3.5 bg-border shrink-0" />
          <button className={seg}><Coins size={12} /></button>
        </div>
        <span className="relative text-muted p-1 shrink-0">
          <Bell size={17} />
          <span className="absolute -top-1 -right-1 rounded-full bg-accent px-1 text-[10px] text-accent-fg">99+</span>
        </span>
      </div>
    </header>
  )
}

/** Which form to render. The real shell branches on `useIsMobile()` (viewport
 *  < 768px); mirror that with the same query so an animated width shows the
 *  actual switch rather than a desktop DOM under a mobile grid template. An
 *  explicit ?form= override wins, for stills. */
function Harness() {
  const forced = params.get('form')
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width:767px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width:767px)')
    const on = () => setMobile(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  const isMobile = forced ? forced === 'mobile' : mobile
  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh' }}>
      {isMobile ? <TopBarMobile /> : <TopBar />}
      <div style={{ height: 30 }} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Harness />)
