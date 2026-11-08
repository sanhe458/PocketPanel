"""应用商店的内置清单。

单独放一个文件，是为了让「有哪些应用」和「怎么装」分开：
appstore.py 只管流程，这里只管数据。`tests/test_appstore.py` 会拿它和
`appstore-worker/seed/catalog.json` 逐条比对，防止线上清单和内置清单漂移。

字段约定（新增应用照着填）：

  id          小写短横线，装完的目录名和容器 label 都用它，改了等于换一个应用
  ports       host 网络，容器里听哪个端口宿主机就是哪个，所以 host 必须等于
              应用真实监听的端口；只有第一个端口允许用户改
  port_fixed  true 表示端口写死在镜像里（entrypoint 带参数那种），前端锁住输入框
  command     覆盖镜像 CMD；`{{port}}` 会替换成用户选的端口
  env         `{{port}}` / `{{data}}` 会展开；用户装的时候还能覆盖值
  volumes     host 用 `{{data}}/xxx`，实际落在 ~/pocketpanel/apps/<id>/
  verified    真机 Android + udocker 上装起来并打开过首页才算 true

尺寸是 arm64 镜像压缩后的大小，实际占盘要乘 2–3 倍，写个约数就行。

**上架前先过这三关**（都是真机实测踩出来的，不是理论风险）：

  1. 入口不能是 tini。proot 下 tini 会 FATAL —— filebrowser 报
     `exec /init.sh failed`，n8n 报 `tcsetpgrp failed: Permission denied`。
     FileBrowser 用 entrypoint 覆盖绕过过一次，但用户用下来明确说手机上
     不能用，已永久下架；n8n 直接撤。
  2. 不能依赖 proot 没实现的系统调用。PostgreSQL 的 initdb 卡在
     `shmget ... Function not implemented`，无解。
  3. 不能靠 supervisor 常驻。linkding 在 proot 下会死循环
     `Unlinking stale socket`，容器起不来。

顺带一条经验：镜像超过 500MB 的慎选，手机上解层要几分钟，
装在兜里那台机器上占空间也太贵。n8n（1.4G 解压后）就是这么被劝退的。

反过来，**纯 Go 静态二进制（CGO_ENABLED=0）是最省心的一类**：没有
`/lib64/ld-linux` 这种 Android 上根本不存在的解释器，也不 fork 子进程。
new-api 就是这种，解压后 230MB、常驻 73MB、冷启动 2 秒。上架前先
`file` 一下二进制，看到 `statically linked` 基本就稳了一半。
"""

UPDATED = "2026-11-07"

# 分类：前端按这个出筛选条，顺序就是展示顺序
CATS = [
    ("web", "网站"),
    ("files", "文件"),
    ("media", "媒体"),
    ("dev", "开发"),
    ("db", "数据库"),
    ("monitor", "监控"),
    ("note", "笔记"),
    ("security", "安全"),
    ("tool", "工具"),
    ("other", "其它"),
]

