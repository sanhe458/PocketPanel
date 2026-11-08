"""HTTP 层：路由、认证、静态资源、文件流。

路由表是显式的 (method, 正则, 处理函数, 是否需要登录)，
不引入 Web 框架，避免在设备上装依赖。
"""
import base64
import json
import mimetypes
import os
import re
import signal
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
import alerts
import appstore
import cloudflare
import dockerclient
import events
import files
import gateway
import messages
import metrics
import netguard
import sched
import share
import svc
import tasktypes
import runtime
import term
import tunnel
import tunnel_install
import udocker_install
import ws

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class FileOut:
    def __init__(self, path, filename=None, download=False):
        self.path = path
        self.filename = filename or os.path.basename(path)
        self.download = download


class Err(Exception):
    def __init__(self, code, msg, field_errors=None):
        self.code = code
        self.msg = msg
        self.field_errors = field_errors or {}


class Req:
    def __init__(self, handler):
        self.h = handler
        u = urllib.parse.urlparse(handler.path)
        self.path = u.path
        self.query = urllib.parse.parse_qs(u.query)
        self.method = handler.command
        self.headers = handler.headers
        self._body = None
        self._consumed = False

    def q(self, name, default=""):
        v = self.query.get(name)
        return v[0] if v else default

    def json(self):
        if self._body is None:
            raw = self.raw()
            self._body = json.loads(raw or b"{}")
        return self._body

    def raw(self):
        """读走请求体（原始字节）。只读一次，重复调用返回空。"""
        if self._consumed:
            return b""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        self._consumed = True
        if n <= 0:
            return b""
        try:
            return self.h.rfile.read(n)
        except OSError:
            return b""

    def drain(self):
        """把没读完的请求体读干净。

        keep-alive 连接上，如果 handler 没读 body（比如只需要一个动作、
        不关心参数的 POST），残留字节会被下一个请求当成请求行解析，
        表现为 `Unsupported method ('{}GET')`。dispatch 在 finally 里
        统一调一次，保证任何 POST 端点都不会给下一个请求留垃圾。

        上限 8MB：超大的请求体不该被这里吞掉，那属于该 handler 的事。

        不碰 self._body —— handler 之后若再调 json()，仍应按自己的
        逻辑解析（body 非法的 POST 该报 400，而不是被静默吞成 {}）。
        """
        if self._consumed:
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if n <= 0:
            self._consumed = True
            return
        if n > 8 * 1024 * 1024:
            return
        self._consumed = True
        try:
            self.h.rfile.read(n)
        except OSError:
            pass


    def cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""


def jresp(obj, code=200, headers=None, cookies=None):
    return {"json": obj, "code": code, "headers": headers or {}, "cookies": cookies or []}


def cookie_header(name, value, max_age=86400, clear=False):
    if clear:
        return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"


# ---------------- 认证 ----------------

def need_auth(req):
    """会话校验。

    cookie 为主；额外接受 ?token= 查询参数 —— 浏览器的 WebSocket API
    不能自定义请求头，只靠 cookie 在部分环境（代理、跨站策略）会失效，
    事件通道就收不到推送了。两种方式等价，都是会话 token。
    """
    tok = req.cookie("pp_token") or req.q("token")
    return core.check_session(tok)


def login(req):
    body = req.json()
    user = core.cfg_get("username", default=core.DEFAULT_USER)
    salt = core.cfg_get("salt", default="")
    h = core.cfg_get("hash", default="")
    user_ok = body.get("username") == user
    pwd_ok = core.hash_pwd(salt, body.get("password", "")) == h
    if not (user_ok and pwd_ok):
        time.sleep(0.3)
        raise Err(401, "用户名或密码错误")
    tok = core.new_session(user)
    return jresp({"token": tok, "username": user},
                 cookies=[cookie_header("pp_token", tok)])


def logout(req):
    core.drop_session(req.cookie("pp_token"))
    return jresp({"ok": "1"}, cookies=[cookie_header("pp_token", "", clear=True)])


def session(req):
    if not need_auth(req):
        return jresp({"authed": False, "version": core.APP_VERSION})
    # 回传会话 token：刷新页面后前端要靠它给事件通道的 WebSocket 鉴权
    # （浏览器的 WS API 不能自定义请求头）
    return jresp({"authed": True,
                  "username": core.cfg_get("username", default=core.DEFAULT_USER),
                  "created_at": core.cfg_get("created_at", default=0),
                  "token": req.cookie("pp_token") or req.q("token") or "",
                  "version": core.APP_VERSION})


def change_account(req):
    body = req.json()
    old = body.get("old_password", "")
    user = core.cfg_get("username", default=core.DEFAULT_USER)
    salt = core.cfg_get("salt", default="")
    if core.hash_pwd(salt, old) != core.cfg_get("hash", default=""):
        time.sleep(0.3)
        raise Err(403, "当前密码错误")
    new_user = (body.get("username") or "").strip() or user
    if not (2 <= len(new_user) <= 32):
        raise Err(400, "用户名需为 2–32 个字符")
    if any(c in new_user for c in " :/\\"):
        raise Err(400, "用户名不能包含空格、冒号或斜杠")
    pwd = body.get("password") or ""
    if pwd and len(pwd) < 6:
        raise Err(400, "新密码至少 6 位")
    ns, nh = salt, core.cfg_get("hash", default="")
    if pwd:
        ns = os.urandom(8).hex()
        nh = core.hash_pwd(ns, pwd)
    core.cfg_update(username=new_user, salt=ns, hash=nh, init_pwd="")
    core.drop_all_sessions()
    tok = core.new_session(new_user)
    return jresp({"ok": "1", "username": new_user, "token": tok},
                 cookies=[cookie_header("pp_token", tok)])


# ---------------- 指标 ----------------

def overview(req):
    return jresp(metrics.overview())


def services(req):
    return jresp(metrics.list_services())


def service_action(req, name, action):
    return jresp({"ok": metrics.service_action(name, action)})


def processes(req):
    return jresp(metrics.list_processes())


def kill_proc(req, pid, act):
    metrics.kill_process(int(pid), hard=(act == "kill9"))
    return jresp({"ok": "1"})


def network(req):
    return jresp(metrics.network())


def logs(req):
    return jresp(metrics.list_logs())


def log_tail(req, name):
    lines = int(req.q("lines", "200"))
    lines = max(1, min(2000, lines))
    return jresp(metrics.tail_log(name, lines))


def disk(req):
    return jresp(metrics.disk_usage_dirs())


# ---------------- 文件 ----------------

def file_list(req):
    return jresp(files.list_dir(req.q("path")))


