#!/usr/bin/env python3
"""守护进程验收（纯本地，不碰真机、不启真面板）。

覆盖四件事：
  1. 心跳/pid/状态三种文件能写能读，坏文件不会把 guard 带崩；
  2. 探活判定：200 + ok:true 才算健康，500、非 JSON、连不上都不算；
  3. 反向看门狗：主动停用不拉、心跳新鲜不拉、过期或没进程才拉；
  4. runit 服务脚本生成正确，单实例锁真的锁得住。
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import core

TMP = tempfile.mkdtemp(prefix="pp-guard-")
core.DATA = TMP
core.SVC_DIR = os.path.join(TMP, "service")
os.makedirs(core.SVC_DIR, exist_ok=True)

import guard  # noqa: E402

ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if cond:
        ok += 1
    else:
        fails.append(name)


# ---------------- 假面板 ----------------

class _H(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if _H.mode == "ok":
            code, body = 200, b'{"ok": true}'
        elif _H.mode == "notok":
            code, body = 200, b'{"ok": false}'
        elif _H.mode == "html":
            code, body = 200, b"<html>hi</html>"
        else:
            code, body = 500, b"boom"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
guard.port = lambda: PORT


print("[1] 文件读写")
guard.heartbeat()
hb = guard.read_hb()
check("心跳写进去能读回来", abs(hb - int(time.time())) <= 2, hb)
check("心跳文件没有 .tmp 残留", not os.path.exists(guard.HB_FILE + ".tmp"))
check("没有 pid 文件时读成 0", guard.read_pid(os.path.join(TMP, "nope.pid")) == 0)

with open(os.path.join(TMP, "bad.pid"), "w") as f:
    f.write("not-a-number")
check("pid 文件是垃圾也不抛异常", guard.read_pid(os.path.join(TMP, "bad.pid")) == 0)

guard.state_save({"restarts": 7, "last_how": "runit"})
st = guard.state_load()
check("状态往返", st.get("restarts") == 7 and st.get("last_how") == "runit")
with open(guard.STATE_FILE, "w") as f:
    f.write("{broken")
check("状态文件损坏时返回空字典", guard.state_load() == {})


print("[2] 探活判定")
_H.mode = "ok"
good, why, dead = guard.probe()
check("200 + ok:true 判健康", good and why == "", why)

_H.mode = "notok"
good, why, dead = guard.probe()
check("200 + ok:false 判失败", not good, why)
check("ok:false 不算立即死亡", dead is False)

_H.mode = "html"
good, why, dead = guard.probe()
check("返回 HTML 判失败（不是 JSON）", not good and "JSON" in why, why)

_H.mode = "500"
good, why, dead = guard.probe()
check("500 判失败", not good and "500" in why, why)
check("500 要攒够次数才动手", dead is False)

_real_port = guard.port
guard.port = lambda: 1  # 没人监听
good, why, dead = guard.probe()
check("连不上判失败", not good and why, why)
check("连接被拒 = 立即判定死亡", dead is True, why)
guard.port = _real_port


print("[3] 反向看门狗")
calls = []


def fake_sv(action, name):
    calls.append((action, name))
    return True, ""


guard.sv = fake_sv
guard.alive = lambda pid: pid == os.getpid()
guard.cmdline = lambda pid: "python3 guard.py"

# 情况 A：用户主动停用
open(guard.OFF_FILE, "w").write("0")
calls.clear()
guard.check_guard()
check("停用标记存在时不拉", calls == [], calls)
os.remove(guard.OFF_FILE)

# 情况 B：guard 活得很好
guard.heartbeat()
with open(guard.PID_FILE, "w") as f:
    f.write(str(os.getpid()))
calls.clear()
guard.check_guard()
check("心跳新鲜 + 进程在，不拉", calls == [], calls)

# 情况 C：进程在，心跳过期
old = int(time.time()) - guard.HB_STALE - 10
with open(guard.HB_FILE, "w") as f:
    f.write(str(old))
calls.clear()
guard.check_guard()
check("心跳过期就重启 guard", calls == [("restart", guard.GUARD_SERVICE)], calls)

# 情况 D：进程不在
guard.heartbeat()
with open(guard.PID_FILE, "w") as f:
    f.write("999999")
calls.clear()
guard.check_guard()
check("guard 进程不在就拉起", calls == [("restart", guard.GUARD_SERVICE)], calls)


print("[4] runit 服务脚本")
guard.alive = lambda pid: False
changed = guard.ensure_service()
base = os.path.join(core.SVC_DIR, guard.GUARD_SERVICE)
run = os.path.join(base, "run")
logrun = os.path.join(base, "log", "run")
check("首次安装返回 changed", changed is True)
check("run 脚本生成", os.path.isfile(run))
check("log/run 生成", os.path.isfile(logrun))
check("run 可执行", (os.stat(run).st_mode & 0o777) == 0o755, oct(os.stat(run).st_mode & 0o777))
with open(run, encoding="utf-8") as f:
    body = f.read()
check("run 里执行 guard.py", "exec python3 guard.py" in body, body)
check("run 里 cd 到代码目录", f"cd {ROOT}" in body, body)
check("内容没变时返回 False", guard.ensure_service() is False)
check("service_installed 认得出", guard.service_installed() is True)


print("[5] 单实例锁")
guard.LOCK_FILE = os.path.join(TMP, "guard.lock")
f1 = guard._lock()
check("第一次拿到锁", f1 is not None)
f2 = guard._lock()
check("第二次抢不到", f2 is None)
if f1:
    f1.close()
f3 = guard._lock()
check("释放后能再拿", f3 is not None)
if f3:
    f3.close()


print("[6] 退避与状态快照")
check("退避序列单调不减", all(guard.BACKOFF[i] <= guard.BACKOFF[i + 1]
                              for i in range(len(guard.BACKOFF) - 1)))
check("最长退避 5 分钟", guard.BACKOFF[-1] == 600)
guard.heartbeat()
with open(guard.PID_FILE, "w") as f:
    f.write(str(os.getpid()))
guard.alive = lambda pid: True
guard.cmdline = lambda pid: "python3 guard.py"
snap = guard.status()
check("status 有 running", snap.get("running") is True, snap)
check("status 有 healthy", snap.get("healthy") is True, snap)
check("status 带心跳年龄", isinstance(snap.get("heartbeat_age"), int), snap)
check("status 带 restarts", isinstance(snap.get("restarts"), int), snap)
check("status 带 interval/fail_limit",
      snap.get("interval") == guard.INTERVAL and snap.get("fail_limit") == guard.FAIL_LIMIT)

srv.shutdown()
print(f"\n结果: {ok} 项通过, {len(fails)} 项失败")
if fails:
    print("失败项: " + ", ".join(fails))
sys.exit(1 if fails else 0)
