/* Cron 引擎（纯函数）：与后端 sched.py 同一套语义，表单预览在本地算，不走网络。
   两份实现靠 tests 对齐：这里改了规则，sched.py 的 describe/next_run 要同步。 */

const pad2 = (n) => String(n).padStart(2, '0')
const FIELD_RE = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/
const DOW = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
export const ALIASES = {
  '@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *',
  '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *', '@yearly': '0 0 1 1 *'
}
export const normalize = (e) => ALIASES[String(e || '').trim().toLowerCase()] || String(e || '').trim().toLowerCase()

function parseField(raw, lo, hi) {
  const s = String(raw == null ? '' : raw).trim()
  if (s === '') return { err: '不能为空' }
  if (s === '*') return { set: null }
  const set = new Set()
  for (const seg of s.split(',')) {
    if (seg === '') return { err: '存在空的取值段' }
    const m = seg.match(FIELD_RE)
    if (!m) return { err: '无法识别 ' + seg }
    const step = m[3] ? parseInt(m[3], 10) : 1
    if (step < 1) return { err: '步长必须大于 0' }
    let start, end
    if (m[1] === '*') { start = lo; end = hi }
    else {
      start = parseInt(m[1], 10)
      end = m[2] !== undefined ? parseInt(m[2], 10) : (m[3] !== undefined ? hi : start)
    }
    if (start < lo || start > hi || end < lo || end > hi) return { err: '取值应在 ' + lo + '-' + hi }
    if (start > end) return { err: '起始值大于结束值' }
    for (let v = start; v <= end; v += step) set.add(v)
  }
  return set.size ? { set } : { err: '没有有效取值' }
}

const FIELDS = [
  ['minute', '分钟', 0, 59], ['hour', '小时', 0, 23], ['dom', '日', 1, 31],
  ['month', '月', 1, 12], ['dow', '星期', 0, 7]
]

export function parse(expr) {
  const e = normalize(expr)
  const parts = e.split(/\s+/)
  if (parts.length !== 5) {
    return { err: '需要 5 个字段：分 时 日 月 周（当前 ' + parts.length + ' 个）', parts }
  }
  const f = {}
  FIELDS.forEach(([key, label, lo, hi], i) => {
    const r = parseField(parts[i], lo, hi)
    f[key] = r.set !== undefined ? r : { err: label + '：' + r.err }
  })
  for (const [key, label] of FIELDS) {
    if (f[key].err) return { err: f[key].err, field: key, parts }
  }
  if (f.dow.set && f.dow.set.has(7)) { f.dow.set.add(0); f.dow.set.delete(7) }
  return { f, parts }
}

/* 日与周同时受限时按 Vixie cron 语义取 OR */
function dayOk(f, d) {
  const domAny = !f.dom.set, dowAny = !f.dow.set
  const domOk = domAny || f.dom.set.has(d.getDate())
  const dowOk = dowAny || f.dow.set.has(d.getDay())
  if (domAny && dowAny) return true
  if (domAny) return dowOk
  if (dowAny) return domOk
  return domOk || dowOk
}

/* 严格晚于 from 的下一次执行时间戳（秒） */
export function nextRun(expr, from) {
  const p = parse(expr)
  if (p.err) return null
  const f = p.f
  const base = from ? new Date(from * 1000) : new Date()
  base.setSeconds(0, 0)
  base.setMinutes(base.getMinutes() + 1)
  let startMin = base.getHours() * 60 + base.getMinutes()
  let d = new Date(base.getFullYear(), base.getMonth(), base.getDate())
  for (let i = 0; i < 730; i++) {
    if (f.month.set && !f.month.set.has(d.getMonth() + 1)) {
      d = new Date(d.getFullYear(), d.getMonth() + 1, 1); continue
    }
    if (dayOk(f, d)) {
      for (let h = 0; h < 24; h++) {
        if (f.hour.set && !f.hour.set.has(h)) continue
        for (let mi = 0; mi < 60; mi++) {
          if (f.minute.set && !f.minute.set.has(mi)) continue
          if (h * 60 + mi >= startMin) return d.getTime() / 1000 + h * 3600 + mi * 60
        }
      }
    }
    d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)
    startMin = 0
  }
  return null
}

const joinSet = (s) => [...s].sort((a, b) => a - b).join('、')

