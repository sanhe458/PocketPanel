<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="folder" />文件管理</h2>
      <p>上传、编辑、压缩、分享与回收站，回收站里的文件可随时恢复</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="toggleSearch"><Icon name="search" />搜索</button>
      <button class="btn" @click="sheet = { title: '新建', items: NEW_ITEMS, mode: 'new' }"><Icon name="plus" />新建</button>
      <button class="btn primary" @click="pickFile"><Icon name="upload" />上传</button>
    </div>
  </div>

  <!-- 工具栏 -->
  <div class="card flush mb12">
    <div class="card-h">
      <Icon name="home" />
      <div class="crumb grow">
        <span @click="load(HOME)">~</span>
        <template v-for="(p, i) in crumbs" :key="i">
          <i class="sepc">/</i><span @click="load(p.path)">{{ p.name }}</span>
        </template>
      </div>
      <div class="sub">
        <span class="fs11 dim">{{ entries.length }} 项</span>
      </div>
    </div>
    <div class="card-b tight">
      <div class="ftool">
        <button class="btn sm" @click="load(cwd)"><Icon name="refresh" class="ni s12" />刷新</button>
        <button v-if="clip" class="btn sm primary" @click="paste">
          <Icon name="paste" class="ni s12" />粘贴 {{ clip.paths.length }} 项{{ clip.cut ? '（剪切）' : '' }}
        </button>
        <span class="spacer"></span>
        <button class="btn sm" @click="sheet = { title: '更多操作', items: MORE_ITEMS, mode: 'more' }">
          <Icon name="moreH" class="ni s12" />更多
        </button>
      </div>
    </div>
  </div>

  <!-- 搜索 -->
  <div v-if="searchOpen" class="card mb12">
    <div class="row" style="margin-bottom:10px">
      <input class="input" v-model="query" placeholder="文件名或内容关键字" style="flex:1" @keydown.enter="doSearch">
      <button class="btn primary" @click="doSearch"><Icon name="search" class="ni s12" />查找</button>
    </div>
    <div class="row wrap" style="gap:12px;font-size:12.5px;color:var(--txt-2)">
      <label class="row" style="gap:5px"><input type="checkbox" v-model="sub"> 包含子目录</label>
      <label class="row" style="gap:5px"><input type="checkbox" v-model="withContent"> 搜索内容</label>
      <input class="input" v-model="suffix" placeholder="后缀过滤，如 go,conf" style="flex:1;min-width:110px;height:30px">
    </div>
    <div class="hint" style="margin-top:8px">超时 8 秒、最多 200 条，到限即返回，不会卡住面板。</div>
  </div>

  <!-- 多选条 -->
  <div v-if="sel.length" class="selbar">
    <span class="dim" style="font-size:12px">已选 {{ sel.length }} 项</span>
    <span class="spacer"></span>
    <button class="btn sm" @click="batch('copy')">复制</button>
    <button class="btn sm" @click="batch('cut')">剪切</button>
    <button class="btn sm" @click="batch('zip')">压缩</button>
    <button class="btn sm danger" @click="batch('del')">删除</button>
    <button class="btn sm ghost" @click="sel = []">取消</button>
  </div>

  <!-- 搜索结果 -->
  <div v-if="searching" class="card flush mb12">
    <div class="card-h">
      <Icon name="search" />
      <h3>搜索结果</h3>
      <div class="sub"><span class="fs11 dim">{{ done ? `${hits.length} 条${truncated ? ' · 已截断' : ''}` : '搜索中…' }}</span></div>
    </div>
    <div class="list" style="padding:0 16px">
      <div v-for="h in hits" :key="h.path + h.line" class="frow">
        <span style="width:18px;text-align:center;color:var(--txt-3)">{{ h.is_dir ? '▸' : '·' }}</span>
        <div class="nm" @click="h.is_dir ? load(h.path) : openEditor(h.path, h.name)">
          <b>{{ h.name }}</b><span>{{ h.path.replace(/\/[^/]+$/, '') }}</span>
          <div v-if="h.match === 'content'" class="hit-line">{{ h.line }}: {{ h.text }}</div>
        </div>
      </div>
      <div v-if="done && !hits.length"><EmptyState icon-name="search" title="没有匹配项" /></div>
    </div>
  </div>

  <!-- 文件列表 -->
  <div v-else class="card flush">
    <div class="card-h">
      <Icon name="folder" />
      <h3>文件列表</h3>
      <div class="sub"><span class="fs11 dim mono ell" style="max-width:46%">{{ cwd }}</span></div>
    </div>
    <div class="list" style="padding:0 16px">
      <div v-for="f in entries" :key="f.name" class="frow" :class="{ sel: sel.includes(f.name) }"
           @dragover.prevent @drop.prevent="onDrop">
        <div class="fchk" @click.stop="toggleSel(f.name)">✓</div>
        <span class="file-type" :class="{ dir: f.is_dir }"><Icon :name="f.is_dir ? 'folder' : fileIcon(f)" class="ni s12" /></span>
        <div class="nm" @click="f.is_dir ? load(join(f.name)) : openEditor(join(f.name), f.name)">
          <b>{{ f.name }}</b>
          <span>{{ f.is_dir ? '目录' : fmtB(f.size) }} · {{ f.mode }} · {{ fmtTime(f.mtime) }}</span>
        </div>
        <div class="fops">
          <button @click.stop="openMenu(f)" title="更多操作"><Icon name="moreH" class="ni s14" /></button>
        </div>
      </div>
      <div v-if="!entries.length">
        <EmptyState icon-name="folder" title="空目录" desc="点右上角「上传」或「新建」添加内容" />
      </div>
    </div>
  </div>

  <!-- 上传进度 -->
  <div v-if="up.show" class="upbar">
    <div class="row" style="margin-bottom:6px;font-size:12px">
      <b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ up.name }}</b>
      <span class="spacer"></span><span class="dim">{{ up.pct }}%</span>
    </div>
    <div class="bar"><i :style="{ width: up.pct + '%' }"></i></div>
    <div class="row" style="margin-top:8px">
      <span class="dim" style="font-size:11px">{{ fmtB(up.sent) }} / {{ fmtB(up.total) }}</span>
      <span class="spacer"></span><button class="btn sm ghost" @click="abort = true">取消上传</button>
    </div>
  </div>

  <input ref="fileInput" type="file" multiple style="display:none" @change="onPick">

  <!-- 单行输入（替代 prompt，WebView 里可控） -->
  <Panel v-if="ask.show" :title="ask.title" icon-name="edit" @close="ask.show = false">
    <div class="field"><label>名称</label>
      <input class="input" v-model="ask.value" :placeholder="ask.hint" autocapitalize="off" spellcheck="false"
             @keydown.enter="submitAsk"></div>
    <template #footer>
      <button class="btn ghost" style="flex:1" @click="ask.show = false">取消</button>
      <button class="btn primary" style="flex:1" @click="submitAsk">确定</button>
    </template>
  </Panel>

  <!-- 动作菜单 -->
  <Sheet v-if="sheet" :title="sheet.title" :items="sheet.items" @close="sheet = null"
         @pick="onSheetPick" />

  <!-- 通用面板 -->
  <Panel v-if="panel" :title="panel.title" :icon-name="panelIcon" @close="panel = null">
    <!-- 回收站 -->
    <template v-if="panel.type === 'trash'">
      <div class="row between" style="margin-bottom:10px">
        <span class="dim" style="font-size:11px">{{ trash.count }} 项 · 占用 {{ fmtB(trash.total_size) }}</span>
        <button class="btn sm danger" :disabled="!trash.count" @click="emptyTrash">清空回收站</button>
      </div>
      <div v-if="trash.items && trash.items.length" class="list">
        <div v-for="it in trash.items" :key="it.id" class="mini">
          <span style="width:18px;text-align:center;color:var(--txt-3)">{{ it.is_dir ? '▸' : '·' }}</span>
          <div class="m"><b>{{ it.name }}</b>
            <span>{{ it.orig_path.replace(/\/[^/]+$/, '') }} · {{ fmtB(it.size) }} · {{ fmtTime(it.deleted_at) }}</span></div>
          <button class="btn sm" style="min-width:52px" @click="restore(it.id)">恢复</button>
          <button class="btn sm danger" style="min-width:64px" @click="purge(it.id)">删除</button>
        </div>
      </div>
      <div v-else><EmptyState icon-name="trash" title="回收站是空的" desc="删除的文件会先放在这里，可随时恢复" /></div>
      <div class="row between" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
        <span class="dim" style="font-size:11px">自动清理</span>
        <div class="row" style="gap:6px">
          <input class="input" v-model.number="keepDays" style="width:64px;height:30px;text-align:center">
          <span class="dim" style="font-size:11px">天</span>
          <button class="btn sm" @click="saveKeep">保存</button>
        </div>
      </div>
      <div class="dim" style="font-size:11px;margin-top:6px">填 0 表示不自动清理。</div>
    </template>

    <!-- 历史版本 -->
    <template v-else-if="panel.type === 'versions'">
      <div class="dim" style="font-size:11px;margin-bottom:8px">
        保留最近 {{ panel.data.keep }} 份。恢复前会把当前内容也存为一份版本。</div>
      <div v-if="panel.data.versions.length" class="list">
        <div v-for="v in panel.data.versions" :key="v.id" class="mini">
          <div class="m"><b>{{ fmtFull(v.ts) }}</b><span>{{ fmtB(v.size) }}</span></div>
          <button class="btn sm ghost" @click="readVersion(v.id)">查看</button>
          <button class="btn sm" @click="restoreVersion(v.id)">恢复</button>
        </div>
      </div>
      <div v-else><EmptyState icon-name="history" title="暂无历史版本" desc="在面板内编辑并保存时，会自动保留旧内容" /></div>
    </template>

    <!-- 版本内容 -->
    <template v-else-if="panel.type === 'vread'">
      <pre class="code" style="max-height:56vh">{{ panel.data }}</pre>
    </template>

    <!-- 分享 -->
    <template v-else-if="panel.type === 'share'">
      <div class="field"><label>有效期</label>
        <div class="seg">
          <button v-for="h in [1, 24, 168, 720]" :key="h" :class="{ on: hours === h }" @click="hours = h">
            {{ { 1: '1 小时', 24: '24 小时', 168: '7 天', 720: '30 天' }[h] }}</button>
        </div>
      </div>
      <div class="row between" style="padding:6px 0 12px">
        <div><div style="font-size:12px;font-weight:600">需要提取码</div>
          <div class="dim" style="font-size:11px">生成 4 位提取码，防止链接被随意转发</div></div>
        <button class="switch" :class="{ on: needCode }" @click="needCode = !needCode"
                :aria-label="needCode ? '需要提取码' : '不需要提取码'"></button>
      </div>
      <div class="dim" style="font-size:11px">链接只读、可随时撤销。目录需先压缩再分享。</div>
      <div v-if="shareResult" style="margin-top:12px;padding:10px;background:var(--card-2);border-radius:9px">
        <div class="mono" style="font-size:11.5px;word-break:break-all;color:var(--brand)">{{ shareResult.url }}</div>
        <div v-if="shareResult.code" style="margin-top:6px;font-size:12px">
          提取码 <b class="mono" style="font-size:15px;letter-spacing:2px">{{ shareResult.code }}</b></div>
        <div class="dim" style="font-size:11px;margin-top:6px">到期：{{ fmtFull(shareResult.expires_at) }}</div>
        <button class="btn sm" style="margin-top:8px" @click="copy(shareResult.url)">复制链接</button>
      </div>
    </template>

    <!-- 分享列表 -->
    <template v-else-if="panel.type === 'shares'">
      <div v-if="panel.data.length" class="list">
        <div v-for="s in panel.data" :key="s.token" class="mini" :class="{ dim: s.expired }">
          <div class="m"><b>{{ s.name }}</b>
            <span>{{ s.expired ? '已过期 · ' : '' }}下载 {{ s.downloads }} 次 · 到期 {{ fmtFull(s.expires_at) }}</span></div>
          <button class="btn sm danger" @click="revoke(s.token)">撤销</button>
        </div>
      </div>
      <div v-else><EmptyState icon-name="share" title="还没有创建分享" /></div>
    </template>

    <!-- 收藏夹 -->
    <template v-else-if="panel.type === 'favs'">
      <div v-if="panel.data.length" class="list">
        <div v-for="f in panel.data" :key="f.path" class="mini" :class="{ dim: !f.valid }">
          <div class="m"><b>{{ f.name }}</b><span>{{ f.path }}{{ f.valid ? '' : ' · 路径不存在' }}</span></div>
          <button v-if="f.valid" class="btn sm" @click="panel = null; load(f.path)">打开</button>
        </div>
      </div>
      <div v-else><EmptyState icon-name="bookmark" title="还没有收藏路径" desc="在「更多」里收藏当前目录" /></div>
    </template>

    <!-- 属性 -->
    <template v-else-if="panel.type === 'stat'">
      <div class="kv"><span class="k">名称</span><span class="v">{{ panel.data.name }}</span></div>
      <div class="kv"><span class="k">路径</span><span class="v mono" style="font-size:11px">{{ panel.data.path }}</span></div>
      <div class="kv"><span class="k">类型</span><span class="v">{{ panel.data.is_dir ? '目录' : '文件' }}</span></div>
      <div class="kv"><span class="k">大小</span><span class="v">{{ panel.data.is_dir ? fmtB(panel.data.dir_size) + '（含子项）' : fmtB(panel.data.size) }}</span></div>
      <div class="kv"><span class="k">权限</span><span class="v mono">{{ panel.data.mode }} ({{ panel.data.mode_str }})</span></div>
      <div class="kv"><span class="k">属主</span><span class="v">{{ panel.data.owner }}</span></div>
      <div class="kv"><span class="k">修改时间</span><span class="v">{{ fmtFull(panel.data.mtime) }}</span></div>
      <div class="kv"><span class="k">历史版本</span><span class="v">{{ panel.data.versions }} 份</span></div>
    </template>

    <!-- 权限 -->
    <template v-else-if="panel.type === 'chmod'">
      <div class="field"><label>权限（八进制）</label>
        <input class="input mono" v-model="chmodMode" maxlength="4"></div>
      <div class="row wrap" style="gap:6px;margin-bottom:14px">
        <button v-for="m in ['644', '755', '600', '777']" :key="m" class="btn sm" @click="chmodMode = m">{{ m }}</button>
      </div>
      <div class="hint" style="color:var(--warn)">
        注意：勾选递归会把权限应用到该目录下所有文件与子目录，数量可能上万，且无法一键撤销。
      </div>
      <label class="row" style="gap:8px;margin-top:14px;min-height:44px">
        <input type="checkbox" v-model="chmodRec"> <span style="font-size:13px">递归应用到子目录</span>
      </label>
    </template>

    <!-- 远程下载 -->
    <template v-else-if="panel.type === 'remote'">
      <div class="field"><label>文件 URL</label>
        <input class="input mono" v-model="remoteUrl" placeholder="https://example.com/file.tar.gz"></div>
      <div class="field"><label>保存到</label>
        <input class="input mono" :value="cwd" readonly></div>
      <div class="field"><label>文件名（可选）</label>
        <input class="input mono" v-model="remoteName" placeholder="留空则从 URL 推断"></div>
      <div class="hint">下载在后台进行，可以先去做别的事。文件先写 .part，完成后改名。</div>
    </template>

    <!-- 文件设置 -->
    <template v-else-if="panel.type === 'fsettings'">
      <div class="field"><label>回收站保留天数</label>
        <input class="input" v-model.number="keepDays" inputmode="numeric">
        <div class="hint">0 表示不自动清理，每小时检查一次。</div></div>
      <div class="field"><label>每个文件保留的历史版本数</label>
        <input class="input" v-model.number="verKeep" inputmode="numeric">
        <div class="hint">仅对 8MB 以下的文件做快照。</div></div>
    </template>

    <template #footer>
      <button class="btn ghost" style="flex:1" @click="panel = null">关闭</button>
      <button v-if="panel.type === 'share'" class="btn primary" style="flex:1" @click="createShare">生成链接</button>
      <button v-if="panel.type === 'chmod'" class="btn primary" style="flex:1" @click="applyChmod">应用</button>
      <button v-if="panel.type === 'remote'" class="btn primary" style="flex:1" @click="startRemote">开始下载</button>
      <button v-if="panel.type === 'fsettings'" class="btn primary" style="flex:1" @click="saveFileSettings">保存</button>
    </template>
  </Panel>

  <!-- 编辑器 -->
  <div v-if="editing" class="editor">
    <div class="ed-h">
      <b>{{ editing.name }}</b><span class="dim">{{ editing.path }}</span>
      <span class="spacer"></span>
      <button class="btn sm ghost" @click="closeEditor">关闭</button>
    </div>
    <textarea class="ed-text" v-model="editing.content" spellcheck="false"></textarea>
    <div class="ed-f">
      <span class="dim" style="font-size:11px">{{ editing.content.length }} 字符</span>
      <span class="spacer"></span>
      <button class="btn sm" @click="openEditor(editing.path, editing.name)"><Icon name="refresh" class="ni s12" />重新载入</button>
      <button class="btn sm primary" @click="saveEditor"><Icon name="save" class="ni s12" />保存</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, fmtB, fmtTime, fmtFull, uploadFile, pollTask } from '../api'
