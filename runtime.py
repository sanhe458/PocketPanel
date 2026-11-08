"""M17 容器运行时：ContainerRuntimeAdapter。

定位：**兼容 Docker 镜像生态的受限容器管理**，不是 Docker 管理器。
面板只通过 Docker Engine API 与 udockerd 通信（dockerclient.py），
不直接拼 udocker 命令；udockerd 自己作为服务交给 svc.py 托管。

边界是产品的一部分，所以在 `capabilities()` 里显式返回，前端据此渲染，
不在 UI 里写死一份、后端再假设另一份：

* 网络恒为 host：端口映射只记录，容器直接监听宿主机端口；
* 无 cgroups：不做 CPU / 内存配额；
* 无 Volume 对象：持久化只靠 bind mount，且 PRoot 无法强制只读；
* 无构建：udocker 1.3.x 没有 build 子命令，BuildKit 也不可用；
* Compose 只支持子集（compose.py），不是原生 API；
* 端口 < 1024 不可监听（非 root 限制）。
"""
import calendar
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

import compose
import core
import dockerclient
import messages
import svc
import term
import udocker_install

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UDOCKERD = os.path.join(APP_DIR, "udockerd.py")
SETTINGS_FILE = os.path.join(core.DATA, "docker.json")
RUNTIME_DIR = os.path.join(core.DATA, "docker-runtime")
IMPORT_DIR = os.path.join(core.DATA, "docker-import")
SERVICE = "udockerd"
SERVICE_TITLE = "容器运行时 udockerd"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
USER_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,32}(?::[A-Za-z0-9_.\-]{0,32})?$")
SECRET_RE = re.compile(r"(token|secret|passw|pwd|key|credential|auth)", re.I)
PLATFORM_RE = re.compile(r"^linux/[a-z0-9_]+$")
REGISTRY_RE = re.compile(r"^https?://[^\s/]+/?$")

EXEC_MODES = {
    "P1": "PRoot + seccomp（默认）",
    "P2": "PRoot 关加速（老内核更稳）",
    "F1": "Fakechroot（更快，需内核支持）",
}

DEFAULT_RESTART = "no"
RESTARTS = ("no", "always", "on-failure")

# /events 是长连接，每隔这么久主动重建一次：用户改了 Engine API 端口、或换了
# 一个守护进程实例，最迟这个时间就能生效；重连按水位续上，不会丢事件。
EVENTS_TIMEOUT = 30
# 连上后按状态补账的时间窗口，只补最近 5 分钟内的异常退出。
CATCH_UP_WINDOW = 300

# 不允许映射进容器的宿主机目录：面板数据（含密码哈希、隧道 token）就在里面
PROTECTED_HOSTS = (core.DATA,)
FORBIDDEN_CONTAINER_PATHS = ("/", "/proc", "/sys", "/dev", "/etc/passwd",
                             "/etc/group", "/etc/hosts", "/etc/resolv.conf")

_lock = threading.RLock()
_cache = {"at_c": 0, "containers": [], "at_i": 0, "images": []}
_uck = {"at": 0, "data": {}}
_monitor_started = False
_alert_at = {}
_last_event = 0.0


# ---------------- 配置 ----------------

def _arch():
    m = (os.uname().machine or "").lower()
    return {"aarch64": "arm64", "armv8l": "arm64", "armv7l": "arm",
            "x86_64": "amd64", "i686": "386", "i386": "386"}.get(m, m or "arm64")


def _default_settings():
    return {
        "registry": "",              # 空 = Docker Hub 官方地址
        "platform": "linux/" + _arch(),
        "execmode": "P1",
        "port": 2375,
        "autostart": True,
        "log_tail": 300,
        "udocker": "",               # 空 = 自动探测
    }


def settings():
    d = core.read_json(SETTINGS_FILE, {})
    s = _default_settings()
    if isinstance(d, dict):
        for k in s:
            if k in d and d[k] not in (None, ""):
                s[k] = d[k]
    return s


