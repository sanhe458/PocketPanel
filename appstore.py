"""应用商店：从远程拉清单，按类型分派安装。

几个取舍：

- **清单整包拉**，不做分页。几十个应用的 JSON 不过几十 KB，分页只会
  多几次失败机会。缓存 6 小时，进页面不会每次都打网络。
- **三级降级**：远程 → 本地缓存 → 内置清单。断网、Worker 挂了、KV 空了，
  都还有东西可装，不会白屏。
- **安装复用 `runtime.create_container`**：端口 ≥1024、目录沙箱校验、
  容器名唯一这些规则只写一份，商店不另起一套。
- **数据目录放 `~/pocketpanel/apps/<id>`**，和 `data/` 平级。`data/` 是
  受保护目录（里面有密码哈希与隧道 token），不允许映射进容器。

应用类型（`type` 字段）现在只有 `docker`，但架构是注册表式的：新类型
在 `PROVIDERS` 里加一项（valid / plan / install / installed 四个函数），
前端 `TYPE_META` 补一条标签，catalog 与安装接口的签名都不用动。

清单里出现当前版本不认识的类型时**不丢弃**——仍会列出来，只是装不了，
并给一句明确的提示。这样老面板遇到新商店不会白屏，也不会假装能装。
"""
import json
import os
import time
import urllib.error
import urllib.request

import core
import runtime

from catalog import APPS, CATS, UPDATED

DEFAULT_URL = "https://store.sanhe.dpdns.org"
CONFIG_FILE = os.path.join(core.DATA, "appstore.json")
CACHE_FILE = os.path.join(core.DATA, "appstore-cache.json")
APPS_DIR = os.path.join(core.HOME, "pocketpanel", "apps")
CACHE_TTL = 6 * 3600
TIMEOUT = 8

# 内置清单与商店种子数据都在 catalog.py：那边是数据，这边是流程。
# 换清单不用动这个文件。
BUILTIN = {"version": 2, "updated": UPDATED, "apps": APPS}


def settings():
    d = core.read_json(CONFIG_FILE, {})
    s = {"url": DEFAULT_URL, "key": "", "enabled": True}
    if isinstance(d, dict):
        for k in s:
            if k in d and d[k] is not None:
                s[k] = d[k]
    return s


def save_settings(patch):
    s = settings()
    for k, v in (patch or {}).items():
        if k not in s:
            continue
        if k == "url":
            v = str(v or "").strip().rstrip("/")
            if v and not v.startswith(("http://", "https://")):
                raise ValueError("商店地址要带 http:// 或 https://")
        elif k == "key":
            v = str(v or "").strip()
        elif k == "enabled":
            v = bool(v)
        s[k] = v
    core.write_json(CONFIG_FILE, s)
    return s


