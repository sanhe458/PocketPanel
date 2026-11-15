#!/usr/bin/env python3
"""PocketPanel 守护进程（guard）。

和面板互为看门狗：

  guard 看面板   每 15 秒探一次 /api/health。面板进程还在、但 HTTP 已经
                 不响应（死锁、线程打满、GC 卡住）这种情况 runit 是看不
                 出来的 —— 它只管进程活着。连续 3 次探不到就重启。
  面板看 guard   面板每 20 秒读一次 guard 的心跳，超过 60 秒没更新就把它
                 重启。两边都优先走 runit，不会出现两个实例。

重启带退避：面板要是因为代码错误根本起不来，短时间内连着拉几次仍失败，
就退到 5 分钟一次，不至于把 CPU 和电量烧了。

独立运行：python3 guard.py
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import core  # noqa: E402

APP_SERVICE = "pocketpanel-py"
GUARD_SERVICE = "pocketpanel-guard"

INTERVAL = 15        # 巡检间隔（秒）
TIMEOUT = 5          # 单次探活超时
FAIL_LIMIT = 3       # 连续失败几次判定宕机
BOOT_GRACE = 25      # 重启后给面板的启动时间
HB_STALE = 60        # 心跳多久没更新算 guard 挂了
BACKOFF = (0, 20, 60, 180, 600)

PID_FILE = os.path.join(core.DATA, "guard.pid")
HB_FILE = os.path.join(core.DATA, "guard.hb")
STATE_FILE = os.path.join(core.DATA, "guard.json")
OFF_FILE = os.path.join(core.DATA, "guard.off")
LOCK_FILE = os.path.join(core.DATA, "guard.lock")
APP_PID_FILE = os.path.join(core.DATA, "app.pid")
APP_LOG = os.path.join(core.DATA, "app-guard.log")


# ---------------- 基础工具 ----------------

def port():
    """和面板取端口的规则保持一致：PP_PORT 优先，其次 config.json。"""
    try:
        p = int(os.environ.get("PP_PORT") or 0)
    except ValueError:
        p = 0
    return p or int(core.cfg_get("port", default=8888))


def alive(pid):
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def read_hb():
    try:
        with open(HB_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def heartbeat():
    """写心跳。用 tmp + replace，读的一方不会读到半截数字。"""
    try:
        tmp = HB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        os.replace(tmp, HB_FILE)
    except OSError:
        traceback.print_exc()


def state_load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def state_save(st):
    try:
        core.write_json(STATE_FILE, st)
    except Exception:
        traceback.print_exc()


def sv(action, name):
    """调 runit。返回 (成功?, 输出)。"""
    cmd = f"SVDIR={core.SVC_DIR} sv {action} {name} 2>&1"
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, timeout=30)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        return (r.returncode == 0 and "fail:" not in out), out
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def probe():
    """探面板的 HTTP。返回 (健康?, 原因, 是否立刻判定死亡)。

    连接被拒是「进程都没了」，没必要等三次；超时和 5xx 才可能是短暂的
    抖动（比如正在处理一个大请求），要连续失败几次才动手。
    """
    url = f"http://127.0.0.1:{port()}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            body = r.read(512)
            try:
                d = json.loads(body or b"{}")
            except ValueError:
                return False, f"响应不是 JSON: {body[:80]!r}", False
            if r.status == 200 and d.get("ok"):
                return True, "", False
            return False, f"HTTP {r.status}", False
    except urllib.error.URLError as e:
        refused = isinstance(e.reason, ConnectionRefusedError) or "refused" in str(e).lower()
        return False, f"URLError: {e}", refused
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", False


# ---------------- 拉起面板 ----------------

def app_pid():
    pid = read_pid(APP_PID_FILE)
    if pid and alive(pid) and "app.py" in cmdline(pid):
        return pid
    return 0


def app_uptime():
    """从 sv status 里抠出「这个服务已经跑了多少秒」。

    用户手动 `sv restart` 时会出现 1~2 秒的空窗，探活正好撞上就会判成宕机，
    再补一刀把人刚重启的面板又重启一遍。有 uptime 就能认出「刚起来」。
    """
    _ok, out = sv("status", APP_SERVICE)
    m = re.search(r"\(pid \d+\) (\d+)s", out)
    return int(m.group(1)) if m else None


def spawn_app():
    """runit 不可用时的兜底：直接起一个脱离会话的面板进程。"""
    if app_pid():
        return False
    try:
        log = open(APP_LOG, "ab", buffering=0)
    except OSError:
        log = subprocess.DEVNULL
    env = dict(os.environ)
    env["PP_PORT"] = str(port())
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "app.py")],
            cwd=HERE, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env,
        )
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


def restart_app(reason):
    """优先让 runit 重启；runit 不在才自己拉。返回用了哪种方式。"""
    up = app_uptime()
    if up is not None and up < BOOT_GRACE + 15:
        print(f"[guard] 面板 {up}s 前才起来，这次不算，继续观察", flush=True)
        return "boot-grace"

    pid = app_pid()
    print(f"[guard] 面板无响应：{reason}（pid={pid or '?'}），开始重启", flush=True)

    ok, out = sv("restart", APP_SERVICE)
    if ok:
        return "runit"

    # runit 没接住。可能只是 sv 自己失败，但面板其实还活着 —— 那就别起第二个。
    good, _, _ = probe()
    if good:
        print(f"[guard] sv 重启失败但面板仍可访问，跳过（{out}）", flush=True)
        return "skip"

    print(f"[guard] sv restart 失败（{out}），改用直接启动", flush=True)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except OSError:
            pass
    return "spawn" if spawn_app() else "failed"


# ---------------- 服务安装 ----------------

def _run_body():
    return (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        f"export HOME={core.HOME}\n"
        f"export PREFIX={core.PREFIX}\n"
        f"export PATH={core.PREFIX}/bin:$PATH\n"
        f"cd {HERE} || exit 1\n"
        "exec python3 guard.py 2>&1\n"
    )


def _log_body():
    logdir = os.path.join(core.DATA, "logs", GUARD_SERVICE)
    return (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        f"export PATH={core.PREFIX}/bin:$PATH\n"
        f"exec svlogd -tt {logdir}\n"
    )


def service_installed():
    return os.path.isfile(os.path.join(core.SVC_DIR, GUARD_SERVICE, "run"))


def ensure_service():
    """把 guard 注册成 runit 服务。已经装了就只在内容变了时重写。"""
    if not os.path.isdir(core.SVC_DIR):
        return False
    base = os.path.join(core.SVC_DIR, GUARD_SERVICE)
    try:
        os.makedirs(os.path.join(base, "log"), exist_ok=True)
        os.makedirs(os.path.join(core.DATA, "logs", GUARD_SERVICE), mode=0o700, exist_ok=True)
        changed = False
        for path, body in ((os.path.join(base, "run"), _run_body()),
                           (os.path.join(base, "log", "run"), _log_body())):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                old = None
            if old != body:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                os.chmod(path, 0o755)
                changed = True
        # 新目录 runsvdir 要几秒才发现，up 失败就重试几次
        if changed or not status()["running"]:
            for _ in range(3):
                ok, _out = sv("up", GUARD_SERVICE)
                if ok:
                    break
                time.sleep(1)
        return changed
    except OSError:
        traceback.print_exc()
        return False


# ---------------- 面板侧的反向看门狗 ----------------

def _guard_status():
    pid = read_pid(PID_FILE)
    hb = read_hb()
    now = int(time.time())
    fresh = bool(hb) and (now - hb) <= HB_STALE
    ok = bool(pid) and alive(pid) and "guard.py" in cmdline(pid)
    return ok, fresh, pid, hb, now


def check_guard():
    """由面板的线程定期调用：guard 没了就拉起来。"""
    if os.path.exists(OFF_FILE):
        return
    ok, fresh, pid, hb, now = _guard_status()
    if ok and fresh:
        return
    if not ok and not fresh:
        why = "进程不在，心跳也断了"
    elif not ok:
        why = "进程不在"
    else:
        why = f"心跳停在 {now - hb}s 前"

    print(f"[面板] 守护进程异常（{why}），拉起", flush=True)
    ok2, out = sv("restart", GUARD_SERVICE)
    if ok2:
        return
    print(f"[面板] sv restart 失败（{out}），直接启动", flush=True)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except OSError:
            pass
    try:
        log = open(os.path.join(core.DATA, "guard-spawn.log"), "ab", buffering=0)
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "guard.py")],
            cwd=HERE, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=dict(os.environ, PP_PORT=str(port())),
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def watch_from_app():
    """面板启动时挂一个线程跑它。前 60 秒不检查，等 guard 自己起来。"""
    time.sleep(BOOT_GRACE + 30)
    while True:
        try:
            check_guard()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(INTERVAL)


def status():
    """给 API / 界面用的一份状态快照。"""
    ok, fresh, pid, hb, now = _guard_status()
    st = state_load()
    return {
        "running": ok,
        "healthy": ok and fresh,
        "pid": pid,
        "heartbeat": hb,
        "heartbeat_age": (now - hb) if hb else None,
        "enabled": not os.path.exists(OFF_FILE),
        "installed": service_installed(),
        "restarts": int(st.get("restarts", 0)),
        "last_restart": st.get("last_restart"),
        "last_how": st.get("last_how", ""),
        "last_error": st.get("last_error", ""),
        "last_ok": st.get("last_ok"),
        "interval": INTERVAL,
        "fail_limit": FAIL_LIMIT,
    }


# ---------------- 主循环 ----------------

def _lock():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main():
    lock = _lock()  # noqa: F841  （持有到进程退出）
    if lock is None:
        print("[guard] 已经有一个 guard 在跑，退出", flush=True)
        return
    st = state_load()
    now = int(time.time())
    st.update({"pid": os.getpid(), "started": now, "version": core.APP_VERSION})
    state_save(st)
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        traceback.print_exc()
    heartbeat()

    print(f"[guard] 启动 pid={os.getpid()}，每 {INTERVAL}s 探 "
          f"http://127.0.0.1:{port()}/api/health", flush=True)

    fails = 0
    while True:
        ok, detail, dead = probe()
        now = int(time.time())
        st["last_probe"] = now
        if ok:
            if st.get("fails"):
                print("[guard] 面板已恢复", flush=True)
            fails = 0
            st.update({"fails": 0, "last_ok": now, "last_error": "", "streak": 0})
        else:
            fails += 1
            st.update({"fails": fails, "last_error": detail})
            limit = 1 if dead else FAIL_LIMIT
            print(f"[guard] 探活失败 {fails}/{limit}：{detail}", flush=True)
            if fails >= limit:
                how = restart_app(detail)
                if how == "boot-grace":
                    fails = 0
                    st["fails"] = 0
                    heartbeat()
                    state_save(st)
                    time.sleep(INTERVAL)
                    continue
                st["restarts"] = int(st.get("restarts", 0)) + 1
                st["last_restart"] = now
                st["last_how"] = how
                st["streak"] = int(st.get("streak", 0)) + 1
                fails = 0
                st["fails"] = 0
                state_save(st)
                heartbeat()
                wait = BOOT_GRACE + BACKOFF[min(st["streak"], len(BACKOFF) - 1)]
                print(f"[guard] 第 {st['streak']} 次重启（{how}），等 {wait}s 再巡检", flush=True)
                time.sleep(wait)
                continue
        heartbeat()
        state_save(st)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        pass
