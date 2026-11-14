<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="share" />ME Frp 托管</h2>
      <p>幻缘映射 · 隧道建在平台上 · 本机用官方客户端接管</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load(true)"><Icon name="refresh" />刷新</button>
      <button v-if="acc.logged_in" class="btn" @click="signOut"><Icon name="logout" />退出账号</button>
    </div>
  </div>

  <!-- 登录 -->
  <div v-if="!acc.logged_in" class="card" style="max-width:560px">
    <div class="card-h">
      <Icon name="key" class="ni s16" style="color:var(--brand)" />
      <b class="grow">登录 ME Frp</b>
      <span class="tag">用户名密码</span>
    </div>
    <div class="card-b" style="padding:14px">
      <div class="field">
        <label>用户名 / 邮箱</label>
        <input class="input" v-model.trim="form.username" autocomplete="username"
               placeholder="sanhe" @keyup.enter="doLogin" />
      </div>
      <div class="field">
        <label>密码</label>
        <input class="input" v-model="form.password" type="password" autocomplete="current-password"
               placeholder="••••••••" @keyup.enter="doLogin" />
      </div>
      <div class="mf-note">
        面板会直接和 mefrp 的接口通信，人机验证（sha256 PoW）在本地算完，
        不需要打开浏览器。账号密码只在这次登录时用一次，落盘的是登录令牌。
      </div>
      <div v-if="form.err" class="mf-err">{{ form.err }}</div>
      <button class="btn primary block" style="margin-top:12px" :disabled="form.busy" @click="doLogin">
        <span v-if="form.busy" class="spinner"></span><Icon v-else name="login" />
        {{ form.busy ? '正在登录（含人机验证）…' : '登录' }}
      </button>
    </div>
  </div>

  <template v-else>
    <!-- 客户端 -->
    <div class="card flush" style="margin-bottom:14px">
      <div class="card-h">
        <Icon name="package" class="ni s16" style="color:var(--brand)" />
        <b class="grow">mefrpc 官方客户端</b>
        <span class="tag" :class="cli.found ? 'g' : 'y'">{{ cli.found ? '就绪' : '未找到' }}</span>
      </div>
      <div class="card-b" style="padding:10px 14px">
        <div v-if="cli.found">
          <div class="mf-kv"><span>版本</span><b class="mono">{{ cli.version || '未知' }}</b></div>
          <div class="mf-kv"><span>路径</span><b class="mono ell" :title="cli.path">{{ cli.path }}</b></div>
        </div>
        <div v-else class="dim" style="font-size:12px;line-height:1.75">
          没找到 mefrpc。点「一键安装」会扫描手机下载目录里的官方安装包
          （<span class="mono">mefrpc_*.tar</span>）并装到面板数据目录；
          也可以手动指定已有二进制的路径。
        </div>
      </div>
      <div class="card-f" style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
        <button class="btn xs" @click="pickPath"><Icon name="folder" />指定路径</button>
        <button class="btn xs primary" :disabled="inst.busy" @click="installClient">
          <span v-if="inst.busy" class="spinner"></span><Icon v-else name="download" />
          {{ cli.found ? '重装 / 升级' : '一键安装' }}
        </button>
      </div>
    </div>

    <!-- 账号概览 -->
    <div class="mf-stats" v-if="info">
      <div class="card mf-stat">
        <span class="mf-stat-l">分组</span>
        <b>{{ info.group_name || info.group || '—' }}</b>
        <span class="dim">{{ info.realname ? '已实名' : '未实名' }}</span>
      </div>
      <div class="card mf-stat">
        <span class="mf-stat-l">隧道</span>
        <b>{{ info.used_proxies ?? proxies.length }} / {{ info.max_proxies ?? '—' }}</b>
        <span class="dim">已用 / 上限</span>
      </div>
      <div class="card mf-stat">
        <span class="mf-stat-l">本月流量</span>
        <b>{{ kb(info.traffic) }}</b>
        <span class="dim">出/入上限 {{ kb(info.out_bound) }} · {{ kb(info.in_bound) }}</span>
      </div>
      <div class="card mf-stat">
        <span class="mf-stat-l">客户端令牌</span>
        <b class="mono">{{ info.frp_token_masked || '—' }}</b>
        <span class="dim">连节点用，不是登录令牌</span>
      </div>
    </div>

    <!-- 隧道列表 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="list" class="ni s16" style="color:var(--brand)" />
        <b class="grow">隧道（{{ proxies.length }}）</b>
        <button class="btn xs primary" @click="openCreate"><Icon name="plus" />新建</button>
      </div>

      <div v-if="!proxies.length" class="card-b">
        <div class="dim" style="font-size:13px;padding:18px 0;text-align:center">
          平台上还没有隧道。点「新建」建一条，回来点「启动」就由这台手机接管。
        </div>
      </div>

      <div v-for="p in proxies" :key="p.id" class="mf-row">
        <div class="mf-row-main">
          <div class="mf-row-t">
            <span class="tag" :class="typeTone(p.type)">{{ p.type.toUpperCase() }}</span>
            <b class="ell">{{ p.name }}</b>
            <span class="tag" :class="p.online ? 'g' : ''">{{ p.online ? '平台在线' : '平台离线' }}</span>
            <span class="tag" :class="localTone(p)">{{ localText(p) }}</span>
            <span class="tag y" v-if="p.disabled">已禁用</span>
            <span class="tag r" v-if="p.banned" :title="p.ban_reason">已封禁</span>
          </div>
          <div class="mf-row-sub mono ell">
            {{ p.local_ip }}:{{ p.local_port }}
            <Icon name="chevronRight" class="ni s12" />
            {{ p.address || p.node_name }}
            <span class="dim"> · {{ p.node_name }}</span>
          </div>
          <div class="mf-row-sub dim" v-if="p.last_error">上次错误：{{ p.last_error }}</div>
        </div>
        <div class="mf-row-ops">
          <button v-if="p.local_state !== 'running'" class="btn xs primary" :disabled="busy[p.id]"
                  @click="act(p, 'start')"><Icon name="play" />启动</button>
          <button v-else class="btn xs" :disabled="busy[p.id]" @click="act(p, 'stop')">
            <Icon name="stop" />停止</button>
          <button class="btn xs" @click="openLog(p)"><Icon name="log" />日志</button>
          <button class="btn xs" @click="openMore(p)"><Icon name="more" />更多</button>
        </div>
      </div>
    </div>
  </template>

  <!-- 新建 / 编辑 -->
  <Modal v-if="ed.open" @close="ed.open = false" :title="ed.id ? '编辑隧道' : '新建隧道'">
    <div class="field">
      <label>隧道名</label>
      <input class="input" v-model.trim="ed.name" placeholder="只给人类看，别用特殊符号" />
    </div>
    <div class="field">
      <label>类型</label>
      <div class="mf-chips">
        <button v-for="t in TYPES" :key="t" class="chip" :class="{ on: ed.type === t }"
                :disabled="!!ed.id" @click="pickType(t)">{{ t.toUpperCase() }}</button>
      </div>
    </div>
    <div class="field" v-if="!ed.id">
      <label>节点</label>
      <select class="input" v-model.number="ed.node_id">
        <option v-for="n in usableNodes" :key="n.id" :value="n.id">
          {{ n.name }}{{ n.online ? '' : '（离线）' }} · {{ n.bandwidth }} · 端口 {{ n.allow_port }}
        </option>
      </select>
    </div>
    <div class="mf-grid2">
      <div class="field">
        <label>本地 IP</label>
        <input class="input mono" v-model.trim="ed.local_ip" placeholder="127.0.0.1" />
      </div>
      <div class="field">
        <label>本地端口</label>
        <input class="input mono" v-model.number="ed.local_port" type="number" min="1" max="65535" />
      </div>
    </div>
    <div class="mf-grid2" v-if="ed.type === 'tcp' || ed.type === 'udp'">
      <div class="field">
        <label>远程端口</label>
        <div class="mf-inline">
          <input class="input mono" v-model.number="ed.remote_port" type="number" min="1" max="65535"
                 placeholder="留空由平台分配" />
          <button class="btn xs" :disabled="ed.gettingPort || !ed.node_id" @click="allocPort">
            <span v-if="ed.gettingPort" class="spinner"></span><Icon v-else name="refresh" />取空闲
          </button>
        </div>
      </div>
    </div>
    <div class="field" v-else>
      <label>域名</label>
      <input class="input mono" v-model.trim="ed.domain" placeholder="免费节点给子域名，如 abc.node.mefrp.com" />
      <div class="mf-note" v-if="ed.type === 'https'">HTTPS 隧道还要证书与私钥，先建好隧道再到「更多 → 配置」里补。</div>
    </div>
    <div class="mf-checks">
      <label class="mf-check"><input type="checkbox" v-model="ed.encrypt" />传输加密</label>
      <label class="mf-check"><input type="checkbox" v-model="ed.compress" />压缩</label>
    </div>
    <div v-if="ed.err" class="mf-err">{{ ed.err }}</div>
    <template #footer>
      <button class="btn" @click="ed.open = false">取消</button>
      <button class="btn primary" :disabled="ed.busy" @click="saveProxy">
        <span v-if="ed.busy" class="spinner"></span>{{ ed.id ? '保存' : '创建' }}
      </button>
    </template>
  </Modal>

  <!-- 更多 -->
  <Modal v-if="more.open" @close="more.open = false" :title="more.p ? more.p.name : ''">
    <div class="mf-menu" v-if="more.p">
      <button class="mf-menu-i" @click="toConfig('toml')"><Icon name="file" />生成配置（TOML）</button>
      <button class="mf-menu-i" @click="toConfig('ini')"><Icon name="file" />生成配置（INI）</button>
      <button class="mf-menu-i" @click="askEdit"><Icon name="edit" />编辑</button>
      <button class="mf-menu-i" @click="doToggle">
        <Icon name="power" />{{ more.p.disabled ? '启用隧道' : '禁用隧道' }}
      </button>
      <button class="mf-menu-i" @click="doKick"><Icon name="link" />强制下线</button>
      <button class="mf-menu-i" @click="doRestart"><Icon name="refresh" />重启本机客户端</button>
      <button class="mf-menu-i danger" @click="doDelete"><Icon name="trash" />删除隧道</button>
    </div>
  </Modal>

  <!-- 配置 -->
  <Modal v-if="cfg.open" @close="cfg.open = false" :title="'启动配置 · ' + cfg.fmt.toUpperCase()">
    <div class="mf-note">官方客户端快捷启动（推荐，和网页面板的「启动参数」一致）：</div>
    <pre class="mf-pre">{{ cfg.command }}</pre>
    <div class="mf-note" style="margin-top:10px">配置文件方式（任意 frpc 0.52+ 都能用）：</div>
    <pre class="mf-pre mf-pre-lg">{{ cfg.text || '加载中…' }}</pre>
    <template #footer>
      <button class="btn" @click="copy(cfg.command)"><Icon name="copy" />复制命令</button>
      <button class="btn primary" @click="copy(cfg.text)"><Icon name="copy" />复制配置</button>
    </template>
  </Modal>

  <!-- 日志 -->
  <Modal v-if="log.open" @close="log.open = false" :title="'客户端日志 · ' + log.name">
    <pre class="mf-pre mf-pre-lg">{{ log.text || '（还没有输出）' }}</pre>
    <template #footer>
      <button class="btn" @click="loadLog"><Icon name="refresh" />刷新</button>
      <button class="btn primary" @click="log.open = false">关闭</button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, fmtB } from '../api'

