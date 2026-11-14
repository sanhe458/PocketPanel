"""ME Frp（幻缘映射）托管：账号、隧道、官方客户端进程。

为什么单独一个模块而不是塞进 tunnel.py：MEFrp 有账号体系与控制台 API，
隧道由平台侧定义（端口/域名/节点都是平台分配），本地只负责「用一个
授权令牌把官方客户端拉起来」。这和 tunnel.py 里「自己写 frpc 配置文件」
是两条完全不同的路径：那边真相源是本地配置文件，这边真相源是平台。

零依赖实现的两个关键点：

1. 登录要过 Cap 人机验证（captcha.mefrp.com，sha256 PoW）。挑战只有
   `{c, s, d}` 三个数字，salt 与 target 由前端用 FNV-1a + xorshift32
   从 challenge token 派生，算法见 `_cap_hash()`。全部是纯计算，
   标准库就能解，不需要浏览器。
2. 隧道进程用官方 `mefrpc` 客户端快捷启动：`mefrpc -t <用户令牌> -p <隧道Id>`。
   用户令牌（frpToken）与网页面板的访问令牌（Bearer token）是两回事：
   前者只用于连节点，后者只用于调 API，都不外泄。

数据布局（core.DATA/mefrp/）：
  account.json   账号与会话令牌（secretbox 加密，0600）
  run.json       托管意图与重启计数（面板重启后自动拉起）
  bin/mefrpc     面板自装的官方客户端（`client_install()`）
  {id}.log       每个隧道客户端的输出
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request

import core
import secretbox

API = "https://api.mefrp.com/api"
CAP = "https://captcha.mefrp.com/2bf50e050d/"
UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")

DIR = os.path.join(core.DATA, "mefrp")
BIN_DIR = os.path.join(DIR, "bin")
PREFIX = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
HOME = os.path.expanduser("~")

MASK = "********"
PROXY_TYPES = ("tcp", "udp", "http", "https")
TRANSPORT = ("tcp", "kcp", "quic", "websocket")
LOG_MAX = 512 * 1024          # 单个日志超过这个就轮转一次
MONITOR_EVERY = 20            # 保活轮询间隔（秒）
FAIL_LIMIT = 5                # 连续失败多少次熔断
BACKOFF_MAX = 120             # 退避上限（秒）

_lock = threading.RLock()
_run = {}          # proxyId(str) -> {"want", "restarts", "fail_cnt", "next_retry", "last_error"}
_procs = {}        # proxyId(str) -> {"proc", "started_at", "lf"}
_loaded = False
_monitor_started = False
_client_cache = {"ts": 0, "info": None}


class Err(Exception):
    """带面板错误码的业务异常，app.py 会转成 {error: msg}。"""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def panel_port():
    """面板自身端口：与 tunnel.panel_port() 同一口径（config 优先，PP_PORT 覆盖）。"""
    try:
        return int(os.environ.get("PP_PORT") or core.cfg_get("port", default=8888))
    except (TypeError, ValueError):
        return 8888


# ---------------- 账号存储 ----------------

def _file_account():
    return os.path.join(DIR, "account.json")


def _file_run():
    return os.path.join(DIR, "run.json")


def _secret_path():
    return os.path.join(core.DATA, "secret.key")


def _master():
    return secretbox.load_key(_secret_path())


def _load_account():
    return core.read_json(_file_account(), {}) or {}


def _save_account(obj):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    core.write_json(_file_account(), obj)
    try:
        os.chmod(_file_account(), 0o600)
    except OSError:
        pass


def token():
    """网页面板的访问令牌（Bearer），用于调 API。"""
    a = _load_account()
    enc = a.get("token_enc")
    if not enc:
        return ""
    try:
        return secretbox.decrypt_str(_master(), enc)
    except Exception:  # noqa: BLE001  密钥换了就当作没登录
        return ""


def frp_token():
    """客户端令牌（连节点用），优先用缓存，取不到再问 API。"""
    a = _load_account()
    enc = a.get("frp_enc")
    if enc:
        try:
            return secretbox.decrypt_str(_master(), enc)
        except Exception:  # noqa: BLE001
            pass
    tok = _api("GET", "/auth/user/frpToken").get("token") or ""
    if tok:
        _save_frp_token(tok)
    return tok


def _save_frp_token(tok):
    a = _load_account()
    a["frp_enc"] = secretbox.encrypt_str(_master(), tok)
    a["frp_at"] = int(time.time())
    _save_account(a)


def logged_in():
    return bool(token())


def account_view():
    a = _load_account()
    return {"username": a.get("username") or "",
            "group": a.get("group") or "",
            "logged_in": bool(a.get("token_enc"))}


def logout():
    a = _load_account()
    for k in ("token_enc", "frp_enc", "group", "frp_at"):
        a.pop(k, None)
    a["username"] = a.get("username") or ""
    _save_account(a)
    return True


# ---------------- Cap 人机验证（纯计算） ----------------

def _i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _u32(x):
    return x & 0xFFFFFFFF


def _cap_hash(s, ln):
    """复现 cap.js widget 的 `u(str, len)`：FNV-1a 32 位 + xorshift32 展开。

    JS 里 `t ^= c` 每次都把 t 截成 int32，而 `t += (t<<1)+...` 那一步是
    普通浮点加法（可以超过 2^32），下一轮再被 `^=` 截断。所以这里每步都
    显式做 i32 回绕，否则算出来的 salt 和浏览器对不上。
    """
    h = _i32(2166136261)
    for ch in s:
        h = _i32(h ^ ord(ch))
        h = h + (_i32(h << 1) + _i32(h << 4) + _i32(h << 7)
                 + _i32(h << 8) + _i32(h << 24))
    h = _u32(h)
    out = ""
    while len(out) < ln:
        h = _i32(h ^ _i32(h << 13))
        h = _i32(h ^ (_u32(h) >> 17))
        h = _i32(h ^ _i32(h << 5))
        h = _u32(h)
        out += format(h, "08x")
    return out[:ln]


def captcha_solve():
    """走一遍 challenge → 解 PoW → redeem，返回一次性的 captchaToken。"""
    ch = _http_json("POST", CAP + "challenge", {})
    c = int(ch["challenge"]["c"])
    s = int(ch["challenge"]["s"])
    d = int(ch["challenge"]["d"])
    tok = ch["token"]
    solutions = []
    for i in range(1, c + 1):
        salt = _cap_hash(tok + str(i), s)
        target = _cap_hash(tok + str(i) + "d", d)
        n = 0
        while True:
            if hashlib.sha256((salt + str(n)).encode()).hexdigest()[:len(target)] == target:
                solutions.append(n)
                break
            n += 1
    r = _http_json("POST", CAP + "redeem", {"token": tok, "solutions": solutions})
    ct = r.get("token") or ""
    if not ct:
        raise Err("人机验证失败，请稍后重试")
    return ct


# ---------------- HTTP ----------------

def _http_json(method, url, body=None, token_=None, timeout=30):
    data = None
    hdrs = {"User-Agent": UA, "Accept": "application/json",
            "Origin": "https://www.mefrp.com", "Referer": "https://www.mefrp.com/"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token_:
        hdrs["Authorization"] = "Bearer " + token_
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
    except Exception as e:  # noqa: BLE001  网络层
        raise Err(f"MEFrp 接口连不上：{e}")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        raise Err("MEFrp 返回了非 JSON 内容（可能被网关拦截）")


def _api(method, path, body=None, timeout=30):
    tok = token()
    if not tok and path not in ("/public/login",):
        raise Err("尚未登录 MEFrp", 401)
    d = _http_json(method, API + path, body, token_=tok, timeout=timeout)
    code = d.get("code")
    if code != 200:
        msg = d.get("message") or f"接口返回 {code}"
        if code in (401, 403) and "登录" in msg:
            raise Err(msg, 401)
        raise Err(msg)
    return d.get("data")


# ---------------- 账号操作 ----------------

def login(username, password):
    username = (username or "").strip()
    if not username or not password:
        raise Err("用户名和密码都要填")
    ct = captcha_solve()
    d = _http_json("POST", API + "/public/login",
                   {"username": username, "password": password, "captchaToken": ct})
    if d.get("code") != 200:
        raise Err(d.get("message") or "登录失败")
    data = d.get("data") or {}
    tok = data.get("token") or ""
    if not tok:
        raise Err("登录返回里没有令牌")
    a = _load_account()
    a.update({"username": data.get("username") or username,
              "group": data.get("group") or "",
              "token_enc": secretbox.encrypt_str(_master(), tok),
              "saved_at": int(time.time())})
    _save_account(a)
    # 顺手把客户端令牌取回来缓存，省得托管时再要一次
    try:
        ft = _api("GET", "/auth/user/frpToken").get("token") or ""
        if ft:
            _save_frp_token(ft)
    except Err:
        pass
    return account_view()


def overview():
    info = _api("GET", "/auth/user/info") or {}
    nodes = _api("GET", "/auth/node/list") or []
    view = {
        "username": info.get("username"),
        "group": info.get("group"),
        "group_name": info.get("friendlyGroup"),
        "traffic": info.get("traffic"),
        "in_bound": info.get("inBound"),
        "out_bound": info.get("outBound"),
        "max_proxies": info.get("maxProxies"),
        "used_proxies": info.get("usedProxies"),
        "realname": bool(info.get("isRealname")),
        "reg_time": info.get("regTime"),
        "nodes": [_node_view(n) for n in nodes],
    }
    try:
        ft = frp_token()
        view["frp_token_masked"] = secretbox.mask(ft, 4) if ft else ""
    except Err:
        view["frp_token_masked"] = ""
    return view


def _node_view(n):
    allow = [x for x in (n.get("allowGroup") or "").split(";") if x]
    return {
        "id": n.get("nodeId"),
        "name": n.get("name") or f"节点 {n.get('nodeId')}",
        "desc": n.get("description") or "",
        "region": n.get("region") or "",
        "bandwidth": n.get("bandwidth") or "",
        "allow_port": n.get("allowPort") or "",
        "allow_type": [x for x in (n.get("allowType") or "").split(";") if x],
        "allow_group": allow,
        "online": bool(n.get("isOnline")),
        "disabled": bool(n.get("isDisabled")),
        "load": n.get("loadPercent"),
    }


# ---------------- 隧道（平台侧） ----------------

def proxies():
    d = _api("GET", "/auth/proxy/list") or {}
    nodes = {n.get("nodeId"): n.get("name") for n in (d.get("nodes") or [])}
    out = []
    for p in d.get("proxies") or []:
        pid = str(p.get("proxyId"))
        st = _run.get(pid) or {}
        pr = _procs.get(pid)
        local = "running" if (pr and pr["proc"].poll() is None) else "stopped"
        doms = _domains(p.get("domain"))
        node_name = nodes.get(p.get("nodeId")) or f"节点 {p.get('nodeId')}"
        out.append({
            "id": p.get("proxyId"),
            "name": p.get("proxyName") or "",
            "type": p.get("proxyType") or "tcp",
            "node_id": p.get("nodeId"),
            "node_name": node_name,
            "local_ip": p.get("localIp") or "",
            "local_port": p.get("localPort"),
            "remote_port": p.get("remotePort"),
            "domain": doms[0] if doms else "",
            "domains": doms,
            "online": bool(p.get("isOnline")),
            "disabled": bool(p.get("isDisabled")),
            "banned": bool(p.get("isBanned")),
            "ban_reason": p.get("banReason") or "",
            "transport": p.get("transportProtocol") or "tcp",
            "encrypt": bool(p.get("useEncryption")),
            "compress": bool(p.get("useCompression")),
            "client_version": p.get("clientVersion") or "",
            "last_start": p.get("lastStartTime") or 0,
            "http_user": p.get("httpUser") or "",
            "local_state": local,
            "want": bool(st.get("want")),
            "restarts": st.get("restarts") or 0,
            "last_error": st.get("last_error") or "",
            "address": _address_of(doms, p.get("proxyType"), node_name,
                                   p.get("remotePort") or 0),
        })
    out.sort(key=lambda x: (x["type"], x["name"]))
    return out


def _domains(raw):
    """平台把域名存成 JSON 数组字符串（`["a.com","b.com"]`），也可能直接是逗号串。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw[:1] == "[":
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
    return [x.strip() for x in raw.split(",") if x.strip()]


