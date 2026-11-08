"""M15 内网穿透：Client 层 P0 落地。

零依赖。每个隧道实例一份配置文件（TOML / JSON / INI，真相源），
structured 是解析后的归一化视图；同一时刻只有一个可编辑入口，
切换会明确告知注释丢失。进程用 Popen 托管并做指数退避保活，
公网拨测的告警与恢复走消息中心。

数据布局（core.DATA/tunnels/）：
  instances.json   实例元数据（名称、模式、自启、拨测地址）
  {id}.conf        配置原文（0600，token 明文只在这里）
  {id}.log         客户端进程输出（脱敏后读出）

与设计文档的偏差：YAML 暂不支持——标准库没有 YAML 解析器，
为保住「零第三方依赖」的底线，导入 YAML 时提示先转成 TOML/JSON。
"""
import configparser
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import traceback
import urllib.request

import core
import messages

DIR = os.path.join(core.DATA, "tunnels")
PREFIX = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
SVDIR = os.path.join(PREFIX, "var", "service")

MASK = "********"


def panel_port():
    """面板自身端口：config 优先，PP_PORT 环境变量覆盖（与 app.main 一致）。"""
    return int(os.environ.get("PP_PORT") or core.cfg_get("port", default=8888))

PROXY_TYPES = ("tcp", "udp", "http", "https", "stcp", "sudp", "tcpmux")
PROXY_CORE = ("name", "type", "local_ip", "local_port", "remote_port",
              "subdomain", "custom_domains")
COMMON_KEYS = ("server_addr", "server_port", "token", "user",
               "serverAddr", "serverPort", "auth_token", "user")
SECRET_KEYS = re.compile(r"(token|secret|password|passwd|sk|api[_-]?key)", re.I)
PORT_RANGE = (1, 65535)
# FreeFrp 的 TCP/UDP 远端端口范围（15.8）
FREEFRP_RANGE = (10001, 50000)

PRESETS = {
    "freefrp": {
        "kind": "preset",
        "label": "FreeFrp（公益）",
        "badge": "公益",
        "note": "无需申请，token 为公开固定值；TCP/UDP 远端端口 10001–50000；HTTP 需自有域名 CNAME 到 frp.freefrp.net。公益资源无可用性承诺。",
        "server": {"serverAddr": "frp.freefrp.net", "serverPort": 7000,
                   "auth_token": "freefrp.net"},
        "port_range": FREEFRP_RANGE,
    },
}

DEFAULT_AUTOSTART = True

_lock = threading.RLock()
_instances = {}      # id -> 元数据 dict
_procs = {}          # id -> {"proc", "started_at", "restarts", "last_exit", "fail_cnt"}
_probe = {}          # id -> {"ok", "last_ts", "fails", "alerted", "detail"}
_loaded = False
_monitor_started = False


# ---------------- 存储 ----------------

def _file_meta():
    return os.path.join(DIR, "instances.json")


def _file_conf(tid):
    return os.path.join(DIR, f"{tid}.conf")


def _file_run_conf(tid):
    """运行时配置：用户原文 + 面板注入的 webServer 段。

    与用户配置分开存放，用户在面板里看到的配置永远是他自己写的那份。
    """
    return os.path.join(DIR, f"{tid}.run.conf")


def _file_log(tid):
    return os.path.join(DIR, f"{tid}.log")