import { toast } from '../store'
import Panel from '../components/Panel.vue'
import Sheet from '../components/Sheet.vue'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'

const HOME = '/data/data/com.termux/files/home'
const cwd = ref(HOME)
const entries = ref([])
const sel = ref([])
const clip = ref(null)
const fileInput = ref(null)

const searchOpen = ref(false)
const query = ref('')
const sub = ref(true)
const withContent = ref(true)
const suffix = ref('')
const searching = ref(false)
const done = ref(false)
const hits = ref([])
const truncated = ref(false)

const sheet = ref(null)
const panel = ref(null)
const editing = ref(null)
const up = ref({ show: false, name: '', pct: 0, sent: 0, total: 0 })
const abort = ref(false)

const trash = ref({ items: [], count: 0, total_size: 0, keep_days: 7 })
const keepDays = ref(7)
const verKeep = ref(10)
const chmodMode = ref('644')
const chmodRec = ref(false)
const chmodTarget = ref('')
const hours = ref(24)
const needCode = ref(true)
const sharePath = ref('')
const shareName = ref('')
const shareResult = ref(null)
const ask = ref({ show: false, title: '', value: '', hint: '', cb: null })
const remoteUrl = ref('')
const remoteName = ref('')

const crumbs = computed(() => {
  const rel = cwd.value.startsWith(HOME) ? cwd.value.slice(HOME.length) : cwd.value
  const parts = rel.split('/').filter(Boolean)
  let acc = HOME
  return parts.map(p => { acc += '/' + p; return { name: p, path: acc } })
})
const join = (n) => cwd.value.replace(/\/$/, '') + '/' + n

