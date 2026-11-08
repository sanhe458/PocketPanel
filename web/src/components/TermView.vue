<template>
  <div class="term-view" :class="{ off: !active }">
    <div class="term-body" ref="host"></div>
    <div class="term-bar" v-show="active">
      <button v-for="k in KEYS" :key="k.label" class="kbtn" @click="hit(k.seq)">{{ k.label }}</button>
      <button class="kbtn" @click="$emit('paste')">粘贴</button>
      <span class="spacer"></span>
      <button class="kbtn" @click="zoom(-1)">A−</button>
      <button class="kbtn" @click="zoom(1)">A+</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api } from '../api'
import { ui } from '../store'

const props = defineProps({
  id: { type: String, required: true },
  active: { type: Boolean, default: false },
  theme: { type: String, default: 'dark' }
})
const emit = defineEmits(['exit', 'paste', 'transport'])

const host = ref(null)
let term = null
let fit = null
let ro = null
let opened = false
let stopped = false
let seq = 0
let fs = 0

// 通道代号：每次切换传输方式 +1，旧通道的回调看到代号不匹配就自行退场。
// 不这么做的话，WS 断开瞬间轮询刚好返回一帧，两条路会同时写 term。
let channel = 0
let mode = ''          // '' | 'ws' | 'poll'，防止重复开通道
let ws = null
let pollTimer = 0
let hbTimer = 0

const KEYS = [
  { label: 'Esc', seq: '\x1b' },
  { label: 'Tab', seq: '\t' },
  { label: '^C', seq: '\x03' },
  { label: '↑', seq: '\x1b[A' },
  { label: '↓', seq: '\x1b[B' },
  { label: '←', seq: '\x1b[D' },
  { label: '→', seq: '\x1b[C' },
  { label: '^D', seq: '\x04' },
  { label: '^Z', seq: '\x1a' },
  { label: '^L', seq: '\x0c' },
  { label: '^R', seq: '\x12' }
]

const DARK = {
  background: '#05080e', foreground: '#c9d6e8', cursor: '#5b9aff', cursorAccent: '#05080e',
  selectionBackground: 'rgba(77,141,255,.30)',
  black: '#1b2533', red: '#f0716f', green: '#4ade80', yellow: '#e8b45a',
  blue: '#63a4ff', magenta: '#c084fc', cyan: '#22d3ee', white: '#dbe6f5',
  brightBlack: '#64748b', brightRed: '#ff8a88', brightGreen: '#75e8a0',
  brightYellow: '#f6cf7d', brightBlue: '#8bbcff', brightMagenta: '#d8a8ff',
  brightCyan: '#67e8f9', brightWhite: '#f4f8ff'
}
const LIGHT = {
  background: '#fbfcfe', foreground: '#1b2535', cursor: '#2563eb', cursorAccent: '#fbfcfe',
  selectionBackground: 'rgba(37,99,235,.20)',
  black: '#1b2535', red: '#c62828', green: '#16703a', yellow: '#8a6100',
  blue: '#1d4ed8', magenta: '#7e22ce', cyan: '#0e7490', white: '#e6eaf1',
  brightBlack: '#64748b', brightRed: '#dc2626', brightGreen: '#15803d',
  brightYellow: '#a16207', brightBlue: '#2563eb', brightMagenta: '#9333ea',
  brightCyan: '#0891b2', brightWhite: '#f8fafc'
}
const themeOf = (t) => (t === 'light' ? LIGHT : DARK)

function b64bytes(b64) {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}
function bytesB64(u8) {
  let s = ''
  const CH = 0x8000
  for (let i = 0; i < u8.length; i += CH) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CH))
  }
  return btoa(s)
}