def file_dispatch(req):
    if req.method in ("PUT", "POST"):
        body = req.json()
        return jresp(files.write_text(body.get("path", ""), body.get("content", "")))
    if req.method == "DELETE":
        if req.q("permanent") == "1":
            return jresp(files.delete_permanent(req.q("path")))
        return jresp(files.delete_path(req.q("path")))
    path = req.q("path")
    if req.q("download") == "1" or req.q("raw") == "1":
        p, name, size, mt = files.open_file(path)
        return jresp(None, headers={}, cookies=[]) | {"file": FileOut(p, name, req.q("download") == "1")}
    return jresp(files.read_text(path))


def file_mkdir(req):
    b = req.json()
    return jresp(files.mkdir(b.get("path", ""), b.get("name", "")))


def file_touch(req):
    b = req.json()
    return jresp(files.touch(b.get("path", ""), b.get("name", "")))


def file_rename(req):
    b = req.json()
    return jresp(files.rename(b.get("path", ""), b.get("name", "")))


def file_copy(req):
    b = req.json()
    return jresp(files.copy_move(b.get("paths") or [], b.get("dest", ""), bool(b.get("move"))))


def file_chmod(req):
    b = req.json()
    return jresp(files.chmod(b.get("path", ""), b.get("mode", ""), bool(b.get("recursive"))))


def file_stat(req):
    return jresp(files.stat(req.q("path")))


def trash_list(req):
    return jresp(files.trash_list())


def trash_restore(req):
    b = req.json()
    ids = b.get("ids") or ([b["id"]] if b.get("id") else [])
    if not ids:
        raise Err(400, "未指定条目")
    return jresp(files.trash_restore(ids))


def trash_purge(req):
    b = req.json()
    ids = b.get("ids") or ([b["id"]] if b.get("id") else [])
    return jresp(files.trash_purge(ids, bool(b.get("all"))))


def versions(req):
    return jresp(files.versions_of(req.q("path")))


def version_read(req):
    return jresp(files.version_read(req.q("id")))


def version_restore(req):
    return jresp(files.version_restore(req.json().get("id", "")))


def version_delete(req):
    return jresp(files.version_delete(req.json().get("id", "")))


def favs(req):
    return jresp(files.fav_list())


def fav_toggle(req):
    return jresp(files.fav_toggle(req.json().get("path", "")))


def search(req):
    b = req.json()
    return jresp(files.start_search(b.get("path", ""), b.get("q", ""),
                                    b.get("suffix", ""), bool(b.get("sub")),
                                    int(b.get("limit") or 200), int(b.get("timeout_ms") or 8000),
                                    bool(b.get("content", True))))


def archive(req):
    b = req.json()
    return jresp(files.start_archive(b.get("paths") or [], b.get("format", "zip"), b.get("dest", "")))


def extract(req):
    b = req.json()
    return jresp(files.start_extract(b.get("path", ""), b.get("dest", "")))


def download_remote(req):
    b = req.json()
    return jresp(files.remote_download(b.get("url", ""), b.get("dir", ""), b.get("name", "")))


def file_settings(req):
    if req.method == "POST":
        b = req.json()
        days = int(b.get("trash_keep_days", 7))
        keep = int(b.get("version_keep", 10))
        if days < 0 or days > 365:
            raise Err(400, "保留天数需在 0–365 之间")
        if keep < 1 or keep > 50:
            raise Err(400, "版本保留数需在 1–50 之间")
        s = {"trash_keep_days": days, "version_keep": keep}
        core.save_file_settings(s)
        return jresp(s)
    return jresp(core.file_settings())


# ---------------- 任务 ----------------

def exec_cmd(req):
    b = req.json()
    cmd = (b.get("cmd") or "").strip()
    if not cmd:
        raise Err(400, "命令为空")
    cwd = b.get("cwd") or core.HOME
    t = core.new_task("cmd", cmd)
    t["cmd"], t["cwd"] = cmd, cwd

    def run():
        try:
            core.run_cmd_task(t, cmd, cwd)
            core.task_finish(t)
        except Exception as e:  # noqa: BLE001
            core.task_finish(t, e)

    threading.Thread(target=run, daemon=True).start()
    return jresp(core.task_view(t))


def task_get(req, tid):
    with core.TASK_LOCK:
        t = core.TASKS.get(tid)
    if not t:
        raise Err(404, "任务不存在")
    return jresp(core.task_view(t))


def tasks(req):
    return jresp(core.list_tasks())


# ---------------- 终端（PTY） ----------------

def _sess(sid):
    try:
        return term.get(sid)
    except KeyError as e:
        raise Err(404, str(e.args[0]) if e.args else "会话不存在")


def term_new(req):
    b = req.json()
    cols = b.get("cols") or 80
    rows = b.get("rows") or 24
    if b.get("kind") == "ssh":
        s = term.create_ssh(
            b.get("host", ""), b.get("port", 22), b.get("user", ""),
            auth=b.get("auth", "password"), password=b.get("password", ""),
            key_path=b.get("key", ""), cols=cols, rows=rows,
            name=b.get("name", ""), strict=bool(b.get("strict")),
            host_id=b.get("host_id"))
    else:
        s = term.create_local(b.get("shell", "bash"), cols, rows,
                              (b.get("cmd") or "").strip() or None)
    return jresp(s.info())


def term_list(req):
    return jresp({"sessions": term.listing(), "max": term.MAX_SESSIONS})


def term_output(req, sid):
    s = _sess(sid)
    try:
        seq = int(req.q("since", "0") or 0)
    except ValueError:
        seq = 0
    try:
        wait = float(req.q("wait", "0") or 0)
    except ValueError:
        wait = 0.0
    return jresp(s.read(seq, wait))


def term_input(req, sid):
    s = _sess(sid)
    raw = req.json().get("data") or ""
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        raise Err(400, "输入编码错误")
    if len(data) > 64 * 1024:
        raise Err(413, "单次输入过大")
    s.write(data)
    return jresp({"ok": True})


def term_resize(req, sid):
    b = req.json()
    _sess(sid).resize(b.get("cols") or 80, b.get("rows") or 24)
    return jresp({"ok": True})


def term_signal(req, sid):
    b = req.json()
    _sess(sid).signal(b.get("sig") or "int")
    return jresp({"ok": True})


def term_close(req, sid):
    return jresp({"ok": term.close(sid)})


# ---------------- SSH 主机簿 / 密钥 ----------------

def ssh_hosts(req):
    return jresp({"hosts": term.hosts(), "keys": term.list_keys()})


def ssh_host_save(req):
    return jresp(term.save_host(req.json()))


def ssh_host_delete(req):
    hid = req.json().get("id", "")
    return jresp({"ok": term.delete_host(hid)})


def ssh_key_import(req):
    b = req.json()
    return jresp(term.import_key(b.get("name", ""), b.get("content", "")))


def ssh_keys(req):
    return jresp({"keys": term.list_keys()})


# ---------------- 计划任务 ----------------