function fileIcon(f) {
  const n = (f.name || '').toLowerCase()
  if (/\.(zip|tar|gz|tgz|xz|7z|rar)$/.test(n)) return 'archive'
  if (/\.(sh|bash|zsh|fish)$/.test(n)) return 'terminal'
  if (/\.(js|json|go|py|php|css|html|yml|yaml|toml|conf|ini|md|txt|log)$/.test(n)) return 'code'
  if (/\.(png|jpg|jpeg|gif|webp|svg)$/.test(n)) return 'eye'
  return 'file'
}
const panelIcon = computed(() => ({
  trash: 'trash', versions: 'history', vread: 'file', share: 'share',
  shares: 'share', favs: 'bookmark', stat: 'info', chmod: 'shield',
  remote: 'download', fsettings: 'settings'
}[panel.value && panel.value.type] || 'folder'))

async function load(p) {
  try {
    const d = await api('/api/files?path=' + encodeURIComponent(p || ''))
    cwd.value = d.path
    entries.value = d.entries
    sel.value = []
    searching.value = false
    try { localStorage.setItem('pp_term_cwd', d.path) } catch { /* ignore */ }
  } catch (e) { toast(e.message, 'err') }
}

function toggleSel(n) {
  const i = sel.value.indexOf(n)
  if (i >= 0) sel.value.splice(i, 1); else sel.value.push(n)
}

