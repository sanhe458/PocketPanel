"""网络守卫：网络变动 → 防抖 → 双客户端智能探测 → 定向恢复。

背景
----
手机切 WiFi / 4G、电梯断网、基站切换都会让 IP 变化。隧道客户端（frpc /
cloudflared）此时往往已经自己重连成功了——**全局重启会把本来好好的
隧道全部掀翻**，用户感知是"每次切网所有内网地址都要等十几秒"。

所以这里的原则是：**先探测，按需重启，一个都不多动。**

两个客户端的"活着吗"信号完全不同，因此探测方式也必须不同：

- frpc：配置里开 `webServer.port` 后，本地 HTTP 服务提供 `/api/status`。
  响应按协议分组（`{"tcp":[{"name":..,"status":"running"}]}`），
  有代理且状态为 running 说明它自己连上了；连接断开时响应变成 `{}`。
  frpc 0.71 的 webServer 支持 user/password 鉴权（不支持 auth.token）。
- cloudflared：`--metrics 127.0.0.1:PORT` 打开后本地暴露 `/ready`，
  返回 `{"status":200,"readyConnections":N}`；隧道掉线时连接数归零。

**探测不了就不动。** 实例若没有可用的探测端点（缺 webServer 端口、
metrics 端口没记下来），一律跳过并记日志——绝不能因为"探测失败"就
认定它坏了进而重启，那正是需求里禁止的粗暴全局重启。

状态机
------
    稳态 ──检测到 IP 变化──▶ 等待稳定（防抖计时器）
                               │  计时器内又变 → 重置计时
                               ▼
                          倒计时结束 → 再确认一次有 IP 且非断网
                               ▼
                        逐个探测 running 的隧道
                               │
                    ┌──────────┴──────────┐
                 健康（跳过）           不健康 → 只重启这一个
                               ▼
                          汇总 → 推送 network_recovered

熔断：5 分钟内同一实例因网络变动被重启超过 3 次，就暂停它的自动恢复并
告警。反复闪断的移动网络下，这能避免"面板不停重启 → 客户端起不来 → 再重启"
的死循环。

网络状态获取
------------
优先用 termux-api 的 `termux-wifi-connectioninfo`（拿 WiFi 的 IP/SSID），
但它只看 WiFi：切到 4G 后 WiFi 信息会变成空或残留旧值。所以同时解析
`ifconfig`，按接口名区分 WiFi(wlan*) 与蜂窝(rmnet*/cc*/ap*)，
两者结合才能正确识别"从 WiFi 切到 4G"。
"""
import json
import os
import re
import subprocess
import threading
import time

import core

# ---------------- 常量 ----------------

POLL_INTERVAL = 10          # 轮询网络状态的间隔（秒）
DEBOUNCE_DEFAULT = 45       # 防抖默认秒数
DEBOUNCE_MIN = 30
DEBOUNCE_MAX = 60
CIRCUIT_WINDOW = 300        # 熔断统计窗口（秒）
CIRCUIT_MAX = 3             # 窗口内允许的因网络变动重启次数

SETTINGS_FILE = os.path.join(core.DATA, "netguard.json")
DEFAULT_SETTINGS = {
    "enabled": True,
    "debounce": DEBOUNCE_DEFAULT,
    "poll_interval": POLL_INTERVAL,
    "circuit_max": CIRCUIT_MAX,
}

# 蜂窝 / 移动数据接口名特征（ifconfig 输出里的网卡名）
CELL_IFACE_RE = re.compile(r"^(rmnet|cc|ap|rmnet_data|pdp|wwan|usb|rndis)", re.I)
WIFI_IFACE_RE = re.compile(r"^(wlan|wl|wifi)", re.I)

_lock = threading.RLock()
_settings = dict(DEFAULT_SETTINGS)
_loaded = False

# 运行时状态（不落盘，重启后重新开始观察）
_last_net = None            # 上一次的 {"ip","kind"} 快照
_pending = None             # {"ip","kind","deadline"} 防抖计时器
_recovering = False         # 恢复流程是否在跑
_circuit = {}               # inst_id -> [重启时间戳, ...]
_last_report = None         # 上一次恢复结果摘要
_thread_started = False


# ---------------- 设置 ----------------

