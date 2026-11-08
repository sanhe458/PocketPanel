<template>
  <div v-if="!d" class="grid g4">
    <div v-for="i in 4" :key="i" class="stat">
      <div class="skel" style="height:12px;width:56px"></div>
      <div class="skel" style="height:26px;width:76px;margin-top:12px"></div>
      <div class="skel" style="height:5px;margin-top:14px"></div>
    </div>
  </div>

  <template v-else>
    <!-- 资源指标 -->
    <div class="grid g4" style="margin-bottom:12px">
      <StatCard label="CPU 占用" :value="d.cpu.toFixed(1)" unit="%" :sub="`${d.cores} 核 · 3 秒采样`"
                :pct="d.cpu" icon-name="cpu" :tone="d.cpu > 75 ? 'danger' : d.cpu > 50 ? 'warn' : 'brand'" />
      <StatCard label="内存占用" :value="(d.mem_used / 1073741824).toFixed(1)" unit="GB"
                :sub="`共 ${(d.mem_total / 1073741824).toFixed(1)} GB · ${pct(memP)}%`"
                :pct="memP" icon-name="memory" :tone="memP > 85 ? 'danger' : 'brand'" />
      <StatCard label="存储占用" :value="(d.disk_used / 1073741824).toFixed(1)" unit="GB"
                :sub="`共 ${(d.disk_total / 1073741824).toFixed(0)} GB · ${pct(diskP)}%`"
                :pct="diskP" icon-name="disk" :tone="diskP > 85 ? 'warn' : 'ok'" />
      <StatCard label="电池电量" :value="d.battery" unit="%"
                :sub="`${d.charging ? '充电中' : '放电'} · ${d.temp.toFixed(1)} °C`"
                :pct="d.battery" icon-name="battery" :tone="d.charging ? 'ok' : d.battery < 20 ? 'danger' : 'warn'" />
    </div>

    <div class="grid g-2-1" style="margin-bottom:12px">
      <!-- 趋势 -->
      <div class="card flush">
        <div class="card-h">
          <Icon name="activity" />
          <h3>资源趋势</h3>
          <div class="sub">
            <div class="seg">
              <button :class="{ on: metric === 'cpu' }" @click="metric = 'cpu'">CPU</button>
              <button :class="{ on: metric === 'mem' }" @click="metric = 'mem'">内存</button>
            </div>
          </div>
        </div>
        <div class="card-b">
          <div style="height:168px"><canvas ref="cv" style="width:100%;height:100%"></canvas></div>
          <div class="row between fs11 dim" style="margin-top:8px">
            <span>0%</span><span>最近 {{ series.length }} 次 · 每 3 秒刷新</span><span>{{ axis }}%</span>
          </div>
        </div>
      </div>

      <!-- 快捷操作 -->
      <div class="card flush">
        <div class="card-h">
          <Icon name="grid" />
          <h3>快捷操作</h3>
        </div>
        <div class="card-b">
          <div class="quick">
            <div class="quick-i" @click="go('terminal')"><div class="qi"><Icon name="terminal" /></div>终端</div>
            <div class="quick-i" @click="go('files')"><div class="qi"><Icon name="folder" /></div>文件</div>
            <div class="quick-i" @click="go('schedule')"><div class="qi ok"><Icon name="clock" /></div>计划任务</div>
            <div class="quick-i" @click="go('services')"><div class="qi purple"><Icon name="server" /></div>服务</div>
            <div class="quick-i" @click="go('logs')"><div class="qi warn"><Icon name="log" /></div>日志</div>
            <div class="quick-i" @click="go('settings')"><div class="qi"><Icon name="settings" /></div>设置</div>
          </div>
        </div>
        <div class="card-f">
          <div class="row between">
            <span class="fs11 dim">面板运行</span>
            <b class="fs12 mono">{{ fmtDur(d.uptime) }}</b>
          </div>
          <div class="row between mt8">
            <span class="fs11 dim">进程</span>
            <b class="fs12 mono">{{ d.running }} 运行 / {{ d.procs }} 总计</b>
          </div>
        </div>
      </div>
    </div>

    <div class="grid g-1-2">
      <!-- 设备信息 -->
      <div class="card flush">
        <div class="card-h">
          <Icon name="cpu" />
          <h3>设备信息</h3>
        </div>
        <div class="card-b tight">
          <div class="kv"><span class="k">型号</span><span class="v">{{ d.model || '—' }}</span></div>
          <div class="kv"><span class="k">系统</span><span class="v">Android {{ d.android || '—' }}</span></div>
          <div class="kv"><span class="k">架构</span><span class="v mono">{{ d.arch }}</span></div>
          <div class="kv"><span class="k">运行时</span><span class="v mono">{{ d.go }}</span></div>
          <div class="kv"><span class="k">可用核心</span><span class="v">{{ d.quota }} / {{ d.cores }}</span></div>
        </div>
      </div>

      <!-- 服务状态 -->
      <div class="card flush">
        <div class="card-h">
          <Icon name="server" />
          <h3>服务状态</h3>
          <div class="sub">
            <span class="tag" :class="svcs.length && runningSvc === svcs.length ? 'g' : 'y'">{{ runningSvc }} / {{ svcs.length }} 运行</span>
          </div>
        </div>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>服务</th><th>状态</th><th>PID</th><th>运行时长</th><th class="r">操作</th></tr></thead>
            <tbody>
              <tr v-for="s in svcs" :key="s.name">
                <td class="tname">{{ s.name }}</td>
                <td>
                  <span class="state" :class="s.status === 'run' ? 'run' : s.status === 'fail' ? 'fail' : 'stop'">
                    <i class="dot" :class="{ off: s.status !== 'run', fail: s.status === 'fail' }"></i>
                    {{ s.status === 'run' ? '运行中' : s.status === 'fail' ? '失败' : '已停止' }}
                  </span>
                </td>
                <td class="mono dim">{{ s.pid || '—' }}</td>
                <td class="dim nowrap">{{ s.uptime || '—' }}</td>
                <td class="r">
                  <div class="tbl-ops">
                    <span v-if="s.name === 'pocketpanel' || s.name === 'pocketpanel-py'" class="tag b">面板自身</span>
                    <template v-else>
                      <button class="btn xs" @click="toggleSvc(s, 'restart')">重启</button>
                      <button class="btn xs" :class="s.status === 'run' ? 'danger' : 'success'"
                              @click="toggleSvc(s, s.status === 'run' ? 'stop' : 'start')">
                        {{ s.status === 'run' ? '停止' : '启动' }}
                      </button>
                    </template>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="!svcs.length" class="tbl-empty">
            <EmptyState icon-name="server" title="没有 runit 服务" desc="未发现 $PREFIX/var/service 目录下的服务" />
          </div>
        </div>
      </div>
    </div>
  </template>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { api, fmtDur } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

