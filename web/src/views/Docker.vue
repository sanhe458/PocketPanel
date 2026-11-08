<template>
  <div class="page-head dk-page">
    <div class="ph-t">
      <h2><Icon name="package" />容器</h2>
      <p>基于 udocker / udockerd 的无 root 容器运行时 · 兼容 Docker 镜像，不是完整 Docker 管理器</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="loadAll(true)"><Icon name="refresh" />刷新</button>
      <button class="btn" @click="openSettings"><Icon name="settings" />运行时设置</button>
      <button class="btn primary" :disabled="!ready" @click="openCreate()"><Icon name="plus" />新建容器</button>
    </div>
  </div>

  <!-- 四条硬边界：常驻展示，不做折叠 -->
  <div class="dk-bounds">
    <div v-for="b in boundList" :key="b.key" class="dk-bound">
      <div class="dk-bi" :class="b.tone"><Icon :name="b.icon" /></div>
      <div class="grow">
        <b>{{ b.title }}</b>
        <span>{{ b.short }}</span>
      </div>
    </div>
  </div>
  <div class="dk-bound-note">
    <Icon name="info" class="ni s14" />
    <span>以上为运行时能力边界，直接影响使用方式。</span>
    <button class="btn link" @click="boundOpen = true">完整说明</button>
  </div>

  <!-- 运行时状态 -->
  <div class="dk-rt">
    <div class="card flush">
      <div class="card-h">
        <Icon name="layers" class="ni s16" style="color:var(--brand)" />
        <b class="grow">udocker 客户端</b>
        <span class="tag" :class="uck.installed ? 'g' : 'y'">{{ uck.installed ? '已安装' : '未安装' }}</span>
      </div>
      <div class="card-b" style="padding:10px 14px">
        <template v-if="uck.installed">
          <div class="kv"><span class="k">版本</span><span class="v mono">{{ uck.version || '未知' }}</span></div>
          <div class="kv"><span class="k">englib</span><span class="v">
            <span class="tag" :class="uck.englib ? 'g' : 'r'">{{ uck.englib ? '引擎就绪' : '缺少 PRoot' }}</span>
          </span></div>
          <div class="kv"><span class="k">路径</span><span class="v mono ell" style="max-width:230px">{{ uck.path }}</span></div>
        </template>
        <div v-else class="dim" style="font-size:12px;line-height:1.75">
          容器运行时需要 udocker（客户端约 80KB + englib 约 46MB）。
          支持 GitHub 镜像源与测速，安装走后台任务。
        </div>
      </div>
      <div class="card-f" style="display:flex;gap:8px;justify-content:flex-end">
        <button v-if="uck.managed" class="btn xs danger" @click="uninstall"><Icon name="trash" />卸载</button>
        <button class="btn xs primary" @click="openInstall">
          <Icon name="download" />{{ uck.installed ? (uck.englib ? '重装' : '补装引擎') : '一键安装' }}
        </button>
      </div>
    </div>

    <div class="card flush">
      <div class="card-h">
        <Icon name="activity" class="ni s16" style="color:var(--ok)" />
        <b class="grow">udockerd 守护进程</b>
        <span class="state" :class="svcState.cls"><i class="dot" :class="svcState.dot"></i>{{ svcState.label }}</span>
      </div>
      <div class="card-b" style="padding:10px 14px">
        <div class="kv"><span class="k">Engine API</span><span class="v mono">tcp://127.0.0.1:{{ settings.port }}</span></div>
        <div class="kv"><span class="k">版本</span><span class="v mono">{{ dm.version || '—' }}<template v-if="dm.api"> · API {{ dm.api }}</template></span></div>
        <div class="kv"><span class="k">托管方式</span><span class="v">{{ dm.adapter === 'runit' ? 'runit（sv）' : dm.adapter === 'direct' ? '面板直管' : '未注册' }}</span></div>
        <div class="kv"><span class="k">容器 / 镜像</span><span class="v">{{ counts.running }} 运行 / {{ counts.containers }} 个 · {{ counts.images }} 个镜像</span></div>
      </div>
      <div class="card-f" style="display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap">
        <button class="btn xs" @click="daemonLogs"><Icon name="log" />日志</button>
        <button v-if="!dm.registered" class="btn xs primary" :disabled="!uck.found" @click="daemon('ensure')">
          <Icon name="check" />注册并启用
        </button>
        <template v-else>
          <label class="dk-switch" :title="dm.autostart ? '开机自启已开启' : '开机自启已关闭'">
            <input type="checkbox" :checked="dm.autostart" @change="toggleAuto($event.target.checked)">
            <span>自启</span>
          </label>
          <button class="btn xs" :disabled="busy === 'daemon'" @click="daemon('restart')"><Icon name="restart" />重启</button>
          <button v-if="dm.state === 'run'" class="btn xs danger" @click="daemon('stop')"><Icon name="stop" />停止</button>
          <button v-else class="btn xs success" @click="daemon('start')"><Icon name="play" />启动</button>
        </template>
      </div>
    </div>
  </div>

  <div v-if="!uck.installed" class="note dk-note warn" style="margin-top:14px">
    <Icon name="warning" class="ni s14" />
    <span>未检测到 udocker。先点右上「一键安装」；装好后再点守护进程卡的「注册并启用」。</span>
  </div>

  <!-- 主体 -->
  <div class="dk-tabs">
    <div class="seg">
      <button :class="{ on: tab === 'apps' }" @click="goApps">
        应用 <span class="dk-num" v-if="store.apps.length">{{ store.apps.length }}</span>
      </button>
      <button :class="{ on: tab === 'containers' }" @click="tab = 'containers'">
        容器 <span class="dk-num">{{ containers.length }}</span>
      </button>
      <button :class="{ on: tab === 'images' }" @click="tab = 'images'">
        镜像 <span class="dk-num">{{ images.length }}</span>
      </button>
      <button :class="{ on: tab === 'compose' }" @click="tab = 'compose'">编排</button>
    </div>
    <div class="grow"></div>
    <template v-if="tab === 'apps'">
      <button class="btn sm" @click="goStore"><Icon name="grid" />应用商店</button>
      <button class="btn sm" @click="openStore"><Icon name="settings" />商店设置</button>
      <button class="btn sm" @click="loadStore(true)" :disabled="store.loading">
        <Icon name="refresh" />{{ store.loading ? '加载中' : '刷新' }}
      </button>
    </template>
    <button v-if="tab === 'images'" class="btn sm" @click="openImport"><Icon name="up" />导入镜像</button>
    <button v-if="tab === 'images'" class="btn sm primary" @click="openPull"><Icon name="download" />拉取镜像</button>
  </div>

  <!-- 应用商店 -->
  <div v-if="tab === 'apps'">
    <div class="dk-store-bar">
      <span class="dk-store-dot" :class="{ off: !store.ok }"></span>
      <span class="dk-store-txt">
        {{ store.ok ? '已连接应用商店' : '离线，使用内置清单' }} · {{ store.apps.length }} 个应用
        <template v-if="store.source === 'builtin'">（内置）</template>
        <template v-else-if="store.source === 'cache'">（缓存）</template>
      </span>
      <span v-if="store.error" class="dk-store-err" :title="store.error">{{ store.error }}</span>
    </div>
    <div v-if="!store.apps.length" class="card" style="padding:36px 18px;text-align:center;color:var(--muted)">
      {{ store.loading ? '正在拉取应用清单…' : '清单为空，点右上角「刷新」重试' }}
    </div>
    <div class="dk-app-grid">
      <div v-for="a in store.apps" :key="a.id" class="dk-app">
        <div class="dk-app-h">
          <Icon v-if="a.icon_name" :name="a.icon_name" class="dk-app-ico" />
          <span v-else class="dk-app-ico-emoji">{{ a.icon || '📦' }}</span>
          <div class="dk-app-t">
            <div class="dk-app-n">{{ a.name }}</div>
            <div class="dk-app-s">{{ a.summary }}</div>
          </div>
        </div>
        <p class="dk-app-d">{{ a.desc }}</p>
        <div class="dk-app-m">
          <span class="tag">{{ a.size || '未知大小' }}</span>
          <span v-if="a.verified" class="tag ok">已实测</span>
          <span v-else class="tag y">未实测</span>
          <span v-if="store.installed[a.id]" class="tag"
                :class="{ ok: store.installed[a.id].state === 'running' }">
            {{ store.installed[a.id].state === 'running' ? '运行中' : '已安装' }}
          </span>
        </div>
        <div class="dk-app-a">
          <button v-if="!store.installed[a.id]" class="btn sm primary"
                  :disabled="!uck.installed || !dm.reachable"
                  @click="openAppInstall(a)">一键安装</button>
          <button v-else class="btn sm" @click="goContainer(a)">查看容器</button>
        </div>
      </div>
    </div>
    <div v-if="!uck.installed" class="note dk-note warn" style="margin-top:14px">
      <Icon name="warning" class="ni s14" />
      <span>还没装 udocker，先到「运行时」卡片点「一键安装」，装完才能装应用。</span>
    </div>
  </div>

  <!-- 容器列表 -->
  <div v-if="tab === 'containers'" class="card flush">
    <div v-if="!containers.length" class="empty" style="padding:46px 18px">
      <div class="ei"><Icon name="package" /></div>
      <b>{{ ready ? '还没有容器' : '容器运行时未就绪' }}</b>
      <span v-if="ready">先拉取一个镜像（如 alpine:3.19），再新建容器</span>
      <span v-else>安装 udocker 并启动 udockerd 后，这里会显示容器</span>
      <button v-if="ready" class="btn primary" @click="openCreate()"><Icon name="plus" />新建容器</button>
    </div>

    <div v-for="c in containers" :key="c.id" class="dk-row">
      <div class="dk-row-main">
        <div class="dk-row-t">
          <i class="dot" :class="c.state === 'running' ? 'busy' : c.state === 'exited' && c.exit_code ? 'fail' : 'off'"></i>
          <b class="ell">{{ c.name }}</b>
          <span v-if="c.restart && c.restart !== 'no'" class="tag b">{{ restartLabel(c.restart) }}</span>
          <span class="tag">{{ c.execmode }}</span>
          <span v-if="c.restarts" class="tag y">重启 {{ c.restarts }}</span>
        </div>
        <div class="dk-row-s mono ell">{{ c.image }}</div>
        <div class="dk-row-m">
          <span class="state" :class="c.state === 'running' ? 'run' : c.exit_code ? 'fail' : 'stop'">
            {{ c.state === 'running' ? '运行中' : c.state === 'created' ? '已创建' : '已退出' }}
            <template v-if="c.state === 'exited'">（{{ c.exit_code }}）</template>
          </span>
          <span v-if="c.state === 'running' && c.started_at" class="dim">{{ uptime(c.started_at) }}</span>
          <span v-for="p in c.ports.slice(0, 3)" :key="p.host + p.proto" class="tag mono">:{{ p.host }}→{{ p.container }}</span>
          <span v-if="c.mounts.length" class="tag"><Icon name="folder" class="ni s12" />{{ c.mounts.length }} 映射</span>
        </div>
      </div>
      <div class="dk-acts">
        <button v-if="c.state === 'running'" class="btn xs" @click="act(c, 'restart')"><Icon name="restart" />重启</button>
        <button v-if="c.state === 'running'" class="btn xs danger" @click="act(c, 'stop')"><Icon name="stop" />停止</button>
        <button v-else class="btn xs success" @click="act(c, 'start')"><Icon name="play" />启动</button>
        <button class="btn xs" @click="openLogs(c)"><Icon name="log" />日志</button>
        <button class="btn xs" :disabled="c.state !== 'running'" @click="openExec(c)"><Icon name="terminal" />终端</button>
        <button class="btn xs" @click="openDetail(c)"><Icon name="eye" />详情</button>
        <button class="btn xs danger" @click="remove(c)"><Icon name="trash" /></button>
      </div>
    </div>
  </div>

  <!-- 镜像列表 -->
  <div v-else-if="tab === 'images'" class="card flush">
    <div v-if="!images.length" class="empty" style="padding:46px 18px">
      <div class="ei"><Icon name="layers" /></div>
      <b>还没有镜像</b>
      <span>拉取 ARM64 镜像（如 alpine:3.19），或导入 docker save 产物</span>
      <button class="btn primary" @click="openPull"><Icon name="download" />拉取镜像</button>
    </div>
    <div v-for="im in images" :key="im.id" class="dk-row">
      <div class="dk-row-main">
        <div class="dk-row-t">
          <Icon name="layers" class="ni s14" style="color:var(--brand)" />
          <b class="ell mono">{{ im.name }}</b>
          <span class="tag">{{ im.arch || '—' }}</span>
        </div>
        <div class="dk-row-s mono ell">sha256:{{ im.id.slice(0, 12) }}</div>
        <div class="dk-row-m">
          <span class="dim">{{ fmtB(im.size) }}</span>
          <span class="dim">{{ fmtTime(im.created) }}</span>
        </div>
      </div>
      <div class="dk-acts">
        <button class="btn xs primary" @click="openCreate(im)"><Icon name="plus" />用它创建</button>
        <button class="btn xs" @click="pullAgain(im)"><Icon name="refresh" />更新</button>
        <button class="btn xs danger" @click="removeImage(im)"><Icon name="trash" /></button>
      </div>
    </div>
  </div>

  <!-- Compose -->
  <div v-else class="dk-compose">
    <div class="note dk-note" style="margin-bottom:12px">
      <Icon name="info" class="ni s14" />
      <span>内置解析器支持 <b>子集</b>：image / command / environment / env_file / volumes（仅宿主机目录）/
        ports / restart / depends_on / .env 插值。不是原生 Compose，也不支持 build 与命名卷。</span>
    </div>
    <div class="grid2">
      <div class="card">
        <div class="field">
          <label>docker-compose.yml</label>
          <textarea class="input mono dk-code" v-model="cp.text" spellcheck="false"
                    placeholder="services:&#10;  web:&#10;    image: nginx:alpine&#10;    ports:&#10;      - &quot;8080:80&quot;&#10;    volumes:&#10;      - ./html:/usr/share/nginx/html"></textarea>
        </div>
        <div class="fgrid">
          <div class="field"><label>项目名（可选）</label><input class="input" v-model="cp.project" placeholder="demo"></div>
          <div class="field"><label>工作目录（相对路径基准）</label>
            <input class="input mono" v-model="cp.base" placeholder="留空 = ~/pocketpanel/compose/demo">
          </div>
        </div>
        <div class="field">
          <label>.env（可选，用于 ${VAR} 插值）</label>
          <textarea class="input mono dk-code sm" v-model="cp.env" spellcheck="false" placeholder="TAG=3.19"></textarea>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn" :disabled="cp.busy" @click="preview"><span v-if="cp.busy" class="spinner"></span>解析预览</button>
          <button class="btn primary" :disabled="cp.busy || !cp.plan" @click="composeUp"><Icon name="play" />启动</button>
        </div>
      </div>

      <div class="card flush">
        <div class="card-h"><Icon name="list" class="ni s16" /><b class="grow">解析结果</b>
          <span v-if="cp.plan" class="tag g">{{ cp.plan.order.length }} 个服务</span>
        </div>
        <div v-if="!cp.plan" class="empty" style="padding:36px 16px">
          <div class="ei"><Icon name="code" /></div>
          <b>还没有解析</b><span>粘贴 compose 文件后点「解析预览」</span>
        </div>
        <div v-else class="card-b" style="padding:8px 12px">
          <div class="kv"><span class="k">项目</span><span class="v mono">{{ cp.plan.project }}</span></div>
          <div class="kv"><span class="k">工作目录</span><span class="v mono ell" style="max-width:260px">{{ cp.plan.base_dir }}</span></div>
          <div class="kv"><span class="k">启动顺序</span><span class="v mono">{{ cp.plan.order.join(' → ') }}</span></div>
          <div v-for="s in cp.plan.services" :key="s.service" class="dk-cp-svc">
            <div class="dk-row-t">
              <b>{{ s.service }}</b><span class="tag mono">{{ s.image }}</span>
              <span v-if="s.restart !== 'no'" class="tag b">{{ restartLabel(s.restart) }}</span>
            </div>
            <div class="dk-row-s mono ell">{{ s.name }}{{ s.cmd ? ' · ' + s.cmd : '' }}</div>
            <div class="dk-row-m">
              <span v-for="m in s.mounts" :key="m.host + m.container" class="tag mono" :title="m.host + ' → ' + m.container">{{ m.host }} → {{ m.container }}</span>
              <span v-for="p in s.ports" :key="p.host + p.proto" class="tag mono">:{{ p.host }}→{{ p.container }}</span>
              <span v-if="s.depends_on.length" class="tag">依赖 {{ s.depends_on.join(',') }}</span>
            </div>
          </div>
          <div v-for="w in cp.plan.warnings" :key="w" class="dk-warn"><Icon name="warning" class="ni s12" />{{ w }}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 新建容器 -->
  <Modal v-if="wiz.open" title="新建容器" sub="host 网络 · 无 cgroups · 无 Volume 对象" icon-name="package"
         size="wide" @close="wiz.open = false">
    <div v-if="wiz.err" class="note dk-note danger" style="margin-bottom:12px">
      <Icon name="error" class="ni s14" /><span>{{ wiz.err }}</span>
    </div>
    <div class="fgrid">
      <div class="field"><label>容器名</label>
        <input class="input" v-model="wiz.form.name" placeholder="留空自动生成" autocapitalize="off">
        <span v-if="wiz.fe.name" class="dk-fielderr">{{ wiz.fe.name }}</span>
      </div>
      <div class="field"><label>镜像 *</label>
        <input class="input mono" v-model="wiz.form.image" list="dk-images" placeholder="alpine:3.19">
        <datalist id="dk-images"><option v-for="im in images" :key="im.id" :value="im.name" /></datalist>
        <span v-if="wiz.fe.image" class="dk-fielderr">{{ wiz.fe.image }}</span>
      </div>
      <div class="field"><label>命令（覆盖镜像 CMD）</label>
        <input class="input mono" v-model="wiz.form.cmd" placeholder="如 sleep 3600，或 sh -c &quot;echo hi&quot;">
      </div>
      <div class="field"><label>重启策略</label>
        <select class="input" v-model="wiz.form.restart">
          <option value="no">no（不自动重启）</option>
          <option value="on-failure">on-failure（异常退出才重启）</option>
          <option value="always">always（总是保活）</option>
        </select>
      </div>
      <div class="field"><label>执行模式</label>
        <select class="input" v-model="wiz.form.execmode">
          <option v-for="(desc, m) in caps.exec_modes" :key="m" :value="m">{{ m }} · {{ desc }}</option>
        </select>
      </div>
      <div class="field"><label>工作目录 / 用户（可选）</label>
        <div class="dk-two">
          <input class="input mono" v-model="wiz.form.workdir" placeholder="/app">
          <input class="input mono" v-model="wiz.form.user" placeholder="root">
        </div>
      </div>
    </div>

    <!-- 环境变量 -->
    <div class="dk-sec">
      <div class="dk-sec-h"><b>环境变量</b><span class="dim">值里含 token / password 等字样时，详情页会自动打码</span>
        <button class="btn xs" @click="wiz.envs.push({ key: '', value: '' })"><Icon name="plus" />添加</button>
      </div>
      <div v-for="(e, i) in wiz.envs" :key="'e' + i" class="dk-env-row">
        <input class="input mono" v-model="e.key" placeholder="KEY" autocapitalize="off">
        <span class="dk-eq">=</span>
        <input class="input mono" v-model="e.value" placeholder="value">
        <button class="ico-btn" @click="wiz.envs.splice(i, 1)" title="删除"><Icon name="close" /></button>
      </div>
      <div v-if="!wiz.envs.length" class="dim dk-hint">没有环境变量</div>
    </div>

    <!-- 目录映射 -->
    <div class="dk-sec">
      <div class="dk-sec-h"><b>目录映射</b><span class="dim">仅宿主机目录（bind mount），不是 Docker Volume</span>
        <button class="btn xs" @click="wiz.mounts.push({ host: '', container: '', ro: false })"><Icon name="plus" />添加</button>
      </div>
      <div v-for="(m, i) in wiz.mounts" :key="'m' + i" class="dk-mount-row">
        <div class="dk-mount-h">
          <input class="input mono" v-model="m.host" placeholder="宿主机目录，如 ~/site" autocapitalize="off">
          <button class="btn xs" @click="openBrowse(i)"><Icon name="folder" />选择</button>
        </div>
        <div class="dk-mount-c">
          <span class="dim mono">→</span>
          <input class="input mono" v-model="m.container" placeholder="容器内路径，如 /data">
          <label class="dk-check"><input type="checkbox" v-model="m.ro">只读</label>
          <button class="ico-btn" @click="wiz.mounts.splice(i, 1)" title="删除"><Icon name="close" /></button>
        </div>
        <div v-if="m.ro" class="dk-hint warn">PRoot 无法强制只读，该选项只作标注，容器内实际可写</div>
      </div>
      <div v-if="!wiz.mounts.length" class="dim dk-hint">没有目录映射（容器重启后数据会丢）</div>
    </div>

    <!-- 端口 -->
    <div class="dk-sec">
      <div class="dk-sec-h"><b>端口</b><span class="dim">host 网络：容器端口即宿主机端口，仅作记录与校验</span>
        <button class="btn xs" @click="wiz.ports.push({ host: '', container: '', proto: 'tcp' })"><Icon name="plus" />添加</button>
      </div>
      <div v-for="(p, i) in wiz.ports" :key="'p' + i" class="dk-port-row">
        <input class="input mono" type="number" v-model.number="p.host" placeholder="宿主机 ≥1024">
        <span class="dim">→</span>
        <input class="input mono" type="number" v-model.number="p.container" placeholder="容器端口">
        <select class="input" v-model="p.proto"><option value="tcp">tcp</option><option value="udp">udp</option></select>
        <button class="ico-btn" @click="wiz.ports.splice(i, 1)" title="删除"><Icon name="close" /></button>
      </div>
      <div v-if="!wiz.ports.length" class="dim dk-hint">1024 以下端口不可监听（非 root）</div>
    </div>

    <label class="dk-check" style="margin-top:10px">
      <input type="checkbox" v-model="wiz.form.start">创建后立即启动
    </label>

    <template #footer>
      <button class="btn" @click="wiz.open = false">取消</button>
      <button class="btn primary" :disabled="wiz.busy" @click="submitCreate">
        <span v-if="wiz.busy" class="spinner"></span>{{ wiz.busy ? '创建中…' : '创建容器' }}
      </button>
    </template>
  </Modal>

  <!-- 目录选择 -->
  <Modal v-if="browse.open" title="选择宿主机目录" :sub="browse.path" icon-name="folder" size="wide" @close="browse.open = false">
    <div class="dk-browse-bar">
      <button class="btn sm" @click="browseTo(browse.parent)"><Icon name="up" />上级</button>
      <button class="btn sm" @click="browseTo(homeDir)"><Icon name="home" />主目录</button>
      <button class="btn sm" @click="mkHere"><Icon name="plus" />新建文件夹</button>
    </div>
    <div v-if="browse.err" class="note dk-note danger" style="margin-bottom:10px">{{ browse.err }}</div>
    <div class="dk-dirs">
      <div v-for="d in browse.dirs" :key="d.name" class="dk-dir" @click="browseTo(joinPath(browse.path, d.name))">
        <Icon name="folder" class="ni s14" /><span class="ell">{{ d.name }}</span><Icon name="chevronRight" class="ni s12" />
      </div>
      <div v-if="!browse.dirs.length" class="dim" style="padding:16px;text-align:center">没有子目录</div>
    </div>
    <template #footer>
      <button class="btn" @click="browse.open = false">取消</button>
      <button class="btn primary" @click="pickDir">选择当前目录</button>
    </template>
  </Modal>

  <!-- 详情 -->
  <Modal v-if="detail.open" :title="detail.data ? detail.data.name : '容器详情'" icon-name="package" size="wide"
         @close="detail.open = false">
    <div v-if="detail.err" class="note dk-note danger">{{ detail.err }}</div>
    <template v-else-if="detail.data">
      <div class="dk-d-stats">
        <div class="stat"><div class="stat-h">状态</div>
          <div class="stat-v">{{ detail.data.state === 'running' ? '运行中' : detail.data.state === 'created' ? '已创建' : '已退出' }}</div>
          <div class="stat-sub">退出码 {{ detail.data.exit_code }}</div>
        </div>
        <div class="stat"><div class="stat-h">重启次数</div><div class="stat-v">{{ detail.data.restarts }}</div>
          <div class="stat-sub">策略 {{ restartLabel(detail.data.restart) }}</div>
        </div>
        <div class="stat"><div class="stat-h">网络</div><div class="stat-v">host</div>
          <div class="stat-sub mono">{{ detail.data.ip || '共享宿主机网络' }}</div>
        </div>
        <div class="stat"><div class="stat-h">执行模式</div><div class="stat-v">{{ detail.data.execmode }}</div>
          <div class="stat-sub">PRoot / Fakechroot</div>
        </div>
      </div>
      <div class="kv"><span class="k">镜像</span><span class="v mono">{{ detail.data.image }}</span></div>
      <div class="kv"><span class="k">命令</span><span class="v mono">{{ detail.data.command || '（镜像默认）' }}</span></div>
      <div class="kv"><span class="k">工作目录 / 用户</span><span class="v mono">{{ detail.data.workdir || '/' }} · {{ detail.data.user || 'root' }}</span></div>
      <div class="kv"><span class="k">启动 / 退出</span><span class="v mono">{{ fmtWhen(detail.data.started_at) }} → {{ fmtWhen(detail.data.finished_at) }}</span></div>
      <div v-for="w in detail.data.warnings" :key="w" class="dk-warn"><Icon name="warning" class="ni s12" />{{ w }}</div>

      <h4 class="dk-h4">目录映射</h4>
      <div v-if="!detail.data.mounts.length" class="dim dk-hint">无</div>
      <div v-for="m in detail.data.mounts" :key="m.host + m.container" class="dk-mnt">
        <span class="mono ell">{{ m.host }}</span><Icon name="chevronRight" class="ni s12" />
        <span class="mono ell">{{ m.container }}</span>
        <span class="tag" :class="m.ro ? 'y' : ''">{{ m.ro ? 'ro（仅标注）' : 'rw' }}</span>
      </div>

      <h4 class="dk-h4">环境变量</h4>
      <div v-if="!detail.data.env.length" class="dim dk-hint">无</div>
      <div v-for="e in detail.data.env" :key="e.key" class="dk-env">
        <span class="mono">{{ e.key }}</span><span class="dim">=</span>
        <span class="mono ell">{{ e.value }}</span>
        <span v-if="e.masked" class="tag">已打码</span>
      </div>

      <h4 class="dk-h4">端口记录</h4>
      <div v-if="!detail.data.ports.length" class="dim dk-hint">无（host 网络，容器直接监听宿主机端口）</div>
      <div v-else class="dk-row-m">
        <span v-for="p in detail.data.ports" :key="p.host + p.proto" class="tag mono">{{ p.host }} → {{ p.container }}/{{ p.proto }}</span>
      </div>
    </template>
    <template #footer>
      <button class="btn" @click="detail.open = false">关闭</button>
      <button class="btn" @click="openLogs(detail.data)"><Icon name="log" />日志</button>
      <button class="btn" :disabled="!detail.data || detail.data.state !== 'running'" @click="openExec(detail.data)">
        <Icon name="terminal" />终端
      </button>
      <button v-if="detail.data && detail.data.state === 'running'" class="btn danger" @click="act(detail.data, 'stop')">停止</button>
      <button v-else-if="detail.data" class="btn success" @click="act(detail.data, 'start')">启动</button>
    </template>
  </Modal>

  <!-- 日志 -->
  <Modal v-if="logs.open" :title="'日志 · ' + logs.name" icon-name="log" size="wide" @close="logs.open = false">
    <div class="dk-log-bar">
      <div class="seg">
        <button v-for="n in [100, 300, 1000]" :key="n" :class="{ on: logs.tail === n }" @click="loadLogs(n)">{{ n }} 行</button>
      </div>
      <label class="dk-check"><input type="checkbox" v-model="logs.auto" @change="syncLogTimer">自动刷新</label>
      <span class="grow"></span>
      <span class="dim">{{ logs.count }} 行</span>
    </div>
    <pre class="log-pre dk-log">{{ logs.lines.join('\n') || '（暂无输出）' }}</pre>
    <template #footer>
      <button class="btn" @click="loadLogs(logs.tail)"><Icon name="refresh" />刷新</button>
      <button class="btn" @click="openExec(logs.cont)" :disabled="!logs.cont || logs.cont.state !== 'running'">
        <Icon name="terminal" />终端
      </button>
      <button class="btn ghost" @click="logs.open = false">关闭</button>
    </template>
  </Modal>

  <!-- 拉取镜像 -->
  <Modal v-if="pull.open" title="拉取镜像" sub="按当前平台拉取，默认 linux/arm64" icon-name="download" @close="pull.open = false">
    <div class="field"><label>镜像名 *</label>
      <input class="input mono" v-model="pull.ref" placeholder="alpine:3.19" @keyup.enter="doPull">
    </div>
    <div class="field"><label>平台</label>
      <select class="input" v-model="pull.platform">
        <option value="linux/arm64">linux/arm64（手机）</option>
        <option value="linux/arm">linux/arm</option>
        <option value="linux/amd64">linux/amd64</option>
      </select>
    </div>
    <div class="note dk-note">
      <Icon name="info" class="ni s14" />
      <span>镜像仓库：<b>{{ settings.registry || 'Docker Hub 官方' }}</b>。国内直连慢时，到「运行时设置」换成镜像加速地址。</span>
    </div>
    <template #footer>
      <button class="btn" @click="pull.open = false">取消</button>
      <button class="btn primary" :disabled="pull.busy" @click="doPull">
        <span v-if="pull.busy" class="spinner"></span>{{ pull.busy ? '拉取中…' : '开始拉取' }}
      </button>
    </template>
  </Modal>

  <!-- 导入镜像 -->
  <Modal v-if="imp.open" title="导入镜像" sub="兼容 docker save 产物（tar）" icon-name="up" @close="imp.open = false">
    <div class="note dk-note" style="margin-bottom:12px">
      <Icon name="info" class="ni s14" />
      <span>本机不支持 build。请在其他机器执行 <span class="mono">docker save -o img.tar 镜像名</span>，再上传到这里。</span>
    </div>
    <div class="field">
      <label>选择 tar 文件</label>
      <input ref="fileEl" class="input" type="file" accept=".tar,.tar.gz,.tgz" @change="imp.file = $event.target.files[0]">
    </div>
    <div v-if="imp.file" class="dim" style="font-size:12px">{{ imp.file.name }} · {{ fmtB(imp.file.size) }}</div>
    <template #footer>
      <button class="btn" @click="imp.open = false">取消</button>
      <button class="btn primary" :disabled="imp.busy || !imp.file" @click="doImport">
        <span v-if="imp.busy" class="spinner"></span>{{ imp.busy ? '导入中…' : '上传并导入' }}
      </button>
    </template>
  </Modal>

  <!-- 安装应用 -->
  <Modal v-if="appIns.open" :title="'安装 ' + (appIns.app ? appIns.app.name : '')"
         :sub="appIns.app ? appIns.app.image : ''" icon-name="download"
         @close="appIns.open = false">
    <div class="fgrid">
      <div class="field"><label>容器名</label>
        <input class="input mono" v-model="appIns.name">
      </div>
      <div class="field" v-if="appIns.app && appIns.app.ports && appIns.app.ports.length">
        <label>宿主机端口</label>
        <input class="input mono" type="number" v-model.number="appIns.port">
      </div>
    </div>
    <div class="field"><label>数据目录（留空用默认）</label>
      <input class="input mono" v-model="appIns.data" :placeholder="'~/pocketpanel/apps/' + (appIns.app ? appIns.app.id : '')">
    </div>
    <div class="dk-hint" v-if="appIns.app && appIns.app.ports && appIns.app.ports.length">
      容器内监听 {{ appIns.app.ports[0].container }}，host 网络下直接占用宿主机端口；
      1024 以下绑不了。
    </div>
    <div class="dk-hint" v-if="appIns.app && appIns.app.tips">{{ appIns.app.tips }}</div>
    <div class="dk-hint warn" v-if="appIns.app && !appIns.app.verified">
      这个应用还没在 udocker 上跑通过，装完可能起不来；起不来先看容器日志。
    </div>
    <div v-if="appIns.error" class="dk-fielderr">{{ appIns.error }}</div>
    <template #footer>
      <button class="btn" @click="appIns.open = false">取消</button>
      <button class="btn primary" :disabled="appIns.busy" @click="doAppInstall">
        <span v-if="appIns.busy" class="spinner"></span>{{ appIns.busy ? '安装中…' : '开始安装' }}
      </button>
    </template>
  </Modal>

  <!-- 商店设置 -->
  <Modal v-if="storeOpen" title="应用商店" sub="清单地址与访问密钥" icon-name="settings"
         @close="storeOpen = false">
    <div class="field"><label>商店地址</label>
      <input class="input mono" v-model="storeForm.url" placeholder="https://store.sanhe.dpdns.org">
      <div class="dk-hint">留空则只用内置清单。</div>
    </div>
    <div class="field"><label>访问密钥（可选）</label>
      <input class="input mono" v-model="storeForm.key" placeholder="X-Store-Key">
    </div>
    <div v-if="storeForm.error" class="dk-fielderr">{{ storeForm.error }}</div>
    <template #footer>
      <button class="btn" @click="storeOpen = false">取消</button>
      <button class="btn primary" :disabled="storeForm.busy" @click="saveStore">
        {{ storeForm.busy ? '保存中…' : '保存并刷新' }}
      </button>
    </template>
  </Modal>

  <!-- 运行时设置 -->
  <Modal v-if="stOpen" title="运行时设置" icon-name="settings" @close="stOpen = false">
    <div class="field"><label>镜像仓库（加速）</label>
      <select class="input" v-model="stForm.registryPreset" @change="onRegistryPreset">
        <option value="">Docker Hub 官方（registry-1.docker.io）</option>
        <option value="https://docker.m.daocloud.io">DaoCloud 镜像（docker.m.daocloud.io）</option>
        <option value="custom">自定义…</option>
      </select>
    </div>
    <div v-if="stForm.registryPreset === 'custom'" class="field">
      <label>自定义地址</label>
      <input class="input mono" v-model="stForm.registry" placeholder="https://registry.example.com">
    </div>
    <div class="fgrid">
      <div class="field"><label>默认平台</label>
        <select class="input" v-model="stForm.platform">
          <option value="linux/arm64">linux/arm64（手机）</option>
          <option value="linux/arm">linux/arm</option>
          <option value="linux/amd64">linux/amd64</option>
        </select>
      </div>
      <div class="field"><label>默认执行模式</label>
        <select class="input" v-model="stForm.execmode">
          <option v-for="(desc, m) in caps.exec_modes" :key="m" :value="m">{{ m }} · {{ desc }}</option>
        </select>
      </div>
      <div class="field"><label>Engine API 端口</label><input class="input mono" type="number" v-model.number="stForm.port"></div>
      <div class="field"><label>日志默认行数</label><input class="input mono" type="number" v-model.number="stForm.log_tail"></div>
    </div>
    <div class="note dk-note warn">
      <Icon name="warning" class="ni s14" />
      <span>切换执行模式只影响新建容器；已有容器在详情页单独改（需要重启生效）。
        端口与 udocker 路径变更后需要重启 udockerd。</span>
    </div>
    <template #footer>
      <button class="btn" @click="stOpen = false">取消</button>
      <button class="btn primary" :disabled="stBusy" @click="saveSettings"><span v-if="stBusy" class="spinner"></span>保存</button>
    </template>
  </Modal>

  <!-- 安装 udocker -->
  <Modal v-if="ins.open" title="安装容器运行时" sub="udocker 客户端 + englib 引擎" icon-name="download"
         size="wide" @close="ins.open = false">
    <div v-if="ins.busy" class="dk-inst">
      <div class="dk-progress"><i :style="{ width: (ins.progress || 0) + '%' }"></i></div>
      <div class="dim" style="font-size:12px">{{ ins.progress || 0 }}% · {{ ins.stage || '准备中' }}</div>
      <pre class="log-pre dk-log">{{ ins.log.join('\n') }}</pre>
    </div>
    <template v-else>
      <div class="fgrid">
        <div class="field"><label>镜像源</label>
          <select class="input" v-model="ins.mirror">
            <option value="">自动测速（推荐）</option>
            <option v-for="m in ins.mirrors" :key="m.id" :value="m.url">{{ m.label }}</option>
          </select>
        </div>
        <div class="field"><label>版本</label><input class="input mono" v-model="ins.version" :placeholder="ins.defaultVersion"></div>
      </div>
      <div class="dk-sec">
        <div class="dk-sec-h"><b>测速</b><span class="dim">englib 约 46MB，直连 GitHub 常常只有几十 KB/s</span>
          <button class="btn xs" :disabled="ins.testing" @click="doSpeed">
            <span v-if="ins.testing" class="spinner"></span><Icon v-else name="activity" />开始测速
          </button>
        </div>
        <div v-for="r in ins.speed" :key="r.id" class="dk-speed">
          <span class="mono">{{ r.label }}</span>
          <span class="grow"></span>
          <span v-if="r.ok" class="tag" :class="r.ms < 800 ? 'g' : r.ms < 2500 ? 'y' : 'r'">{{ r.ms }} ms</span>
          <span v-else class="tag r">失败</span>
        </div>
        <div v-if="!ins.speed.length" class="dim dk-hint">还没测速；不测速就直接安装也可以（自动模式下会先测）</div>
      </div>
      <div class="note dk-note">
        <Icon name="info" class="ni s14" />
        <span>安装目录：<span class="mono">~/pocketpanel/data/runtime/udocker</span>；
          引擎解压到 <span class="mono">~/.udocker</span>。卸载不会删除镜像与容器。</span>
      </div>
    </template>
    <template #footer>
      <button class="btn" @click="ins.open = false">{{ ins.busy ? '后台运行' : '取消' }}</button>
      <button class="btn primary" :disabled="ins.busy" @click="doInstall">
        <Icon name="download" />开始安装
      </button>
    </template>
  </Modal>

  <!-- 通用任务 -->
  <Modal v-if="task.open" :title="task.title" icon-name="activity" size="wide" @close="task.open = false">
    <div class="dk-progress"><i :style="{ width: (task.progress || 0) + '%' }"></i></div>
    <div class="dim" style="font-size:12px;margin:6px 0 8px">
      {{ task.status === 'running' ? '执行中…' : task.status === 'success' ? '已完成' : task.status === 'failed' ? '失败' : task.status }}
      · {{ task.progress || 0 }}%
    </div>
    <pre class="log-pre dk-log">{{ task.log.join('\n') || '（等待输出）' }}</pre>
    <template #footer>
      <button class="btn ghost" @click="task.open = false">关闭</button>
    </template>
  </Modal>

  <!-- 边界说明 -->
  <Modal v-if="boundOpen" title="容器运行时的能力边界" sub="先把不能做的事说清楚，避免错误预期" icon-name="shield"
         size="wide" @close="boundOpen = false">
    <div v-for="b in allBounds" :key="b.title" class="dk-bound-full">
      <div class="dk-bi" :class="b.tone"><Icon :name="b.icon" /></div>
      <div>
        <b>{{ b.title }}</b>
        <p>{{ b.detail }}</p>
      </div>
    </div>
    <template #footer><button class="btn" @click="boundOpen = false">知道了</button></template>
  </Modal>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { api, pollTask, uploadFile, fmtB, fmtTime } from '../api'
