#!/usr/bin/env python3.11
"""监控告警（M09）单元测试。

不连真 SMTP：本地起一个手写的最小 SMTP 服务器（PLAIN / LOGIN 两种认证、
MAIL / RCPT / DATA 全流程），断言真实发出的邮件字节。
不碰真设备：网络、电量、隧道、容器、任务全部用假的采集函数注入。

直接跑：python3 tests/test_alerts.py
"""
import base64
import email
import email.policy
import http.server
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts  # noqa: E402
import core  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


# ---------------- 最小 SMTP 服务器 ----------------

class _SMTPHandler(socketserver.StreamRequestHandler):
    timeout = 20

    def _send(self, line):
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        srv = self.server
        self._send("220 fake.smtp ESMTP ready")
        mail_from, rcpts, chunks = "", [], []
        try:
            while True:
                raw = self.rfile.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                up = line.upper()
                if up.startswith("EHLO") or up.startswith("HELO"):
                    self._send("250-fake.smtp greets you")
                    self._send("250-AUTH PLAIN LOGIN")
                    self._send("250-SIZE 10485760")
                    self._send("250 8BITMIME")
                elif up.startswith("AUTH PLAIN"):
                    rest = line[len("AUTH PLAIN"):].strip()
                    if rest:
                        blob = base64.b64decode(rest)
                    else:
                        self._send("334 ")
                        blob = base64.b64decode(self.rfile.readline().strip())
                    parts = blob.split(b"\x00")
                    user = parts[-2].decode() if len(parts) >= 2 else ""
                    pwd = parts[-1].decode() if parts else ""
                    if user == srv.user and pwd == srv.pwd:
                        self._send("235 2.7.0 Authentication successful")
                    else:
                        self._send("535 5.7.8 Authentication failed")
                elif up.startswith("AUTH LOGIN"):
                    self._send("334 VXNlcm5hbWU6")
                    user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self._send("334 UGFzc3dvcmQ6")
                    pwd = base64.b64decode(self.rfile.readline().strip()).decode()
                    if user == srv.user and pwd == srv.pwd:
                        self._send("235 2.7.0 Authentication successful")
                    else:
                        self._send("535 5.7.8 Authentication failed")
                elif up.startswith("MAIL FROM"):
                    mail_from = line[10:].strip()
                    self._send("250 2.1.0 Ok")
                elif up.startswith("RCPT TO"):
                    rcpts.append(line[8:].strip())
                    self._send("250 2.1.5 Ok")
                elif up.startswith("DATA"):
                    self._send("354 End data with <CR><LF>.<CR><LF>")
                    body = []
                    while True:
                        d = self.rfile.readline()
                        if not d:
                            break
                        if d in (b".\r\n", b".\n"):
                            break
                        body.append(d)
                    chunks = b"".join(body)
                    srv.mails.append({"from": mail_from, "rcpt": list(rcpts),
                                      "raw": chunks})
                    self._send("250 2.0.0 Ok: queued")
                elif up.startswith("RSET"):
                    mail_from, rcpts, chunks = "", [], b""
                    self._send("250 2.0.0 Ok")
                elif up.startswith("NOOP"):
                    self._send("250 2.0.0 Ok")
                elif up.startswith("QUIT"):
                    self._send("221 2.0.0 Bye")
                    break
                else:
                    self._send("250 2.0.0 Ok")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user="u@test.local", pwd="secret"):
        self.user, self.pwd = user, pwd
        self.mails = []
        super().__init__(("127.0.0.1", 0), _SMTPHandler)

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


# ---------------- 假 HTTP 端点 ----------------

class _Page(http.server.BaseHTTPRequestHandler):
    codes = {}          # path -> 状态码

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        code = self.codes.get(self.path, 200)
        body = b"ok" if code == 200 else b"bad"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET


def start_http(codes):
    handler = type("H", (_Page,), {"codes": codes})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------- 环境 ----------------

TMP = tempfile.mkdtemp(prefix="pp-alerts-")
core.DATA = TMP
ORIG_AUTO = alerts._auto_items


def fresh(cfg=None, smtp=None):
    """每个小节重置内存状态与磁盘。"""
    alerts.reset_all_for_tests()
    alerts._auto_items = lambda c: []
    for f in ("alerts.json", "alert_events.json", "secret.key"):
        try:
            os.unlink(os.path.join(TMP, f))
        except OSError:
            pass
    if cfg:
        alerts.save_settings(cfg)
    if smtp:
        alerts.save_smtp(smtp)


