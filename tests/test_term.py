#!/usr/bin/env python3
"""PTY 终端 + SSH 全链路验收脚本。

在能访问面板的机器上跑（本机端口 / SSH 隧道都可以）：

    PP_BASE=http://127.0.0.1:8888 PP_PASS=你的密码 python3 tests/test_term.py

注意 SSH 那几项要连本机 sshd（默认 127.0.0.1:8022），密码写死在这里只用于自测，
换了环境请改 SSHD_PASS 或跳过该段。
"""
import base64
import os
import time

import requests

BASE = os.environ.get("PP_BASE", "http://127.0.0.1:8888")
PW = os.environ.get("PP_PASS", "")
SSHD_USER = os.environ.get("SSHD_USER", "u0_a000")
SSHD_PASS = os.environ.get("SSHD_PASS", "")
SSHD_PORT = int(os.environ.get("SSHD_PORT", "8022"))
ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if cond:
        ok += 1
    else:
        fails.append(name)


def rd(s, sid, seq, wait=4):
    j = s.get(f"{BASE}/api/term/{sid}/output", params={"since": seq, "wait": wait}).json()
    j["raw"] = base64.b64decode(j["data"]).decode("utf-8", "replace") if j["data"] else ""
    return j


def main():
    s = requests.Session()
    check("未登录访问被拒", requests.get(BASE + "/api/term/list").status_code == 401)
    r = s.post(BASE + "/api/login", json={"username": "admin", "password": PW})
    check("登录", r.ok, r.text)

    for t in s.get(BASE + "/api/term/list").json()["sessions"]:
        s.delete(BASE + "/api/term/" + t["id"])

    # --- bash ---
    r = s.post(BASE + "/api/term/new", json={"kind": "local", "shell": "bash", "cols": 100, "rows": 30})
    check("建 bash 会话", r.ok, r.text)
    sid = r.json()["id"]
    a = rd(s, sid, 0, 8)
    check("bash 有提示符", a["seq"] > 0 and a["alive"], a["raw"][-80:])

    def inp(t, tid=None):
        tid = tid or sid
        return s.post(f"{BASE}/api/term/{tid}/input",
                      json={"data": base64.b64encode(t.encode()).decode()})

    inp("echo V1_$((6*7)); tty; stty size\n")
    time.sleep(1.2)
    b = rd(s, sid, a["seq"])
    check("命令执行", "V1_42" in b["raw"], b["raw"][-100:])
    check("分配了真实 pty", "/dev/pts/" in b["raw"])
    check("初始尺寸生效", "30 100" in b["raw"], b["raw"][-80:])

    s.post(f"{BASE}/api/term/{sid}/resize", json={"cols": 120, "rows": 44})
    inp("stty size\n")
    time.sleep(0.9)
    c = rd(s, sid, b["seq"])
    check("resize 生效", "44 120" in c["raw"], c["raw"][-60:])

    inp("sleep 30\n")
    time.sleep(0.7)
    inp("\x03")
    time.sleep(1.2)
    d = rd(s, sid, c["seq"])
    check("Ctrl+C 打断前台进程", "^C" in d["raw"])
    inp("echo AFTER\n")
    time.sleep(0.9)
    e = rd(s, sid, d["seq"])
    check("Ctrl+C 后 shell 恢复", "AFTER" in e["raw"], e["raw"][-80:])

    inp("echo HIST_A\n")
    time.sleep(0.8)
    f = rd(s, sid, e["seq"])
    inp("\x1b[A")
    time.sleep(0.7)
    g = rd(s, sid, f["seq"])
    check("方向键历史", "echo HIST_A" in g["raw"], g["raw"][-60:])
    inp("\x03")
    time.sleep(0.4)
    rd(s, sid, g["seq"])

    # --- sh ---
    r = s.post(BASE + "/api/term/new", json={"kind": "local", "shell": "sh", "cols": 80, "rows": 24})
    sid2 = r.json()["id"]
    h = rd(s, sid2, 0, 8)
    inp("echo SHOK; ps -o comm= -p $$\n", sid2)
    time.sleep(1.0)
    i = rd(s, sid2, h["seq"])
    check("sh 会话可用", "SHOK" in i["raw"] and "/bin/sh" in i["raw"], i["raw"][-90:])

    # --- 自定义命令 ---
    r = s.post(BASE + "/api/term/new", json={"kind": "local", "cmd": "printf 'CUSTOM_OK\\n'; sleep 0.2"})
    check("自定义命令会话", r.ok, r.text)
    sid3 = r.json()["id"]
    j = rd(s, sid3, 0, 5)
    check("自定义命令输出", "CUSTOM_OK" in j["raw"], j["raw"][-60:])

    # --- 参数校验 ---
    check("拒绝非法主机", s.post(BASE + "/api/term/new",
          json={"kind": "ssh", "host": "a b;ls", "user": "root", "password": "x"}).status_code == 400)
    check("拒绝未知 shell", s.post(BASE + "/api/term/new",
          json={"kind": "local", "shell": "zsh"}).status_code == 400)
    check("密码为空被拒", s.post(BASE + "/api/term/new",
          json={"kind": "ssh", "host": "1.2.3.4", "user": "root", "auth": "password"}).status_code == 400)

    # --- SSH 密码登录本机 ---
    r = s.post(BASE + "/api/term/new", json={"kind": "ssh", "host": "127.0.0.1", "port": SSHD_PORT,
                                             "user": SSHD_USER, "auth": "password",
                                             "password": SSHD_PASS, "cols": 90, "rows": 26})
    check("建 SSH 会话", r.ok, r.text)
    sid4 = r.json()["id"]
    k = rd(s, sid4, 0, 15)
    check("SSH 登录成功", k["alive"] and "Termux" in k["raw"], k["raw"][-120:])
    if k["alive"]:
        s.post(f"{BASE}/api/term/{sid4}/input",
               json={"data": base64.b64encode(b"echo SSH_V_$((3*9))\n").decode()})
        time.sleep(2)
        m = rd(s, sid4, k["seq"], 8)
        check("SSH 内执行命令", "SSH_V_27" in m["raw"], m["raw"][-120:])
    else:
        check("SSH 内执行命令", False, "会话未存活")

    # --- SSH 错误密码 ---
    r = s.post(BASE + "/api/term/new", json={"kind": "ssh", "host": "127.0.0.1", "port": SSHD_PORT,
                                             "user": SSHD_USER, "auth": "password",
                                             "password": "definitely-wrong"})
    sid5 = r.json()["id"]
    dead = False
    for _ in range(8):
        n = rd(s, sid5, 0, 6)
        if not n["alive"]:
            dead = True
            check("错误密码给出明确错误", "Permission denied" in n["raw"], n["raw"][-100:])
            check("错误密码退出码非 0", n["exit"] not in (0, None), n["exit"])
            break
    check("错误密码会话终止", dead)

    # --- 主机簿 ---
    for h in s.get(BASE + "/api/ssh/hosts").json()["hosts"]:
        s.post(BASE + "/api/ssh/hosts/delete", json={"id": h["id"]})
    for _ in range(2):
        s.post(BASE + "/api/ssh/hosts", json={"name": "验收机", "host": "10.1.2.3",
                                              "port": 22, "user": "root", "auth": "password"})
    hs = s.get(BASE + "/api/ssh/hosts").json()["hosts"]
    check("主机簿去重", len(hs) == 1, [h["name"] for h in hs])
    check("主机簿不含密码", "password" not in hs[0], hs[0].keys())
    s.post(BASE + "/api/ssh/hosts/delete", json={"id": hs[0]["id"]})
    check("主机簿删除", len(s.get(BASE + "/api/ssh/hosts").json()["hosts"]) == 0)

    # --- 会话管理 ---
    lst = s.get(BASE + "/api/term/list").json()
    check("会话列表", len(lst["sessions"]) >= 4, len(lst["sessions"]))
    check("关闭会话", s.delete(BASE + "/api/term/" + sid3).ok)
    check("关闭后不在列表", all(x["id"] != sid3 for x in
                                s.get(BASE + "/api/term/list").json()["sessions"]))
    check("关闭不存在的会话返回 false",
          s.delete(BASE + "/api/term/nope").json().get("ok") is False)

    # sh 已退出（没发 exit，这里手动关）
    for x in (sid, sid2, sid4):
        s.delete(BASE + "/api/term/" + x)

    print(f"\n{ok} 项通过，{len(fails)} 项失败")
    if fails:
        print("失败项：", fails)


if __name__ == "__main__":
    main()
