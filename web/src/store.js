import { reactive } from 'vue'

export const ui = reactive({
  toasts: [],
  page: 'overview',
  user: '',
  theme: 'dark',
  drawer: false,
  version: '',
  // 终端传输方式：auto（默认，WS 优先、失败退回轮询）/ ws / poll
  termTransport: (() => {
    try { return localStorage.getItem('pp_term_transport') || 'auto' } catch { return 'auto' }
  })()
})

export function setTermTransport(t) {
  ui.termTransport = t
  try { localStorage.setItem('pp_term_transport', t) } catch { /* 隐私模式 */ }
}

let seq = 0
export function toast(msg, type = '') {
  const id = ++seq
  ui.toasts.push({ id, msg, type })
  setTimeout(() => {
    const i = ui.toasts.findIndex(t => t.id === id)
    if (i >= 0) ui.toasts.splice(i, 1)
  }, type === 'err' ? 3600 : 2400)
}

export function applyTheme(t) {
  ui.theme = t
  document.documentElement.setAttribute('data-theme', t)
  try { localStorage.setItem('pp_theme', t) } catch { /* ignore */ }
  const m = document.querySelector('meta[name=theme-color]')
  if (m) m.setAttribute('content', t === 'dark' ? '#070a10' : '#f2f5fa')
}
