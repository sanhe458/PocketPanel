<template>
  <div class="term-page" :class="{ full: fullscreen }">
    <div class="term-tabs">
      <div class="tabs-scroll" ref="tabsEl">
        <div v-for="s in sessions" :key="s.id" class="ttab"
             :class="{ on: s.id === activeId, dead: !s.alive }" @click="select(s.id)">
          <span class="k" v-if="s.kind === 'ssh'">SSH</span>
          <span class="nm">{{ s.title }}</span>
          <button class="x" @click.stop="closeTab(s)" title="关闭"><Icon name="close" class="ni s12" /></button>
        </div>
        <button class="tnew" @click="openNew" title="新建会话"><Icon name="plus" class="ni s14" /></button>
      </div>
      <button class="tfull" @click="fullscreen = !fullscreen" :title="fullscreen ? '退出全屏' : '全屏'">
        <Icon name="maximize" class="ni s14" />
      </button>
    </div>

    <div class="term-stage" ref="stageEl">
      <div v-if="!sessions.length" class="term-empty">
        <div class="te-logo">&gt;_</div>
        <h3>真终端，PTY 直连</h3>
        <p>bash / sh 交互式会话，支持 top、vim、htop 这类全屏程序和 Ctrl+C。
           SSH 走系统 OpenSSH，密钥与 known_hosts 都能复用。</p>
        <div class="te-grid">
          <button class="te-card" @click="quickLocal('bash')">
            <b>bash</b><span>登录 shell，读 .bashrc</span>
          </button>
          <button class="te-card" @click="quickLocal('sh')">
            <b>sh</b><span>dash，最省内存</span>
          </button>
          <button class="te-card" @click="openSsh">
            <b>SSH</b><span>连接远程主机</span>
          </button>
        </div>
      </div>
      <TermView v-for="s in sessions" :key="s.id" :ref="el => setRef(s.id, el)"
                :id="s.id" :active="s.id === activeId" :theme="ui.theme"
                @exit="onExit(s.id, $event)" @paste="openPaste"
                @transport="onTransport(s.id, $event)" />
    </div>
    <div class="term-status">
      <span class="dot" :class="{ fail: !activeSess || !activeSess.alive }"></span>
      <span>{{ statusText }}</span>
      <span class="spacer"></span>
      <span v-if="metrics" class="dim">{{ metrics }}</span>
      <span v-if="activeSess && activeSess.transport" class="xfer"
            :class="activeSess.transport"
            :title="activeSess.transport === 'ws' ? 'WebSocket 实时通道' : 'HTTP 长轮询（降级）'">
        {{ activeSess.transport === 'ws' ? 'WS' : '轮询' }}
      </span>
    </div>
  </div>

  <Panel v-if="newOpen" title="新建会话" sub="本地 Shell 或 SSH 远程登录" icon-name="terminal" @close="newOpen = false">
    <div class="seg" style="margin-bottom:14px">
      <button :class="{ on: mode === 'local' }" @click="mode = 'local'">本地 Shell</button>
      <button :class="{ on: mode === 'ssh' }" @click="mode = 'ssh'">SSH 远程</button>
    </div>

    <template v-if="mode === 'local'">
      <div class="field">
        <label>Shell</label>
        <div class="seg" style="width:100%">
          <button style="flex:1" :class="{ on: shell === 'bash' }" @click="shell = 'bash'">bash</button>
          <button style="flex:1" :class="{ on: shell === 'sh' }" @click="shell = 'sh'">sh (dash)</button>
        </div>
      </div>
      <div class="field">
        <label>或直接运行命令（可选）</label>
        <input class="input" v-model="cmd" placeholder="如 htop、python、top -d 1"
               autocomplete="off" autocapitalize="off" spellcheck="false">
      </div>
      <p class="hint" style="margin:0 0 12px">
        {{ shell === 'bash' ? 'bash -l：加载 /etc/profile 与 ~/.bashrc，Termux 默认体验。'
                            : 'sh -l：dash，启动更快、内存占用更低，但没有 Tab 补全增强和数组。' }}
      </p>
    </template>

    <template v-else>
      <div v-if="hosts.length" class="field">
        <label>已保存的主机</label>
        <div class="hlist">
          <div v-for="h in hosts" :key="h.id" class="hitem" @click="pickHost(h)">
            <b>{{ h.name }}</b>
            <span>{{ h.user }}@{{ h.host }}:{{ h.port }} · {{ h.auth === 'key' ? '密钥' : '密码' }}</span>
            <button class="hx" @click.stop="delHost(h)" title="删除"><Icon name="close" class="ni s12" /></button>
          </div>
        </div>
      </div>
      <div class="field">
        <label>名称（可选）</label>
        <input class="input" v-model="form.name" placeholder="我的服务器" autocomplete="off">
      </div>
      <div class="row" style="gap:10px;align-items:flex-start">
        <div class="field" style="flex:1;min-width:0">
          <label>主机</label>
          <input class="input" v-model="form.host" placeholder="192.168.1.10 或域名"
                 autocomplete="off" autocapitalize="off" spellcheck="false">
        </div>
        <div class="field" style="width:84px;flex:0 0 84px">
          <label>端口</label>
          <input class="input" v-model="form.port" inputmode="numeric" placeholder="22">
        </div>
      </div>
      <div class="field">
        <label>用户名</label>
        <input class="input" v-model="form.user" placeholder="root"
               autocomplete="off" autocapitalize="off" spellcheck="false">
      </div>
      <div class="field">
        <label>认证方式</label>
        <div class="seg" style="width:100%">
          <button style="flex:1" :class="{ on: form.auth === 'password' }"
                  @click="form.auth = 'password'">密码</button>
          <button style="flex:1" :class="{ on: form.auth === 'key' }"
                  @click="form.auth = 'key'">密钥</button>
        </div>
      </div>
      <div class="field" v-if="form.auth === 'password'">
        <label>密码</label>
        <input class="input" type="password" v-model="form.password" placeholder="仅本次连接使用，不落盘"
               autocomplete="current-password">
      </div>
      <template v-else>
        <div class="field">
          <label>私钥</label>
          <select class="input" v-model="form.key">
            <option value="">选择 ~/.ssh 下的私钥</option>
            <option v-for="k in keys" :key="k.path" :value="k.path">{{ k.name }}</option>
          </select>
        </div>
        <button class="btn sm ghost" style="margin-bottom:12px" @click="importOpen = !importOpen">
          {{ importOpen ? '收起导入' : '粘贴导入私钥' }}
        </button>
        <div v-if="importOpen" class="field">
          <textarea class="input" rows="5" v-model="keyText"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>
          <input class="input" v-model="keyName" placeholder="保存为 id_imported" style="margin-top:6px">
          <button class="btn sm" style="margin-top:6px" @click="doImport"><Icon name="key" class="ni s12" />导入到 ~/.ssh</button>
        </div>
      </template>
      <label class="chkline">
        <input type="checkbox" v-model="form.save">
        <span>保存到主机簿（不含密码）</span>
      </label>
      <label class="chkline">
        <input type="checkbox" v-model="form.strict">
        <span>严格校验主机指纹（关闭则首次自动记录）</span>
      </label>
    </template>

    <div class="err" style="margin-top:6px">{{ err }}</div>
    <template #footer>
      <button class="btn" @click="newOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="create">
        <span v-if="busy" class="spinner"></span>{{ busy ? '连接中…' : '创建会话' }}</button>
    </template>
  </Panel>

  <Panel v-if="pasteOpen" title="粘贴到终端" sub="浏览器 http 下不开放剪贴板读取权限" icon-name="paste" @close="closePaste">
    <div class="field">
      <label>内容</label>
      <textarea class="input" ref="pasteEl" rows="6" v-model="pasteText"
                placeholder="长按输入框选择粘贴，或直接用系统输入法输入"></textarea>
    </div>
    <p class="hint" style="margin:0">
      浏览器在 http 下不开放剪贴板读取权限，所以粘贴走这个中转框。发送后会自动回车。
    </p>
    <template #footer>
      <button class="btn" @click="closePaste">取消</button>
      <button class="btn primary" @click="doPaste"><Icon name="send" class="ni s12" />发送</button>
    </template>
  </Panel>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { api } from '../api'