const NEW_ITEMS = [
  { icon: 'folder', label: '文件夹', act: 'mkdir' },
  { icon: 'file', label: '空文件', act: 'touch' },
  { icon: 'download', label: '远程下载', act: 'remote' }
]
const MORE_ITEMS = [
  { icon: 'trash', label: '回收站', act: 'trash' },
  { icon: 'share', label: '我的分享', act: 'shares' },
  { icon: 'bookmark', label: '收藏夹', act: 'favs' },
  { icon: 'star', label: '收藏当前目录', act: 'fav' },
  { icon: 'settings', label: '文件页设置', act: 'fsettings' }
]

function openAsk(title, value, hint, cb) {
  ask.value = { show: true, title, value, hint, cb }
}
function submitAsk() {
  const v = (ask.value.value || '').trim()
  if (!v) { toast('名称不能为空', 'err'); return }
  const cb = ask.value.cb
  ask.value.show = false
  if (cb) cb(v)
}

function onSheetPick(it) {
  const mode = sheet.value && sheet.value.mode
  sheet.value = null
  if (mode === 'file') { onMenuPick(it); return }
  if (mode === 'pick') { doArchive(it.paths, it.act === 'targz' ? 'tar.gz' : 'zip'); return }
  if (it.act === 'mkdir' || it.act === 'touch') {
    const mk = it.act === 'mkdir'
    openAsk(mk ? '新建文件夹' : '新建文件', '', mk ? '文件夹名称' : '例如 config.json', (name) => {
      const url = mk ? '/api/file/mkdir' : '/api/file/touch'
      api(url, { method: 'POST', body: { path: cwd.value, name } })
        .then(r => { load(cwd.value); if (!mk) openEditor(r.path, name) })
        .catch(e => toast(e.message, 'err'))
    })
    return
  }
  if (it.act === 'remote') { panel.value = { type: 'remote', title: '远程下载' }; return }
  if (it.act === 'trash') return openTrash()
  if (it.act === 'shares') return openShares()
  if (it.act === 'favs') return openFavs()
  if (it.act === 'fav') {
    api('/api/favorites/toggle', { method: 'POST', body: { path: cwd.value } })
      .then(r => toast(r.added ? '已加入收藏' : '已取消收藏', 'ok'))
      .catch(e => toast(e.message, 'err'))
    return
  }
  if (it.act === 'fsettings') {
    api('/api/file-settings').then(s => {
      keepDays.value = s.trash_keep_days; verKeep.value = s.version_keep
      panel.value = { type: 'fsettings', title: '文件页设置' }
    })
  }
}