export function describe(expr) {
  const e = normalize(expr)
  const named = {
    '0 * * * *': '每小时整点', '0 0 * * *': '每天 00:00', '0 0 * * 0': '每周日 00:00',
    '0 0 1 * *': '每月 1 日 00:00', '0 0 1 1 *': '每年 1 月 1 日 00:00'
  }
  if (named[e]) return named[e]
  const p = parse(e)
  if (p.err) return '表达式无效'
  const f = p.f
  const [mi, ho, dom, mo, dow] = p.parts
  const isNum = (v) => /^\d+$/.test(v)
  const timeText = isNum(mi) && isNum(ho) ? pad2(+ho) + ':' + pad2(+mi) : null
  const othersAll = dom === '*' && mo === '*' && dow === '*'

  if (/^\*\/\d+$/.test(mi) && ho === '*' && dom === '*' && mo === '*' && dow === '*')
    return '每 ' + mi.slice(2) + ' 分钟'
  if (/^\*\/\d+$/.test(ho) && dom === '*' && mo === '*' && dow === '*') {
    const tail = isNum(mi) && +mi ? '第 ' + (+mi) + ' 分' : '整点'
    return '每 ' + ho.slice(2) + ' 小时（' + tail + '）'
  }
  if (timeText && othersAll) return '每天 ' + timeText
  if (timeText && dom === '*' && mo === '*' && dow !== '*') {
    return '每' + joinSet(f.dow.set).split('、').map(x => DOW[+x] || x).join('、') + ' ' + timeText
  }
  if (timeText && dom !== '*' && mo === '*' && dow === '*')
    return '每月 ' + joinSet(f.dom.set) + ' 日 ' + timeText
  if (timeText && mo !== '*')
    return '每年 ' + joinSet(f.month.set) + ' 月 ' + (f.dom.set ? joinSet(f.dom.set) + ' 日 ' : '每') + ' ' + timeText
  if (othersAll && mi !== '*' && ho !== '*') {
    const hd = f.hour.set === null ? '每小时' : joinSet(f.hour.set) + ' 点'
    const md = f.minute.set === null ? '整点' : '第 ' + joinSet(f.minute.set) + ' 分'
    return '每天 ' + hd + md
  }
  return '自定义（' + e + '）'
}

export function preview(expr, count = 3) {
  const p = parse(expr)
  if (p.err) return { valid: false, err: p.err, human: '表达式无效', next: [] }
  let ts = Date.now() / 1000
  const out = []
  for (let i = 0; i < count; i++) {
    const n = nextRun(expr, ts)
    if (n == null) break
    out.push(Math.round(n))
    ts = n + 60
  }
  return { valid: true, human: describe(expr), next: out }
}

/* 「今天 18:15 · 3 分钟后」 */
export function fmtNext(ts, now = Date.now() / 1000) {
  if (!ts) return '—'
  const d = new Date(ts * 1000), n = new Date()
  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes())
  const diff = Math.round(ts - now / 1)
  const days = Math.floor((new Date(ts * 1000).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000)
  let when
  if (days === 0) when = '今天 ' + hm
  else if (days === 1) when = '明天 ' + hm
  else when = (d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + hm
  if (diff >= 0 && diff < 60) when += ' · 1 分钟内'
  else if (diff >= 60 && diff < 3600) when += ' · ' + Math.floor(diff / 60) + ' 分钟后'
  else if (diff >= 3600 && days === 0) when += ' · ' + Math.floor(diff / 3600) + ' 小时后'
  return when
}

/* 简易模式 → cron 表达式 */
export function buildExpr(kind, opts) {
  const o = opts || {}
  const mi = String(parseInt(o.minute || 0, 10) % 60)
  if (kind === 'reboot') return '@reboot'
  if (kind === 'everyN') {
    const n = Math.max(1, parseInt(o.every || 5, 10))
    return n > 59 ? `0 */${Math.round(n / 60)} * * *` : `*/${n} * * * *`
  }
  if (kind === 'hourly') {
    const n = Math.max(1, parseInt(o.every || 1, 10))
    return `${mi} */${n} * * *`
  }
  if (kind === 'daily') return `${mi} ${pad2(parseInt(o.hour || 0, 10) % 24)} * * *`
  if (kind === 'weekly') {
    const w = (o.weeks && o.weeks.length ? o.weeks : [1]).map(Number).sort()
    return `${mi} ${parseInt(o.hour || 0, 10) % 24} * * ${w.join(',')}`
  }
  if (kind === 'monthly') {
    const d = Math.max(1, Math.min(31, parseInt(o.dom || 1, 10)))
    return `${mi} ${parseInt(o.hour || 0, 10) % 24} ${d} * *`
  }
  return String(o.expr || '')
}
