<template>
  <!-- 登录 -->
  <div v-if="!authed" class="login-wrap">
    <form class="login-card" @submit.prevent="doLogin">
      <div class="logo">P</div>
      <h1>PocketPanel</h1>
      <p>运行在 Termux 上的轻量服务器面板</p>
      <div class="field">
        <label>用户名</label>
        <input class="input" v-model="form.username" autocomplete="username" autocapitalize="off" spellcheck="false">
      </div>
      <div class="field">
        <label>密码</label>
        <input class="input" type="password" v-model="form.password" autocomplete="current-password" placeholder="请输入密码">
      </div>
      <div class="err" style="min-height:18px">{{ loginErr }}</div>
      <button class="btn primary block lg" type="submit" :disabled="busy">
        <span v-if="busy" class="spinner"></span>{{ busy ? '登录中…' : '登录' }}
      </button>
      <div class="dim" style="font-size:11.5px;margin-top:14px;line-height:1.7">
        面板仅监听局域网，请勿直接暴露到公网。
      </div>
    </form>
  </div>

  <div v-else class="shell">
    <div class="scrim" :class="{ on: ui.drawer }" @click="ui.drawer = false"></div>

    <!-- 侧栏 -->
    <aside class="side" :class="{ open: ui.drawer }">
      <div class="brand">
        <div class="brand-logo">P</div>
        <div class="grow" style="min-width:0">
          <b>PocketPanel</b>
          <span>Termux 面板 v{{ ui.version || '0.3.0' }}</span>
        </div>
      </div>

      <nav class="side-scroll">
        <div v-for="g in NAV_GROUPS" :key="g.title" class="nav-group">
          <div class="nav-group-t">{{ g.title }}</div>
          <div v-for="n in g.items" :key="n.id" class="nav-item" :class="{ on: ui.page === n.id }"
               @click="go(n.id)">
            <Icon :name="n.icon" />
            <span class="grow ell">{{ n.label }}</span>
            <span v-if="n.id === 'tasks' && msg.unread" class="badge-dot">{{ msg.unread > 9 ? '9+' : msg.unread }}</span>
          </div>
        </div>
      </nav>

      <div class="side-foot">
        <div class="who" @click="go('settings')">
          <div class="avatar">{{ (ui.user || 'A').charAt(0).toUpperCase() }}</div>
          <div class="grow" style="min-width:0">
            <b class="ell">{{ ui.user }}</b>
            <span>账户与安全</span>
          </div>
          <Icon name="chevronRight" class="ni s14" />
        </div>
        <div class="conn">
          <i class="dot" :class="online ? '' : 'fail'"></i>
          <span>{{ online ? '面板在线' : '连接已断开' }}</span>
        </div>
      </div>
    </aside>

    <!-- 主区 -->
    <div class="main">
      <div v-if="netBanner" class="net-banner" :class="netBanner.type">
        <Icon :name="netBanner.type === 'err' ? 'error' : netBanner.type === 'warn' ? 'warning' : netBanner.type === 'ok' ? 'success' : 'info'" class="ni s16" />
        <span class="grow">{{ netBanner.text }}</span>
        <span v-if="netBanner.countdown > 0" class="net-count mono">{{ netBanner.countdown }}s</span>
        <button class="ico-btn s14" @click="netBanner = null" title="关闭"><Icon name="close" /></button>
      </div>
      <header class="top">
        <button class="ico-btn" @click="ui.drawer = !ui.drawer" title="菜单" style="display:none" id="menu-btn">
          <Icon name="menu" />
        </button>
        <div class="top-title grow">
          <b>{{ cur.label }}</b>
          <span class="ell">{{ cur.desc || '' }}</span>
        </div>
        <button class="ico-btn" @click="openMsgs" title="消息中心" :class="{ on: msgOpen }">
          <Icon name="bell" />
          <span v-if="msg.unread" class="badge">{{ msg.unread > 9 ? '9+' : msg.unread }}</span>
        </button>
        <button class="ico-btn" @click="toggleTheme" :title="'主题：' + (ui.theme === 'dark' ? '深色' : '浅色')">
          <Icon :name="ui.theme === 'dark' ? 'sun' : 'moon'" />
        </button>
        <button class="ico-btn" @click="doLogout" title="退出登录"><Icon name="logout" /></button>
      </header>

      <main class="content">
        <div class="content-inner">
          <Transition name="pg" mode="out-in">
            <component :is="view" :key="ui.page" @go="go" />
          </Transition>
        </div>
      </main>
    </div>

    <!-- 移动端底部导航 -->
    <nav class="tabbar">
      <button v-for="n in TABS" :key="n.id" :class="{ on: ui.page === n.id }"
              @click="go(n.id)">
        <Icon :name="n.icon" />
        <span>{{ n.label }}</span>
      </button>
      <button @click="ui.drawer = true">
        <Icon name="menu" />
        <span>更多</span>
      </button>
    </nav>
  </div>

  <!-- 消息中心 -->
  <Modal v-if="msgOpen" title="消息中心" icon-name="bell" @close="msgOpen = false">
    <div v-if="msg.items.length">
      <div v-for="m in msg.items" :key="m.id" class="msg-item">
        <i class="dot" :class="{ off: m.type === 'info', fail: m.type === 'err', warn: m.type === 'warn' }"></i>
        <div class="m-main">
          <b>{{ m.title }}</b>
          <span>{{ m.body }}</span>
        </div>
        <span class="mt">{{ fmtShort(m.ts) }}</span>
      </div>
    </div>
    <EmptyState v-else icon-name="bell" title="没有消息"
                desc="计划任务的完成与失败、后台异常都会推送到这里" />
    <template #footer>
      <button class="btn" @click="clearMsgs">清空</button>
      <button class="btn ghost" @click="msgOpen = false">关闭</button>
    </template>
  </Modal>

  <div class="toasts">
    <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="t.type">
      <Icon :name="t.type === 'ok' ? 'success' : t.type === 'err' ? 'error' : t.type === 'warn' ? 'warning' : 'info'" />
      <span>{{ t.msg }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted, defineAsyncComponent } from 'vue'
