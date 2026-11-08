<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="bell" />监控告警</h2>
      <p>存活探测 + 阈值提醒 · 邮件直达手机，不做曲线与指标聚合</p>
    </div>
    <div class="ph-a">
      <button class="btn" :disabled="busy" @click="checkNow">
        <Icon name="refresh" :class="{ spin: busy }" />立即探测
      </button>
    </div>
  </div>

  <!-- 概览 -->
  <div class="al-stats">
    <div class="al-stat">
      <span class="al-stat-n">{{ st.item_total || 0 }}</span>
      <span class="al-stat-l">监控项<i v-if="st.manual_total">（手动 {{ st.manual_total }}）</i></span>
    </div>
    <div class="al-stat" :class="{ bad: st.alert_total }">
      <span class="al-stat-n">{{ st.alert_total || 0 }}</span>
      <span class="al-stat-l">告警中</span>
    </div>
    <div class="al-stat" :class="{ warn: st.unrecovered }">
      <span class="al-stat-n">{{ st.unrecovered || 0 }}</span>
      <span class="al-stat-l">未恢复事件</span>
    </div>
    <div class="al-stat">
      <span class="al-stat-n">{{ sendRate }}</span>
      <span class="al-stat-l">邮件发送成功</span>
    </div>
  </div>

  <div class="grid g2">
    <!-- SMTP -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="send" />
        <h3>邮件发信</h3>
        <div class="sub">
          <span class="al-dirty" v-if="smtpDirty">未保存</span>
          <span class="al-pill" :class="st.smtp?.configured ? 'ok' : 'off'">
            {{ st.smtp?.configured ? '已配置' : '未配置' }}
          </span>
        </div>
      </div>
      <div class="card-b">
        <div class="form-grid">
          <div class="field">
            <label>发件服务器</label>
            <input class="input" v-model.trim="smtp.host" placeholder="smtp.qq.com" autocapitalize="off" spellcheck="false">
          </div>
          <div class="field">
            <label>端口 / 加密</label>
            <div class="al-row">
              <input class="input" v-model.number="smtp.port" type="number" style="width:88px">
              <select class="input" v-model="smtp.security" style="flex:1">
                <option value="ssl">SSL（465）</option>
                <option value="starttls">STARTTLS（587）</option>
                <option value="none">不加密（25）</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label>发件邮箱</label>
            <input class="input" v-model.trim="smtp.user" placeholder="12345678@qq.com" autocapitalize="off" spellcheck="false">
          </div>
          <div class="field">
            <label>邮箱授权码 <span class="req">*</span></label>
            <input class="input" v-model="smtp.password" type="password"
                   :placeholder="smtp.password_masked || '不是邮箱登录密码'"
                   autocomplete="new-password">
            <span class="hint" v-if="smtp.has_password">已保存：{{ smtp.password_masked }}，留空表示不修改</span>
          </div>
          <div class="field span2">
            <label>收件人</label>
            <input class="input" v-model.trim="smtp.to" placeholder="默认与发件邮箱相同，多个用逗号分隔">
          </div>
          <div class="field span2">
            <label>抄送（可选）</label>
            <input class="input" v-model.trim="smtp.cc" placeholder="多个用逗号分隔">
          </div>
        </div>

        <details class="al-help">
          <summary>怎么拿到授权码？（QQ / 163 邮箱）</summary>
          <div class="al-help-b">
            <p><b>QQ 邮箱</b></p>
            <ol>
              <li>电脑打开 <code>mail.qq.com</code> → 右上角「设置」→「账号与安全」→「安全设置」</li>
              <li>找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」，开启 <b>IMAP/SMTP 服务</b></li>
              <li>按提示用手机发短信验证，成功后页面会显示一串 <b>16 位授权码</b></li>
              <li>把这串授权码填到上面，<b>不是 QQ 登录密码</b></li>
            </ol>
            <p><b>163 / 126 邮箱</b>：设置 → POP3/SMTP/IMAP → 开启 SMTP → 设置客户端授权密码。</p>
            <p class="dim">授权码只存在本机 <code>data/alerts.json</code> 里，用 <code>data/secret.key</code>
              加密；界面只显示尾 4 位，服务端从不回传明文。</p>
          </div>
        </details>

        <div class="al-test" v-if="test.ok !== null || test.message">
          <span class="dot" :class="test.ok === true ? '' : test.ok === false ? 'fail' : 'busy'"></span>
          <span :class="test.ok === false ? 'err' : 'ok-txt'">{{ test.message }}</span>
        </div>
        <div class="al-dirty-tip" v-if="smtpDirty">
          有未保存的修改 · 自动刷新不会覆盖已填内容，点「保存配置」后才会生效
        </div>
      </div>
      <div class="card-f">
        <button class="btn" style="flex:1" :disabled="saving" @click="saveSmtp">
          <span v-if="saving" class="spinner"></span>保存配置
        </button>
        <button class="btn primary" style="flex:1" :disabled="testing || smtpDirty"
                :title="smtpDirty ? '有未保存的修改，先保存再测试' : ''" @click="sendTest">
          <span v-if="testing" class="spinner"></span>{{ testing ? '发送中…' : '发送测试邮件' }}
        </button>
      </div>
    </div>

    <!-- 阈值与联动 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="shield" />
        <h3>告警策略</h3>
        <div class="sub">
          <span class="al-dirty" v-if="tuneDirty">未保存</span>
          <span class="fs11">防抖优先，宁少勿滥</span>
        </div>
      </div>
      <div class="card-b">
        <div class="form-grid">
          <div class="field">
            <label>连续失败几次告警</label>
            <input class="input" v-model.number="tune.fail_threshold" type="number" min="1" max="10">
            <span class="hint">默认 3 次（约 2 分钟）</span>
          </div>
          <div class="field">
            <label>静默期（分钟）</label>
            <input class="input" v-model.number="tune.silence_min" type="number" min="0" max="1440">
            <span class="hint">期间不重复发信，0 表示不限</span>
          </div>
          <div class="field">
            <label>探测间隔（秒）</label>
            <input class="input" v-model.number="tune.poll_interval" type="number" min="15" max="3600">
            <span class="hint">默认 60 秒</span>
          </div>
          <div class="field">
            <label>低电量阈值（%）</label>
            <input class="input" v-model.number="tune.battery_low" type="number" min="1" max="100">
            <span class="hint">未充电且低于此值告警</span>
          </div>
        </div>

        <div class="al-sect">开箱即用的联动规则</div>
        <div class="al-rule" v-for="r in ruleDefs" :key="r.key">
          <div class="al-rule-t">
            <b>{{ r.label }}</b>
            <span class="hint">{{ r.desc }}</span>
          </div>
          <button class="switch" :class="{ on: st.rules?.[r.key] }"
                  @click="toggleRule(r.key)" :aria-label="r.label"></button>
        </div>
      </div>
      <div class="card-f">
        <button class="btn primary" style="flex:1" :disabled="saving" @click="saveTune">
          <span v-if="saving" class="spinner"></span>保存策略
        </button>
      </div>
    </div>
  </div>

  <!-- 监控项 -->
  <div class="card flush">
    <div class="card-h">
      <Icon name="activity" />
      <h3>监控项</h3>
      <div class="sub"><span class="fs11">{{ items.length }} 个 · 每分钟探测一次</span></div>
      <button class="btn sm" @click="addOpen = !addOpen">
        <Icon :name="addOpen ? 'close' : 'plus'" />{{ addOpen ? '收起' : '添加' }}
      </button>
    </div>

    <div class="card-b al-add" v-if="addOpen">
      <div class="al-kind">
        <button :class="{ on: nf.kind === 'url' }" @click="nf.kind = 'url'">URL 拨测</button>
        <button :class="{ on: nf.kind === 'port' }" @click="nf.kind = 'port'">端口探活</button>
      </div>
      <div class="form-grid">
        <div class="field">
          <label>名称</label>
          <input class="input" v-model.trim="nf.name" placeholder="例如 官网 / 面板端口">
        </div>
        <div class="field" v-if="nf.kind === 'url'">
          <label>URL <span class="req">*</span></label>
          <input class="input" v-model.trim="nf.target" placeholder="https://example.com/health" spellcheck="false">
        </div>
        <template v-else>
          <div class="field">
            <label>主机 <span class="req">*</span></label>
            <input class="input" v-model.trim="nf.host" placeholder="127.0.0.1">
          </div>
          <div class="field">
            <label>端口 <span class="req">*</span></label>
            <input class="input" v-model.number="nf.port_num" type="number" placeholder="8888">
          </div>
        </template>
        <template v-if="nf.kind === 'url'">
          <div class="field">
            <label>请求方法</label>
            <select class="input" v-model="nf.method">
              <option value="GET">GET</option>
              <option value="HEAD">HEAD</option>
            </select>
          </div>
          <div class="field">
            <label>期望状态码</label>
            <input class="input" v-model.number="nf.expect" type="number" placeholder="200">
          </div>
        </template>
        <div class="field">
          <label>超时（秒）</label>
          <input class="input" v-model.number="nf.timeout" type="number" placeholder="10">
        </div>
      </div>
      <div class="err" v-if="addErr">{{ addErr }}</div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:4px">
        <button class="btn" @click="resetAdd">重置</button>
        <button class="btn primary" :disabled="saving" @click="saveItem">保存监控项</button>
      </div>
    </div>

    <div class="tbl-wrap">
      <table class="tbl al-tbl">
        <thead>
          <tr>
            <th>状态</th><th>名称</th><th>类型</th><th>目标</th>
            <th>最近探测</th><th>失败</th><th style="text-align:right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td data-l="状态">
              <span class="state" :class="stateCls(it.state)">
                <span class="dot" :class="dotCls(it.state)"></span>{{ it.state_text }}
              </span>
            </td>
            <td data-l="名称"><b>{{ it.name }}</b><div class="hint" v-if="!it.manual">自动</div></td>
            <td data-l="类型"><span class="al-tag">{{ it.kind_text }}</span></td>
            <td class="al-target" data-l="目标">
              <span class="mono">{{ it.target || '—' }}</span>
              <div class="hint" v-if="it.last_error">{{ it.last_error }}</div>
            </td>
            <td class="mono fs12" data-l="最近">{{ it.last_check ? fmtTime(it.last_check) : '—' }}</td>
            <td data-l="失败">
              <span :class="it.fails ? 'err' : 'dim'">{{ it.fails || 0 }}</span>
              <!-- 静默只在「告警中」有意义：恢复后计时器还留着，但跟用户无关 -->
              <span class="hint" v-if="it.silence_left && it.state === 'alerting'">
                · 静默 {{ Math.ceil(it.silence_left / 60) }}m</span>
            </td>
            <td data-l="">
              <div class="tbl-ops">
                <button class="btn xs" @click="toggleItem(it)">{{ it.enabled ? '停用' : '启用' }}</button>
                <button class="btn xs danger" v-if="it.manual" @click="delItem(it)">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="7" class="tbl-empty">还没有监控项。添加一个 URL 或端口，或打开上面的联动规则。</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 历史 -->
  <div class="card flush">
    <div class="card-h">
      <Icon name="history" />
      <h3>告警历史</h3>
      <div class="sub"><span class="fs11">{{ ev.total }} 条 · 未恢复 {{ ev.unrecovered }}</span></div>
      <button class="btn sm" :disabled="!ev.total" @click="clearEvents">
        <Icon name="broom" />清空已恢复
      </button>
    </div>
    <div class="card-b" style="padding-top:2px">
      <div class="al-ev" v-for="e in ev.items" :key="e.id">
        <span class="dot" :class="e.kind === 'alert' ? 'fail' : e.kind === 'recover' ? '' : 'warn'"></span>
        <div class="al-ev-main">
          <b>{{ e.subject || e.name || '（无主题）' }}</b>
          <div class="hint">
            {{ fmtFull(e.ts) }} · {{ e.name || '—' }}
            <span v-if="e.target"> · {{ e.target }}</span>
            <span v-if="!e.ok" class="err"> · 发送失败：{{ e.error }}</span>
          </div>
        </div>
        <span class="al-pill" :class="evPillCls(e)">{{ evPillText(e) }}</span>
      </div>
      <div class="tbl-empty" v-if="!ev.items?.length">还没有告警记录。故障触发后会在这里留一条。</div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { api, fmtTime, fmtFull } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'