function openMenu(f) {
  const path = join(f.name)
  const isDir = f.is_dir
  const ar = /\.(zip|tar|tar\.gz|tgz|tar\.xz|gz)$/i.test(f.name)
  const items = []
  if (!isDir) items.push({ icon: 'edit', label: '在线编辑', act: 'edit' })
  if (!isDir && ar) items.push({ icon: 'archive', label: '解压到当前目录', act: 'extract' })
  items.push({ icon: 'download', label: '下载', act: 'download' })
  items.push({ icon: 'edit', label: '重命名', act: 'rename' })
  items.push({ icon: 'copy', label: '复制', act: 'copy' })
  items.push({ icon: 'cut', label: '剪切', act: 'cut' })
  items.push({ icon: 'archive', label: '压缩', act: 'zip' })
  if (!isDir) items.push({ icon: 'share', label: '分享链接', act: 'share' })
  items.push({ icon: 'history', label: '历史版本', act: 'versions' })
  items.push({ icon: 'shield', label: '权限', act: 'chmod' })
  items.push({ icon: 'info', label: '属性', act: 'stat' })
  items.push({ icon: 'trash', label: '删除', act: 'del', danger: true })
  items.forEach(it => { it.f = f; it.path = path })
  sheet.value = { title: f.name, items, mode: 'file' }
}