import { ui, toast } from '../store'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

const emit = defineEmits(['go'])

const st = ref(null)
const containers = ref([])
const images = ref([])
const tab = ref('apps')
const busy = ref('')
const boundOpen = ref(false)
const homeDir = ref('')

const settings = computed(() => (st.value && st.value.settings) || {})
const caps = computed(() => (st.value && st.value.capabilities) || {})
const uck = computed(() => (st.value && st.value.udocker) || { installed: false, found: false })
const dm = computed(() => (st.value && st.value.daemon) || {})
const counts = computed(() => (st.value && st.value.counts) || { containers: 0, running: 0, images: 0 })
const ready = computed(() => !!(dm.value.reachable))

const svcState = computed(() => {
  const d = dm.value
  if (d.reachable) return { cls: 'run', dot: 'busy', label: '运行中' }
  if (d.state === 'run') return { cls: 'warn', dot: 'warn', label: '启动中' }
  if (d.state === 'fail') return { cls: 'fail', dot: 'fail', label: '启动失败' }
  if (!d.registered) return { cls: 'stop', dot: 'off', label: '未注册' }
  return { cls: 'stop', dot: 'off', label: '已停止' }
})

const boundList = computed(() => {
  const c = caps.value
  if (!c.network) return []
  return [
    { key: 'net', icon: 'globe', tone: 'info', title: c.network.title, short: '无 bridge、无隔离，容器直接占用宿主机端口' },
    { key: 'res', icon: 'cpu', tone: 'warn', title: c.resources.title, short: '没有 cgroups，不提供 CPU / 内存配额' },
    { key: 'vol', icon: 'disk', tone: 'warn', title: c.volumes.title, short: '持久化只用目录映射，docker volume 为空' },
    { key: 'bld', icon: 'package', tone: 'danger', title: c.build.title, short: '不能构建镜像，请 docker save 后导入' }
  ]
})