def save_settings(patch):
    s = settings()
    for k, v in (patch or {}).items():
        if k not in s:
            continue
        if k == "registry":
            v = (v or "").strip().rstrip("/")
            if v and not REGISTRY_RE.match(v):
                raise ValueError("镜像仓库地址需形如 https://registry-1.docker.io")
        elif k == "platform":
            if not PLATFORM_RE.match(str(v or "")):
                raise ValueError("平台格式应为 linux/arm64")
        elif k == "execmode":
            if v not in EXEC_MODES:
                raise ValueError("执行模式只支持 P1 / P2 / F1")
        elif k == "port":
            v = int(v)
            if not (1024 <= v <= 65535):
                raise ValueError("端口需在 1024–65535")
        elif k == "autostart":
            v = bool(v)
        elif k == "log_tail":
            v = max(50, min(int(v), 5000))
        elif k == "udocker":
            v = (v or "").strip()
        s[k] = v
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_FILE)
    _uck["at"] = 0
    _cache["at_c"] = _cache["at_i"] = 0
    return s


def client():
    s = settings()
    base = os.environ.get("DOCKER_HOST") or f"tcp://127.0.0.1:{s['port']}"
    return dockerclient.EngineClient(base)


# ---------------- udocker 探测 ----------------

def _find_udocker():
    s = settings()
    cands = [s.get("udocker") or "",
             shutil.which("udocker") or "",
             os.path.join(core.PREFIX, "bin", "udocker"),
             os.path.join(core.HOME, "udocker", "udocker"),
             os.path.join(core.HOME, ".local", "bin", "udocker"),
             # 面板「一键安装」的落地位置：<data>/runtime/udocker/udocker/udocker
             udocker_install.udocker_bin()]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def udocker_info(force=False):
    now = time.time()
    with _lock:
        if not force and now - _uck["at"] < 60 and _uck["data"]:
            return dict(_uck["data"])
    path = _find_udocker()
    # installed = 客户端可用；managed = 装在面板自己的目录里（只有这种能一键卸载）
    info = {"found": bool(path), "installed": bool(path),
            "managed": bool(path) and path == udocker_install.udocker_bin(),
            "path": path, "version": "", "englib": False, "englib_modes": []}
    if path:
        try:
            # 走 udocker_install._argv：udocker 是 `#!/usr/bin/env python`
            # 的脚本，而 Android 没有 /usr/bin。从 runit 起的服务缺
            # LD_PRELOAD 时 execve 直接 ENOENT，版本号会一直是空。
            r = subprocess.run(udocker_install._argv(path) + ["--version"],
                               capture_output=True, timeout=8)
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
            for line in out.splitlines():
                if line.lower().startswith("version:"):
                    info["version"] = line.split(":", 1)[1].strip()
                    break
        except (OSError, subprocess.TimeoutExpired):
            info["version"] = ""
        bin_dir = os.path.join(core.HOME, ".udocker", "bin")
        try:
            proots = [f for f in os.listdir(bin_dir) if f.startswith("proot-")]
        except OSError:
            proots = []
        info["englib"] = bool(proots)
        info["englib_modes"] = sorted({f.split("-")[0] for f in proots})
    with _lock:
        _uck.update(at=now, data=info)
    return dict(info)


def _pick_modes(englib_modes):
    """按 englib 实际提供的引擎裁剪执行模式。

    官方 englib 同时带 proot 与 fakechroot；Android 上只有 proot，
    此时再把 F1 列出来就是骗人 —— 选了也起不来。
    """
    modes = dict(EXEC_MODES)
    if not englib_modes:
        return modes
    if "fakechroot" not in englib_modes:
        for k in [k for k in modes if k.startswith("F")]:
            modes.pop(k, None)
    if "proot" not in englib_modes:
        for k in [k for k in modes if k.startswith("P")]:
            modes.pop(k, None)
    return modes


def capabilities(englib_modes=None):
    if englib_modes is None:
        englib_modes = udocker_info().get("englib_modes") or []
    return {
        "network": {
            "mode": "host", "isolated": False,
            "title": "网络恒为 host",
            "detail": "容器与 Termux 共享网络栈：没有 bridge、没有容器间隔离，"
                      "端口映射只作记录，容器直接监听宿主机端口。",
        },
        "resources": {
            "cpu": False, "memory": False,
            "title": "无资源限制",
            "detail": "没有 cgroups，CPU / 内存配额无法生效，因此不提供配额滑块。",
        },
        "volumes": {
            "objects": False, "bind": True, "readonly_enforced": False,
            "title": "无 Volume 对象",
            "detail": "docker volume 相关能力为空；持久化只用宿主机目录映射"
                      "（bind mount）。PRoot 无法强制只读，只读选项仅作标注。",
        },
        "build": {
            "buildkit": False, "supported": False,
            "title": "无 BuildKit / 无构建",
            "detail": "udocker 1.3.x 没有 build 子命令，BuildKit 也不可用。"
                      "请在其他机器 docker build 后 docker save，再用「导入镜像」。",
        },
        "compose": {
            "native": False, "subset": True,
            "title": "Compose 仅子集",
            "detail": "内置解析器支持 image / command / environment / volumes / "
                      "ports / restart / depends_on / .env，不是原生 Compose。",
        },
        "ports": {"min": 1024, "max": 65535,
                  "detail": "非 root 无法绑定 1024 以下端口。"},
        "exec_modes": _pick_modes(englib_modes),
    }