import { api } from './api'
import { ui, toast, applyTheme } from './store'
import { NAV_GROUPS, NAV_MAP, TAB_IDS } from './nav'
import { startEvents, stopEvents, on as onEvent, setEventToken } from './events'
import Icon from './components/Icon.vue'
import Modal from './components/Modal.vue'
import EmptyState from './components/EmptyState.vue'
import PageLoading from './components/PageLoading.vue'
import Overview from './views/Overview.vue'
import Services from './views/Services.vue'
import Processes from './views/Processes.vue'
import Network from './views/Network.vue'
import Logs from './views/Logs.vue'
import Tasks from './views/Tasks.vue'
import Schedule from './views/Schedule.vue'
import Settings from './views/Settings.vue'

// 终端与文件页体积大（xterm.js / 大模板），按需加载；首屏只带仪表盘。
// loadingComponent 只在 chunk 加载超过 delay 才出现，本地加载几乎无感。
const asyncPage = (loader) => defineAsyncComponent({
  loader,
  loadingComponent: PageLoading,
  delay: 150,
})
const Terminal = asyncPage(() => import('./views/Terminal.vue'))
const Files = asyncPage(() => import('./views/Files.vue'))
const Tunnels = asyncPage(() => import('./views/Tunnels.vue'))
const Cloudflare = asyncPage(() => import('./views/Cloudflare.vue'))
const Docker = asyncPage(() => import('./views/Docker.vue'))
const AppStore = asyncPage(() => import('./views/AppStore.vue'))
const Gateway = asyncPage(() => import('./views/Gateway.vue'))
const Alerts = asyncPage(() => import('./views/Alerts.vue'))

