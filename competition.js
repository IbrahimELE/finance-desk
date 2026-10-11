// "Global competition" leaderboard: one $100,000 account per Möbius @handle, kept and priced by the
// competition host (fd_compete.py). This card only ranks players; trading the competition account
// happens in the Paper trading card (its Competition switch), so there is one order form for both.
// Events: "fd:account" asks the paper card to switch account; "fd:compete-changed" means a trade or
// join happened there, so the ranking is refreshed.

const MEDAL = ['🥇', '🥈', '🥉']
const PERIOD_NAME = { day: 'today', week: 'this week', month: 'this month' }
const REFRESH_MS = 30000

const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
const usd = (x) => (x == null ? '–' : Number(x).toLocaleString('en-US', { style: 'currency', currency: 'USD' }))
const cls = (x) => (x > 0.004 ? 'up' : x < -0.004 ? 'down' : 'muted')
const money = (x) => `${x > 0.004 ? '+' : x < -0.004 ? '−' : ''}${usd(Math.abs(x || 0))}`
const signed = (x, d = 2) => `${Math.abs(x) < 0.005 ? '' : x > 0 ? '+' : ''}${(Math.abs(x) < 0.005 ? 0 : x).toFixed(d)}%`
const rid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 14)}`)

export function startCompetition({ root, call, every }) {
  root.querySelector('#paper').insertAdjacentHTML('afterend', `
    <div class="card acct" id="gc-card">
      <div class="card-h"><h3>Leaderboard</h3><span class="pill" id="gc-pill">All Möbius players</span></div>
      <div id="gc-body"><div class="loading"><div class="spinner"></div><span class="muted">Connecting to the competition…</span></div></div>
    </div>`)
  const body = root.querySelector('#gc-body')
  const pill = root.querySelector('#gc-pill')
  let ctx = null
  let board = null
  let period = 'week'
  let seq = 0

  const openAccount = () => document.dispatchEvent(new CustomEvent('fd:account', { detail: 'compete' }))
  const act = (action, payload = {}) => call(`/compete/${action}`, { ...payload, request_id: rid() })

  async function refresh() {
    const mine = ++seq
    try {
      if (!ctx) ctx = await call('/compete/context')
      const next = await act('standings', { period })
      if (mine !== seq) return
      board = next
      render()
    } catch (err) {
      if (mine !== seq) return
      if (!board) renderProblem(err.message)
    }
  }

  function renderProblem(message) {
    pill.textContent = 'Unavailable'
    body.innerHTML = `<p class="muted" style="margin:0 0 12px">${esc(message)}</p><button class="btn ghost sm" id="gc-retry">Try again</button>`
  }

  // One row per person (@handle), ranked by the money made in the period; % is secondary.
  function rowHtml(s, moved) {
    const mine = s.project_id === ctx.handle
    return `
      <div class="lb-row${mine ? ' me' : ''}${moved && s.rank === 1 ? ' first' : ''}">
        <span class="rk">${moved && s.rank <= 3 ? MEDAL[s.rank - 1] : s.rank}</span>
        <span class="nm" title="${esc(s.project_id)}">${esc(s.project_id)}${mine ? ' <span class="faint">(you)</span>' : ''}</span>
        <span style="text-align:right;line-height:1.25"><b class="${cls(s.profit)}">${money(s.profit)}</b><small class="${cls(s.return_pct)}" style="display:block;font-size:11px">${signed(s.return_pct)}</small></span></div>`
  }

  function standingsHtml() {
    const rows = board.standings
    const moved = rows.some((s) => Math.abs(s.profit || 0) >= 0.005)
    const list = rows.map((s) => rowHtml(s, moved)).join('')
    const me = board.me
    const outside = me && !rows.some((s) => s.project_id === me.project_id)
      ? `<div class="faint" style="text-align:center;letter-spacing:4px;margin:2px 0" aria-hidden="true">···</div>${rowHtml(me, moved)}
         <div class="note" style="margin:6px 0 0">You are #${me.rank} of ${board.ranked}, ${usd(Math.max(0, rows[rows.length - 1].profit - me.profit))} behind the top ${rows.length}.</div>`
      : ''
    const idle = list && !moved ? `<div class="note" style="margin:8px 0 0">No price moves yet ${PERIOD_NAME[period]}; medals go only to periods where something moved.</div>` : ''
    const medals = board.medals.length ? `<div class="eyebrow" style="margin:16px 0 6px">All-time podiums</div><div class="lb-medals"><span></span><span>🥇</span><span>🥈</span><span>🥉</span>${board.medals.slice(0, 8).map((m) => `<span class="nm">${esc(m.project_id)}</span><span>${m.gold}</span><span>${m.silver}</span><span>${m.bronze}</span>`).join('')}</div>` : ''
    return `
      <div class="tabs lb-tabs" id="gc-tabs" role="tablist">${['day', 'week', 'month'].map((p) => `<button role="tab" data-gp="${p}" class="${p === period ? 'on' : ''}">${p[0].toUpperCase() + p.slice(1)}</button>`).join('')}</div>
      ${list || '<div class="empty">Nobody has traded yet. Be the first on the board.</div>'}${outside}${idle}${medals}`
  }

  function render() {
    pill.textContent = `${board.players} player${board.players === 1 ? '' : 's'}`
    const me = board.me
    const head = !board.joined
      ? `<p class="muted" style="margin:0 0 12px;font-size:14px">Trade a separate $100,000 account against every Finance Desk player as <b>${esc(ctx.handle)}</b>.</p>
         <button class="btn primary" data-gopen style="width:100%">Join the competition</button>`
      : `<div class="lb-you"><span>${me ? `You are <b>#${me.rank}</b> of ${board.ranked} ${PERIOD_NAME[period]}` : `No trades ${PERIOD_NAME[period]} yet`}</span>
         <button class="btn ghost sm" data-gopen>Trade</button></div>`
    body.innerHTML = `${head}<div style="margin-top:16px">${standingsHtml()}</div>
      <div class="note">Ranked by money made in the period. Trade your competition account in Paper trading → Competition.</div>`
  }

  body.addEventListener('click', (e) => {
    if (e.target.id === 'gc-retry') { body.innerHTML = '<div class="loading"><div class="spinner"></div></div>'; ctx = null; refresh(); return }
    if (e.target.closest('[data-gopen]')) { openAccount(); return }
    const tab = e.target.closest('[data-gp]')
    if (tab && tab.dataset.gp !== period) { period = tab.dataset.gp; refresh() }
  })
  document.addEventListener('fd:compete-changed', () => refresh())

  refresh()
  every(() => { if (!document.hidden) refresh() }, REFRESH_MS)
}
