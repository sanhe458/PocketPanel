<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="wand" />AI 网关</h2>
      <p>统一管理各家 Key · OpenAI Chat / Responses / Anthropic / Gemini · 同协议零拷贝透传</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="openCfg"><Icon name="settings" />设置</button>
      <button class="btn" :disabled="loading" @click="load(true)">
        <span v-if="loading" class="spinner"></span><Icon v-else name="refresh" />刷新
      </button>
    </div>
  </div>

  <!-- 状态条 -->
  <div class="card gw-bar">
    <span class="gw-dot" :class="{ off: !ov.settings.has_token || !ov.providers.length }"></span>
    <div class="gw-bar-l">
      <b>{{ ov.settings.has_token ? '网关已就绪' : '还没生成访问 Token' }}</b>
      <span class="dim mono">{{ endpoint }}</span>
    </div>
    <div class="gw-bar-r">
      <span class="tag" :class="ov.settings.has_token ? 'ok' : 'y'">
        {{ ov.settings.has_token ? 'Token 已设置' : '未设置 Token' }}
      </span>
      <span class="tag">{{ activeProviders }} 个 Provider</span>
      <span class="tag">{{ ov.models.items.length }} 个模型</span>
    </div>
  </div>

  <!-- 页签 -->
  <div class="gw-tabs">
    <button v-for="t in tabs" :key="t.id" class="gw-tab" :class="{ on: tab === t.id }"
            @click="tab = t.id">
      <Icon :name="t.icon" class="ni s14" />{{ t.label }}
      <span v-if="t.id === 'providers' && ov.providers.length" class="gw-tab-n">{{ ov.providers.length }}</span>
    </button>
  </div>

  <!-- ================= Provider ================= -->
  <template v-if="tab === 'providers'">
    <div v-if="!ov.providers.length" class="card gw-empty">
      <Icon name="wand" class="gw-empty-ico" />
      <b>还没有接入任何 Provider</b>
      <span class="dim">填一个 Base URL 和 Key，网关就会把它变成统一的 /v1 接口。支持 OpenAI、DeepSeek、Anthropic、Gemini、Ollama 等。</span>
      <button class="btn primary" @click="openProvider()"><Icon name="plus" />添加 Provider</button>
    </div>

    <div v-else class="gw-grid">
      <div v-for="p in ov.providers" :key="p.id" class="card gw-card" :class="{ off: !p.enabled }">
        <div class="gw-card-h">
          <span class="gw-logo" :class="{ warn: p.key_error }">{{ initials(p.name) }}</span>
          <div class="gw-card-t">
            <b class="ell">{{ p.name }}</b>
            <span class="dim mono ell">{{ p.id }}</span>
          </div>
          <button class="switch" :class="{ on: p.enabled }" :title="p.enabled ? '已启用' : '已停用'"
                  @click="toggleProvider(p)"></button>
        </div>

        <div class="gw-base mono ell" :title="p.base_url">{{ p.base_url }}</div>

        <div class="gw-chips">
          <span v-for="x in p.protocols" :key="x" class="tag" :class="x === p.default_protocol ? 'g' : ''">
            {{ protoLabel(x) }}<template v-if="x === p.default_protocol"> · 默认</template>
          </span>
        </div>

        <div class="gw-models" v-if="p.models.length">
          <span v-for="m in p.models.slice(0, 3)" :key="m" class="tag mono">
            {{ m === '*' ? '*（任意模型兜底）' : m }}
          </span>
          <span v-if="p.models.length > 3" class="tag">+{{ p.models.length - 3 }}</span>
        </div>
        <div class="gw-models" v-else>
          <span class="tag y">没写模型列表，只能走 provider/model 前缀</span>
        </div>

        <div class="gw-key">
          <Icon name="key" class="ni s13" />
          <span class="mono">{{ p.key_masked || '未设置 Key' }}</span>
          <span v-if="p.key_error" class="gw-key-err" :title="p.key_error">解密失败</span>
        </div>

        <div class="gw-card-a">
          <button class="btn xs" @click="openTest(p)"><Icon name="activity" />测试</button>
          <button class="btn xs" @click="openProvider(p)"><Icon name="edit" />编辑</button>
          <button class="btn xs danger" @click="del.provider = p"><Icon name="trash" /></button>
        </div>
      </div>

      <button class="card gw-add" @click="openProvider()">
        <Icon name="plus" class="ni s22" />
        <b>添加 Provider</b>
        <span class="dim">OpenAI / Anthropic / Gemini / Ollama…</span>
      </button>
    </div>

    <div class="note gw-note" v-if="ov.providers.length">
      <Icon name="info" class="ni s14" />
      <span>
        模型名直接写 <span class="mono">gpt-4o</span> 时会自动找声明过它的 Provider；
        写 <span class="mono">provider/model</span> 可以强制指定。加 <span class="mono">*</span> 表示兜底接收任意模型。
      </span>
    </div>
  </template>

  <!-- ================= 测试对话 ================= -->
  <template v-else-if="tab === 'chat'">
    <div class="gw-chat card">
      <div class="gw-chat-h">
        <Icon name="send" class="ni s15" />
        <input class="input mono grow" v-model.trim="chat.model" list="gw-model-list"
               placeholder="输入或选择模型，如 gpt-4o / fake/gpt-4o" />
        <datalist id="gw-model-list">
          <option v-for="m in ov.models.items" :key="m.route" :value="m.route">
            {{ m.provider_name }} · {{ m.wildcard ? '任意模型（通配兜底）' : m.upstream }}
          </option>
        </datalist>
        <button class="btn sm" :class="{ primary: chat.stream }" @click="chat.stream = !chat.stream">
          {{ chat.stream ? '流式' : '非流式' }}
        </button>
        <button class="btn sm" @click="clearChat"><Icon name="broom" />清空</button>
      </div>

      <div class="gw-sys">
        <input class="input" v-model.trim="chat.system" placeholder="系统提示（可选）" />
        <span class="dim">Token：{{ ov.settings.has_token ? '已配置' : '未配置，先到设置里生成' }}</span>
      </div>

      <div class="gw-msgs" ref="msgsEl">
        <div v-if="!chat.messages.length" class="gw-chat-empty">
          <Icon name="wand" class="gw-empty-ico" />
          <b>发一条消息验证网关</b>
          <span class="dim">这里走真实的 <span class="mono">/v1/chat/completions</span>，会和 LobeChat 等客户端完全一样地经过鉴权、路由和转换。</span>
        </div>
        <div v-for="(m, i) in chat.messages" :key="i" class="gw-msg" :class="m.role">
          <div class="gw-avatar">{{ m.role === 'user' ? '你' : 'AI' }}</div>
          <div class="gw-bubble">
            <div v-if="m.content" class="gw-text">{{ m.content }}</div>
            <div v-else-if="m.streaming" class="gw-typing"><i></i><i></i><i></i></div>
            <div v-for="(t, ti) in (m.tools || [])" :key="ti" class="gw-tool">
              <Icon name="code" class="ni s13" />
              <b>{{ t.name }}</b>
              <span class="mono">{{ t.args || '{}' }}</span>
            </div>
            <div v-if="m.err" class="gw-err">{{ m.err }}</div>
          </div>
        </div>
      </div>

      <div class="gw-compose">
        <textarea class="input gw-ta" v-model="chat.input" rows="2"
                  placeholder="输入消息，Enter 发送，Shift+Enter 换行"
                  @keydown.enter.exact.prevent="sendChat"></textarea>
        <button v-if="chat.busy" class="btn danger gw-send" @click="stopChat">
          <Icon name="stop" />停止
        </button>
        <button v-else class="btn primary gw-send" :disabled="!chat.input.trim() || !chat.model"
                @click="sendChat">
          <Icon name="send" />发送
        </button>
      </div>
      <div class="gw-chat-f">
        <span class="dim" v-if="chat.usage">
          本次用量：输入 {{ chat.usage.prompt_tokens ?? chat.usage.input_tokens ?? 0 }} ·
          输出 {{ chat.usage.completion_tokens ?? chat.usage.output_tokens ?? 0 }}
        </span>
        <span v-else class="dim">同协议直接透传；跨协议走 IR 转换，工具调用也会一起转。</span>
        <span class="dim" v-if="chat.ms">· {{ chat.ms }} ms</span>
      </div>
    </div>
  </template>

  <!-- ================= 用量 ================= -->
  <template v-else>
    <div class="gw-stats">
      <div class="card gw-stat">
        <span class="dim">今日调用</span>
        <b>{{ fmtNum(ov.usage.today.calls) }}</b>
        <span class="dim">成功 {{ fmtNum(ov.usage.today.ok) }} · 失败 {{ fmtNum(ov.usage.today.err) }}</span>
      </div>
      <div class="card gw-stat">
        <span class="dim">今日 Token</span>
        <b>{{ fmtNum(todayTokens) }}</b>
        <span class="dim">输入 {{ fmtNum(ov.usage.today.in_tok) }} · 输出 {{ fmtNum(ov.usage.today.out_tok) }}</span>
      </div>
      <div class="card gw-stat">
        <span class="dim">近 7 天调用</span>
        <b>{{ fmtNum(weekCalls) }}</b>
        <span class="dim">失败 {{ fmtNum(weekErr) }}</span>
      </div>
      <div class="card gw-stat">
        <span class="dim">累计调用</span>
        <b>{{ fmtNum(ov.usage.total.calls) }}</b>
        <span class="dim">累计 Token {{ fmtNum((ov.usage.total.in_tok || 0) + (ov.usage.total.out_tok || 0)) }}</span>
      </div>
    </div>

    <div class="card gw-chart-card">
      <div class="card-h">
        <Icon name="activity" class="ni s16" />
        <b class="grow">近 14 天调用</b>
        <span class="dim">单位：次</span>
      </div>
      <div class="gw-chart">
        <div v-for="d in ov.usage.days" :key="d.day" class="gw-col" :title="`${d.day} · ${d.calls} 次 · ${d.in_tok + d.out_tok} tokens`">
          <span class="gw-col-v">{{ d.calls || '' }}</span>
          <div class="gw-col-bar" :style="{ height: barH(d.calls) }">
            <i class="ok" :style="{ height: okPct(d) }"></i>
          </div>
          <span class="gw-col-d">{{ d.day.slice(5) }}</span>
        </div>
      </div>
    </div>

    <div class="card flush gw-usage-table">
      <div class="card-h"><Icon name="layers" class="ni s16" /><b class="grow">按 Provider 统计（近 14 天）</b></div>
      <div class="gw-tr gw-th">
        <span>Provider</span><span>调用</span><span>成功</span><span>失败</span><span>输入 Token</span><span>输出 Token</span>
      </div>
      <div v-if="!ov.usage.by_provider.length" class="gw-empty" style="padding:34px 16px">
        <span class="dim">还没有调用记录。去「测试对话」发一条消息。</span>
      </div>
      <div v-for="r in ov.usage.by_provider" :key="r.provider" class="gw-tr">
        <span class="mono ell">{{ r.name }}</span>
        <span>{{ fmtNum(r.calls) }}</span>
        <span class="ok-txt">{{ fmtNum(r.ok) }}</span>
        <span :class="{ 'err-txt': r.err }">{{ fmtNum(r.err) }}</span>
        <span>{{ fmtNum(r.in_tok) }}</span>
        <span>{{ fmtNum(r.out_tok) }}</span>
      </div>
    </div>
  </template>

  <!-- Provider 编辑 -->
  <Modal v-if="dlg.open" :title="dlg.editing ? '编辑 Provider' : '添加 Provider'"
         :sub="dlg.editing ? dlg.editing.id : '一个上游服务 = 一份 Key + 一组协议'"
         icon-name="wand" size="wide" @close="dlg.open = false">
    <div class="gw-form">
      <div class="field" v-if="!dlg.editing">
        <label>预设</label>
        <div class="gw-presets">
          <button v-for="p in presets" :key="p.id" class="gw-preset" @click="applyPreset(p)">
            {{ p.name }}
          </button>
        </div>
      </div>
      <div class="gw-form-2">
        <div class="field">
          <label>名称</label>
          <input class="input" v-model.trim="dlg.form.name" placeholder="例如 DeepSeek" />
        </div>
        <div class="field">
          <label>ID <span class="dim">（路由前缀）</span></label>
          <input class="input mono" v-model.trim="dlg.form.id" :disabled="!!dlg.editing"
                 placeholder="留空自动生成" />
        </div>
      </div>
      <div class="field">
        <label>Base URL</label>
        <input class="input mono" v-model.trim="dlg.form.base_url" placeholder="https://api.openai.com/v1" />
        <span class="hint">填到版本号即可。OpenAI 兼容服务一般以 <span class="mono">/v1</span> 结尾，Anthropic 填 <span class="mono">https://api.anthropic.com</span>。</span>
      </div>
      <div class="field">
        <label>API Key</label>
        <input class="input mono" v-model.trim="dlg.form.api_key" type="password"
               :placeholder="dlg.editing && dlg.editing.has_key ? '留空保持不变（' + dlg.editing.key_masked + '）' : 'sk-…'" />
        <label class="gw-clear" v-if="dlg.editing && dlg.editing.has_key">
          <input type="checkbox" v-model="dlg.form.clear_key" /> 清除已保存的 Key
        </label>
        <span class="hint">Key 用 <span class="mono">data/secret.key</span> 加密后存 SQLite，接口只回显掩码。</span>
      </div>

      <div class="field">
        <label>支持的协议</label>
        <div class="gw-prots">
          <label v-for="p in protocolList" :key="p.id" class="gw-prot" :class="{ on: dlg.form.protocols.includes(p.id) }">
            <input type="checkbox" v-model="dlg.form.protocols" :value="p.id" />
            <span>{{ p.label }}</span>
            <small>{{ p.desc }}</small>
          </label>
        </div>
      </div>

      <div class="gw-form-2">
        <div class="field">
          <label>默认格式</label>
          <select class="input" v-model="dlg.form.default_protocol">
            <option v-for="p in selectedProtocols" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
          <span class="hint">跨协议请求会转成这个格式发出去。</span>
        </div>
        <div class="field">
          <label>状态</label>
          <label class="gw-enabled"><button class="switch" :class="{ on: dlg.form.enabled }"
                 @click.prevent="dlg.form.enabled = !dlg.form.enabled"></button>
            {{ dlg.form.enabled ? '启用' : '停用' }}</label>
        </div>
      </div>

      <div class="field">
        <label>模型列表</label>
        <textarea class="input mono gw-models-ta" v-model="dlg.form.models" rows="4"
                  placeholder="gpt-4o&#10;deepseek-chat&#10;*"></textarea>
        <span class="hint">一行一个。支持 <span class="mono">别名=上游模型</span>，例如 <span class="mono">gpt-4o=gpt-4o-2024-11-20</span>；<span class="mono">*</span> 表示兜底接收任意模型。</span>
      </div>

      <div class="field">
        <label>备注（可选）</label>
        <input class="input" v-model.trim="dlg.form.note" placeholder="例：主力账号 / 备用" />
      </div>
      <div v-if="dlg.err" class="gw-err">{{ dlg.err }}</div>
    </div>
    <template #footer>
      <button class="btn" @click="dlg.open = false">取消</button>
      <button class="btn primary" :disabled="dlg.busy" @click="saveProvider">
        <span v-if="dlg.busy" class="spinner"></span>{{ dlg.busy ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>

  <!-- 连通性测试 -->
  <Modal v-if="test.open" :title="'测试 · ' + (test.provider?.name || '')"
         sub="先试 /models，不通再发一个 1 token 的请求" icon-name="activity" @close="test.open = false">
    <div v-if="test.busy" class="gw-testing">
      <span class="spinner"></span><span>正在请求上游…</span>
    </div>
    <template v-else-if="test.result">
      <div class="gw-test-res" :class="test.result.ok ? 'ok' : 'bad'">
        <Icon :name="test.result.ok ? 'success' : 'error'" class="ni s20" />
        <div>
          <b>{{ test.result.ok ? '连通' : '失败' }}</b>
          <div class="dim">
            {{ test.result.ok ? test.result.message : test.result.error }}
          </div>
        </div>
      </div>
      <div class="gw-kv" v-if="test.result.ok">
        <div><span>延迟</span><b>{{ test.result.latency_ms }} ms</b></div>
        <div><span>方式</span><b>{{ test.result.via === 'models' ? 'GET /models' : '最小对话请求' }}</b></div>
        <div><span>测试模型</span><b class="mono">{{ test.result.model }}</b></div>
        <div v-if="test.result.models"><span>模型数</span><b>{{ test.result.models }}</b></div>
      </div>
    </template>
    <div v-else class="dim">点下面的按钮开始测试。</div>
    <template #footer>
      <button class="btn" @click="test.open = false">关闭</button>
      <button class="btn primary" :disabled="test.busy" @click="runTest">
        <span v-if="test.busy" class="spinner"></span>{{ test.busy ? '测试中…' : '重新测试' }}
      </button>
    </template>
  </Modal>

  <!-- 设置 -->
  <Modal v-if="cfg.open" title="网关设置" sub="访问 Token 与请求体上限" icon-name="settings" size="wide"
         @close="cfg.open = false">
    <div class="gw-set">
      <div class="field">
        <label>访问 Token</label>
        <div class="gw-token">
          <input class="input mono" :type="cfg.show ? 'text' : 'password'" :value="cfg.form.access_token" readonly />
          <button class="btn sm" @click="cfg.show = !cfg.show"><Icon name="eye" />{{ cfg.show ? '隐藏' : '显示' }}</button>
          <button class="btn sm" @click="copyToken"><Icon name="copy" />复制</button>
          <button class="btn sm danger" @click="cfg.rotateAsk = true"><Icon name="refresh" />重置</button>
        </div>
        <span class="hint">第三方客户端（LobeChat / Cherry Studio / SDK）调用网关时带这个 Token。不要把网关地址和 Token 一起发到公开群。</span>
      </div>

      <div class="gw-endpoints">
        <div><span>Chat</span><b class="mono">{{ endpoint }}/chat/completions</b></div>
        <div><span>Responses</span><b class="mono">{{ endpoint }}/responses</b></div>
        <div><span>Anthropic</span><b class="mono">{{ endpoint }}/messages</b></div>
        <div><span>Gemini</span><b class="mono">{{ origin }}/v1beta/models/{model}:generateContent</b></div>
      </div>

      <div class="gw-form-2">
        <div class="field">
          <label>单次请求体上限（MB）</label>
          <input class="input mono" type="number" min="1" max="100" v-model.number="cfg.form.max_body_mb" />
        </div>
        <div class="field">
          <label>跨协议转换上限（MB）</label>
          <input class="input mono" type="number" min="1" max="50" v-model.number="cfg.form.cross_max_mb" />
          <span class="hint">跨协议要完整解析 JSON，上限必须小于单次上限。</span>
        </div>
      </div>
      <div class="gw-form-2">
        <div class="field">
          <label>上游读超时（秒）</label>
          <input class="input mono" type="number" min="10" max="3600" v-model.number="cfg.form.upstream_timeout" />
        </div>
        <div class="field">
          <label>兜底 Provider（可选）</label>
          <select class="input" v-model="cfg.form.fallback_provider">
            <option value="">不兜底，匹配不到就 404</option>
            <option v-for="p in ov.providers" :key="p.id" :value="p.id">{{ p.name }}（{{ p.id }}）</option>
          </select>
        </div>
      </div>
      <div class="note">
        <Icon name="info" class="ni s14" />
        <span>前面套了 Nginx 的话，记得加 <span class="mono">client_max_body_size 100M;</span> 和 <span class="mono">proxy_request_buffering off;</span>，否则大图会被 Nginx 先拦或先落盘。</span>
      </div>
      <div v-if="cfg.err" class="gw-err">{{ cfg.err }}</div>
    </div>
    <template #footer>
      <button class="btn" @click="cfg.open = false">取消</button>
      <button class="btn primary" :disabled="cfg.busy" @click="saveCfg">
        <span v-if="cfg.busy" class="spinner"></span>{{ cfg.busy ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>

  <!-- 重置 Token 确认 -->
  <Modal v-if="cfg.rotateAsk" title="重置访问 Token？" sub="旧 Token 会立即失效" icon-name="warning" size="narrow"
         @close="cfg.rotateAsk = false">
    <p class="gw-confirm">已经填了旧 Token 的客户端（LobeChat 等）需要重新填一遍。</p>
    <template #footer>
      <button class="btn" @click="cfg.rotateAsk = false">取消</button>
      <button class="btn danger" :disabled="cfg.rotating" @click="rotateToken">
        <span v-if="cfg.rotating" class="spinner"></span>确认重置
      </button>
    </template>
  </Modal>

  <!-- 删除确认 -->
  <Modal v-if="del.provider" title="删除 Provider？" :sub="del.provider.name" icon-name="trash" size="narrow"
         @close="del.provider = null">
    <p class="gw-confirm">只删配置，不会动上游账号；历史用量统计保留。</p>
    <template #footer>
      <button class="btn" @click="del.provider = null">取消</button>
      <button class="btn danger" :disabled="del.busy" @click="removeProvider">
        <span v-if="del.busy" class="spinner"></span>删除
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

const tabs = [
  { id: 'providers', label: 'Provider', icon: 'wand' },
  { id: 'chat', label: '测试对话', icon: 'send' },
  { id: 'usage', label: '用量', icon: 'activity' },
]
const protocolList = [
  { id: 'openai_chat', label: 'OpenAI Chat', desc: '/v1/chat/completions' },
  { id: 'openai_responses', label: 'OpenAI Responses', desc: '/v1/responses' },
  { id: 'anthropic', label: 'Anthropic', desc: '/v1/messages' },
  { id: 'gemini', label: 'Gemini', desc: '/v1beta/models/…' },
]
const presets = [
  { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1',
    protocols: ['openai_chat', 'openai_responses'], models: 'gpt-4o\ngpt-4o-mini\no3-mini' },
  { id: 'deepseek', name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1',
    protocols: ['openai_chat'], models: 'deepseek-chat\ndeepseek-reasoner' },
  { id: 'anthropic', name: 'Anthropic', base_url: 'https://api.anthropic.com',
    protocols: ['anthropic'], models: 'claude-sonnet-4-20250514\nclaude-3-5-haiku-latest' },
  { id: 'gemini', name: 'Google Gemini', base_url: 'https://generativelanguage.googleapis.com',
    protocols: ['gemini'], models: 'gemini-2.0-flash\ngemini-2.5-pro' },
  { id: 'openrouter', name: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1',
    protocols: ['openai_chat'], models: 'openai/gpt-4o\nanthropic/claude-3.5-sonnet' },
  { id: 'ollama', name: 'Ollama（本机）', base_url: 'http://127.0.0.1:11434/v1',
    protocols: ['openai_chat'], models: 'qwen2.5:7b\nllama3.2' },
]

const tab = ref('providers')
const loading = ref(false)
const msgsEl = ref(null)
const ov = reactive({
  providers: [], settings: {}, models: { items: [], bare: [], prefixed: [] },
  usage: { today: {}, days: [], by_provider: [], total: {} },
})

const dlg = reactive({
  open: false, editing: null, busy: false, err: '',
  form: { name: '', id: '', base_url: '', api_key: '', protocols: ['openai_chat'],
          default_protocol: 'openai_chat', models: '', note: '', enabled: true, clear_key: false },
})
const test = reactive({ open: false, provider: null, busy: false, result: null })
const cfg = reactive({
  open: false, show: false, busy: false, rotating: false, rotateAsk: false, err: '',
  form: { access_token: '', max_body_mb: 50, cross_max_mb: 10, upstream_timeout: 300, fallback_provider: '' },
})
const del = reactive({ provider: null, busy: false })
const chat = reactive({
  model: '', system: '', messages: [], input: '', stream: true,
  busy: false, err: '', usage: null, ms: 0,
})
let ctrl = null

const origin = location.origin
const endpoint = computed(() => location.origin + '/v1')
const activeProviders = computed(() => ov.providers.filter(p => p.enabled).length)
const selectedProtocols = computed(() => protocolList.filter(p => dlg.form.protocols.includes(p.id)))
const todayTokens = computed(() => (ov.usage.today.in_tok || 0) + (ov.usage.today.out_tok || 0))
const weekCalls = computed(() => (ov.usage.days || []).slice(-7).reduce((s, d) => s + d.calls, 0))
const weekErr = computed(() => (ov.usage.days || []).slice(-7).reduce((s, d) => s + d.err, 0))
const maxCalls = computed(() => Math.max(1, ...(ov.usage.days || []).map(d => d.calls)))

const fmtNum = (n) => {
  n = Number(n || 0)
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e4) return (n / 1e4).toFixed(1) + 'w'
  return String(n)
}
const protoLabel = (id) => (protocolList.find(p => p.id === id)?.label || id)
const initials = (name) => (name || '?').trim().slice(0, 1).toUpperCase()

async function load(spin = false) {
  if (spin) loading.value = true
  try {
    const j = await api('/api/ai/overview')
    ov.providers = j.providers || []
    ov.settings = j.settings || {}
    ov.models = j.models || { items: [] }
    ov.usage = j.usage || ov.usage
    if (!chat.model && ov.models.items.length) chat.model = ov.models.items[0].route
    cfg.form.access_token = ov.settings.access_token || ''
  } catch (e) {
    if (spin) toast(e.message, 'err')
  } finally {
    loading.value = false
  }
}

function openProvider(p) {
  dlg.editing = p || null
  dlg.err = ''
  dlg.form = p ? {
    name: p.name, id: p.id, base_url: p.base_url, api_key: '', protocols: [...p.protocols],
    default_protocol: p.default_protocol, models: p.models.join('\n'), note: p.note,
    enabled: p.enabled, clear_key: false,
  } : {
    name: '', id: '', base_url: '', api_key: '', protocols: ['openai_chat'],
    default_protocol: 'openai_chat', models: '', note: '', enabled: true, clear_key: false,
  }
  dlg.open = true
}

function applyPreset(p) {
  dlg.form.name = p.name
  dlg.form.base_url = p.base_url
  dlg.form.protocols = [...p.protocols]
  dlg.form.default_protocol = p.protocols[0]
  dlg.form.models = p.models
}

async function saveProvider() {
  if (!dlg.form.name.trim()) return (dlg.err = '名称不能为空')
  if (!dlg.form.protocols.length) return (dlg.err = '至少选一种协议')
  dlg.busy = true; dlg.err = ''
  try {
    const body = {
      ...dlg.form,
      models: dlg.form.models.split('\n').map(x => x.trim()).filter(Boolean),
      id: dlg.editing ? dlg.editing.id : (dlg.form.id || ''),
    }
    if (!body.api_key && dlg.editing) delete body.api_key
    await api('/api/ai/providers', { method: 'POST', body })
    toast('已保存', 'ok')
    dlg.open = false
    await load()
  } catch (e) { dlg.err = e.message } finally { dlg.busy = false }
}

async function toggleProvider(p) {
  try {
    await api('/api/ai/providers', { method: 'POST', body: { ...p, enabled: !p.enabled, api_key: '' } })
    p.enabled = !p.enabled
  } catch (e) { toast(e.message, 'err') }
}

async function removeProvider() {
  del.busy = true
  try {
    await api(`/api/ai/providers/${encodeURIComponent(del.provider.id)}/delete`, { method: 'POST' })
    toast('已删除', 'ok')
    del.provider = null
    await load()
  } catch (e) { toast(e.message, 'err') } finally { del.busy = false }
}

function openTest(p) {
  test.provider = p; test.result = null; test.open = true; test.busy = false
  runTest()
}

async function runTest() {
  if (!test.provider) return
  test.busy = true; test.result = null
  try {
    test.result = await api(`/api/ai/providers/${encodeURIComponent(test.provider.id)}/test`, { method: 'POST' })
  } catch (e) {
    test.result = { ok: false, error: e.message }
  } finally { test.busy = false }
}

function openCfg() {
  cfg.form = {
    access_token: ov.settings.access_token || '',
    max_body_mb: ov.settings.max_body_mb || 50,
    cross_max_mb: ov.settings.cross_max_mb || 10,
    upstream_timeout: ov.settings.upstream_timeout || 300,
    fallback_provider: ov.settings.fallback_provider || '',
  }
  cfg.err = ''; cfg.show = true; cfg.open = true
}

async function saveCfg() {
  cfg.busy = true; cfg.err = ''
  try {
    await api('/api/ai/settings', { method: 'POST', body: {
      max_body_mb: cfg.form.max_body_mb,
      cross_max_mb: cfg.form.cross_max_mb,
      upstream_timeout: cfg.form.upstream_timeout,
      fallback_provider: cfg.form.fallback_provider,
    } })
    toast('设置已保存', 'ok')
    cfg.open = false
    await load()
  } catch (e) { cfg.err = e.message } finally { cfg.busy = false }
}

async function rotateToken() {
  cfg.rotating = true
  try {
    const j = await api('/api/ai/token', { method: 'POST' })
    cfg.form.access_token = j.access_token
    ov.settings = j
    cfg.rotateAsk = false
    cfg.show = true
    toast('Token 已重置，旧的立即失效', 'ok')
    await load()
    cfg.form.access_token = j.access_token
  } catch (e) { toast(e.message, 'err') } finally { cfg.rotating = false }
}

function copyToken() {
  const t = cfg.form.access_token
  navigator.clipboard.writeText(t).then(() => toast('已复制', 'ok')).catch(() => prompt('手动复制', t))
}

function clearChat() {
  chat.messages = []; chat.usage = null; chat.ms = 0; chat.err = ''
}

function stopChat() {
  if (ctrl) ctrl.abort()
}

async function sendChat() {
  const text = chat.input.trim()
  if (!text || chat.busy || !chat.model) return
  if (!ov.settings.has_token) return toast('先去设置里生成访问 Token', 'err')
  chat.input = ''
  chat.busy = true; chat.err = ''; chat.usage = null; chat.ms = 0
  chat.messages.push({ role: 'user', content: text })
  const reply = reactive({ role: 'assistant', content: '', streaming: true, tools: [], err: '' })
  chat.messages.push(reply)
  await nextTick()
  msgsEl.value && (msgsEl.value.scrollTop = msgsEl.value.scrollHeight)

  const history = []
  if (chat.system) history.push({ role: 'system', content: chat.system })
  for (const m of chat.messages) {
    if (m === reply) continue
    if (m.role === 'user' || (m.role === 'assistant' && m.content)) {
      history.push({ role: m.role, content: m.content })
    }
  }
  const body = { model: chat.model, messages: history, stream: chat.stream }
  const t0 = performance.now()
  ctrl = new AbortController()
  try {
    const res = await fetch('/v1/chat/completions', {
      method: 'POST', signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + ov.settings.access_token,
      },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.error?.message || j.error || `HTTP ${res.status}`)
    }
    if (!chat.stream) {
      const j = await res.json()
      const msg = j.choices?.[0]?.message || {}
      reply.content = msg.content || ''
      reply.tools = (msg.tool_calls || []).map(t => ({ name: t.function?.name, args: t.function?.arguments }))
      chat.usage = j.usage || null
    } else {
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let i
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, i)
          buf = buf.slice(i + 2)
          handleChunk(block, reply)
        }
        if (msgsEl.value) msgsEl.value.scrollTop = msgsEl.value.scrollHeight
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      reply.content = reply.content || '（已停止）'
    } else {
      reply.err = e.message
    }
  } finally {
    reply.streaming = false
    chat.busy = false
    chat.ms = Math.round(performance.now() - t0)
    ctrl = null
    await nextTick()
    if (msgsEl.value) msgsEl.value.scrollTop = msgsEl.value.scrollHeight
  }
}

function handleChunk(block, reply) {
  for (const line of block.split('\n')) {
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (!data || data === '[DONE]') continue
    let j
    try { j = JSON.parse(data) } catch { continue }
    if (j.usage) chat.usage = j.usage
    if (j.error) { reply.err = j.error.message || String(j.error); continue }
    const d = j.choices?.[0]?.delta
    if (!d) continue
    if (d.content) reply.content += d.content
    for (const t of d.tool_calls || []) {
      const idx = t.index || 0
      if (!reply.tools[idx]) reply.tools[idx] = { name: '', args: '' }
      if (t.function?.name) reply.tools[idx].name = t.function.name
      if (t.function?.arguments) reply.tools[idx].args += t.function.arguments
    }
  }
}

function barH(n) {
  return Math.max(3, Math.round((n / maxCalls.value) * 86)) + 'px'
}
function okPct(d) {
  if (!d.calls) return '0%'
  return Math.round((d.ok / d.calls) * 100) + '%'
}

onMounted(load)
</script>