const VIEWS = { overview: Overview, services: Services, processes: Processes, files: Files,
  terminal: Terminal, schedule: Schedule, network: Network, logs: Logs, tasks: Tasks,
  tunnels: Tunnels, cloudflare: Cloudflare, settings: Settings, docker: Docker,
  store: AppStore, gateway: Gateway, alerts: Alerts }

const TABS = TAB_IDS.map(id => NAV_MAP[id])

const msg = ref({ items: [], unread: 0 })
const msgOpen = ref(false)
const authed = ref(false)

// ---------------- 网络守卫全局提示 ----------------
// 网络切换时顶部挂一条横幅，恢复后换成结果摘要。
// 倒计时由本地 tick 递减，不用每秒轮询后端。
const netBanner = ref(null)
let netTimer = 0

function showBanner(type, text, countdown = 0) {
  netBanner.value = { type, text, countdown }
  clearInterval(netTimer)
  if (countdown > 0) {
    netTimer = setInterval(() => {
      if (!netBanner.value || netBanner.value.type !== 'info') { clearInterval(netTimer); return }
      netBanner.value.countdown -= 1
      if (netBanner.value.countdown <= 0) {
        netBanner.value.text = '网络稳定中，正在探测隧道…'
        clearInterval(netTimer)
      }
    }, 1000)
  }
}

function bindNetEvents() {
  onEvent('network_changed', (d) => {
    showBanner('info', d?.message || `检测到网络切换，正在等待网络稳定（${d?.debounce || 45}s）...`,
      Math.max(0, Math.ceil(((d?.deadline || 0) * 1000 - Date.now()) / 1000)))
  })
  onEvent('network_recovered', (d) => {
    showBanner(d?.fail_count ? 'warn' : 'ok', d?.message || '网络已恢复')
  })
  onEvent('network_recover_canceled', (d) => {
    showBanner('warn', d?.message || '网络尚未恢复，已取消本次自动恢复')
  })
  onEvent('network_circuit_broken', (d) => {
    showBanner('err', d?.message || '网络频繁闪断，已暂停自动重连，请检查网络环境')
  })
}
const busy = ref(false)
const online = ref(true)
const loginErr = ref('')
const form = ref({ username: 'admin', password: '' })

const view = computed(() => VIEWS[ui.page] || Overview)
const cur = computed(() => NAV_MAP[ui.page] || NAV_MAP.overview)

// ---------------- hash 路由 ----------------
// URL 形如 /#/files，刷新/分享/前进后退都能直达对应页面；
// 页面切换本身仍是组件级替换（宝塔式无整页刷新）。
function pageFromHash() {
  const h = decodeURIComponent((location.hash || '').replace(/^#\/?/, '')).replace(/\/+$/, '')
  return NAV_MAP[h] ? h : ''
}

function go(p) {
  if (!NAV_MAP[p]) p = 'overview'
  ui.drawer = false
  if (ui.page === p) { window.scrollTo(0, 0); return }
  const h = '#/' + p
  if (location.hash === h) {
    // hash 已在目标位（如登录后恢复），直接切
    ui.page = p
    window.scrollTo(0, 0)
  } else {
    location.hash = h  // 触发 hashchange → onHash，统一走一处
  }
}

function onHash() {
  if (!authed.value) return
  const p = pageFromHash()
  if (!p) {
    // 非法/空 hash：回到仪表盘并修正 URL（replace 不留历史记录）
    if (ui.page !== 'overview') go('overview')
    else history.replaceState(null, '', '#/overview')
    return
  }
  if (ui.page !== p) {
    ui.page = p
    ui.drawer = false
    window.scrollTo(0, 0)
  }
}

function toggleTheme() { applyTheme(ui.theme === 'dark' ? 'light' : 'dark') }

async function doLogin() {
  if (!form.value.username.trim()) { loginErr.value = '请输入用户名'; return }
  if (!form.value.password) { loginErr.value = '请输入密码'; return }
  busy.value = true; loginErr.value = ''
  try {
    const d = await api('/api/login', { method: 'POST', body: form.value })
    ui.user = d.username || form.value.username
    authed.value = true
    // 会话 token 存一份：事件通道的 WebSocket 不能自定义请求头，
    // 要靠查询参数把 token 带过去
    setEventToken(d.token || '')
    form.value.password = ''
    // 刷新后重登录：恢复 hash 指向的页面；无 hash 则回仪表盘
    const p = pageFromHash()
    if (p) ui.page = p
    else go('overview')
  } catch (e) {
    loginErr.value = e.message
  } finally { busy.value = false }
}

async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }) } catch { /* ignore */ }
  setEventToken('')
  authed.value = false
}