async function onMenuPick(it) {
  const f = it.f
  const path = it.path
  if (it.act === 'edit') return openEditor(path, f.name)
  if (it.act === 'download') {
    const a = document.createElement('a')
    a.href = '/api/file?download=1&path=' + encodeURIComponent(path)
    a.download = ''
    document.body.appendChild(a); a.click(); a.remove()
    return
  }
  if (it.act === 'rename') {
    openAsk('重命名', f.name, '新名称', (n) => {
      if (n === f.name) return
      api('/api/file/rename', { method: 'POST', body: { path, name: n } })
        .then(() => load(cwd.value)).catch(e => toast(e.message, 'err'))
    })
    return
  }
  if (it.act === 'copy' || it.act === 'cut') {
    clip.value = { paths: [path], cut: it.act === 'cut' }
    toast('已' + (it.act === 'cut' ? '剪切' : '复制') + '，进入目标目录后点「粘贴」', 'ok')
    return
  }
  if (it.act === 'zip') return chooseFormat([path])
  if (it.act === 'extract') return doExtract(path)
  if (it.act === 'share') {
    sharePath.value = path; shareName.value = f.name; shareResult.value = null
    panel.value = { type: 'share', title: '分享 · ' + f.name }
    return
  }
  if (it.act === 'versions') {
    const d = await api('/api/versions?path=' + encodeURIComponent(path))
    panel.value = { type: 'versions', title: '历史版本 · ' + f.name, data: d }
    return
  }
  if (it.act === 'chmod') {
    chmodTarget.value = path; chmodMode.value = '644'; chmodRec.value = false
    panel.value = { type: 'chmod', title: '权限 · ' + f.name }
    return
  }
  if (it.act === 'stat') {
    const d = await api('/api/file/stat?path=' + encodeURIComponent(path))
    panel.value = { type: 'stat', title: '属性 · ' + f.name, data: d }
    return
  }
  if (it.act === 'del') {
    if (!confirm(`将「${f.name}」移入回收站？`)) return
    api('/api/file?path=' + encodeURIComponent(path), { method: 'DELETE' })
      .then(r => { toast('已移入回收站：' + f.name, 'ok'); load(cwd.value) })
      .catch(e => toast(e.message, 'err'))
  }
}

/* ---------- 上传 ---------- */
function pickFile() { fileInput.value.value = ''; fileInput.value.click() }
async function onPick(e) {
  const files = Array.from(e.target.files || [])
  if (files.length) await uploadAll(files)
}
async function onDrop(e) {
  const files = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
  if (files.length) await uploadAll(files)
  else toast('暂不支持直接拖入文件夹，请先压缩', 'err')
}
async function uploadAll(files) {
  abort.value = false
  let total = files.reduce((a, f) => a + f.size, 0)
  let doneBytes = 0
  for (let i = 0; i < files.length; i++) {
    const f = files[i]
    up.value = { show: true, name: (files.length > 1 ? `(${i + 1}/${files.length}) ` : '') + f.name, pct: 0, sent: 0, total: f.size }
    try {
      await uploadFile(f, cwd.value, (sent, tot) => {
        up.value.pct = tot ? Math.round(sent / tot * 100) : 0
        up.value.sent = sent
      }, () => abort.value)
      doneBytes += f.size
    } catch (e) { toast(f.name + ' 上传失败：' + e.message, 'err') }
    if (abort.value) break
  }
  up.value.show = false
  if (doneBytes) { toast('上传完成', 'ok'); load(cwd.value) }
}