# ---------------- 服务托管 ----------------

def _service_argv(u):
    s = settings()
    return [sys.executable, UDOCKERD,
            "--host", "127.0.0.1", "--port", str(s["port"]),
            "--data", RUNTIME_DIR, "--config", SETTINGS_FILE,
            "--udocker", u["path"]]


def ensure_service():
    """把 udockerd 注册到 ServiceManager（幂等）。返回 (spec, udocker_info)。"""
    u = udocker_info(force=True)
    if not u["found"]:
        raise ValueError("未找到 udocker：请先安装（面板「运行时 → 一键安装」或手动放到 ~/udocker）")
    argv = _service_argv(u)
    cur = svc.spec(SERVICE)
    if not cur or cur.get("argv") != argv or cur.get("adapter") == "":
        spec = svc.register(SERVICE, argv, cwd=APP_DIR, autostart=True,
                            title=SERVICE_TITLE)
    else:
        spec = cur
    return spec, u


def daemon_status():
    st = svc.status(SERVICE)
    c = client()
    reachable = c.ping(timeout=1.2)
    ver = {}
    if reachable:
        try:
            ver = c.version()
        except dockerclient.EngineError:
            ver = {}
    st["reachable"] = reachable
    st["version"] = ver.get("Version", "")
    st["api"] = ver.get("ApiVersion", "")
    st["registered"] = bool(svc.spec(SERVICE))
    return st


def daemon_action(act):
    spec, _u = ensure_service()
    svc.action(SERVICE, act)
    if act in ("start", "restart"):
        deadline = time.time() + 8
        while time.time() < deadline:
            if client().ping(timeout=1.0):
                break
            time.sleep(0.3)
    return daemon_status()


def daemon_logs(lines=200):
    try:
        return svc.tail(SERVICE, lines)
    except FileNotFoundError:
        return {"name": SERVICE, "lines": [], "size": 0, "truncated": False}


# ---------------- 状态 ----------------

def status():
    u = udocker_info()
    d = daemon_status()
    caps = capabilities(u.get("englib_modes"))
    counts = {"containers": 0, "running": 0, "images": 0}
    if d["reachable"]:
        try:
            cs = client().containers(all_=True)
            ims = client().images()
            counts = {"containers": len(cs),
                      "running": sum(1 for c in cs if c.get("State") == "running"),
                      "images": len(ims)}
        except (dockerclient.EngineError, OSError):
            pass
    return {
        "udocker": u, "daemon": d, "settings": settings(), "capabilities": caps,
        "counts": counts, "runtimedir": RUNTIME_DIR, "home": core.HOME,
        "client": "builtin-engine-api",
        "arch": _arch(),
    }


# ---------------- 容器 ----------------

def _norm_mounts(ins):
    out = []
    for m in ins.get("Mounts") or []:
        out.append({"host": m.get("Source", ""), "container": m.get("Destination", ""),
                    "ro": m.get("Mode") == "ro"})
    return out


def _norm_container(item, ins):
    pp = ins.get("PocketPanel") or {}
    state = ins.get("State") or {}
    cfg = ins.get("Config") or {}
    hc = ins.get("HostConfig") or {}
    nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
    return {
        "id": item.get("Id") or ins.get("Id"),
        "name": (item.get("Names") or ["/" + ins.get("Name", "").lstrip("/")])[0].lstrip("/"),
        "image": item.get("Image") or cfg.get("Image", ""),
        "image_id": item.get("ImageID", ""),
        "state": "running" if state.get("Running") else (
            "created" if state.get("Status") == "created" else "exited"),
        "status": item.get("Status", ""),
        "created": item.get("Created", 0),
        "command": " ".join(cfg.get("Cmd") or []),
        "mounts": _norm_mounts(ins),
        "ports": pp.get("Ports") or [],
        "restart": (hc.get("RestartPolicy") or {}).get("Name", "no"),
        "exit_code": state.get("ExitCode", 0),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "restarts": ins.get("RestartCount", 0),
        "execmode": pp.get("ExecMode", "P1"),
        "network": "host",
        "ip": (ins.get("NetworkSettings") or {}).get("IPAddress", ""),
        # 应用商店靠 pp.app 标签认出「这个容器是哪个应用装的」
        "labels": cfg.get("Labels") or {},
    }


