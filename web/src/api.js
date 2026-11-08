// 所有接口调用的唯一入口：401 统一跳登录，错误统一抛 Error
export async function api(path, opt = {}) {
  const o = { credentials: 'same-origin', ...opt }
  o.headers = { ...(opt.headers || {}) }
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body)
    o.headers['Content-Type'] = 'application/json'
  }
  const r = await fetch(path, o)
  if (r.status === 401 && !path.startsWith('/api/login')) {
    window.dispatchEvent(new CustomEvent('pp-unauth'))
    throw new Error('登录已过期')
  }
  const text = await r.text()
  let j = {}
  try { j = text ? JSON.parse(text) : {} } catch { j = {} }
  if (!r.ok) {
    const e = new Error(j.error || `HTTP ${r.status}`)
    e.fieldErrors = j.field_errors
    e.payload = j
    throw e
  }
  return j
}

export const fmtB = (n) => {
  n = n || 0
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB'
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB'
  return n + ' B'
}

export const fmtDur = (s) => {
  s = Math.floor(s || 0)
  if (s < 60) return s + ' 秒'
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60)
  if (d) return `${d} 天 ${h} 小时`
  if (h) return `${h} 小时 ${m} 分`
  return m + ' 分钟'
}

const pad = (x) => (x < 10 ? '0' : '') + x

export const fmtTime = (ts) => {
  if (!ts) return '—'
  const d = new Date(ts * 1000), n = new Date()
  if (d.toDateString() === n.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export const fmtFull = (ts) => {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 分片上传：1MB 一片，中断后带同一个 id 重连，服务端返回已传分片
export async function uploadFile(file, dir, onProgress, isAborted = () => false) {
  const CHUNK = 1 << 20
  const key = `ppup:${dir}:${file.name}:${file.size}:${file.lastModified}`
  let saved = ''
  try { saved = localStorage.getItem(key) || '' } catch { /* 隐私模式 */ }

  const init = await api('/api/upload/init', {
    method: 'POST',
    body: { path: dir, name: file.name, size: file.size, chunk_size: CHUNK, id: saved }
  })
  try { localStorage.setItem(key, init.id) } catch { /* ignore */ }

  const uploaded = new Set(init.uploaded || [])
  const todo = []
  for (let i = 0; i < init.total; i++) if (!uploaded.has(i)) todo.push(i)
  let sent = (init.total - todo.length) * init.chunk_size
  onProgress(Math.min(sent, file.size), file.size)

  for (const idx of todo) {
    if (isAborted()) throw new Error('已取消')
    const start = idx * init.chunk_size
    const blob = file.slice(start, Math.min(start + init.chunk_size, file.size))
    const r = await fetch(`/api/upload/${init.id}/${idx}`, {
      method: 'PUT', body: blob, credentials: 'same-origin'
    })
    if (!r.ok) {
      const j = await r.json().catch(() => ({}))
      throw new Error(j.error || `分片 ${idx} 上传失败`)
    }
    sent += blob.size
    onProgress(Math.min(sent, file.size), file.size)
  }
  const done = await api(`/api/upload/${init.id}/complete`, { method: 'POST' })
  try { localStorage.removeItem(key) } catch { /* ignore */ }
  return done
}

// 轮询任务直到终态
export function pollTask(id, cb, interval = 400, maxTries = 500) {
  let n = 0
  const tick = async () => {
    try {
      const t = await api('/api/exec/' + id)
      if (t.status === 'running' && n++ < maxTries) return setTimeout(tick, interval)
      cb(t)
    } catch (e) {
      cb({ status: 'failed', output: e.message })
    }
  }
  setTimeout(tick, interval)
}