const st = ref({ smtp: {}, counts: {}, rules: {}, send_stat: {}, test: {} })
const items = ref([])
const ev = ref({ items: [], total: 0, unrecovered: 0 })
const test = ref({ ok: null, message: '' })
const busy = ref(false)
const saving = ref(false)
const testing = ref(false)
const addOpen = ref(false)
const addErr = ref('')

const smtp = reactive({
  host: '', port: 465, security: 'ssl', user: '', sender: '',
  to: '', cc: '', password: '', has_password: false, password_masked: ''
})
const tune = reactive({ fail_threshold: 3, silence_min: 30, poll_interval: 60, battery_low: 20 })
const nf = reactive({
  kind: 'url', name: '', target: '', method: 'GET', expect: 200, timeout: 10,
  host: '127.0.0.1', port_num: 0
})

// 表单基线：上一次「从服务端写进表单」的值。轮询回来时拿当前表单跟它比，
// 一样说明用户没动过、可以安全覆盖；不一样说明正在编辑，一个字都不能改。
const smtpBase = ref('')
const tuneBase = ref('')

function smtpSnap() {
  return JSON.stringify([smtp.host, smtp.port, smtp.security, smtp.user,
    smtp.sender, smtp.to, smtp.cc])
}
function tuneSnap() {
  return JSON.stringify([tune.fail_threshold, tune.silence_min,
    tune.poll_interval, tune.battery_low])
}
const smtpDirty = computed(() => smtpBase.value !== '' && smtpSnap() !== smtpBase.value)
const tuneDirty = computed(() => tuneBase.value !== '' && tuneSnap() !== tuneBase.value)