const emit = defineEmits(['go'])
const d = ref(null)
const svcs = ref([])
const cpuSeries = ref([])
const memSeries = ref([])
const metric = ref('cpu')
const axis = ref(100)
const cv = ref(null)

const memP = computed(() => (d.value && d.value.mem_total ? d.value.mem_used / d.value.mem_total * 100 : 0))
const diskP = computed(() => (d.value && d.value.disk_total ? d.value.disk_used / d.value.disk_total * 100 : 0))
const runningSvc = computed(() => svcs.value.filter(s => s.status === 'run').length)
const pct = (n) => Math.max(0, Math.min(100, Math.round(n || 0)))
const series = computed(() => metric.value === 'cpu' ? cpuSeries.value : memSeries.value)

const COLORS = {
  cpu: { light: 'rgba(37,99,235,.24)', dark: 'rgba(77,141,255,.35)', lineL: '#2563eb', lineD: '#4d8dff' },
  mem: { light: 'rgba(22,163,74,.22)', dark: 'rgba(49,194,109,.32)', lineL: '#16a34a', lineD: '#31c26d' }
}

function draw() {
  const el = cv.value
  if (!el) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const r = el.getBoundingClientRect()
  const w = Math.max(r.width, 10), h = Math.max(r.height, 10)
  el.width = w * dpr; el.height = h * dpr
  const ctx = el.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)
  const dark = document.documentElement.getAttribute('data-theme') !== 'light'
  const c = COLORS[metric.value]
  ctx.strokeStyle = dark ? '#1e2a3b' : '#e6ecf4'
  ctx.lineWidth = 1
  for (let i = 0; i <= 3; i++) {
    const y = Math.round(h * i / 3) + .5
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
  }
  const arr = series.value
  if (!arr.length) return
  const ax = Math.max(10, Math.ceil(Math.max(...arr) * 1.35 / 10) * 10)
  axis.value = ax
  const px = (i) => (arr.length === 1 ? w - 3 : i / (arr.length - 1) * w)
  const py = (v) => h - Math.min(v, ax) / ax * h
  if (arr.length > 1) {
    const g = ctx.createLinearGradient(0, 0, 0, h)
    g.addColorStop(0, dark ? c.dark : c.light)
    g.addColorStop(1, 'rgba(77,141,255,0)')
    ctx.beginPath(); ctx.moveTo(0, h)
    arr.forEach((v, i) => ctx.lineTo(px(i), py(v)))
    ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = g; ctx.fill()
  }
  ctx.beginPath()
  arr.forEach((v, i) => (i ? ctx.lineTo(px(i), py(v)) : ctx.moveTo(px(i), py(v))))
  ctx.strokeStyle = dark ? c.lineD : c.lineL
  ctx.lineWidth = 2
  ctx.lineJoin = 'round'
  ctx.stroke()
  const last = arr.length - 1
  ctx.beginPath(); ctx.arc(px(last), py(arr[last]), 3, 0, Math.PI * 2)
  ctx.fillStyle = dark ? c.lineD : c.lineL; ctx.fill()
}

async function load() {
  try {
    const r = await api('/api/overview')
    d.value = r
    cpuSeries.value.push(r.cpu)
    memSeries.value.push(memP.value)
    if (cpuSeries.value.length > 60) { cpuSeries.value.shift(); memSeries.value.shift() }
    await nextTick()
    draw()
  } catch { /* 心跳由外壳负责 */ }
}
async function loadSvc() {
  try { svcs.value = await api('/api/services') } catch { /* ignore */ }
}
async function toggleSvc(s, a) {
  try {
    await api(`/api/services/${encodeURIComponent(s.name)}/${a}`, { method: 'POST' })
    toast(`${s.name} ${{ start: '已启动', stop: '已停止', restart: '已重启' }[a]}`, 'ok')
    loadSvc()
  } catch (e) { toast(e.message, 'err') }
}
function go(p) { emit('go', p) }

let t1 = null, t2 = null
onMounted(() => { load(); loadSvc(); t1 = setInterval(load, 3000); t2 = setInterval(loadSvc, 8000) })
onUnmounted(() => { clearInterval(t1); clearInterval(t2) })
</script>