def item(kind="url", name="测试项", target="http://127.0.0.1:1/", **kw):
    it = alerts._new_item("t1", name, kind, target, manual=True)
    it.update(kw)
    return it


def drain_mail(timeout=6):
    """等发信队列清空。"""
    end = time.time() + timeout
    while time.time() < end:
        if alerts._queue.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return False


# ================= 1. 配置与加密 =================

print("[1] SMTP 配置与加密")
fresh()
alerts.save_smtp({"host": "smtp.qq.com", "port": 465, "security": "ssl",
                  "user": "someone@example.com", "to": "a@x.com, b@y.com",
                  "cc": ["c@z.com"], "password": "abcd1234efgh5678"})
v = alerts.smtp_view()
check("服务器写入", v["host"] == "smtp.qq.com" and v["port"] == 465)
check("加密方式写入", v["security"] == "ssl")
check("收件人支持逗号串", v["to"] == ["a@x.com", "b@y.com"], v["to"])
check("抄送写入", v["cc"] == ["c@z.com"])
check("发件人自动回填", v["sender"] == "someone@example.com")
check("授权码只回掩码", "abcd1234efgh5678" not in json.dumps(v, ensure_ascii=False))
check("掩码保留尾 4 位", v["password_masked"].endswith("5678"), v["password_masked"])
check("has_password 为真", v["has_password"] is True)
check("configured 判定", v["configured"] is True)

raw = open(os.path.join(TMP, "alerts.json"), encoding="utf-8").read()
check("授权码不落明文", "abcd1234efgh5678" not in raw)
check("落盘字段是密文", "pwd_enc" in raw)
check("secret.key 已生成", os.path.exists(os.path.join(TMP, "secret.key")))
mode = oct(os.stat(os.path.join(TMP, "secret.key")).st_mode & 0o777)
check("secret.key 权限 0600", mode == "0o600", mode)
check("配置文件权限 0600",
      oct(os.stat(os.path.join(TMP, "alerts.json")).st_mode & 0o777) == "0o600")

alerts.save_smtp({"host": "smtp.qq.com", "password": ""})
check("空授权码不覆盖", alerts.smtp_view()["has_password"] is True)

try:
    alerts.save_smtp({"port": 99999})
    check("端口越界被拒", False)
except ValueError as e:
    check("端口越界被拒", "65535" in str(e), str(e))

try:
    alerts.save_smtp({"to": ["not-an-email"]})
    check("非法邮箱被拒", False)
except ValueError as e:
    check("非法邮箱被拒", "不合法" in str(e), str(e))

# ================= 2. 邮件模板 =================

print("[2] 邮件模板")
h, t = alerts.render_mail({
    "kind": "alert", "title": "网站 持续异常",
    "rows": [("告警对象", "网站"), ("类型", "URL 拨测"),
             ("目标", "https://example.com"), ("故障原因", "HTTP 502")],
    "note": "已连续 3 次失败", "panel": "http://192.168.1.7:8888",
    "ts": 1793412000, "event_id": "abc123"})
check("含对象名", "网站" in h)
check("含目标 URL", "https://example.com" in h)
check("含故障原因", "HTTP 502" in h)
check("含面板链接", "192.168.1.7:8888" in h)
check("含事件编号", "abc123" in h)
check("声明 utf-8", 'charset="utf-8"' in h)
check("内联样式（不靠 style 标签）", "<style" not in h and "style=" in h)
check("600px 卡片宽度", "max-width:600px" in h)
check("纯文本备选含标题", "持续异常" in t and "HTTP 502" in t)

h2, _ = alerts.render_mail({"kind": "recover", "title": "x 已恢复",
                            "rows": [("恢复时间", "2026-10-31 10:00:00")]})
check("恢复模板用绿色", "#30a46c" in h2)
h3, _ = alerts.render_mail({"kind": "test", "title": "测试"})
check("测试模板用蓝色", "#3b82f6" in h3)
hs, _ = alerts.render_mail({"kind": "alert", "title": "<script>x</script>",
                            "rows": [("目标", "a&b")]})
check("HTML 转义", "<script>" not in hs and "&lt;script&gt;" in hs and "a&amp;b" in hs)