def _address_of(domains, ptype, node_name="", remote_port=0):
    t = (ptype or "tcp").lower()
    if t in ("http", "https"):
        if domains:
            return f"{'https' if t == 'https' else 'http'}://{domains[0]}"
        return ""
    port = remote_port
    if port:
        return f":{port}"
    return ""


def free_port(node_id, protocol="tcp"):
    proto = "udp" if str(protocol).lower() == "udp" else "tcp"
    return _api("POST", "/auth/node/freePort", {"nodeId": int(node_id), "protocol": proto})


def create_proxy(body):
    name = (body.get("name") or "").strip()
    if not name:
        raise Err("隧道名不能为空")
    ptype = (body.get("type") or "tcp").lower()
    if ptype not in PROXY_TYPES:
        raise Err("不支持的穿透类型")
    try:
        node_id = int(body.get("node_id") or 0)
    except (TypeError, ValueError):
        raise Err("请选择节点")
    if not node_id:
        raise Err("请选择节点")
    try:
        local_port = int(body.get("local_port") or 0)
    except (TypeError, ValueError):
        raise Err("本地端口要是数字")
    if not (1 <= local_port <= 65535):
        raise Err("本地端口超出范围")
    local_ip = (body.get("local_ip") or "127.0.0.1").strip()
    # 面板自身端口默认禁止穿透（和 tunnel.py 的 15.22 约定一致）：
    # 公网暴露管理面板等于把整台手机交出去。要开就先在本机改好强密码/TOTP。
    if local_port == panel_port():
        raise Err(f"本地端口 {local_port} 是面板自身端口，默认禁止通过隧道暴露")

    payload = {
        "nodeId": node_id, "proxyName": name, "localIp": local_ip,
        "localPort": local_port, "remotePort": 0, "domain": "",
        "proxyType": ptype, "accessKey": "", "httpPlugin": "",
        "httpUser": body.get("http_user") or "", "httpPassword": body.get("http_password") or "",
        "crtPath": "", "keyPath": "", "proxyProtocolVersion": "",
        "useEncryption": bool(body.get("encrypt")), "useCompression": bool(body.get("compress")),
        "transportProtocol": (body.get("transport") or "tcp"),
        "locations": "", "hostHeaderRewrite": "", "requestHeaders": {}, "responseHeaders": {},
    }
    if ptype in ("tcp", "udp"):
        try:
            payload["remotePort"] = int(body.get("remote_port") or 0)
        except (TypeError, ValueError):
            raise Err("远程端口要是数字")
        if payload["remotePort"] and not (1 <= payload["remotePort"] <= 65535):
            raise Err("远程端口超出范围")
    else:
        dom = (body.get("domain") or "").strip()
        if not dom:
            raise Err("HTTP/HTTPS 隧道要填域名")
        payload["domain"] = json.dumps([dom])
        payload["httpPlugin"] = "https2https" if ptype == "https" else "http2http"
        if ptype == "https":
            payload["crtPath"] = (body.get("crt_path") or "").strip()
            payload["keyPath"] = (body.get("key_path") or "").strip()
            if not payload["crtPath"] or not payload["keyPath"]:
                raise Err("HTTPS 隧道要填证书与私钥路径")
    _api("POST", "/auth/proxy/create", payload)
    return True