// MEFrp 的流量字段单位是 KB（不是字节），直接 fmtB 会显示成 KB 级数字
const kb = (n) => fmtB((Number(n) || 0) * 1024)
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

const TYPES = ['tcp', 'udp', 'http', 'https']

const acc = ref({ logged_in: false, username: '', group: '' })
const cli = ref({ found: false, path: '', version: '' })
const info = ref(null)
const proxies = ref([])
const state = ref({})
const busy = reactive({})
const form = reactive({ username: '', password: '', busy: false, err: '' })
const inst = reactive({ busy: false })
const ed = reactive({ open: false, id: 0, name: '', type: 'tcp', node_id: 0, local_ip: '127.0.0.1',
  local_port: 8080, remote_port: 0, domain: '', encrypt: false, compress: false,
  busy: false, err: '', gettingPort: false })
const more = reactive({ open: false, p: null })
const cfg = reactive({ open: false, fmt: 'toml', text: '', command: '', id: 0 })
const log = reactive({ open: false, id: 0, name: '', text: '' })

let timer = 0

const myGroup = computed(() => (info.value && info.value.group) || '')

// 节点列表里带着「谁能用」的组限制（admin;sponsor;vip;default…）。不过滤的话
// 普通账号会选到管理员的节点，提交后由平台报错，白填一遍表单。
const usableNodes = computed(() => {
  const nodes = (info.value && info.value.nodes) || []
  const t = ed.type
  const g = myGroup.value
  return nodes.filter(n => {
    if (n.disabled || !n.online) return false
    if (!n.allow_type.includes(t)) return false
    const allow = n.allow_group || []
    if (!allow.length) return true
    return !g || allow.includes(g)
  })
})