APPS = [
    # ---------------- 网站 ----------------
    {
        "id": "nginx",
        "name": "Nginx",
        "summary": "静态站点服务器",
        "desc": "把网页文件放进数据目录，用手机当小型网站服务器。"
                "用 unprivileged 版本，监听 8080。",
        "category": "web",
        "icon": "🌐",
        "icon_name": "globe",
        "image": "nginxinc/nginx-unprivileged:alpine",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 8080, "host": 8080, "label": "HTTP"}],
        "port_fixed": True,
        "volumes": [
            {"container": "/usr/share/nginx/html", "host": "{{data}}/html",
             "label": "网站目录"},
        ],
        "env": [{"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 20MB",
        "website": "https://nginx.org",
        "tips": "网页放到数据目录的 html 子目录，改完刷新即可。"
                "端口固定 8080：镜像里的配置写死了，改端口要自己挂 nginx.conf。",
        "verified": True,
    },

    # ---------------- 文件 ----------------
    {
        "id": "openlist",
        "name": "OpenList",
        "summary": "网盘聚合与文件列表",
        "desc": "AList 的社区分支。把阿里云盘、夸克、百度、WebDAV、"
                "S3 挂进同一棵目录树，自带 WebDAV 和直链。",
        "category": "files",
        "icon": "🗂️",
        "icon_name": "layers",
        "image": "openlistteam/openlist:latest",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 5244, "host": 5244, "label": "Web"}],
        "volumes": [
            {"container": "/opt/openlist/data", "host": "{{data}}/data", "label": "数据"},
            {"container": "/opt/openlist/share", "host": "{{data}}/share", "label": "共享目录"},
        ],
        "env": [{"key": "TZ", "value": "Asia/Shanghai", "label": "时区"},
                {"key": "UMASK", "value": "022", "label": "文件权限掩码"},
                # 不给的话首次密码写进日志，很多环境根本不打印，用户会卡在登录页
                {"key": "OPENLIST_ADMIN_PASSWORD", "value": "openlist",
                 "label": "初始密码（首启生效）"}],
        "restart": "always",
        "size": "约 60MB",
        "website": "https://oplist.org",
        "tips": "账号 admin / openlist，进去第一件事是改密码。"
                "密码只在首次启动、数据库为空时生效。",
        "verified": True,
    },
    {
        "id": "syncthing",
        "name": "Syncthing",
        "summary": "设备间文件同步",
        "desc": "点对点同步，不经过第三方服务器。手机 ↔ 电脑 ↔ NAS 互相同步照片、"
                "笔记、配置都行，冲突会留两份不覆盖。",
        "category": "files",
        "icon": "🔄",
        "icon_name": "refresh",
        "image": "syncthing/syncthing:latest",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 8384, "host": 8384, "label": "Web UI"}],
        "volumes": [
            {"container": "/var/syncthing", "host": "{{data}}/data", "label": "配置与数据"},
        ],
        "env": [{"key": "STGUIADDRESS", "value": "0.0.0.0:{{port}}", "label": "监听地址"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 20MB",
        "website": "https://syncthing.net",
        "tips": "管理界面没有密码，只在内网或隧道后面用。"
                "同步端口 22000 是固定的，同机不要装第二个 Syncthing。",
        "verified": True,
    },

    # ---------------- 媒体 ----------------
    {
        "id": "navidrome",
        "name": "Navidrome",
        "summary": "自建音乐流媒体",
        "desc": "把本地音乐变成网易云那样的在线曲库：网页播放、手机 App（Subsonic 协议）、"
                "歌词封面、按专辑歌手浏览都有。",
        "category": "media",
        "icon": "🎵",
        "icon_name": "play",
        "image": "deluan/navidrome:latest",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 4533, "host": 4533, "label": "Web"}],
        "volumes": [
            {"container": "/data", "host": "{{data}}/data", "label": "数据库与缓存"},
            {"container": "/music", "host": "{{data}}/music", "label": "音乐目录"},
        ],
        "env": [{"key": "ND_PORT", "value": "{{port}}", "label": "监听端口"},
                {"key": "ND_MUSICFOLDER", "value": "/music", "label": "音乐目录"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 91MB",
        "website": "https://www.navidrome.org",
        "tips": "把歌放进「音乐目录」子目录，后台扫描完就能听。"
                "首次启动会建库，歌多的时候多等一会儿。",
        "verified": True,
    },
    # ---------------- 开发 ----------------
    {
        "id": "gitea",
        "name": "Gitea",
        "summary": "轻量 Git 服务",
        "desc": "自己搭一个 GitHub：仓库、Issue、PR、Actions 都有，"
                "资源占用比 GitLab 小两个数量级，手机上跑得动。",
        "category": "dev",
        "icon": "🐙",
        "icon_name": "code",
        "image": "gitea/gitea:1-rootless",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 3000, "host": 3000, "label": "HTTP"}],
        "volumes": [
            {"container": "/var/lib/gitea", "host": "{{data}}/data", "label": "仓库与配置"},
            {"container": "/etc/gitea", "host": "{{data}}/etc", "label": "全局配置"},
        ],
        "env": [{"key": "GITEA__server__HTTP_PORT", "value": "{{port}}", "label": "监听端口"},
                {"key": "GITEA__server__DISABLE_SSH", "value": "true", "label": "关闭 SSH（走 HTTP）"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 65MB",
        "website": "https://gitea.com",
        "tips": "首次打开是安装页：数据库选 SQLite3，其它保持默认，点「安装」就行。"
                "SSH 已关闭，克隆用 http:// 地址。",
        "verified": True,
    },
    # ---------------- 数据库 ----------------
    {
        "id": "redis",
        "name": "Redis",
        "summary": "内存键值缓存",
        "desc": "拿来做缓存、队列、计数器。单线程但够快，"
                "给 n8n、Gitea 这类应用当外部缓存也能用。",
        "category": "db",
        "icon": "🧱",
        "icon_name": "layers",
        "image": "redis:7-alpine",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 6379, "host": 6379, "label": "TCP"}],
        "volumes": [
            {"container": "/data", "host": "{{data}}/data", "label": "持久化目录"},
        ],
        "env": [{"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "command": ["redis-server", "--port", "{{port}}", "--appendonly", "yes",
                    "--dir", "/data"],
        "restart": "always",
        "size": "约 17MB",
        "website": "https://redis.io",
        "tips": "默认没有密码，只给内网用。要密码就在启动参数里加 --requirepass。"
                "开了 AOF，重启数据还在。",
        "verified": True,
    },

    # ---------------- 监控 ----------------
    {
        "id": "uptime-kuma",
        "name": "Uptime Kuma",
        "summary": "自托管监控面板",
        "desc": "HTTP / TCP / Ping 探活，带状态页和通知渠道。"
                "适合盯着家里的小服务。",
        "category": "monitor",
        "icon": "📈",
        "icon_name": "activity",
        "image": "louislam/uptime-kuma:1",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 3001, "host": 3001, "label": "Web"}],
        "volumes": [
            {"container": "/app/data", "host": "{{data}}/data", "label": "数据"},
        ],
        "env": [{"key": "UPTIME_KUMA_PORT", "value": "{{port}}", "label": "端口"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 90MB",
        "website": "https://uptime.kuma.pet",
        "tips": "首次打开会让你创建管理员账号。和面板自带的 M09 告警二选一即可："
                "这个能画曲线，M09 负责发邮件。",
        "verified": True,
    },
    # ---------------- 笔记 / 安全 ----------------
    {
        "id": "memos",
        "name": "Memos",
        "summary": "轻量卡片笔记",
        "desc": "像发微博一样记东西：一句话、一张图、一个链接，打上标签自动成时间线。"
                "自带 API，手机上用第三方客户端也行。",
        "category": "note",
        "icon": "📝",
        "icon_name": "edit",
        "image": "neosmemo/memos:stable",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 5230, "host": 5230, "label": "Web"}],
        "volumes": [
            {"container": "/var/opt/memos", "host": "{{data}}/data", "label": "数据"},
        ],
        "env": [{"key": "MEMOS_PORT", "value": "{{port}}", "label": "监听端口"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 24MB",
        "website": "https://www.usememos.com",
        "tips": "第一个注册的账号就是管理员，注册完把「允许注册」关掉。"
                "所有笔记在一个 SQLite 文件里，备份数据目录就行。",
        "verified": True,
    },
    {
        "id": "vaultwarden",
        "name": "Vaultwarden",
        "summary": "自建密码库",
        "desc": "Bitwarden 的轻量实现，浏览器插件、手机 App、桌面端全兼容。"
                "密码只存在自己手机上，不经过第三方。",
        "category": "security",
        "icon": "🔐",
        "icon_name": "key",
        "image": "vaultwarden/server:alpine",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 80, "host": 8222, "label": "Web"}],
        "volumes": [
            {"container": "/data", "host": "{{data}}/data", "label": "数据库与附件"},
        ],
        "env": [{"key": "ROCKET_PORT", "value": "{{port}}", "label": "监听端口"},
                {"key": "SIGNUPS_ALLOWED", "value": "true", "label": "允许注册（首次）"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 64MB",
        "website": "https://github.com/dani-garcia/vaultwarden",
        "tips": "打开先注册一个账号（第一个就是管理员），然后回来把"
                "「允许注册」改成 false 保存。正式用建议套 HTTPS，"
                "否则 Bitwarden App 不给连。",
        "verified": True,
    },

    # ---------------- 工具 ----------------
    {
        "id": "new-api",
        "name": "New API",
        "summary": "AI 模型统一网关",
        "desc": "把 OpenAI、Claude、Gemini、DeepSeek 这些上游合成一个 OpenAI 兼容接口，"
                "带密钥、额度、用量统计和模型映射。自己用，或者分给几个人用都行。",
        "category": "tool",
        "icon": "🤖",
        "icon_name": "cpu",
        "image": "calciumion/new-api:latest",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 3000, "host": 3002, "label": "Web"}],
        "volumes": [
            {"container": "/data", "host": "{{data}}/data", "label": "数据库与日志"},
        ],
        "env": [{"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        # 镜像 ENTRYPOINT 是 /new-api，没有 CMD；这里给的就是它的启动参数
        "command": ["--port", "{{port}}", "--log-dir", "/data/logs"],
        "restart": "always",
        "size": "约 76MB",
        "website": "https://github.com/QuantumNous/new-api",
        "tips": "第一次打开先设管理员账号，再去「渠道」页加 Base URL 和密钥。"
                "默认 SQLite，库和日志都在数据目录，备份整个目录即可。"
                "手机并发能力有限，512MB 内存的机器建议同时不超过 3–5 个请求。",
        "verified": True,
    },
    {
        "id": "gotify",
        "name": "Gotify",
        "summary": "自建推送服务",
        "desc": "往一个 HTTP 接口 POST 一条消息，手机 App 立刻弹通知。"
                "脚本跑完、备份失败、门开了都能推，安卓上不用挂梯子。",
        "category": "tool",
        "icon": "🔔",
        "icon_name": "bell",
        "image": "gotify/server:latest",
        "arch": ["arm64", "amd64"],
        "ports": [{"container": 80, "host": 8070, "label": "Web"}],
        "volumes": [
            {"container": "/app/data", "host": "{{data}}/data", "label": "数据"},
        ],
        "env": [{"key": "GOTIFY_SERVER_PORT", "value": "{{port}}", "label": "监听端口"},
                {"key": "TZ", "value": "Asia/Shanghai", "label": "时区"}],
        "restart": "always",
        "size": "约 52MB",
        "website": "https://gotify.net",
        "tips": "默认 admin / admin，先改密码，再建一个「应用」拿 token。"
                "推送就是 curl -X POST 'http://IP:8070/message?token=xxx' "
                "-F 'title=标题' -F 'message=内容'。",
        "verified": True,
    },
    # ---------------- 工具箱 ----------------
    {
        "id": "alpine",
        "name": "Alpine 工具箱",
        "summary": "一个空的 Linux 环境",
        "desc": "没想好装什么的时候，先用它练手：进终端随便敲，"
                "删掉重建都不心疼。",
        "category": "tool",
        "icon": "🧰",
        "icon_name": "package",
        "image": "alpine:latest",
        "arch": ["arm64", "amd64"],
        "ports": [],
        "volumes": [
            {"container": "/work", "host": "{{data}}/work", "label": "工作目录"},
        ],
        "env": [],
        "command": ["/bin/sh", "-c", "while true; do sleep 3600; done"],
        "restart": "no",
        "size": "约 4MB",
        "website": "https://alpinelinux.org",
        "tips": "容器详情页点「终端」就能进去。",
        "verified": True,
    },
]