# ================= 3. SMTP 发送 =================

print("[3] SMTP 发送")
smtp = FakeSMTP(user="u@test.local", pwd="goodpass").start()
fresh(smtp={"host": "127.0.0.1", "port": smtp.port, "security": "none",
            "user": "u@test.local", "sender": "u@test.local",
            "to": ["a@x.com", "b@y.com"], "cc": ["c@z.com"],
            "password": "goodpass"})
ok, msg = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e1"))
check("发送成功", ok is True, msg)
check("服务器收到 1 封", len(smtp.mails) == 1, len(smtp.mails))
if smtp.mails:
    m = smtp.mails[0]
    check("收件人含 to + cc", len(m["rcpt"]) == 3, m["rcpt"])
    parsed = email.message_from_bytes(m["raw"], policy=email.policy.default)
    subj = str(parsed["Subject"])
    check("主题是 UTF-8 中文", "测试邮件" in subj, subj)
    check("From 带 PocketPanel 名", "PocketPanel" in str(parsed["From"]))
    parts = [p.get_content_type() for p in parsed.walk()]
    check("多部分邮件含 text/plain", "text/plain" in parts, parts)
    check("多部分邮件含 text/html", "text/html" in parts, parts)
    html_part = [p for p in parsed.walk() if p.get_content_type() == "text/html"][0]
    hb = html_part.get_payload(decode=True)
    check("HTML 中文不乱码", "监控告警" in hb.decode("utf-8"), hb[:80])
    check("HTML 声明 charset", html_part.get_content_charset() == "utf-8",
          html_part.get_content_charset())

# 认证失败
ok2, msg2 = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e2"))
check("正确授权码可重复发送", ok2 is True, msg2)

smtp.pwd = "other"
ok3, msg3 = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e3"))
check("授权码错误被准确识别", ok3 is False and "认证失败" in msg3, msg3)
check("提示里给出授权码指引", "授权码" in msg3 and "SMTP" in msg3, msg3)

# 连接被拒
fresh(smtp={"host": "127.0.0.1", "port": 1, "security": "none",
            "user": "u@test.local", "to": ["a@x.com"], "password": "x"})
ok4, msg4 = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e4"))
check("连接失败给出可读原因", ok4 is False and ("拒绝" in msg4 or "超时" in msg4), msg4)

# 域名不存在
fresh(smtp={"host": "no-such-host.invalid", "port": 465, "security": "ssl",
            "user": "u@x.com", "to": ["a@x.com"], "password": "x"})
ok5, msg5 = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e5"))
check("域名解析失败有提示", ok5 is False and ("解析" in msg5 or "网络" in msg5), msg5)

# 缺配置
fresh()
ok6, msg6 = alerts._send_now(alerts._mail_ctx(
    "test", {"name": "t", "kind": "url", "target": "x"}, "", int(time.time()), "e6"))
check("未配置时明确拒绝", ok6 is False and "未配置" in msg6, msg6)

smtp.shutdown()

# ================= 4. 状态机 =================

print("[4] 状态机与防抖")
fresh(cfg={"fail_threshold": 3, "silence_min": 30}, smtp={
    "host": "127.0.0.1", "port": 1, "security": "none",
    "user": "u@x.com", "to": ["a@x.com"], "password": "x"})
cfg = alerts.state()
it = item()
now = 1793412000
r1 = alerts._evaluate(it, False, "挂了", now, cfg)
check("第 1 次失败 → retrying", r1 == "retrying" and it["state"] == "retrying", r1)
r2 = alerts._evaluate(it, False, "挂了", now + 60, cfg)
check("第 2 次失败仍 retrying", r2 == "retrying" and it["fails"] == 2, (r2, it["fails"]))
r3 = alerts._evaluate(it, False, "挂了", now + 120, cfg)
check("第 3 次失败 → alerting", r3 == "alert" and it["state"] == "alerting", (r3, it["state"]))
check("告警计数 +1", it["alert_count"] == 1, it["alert_count"])
check("静默期 30 分钟", it["silence_until"] == now + 120 + 1800, it["silence_until"])
r4 = alerts._evaluate(it, False, "还挂着", now + 180, cfg)
check("静默期内不重复发信", r4 == "silenced", r4)
r5 = alerts._evaluate(it, False, "还挂着", now + 120 + 1801, cfg)
check("静默期过后再提醒", r5 == "alert", r5)
check("告警累计 2 次", it["alert_count"] == 2, it["alert_count"])

