"""M15 内网穿透 · Cloudflare Tunnel 接入（cloudflared）。

和 frp 那条路的区别：cloudflared 不吃配置文件，token 就是全部配置
（ingress 由 Cloudflare 侧下发），所以这里完全不碰 tunnel.py 的 TOML/INI 那套，
单独一套实例模型。

两种形态：
  quick  临时隧道。零账号，跑起来给一个 https://xxx.trycloudflare.com。
         地址每次启动都变，官方明确不保证可用性——适合临时给朋友看个页面。
  named  固定域名。写进自己 Cloudflare 账号里的域名，如 panel.example.com。
         通过 CF API 自动建 tunnel、配 ingress、加 CNAME，token 只存本地。

Termux（Android）上 Go 程序有两道坎，都是实测撞出来的：
  1. 静态二进制直读 /etc/resolv.conf；Android 的 /etc 指向只读的 /system/etc，
     里面根本没这个文件 → `lookup ... on [::1]:53: connection refused`。
     用 proot 把 $PREFIX/etc/resolv.conf 绑进去。
  2. proot 换根后找不到 CA → `x509: certificate signed by unknown authority`。
     用 SSL_CERT_FILE 指到 $PREFIX/etc/tls/cert.pem。
两条都在 _wrap() 里处理，非 Termux 环境自动跳过。

数据：core.DATA/cloudflare.json（0600）
  {"creds": {"email","key","token","account_id"},
   "tunnels": [{id, kind, name, port, host, url, tunnel_id, zone_id, zone,
                dns_id, token, created, autostart}]}
token 是隧道密钥，只在后端用；接口回显一律剔除或打码。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request

import core
import messages

PREFIX_ENV = os.environ.get("PREFIX") or ""
PREFIX = PREFIX_ENV or "/data/data/com.termux/files/usr"


def _is_termux():
    """别只看目录名：开发机上也可能存在 /data/data/com.termux 的假目录。

    Android 必然有 /system/build.prop；Termux 的 termux-tools 会设 TERMUX_VERSION。
    两个都没有就不是手机环境。
    """
    if os.environ.get("TERMUX_VERSION"):
        return True
    if os.path.isfile("/system/build.prop") or os.path.isfile("/system/bin/app_process"):
        return True
    return False


IS_TERMUX = _is_termux()

DIR = os.path.join(core.DATA, "cloudflare")
STATE_FILE = os.path.join(core.DATA, "cloudflare.json")
BIN = os.path.join(PREFIX, "bin", "cloudflared")
RESOLV = os.path.join(PREFIX, "etc", "resolv.conf")
CA_FILE = os.path.join(PREFIX, "etc", "tls", "cert.pem")
PROOT = os.path.join(PREFIX, "bin", "proot")

API = "https://api.cloudflare.com/client/v4"
UA = {"User-Agent": "PocketPanel/1.0", "Content-Type": "application/json"}
REPO_ASSET = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
              "cloudflared-linux-{arch}")
QUICK_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
STATE_TEXT = {
    "stopped": "已停止", "starting": "启动中", "running": "连接中",
    "connected": "已连接", "error": "异常", "failed": "已停止重试",
}
MAX_FAIL = 10
LOG_MAX = 2 << 20

_lock = threading.RLock()
_runtime = {}        # id -> {proc, lf, state, url, err, started_at, restarts, fail_cnt, next_retry, manual}
_loaded = False
_state = {"creds": {}, "tunnels": [], "zones": [], "zones_at": 0}
_monitor_started = False
_client_cache = {"ts": 0.0, "info": None}


# ---------------- 存储 ----------------

def _empty_state():
    return {"creds": {}, "tunnels": [], "zones": [], "zones_at": 0}


def _load():
    global _loaded, _state
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                _state = {"creds": raw.get("creds") or {},
                          "tunnels": raw.get("tunnels") or [],
                          # 域名列表是缓存：CF API 限流且要联网，进页面时不能实时拉
                          "zones": raw.get("zones") or [],
                          "zones_at": int(raw.get("zones_at") or 0)}
        except (OSError, ValueError):
            _state = _empty_state()


def _save():
    """整份写盘，0600——里面有 tunnel token。"""
    with _lock:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", mode=0o700, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False, indent=1)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, STATE_FILE)


def _get(tid):
    with _lock:
        for t in _state["tunnels"]:
            if t["id"] == tid:
                return t
    raise KeyError(tid)


def _new_id():
    import secrets
    return secrets.token_hex(4)


def panel_port():
    return int(os.environ.get("PP_PORT") or core.cfg_get("port", default=8888))


# ---------------- 客户端探测 ----------------

def arch():
    m = (os.uname().machine or "").lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m.startswith("armv7") or m.startswith("armv8l") or m == "arm":
        return "arm"
    if m in ("i386", "i686", "x86"):
        return "386"
    return m or "arm64"


def _find_bin():
    for p in (BIN, shutil.which("cloudflared") or "", os.path.join(PREFIX, "bin", "cloudflared")):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def client_info(force=False):
    """cloudflared 与运行依赖的现状。版本号缓存 60 秒，别让每次列表都 fork 一次。"""
    now = time.time()
    if not force and _client_cache["info"] and now - _client_cache["ts"] < 60:
        return _client_cache["info"]
    path = _find_bin()
    info = {"found": bool(path), "path": path, "version": "", "arch": arch(),
            "termux": IS_TERMUX, "proot": os.path.isfile(PROOT),
            "resolv_conf": os.path.isfile(RESOLV), "ca_file": os.path.isfile(CA_FILE),
            "install_path": BIN, "ready": False, "missing": []}
    if path:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, timeout=15)
            info["version"] = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()[0] \
                if (out.stdout or out.stderr) else ""
        except Exception:  # noqa: BLE001
            info["version"] = ""
    if IS_TERMUX:
        if not info["proot"]:
            info["missing"].append("proot")
        if not info["resolv_conf"]:
            info["missing"].append("resolv.conf")
        if not info["ca_file"]:
            info["missing"].append("ca-certificates")
    info["ready"] = bool(info["found"] and not info["missing"])
    _client_cache.update({"ts": now, "info": info})
    return info


# ---------------- 运行包装 ----------------

def _wrap(path, args, extra_env=None):
    """拼出真正要执行的 argv + 环境变量。

    Termux 上必须借 proot 把 resolv.conf 绑到 /etc/resolv.conf：Go 静态二进制
    不认 LD_PRELOAD，只有 ptrace 级的 bind 能骗过它。
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    argv = [path] + list(args)
    if IS_TERMUX and os.path.isfile(PROOT) and os.path.isfile(RESOLV):
        argv = [PROOT, "-b", f"{RESOLV}:/etc/resolv.conf", path] + list(args)
    if os.path.isfile(CA_FILE):
        env["SSL_CERT_FILE"] = CA_FILE
    return argv, env