def _guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except gateway.GatewayError as e:
        raise Err(e.code, e.message)
    except dockerclient.EngineError as e:
        code = {400: 400, 404: 404, 409: 409, 501: 501}.get(e.status, 502)
        raise Err(code, f"容器运行时：{e.message}")
    except KeyError as e:
        raise Err(404, e.args[0] if e.args else "任务不存在")
    except ValueError as e:
        raise Err(400, str(e))
    except RuntimeError as e:
        raise Err(409, str(e))
    except tasktypes.ValidationError as e:
        raise Err(400, str(e), e.errors)


def schedule_list(req):
    return jresp(sched.listing())


def schedule_create(req):
    return jresp(_guarded(sched.create, req.json()))


def schedule_update(req, tid):
    return jresp(_guarded(sched.update, tid, req.json()))


def schedule_delete(req, tid):
    return jresp(_guarded(sched.delete, tid))


def schedule_toggle(req, tid):
    return jresp(_guarded(sched.toggle, tid))


def schedule_run(req, tid):
    return jresp(_guarded(sched.run_now, tid, "manual"))


def schedule_runs(req, tid):
    return jresp({"runs": _guarded(sched.task_runs, tid)})


def schedule_preview(req):
    """周期 + 类型动作 → 完整人话描述与下 3 次触发。"""
    try:
        b = req.json()
    except Exception:  # noqa: BLE001
        b = {}
    expr = (b.get("expr") or req.q("expr") or "").strip()
    ttype = (b.get("type") or "shell").strip().lower()
    params = b.get("params") if isinstance(b.get("params"), dict) else {}
    reboot = expr.lower() == "@reboot"
    err = ""
    if reboot:
        cron_desc, valid = "面板启动时", True
    else:
        _f, err = sched.parse(expr)
        cron_desc, valid = (sched.describe(expr), not err) if not err else (f"表达式无效：{err}", False)
    action = tasktypes.humanize(ttype, params)
    human = (cron_desc + (" " + action if action else "")).strip()
    nxt = []
    if valid and not reboot:
        ts = time.time()
        for _ in range(3):
            n = sched.next_run(expr, ts)
            if n is None:
                break
            nxt.append(int(n))
            ts = n + 60
    return jresp({"valid": valid, "err": err, "human": human, "next": nxt})


def task_types_list(req):
    return jresp({"types": tasktypes.registry_view()})


def task_type_dryrun(req, ttype):
    try:
        b = req.json()
        params = b.get("params") if isinstance(b.get("params"), dict) else b
        return jresp(tasktypes.dry_run(ttype, params or {}))
    except tasktypes.ValidationError as e:
        return jresp({"error": str(e), "field_errors": e.errors}, 400)


# ---------------- 内网穿透（M15） ----------------

def tunnel_clients(req):
    return jresp({"frpc": tunnel.client_info(),
                  "external": tunnel.external_clients(),
                  "panel_port": tunnel.panel_port()})


def tunnel_presets(req):
    return jresp({"presets": tunnel.PRESETS})


def tunnel_instances(req):
    return jresp({"instances": tunnel.list_instances(),
                  "panel_port": tunnel.panel_port()})


def tunnel_detail(req, tid):
    return jresp(_guarded(tunnel.instance_detail, tid))


def tunnel_create(req):
    b = req.json()
    tid = _guarded(
        tunnel.create,
        (b.get("name") or "").strip(),
        text=b.get("text"),
        struct=b.get("struct"),
        fmt=b.get("fmt"),
        mode=(b.get("mode") or "semi").strip(),
        autostart=b.get("autostart", True),
        probe_url=b.get("probe_url") or "",
    )
    return jresp({"id": tid}, 201)


def tunnel_update(req, tid):
    b = req.json()
    kw = {k: b[k] for k in ("text", "struct", "fmt", "probe_url", "autostart", "name")
          if k in b}
    _guarded(tunnel.update, tid, **kw)
    return jresp({"ok": True})


def tunnel_action(req, tid, act):
    if act == "delete":
        _guarded(tunnel.delete, tid)
        return jresp({"ok": True})
    fn = {"start": tunnel.start, "stop": tunnel.stop, "restart": tunnel.restart}.get(act)
    if fn is None:
        raise Err(404, f"未知操作 {act}")
    _guarded(fn, tid)
    return jresp({"ok": True})


def tunnel_probe(req, tid):
    ok, detail = tunnel.probe_now(tid)
    return jresp({"ok": ok, "detail": detail})


# ---------------- 网络守卫（M15 智能恢复） ----------------

def netguard_status(req):
    """网络守卫状态：当前网络、防抖计时、上次恢复结果、熔断情况。"""
    return jresp(netguard.status())


def netguard_settings(req):
    b = req.json()
    if isinstance(b, dict) and b:
        kw = {}
        for k in ("enabled", "debounce", "poll_interval", "circuit_max"):
            if k in b and b[k] is not None:
                kw[k] = b[k]
        netguard.save_settings(**kw)
    return jresp(netguard.settings())


def netguard_recover(req):
    """手动触发一次探测恢复。"""
    return jresp(netguard.trigger_recover())


def netguard_reset_circuit(req):
    """清空熔断计数（用户确认网络环境已恢复后手动解封）。"""
    netguard.reset_circuit()
    return jresp({"ok": True})


# ---------------- 监控告警（M09） ----------------

def alert_status(req):
    return jresp(alerts.status())


def alert_settings(req):
    """GET 读配置 / POST 存配置。授权码只回掩码，传空表示不改。"""
    if req.method == "GET":
        return jresp(alerts.status())
    b = req.json()
    out = alerts.save_settings(b if isinstance(b, dict) else {})
    return jresp(out)


def alert_smtp_save(req):
    b = req.json()
    return jresp({"ok": True, "smtp": alerts.save_smtp(b if isinstance(b, dict) else {})})


def alert_test(req):
    return jresp(alerts.test_send_async())


def alert_test_result(req):
    return jresp(alerts.test_result())


def alert_items(req):
    return jresp({"items": alerts.items_view()})


def alert_item_save(req):
    b = req.json()
    return jresp(alerts.upsert_item(b if isinstance(b, dict) else {}))


def alert_item_delete(req):
    b = req.json()
    return jresp(alerts.delete_item((b or {}).get("id") or ""))


def alert_item_toggle(req):
    b = req.json()
    return jresp(alerts.toggle_item((b or {}).get("id") or "",
                                    bool((b or {}).get("enabled"))))


