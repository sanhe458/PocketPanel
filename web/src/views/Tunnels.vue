<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="share" />内网穿透</h2>
      <p>frpc 隧道配置、进程保活与公网拨测 · 面板端口 {{ panelPort }} 已禁止暴露</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />刷新</button>
      <button class="btn primary" @click="openWizard"><Icon name="plus" />新建隧道</button>
    </div>
  </div>

  <!-- 客户端与外部进程 -->
  <div class="grid2" style="margin-bottom:14px">
    <div class="card flush">
      <div class="card-h">
        <Icon name="package" class="ni s16" style="color:var(--brand)" />
        <b class="grow">frpc 客户端</b>
        <span class="tag" :class="cli.found ? 'g' : 'y'">{{ cli.found ? '已安装' : '未安装' }}</span>
      </div>
      <div class="card-b" style="padding:10px 14px">
        <template v-if="cli.found">
          <div class="kv"><span>版本</span><b class="mono">{{ cli.version || '未知' }}</b></div>
          <div class="kv"><span>路径</span><b class="mono ell" style="max-width:220px">{{ cli.path }}</b></div>
          <div class="kv"><span>来源</span><b>{{ install.source === 'pkg' ? 'Termux pkg' : install.source === 'github' ? 'GitHub Release' : '系统' }}</b></div>
          <div class="kv"><span>默认格式</span><b class="mono">{{ (cli.fmt || 'toml').toUpperCase() }}</b></div>
        </template>
        <div v-else class="dim" style="font-size:12px;line-height:1.7">
          未安装 frpc。支持 Termux <span class="mono">pkg</span> 源或
          GitHub Release（自动选架构 + SHA256 校验，国内可走镜像加速）。
          安装后可用「权威校验」与进程托管。
        </div>
      </div>
      <div class="card-f" style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn xs danger" v-if="cli.found" @click="uninstall"><Icon name="trash" />卸载</button>
        <button class="btn xs primary" :disabled="!!install.busy" @click="openInstall">
          <span v-if="install.busy" class="spinner"></span><Icon v-else name="download" />
          {{ install.busy ? '安装中…' : cli.found ? '重装 / 升级' : '一键安装' }}
        </button>
      </div>
    </div>

    <div class="card flush" v-if="external.length">
      <div class="card-h">
        <Icon name="info" class="ni s16" style="color:var(--info)" />
        <b class="grow">检测到的外部穿透进程</b>
        <span class="tag">只读</span>
      </div>
      <div class="card-b" style="padding:8px 10px">
        <div v-for="x in external" :key="x.name" class="ext-row">
          <span class="state" :class="x.running ? 'run' : 'stop'">
            <i class="dot" :class="{ off: !x.running }"></i>{{ x.running ? '运行中' : '已停止' }}
          </span>
          <b class="mono" style="font-size:12px">{{ x.name }}</b>
          <span class="dim ell mono grow" style="font-size:11px">{{ x.cmd }}</span>
          <a v-if="x.webui" class="btn xs" :href="x.webui" target="_blank" rel="noreferrer">
            <Icon name="link" />WebUI
          </a>
        </div>
        <div class="dim" style="font-size:11px;margin-top:6px">
          外部进程由 runit 托管，面板只展示状态；启停请到「服务」页。
        </div>
      </div>
    </div>
  </div>

  <!-- 实例列表 -->
  <DataTable :cols="cols" :rows="list" title="隧道实例" icon-name="share"
             :row-key="i => i.id" empty-text="还没有隧道实例，点右上角「新建隧道」开始">
    <template #toolbar>
      <span class="tag" :class="runCount ? 'g' : 'y'">{{ runCount }} / {{ list.length }} 运行中</span>
    </template>
    <template #c-name="{ row }">
      <div class="tname">{{ row.name }}</div>
      <div class="tsub">
        <span class="tag" :class="row.mode === 'preset' ? 'b' : ''">{{ modeLabel(row.mode) }}</span>
        <span class="mono dim">{{ (row.format || 'toml').toUpperCase() }}</span>
      </div>
    </template>
    <template #c-server="{ row }">
      <span class="mono" style="font-size:12px">{{ row.addr || '—' }}<template v-if="row.port">:{{ row.port }}</template></span>
    </template>
    <template #c-proxies="{ row }">
      <div class="px-chips">
        <span v-for="p in (row.proxies || []).slice(0, 3)" :key="p.name" class="tag mono">{{ p.name }}:{{ p.type }}{{ p.port !== '' && p.port != null ? '@' + p.port : '' }}</span>
        <span v-if="(row.proxies || []).length > 3" class="tag">+{{ row.proxies.length - 3 }}</span>
      </div>
    </template>
    <template #c-status="{ row }">
      <span class="state" :class="row.status === 'running' ? 'run' : row.status === 'failed' ? 'fail' : 'stop'">
        <i class="dot" :class="{ off: row.status !== 'running', fail: row.status === 'failed', busy: row.status === 'running' }"></i>
        {{ { running: '运行中', stopped: '已停止', failed: '失败' }[row.status] }}
      </span>
      <div v-if="row.probe && row.probe.ts" class="tsub" :style="{ color: row.probe.ok === false ? 'var(--danger)' : '' }">
        <Icon :name="row.probe.ok === false ? 'error' : row.probe.ok ? 'success' : 'info'" class="ni s12" />
        {{ row.probe.detail || '未拨测' }}
      </div>
    </template>
    <template #c-ops="{ row }">
      <div class="tbl-ops">
        <button class="btn xs" @click.stop="act(row, 'restart')" v-if="row.status === 'running'"><Icon name="restart" />重启</button>
        <button class="btn xs" :class="row.status === 'running' ? 'danger' : 'success'" @click.stop="act(row, row.status === 'running' ? 'stop' : 'start')">
          <Icon :name="row.status === 'running' ? 'stop' : 'play'" />{{ row.status === 'running' ? '停止' : '启动' }}
        </button>
        <button class="btn xs" @click.stop="openDetail(row)"><Icon name="eye" />详情</button>
      </div>
    </template>
  </DataTable>

  <div class="note" style="margin-top:14px">
    <Icon name="info" class="ni s14" />
    <span>
      保活策略：进程意外退出按 <b>1s→2s→4s…60s</b> 指数退避自动重启，连续 10 次失败标记失败并通知；
      公网拨测每 60 秒一轮，连续 3 次失败进消息中心，恢复后同样通知。
    </span>
  </div>

  <!-- 网络守卫 -->
  <div class="card flush" style="margin-top:14px">
    <div class="card-h">
      <Icon name="shield" class="ni s16" style="color:var(--brand)" />
      <b class="grow">网络守卫</b>
      <span class="tag" :class="ng.enabled ? 'g' : 'y'">{{ ng.enabled ? '已启用' : '已停用' }}</span>
      <span v-if="ng.pending" class="tag y">防抖中 {{ ng.pending.remaining || 0 }}s</span>
      <span v-if="ng.recovering" class="tag b">探测中</span>
    </div>
    <div class="card-b" style="padding:12px 14px">
      <div class="ng-grid">
        <div>
          <div class="kv"><span class="k">当前网络</span>
            <span class="v mono">{{ ng.network?.ip || '无 IP' }}
              <span class="tag" :class="ng.network?.kind === 'wifi' ? 'c' : ng.network?.kind === 'cellular' ? 'p' : 'y'">
                {{ { wifi: 'WiFi', cellular: '蜂窝', none: '未连接' }[ng.network?.kind] || '未知' }}
              </span>
            </span>
          </div>
          <div class="kv"><span class="k">探测间隔</span><span class="v mono">{{ ng.poll_interval }}s</span></div>
          <div class="kv"><span class="k">防抖等待</span><span class="v mono">{{ ng.debounce }}s</span></div>
          <div class="kv"><span class="k">熔断阈值</span><span class="v mono">5 分钟内 > {{ ng.circuit_max }} 次</span></div>
        </div>
        <div>
          <div class="kv"><span class="k">防抖时长</span><span class="v">
            <select class="input xs" v-model.number="debounceInput" @change="saveDebounce" style="width:92px;display:inline-block">
              <option v-for="s in [30,40,45,50,60]" :key="s" :value="s">{{ s }} 秒</option>
            </select>
          </span></div>
          <div class="kv"><span class="k">自动恢复</span><span class="v">
            <button class="btn xs" :class="ng.enabled ? 'danger' : 'success'" @click="toggleEnabled">
              {{ ng.enabled ? '停用' : '启用' }}
            </button>
          </span></div>
          <div class="kv"><span class="k">操作</span><span class="v">
            <button class="btn xs" @click="doRecover" :disabled="recovering">
              <span v-if="recovering" class="spinner"></span><Icon v-else name="refresh" />立即探测
            </button>
            <button class="btn xs" @click="resetCircuit" v-if="(ng.circuit || []).length">
              <Icon name="shield" />解封熔断
            </button>
          </span></div>
        </div>
      </div>

      <!-- 上次恢复结果 -->
      <div v-if="ng.report" style="margin-top:12px">
        <div class="section-title" style="margin-bottom:8px">上次恢复结果</div>
        <div class="ng-items">
          <div v-for="(h, i) in ng.report.healthy" :key="'h' + i" class="ng-item">
            <span class="tag g">正常</span>
            <b class="ell">{{ h.name }}</b>
            <span class="dim ell mono grow">{{ h.detail }}</span>
          </div>
          <div v-for="(h, i) in ng.report.restarted" :key="'r' + i" class="ng-item">
            <span class="tag y">已重启</span>
            <b class="ell">{{ h.name }}</b>
            <span class="dim ell mono grow">{{ h.detail }}</span>
          </div>
          <div v-for="(h, i) in ng.report.failed" :key="'f' + i" class="ng-item">
            <span class="tag r">失败</span>
            <b class="ell">{{ h.name }}</b>
            <span class="dim ell mono grow">{{ h.detail }}</span>
          </div>
          <div v-for="(h, i) in ng.report.skipped" :key="'s' + i" class="ng-item">
            <span class="tag">跳过</span>
            <b class="ell">{{ h.name }}</b>
            <span class="dim ell mono grow">{{ h.reason }}</span>
          </div>
          <div v-if="!ng.report.healthy?.length && !ng.report.restarted?.length &&
                   !ng.report.failed?.length && !ng.report.skipped?.length" class="dim" style="font-size:12px">
            当时没有运行中的隧道实例。
          </div>
        </div>
      </div>

      <!-- 熔断 -->
      <div v-if="(ng.circuit || []).length" style="margin-top:12px">
        <div class="section-title" style="margin-bottom:8px">熔断</div>
        <div class="ng-items">
          <div v-for="c in ng.circuit" :key="c.id" class="ng-item">
            <span class="tag" :class="c.paused ? 'r' : 'y'">{{ c.paused ? '已暂停' : '计数中' }}</span>
            <b class="mono ell grow">{{ c.id }}</b>
            <span class="dim mono">5 分钟内 {{ c.count }} 次</span>
          </div>
        </div>
      </div>

      <!-- 日志 -->
      <details v-if="(ng.log || []).length" style="margin-top:12px">
        <summary class="dim" style="cursor:pointer;font-size:12px;padding:4px 0">探测日志（{{ (ng.log || []).length }} 条）</summary>
        <div class="ng-log mono">
          <div v-for="(l, i) in ng.log" :key="i">{{ l }}</div>
        </div>
      </details>
    </div>
    <div class="card-f" style="display:flex;gap:8px;justify-content:flex-end">
      <span class="dim grow" style="font-size:11.5px;align-self:center">
        检测到网络切换后等待网络稳定，再逐个探测隧道：自己重连上的不动，只重启真正失联的。
      </span>
      <button class="btn xs" @click="loadNg"><Icon name="refresh" />刷新</button>
    </div>
  </div>

  <!-- 新建向导 -->
  <Modal v-if="wiz.open" :title="'新建隧道 · ' + wizTabTitle" icon-name="plus" size="wide" @close="wiz.open = false">
    <div class="seg" style="margin-bottom:14px">
      <button v-for="t in wizTabs" :key="t.id" :class="{ on: wiz.tab === t.id }" @click="wiz.tab = t.id">{{ t.label }}</button>
    </div>

    <!-- 预设 -->
    <template v-if="wiz.tab === 'preset'">
      <div class="note" style="margin-bottom:12px">
        <Icon name="info" class="ni s14" />
        <span>{{ wiz.preset.note || '预填服务器参数，无需申请凭据。' }}</span>
      </div>
      <div class="fgrid">
        <div class="field"><label>实例名称 *</label><input class="input" v-model="wiz.form.name" placeholder="如 frp-ssh"></div>
        <div class="field"><label>类型</label>
          <select class="input" v-model="wiz.form.ptype">
            <option value="tcp">TCP（远程端口）</option>
            <option value="http">HTTP（自有域名）</option>
          </select>
        </div>
        <div class="field"><label>本地端口 *</label><input class="input" type="number" v-model.number="wiz.form.localPort" placeholder="如 8022"></div>
        <div class="field" v-if="wiz.form.ptype === 'tcp'">
          <label>远程端口（10001–50000）</label>
          <input class="input" type="number" v-model.number="wiz.form.remotePort">
          <button class="btn xs" style="margin-top:6px" @click="randPort"><Icon name="wand" />随机</button>
        </div>
        <div class="field" v-else><label>自有域名 *</label><input class="input" v-model="wiz.form.domain" placeholder="app.example.com（需 CNAME 到服务商）"></div>
        <div class="field"><label>本地 IP</label><input class="input" v-model="wiz.form.localIp" placeholder="127.0.0.1"></div>
        <div class="field"><label>拨测地址（可选）</label>
          <input class="input" v-model="wiz.form.probeUrl" :placeholder="wiz.form.ptype === 'tcp' ? 'tcp://服务器IP:' + wiz.form.remotePort : 'http://' + (wiz.form.domain || '域名')">
        </div>
      </div>
    </template>

    <!-- 纯本地 -->
    <template v-else-if="wiz.tab === 'local'">
      <div class="fgrid">
        <div class="field"><label>实例名称 *</label><input class="input" v-model="wiz.form.name"></div>
        <div class="field"><label>FRPS 地址 *</label><input class="input" v-model="wiz.form.serverAddr" placeholder="frps.example.com"></div>
        <div class="field"><label>FRPS 端口 *</label><input class="input" type="number" v-model.number="wiz.form.serverPort" placeholder="7000"></div>
        <div class="field"><label>token</label><input class="input" v-model="wiz.form.token" placeholder="服务端 token"></div>
        <div class="field"><label>代理类型</label>
          <select class="input" v-model="wiz.form.ptype">
            <option value="tcp">TCP</option><option value="udp">UDP</option>
            <option value="http">HTTP</option><option value="https">HTTPS</option>
          </select>
        </div>
        <div class="field"><label>本地端口 *</label><input class="input" type="number" v-model.number="wiz.form.localPort"></div>
        <div class="field" v-if="['tcp', 'udp'].includes(wiz.form.ptype)"><label>远程端口 *</label><input class="input" type="number" v-model.number="wiz.form.remotePort"></div>
        <div class="field" v-else><label>域名 / 子域名 *</label><input class="input" v-model="wiz.form.domain" placeholder="app.example.com"></div>
        <div class="field"><label>拨测地址（可选）</label><input class="input" v-model="wiz.form.probeUrl" placeholder="tcp://公网IP:远程端口 或 https://域名"></div>
      </div>
    </template>

    <!-- 导入 -->
    <template v-else>
      <div class="field">
        <label>粘贴 frpc 配置（TOML / JSON / INI 自动识别）*</label>
        <textarea class="input mono" rows="10" v-model="wiz.form.raw" spellcheck="false"
                  placeholder='serverAddr = "frps.example.com"&#10;serverPort = 7000&#10;&#10;[auth]&#10;token = "..."&#10;&#10;[[proxies]]&#10;name = "ssh"&#10;type = "tcp"&#10;local_port = 22&#10;remote_port = 6022'></textarea>
      </div>
      <div class="field"><label>实例名称 *</label><input class="input" v-model="wiz.form.name"></div>
      <button class="btn" :disabled="wiz.parsing" @click="doParse" style="margin-bottom:10px">
        <span v-if="wiz.parsing" class="spinner"></span><Icon v-else name="shield" />校验配置
      </button>
      <div v-if="wiz.parseResult" class="parse-result">
        <div v-if="wiz.parseResult.ok" class="note ok" style="margin-bottom:8px">
          <Icon name="success" class="ni s14" />
          <span>校验通过：{{ wiz.parseResult.format.toUpperCase() }} · {{ (wiz.parseResult.structured?.proxies || []).length }} 个代理
            <template v-if="wiz.parseResult.verify?.skipped">（未装 frpc，已跳过权威校验）</template>
          </span>
        </div>
        <div v-else class="note err" style="margin-bottom:8px">
          <Icon name="error" class="ni s14" />
          <span>{{ wiz.parseResult.error || '校验未通过' }}</span>
        </div>
        <div v-for="(e, i) in wiz.parseResult.errors || []" :key="i" class="note err" style="margin-bottom:6px">
          <Icon name="error" class="ni s14" />
          <span><b class="mono">{{ e.field }}</b>：{{ e.msg }}</span>
        </div>
        <div v-if="wiz.parseResult.verify && !wiz.parseResult.verify.ok && !wiz.parseResult.verify.skipped" class="note err">
          <Icon name="error" class="ni s14" /><span class="mono" style="white-space:pre-wrap">{{ wiz.parseResult.verify.output }}</span>
        </div>
      </div>
    </template>

    <template #footer>
      <button class="btn" @click="wiz.open = false">取消</button>
      <button class="btn primary" :disabled="wiz.saving" @click="doCreate">
        <span v-if="wiz.saving" class="spinner"></span><Icon v-else name="save" />创建并启动
      </button>
    </template>
  </Modal>

  <!-- frpc 安装 -->
  <Modal v-if="inst.open" title="frpc 一键安装" icon-name="download" size="wide" @close="inst.open = false">
    <div class="fgrid" style="margin-bottom:12px">
      <div class="field"><label>架构</label><input class="input mono" :value="install.arch" disabled></div>
      <div class="field"><label>安装位置</label><input class="input mono ell" :value="install.install_path" disabled></div>
    </div>

    <div class="seg" style="margin-bottom:14px">
      <button :class="{ on: inst.source === 'github' }" @click="inst.source = 'github'">GitHub Release</button>
      <button :class="{ on: inst.source === 'pkg' }" @click="inst.source = 'pkg'">Termux pkg</button>
    </div>

    <template v-if="inst.source === 'github'">
      <div class="note" style="margin-bottom:12px">
        <Icon name="shield" class="ni s14" />
        <span>自动按架构（{{ install.arch }}）下载官方预编译包并做 <b>SHA256 校验</b>；国内直连慢时用镜像加速，可测速选最快。</span>
      </div>
      <div class="fgrid">
        <div class="field">
          <label>版本 *</label>
          <div style="display:flex;gap:6px">
            <input class="input mono" v-model="inst.version" placeholder="如 0.61.0">
            <button class="btn xs" :disabled="inst.fetching" @click="fetchLatest" style="flex:0 0 auto">
              <span v-if="inst.fetching" class="spinner"></span><Icon v-else name="download" />最新
            </button>
          </div>
        </div>
        <div class="field">
          <label>下载镜像</label>
          <select class="input" v-model="inst.mirror">
            <option v-for="m in inst.mirrorRows" :key="m.id" :value="m.url">
              {{ m.label }}{{ m.ok === true ? ' · ' + m.ms + 'ms' : m.ok === false ? ' · 不可达' : '' }}{{ m.url ? '' : '' }}
            </option>
            <option value="__custom">自定义…</option>
          </select>
        </div>
        <div class="field" v-if="inst.mirror === '__custom'">
          <label>自定义镜像前缀（拼在 GitHub URL 前，以 / 结尾）</label>
          <input class="input mono" v-model="inst.customMirror" placeholder="https://your-mirror.example/https://github.com/">
        </div>
      </div>
      <button class="btn xs" :disabled="inst.testing" @click="speedTest" style="margin-bottom:10px">
        <span v-if="inst.testing" class="spinner"></span><Icon v-else name="activity" />镜像测速
      </button>
      <div v-if="inst.tested.length" class="mirror-table">
        <div v-for="m in inst.tested" :key="m.id" class="ext-row">
          <span class="state" :class="m.ok ? 'run' : 'fail'">
            <i class="dot" :class="{ off: !m.ok, fail: !m.ok }"></i>{{ m.ok ? m.ms + 'ms' : '不可达' }}
          </span>
          <b style="font-size:12px" class="grow">{{ m.label }}</b>
          <button class="btn xs" @click="inst.mirror = m.url">使用</button>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="note" style="margin-bottom:12px">
        <Icon name="info" class="ni s14" />
        <span>使用 Termux 官方源安装（<span class="mono">pkg install -y frpc</span>）。版本跟随 Termux 仓库更新，无需镜像。</span>
      </div>
    </template>

    <div v-if="inst.task" class="card flush" style="margin-top:12px">
      <div class="card-h">
        <Icon name="log" class="ni s16" style="color:var(--brand)" />
        <b class="grow">安装任务</b>
        <span class="tag" :class="inst.task.status === 'success' ? 'g' : inst.task.status === 'failed' ? 'danger' : ''">
          {{ { running: '进行中', success: '完成', failed: '失败' }[inst.task.status] || inst.task.status }}
        </span>
      </div>
      <div class="stat-bar" style="margin:0 14px"><i :style="{ width: (inst.task.progress || 0) + '%' }"></i></div>
      <pre class="code log-pre">{{ inst.task.output || '等待输出…' }}</pre>
    </div>

    <template #footer>
      <button class="btn" @click="inst.open = false">关闭</button>
      <button class="btn primary" :disabled="!!install.busy || inst.starting" @click="doInstall">
        <span v-if="inst.starting" class="spinner"></span><Icon v-else name="download" />开始安装
      </button>
    </template>
  </Modal>

  <!-- 详情 -->
  <Modal v-if="detail.open" :title="'隧道 · ' + detail.meta.name" icon-name="share" size="wide" @close="detail.open = false">
    <div class="d-stats">
      <div class="stat"><div class="stat-h">状态</div>
        <div class="stat-v" :style="{ color: detail.meta.status === 'running' ? 'var(--ok)' : '' }">
          {{ { running: '运行中', stopped: '已停止', failed: '失败' }[detail.meta.status] }}</div>
        <div class="stat-sub">重启 {{ detail.meta.restarts }} 次<template v-if="detail.meta.last_exit !== '' && detail.meta.last_exit != null"> · 上次退出码 {{ detail.meta.last_exit }}</template></div>
      </div>
      <div class="stat"><div class="stat-h">服务器</div>
        <div class="stat-v mono" style="font-size:15px">{{ detail.meta.addr || '—' }}</div>
        <div class="stat-sub">{{ detail.meta.mode === 'preset' ? '预设' : detail.meta.mode === 'local' ? '自建' : '导入' }} · {{ (detail.meta.format || 'toml').toUpperCase() }}</div>
      </div>
      <div class="stat"><div class="stat-h">公网拨测</div>
        <div class="stat-v" :style="{ fontSize: '15px', color: detail.meta.probe?.ok === false ? 'var(--danger)' : detail.meta.probe?.ok ? 'var(--ok)' : '' }">
          {{ detail.meta.probe?.ok == null ? '未拨测' : detail.meta.probe.ok ? '可达' : '不可达' }}</div>
        <div class="stat-sub">{{ detail.meta.probe?.detail || '—' }}</div>
      </div>
      <div class="stat"><div class="stat-h">拨测地址</div>
        <div class="d-probe-edit">
          <input class="input" v-model="detail.probeUrl" placeholder="tcp://ip:port 或 https://域名">
          <button class="btn xs" @click="saveProbe"><Icon name="save" />保存</button>
          <button class="btn xs" @click="probeNow"><Icon name="activity" />拨测</button>
        </div>
      </div>
    </div>

    <div class="seg" style="margin:6px 0 12px">
      <button :class="{ on: detail.tab === 'raw' }" @click="detail.tab = 'raw'">配置文件</button>
      <button :class="{ on: detail.tab === 'form' }" @click="switchForm">表单编辑</button>
      <button :class="{ on: detail.tab === 'log' }" @click="detail.tab = 'log'">日志</button>
    </div>

    <!-- raw -->
    <div v-if="detail.tab === 'raw'">
      <div class="note" style="margin-bottom:8px">
        <Icon name="lock" class="ni s14" />
        <span>token 已打码显示；未修改打码字段保存时会自动回填真实值。切换到表单编辑会丢失配置内注释。</span>
      </div>
      <textarea class="input mono" rows="14" v-model="detail.raw" spellcheck="false"></textarea>
      <div class="mt8" style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" @click="reloadDetail"><Icon name="refresh" />放弃修改</button>
        <button class="btn primary" :disabled="detail.saving" @click="saveRaw">
          <span v-if="detail.saving" class="spinner"></span><Icon v-else name="save" />保存并生效
        </button>
      </div>
    </div>

    <!-- structured 表单 -->
    <div v-else-if="detail.tab === 'form'">
      <div class="section-title">服务器</div>
      <div class="fgrid">
        <div class="field"><label>serverAddr</label><input class="input mono" v-model="detail.struct.server.serverAddr"></div>
        <div class="field"><label>serverPort</label><input class="input mono" type="number" v-model.number="detail.struct.server.serverPort"></div>
        <div class="field"><label>token</label><input class="input mono" v-model="detail.struct.server.auth_token" :placeholder="MASK"></div>
        <div class="field"><label>user</label><input class="input mono" v-model="detail.struct.server.user"></div>
      </div>
      <div class="section-title">代理（{{ detail.struct.proxies.length }}）</div>
      <div v-for="(p, i) in detail.struct.proxies" :key="i" class="px-editor">
        <div class="fgrid">
          <div class="field"><label>名称</label><input class="input mono" v-model="p.name"></div>
          <div class="field"><label>类型</label>
            <select class="input" v-model="p.type">
              <option v-for="t in types" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>
          <div class="field"><label>本地 IP</label><input class="input mono" v-model="p.local_ip" placeholder="127.0.0.1"></div>
          <div class="field"><label>本地端口</label><input class="input mono" type="number" v-model.number="p.local_port"></div>
          <div class="field" v-if="['tcp', 'udp', 'stcp', 'sudp'].includes(p.type)"><label>远程端口</label><input class="input mono" type="number" v-model.number="p.remote_port"></div>
          <div class="field" v-else><label>域名（逗号分隔）</label><input class="input mono" :value="(p.custom_domains || []).join(', ')" @input="p.custom_domains = $event.target.value.split(',').map(s => s.trim()).filter(Boolean)"></div>
        </div>
        <button class="btn xs danger" @click="detail.struct.proxies.splice(i, 1)"><Icon name="trash" />删除该代理</button>
      </div>
      <button class="btn xs" @click="addProxy" style="margin-top:8px"><Icon name="plus" />添加代理</button>
      <div class="mt8" style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" @click="detail.tab = 'raw'"><Icon name="code" />查看生成文件</button>
        <button class="btn primary" :disabled="detail.saving" @click="saveForm">
          <span v-if="detail.saving" class="spinner"></span><Icon v-else name="save" />保存并生效
        </button>
      </div>
    </div>

    <!-- 日志 -->
    <div v-else>
      <div class="card flush">
        <div class="card-h">
          <Icon name="log" class="ni s16" style="color:var(--brand)" />
          <b class="grow">进程输出（已脱敏）</b>
          <button class="btn xs" @click="reloadDetail"><Icon name="refresh" />刷新</button>
        </div>
        <pre class="code log-pre">{{ detail.log || '暂无输出' }}</pre>
      </div>
    </div>

    <template #footer>
      <button class="btn danger" @click="del"><Icon name="trash" />删除隧道</button>
      <div class="grow"></div>
      <button class="btn" @click="act(detail.meta, 'restart')"><Icon name="restart" />重启</button>
      <button class="btn" :class="detail.meta.status === 'running' ? 'danger' : 'success'"
              @click="act(detail.meta, detail.meta.status === 'running' ? 'stop' : 'start')">
        <Icon :name="detail.meta.status === 'running' ? 'stop' : 'play'" />
        {{ detail.meta.status === 'running' ? '停止' : '启动' }}
      </button>
    </template>
  </Modal>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'
