# PocketPanel · 真机运行版

已在 **华为 BLA-AL00（Mate 10 / Android 10 / Termux / aarch64 / 无 root）** 上部署并验证。

## 访问

| 项 | 值 |
|---|---|
| 局域网地址 | `http://<手机IP>:8888` |
| 用户名 | `admin` |
| 密码 | 首次启动随机生成并打印在控制台，登录后可在「设置」修改 |
| 会话 | HttpOnly Cookie，24 小时 |

外网访问走设备已有的 frp，或本地建 SSH 隧道：

```bash
ssh -p <SSH端口> -L 8888:127.0.0.1:8888 <用户名>@<主机>
# 浏览器打开 http://127.0.0.1:8888
```

## 功能

| 模块 | 能力 |
|---|---|
| 账户 | 用户名 + 密码登录（等时比较）、修改用户名与密码（验证旧密码）、改后旧会话失效 |
| 概览 | CPU（8 秒采样）、内存、存储、电池、温度、进程数、面板运行时长、CPU 曲线，3 秒轮询 |
| 服务 | runit 服务列表、启动 / 停止 / 重启、运行时长、面板自身保护 |
| 进程 | 按内存排序、完整命令行、PID/PPID/状态、结束进程、关键进程保护 |
| 文件 | 见下方专节：分片上传、回收站、历史版本、分享、搜索、压缩解压、远程下载、权限、收藏 |
| 网络 | 网络接口（IPv4/MTU/状态）、20 个常用本地端口的可连性探测 |
| 日志 | runit 服务日志尾读，100–1000 行可调，自动去掉 TAI64N 前缀 |
| 终端 | 命令执行、输出回传、历史保留 |
| 任务 | 执行记录、退出码、输出，最近 50 条 |
| 主题 | 深色 / 浅色，偏好持久化 |
| 移动端 | 底部 5 项 Tab，「更多」打开侧栏抽屉，9 个页面无横向溢出 |

二进制 **5.9 MB**，空载内存 **5 MB 左右**。

## 文件管理（M05）

以宝塔的文件管理为基线，移动端优先。所有长耗时操作都走异步任务，可以切页离开。

| 能力 | 说明 |
|---|---|
| 上传 | 点击选择或桌面拖拽；1MB 分片、**断点续传**、按分片显示进度 |
| 下载 | 流式输出，支持 Range 断点 |
| 新建 / 重命名 / 复制 / 移动 | 批量操作，重名自动加序号，拒绝复制到自身子目录 |
| 删除 | **进回收站**，不是直接销毁 |
| 回收站 | 列表、恢复、永久删除、清空、保留天数可配、每小时自动清理 |
| 历史版本 | 保存前自动快照，保留最近 N 份，可查看、可恢复；恢复前再快照当前内容 |
| 在线编辑 | 全屏等宽编辑器、字符计数、未保存关闭确认 |
| 搜索 | 文件名 + 内容、后缀过滤、递归开关、8 秒超时、200 条上限 |
| 压缩 / 解压 | zip 或 tar.gz；解压到同名目录或指定目录 |
| 分享 | 只读链接、有效期 1h/24h/7d/30d、4 位提取码、可撤销、错误限流 |
| 远程下载 | URL → 后台下载，`.part` 落盘完成后再改名 |
| 权限 | 八进制输入、常用值快捷、递归需二次确认 |
| 收藏夹 | 最多 30 项，失效路径标注 |
| 终端联动 | 从当前目录打开终端，`pwd` 与文件页一致 |

### 与宝塔功能面的差异

| 不实现 | 理由 |
|---|---|
| chmod 勾选矩阵 | 移动端 9 宫格占空间且易误触，数字输入两次点击完成 |
| chown | 无 root，必然 EPERM |
| 回收站开关 | 关闭回收站等于删除即销毁，风险大于收益；改为可配保留天数 |
| 语法高亮 | 需 CodeMirror/Prism（200KB+），与单文件零依赖冲突 |
| 木马特征扫描 | 特征匹配对混淆 webshell 无效，误报会让用户误以为「扫过就安全」；挂 M12 延后 |

### 三条设计约束

1. **删除是 rename。** 同分区内 `os.Rename` 是元数据操作，20GB 目录也能瞬间移入回收站；跨分区才回退成复制后删除。
2. **版本快照只对 8MB 以下的文件做。** 否则编辑一个 500MB 的日志就能把磁盘写满。版本文件名用纳秒时间戳，同一秒内连续保存也不会排错序。
3. **搜索是异步任务。** 8 秒软超时、200 条硬上限，到限即返回并标记 `truncated`；二进制文件读前 512 字节检测 `\0` 直接跳过。