import { ui, toast } from '../store'
import TermView from '../components/TermView.vue'
import Panel from '../components/Panel.vue'
import Icon from '../components/Icon.vue'

const sessions = ref([])
const activeId = ref('')
const newOpen = ref(false)
const pasteOpen = ref(false)
const fullscreen = ref(false)
const importOpen = ref(false)
const mode = ref('local')
const shell = ref('bash')
const cmd = ref('')
const busy = ref(false)
const err = ref('')
const hosts = ref([])
const keys = ref([])
const keyText = ref('')
const keyName = ref('')
const pasteText = ref('')
const pasteEl = ref(null)
const tabsEl = ref(null)
const stageEl = ref(null)
const refs = {}

const form = reactive({
  name: '', host: '', port: 22, user: 'root',
  auth: 'password', password: '', key: '', strict: false, save: true
})

const activeSess = computed(() => sessions.value.find(s => s.id === activeId.value))

const statusText = computed(() => {
  const s = activeSess.value
  if (!s) return '未连接'
  if (!s.alive) return `${s.title} · 已退出${s.exit == null ? '' : '（' + s.exit + '）'}`
  if (s.kind === 'ssh') return `${s.subtitle} · ${s.cols}×${s.rows}`
  return `${s.title} · 本地 · ${s.cols}×${s.rows}`
})

const metrics = computed(() => {
  const s = activeSess.value
  if (!s) return ''
  return s.bytes ? (s.bytes > 1024 ? (s.bytes / 1024).toFixed(1) + ' KB' : s.bytes + ' B') : ''
})

function setRef(id, el) { if (el) refs[id] = el; else delete refs[id] }

function select(id) {
  activeId.value = id
  nextTick(() => {
    const v = refs[id]
    if (v) v.focus()
  })
}

function guessSize() {
  const el = stageEl.value
  const w = (el && el.clientWidth) || window.innerWidth
  const h = (el && el.clientHeight) || 400
  return {
    cols: Math.max(40, Math.min(400, Math.floor((w - 16) / 7.6))),
    rows: Math.max(10, Math.min(200, Math.floor((h - 34) / 15)))
  }
}