r6 = alerts._evaluate(it, True, "", now + 120 + 1900, cfg)
check("恢复 → recovered", r6 == "recover" and it["state"] == "recovered", (r6, it["state"]))
check("恢复计数 +1", it["recover_count"] == 1)
check("失败计数清零", it["fails"] == 0)
r7 = alerts._evaluate(it, True, "", now + 1960, cfg)
check("继续正常 → healthy", r7 == "healthy" and it["state"] == "healthy", r7)

it2 = item(name="二次")
it2["state"] = "recovered"
alerts._evaluate(it2, False, "又挂了", now + 2000, cfg)
check("恢复后再次失败重新累计", it2["state"] == "retrying" and it2["fails"] == 1,
      (it2["state"], it2["fails"]))

it3 = item(name="跳过")
alerts._evaluate(it3, None, "", now, cfg)
check("ok=None 不参与判定", it3["state"] == "healthy" and it3["fails"] == 0)

# 告警中的对象被用户手动停掉（容器 stop / 隧道 stop）→ 告警失去意义，安静闭环
it5 = item(name="告警后停用")
it5["state"] = "alerting"
r8 = alerts._evaluate(it5, None, "", now + 3000, cfg)
check("告警中变不适用 → 安静闭环", r8 == "closed" and it5["state"] == "recovered",
      (r8, it5["state"]))
check("闭环后清掉失败计数与旧错误", it5["fails"] == 0 and it5["last_error"] == "",
      (it5["fails"], it5["last_error"]))

it6 = item(name="已恢复但残留失败")
it6["state"] = "recovered"
it6["fails"] = 4
it6["last_error"] = "旧错误"
r9 = alerts._evaluate(it6, None, "", now + 3100, cfg)
check("跳过时清掉残留失败计数", r9 == "skip" and it6["fails"] == 0
      and it6["last_error"] == "", (r9, it6["fails"], it6["last_error"]))

# 事件型去重（计划任务同一笔失败只告警一次）
fresh(cfg={"fail_threshold": 1, "silence_min": 0})
cfg = alerts.state()
it4 = item(kind="schedule", name="任务")
for i in range(4):
    alerts._evaluate(it4, False, "失败", now + i * 60, cfg, {"run_id": "run-A"})
check("同一 run 只告警一次", it4["alert_count"] == 1, it4["alert_count"])
check("去重后仍在 alerting", it4["state"] == "alerting")
alerts._evaluate(it4, False, "失败", now + 300, cfg, {"run_id": "run-B"})
check("新的 run 重新告警", it4["alert_count"] == 2, it4["alert_count"])

# ================= 5. URL 探活 =================

print("[5] URL 拨测")
srv, port = start_http({"/ok": 200, "/bad": 500, "/403": 403})
fresh()
check("200 命中", alerts._check_url({"target": f"http://127.0.0.1:{port}/ok",
                                    "expect": 200, "timeout": 5})[0] is True)
ok_, err_, det_ = alerts._check_url({"target": f"http://127.0.0.1:{port}/bad",
                                     "expect": 200, "timeout": 5})
check("500 判失败且报状态码", ok_ is False and "500" in err_, err_)
check("失败也带回状态码", det_.get("status") == 500, det_)
ok_, err_, _ = alerts._check_url({"target": f"http://127.0.0.1:{port}/403",
                                  "expect": 403, "timeout": 5})
check("期望 403 时可判成功", ok_ is True, err_)
ok_, err_, _ = alerts._check_url({"target": f"http://127.0.0.1:{port}/ok",
                                  "expect": 201, "timeout": 5})
check("状态码不符判失败", ok_ is False and "201" in err_, err_)
ok_, err_, _ = alerts._check_url({"target": "http://127.0.0.1:1/x", "timeout": 3})
check("连不上判失败", ok_ is False, err_)
ok_, err_, _ = alerts._check_url({"target": f"http://127.0.0.1:{port}/ok",
                                  "method": "HEAD", "timeout": 5})
check("HEAD 方法可用", ok_ is True, err_)
check("耗时字段存在", isinstance(alerts._check_url(
    {"target": f"http://127.0.0.1:{port}/ok", "timeout": 5})[2].get("ms"), int))