function make() {
  if (term) return
  try { fs = parseInt(localStorage.getItem('pp_term_fs') || '0', 10) || 0 } catch { fs = 0 }
  if (!fs) fs = window.innerWidth < 700 ? 12.5 : 13.5
  term = new Terminal({
    fontSize: fs,
    // 不要把 CJK 字体放在等宽栈里：它的 ascent/descent 会把行高撑到 1.7em，
    // 手机上 692px 只能放 26 行。中文交给系统 fallback，xterm 按双宽字符处理。
    fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", "Courier New", monospace',
    lineHeight: 1.0,
    letterSpacing: 0,
    cursorBlink: true,
    cursorStyle: 'bar',
    scrollback: 3000,
    allowTransparency: false,
    macOptionIsMeta: true,
    theme: themeOf(props.theme)
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.onData((d) => send(d))
  term.onResize(({ cols, rows }) => pushResize(cols, rows))
  term.attachCustomKeyEventHandler((e) => {
    // 桌面端 Ctrl/Cmd+C 有选中内容时走浏览器复制，不发给远端
    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && term.hasSelection()) return false
    if ((e.ctrlKey || e.metaKey) && e.key === 'v') return false
    return true
  })
}

function openTerm() {
  if (opened || !term) return
  const el = host.value
  if (!el || el.offsetWidth < 60 || el.offsetHeight < 40) return
  term.open(el)
  opened = true
  fitNow()
  startChannel()
}

function fitNow() {
  if (!opened || !fit) return
  const el = host.value
  if (!el || el.offsetWidth < 60 || el.offsetHeight < 40) return
  try { fit.fit() } catch { /* 容器尚未布局 */ }
}

// ---------------- 双通道 ----------------

function setTransport(t) {
  emit('transport', t)
}

function closeChannel() {
  channel++
  mode = ''
  clearTimeout(pollTimer)
  clearInterval(hbTimer)
  if (ws) {
    const old = ws
    ws = null
    try {
      old.onopen = old.onmessage = old.onclose = old.onerror = null
      old.close()
    } catch { /* ignore */ }
  }
}

function startChannel() {
  if (stopped || !props.active || mode) return
  const pref = ui.termTransport || 'auto'
  if (pref === 'poll' || typeof WebSocket === 'undefined') {
    usePoll(0)
    return
  }
  useWS()
}

function useWS() {
  closeChannel()
  mode = 'ws'
  const ch = ++channel
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  let sock
  try {
    sock = new WebSocket(`${proto}//${location.host}/api/term/${props.id}/ws?since=${seq}`)
  } catch {
    usePoll(0)
    return
  }
  ws = sock
  let openedWs = false
  // 握手卡住（代理吞掉 Upgrade 又不报错）时不能干等，5 秒没连上就退回轮询
  const guard = setTimeout(() => {
    if (!openedWs && ch === channel) {
      try { sock.close() } catch { /* ignore */ }
      usePoll(0)
    }
  }, 5000)

  sock.onopen = () => {
    openedWs = true
    clearTimeout(guard)
    if (ch !== channel) return
    setTransport('ws')
    clearInterval(hbTimer)
    hbTimer = setInterval(() => {
      if (ws && ws.readyState === 1) ws.send('{"t":"ping"}')
    }, 25000)
  }
  sock.onmessage = (ev) => {
    if (ch !== channel) return
    let m
    try { m = JSON.parse(ev.data) } catch { return }
    if (m.t === 'out') applyOutput(m)
  }
  sock.onclose = () => {
    clearTimeout(guard)
    clearInterval(hbTimer)
    if (ch !== channel) return
    ws = null
    mode = ''
    // 断线先退回轮询保证可用，轮询自己会继续重试
    usePoll(250)
  }
  sock.onerror = () => { /* 统一由 onclose 收尾 */ }
}

function usePoll(delay) {
  closeChannel()
  mode = 'poll'
  const ch = ++channel
  setTransport('poll')
  pollTimer = setTimeout(() => pollLoop(ch), delay || 0)
}

async function pollLoop(ch) {
  if (ch !== channel || stopped || !props.active) return
  let r
  try {
    r = await api(`/api/term/${props.id}/output?since=${seq}&wait=20`)
  } catch (e) {
    if (ch !== channel) return
    if (e && (e.name === 'AbortError' || /aborted/i.test(e.message || ''))) return
    if (/不存在|已关闭/.test(e.message || '')) { stopped = true; emit('exit', -1); return }
    pollTimer = setTimeout(() => pollLoop(ch), 1200)
    return
  }
  if (ch !== channel) return
  if (applyOutput(r)) return
  pollTimer = setTimeout(() => pollLoop(ch), 0)
}

/** 把一帧输出写进终端；返回 true 表示会话已结束。 */
function applyOutput(r) {
  if (!term) return false
  if (r.reset) {
    term.reset()
    seq = r.start || 0
  }
  if (r.data) {
    term.write(b64bytes(r.data))
    seq = r.seq
  } else if (typeof r.seq === 'number') {
    seq = r.seq
  }
  if (r.alive === false) {
    stopped = true
    term.write('\r\n\x1b[38;5;244m[连接已结束，退出码 ' + (r.exit == null ? '?' : r.exit) + ']\x1b[0m\r\n')
    emit('exit', r.exit)
    return true
  }
  return false
}

// ---------------- 输入 ----------------

// 输入必须串行：每个按键一个 POST 的话，并发请求到达顺序不保证，
// 快速输入会乱序甚至被服务端丢弃（实测 paste 一段命令只剩首字母）。
// WS 通道本身有序，走队列只是复用了同一套代码路径。
let queue = []
let flushing = false

function send(data) {
  if (!data || stopped) return
  queue.push(data)
  if (!flushing) flush()
}

async function flush() {
  flushing = true
  while (queue.length) {
    const chunk = queue.join('')
    queue = []
    const u8 = new TextEncoder().encode(chunk)
    if (!u8.length) continue
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ t: 'in', d: bytesB64(u8) }))
      continue
    }
    try {
      await api(`/api/term/${props.id}/input`, { method: 'POST', body: { data: bytesB64(u8) } })
    } catch (e) {
      if (/不存在|已关闭/.test(e.message || '')) { emit('exit', -1); break }
      await new Promise(r => setTimeout(r, 60))
    }
  }
  flushing = false
}

