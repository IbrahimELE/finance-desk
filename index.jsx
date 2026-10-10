import { useEffect, useRef, useState } from 'react'
import './vendor/lightweight-charts.js' // sets window.LightweightCharts (TradingView, Apache-2.0)
import { CSS } from './ui_css.js'
import { MARKUP } from './ui_markup.js'
import { start } from './ui_desk.js'
import { makeEnv } from './runtime.js'
import { startCompetition } from './competition.js'

const SHELL_CSS = `
  .fd-boot { min-height: 100vh; display: grid; place-items: center; color: var(--muted); font: 15px var(--font); background: var(--bg); }
  .fd-boot .fd-spin { width: 22px; height: 22px; border-radius: 50%; border: 3px solid var(--surface-2); border-top-color: var(--accent); animation: fdspin .8s linear infinite; margin: 0 auto 12px; }
  @keyframes fdspin { to { transform: rotate(360deg) } }
  @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; } }
`

export default function App({ appId, token }) {
  const rootRef = useRef(null)
  const [error, setError] = useState(null)
  const [booted, setBooted] = useState(false)

  useEffect(() => {
    let env = null
    let cancelled = false
    ;(async () => {
      // UI preferences (workspace, alerts, recent orders) live in one storage document.
      let state = {}
      try { state = (await window.mobius.storage.get('ui-state.json')) || {} } catch { state = {} }
      if (cancelled) return
      env = makeEnv({ appId, token, state })
      rootRef.current.innerHTML = MARKUP
      setBooted(true)
      try {
        start(env)
        startCompetition({ root: rootRef.current, call: env.call, every: env.every })
        window.mobius.signal?.('app_ready', { saved_workspace: state.proj ? 1 : 0 })
      } catch (err) {
        window.mobius.signal?.('error', { message: String(err?.message || err), source: 'start' })
        setError(String(err?.message || err))
      }
    })()
    return () => { cancelled = true; env?.dispose() }
  }, [appId, token])

  return (
    <>
      <style>{SHELL_CSS + CSS}</style>
      {!booted && <div className="fd-boot"><div><div className="fd-spin" />Opening Finance Desk…</div></div>}
      {error && <div className="fd-boot">Finance Desk could not start: {error}</div>}
      <div ref={rootRef} />
    </>
  )
}
