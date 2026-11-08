<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="globe" />Cloudflare 穿透</h2>
      <p>cloudflared 隧道 · 不需要公网 IP · 临时地址或固定域名</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load(true)"><Icon name="refresh" />刷新</button>
      <button class="btn" @click="openAccount"><Icon name="key" />账号</button>
    </div>
  </div>

  <!-- 客户端状态 -->
  <div class="card flush" style="margin-bottom:14px">
    <div class="card-h">
      <Icon name="package" class="ni s16" style="color:var(--brand)" />
      <b class="grow">cloudflared 客户端</b>
      <span class="tag" :class="cli.found ? (cli.ready ? 'g' : 'y') : 'y'">
        {{ cli.found ? (cli.ready ? '就绪' : '缺依赖') : '未安装' }}
      </span>
    </div>
    <div class="card-b" style="padding:10px 14px;max-width:620px">
      <template v-if="cli.found">
        <div class="cf-kv"><span>版本</span><b class="mono" :title="cli.version">{{ shortVer }}</b></div>
        <div class="cf-kv"><span>路径</span><b class="mono ell" :title="cli.path">{{ cli.path }}</b></div>
        <div class="cf-kv" v-if="cli.termux">
          <span>运行依赖</span>
          <b>
            <span class="tag" :class="cli.proot ? 'g' : 'r'">proot</span>
            <span class="tag" :class="cli.resolv_conf ? 'g' : 'r'">resolv.conf</span>
            <span class="tag" :class="cli.ca_file ? 'g' : 'r'">CA 证书</span>
          </b>
        </div>
        <div class="cf-note" v-if="cli.termux">
          Termux 上 Go 程序读不到 <span class="mono">/etc/resolv.conf</span>，启动时会用 proot
          把它绑进去，再用 <span class="mono">SSL_CERT_FILE</span> 指到 CA——这两步是自动的。
        </div>
      </template>
      <div v-else class="dim" style="font-size:12px;line-height:1.75">
        cloudflared 是 Cloudflare 官方的隧道客户端，约 38MB。装好后可以直接把本机端口
        映射到公网，不用公网 IP、不用路由器端口转发、不用备案。
      </div>
    </div>
    <div class="card-f" style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn xs danger" v-if="cli.found" @click="uninstall"><Icon name="trash" />卸载</button>
      <button class="btn xs primary" @click="openInstall">
        <Icon name="download" />{{ cli.found ? '重装 / 升级' : '一键安装' }}
      </button>
    </div>
  </div>

  <!-- 两种入口 -->
  <div class="cf-entries">
    <div class="card cf-entry">
      <div class="cf-entry-h">
        <span class="cf-badge quick">临时</span>
        <b>临时地址</b>
        <span class="tag">免账号</span>
      </div>
      <p class="cf-entry-p">
        点一下给一个 <span class="mono">https://xxx.trycloudflare.com</span>，几分钟就能发给别人看。
        每次启动地址都会变，Cloudflare 不承诺可用性。
      </p>
      <div class="cf-entry-form">
        <div class="field">
          <label>本地端口</label>
          <input class="input" v-model.number="quick.port" type="number" min="1" max="65535" placeholder="8888" />
        </div>
        <button class="btn primary" :disabled="quick.busy || !cli.ready" @click="startQuick">
          <span v-if="quick.busy" class="spinner"></span><Icon v-else name="play" />
          {{ quick.busy ? '开通中…' : '开通' }}
        </button>
      </div>
      <div v-if="quick.url" class="cf-result">
        <div class="cf-result-url mono">{{ quick.url }}</div>
        <div class="cf-result-ops">
          <button class="btn xs" @click="copy(quick.url)"><Icon name="copy" />复制</button>
          <a class="btn xs" :href="quick.url" target="_blank" rel="noreferrer"><Icon name="external" />打开</a>
        </div>
      </div>
      <div v-if="quick.err" class="cf-err">{{ quick.err }}</div>
    </div>

    <div class="card cf-entry">
      <div class="cf-entry-h">
        <span class="cf-badge named">固定</span>
        <b>固定域名</b>
        <span class="tag">需账号</span>
      </div>
      <p class="cf-entry-p">
        用自己接进 Cloudflare 的域名，比如 <span class="mono">panel.example.com</span>。
        自动建隧道、配 ingress、加 CNAME，地址永远不变。
      </p>
      <div class="cf-entry-form">
        <div class="field grow">
          <div class="cf-lbl">
            <span>域名</span>
            <button v-if="settings.has_creds" class="cf-mini" :disabled="zonesBusy"
                    :title="zones.length ? '重新读取域名列表' : '读取域名列表'" @click="loadZones(true)">
              <span v-if="zonesBusy" class="spinner"></span><Icon v-else name="refresh" />
            </button>
          </div>
          <div v-if="zones.length" class="cf-host">
            <input class="input" v-model.trim="named.sub" placeholder="panel" spellcheck="false" />
            <span class="cf-dot">.</span>
            <select class="input cf-zone" v-model="named.zone" @change="rememberZone">
              <option v-for="z in zones" :key="z.id" :value="z.name">{{ z.name }}</option>
            </select>
          </div>
          <input v-else class="input" v-model.trim="named.host" placeholder="panel.example.com"
                 spellcheck="false" />
        </div>
        <div class="field" style="width:110px">
          <label>端口</label>
          <input class="input" v-model.number="named.port" type="number" min="1" max="65535" placeholder="8888" />
        </div>
      </div>
      <div v-if="namedHost" class="cf-host-preview" :class="{ bad: !!namedBad }">
        <span class="dim">地址</span>
        <span class="mono">https://{{ namedHost }}</span>
        <span v-if="namedBad" class="cf-bad">{{ namedBad }}</span>
      </div>
      <div v-else-if="settings.has_creds && !zones.length && !zonesBusy" class="cf-host-preview">
        <span class="dim">没读到你账号下的域名，点「域名」右边 ↻ 试一次，或直接填完整域名</span>
      </div>
      <div class="cf-entry-ft">
        <span class="dim" style="font-size:11.5px" v-if="!settings.has_creds">
          还没配账号，先点右上角「账号」
        </span>
        <span v-else class="dim" style="font-size:11.5px">
          {{ settings.mode === 'token' ? 'API 令牌' : 'Global Key' }} ·
          {{ zones.length ? zones.length + ' 个域名' : '域名列表未加载' }}
        </span>
        <button class="btn primary" :disabled="named.busy || !cli.ready || !namedHost || !!namedBad"
                @click="createNamed">
          <span v-if="named.busy" class="spinner"></span><Icon v-else name="plus" />
          {{ named.busy ? '创建中…' : '创建隧道' }}
        </button>
      </div>
      <div v-if="named.err" class="cf-err">{{ named.err }}</div>
      <div v-if="named.taskLog" class="cf-tasklog mono">{{ named.taskLog }}</div>
    </div>
  </div>

  <!-- 隧道列表 -->
  <div class="card flush">
    <div class="card-h">
      <Icon name="share" class="ni s16" style="color:var(--brand)" />
      <b class="grow">隧道</b>
      <span class="tag">{{ tunnels.length }}</span>
    </div>
    <div v-if="!tunnels.length" class="empty" style="padding:26px 0">
      <Icon name="globe" class="ni s24" style="color:var(--txt-3)" />
      <div>还没有隧道</div>
      <div class="dim" style="font-size:12px">先用上面的「临时地址」试一下，不需要任何账号</div>
    </div>
    <div v-else class="cf-list">
      <div v-for="t in tunnels" :key="t.id" class="cf-row">
        <span class="state" :class="stateClass(t.state)">
          <i class="dot" :class="{ off: !t.alive }"></i>{{ t.state_text }}
        </span>
        <div class="cf-row-main">
          <div class="cf-row-title">
            <b class="ell">{{ t.name }}</b>
            <span class="tag" :class="t.kind === 'quick' ? 'b' : 'g'">
              {{ t.kind === 'quick' ? '临时' : '固定' }}
            </span>
            <span class="dim" style="font-size:11.5px">端口 {{ t.port }}</span>
          </div>
          <a v-if="t.url" class="cf-row-url mono ell" :href="t.url" target="_blank" rel="noreferrer">{{ t.url }}</a>
          <div v-else class="dim" style="font-size:11.5px">
            {{ !t.alive ? '未运行，启动后生成地址' : (t.kind === 'named' ? '等待 cloudflared 注册…' : '还没拿到地址') }}
            <span v-if="t.error" style="color:var(--warn)"> · {{ t.error }}</span>
          </div>
        </div>
        <div class="cf-row-ops">
          <button class="btn xs" v-if="t.url" @click="copy(t.url)" title="复制地址"><Icon name="copy" /></button>
          <button class="btn xs" v-if="t.alive" @click="act(t, 'stop')"><Icon name="stop" />停止</button>
          <button class="btn xs" v-else @click="act(t, 'start')"><Icon name="play" />启动</button>
          <button class="btn xs" @click="showLog(t)"><Icon name="log" /></button>
          <button class="btn xs danger" @click="remove(t)"><Icon name="trash" /></button>
        </div>
      </div>
    </div>
  </div>

  <!-- 安装 -->
  <Modal v-if="ins.open" title="安装 cloudflared" sub="官方 Release · 自动选架构 · 可走镜像" icon-name="download"
         @close="ins.open = false">
    <div class="field" style="margin-bottom:10px">
      <label>下载源</label>
      <div class="cf-mirrors">
        <button class="btn xs" :class="{ on: ins.mirror === '' }" @click="ins.mirror = ''">
          自动选择
        </button>
        <button v-for="m in ins.mirrors" :key="m.id" class="btn xs"
                :class="{ on: ins.mirror === m.url }" @click="ins.mirror = m.url">
          {{ m.label }}<span v-if="probeMap[m.url]" class="cf-probe-dot"
                            :class="probeMap[m.url].ok ? 'ok' : 'bad'"></span>
        </button>
      </div>
      <div class="hint">
        自动模式会先并发探测各镜像，挑最快的一个下。同一个源在不同网络下差别很大，
        探测结果只代表此刻。
      </div>
      <button class="btn xs" style="margin-top:8px;align-self:flex-start" :disabled="ins.probing" @click="probe">
        <span v-if="ins.probing" class="spinner"></span><Icon v-else name="activity" />检测各源
      </button>
    </div>
    <div v-if="ins.task" class="cf-task">
      <div class="cf-task-bar"><i :style="{ width: (ins.task.progress || 0) + '%' }"></i></div>
      <pre class="cf-tasklog mono">{{ ins.task.output || '准备中…' }}</pre>
    </div>
    <template #footer>
      <button class="btn" @click="ins.open = false">{{ ins.task && ins.task.status === 'running' ? '后台运行' : '关闭' }}</button>
      <button class="btn primary" :disabled="ins.running" @click="doInstall">
        <span v-if="ins.running" class="spinner"></span>
        {{ ins.running ? '安装中…' : '开始安装' }}
      </button>
    </template>
  </Modal>

  <!-- 账号 -->
  <Modal v-if="acct.open" title="Cloudflare 账号" sub="只存在本机，接口不回显密钥" icon-name="key"
         @close="acct.open = false">
    <div class="cf-tabs">
      <button :class="{ on: acct.mode === 'token' }" @click="acct.mode = 'token'">API 令牌</button>
      <button :class="{ on: acct.mode === 'global' }" @click="acct.mode = 'global'">Global Key</button>
    </div>
    <div v-if="acct.mode === 'token'" class="field" style="margin-bottom:10px">
      <label>API 令牌</label>
      <input class="input" v-model.trim="acct.token" type="password" placeholder="粘贴 Cloudflare API Token" autocomplete="off" />
      <div class="hint">
        推荐。去 <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noreferrer">My Profile → API Tokens</a>
        建一个，权限勾 <b>Account · Cloudflare Tunnel · Edit</b> 和 <b>Zone · DNS · Edit</b>（Zone 选 All zones）。
      </div>
    </div>
    <template v-else>
      <div class="field" style="margin-bottom:10px">
        <label>账号邮箱</label>
        <input class="input" v-model.trim="acct.email" type="email" placeholder="you@example.com" autocomplete="off" />
      </div>
      <div class="field" style="margin-bottom:10px">
        <label>Global API Key</label>
        <input class="input" v-model.trim="acct.key" type="password" placeholder="在 My Profile → API Tokens 页底部" autocomplete="off" />
        <div class="hint" style="color:var(--warn)">
          Global Key 是整个账号的万能钥匙，权限比令牌大得多。能用令牌就用令牌。
        </div>
      </div>
    </template>

    <div v-if="acct.zones.length" class="cf-zones">
      <div class="cf-zones-h">已验证，可用域名：</div>
      <div v-for="z in acct.zones" :key="z.id" class="cf-zone-row">
        <span class="mono">{{ z.name }}</span>
        <span class="tag" :class="z.status === 'active' ? 'g' : 'y'">{{ z.status }}</span>
      </div>
    </div>
    <div v-if="acct.err" class="cf-err">{{ acct.err }}</div>
    <div v-if="acct.ok" class="cf-ok">{{ acct.ok }}</div>

    <template #footer>
      <button class="btn" @click="acct.open = false">关闭</button>
      <button class="btn" :disabled="acct.verifying" @click="verifyAcct">
        <span v-if="acct.verifying" class="spinner"></span>验证
      </button>
      <button class="btn primary" :disabled="acct.saving" @click="saveAcct">
        <span v-if="acct.saving" class="spinner"></span>保存
      </button>
    </template>
  </Modal>

  <!-- 日志 -->
  <Modal v-if="log.open" :title="log.name" sub="最近 8KB 输出" icon-name="log" size="wide" @close="log.open = false">
    <pre class="cf-tasklog mono" style="max-height:60vh">{{ log.text || '（空）' }}</pre>
    <template #footer>
      <button class="btn" @click="log.open = false">关闭</button>
      <button class="btn" @click="refreshLog"><Icon name="refresh" />刷新</button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