def _settings_path():
    return SETTINGS_FILE


def _load_settings():
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            with open(_settings_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for k in DEFAULT_SETTINGS:
                    if k in raw:
                        _settings[k] = raw[k]
        except (OSError, ValueError):
            pass
        _settings["debounce"] = _clamp_debounce(_settings.get("debounce"))
        _settings["poll_interval"] = max(5, int(_settings.get("poll_interval") or POLL_INTERVAL))
        _settings["circuit_max"] = max(1, int(_settings.get("circuit_max") or CIRCUIT_MAX))


def _clamp_debounce(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DEBOUNCE_DEFAULT
    return max(DEBOUNCE_MIN, min(DEBOUNCE_MAX, v))


def _save_settings():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = _settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_settings, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _settings_path())


def settings():
    _load_settings()
    with _lock:
        return dict(_settings)


def save_settings(**kw):
    _load_settings()
    with _lock:
        if "enabled" in kw:
            _settings["enabled"] = bool(kw["enabled"])
        if "debounce" in kw:
            _settings["debounce"] = _clamp_debounce(kw["debounce"])
        if "poll_interval" in kw:
            try:
                pi = int(kw["poll_interval"])
            except (TypeError, ValueError):
                pi = POLL_INTERVAL
            _settings["poll_interval"] = max(5, min(300, pi))
        if "circuit_max" in kw:
            try:
                cm = int(kw["circuit_max"])
            except (TypeError, ValueError):
                cm = CIRCUIT_MAX
            _settings["circuit_max"] = max(1, min(20, cm))
        _save_settings()
        return dict(_settings)


# ---------------- 日志 ----------------

_log_lines = []
_log_lock = threading.RLock()
LOG_MAX = 300


def _log(text):
    line = f"[{time.strftime('%H:%M:%S')}] {text}"
    with _log_lock:
        _log_lines.append(line)
        if len(_log_lines) > LOG_MAX:
            del _log_lines[:len(_log_lines) - LOG_MAX]
    print("[netguard] " + line, flush=True)
    try:
        import messages
        messages.add("info", "网络守卫", text[:180])
    except Exception:  # noqa: BLE001
        pass


def log_tail(n=80):
    with _log_lock:
        return list(_log_lines[-n:])


# ---------------- 网络状态探测 ----------------

def _run(cmd, timeout=6):
    """跑一个外部命令，返回 stdout 文本；失败返回空串。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _wifi_info():
    """termux-wifi-connectioninfo → {"ip","ssid","state"} 或 None。

    termux-api 没装 / 没权限时返回 None，调用方会退回到 ifconfig 解析。
    """
    binp = "/data/data/com.termux/files/usr/bin/termux-wifi-connectioninfo"
    if not os.path.exists(binp):
        return None
    out = _run(["termux-wifi-connectioninfo"], timeout=6)
    if not out.strip():
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("error"):
        return None
    return {
        "ip": (d.get("ip") or "").strip(),
        "ssid": (d.get("ssid") or "").strip(),
        "state": (d.get("supplicant_state") or "").strip(),
        "rssi": d.get("rssi"),
    }


_IFACE_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s+flags=")
_INET_RE = re.compile(r"^\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})")


def _ifconfig_ips():
    """解析 ifconfig → [(iface, ip)]，只保留 IPv4 且非回环。

    Android 没有 `ip` 命令，ifconfig 是唯一可靠的接口清单来源。
    """
    out = _run(["ifconfig"], timeout=6)
    res = []
    iface = ""
    for line in out.splitlines():
        m = _IFACE_RE.match(line)
        if m:
            iface = m.group(1)
            continue
        m = _INET_RE.match(line)
        if m and iface:
            ip = m.group(1)
            if ip.startswith("127."):
                continue
            res.append((iface, ip))
            iface = ""
    return res


def current_network():
    """当前网络状态：{"ip","kind","ssid","ifaces"}。

    kind: "wifi" | "cellular" | "none"
    """
    wifi = _wifi_info()
    ips = _ifconfig_ips()

    cell_ips = [(i, ip) for i, ip in ips if CELL_IFACE_RE.match(i)]
    wifi_ips = [(i, ip) for i, ip in ips if WIFI_IFACE_RE.match(i)]

    # 蜂窝接口优先：切到 4G 时 WiFi 那份数据可能还是旧的
    if cell_ips:
        iface, ip = cell_ips[0]
        return {"ip": ip, "kind": "cellular", "ssid": "",
                "ifaces": ips, "iface": iface}
    if wifi_ips:
        iface, ip = wifi_ips[0]
        return {"ip": ip, "kind": "wifi",
                "ssid": (wifi or {}).get("ssid", ""),
                "ifaces": ips, "iface": iface}
    if wifi and wifi.get("ip") and wifi.get("state") == "COMPLETED":
        # ifconfig 没读到但 termux-api 说连着（权限差异），仍然采信
        return {"ip": wifi["ip"], "kind": "wifi", "ssid": wifi.get("ssid", ""),
                "ifaces": ips, "iface": "wlan0"}
    return {"ip": "", "kind": "none", "ssid": "", "ifaces": ips, "iface": ""}


# ---------------- 事件推送 ----------------

def _emit(event, data):
    """向前端推送事件（events 未就绪时静默）。"""
    try:
        import events
        events.emit(event, data)
    except Exception:  # noqa: BLE001
        pass


# ---------------- 熔断 ----------------

def _circuit_count(inst_id):
    now = time.time()
    with _lock:
        hits = [t for t in _circuit.get(inst_id, []) if now - t < CIRCUIT_WINDOW]
        _circuit[inst_id] = hits
        return len(hits)


def _circuit_hit(inst_id):
    with _lock:
        _circuit.setdefault(inst_id, []).append(time.time())


def circuit_state():
    """返回熔断状态列表，供前端展示。"""
    now = time.time()
    cmax = settings()["circuit_max"]
    out = []
    with _lock:
        for inst_id, hits in list(_circuit.items()):
            recent = [t for t in hits if now - t < CIRCUIT_WINDOW]
            if recent:
                out.append({"id": inst_id, "count": len(recent),
                            "paused": len(recent) >= cmax})
    return out


def _circuit_paused(inst_id):
    """窗口内已重启次数达到阈值 → 暂停自动恢复。

    阈值是"允许的次数"：circuit_max=3 表示允许重启 3 次，
    第 4 次就被拦下并告警，不再无限重启。
    """
    return _circuit_count(inst_id) >= settings()["circuit_max"]


def reset_circuit(inst_id=None):
    with _lock:
        if inst_id:
            _circuit.pop(inst_id, None)
        else:
            _circuit.clear()


# ---------------- 客户端实例枚举（tunnel/cloudflare 的桥） ----------------

def _collect_instances():
    """枚举所有运行中的隧道实例，标注 client_kind。

    由 app.py 注入，避免 netguard 直接依赖 tunnel/cloudflare 造成
    循环导入。返回 [{"id","name","kind","probe"}]：
      probe 为 None 表示该实例没有可用的探测端点——必须跳过，不能重启。
    """
    try:
        import tunnel
    except Exception:  # noqa: BLE001
        tunnel = None
    try:
        import cloudflare
    except Exception:  # noqa: BLE001
        cloudflare = None

    out = []
    if tunnel is not None:
        for m in tunnel.list_instances():
            if m.get("status") != "running":
                continue
            out.append({
                "id": m["id"], "name": m.get("name") or m["id"],
                "kind": "frpc", "probe": m.get("probe_endpoint"),
                "meta": m,
            })
    if cloudflare is not None:
        for m in cloudflare.list_tunnels():
            if not m.get("alive"):
                continue
            out.append({
                "id": m["id"], "name": m.get("name") or m["id"],
                "kind": "cloudflared", "probe": m.get("probe_endpoint"),
                "meta": m,
            })
    return out


def _probe_instance(inst):
    """探测单个实例是否健康。

    返回 {"ok":bool,"detail":str,"skip":bool}
    skip=True 表示"探测不了，不做任何动作"。
    """
    endpoint = inst.get("probe")
    kind = inst.get("kind")
    if not endpoint:
        return {"ok": False, "skip": True,
                "detail": "无探测端点（未配置 webServer / metrics 端口），跳过"}
    try:
        if kind == "frpc":
            return _probe_frpc(endpoint)
        if kind == "cloudflared":
            return _probe_cloudflared(endpoint)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skip": False, "detail": f"探测异常：{e}"}
    return {"ok": False, "skip": True, "detail": "未知客户端类型，跳过"}


def _http_get(url, timeout=5, auth=None):
    """GET 一个本地 HTTP 端点。返回 (status, body_text)。"""
    import base64
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "PocketPanel-NetGuard/1.0"})
    if auth:
        raw = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + raw)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _probe_frpc(endpoint):
    """探测 frpc：GET /api/status。

    frpc 0.71 的响应按协议分组：{"tcp":[{"name":"p1","status":"running"}]}；
    与服务端断开时整个响应变成 {}。webServer 若配了 user/password，
    未带鉴权会返回 401——那属于"探测端点配置不当"，不是客户端坏了。
    """
    port = endpoint.get("port")
    if not port:
        return {"ok": False, "skip": True, "detail": "未记录 webServer 端口，跳过"}
    url = f"http://127.0.0.1:{port}/api/status"
    auth = None
    if endpoint.get("user"):
        auth = (endpoint.get("user") or "", endpoint.get("password") or "")
    status, body = _http_get(url, timeout=5, auth=auth)
    if status == 401:
        return {"ok": False, "skip": True,
                "detail": "webServer 需要鉴权但凭据不对，跳过（不当成故障）"}
    if status != 200:
        return {"ok": False, "skip": False,
                "detail": f"webServer 返回 HTTP {status or '无响应'}"}
    try:
        data = json.loads(body)
    except ValueError:
        return {"ok": False, "skip": False, "detail": "webServer 返回非 JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "skip": False, "detail": "webServer 返回结构异常"}
    # 按协议分组收集所有代理项
    entries = []
    for key, val in data.items():
        if isinstance(val, list):
            for it in val:
                if isinstance(it, dict):
                    entries.append(it)
    if not entries:
        # {} 或空分组 = 与服务端断开
        return {"ok": False, "skip": False, "detail": "无在线代理（status 为空）"}
    want = endpoint.get("proxies") or []
    if want:
        by_name = {e.get("name"): e for e in entries}
        bad = []
        for name in want:
            e = by_name.get(name)
            if e is None:
                continue          # 该代理还没上报，暂不判死
            st = str(e.get("status") or "").lower()
            if st not in ("running", "online"):
                bad.append(f"{name}={st or 'unknown'}")
        if bad:
            return {"ok": False, "skip": False,
                    "detail": "代理状态异常：" + "、".join(bad)}
        return {"ok": True, "detail": f"{len(entries)} 个代理在线"}
    running = sum(1 for e in entries
                  if str(e.get("status") or "").lower() in ("running", "online"))
    if running == 0:
        return {"ok": False, "skip": False, "detail": "代理状态异常：全部非 running"}
    return {"ok": True, "detail": f"{running} 个代理在线"}


def _probe_cloudflared(endpoint):
    """探测 cloudflared：GET /ready。

    正常返回 {"status":200,"readyConnections":N}；隧道掉线时连接数归零。
    """
    port = endpoint.get("port")
    if not port:
        return {"ok": False, "skip": True, "detail": "未记录 metrics 端口，跳过"}
    url = f"http://127.0.0.1:{port}/ready"
    status, body = _http_get(url, timeout=5)
    if status != 200:
        return {"ok": False, "skip": False,
                "detail": f"/ready 返回 HTTP {status or '无响应'}"}
    try:
        data = json.loads(body)
    except ValueError:
        # 老版本可能直接返回纯文本
        if "ready" in body.lower():
            return {"ok": True, "detail": "ready"}
        return {"ok": False, "skip": False, "detail": "/ready 返回内容异常"}
    if not isinstance(data, dict):
        return {"ok": False, "skip": False, "detail": "/ready 返回结构异常"}
    if int(data.get("status") or 0) != 200:
        return {"ok": False, "skip": False,
                "detail": f"ready status={data.get('status')}"}
    conns = int(data.get("readyConnections") or 0)
    if conns <= 0:
        return {"ok": False, "skip": False, "detail": "readyConnections=0（隧道未连通）"}
    return {"ok": True, "detail": f"{conns} 条连接就绪"}


def _restart_instance(inst):
    """只重启这一个实例。"""
    kind = inst.get("kind")
    tid = inst.get("id")
    try:
        if kind == "frpc":
            import tunnel
            tunnel.restart(tid)
        elif kind == "cloudflared":
            import cloudflare
            cloudflare.restart(tid)
        else:
            return {"ok": False, "detail": "未知客户端类型"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"重启失败：{e}"}
    return {"ok": True, "detail": "已重启"}


# ---------------- 主循环 ----------------

def ensure_thread():
    global _thread_started
    _load_settings()
    with _lock:
        if _thread_started:
            return
        _thread_started = True
    threading.Thread(target=_loop, daemon=True, name="netguard").start()


def _loop():
    global _last_net
    _load_settings()
    _last_net = current_network()
    _log(f"网络守卫启动，当前 {_last_net['kind']} IP={_last_net['ip'] or '无'}")
    while True:
        try:
            cfg = settings()
            if not cfg.get("enabled"):
                time.sleep(cfg.get("poll_interval", POLL_INTERVAL))
                continue
            _tick(cfg)
        except Exception as e:  # noqa: BLE001
            _log(f"轮询异常：{e}")
        time.sleep(settings().get("poll_interval", POLL_INTERVAL))


def _tick(cfg):
    global _last_net, _pending
    now = time.time()
    net = current_network()
    last = _last_net or net

    changed = (net["ip"] != last["ip"]) or (net["kind"] != last["kind"])
    if not changed:
        # 没变化：如果在等防抖，看倒计时是否到了
        if _pending and now >= _pending["deadline"]:
            _confirm_and_recover(cfg, net)
        return

    # 变了：进入/重置防抖。
    # 基线必须立刻更新 —— 否则下一轮轮询又会认为"又变了"，
    # 计时器被反复重置，45 秒永远走不到。
    _last_net = dict(net)
    _pending = {"ip": net["ip"], "kind": net["kind"],
                "deadline": now + cfg["debounce"],
                "started": now, "resets": (_pending or {}).get("resets", 0) + 1}
    _log(f"检测到网络变动：{last['kind']}/{last['ip'] or '无'} → "
         f"{net['kind']}/{net['ip'] or '无'}，等待 {cfg['debounce']}s 稳定")
    _emit("network_changed", {
        "ip": net["ip"], "kind": net["kind"],
        "old_ip": last["ip"], "old_kind": last["kind"],
        "debounce": cfg["debounce"], "deadline": int(_pending["deadline"]),
        "message": f"检测到网络切换，正在等待网络稳定（{cfg['debounce']}s）...",
    })


def _confirm_and_recover(cfg, net):
    """防抖倒计时结束：再确认一次网络确实可用，然后触发恢复。"""
    global _pending, _last_net, _recovering
    now = time.time()
    pend = _pending
    if not pend:
        return
    confirm = current_network()
    if not confirm["ip"] or confirm["kind"] == "none":
        # 还是断的：不算恢复，把计时器清掉，等下次变动
        _log(f"倒计时结束但网络仍不可用（{confirm['kind']}/无 IP），取消本次恢复")
        _pending = None
        _emit("network_recover_canceled", {
            "message": "网络尚未恢复，已取消本次自动恢复",
            "kind": confirm["kind"],
        })
        return
    if confirm["ip"] != pend["ip"] or confirm["kind"] != pend["kind"]:
        # 又变了：重新计时
        _pending = {"ip": confirm["ip"], "kind": confirm["kind"],
                    "deadline": now + cfg["debounce"], "started": now,
                    "resets": pend.get("resets", 0) + 1}
        _log(f"倒计时结束时 IP 又变了，重置计时："
             f"{pend['ip'] or '无'} → {confirm['ip'] or '无'}")
        return

    _pending = None
    _last_net = confirm
    if _recovering:
        _log("已有恢复流程在跑，跳过本次")
        return
    _recovering = True
    threading.Thread(target=_recover, args=(confirm,),
                     daemon=True, name="netguard-recover").start()


def _recover(net):
    """逐个探测 running 的隧道实例，按需重启。"""
    global _recovering, _last_report
    try:
        _log(f"网络已稳定（{net['kind']}/{net['ip']}），开始探测隧道")
        insts = _collect_instances()
        if not insts:
            _log("没有运行中的隧道实例，跳过")
        healthy, restarted, failed, skipped = [], [], [], []
        for inst in insts:
            inst_id = inst.get("id")
            name = inst.get("name") or inst_id
            kind = inst.get("kind")
            if _circuit_paused(inst_id):
                skipped.append({"id": inst_id, "name": name, "kind": kind,
                                "reason": "熔断中"})
                _log(f"探测 {kind} {name}：熔断中，跳过自动恢复")
                continue
            res = _probe_instance(inst)
            if res.get("skip"):
                skipped.append({"id": inst_id, "name": name, "kind": kind,
                                "reason": res.get("detail", "")})
                _log(f"探测 {kind} {name}：{res.get('detail', '跳过')}，不动作")
                continue
            if res.get("ok"):
                healthy.append({"id": inst_id, "name": name, "kind": kind,
                                "detail": res.get("detail", "")})
                _log(f"探测 {kind} {name}：{res.get('detail', '正常')}，已自行恢复，跳过")
                continue
            # 确认不健康 → 只重启这一个
            _log(f"探测 {kind} {name}：{res.get('detail', '异常')}，准备重启")
            rr = _restart_instance(inst)
            _circuit_hit(inst_id)
            if rr.get("ok"):
                restarted.append({"id": inst_id, "name": name, "kind": kind,
                                  "detail": rr.get("detail", "")})
                _log(f"探测 {kind} {name}：已重启")
            else:
                failed.append({"id": inst_id, "name": name, "kind": kind,
                               "detail": rr.get("detail", "")})
                _log(f"探测 {kind} {name}：重启失败（{rr.get('detail', '')}）")

        # 熔断检查：窗口内重启次数达到阈值 → 暂停自动恢复
        cmax = settings()["circuit_max"]
        tripped, names = [], []
        for item in restarted + failed:
            if item["id"] in tripped:
                continue
            if _circuit_count(item["id"]) >= cmax:
                tripped.append(item["id"])
                names.append(item["name"])
        if tripped:
            _log(f"熔断触发：{'、'.join(names)} 在 {CIRCUIT_WINDOW}s 内重启超过 "
                 f"{cmax} 次，暂停自动恢复")
            _emit("network_circuit_broken", {
                "ids": tripped, "names": names,
                "message": "网络频繁闪断，已暂停自动重连，请检查网络环境",
            })

        report = {
            "ts": int(time.time()),
            "ip": net["ip"], "kind": net["kind"],
            "healthy": healthy, "restarted": restarted,
            "failed": failed, "skipped": skipped,
            "ok_count": len(healthy), "restart_count": len(restarted),
            "fail_count": len(failed), "skip_count": len(skipped),
        }
        _last_report = report
        msg = f"网络已恢复，{len(healthy)} 个隧道正常，{len(restarted)} 个隧道已重启"
        if failed:
            msg += f"，{len(failed)} 个重启失败"
        if skipped:
            msg += f"，{len(skipped)} 个跳过"
        _log(msg)
        _emit("network_recovered", {**report, "message": msg})
    except Exception as e:  # noqa: BLE001
        _log(f"恢复流程异常：{e}")
    finally:
        _recovering = False


# ---------------- 对外视图 ----------------

def status():
    """前端用的状态快照。"""
    global _last_net, _pending, _recovering
    _load_settings()
    with _lock:
        pend = dict(_pending) if _pending else None
        last = dict(_last_net) if _last_net else None
        recovering = _recovering
        report = _last_report
    now = int(time.time())
    if pend:
        pend["remaining"] = max(0, int(pend["deadline"] - now))
    return {
        "enabled": settings()["enabled"],
        "debounce": settings()["debounce"],
        "poll_interval": settings()["poll_interval"],
        "circuit_max": settings()["circuit_max"],
        "network": last or {},
        "pending": pend,
        "recovering": recovering,
        "report": report,
        "circuit": circuit_state(),
        "log": log_tail(60),
    }


def trigger_recover():
    """手动触发一次恢复（前端「立即探测」按钮）。"""
    net = current_network()
    if not net["ip"]:
        return {"ok": False, "message": "当前没有可用网络"}
    _recover(net)
    return {"ok": True, "message": "已执行探测"}