function typeTone(t) {
  return { tcp: '', udp: '', http: 'g', https: 'g' }[t] || ''
}
function localTone(p) {
  if (p.local_state === 'running') return 'g'
  if (p.want) return 'y'
  return ''
}
function localText(p) {
  if (p.local_state === 'running') return '本机运行中'
  if (p.want) return '等待拉起'
  return '本机未运行'
}

async function load(spin) {
  try {
    const s = await api('/api/mefrp/status')
    acc.value = s.account
    cli.value = s.client
    state.value = s.state || {}
    if (s.account.logged_in) {
      const [ov, px] = await Promise.all([api('/api/mefrp/overview'), api('/api/mefrp/proxies')])
      info.value = ov
      proxies.value = px.proxies || []
      state.value = px.state || {}
    } else {
      info.value = null
      proxies.value = []
    }
  } catch (e) {
    if (spin) toast(e.message, 'err')
  }
}

async function doLogin() {
  form.err = ''
  if (!form.username || !form.password) { form.err = '用户名和密码都要填'; return }
  form.busy = true
  try {
    const r = await api('/api/mefrp/login', { method: 'POST',
      body: { username: form.username, password: form.password } })
    acc.value = r.account
    cli.value = r.client
    form.password = ''
    toast('已登录 ME Frp', 'ok')
    await load()
  } catch (e) {
    form.err = e.message
  } finally {
    form.busy = false
  }
}