const ruleDefs = [
  { key: 'tunnel', label: '隧道掉线', desc: '联动 M15：frpc 停止重试、cloudflared 异常退出或公网拨测失败' },
  { key: 'container', label: '容器宕机', desc: '联动 M17：容器意外退出且退出码不为 0' },
  { key: 'schedule', label: '计划任务失败', desc: '联动 M08：定时备份或脚本执行失败、超时' },
  { key: 'net', label: '网络断开', desc: '设备失去所有 IP（断网 / 飞行模式）时提醒' },
  { key: 'battery', label: '电量过低', desc: '未充电且电量低于阈值时提醒，避免设备静默关机' }
]

const sendRate = computed(() => {
  const s = st.value.send_stat || {}
  const ok = s.ok || 0, fail = s.fail || 0
  if (!ok && !fail) return '—'
  return Math.round(ok / (ok + fail) * 100) + '%'
})

function stateCls(s) {
  return { healthy: 'run', retrying: 'warn', alerting: 'fail', recovered: 'run' }[s] || 'stop'
}
function dotCls(s) {
  return { healthy: '', retrying: 'warn', alerting: 'fail', recovered: '' }[s] || 'off'
}

// 历史徽章：测试邮件不是告警，不该显示「未恢复」
function evPillText(e) {
  if (e.kind === 'test') return e.ok ? '已发送' : '发送失败'
  if (e.kind === 'recover') return '已恢复'
  if (e.auto_closed) return '已失效'
  return e.recovered ? '已恢复' : '未恢复'
}
function evPillCls(e) {
  if (e.kind === 'test') return e.ok ? 'ok' : 'bad'
  if (e.kind === 'recover') return 'ok'
  if (e.auto_closed) return 'off'
  return e.recovered ? 'ok' : 'bad'
}