def list_containers(force=False):
    now = time.time()
    with _lock:
        if not force and now - _cache["at_c"] < 3 and _cache["containers"]:
            return _cache["containers"]
    c = client()
    out = []
    for item in c.containers(all_=True)[:80]:
        try:
            ins = c.container_inspect(item["Id"])
        except dockerclient.EngineError:
            continue
        out.append(_norm_container(item, ins))
    with _lock:
        _cache.update(at_c=now, containers=out)
    return out


def container_detail(cid):
    ins = client().container_inspect(cid)
    item = {"Id": ins.get("Id"), "Names": ["/" + ins.get("Name", "").lstrip("/")],
            "Image": (ins.get("Config") or {}).get("Image", ""),
            "ImageID": ins.get("Image", ""), "Created": 0,
            "Status": (ins.get("State") or {}).get("Status", "")}
    view = _norm_container(item, ins)
    env = []
    for kv in (ins.get("Config") or {}).get("Env") or []:
        k, _, v = str(kv).partition("=")
        masked = bool(SECRET_RE.search(k))
        env.append({"key": k, "value": "••••••••" if masked else v, "masked": masked})
    view["env"] = env
    view["workdir"] = (ins.get("Config") or {}).get("WorkingDir", "")
    view["user"] = (ins.get("Config") or {}).get("User", "")
    view["binds_raw"] = (ins.get("HostConfig") or {}).get("Binds") or []
    view["ro_enforced"] = bool((ins.get("PocketPanel") or {}).get("BindMountROEnforced"))
    view["warnings"] = []
    for m in view["mounts"]:
        if m["ro"] and not view["ro_enforced"]:
            view["warnings"].append(
                f"PRoot 无法强制只读：{m['host']} → {m['container']} 实际可写")
    return view


def _clean_env(raw):
    out = []
    items = raw if isinstance(raw, list) else []
    if len(items) > 64:
        raise ValueError("环境变量最多 64 条")
    for it in items:
        if isinstance(it, dict):
            k, v = str(it.get("key") or "").strip(), str(it.get("value") or "")
        else:
            k, _, v = str(it).partition("=")
            k = k.strip()
        if not k:
            continue
        if not ENV_KEY_RE.match(k):
            raise ValueError(f"环境变量名不合法：{k}")
        if "\x00" in v or len(v) > 4096:
            raise ValueError(f"环境变量 {k} 的值非法或过长")
        out.append(f"{k}={v}")
    return out


def _clean_mounts(raw, create=True):
    out = []
    items = raw if isinstance(raw, list) else []
    if len(items) > 16:
        raise ValueError("最多 16 个目录映射")
    for m in items:
        if not isinstance(m, dict):
            raise ValueError("目录映射格式错误")
        host = str(m.get("host") or "").strip()
        cont = str(m.get("container") or "").strip()
        ro = bool(m.get("ro"))
        if not host or not cont:
            raise ValueError("目录映射需要宿主机路径与容器路径")
        try:
            host = core.sandbox(host)
        except ValueError as e:
            raise ValueError(f"宿主机路径不允许：{e}")
        real = os.path.realpath(host)
        for p in PROTECTED_HOSTS:
            if real == p or real.startswith(p + os.sep):
                raise ValueError("不允许映射面板数据目录（含凭据与配置）")
        if real == os.path.realpath(core.HOME):
            raise ValueError("不允许把整个 HOME 映射进容器，请选择具体子目录")
        if not os.path.isabs(cont):
            raise ValueError("容器内路径必须是绝对路径")
        cont = os.path.normpath(cont)
        if cont in FORBIDDEN_CONTAINER_PATHS:
            raise ValueError(f"不允许覆盖容器关键路径：{cont}")
        if create is not False and not os.path.exists(host):
            try:
                os.makedirs(host, mode=0o755, exist_ok=True)
            except OSError as e:
                raise ValueError(f"无法创建宿主机目录：{e.strerror}")
        elif create is False and not os.path.exists(host):
            raise ValueError(f"宿主机目录不存在：{host}（执行时会自动创建）")
        out.append({"host": host, "container": cont, "ro": ro})
    return out