srv.shutdown()

# ================= 6. 端口探活 =================

print("[6] 端口探活")
lsock = socket.socket()
lsock.bind(("127.0.0.1", 0))
lsock.listen(5)
lport = lsock.getsockname()[1]
ok_, err_, _ = alerts._check_port({"host": "127.0.0.1", "port_num": lport, "timeout": 3})
check("监听中的端口判通", ok_ is True, err_)
ok_, err_, _ = alerts._check_port({"host": "127.0.0.1", "port_num": 1, "timeout": 2})
check("未监听端口判失败", ok_ is False and ("拒绝" in err_ or "失败" in err_), err_)
check("端口为空判失败", alerts._check_port({"port_num": 0})[0] is False)
lsock.close()

# ================= 7. 联动采集 =================

print("[7] 系统联动")
fresh(cfg={"rules": {"tunnel": True, "container": True, "schedule": False,
                     "net": False, "battery": False}})
alerts._auto_items = ORIG_AUTO
cfg = alerts.state()

import types  # noqa: E402

fake_tunnel = types.SimpleNamespace(list_instances=lambda: [
    {"id": "t1", "name": "正常隧道", "status": "running", "addr": "frp.a:7000",
     "probe": {"ok": True}},
    {"id": "t2", "name": "死隧道", "status": "failed", "addr": "frp.b:7000",
     "probe": {"ok": None}},
    {"id": "t3", "name": "停着的", "status": "stopped", "addr": "frp.c:7000",
     "probe": {"ok": None}},
    {"id": "t4", "name": "探测失败", "status": "running", "addr": "frp.d:7000",
     "probe": {"ok": False, "detail": "TCP 不可达"}},
])
fake_cf = types.SimpleNamespace(list_tunnels=lambda: [
    {"id": "c1", "name": "CF 正常", "state": "connected", "alive": True,
     "url": "https://a.example.com"},
    {"id": "c2", "name": "CF 挂了", "state": "failed", "alive": False,
     "url": "", "error": "连续重试失败"},
])
fake_rt = types.SimpleNamespace(list_containers=lambda: [
    {"id": "aaa111", "name": "web", "image": "nginx", "state": "running", "exit_code": 0},
    {"id": "bbb222", "name": "db", "image": "mysql", "state": "exited", "exit_code": 137},
    {"id": "ccc333", "name": "job", "image": "busybox", "state": "exited", "exit_code": 0},
    {"id": "ddd444", "name": "stopped", "image": "redis", "state": "exited", "exit_code": 143},
])
sys.modules["tunnel"] = fake_tunnel
sys.modules["cloudflare"] = fake_cf
sys.modules["runtime"] = fake_rt
auto = alerts._auto_items(cfg)
byid = {a[0]: a for a in auto}
check("隧道：running 判健康", byid["auto:tunnel:t1"][4] is True)
check("隧道：failed 判失败", byid["auto:tunnel:t2"][4] is False)
check("隧道：主动停止不判定", byid["auto:tunnel:t3"][4] is None)
check("隧道：拨测失败判失败", byid["auto:tunnel:t4"][4] is False)
check("CF：connected 判健康", byid["auto:cf:c1"][4] is True)
check("CF：failed 判失败", byid["auto:cf:c2"][4] is False)
check("容器：running 判健康", byid["auto:container:aaa111"][4] is True)
check("容器：exit 137 判失败", byid["auto:container:bbb222"][4] is False)
check("容器：exit 0 不判定", byid["auto:container:ccc333"][4] is None)
check("容器：exit 143（主动停止）不判定", byid["auto:container:ddd444"][4] is None)