const allBounds = computed(() => {
  const c = caps.value
  if (!c.network) return []
  return [
    { ...c.network, icon: 'globe', tone: 'info' },
    { ...c.resources, icon: 'cpu', tone: 'warn' },
    { ...c.volumes, icon: 'disk', tone: 'warn' },
    { ...c.build, icon: 'package', tone: 'danger' },
    { ...c.compose, icon: 'grid', tone: 'info' },
    { title: '端口不能低于 1024', detail: c.ports.detail, icon: 'network', tone: 'warn' },
    { title: 'PRoot / Fakechroot 性能模式', detail: 'P1 默认；P2 关闭 seccomp 加速，兼容性更好但更慢；F1 Fakechroot 更快，依赖内核与 libfakechroot。切换需重建运行时配置并重启容器。', icon: 'activity', tone: 'info' }
  ]
})

// ---------------- 加载 ----------------

async function loadStatus() {
  try {
    st.value = await api('/api/docker/status')
    homeDir.value = (st.value && st.value.home) || homeDir.value
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function loadAll(force = false) {
  await loadStatus()
  if (!ready.value) { containers.value = []; images.value = []; return }
  const q = force ? '?refresh=1' : ''
  try {
    const [cs, ims] = await Promise.all([
      api('/api/docker/containers' + q),
      api('/api/docker/images' + q)
    ])
    containers.value = cs.containers || []
    images.value = ims.images || []
  } catch (e) {
    toast(e.message, 'err')
  }
}

function uptime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!t) return ''
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 60) return s + ' 秒'
  if (s < 3600) return Math.floor(s / 60) + ' 分钟'
  if (s < 86400) return Math.floor(s / 3600) + ' 小时'
  return Math.floor(s / 86400) + ' 天'
}

