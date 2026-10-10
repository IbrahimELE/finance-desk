// "Global competition" card: one $100,000 account per Möbius @handle, kept and priced by the
// competition host (fd_compete.py). Möbius-only, so it lives here rather than in frontend/index.html;
// it reuses the desk's classes (card, tabs, lb-row, acct-grid, btn) so it looks native to it.

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
  const aside = root.querySelector('aside')
  aside.insertAdjacentHTML('afterbegin', `
    <div class="card acct" id="gc-card">
      <div class="card-h"><h3>Leaderboard</h3><span class="pill" id="gc-pill">All Möbius players</span></div>
      <div id="gc-body"><div class="loading"><div class="spinner"></div><span class="muted">Connecting to the competition…</span></div></div>
    </div>`)
  const body = root.querySelector('#gc-body')
  const pill = root.querySelector('#gc-pill')
  let ctx = null
  let board = null
  let account = null
  let period = 'week'
  let qty = '10'
  let busy = false
  let seq = 0

  const toast = (msg, kind = '') => {
    const t = document.createElement('div')
    t.className = `toast ${kind}`
    t.innerHTML = msg
    root.querySelector('#toasts')?.appendChild(t)
    setTimeout(() => t.remove(), 4200)
  }
  const act = (action, payload = {}) => call(`/compete/${action}`, { ...payload, request_id: rid() })

  async function refresh() {
    const mine = ++seq
    try {
      if (!ctx) ctx = await call('/compete/context')
      const next = await act('standings', { period })
      const acct = next.joined ? await act('portfolio') : null
      if (mine !== seq) return
      board = next
      account = acct
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
    if (!board.joined) {
      body.innerHTML = `
        <p style="margin:0 0 12px">Trade a separate <b>$100,000</b> account against every Finance Desk player on Möbius as <b>${esc(ctx.handle)}</b>.</p>
        <p class="muted" style="margin:0 0 16px;font-size:14px">Orders are filled by the competition host at its own price, and competition accounts can't be reset, so the ranking is fair.</p>
        <button class="btn primary" id="gc-join" style="width:100%">Join as ${esc(ctx.handle)}</button>
        <div style="margin-top:20px">${standingsHtml()}</div>`
      return
    }
    const ret = account ? ((account.equity / account.starting_cash) - 1) * 100 : 0
    const ticker = (root.querySelector('#t')?.value || '').trim().toUpperCase()
    const positions = account?.positions?.length
      ? `<table style="margin-top:12px"><tr><th>Stock</th><th>Shares</th><th>Value</th><th>P/L</th></tr>${account.positions.map((p) => `<tr><td>${esc(p.ticker)}</td><td>${p.qty}</td><td>${usd(p.market_value)}</td><td class="${cls(p.pnl)}">${signed(p.pnl_pct, 1)}</td></tr>`).join('')}</table>`
      : ''
    body.innerHTML = `
      <div class="acct-grid" style="margin-top:0">
        <div><small>Total value</small><b>${usd(account?.equity)}</b></div>
        <div><small>Return</small><b class="${cls(ret)}">${signed(ret)}</b></div>
        <div><small>Cash</small><b>${usd(account?.cash)}</b></div>
        <div><small>Playing as</small><b>${esc(ctx.handle)}</b></div>
      </div>
      <form class="trade" id="gc-trade" style="margin-bottom:0">
        <div class="row" style="margin-bottom:8px">
          <input class="input" id="gc-ticker" placeholder="Ticker" value="${esc(ticker)}" aria-label="Competition ticker" autocomplete="off" style="flex:1;min-width:0;text-transform:uppercase">
          <div class="stepper"><button type="button" data-gq="-1" aria-label="Fewer shares">−</button><input id="gc-qty" type="number" min="1" value="${esc(qty)}" aria-label="Shares"><button type="button" data-gq="1" aria-label="More shares">+</button></div>
        </div>
        <div class="row"><button class="btn success sm" data-gs="buy" style="flex:1" ${busy ? 'disabled' : ''}>Buy</button><button class="btn danger sm" data-gs="sell" style="flex:1" ${busy ? 'disabled' : ''}>Sell</button></div>
      </form>
      ${positions}
      <div style="margin-top:20px">${standingsHtml()}</div>
      <div class="note">Ranked by money made in the period. Your competition account is separate from your workspaces, filled at the host's price, with no resets.</div>`
  }

  async function trade(side) {
    const t = (body.querySelector('#gc-ticker')?.value || '').trim().toUpperCase()
    const n = Number(body.querySelector('#gc-qty')?.value || 0)
    if (!t || !(n > 0)) { toast('Enter a ticker and how many shares.', 'err'); return }
    busy = true
    render()
    try {
      account = await act('order', { ticker: t, side, qty: n })
      const fill = account.fill || {}
      toast(`${side === 'buy' ? '✅ Bought' : '💰 Sold'} ${esc(fill.qty ?? n)} ${esc(t)} at ${usd(fill.price)} in the competition`, 'ok')
      window.mobius?.signal?.('item_created', { type: 'competition_order' })
      busy = false
      await refresh()
    } catch (err) {
      busy = false
      render()
      toast(esc(err.message), 'err')
    }
  }

  body.addEventListener('click', async (e) => {
    if (e.target.id === 'gc-retry') { body.innerHTML = '<div class="loading"><div class="spinner"></div></div>'; ctx = null; refresh(); return }
    if (e.target.id === 'gc-join') {
      e.target.disabled = true
      try {
        await act('join')
        toast(`🏆 You joined the global competition as <b>${esc(ctx.handle)}</b>`, 'ok')
        window.mobius?.signal?.('item_created', { type: 'competition_join' })
        await refresh()
      } catch (err) { e.target.disabled = false; toast(esc(err.message), 'err') }
      return
    }
    const tab = e.target.closest('[data-gp]')
    if (tab && tab.dataset.gp !== period) { period = tab.dataset.gp; refresh(); return }
    const step = e.target.closest('[data-gq]')
    if (step) {
      const input = body.querySelector('#gc-qty')
      input.value = Math.max(1, (Number(input.value) || 0) + Number(step.dataset.gq))
      qty = input.value
      return
    }
    const side = e.target.closest('[data-gs]')
    if (side) { e.preventDefault(); if (!busy) trade(side.dataset.gs) }
  })
  body.addEventListener('input', (e) => { if (e.target.id === 'gc-qty') qty = e.target.value })
  body.addEventListener('submit', (e) => e.preventDefault())

  refresh()
  every(() => { if (!document.hidden && !busy && !body.contains(document.activeElement)) refresh() }, REFRESH_MS)
}