# 计划任务：以「当前任务」为准，历史里已删除任务的 run 不能生成幽灵监控项
now_ts = int(time.time())
fake_sched = types.SimpleNamespace(listing=lambda: {
    "tasks": [
        {"id": "s1", "name": "备份", "human": "每天 03:00"},
        {"id": "s2", "name": "很久以前失败过", "human": "每天 04:00"},
        {"id": "s3", "name": "从没跑过", "human": "每天 05:00"},
    ],
    "runs": [
        {"id": "run1", "task_id": "s1", "status": "failed", "exit": 1,
         "started": now_ts - 60},
        {"id": "run2", "task_id": "s2", "status": "failed", "exit": 1,
         "started": now_ts - 3 * 86400},
        {"id": "runX", "task_id": "gone", "status": "failed", "exit": 1,
         "started": now_ts - 60, "task_name": "已删除的任务"},
    ],
})
ORIG_SCHED = sys.modules.get("sched")
sys.modules["sched"] = fake_sched
cfg["rules"]["schedule"] = True
auto3 = alerts._auto_items(cfg)
byid3 = {a[0]: a for a in auto3}
check("计划任务：最近失败判失败", byid3["auto:schedule:s1"][4] is False,
      byid3.get("auto:schedule:s1"))
check("计划任务：已删除任务不产生监控项", "auto:schedule:gone" not in byid3,
      list(byid3))
check("计划任务：超过 24h 的旧失败不告警", byid3["auto:schedule:s2"][4] is None,
      byid3.get("auto:schedule:s2"))
check("计划任务：没跑过不判定", byid3["auto:schedule:s3"][4] is None,
      byid3.get("auto:schedule:s3"))
cfg["rules"]["schedule"] = False
if ORIG_SCHED is not None:
    sys.modules["sched"] = ORIG_SCHED
else:
    sys.modules.pop("sched", None)

# 规则关掉就不采集
cfg["rules"]["tunnel"] = False
auto2 = alerts._auto_items(cfg)
check("关掉隧道规则后不采集", not any(a[0].startswith("auto:tunnel") for a in auto2))

# tick 会把自动项写进 items，并在系统里消失后清掉
cfg["rules"]["tunnel"] = True
alerts._auto_items = lambda c: [
    ("auto:tunnel:t1", "隧道 正常隧道", "tunnel", "frp.a:7000", True, "", {}),
]
r = alerts.tick(now=1793412000, cfg=cfg)
check("tick 写入自动项", "auto:tunnel:t1" in alerts.state()["items"], list(alerts.state()["items"]))
check("tick 汇总 checked", r["checked"] == 1, r)
alerts._auto_items = lambda c: []
alerts.tick(now=1793412060, cfg=cfg)
check("系统里消失后自动清理", "auto:tunnel:t1" not in alerts.state()["items"])

# 自动项带着未恢复的告警消失（任务被删 / 容器被移除 / 规则被关）：
# 历史里那条告警就地闭环，标成「已失效」，不发信也不留永久红点
fresh(cfg={"fail_threshold": 1, "silence_min": 0})
cfg = alerts.state()
alerts._auto_items = lambda c: [
    ("auto:tunnel:gone", "隧道 将消失", "tunnel", "frp.x:7000", False, "挂了", {}),
]
alerts.tick(now=1793412100, cfg=cfg)
check("消失前留下告警事件", any(e["item_id"] == "auto:tunnel:gone"
                          and e["kind"] == "alert" for e in alerts.events()["items"]))
check("消失前算未恢复", alerts.events()["unrecovered"] == 1,
      alerts.events()["unrecovered"])
alerts._auto_items = lambda c: []
alerts.tick(now=1793412160, cfg=cfg)
row = [e for e in alerts.events()["items"] if e["item_id"] == "auto:tunnel:gone"][0]
check("自动项消失后告警闭环", row["recovered"] is True and bool(row.get("auto_closed")),
      row)
check("闭环后未恢复计数归零", alerts.events()["unrecovered"] == 0,
      alerts.events()["unrecovered"])

for k in ("tunnel", "cloudflare", "runtime"):
    sys.modules.pop(k, None)
alerts._auto_items = lambda c: []

# ================= 8. 历史记录 =================

print("[8] 历史记录")
fresh(cfg={"fail_threshold": 1, "silence_min": 30})
cfg = alerts.state()
itx = item(name="历史项")
alerts._evaluate(itx, False, "挂了", 1793412000, cfg)
check("告警写历史", len(alerts.events()["items"]) == 1, alerts.events()["total"])
ev0 = alerts.events()["items"][0]
check("历史含对象名", ev0["name"] == "历史项")
check("历史初始未恢复", ev0["recovered"] is False)
check("历史带类型", ev0["target_kind"] == "url")
alerts._evaluate(itx, True, "", 1793412060, cfg)
evs = alerts.events()
check("恢复写历史", evs["total"] == 2, evs["total"])
check("告警被标为已恢复", any(e["kind"] == "alert" and e["recovered"] for e in evs["items"]))
check("未恢复计数归零", evs["unrecovered"] == 0)