function fill(s, force = {}) {
  st.value = s
  const m = s.smtp || {}
  // 这两个只用于打码展示，任何时候都可以同步
  smtp.has_password = !!m.has_password
  smtp.password_masked = m.password_masked || ''
  // 正在等测试结果时，别让轮询把「正在发送…」覆盖成上一次的结果
  if (!testing.value) {
    const t = s.test || {}
    if (t.ok !== null && t.ok !== undefined) test.value = { ok: t.ok, message: t.message }
  }

  // 表单分两块各自判断：用户正在编辑的那块绝不回填。
  // 否则 6 秒一次的轮询会把没保存的输入冲掉——正在填授权码，回来一看全空了。
  if (force.smtp || !smtpDirty.value) {
    smtp.host = m.host || ''
    smtp.port = m.port || 465
    smtp.security = m.security || 'ssl'
    smtp.user = m.user || ''
    smtp.sender = m.sender || ''
    smtp.to = (m.to || []).join(', ')
    smtp.cc = (m.cc || []).join(', ')
    // 密码框永远不清空：清空应当只发生在「保存成功」之后
    smtpBase.value = smtpSnap()
  }
  if (force.tune || !tuneDirty.value) {
    tune.fail_threshold = s.fail_threshold ?? 3
    tune.silence_min = s.silence_min ?? 30
    tune.poll_interval = s.poll_interval ?? 60
    tune.battery_low = s.battery_low ?? 20
    tuneBase.value = tuneSnap()
  }
}