async function signOut() {
  try { await api('/api/mefrp/logout', { method: 'POST' }); toast('已退出', 'ok') }
  catch (e) { toast(e.message, 'err') }
  acc.value = { logged_in: false, username: '', group: '' }
  info.value = null; proxies.value = []
}

async function installClient() {
  inst.busy = true
  try {
    const r = await api('/api/mefrp/client/install', { method: 'POST', body: {} })
    toast(`已安装 ${r.version || 'mefrpc'}`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
  finally { inst.busy = false }
}

async function pickPath() {
  const p = prompt('mefrpc 可执行文件路径（留空恢复自动查找）', cli.value.path || '')
  if (p === null) return
  try {
    const r = await api('/api/mefrp/client/path', { method: 'POST', body: { path: p.trim() } })
    cli.value = r.client
    toast(r.client.found ? '已设置' : '这个路径没找到可执行文件', r.client.found ? 'ok' : 'err')
  } catch (e) { toast(e.message, 'err') }
}

function openCreate() {
  Object.assign(ed, { open: true, id: 0, name: '', type: 'tcp', local_ip: '127.0.0.1',
    local_port: 8080, remote_port: 0, domain: '', encrypt: false, compress: false,
    busy: false, err: '' })
  const n = usableNodes.value[0]
  ed.node_id = n ? n.id : 0
}

function pickType(t) {
  ed.type = t
  ed.err = ''
  if (!ed.id) {
    const ok = usableNodes.value.some(n => n.id === ed.node_id)
    if (!ok) ed.node_id = usableNodes.value.length ? usableNodes.value[0].id : 0
    if (t === 'http' || t === 'https') ed.remote_port = 0
  }
}

async function allocPort() {
  ed.gettingPort = true
  try {
    const r = await api('/api/mefrp/freeport', { method: 'POST',
      body: { node_id: ed.node_id, protocol: ed.type } })
    ed.remote_port = r.port
    toast(`分到端口 ${r.port}`, 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { ed.gettingPort = false }
}

async function saveProxy() {
  ed.err = ''
  ed.busy = true
  try {
    const body = { name: ed.name, type: ed.type, node_id: ed.node_id, local_ip: ed.local_ip,
      local_port: ed.local_port, remote_port: ed.remote_port, domain: ed.domain,
      encrypt: ed.encrypt, compress: ed.compress }
    if (ed.id) await api('/api/mefrp/proxy/' + ed.id, { method: 'POST', body })
    else await api('/api/mefrp/proxy', { method: 'POST', body })
    toast(ed.id ? '已保存' : '隧道已创建', 'ok')
    ed.open = false
    await load()
  } catch (e) { ed.err = e.message }
  finally { ed.busy = false }
}

async function act(p, what) {
  busy[p.id] = true
  try {
    await api(`/api/mefrp/proxy/${p.id}/${what}`, { method: 'POST' })
    toast(what === 'stop' ? '已停止' : what === 'start' ? '已启动' : '已重启', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
  finally { busy[p.id] = false }
}

function openMore(p) { more.p = p; more.open = true }

async function toConfig(fmt) {
  const p = more.p
  more.open = false
  cfg.open = true; cfg.fmt = fmt; cfg.id = p.id; cfg.text = ''; cfg.command = ''
  try {
    const r = await api(`/api/mefrp/proxy/${p.id}/config?format=${fmt}`)
    cfg.text = r.config
    cfg.command = r.command
  } catch (e) { cfg.text = ''; toast(e.message, 'err') }
}

function askEdit() {
  const p = more.p
  more.open = false
  Object.assign(ed, { open: true, id: p.id, name: p.name, type: p.type, node_id: p.node_id,
    local_ip: p.local_ip, local_port: p.local_port, remote_port: p.remote_port || 0,
    domain: p.domain || '', encrypt: p.encrypt, compress: p.compress, busy: false, err: '' })
}

async function doToggle() {
  const p = more.p
  more.open = false
  try {
    await api(`/api/mefrp/proxy/${p.id}/toggle`, { method: 'POST', body: { disabled: !p.disabled } })
    toast(p.disabled ? '已启用' : '已禁用', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function doKick() {
  const p = more.p
  more.open = false
  try {
    await api(`/api/mefrp/proxy/${p.id}/kick`, { method: 'POST' })
    toast('已请求平台强制下线', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

async function doRestart() {
  const p = more.p
  more.open = false
  await act(p, 'restart')
}

async function doDelete() {
  const p = more.p
  more.open = false
  if (!confirm(`删除隧道「${p.name}」？本机客户端会一起停掉。`)) return
  try {
    await api(`/api/mefrp/proxy/${p.id}/delete`, { method: 'POST' })
    toast('已删除', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

function openLog(p) {
  log.open = true; log.id = p.id; log.name = p.name; log.text = ''
  loadLog()
}

async function loadLog() {
  try {
    const r = await api(`/api/mefrp/proxy/${log.id}/log?lines=300`)
    log.text = r.text
  } catch (e) { log.text = '读取失败：' + e.message }
}

function copy(t) {
  if (!t) return
  navigator.clipboard.writeText(t).then(() => toast('已复制', 'ok')).catch(() => prompt('手动复制', t))
}

onMounted(() => {
  load(true)
  timer = setInterval(() => {
    if (!acc.value.logged_in) return
    if (proxies.value.some(p => p.local_state === 'running' || p.want)) load()
  }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.mf-note {
  font-size: 12px; line-height: 1.7; color: var(--dim);
  background: var(--bg2, rgba(127, 127, 127, .06));
  border-radius: 8px; padding: 9px 11px; margin: 8px 0;
}
.mf-err {
  font-size: 12.5px; color: var(--danger, #d03050); margin-top: 8px;
  background: rgba(208, 48, 80, .08); border-radius: 8px; padding: 8px 10px;
}
.mf-kv { display: flex; gap: 10px; font-size: 12.5px; padding: 3px 0; }
.mf-kv > span { color: var(--dim); min-width: 56px; }
.mf-kv > b { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mf-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin-bottom: 14px;
}
.mf-stat { padding: 12px 14px; display: flex; flex-direction: column; gap: 3px; }
.mf-stat-l { font-size: 11.5px; color: var(--dim); }
.mf-stat b { font-size: 15px; }
.mf-stat .dim { font-size: 11px; }
.mf-row {
  display: flex; gap: 10px; align-items: center; padding: 11px 14px;
  border-top: 1px solid var(--line, rgba(127, 127, 127, .14));
}
.mf-row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.mf-row-t { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.mf-row-t b { font-size: 13.5px; max-width: 46vw; }
.mf-row-sub { font-size: 11.5px; color: var(--dim); }
.mf-row-ops { display: flex; gap: 6px; flex-shrink: 0; }
.mf-row-ops .btn { padding: 4px 8px; font-size: 12px; }
.mf-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip {
  padding: 5px 12px; border-radius: 999px; font-size: 12.5px; cursor: pointer;
  border: 1px solid var(--line, rgba(127, 127, 127, .25)); background: transparent;
  color: var(--fg);
}
.chip.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.mf-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.mf-inline { display: flex; gap: 6px; }
.mf-inline .input { flex: 1; min-width: 0; }
.mf-checks { display: flex; gap: 14px; margin: 6px 0 2px; }
.mf-check { display: flex; gap: 6px; align-items: center; font-size: 12.5px; }
.mf-menu { display: flex; flex-direction: column; gap: 2px; }
.mf-menu-i {
  display: flex; gap: 10px; align-items: center; padding: 11px 6px; font-size: 13.5px;
  background: none; border: none; border-bottom: 1px solid var(--line, rgba(127, 127, 127, .12));
  color: var(--fg); cursor: pointer; text-align: left;
}
.mf-menu-i.danger { color: var(--danger, #d03050); }
.mf-pre {
  font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; line-height: 1.6;
  background: var(--bg2, rgba(127, 127, 127, .08)); border-radius: 8px; padding: 10px;
  overflow: auto; max-height: 340px; white-space: pre; margin: 6px 0;
}
.mf-pre-lg { max-height: 46vh; }
@media (max-width: 560px) {
  .mf-row { flex-direction: column; align-items: stretch; }
  .mf-row-ops { justify-content: flex-end; }
  .mf-grid2 { grid-template-columns: 1fr; }
}
</style>
