"""服务托管（ServiceManager）：统一 runit 与面板直管两种适配器。

Termux 没有 systemd。设计文档 3.1 约定三层适配器，本实现落地两层：

* **RunitAdapter**：`$PREFIX/var/service` 存在且 `sv` 可用时优先。服务由
  runsvdir 托管，面板只负责注册（写 run / log/run）与 `sv` 调用；面板重启
  甚至被系统杀掉都不影响服务本身。
* **DirectAdapter**：兜底。面板 fork + setsid 启动，写 pid 文件、日志落盘，
  意外退出按 1s→2s→…→60s 指数退避重启，连败 10 次标记失败。面板重启后
  按 pid 文件接管仍在运行的进程，不会重复拉起。

`ProotAdapter` 需要 proot-distro 内的 systemd，与「无 root」边界冲突，不实现。

注册信息（怎么起）存在 `data/services.json`，运行态（pid / 退避计数）只在
内存与 `data/run/<name>.json`，两者分开，避免把瞬时状态写进配置。
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback

import core

REG_FILE = os.path.join(core.DATA, "services.json")
RUN_DIR = os.path.join(core.DATA, "run")
LOG_DIR = os.path.join(core.DATA, "logs")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MAX_BACKOFF = 60
MAX_FAIL = 10
SUP_INTERVAL = 3
LOG_MAX = 2 * 1024 * 1024

_lock = threading.RLock()
_specs = {}
_loaded = False
_sup_started = False
_rt = {}          # name -> {"proc", "pid", "started", "fail", "next", "log"}
_tai = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}\.\d+\s")


# ---------------- 注册表 ----------------

def init():
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _loaded = True
        os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
        data = core.read_json(REG_FILE, {})
        if isinstance(data, dict):
            _specs.update(data)


def _save():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = REG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_specs, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, REG_FILE)


def runit_available():
    """runit 是否真的在跑：目录存在且 sv 可执行。"""
    return os.path.isdir(core.SVC_DIR) and bool(shutil.which("sv"))


def spec(name):
    init()
    with _lock:
        s = _specs.get(name)
        return dict(s) if s else None


def registered():
    init()
    with _lock:
        return [dict(v) for v in _specs.values()]


def register(name, argv, cwd=None, env=None, autostart=True,
             adapter=None, title=""):
    """注册（或更新）一个服务。重复调用是幂等的，内容不变不重写文件。"""
    if not NAME_RE.match(name or ""):
        raise ValueError("服务名只能是小写字母、数字、- 和 _，2–32 位")
    if not argv:
        raise ValueError("缺少启动命令")
    init()
    adapter = adapter or ("runit" if runit_available() else "direct")
    if adapter not in ("runit", "direct"):
        raise ValueError("未知适配器")
    env = {str(k): str(v) for k, v in (env or {}).items()}
    spec_obj = {
        "name": name, "argv": [str(a) for a in argv],
        "cwd": cwd or core.HOME, "env": env, "autostart": bool(autostart),
        "adapter": adapter, "title": title or name,
        "created": int(time.time()),
    }
    with _lock:
        old = _specs.get(name)
        if old:
            # want_run 属于运行意图，不能被重注册抹掉
            spec_obj["want_run"] = old.get("want_run", False)
        else:
            spec_obj["want_run"] = False
        _specs[name] = spec_obj
        _save()
    if adapter == "runit":
        _write_runit(spec_obj)
    return spec_obj


def _write_runit(s):
    base = os.path.join(core.SVC_DIR, s["name"])
    os.makedirs(os.path.join(base, "log"), exist_ok=True)
    run = os.path.join(base, "run")
    env_lines = "\n".join(f"export {k}={shlex.quote(v)}"
                          for k, v in s["env"].items() if re.match(r"^\w+$", k))
    body = (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        f"export HOME={shlex.quote(core.HOME)}\n"
        f"export PREFIX={shlex.quote(core.PREFIX)}\n"
        f"export PATH={shlex.quote(core.PREFIX + '/bin:' + os.environ.get('PATH', '/usr/bin:/bin'))}\n"
        f"{env_lines}\n"
        f"cd {shlex.quote(s['cwd'])} || exit 1\n"
        f"exec {shlex.join(s['argv'])} 2>&1\n"
    )
    _write_if_changed(run, body, 0o755)
    logrun = os.path.join(base, "log", "run")
    logdir = os.path.join(LOG_DIR, s["name"])
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    body2 = (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        f"exec svlogd -tt {shlex.quote(logdir)}\n"
    )
    _write_if_changed(logrun, body2, 0o755)


def _write_if_changed(path, body, mode):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == body:
                return
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def unregister(name):
    """只删注册信息，不碰进程；先 stop。"""
    init()
    with _lock:
        s = _specs.pop(name, None)
        if s:
            _save()
    if s and s.get("adapter") == "runit":
        d = os.path.join(core.SVC_DIR, name)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    try:
        os.unlink(os.path.join(RUN_DIR, name + ".json"))
    except OSError:
        pass
    return bool(s)


# ---------------- 状态 ----------------

def _pid_alive(pid):
    """僵尸进程不算活着：直管服务退出后若只看 kill(0)，会永远显示运行中。"""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
        pos = raw.rfind(b")")
        if pos > 0 and raw[pos + 2:pos + 3] == b"Z":
            return False
        return True
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
    except OSError:
        return ""


def _uptime_text(started):
    if not started:
        return ""
    sec = max(0, int(time.time() - started))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


def status(name):
    init()
    s = spec(name)
    if not s:
        return {"name": name, "adapter": "", "state": "none", "pid": 0,
                "uptime": "", "autostart": False, "title": name}
    if s["adapter"] == "runit":
        out = core.sh(f"SVDIR={shlex.quote(core.SVC_DIR)} sv status {shlex.quote(name)} 2>&1")
        state, pid, up = "stop", 0, ""
        if out.startswith("run:"):
            state = "run"
        elif out.startswith("fail:"):
            state = "fail"
        m = re.search(r"\(pid (\d+)\)", out)
        if m:
            pid = int(m.group(1))
        m = re.search(r"\)\s+([^;]+)", out)
        if m:
            up = m.group(1).strip()
        return {"name": name, "adapter": "runit", "state": state, "pid": pid,
                "uptime": up, "autostart": _runit_enabled(name),
                "title": s.get("title", name)}
    with _lock:
        rt = _rt.get(name) or {}
        pid = rt.get("pid") or _pidfile(name)
        started = rt.get("started") or _pidfile_started(name)
        fail = rt.get("fail", 0)
    if pid and _pid_alive(pid):
        return {"name": name, "adapter": "direct", "state": "run", "pid": pid,
                "uptime": _uptime_text(started), "autostart": s.get("autostart", True),
                "title": s.get("title", name), "restarts": fail}
    if fail >= MAX_FAIL:
        return {"name": name, "adapter": "direct", "state": "fail", "pid": 0,
                "uptime": "", "autostart": s.get("autostart", True),
                "title": s.get("title", name), "restarts": fail}
    return {"name": name, "adapter": "direct", "state": "stop", "pid": 0,
            "uptime": "", "autostart": s.get("autostart", True),
            "title": s.get("title", name), "restarts": fail}


def _runit_enabled(name):
    return not os.path.exists(os.path.join(core.SVC_DIR, name, "down"))


def _pidfile(name):
    d = core.read_json(os.path.join(RUN_DIR, name + ".json"), {})
    try:
        return int(d.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _pidfile_started(name):
    d = core.read_json(os.path.join(RUN_DIR, name + ".json"), {})
    try:
        return float(d.get("started") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- 启停 ----------------

def action(name, act):
    init()
    s = spec(name)
    if not s:
        raise KeyError("服务未注册")
    if act not in ("start", "stop", "restart", "enable", "disable"):
        raise ValueError(f"不支持的操作: {act}")
    if s["adapter"] == "runit":
        return _runit_action(name, act)
    if act == "enable":
        _set_autostart(name, True)
        return True
    if act == "disable":
        _set_autostart(name, False)
        return True
    if act == "start":
        return _direct_start(name)
    if act == "stop":
        return _direct_stop(name)
    if act == "restart":
        _direct_stop(name)
        return _direct_start(name)
    return True


def _runit_action(name, act):
    if act in ("enable", "disable"):
        down = os.path.join(core.SVC_DIR, name, "down")
        try:
            if act == "disable":
                open(down, "a").close()
            else:
                os.unlink(down)
        except FileNotFoundError:
            pass
        with _lock:
            if name in _specs:
                _specs[name]["autostart"] = act == "enable"
                _save()
        return True
    if act == "stop":
        # 停止也算「不想跑」，否则 runsvdir 重启后又被拉起
        _set_want(name, False)
    if act == "start":
        _set_want(name, True)
    out = core.sh(f"SVDIR={shlex.quote(core.SVC_DIR)} sv {act} {shlex.quote(name)} 2>&1")
    return out


def _set_want(name, on):
    with _lock:
        if name in _specs:
            _specs[name]["want_run"] = bool(on)
            _save()


def _set_autostart(name, on):
    with _lock:
        if name in _specs:
            _specs[name]["autostart"] = bool(on)
            _save()
    if on:
        # 打开自启时如果确实没在跑，顺手拉起来
        st = status(name)
        if st["state"] != "run":
            action(name, "start")


def _direct_start(name, keep_fail=False):
    s = spec(name)
    if not s:
        raise KeyError("服务未注册")
    with _lock:
        rt = _rt.get(name) or {}
        if rt.get("pid") and _pid_alive(rt["pid"]):
            return True
        logf = _open_log(name)
        env = {**os.environ, "TERM": "dumb", **s.get("env", {})}
        try:
            proc = subprocess.Popen(
                s["argv"], cwd=s.get("cwd") or core.HOME, env=env,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"无法启动 {name}: {e}")
        _rt[name] = {"proc": proc, "pid": proc.pid, "started": time.time(),
                     "fail": rt.get("fail", 0) if keep_fail else 0,
                     "next": 0, "log": logf}
        _write_pidfile(name, proc.pid)
        _set_want(name, True)
    _log_line(name, f"--- 启动 pid={proc.pid} argv={shlex.join(s['argv'])}")
    return True


def _direct_stop(name):
    with _lock:
        rt = _rt.get(name) or {}
        pid = rt.get("pid") or _pidfile(name)
        proc = rt.get("proc")
        _set_want(name, False)
        rt["pid"] = 0
        rt["next"] = 0
        _rt[name] = rt
        logf = rt.get("log")
    if not pid or not _pid_alive(pid):
        _cleanup_log(logf)
        _remove_pidfile(name)
        return True
    for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                break
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            if proc is not None:
                try:
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.2)
        if not _pid_alive(pid):
            break
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    _log_line(name, "--- 已停止")
    _cleanup_log(logf)
    _remove_pidfile(name)
    return True


def _cleanup_log(logf):
    try:
        if logf:
            logf.close()
    except Exception:  # noqa: BLE001
        pass


def _open_log(name):
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(LOG_DIR, name + ".log")
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass
    return open(path, "ab")  # noqa: SIM115 进程生命周期内持有


def _log_line(name, line):
    try:
        with open(os.path.join(LOG_DIR, name + ".log"), "ab") as fh:
            fh.write((time.strftime("[%Y-%m-%d %H:%M:%S] ") + line + "\n").encode())
    except OSError:
        pass


def _write_pidfile(name, pid):
    core.write_json(os.path.join(RUN_DIR, name + ".json"),
                    {"pid": pid, "started": time.time(), "at": int(time.time())})


def _remove_pidfile(name):
    try:
        os.unlink(os.path.join(RUN_DIR, name + ".json"))
    except OSError:
        pass


# ---------------- 直管保活 ----------------

def ensure_supervisor():
    """幂等启动直管服务的保活线程，并接管上次遗留的进程。"""
    global _sup_started
    init()
    with _lock:
        if _sup_started:
            return
        _sup_started = True
    _adopt()
    threading.Thread(target=_sup_loop, daemon=True, name="svc-supervisor").start()


def _adopt():
    """面板重启后接管仍在运行的直管进程（按 pid 文件 + cmdline 校验）。"""
    for s in registered():
        if s.get("adapter") != "direct" or not s.get("want_run"):
            continue
        pid = _pidfile(s["name"])
        if not pid or not _pid_alive(pid):
            continue
        want = os.path.basename(s["argv"][0])
        got = _cmdline(pid)
        if want and want not in got:
            # pid 已被系统复用，不能接管
            _remove_pidfile(s["name"])
            continue
        with _lock:
            _rt[s["name"]] = {"proc": None, "pid": pid,
                              "started": _pidfile_started(s["name"]),
                              "fail": 0, "next": 0, "log": None}


def _sup_loop():
    while True:
        try:
            _sup_tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(SUP_INTERVAL)


def _sup_tick():
    now = time.time()
    for s in registered():
        if s.get("adapter") != "direct" or not s.get("want_run"):
            continue
        name = s["name"]
        retry = alert = False
        with _lock:
            rt = _rt.setdefault(name, {"proc": None, "pid": 0, "started": 0,
                                       "fail": 0, "next": 0, "log": None})
            pid = rt.get("pid") or _pidfile(name)
            if _pid_alive(pid):
                if not rt.get("pid"):
                    rt["pid"], rt["started"] = pid, _pidfile_started(name)
                # 稳定运行 60s 才清零连败计数，否则秒退的服务会被无限重启
                if rt.get("fail") and now - rt.get("started", now) > 60:
                    rt["fail"] = 0
                continue
            if rt.get("proc") is not None:
                try:
                    rt["proc"].wait(timeout=1)
                except Exception:  # noqa: BLE001
                    pass
                rt["proc"] = None
            rt["pid"] = 0
            if rt.get("log"):
                _cleanup_log(rt["log"])
                rt["log"] = None
            fail = rt.get("fail", 0)
            if fail >= MAX_FAIL:
                continue
            if not rt.get("next"):
                # 刚发现退出：记一次失败并安排退避，下一轮（>=1s）才真正拉起
                rt["fail"] = fail + 1
                backoff = min(MAX_BACKOFF, 2 ** fail)
                rt["next"] = now + backoff
                _log_line(name, f"--- 进程退出，{backoff}s 后自动重启（第 {fail + 1} 次）")
                alert = rt["fail"] >= MAX_FAIL
                if not alert:
                    continue
            elif rt["next"] <= now:
                retry = True
        if alert:
            import messages  # 延迟导入：无 DATA 环境下也不影响 svc 单独使用
            messages.add("err", f"服务 {s.get('title') or name} 反复退出已停止",
                         f"连续 {MAX_FAIL} 次退出，停止自动重启；请查看日志")
            continue
        if not retry:
            continue
        try:
            _direct_start(name, keep_fail=True)
            with _lock:
                rt = _rt.get(name) or {}
                rt["next"] = 0
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# ---------------- 日志 ----------------

def tail(name, lines=200):
    init()
    s = spec(name)
    if not s:
        raise FileNotFoundError("服务未注册")
    lines = max(10, min(int(lines or 200), 2000))
    if s["adapter"] == "runit":
        path = os.path.join(LOG_DIR, name, "current")
    else:
        path = os.path.join(LOG_DIR, name + ".log")
    if not os.path.isfile(path):
        return {"name": name, "lines": [], "size": 0, "truncated": False}
    size = os.path.getsize(path)
    start = max(0, size - 256 * 1024)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read().decode("utf-8", "replace")
    rows = [r for r in data.rstrip("\n").split("\n") if r != ""]
    rows = [_tai.sub("", r) for r in rows][-lines:]
    return {"name": name, "lines": rows, "size": size, "truncated": start > 0}