async function load(force = {}) {
  try {
    const [a, b, c] = await Promise.all([
      api('/api/alerts/status'), api('/api/alerts/items'), api('/api/alerts/events?limit=200')
    ])
    fill(a, force)
    items.value = b.items || []
    ev.value = c
  } catch { /* 轮询失败静默 */ }
}

async function saveSmtp() {
  saving.value = true
  try {
    await api('/api/alerts/smtp', {
      method: 'POST',
      body: {
        host: smtp.host, port: smtp.port, security: smtp.security,
        user: smtp.user, sender: smtp.sender || smtp.user,
        to: smtp.to, cc: smtp.cc, password: smtp.password
      }
    })
    toast('邮件配置已保存', 'ok')
    smtp.password = ''
    await load({ smtp: true })
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

async function sendTest() {
  testing.value = true
  test.value = { ok: null, message: '正在发送…' }
  try {
    const r = await api('/api/alerts/test', { method: 'POST' })
    if (!r.started) {
      test.value = { ok: false, message: r.message || '无法开始发送' }
      return
    }
    // 最多等 30 秒，每 1 秒问一次结果
    for (let i = 0; i < 30; i++) {
      await new Promise(r2 => setTimeout(r2, 1000))
      const t = await api('/api/alerts/test')
      if (t.ok !== null && t.ok !== undefined) {
        test.value = { ok: t.ok, message: t.message }
        toast(t.ok ? '测试邮件已发送' : '发送失败', t.ok ? 'ok' : 'err')
        break
      }
      test.value = { ok: null, message: t.message || '正在发送…' }
    }
    if (test.value.ok === null) test.value = { ok: false, message: '等待超时，请检查网络或服务器地址' }
  } catch (e) {
    test.value = { ok: false, message: e.message }
  } finally {
    testing.value = false
    load()
  }
}

async function saveTune() {
  saving.value = true
  try {
    await api('/api/alerts/settings', {
      method: 'POST',
      body: {
        fail_threshold: tune.fail_threshold,
        silence_min: tune.silence_min,
        poll_interval: tune.poll_interval,
        battery_low: tune.battery_low
      }
    })
    toast('策略已保存', 'ok')
    await load({ tune: true })
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

async function toggleRule(key) {
  const next = { ...(st.value.rules || {}) }
  next[key] = !next[key]
  st.value.rules = next
  try {
    await api('/api/alerts/settings', { method: 'POST', body: { rules: next } })
    toast(next[key] ? '已开启' : '已关闭', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err'); await load() }
}

async function checkNow() {
  busy.value = true
  try {
    const r = await api('/api/alerts/check', { method: 'POST' })
    toast(`已探测 ${r.checked} 项 · 告警 ${r.alerted} · 恢复 ${r.recovered}`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

function resetAdd() {
  Object.assign(nf, {
    kind: 'url', name: '', target: '', method: 'GET', expect: 200, timeout: 10,
    host: '127.0.0.1', port_num: 0
  })
  addErr.value = ''
}

async function saveItem() {
  addErr.value = ''
  saving.value = true
  try {
    await api('/api/alerts/items/save', {
      method: 'POST',
      body: {
        kind: nf.kind, name: nf.name, target: nf.target, method: nf.method,
        expect: nf.expect, timeout: nf.timeout, host: nf.host, port_num: nf.port_num
      }
    })
    toast('监控项已添加', 'ok')
    resetAdd()
    addOpen.value = false
    await load()
  } catch (e) { addErr.value = e.message } finally { saving.value = false }
}

async function toggleItem(it) {
  try {
    await api('/api/alerts/items/toggle', {
      method: 'POST', body: { id: it.id, enabled: !it.enabled }
    })
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function delItem(it) {
  if (!confirm(`删除监控项「${it.name}」？`)) return
  try {
    await api('/api/alerts/items/delete', { method: 'POST', body: { id: it.id } })
    toast('已删除', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function clearEvents() {
  if (!confirm('清空所有已恢复的历史记录？未恢复的会保留。')) return
  try {
    const r = await api('/api/alerts/events/clear', { method: 'POST', body: {} })
    toast(`已清理 ${r.removed} 条`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

let timer = 0
onMounted(async () => { await load(); timer = setInterval(load, 6000) })
onUnmounted(() => clearInterval(timer))
</script>