def alert_events(req):
    try:
        limit = int(req.q("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    return jresp(alerts.events(limit))


def alert_events_clear(req):
    b = req.json()
    only_recovered = True
    if isinstance(b, dict) and b.get("all"):
        only_recovered = False
    return jresp(alerts.clear_events(only_recovered))


def alert_check(req):
    """立即跑一轮探测（不等下一个 60s tick）。"""
    return jresp(alerts.tick())


def tunnel_parse(req):
    """raw → 三层校验预览（不落盘）。"""
    b = req.json()
    text = b.get("text") or ""
    fmt = b.get("fmt") or "auto"
    try:
        f, s = tunnel.parse(text, fmt)
    except ValueError as e:
        return jresp({"ok": False, "error": str(e)}, 200)
    errs = tunnel.validate(s)
    auth = tunnel.verify_with_frpc(text)
    return jresp({"ok": not errs and auth["ok"], "format": f,
                  "structured": tunnel.redact_obj(s),
                  "errors": errs, "verify": auth})


def tunnel_render(req):
    """structured → 配置文本（按客户端版本选格式由前端用 clients 接口决定）。"""
    b = req.json()
    try:
        return jresp({"ok": True, "text": tunnel.render(b.get("struct") or {},
                                                        b.get("fmt") or "toml")})
    except (ValueError, TypeError) as e:
        return jresp({"ok": False, "error": str(e)}, 400)


def tunnel_install_status(req):
    return jresp(tunnel_install.status())


def tunnel_install_mirrors(req):
    return jresp({"mirrors": tunnel_install.MIRRORS})


def tunnel_install_speedtest(req):
    try:
        b = req.json()
    except Exception:  # noqa: BLE001
        b = {}
    urls = b.get("urls") if isinstance(b.get("urls"), list) else None
    return jresp({"results": tunnel_install.speedtest(urls)})


def tunnel_install_create(req):
    b = req.json()
    tid = _guarded(tunnel_install.start_install,
                   (b.get("source") or "").strip(),
                   version=(b.get("version") or "").strip(),
                   mirror_url=(b.get("mirror") or "").strip())
    return jresp({"id": tid}, 201)


def tunnel_uninstall(req):
    _guarded(tunnel_install.uninstall)
    return jresp({"ok": True})


# ---------------- Cloudflare Tunnel（M15 · 免费内网穿透） ----------------

def cf_status(req):
    return jresp({"client": cloudflare.client_info(),
                  "tunnels": cloudflare.list_tunnels(),
                  "settings": cloudflare.settings_view(),
                  "panel_port": cloudflare.panel_port()})


def cf_mirrors(req):
    return jresp({"mirrors": tunnel_install.MIRRORS})


def cf_probe_mirrors(req):
    return jresp({"results": _guarded(cloudflare.probe_mirrors)})


def cf_install(req):
    b = req.json()
    tid = _guarded(cloudflare.start_install, (b.get("mirror") or "").strip())
    return jresp({"id": tid}, 201)


def cf_uninstall(req):
    _guarded(cloudflare.uninstall)
    return jresp({"ok": True})


def cf_settings_get(req):
    return jresp(cloudflare.settings_view())


def cf_settings_save(req):
    b = req.json()
    return jresp(cloudflare.save_settings(
        email=b.get("email"), key=b.get("key"), token=b.get("token"),
        account_id=b.get("account_id")))


def cf_verify(req):
    """验凭证 + 列域名。不落盘，前端确认后再 save。"""
    try:
        b = req.json()
    except Exception:  # noqa: BLE001
        b = {}
    override = {k: b[k] for k in ("email", "key", "token", "account_id") if b.get(k)}
    return jresp(_guarded(cloudflare.verify, override or None))


def cf_zones(req):
    """重新拉一遍账号下的域名列表（下拉框数据源）。"""
    return jresp(_guarded(cloudflare.refresh_zones))


def cf_quick(req):
    b = req.json()
    tid = _guarded(cloudflare.create_quick, int(b.get("port") or cloudflare.panel_port()),
                   name=(b.get("name") or "").strip())
    return jresp({"id": tid, "tunnel": next(
        (x for x in cloudflare.list_tunnels() if x["id"] == tid), None)}, 201)


def cf_named(req):
    b = req.json()
    tid = _guarded(cloudflare.start_named, (b.get("host") or "").strip(),
                   int(b.get("port") or cloudflare.panel_port()),
                   (b.get("name") or "").strip())
    return jresp({"id": tid}, 201)


def cf_tunnel(req, tid):
    return jresp(_guarded(cloudflare.tunnel_detail, tid))


def cf_action(req, tid, act):
    if act == "delete":
        b = {}
        try:
            b = req.json()
        except Exception:  # noqa: BLE001
            pass
        errs = cloudflare.delete(tid, purge=bool(b.get("purge", True)))
        return jresp({"ok": True, "remote_errors": errs})
    fn = {"start": cloudflare.start, "stop": cloudflare.stop,
          "restart": cloudflare.restart}.get(act)
    if fn is None:
        raise Err(404, f"未知操作 {act}")
    _guarded(fn, tid)
    return jresp({"ok": True})


# ---------------- 容器运行时（M17） ----------------

def docker_status(req):
    return jresp(_guarded(runtime.status))


def docker_daemon(req, act):
    if act == "ensure":
        _guarded(runtime.ensure_service)
        return jresp({"registered": True, "daemon": runtime.daemon_status()})
    return jresp(_guarded(runtime.daemon_action, act))


def docker_daemon_logs(req):
    try:
        lines = int(req.q("lines", "200") or 200)
    except ValueError:
        lines = 200
    return jresp(_guarded(runtime.daemon_logs, lines))


def docker_install_status(req):
    return jresp(udocker_install.status())


def docker_install_meta(req):
    return jresp({"mirrors": udocker_install.MIRRORS,
                  "version": udocker_install.DEFAULT_VERSION,
                  "arch": udocker_install.arch(),
                  "englib": udocker_install.ENGLIB_NAME})


def docker_install_speedtest(req):
    return jresp({"results": udocker_install.speedtest()})


def docker_install(req):
    b = req.json()
    tid = udocker_install.start_install((b.get("mirror") or "").strip(),
                                        (b.get("version") or "").strip())
    return jresp({"id": tid}, 202)


def docker_uninstall(req):
    return jresp(_guarded(udocker_install.uninstall))


def docker_settings_get(req):
    return jresp({"settings": runtime.settings(),
                  "capabilities": runtime.capabilities(),
                  "arch": runtime._arch()})


def docker_settings_save(req):
    s = _guarded(runtime.save_settings, req.json())
    # 端口或 udocker 路径变了要重新注册 run 脚本；失败不影响保存
    try:
        runtime.ensure_service()
    except Exception:  # noqa: BLE001
        pass
    return jresp({"settings": s})


def docker_containers(req):
    return jresp({"containers": _guarded(runtime.list_containers,
                                         req.q("refresh") == "1")})


def docker_container_create(req):
    return jresp(_guarded(runtime.create_container, req.json()), 201)


def docker_container(req, cid):
    return jresp(_guarded(runtime.container_detail, cid))


def docker_container_action(req, cid, act):
    _guarded(runtime.container_action, cid, act)
    return jresp({"ok": True})


def docker_container_logs(req, cid):
    try:
        tail = int(req.q("tail")) if req.q("tail") else None
    except ValueError:
        tail = None
    return jresp(_guarded(runtime.container_logs, cid, tail))


def docker_container_exec(req, cid):
    b = req.json()
    return jresp(_guarded(runtime.exec_session, cid, b.get("shell") or "sh",
                          b.get("cols") or 80, b.get("rows") or 24))


def docker_images(req):
    return jresp({"images": _guarded(runtime.list_images, req.q("refresh") == "1")})


def docker_image_pull(req):
    b = req.json()
    ref = (b.get("ref") or "").strip()
    if not ref:
        raise Err(400, "请填写镜像名，如 alpine:3.19")
    t = core.run_job("docker-pull", f"拉取镜像 {ref}",
                     lambda task: runtime.pull_task(task, ref, b.get("platform") or None))
    return jresp({"id": t["id"]}, 202)


def docker_image_remove(req):
    return jresp({"ok": _guarded(runtime.remove_image, req.json().get("ref") or "")})


def docker_image_import(req):
    path = (req.json().get("path") or "").strip()
    if not path:
        raise Err(400, "请选择要导入的 tar 文件")
    t = core.run_job("docker-import", "导入容器镜像",
                     lambda task: runtime.import_task(task, path))
    return jresp({"id": t["id"]}, 202)


def docker_import_dir(req):
    return jresp({"dir": runtime.import_dir()})


def docker_compose_preview(req):
    b = req.json()
    return jresp(_guarded(runtime.compose_preview, b.get("text") or "",
                          b.get("env") or "", b.get("project") or "",
                          b.get("base_dir") or ""))


def docker_compose_up(req):
    b = req.json()
    # 先同步校验：错误直接回 400，不要让用户等一个必然失败的任务
    _guarded(runtime.compose_preview, b.get("text") or "", b.get("env") or "",
             b.get("project") or "", b.get("base_dir") or "")
    t = core.run_job("docker-compose", "Compose 一键启动",
                     lambda task: runtime.compose_up_task(
                         task, b.get("text") or "", b.get("env") or "",
                         b.get("project") or "", b.get("base_dir") or ""))
    return jresp({"id": t["id"]}, 202)


# ---------------- 应用商店 ----------------

def store_catalog(req):
    force = (req.q("force") or "") in ("1", "true", "yes")
    return jresp(_guarded(appstore.catalog, force))


def store_installed(req):
    return jresp({"items": _guarded(appstore.installed)})


def store_settings_get(req):
    return jresp({"settings": appstore.settings()})


def store_settings_save(req):
    return jresp({"settings": _guarded(appstore.save_settings, req.json())})


def store_preview(req):
    b = req.json()
    app = appstore.find(str(b.get("id") or ""))
    if not app:
        raise Err(404, "商店里没有这个应用")
    return jresp(_guarded(appstore.plan, app, b))


def store_install(req):
    b = req.json()
    app_id = str(b.get("id") or "").strip()
    if not app_id:
        raise Err(400, "缺少应用 id")
    app = appstore.find(app_id)
    if not app:
        raise Err(404, f"商店里没有应用 {app_id}")
    # 先把参数算一遍：目录越界、端口冲突这类问题当场报，不甩给异步任务
    _guarded(appstore.plan, app, b)
    t = core.run_job("app-install", f"安装 {app.get('name') or app_id}",
                     lambda task: appstore.install_task(task, app_id, b))
    return jresp({"id": t["id"]}, 202)


# ---------------- AI 网关 ----------------

def ai_overview(req):
    return jresp(gateway.admin_overview())


def ai_providers(req):
    return jresp({"items": gateway.list_providers()})


def ai_provider_save(req):
    return jresp(_guarded(gateway.save_provider, req.json()))


def ai_provider_delete(req, pid):
    return jresp(_guarded(gateway.delete_provider, pid))


def ai_provider_test(req, pid):
    return jresp(_guarded(gateway.test_provider, pid))


def ai_settings_get(req):
    return jresp(gateway.settings_view())


def ai_settings_save(req):
    return jresp(_guarded(gateway.save_settings, req.json()))


def ai_token(req):
    gateway.rotate_token()
    return jresp(gateway.settings_view())


def ai_usage(req):
    try:
        days = int(req.q("days", "14") or 14)
    except ValueError:
        days = 14
    return jresp(gateway.usage_report(days))


def ai_models(req):
    return jresp(gateway.admin_models())


# ---------------- 消息中心 ----------------

def message_list(req):
    return jresp(messages.listing())


def message_read(req):
    b = req.json()
    return jresp({"ok": messages.mark_read(b.get("ids"), bool(b.get("all")))})


def message_clear(req):
    return jresp({"ok": messages.clear()})


# ---------------- 上传 ----------------

def upload_init(req):
    b = req.json()
    return jresp(files.upload_init(b.get("path", ""), b.get("name", ""),
                                   b.get("size", 0), b.get("chunk_size", 0), b.get("id", "")))


def upload_route(req):
    rest = req.path[len("/api/upload/"):]
    if rest.endswith("/complete"):
        return jresp(files.upload_complete(rest[:-9]))
    if rest.endswith("/abort"):
        return jresp(files.upload_abort(rest[:-6]))
    if req.method == "PUT":
        uid, _, idx = rest.rpartition("/")
        return jresp(files.upload_chunk(uid, int(idx), req.raw()))
    meta = files.upload_meta(rest)
    return jresp({"id": meta["id"], "name": meta["name"], "size": meta["size"],
                  "total": meta["total"], "chunk_size": meta["chunk_size"],
                  "uploaded": files.uploaded_parts(meta["id"])})


# ---------------- 分享 ----------------

def share_create(req):
    b = req.json()
    return jresp(share.create(b.get("path", ""), b.get("hours", 24), bool(b.get("need_code", True))))


def share_list(req):
    return jresp(share.listing())


def share_revoke(req):
    return jresp(share.revoke(req.json().get("token", "")))


def share_page(req, token):
    return {"html": share.page(token)}


def share_download(req, rest):
    token, _, action = rest.partition("/")
    if action != "download":
        raise Err(404, "not found")
    s = share.unlock(token, req.q("code"), bump=True)
    if not os.path.isfile(s["path"]):
        raise Err(404, "文件已被移动或删除")
    return {"file": FileOut(s["path"], s["name"], True)}


# ---------------- 路由表 ----------------

ROUTES = [
    ("GET", r"^/api/health$", lambda r: jresp({"ok": True, "version": core.APP_VERSION,
                                              "python": __import__("platform").python_version()}), False),
    ("POST", r"^/api/login$", login, False),
    ("GET", r"^/api/public/share/([^/]+)/download$", lambda r, t: share_download(r, t + "/download"), False),
    ("GET", r"^/s/([^/]+)$", share_page, False),

    ("POST", r"^/api/logout$", logout, True),
    ("GET", r"^/api/session$", session, False),
    ("POST", r"^/api/passwd$", change_account, True),
    ("GET", r"^/api/overview$", overview, True),
    ("GET", r"^/api/services$", services, True),
    ("POST", r"^/api/services/([^/]+)/([^/]+)$", service_action, True),
    ("GET", r"^/api/processes$", processes, True),
    ("POST", r"^/api/processes/(\d+)/(kill|kill9)$", kill_proc, True),
    ("GET", r"^/api/network$", network, True),
    ("GET", r"^/api/logs$", logs, True),
    ("GET", r"^/api/logs/([^/]+)$", log_tail, True),
    ("GET", r"^/api/disk$", disk, True),

    ("GET", r"^/api/files$", file_list, True),
    (None, r"^/api/file$", file_dispatch, True),
    ("POST", r"^/api/file/mkdir$", file_mkdir, True),
    ("POST", r"^/api/file/touch$", file_touch, True),
    ("POST", r"^/api/file/rename$", file_rename, True),
    ("POST", r"^/api/file/copy$", file_copy, True),
    ("POST", r"^/api/file/chmod$", file_chmod, True),
    ("GET", r"^/api/file/stat$", file_stat, True),

    ("GET", r"^/api/trash$", trash_list, True),
    ("POST", r"^/api/trash/restore$", trash_restore, True),
    ("POST", r"^/api/trash/purge$", trash_purge, True),
    ("GET", r"^/api/versions$", versions, True),
    ("GET", r"^/api/versions/read$", version_read, True),
    ("POST", r"^/api/versions/restore$", version_restore, True),
    ("POST", r"^/api/versions/delete$", version_delete, True),
    ("GET", r"^/api/favorites$", favs, True),
    ("POST", r"^/api/favorites/toggle$", fav_toggle, True),
    ("POST", r"^/api/search$", search, True),
    ("POST", r"^/api/archive$", archive, True),
    ("POST", r"^/api/extract$", extract, True),
    ("POST", r"^/api/download/remote$", download_remote, True),
    (None, r"^/api/file-settings$", file_settings, True),

    ("POST", r"^/api/exec$", exec_cmd, True),
    ("GET", r"^/api/exec/([^/]+)$", task_get, True),
    ("GET", r"^/api/tasks$", tasks, True),

    ("GET", r"^/api/term/list$", term_list, True),
    ("POST", r"^/api/term/new$", term_new, True),
    ("GET", r"^/api/term/([^/]+)/output$", term_output, True),
    ("POST", r"^/api/term/([^/]+)/input$", term_input, True),
    ("POST", r"^/api/term/([^/]+)/resize$", term_resize, True),
    ("POST", r"^/api/term/([^/]+)/signal$", term_signal, True),
    ("DELETE", r"^/api/term/([^/]+)$", term_close, True),
    ("GET", r"^/api/ssh/hosts$", ssh_hosts, True),
    ("POST", r"^/api/ssh/hosts$", ssh_host_save, True),
    ("POST", r"^/api/ssh/hosts/delete$", ssh_host_delete, True),
    ("GET", r"^/api/ssh/keys$", ssh_keys, True),
    ("POST", r"^/api/ssh/keys$", ssh_key_import, True),

    ("GET", r"^/api/tasktypes$", task_types_list, True),
    ("POST", r"^/api/tasktype/([^/]+)/dryrun$", task_type_dryrun, True),    ("GET", r"^/api/schedule$", schedule_list, True),
    ("GET", r"^/api/schedule/([^/]+)/runs$", schedule_runs, True),
    ("POST", r"^/api/schedule/preview$", schedule_preview, True),
    ("POST", r"^/api/schedule$", schedule_create, True),
    ("POST", r"^/api/schedule/([^/]+)/delete$", schedule_delete, True),
    ("POST", r"^/api/schedule/([^/]+)/run$", schedule_run, True),
    ("POST", r"^/api/schedule/([^/]+)/toggle$", schedule_toggle, True),
    ("POST", r"^/api/schedule/([^/]+)$", schedule_update, True),

    ("GET", r"^/api/messages$", message_list, True),
    ("POST", r"^/api/messages/read$", message_read, True),
    ("POST", r"^/api/messages/clear$", message_clear, True),

    ("GET", r"^/api/tunnel/clients$", tunnel_clients, True),
    ("GET", r"^/api/tunnel/presets$", tunnel_presets, True),
    ("GET", r"^/api/tunnel/instances$", tunnel_instances, True),
    ("GET", r"^/api/tunnel/instance/([^/]+)$", tunnel_detail, True),
    ("POST", r"^/api/tunnel/instance$", tunnel_create, True),
    ("POST", r"^/api/tunnel/instance/([^/]+)$", tunnel_update, True),
    ("POST", r"^/api/tunnel/instance/([^/]+)/(start|stop|restart|delete)$", tunnel_action, True),
    ("POST", r"^/api/tunnel/instance/([^/]+)/probe$", tunnel_probe, True),
    ("POST", r"^/api/tunnel/parse$", tunnel_parse, True),
    ("POST", r"^/api/tunnel/render$", tunnel_render, True),
    ("GET", r"^/api/tunnel/install/status$", tunnel_install_status, True),
    ("GET", r"^/api/tunnel/install/mirrors$", tunnel_install_mirrors, True),
    ("POST", r"^/api/tunnel/install/speedtest$", tunnel_install_speedtest, True),
    ("POST", r"^/api/tunnel/install$", tunnel_install_create, True),
    ("POST", r"^/api/tunnel/uninstall$", tunnel_uninstall, True),

    ("GET", r"^/api/netguard/status$", netguard_status, True),
    ("GET", r"^/api/netguard/settings$", netguard_status, True),
    ("POST", r"^/api/netguard/settings$", netguard_settings, True),
    ("POST", r"^/api/netguard/recover$", netguard_recover, True),
    ("POST", r"^/api/netguard/circuit/reset$", netguard_reset_circuit, True),

    # 监控告警（M09）
    ("GET", r"^/api/alerts/status$", alert_status, True),
    (None, r"^/api/alerts/settings$", alert_settings, True),
    ("POST", r"^/api/alerts/smtp$", alert_smtp_save, True),
    ("POST", r"^/api/alerts/test$", alert_test, True),
    ("GET", r"^/api/alerts/test$", alert_test_result, True),
    ("GET", r"^/api/alerts/items$", alert_items, True),
    ("POST", r"^/api/alerts/items/save$", alert_item_save, True),
    ("POST", r"^/api/alerts/items/delete$", alert_item_delete, True),
    ("POST", r"^/api/alerts/items/toggle$", alert_item_toggle, True),
    ("GET", r"^/api/alerts/events$", alert_events, True),
    ("POST", r"^/api/alerts/events/clear$", alert_events_clear, True),
    ("POST", r"^/api/alerts/check$", alert_check, True),

    ("GET", r"^/api/cf/status$", cf_status, True),
    ("GET", r"^/api/cf/mirrors$", cf_mirrors, True),
    ("POST", r"^/api/cf/mirrors/probe$", cf_probe_mirrors, True),
    ("POST", r"^/api/cf/install$", cf_install, True),
    ("POST", r"^/api/cf/uninstall$", cf_uninstall, True),
    ("GET", r"^/api/cf/settings$", cf_settings_get, True),
    ("POST", r"^/api/cf/settings$", cf_settings_save, True),
    ("POST", r"^/api/cf/verify$", cf_verify, True),
    ("GET", r"^/api/cf/zones$", cf_zones, True),
    ("POST", r"^/api/cf/quick$", cf_quick, True),
    ("POST", r"^/api/cf/named$", cf_named, True),
    ("GET", r"^/api/cf/tunnel/([^/]+)$", cf_tunnel, True),
    ("POST", r"^/api/cf/tunnel/([^/]+)/(start|stop|restart|delete)$", cf_action, True),

    ("GET", r"^/api/docker/status$", docker_status, True),
    ("POST", r"^/api/docker/daemon/(start|stop|restart|enable|disable|ensure)$", docker_daemon, True),
    ("GET", r"^/api/docker/daemon/logs$", docker_daemon_logs, True),
    ("GET", r"^/api/docker/install/status$", docker_install_status, True),
    ("GET", r"^/api/docker/install/meta$", docker_install_meta, True),
    ("POST", r"^/api/docker/install/speedtest$", docker_install_speedtest, True),
    ("POST", r"^/api/docker/install$", docker_install, True),
    ("POST", r"^/api/docker/uninstall$", docker_uninstall, True),
    ("GET", r"^/api/docker/settings$", docker_settings_get, True),
    ("POST", r"^/api/docker/settings$", docker_settings_save, True),
    ("GET", r"^/api/docker/containers$", docker_containers, True),
    ("POST", r"^/api/docker/containers$", docker_container_create, True),
    ("GET", r"^/api/docker/container/([^/]+)$", docker_container, True),
    ("POST", r"^/api/docker/container/([^/]+)/(start|stop|restart|remove)$", docker_container_action, True),
    ("GET", r"^/api/docker/container/([^/]+)/logs$", docker_container_logs, True),
    ("POST", r"^/api/docker/container/([^/]+)/exec$", docker_container_exec, True),
    ("GET", r"^/api/docker/images$", docker_images, True),
    ("POST", r"^/api/docker/images/pull$", docker_image_pull, True),
    ("POST", r"^/api/docker/images/remove$", docker_image_remove, True),
    ("POST", r"^/api/docker/images/import$", docker_image_import, True),
    ("GET", r"^/api/docker/import/dir$", docker_import_dir, True),
    ("POST", r"^/api/docker/compose/preview$", docker_compose_preview, True),
    ("POST", r"^/api/docker/compose/up$", docker_compose_up, True),

    ("GET", r"^/api/ai/overview$", ai_overview, True),
    ("GET", r"^/api/ai/providers$", ai_providers, True),
    ("POST", r"^/api/ai/providers$", ai_provider_save, True),
    ("POST", r"^/api/ai/providers/([^/]+)/delete$", ai_provider_delete, True),
    ("POST", r"^/api/ai/providers/([^/]+)/test$", ai_provider_test, True),
    ("GET", r"^/api/ai/settings$", ai_settings_get, True),
    ("POST", r"^/api/ai/settings$", ai_settings_save, True),
    ("POST", r"^/api/ai/token$", ai_token, True),
    ("GET", r"^/api/ai/usage$", ai_usage, True),
    ("GET", r"^/api/ai/models$", ai_models, True),

    ("GET", r"^/api/appstore/catalog$", store_catalog, True),
    ("GET", r"^/api/appstore/installed$", store_installed, True),
    ("GET", r"^/api/appstore/settings$", store_settings_get, True),
    ("POST", r"^/api/appstore/settings$", store_settings_save, True),
    ("POST", r"^/api/appstore/preview$", store_preview, True),
    ("POST", r"^/api/appstore/install$", store_install, True),

    ("POST", r"^/api/upload/init$", upload_init, True),
    (None, r"^/api/upload/", upload_route, True),

    ("POST", r"^/api/shares$", share_create, True),
    ("GET", r"^/api/shares/list$", share_list, True),
    ("POST", r"^/api/shares/revoke$", share_revoke, True),
]

COMPILED = [(m, re.compile(p), fn, auth) for (m, p, fn, auth) in ROUTES]


def dispatch(req):
    for method, rx, fn, auth in COMPILED:
        m = rx.match(req.path)
        if not m:
            continue
        if method and method != req.method:
            continue
        if auth and not need_auth(req):
            raise Err(401, "未登录")
        args = [g for g in m.groups()] if m.groups() else []
        return fn(req, *args)
    # 没有路由匹配上：必须是 404，不能返回 None 让 _send_result 发一个
    # 200 + 空 JSON —— 那样调用方（和拨测任务）会以为接口存在。
    raise Err(404, "接口不存在")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PocketPanel/" + core.APP_VERSION

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/") or self.path.startswith("/v1"):
            print(time.strftime("%H:%M:%S"), self.command, self.path, flush=True)

    def _handle(self):
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._handle_ws()
            return
        # 大模型接口独立走一条原始流式通道：不能进 Req.json()，否则
        # 20MB 的图片请求会先被完整读进内存，探针透传就白做了。
        _p = urllib.parse.urlsplit(self.path).path
        if _p == "/v1" or _p.startswith("/v1/") or _p.startswith("/v1beta/"):
            gateway.handle(self)
            return
        req = None
        try:
            req = Req(self)
            if req.path.startswith("/api/") or req.path.startswith("/s/"):
                res = dispatch(req)
                self._send_result(res)
            else:
                self._send_static(req.path)
        except Err as e:
            body = {"error": e.msg}
            if e.field_errors:
                body["field_errors"] = e.field_errors
            self._send_json(body, e.code)
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, 404)
        except PermissionError as e:
            self._send_json({"error": str(e)}, 403)
        except TimeoutError as e:
            self._send_json({"error": str(e)}, 410)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass
        except OSError as e:
            # 文件系统层错误不应该变成 500：磁盘满、权限、路径过长都归为可读的 4xx
            self._send_json({"error": f"{e.strerror or e}: {e.filename or ''}".strip()}, 400)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            traceback.print_exc()
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log"), "a") as f:
                    f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.command} {self.path}\n{tb}")
            except OSError:
                pass
            self._send_json({"error": f"内部错误: {e}"}, 500)
        finally:
            # 请求体必须被读完：keep-alive 连接上残留的 body 会被下一个
            # 请求当成请求行解析（表现为 "Unsupported method ('{}GET')"）。
            # 放在最外层，401 / 404 / 未匹配路由等所有分支都能兜住。
            # handler 若已读过，这里是空操作。
            if req is not None:
                req.drain()

    def _handle_ws(self):
        """终端实时通道 / 事件订阅通道。握手失败走普通 HTTP 错误。"""
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/term/([^/]+)/ws$", path)
        is_event = path == "/api/events/ws"
        if not m and not is_event:
            self.close_connection = True
            self._send_json({"error": "not found"}, 404)
            return
        if not need_auth(Req(self)):
            self.close_connection = True
            self._send_json({"error": "未登录"}, 401)
            return
        # 浏览器发起的 WS 一定带 Origin，且 cookie 是自动带上的。不校验的话，
        # 用户随便打开一个恶意页面就能在对局域网面板开 WS（CSWSH）。
        origin = self.headers.get("Origin")
        if origin:
            if urllib.parse.urlparse(origin).netloc != (self.headers.get("Host") or ""):
                self.close_connection = True
                self._send_json({"error": "来源不被允许"}, 403)
                return
        key = self.headers.get("Sec-WebSocket-Key") or ""
        if not key or (self.headers.get("Sec-WebSocket-Version") or "") != "13":
            self.close_connection = True
            self._send_json({"error": "需要 WebSocket 握手"}, 400)
            return
        if is_event:
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
            self.end_headers()
            try:
                self.wfile.flush()
            except OSError:
                return
            self.close_connection = True
            try:
                events.pump(self.connection, self.rfile, self.wfile)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            return
        try:
            sess = term.get(m.group(1))
        except KeyError as e:
            self.close_connection = True
            self._send_json({"error": e.args[0] if e.args else "会话不存在"}, 404)
            return

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return
        self.close_connection = True
        try:
            ws.pump(self.connection, self.rfile, self.wfile, sess)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle

    def do_HEAD(self):
        """curl -I / 外部探活只看响应头：走同一套路由，但不写 body。"""
        self._head_only = True
        try:
            self._handle()
        finally:
            self._head_only = False

    def _send_result(self, res):
        if isinstance(res, dict) and "file" in res:
            self._send_file(res["file"])
            return
        if isinstance(res, dict) and "html" in res:
            body = res["html"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if isinstance(res, dict) and "json" in res:
            self._send_json(res["json"], res.get("code", 200), res.get("headers"), res.get("cookies"))
            return
        self._send_json(res or {})

    def _send_json(self, obj, code=200, headers=None, cookies=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _send_file(self, fo):
        path = fo.path
        st = os.stat(path)
        size = st.st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        ctype = "application/octet-stream" if fo.download else (
            mimetypes.guess_type(fo.filename)[0] or "application/octet-stream")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if fo.download:
            quoted = urllib.parse.quote(fo.filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        with open(path, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(64 * 1024, remain))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    return
                remain -= len(chunk)

    def _send_static(self, path):
        if path == "/":
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        target = os.path.normpath(os.path.join(STATIC, rel))
        # 加 sep 再比，否则 static-evil/ 这种同级目录也能通过 startswith
        if not target.startswith(STATIC + os.sep) and target != STATIC:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(target):
            # 带 hash 的构建产物不存在就是不存在，回退成 HTML 只会让浏览器报
            # 「Unexpected token '<'」。发版后旧标签页请求老 hash 时也会走到这里。
            if rel.startswith("assets/"):
                self._send_json({"error": "not found"}, 404)
                return
            # SPA 路由回退
            target = os.path.join(STATIC, "index.html")
            if not os.path.isfile(target):
                self._send_json({"error": "前端未构建：static/index.html 不存在"}, 503)
                return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(body)))
        if "/assets/" in target:
            # 文件名带内容 hash，内容一变名字就变，可以放心长缓存
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            # index.html 一旦被浏览器「先用缓存、后台再校验」（微信/夸克这类
            # 内置内核很常见），发版后侧边栏还是旧的。no-store 直接禁掉存储，
            # 470 字节的东西每次重下比让人强刷一次划算。
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)