def update_proxy(pid, body):
    cur = None
    for p in proxies():
        if str(p["id"]) == str(pid):
            cur = p
            break
    if not cur:
        raise Err("隧道不存在", 404)
    ptype = cur["type"]
    payload = {
        "proxyId": int(pid),
        "proxyName": (body.get("name") or cur["name"]).strip(),
        "localIp": (body.get("local_ip") or cur["local_ip"]).strip(),
        "localPort": int(body.get("local_port") or cur["local_port"] or 0),
        "remotePort": int(body.get("remote_port") or cur["remote_port"] or 0),
        "domain": cur["domain"] or "",
        "proxyType": ptype,
        "httpPlugin": "https2https" if ptype == "https" else ("http2http" if ptype == "http" else ""),
        "httpUser": body.get("http_user", cur.get("http_user") or ""),
        "httpPassword": body.get("http_password") or "",
        "useEncryption": bool(body.get("encrypt", cur["encrypt"])),
        "useCompression": bool(body.get("compress", cur["compress"])),
        "transportProtocol": body.get("transport") or cur["transport"],
        "locations": "", "hostHeaderRewrite": "", "requestHeaders": {}, "responseHeaders": {},
        "crtPath": "", "keyPath": "", "proxyProtocolVersion": "",
    }
    if ptype in ("http", "https") and body.get("domain"):
        payload["domain"] = json.dumps([body["domain"].strip()])
    if ptype == "https":
        payload["crtPath"] = (body.get("crt_path") or "").strip()
        payload["keyPath"] = (body.get("key_path") or "").strip()
    _api("POST", "/auth/proxy/update", payload)
    return True