function fmtShort(ts) {
  const d = new Date(ts * 1000), n = new Date()
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
  if (d.toDateString() === n.toDateString()) return hm
  return (d.getMonth() + 1) + '-' + String(d.getDate()).padStart(2, '0') + ' ' + hm
}

async function loadMsgs() {
  try { msg.value = await api('/api/messages') } catch { /* 静默 */ }
}

function openMsgs() {
  msgOpen.value = true
  loadMsgs().then(() => {
    if (msg.value.unread) {
      api('/api/messages/read', { method: 'POST', body: { all: true } })
        .then(() => { msg.value.unread = 0 })
    }
  })
}

async function clearMsgs() {
  try { await api('/api/messages/clear', { method: 'POST' }); msg.value = { items: [], unread: 0 } } catch { /* ignore */ }
}

async function boot() {
  try {
    const d = await api('/api/session')
    ui.version = d.version
    if (d.authed) {
      ui.user = d.username || 'admin'
      // 刷新后仍要拿到 token：事件通道的 WebSocket 靠它鉴权
      setEventToken(d.token || '')
      authed.value = true
      online.value = true
      // 已有会话：按当前 hash 直达页面（刷新/前进后退场景），无 hash 则补上
      const p = pageFromHash()
      if (p) ui.page = p
      else if (!location.hash) history.replaceState(null, '', '#/overview')
    } else {
      authed.value = false
    }
  } catch {
    authed.value = false
  }
}

let timer = null
function onUnauth() { authed.value = false }

// Esc 关闭移动端抽屉
function onKey(e) {
  if (e.key === 'Escape' && ui.drawer) ui.drawer = false
}

function syncMenuBtn() {
  const b = document.getElementById('menu-btn')
  if (!b) return
  const mobile = window.matchMedia('(max-width:900px)').matches
  b.style.display = mobile ? 'grid' : 'none'
}

// 登录前 #menu-btn 还在 v-else 分支里，onMounted 那次是空跑；
// 手机上首次登录后菜单按钮会一直是 display:none，必须进 shell 再同步一次
watch(authed, v => {
  if (v) { nextTick(syncMenuBtn); startEvents() }
  else { stopEvents(); netBanner.value = null }
})

onMounted(() => {
  let t = 'dark'
  try { t = localStorage.getItem('pp_theme') || 'dark' } catch { /* ignore */ }
  applyTheme(t)
  boot()
  bindNetEvents()
  window.addEventListener('pp-unauth', onUnauth)
  window.addEventListener('resize', syncMenuBtn)
  window.addEventListener('keydown', onKey)
  window.addEventListener('hashchange', onHash)
  syncMenuBtn()
  // 轻量心跳，只用于顶部状态指示与消息角标
  timer = setInterval(async () => {
    if (!authed.value) return
    try { await api('/api/overview'); online.value = true } catch { online.value = false }
    loadMsgs()
  }, 15000)
  setTimeout(loadMsgs, 800)
})

onUnmounted(() => {
  window.removeEventListener('pp-unauth', onUnauth)
  window.removeEventListener('resize', syncMenuBtn)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('hashchange', onHash)
  if (timer) clearInterval(timer)
  clearInterval(netTimer)
  stopEvents()
})
</script>
