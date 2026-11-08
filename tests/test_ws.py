#!/usr/bin/env python3
"""WebSocket 通道协议级测试（手写 RFC 6455 客户端，不依赖第三方库）。

    PP_BASE=http://127.0.0.1:8888 PP_PASS=密码 python3 tests/test_ws.py
"""
import base64
import json
import os
import socket
import struct
import sys
import time

import requests

BASE = os.environ.get("PP_BASE", "http://127.0.0.1:8888")
PW = os.environ.get("PP_PASS", "")
HOST = BASE.split("//", 1)[1]
HOSTNAME, _, PORTSTR = HOST.partition(":")
PORT = int(PORTSTR or 80)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if cond:
        ok += 1
    else:
        fails.append(name)


class WS:
    def __init__(self, sock, head, leftover):
        self.s = sock
        self.head = head
        self.buf = leftover

    def _read(self, n, timeout=8):
        self.s.settimeout(timeout)
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout=8):
        b1, b2 = self._read(2, timeout)
        fin = bool(b1 & 0x80)
        op = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if b2 & 0x80 else b""
        data = self._read(ln) if ln else b""
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return fin, op, data

    def send(self, op, data=b""):
        if isinstance(data, str):
            data = data.encode()
        n = len(data)
        head = bytearray([0x80 | op])
        mask = os.urandom(4)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.s.sendall(bytes(head) + masked)

    def send_json(self, obj):
        self.send(0x1, json.dumps(obj))

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def handshake(path, cookie="", origin=None, key=None, version="13"):
    s = socket.create_connection((HOSTNAME, PORT), timeout=8)
    key = key or base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: {HOSTNAME}:{PORT}",
             "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", f"Sec-WebSocket-Version: {version}"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    if origin:
        lines.append(f"Origin: {origin}")
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = s.recv(4096)
        if not chunk:
            break
        raw += chunk
    head, _, leftover = raw.partition(b"\r\n\r\n")
    return s, head.decode("latin1"), leftover, key


def pack_in(text):
    return {"t": "in", "d": base64.b64encode(text.encode()).decode()}


def collect(ws, seconds=2.5, stop_on=None):
    """收若干帧，拼出文本输出。"""
    out, exit_code, deadline = "", None, time.time() + seconds
    while time.time() < deadline:
        try:
            fin, op, data = ws.recv(timeout=max(0.2, deadline - time.time()))
        except (socket.timeout, TimeoutError):
            break
        except ConnectionError:
            break
        if op == 0x8:
            break
        if op == 0x9:
            ws.send(0xA, data)
            continue
        if op != 0x1:
            continue
        msg = json.loads(data)
        if msg.get("t") == "out":
            if msg.get("data"):
                out += base64.b64decode(msg["data"]).decode("utf-8", "replace")
            if msg.get("alive") is False:
                exit_code = msg.get("exit")
        elif msg.get("t") == "exit":
            exit_code = msg.get("code")
            break
        if stop_on and stop_on in out:
            break
    return out, exit_code


