<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="settings" />设置</h2>
      <p>账户、主题、终端通道与安全选项</p>
    </div>
  </div>

  <div class="grid g2">
    <!-- 账户 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="user" />
        <h3>账户</h3>
        <div class="sub"><span class="fs11">当前登录 {{ ui.user }}</span></div>
      </div>
      <div class="card-b">
        <div class="field">
          <label>用户名</label>
          <input class="input" v-model="form.username" autocapitalize="off" spellcheck="false">
          <div class="hint">2–32 字符，不能包含空格、冒号、斜杠</div>
        </div>
        <div class="field">
          <label>当前密码</label>
          <div class="pwd-wrap">
            <input class="input" :type="showOld ? 'text' : 'password'" v-model="form.old"
                   placeholder="修改前需验证" autocomplete="current-password">
            <button type="button" class="pwd-eye" @click="showOld = !showOld"
                    :aria-label="showOld ? '隐藏密码' : '显示密码'"
                    :title="showOld ? '隐藏密码' : '显示密码'">
              <Icon :name="showOld ? 'eyeOff' : 'eye'" size="s18" />
            </button>
          </div>
        </div>
        <div class="field">
          <label>新密码</label>
          <div class="pwd-wrap">
            <input class="input" :type="showNew ? 'text' : 'password'" v-model="form.pwd"
                   placeholder="留空则不修改，至少 6 位" autocomplete="new-password">
            <button type="button" class="pwd-eye" @click="showNew = !showNew"
                    :aria-label="showNew ? '隐藏密码' : '显示密码'"
                    :title="showNew ? '隐藏密码' : '显示密码'">
              <Icon :name="showNew ? 'eyeOff' : 'eye'" size="s18" />
            </button>
          </div>
        </div>
        <div class="err">{{ err }}</div>
      </div>
      <div class="card-f">
        <button class="btn primary" style="flex:1" :disabled="busy" @click="save">
          <span v-if="busy" class="spinner"></span>保存账户
        </button>
      </div>
    </div>

    <!-- 外观 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="sun" />
        <h3>外观</h3>
      </div>
      <div class="card-b">
        <div class="row between" style="padding:8px 0">
          <div>
            <div class="fs13" style="font-weight:600">主题</div>
            <div class="hint">{{ ui.theme === 'dark' ? '当前为深色' : '当前为浅色' }}</div>
          </div>
          <div class="seg">
            <button :class="{ on: ui.theme === 'dark' }" @click="pickTheme('dark')">深色</button>
            <button :class="{ on: ui.theme === 'light' }" @click="pickTheme('light')">浅色</button>
          </div>
        </div>
        <div class="hr"></div>
        <div class="fs13" style="font-weight:600;margin-bottom:4px">侧栏分组</div>
        <div class="hint">导航按「概览 / 主机 / 文件与终端 / 自动化 / 系统」分组，便于在手机上快速定位。</div>
      </div>
    </div>

    <!-- 终端通道 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="terminal" />
        <h3>终端传输</h3>
      </div>
      <div class="card-b">
        <div class="field">
          <label>通道</label>
          <div class="seg" style="width:100%">
            <button style="flex:1" :class="{ on: ui.termTransport === 'auto' }" @click="pick('auto')">自动</button>
            <button style="flex:1" :class="{ on: ui.termTransport === 'ws' }" @click="pick('ws')">WebSocket</button>
            <button style="flex:1" :class="{ on: ui.termTransport === 'poll' }" @click="pick('poll')">轮询</button>
          </div>
        </div>
        <div class="note">
          <b>自动</b>：优先 WebSocket，握手失败或断线时立刻退回轮询。<br>
          <b>WebSocket</b>：出字即推，只占一条 TCP 连接，适合手机长开。<br>
          <b>轮询</b>：兼容不支持 WebSocket 的浏览器与会吞 Upgrade 的中间代理。
        </div>
        <div class="hint mt8">当前实际走哪条通道，看终端页底部状态栏右侧的 WS / 轮询 标记。</div>
      </div>
    </div>

    <!-- 守护进程 -->
    <div class="card flush" style="grid-column:1/-1">
      <div class="card-h">
        <Icon name="shield" />
        <h3>守护进程</h3>
        <div class="sub">
          <span class="tag" :class="g.running ? (g.healthy ? 'g' : 'y') : 'y'">
            <i class="dot" :style="g.running && g.healthy ? '' : 'background:var(--warn)'"></i>
            {{ g.running ? (g.healthy ? '运行中' : '心跳异常') : '未运行' }}
          </span>
        </div>
      </div>
      <div class="card-b tight">
        <div class="grid g3">
          <div>
            <div class="fs12 dim">最近心跳</div>
            <div class="fs13" style="font-weight:600;margin-top:2px">{{ hbText }}</div>
          </div>
          <div>
            <div class="fs12 dim">已拉起面板</div>
            <div class="fs13" style="font-weight:600;margin-top:2px">{{ g.restarts || 0 }} 次</div>
          </div>
          <div>
            <div class="fs12 dim">巡检间隔</div>
            <div class="fs13" style="font-weight:600;margin-top:2px">{{ g.interval || 15 }} 秒</div>
          </div>
        </div>
        <div class="hint mt8">
          面板与守护进程互相盯着：面板 HTTP 连续无响应由守护进程重启；守护进程心跳断了由面板拉起。
        </div>
        <div class="hint" v-if="g.last_error">最近一次异常：{{ g.last_error }}</div>
        <div class="hint" v-if="g.last_restart">上次拉起方式：{{ g.last_how || '—' }}，{{ fmtTime(g.last_restart) }}</div>
      </div>
      <div class="card-f">
        <button class="btn" style="flex:1" :disabled="gbusy" @click="guardAct('restart')">重启守护</button>
        <button v-if="g.enabled" class="btn danger" style="flex:1" :disabled="gbusy"
                @click="guardAct('stop')">停止守护</button>
        <button v-else class="btn success" style="flex:1" :disabled="gbusy"
                @click="guardAct('start')">启动守护</button>
      </div>
    </div>

    <!-- 面板信息 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="info" />
        <h3>面板信息</h3>
      </div>
      <div class="card-b tight">
        <div class="kv"><span class="k">版本</span><span class="v mono">{{ ui.version || '0.4.0' }}</span></div>
        <div class="kv"><span class="k">后端</span><span class="v mono">Python {{ py || '—' }}</span></div>
        <div class="kv"><span class="k">前端</span><span class="v mono">Vue 3 + Vite</span></div>
        <div class="kv"><span class="k">当前访问</span><span class="v mono">{{ host }}</span></div>
        <div class="kv"><span class="k">访问端口</span><span class="v mono">{{ port }}</span></div>
        <div class="kv"><span class="k">登录用户</span><span class="v">{{ ui.user }}</span></div>
        <div class="kv"><span class="k">会话有效期</span><span class="v">90 天 · 用着自动续期</span></div>
      </div>
      <div class="card-f">
        <button class="btn danger" style="flex:1" @click="logout"><Icon name="logout" />退出登录</button>
      </div>
    </div>

    <!-- 安全 -->
    <div class="card flush" style="grid-column:1/-1">
      <div class="card-h">
        <Icon name="shield" />
        <h3>安全</h3>
      </div>
      <div class="card-b tight">
        <div class="grid g3">
          <div>
            <div class="fs12 dim">密码存储</div>
            <div class="fs13 mono" style="font-weight:600;margin-top:2px">SHA-256 + 随机盐</div>
          </div>
          <div>
            <div class="fs12 dim">传输方式</div>
            <div class="fs13" style="font-weight:600;margin-top:2px">明文 HTTP，仅限局域网</div>
          </div>
          <div>
            <div class="fs12 dim">监听范围</div>
            <div class="fs13" style="font-weight:600;margin-top:2px">局域网 + 已登录会话</div>
          </div>
        </div>
      </div>
      <div class="card-b" style="padding-top:0">
        <div class="note warn">
          <b>公网访问</b>：请在面板前加一层 HTTPS 反向代理，或通过 SSH 隧道访问。不要把 8888 端口直接映射到公网。
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { ui, toast, applyTheme, setTermTransport } from '../store'
import Icon from '../components/Icon.vue'