const cli = ref({ found: false, ready: false, missing: [], termux: false })
const tunnels = ref([])
const settings = ref({ has_creds: false, mode: '' })
const zones = ref([])
const zonesBusy = ref(false)
const panelPort = ref(8888)

const ZONE_KEY = 'pp-cf-zone'

const quick = reactive({ port: 8888, busy: false, url: '', err: '' })
const named = reactive({ sub: '', zone: '', host: '', port: 8888, busy: false, err: '', taskLog: '' })

const ins = reactive({ open: false, mirrors: [], mirror: '', task: null, running: false,
  taskId: '', probing: false, probeResults: [] })
const acct = reactive({ open: false, mode: 'token', token: '', email: '', key: '', account_id: '',
  zones: [], err: '', ok: '', verifying: false, saving: false })
const log = reactive({ open: false, id: '', name: '', text: '' })

let timer = 0

const shortVer = computed(() => {
  const v = cli.value.version || ''
  const m = v.match(/version\s+([\w.\-]+)/i)
  return m ? m[1] : (v || '未知')
})

const SUB_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/i

// 有域名列表时拼「子域名.域名」，没有列表就退回手填完整域名
const namedHost = computed(() => {
  if (zones.value.length) {
    const s = (named.sub || '').trim().replace(/^\.+|\.+$/g, '').toLowerCase()
    return s && named.zone ? `${s}.${named.zone}` : ''
  }
  return (named.host || '').trim()
    .replace(/^https?:\/\//i, '').replace(/\/.*$/, '').toLowerCase()
})

const namedBad = computed(() => {
  if (!zones.value.length) return ''
  const s = (named.sub || '').trim().replace(/^\.+|\.+$/g, '')
  if (!s) return ''
  if (!SUB_RE.test(s)) return '只能用小写字母、数字和连字符'
  return ''
})

function pickZone() {
  const names = zones.value.map(z => z.name)
  if (!names.length) { named.zone = ''; return }
  let keep = named.zone
  if (!keep) { try { keep = localStorage.getItem(ZONE_KEY) || '' } catch { keep = '' } }
  if (names.includes(keep)) { named.zone = keep; return }
  // 已建过固定隧道的域名优先，用户多半还用它
  const used = [...tunnels.value].reverse().find(t => t.kind === 'named' && names.includes(t.zone))
  if (used) { named.zone = used.zone; return }
  const act = zones.value.find(z => z.status === 'active') || zones.value[0]
  named.zone = act.name
}

function rememberZone() {
  try { if (named.zone) localStorage.setItem(ZONE_KEY, named.zone) } catch { /* 隐私模式下忽略 */ }
}

async function loadZones(manual = false) {
  if (!settings.value.has_creds) {
    if (manual) toast('先配 Cloudflare 账号', 'err')
    return
  }
  if (zonesBusy.value) return
  zonesBusy.value = true
  try {
    const j = await api('/api/cf/zones')
    zones.value = j.zones || []
    pickZone()
    if (manual) {
      if (zones.value.length) toast(`读到 ${zones.value.length} 个域名`, 'ok')
      else toast('这个账号下没有已接入的域名', 'err')
    }
  } catch (e) {
    if (manual) toast(e.message, 'err')
  } finally {
    zonesBusy.value = false
  }
}

const stateClass = (s) => ({
  connected: 'run', running: 'busy', starting: 'busy',
  error: 'warn', failed: 'fail', stopped: 'stop',
}[s] || 'stop')

function copy(t) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(() => toast('已复制', 'ok')).catch(() => prompt('手动复制', t))
    return
  }
  const el = document.createElement('textarea')
  el.value = t; document.body.appendChild(el); el.select()
  try { document.execCommand('copy'); toast('已复制', 'ok') } catch { prompt('手动复制', t) }
  document.body.removeChild(el)
}