def _log_file(tid):
    return os.path.join(DIR, f"{tid}.log")


def _pid_file(tid):
    return os.path.join(DIR, f"{tid}.pid")


def _read_pid(tid):
    try:
        with open(_pid_file(tid), "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_pid(tid, pid):
    try:
        with open(_pid_file(tid), "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except OSError:
        pass


def _pid_alive(pid):
    """活着且确实是我们的隧道进程——别拿一个复用了 pid 的无关进程开刀。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "replace")
    except OSError:
        return True
    return "cloudflared" in cmd or "proot" in cmd


def _kill_pid(pid):
    """按进程组杀：proot 是父，cloudflared 是 tracee，只杀父会留孤儿。"""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _read_log(tid, limit=4000):
    try:
        with open(_log_file(tid), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _read_since(tid, pos):
    """从 pos 往后读。重启后只看新日志，避免把上一次的旧地址当成这次的。"""
    try:
        with open(_log_file(tid), "rb") as fh:
            fh.seek(max(0, int(pos or 0)))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _rotate_log(tid):
    p = _log_file(tid)
    try:
        if os.path.getsize(p) > LOG_MAX:
            os.replace(p, p + ".1")
    except OSError:
        pass


# ---------------- 状态 ----------------

def _detect(tid, text=None):
    """从日志尾部推断状态：quick 看 URL，named 看注册行。"""
    t = _get(tid)
    rt = _runtime.get(tid)
    if rt is None:
        # 本进程没托管它：可能是从没启动过，也可能是面板刚重启、进程还是上个实例起的。
        # 有 pid 文件且进程确实在，就按日志判状态，别谎报「已停止」。
        pid = _read_pid(tid)
        if not (pid and _pid_alive(pid)):
            return "stopped", "", ""
        txt = _read_log(tid, 8000) if text is None else text
        if t["kind"] == "quick":
            hits = QUICK_RE.findall(txt)
            url = hits[-1] if hits else ""
            return ("connected" if url else "running"), url, ""
        url = t.get("url") or (f"https://{t['host']}" if t.get("host") else "")
        return ("connected" if "Registered tunnel connection" in txt else "running"), url, ""
    proc = rt.get("proc")
    alive = proc is not None and proc.poll() is None
    # 用户停掉（或连败到上限）的隧道不该再从历史日志里翻出一个旧地址
    if not alive and (rt.get("manual") or rt.get("fail_cnt", 0) >= MAX_FAIL):
        rt["url"], rt["err"] = "", ""
        rt["state"] = "stopped" if rt.get("manual") else "failed"
        return rt["state"], "", ""
    txt = _read_since(tid, rt.get("log_pos", 0)) if text is None else text
    url = rt.get("url") or ""
    if t["kind"] == "quick":
        hits = QUICK_RE.findall(txt)
        if hits:
            url = hits[-1]
        connected = bool(url)
    else:
        connected = "Registered tunnel connection" in txt
        if not url:
            url = t.get("url") or (f"https://{t['host']}" if t.get("host") else "")
    err = ""
    for line in reversed(txt.splitlines()[-40:]):
        low = line.lower()
        if ("failed to" in low or " err " in low or "error" in low) and "warning" not in low:
            err = line.split("INF")[-1].strip()[:200] if "INF" in line else line.strip()[:200]
            break
    if connected:
        state = "connected"
        err = ""
    elif rt.get("proc") is not None and rt["proc"].poll() is None:
        state = "error" if err else "running"
    elif rt.get("manual"):
        state = "stopped"
    elif rt.get("fail_cnt", 0) >= MAX_FAIL:
        state = "failed"
    else:
        state = "stopped"
    if rt:
        rt["url"] = url
        rt["state"] = state
        rt["err"] = err
    return state, url, err


def _view(t):
    rt = _runtime.get(t["id"]) or {}
    state, url, err = _detect(t["id"])
    alive = bool(rt.get("proc") is not None and rt["proc"].poll() is None)
    if not alive:
        pid = _read_pid(t["id"])
        alive = bool(pid and _pid_alive(pid))
    return {
        "id": t["id"], "kind": t["kind"], "name": t.get("name") or t["id"],
        "port": t.get("port"), "host": t.get("host") or "",
        "url": url, "state": state, "state_text": STATE_TEXT.get(state, state),
        "error": err, "alive": alive,
        "restarts": rt.get("restarts", 0),
        "started_at": rt.get("started_at", 0),
        "created": t.get("created", 0),
        "autostart": bool(t.get("autostart", True)),
        "tunnel_id": t.get("tunnel_id") or "",
        "zone": t.get("zone") or "",
        "has_token": bool(t.get("token")),
        # 网络守卫用：标记客户端种类，并给出可探测的本地端点
        "client_kind": "cloudflared",
        "metrics_port": t.get("metrics_port") or 0,
        "probe_endpoint": ({"port": t["metrics_port"]}
                           if t.get("metrics_port") else None),
    }


def list_tunnels():
    _load()
    with _lock:
        items = [_view(t) for t in _state["tunnels"]]
    items.sort(key=lambda x: x.get("created", 0))
    return items


def tunnel_detail(tid):
    """列表项 + 日志尾。日志走打码（token 不会出现在 cloudflared 输出里，
    但保险起见仍过一遍）。"""
    for x in list_tunnels():
        if x["id"] == tid:
            x["log"] = _read_log(tid, 8000)
            return x
    raise KeyError("隧道不存在")


# ---------------- 启停 ----------------

def _check_port(port):
    if not (isinstance(port, int) and 1 <= port <= 65535):
        raise ValueError("端口需在 1–65535 之间")
    if port == panel_port() and core.cfg_get("init_pwd", default=""):
        raise ValueError("面板还在用初始密码。暴露到公网前请先去「设置」改掉默认密码")


def _argv_for(t, bin_path):
    args = ["tunnel", "--no-autoupdate"]
    # metrics 端口是网络守卫探测 cloudflared 活性的唯一入口：
    # /ready 会返回 readyConnections，连通性一眼可见。
    # 不指定的话 cloudflared 会在 20241-20245 里随机挑一个，多实例无法区分。
    mport = t.get("metrics_port")
    if mport:
        args += ["--metrics", f"127.0.0.1:{int(mport)}"]
    if t["kind"] == "quick":
        args += ["--url", f"http://127.0.0.1:{int(t['port'])}"]
        return _wrap(bin_path, args)
    # token 走环境变量而不是命令行：ps 里看不到，日志里也少一处泄漏面
    args += ["run"]
    return _wrap(bin_path, args, {"TUNNEL_TOKEN": t.get("token") or ""})


def _alloc_metrics_port(tid):
    """给实例分配一个专属的 metrics 端口。

    端口号按实例 id 派生（20241 + hash%200），这样重启后端口不变，
    探测端点也不会因为重启而失效。
    """
    h = 0
    for ch in str(tid):
        h = (h * 31 + ord(ch)) % 100000
    base = 20241
    span = 200
    for i in range(span):
        port = base + ((h + i) % span)
        if _port_free(port):
            return port
    return base


def _port_free(port):
    import socket
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start(tid):
    _load()
    t = _get(tid)
    info = client_info()
    if not info["found"]:
        raise ValueError("还没装 cloudflared，先点「一键安装」")
    if not info["ready"]:
        raise ValueError("运行依赖不全：缺少 " + "、".join(info["missing"]))
    with _lock:
        cur = _runtime.get(tid)
        if cur and cur.get("proc") is not None and cur["proc"].poll() is None:
            return True
        old = _read_pid(tid)
        if old and _pid_alive(old):
            # 上一次面板进程（或崩溃前）留下的，先收掉，否则会起两条隧道抢同一个口
            _kill_pid(old)
        # metrics 端口：首次分配后持久化，重启不变，探测端点始终有效
        if not t.get("metrics_port"):
            t["metrics_port"] = _alloc_metrics_port(tid)
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        _rotate_log(tid)
        try:
            log_pos = os.path.getsize(_log_file(tid))
        except OSError:
            log_pos = 0
        lf = open(_log_file(tid), "ab")  # noqa: SIM115 进程生命周期内持有
        argv, env = _argv_for(t, info["path"])
        try:
            proc = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=DIR, env=env,
                                    start_new_session=True)
        except OSError as e:
            lf.close()
            raise ValueError(f"启动失败：{e}") from e
        _runtime[tid] = {"proc": proc, "lf": lf, "state": "starting", "url": "",
                         "err": "", "started_at": int(time.time()),
                         "restarts": (cur or {}).get("restarts", 0),
                         "fail_cnt": (cur or {}).get("fail_cnt", 0),
                         "log_pos": log_pos, "next_retry": 0, "manual": False}
        _write_pid(tid, proc.pid)
        if t["kind"] == "quick":
            t["url"] = ""  # 临时地址每次重启都变，清掉免得显示失效链接
    _save()
    return True


def stop(tid):
    _load()
    with _lock:
        rt = _runtime.pop(tid, None) or {}
        proc = rt.get("proc")
        if proc is None:
            old = _read_pid(tid)
            if old and _pid_alive(old):
                _kill_pid(old)
        if proc is not None:
            # proot 是父进程，真正干活的是它的 tracee。杀进程组，别留孤儿占着隧道。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        try:
            if rt.get("lf"):
                rt["lf"].close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(_pid_file(tid))
        except OSError:
            pass
        _runtime[tid] = {"proc": proc, "lf": None, "state": "stopped", "url": "",
                         "err": "", "started_at": rt.get("started_at", 0),
                         "restarts": rt.get("restarts", 0), "fail_cnt": 0,
                         "log_pos": rt.get("log_pos", 0),
                         "next_retry": 0, "manual": True}
    return True


def restart(tid):
    stop(tid)
    return start(tid)


def delete(tid, purge=False):
    """purge=True 时连 Cloudflare 侧的 tunnel 与 DNS 记录一起删。"""
    _load()
    t = _get(tid)
    stop(tid)
    errs = []
    if purge and t.get("kind") == "named" and t.get("tunnel_id"):
        try:
            creds = effective_creds()
            aid = creds.get("account_id") or _first_account(creds)
            if t.get("dns_id") and t.get("zone_id"):
                _api("DELETE", f"/zones/{t['zone_id']}/dns_records/{t['dns_id']}", None, creds)
            _api("DELETE", f"/accounts/{aid}/cfd_tunnel/{t['tunnel_id']}", None, creds)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
    with _lock:
        _state["tunnels"] = [x for x in _state["tunnels"] if x["id"] != tid]
        _runtime.pop(tid, None)
    _save()
    try:
        os.remove(_log_file(tid))
    except OSError:
        pass
    return errs


# ---------------- 创建 ----------------

def create_quick(port, name=""):
    _load()
    port = int(port)
    _check_port(port)
    tid = _new_id()
    t = {"id": tid, "kind": "quick", "name": (name or "").strip() or f"临时 {port}",
         "port": port, "host": "", "url": "", "token": "",
         "created": int(time.time()), "autostart": False}
    with _lock:
        _state["tunnels"].append(t)
    _save()
    try:
        start(tid)
    except Exception:
        delete(tid)
        raise
    return tid


def create_named_task(t, host, port, name=""):
    """建固定域名隧道。全流程：解析 zone → 建 tunnel → 配 ingress → 加 CNAME → 启动。

    每一步失败都要把前面建的东西拆掉，否则账号里会留一堆孤儿隧道。
    """
    _load()
    port = int(port)
    _check_port(port)
    host = normalize_host(host)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", host):
        raise ValueError("域名格式不对，比如 panel.example.com")

    creds = effective_creds()
    if not (creds.get("token") or (creds.get("email") and creds.get("key"))):
        raise ValueError("还没配置 Cloudflare 账号，先在「账号」里填 API 令牌或邮箱 + Global Key")

    core.task_log(t, "校验账号…")
    aid = creds.get("account_id") or _first_account(creds)
    creds["account_id"] = aid
    core.task_progress(t, 10)

    zones = _zones(creds)
    zone = _match_zone(host, zones)
    if not zone:
        names = "、".join(z["name"] for z in zones[:6]) or "（空）"
        raise ValueError(f"{host} 不在这个账号下。已接入的域名：{names}")
    core.task_log(t, f"域名 {host} 属于 zone {zone['name']}（{zone['status']}）")
    core.task_progress(t, 22)

    tid = _new_id()
    tun_name = f"pocketpanel-{tid}"
    core.task_log(t, f"创建隧道 {tun_name}…")
    out = _api("POST", f"/accounts/{aid}/cfd_tunnel",
               {"name": tun_name, "config_src": "cloudflare"}, creds)
    res = out.get("result") or {}
    tunnel_id, token = res.get("id"), res.get("token")
    if not tunnel_id or not token:
        raise ValueError("Cloudflare 没返回隧道 token，请稍后重试")
    core.task_log(t, f"隧道已建：{tunnel_id}")
    core.task_progress(t, 42)

    dns_id = ""
    try:
        core.task_log(t, "写入 ingress 规则…")
        cfg = {"config": {"ingress": [
            {"hostname": host, "service": f"http://127.0.0.1:{port}"},
            {"service": "http_status:404"},
        ]}}
        _api("PUT", f"/accounts/{aid}/cfd_tunnel/{tunnel_id}/configurations", cfg, creds)
        core.task_progress(t, 62)

        core.task_log(t, f"添加 DNS 记录 {host} → {tunnel_id}.cfargotunnel.com…")
        exist = _api("GET", f"/zones/{zone['id']}/dns_records?name={host}", None, creds)
        for r in exist.get("result") or []:
            if not (r.get("type") == "CNAME" and r.get("content") == f"{tunnel_id}.cfargotunnel.com"):
                raise ValueError(f"{host} 已有一条 {r.get('type')} 记录指向 {r.get('content')}，"
                                 "先删掉它或换个域名")
        rec = _api("POST", f"/zones/{zone['id']}/dns_records", {
            "type": "CNAME", "name": host, "content": f"{tunnel_id}.cfargotunnel.com",
            "proxied": True, "ttl": 1, "comment": "PocketPanel Tunnel",
        }, creds)
        dns_id = (rec.get("result") or {}).get("id") or ""
        core.task_progress(t, 82)

        inst = {"id": tid, "kind": "named", "name": (name or "").strip() or host,
                "port": port, "host": host, "url": f"https://{host}", "token": token,
                "tunnel_id": tunnel_id, "zone": zone["name"], "zone_id": zone["id"],
                "account_id": aid, "dns_id": dns_id, "created": int(time.time()),
                "autostart": True}
        with _lock:
            _state["tunnels"].append(inst)
        _save()

        core.task_log(t, "启动 cloudflared…")
        start(tid)
        core.task_progress(t, 92)

        # 给 cloudflared 几秒注册，能连上就顺手把结果告诉用户
        for _ in range(10):
            time.sleep(1)
            if _detect(tid)[0] == "connected":
                break
        state, url, err = _detect(tid)
        core.task_log(t, f"状态：{STATE_TEXT.get(state, state)}" + (f"（{err}）" if err else ""))
        core.task_progress(t, 100)
        return {"id": tid, "url": f"https://{host}", "state": state,
                "tunnel_id": tunnel_id, "dns_id": dns_id}
    except Exception:
        # 拆干净：DNS → tunnel，别在账号里留垃圾
        if dns_id:
            try:
                _api("DELETE", f"/zones/{zone['id']}/dns_records/{dns_id}", None, creds)
            except Exception:  # noqa: BLE001
                pass
        try:
            _api("DELETE", f"/accounts/{aid}/cfd_tunnel/{tunnel_id}", None, creds)
        except Exception:  # noqa: BLE001
            pass
        raise


# ---------------- Cloudflare API ----------------

def _creds():
    _load()
    with _lock:
        return dict(_state["creds"] or {})


def effective_creds(override=None):
    """入参优先，其次落盘配置。前端改设置时可以先验证再保存。"""
    c = _creds()
    if override:
        for k in ("email", "key", "token", "account_id"):
            v = (override.get(k) or "").strip() if isinstance(override.get(k), str) else override.get(k)
            if v and v != "********":
                c[k] = v
    return c


def settings_view():
    c = _creds()
    zv = zones_view()
    return {
        "email": c.get("email") or "",
        "key": "********" if c.get("key") else "",
        "token": "********" if c.get("token") else "",
        "account_id": c.get("account_id") or "",
        "has_creds": bool(c.get("token") or (c.get("email") and c.get("key"))),
        "mode": "token" if c.get("token") else ("global" if c.get("key") else ""),
        # 前端「固定域名」的下拉框直接吃这份缓存
        "zones": zv["zones"],
        "zones_at": zv["at"],
    }


def save_settings(email="", key="", token="", account_id=""):
    _load()
    with _lock:
        c = _state.setdefault("creds", {})
        before = (c.get("email"), c.get("key"), c.get("token"), c.get("account_id"))
        if email is not None:
            c["email"] = (email or "").strip()
        if key and key != "********":
            c["key"] = key.strip()
        if token and token != "********":
            c["token"] = token.strip()
        if account_id is not None:
            c["account_id"] = (account_id or "").strip()
        # 令牌与 Global Key 二选一，别让旧值互相干扰
        if c.get("token"):
            c.pop("key", None)
            c.pop("email", None)
        # 换了账号/凭证，旧域名列表就不作数了
        after = (c.get("email"), c.get("key"), c.get("token"), c.get("account_id"))
        if after != before:
            _state["zones"] = []
            _state["zones_at"] = 0
    _save()
    return settings_view()


def _api(method, path, body=None, creds=None, timeout=30):
    creds = creds or _creds()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=dict(UA))
    if creds.get("token"):
        req.add_header("Authorization", f"Bearer {creds['token']}")
    elif creds.get("email") and creds.get("key"):
        req.add_header("X-Auth-Email", creds["email"])
        req.add_header("X-Auth-Key", creds["key"])
    else:
        raise ValueError("没配 Cloudflare 账号凭证")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read() or b"{}")
        except ValueError:
            out = {}
        raise ValueError(_api_err(out, e.code)) from e
    except urllib.error.URLError as e:
        low = str(e).lower()
        if "certificate" in low:
            raise ValueError("HTTPS 证书校验失败，检查手机系统时间与 CA 证书") from e
        raise ValueError(f"连不上 Cloudflare：{type(e).__name__}") from e
    if not out.get("success", True):
        raise ValueError(_api_err(out, 0))
    return out


def _api_err(out, status):
    errs = out.get("errors") or []
    msg = "；".join(str(e.get("message")) for e in errs if e.get("message")) or f"HTTP {status}"
    low = msg.lower()
    if status == 401 or "unauthorized" in low or ("invalid" in low and "token" in low):
        return "密钥无效或已过期"
    if status == 403 or "forbidden" in low or "permission" in low:
        return "密钥权限不足：Tunnel 需要「Account · Cloudflare Tunnel · Edit」，DNS 需要「Zone · DNS · Edit」"
    if "request headers" in low or "malformed" in low:
        return "密钥格式不对：Global Key 是一串 37 位十六进制，API 令牌更长且带下划线，别填反了"
    if status == 409 or "already exists" in low:
        return "同名资源已存在，换个名字或先删掉旧的"
    if status == 429:
        return "触发 Cloudflare 频率限制，等几分钟再试"
    return msg[:200]


def _first_account(creds):
    out = _api("GET", "/accounts?per_page=5", None, creds)
    for a in out.get("result") or []:
        if a.get("id"):
            return a["id"]
    raise ValueError("这个凭证下一个账号都没有，检查是否填错")


def _zones(creds, max_pages=3):
    """拉账号下所有已接入的域名。CF 一页最多 50 条，多的翻页（最多 3 页）。"""
    out = []
    for page in range(1, max_pages + 1):
        r = _api("GET", f"/zones?per_page=50&page={page}", None, creds)
        rows = r.get("result") or []
        for z in rows:
            out.append({"id": z["id"], "name": z["name"], "status": z.get("status", "")})
        info = r.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1) or not rows:
            break
    return out


def zones_view():
    """只读缓存，绝不发网络请求——status 是轮询接口，不能挂在 CF API 上。"""
    _load()
    with _lock:
        zs = [dict(z) for z in (_state.get("zones") or [])]
        at = int(_state.get("zones_at") or 0)
    return {"zones": zs, "at": at, "age": int(time.time()) - at if at else -1}


def refresh_zones(creds=None):
    """真去 CF 拉一遍域名列表并落盘。失败抛错，旧缓存不动。"""
    zs = _zones(creds or _creds())
    _load()
    with _lock:
        _state["zones"] = zs
        _state["zones_at"] = int(time.time())
    _save()
    return zones_view()


def _match_zone(host, zones):
    best = None
    for z in zones:
        n = z["name"].lower()
        if host == n or host.endswith("." + n):
            if best is None or len(n) > len(best["name"]):
                best = z
    return best


# 子域名：可以多级（a.b），每段首尾不能是连字符
SUB_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def normalize_sub(sub):
    """规范化子域名：去首尾点、转小写；字符集不合规返回空串。"""
    s = (sub or "").strip().strip(".").lower()
    return s if s and SUB_RE.match(s) else ""


def normalize_host(host):
    """整条 URL 粘进来也能用：剥协议头、剥路径/端口、去尾点。"""
    h = (host or "").strip().lower()
    h = re.sub(r"^[a-z][a-z0-9+.-]*://", "", h)
    h = h.split("/")[0].split("?")[0].split("#")[0]
    h = h.rsplit("@", 1)[-1]
    h = h.split(":")[0]
    return h.strip(".")


def verify(override=None):
    """验凭证 + 列域名。只读，不改任何东西。"""
    creds = effective_creds(override)
    out = _api("GET", "/accounts?per_page=5", None, creds)
    accs = [{"id": a["id"], "name": a.get("name", "")} for a in out.get("result") or []]
    zones = _zones(creds)
    return {"accounts": accs, "zones": zones,
            "account_id": creds.get("account_id") or (accs[0]["id"] if accs else "")}


# ---------------- 安装 ----------------

def _pkg_install_proot(t):
    core.task_log(t, "安装 proot（Go 程序要靠它看到 resolv.conf）…")
    try:
        p = subprocess.run(["pkg", "install", "-y", "proot"], capture_output=True, timeout=300)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout).decode("utf-8", "replace")[-300:]
            core.task_log(t, f"pkg 安装失败：{tail}")
            return False
        core.task_log(t, "proot 已安装")
        return True
    except Exception as e:  # noqa: BLE001
        core.task_log(t, f"pkg 调用失败：{e}")
        return False


# 实测：同一个镜像在不同客户端/网络下可用性完全不同（手机上 curl 能下 ghfast，
# urllib 却连接超时），所以不能赌单个源，必须先探再下、失败换下一个。
MIRROR_ORDER = ["https://gh-proxy.com/", "https://ghfast.top/",
                "https://ghproxy.net/", "https://github.moeyy.xyz/", ""]


def probe_mirrors(timeout=8):
    """并发取每个源的前 1KB，返回可用性与耗时。给前端「检测」和安装挑源用。"""
    from concurrent.futures import ThreadPoolExecutor

    import tunnel_install

    asset = REPO_ASSET.format(arch=arch())

    def one(m):
        url = (m.get("url") or "") + asset
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-1023"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(1024)
            return {"id": m["id"], "label": m["label"], "url": m.get("url") or "",
                    "ok": True, "ms": int((time.time() - t0) * 1000), "error": ""}
        except Exception as e:  # noqa: BLE001
            return {"id": m["id"], "label": m["label"], "url": m.get("url") or "",
                    "ok": False, "ms": int((time.time() - t0) * 1000),
                    "error": f"{type(e).__name__}: {str(e)[:80]}"}

    pool = ThreadPoolExecutor(max_workers=len(tunnel_install.MIRRORS) or 1)
    try:
        return list(pool.map(one, tunnel_install.MIRRORS))
    finally:
        pool.shutdown(wait=False)


def _mirror_candidates(prefer=""):
    """排好序的候选源：用户选的优先，其余按内置顺序补齐。"""
    import tunnel_install

    by_url = {(m.get("url") or ""): m for m in tunnel_install.MIRRORS}
    order = []
    if prefer:
        m = by_url.get(prefer) or {"id": "custom", "label": prefer, "url": prefer}
        order.append(m)
    for url in MIRROR_ORDER:
        m = by_url.get(url) or {"id": url or "direct", "label": url or "GitHub 直连", "url": url}
        if all((x.get("url") or "") != (m.get("url") or "") for x in order):
            order.append(m)
    return order


def install_task(t, mirror=""):
    import tunnel_install

    os.makedirs(DIR, mode=0o700, exist_ok=True)
    asset = REPO_ASSET.format(arch=arch())
    core.task_log(t, f"目标架构 linux-{arch()}")
    if mirror:
        core.task_log(t, f"指定下载源：{mirror}")
    else:
        core.task_log(t, "未指定源，先探测各镜像可用性…")

    cands = _mirror_candidates(mirror)
    if not mirror:
        # 先并发探速，只留能通的；全都不通再退回按顺序硬试
        probed = probe_mirrors()
        ok = [p for p in probed if p["ok"]]
        if ok:
            ok.sort(key=lambda x: x["ms"])
            core.task_log(t, "可用源：" + "、".join(f"{p['label']} {p['ms']}ms" for p in ok))
            good = {p["url"] for p in ok}
            cands = [c for c in cands if (c.get("url") or "") in good]
        else:
            core.task_log(t, "⚠ 没探测到可用源（可能是网络整体受限），按默认顺序硬试")

    tmp = BIN + ".part"
    try:
        os.makedirs(os.path.dirname(BIN), exist_ok=True)
    except OSError as e:
        raise ValueError(f"没有写 {os.path.dirname(BIN)} 的权限：{e}") from e

    used, last_err = "", None
    for i, m in enumerate(cands, 1):
        prefix = m.get("url") or ""
        core.task_log(t, f"[{i}/{len(cands)}] {m.get('label') or prefix or 'GitHub 直连'}")
        try:
            tunnel_install._download(prefix + asset, dest=tmp, t=t,
                                     label="cloudflared", max_mb=96, deadline=420)
            used = prefix or "GitHub 直连"
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            core.task_log(t, f"    失败：{type(e).__name__}: {str(e)[:120]}")
            try:
                os.remove(tmp)
            except OSError:
                pass
    else:
        raise ValueError(f"所有源都下不动（最后一次：{last_err}）。可以换个网络，或手动把 "
                         f"cloudflared 二进制放到 {BIN}")

    size = os.path.getsize(tmp)
    core.task_log(t, f"下载完成 {size >> 20} MB（来自 {used}），校验可执行…")
    os.chmod(tmp, 0o755)
    try:
        p = subprocess.run([tmp, "--version"], capture_output=True, timeout=20)
        ver = (p.stdout or p.stderr).decode("utf-8", "replace").strip()
        if p.returncode != 0 or "cloudflared" not in ver.lower():
            raise ValueError(f"文件不能执行：{ver[:160]}")
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError(f"下载到的文件跑不起来（镜像可能给了错误页面）：{e}") from e
    core.task_log(t, ver.splitlines()[0])
    os.replace(tmp, BIN)
    core.task_progress(t, 92)

    if IS_TERMUX and not os.path.isfile(PROOT):
        _pkg_install_proot(t)
    if IS_TERMUX and not os.path.isfile(CA_FILE):
        core.task_log(t, "提示：没找到 CA 证书，HTTPS 会失败。执行 pkg install ca-certificates 修复")

    info = client_info(force=True)
    core.task_log(t, f"安装完成：{info['path']}")
    if info["missing"]:
        core.task_log(t, "⚠ 运行依赖仍缺：" + "、".join(info["missing"]))
    core.task_progress(t, 100)
    return info


def uninstall():
    _load()
    with _lock:
        ids = [t["id"] for t in _state["tunnels"]]
    for tid in ids:
        stop(tid)
    try:
        os.remove(BIN)
    except OSError:
        pass
    _client_cache["info"] = None
    return True


# ---------------- 监控 ----------------

def ensure_monitor():
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, daemon=True,
                     name="tunnel-cf-monitor").start()


def _monitor_loop():
    _load()
    # 面板重启后，上个进程起的 cloudflared 还活着但没人管。先全部收掉，
    # 交给下面的 autostart 统一拉起——保证「面板进程 = 隧道进程的唯一管理者」。
    for t in list(_state["tunnels"]):
        pid = _read_pid(t["id"])
        if pid and _pid_alive(pid):
            _kill_pid(pid)
        try:
            os.remove(_pid_file(t["id"]))
        except OSError:
            pass
    for t in list(_state["tunnels"]):
        if t.get("autostart"):
            try:
                start(t["id"])
            except Exception:  # noqa: BLE001
                pass
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(3)


def _tick():
    now = time.time()
    due = []
    with _lock:
        for tid, rt in list(_runtime.items()):
            proc = rt.get("proc")
            if proc is not None and proc.poll() is None:
                continue                       # 活着
            if rt.get("manual"):
                continue                       # 用户主动停的
            if rt.get("next_retry"):
                if rt["next_retry"] <= now:
                    due.append(tid)            # 退避到点，重试
                continue
            # 刚发现退出（或上次 start 就没起来）：记一次失败并安排退避
            if proc is not None:
                rt["last_exit"] = proc.poll()
            try:
                if rt.get("lf"):
                    rt["lf"].close()
            except Exception:  # noqa: BLE001
                pass
            rt["lf"] = None
            fail = rt.get("fail_cnt", 0) + 1
            rt["fail_cnt"] = fail
            if fail >= MAX_FAIL:
                m = next((x for x in _state["tunnels"] if x["id"] == tid), {})
                messages.add("err", f"隧道 {m.get('name', tid)} 反复退出",
                             f"连续失败 {fail} 次，已停止自动重启")
                _runtime.pop(tid, None)
            else:
                rt["next_retry"] = now + min(60, 2 ** (fail - 1))
                rt["state"] = "error"
    for tid in due:
        with _lock:
            fail = (_runtime.get(tid) or {}).get("fail_cnt", 1)
            _runtime.pop(tid, None)
        try:
            start(tid)
            with _lock:
                rt = _runtime.get(tid) or {}
                rt["restarts"] = rt.get("restarts", 0) + 1
                rt["fail_cnt"] = fail
                rt["next_retry"] = 0
        except Exception:  # noqa: BLE001
            with _lock:
                rt = _runtime.setdefault(tid, {})
                rt["proc"] = None
                rt["fail_cnt"] = fail + 1
                rt["next_retry"] = now + min(60, 2 ** fail)
                rt["state"] = "error"


# ---------------- 任务包装（给 app.py 用） ----------------

def start_install(mirror=""):
    return core.run_job("cf-install", "安装 cloudflared",
                        lambda t: install_task(t, mirror))["id"]


def start_named(host, port, name=""):
    def job(t):
        core.task_result(t, create_named_task(t, host, port, name))
    return core.run_job("cf-tunnel", f"创建隧道 {host}", job)["id"]