def cleaner():
    while True:
        try:
            files.trash_autoclean()
            files.clean_uploads()
            files.gc_orphan_versions()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(3600)


def main():
    # runsv 按惯例把被管进程的 SIGINT/SIGQUIT 设为忽略。exec 会保留这个
    # 处置，于是面板 fork 出去的每个 shell 都继承了"忽略 Ctrl+C"，
    # 终端里按 ^C 毫无反应。面板自己不需要忽略这两个信号，改回默认。
    for _s in (signal.SIGINT, signal.SIGQUIT):
        try:
            signal.signal(_s, signal.SIG_DFL)
        except (OSError, ValueError):
            pass

    core.cfg_reload()
    port = int(core.cfg_get("port", default=8888))
    port = int(os.environ.get("PP_PORT") or port)
    os.makedirs(os.path.join(core.DATA), mode=0o700, exist_ok=True)
    gateway.ensure_ready()

    threading.Thread(target=metrics.cpu_loop, daemon=True).start()
    threading.Thread(target=cleaner, daemon=True).start()
    term.ensure_reaper()
    sched.ensure_scheduler()
    tunnel.ensure_monitor()
    cloudflare.ensure_monitor()
    netguard.ensure_thread()
    alerts.ensure_thread()
    svc.ensure_supervisor()
    runtime.ensure_monitor()
    # Android 上 udocker 自带的 proot 会被 seccomp 打死，启动时修一次（幂等）
    try:
        udocker_install.ensure_android_proot()
    except Exception:  # noqa: BLE001
        pass
    os.environ.setdefault("DOCKER_HOST",
                          f"tcp://127.0.0.1:{runtime.settings()['port']}")

    def _bye(signum, frame):  # noqa: ARG001
        # sv down / sv restart 走 SIGTERM。不在这里收尾的话，PTY 会话
        # 的父进程没了、master fd 关了，但 login shell 往往不理会
        # SIGHUP，会留一堆几 MB 的孤儿 bash 在后台。
        try:
            term.close_all()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    for _s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_s, _bye)
        except (OSError, ValueError):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"PocketPanel {core.APP_VERSION} 监听 0.0.0.0:{port}")
    print(f"本机访问: http://127.0.0.1:{port}")
    print(f"局域网:   http://{metrics.lan_ip()}:{port}")
    print("运行内存: 见 ps -o rss=", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