## 技术选型

- **后端**：Go 1.21，纯标准库，无第三方依赖
- **前端**：单 HTML，嵌入二进制，无构建步骤，ES2017 语法基线
- **进程**：runit（termux-services），随 Termux 启动自动拉起
- **存储**：JSON 配置文件，会话在内存

## 设备适配（重要）

Android 10 对普通 app 的限制导致常规采集方式大面积失效，已逐项替代。**这些结论都是实测的，不是推测：**

| 常规方案 | 实际结果 | 现方案 |
|---|---|---|
| `/proc/stat` | Permission denied | 汇总 `top` 进程行的 `%CPU`，除以物理核数 |
| `/proc/loadavg` | Permission denied | 不显示负载，改显示进程数 |
| `/sys/class/thermal/*` | Permission denied | `termux-battery-status` 的电池温度 |
| `/sys/class/power_supply/*` | Permission denied | `termux-battery-status` |
| `/proc/uptime` | Permission denied | 显示面板自身运行时长 |
| `syscall.Statfs` | 进程 SIGSEGV | `df -k` 解析，缓存 30 秒 |
| `ss` | 命令不存在 | 本地端口 TCP 探测 |
| `netstat -tuln` | `no support for AF INET (tcp)` | 同上 |
| `lsof -i` | 空输出 | 同上 |
| `/proc/net/tcp` | 空 | 同上 |
| 网卡 MAC | Permission denied | 显示「系统限制不可读」 |
| `crontab` / `crond` | 未安装 | 计划任务需内置调度器 |

**CPU 采样的两个坑：**

1. `top -b -n1` 首次采样没有 CPU 汇总行（缺少时间差数据），必须 `-n2`；而 `-n2` 至少 1.1 秒，不能放在请求路径里 → 改为后台协程每 8 秒采样、请求只读缓存。overview 响应从 750ms 降到 5ms。
2. `top` 汇总行的 idle 恒为满值（因为读不到 `/proc/stat`），不可信；**进程行的 `%CPU` 才是真实值**。所以使用率 = 所有进程 `%CPU` 之和 ÷ 物理核数。设备实际 8 核，但 Termux 的 cpuset 只给 2 核（`nproc` 返回 2），界面上分开展示这两个数字。

## 编译（必须注意）

**必须用 `GOOS=android`，不能用 `GOOS=linux`。**

linux/arm64 的二进制在这台设备上执行 `exec` 子进程时会 SIGSEGV（fork/exec 与 Android 的 seccomp 不兼容），表现是服务反复重启、接口返回空。崩溃现场寄存器里能看到 `/data/data/com.termux/files/usr/bin/sh`，可作为判断依据。

```bash
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o pocketpanel .
```

提交前建议跑一次 `go vet ./...`。它会发现 `Task` 这类含互斥锁的结构体被按值拷贝的问题——副本里的锁与原文无关，后续加锁等于没加。

## 部署 / 更新

直接覆盖运行中的二进制会失败（ETXTBSY），必须走「上传到 .new + mv 原子替换」：

```bash
# 1. 编译
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o pocketpanel .

# 2. 上传为 .new
scp -P <SSH端口> pocketpanel <用户名>@<主机>:~/pocketpanel/bin/pocketpanel.new

# 3. 原子替换并重启
ssh -p <SSH端口> <用户名>@<主机> \
  'cd ~/pocketpanel/bin && chmod +x pocketpanel.new && mv -f pocketpanel.new pocketpanel && sv restart pocketpanel'
```

`deploy.sh` 封装了这三步。

runit 的 run 脚本必须显式 `export HOME`，否则服务启动时环境变量不完整：

```sh
#!/data/data/com.termux/files/usr/bin/sh
export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PATH
cd $HOME/pocketpanel || exit 1
exec $HOME/pocketpanel/bin/pocketpanel 2>&1
```

## 运维命令

```bash
sv status pocketpanel                          # 状态
sv restart pocketpanel                         # 重启
tail -f $PREFIX/var/log/sv/pocketpanel/current # 日志
cat ~/pocketpanel/data/config.json             # 配置（用户名 + 密码哈希）
```

忘记密码：删除 `~/pocketpanel/data/config.json` 后重启，会重新生成账号并把密码打印到日志。旧配置没有 `username` 字段时会自动补为 `admin`。

## 接口

