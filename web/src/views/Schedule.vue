<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="clock" />计划任务</h2>
      <p>{{ enabledCount }}/{{ tasks.length }} 个任务启用 · 支持 Shell、清理日志、访问 URL 等类型</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />刷新</button>
      <button class="btn primary" @click="openCreate"><Icon name="plus" />新建任务</button>
    </div>
  </div>

  <div class="note warn mb12">
    <b>易错点</b>：<code>*/N</code> 以系统分钟刻度为锚点，不是任务创建时刻。14:07 创建「每 5 分钟」，
    首次执行是 14:10 而非 14:12；要固定偏移请手写表达式，例如 <code>17 */6 * * *</code>（每 6 小时的第 17 分）。
    息屏期间错过的触发，恢复后最多补跑最近一次。
  </div>

  <!-- 任务列表 -->
  <div class="card flush mb12">
    <div class="card-h">
      <Icon name="clock" />
      <h3>任务</h3>
      <div class="sub">
        <span class="tag" :class="enabledCount ? 'g' : ''">{{ enabledCount }} 启用</span>
        <span class="tag">{{ tasks.length - enabledCount }} 停用</span>
      </div>
    </div>
    <div class="card-b tight">
      <div v-for="t in tasks" :key="t.id" class="sched-item" :class="{ off: !t.enabled }">
        <div class="s-main">
          <div class="s-t">
            <i class="dot" :class="{ off: !t.enabled, fail: t.last_status && t.last_status !== 'success', busy: t.running }"></i>
            <b>{{ t.name }}</b>
            <span class="tag ty">{{ typeName(t.type) }}</span>
            <span class="tag" :class="t.running ? 'y' : t.last_status === 'success' ? 'g' : t.last_status ? 'r' : ''">
              {{ t.running ? '运行中' : { success: '上次成功', failed: '上次失败', timeout: '上次超时', skipped: '上次跳过' }[t.last_status] || '未执行' }}
            </span>
          </div>
          <div class="s-s">{{ t.human }} <span class="mono dim">· {{ t.expr }}</span></div>
          <div class="s-s dim">
            下次 {{ t.enabled ? fmtNext(t.next_run) : '已停用' }}
            <template v-if="t.last_run">· 上次 {{ fmtFull(t.last_run) }} · 耗时 {{ lastDur(t.id) }}</template>
          </div>
        </div>
        <div class="s-ops">
          <button class="btn xs" :disabled="t.running" @click="runTask(t)"><Icon name="play" />运行</button>
          <button class="btn xs" @click="showRuns(t)"><Icon name="history" />历史</button>
          <button class="btn xs" @click="editTask(t)"><Icon name="edit" />编辑</button>
          <button class="btn xs" @click="toggleTask(t)">{{ t.enabled ? '停用' : '启用' }}</button>
          <button class="btn xs danger" @click="delTask(t)"><Icon name="trash" />删除</button>
        </div>
      </div>
      <div v-if="!tasks.length">
        <EmptyState icon-name="clock" title="还没有计划任务"
                    desc="拨测 URL、清理日志、服务控制、Shell 命令都可以交给它">
          <button class="btn primary" @click="openCreate"><Icon name="plus" />新建任务</button>
        </EmptyState>
      </div>
    </div>
  </div>

  <!-- 执行记录 -->
  <div class="card flush">
    <div class="card-h">
      <Icon name="history" />
      <h3>执行记录</h3>
      <div class="sub"><span class="fs11 dim">最近 {{ runs.length }} 条</span></div>
    </div>
    <div class="card-b tight">
      <div v-for="r in runs" :key="r.id" class="run-item" @click="showRun(r)">
        <i class="dot" :class="{ off: r.status === 'skipped', fail: r.status !== 'success' && r.status !== 'skipped' }"></i>
        <div class="r-main">
          <b>{{ r.task_name }} <span class="tag ty">{{ typeName(r.type) }}</span></b>
          <span class="dim">{{ fmtFull(r.started) }} · {{ { cron: '定时', manual: '手动', catchup: '补跑', reboot: '开机' }[r.trigger] || r.trigger }}
            <template v-if="r.missed > 1"> · 跳过 {{ r.missed - 1 }} 轮</template></span>
        </div>
        <span class="tag" :class="r.status === 'success' ? 'g' : r.status === 'skipped' ? '' : 'r'">
          {{ { success: '成功', failed: '失败', timeout: '超时', skipped: '跳过' }[r.status] || r.status }} {{ r.duration }}s</span>
      </div>
      <div v-if="!runs.length"><EmptyState icon-name="history" title="还没有执行记录" /></div>
    </div>
  </div>

  <!-- 任务表单 -->
  <Modal v-if="form.open" :title="form.id ? '编辑任务' : '新建任务'" icon-name="clock" size="wide"
         @close="form.open = false">
    <div class="field">
      <label>任务名称</label>
      <input class="input" v-model="form.name" placeholder="如：每日备份照片" autocomplete="off">
      <div class="ferr" v-if="form.fieldErrors.name">{{ form.fieldErrors.name }}</div>
    </div>

    <div class="field">
      <label>任务类型</label>
      <select class="input" v-model="form.type" data-field="type">
        <option v-for="t in types" :key="t.type_id" :value="t.type_id">{{ t.name }}</option>
      </select>
      <div class="ferr" v-if="form.fieldErrors.type">{{ form.fieldErrors.type }}</div>
    </div>

    <template v-for="f in curFields" :key="f.name">
      <div class="field" v-if="fieldVisible(f)" :data-field="f.name">
        <label>{{ f.label }}<span v-if="f.required" class="req"> *</span></label>

        <input v-if="f.type === 'string'" class="input" v-model="form.params[f.name]"
               :placeholder="f.placeholder" autocapitalize="off" spellcheck="false">
        <input v-else-if="f.type === 'int'" class="input" v-model.number="form.params[f.name]"
               :placeholder="f.placeholder" inputmode="numeric">
        <label v-else-if="f.type === 'bool'" class="chkline">
          <input type="checkbox" v-model="form.params[f.name]">
          <span>{{ f.hint || '启用' }}</span>
        </label>
        <select v-else-if="f.type === 'enum' || f.type === 'select'" class="input" v-model="form.params[f.name]">
          <option v-for="o in f.options || []" :key="optVal(o)" :value="optVal(o)">{{ optLabel(o) }}</option>
        </select>
        <div v-else-if="f.type === 'multiselect'" class="row wrap" style="gap:6px">
          <button v-for="o in f.options || []" :key="optVal(o)" class="btn sm"
                  :class="{ primary: isSel(f, o) }" @click="toggleMs(f, o)">{{ optLabel(o) }}</button>
        </div>
        <textarea v-else-if="f.type === 'textarea'" class="input" rows="3" v-model="form.params[f.name]"
                  :placeholder="f.placeholder" spellcheck="false"></textarea>

        <div class="hint" v-if="f.hint && f.type !== 'bool'">{{ f.hint }}</div>
        <div class="ferr" v-if="form.fieldErrors[f.name]">{{ form.fieldErrors[f.name] }}</div>
      </div>
    </template>

    <div class="field">
      <label>周期</label>
      <div class="seg" style="width:100%">
        <button style="flex:1" :class="{ on: form.mode === 'easy' }" @click="form.mode = 'easy'">简易</button>
        <button style="flex:1" :class="{ on: form.mode === 'cron' }" @click="form.mode = 'cron'">Cron</button>
      </div>
    </div>

    <template v-if="form.mode === 'easy'">
      <div class="field">
        <select class="input" v-model="form.kind">
          <option value="everyN">每 N 分钟</option>
          <option value="hourly">每 N 小时</option>
          <option value="daily">每天</option>
          <option value="weekly">每周</option>
          <option value="monthly">每月</option>
          <option value="reboot">面板启动时</option>
        </select>
      </div>
      <div class="field" v-if="form.kind === 'everyN'">
        <label>间隔（分钟，1–59，按整刻度对齐）</label>
        <input class="input" v-model.number="form.every" inputmode="numeric">
      </div>
      <div class="row" style="gap:10px;align-items:flex-start" v-else-if="form.kind === 'hourly'">
        <div class="field grow">
          <label>间隔（小时）</label>
          <input class="input" v-model.number="form.every" inputmode="numeric">
        </div>
        <div class="field" style="width:96px;flex:0 0 96px">
          <label>第几分</label>
          <input class="input" v-model.number="form.minute" inputmode="numeric">
        </div>
      </div>
      <div class="field" v-else-if="form.kind === 'daily'">
        <label>时间</label>
        <input class="input" type="time" v-model="form.time">
      </div>
      <template v-else-if="form.kind === 'weekly'">
        <div class="field">
          <label>星期</label>
          <div class="row wrap" style="gap:6px">
            <button v-for="(w, i) in weekNames" :key="i" class="btn sm"
                    :class="{ primary: form.weeks.includes(i) }" @click="toggleWeek(i)">{{ w }}</button>
          </div>
        </div>
        <div class="field"><label>时间</label>
          <input class="input" type="time" v-model="form.time"></div>
      </template>
      <div class="row" style="gap:10px;align-items:flex-start" v-else-if="form.kind === 'monthly'">
        <div class="field" style="width:96px;flex:0 0 96px">
          <label>几号</label>
          <input class="input" v-model.number="form.dom" inputmode="numeric">
        </div>
        <div class="field grow">
          <label>时间</label>
          <input class="input" type="time" v-model="form.time">
        </div>
      </div>
      <p v-else-if="form.kind === 'reboot'" class="hint" style="margin:0 0 14px">
        面板每次启动时执行一次（@reboot），适合拉起自建服务。
      </p>
    </template>

    <div class="field" v-else>
      <label>Cron 表达式（分 时 日 月 周）</label>
      <input class="input mono" v-model="form.expr" placeholder="30 8 * * 1-5" autocapitalize="off" spellcheck="false">
      <div class="hint">支持 * 、*/n、a,b、a-b、a-b/n；日与周同时受限按 Vixie cron 取「或」。</div>
    </div>

    <div class="sc-preview" :class="{ bad: !previewObj.valid }">
      <b>{{ previewObj.human }}</b>
      <div v-if="previewObj.err" class="dim">{{ previewObj.err }}</div>
      <div v-for="(n, i) in previewObj.next" :key="i" class="dim">第 {{ i + 1 }} 次：{{ fmtNext(n) }}</div>
      <div v-if="previewObj.valid && !previewObj.next.length" class="dim">没有可触发的日期</div>
    </div>

    <div class="row" style="gap:10px;align-items:flex-start" v-if="form.type === 'shell'">
      <div class="field" style="width:130px;flex:0 0 130px">
        <label>超时（秒）</label>
        <input class="input" v-model.number="form.timeout" inputmode="numeric">
      </div>
      <div class="field grow">
        <label>错过触发时</label>
        <div class="seg" style="width:100%">
          <button style="flex:1" :class="{ on: form.miss === 'run' }" @click="form.miss = 'run'">补跑最近一次</button>
          <button style="flex:1" :class="{ on: form.miss === 'skip' }" @click="form.miss = 'skip'">跳过</button>
        </div>
      </div>
    </div>
    <div class="field" v-else>
      <label>错过触发时</label>
      <div class="seg" style="width:100%">
        <button style="flex:1" :class="{ on: form.miss === 'run' }" @click="form.miss = 'run'">补跑最近一次</button>
        <button style="flex:1" :class="{ on: form.miss === 'skip' }" @click="form.miss = 'skip'">跳过</button>
      </div>
    </div>
    <div class="err" style="margin-top:4px">{{ form.err }}</div>

    <template #footer>
      <button v-if="curType && curType.supports_dry_run" class="btn" :disabled="form.busy"
              @click="doDryRun"><Icon name="eye" />预览</button>
      <button class="btn ghost" @click="form.open = false">取消</button>
      <button class="btn primary" :disabled="form.busy" @click="submit">
        <span v-if="form.busy" class="spinner"></span>{{ form.busy ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>

  <!-- 执行结果 -->
  <Modal v-if="runView" :title="runView.task_name + ' · 执行结果'" icon-name="activity" @close="runView = null">
    <div class="hint mb12">
      {{ fmtFull(runView.started) }} · {{ runView.trigger }} · {{ runView.status }} · 退出码
      {{ runView.exit == null ? '—' : runView.exit }} · {{ runView.duration }}s
    </div>

    <template v-if="runView.type === 'log_clean' && runView.result">
      <div class="row wrap mb12" style="gap:6px">
        <span class="tag" :class="runView.result.dry_run ? 'y' : 'g'">
          {{ runView.result.dry_run ? 'DRY-RUN 预览' : '已实际执行' }}</span>
        <span v-if="runView.result.truncated" class="tag r">达到单次上限</span>
        <span class="dim fs12">
          共 {{ runView.result.total.files }} 项 / {{ fmtSize(runView.result.total.size) }}
          <template v-if="!runView.result.dry_run"> · 释放 {{ fmtSize(runView.result.total.freed) }}</template></span>
      </div>
      <div v-for="(v, k) in runView.result.targets" :key="k" class="lc-row">
        <b>{{ targetLabel(k) }}</b>
        <span class="dim">{{ v.files }} 项 · {{ fmtSize(v.size) }}<template v-if="v.note"> · {{ v.note }}</template></span>
        <div v-for="(e, i) in v.errors" :key="i" class="ferr" style="margin-top:3px">
          {{ e.path.split('/').pop() }}：{{ e.reason }}
        </div>
      </div>
    </template>

    <template v-else-if="runView.type === 'url_visit' && runView.result">
      <div class="row wrap mb12" style="gap:6px">
        <span class="tag" :class="runView.result.ok ? 'g' : 'r'">{{ runView.result.ok ? '成功' : '失败' }}</span>
        <span v-if="runView.result.status" class="tag ty">HTTP {{ runView.result.status }}</span>
        <span class="dim fs12">{{ runView.result.duration_ms }}ms ·
          {{ fmtSize(runView.result.size) }} · {{ runView.result.redirects || 0 }} 次重定向
          <template v-if="runView.result.retries_used"> · 重试 {{ runView.result.retries_used }} 次</template></span>
      </div>
      <div v-if="runView.result.fail" class="ferr mb12">{{ runView.result.fail.message }}</div>
      <div class="kv" v-if="runView.result.final_url"><span class="k">最终 URL</span>
        <span class="v mono" style="word-break:break-all">{{ runView.result.final_url }}</span></div>
      <details v-if="runView.result.body_snippet" class="resp-details">
        <summary>响应摘要（前 2KB）</summary>
        <pre class="code" style="max-height:240px;margin-top:6px">{{ runView.result.body_snippet }}</pre>
      </details>
    </template>

    <pre v-else class="code" style="max-height:340px">{{ runView.output || '(无输出)' }}</pre>
  </Modal>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { api, fmtFull } from '../api'
import { toast } from '../store'
import { buildExpr, fmtNext } from '../cron'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'
import EmptyState from '../components/EmptyState.vue'

const types = ref([])
const tasks = ref([])
const runs = ref([])
const weekNames = ['日', '一', '二', '三', '四', '五', '六']
const runView = ref(null)
let timer = 0
let previewTimer = 0

const enabledCount = computed(() => tasks.value.filter(t => t.enabled).length)
const curType = computed(() => types.value.find(t => t.type_id === form.type))
const curFields = computed(() => (curType.value && curType.value.fields) || [])

const form = reactive({
  open: false, busy: false, err: '', id: '', fieldErrors: {},
  name: '', type: 'shell', params: {},
  mode: 'easy', kind: 'daily',
  every: 15, minute: 0, time: '08:30', weeks: [1, 2, 3, 4, 5], dom: 1,
  expr: '', timeout: 600, miss: 'run'
})

const effectiveExpr = computed(() => {
  if (form.mode === 'cron') return form.expr.trim()
  if (form.kind === 'everyN') return buildExpr('everyN', { every: form.every })
  if (form.kind === 'hourly') return buildExpr('hourly', { every: form.every, minute: form.minute })
  const [h, m] = (form.time || '08:30').split(':')
  return buildExpr(form.kind, {
    minute: Number(m || 0), hour: Number(h || 0),
    weeks: form.weeks, dom: form.dom, expr: form.expr
  })
})

const previewObj = ref({ human: '填写周期后自动生成', next: [], valid: true, err: '' })

const optVal = (o) => (typeof o === 'object' && o !== null ? o.value : o)
const optLabel = (o) => (typeof o === 'object' && o !== null ? o.label : o)
const typeName = (id) => (types.value.find(t => t.type_id === id) || {}).name || id
const targetLabel = (k) => ({ panel_logs: '面板日志', task_logs: '执行历史', site_logs: '站点日志',
  system_logs: '系统日志', backups: '旧备份', pkg_cache: 'pkg/pip 缓存' }[k] || k)

function fmtSize(n) {
  n = n || 0
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB'
  if (n >= 1048576) return (n / 1048576).toFixed(2) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

function fieldVisible(f) {
  const sw = f.show_when
  if (!sw) return true
  const v = form.params[sw.field]
  if (sw.in) return Array.isArray(v) ? v.some(x => sw.in.includes(x)) : sw.in.includes(v)
  if (sw.not_in) return Array.isArray(v) ? !v.every(x => sw.not_in.includes(v)) : !sw.not_in.includes(v)
  return true
}

function isSel(f, o) {
  const cur = form.params[f.name] || []
  return cur.includes(optVal(o))
}

function toggleMs(f, o) {
  const v = optVal(o)
  const cur = Array.isArray(form.params[f.name]) ? [...form.params[f.name]] : []
  const at = cur.indexOf(v)
  if (at >= 0) cur.splice(at, 1)
  else cur.push(v)
  form.params[f.name] = cur
}

function applyDefaults(t) {
  const p = {}
  for (const f of t.fields) {
    if (f.default !== undefined) p[f.name] = JSON.parse(JSON.stringify(f.default))
    else if (f.type === 'multiselect') p[f.name] = []
    else if (f.type === 'bool') p[f.name] = false
    else p[f.name] = ''
  }
  return p
}

function lastDur(taskId) {
  const r = runs.value.find(x => x.task_id === taskId && x.status !== 'skipped')
  return r ? r.duration + 's' : '—'
}

async function load() {
  try {
    const d = await api('/api/schedule')
    tasks.value = d.tasks || []
    runs.value = d.runs || []
  } catch (e) { toast(e.message, 'err') }
}

async function loadTypes() {
  try {
    const d = await api('/api/tasktypes')
    types.value = d.types || []
    if (!form.params || !Object.keys(form.params).length) {
      const def = types.value.find(t => t.type_id === form.type)
      if (def) form.params = applyDefaults(def)
    }
  } catch { /* 静默：老后端无此接口 */ }
}

watch([effectiveExpr, () => form.type, () => form.params], () => {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, 500)
}, { deep: true })

async function refreshPreview() {
  if (!form.open) return
  try {
    previewObj.value = await api('/api/schedule/preview', {
      method: 'POST',
      body: { expr: effectiveExpr.value, type: form.type, params: form.params }
    })
  } catch { /* 后端不可达时保留上次结果 */ }
}

function openCreate() {
  Object.assign(form, {
    open: true, busy: false, err: '', id: '', fieldErrors: {},
    name: '', type: 'shell', params: {},
    mode: 'easy', kind: 'daily', every: 15, minute: 0, time: '08:30',
    weeks: [1, 2, 3, 4, 5], dom: 1, expr: '', timeout: 600, miss: 'run'
  })
  const def = types.value.find(t => t.type_id === 'shell')
  if (def) form.params = applyDefaults(def)
  refreshPreview()
}

function editTask(t) {
  Object.assign(form, {
    open: true, busy: false, err: '', id: t.id, fieldErrors: {},
    name: t.name, type: t.type, params: JSON.parse(JSON.stringify(t.params || {})),
    mode: 'cron', kind: 'daily', every: 15, minute: 0, time: '08:30',
    weeks: [1], dom: 1, expr: t.expr, timeout: t.timeout, miss: t.miss
  })
  refreshPreview()
}

watch(() => form.type, (tid) => {
  if (form.id) return
  const def = types.value.find(x => x.type_id === tid)
  if (def) form.params = applyDefaults(def)
})

function toggleWeek(i) {
  const at = form.weeks.indexOf(i)
  if (at >= 0) form.weeks.splice(at, 1)
  else form.weeks.push(i)
}

async function submit() {
  form.err = ''
  form.fieldErrors = {}
  if (!form.name.trim()) { form.err = '请填写任务名称'; form.fieldErrors.name = '任务名称不能为空'; return }
  if (!previewObj.value.valid) { form.err = '周期表达式无效：' + previewObj.value.err; return }
  form.busy = true
  try {
    const body = {
      name: form.name.trim(), type: form.type, params: form.params,
      expr: effectiveExpr.value, timeout: form.timeout || 600, miss: form.miss
    }
    if (form.id) await api('/api/schedule/' + form.id, { method: 'POST', body })
    else await api('/api/schedule', { method: 'POST', body })
    form.open = false
    toast('已保存', 'ok')
    load()
  } catch (e) {
    form.err = e.message
    if (e.fieldErrors) form.fieldErrors = e.fieldErrors
  } finally { form.busy = false }
}

async function doDryRun() {
  form.busy = true
  form.fieldErrors = {}
  try {
    const r = await api(`/api/tasktype/${form.type}/dryrun`, {
      method: 'POST', body: { params: form.params }
    })
    runView.value = { task_name: (form.name || '预览') + '（未保存的参数）',
      started: Date.now() / 1000, trigger: 'dry-run',
      status: r.ok ? 'success' : 'failed', exit: null,
      duration: '—', output: '', type: form.type, result: r }
  } catch (e) {
    form.err = e.message
    if (e.fieldErrors) form.fieldErrors = e.fieldErrors
  } finally { form.busy = false }
}

async function runTask(t) {
  try {
    await api(`/api/schedule/${t.id}/run`, { method: 'POST' })
    toast('已触发，稍后看执行记录', 'ok')
    setTimeout(load, 900)
    setTimeout(load, 2800)
  } catch (e) { toast(e.message, 'err') }
}

async function toggleTask(t) {
  try {
    await api(`/api/schedule/${t.id}/toggle`, { method: 'POST' })
    load()
  } catch (e) { toast(e.message, 'err') }
}

async function delTask(t) {
  if (!confirm(`删除「${t.name}」？执行记录会保留`)) return
  try {
    await api(`/api/schedule/${t.id}/delete`, { method: 'POST' })
    toast('已删除', 'ok')
    load()
  } catch (e) { toast(e.message, 'err') }
}

function showRuns(t) {
  api(`/api/schedule/${t.id}/runs`)
    .then(d => {
      if (!d.runs.length) { toast('该任务还没有执行记录'); return }
      runView.value = d.runs[0]
    })
    .catch(e => toast(e.message, 'err'))
}

function showRun(r) { runView.value = r }

onMounted(() => {
  load()
  loadTypes()
  timer = setInterval(load, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>
