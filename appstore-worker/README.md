# PocketPanel 应用商店 · Worker

一个只读的 JSON 接口，告诉面板「有哪些应用、每个怎么装」。
Workers + KV，免费额度足够（清单一天读不了几次，写更少）。

## 为什么是 KV 而不是 D1

清单就一份 JSON（几 KB），读多写少、没有关联查询、不需要事务。
KV 一次读整包，全球边缘缓存，免费额度 10 万读/天；D1 要建表、写 SQL、
每次还得连库，对这个场景是纯负担。真要换成 D1，`loadCatalog()` 一个函数就能替。

## 部署

```bash
export CF_EMAIL=you@example.com      # Cloudflare 账号邮箱
export CF_KEY=你的 Global API Key    # My Profile → API Tokens → Global API Key
python3 deploy.py --host store.sanhe.dpdns.org
```

脚本会建 KV、灌种子数据、上传 Worker、设两个密钥、绑域名，
最后打印**商店地址**和**访问密钥**——填到面板「应用 → 商店」里。

重复执行是幂等的，改了 `seed/catalog.json` 再跑一次就更新线上清单。

> 用自定义域名而不是 `*.workers.dev`：后者在国内基本访问不了。

**凭据就在面板自己的配置里**：`~/pocketpanel/data/cloudflare.json` 的
`creds.email` / `creds.key` 就是 Cloudflare 的 Global API Key（面板的
CF 穿透用的是同一对），不用另找。

**更新清单时一定要带上原来的 STORE_KEY**：

```bash
STORE_KEY=$(python3 -c "import json;print(json.load(open('$HOME/pocketpanel/data/appstore.json'))['key'])")
CF_EMAIL=... CF_KEY=... STORE_KEY=$STORE_KEY python3 deploy.py --host store.example.com
```

不带的话脚本会 `uuid4()` 生成一个新的写进 Worker，**手机上已经配好的
访问密钥当场失效**，商店变 401。ADMIN_KEY 同理（它只用于
`POST /v1/admin/catalog` 覆盖写清单，丢了重新生成即可，但要在别处同步更新）。

### 绑域名为什么不用 Custom Domain

`PUT /accounts/{id}/workers/domains` 只认 API Token，用 Global API Key 调会回
`Method not allowed for this authentication scheme`。所以脚本改走
**DNS 占位记录 + Worker Route**：

```
A     apps.example.com → 192.0.2.1   (proxied)
AAAA  apps.example.com → 100::       (proxied)
route apps.example.com/* → pocketpanel-store
```

两个地址都是 RFC 5737 / 6666 里的文档用地址，流量到 CF 边缘就被 Worker 截走，
不会真发出去。**A 和 AAAA 必须都建**：只建 AAAA 时，纯 IPv4 的手机会在
`getaddrinfo` 阶段就报 `No address associated with hostname`，连 CF 边缘都摸不到。

DNS 刚改完那阵子，手机可能还留着旧的「域名不存在」负缓存（按 SOA 的 minimum，
最长半小时）。这期间面板会显示「域名解析不到」，等缓存过期自动恢复，
不用改任何配置。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | `{ok, apps, updated, keyed}` |
| GET | `/v1/catalog` | 全量清单，带 `ETag`，命中返回 304 |
| GET | `/v1/apps/:id` | 单个应用 |
| POST | `/v1/admin/catalog` | 覆盖写清单，要 `ADMIN_KEY` |

`/v1/catalog` 与 `/v1/apps/:id` 需要 `X-Store-Key`（或 `?key=`）。
`STORE_KEY` 没设置时接口是公开的。

## 加一个应用

改 `seed/catalog.json` 里的 `apps` 数组，跑一次 `deploy.py`。字段：

```jsonc
{
  "id": "nginx",                     // 唯一，容器 label 用它认领
  "name": "Nginx",
  "summary": "一句话",
  "desc": "两三句话，卡片上显示",
  "icon_name": "globe",              // 面板内置图标名；没有就退回 icon 里的 emoji
  "image": "nginxinc/nginx-unprivileged:alpine",
  "arch": ["arm64", "amd64"],
  "ports": [{"container": 8080, "host": 8080, "label": "HTTP"}],
  "volumes": [{"container": "/usr/share/nginx/html", "host": "{{data}}/html"}],
  "env": [{"key": "TZ", "value": "Asia/Shanghai"}],
  "command": [],                     // 覆盖 CMD，可选
  "restart": "always",
  "size": "约 20MB",
  "tips": "装完怎么用",
  "verified": true                   // 在 Android + udocker 上实测跑通过
}
```

`{{data}}` 会替换成 `~/pocketpanel/apps/<id>`。

## 现实约束：什么应用能装

面板跑的是 udocker + PRoot，不是真 Docker。挑应用时注意：

- **单容器**，不能带 sidecar（没有 compose 原生支持）；
- **host 网络**，容器里监听什么端口宿主机就是什么端口，`ports` 只用于提示；
- **端口 ≥1024**，非 root 绑不了 80；
- **必须是 linux/arm64**（手机）或 amd64；
- 镜像里如果只有 `ENTRYPOINT` 没有可执行 shell，面板的「终端」进不去。

`verified: true` 表示这几条都验证过。新加应用最好先在手机上装一遍再标。
