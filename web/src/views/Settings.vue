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
          <input class="input" type="password" v-model="form.old" placeholder="修改前需验证" autocomplete="current-password">
        </div>
        <div class="field">
          <label>新密码</label>
          <input class="input" type="password" v-model="form.pwd" placeholder="留空则不修改，至少 6 位" autocomplete="new-password">
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

    <!-- 面板信息 -->
    <div class="card flush">
      <div class="card-h">
        <Icon name="info" />
        <h3>面板信息</h3>
      </div>
      <div class="card-b tight">
        <div class="kv"><span class="k">版本</span><span class="v mono">{{ ui.version || '0.3.0' }}</span></div>
        <div class="kv"><span class="k">后端</span><span class="v mono">Python {{ py || '—' }}</span></div>
        <div class="kv"><span class="k">前端</span><span class="v mono">Vue 3 + Vite</span></div>
        <div class="kv"><span class="k">当前访问</span><span class="v mono">{{ host }}</span></div>
        <div class="kv"><span class="k">访问端口</span><span class="v mono">{{ port }}</span></div>
        <div class="kv"><span class="k">登录用户</span><span class="v">{{ ui.user }}</span></div>
        <div class="kv"><span class="k">会话有效期</span><span class="v">24 小时</span></div>
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
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { ui, toast, applyTheme, setTermTransport } from '../store'
import Icon from '../components/Icon.vue'

const form = ref({ username: ui.user, old: '', pwd: '' })
const err = ref('')
const busy = ref(false)
const py = ref('')
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
})
</script>