def _clean_ports(raw):
    out = []
    items = raw if isinstance(raw, list) else []
    for p in items:
        if not isinstance(p, dict):
            raise ValueError("端口格式错误")
        try:
            host = int(p.get("host"))
            cont = int(p.get("container"))
        except (TypeError, ValueError):
            raise ValueError("端口必须是数字")
        proto = str(p.get("proto") or "tcp").lower()
        if proto not in ("tcp", "udp"):
            raise ValueError("端口协议只支持 tcp / udp")
        if not (1 <= cont <= 65535):
            raise ValueError("容器端口需在 1–65535")
        if not (1 <= host <= 65535):
            raise ValueError("宿主机端口需在 1–65535")
        if host < 1024:
            raise ValueError(f"宿主机端口 {host} 不可用：非 root 无法监听 1024 以下端口")
        if any(x["host"] == host and x["proto"] == proto for x in out):
            raise ValueError(f"宿主机端口 {host}/{proto} 重复")
        out.append({"host": host, "container": cont, "proto": proto})
    return out


def _clean_cmd(raw):
    if isinstance(raw, list):
        cmd = [str(x) for x in raw if str(x) != ""]
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            cmd = shlex.split(text)
        except ValueError as e:
            raise ValueError(f"命令解析失败：{e}")
    if len(cmd) > 32:
        raise ValueError("命令参数过多")
    for c in cmd:
        if "\x00" in c or len(c) > 2048:
            raise ValueError("命令参数非法或过长")
    return cmd


def _port_free(port):
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def create_container(spec):
    spec = spec or {}
    name = str(spec.get("name") or "").strip()
    if name and not NAME_RE.match(name):
        raise ValueError("容器名只能包含字母、数字、_ . -，且不能以符号开头")
    image = str(spec.get("image") or "").strip()
    if not image:
        raise ValueError("请填写镜像")
    cmd = _clean_cmd(spec.get("cmd"))
    env = _clean_env(spec.get("env"))
    mounts = _clean_mounts(spec.get("mounts"))
    ports = _clean_ports(spec.get("ports"))
    restart = str(spec.get("restart") or DEFAULT_RESTART)
    if restart not in RESTARTS:
        raise ValueError("重启策略只支持 no / always / on-failure")
    execmode = str(spec.get("execmode") or settings()["execmode"])
    if execmode not in EXEC_MODES:
        raise ValueError("执行模式只支持 P1 / P2 / F1")
    # englib 缺哪个引擎就拦哪个：Android 上只有 proot，F1 选了也起不来
    avail = _pick_modes(udocker_info().get("englib_modes"))
    if execmode not in avail:
        raise ValueError(
            f"本机 englib 未提供 {execmode} 所需引擎"
            f"（可用：{' / '.join(avail) or '无'}）")
    workdir = str(spec.get("workdir") or "").strip()
    if workdir and not workdir.startswith("/"):
        raise ValueError("工作目录必须是容器内绝对路径")
    user = str(spec.get("user") or "").strip()
    if user and not USER_RE.match(user):
        raise ValueError("运行用户格式不合法")
    # 入口覆盖：None = 用镜像自带；"" 或 [] = 清空；单个路径 = 换成它。
    # udocker 的 --entrypoint 只认一个可执行文件，带不了参数，参数仍然走 Cmd。
    ep = None
    if spec.get("entrypoint") is not None:
        raw = spec.get("entrypoint")
        ep = ([str(raw)] if isinstance(raw, str) else
              [str(x) for x in (raw or [])])
        ep = [x for x in ep if x != ""]
        if len(ep) > 1:
            raise ValueError("入口程序只能填一个路径（参数请写在启动命令里）")
    if not name:
        name = "pp-" + hashlib.sha256(os.urandom(8)).hexdigest()[:8]

    c = client()
    try:
        c.image_inspect(image)
    except dockerclient.EngineError as e:
        if e.status == 404:
            raise ValueError(f"镜像 {image} 未拉取，请先在「镜像」页拉取")
        raise
    warnings = []
    for p in ports:
        if not _port_free(p["host"]):
            warnings.append(f"宿主机端口 {p['host']} 已在监听，容器可能启动失败"
                            "（host 网络没有端口映射）")
    body = {
        "Image": image, "Cmd": cmd, "Env": env, "WorkingDir": workdir,
        "User": user,
        "Labels": {"pp.managed": "1", **(spec.get("labels") or {})},
        "HostConfig": {
            "Binds": [f"{m['host']}:{m['container']}" for m in mounts],
            "RestartPolicy": {"Name": restart, "MaximumRetryCount": 0},
            "NetworkMode": "host",
        },
        "PocketPanel": {"Ports": ports, "ExecMode": execmode},
    }
    if ep is not None:
        body["Entrypoint"] = ep
    try:
        res = c.create_container(name, body)
    except dockerclient.EngineError as e:
        if e.status == 409:
            raise ValueError(f"容器名 {name} 已存在")
        raise ValueError(e.message)
    cid = res.get("Id", "")
    with _lock:
        _cache["at_c"] = _cache["at_i"] = 0
    if spec.get("start", True):
        try:
            c.start(cid)
        except dockerclient.EngineError as e:
            raise ValueError(f"容器已创建但启动失败：{e.message}")
    return {"id": cid, "name": name, "warnings": warnings,
            "started": bool(spec.get("start", True))}