def main():
    s = requests.Session()
    r = s.post(BASE + "/api/login", json={"username": "admin", "password": PW})
    if not r.ok:
        print("登录失败：", r.text)
        sys.exit(1)
    # requests 的 RequestsCookieJar.get(name) 直接返回字符串值
    val = s.cookies.get("pp_token")
    cookie = f"pp_token={val}" if val else ""
    check("取到登录 Cookie", cookie.startswith("pp_token="), cookie[:16])

    for t in s.get(BASE + "/api/term/list").json()["sessions"]:
        s.delete(BASE + "/api/term/" + t["id"])
    sid = s.post(BASE + "/api/term/new",
                 json={"kind": "local", "shell": "bash", "cols": 90, "rows": 26}).json()["id"]

    # --- 握手 ---
    sock, head, leftover, key = handshake(f"/api/term/{sid}/ws", cookie)
    first = head.splitlines()[0]
    check("101 升级成功", "101" in first, first)
    expect = base64.b64encode(
        __import__("hashlib").sha1((key + GUID).encode()).digest()).decode()
    check("Sec-WebSocket-Accept 正确", expect in head, head)
    check("升级头完整", "Upgrade: websocket" in head and "Connection: Upgrade" in head)
    ws = WS(sock, head, leftover)

    out, _ = collect(ws, 2.5)
    check("首帧为会话全量输出", "~ $" in out or len(out) > 0, repr(out[-80:]))

    # --- 输入 ---
    ws.send_json(pack_in("echo WS_$((20+3))\n"))
    out, _ = collect(ws, 3, stop_on="WS_23")
    check("WS 通道执行命令", "WS_23" in out, repr(out[-120:]))

    # --- resize ---
    ws.send_json({"t": "resize", "cols": 111, "rows": 33})
    time.sleep(0.3)
    ws.send_json(pack_in("stty size\n"))
    out, _ = collect(ws, 3, stop_on="33 111")
    check("resize 走 WS 生效", "33 111" in out, repr(out[-100:]))

    # --- 应用层 ping ---
    ws.send_json({"t": "ping"})
    pong = False
    for _ in range(6):
        try:
            fin, op, data = ws.recv(timeout=3)
        except (socket.timeout, TimeoutError, ConnectionError):
            break
        if op == 0xA:
            pong = True
            break
        if op == 0x1:
            m = json.loads(data)
            if m.get("t") == "pong":
                pong = True
                break
    check("应用层 ping/pong", pong)

    # --- 协议层 ping ---
    ws.send(0x9, b"hb")
    got = False
    for _ in range(6):
        try:
            fin, op, data = ws.recv(timeout=3)
        except (socket.timeout, TimeoutError, ConnectionError):
            break
        if op == 0xA and data == b"hb":
            got = True
            break
    check("协议层 ping 回 pong", got)

    # --- 分片消息 ---
    payload = json.dumps(pack_in("echo FRAG_OK\n")).encode()
    mid = len(payload) // 2
    for fin, op, chunk in ((0, 0x1, payload[:mid]), (1, 0x0, payload[mid:])):
        n = len(chunk)
        mask = os.urandom(4)
        head_b = bytearray([(0x80 if fin else 0) | op, 0x80 | n])
        head_b += mask
        ws.s.sendall(bytes(head_b) + bytes(b ^ mask[i & 3] for i, b in enumerate(chunk)))
    out, _ = collect(ws, 3, stop_on="FRAG_OK")
    check("分片消息可重组", "FRAG_OK" in out, repr(out[-100:]))

    # --- close ---
    ws.send(0x8, struct.pack(">H", 1000))
    closed = False
    for _ in range(5):
        try:
            fin, op, data = ws.recv(timeout=2)
        except (socket.timeout, TimeoutError, ConnectionError):
            break
        if op == 0x8:
            closed = True
            break
    check("收到 close 帧", closed)
    ws.close()

    # --- 鉴权与来源校验 ---
    sock2, head2, _, _ = handshake(f"/api/term/{sid}/ws")
    check("未登录握手被拒", "401" in head2.splitlines()[0], head2.splitlines()[0])
    sock2.close()

    sock3, head3, _, _ = handshake(f"/api/term/{sid}/ws", cookie,
                                   origin="http://evil.example.com")
    check("跨站 Origin 被拒", "403" in head3.splitlines()[0], head3.splitlines()[0])
    sock3.close()

    sock4, head4, _, _ = handshake(f"/api/term/{sid}/ws", cookie,
                                   origin=f"http://{HOSTNAME}:{PORT}")
    check("同源 Origin 放行", "101" in head4.splitlines()[0], head4.splitlines()[0])
    sock4.close()

    sock5, head5, _, _ = handshake("/api/term/nosuch/ws", cookie)
    check("不存在的会话返回 404", "404" in head5.splitlines()[0], head5.splitlines()[0])
    sock5.close()

    sock6, head6, _, _ = handshake("/api/whatever", cookie)
    check("非终端路径返回 404", "404" in head6.splitlines()[0], head6.splitlines()[0])
    sock6.close()

    # --- 会话结束后推送 exit ---
    sock7, head7, leftover7, _ = handshake(f"/api/term/{sid}/ws", cookie)
    ws7 = WS(sock7, head7, leftover7)
    collect(ws7, 1.0)
    ws7.send_json(pack_in("exit\n"))
    out, code = collect(ws7, 4)
    check("shell 退出推送 exit 事件", code is not None, repr(out[-80:]) + " code=" + str(code))
    ws7.close()

    for t in s.get(BASE + "/api/term/list").json()["sessions"]:
        s.delete(BASE + "/api/term/" + t["id"])

    print(f"\n{ok} 项通过，{len(fails)} 项失败")
    if fails:
        print("失败项：", fails)


if __name__ == "__main__":
    main()