import DataTable from '../components/DataTable.vue'

const MASK = '********'
const types = ['tcp', 'udp', 'http', 'https', 'stcp', 'sudp', 'tcpmux']

const list = ref([])
const cli = ref({})
const external = ref([])
const panelPort = ref(8888)
const install = ref({ installed: false, version: '', source: '', arch: '', install_path: '', latest: '', busy: false })
const runCount = computed(() => list.value.filter(i => i.status === 'running').length)

// ---------------- 网络守卫 ----------------
const ng = ref({ enabled: true, debounce: 45, poll_interval: 10, circuit_max: 3,
                 network: {}, pending: null, recovering: false, report: null,
                 circuit: [], log: [] })
const debounceInput = ref(45)
const recovering = ref(false)

async function loadNg() {
  try {
    const d = await api('/api/netguard/status')
    ng.value = { ...ng.value, ...d }
    debounceInput.value = d.debounce || 45
  } catch (e) { /* 静默：状态面板非关键 */ }
}

async function saveDebounce() {
  try {
    const d = await api('/api/netguard/settings', { method: 'POST', body: { debounce: debounceInput.value } })
    ng.value.debounce = d.debounce
    toast(`防抖等待已设为 ${d.debounce} 秒`, 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function toggleEnabled() {
  try {
    const d = await api('/api/netguard/settings', { method: 'POST', body: { enabled: !ng.value.enabled } })
    ng.value.enabled = d.enabled
    toast(d.enabled ? '网络守卫已启用' : '网络守卫已停用', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function doRecover() {
  recovering.value = true
  try {
    const d = await api('/api/netguard/recover', { method: 'POST', body: {} })
    toast(d.message || '已执行探测', d.ok ? 'ok' : 'err')
    await loadNg()
  } catch (e) { toast(e.message, 'err') } finally { recovering.value = false }
}

async function resetCircuit() {
  try {
    await api('/api/netguard/circuit/reset', { method: 'POST', body: {} })
    toast('熔断计数已清空', 'ok')
    await loadNg()
  } catch (e) { toast(e.message, 'err') }
}

const cols = [
  { key: 'name', title: '实例' },
  { key: 'server', title: '服务器', width: '180px' },
  { key: 'proxies', title: '代理' },
  { key: 'status', title: '状态', width: '170px' },
  { key: 'ops', title: '操作', align: 'r', width: '230px' }
]

const modeLabel = (m) => ({ preset: '预设', semi: '导入', local: '自建' }[m] || m)

async function load() {
  try {
    const [a, b, c] = await Promise.all([
      api('/api/tunnel/instances'),
      api('/api/tunnel/clients'),
      api('/api/tunnel/install/status').catch(() => null)
    ])
    list.value = a.instances || []
    panelPort.value = a.panel_port
    cli.value = b.frpc || {}
    external.value = b.external || []
    if (c) install.value = c
    // 后台有安装任务在跑（页面刚进来/刷新过）：恢复轮询
    if (c && c.busy && c.task_id && !inst.taskId) {
      inst.taskId = c.task_id
      inst.task = { status: 'running', output: '', progress: 0 }
      pollInstallTask()
    }
  } catch (e) { toast(e.message, 'err') }
}

let refreshTimer = 0
let ngTimer = 0
onMounted(() => {
  load()
  loadNg()
  // 安装任务进行中时保持按钮/状态新鲜
  refreshTimer = setInterval(() => { if (install.value.busy || inst.taskId) load() }, 10000)
  // 网络守卫状态每 10 秒刷一次（防抖倒计时要走字）
  ngTimer = setInterval(loadNg, 10000)
})
onUnmounted(() => { clearInterval(refreshTimer); clearInterval(ngTimer) })

// ---------------- frpc 安装 ----------------
const inst = reactive({
  open: false, source: 'github', version: '', mirror: '', customMirror: '',
  mirrors: [], mirrorRows: [], tested: [], testing: false, fetching: false,
  starting: false, task: null, taskId: '', pollTimer: 0
})

async function openInstall() {
  inst.open = true
  inst.taskId = ''
  inst.task = null
  inst.tested = []
  // 已有安装任务在跑（比如上次页面刷新丢了轮询）：恢复显示与轮询
  if (install.value.busy && install.value.task_id) {
    inst.taskId = install.value.task_id
    inst.task = { status: 'running', output: '', progress: 0 }
    pollInstallTask()
  }
  if (!inst.mirrors.length) {
    try {
      const j = await api('/api/tunnel/install/mirrors')
      inst.mirrors = j.mirrors || []
      inst.mirrorRows = inst.mirrors.map(m => ({ ...m }))
      // 默认选一个非直连镜像（国内环境直连大多不可达）
      const def = inst.mirrorRows.find(m => m.url)
      inst.mirror = def ? def.url : ''
    } catch (e) { /* 忽略，仍可手填 */ }
  }
  if (!inst.version && install.value.latest) inst.version = install.value.latest
  speedTest()
}

async function fetchLatest() {
  inst.fetching = true
  try {
    const j = await api('/api/tunnel/install/status')
    if (j.latest) { inst.version = j.latest; install.value.latest = j.latest }
    else toast('未能自动获取最新版本，请手动填写（如 0.61.0）', 'warn')
  } catch (e) { toast(e.message, 'err') } finally { inst.fetching = false }
}

async function speedTest() {
  inst.testing = true
  try {
    const j = await api('/api/tunnel/install/speedtest', { method: 'POST', body: {} })
    inst.tested = j.results || []
    inst.mirrorRows = inst.mirrors.map(m => {
      const r = (j.results || []).find(x => x.id === m.id)
      return { ...m, ok: r ? r.ok : null, ms: r ? r.ms : null }
    })
    // 自动选最快的可用镜像（含直连）
    const best = inst.tested.find(r => r.ok)
    if (best && inst.mirror !== '__custom') inst.mirror = best.url
  } catch (e) { toast(e.message, 'err') } finally { inst.testing = false }
}

function mirrorUrl() {
  return inst.mirror === '__custom' ? (inst.customMirror || '') : (inst.mirror || '')
}

async function doInstall() {
  inst.starting = true
  try {
    const j = await api('/api/tunnel/install', {
      method: 'POST',
      body: { source: inst.source, version: inst.version, mirror: mirrorUrl() }
    })
    inst.taskId = j.id
    inst.task = { status: 'running', output: '', progress: 0 }
    toast('安装任务已启动', 'ok')
    pollInstallTask()
  } catch (e) { toast(e.message, 'err') } finally { inst.starting = false }
}

let pollBusy = false
async function pollInstallTask() {
  if (pollBusy || !inst.taskId) return
  pollBusy = true
  try {
    const t = await api('/api/exec/' + inst.taskId)
    inst.task = t
    if (t.status === 'running') {
      clearTimeout(inst.pollTimer)
      inst.pollTimer = setTimeout(pollInstallTask, 1500)
    } else {
      if (t.status === 'success') { toast('frpc 安装成功', 'ok') }
      else if (t.status === 'failed') toast('安装失败，见安装任务日志', 'err')
      inst.taskId = ''
      load()
    }
  } catch (e) { /* 网络抖动下一轮继续 */ } finally { pollBusy = false }
}

async function uninstall() {
  if (!confirm(`卸载 frpc（${cli.value.version || '未知版本'}）？`)) return
  try {
    await api('/api/tunnel/uninstall', { method: 'POST' })
    toast('frpc 已卸载', 'ok')
    load()
  } catch (e) { toast(e.message, 'err') }
}

async function act(row, a) {
  try {
    await api(`/api/tunnel/instance/${row.id}/${a}`, { method: 'POST' })
    toast(`${row.name} ${{ start: '已启动', stop: '已停止', restart: '已重启' }[a]}`, 'ok')
    setTimeout(() => { load(); if (detail.open && detail.meta.id === row.id) reloadDetail() }, 600)
  } catch (e) { toast(e.message, 'err') }
}

// ---------------- 新建向导 ----------------
const wizTabs = [
  { id: 'preset', label: 'FreeFrp 预设' },
  { id: 'local', label: '自建 FRPS' },
  { id: 'semi', label: '导入配置' }
]
const wizTabTitle = computed(() => wizTabs.find(t => t.id === wiz.tab)?.label || '')
const wiz = reactive({
  open: false, tab: 'preset', parsing: false, saving: false, parseResult: null,
  preset: {},
  form: { name: '', ptype: 'tcp', localPort: 8022, remotePort: 0, domain: '',
          localIp: '127.0.0.1', serverAddr: '', serverPort: 7000, token: '', raw: '', probeUrl: '' }
})

async function openWizard() {
  wiz.open = true
  wiz.tab = 'preset'
  wiz.parseResult = null
  wiz.form.name = ''
  wiz.form.raw = ''
  wiz.form.probeUrl = ''
  try {
    const j = await api('/api/tunnel/presets')
    wiz.preset = (j.presets && j.presets.freefrp) || {}
    const c = await api('/api/tunnel/clients')
    if (c.frpc?.found) wiz.form.serverPort = wiz.form.serverPort || 7000
    if (!wiz.form.remotePort) randPort()
  } catch (e) { /* 预设拿不到也不挡创建 */ }
}

function randPort() {
  const [lo, hi] = wiz.preset.port_range || [10001, 50000]
  wiz.form.remotePort = lo + Math.floor(Math.random() * (hi - lo))
}

function buildStruct() {
  const f = wiz.form
  if (wiz.tab === 'preset') {
    const srv = { ...(wiz.preset.server || {}) }
    const p = { name: f.name || 'proxy', type: f.ptype, local_ip: f.localIp || '127.0.0.1', local_port: f.localPort }
    if (f.ptype === 'tcp') { p.remote_port = f.remotePort }
    else { p.custom_domains = [f.domain] }
    return { server: srv, proxies: [p] }
  }
  if (wiz.tab === 'local') {
    const p = { name: f.name || 'proxy', type: f.ptype, local_ip: f.localIp || '127.0.0.1', local_port: f.localPort }
    if (['tcp', 'udp'].includes(f.ptype)) p.remote_port = f.remotePort
    else p.custom_domains = f.domain.split(',').map(s => s.trim()).filter(Boolean)
    return {
      server: { serverAddr: f.serverAddr, serverPort: f.serverPort, auth_token: f.token },
      proxies: [p]
    }
  }
  return null
}

function guessProbe() {
  const f = wiz.form
  if (f.probeUrl) return f.probeUrl
  if (wiz.tab === 'preset' || wiz.tab === 'local') {
    if (['tcp', 'udp'].includes(f.ptype) && f.remotePort) {
      const host = (wiz.tab === 'preset' ? (wiz.preset.server?.serverAddr || '') : f.serverAddr) || ''
      if (host) return `tcp://${host}:${f.remotePort}`
    }
    if (f.ptype === 'http' && f.domain) return `http://${f.domain}`
  }
  return ''
}

async function doParse() {
  wiz.parsing = true
  try {
    wiz.parseResult = await api('/api/tunnel/parse', { method: 'POST', body: { text: wiz.form.raw } })
  } catch (e) { toast(e.message, 'err') } finally { wiz.parsing = false }
}

async function doCreate() {
  const f = wiz.form
  if (!f.name.trim()) return toast('请填实例名称', 'err')
  wiz.saving = true
  try {
    let body
    if (wiz.tab === 'semi') {
      if (!f.raw.trim()) return toast('请粘贴配置', 'err')
      body = { name: f.name.trim(), text: f.raw, mode: 'semi' }
    } else {
      body = { name: f.name.trim(), struct: buildStruct(), fmt: 'toml',
               mode: wiz.tab === 'preset' ? 'preset' : 'local' }
    }
    body.probe_url = guessProbe()
    const j = await api('/api/tunnel/instance', { method: 'POST', body })
    toast(`隧道 ${f.name} 已创建并启动`, 'ok')
    wiz.open = false
    load()
    if (j.id) openDetailById(j.id)
  } catch (e) { toast(e.message, 'err') } finally { wiz.saving = false }
}

// ---------------- 详情 ----------------
const detail = reactive({
  open: false, tab: 'raw', saving: false,
  meta: {}, raw: '', struct: { server: {}, proxies: [] }, log: '', probeUrl: ''
})

async function openDetailById(id) {
  const j = await api(`/api/tunnel/instance/${id}`)
  applyDetail(j)
  detail.open = true
}

function openDetail(row) {
  openDetailById(row.id).catch(e => toast(e.message, 'err'))
}

function applyDetail(j) {
  detail.meta = j
  detail.raw = j.raw_masked || ''
  detail.struct = j.structured || { server: {}, proxies: [] }
  detail.log = j.log || ''
  detail.probeUrl = j.probe_url || ''
}

async function reloadDetail() {
  try {
    applyDetail(await api(`/api/tunnel/instance/${detail.meta.id}`))
  } catch (e) { toast(e.message, 'err') }
}

function switchForm() {
  detail.tab = 'form'
  if (!detail.struct.server) detail.struct = { server: {}, proxies: [] }
  if (!detail.struct.server.serverAddr && detail.raw) {
    toast('当前配置无法解析为表单，请检查格式', 'warn')
  }
}

function addProxy() {
  detail.struct.proxies.push({ name: 'proxy' + (detail.struct.proxies.length + 1),
                               type: 'tcp', local_ip: '127.0.0.1',
                               local_port: 80, remote_port: 0, custom_domains: [] })
}

async function saveRaw() {
  detail.saving = true
  try {
    await api(`/api/tunnel/instance/${detail.meta.id}`, { method: 'POST', body: { text: detail.raw } })
    toast('配置已保存并热重启', 'ok')
    reloadDetail(); load()
  } catch (e) { toast(e.message, 'err') } finally { detail.saving = false }
}

async function saveForm() {
  detail.saving = true
  try {
    await api(`/api/tunnel/instance/${detail.meta.id}`, { method: 'POST', body: { struct: detail.struct } })
    toast('配置已保存并热重启', 'ok')
    reloadDetail(); load()
  } catch (e) { toast(e.message, 'err') } finally { detail.saving = false }
}

async function saveProbe() {
  try {
    await api(`/api/tunnel/instance/${detail.meta.id}`, { method: 'POST', body: { probe_url: detail.probeUrl } })
    toast('拨测地址已保存', 'ok'); reloadDetail()
  } catch (e) { toast(e.message, 'err') }
}

async function probeNow() {
  try {
    const j = await api(`/api/tunnel/instance/${detail.meta.id}/probe`, { method: 'POST' })
    toast(j.ok ? '拨测通过：' + j.detail : '拨测失败：' + j.detail, j.ok ? 'ok' : 'err')
    reloadDetail()
  } catch (e) { toast(e.message, 'err') }
}

async function del() {
  if (!confirm(`删除隧道「${detail.meta.name}」？配置文件与日志将一并删除`)) return
  try {
    await api(`/api/tunnel/instance/${detail.meta.id}/delete`, { method: 'POST' })
    toast('已删除', 'ok')
    detail.open = false
    load()
  } catch (e) { toast(e.message, 'err') }
}

</script>