const form = ref({ username: ui.user, old: '', pwd: '' })
const showOld = ref(false)
const showNew = ref(false)
const err = ref('')
const busy = ref(false)
const py = ref('')
const g = ref({})
const gbusy = ref(false)
let gTimer = 0
const hbText = computed(() => {
  const a = g.value.heartbeat_age
  if (a === null || a === undefined) return '无心跳'
  if (a < 5) return '刚刚'
  if (a < 60) return a + ' 秒前'
  return Math.floor(a / 60) + ' 分钟前'
})
function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString('zh-CN', { hour12: false })
}
async function loadGuard() {
  try { g.value = await api('/api/guard/status') } catch { /* ignore */ }
}
async function guardAct(a) {
  gbusy.value = true
  try {
    await api('/api/guard/' + a, { method: 'POST' })
    toast(a === 'stop' ? '守护进程已停止' : a === 'start' ? '守护进程已启动' : '守护进程已重启', 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally {
    gbusy.value = false
    setTimeout(loadGuard, 700)
  }
}
const host = location.host
const port = location.port || (location.protocol === 'https:' ? '443' : '80')

async function save() {
  err.value = ''
  if (!form.value.old) { err.value = '请输入当前密码'; return }
  if (form.value.pwd && form.value.pwd.length < 6) { err.value = '新密码至少 6 位'; return }
  busy.value = true
  try {
    const d = await api('/api/passwd', {
      method: 'POST',
      body: { old_password: form.value.old, username: form.value.username, password: form.value.pwd }
    })
    ui.user = d.username
    form.value = { username: d.username, old: '', pwd: '' }
    toast('账户已更新', 'ok')
  } catch (e) { err.value = e.message } finally { busy.value = false }
}
function pickTheme(t) { applyTheme(t) }
function pick(t) {
  setTermTransport(t)
  toast(t === 'auto' ? '自动选择通道' : t === 'ws' ? '固定使用 WebSocket' : '固定使用轮询', 'ok')
}
async function logout() {
  try { await api('/api/logout', { method: 'POST' }) } catch { /* ignore */ }
  location.reload()
}
onMounted(async () => {
  try { py.value = (await api('/api/health')).python } catch { /* ignore */ }
  loadGuard()
  gTimer = setInterval(loadGuard, 10000)
})
onUnmounted(() => { clearInterval(gTimer) })
</script>