def delete_proxy(pid):
    if (_procs.get(str(pid)) and _procs[str(pid)]["proc"].poll() is None):
        stop(pid)
    _api("POST", "/auth/proxy/delete", {"proxyId": int(pid)})
    with _lock:
        _run.pop(str(pid), None)
        _save_run()
    return True


def toggle_proxy(pid, disabled):
    _api("POST", "/auth/proxy/toggle", {"proxyId": int(pid), "isDisabled": bool(disabled)})
    return True


def kick_proxy(pid):
    _api("POST", "/auth/proxy/kick", {"proxyId": int(pid)})
    return True


def proxy_config(pid, fmt="toml"):
    fmt = fmt if fmt in ("toml", "ini", "json", "yml") else "toml"
    d = _api("POST", "/auth/proxy/config", {"proxyId": int(pid), "format": fmt}) or {}
    return {"format": fmt, "config": d.get("config") or ""}


def launch_command(pid):
    """官方客户端快捷启动命令（等价于网页面板里的「启动参数」）。"""
    return f"./mefrpc -t <客户端令牌> -p {int(pid)}"


# ---------------- 客户端二进制 ----------------

def _client_candidates():
    seen = []
    cfg = (core.cfg_get("mefrp", "client_path", default="") or "").strip()
    if cfg:
        seen.append(cfg)
    for p in (os.path.join(BIN_DIR, "mefrpc"), os.path.join(HOME, "mefrpc"),
              os.path.join(PREFIX, "bin", "mefrpc"), shutil.which("mefrpc")):
        if p:
            seen.append(p)
    # ~/mefrpc_android_arm_* 这类解压目录
    try:
        for name in sorted(os.listdir(HOME), reverse=True):
            if name.startswith("mefrpc"):
                q = os.path.join(HOME, name, "mefrpc")
                if os.path.isfile(q):
                    seen.append(q)
    except OSError:
        pass
    out = []
    for p in seen:
        if p and p not in out and os.path.isfile(p) and os.access(p, os.X_OK):
            out.append(p)
    return out