def _request(url, key="", timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "PocketPanel/1.0"})
    if key:
        req.add_header("X-Store-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError("清单格式不对")
    return data


# ---------------- 应用类型 ----------------
#
# 除了 docker，其余几项目前只是占位：清单里出现会照常展示并标「暂不支持」，
# 装的时候直接拒绝。加实现只要往 PROVIDERS 里注册，不用改接口。

TYPES = {
    "docker": {"label": "Docker 应用", "short": "Docker", "icon": "package",
               "desc": "容器化应用，跑在本机 udocker 上"},
    "termux": {"label": "Termux 包", "short": "Termux", "icon": "terminal",
               "desc": "apt 安装，交给 runit 托管（预留）"},
    "binary": {"label": "单文件程序", "short": "二进制", "icon": "download",
               "desc": "下载解压即用的静态二进制（预留）"},
    "script": {"label": "一键脚本", "short": "脚本", "icon": "code",
               "desc": "按步骤执行的安装脚本（预留）"},
}

_cap = {"at": 0.0, "val": False}


def docker_ready(ttl=10):
    """udocker 装了且 udockerd 能 ping 通，才算 docker 应用可装。

    catalog 每次请求都会问一遍，所以带 10 秒缓存——`daemon_status()` 里
    那次 ping 是 1.2 秒超时，不能让它挂在页面加载路径上。
    """
    now = time.time()
    if _cap["at"] and now - _cap["at"] < ttl:
        return _cap["val"]
    val = False
    try:
        val = bool(runtime.udocker_info().get("installed")) and \
            bool(runtime.daemon_status().get("reachable"))
    except Exception:  # noqa: BLE001
        val = False
    _cap.update(at=now, val=val)
    return val


def type_of(app):
    return str((app or {}).get("type") or "docker").strip().lower()


def provider(app):
    return PROVIDERS.get(type_of(app))


def _type_views(counts):
    out = []
    for tid, meta in TYPES.items():
        p = PROVIDERS.get(tid)
        out.append({"id": tid, **meta, "count": int(counts.get(tid, 0)),
                    "implemented": bool(p),
                    "ready": bool(p) and p["ready"](),
                    "reason": "" if p else "当前版本暂不支持"})
    # 清单里出现的不认识类型也列出来，让用户知道不是丢了应用
    for tid, n in counts.items():
        if tid in TYPES:
            continue
        out.append({"id": tid, "label": tid, "short": tid, "icon": "package",
                    "desc": "", "count": int(n), "implemented": False,
                    "ready": False, "reason": "当前版本不认识这种类型"})
    return out


def _valid(app):
    p = provider(app)
    if not p:
        return True          # 未知类型：留着展示，装的时候再拒绝
    return p["valid"](app)


def _cat_views(counts):
    """分类视图。只回有应用的分类，避免界面上挂一排 0。"""
    known = dict(CATS)
    out = [{"id": cid, "label": label, "count": int(counts[cid])}
           for cid, label in CATS if counts.get(cid)]
    for cid, n in counts.items():
        if cid not in known:
            out.append({"id": cid, "label": cid, "count": int(n)})
    return out


def _norm(data, source):
    apps, counts, cats = [], {}, {}
    for a in data.get("apps") or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        a = dict(a)
        a["type"] = type_of(a)
        a["category"] = str(a.get("category") or "other").strip().lower()
        if not _valid(a):
            continue
        apps.append(a)
        counts[a["type"]] = counts.get(a["type"], 0) + 1
        cats[a["category"]] = cats.get(a["category"], 0) + 1
    return {"apps": apps, "updated": data.get("updated", ""),
            "version": data.get("version", 1), "source": source,
            "types": _type_views(counts), "cats": _cat_views(cats)}


def _err_text(e):
    """把网络异常翻成人话，别把 urllib 的堆栈甩到界面上。"""
    s = f"{type(e).__name__}: {e}"
    low = s.lower()
    # Android 报的是 "No address associated with hostname"，和 Linux 的措辞不一样
    if ("name or service not known" in low or "nodename nor servname" in low
            or "no address associated with" in low
            or "temporary failure in name resolution" in low
            or "getaddrinfo failed" in low):
        return "域名解析不到：可能刚加解析还没生效，或手机 DNS 缓存未过期"
    if "timed out" in low or "timeout" in low:
        return "连接超时：商店响应太慢"
    if "401" in s or "unauthorized" in low:
        return "密钥不对，检查商店设置里的访问密钥"
    if "403" in s or "forbidden" in low:
        return "没有访问权限"
    if "certificate" in low or "ssl" in low:
        return "HTTPS 证书校验失败"
    if "connection refused" in low or "network is unreachable" in low:
        return "连不上商店服务器：检查手机网络"
    return s[:120]


def catalog(force=False):
    """三级降级：远程 → 缓存 → 内置。"""
    s = settings()
    cache = core.read_json(CACHE_FILE, {})
    cached = cache.get("data") if isinstance(cache, dict) else None
    at = cache.get("at", 0) if isinstance(cache, dict) else 0
    err = ""

    if not force and cached and time.time() - at < CACHE_TTL:
        out = _norm(cached, "cache")
        out["age"] = int(time.time() - at)
        out["store"] = {"url": s["url"], "ok": True, "error": ""}
        return out

    if s["enabled"] and s["url"]:
        try:
            data = _request(s["url"] + "/v1/catalog", s["key"])
            core.write_json(CACHE_FILE, {"at": int(time.time()), "data": data})
            out = _norm(data, "remote")
            out["age"] = 0
            out["store"] = {"url": s["url"], "ok": True, "error": ""}
            return out
        except Exception as e:  # noqa: BLE001
            err = _err_text(e)

    if cached:
        out = _norm(cached, "cache")
        out["age"] = int(time.time() - at)
        out["stale"] = True
        out["store"] = {"url": s["url"], "ok": False, "error": err}
        return out

    out = _norm(BUILTIN, "builtin")
    out["store"] = {"url": s["url"], "ok": False, "error": err or "未配置商店"}
    return out


def find(app_id, force=False):
    for a in catalog(force).get("apps") or []:
        if a.get("id") == app_id:
            return a
    return None


def _has_image(image):
    ref = image if ":" in image.rsplit("/", 1)[-1] else image + ":latest"
    for im in runtime.list_images():
        tags = im.get("tags") or []
        if ref in tags or image in tags:
            return True
    return False


def _data_dir(app, opts):
    raw = str((opts or {}).get("data") or "").strip()
    if raw:
        try:
            return core.sandbox(raw)
        except ValueError as e:
            raise ValueError(f"数据目录不合法：{e}")
    return os.path.join(APPS_DIR, app["id"])


def plan(app, opts):
    """把应用定义 + 用户输入，算成一份可以预览的安装计划。"""
    p = provider(app)
    if not p:
        raise ValueError(f"「{type_of(app)}」类型的应用当前版本还不支持，先升级面板")
    return p["plan"](app, opts)


def _plan_docker(app, opts):
    arch = runtime._arch()
    want = app.get("arch") or []
    if want and arch not in want:
        raise ValueError(f"该应用只提供 {'/'.join(want)}，本机是 {arch}")

    data_dir = _data_dir(app, opts)
    want_port = (opts or {}).get("port")
    fixed = bool(app.get("port_fixed"))
    ports, mounts = [], []
    for i, p in enumerate(app.get("ports") or []):
        # 只有第一个端口让用户改。多端口应用（Gitea 的 SSH、MinIO 的
        # Console）后几个端口必须保持清单里的值，否则会全被改成同一个数，
        # 两个端口撞在一起，容器直接起不来。
        if i == 0 and want_port and not fixed:
            host = int(want_port)
            if not (1024 <= host <= 65535):
                raise ValueError("端口需在 1024–65535")
        else:
            host = int(p.get("host") or p["container"])
        ports.append({"host": host, "container": int(p["container"])})
    for v in app.get("volumes") or []:
        host = str(v.get("host") or "").replace("{{data}}", data_dir)
        mounts.append({"host": host, "container": v["container"]})

    # host 网络下容器里监听哪个端口就是宿主机哪个端口，所以用户改了端口，
    # 必须让容器也跟着换。两条路：命令行 `--port {{port}}`（redis、postgres），
    # 或者环境变量 `ROCKET_PORT={{port}}`（vaultwarden、navidrome 这一类）。
    # 两种都要支持 —— 只替换 command 的话，靠环境变量配端口的镜像会永远
    # 听着默认端口，用户点开 127.0.0.1:8222 只会看到连接被拒绝。
    port_arg = str(ports[0]["host"]) if ports else ""

    def sub(x):
        return (str(x).replace("{{port}}", port_arg)
                .replace("{{data}}", data_dir))

    cmd = [sub(x) for x in app.get("command") or []]

    env = []
    for e in app.get("env") or []:
        env.append({"key": e.get("key"), "value": sub(e.get("value", ""))})
    for k, v in ((opts or {}).get("env") or {}).items():
        env = [x for x in env if x["key"] != k] + [{"key": k, "value": sub(v)}]

    return {
        "id": app["id"], "type": "docker", "image": app["image"],
        "name": str((opts or {}).get("name") or app["id"])[:40],
        "cmd": cmd,
        "env": env, "mounts": mounts, "ports": ports,
        # None（清单里没写）＝照镜像自带入口，runtime 不会传 Entrypoint
        "entrypoint": app.get("entrypoint"),
        "restart": app.get("restart") or "no",
        "data_dir": data_dir,
    }


def install_task(t, app_id, opts):
    app = find(app_id)
    if not app:
        raise ValueError(f"商店里没有应用 {app_id}")
    p = provider(app)
    if not p:
        raise ValueError(f"「{type_of(app)}」类型的应用当前版本还不支持，先升级面板")
    return p["install"](t, app, opts)


def _install_docker(t, app, opts):
    """异步安装：拉镜像（如缺）→ 建目录 → 创建并启动容器。"""
    p = _plan_docker(app, opts)
    core.task_log(t, f"应用：{app.get('name')}（{p['image']}）")
    core.task_progress(t, 5)

    for m in p["mounts"]:
        os.makedirs(m["host"], mode=0o755, exist_ok=True)
    if p["mounts"]:
        core.task_log(t, f"数据目录：{p['data_dir']}")

    if not _has_image(p["image"]):
        core.task_log(t, "本地没有该镜像，先拉取")
        runtime.pull_task(t, p["image"], runtime.settings()["platform"])
        core.task_progress(t, 60)
    else:
        core.task_log(t, "镜像已在本地")

    spec = {
        "name": p["name"], "image": p["image"], "cmd": p["cmd"],
        "env": p["env"], "mounts": p["mounts"], "ports": p["ports"],
        # 忘了带这一项，tini 类镜像装完就是一个立刻退出的容器
        "entrypoint": p.get("entrypoint"),
        "restart": p["restart"], "start": True,
        "labels": {"pp.app": app["id"]},
    }
    res = runtime.create_container(spec)
    core.task_log(t, f"容器已创建：{res.get('name')}（{res.get('id', '')[:8]}）")
    for w in res.get("warnings") or []:
        core.task_log(t, f"注意：{w}")
    core.task_progress(t, 100)

    urls = []
    for port in p["ports"]:
        urls.append(f"http://127.0.0.1:{port['host']}")
    result = {"container": res, "urls": urls, "data_dir": p["data_dir"],
              "app": app["id"], "name": p["name"]}
    core.task_result(t, result)
    return result


def installed():
    """哪些应用已经装过。各类型自己认自己的，合并成一张表。"""
    out = {}
    for tid, p in PROVIDERS.items():
        try:
            for k, v in (p["installed"]() or {}).items():
                out[k] = dict(v, type=tid)
        except Exception:  # noqa: BLE001
            continue
    return out


def _installed_docker():
    """docker 应用靠容器 label `pp.app` 认。"""
    out = {}
    try:
        rows = runtime.list_containers()
    except Exception:  # noqa: BLE001
        return out
    for c in rows:
        app_id = ((c.get("labels") or {}).get("pp.app")
                  or (c.get("Labels") or {}).get("pp.app"))
        if not app_id:
            continue
        out[app_id] = {"container": c.get("id"), "name": c.get("name"),
                       "state": c.get("state")}
    return out


# 类型注册表。加新类型＝在这里加一项 + 前端 TYPE_META 补标签。
# 四个函数都是纯函数式的：valid(校验) / plan(算安装计划) /
# install(异步任务体) / installed(已装清单)。
PROVIDERS = {
    "docker": {
        "valid": lambda a: bool(a.get("image")),
        "ready": docker_ready,
        "plan": _plan_docker,
        "install": _install_docker,
        "installed": _installed_docker,
    },
}
