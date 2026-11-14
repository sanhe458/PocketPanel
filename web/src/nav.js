// 导航结构：按「主机 / 文件 / 终端 / 自动化 / 系统」分组，参照宝塔与 1Panel 的 IA
export const NAV_GROUPS = [
  {
    title: '概览',
    items: [
      { id: 'overview', label: '仪表盘', icon: 'dashboard', desc: '设备状态、资源占用与服务概览' }
    ]
  },
  {
    title: '主机',
    items: [
      { id: 'services', label: '服务', icon: 'server', desc: 'runit 服务启停与运行状态' },
      { id: 'docker', label: '容器', icon: 'package', desc: 'udocker 容器与镜像 · host 网络 · 无 cgroups' },
      { id: 'processes', label: '进程', icon: 'cpu', desc: '查看并结束系统进程' },
      { id: 'network', label: '网络', icon: 'network', desc: '网卡信息与本地端口探测' },
      { id: 'logs', label: '日志', icon: 'log', desc: '面板与服务运行日志' }
    ]
  },
  {
    title: '应用',
    items: [
      { id: 'store', label: '应用商店', icon: 'grid', desc: '一键装应用 · 目前是 Docker 容器，其它类型预留' }
    ]
  },
  {
    title: 'AI',
    items: [
      { id: 'gateway', label: 'AI 网关', icon: 'wand', desc: '多厂商 Key 与四大协议 · 流式透传 · 用量统计' }
    ]
  },
  {
    title: '文件与终端',
    items: [
      { id: 'files', label: '文件管理', icon: 'folder', desc: '上传、编辑、压缩、分享与回收站' },
      { id: 'terminal', label: '终端', icon: 'terminal', desc: '本地 shell 与 SSH 远程登录' }
    ]
  },
  {
    title: '自动化',
    items: [
      { id: 'schedule', label: '计划任务', icon: 'clock', desc: '定时任务与多类型执行器' },
      { id: 'tasks', label: '任务队列', icon: 'list', desc: '后台任务执行状态与输出' },
      { id: 'alerts', label: '监控告警', icon: 'bell', desc: '存活探测 · 邮件告警 · 恢复通知' }
    ]
  },
  {
    title: '穿透',
    items: [
      { id: 'tunnels', label: '内网穿透', icon: 'share', desc: 'frpc 隧道配置、保活与公网拨测' },
      { id: 'cloudflare', label: 'CF 穿透', icon: 'globe', desc: 'Cloudflare Tunnel · 免公网 IP · 临时地址与固定域名' },
      { id: 'mefrp', label: 'ME Frp', icon: 'wand', desc: '幻缘映射 · 平台建隧道 · 官方 mefrpc 客户端接管' }
    ]
  },
  {
    title: '系统',
    items: [
      { id: 'settings', label: '设置', icon: 'settings', desc: '账户、主题、终端通道与安全' }
    ]
  }
]

export const NAV_FLAT = NAV_GROUPS.flatMap(g => g.items)

export const NAV_MAP = Object.fromEntries(NAV_FLAT.map(n => [n.id, n]))

// 移动端底部 5 个入口
export const TAB_IDS = ['overview', 'files', 'terminal', 'schedule', 'services']
