<template>
  <!-- 顶部：标题 + 操作 -->
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="grid" />应用商店</h2>
      <p>装好就能用的应用 · 目前支持 Docker 容器，其它类型已预留接入位</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="openSettings"><Icon name="settings" />商店</button>
      <button class="btn" :disabled="st.loading" @click="load(true)">
        <Icon name="refresh" />{{ st.loading ? '加载中' : '刷新' }}
      </button>
    </div>
  </div>

  <!-- 商店状态 -->
  <div class="card st-bar">
    <span class="st-dot" :class="{ off: !st.ok }"></span>
    <span class="st-txt">
      {{ st.ok ? '已连接应用商店' : '离线，使用' + (st.source === 'builtin' ? '内置清单' : '本地缓存') }}
      <span class="dim" v-if="st.updated"> · 更新于 {{ st.updated }}</span>
    </span>
    <span v-if="st.error" class="st-err ell" :title="st.error">{{ st.error }}</span>
    <span class="grow"></span>
    <span class="tag">{{ st.apps.length }} 个应用</span>
  </div>

  <!-- 搜索 + 类型 -->
  <div class="st-filters">
    <div class="st-search">
      <Icon name="search" class="ni s14" />
      <input class="input" v-model.trim="q" placeholder="搜索应用名、简介或镜像" spellcheck="false" />
      <button v-if="q" class="st-clr" @click="q = ''" title="清空"><Icon name="close" /></button>
    </div>
    <div class="st-types" v-if="st.cats.length > 1">
      <button class="st-chip" :class="{ on: cat === 'all' }" @click="cat = 'all'">
        全部 <b>{{ st.apps.length }}</b>
      </button>
      <button v-for="c in st.cats" :key="c.id" class="st-chip"
              :class="{ on: cat === c.id }" @click="cat = c.id">
        {{ c.label }} <b>{{ c.count }}</b>
      </button>
    </div>
    <div class="st-types" v-if="st.types.length > 1">
      <button class="st-chip" :class="{ on: type === 'all' }" @click="type = 'all'">
        全部 <b>{{ st.apps.length }}</b>
      </button>
      <button v-for="t in st.types" :key="t.id" class="st-chip"
              :class="{ on: type === t.id, dimmed: !t.count }"
              :title="t.desc || t.reason" @click="type = t.id">
        <Icon :name="t.icon" class="ni s13" />
        {{ t.short || t.label }} <b>{{ t.count }}</b>
        <span v-if="!t.implemented" class="st-soon">待接入</span>
      </button>
    </div>
  </div>

  <!-- 应用网格 -->
  <div v-if="st.loading && !st.apps.length" class="card st-skel">
    <div v-for="i in 6" :key="i" class="st-sk"></div>
  </div>

  <div v-else-if="shown.length" class="st-grid">
    <div v-for="a in shown" :key="a.id" class="card st-card" :class="{ off: !typeReady(a) }"
         @click="openApp(a)">
      <div class="st-card-h">
        <span class="st-ico" :class="'t-' + a.type">
          <Icon v-if="a.icon_name" :name="a.icon_name" />
          <template v-else>{{ a.icon || '📦' }}</template>
        </span>
        <div class="st-card-t">
          <b class="ell">{{ a.name }}</b>
          <span class="ell dim">{{ a.summary }}</span>
        </div>
        <span v-if="st.installed[a.id]" class="tag ok st-badge">
          {{ st.installed[a.id].state === 'running' ? '运行中' : '已安装' }}
        </span>
      </div>
      <p class="st-card-d">{{ a.desc }}</p>
      <div class="st-card-m">
        <span class="tag">{{ typeLabel(a.type) }}</span>
        <span class="tag">{{ a.size || '大小未知' }}</span>
        <span v-if="a.verified" class="tag ok">已实测</span>
        <span v-else class="tag y">未实测</span>
      </div>
      <div class="st-card-a">
        <span v-if="a.ports && a.ports.length" class="dim mono">:{{ a.ports[0].host }}</span>
        <button v-if="st.installed[a.id]" class="btn xs" @click.stop="openUrl(a)">打开</button>
        <button v-else class="btn xs primary" :disabled="!typeReady(a)"
                :title="typeReady(a) ? '' : typeReason(a.type)" @click.stop="openApp(a)">
          {{ typeReady(a) ? '安装' : '暂不支持' }}
        </button>
      </div>
    </div>
  </div>

  <div v-else class="card st-empty">
    <Icon :name="q ? 'search' : emptyIcon" class="st-empty-ico" />
    <b>{{ q ? '没搜到匹配的应用' : emptyTitle }}</b>
    <span class="dim">{{ q ? '换个关键词，或者切到「全部」' : emptyHint }}</span>
    <button v-if="q" class="btn sm" @click="q = ''">清空搜索</button>
  </div>

  <!-- 应用详情 / 安装 -->
  <Modal v-if="dlg.open && dlg.app" :title="dlg.app.name" :sub="dlg.app.summary"
         :icon-name="dlg.app.icon_name || 'package'" size="wide" @close="closeApp">
    <div class="st-det">
      <div class="st-det-l">
        <div class="st-det-hero">
          <span class="st-ico lg" :class="'t-' + dlg.app.type">
            <Icon v-if="dlg.app.icon_name" :name="dlg.app.icon_name" />
            <template v-else>{{ dlg.app.icon || '📦' }}</template>
          </span>
          <div class="st-det-t">
            <div class="st-det-n">
              {{ dlg.app.name }}
              <span class="tag">{{ typeLabel(dlg.app.type) }}</span>
              <span v-if="dlg.app.verified" class="tag ok">已实测</span>
              <span v-else class="tag y">未实测</span>
            </div>
            <div class="dim mono">{{ dlg.app.id }} · {{ dlg.app.size || '大小未知' }}</div>
          </div>
        </div>

        <p class="st-det-d">{{ dlg.app.desc }}</p>

        <div class="st-kv">
          <div v-if="dlg.app.image" class="st-kv-r long"><span>镜像</span><b class="mono">{{ dlg.app.image }}</b></div>
          <div v-if="dlg.app.arch && dlg.app.arch.length" class="st-kv-r">
            <span>架构</span><b class="mono">{{ dlg.app.arch.join(' / ') }}</b>
          </div>
          <div v-if="dlg.app.ports && dlg.app.ports.length" class="st-kv-r">
            <span>端口</span><b class="mono">{{ portText(dlg.app) }}</b>
          </div>
          <div v-if="dlg.app.website" class="st-kv-r">
            <span>官网</span><a :href="dlg.app.website" target="_blank" rel="noreferrer" class="mono">{{ dlg.app.website }}</a>
          </div>
        </div>

        <div v-if="dlg.app.tips" class="note st-tip"><Icon name="info" class="ni s14" /><span>{{ dlg.app.tips }}</span></div>
        <div v-if="dlg.app.verified === false" class="note warn st-tip">
          <Icon name="warning" class="ni s14" />
          <span>这个应用还没在这台设备上实测跑通，装完可能起不来；起不来先看容器日志。</span>
        </div>
        <div v-if="!typeReady(dlg.app)" class="note warn st-tip">
          <Icon name="warning" class="ni s14" /><span>{{ typeReason(dlg.app.type) }}</span>
        </div>
      </div>

      <!-- 右栏：安装 -->
      <div class="st-det-r">
        <template v-if="st.installed[dlg.app.id]">
          <div class="st-inst-ok">
            <Icon name="success" class="ni s18" />
            <div>
              <b>已安装</b>
              <div class="dim">{{ st.installed[dlg.app.id].name }} ·
                {{ st.installed[dlg.app.id].state === 'running' ? '运行中' : '未运行' }}</div>
            </div>
          </div>
          <button class="btn block" @click="openUrl(dlg.app)"><Icon name="external" />打开应用</button>
          <p class="dim st-mini">要改端口或看日志，去「容器」页面找 {{ st.installed[dlg.app.id].name }}。</p>
        </template>

        <template v-else-if="dlg.task">
          <div class="st-task-h">
            <span class="spinner" v-if="dlg.task.status === 'running'"></span>
            <b>{{ taskTitle }}</b>
          </div>
          <div class="st-task-bar"><i :style="{ width: (dlg.task.progress || 0) + '%' }"></i></div>
          <pre class="st-task-log mono">{{ dlg.task.output || '等待输出…' }}</pre>
          <button v-if="dlg.task.status !== 'running'" class="btn block" @click="dlg.open = false">关闭</button>
        </template>

        <template v-else>
          <div class="field">
            <label>容器名</label>
            <input class="input mono" v-model.trim="dlg.name" :placeholder="dlg.app.id" />
          </div>
          <div class="field" v-if="dlg.app.ports && dlg.app.ports.length">
            <label>宿主机端口{{ dlg.app.port_fixed ? '（固定）' : '' }}</label>
            <input class="input mono" type="number" min="1024" max="65535"
                   v-model.number="dlg.port" :disabled="!!dlg.app.port_fixed" />
            <span class="st-mini dim" v-if="dlg.app.port_fixed">
              这个镜像的启动参数里写死了监听端口，改了容器也不会跟着变，所以锁住。
            </span>
            <span class="st-mini dim" v-else>
              {{ portText(dlg.app) }}；host 网络下直接占宿主机端口，1024 以下绑不了。
            </span>
          </div>
          <div class="field">
            <label>数据目录</label>
            <input class="input mono" v-model.trim="dlg.data"
                   :placeholder="'~/pocketpanel/apps/' + dlg.app.id" />
          </div>
          <div v-if="dlg.err" class="st-err2">{{ dlg.err }}</div>
          <button class="btn primary block" :disabled="dlg.busy || !typeReady(dlg.app)" @click="install">
            <span v-if="dlg.busy" class="spinner"></span>
            <Icon v-else name="download" />
            {{ dlg.busy ? '提交中…' : '一键安装' }}
          </button>
        </template>
      </div>
    </div>
  </Modal>

  <!-- 商店设置 -->
  <Modal v-if="cfg.open" title="应用商店" sub="清单地址与访问密钥" icon-name="settings" @close="cfg.open = false">
    <div class="field"><label>商店地址</label>
      <input class="input mono" v-model.trim="cfg.url" placeholder="https://store.sanhe.dpdns.org" />
      <span class="st-mini dim">留空则只用内置清单。改完保存会立即重拉。</span>
    </div>
    <div class="field"><label>访问密钥（可选）</label>
      <input class="input mono" v-model.trim="cfg.key" placeholder="X-Store-Key" />
    </div>
    <div v-if="cfg.err" class="st-err2">{{ cfg.err }}</div>
    <template #footer>
      <button class="btn" @click="cfg.open = false">取消</button>
      <button class="btn primary" :disabled="cfg.busy" @click="saveSettings">
        <span v-if="cfg.busy" class="spinner"></span>{{ cfg.busy ? '保存中…' : '保存并刷新' }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

const st = reactive({
  apps: [], types: [], cats: [], installed: {}, source: '', updated: '',
  ok: true, error: '', loading: true,
})
const q = ref('')
const type = ref('all')
const cat = ref('all')

const dlg = reactive({
  open: false, app: null, name: '', port: '', data: '',
  busy: false, err: '', task: null, taskId: '',
})
const cfg = reactive({ open: false, url: '', key: '', busy: false, err: '' })

const typeMap = computed(() => Object.fromEntries(st.types.map(t => [t.id, t])))

function typeLabel(id) {
  const t = typeMap.value[id]
  return t ? (t.short || t.label) : (id || '未知')
}

function typeReady(app) {
  const t = typeMap.value[app.type || 'docker']
  return !!(t && t.implemented && t.ready)
}

function typeReason(id) {
  const t = typeMap.value[id]
  if (!t) return '当前版本不认识这种类型的应用'
  if (!t.implemented) return `${t.label}还没接入，等面板升级后可用`
  if (!t.ready) {
    return id === 'docker'
      ? '容器运行时没就绪：先到「容器」页装 udocker 并启动 udockerd'
      : '该类型的运行时未就绪'
  }
  return ''
}

const shown = computed(() => {
  const kw = q.value.toLowerCase()
  return st.apps.filter(a => {
    if (type.value !== 'all' && (a.type || 'docker') !== type.value) return false
    if (cat.value !== 'all' && (a.category || 'other') !== cat.value) return false
    if (!kw) return true
    return [a.name, a.summary, a.desc, a.id, a.image, a.category]
      .filter(Boolean).join(' ').toLowerCase().includes(kw)
  })
})

const catLabel = computed(() => (st.cats.find(c => c.id === cat.value) || {}).label || '')

const emptyTitle = computed(() => {
  if (cat.value !== 'all' && type.value === 'all') return `${catLabel.value}分类下还没有应用`
  if (type.value === 'all') return '商店里还没有应用'
  const t = typeMap.value[type.value]
  return t ? `还没有 ${t.label}` : '没有应用'
})

const emptyHint = computed(() => {
  if (cat.value !== 'all' && type.value === 'all') return '切到「全部」看看，或者点「刷新」重拉清单'
  if (type.value === 'all') return '点右上角「刷新」重拉清单，或检查商店设置里的地址'
  const t = typeMap.value[type.value]
  if (t && !t.implemented) return '这类应用的接入位已经预留，商店上架后会自动出现在这里'
  if (t && t.implemented && !t.ready) return typeReason(t.id)
  return '换个分类或类型看看，或者点「刷新」'
})

const emptyIcon = computed(() => (typeMap.value[type.value] || {}).icon || 'package')

const taskTitle = computed(() => {
  const s = dlg.task && dlg.task.status
  if (s === 'success') return '安装完成'
  if (s === 'failed') return '安装失败'
  if (s === 'canceled') return '已取消'
  return '正在安装…'
})

function portText(app) {
  const ps = app.ports || []
  if (!ps.length) return '不需要端口'
  return ps.map(p => `容器 ${p.container} → 宿主 ${p.host}`).join('，')
}

function appUrl(a) {
  const p = (a.ports || [])[0]
  if (!p) return ''
  return `${location.protocol}//${location.hostname}:${p.host}`
}

function openUrl(a) {
  const u = appUrl(a)
  if (u) window.open(u, '_blank', 'noreferrer')
}

async function load(force = false) {
  st.loading = true
  try {
    const r = await api('/api/appstore/catalog' + (force ? '?force=1' : ''))
    st.apps = r.apps || []
    st.types = r.types || []
    st.cats = r.cats || []
    st.source = r.source || ''
    st.updated = r.updated || ''
    st.ok = !!(r.store && r.store.ok)
    st.error = (r.store && r.store.error) || ''
    const ins = await api('/api/appstore/installed')
    st.installed = ins.items || {}
  } catch (e) {
    st.ok = false
    st.error = e.message || String(e)
  } finally {
    st.loading = false
  }
}

function openApp(a) {
  dlg.app = a
  dlg.open = true
  dlg.name = a.id
  dlg.port = a.ports && a.ports.length ? a.ports[0].host : ''
  dlg.data = ''
  dlg.err = ''
  dlg.task = null
  dlg.taskId = ''
  dlg.busy = false
}

function closeApp() {
  dlg.open = false
  dlg.task = null
  load()
}

async function install() {
  dlg.busy = true
  dlg.err = ''
  try {
    const r = await api('/api/appstore/install', {
      method: 'POST',
      body: { id: dlg.app.id, name: dlg.name, port: dlg.port, data: dlg.data },
    })
    dlg.taskId = r.id
    dlg.task = { status: 'running', output: '', progress: 2 }
    poll(r.id)
  } catch (e) {
    dlg.err = e.message || String(e)
  } finally {
    dlg.busy = false
  }
}

async function poll(id) {
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 1200))
    let t
    try {
      t = await api('/api/exec/' + id)
    } catch (e) {
      dlg.task = { ...(dlg.task || {}), status: 'failed', output: e.message }
      return
    }
    dlg.task = t
    if (t.status !== 'running') {
      if (t.status === 'success') {
        toast(`${dlg.app.name} 装好了`, 'ok')
        await load()
      }
      return
    }
  }
  dlg.task = { ...(dlg.task || {}), status: 'failed', output: '等了 12 分钟还没结束，去「任务队列」看进度' }
}

async function openSettings() {
  cfg.err = ''
  try {
    const r = await api('/api/appstore/settings')
    cfg.url = r.settings.url || ''
    cfg.key = r.settings.key || ''
  } catch (e) { /* 读不到就用空值 */ }
  cfg.open = true
}

async function saveSettings() {
  cfg.busy = true
  cfg.err = ''
  try {
    await api('/api/appstore/settings', { method: 'POST', body: { url: cfg.url, key: cfg.key } })
    cfg.open = false
    toast('商店设置已保存', 'ok')
    await load(true)
  } catch (e) {
    cfg.err = e.message || String(e)
  } finally {
    cfg.busy = false
  }
}

onMounted(() => load())
</script>