res = alerts.clear_events(only_recovered=True)
check("清空已恢复", res["removed"] == 2 and res["left"] == 0, res)

alerts._evaluate(itx, False, "又挂", 1793412120, cfg)
check("未恢复记录不会被清", alerts.clear_events(True)["left"] == 1)
check("全部清空可用", alerts.clear_events(False)["left"] == 0)

# ================= 9. 总览与接口视图 =================

print("[9] 总览")
fresh(cfg={"fail_threshold": 3, "silence_min": 25, "battery_low": 15})
alerts.save_smtp({"host": "smtp.qq.com", "port": 465, "security": "ssl",
                  "user": "someone@example.com", "to": ["someone@example.com"],
                  "password": "REDACTED-AUTHCODE"})
st = alerts.status()
check("总览含 smtp 掩码", st["smtp"]["has_password"] is True
      and "REDACTED-AUTHCODE" not in json.dumps(st, ensure_ascii=False))
check("总览含阈值", st["fail_threshold"] == 3 and st["silence_min"] == 25)
check("总览含规则", set(st["rules"]) == {"tunnel", "container", "schedule", "net", "battery"})
check("总览含状态计数", "counts" in st and st["counts"]["healthy"] == 0)
check("总览含发送统计", "send_stat" in st and "test" in st)

# 手动项 CRUD
r = alerts.upsert_item({"kind": "url", "name": "官网", "target": "https://example.com",
                        "method": "GET", "expect": 200, "timeout": 8})
iid = r["id"]
check("新增 URL 项", any(i["id"] == iid for i in alerts.items_view()))
try:
    alerts.upsert_item({"kind": "url", "target": "ftp://x"})
    check("非法 URL 被拒", False)
except ValueError as e:
    check("非法 URL 被拒", "http" in str(e), str(e))
try:
    alerts.upsert_item({"kind": "url", "target": "http://x", "method": "POST"})
    check("非法方法被拒", False)
except ValueError as e:
    check("非法方法被拒", "GET" in str(e), str(e))
r2 = alerts.upsert_item({"kind": "port", "name": "本地面板", "host": "127.0.0.1",
                         "port_num": 8888, "timeout": 3})
check("新增端口项", r2["id"].startswith("m"))
check("端口项 target 拼接", any(i["target"] == "127.0.0.1:8888"
                              for i in alerts.items_view()))
alerts.toggle_item(iid, False)
check("停用生效", [i for i in alerts.items_view() if i["id"] == iid][0]["enabled"] is False)
alerts.delete_item(iid)
check("删除生效", not any(i["id"] == iid for i in alerts.items_view()))

# 自动项不允许手删
alerts._auto_items = lambda c: [("auto:tunnel:x", "隧道 x", "tunnel", "a", True, "", {})]
alerts.tick(now=int(time.time()), cfg=alerts.state())
try:
    alerts.delete_item("auto:tunnel:x")
    check("自动项拒绝手删", False)
except ValueError as e:
    check("自动项拒绝手删", "自动" in str(e), str(e))
alerts._auto_items = lambda c: []

# 静默期只对「告警中」可见。恢复后 silence_until 仍留在数据里（同一窗口内
# 再次故障要靠它压住重复发信），但界面上不能出现「已恢复 · 静默 30m」。
_pid = r2["id"]
alerts._state["items"][_pid]["state"] = "alerting"
alerts._state["items"][_pid]["silence_until"] = int(time.time()) + 600
_row = [i for i in alerts.items_view() if i["id"] == _pid][0]
check("告警中显示静默剩余", _row["silence_left"] > 0, _row["silence_left"])
alerts._state["items"][_pid]["state"] = "recovered"
_row = [i for i in alerts.items_view() if i["id"] == _pid][0]
check("已恢复不再显示静默", _row["silence_left"] == 0, _row["silence_left"])
alerts._state["items"][_pid]["state"] = "healthy"

# 模板里的面板地址非空
check("面板地址可生成", alerts._panel_url().startswith("http://"))

alerts._auto_items = ORIG_AUTO
print(f"\n结果: {OK} 项通过, {FAIL} 项失败")
sys.exit(1 if FAIL else 0)