async function load() {
  try {
    const d = await api('/api/term/list')
    sessions.value = d.sessions || []
    if (sessions.value.length) {
      const alive = sessions.value.filter(s => s.alive)
      activeId.value = (alive[0] || sessions.value[0]).id
    }
  } catch { /* 未登录时由全局事件处理 */ }
}

async function create() {
  err.value = ''
  busy.value = true
  try {
    const { cols, rows } = guessSize()
    let body
    if (mode.value === 'local') {
      body = { kind: 'local', shell: shell.value, cmd: cmd.value.trim(), cols, rows }
    } else {
      if (!form.host.trim()) throw new Error('请填写主机')
      if (!form.user.trim()) throw new Error('请填写用户名')
      if (form.auth === 'password' && !form.password) throw new Error('请填写密码')
      if (form.auth === 'key' && !form.key) throw new Error('请选择私钥')
      body = {
        kind: 'ssh', host: form.host.trim(), port: Number(form.port) || 22,
        user: form.user.trim(), auth: form.auth, password: form.password,
        key: form.key, name: form.name.trim(), strict: form.strict, cols, rows
      }
    }
    const s = await api('/api/term/new', { method: 'POST', body })
    sessions.value.push(s)
    activeId.value = s.id
    newOpen.value = false
    if (mode.value === 'ssh' && form.save) {
      try {
        const saved = await api('/api/ssh/hosts', {
          method: 'POST',
          body: {
            name: form.name.trim(), host: form.host.trim(), port: Number(form.port) || 22,
            user: form.user.trim(), auth: form.auth, key: form.key, strict: form.strict
          }
        })
        const i = hosts.value.findIndex(h => h.id === saved.id)
        if (i >= 0) hosts.value[i] = saved; else hosts.value.push(saved)
      } catch { /* 主机簿保存失败不影响连接 */ }
    }
    resetForm()
    nextTick(() => {
      if (tabsEl.value) tabsEl.value.scrollLeft = tabsEl.value.scrollWidth
    })
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

function resetForm() {
  form.name = ''; form.host = ''; form.port = 22; form.user = 'root'
  form.auth = 'password'; form.password = ''; form.key = ''
  form.strict = false; form.save = true
  cmd.value = ''
}

function quickLocal(sh) {
  mode.value = 'local'
  shell.value = sh
  create()
}

function openNew() {
  err.value = ''
  mode.value = 'local'
  newOpen.value = true
  loadHosts()
}

function openSsh() {
  err.value = ''
  mode.value = 'ssh'
  newOpen.value = true
  loadHosts()
}

async function loadHosts() {
  try {
    const d = await api('/api/ssh/hosts')
    hosts.value = d.hosts || []
    keys.value = d.keys || []
  } catch { /* ignore */ }
}

function pickHost(h) {
  form.name = h.name
  form.host = h.host
  form.port = h.port
  form.user = h.user
  form.auth = h.auth
  form.key = h.key || ''
  form.strict = !!h.strict
  form.save = false
  if (h.auth === 'password') {
    nextTick(() => setTimeout(() => {
      const el = document.querySelector('.modal-b input[type=password]')
      if (el) el.focus()
    }, 60))
  }
}

async function delHost(h) {
  try {
    await api('/api/ssh/hosts/delete', { method: 'POST', body: { id: h.id } })
    hosts.value = hosts.value.filter(x => x.id !== h.id)
    toast('已删除主机', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function doImport() {  if (!keyText.value.trim()) { toast('请粘贴私钥内容', 'err'); return }
  try {
    const k = await api('/api/ssh/keys', {
      method: 'POST',
      body: { name: keyName.value || 'id_imported', content: keyText.value }
    })
    keys.value = (await api('/api/ssh/keys')).keys || []
    form.key = k.path
    keyText.value = ''; keyName.value = ''; importOpen.value = false
    toast('私钥已导入', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function closeTab(s) {
  try { await api(`/api/term/${s.id}`, { method: 'DELETE' }) } catch { /* ignore */ }
  const i = sessions.value.findIndex(x => x.id === s.id)
  if (i < 0) return
  sessions.value.splice(i, 1)
  if (activeId.value === s.id) {
    const next = sessions.value[i] || sessions.value[i - 1]
    activeId.value = next ? next.id : ''
  }
}

function onExit(id, code) {
  const s = sessions.value.find(x => x.id === id)
  if (s) { s.alive = false; s.exit = code }
}

function onTransport(id, t) {
  const s = sessions.value.find(x => x.id === id)
  if (s) s.transport = t
}

function openPaste() {
  pasteText.value = ''
  pasteOpen.value = true
  nextTick(() => setTimeout(() => { if (pasteEl.value) pasteEl.value.focus() }, 80))
}

function closePaste() { pasteOpen.value = false }

function doPaste() {
  const v = refs[activeId.value]
  if (v && pasteText.value) v.pasteText(pasteText.value + '\r')
  pasteOpen.value = false
}

onMounted(load)
</script>