def init():
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _loaded = True
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        try:
            with open(_file_meta(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for m in data if isinstance(data, list) else []:
                _instances[m["id"]] = m
        except Exception:  # noqa: BLE001
            _instances.clear()


def _save_meta():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    tmp = _file_meta() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(list(_instances.values()), fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _file_meta())


# ---------------- 配置解析 / 生成 ----------------

def _toml_to_text(cfg):
    """手写 TOML 序列化：server 平铺 + [[proxies]] 数组。"""

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k} = {val(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    lines = []
    for k in ("serverAddr", "serverPort", "user"):
        if k in cfg["server"] and cfg["server"][k] not in ("", None):
            lines.append(f"{k} = {val(cfg['server'][k])}")
    if cfg["server"].get("auth_token"):
        lines.append("")
        lines.append("[auth]")
        lines.append(f"token = {val(cfg['server']['auth_token'])}")
    for p in cfg["proxies"]:
        lines.append("")
        lines.append("[[proxies]]")
        extras = {k: v for k, v in p.items()
                  if k not in PROXY_CORE and v not in ("", None, [])}
        for k, v in extras.items():
            lines.append(f"{k} = {val(v)}")
        for k in PROXY_CORE:
            if k in p and p[k] not in ("", None, []):
                lines.append(f"{k} = {val(p[k])}")
    return "\n".join(lines).lstrip("\n") + "\n"


def _ini_to_text(cfg):
    cp = configparser.RawConfigParser()
    sec = {}
    m = {"serverAddr": "server_addr", "serverPort": "server_port",
         "auth_token": "token", "user": "user"}
    for k, v in cfg["server"].items():
        if k in m and v not in ("", None):
            sec[m[k]] = str(v)
    if sec:
        cp.add_section("common")
        for k, v in sec.items():
            cp.set("common", k, str(v))
    for p in cfg["proxies"]:
        name = str(p.get("name") or "proxy")
        cp.add_section(name)
        for k, v in p.items():
            if k == "name" or v in ("", None, []):
                continue
            key = {"local_ip": "local_ip", "local_port": "local_port",
                   "remote_port": "remote_port", "subdomain": "subdomain"}.get(k, k)
            cp.set(name, key, ", ".join(v) if isinstance(v, list) else str(v))
    out = []
    for s in cp.sections():
        out.append(f"[{s}]")
        for k, v in cp.items(s):
            out.append(f"{k} = {v}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render(cfg, fmt):
    """structured → 配置文本。fmt: toml | json | ini"""
    cfg = _normalize_struct(cfg)
    if fmt == "json":
        return json.dumps({"server": cfg["server"], "proxies": cfg["proxies"]},
                          ensure_ascii=False, indent=2) + "\n"
    if fmt == "ini":
        return _ini_to_text(cfg)
    if fmt == "toml":
        return _toml_to_text(cfg)
    raise ValueError(f"不支持的格式: {fmt}")


def _parse_toml(text):
    d = tomllib.loads(text)
    return _from_native(d)


def _parse_json(text):
    d = json.loads(text)
    return _from_native(d)


def _from_native(d):
    """TOML/JSON 原生 dict → structured。兼容 frpc 的嵌套 auth 段。"""
    srv = d.get("server") if isinstance(d.get("server"), dict) else d
    auth = d.get("auth") if isinstance(d.get("auth"), dict) else {}
    try:
        port = int(srv.get("serverPort") or srv.get("server_port") or 0)
    except (TypeError, ValueError):
        port = 0
    server = {
        "serverAddr": srv.get("serverAddr") or srv.get("server_addr") or "",
        "serverPort": port,
        "auth_token": srv.get("auth_token") or srv.get("token") or auth.get("token") or "",
        "user": srv.get("user") or "",
    }
    proxies = []
    for p in d.get("proxies") or []:
        if not isinstance(p, dict):
            continue
        q = dict(p)
        q["custom_domains"] = q.get("custom_domains") or []
        proxies.append(q)
    return {"server": server, "proxies": proxies}


def _extract_webserver(text, fmt):
    """从配置原文里取出 webServer 段（端口 + 鉴权）。

    frpc 的本地状态端点 /api/status 就挂在这里，网络守卫靠它判断
    「frpc 自己重连上了没有」。配置里没写就返回 None，由调用方动态分配。

    注意 frpc 0.71 只认 webServer.user / webServer.password，
    不认 webServer.auth.token（实测 json: unknown field "auth"）。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        if fmt == "ini":
            cp = configparser.RawConfigParser(strict=False)
            cp.read_string(text)
            if not cp.has_section("webServer"):
                return None
            g = lambda k: cp.get("webServer", k, fallback="")  # noqa: E731
        else:
            try:
                d = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                d = json.loads(text)
            if not isinstance(d, dict):
                return None
            ws = d.get("webServer")
            if not isinstance(ws, dict):
                return None
            g = lambda k: ws.get(k) or ""  # noqa: E731
    except (configparser.Error, ValueError, tomllib.TOMLDecodeError):
        return None
    try:
        port = int(g("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not port:
        return None
    return {"port": port, "addr": str(g("addr") or "127.0.0.1"),
            "user": str(g("user") or ""), "password": str(g("password") or "")}


def _alloc_webserver_port(tid):
    """给实例分配一个 webServer 端口（配置里没写时用）。

    按实例 id 派生，重启后端口不变 —— 探测端点不能因为重启就变。
    """
    h = 0
    for ch in str(tid):
        h = (h * 31 + ord(ch)) % 100000
    base, span = 17400, 200
    for i in range(span):
        port = base + ((h + i) % span)
        if _port_free(port):
            return port
    return base


def _port_free(port):
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _runtime_conf(tid, text, fmt, port):
    """生成 frpc 实际启动用的配置文件（运行时配置）。

    用户配置原文是「真相源」，一个字都不改。面板只在启动时另存一份
    运行时配置，把 webServer 段补进去 —— 这样 /api/status 探测端点
    始终可用，而用户看到的配置里不会多出面板塞进去的东西。

    只对 TOML 注入：frpc 0.71 的 JSON 配置结构与面板的 JSON 结构
    本来就不互通，INI 是 legacy 格式，两种都不动。
    """
    if fmt != "toml":
        return text, False
    if not text.endswith("\n"):
        text += "\n"
    text += (f"\n[webServer]\n"
             f'addr = "127.0.0.1"\n'
             f"port = {port}\n")
    return text, True



def _parse_ini(text):
    cp = configparser.RawConfigParser(strict=False)
    cp.read_string(text)
    if not cp.has_section("common"):
        raise configparser.Error("缺少 [common] 段")
    server = {
        "serverAddr": cp.get("common", "server_addr", fallback=""),
        "serverPort": int(cp.get("common", "server_port", fallback="0") or 0),
        "auth_token": cp.get("common", "token", fallback=""),
        "user": cp.get("common", "user", fallback=""),
    }
    proxies = []
    for s in cp.sections():
        if s == "common":
            continue
        p = {"name": s, "custom_domains": []}
        for k, v in cp.items(s):
            v = v.strip()
            if k in ("custom_domains",):
                p[k] = [x.strip() for x in v.split(",") if x.strip()]
            elif k in ("local_port", "remote_port"):
                p[k] = int(v) if v.isdigit() else v
            else:
                p[k] = v
        proxies.append(p)
    return {"server": server, "proxies": proxies}


def sniff(text):
    """格式自动探测：TOML → JSON → INI。返回 (fmt, structured, err)。"""
    text = (text or "").strip()
    if not text:
        return None, None, "配置内容为空"
    try:
        return "toml", _parse_toml(text), None
    except tomllib.TOMLDecodeError:
        pass
    try:
        return "json", _parse_json(text), None
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return "ini", _parse_ini(text), None
    except (configparser.Error, ValueError) as e:
        return None, None, f"无法识别的配置格式：{e}"


def parse(text, fmt=None):
    """按指定格式解析（语法层）。抛 ValueError 带定位信息。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("配置内容为空")
    if fmt in (None, "", "auto"):
        f, s, e = sniff(text)
        if f is None:
            raise ValueError(e)
        return f, s
    try:
        if fmt == "toml":
            return fmt, _parse_toml(text)
        if fmt == "json":
            return fmt, _parse_json(text)
        if fmt == "ini":
            return fmt, _parse_ini(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"TOML 语法错误：{e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 语法错误：第 {e.lineno} 行第 {e.colno} 列 {e.msg}") from e
    except configparser.Error as e:
        raise ValueError(f"INI 解析错误：{e}") from e
    raise ValueError(f"不支持的格式: {fmt}")


def _normalize_struct(cfg):
    server = dict(cfg.get("server") or {})
    server["serverAddr"] = str(server.get("serverAddr") or "").strip()
    try:
        server["serverPort"] = int(server.get("serverPort") or 0)
    except (TypeError, ValueError):
        server["serverPort"] = 0
    server["auth_token"] = str(server.get("auth_token") or "")
    server["user"] = str(server.get("user") or "")
    proxies = []
    for p in cfg.get("proxies") or []:
        q = dict(p)
        q["name"] = str(q.get("name") or "").strip()
        q["type"] = str(q.get("type") or "tcp").strip().lower()
        try:
            for k in ("local_port", "remote_port"):
                if q.get(k) not in ("", None):
                    q[k] = int(q[k])
        except (TypeError, ValueError):
            pass
        doms = q.get("custom_domains") or []
        if isinstance(doms, str):
            doms = [x.strip() for x in doms.split(",") if x.strip()]
        q["custom_domains"] = doms
        proxies.append(q)
    return {"server": server, "proxies": proxies}


# ---------------- Schema 校验（第二层） ----------------

def validate(cfg):
    """返回错误列表 [{field, msg}]。空列表 = 通过。"""
    cfg = _normalize_struct(cfg)
    errs = []
    s = cfg["server"]
    if not s["serverAddr"]:
        errs.append({"field": "server.serverAddr", "msg": "服务器地址必填"})
    if not (1 <= s["serverPort"] <= 65535):
        errs.append({"field": "server.serverPort", "msg": "服务器端口需在 1–65535"})
    if not cfg["proxies"]:
        errs.append({"field": "proxies", "msg": "至少需要一个代理（proxy）"})
    seen_remote = {}
    seen_local = {}
    for i, p in enumerate(cfg["proxies"]):
        tag = f"proxies[{i}]"
        name = p.get("name")
        if not name:
            errs.append({"field": f"{tag}.name", "msg": "代理名称必填"})
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            errs.append({"field": f"{tag}.name", "msg": "名称只允许字母数字与 . _ -"})
        t = p.get("type")
        if t not in PROXY_TYPES:
            errs.append({"field": f"{tag}.type", "msg": f"类型必须是 {'/'.join(PROXY_TYPES)}"})
            continue
        lp = p.get("local_port")
        if lp in ("", None):
            errs.append({"field": f"{tag}.local_port", "msg": "本地端口必填"})
        elif not (isinstance(lp, int) and 1 <= lp <= 65535):
            errs.append({"field": f"{tag}.local_port", "msg": "本地端口需在 1–65535"})
        elif lp == panel_port():
            errs.append({"field": f"{tag}.local_port",
                         "msg": f"本地端口 {lp} 是面板自身端口，禁止通过隧道暴露"})
        if isinstance(lp, int) and 1 <= lp <= 65535:
            if lp in seen_local:
                errs.append({"field": f"{tag}.local_port",
                             "msg": f"本地端口 {lp} 与代理 {seen_local[lp]} 重复"})
            else:
                seen_local[lp] = name or f"#{i}"
        if t in ("tcp", "udp", "stcp", "sudp"):
            rp = p.get("remote_port")
            if rp in ("", None):
                errs.append({"field": f"{tag}.remote_port", "msg": f"{t} 类型需要远程端口"})
            elif not (isinstance(rp, int) and 1 <= rp <= 65535):
                errs.append({"field": f"{tag}.remote_port", "msg": "远程端口需在 1–65535"})
            elif rp == panel_port():
                errs.append({"field": f"{tag}.remote_port",
                             "msg": f"远程端口 {rp} 与面板端口相同，禁止暴露面板"})
            elif isinstance(rp, int):
                if rp in seen_remote:
                    errs.append({"field": f"{tag}.remote_port",
                                 "msg": f"远程端口 {rp} 与代理 {seen_remote[rp]} 冲突"})
                else:
                    seen_remote[rp] = name or f"#{i}"
        elif t in ("http", "https"):
            if not (p.get("custom_domains") or p.get("subdomain")):
                errs.append({"field": f"{tag}.custom_domains",
                             "msg": "http/https 需要 custom_domains 或 subdomain"})
    return errs


# ---------------- 客户端检测 / 权威校验 ----------------

_client_cache = {"ts": 0.0, "info": None}


def _find_frpc():
    """在 PATH、HOME、HOME/frpc* 里找 frpc 可执行文件。"""
    cands = [shutil.which("frpc")]
    home = os.path.expanduser("~")
    try:
        for name in sorted(os.listdir(home)):
            p = os.path.join(home, name)
            if name in ("frpc", "frpc.exe") and os.path.isfile(p):
                cands.append(p)
            elif name.startswith("frpc") and os.path.isdir(p):
                q = os.path.join(p, "frpc")
                if os.path.isfile(q):
                    cands.append(q)
    except OSError:
        pass
    for p in cands:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def client_info(force=False):
    """frpc 二进制信息（缓存 60s）；version 决定默认配置格式。"""
    now = time.time()
    if not force and _client_cache["info"] and now - _client_cache["ts"] < 60:
        return _client_cache["info"]
    path = _find_frpc()
    info = {"kind": "frpc", "found": False, "path": "", "version": "", "fmt": "toml"}
    if path:
        info.update({"found": True, "path": path})
        try:
            r = subprocess.run([path, "-v"], capture_output=True, text=True,
                               timeout=8)
            v = (r.stdout or r.stderr).strip().splitlines()
            if v:
                info["version"] = v[0].strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            major = int(info["version"].split(".")[0] or 0)
            minor = int((info["version"].split(".") + ["0"])[1] or 0)
            info["fmt"] = "toml" if (major, minor) >= (0, 52) else "ini"
        except (ValueError, IndexError):
            info["fmt"] = "toml"
    _client_cache.update({"ts": now, "info": info})
    return info


def verify_with_frpc(text):
    """第三层校验：frpc verify -c <tmp>。返回 {ok, output, skipped}。"""
    cli = client_info()
    if not cli["found"]:
        return {"ok": True, "skipped": True,
                "output": "未安装 frpc，已跳过权威校验（语法与 Schema 校验已通过）"}
    fd, tmp = secrets.token_hex(4), ""
    tmp = os.path.join(DIR, f".verify-{fd}.conf")
    try:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        try:
            r = subprocess.run([cli["path"], "verify", "-c", tmp],
                               capture_output=True, text=True, timeout=15)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return {"ok": r.returncode == 0, "skipped": False, "output": out or f"exit {r.returncode}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "skipped": False, "output": "frpc verify 超时（15s）"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------- 脱敏 ----------------

def redact_obj(cfg):
    """structured 里的 token 打码。"""
    cfg = json.loads(json.dumps(cfg, ensure_ascii=False))  # deep copy
    s = cfg.get("server") or {}
    for k in list(s):
        if SECRET_KEYS.search(k) and s[k]:
            s[k] = MASK
    return cfg


def mask_text(text):
    """对配置文本按行打码 token=value。"""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^(\s*[A-Za-z0-9_.-]*\s*[=:]\s*)(.+?)\s*$", line)
        if m and SECRET_KEYS.search(m.group(1)) and m.group(2) not in ("", MASK):
            out.append(m.group(1) + MASK)
        else:
            out.append(line)
    return "\n".join(out)


# 命令行参数脱敏：-t <token> / --webui-pass <pwd> / --token <tk> …
_CMD_SECRET = re.compile(
    r"((?:--?[A-Za-z][A-Za-z0-9_-]*(?:pass|passwd|password|token|secret|key))"
    r"(?:[=-]|\s+))(\S+)", re.I)
_CMD_SHORT_T = re.compile(r"(-t\s+)(\S{6,})")


def mask_cmdline(cmd):
    """进程命令行脱敏（flag 形式），配置文件的 key=value 走 mask_text。"""
    cmd = _CMD_SECRET.sub(lambda m: m.group(1) + MASK, cmd or "")
    cmd = _CMD_SHORT_T.sub(lambda m: m.group(1) + MASK, cmd)
    return cmd


def unmask_text(text, old_text):
    """用户提交的 raw 里打码字段替换回旧值，避免把 **** 存进真相源。"""
    if MASK not in (text or ""):
        return text
    old_vals = {}
    for line in (old_text or "").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.+?)\s*$", line)
        if m and SECRET_KEYS.search(m.group(1)) and m.group(2) != MASK:
            old_vals[m.group(1).lower()] = m.group(2)
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*([A-Za-z0-9_.-]+)\s*[=:]\s*)(.+?)\s*$", line)
        if m and m.group(3) == MASK and m.group(2).lower() in old_vals:
            out.append(m.group(1) + old_vals[m.group(2).lower()])
        else:
            out.append(line)
    return "\n".join(out)


# ---------------- 实例管理 ----------------

def _new_id():
    return "tp" + secrets.token_hex(4)


def _summary(s):
    """实例列表用的摘要：服务器 + 每个代理一行。"""
    srv = s.get("server") or {}
    proxies = []
    for p in s.get("proxies") or []:
        port = p.get("remote_port") or (p.get("custom_domains") or [""])[0] or p.get("subdomain") or ""
        proxies.append({"name": p.get("name") or "", "type": p.get("type") or "tcp",
                        "port": port})
    return {"addr": srv.get("serverAddr") or "", "port": srv.get("serverPort") or 0,
            "proxies": proxies}


def _persist_conf(tid, text):
    with open(_file_conf(tid), "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(_file_conf(tid), 0o600)


def list_instances():
    init()
    with _lock:
        items = []
        for tid, m in _instances.items():
            items.append({**_status_view(tid), **m})
        items.sort(key=lambda x: x.get("created", 0))
        return items


def _status_view(tid):
    with _lock:
        pr = _procs.get(tid)
        if pr and pr["proc"].poll() is None:
            state = "running"
        elif pr and pr.get("fail_cnt", 0) >= 10:
            state = "failed"
        else:
            state = "stopped"
        view = {"status": state, "restarts": (pr or {}).get("restarts", 0),
                "started_at": (pr or {}).get("started_at", 0),
                "last_exit": (pr or {}).get("last_exit", "")}
        pv = _probe.get(tid) or {}
        view["probe"] = {"ok": pv.get("ok"), "detail": pv.get("detail", ""),
                         "ts": pv.get("last_ts", 0)}
        m = _instances.get(tid) or {}
        # 网络守卫用：客户端种类 + 可探测端点（/api/status）
        view["client_kind"] = "frpc"
        view["web_port"] = m.get("web_port") or 0
        view["probe_endpoint"] = m.get("probe_endpoint")
        return view


def get_conf_text(tid):
    init()
    with _lock:
        if tid not in _instances:
            raise KeyError(tid)
        try:
            with open(_file_conf(tid), "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""


def instance_detail(tid):
    """详情：元数据 + 打码 raw + structured + 日志尾。"""
    init()
    with _lock:
        if tid not in _instances:
            raise KeyError(tid)
        m = dict(_instances[tid])
        raw = get_conf_text(tid)
        masked = mask_text(raw)
        if raw:
            fmt, struct, err = sniff(raw)
        else:
            fmt, struct, err = None, None, None
        d = {**m, **_status_view(tid),
             "raw_masked": masked, "format": fmt,
             "structured": redact_obj(struct) if struct else None,
             "parse_error": err}
        try:
            with open(_file_log(tid), "rb") as fh:
                d["log"] = mask_text(fh.read().decode("utf-8", "replace"))[-4000:]
        except OSError:
            d["log"] = ""
        return d


def create(name, text=None, struct=None, fmt=None, mode="semi",
           autostart=DEFAULT_AUTOSTART, probe_url=""):
    """新建实例。text（raw）与 struct（表单）二选一；mode: preset|semi|local"""
    init()
    try:
        core.valid_name(name)
    except ValueError as e:
        raise ValueError(f"实例名称非法：{e}") from e
    if text is not None:
        text = unmask_text(text, "")
        fmt, s = parse(text, fmt)
    else:
        if struct is None:
            raise ValueError("需要 text 或 struct 之一")
        s = _normalize_struct(struct)
        errs = validate(s)
        if errs:
            raise ValueError("；".join(e["msg"] for e in errs[:3]))
        text = render(s, fmt or "toml")
        fmt = fmt or "toml"
    errs = validate(s)
    if errs:
        raise ValueError("；".join(e["msg"] for e in errs[:3]))
    auth = verify_with_frpc(text)
    if not auth["ok"]:
        raise ValueError("frpc verify 未通过：" + (auth["output"] or "未知错误")[:300])
    tid = _new_id()
    with _lock:
        _instances[tid] = {"id": tid, "name": name, "mode": mode,
                           "format": fmt, "created": int(time.time()),
                           "autostart": bool(autostart), "probe_url": probe_url or "",
                           **_summary(s)}
        _save_meta()
        _persist_conf(tid, text)
    if autostart:
        try:
            start(tid)
        except Exception:  # noqa: BLE001
            pass
    return tid


def update(tid, text=None, struct=None, fmt=None, probe_url=None,
           autostart=None, name=None):
    init()
    with _lock:
        if tid not in _instances:
            raise KeyError(tid)
        m = _instances[tid]
        if text is not None:
            old = get_conf_text(tid)
            text = unmask_text(text, old)
            fmt, s = parse(text, fmt or m.get("format") or "auto")
            errs = validate(s)
            if errs:
                raise ValueError("；".join(e["msg"] for e in errs[:3]))
            auth = verify_with_frpc(text)
            if not auth["ok"]:
                raise ValueError("frpc verify 未通过：" + (auth["output"] or "未知错误")[:300])
            running = _status_view(tid)["status"] == "running"
            _persist_conf(tid, text)
            m["format"] = fmt
            m.update(_summary(s))
            if running:
                stop(tid)
                start(tid)
        elif struct is not None:
            s = _normalize_struct(struct)
            errs = validate(s)
            if errs:
                raise ValueError("；".join(e["msg"] for e in errs[:3]))
            cur_fmt = m.get("format") or client_info()["fmt"] or "toml"
            text = render(s, cur_fmt)
            auth = verify_with_frpc(text)
            if not auth["ok"]:
                raise ValueError("frpc verify 未通过：" + (auth["output"] or "未知错误")[:300])
            running = _status_view(tid)["status"] == "running"
            _persist_conf(tid, text)
            m["format"] = cur_fmt
            m.update(_summary(s))
            if running:
                stop(tid)
                start(tid)
        if probe_url is not None:
            m["probe_url"] = probe_url.strip()
        if autostart is not None:
            m["autostart"] = bool(autostart)
        if name is not None and name.strip():
            m["name"] = name.strip()
        _save_meta()
    return True


def delete(tid):
    init()
    with _lock:
        if tid not in _instances:
            raise KeyError(tid)
        try:
            stop(tid)
        except Exception:  # noqa: BLE001
            pass
        _instances.pop(tid, None)
        _procs.pop(tid, None)
        _probe.pop(tid, None)
        _save_meta()
        for f in (_file_conf(tid), _file_log(tid), _file_run_conf(tid)):
            try:
                os.unlink(f)
            except OSError:
                pass
    return True


def start(tid):
    init()
    cli = client_info()
    if not cli["found"]:
        raise ValueError("未找到 frpc 可执行文件；请先安装（pkg 或 GitHub Release）")
    with _lock:
        if tid not in _instances:
            raise KeyError(tid)
        cur = _procs.get(tid)
        if cur and cur["proc"].poll() is None:
            return True
        if cur:
            try:
                if cur.get("lf"):
                    cur["lf"].close()
            except Exception:  # noqa: BLE001
                pass
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        # 运行时配置：用户原文 + 注入的 webServer 段（供 /api/status 探测）。
        # 用户配置原文保持不动，运行时配置单独一份，探测端口写回实例元数据。
        raw = get_conf_text(tid)
        fmt = _instances[tid].get("format") or "toml"
        ws = _extract_webserver(raw, fmt)
        if ws:
            wport = ws["port"]
            wuser, wpass = ws["user"], ws["password"]
        else:
            wport = _instances[tid].get("web_port") or _alloc_webserver_port(tid)
            wuser = wpass = ""
        run_text, injected = _runtime_conf(tid, raw, fmt, wport)
        run_path = _file_conf(tid)
        if injected:
            run_path = _file_run_conf(tid)
            with open(run_path, "w", encoding="utf-8") as fh:
                fh.write(run_text)
            os.chmod(run_path, 0o600)
        lf = open(_file_log(tid), "ab")  # noqa: SIM115 进程生命周期内持有
        pr = subprocess.Popen(
            [cli["path"], "-c", run_path],
            stdout=lf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=DIR,
        )
        _procs[tid] = {"proc": pr, "started_at": int(time.time()),
                       "restarts": (cur or {}).get("restarts", 0),
                       "last_exit": "",
                       "fail_cnt": (cur or {}).get("fail_cnt", 0),
                       "next_retry": 0, "lf": lf}
        # 记录探测端点：网络守卫据此判断 frpc 是否自己重连成功
        _instances[tid]["web_port"] = wport
        _instances[tid]["probe_endpoint"] = {
            "port": wport, "user": wuser, "password": wpass,
            "proxies": [p.get("name") for p in
                        ((_instances[tid].get("proxies") or []) or []) if p.get("name")],
        }
        _save_meta()
    return True


def stop(tid):
    init()
    with _lock:
        pr = _procs.pop(tid, None)
        if not pr:
            return True
        proc = pr["proc"]
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        try:
            pr["lf"].close()
        except Exception:  # noqa: BLE001
            pass
        # 主动停止的实例不参与自动重启
        _procs[tid] = {"proc": proc, "started_at": pr.get("started_at", 0),
                       "restarts": pr.get("restarts", 0),
                       "last_exit": proc.poll(), "fail_cnt": 0,
                       "next_retry": 0, "lf": None, "manual": True}
    return True


def restart(tid):
    stop(tid)
    return start(tid)


# ---------------- 外部客户端探测（mefrp 等只读展示） ----------------

_EXT_RE = re.compile(r"(mefrp|frp[cs]?|cloudflared|tailscale|ngrok|zerotier)", re.I)


def external_clients():
    """扫描 runit 服务里与穿透相关的进程，只读展示；不接管、不停启。"""
    out = []
    if not os.path.isdir(SVDIR):
        return out
    for name in sorted(os.listdir(SVDIR)):
        if not _EXT_RE.search(name):
            continue
        cmd = ""
        pid = _sv_pid(name)
        if pid:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
            except OSError:
                cmd = ""
        webui = ""
        m = re.search(r"--webui-addr\s+(\S+)", cmd)
        mp = re.search(r"--webui-port\s+(\d+)", cmd)
        if m and mp:
            host = m.group(1)
            host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            webui = f"http://{host}:{mp.group(1)}"
        out.append({
            "name": name,
            "kind": "runit",
            "running": bool(pid),
            "pid": pid,
            "cmd": mask_cmdline(cmd),
            "webui": webui,
        })
    return out


def _sv_pid(name):
    """runit 服务的 supervise/pid。"""
    try:
        with open(os.path.join(SVDIR, name, "supervise", "pid"), "r") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


# ---------------- 拨测 ----------------

def _probe_once(url):
    """tcp://host:port → socket；http(s):// → GET（任何响应都算通）。"""
    url = (url or "").strip()
    if not url:
        return True, "未配置"
    if url.startswith("tcp://"):
        host, _, port = url[6:].partition(":")
        try:
            s = socket.create_connection((host, int(port)), timeout=5)
            s.close()
            return True, "TCP 可连"
        except OSError as e:
            return False, f"TCP 不通: {e}"
    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PocketPanel/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return True, f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001
            return False, f"HTTP 不可达: {e}"
    return False, "拨测地址需以 tcp:// 或 http(s):// 开头"


def probe_now(tid):
    init()
    m = _instances.get(tid) or {}
    ok, detail = _probe_once(m.get("probe_url", ""))
    with _lock:
        pv = _probe.setdefault(tid, {"fails": 0, "alerted": False})
        pv.update({"ok": ok, "detail": detail, "last_ts": int(time.time())})
    return ok, detail


# ---------------- 保活 + 拨测循环 ----------------

def ensure_monitor():
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, daemon=True,
                     name="tunnel-monitor").start()


MAX_FAIL = 10
_last_probe = 0.0


def _monitor_loop():
    init()
    # 面板启动自愈：autostart 的实例拉起来
    for tid, m in list(_instances.items()):
        if m.get("autostart"):
            try:
                start(tid)
            except Exception:  # noqa: BLE001
                pass
    while True:
        try:
            _monitor_tick()
        except Exception:  # noqa: BLE001
            # 保活/拨测是常驻线程，任何一轮的意外异常都不能杀死它
            try:
                traceback.print_exc()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(5)


def _monitor_tick():
    global _last_probe
    now = time.time()
    due = []
    with _lock:
        for tid, pr in list(_procs.items()):
            proc = pr.get("proc")
            if proc is None or proc.poll() is None:
                continue  # 运行中
            if pr.get("manual"):
                continue  # 用户主动停止，不自动重启
            if pr.get("next_retry"):
                continue  # 已安排退避重启，未到期不再重复计失败
            pr["last_exit"] = proc.poll()
            try:
                pr["lf"].close()
            except Exception:  # noqa: BLE001
                pass
            pr["lf"] = None
            fail = pr.get("fail_cnt", 0) + 1
            pr["fail_cnt"] = fail
            if fail >= MAX_FAIL:
                m = _instances.get(tid) or {}
                messages.add("err", f"隧道 {m.get('name', tid)} 反复退出已停止",
                             f"连续失败 {fail} 次，停止自动重启；请检查配置与服务商状态")
                _procs.pop(tid, None)
            else:
                pr["next_retry"] = now + min(60, 2 ** (fail - 1))
        due = [tid for tid, pr in _procs.items()
               if pr.get("proc") is not None and pr["proc"].poll() is not None
               and pr.get("next_retry", 0) and pr["next_retry"] <= now]
    for tid in due:
        m = _instances.get(tid)
        if not m:
            continue
        try:
            start(tid)  # start 继承 fail_cnt
        except Exception:  # noqa: BLE001
            # start 本身失败（如二进制被删）：与进程退出同一套退避，到达上限即停
            with _lock:
                pr = _procs.get(tid)
                fail = (pr.get("fail_cnt", 1) + 1) if pr else 1
                if pr:
                    pr["fail_cnt"] = fail
                    if fail >= MAX_FAIL:
                        messages.add("err", f"隧道 {m.get('name')} 反复启动失败已停止",
                                     f"连续 {fail} 次启动失败，停止自动重启；请检查客户端是否被移动或删除")
                        _procs.pop(tid, None)
                    else:
                        pr["next_retry"] = time.time() + min(60, 2 ** (fail - 1))
            continue
        with _lock:
            pr = _procs.get(tid) or {}
            pr["restarts"] = pr.get("restarts", 0) + 1
            fail = pr.get("fail_cnt", 0)
            pr["next_retry"] = 0
        messages.add("info", f"隧道 {m.get('name', tid)} 自动重启",
                     f"进程意外退出后自动重启（累计第 {pr.get('restarts', 0)} 次，连续失败 {fail} 次）")
    # 拨测：60s 一轮
    if now - _last_probe >= 60:
        _last_probe = now
        with _lock:
            targets = [(tid, m.get("probe_url", "")) for tid, m in _instances.items()
                       if m.get("probe_url")]
        for tid, url in targets:
            ok, detail = _probe_once(url)
            with _lock:
                m = _instances.get(tid) or {}
                pv = _probe.setdefault(tid, {"fails": 0, "alerted": False})
                prev_alerted = pv.get("alerted", False)
                if ok:
                    pv.update({"ok": True, "detail": detail,
                               "last_ts": int(time.time()), "fails": 0})
                    if prev_alerted:
                        pv["alerted"] = False
                        messages.add("ok", f"隧道 {m.get('name', tid)} 已恢复",
                                     f"{url} 拨测恢复：{detail}")
                else:
                    fails = pv.get("fails", 0) + 1
                    pv.update({"ok": False, "detail": detail,
                               "last_ts": int(time.time()), "fails": fails})
                    if fails >= 3 and not prev_alerted:
                        pv["alerted"] = True
                        messages.add("err", f"隧道 {m.get('name', tid)} 公网不可达",
                                     f"{url} 连续 {fails} 次拨测失败：{detail}。若为公益节点，可能是服务端繁忙。")