async function load(spin = false) {
  try {
    const j = await api('/api/cf/status')
    cli.value = j.client || {}
    tunnels.value = j.tunnels || []
    settings.value = j.settings || {}
    panelPort.value = j.panel_port || 8888
    if (!quick.port || quick.port === 8888) quick.port = panelPort.value
    if (!named.port || named.port === 8888) named.port = panelPort.value
    // 后端缓存过域名列表就直接用，省一次 CF API 往返
    const zs = settings.value.zones || []
    if (zs.length && !zones.value.length) { zones.value = zs; pickZone() }
    // 临时隧道拿到地址后同步到卡片上
    const lastQuick = [...tunnels.value].reverse().find(t => t.kind === 'quick')
    if (lastQuick && lastQuick.url) quick.url = lastQuick.url
    else if (lastQuick && !lastQuick.alive) quick.url = ''
  } catch (e) { if (spin) toast(e.message, 'err') }
}

async function startQuick() {
  quick.busy = true; quick.err = ''; quick.url = ''
  try {
    const j = await api('/api/cf/quick', { method: 'POST', body: { port: quick.port } })
    const id = j.id
    // cloudflared 申请隧道一般 3–8 秒，轮询到拿到地址为止
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1200))
      const d = await api('/api/cf/tunnel/' + id)
      if (d.url) { quick.url = d.url; toast('临时地址已就绪', 'ok'); break }
      if (d.state === 'error' || d.state === 'failed') { quick.err = d.error || '隧道起不来'; break }
    }
    if (!quick.url && !quick.err) quick.err = '等了 24 秒还没拿到地址，点列表里的日志看看'
    await load()
  } catch (e) {
    quick.err = e.message
  } finally {
    quick.busy = false
  }
}