def container_action(cid, act):
    c = client()
    if act == "start":
        c.start(cid)
    elif act == "stop":
        c.stop(cid)
    elif act == "restart":
        c.restart(cid)
    elif act == "remove":
        c.remove_container(cid, force=True)
    else:
        raise ValueError(f"未知操作 {act}")
    with _lock:
        _cache["at_c"] = _cache["at_i"] = 0
    return True


def container_logs(cid, tail=None):
    tail = int(tail or settings()["log_tail"])
    text = client().logs(cid, tail=max(20, min(tail, 5000)))
    rows = text.rstrip("\n").split("\n") if text else []
    return {"lines": rows[-tail:], "count": len(rows[-tail:]), "tail": tail}


def exec_session(cid, shell="sh", cols=80, rows=24):
    shell = (shell or "sh").strip()
    if not re.match(r"^[a-zA-Z0-9_./-]{1,32}$", shell):
        raise ValueError("shell 不合法")
    c = client()
    ins = c.container_inspect(cid)
    if not (ins.get("State") or {}).get("Running"):
        raise ValueError("容器未运行，无法进入终端")
    name = ins.get("Name", "").lstrip("/") or cid[:8]
    eid = c.exec_create(cid, [shell], env=["TERM=xterm-256color"], tty=True)
    sock = c.exec_hijack(eid, tty=True)
    sess = term.create_stream(f"{name} · {shell}", f"容器 {name}", sock,
                              resize_fn=lambda cc, rr: c.exec_resize(eid, cc, rr),
                              close_fn=sock.close)
    sess.resize(cols, rows)
    return sess.info()


# ---------------- 镜像 ----------------