function pushResize(cols, rows) {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ t: 'resize', cols, rows }))
    return
  }
  api(`/api/term/${props.id}/resize`, { method: 'POST', body: { cols, rows } }).catch(() => {})
}

function hit(seqStr) {
  send(seqStr)
  if (term) term.focus()
}

function zoom(d) {
  if (!term) return
  fs = Math.max(9, Math.min(22, fs + d))
  term.options.fontSize = fs
  try { localStorage.setItem('pp_term_fs', String(fs)) } catch { /* ignore */ }
  nextTick(() => setTimeout(fitNow, 30))
}

function focus() { if (term) term.focus() }

function pasteText(text) {
  if (!text) return
  send(text)
  if (term) term.focus()
}

defineExpose({ focus, pasteText, fitNow })

watch(() => props.active, async (v) => {
  if (v) {
    if (!term) make()
    await nextTick()
    setTimeout(() => {
      openTerm()
      fitNow()
      startChannel()
      if (term) term.focus()
    }, 20)
  } else {
    closeChannel()
    setTransport('')
  }
})

watch(() => props.theme, (t) => {
  if (term) term.options.theme = themeOf(t)
})

watch(() => ui.termTransport, () => {
  if (props.active && opened && !stopped) { closeChannel(); startChannel() }
})

onMounted(() => {
  make()
  ro = new ResizeObserver(() => {
    clearTimeout(ro._t)
    ro._t = setTimeout(fitNow, 140)
  })
  if (host.value) ro.observe(host.value)
  if (props.active) {
    nextTick(() => setTimeout(() => {
      openTerm()
      fitNow()
    }, 30))
  }
})

onBeforeUnmount(() => {
  stopped = true
  closeChannel()
  if (ro) ro.disconnect()
  if (term) { term.dispose(); term = null }
})
</script>
