#!/usr/bin/env python3
"""会话持久化验收（纯本地，不联网）。

重点验证三件事：
  1. 重启面板后，原来的 cookie 还能用（会话落盘了）；
  2. 用着就自动续期，不会第 91 天准时掉线；
  3. 落盘文件里没有明文 token，权限 600。
"""
import json
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if cond:
        ok += 1
    else:
        fails.append(name)


TMP = tempfile.mkdtemp(prefix="pp-sess-")
core.DATA = TMP


def reset():
    with core.SESS_LOCK:
        core.SESSIONS.clear()
        core._SESS_HASHED.clear()
    try:
        os.remove(core._sess_path())
    except OSError:
        pass


def reboot():
    """模拟进程重启：内存清空，从磁盘恢复。"""
    with core.SESS_LOCK:
        core.SESSIONS.clear()
        core._SESS_HASHED.clear()
    return core.sessions_init()


def read_file():
    with open(core._sess_path(), encoding="utf-8") as f:
        return f.read()


print("[1] 签发与校验")
reset()
tok = core.new_session("admin")
check("token 是 48 位 hex", len(tok) == 48 and all(c in "0123456789abcdef" for c in tok), tok)
check("刚签发就能通过", core.check_session(tok) is True)
check("空 token 不通过", core.check_session("") is False)
check("乱写的 token 不通过", core.check_session("f" * 48) is False)
check("会话文件已生成", os.path.exists(core._sess_path()))

print("\n[2] 落盘安全")
mode = stat.S_IMODE(os.stat(core._sess_path()).st_mode)
check("文件权限 0600", mode == 0o600, oct(mode))
raw = read_file()
check("文件里没有明文 token", tok not in raw)
check("文件里存的是 sha256", core._tok_key(tok) in raw)
d = json.loads(raw)
check("结构正确", d.get("version") == 1 and len(d.get("sessions") or {}) == 1)
check("记录含 user/exp", "user" in list(d["sessions"].values())[0])

print("\n[3] 重启后仍然有效")
n = reboot()
check("恢复了 1 个会话", n == 1, n)
check("重启后旧 token 通过", core.check_session(tok) is True)
check("重启后内存里仍无明文", tok not in core.SESSIONS)

print("\n[4] 滑动续期")
now = time.time()
key = core._tok_key(tok)
with core.SESS_LOCK:
    core._SESS_HASHED[key]["exp"] = now + 86400       # 只剩 1 天
before = core._SESS_HASHED[key]["exp"]
core.check_session(tok)
after = core._SESS_HASHED[key]["exp"]
check("剩余不足 30 天时续期", after > before + core.SESS_RENEW_AT, (before, after))
check("续到 90 天后", abs(after - (now + core.SESS_TTL)) < 5, after - now)

with core.SESS_LOCK:
    core._SESS_HASHED[key]["exp"] = now + core.SESS_RENEW_AT + 86400   # 还剩 31 天
mid = core._SESS_HASHED[key]["exp"]
core.check_session(tok)
check("剩得多就不写盘", core._SESS_HASHED[key]["exp"] == mid)
check("expiry 接口给的是未来时间", core.session_expiry(tok) > time.time())

print("\n[5] 过期与清理")
with core.SESS_LOCK:
    core._SESS_HASHED[key]["exp"] = time.time() - 10
check("过期后不通过", core.check_session(tok) is False)
check("过期记录从内存删除", key not in core._SESS_HASHED)
check("过期记录从文件删除", key not in (json.loads(read_file())["sessions"]))

print("\n[6] 登出")
reset()
t1 = core.new_session("admin")
t2 = core.new_session("admin")
core.drop_session(t1)
check("登出的 token 失效", core.check_session(t1) is False)
check("另一台设备不受影响", core.check_session(t2) is True)
reboot()
check("登出状态能扛住重启", core.check_session(t1) is False)
check("另一台设备重启后还在", core.check_session(t2) is True)

print("\n[7] 改密码：全部作废")
core.drop_all_sessions()
check("内存清空", len(core.SESSIONS) == 0 and len(core._SESS_HASHED) == 0)
check("文件清空", json.loads(read_file())["sessions"] == {})
reboot()
check("重启后也是空的", core.check_session(t2) is False)

print("\n[8] 多处登录互不干扰")
a = core.new_session("admin")
b = core.new_session("admin")
c = core.new_session("admin")
check("三个都有效", all(core.check_session(x) for x in (a, b, c)))
core.drop_session(b)
check("删中间那个", (core.check_session(a), core.check_session(b), core.check_session(c)) == (True, False, True))
check("文件里剩两个", len(json.loads(read_file())["sessions"]) == 2)

print(f"\n{ok} 项通过，{len(fails)} 项失败")
if fails:
    print("失败项：", fails)
sys.exit(1 if fails else 0)