const restartLabel = (r) => ({ no: '不重启', always: 'always', 'on-failure': 'on-failure' }[r] || r)

// udockerd 输出的是 UTC ISO，直接给用户看是 "2026-10-30T10:07:54Z"，转成本地时间
function fmtWhen(iso) {
  if (!iso || iso.startsWith('0001-')) return '—'
  const t = Date.parse(iso)
  if (!t) return iso
  const d = new Date(t)
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// ---------------- 守护进程 ----------------

async function daemon(act) {
  busy.value = 'daemon'
  try {
    if (act === 'ensure') await api('/api/docker/daemon/ensure', { method: 'POST' })
    else await api('/api/docker/daemon/' + act, { method: 'POST' })
    toast({ start: '已启动', stop: '已停止', restart: '已重启', ensure: '已注册并启用' }[act] || '完成', 'ok')
    setTimeout(loadAll, 800)
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    busy.value = ''
  }
}

async function toggleAuto(on) {
  try {
    await api('/api/docker/daemon/' + (on ? 'enable' : 'disable'), { method: 'POST' })
    await loadStatus()
  } catch (e) { toast(e.message, 'err') }
}

async function daemonLogs() {
  try {
    const d = await api('/api/docker/daemon/logs?lines=300')
    task.title = 'udockerd 日志'
    task.log = d.lines || []
    task.progress = 100
    task.status = 'success'
    task.open = true
  } catch (e) { toast(e.message, 'err') }
}

// ---------------- 容器操作 ----------------

async function act(c, a) {
  if (a === 'remove' && !confirm(`删除容器「${c.name}」？容器内未映射的数据会丢失。`)) return
  try {
    await api(`/api/docker/container/${encodeURIComponent(c.id)}/${a}`, { method: 'POST' })
    toast({ start: '已启动', stop: '已停止', restart: '已重启', remove: '已删除' }[a], 'ok')
    detail.open = false
    setTimeout(() => loadAll(true), 500)
  } catch (e) { toast(e.message, 'err') }
}

function remove(c) { act(c, 'remove') }

async function openExec(c) {
  if (!c) return
  try {
    const s = await api(`/api/docker/container/${encodeURIComponent(c.id)}/exec`, {
      method: 'POST', body: { shell: 'sh', cols: 90, rows: 24 }
    })
    ui.attachTerm = s.id
    detail.open = false
    logs.open = false
    emit('go', 'terminal')
  } catch (e) {
    toast(e.message, 'err')
  }
}

// ---------------- 详情 / 日志 ----------------

const detail = reactive({ open: false, data: null, err: '' })

async function openDetail(c) {
  detail.open = true
  detail.data = null
  detail.err = ''
  try {
    detail.data = await api('/api/docker/container/' + encodeURIComponent(c.id))
  } catch (e) { detail.err = e.message }
}

const logs = reactive({ open: false, cid: '', name: '', lines: [], tail: 300, count: 0, auto: false, cont: null })
let logTimer = null

async function openLogs(c) {
  if (!c) return
  logs.open = true
  logs.cid = c.id
  logs.name = c.name
  logs.cont = c
  logs.lines = []
  await loadLogs(300)
  syncLogTimer()
}

async function loadLogs(tail) {
  logs.tail = tail
  try {
    const d = await api(`/api/docker/container/${encodeURIComponent(logs.cid)}/logs?tail=${tail}`)
    logs.lines = d.lines || []
    logs.count = d.count || 0
  } catch (e) { toast(e.message, 'err') }
}

function syncLogTimer() {
  if (logTimer) { clearInterval(logTimer); logTimer = null }
  if (logs.open && logs.auto) logTimer = setInterval(() => loadLogs(logs.tail), 3000)
}

// ---------------- 创建 ----------------

const wiz = reactive({
  open: false, busy: false, err: '', fe: {},
  form: { name: '', image: '', cmd: '', restart: 'no', execmode: '', workdir: '', user: '', start: true },
  envs: [], mounts: [], ports: []
})

function openCreate(im) {
  wiz.open = true
  wiz.err = ''
  wiz.fe = {}
  wiz.form = {
    name: '', image: (im && im.name) || '', cmd: '', restart: 'no',
    execmode: (settings.value && settings.value.execmode) || 'P1',
    workdir: '', user: '', start: true
  }
  wiz.envs = []
  wiz.mounts = []
  wiz.ports = []
}

async function submitCreate() {
  wiz.busy = true
  wiz.err = ''
  wiz.fe = {}
  try {
    const body = {
      ...wiz.form,
      env: wiz.envs.filter(e => e.key.trim()),
      mounts: wiz.mounts.filter(m => m.host.trim() && m.container.trim()),
      ports: wiz.ports.filter(p => p.host && p.container)
    }
    const r = await api('/api/docker/containers', { method: 'POST', body })
    toast(`容器 ${r.name} 已创建${r.started ? '并启动' : ''}`, 'ok')
    if (r.warnings && r.warnings.length) r.warnings.forEach(w => toast(w, 'warn'))
    wiz.open = false
    await loadAll(true)
  } catch (e) {
    wiz.err = e.message
    wiz.fe = e.fieldErrors || {}
  } finally { wiz.busy = false }
}

// ---------------- 目录选择 ----------------

const browse = reactive({ open: false, idx: -1, path: '', parent: '', dirs: [], err: '' })

function joinPath(base, name) {
  if (!base || base === '/') return '/' + name
  return base.replace(/\/+$/, '') + '/' + name
}

async function openBrowse(idx) {
  browse.open = true
  browse.idx = idx
  browse.err = ''
  const cur = wiz.mounts[idx] && wiz.mounts[idx].host
  await browseTo(cur || homeDir.value || '~')
}

async function browseTo(path) {
  browse.err = ''
  try {
    const d = await api('/api/files?path=' + encodeURIComponent(path || '~'))
    browse.path = d.path
    browse.parent = d.path.replace(/\/[^/]+\/?$/, '') || '/'
    browse.dirs = (d.entries || []).filter(e => e.is_dir).map(e => ({ name: e.name }))
  } catch (e) {
    browse.err = e.message
  }
}

async function mkHere() {
  const name = prompt('新建文件夹名称')
  if (!name) return
  try {
    await api('/api/file/mkdir', { method: 'POST', body: { path: browse.path, name } })
    await browseTo(browse.path)
  } catch (e) { browse.err = e.message }
}

function pickDir() {
  if (browse.idx >= 0 && wiz.mounts[browse.idx]) wiz.mounts[browse.idx].host = browse.path
  browse.open = false
}

// ---------------- 镜像 ----------------

const pull = reactive({ open: false, ref: '', platform: '', busy: false })

function openPull() {
  pull.open = true
  pull.ref = ''
  pull.platform = (settings.value && settings.value.platform) || 'linux/arm64'
}

function pullAgain(im) {
  pull.open = true
  pull.ref = im.name
  pull.platform = (settings.value && settings.value.platform) || 'linux/arm64'
}

async function doPull() {
  if (!pull.ref.trim()) { toast('请填写镜像名', 'err'); return }
  pull.busy = true
  try {
    const r = await api('/api/docker/images/pull', { method: 'POST', body: { ref: pull.ref.trim(), platform: pull.platform } })
    pull.open = false
    watchTask(r.id, '拉取镜像 ' + pull.ref.trim(), () => loadAll(true))
  } catch (e) { toast(e.message, 'err') } finally { pull.busy = false }
}

async function removeImage(im) {
  if (!confirm(`删除镜像 ${im.name}？`)) return
  try {
    await api('/api/docker/images/remove', { method: 'POST', body: { ref: im.name } })
    toast('镜像已删除', 'ok')
    await loadAll(true)
  } catch (e) { toast(e.message, 'err') }
}

const imp = reactive({ open: false, file: null, busy: false })
const fileEl = ref(null)

function openImport() {
  imp.open = true
  imp.file = null
  imp.busy = false
}

async function doImport() {
  if (!imp.file) return
  imp.busy = true
  try {
    const d = await api('/api/docker/import/dir')
    await uploadFile(imp.file, d.dir, () => {})
    const path = d.dir.replace(/\/+$/, '') + '/' + imp.file.name
    const r = await api('/api/docker/images/import', { method: 'POST', body: { path } })
    imp.open = false
    watchTask(r.id, '导入镜像', () => loadAll(true))
  } catch (e) { toast(e.message, 'err') } finally { imp.busy = false }
}

// ---------------- 设置 ----------------

const stOpen = ref(false)
const stBusy = ref(false)
const stForm = reactive({ registryPreset: '', registry: '', platform: 'linux/arm64', execmode: 'P1', port: 2375, log_tail: 300 })

function openSettings() {
  const s = settings.value || {}
  stForm.registryPreset = !s.registry ? '' : (s.registry === 'https://docker.m.daocloud.io' ? s.registry : 'custom')
  stForm.registry = s.registry || ''
  stForm.platform = s.platform || 'linux/arm64'
  stForm.execmode = s.execmode || 'P1'
  stForm.port = s.port || 2375
  stForm.log_tail = s.log_tail || 300
  stOpen.value = true
}

function onRegistryPreset() {
  if (stForm.registryPreset !== 'custom') stForm.registry = stForm.registryPreset
}

async function saveSettings() {
  stBusy.value = true
  try {
    await api('/api/docker/settings', {
      method: 'POST',
      body: {
        registry: stForm.registryPreset === 'custom' ? stForm.registry : stForm.registryPreset,
        platform: stForm.platform, execmode: stForm.execmode,
        port: stForm.port, log_tail: stForm.log_tail
      }
    })
    stOpen.value = false
    toast('设置已保存', 'ok')
    await loadStatus()
  } catch (e) { toast(e.message, 'err') } finally { stBusy.value = false }
}

// ---------------- 安装 / 任务 ----------------

const ins = reactive({
  open: false, busy: false, progress: 0, stage: '', log: [], mirrors: [],
  mirror: '', version: '', defaultVersion: '', speed: [], testing: false
})

async function openInstall() {
  ins.open = true
  ins.speed = []
  try {
    const [stt, meta] = await Promise.all([
      api('/api/docker/install/status'), api('/api/docker/install/meta')
    ])
    ins.mirrors = meta.mirrors || []
    ins.defaultVersion = meta.version || ''
    ins.version = ins.version || ''
    ins.busy = !!stt.busy
    if (stt.busy) watchTask(stt.task_id, '安装容器运行时', () => loadAll(true), true)
  } catch (e) { toast(e.message, 'err') }
}

async function doSpeed() {
  ins.testing = true
  try {
    const d = await api('/api/docker/install/speedtest', { method: 'POST' })
    ins.speed = d.results || []
  } catch (e) { toast(e.message, 'err') } finally { ins.testing = false }
}

async function doInstall() {
  ins.busy = true
  ins.log = []
  ins.progress = 0
  try {
    const r = await api('/api/docker/install', { method: 'POST', body: { mirror: ins.mirror, version: ins.version } })
    watchTask(r.id, '安装容器运行时', () => loadAll(true), true)
  } catch (e) {
    ins.busy = false
    toast(e.message, 'err')
  }
}

async function uninstall() {
  if (!confirm('卸载 udocker 客户端？~/.udocker 中的镜像与容器会保留。')) return
  try {
    await api('/api/docker/uninstall', { method: 'POST' })
    toast('已卸载客户端（镜像与容器保留）', 'ok')
    await loadAll(true)
  } catch (e) { toast(e.message, 'err') }
}

const task = reactive({ open: false, title: '', log: [], progress: 0, status: '', id: '' })

function watchTask(id, title, done, installMode = false) {
  task.open = !installMode
  task.title = title
  task.log = []
  task.progress = 0
  task.status = 'running'
  task.id = id
  const target = installMode ? ins : task
  pollTask(id, (t) => {
    target.log = (t.output || '').split('\n').filter(Boolean).slice(-400)
    target.progress = t.progress || 0
    task.progress = t.progress || 0
    task.status = t.status
    if (installMode) {
      ins.busy = t.status === 'running'
      ins.stage = t.status === 'running' ? '安装中…' : (t.status === 'success' ? '完成' : '失败')
      ins.log = target.log
    }
    if (t.status !== 'running') {
      if (t.status === 'success') {
        toast(title + '完成', 'ok')
        if (done) done()
      } else {
        toast(title + '失败：' + (t.output || '').split('\n').filter(Boolean).slice(-1)[0], 'err')
      }
    }
  })
}

// ---------------- 应用商店 ----------------

const store = reactive({
  apps: [], source: '', ok: true, error: '', loading: false, installed: {},
})
const appIns = reactive({
  open: false, app: null, name: '', port: '', data: '', busy: false, error: '',
})
const storeOpen = ref(false)
const storeForm = reactive({ url: '', key: '', busy: false, error: '' })

async function loadStore(force = false) {
  store.loading = true
  try {
    const r = await api('/api/appstore/catalog' + (force ? '?force=1' : ''))
    store.apps = r.apps || []
    store.source = r.source || ''
    store.ok = !!(r.store && r.store.ok)
    store.error = (r.store && r.store.error) || ''
    const ins = await api('/api/appstore/installed')
    store.installed = ins.items || {}
  } catch (e) {
    store.ok = false
    store.error = e.message || String(e)
  } finally {
    store.loading = false
  }
}

function goApps() {
  tab.value = 'apps'
  if (!store.apps.length) loadStore()
}

function goStore() {
  location.hash = '#/store'
}

function goContainer(a) {
  tab.value = 'containers'
  const hit = containers.value.find((x) => ((x.labels || {})['pp.app'] === a.id))
  if (hit) openDetail(hit)
}

function openAppInstall(a) {
  appIns.app = a
  appIns.name = a.id
  appIns.port = a.ports && a.ports.length ? a.ports[0].host : ''
  appIns.data = ''
  appIns.error = ''
  appIns.busy = false
  appIns.open = true
}

async function doAppInstall() {
  if (!appIns.app) return
  appIns.busy = true
  appIns.error = ''
  try {
    const r = await api('/api/appstore/install', {
      method: 'POST',
      body: {
        id: appIns.app.id, name: appIns.name,
        port: appIns.port, data: appIns.data.trim(),
      },
    })
    const label = appIns.app.name
    appIns.open = false
    watchTask(r.id, '安装 ' + label, () => {
      loadStore(true)
      loadAll(true)
      tab.value = 'containers'
    })
  } catch (e) {
    appIns.error = e.message || String(e)
  } finally {
    appIns.busy = false
  }
}

async function openStore() {
  storeForm.error = ''
  try {
    const r = await api('/api/appstore/settings')
    storeForm.url = r.settings.url || ''
    storeForm.key = r.settings.key || ''
  } catch (e) { /* 读不到就用空值 */ }
  storeOpen.value = true
}

async function saveStore() {
  storeForm.busy = true
  storeForm.error = ''
  try {
    await api('/api/appstore/settings', {
      method: 'POST', body: { url: storeForm.url, key: storeForm.key },
    })
    storeOpen.value = false
    await loadStore(true)
    toast('商店设置已保存', 'ok')
  } catch (e) {
    storeForm.error = e.message || String(e)
  } finally {
    storeForm.busy = false
  }
}

// ---------------- Compose ----------------

const cp = reactive({ text: '', env: '', project: '', base: '', plan: null, busy: false })

async function preview() {
  cp.busy = true
  cp.plan = null
  try {
    cp.plan = await api('/api/docker/compose/preview', {
      method: 'POST',
      body: { text: cp.text, env: cp.env, project: cp.project, base_dir: cp.base }
    })
  } catch (e) {
    toast(e.message, 'err')
  } finally { cp.busy = false }
}

async function composeUp() {
  try {
    const r = await api('/api/docker/compose/up', {
      method: 'POST',
      body: { text: cp.text, env: cp.env, project: cp.project, base_dir: cp.base }
    })
    watchTask(r.id, 'Compose 启动', () => { cp.plan = null; loadAll(true); tab.value = 'containers' })
  } catch (e) { toast(e.message, 'err') }
}

// ---------------- 生命周期 ----------------

let timer = null

onMounted(async () => {
  await loadAll()
  loadStore()
  timer = setInterval(() => {
    if (document.hidden) return
    loadStatus()
    if (ready.value && (tab.value === 'containers' || tab.value === 'images')) {
      api('/api/docker/containers').then(d => { containers.value = d.containers || [] }).catch(() => {})
      if (tab.value === 'images') api('/api/docker/images').then(d => { images.value = d.images || [] }).catch(() => {})
    }
  }, 8000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (logTimer) clearInterval(logTimer)
})
</script>