async function createNamed() {
  named.busy = true; named.err = ''; named.taskLog = ''
  try {
    const j = await api('/api/cf/named', {
      method: 'POST', body: { host: namedHost.value, port: named.port },
    })
    const taskId = j.id
    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 1500))
      const t = await api('/api/exec/' + taskId)
      named.taskLog = (t.output || '').split('\n').slice(-6).join('\n')
      if (t.status === 'success') {
        named.err = ''
        named.sub = ''; named.host = ''
        toast('固定域名隧道已创建', 'ok')
        break
      }
      if (t.status === 'failed' || t.status === 'canceled') {
        named.err = (t.output || '').split('\n').filter(l => l.startsWith('错误')).pop() || '创建失败'
        break
      }
    }
    await load()
  } catch (e) {
    named.err = e.message
  } finally {
    named.busy = false
  }
}

async function act(t, what) {
  try {
    await api(`/api/cf/tunnel/${t.id}/${what}`, { method: 'POST', body: {} })
    toast(what === 'stop' ? '已停止' : what === 'start' ? '已启动' : '已重启', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function remove(t) {
  const extra = t.kind === 'named' ? '\nCloudflare 侧的隧道和 DNS 记录也会一起删掉。' : ''
  if (!confirm(`删除「${t.name}」？${extra}`)) return
  try {
    const j = await api(`/api/cf/tunnel/${t.id}/delete`, { method: 'POST', body: { purge: true } })
    if (j.remote_errors && j.remote_errors.length) toast('本地已删，远端清理失败：' + j.remote_errors[0], 'warn')
    else toast('已删除', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function showLog(t) {
  log.open = true; log.id = t.id; log.name = t.name
  await refreshLog()
}

async function refreshLog() {
  try {
    const d = await api('/api/cf/tunnel/' + log.id)
    log.text = d.log || ''
  } catch (e) { log.text = '读取失败：' + e.message }
}

const probeMap = computed(() => Object.fromEntries(
  (ins.probeResults || []).map(r => [r.url, r])))

function openInstall() {
  ins.open = true; ins.task = null; ins.taskId = ''
  if (!ins.mirrors.length) {
    api('/api/cf/mirrors').then(j => {
      ins.mirrors = j.mirrors || []
    }).catch(() => { ins.mirrors = [{ id: 'direct', label: 'GitHub 直连', url: '' }] })
  }
  if (!ins.probeResults.length) probe()
}

async function probe() {
  ins.probing = true
  try {
    const j = await api('/api/cf/mirrors/probe', { method: 'POST', body: {} })
    ins.probeResults = j.results || []
    const ok = ins.probeResults.filter(r => r.ok).sort((a, b) => a.ms - b.ms)
    if (ok.length) toast(`可用源：${ok[0].label}（${ok[0].ms}ms）`, 'ok')
    else toast('没有源可用，检查网络', 'err')
  } catch (e) { toast(e.message, 'err') }
  finally { ins.probing = false }
}

async function doInstall() {
  ins.running = true
  ins.task = { status: 'running', output: '', progress: 0 }
  try {
    const j = await api('/api/cf/install', { method: 'POST', body: { mirror: ins.mirror } })
    ins.taskId = j.id
    for (let i = 0; i < 400; i++) {
      await new Promise(r => setTimeout(r, 1500))
      const t = await api('/api/exec/' + ins.taskId)
      ins.task = t
      if (t.status !== 'running') break
    }
    if (ins.task.status === 'success') {
      toast('安装完成', 'ok')
      await load()
    }
  } catch (e) {
    ins.task = { ...(ins.task || {}), status: 'failed', output: (ins.task?.output || '') + '\n' + e.message }
  } finally {
    ins.running = false
  }
}

async function uninstall() {
  if (!confirm('卸载 cloudflared？已建的隧道会全部停止。')) return
  try {
    await api('/api/cf/uninstall', { method: 'POST', body: {} })
    toast('已卸载', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function openAccount() {
  acct.open = true; acct.err = ''; acct.ok = ''
  try {
    const s = await api('/api/cf/settings')
    acct.mode = s.mode === 'global' ? 'global' : 'token'
    acct.token = s.token || ''; acct.email = s.email || ''; acct.key = s.key || ''
    acct.account_id = s.account_id || ''
    if (s.has_creds) await verifyAcct(true)
  } catch (e) { /* 首次没配，忽略 */ }
}

async function verifyAcct(silent = false) {
  acct.verifying = true; acct.err = ''; acct.ok = ''
  try {
    const body = acct.mode === 'token'
      ? { token: acct.token, account_id: acct.account_id }
      : { email: acct.email, key: acct.key, account_id: acct.account_id }
    const j = await api('/api/cf/verify', { method: 'POST', body })
    acct.zones = j.zones || []
    zones.value = acct.zones
    pickZone()
    acct.account_id = j.account_id || ''
    acct.ok = `验证通过：${(j.accounts[0] || {}).name || ''}，${acct.zones.length} 个域名`
    if (!silent) toast('验证通过', 'ok')
  } catch (e) {
    acct.err = e.message
    acct.zones = []
  } finally {
    acct.verifying = false
  }
}

async function saveAcct() {
  acct.saving = true; acct.err = ''
  try {
    const body = acct.mode === 'token'
      ? { token: acct.token, account_id: acct.account_id }
      : { email: acct.email, key: acct.key, account_id: acct.account_id }
    const s = await api('/api/cf/settings', { method: 'POST', body })
    settings.value = s
    // 换了凭证后端会把域名缓存清掉，这里跟着刷新下拉框
    zones.value = s.zones || []
    pickZone()
    toast('已保存', 'ok')
    acct.open = false
    if (settings.value.has_creds && !zones.value.length) loadZones()
  } catch (e) { acct.err = e.message }
  finally { acct.saving = false }
}

onMounted(() => {
  load(true).then(() => {
    // 列表里没有缓存、但凭证已经配好了 → 后台补拉一次，用户不用点账号弹窗
    if (settings.value.has_creds && !zones.value.length) loadZones()
  })
  // 有隧道在跑时保持状态新鲜（cloudflared 重连会换地址）
  timer = setInterval(() => {
    if (tunnels.value.some(t => t.alive)) load()
  }, 4000)
})
onUnmounted(() => clearInterval(timer))
</script>
