// 服务端事件订阅：一条 WebSocket 收所有后台事件。
//
// 后台任务（网络守卫、隧道告警、任务完成）通过 /api/events/ws 推送。
// 断线由这里指数退避重连，连上后服务端会补发最近的事件，不会漏。

import { reactive } from 'vue'

export const bus = reactive({
  connected: false,
  lastEvent: null
})

const handlers = new Map()   // event -> Set<fn>
let ws = null
let retry = 0
let timer = 0
let closed = false
let token = ''

// 登录后拿到的会话 token：浏览器的 WebSocket API 不能自定义请求头，
// 只靠 cookie 在部分环境会被拒，所以用查询参数带上同一个 token。
// token 变了要重连：老连接用的可能是空 token。
export function setEventToken(t) {
  const v = t || ''
  if (v === token) return
  token = v
  if (ws) {
    clearTimeout(timer)
    try { ws.close() } catch { /* ignore */ }
    ws = null
    if (!closed) connect()
  }
}

export function on(event, fn) {
  if (!handlers.has(event)) handlers.set(event, new Set())
  handlers.get(event).add(fn)
  return () => off(event, fn)
}

export function off(event, fn) {
  handlers.get(event)?.delete(fn)
}

function fire(event, data) {
  bus.lastEvent = { event, data, ts: Date.now() }
  const set = handlers.get(event)
  if (set) for (const fn of set) { try { fn(data) } catch { /* 单个处理器出错不影响其它 */ } }
  const any = handlers.get('*')
  if (any) for (const fn of any) { try { fn(event, data) } catch { /* ignore */ } }
}

function connect() {
  if (closed) return
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const q = token ? `?token=${encodeURIComponent(token)}` : ''
  try {
    ws = new WebSocket(`${proto}//${location.host}/api/events/ws${q}`)
  } catch {
    schedule()
    return
  }
  ws.onopen = () => {
    bus.connected = true
    retry = 0
  }
  ws.onmessage = (e) => {
    let msg
    try { msg = JSON.parse(e.data) } catch { return }
    if (msg && msg.event) fire(msg.event, msg.data || {})
  }
  ws.onclose = () => {
    bus.connected = false
    schedule()
  }
  ws.onerror = () => {
    try { ws.close() } catch { /* ignore */ }
  }
}

function schedule() {
  clearTimeout(timer)
  // 指数退避，最长 15 秒。手机切网后往往要几十秒才恢复，别把服务端打爆
  const wait = Math.min(15000, 1000 * Math.pow(1.6, retry++))
  timer = setTimeout(connect, wait)
}

export function startEvents() {
  if (ws) return
  closed = false
  connect()
}

export function stopEvents() {
  closed = true
  clearTimeout(timer)
  try { ws?.close() } catch { /* ignore */ }
  ws = null
  bus.connected = false
}