def list_images(force=False):
    now = time.time()
    with _lock:
        if not force and now - _cache["at_i"] < 3 and _cache["images"]:
            return _cache["images"]
    raw = client().images()
    out = []
    for im in raw:
        tags = im.get("RepoTags") or []
        out.append({
            "id": (im.get("Id") or "").replace("sha256:", ""),
            "tags": tags,
            "name": tags[0] if tags else "<none>",
            "size": im.get("Size", 0),
            "created": im.get("Created", 0),
            "arch": im.get("Architecture", ""),
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    with _lock:
        _cache.update(at_i=now, images=out)
    return out


def pull_task(t, ref, platform=None):
    ref = str(ref or "").strip()
    if not ref or len(ref) > 200 or any(ch.isspace() for ch in ref):
        raise ValueError("镜像名不合法")
    plat = platform or settings()["platform"]
    core.task_log(t, f"拉取 {ref}（{plat}）")
    n = [0]

    def progress(obj):
        n[0] += 1
        msg = obj.get("status") or obj.get("stream") or ""
        if msg:
            core.task_log(t, msg.rstrip())
        if n[0] % 3 == 0:
            core.task_progress(t, min(90, 10 + n[0]))

    client().pull(ref, platform=plat, on_progress=progress)
    core.task_progress(t, 100)
    core.task_log(t, f"完成：{ref}")
    with _lock:
        _cache["at_c"] = _cache["at_i"] = 0
    return ref


def remove_image(ref):
    if not ref or len(ref) > 200:
        raise ValueError("镜像名不合法")
    try:
        client().remove_image(ref)
    except dockerclient.EngineError as e:
        if e.status == 409:
            raise ValueError("镜像仍被容器使用，请先删除容器")
        raise ValueError(e.message)
    with _lock:
        _cache["at_c"] = _cache["at_i"] = 0
    return True


def import_task(t, path):
    try:
        path = core.sandbox(path)
    except ValueError as e:
        raise ValueError(str(e))
    if not os.path.isfile(path):
        raise ValueError("导入文件不存在")
    if os.path.getsize(path) > 2 * 1024 ** 3:
        raise ValueError("镜像文件超过 2GB，请先清理")
    core.task_log(t, f"导入 {path}（{core.human_size(os.path.getsize(path))}）")

    def progress(obj):
        line = obj.get("stream") or obj.get("status") or ""
        if line:
            core.task_log(t, line.rstrip())

    client().load_image(path, on_progress=progress)
    core.task_progress(t, 100)
    with _lock:
        _cache["at_c"] = _cache["at_i"] = 0
    return path


def import_dir():
    os.makedirs(IMPORT_DIR, mode=0o700, exist_ok=True)
    return IMPORT_DIR


# ---------------- 事件 / 告警 ----------------

def _dbg(msg):
    """PP_DOCKER_DEBUG=1 时把事件循环的每一步打到 stderr，排障用。"""
    if os.environ.get("PP_DOCKER_DEBUG"):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] [docker-events] {msg}\n")
        sys.stderr.flush()


def ensure_monitor():
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    _dbg("监控线程启动")
    threading.Thread(target=_monitor, daemon=True, name="docker-events").start()


def _monitor():
    global _last_event
    while True:
        try:
            c = client()
            if not c.ping(timeout=1.5):
                _dbg("ping 失败，5s 后重试")
                time.sleep(5)
                continue
            # 事件只在 udockerd 进程内存里留 300 条，守护进程重启、面板重启、
            # 长连接断开都会留下一段空窗。连上先按状态对一次账，别让空窗里
            # 崩掉的容器没人听见。
            _catch_up(c)
            # 首次订阅还没有水位，只看新事件；之后按水位续上（服务端 since 含
            # 等号，同一秒的事件不会掉）。
            since = int(_last_event or time.time())
            _dbg(f"订阅事件 since={since}")
            for ev in c.events(since=since, timeout=EVENTS_TIMEOUT):
                _dbg(f"事件 {ev.get('Action')} {ev.get('time')}")
                ts = int(ev.get("time") or time.time())
                if ts > _last_event:
                    _last_event = ts
                _handle_event(c, ev)
        except (OSError, dockerclient.EngineError) as e:
            _dbg(f"事件流断开：{e!r}，4s 后重连")
            time.sleep(4)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(5)


def _handle_event(c, ev):
    action = ev.get("Action") or ev.get("status") or ""
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name") or (ev.get("id") or "")[:8]
    cid = (ev.get("Actor") or {}).get("ID") or ev.get("id") or ""
    if action != "die":
        return
    # 用户自己点的停止（SIGTERM → 143）不算异常，不然每次停容器都报一次警
    if attrs.get("manual") == "true":
        return
    try:
        code = int(attrs.get("exitCode") or 0)
    except ValueError:
        code = 0
    _alert_die(c, cid, name, code)


def _alert_die(c, cid, name, code, restarts=None):
    """异常退出 + 反复重启，两条告警各节流各的。

    节流不能串在一起：容器每几秒崩一次时，第一条「异常退出」会一直占着
    5 分钟冷却，后面的 die 全被挡在门外，「频繁重启」就永远发不出来了。
    """
    if code == 0:
        return
    now = time.time()
    if now - _alert_at.get(cid, 0) >= 300:
        _alert_at[cid] = now
        messages.add("err", f"容器 {name} 异常退出（{code}）",
                     f"退出码 {code}；查看日志：容器详情 → 日志")
    if restarts is None:
        try:
            restarts = int(c.container_inspect(cid).get("RestartCount") or 0)
        except (OSError, dockerclient.EngineError, TypeError, ValueError):
            restarts = 0
    if int(restarts or 0) >= 3 and now - _alert_at.get(cid + ":flap", 0) > 1800:
        _alert_at[cid + ":flap"] = now
        messages.add("warn", f"容器 {name} 频繁重启（{restarts} 次）",
                     "已按重启策略自动拉起；若持续失败请检查命令与目录映射")


def _iso_ts(text):
    """解析 Engine API 的 `2026-10-30T10:07:54Z`（UTC），解析不了返回 0。"""
    try:
        return int(calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0


def _catch_up(c):
    """连上后按容器实际状态对一次账，补事件流空窗期漏掉的异常退出。"""
    try:
        rows = c.containers(all_=True)
    except (OSError, dockerclient.EngineError) as e:
        _dbg(f"对账跳过：{e!r}")
        return
    now = time.time()
    for row in rows or []:
        if str(row.get("State") or "").lower() != "exited":
            continue
        cid = row.get("Id") or ""
        try:
            ins = c.container_inspect(cid)
        except (OSError, dockerclient.EngineError):
            continue
        state = ins.get("State") or {}
        if (ins.get("PocketPanel") or {}).get("ManualStop"):
            continue
        # 只补最近 5 分钟：更早的崩溃在消息中心里已经过期，重启面板再报一遍
        # 只会变成噪音。
        if now - _iso_ts(state.get("FinishedAt")) > CATCH_UP_WINDOW:
            continue
        try:
            code = int(state.get("ExitCode") or 0)
        except (TypeError, ValueError):
            code = 0
        _dbg(f"对账：{row.get('Names')} 退出码 {code}")
        _alert_die(c, cid, (ins.get("Name") or "").lstrip("/") or cid[:8], code,
                   restarts=ins.get("RestartCount"))


# ---------------- 导入上传目录 ----------------


# ---------------- Compose（P2，子集） ----------------

def _compose_base(base_dir, project="compose"):
    raw = (base_dir or "").strip() or os.path.join(core.HOME, "pocketpanel", "compose", project)
    try:
        base = core.sandbox(raw)
    except ValueError as e:
        raise ValueError(f"工作目录不允许：{e}")
    return os.path.realpath(base)


def compose_preview(text, env_text="", project="", base_dir=""):
    if not (text or "").strip():
        raise ValueError("请粘贴 docker-compose.yml 内容")
    if len(text) > 512 * 1024:
        raise ValueError("compose 文件过大（>512KB）")
    base = _compose_base(base_dir, project or "compose")
    plan = compose.plan(text, env_text, project, base)
    checked = []
    for sname in plan["order"]:
        svc = plan["services"][sname]
        try:
            # 预览不建目录：宿主机目录等到 up 的时候再创建
            mounts = _clean_mounts(svc["mounts"], create=None)
            ports = _clean_ports(svc["ports"])
        except ValueError as e:
            raise ValueError(f"{sname}: {e}")
        checked.append({
            "service": sname, "name": svc["name"], "image": svc["image"],
            "cmd": " ".join(svc["cmd"]), "env": svc["env"],
            "mounts": mounts, "ports": ports,
            "restart": svc["restart"], "depends_on": svc["depends_on"],
        })
    return {"project": plan["project"], "base_dir": base, "order": plan["order"],
            "services": checked, "warnings": plan["warnings"]}


def compose_up_task(t, text, env_text="", project="", base_dir=""):
    plan = compose_preview(text, env_text, project, base_dir)
    core.task_log(t, f"项目 {plan['project']}：{len(plan['order'])} 个服务，"
                     f"工作目录 {plan['base_dir']}")
    created = []
    total = max(1, len(plan["order"]))
    for idx, sname in enumerate(plan["order"]):
        svc = next(x for x in plan["services"] if x["service"] == sname)
        core.task_log(t, f"[{idx + 1}/{total}] 创建 {svc['name']}（{svc['image']}）")
        try:
            res = create_container({
                "name": svc["name"], "image": svc["image"],
                "cmd": svc["cmd"], "env": svc["env"],
                "mounts": svc["mounts"], "ports": svc["ports"],
                "restart": svc["restart"], "start": True,
                "workdir": "", "user": "",
            })
        except ValueError as e:
            raise ValueError(f"服务 {sname} 创建失败：{e}；"
                             f"已创建 {len(created)} 个（保留以便排查）")
        created.append({"service": sname, "id": res["id"], "name": res["name"],
                        "warnings": res.get("warnings") or []})
        core.task_progress(t, int((idx + 1) / total * 100))
    core.task_log(t, f"完成：{len(created)} 个容器已启动")
    return {"project": plan["project"], "created": created}
