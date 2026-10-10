// Möbius environment for the ported Finance Desk UI (ui_desk.js): the same paths the web
// frontend calls on agent_service, served by this app's service, plus the AI flow.

const AI_TIMEOUT_MS = 150000

export function makeEnv({ appId, token, state }) {
  const auth = { Authorization: `Bearer ${token}` }
  const timers = []
  const listeners = []

  // --- transport: same paths as agent_service; /brief and /chat add the AI step ---
  async function service(path, options = {}) {
    return fetch(`/api/apps/${appId}/service${path}`, {
      ...options,
      headers: { ...(options.headers || {}), ...auth },
    })
  }
  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

  async function serviceJson(path, body) {
    const res = await service(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw Object.assign(new Error(data.detail || `Request failed (${res.status})`), { status: res.status })
    return data
  }

  // A brief: data + template thesis from the service, the thesis from the owner's Möbius AI,
  // then saved. Without AI (no provider, timeout, empty answer) the template brief is kept.
  async function brief({ ticker, project_id }) {
    const { brief: draft, prompt } = await serviceJson('/brief/prepare', { ticker, project_id })
    let finished = draft
    try {
      const thesis = await askAI(prompt, `Finance Desk brief · ${draft.ticker}`)
      finished = { ...draft, thesis, mode: 'llm' }
    } catch (err) {
      window.mobius?.signal?.('error', { message: String(err?.message || err), source: 'ai_brief' })
    }
    return serviceJson('/briefs', { project_id, brief: finished })
  }

  async function chat({ message, project_id }) {
    const r = await serviceJson('/chat/prompt', { message, project_id })
    if (r.answer) return r
    try {
      return { answer: await askAI(r.prompt, 'Finance Desk question') }
    } catch (err) {
      return { answer: `The AI could not answer right now (${err.message}). Your saved briefs are still available.` }
    }
  }

  async function api(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase()
    const body = options.body ? JSON.parse(options.body) : null
    try {
      if (method === 'POST' && url === '/brief') return json(await brief(body))
      if (method === 'POST' && url === '/chat') return json(await chat(body))
    } catch (err) {
      return json({ detail: err.message }, err.status || 502)
    }
    return service(url, options)
  }

  // --- AI: one hidden app-owned chat per question, answered by the owner's default model ---
  async function askAI(prompt, title) {
    const { chatId } = await window.mobius.chat.start({ title, draft: prompt, ownerVisible: false })
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), AI_TIMEOUT_MS)
    try {
      const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}/stream`, {
        headers: { ...auth, 'X-Mobius-Stream-Snapshot': 'true' }, signal: controller.signal,
      })
      if (!res.ok || !res.body) throw new Error(`AI stream unavailable (${res.status})`)
      const text = plainAnswer(await readFinalText(res.body))
      if (!text) throw new Error('The AI returned no text')
      return text
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('The AI took too long')
      throw err
    } finally {
      clearTimeout(timer)
    }
  }

  // --- browser features the opaque app frame lacks ---
  // localStorage -> one small JSON document in the app's Möbius storage (state is preloaded).
  let saveTimer = null
  const persist = () => {
    clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      window.mobius.storage.set('ui-state.json', state).catch((err) =>
        window.mobius.signal?.('error', { message: String(err?.message || err), source: 'ui_state' }))
    }, 300)
  }
  const localStorage = new Proxy(state, {
    get(target, key) {
      if (key === 'removeItem') return (k) => { delete target[k]; persist() }
      if (key === 'getItem') return (k) => (k in target ? target[k] : null)
      if (key === 'setItem') return (k, v) => { target[k] = String(v); persist() }
      return target[key]
    },
    set(target, key, value) { target[key] = String(value); persist(); return true },
  })

  // Price alerts become Möbius push notifications.
  class Notification {
    static permission = 'granted'
    static requestPermission() { return Promise.resolve('granted') }
    constructor(title, { body } = {}) {
      fetch('/api/notifications/send', {
        method: 'POST',
        headers: { ...auth, 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tag: `price-alert:${title}:${body || ''}`.slice(0, 120).replace(/[^A-Za-z0-9_.:-]/g, '-') }),
      }).catch(() => {})
    }
  }

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || undefined
  const every = (fn, ms) => timers.push(setInterval(fn, ms))
  const onVisible = (fn) => { document.addEventListener('visibilitychange', fn); listeners.push(fn) }
  const dispose = () => {
    timers.forEach(clearInterval)
    listeners.forEach((fn) => document.removeEventListener('visibilitychange', fn))
    clearTimeout(saveTimer)
  }

  // JSON call to this app's service (GET without a body); errors carry the service's `detail`.
  async function call(path, body) {
    if (body === undefined) {
      const res = await service(path)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw Object.assign(new Error(data.detail || `Request failed (${res.status})`), { status: res.status })
      return data
    }
    return serviceJson(path, body)
  }

  return { api, call, localStorage, Notification, cssVar, every, onVisible, dispose }
}

// Möbius chat answers are written for the chat renderer: currency dollars arrive escaped (\$, so KaTeX
// does not pair them) and emphasis as Markdown. The desk renders plain text.
function plainAnswer(text) {
  return text.replace(/\\\$/g, '$').replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1').trim()
}

// Reads a Möbius chat event stream (SSE) until `done` and returns the last assistant text item.
async function readFinalText(stream) {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  const items = new Map() // text_item_id -> text, in arrival order
  let buffer = ''
  let anon = 0
  const put = (id, text, append) => {
    const key = id || `anon-${anon}`
    items.set(key, append ? (items.get(key) || '') + text : text)
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      const data = frame.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trimStart()).join('\n')
      if (!data) continue
      let event
      try { event = JSON.parse(data) } catch { continue }
      if (event.type === 'stream_snapshot') {
        for (const item of event.items || []) if (item?.type === 'text') put(item.text_item_id, item.content || '', false)
      } else if (event.type === 'text') {
        put(event.text_item_id, event.content || '', true)
      } else if (event.type === 'text_final') {
        put(event.text_item_id, event.content || '', false)
      } else if (event.type === 'text_boundary') {
        if (event.replace_text_item_id) items.delete(event.replace_text_item_id)
        anon += 1
      } else if (event.type === 'done') {
        reader.cancel().catch(() => {})
        return [...items.values()].filter((t) => t.trim()).pop() || ''
      }
    }
  }
  return [...items.values()].filter((t) => t.trim()).pop() || ''
}