/* ---------- 批量 ---------- */
async function batch(kind) {
  const paths = sel.value.map(n => join(n))
  if (!paths.length) return
  if (kind === 'copy' || kind === 'cut') {
    clip.value = { paths, cut: kind === 'cut' }
    sel.value = []
    toast('已' + (kind === 'cut' ? '剪切' : '复制') + ` ${paths.length} 项`, 'ok')
    return
  }
  if (kind === 'zip') return chooseFormat(paths)
  if (kind === 'del') {
    if (!confirm(`将 ${paths.length} 项移入回收站？`)) return
    for (const p of paths) {
      try { await api('/api/file?path=' + encodeURIComponent(p), { method: 'DELETE' }) } catch (e) { toast(e.message, 'err') }
    }
    toast('已移入回收站', 'ok'); load(cwd.value)
  }
}
async function paste() {
  try {
    const r = await api('/api/file/copy', { method: 'POST', body: { paths: clip.value.paths, dest: cwd.value, move: clip.value.cut } })
    toast((clip.value.cut ? '已移动 ' : '已复制 ') + r.done + ' 项' + (r.failed.length ? `，失败 ${r.failed.length}` : ''), 'ok')
    clip.value = null; load(cwd.value)
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 搜索 ---------- */
function toggleSearch() {
  searchOpen.value = !searchOpen.value
  if (!searchOpen.value) { searching.value = false; hits.value = [] }
}
async function doSearch() {
  if (!query.value.trim()) { toast('请输入关键字', 'err'); return }
  searching.value = true; done.value = false; hits.value = []
  try {
    const t = await api('/api/search', {
      method: 'POST',
      body: { path: cwd.value, q: query.value, sub: sub.value, suffix: suffix.value,
              content: withContent.value, limit: 200, timeout_ms: 8000 }
    })
    pollTask(t.id, (t2) => {
      done.value = true
      const r = t2.result || {}
      hits.value = r.hits || []
      truncated.value = !!r.truncated
    })
  } catch (e) { done.value = true; toast(e.message, 'err') }
}

/* ---------- 压缩 / 解压 / 下载 ---------- */
function chooseFormat(paths) {
  sheet.value = {
    title: '压缩格式', mode: 'pick',
    items: [
      { icon: 'archive', label: 'ZIP（Windows 可直接打开）', act: 'zip', paths },
      { icon: 'package', label: 'TAR.GZ（体积更小）', act: 'targz', paths }
    ]
  }
}
async function doArchive(paths, fmt) {
  try {
    const t = await api('/api/archive', { method: 'POST', body: { paths, format: fmt } })
    toast('压缩任务已开始', 'ok')
    pollTask(t.id, (t2) => {
      if (t2.status === 'success') { toast('压缩完成', 'ok'); load(cwd.value) }
      else toast('压缩失败：' + (t2.output || '').trim().split('\n').pop(), 'err')
    })
  } catch (e) { toast(e.message, 'err') }
}
async function doExtract(path) {
  try {
    const t = await api('/api/extract', { method: 'POST', body: { path } })
    toast('解压任务已开始', 'ok')
    pollTask(t.id, (t2) => {
      if (t2.status === 'success') { toast('解压完成', 'ok'); load(cwd.value) }
      else toast('解压失败：' + (t2.output || '').trim().split('\n').pop(), 'err')
    })
  } catch (e) { toast(e.message, 'err') }
}
async function startRemote() {
  if (!remoteUrl.value.trim()) { toast('请输入 URL', 'err'); return }
  try {
    const t = await api('/api/download/remote', {
      method: 'POST', body: { url: remoteUrl.value.trim(), dir: cwd.value, name: remoteName.value.trim() }
    })
    panel.value = null
    toast('下载任务已开始', 'ok')
    pollTask(t.id, (t2) => {
      if (t2.status === 'success') { toast('下载完成', 'ok'); load(cwd.value) }
      else if (t2.status === 'failed') toast('下载失败：' + (t2.output || '').trim().split('\n').pop(), 'err')
    })
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 回收站 ---------- */
async function openTrash() {
  const d = await api('/api/trash')
  trash.value = d
  keepDays.value = d.keep_days
  panel.value = { type: 'trash', title: '回收站' }
}
async function restore(id) {
  const r = await api('/api/trash/restore', { method: 'POST', body: { id } })
  if (r.failed && r.failed.length) { toast(r.failed[0], 'err'); return }
  toast('已恢复', 'ok'); panel.value = null; load(cwd.value)
}
async function purge(id) {
  if (!confirm('永久删除？不可恢复。')) return
  await api('/api/trash/purge', { method: 'POST', body: { id } })
  toast('已永久删除', 'ok'); openTrash()
}
async function emptyTrash() {
  if (!confirm('清空回收站？此操作不可恢复。')) return
  await api('/api/trash/purge', { method: 'POST', body: { all: true } })
  toast('回收站已清空', 'ok'); panel.value = null; load(cwd.value)
}
async function saveKeep() {
  await api('/api/file-settings', { method: 'POST', body: { trash_keep_days: keepDays.value, version_keep: verKeep.value } })
  toast('已保存，保留 ' + keepDays.value + ' 天', 'ok')
}

/* ---------- 版本 ---------- */
async function readVersion(id) {
  const d = await api('/api/versions/read?id=' + encodeURIComponent(id))
  panel.value = { type: 'vread', title: '版本内容', data: d.content }
}
async function restoreVersion(id) {
  if (!confirm('用这个版本覆盖当前文件？当前内容会自动存为一份版本。')) return
  await api('/api/versions/restore', { method: 'POST', body: { id } })
  toast('已恢复到该版本', 'ok'); panel.value = null; load(cwd.value)
}

/* ---------- 分享 ---------- */
async function createShare() {
  try {
    const s = await api('/api/shares', { method: 'POST', body: { path: sharePath.value, hours: hours.value, need_code: needCode.value } })
    shareResult.value = { url: location.origin + '/s/' + s.token, code: s.code, expires_at: s.expires_at }
  } catch (e) { toast(e.message, 'err') }
}
async function openShares() {
  panel.value = { type: 'shares', title: '我的分享', data: await api('/api/shares/list') }
}
async function revoke(token) {
  if (!confirm('撤销该分享？链接会立即失效。')) return
  await api('/api/shares/revoke', { method: 'POST', body: { token } })
  toast('已撤销', 'ok'); openShares()
}

/* ---------- 收藏 / 属性 / 权限 ---------- */
async function openFavs() { panel.value = { type: 'favs', title: '收藏夹', data: await api('/api/favorites') } }
async function applyChmod() {
  if (!/^[0-7]{3,4}$/.test(chmodMode.value)) { toast('权限格式错误，应为 755 之类', 'err'); return }
  if (chmodRec.value && !confirm(`确认递归修改「${chmodTarget.value.split('/').pop()}」下所有内容的权限为 ${chmodMode.value}？`)) return
  try {
    const r = await api('/api/file/chmod', { method: 'POST', body: { path: chmodTarget.value, mode: chmodMode.value, recursive: chmodRec.value } })
    toast('已修改 ' + r.changed + ' 项权限', 'ok'); panel.value = null; load(cwd.value)
  } catch (e) { toast(e.message, 'err') }
}
async function saveFileSettings() {
  await api('/api/file-settings', { method: 'POST', body: { trash_keep_days: keepDays.value, version_keep: verKeep.value } })
  toast('已保存', 'ok'); panel.value = null
}

/* ---------- 编辑器 ---------- */
async function openEditor(path, name) {
  try {
    const d = await api('/api/file?path=' + encodeURIComponent(path))
    editing.value = { path: d.path, name, content: d.content }
  } catch (e) { toast('打开失败：' + e.message, 'err') }
}
function closeEditor() {
  if (!confirm('关闭编辑器？未保存的修改会丢失。')) return
  editing.value = null
}
async function saveEditor() {
  try {
    const r = await api('/api/file', { method: 'PUT', body: { path: editing.value.path, content: editing.value.content } })
    toast('已保存 ' + editing.value.name + (r.version ? '（旧内容已存入历史版本）' : ''), 'ok')
  } catch (e) { toast('保存失败：' + e.message, 'err') }
}

function copy(t) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(() => toast('已复制', 'ok')).catch(() => prompt('手动复制', t))
  } else {
    const ta = document.createElement('textarea')
    ta.value = t; document.body.appendChild(ta); ta.select()
    try { document.execCommand('copy'); toast('已复制', 'ok') } catch { prompt('手动复制', t) }
    ta.remove()
  }
}

onMounted(async () => {
  let start = HOME
  try { start = localStorage.getItem('pp_term_cwd') || HOME } catch { /* ignore */ }
  load(start)
})
</script>