```
POST   /api/login              {username, password}
POST   /api/logout
GET    /api/session            当前用户名、版本
POST   /api/passwd             {old_password, username, password}

GET    /api/overview           指标聚合
GET    /api/services           runit 服务
POST   /api/services/{n}/{act} start|stop|restart
GET    /api/processes          进程列表（含 protected 标记）
POST   /api/processes/{pid}/kill | kill9

GET    /api/files?path=        列目录
GET    /api/file?path=         读文件
PUT    /api/file               {path, content}
DELETE /api/file?path=         删除
POST   /api/file/mkdir         {path, name}
POST   /api/file/touch         {path, name}
POST   /api/file/rename        {path, name}

GET    /api/file?download=1   流式下载（支持 Range）
GET    /api/file?raw=1        原始字节（图片/视频预览）
POST   /api/file/copy          {paths, dest, move}
POST   /api/file/chmod         {path, mode, recursive}
GET    /api/file/stat?path=    属性

GET    /api/trash              回收站列表
POST   /api/trash/restore      {id} 或 {ids}
POST   /api/trash/purge        {id} / {ids} / {all}

GET    /api/versions?path=     历史版本列表
GET    /api/versions/read?id=  读取某个版本
POST   /api/versions/restore   {id}
POST   /api/versions/delete    {id}

GET    /api/favorites          收藏列表
POST   /api/favorites/toggle   {path}

POST   /api/search             {path, q, suffix, sub, limit, timeout_ms} → task
POST   /api/archive            {paths, format} → task
POST   /api/extract            {path, dest} → task
POST   /api/download/remote    {url, dir, name} → task
GET    /api/file-settings      回收站天数、版本保留数
POST   /api/file-settings

POST   /api/upload/init        {path, name, size, chunk_size, id?}
PUT    /api/upload/{id}/{idx}  分片二进制
GET    /api/upload/{id}        已传分片索引（断点续传）
POST   /api/upload/{id}/complete | /abort

POST   /api/shares             {path, hours, need_code}
GET    /api/shares/list
POST   /api/shares/revoke      {token}
GET    /s/{token}              公开分享页（无需登录）
GET    /api/public/share/{token}/download?code=

GET    /api/network            接口 + 端口探测
GET    /api/logs               日志服务列表
GET    /api/logs/{name}?lines= 日志尾部
GET    /api/disk               目录占用

POST   /api/exec               {cmd, cwd} → task
GET    /api/exec/{id}          任务状态与输出
GET    /api/tasks              最近 50 条
```

## 文件

```
pocketpanel/
├── main.go            配置、会话、指标、服务、文件读写、任务、路由
├── ops.go             文件增删改、进程、网络、日志、磁盘
├── files_ext.go       回收站、历史版本、收藏夹、搜索、复制移动、权限
├── archive.go         压缩、解压、远程下载
├── share.go           分享链接、公开下载页、分片上传
├── tasks_ext.go       非命令类任务的公共辅助（进度、结果、取消）
├── web.go             静态资源嵌入
├── go.mod
├── web/
│   └── index.html     全部前端（CSS + HTML + JS，无依赖）
├── deploy.sh          编译 + 上传 + 重启
└── screenshots/
```

## 已知限制

1. **终端是命令执行器，不是 PTY**。`vim`、`top` 这类全屏交互用不了，也没有 job control。真 PTY 需要 `/dev/ptmx` 或 xpty。
2. **没有文件搜索索引**。搜索是实时遍历，不是全文索引；大目录下靠 8 秒超时兜底，第二次搜同一个词不会更快。
3. **没有媒体缩略图**。设备没有 ffmpeg，视频只能点开用浏览器原生播放；图片走原图加载，大图在弱网下会慢。
4. 命令以 `termux` 用户执行，**无 root**。80/443、iptables、改属主都做不到。
5. **端口探测看不到占用进程**。Android 挡住了所有能查监听进程的接口，只能判断「能不能连上」。
6. 服务启停没有二次确认，`sshd` 这类关键服务误点会断连（面板自身已在前后端双重保护）。
7. 会话存内存，重启面板后需要重新登录。
8. **计划任务尚未实现**。设备没有 `crontab`/`crond`，需要在面板进程内做调度器，并处理息屏挂起与补跑。
9. 尚未实现：网站管理、数据库、应用商店、内网穿透、监控告警、备份 —— 这些在 `PocketPanel-设计方案.md` 中有完整方案。

## 下一步

按依赖顺序推进（详见设计文档 13.4）：文件上传下载 → PTY 终端 → 计划任务（内置调度器）→ 应用商店 → 内网穿透（接管已有 mefrp）→ 网站管理。