def find_client():
    c = _client_candidates()
    return c[0] if c else ""


def client_info(force=False):
    now = time.time()
    if not force and _client_cache["info"] and now - _client_cache["ts"] < 60:
        return _client_cache["info"]
    path = find_client()
    info = {"found": bool(path), "path": path, "version": "", "error": ""}
    if path:
        try:
            r = subprocess.run([path, "-v"], capture_output=True, timeout=15,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            info["version"] = out.splitlines()[0].strip() if out else err.splitlines()[0].strip() if err else ""
        except Exception as e:  # noqa: BLE001
            info["error"] = f"执行失败：{e}"
    _client_cache.update({"ts": now, "info": info})
    return info


def client_install(tar_path=""):
    """从本地 tar（或已解压目录）装一份到 data/mefrp/bin/mefrpc。"""
    os.makedirs(BIN_DIR, mode=0o700, exist_ok=True)
    dst = os.path.join(BIN_DIR, "mefrpc")
    candidates = []
    if tar_path:
        candidates.append(tar_path)
    # 常见落点：/sdcard/Download 里的官方包
    for root in ("/sdcard/Download", "/sdcard/下载", os.path.join(HOME, "storage", "downloads"), HOME):
        try:
            for name in sorted(os.listdir(root), reverse=True):
                if name.startswith("mefrpc") and name.endswith(".tar"):
                    candidates.append(os.path.join(root, name))
        except OSError:
            continue
    src = ""
    for c in candidates:
        if os.path.isfile(c):
            src = c
            break
    if not src:
        raise Err("没找到 mefrpc 安装包；请把官方 tar 放到 /sdcard/Download，"
                  "或在设置里指定客户端路径")
    try:
        with tarfile.open(src) as tf:
            member = None
            for m in tf.getmembers():
                if m.isfile() and os.path.basename(m.name) == "mefrpc":
                    member = m
                    break
            if not member:
                raise Err("压缩包里没有 mefrpc")
            fh = tf.extractfile(member)
            tmp = dst + ".tmp"
            with open(tmp, "wb") as out:
                shutil.copyfileobj(fh, out)
            os.chmod(tmp, 0o700)
            os.replace(tmp, dst)
    except Err:
        raise
    except Exception as e:  # noqa: BLE001
        raise Err(f"解压失败：{e}")
    _client_cache.update({"ts": 0, "info": None})
    info = client_info(force=True)
    if not info["found"]:
        raise Err("装好了但执行不起来，检查架构是否匹配")
    return {"installed": dst, "version": info["version"], "source": src}


# ---------------- 隧道进程托管 ----------------

def _log_path(pid):
    return os.path.join(DIR, f"{pid}.log")


def _ensure_dirs():
    os.makedirs(DIR, mode=0o700, exist_ok=True)


def _load_run():
    global _run, _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _run = core.read_json(_file_run(), {}) or {}
        _loaded = True


def _save_run():
    _ensure_dirs()
    core.write_json(_file_run(), _run)


def _rotate_log(path):
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass


class _ProcRef:
    """从 /proc 认领回来的进程句柄：只需要 poll / terminate / kill / wait。

    面板被 SIGKILL、或者客户端被别的东西拉起时，进程还在但 Popen 对象没了。
    这时候再 `start()` 一次，平台会回「隧道当前在线」，两边互相踢。所以先
    扫描 /proc 认领，用一个只有 pid 的轻量对象顶替 Popen。
    """

    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None

    def poll(self):
        try:
            os.kill(self.pid, 0)
        except OSError:
            if self.returncode is None:
                self.returncode = -1
            return self.returncode
        return None

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def wait(self, timeout=None):
        end = time.time() + (timeout or 0)
        while self.poll() is None:
            if time.time() > end:
                raise subprocess.TimeoutExpired("mefrpc", timeout)
            time.sleep(0.1)
        return self.returncode


def _find_running(pid_str, client_path=""):
    """在 /proc 里找「正在托管某条隧道」的 mefrpc 进程，返回 pid 列表。"""
    pid_str = str(pid_str)
    hits = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return hits
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        toks = [t for t in raw.decode("utf-8", "replace").split("\0") if t]
        if not toks:
            continue
        exe = os.path.basename(toks[0])
        if exe not in ("mefrpc", "frpc"):
            continue
        if client_path and toks[0] != client_path:
            continue
        for i, tok in enumerate(toks):
            if tok in ("-p", "--proxy") and i + 1 < len(toks):
                if pid_str in [x.strip() for x in toks[i + 1].split(",")]:
                    hits.append(int(name))
                    break
    return hits


def _adopt(pid_str):
    """已有同隧道的客户端在跑就认领，多出来的杀掉——两个客户端会互相踢。"""
    pid_str = str(pid_str)
    cur = _procs.get(pid_str)
    if cur and cur["proc"].poll() is None:
        return True
    found = _find_running(pid_str)
    if not found:
        return False
    keep = found[0]
    for extra in found[1:]:
        try:
            os.kill(extra, signal.SIGKILL)
        except OSError:
            pass
    with _lock:
        _procs[pid_str] = {"proc": _ProcRef(keep), "started_at": int(time.time()),
                           "lf": None, "adopted": True}
    return True


def start(pid):
    """用官方客户端把一条隧道拉起来（快捷启动，不落配置文件）。"""
    _ensure_dirs()
    _load_run()
    pid = str(pid)
    info = client_info()
    if not info["found"]:
        raise Err("没找到 mefrpc 客户端，先在「客户端」里安装或指定路径")
    ft = frp_token()
    if not ft:
        raise Err("取不到客户端令牌，请重新登录 MEFrp")
    with _lock:
        cur = _procs.get(pid)
        if cur and cur["proc"].poll() is None:
            _run.setdefault(pid, {})["want"] = True
            _save_run()
            return True
    # 面板重启/被强杀后子进程可能还活着，先认领，避免第二个客户端把隧道踢掉
    if _adopt(pid):
        with _lock:
            st = _run.setdefault(pid, {})
            st.update({"want": True, "fail_cnt": 0, "next_retry": 0, "last_error": ""})
            _save_run()
        return True
    with _lock:
        if _procs.get(pid) and _procs[pid]["proc"].poll() is None:
            return True
        log = _log_path(pid)
        _rotate_log(log)
        lf = open(log, "ab")  # noqa: SIM115 进程生命周期内持有
        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动隧道 {pid} =====\n".encode())
        lf.flush()
        try:
            pr = subprocess.Popen(
                [info["path"], "-t", ft, "-p", pid, "-n"],
                stdout=lf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=DIR,
            )
        except Exception as e:  # noqa: BLE001
            lf.close()
            raise Err(f"启动失败：{e}")
        _procs[pid] = {"proc": pr, "started_at": int(time.time()), "lf": lf}
        st = _run.setdefault(pid, {})
        st.update({"want": True, "fail_cnt": 0, "next_retry": 0, "last_error": ""})
        _save_run()
    return True


def stop(pid, forget=False):
    _load_run()
    pid = str(pid)
    with _lock:
        st = _run.setdefault(pid, {})
        st["want"] = False
        if forget:
            st["fail_cnt"] = 0
            st["next_retry"] = 0
        _save_run()
        cur = _procs.pop(pid, None)
    if cur:
        _kill(cur)

    return True


def _kill(cur):
    pr = cur.get("proc")
    if pr and pr.poll() is None:
        try:
            pr.terminate()
            pr.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                pr.kill()
            except Exception:  # noqa: BLE001
                pass
    try:
        if cur.get("lf"):
            cur["lf"].close()
    except Exception:  # noqa: BLE001
        pass


def restart(pid):
    stop(pid)
    time.sleep(0.4)
    return start(pid)


def log_tail(pid, lines=200):
    path = _log_path(str(pid))
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            back = min(size, 64 * 1024)
            fh.seek(size - back)
            raw = fh.read()
    except OSError:
        return {"text": "", "size": 0}
    txt = raw.decode("utf-8", "replace")
    rows = txt.splitlines()[-int(lines or 200):]
    return {"text": "\n".join(rows), "size": size}


def state():
    """本地托管态汇总：面板重启后 run.json 里的 want 会被保活线程拉起。"""
    _load_run()
    out = {}
    for pid, st in _run.items():
        pr = _procs.get(pid)
        running = bool(pr and pr["proc"].poll() is None)
        out[pid] = {"running": running, "want": bool(st.get("want")),
                    "restarts": st.get("restarts") or 0,
                    "last_error": st.get("last_error") or "",
                    "started_at": (pr or {}).get("started_at") or 0}
    return out


# ---------------- 保活 ----------------

def init():
    _load_run()
    start_monitor()


def start_monitor():
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    t = threading.Thread(target=_monitor_loop, name="mefrp-monitor", daemon=True)
    t.start()


def _monitor_loop():
    time.sleep(8)
    while True:
        try:
            _monitor_tick()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(MONITOR_EVERY)


def _reload_run():
    """把磁盘上的托管意图读回内存。

    正常情况下 start/stop 都及时落盘，内存和文件是一致的；但面板重启、
    或者用户在另一处（脚本、别的实例）改过意图时，文件才是权威。每轮
    保活前重读一次，文件很小，开销可以忽略，换来的是「重启即接管」。
    """
    global _run, _loaded
    disk = core.read_json(_file_run(), {}) or {}
    with _lock:
        if not isinstance(disk, dict):
            disk = {}
        _run.clear()
        _run.update({str(k): dict(v or {}) for k, v in disk.items()})
        _loaded = True


def _monitor_tick():
    _reload_run()
    if not _run:
        return
    now = int(time.time())
    for pid, st in list(_run.items()):
        cur = _procs.get(pid)
        alive = bool(cur and cur["proc"].poll() is None)
        if alive:
            continue
        if cur and not alive:
            # 进程自己退了：收尾并记错误
            rc = cur["proc"].returncode
            _kill(cur)
            with _lock:
                _procs.pop(pid, None)
                st["last_error"] = f"客户端退出（code {rc}）"
                _save_run()
        if not st.get("want"):
            continue
        if not logged_in():
            continue
        if now < int(st.get("next_retry") or 0):
            continue
        fail = int(st.get("fail_cnt") or 0)
        if fail >= FAIL_LIMIT:
            st["want"] = False
            st["last_error"] = f"连续 {fail} 次启动失败，已停止自动拉起"
            with _lock:
                _save_run()
            continue
        try:
            start(pid)
            with _lock:
                st2 = _run.get(pid) or {}
                st2["restarts"] = int(st2.get("restarts") or 0) + 1
                st2["next_retry"] = 0
                _save_run()
        except Exception as e:  # noqa: BLE001
            fail += 1
            back = min(BACKOFF_MAX, 2 ** (fail + 1))
            with _lock:
                st = _run.get(pid) or {}
                st["fail_cnt"] = fail
                st["next_retry"] = int(time.time()) + back
                st["last_error"] = str(e)
                _save_run()


def shutdown():
    with _lock:
        for pid in list(_procs):
            cur = _procs.pop(pid)
            _kill(cur)
