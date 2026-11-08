"""监控告警（M09 极简版）：存活探测 + 阈值提醒 + SMTP 邮件。

设计取向是「宁可少发，不可刷屏」：

- 连续 3 次失败才发信（每次探测间隔 60s，也就是故障至少持续 2 分钟）
- 发信后进入 30 分钟静默期，期间同样失败不再重复发信
- 恢复通知**不受静默期限制** —— 「好了」这条消息永远有价值，而且每个
  故障周期只会产生一次，不会刷屏。这样刚告警完就恢复也能立刻收到。

状态机：

    healthy ──失败1-2次──▶ retrying ──失败3次──▶ alerting ──恢复──▶ recovered
       ▲                                              │                 │
       └────────────── 恢复后下一轮成功 ───────────────┴─────────────────┘

不做指标曲线、不做聚合、不存时间序列。只记一条条告警事件。
"""
import json
import os
import queue
import smtplib
import socket
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import core
import secretbox

POLL_INTERVAL = 60          # 探测间隔（秒）
FAIL_THRESHOLD = 3          # 连续失败多少次才告警
SILENCE_MIN = 30            # 告警后的静默期（分钟）
MAX_EVENTS = 500            # 历史事件上限
BATTERY_LOW = 20            # 电量阈值（%）
SMTP_TIMEOUT = 20
# 计划任务的「最近一次失败」超过这个时长就不再算当前故障：一天多没再跑过，
# 多半是任务被停用或已经改好，继续挂红点只会训练用户忽略告警。
SCHEDULE_STALE = 24 * 3600

# 路径用函数算而不是模块级常量：测试换 core.DATA 就能整目录隔离，
# 不必再追着改一堆常量。
def _settings_file():
    return os.path.join(core.DATA, "alerts.json")


def _events_file():
    return os.path.join(core.DATA, "alert_events.json")

KIND_TEXT = {
    "url": "URL 拨测", "port": "端口探活", "net": "网络状态",
    "battery": "设备电量", "tunnel": "隧道掉线", "container": "容器宕机",
    "schedule": "计划任务",
}

STATE_TEXT = {
    "healthy": "正常", "retrying": "重试中",
    "alerting": "告警中", "recovered": "已恢复",
}

_lock = threading.RLock()
_loaded = False
_state = None
_events = []
_queue = queue.Queue()
_worker_started = False
_ticker_started = False
_test_result = {"ts": 0, "ok": None, "message": "尚未测试", "detail": ""}
_send_stat = {"ok": 0, "fail": 0, "last_error": "", "last_ok_ts": 0}


# ---------------- 存储 ----------------

def _default():
    return {
        "enabled": True,
        "poll_interval": POLL_INTERVAL,
        "fail_threshold": FAIL_THRESHOLD,
        "silence_min": SILENCE_MIN,
        "battery_low": BATTERY_LOW,
        "smtp": {
            "host": "smtp.qq.com", "port": 465, "security": "ssl",
            "user": "", "sender": "", "to": [], "cc": [],
            "pwd_enc": "",
        },
        "rules": {
            "tunnel": True, "container": True, "schedule": True,
            "net": False, "battery": False,
        },
        "items": {},
    }


def _load():
    """首次加载。必须整体持有 `_lock`，且**最后**才置 `_loaded`。

    否则 ticker 线程与 HTTP handler 并发调用时，会有一个线程看到
    「_loaded 已为 True 但 _state 还是 None」——读文件期间会释放 GIL，
    这个竞态是真实存在的，不是理论问题。
    """
    global _state, _events, _loaded
    if _loaded and _state is not None:
        return
    with _lock:
        if _loaded and _state is not None:
            return
        try:
            with open(_settings_file(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            data = {}
        base = _default()
        if isinstance(data, dict):
            for k, v in data.items():
                if k == "smtp" and isinstance(v, dict):
                    base["smtp"].update({kk: vv for kk, vv in v.items() if kk in base["smtp"]})
                elif k == "rules" and isinstance(v, dict):
                    base["rules"].update({kk: bool(vv) for kk, vv in v.items() if kk in base["rules"]})
                elif k == "items" and isinstance(v, dict):
                    base["items"] = v
                elif k in base:
                    base[k] = v
        try:
            with open(_events_file(), "r", encoding="utf-8") as fh:
                ev = json.load(fh)
            evs = ev if isinstance(ev, list) else []
        except Exception:  # noqa: BLE001
            evs = []
        # 两个全局一起换，中途不暴露半成品
        _state, _events = base, evs
        _loaded = True


def _save():
    _load()
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    with _lock:
        tmp = _settings_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _settings_file())


def _save_events():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    with _lock:
        tmp = _events_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_events[-MAX_EVENTS:], fh, ensure_ascii=False)
        os.replace(tmp, _events_file())


def state():
    _load()
    return _state


def _secret_path():
    return os.path.join(core.DATA, "secret.key")


def _master():
    return secretbox.load_key(_secret_path())


# ---------------- SMTP 配置 ----------------

def _smtp():
    _load()
    return _state["smtp"]


def _password():
    enc = (_smtp().get("pwd_enc") or "").strip()
    if not enc:
        return ""
    try:
        return secretbox.decrypt_str(_master(), enc)
    except Exception:  # noqa: BLE001
        return ""


def _int_cfg(cfg, key, default):
    """读整数配置。不能用 `cfg.get(k) or default` —— 0 是合法值
    （静默期 0 分钟 = 关闭静默），`or` 会把它吞成默认值。"""
    v = (cfg or {}).get(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _split_addrs(v):
    """收件人既接受列表也接受 'a@x, b@y' 这种串。去重保序。

    这里只拆分、不判断合法性 —— 早先按 `"@" in p` 过滤，结果用户把
    邮箱写成 `foo` 时地址被静默丢掉，界面上什么都不报，只能对着
    「保存成功」发呆。合法性交给 save_smtp 里显式校验。
    """
    if isinstance(v, str):
        parts = [x.strip() for x in v.replace(";", ",").split(",")]
    elif isinstance(v, (list, tuple)):
        parts = [str(x).strip() for x in v]
    else:
        parts = []
    out = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def smtp_view():
    """给前端的配置：授权码只回掩码，绝不回明文。"""
    _load()
    s = _smtp()
    pwd = _password()
    return {
        "host": s.get("host") or "",
        "port": int(s.get("port") or 465),
        "security": s.get("security") or "ssl",
        "user": s.get("user") or "",
        "sender": s.get("sender") or "",
        "to": list(s.get("to") or []),
        "cc": list(s.get("cc") or []),
        "has_password": bool(s.get("pwd_enc")),
        "password_masked": secretbox.mask(pwd) if pwd else "",
        "configured": bool(s.get("host") and s.get("user") and s.get("pwd_enc")
                           and (s.get("to") or s.get("sender"))),
    }


def save_smtp(body):
    """保存 SMTP 配置。

    全程在副本上改，校验通过后一次性合并 —— 否则用户填了个非法端口，
    报错的同时非法值已经写进内存，之后连正确的保存都会一直报同一个错，
    只能重启面板才恢复。
    """
    _load()
    with _lock:
        cur = _state["smtp"]
        nxt = {
            "host": cur.get("host") or "",
            "port": int(cur.get("port") or 465),
            "security": cur.get("security") or "ssl",
            "user": cur.get("user") or "",
            "sender": cur.get("sender") or "",
            "to": list(cur.get("to") or []),
            "cc": list(cur.get("cc") or []),
            "pwd_enc": cur.get("pwd_enc") or "",
        }

    if body.get("host") is not None:
        nxt["host"] = str(body.get("host") or "").strip()
    if body.get("port") is not None:
        try:
            nxt["port"] = int(body.get("port") or 465)
        except (TypeError, ValueError):
            raise ValueError("端口必须是数字")
    if not (1 <= nxt["port"] <= 65535):
        raise ValueError("端口需在 1–65535 之间")
    if body.get("security") is not None:
        sec = str(body.get("security") or "ssl").lower()
        if sec not in ("ssl", "starttls", "none"):
            raise ValueError("加密方式只能是 ssl / starttls / none")
        nxt["security"] = sec
    if body.get("user") is not None:
        nxt["user"] = str(body.get("user") or "").strip()
    if body.get("sender") is not None:
        nxt["sender"] = str(body.get("sender") or "").strip()
    if body.get("to") is not None:
        nxt["to"] = _split_addrs(body.get("to"))
    if body.get("cc") is not None:
        nxt["cc"] = _split_addrs(body.get("cc"))
    # 授权码：只有明确传了非空值才覆盖，传空表示「保持不变」——
    # 前端展示的是掩码，用户不动它就不该把真授权码冲掉。
    pwd = body.get("password")
    if pwd:
        nxt["pwd_enc"] = secretbox.encrypt_str(_master(), str(pwd).strip())
    if body.get("clear_password"):
        nxt["pwd_enc"] = ""

    if not nxt["sender"] and nxt["user"]:
        nxt["sender"] = nxt["user"]
    if not nxt["to"] and nxt["sender"]:
        nxt["to"] = [nxt["sender"]]
    for a in nxt["to"] + nxt["cc"]:
        if "@" not in a:
            raise ValueError(f"邮箱地址不合法：{a}")

    with _lock:
        _state["smtp"] = nxt
    _save()
    return smtp_view()


# ---------------- 邮件模板 ----------------

BRAND = "PocketPanel"


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_mail(ctx):
    """返回 (html, text)。

    QQ 邮箱 App 会剥掉 <style> 标签，所以全部用内联样式 + table 布局；
    宽度 600px、浅底白卡、不依赖任何外部图片，深色模式下也不会糊。
    """
    kind = ctx.get("kind") or "alert"
    accent = {"alert": "#e5484d", "recover": "#30a46c", "test": "#3b82f6"}.get(kind, "#3b82f6")
    badge = {"alert": "故障告警", "recover": "已恢复", "test": "测试邮件"}.get(kind, "通知")
    title = _esc(ctx.get("title") or badge)

    rows = []
    for label, value in (ctx.get("rows") or []):
        if value in (None, ""):
            continue
        rows.append(
            '<tr>'
            f'<td style="padding:9px 0;width:96px;vertical-align:top;'
            f'font-size:13px;color:#7c8798;white-space:nowrap">{_esc(label)}</td>'
            f'<td style="padding:9px 0;font-size:14px;color:#1b2333;'
            f'word-break:break-all;line-height:1.5">{_esc(value)}</td>'
            '</tr>')

    note = ctx.get("note") or ""
    note_html = ""
    if note:
        note_html = (
            '<div style="margin-top:18px;padding:12px 14px;background:#f5f7fb;'
            'border-radius:8px;border-left:3px solid ' + accent + '">'
            f'<div style="font-size:12.5px;color:#5a6678;line-height:1.65">{_esc(note)}</div>'
            '</div>')

    panel = ctx.get("panel") or ""
    panel_html = ""
    if panel:
        panel_html = (
            '<div style="margin-top:22px">'
            f'<a href="{_esc(panel)}" style="display:inline-block;padding:11px 20px;'
            f'background:{accent};color:#ffffff;text-decoration:none;border-radius:8px;'
            'font-size:14px;font-weight:600">打开面板查看</a></div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#eef1f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 style="background:#eef1f6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
 style="width:100%;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;
 box-shadow:0 2px 10px rgba(20,30,50,.08);font-family:-apple-system,BlinkMacSystemFont,
 'PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif;">

  <tr><td style="background:#0d1424;padding:20px 26px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="font-size:17px;font-weight:700;color:#ffffff;letter-spacing:.3px;">
        <span style="display:inline-block;width:9px;height:9px;border-radius:3px;
        background:{accent};margin-right:9px;vertical-align:middle;"></span>{BRAND}
      </td>
      <td align="right" style="font-size:12px;color:#93a0b5;">设备监控告警</td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:26px 26px 6px 26px;">
    <span style="display:inline-block;padding:4px 11px;border-radius:20px;font-size:12px;
    font-weight:600;color:{accent};background:{accent}14;border:1px solid {accent}33">{badge}</span>
    <div style="margin-top:14px;font-size:19px;font-weight:700;color:#111827;line-height:1.45">
      {title}</div>
  </td></tr>

  <tr><td style="padding:12px 26px 0 26px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
     style="border-collapse:collapse;border-top:1px solid #eef1f6;">
      {''.join(rows)}
    </table>
  </td></tr>

  <tr><td style="padding:0 26px;">{note_html}{panel_html}</td></tr>

  <tr><td style="padding:24px 26px 26px 26px;">
    <div style="border-top:1px solid #eef1f6;padding-top:14px;font-size:11.5px;
    color:#98a3b3;line-height:1.8">
      本邮件由 {BRAND} 自动发送，请勿直接回复。<br>
      事件编号 {_esc(ctx.get("event_id") or "—")} · 发送时间 {_fmt_ts(ctx.get("ts") or time.time())}
    </div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""

    lines = [f"【{BRAND}】{ctx.get('title') or badge}", ""]
    for label, value in (ctx.get("rows") or []):
        if value not in (None, ""):
            lines.append(f"{label}：{value}")
    if note:
        lines += ["", note]
    if panel:
        lines += ["", f"面板地址：{panel}"]
    lines += ["", f"事件编号：{ctx.get('event_id') or '—'}",
              f"发送时间：{_fmt_ts(ctx.get('ts') or time.time())}",
              f"—— 本邮件由 {BRAND} 自动发送，请勿直接回复"]
    return html, "\n".join(lines)


def _build_msg(ctx):
    s = _smtp()
    sender = s.get("sender") or s.get("user") or ""
    to = list(s.get("to") or [])
    cc = list(s.get("cc") or [])
    html, text = render_mail(ctx)
    m = EmailMessage()
    m["Subject"] = ctx.get("subject") or f"【{BRAND}】{ctx.get('title') or '通知'}"
    m["From"] = formataddr((BRAND, sender))
    m["To"] = ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] or "pocketpanel.local"))
    m["X-PocketPanel-Event"] = str(ctx.get("event_id") or "")
    m.set_content(text, subtype="plain", charset="utf-8")
    m.add_alternative(html, subtype="html", charset="utf-8")
    return m, to + cc


def _smtp_error_text(e):
    """把 smtplib 的异常翻译成用户能照着改的话。"""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("认证失败：授权码不正确，或该邮箱未开启 SMTP 服务。"
                "QQ 邮箱需在「设置 → 账户」开启 SMTP 并重新生成授权码（不是登录密码）")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"收件人被拒绝：{e.recipients}"
    if isinstance(e, smtplib.SMTPSenderRefused):
        return ("发件人被拒绝：发件邮箱与登录账号不一致，"
                "或该邮箱限制了发信频率")
    if isinstance(e, smtplib.SMTPConnectError):
        return f"无法连接发件服务器（{e.smtp_code}），请检查服务器地址与端口"
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return "服务器提前断开连接：端口与加密方式可能不匹配（465 用 SSL，587 用 STARTTLS）"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return f"服务器不支持该操作：{e}"
    if isinstance(e, smtplib.SMTPException):
        return f"SMTP 错误：{e}"
    if isinstance(e, ssl.SSLError):
        return ("TLS/SSL 握手失败：465 端口应选 SSL，587 端口应选 STARTTLS；"
                "若服务器用自签证书也会报这个错")
    if isinstance(e, socket.gaierror):
        return "域名解析失败：发件服务器地址写错了，或当前网络不可用"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"连接超时：{SMTP_TIMEOUT} 秒内没连上发件服务器，检查端口是否被封"
    if isinstance(e, ConnectionRefusedError):
        return "连接被拒绝：端口没有服务在监听，检查端口号"
    if isinstance(e, OSError):
        return f"网络错误：{e}"
    return f"{type(e).__name__}: {e}"


def _send_now(ctx):
    """真正发信，返回 (ok, message)。调用方负责在线程里跑。"""
    s = _smtp()
    host = (s.get("host") or "").strip()
    port = int(s.get("port") or 465)
    sec = s.get("security") or "ssl"
    user = (s.get("user") or "").strip()
    pwd = _password()
    if not host:
        return False, "未配置发件服务器（SMTP Host）"
    if not user:
        return False, "未配置发件邮箱"
    if not pwd:
        return False, "未配置邮箱授权码"
    to = list(s.get("to") or [])
    if not to:
        return False, "未配置收件人邮箱"
    try:
        msg, rcpts = _build_msg(ctx)
    except Exception as e:  # noqa: BLE001
        return False, f"邮件构造失败：{e}"

    conn = None
    # 记下连接进行到哪一步：QQ / 163 这类邮箱在授权码错误时**不返回 535**，
    # 而是认证失败后直接断开。不区分阶段的话，用户只会看到「端口与加密方式
    # 可能不匹配」，被误导去改 465/587，实际要改的是授权码。
    stage = "connect"
    try:
        if sec == "ssl":
            conn = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT,
                                    context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            conn.ehlo()
            if sec == "starttls":
                conn.starttls(context=ssl.create_default_context())
                conn.ehlo()
        stage = "login"
        conn.login(user, pwd)
        stage = "send"
        conn.sendmail(s.get("sender") or user, rcpts, msg.as_bytes())
        return True, f"已发送至 {', '.join(rcpts)}"
    except smtplib.SMTPServerDisconnected:
        if stage == "login":
            return False, ("认证失败：授权码不正确（服务器拒绝了登录并断开连接）。"
                           "QQ 邮箱需在「设置 → 账户」开启 SMTP 服务后重新生成授权码，"
                           "注意授权码不是邮箱登录密码")
        if stage == "send":
            return False, ("发送阶段被服务器断开：可能是发信频率超限，"
                           "或收件人数过多被拒")
        return False, _smtp_error_text(smtplib.SMTPServerDisconnected("disconnected"))
    except Exception as e:  # noqa: BLE001
        if stage == "login" and isinstance(e, smtplib.SMTPAuthenticationError):
            return False, _smtp_error_text(e)
        return False, _smtp_error_text(e)
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------- 发信队列 ----------------

def _worker():
    while True:
        job = _queue.get()
        try:
            ok, msg = _send_now(job["ctx"])
            with _lock:
                if ok:
                    _send_stat["ok"] += 1
                    _send_stat["last_ok_ts"] = int(time.time())
                else:
                    _send_stat["fail"] += 1
                    _send_stat["last_error"] = msg
            job["done"](ok, msg)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                job["done"](False, f"内部错误：{e}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, daemon=True, name="alert-mailer").start()


def _enqueue(ctx, done=None):
    _ensure_worker()
    _queue.put({"ctx": ctx, "done": done or (lambda ok, msg: None)})


# ---------------- 历史事件 ----------------

def _log_event(kind, item, subject, ok=True, error="", detail=""):
    ev = {
        "id": "%06x" % (int(time.time() * 1000) & 0xFFFFFF),
        "ts": int(time.time()),
        "kind": kind,                      # alert | recover | test
        "item_id": (item or {}).get("id", ""),
        "name": (item or {}).get("name", ""),
        "target_kind": (item or {}).get("kind", ""),
        "target": (item or {}).get("target", ""),
        "subject": subject,
        "ok": bool(ok),
        "error": str(error or "")[:300],
        "detail": str(detail or "")[:300],
        "recovered": kind == "recover",
        "recovered_ts": int(time.time()) if kind == "recover" else 0,
    }
    with _lock:
        _events.append(ev)
        if len(_events) > MAX_EVENTS:
            del _events[:len(_events) - MAX_EVENTS]
        if kind == "recover" and ev["item_id"]:
            # 把同一监控项最近一条告警标成「已恢复」，历史列表里一眼能看出闭环
            for old in reversed(_events):
                if old.get("kind") == "alert" and old.get("item_id") == ev["item_id"]:
                    old["recovered"] = True
                    old["recovered_ts"] = ev["ts"]
                    break
        _save_events()
    return ev


def events(limit=200):
    _load()
    with _lock:
        items = list(reversed(_events))
    return {
        "items": items[:max(1, min(int(limit or 200), MAX_EVENTS))],
        "total": len(items),
        "alert_count": sum(1 for e in items if e.get("kind") == "alert"),
        "recover_count": sum(1 for e in items if e.get("kind") == "recover"),
        "unrecovered": sum(1 for e in items
                           if e.get("kind") == "alert" and not e.get("recovered")),
    }


def clear_events(only_recovered=True):
    _load()
    with _lock:
        if only_recovered:
            keep = [e for e in _events if not e.get("recovered")]
        else:
            keep = []
        removed = len(_events) - len(keep)
        _events[:] = keep
        _save_events()
    return {"removed": removed, "left": len(keep)}


# ---------------- 探活 ----------------

def _check_url(it):
    url = it.get("target") or ""
    method = (it.get("method") or "GET").upper()
    expect = int(it.get("expect") or 200)
    timeout = float(it.get("timeout") or 10)
    if method not in ("GET", "HEAD"):
        method = "GET"
    if not url.lower().startswith(("http://", "https://")):
        return False, "URL 为空或格式不对（需 http:// 或 https:// 开头）", {}
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": "PocketPanel-Monitor/1.0",
        "Accept": "*/*",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            if code == expect:
                return True, "", {"status": code, "ms": int((time.time() - t0) * 1000)}
            return False, f"HTTP {code}（期望 {expect}）", {
                "status": code, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也是有效响应：明确告诉用户实际状态码
        ms = int((time.time() - t0) * 1000)
        if e.code == expect:
            return True, "", {"status": e.code, "ms": ms}
        return False, f"HTTP {e.code}（期望 {expect}）", {"status": e.code, "ms": ms}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return False, f"请求超时（>{int(timeout)}s）", {"ms": int((time.time() - t0) * 1000)}
        return False, f"连接失败：{reason}", {"ms": int((time.time() - t0) * 1000)}
    except (socket.timeout, TimeoutError):
        return False, f"请求超时（>{int(timeout)}s）", {}
    except Exception as e:  # noqa: BLE001
        return False, f"请求异常：{type(e).__name__}: {e}", {}


def _check_port(it):
    host = it.get("host") or "127.0.0.1"
    port = int(it.get("port_num") or 0)
    timeout = float(it.get("timeout") or 5)
    if not port:
        return False, "未配置端口", {}
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "", {"ms": int((time.time() - t0) * 1000)}
    except socket.timeout:
        return False, f"连接超时（{host}:{port}）", {}
    except ConnectionRefusedError:
        return False, f"连接被拒绝（{host}:{port} 无服务监听）", {}
    except OSError as e:
        return False, f"连接失败：{e}", {}


def _check_battery(it):
    try:
        import metrics
        pct, chg, _temp = metrics.battery()
    except Exception as e:  # noqa: BLE001
        return None, f"读不到电量：{e}", {}
    if not pct:
        return None, "读不到电量（设备不支持 termux-api）", {}
    low = _int_cfg(state(), "battery_low", BATTERY_LOW)
    if pct < low and not chg:
        return False, f"电量 {pct}%（低于 {low}% 且未充电）", {"pct": pct, "charging": chg}
    return True, "", {"pct": pct, "charging": chg}


def _check_net(it):
    try:
        import netguard
        net = netguard.current_network()
    except Exception as e:  # noqa: BLE001
        return None, f"读不到网络状态：{e}", {}
    ip = (net or {}).get("ip") or ""
    kind = (net or {}).get("kind") or "none"
    if not ip or kind == "none":
        return False, "设备当前没有可用网络（无 IP，可能已断网）", {"ip": ip, "kind": kind}
    return True, "", {"ip": ip, "kind": kind}


# ---------------- 联动（M15 / M17 / M08） ----------------

def _auto_items(cfg):
    """按开关从各模块拉取当前状态，生成/更新自动监控项。

    返回 [(id, name, kind, target, ok_or_None, err, detail)]。
    ok=None 表示「这次不算失败也不算成功」（例如用户主动停止的隧道），
    直接跳过评估，不改状态机 —— 主动停止不该触发告警。
    """
    out = []
    rules = cfg.get("rules") or {}

    if rules.get("tunnel"):
        try:
            import tunnel
            for t in tunnel.list_instances():
                st = t.get("status") or "stopped"
                name = t.get("name") or t.get("id")
                probe = t.get("probe") or {}
                if st == "failed":
                    out.append((f"auto:tunnel:{t['id']}", f"隧道 {name}", "tunnel",
                                t.get("addr") or "frpc", False,
                                "frpc 进程连续重试失败，已停止", {"status": st}))
                elif st == "running":
                    if probe.get("ok") is False:
                        out.append((f"auto:tunnel:{t['id']}", f"隧道 {name}", "tunnel",
                                    t.get("addr") or "frpc", False,
                                    f"公网拨测失败：{probe.get('detail') or '不可达'}",
                                    {"status": st}))
                    else:
                        out.append((f"auto:tunnel:{t['id']}", f"隧道 {name}", "tunnel",
                                    t.get("addr") or "frpc", True, "", {"status": st}))
                else:
                    out.append((f"auto:tunnel:{t['id']}", f"隧道 {name}", "tunnel",
                                t.get("addr") or "frpc", None, "", {"status": st}))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        try:
            import cloudflare
            for t in cloudflare.list_tunnels():
                st = t.get("state") or "stopped"
                name = t.get("name") or t.get("id")
                target = t.get("url") or t.get("host") or "cloudflared"
                if st in ("error", "failed"):
                    out.append((f"auto:cf:{t['id']}", f"CF 隧道 {name}", "tunnel", target, False,
                                t.get("error") or f"状态异常：{t.get('state_text') or st}",
                                {"state": st}))
                elif st in ("running", "connected") and t.get("alive"):
                    out.append((f"auto:cf:{t['id']}", f"CF 隧道 {name}", "tunnel", target, True,
                                "", {"state": st}))
                elif st == "starting":
                    out.append((f"auto:cf:{t['id']}", f"CF 隧道 {name}", "tunnel", target, None,
                                "", {"state": st}))
                else:
                    out.append((f"auto:cf:{t['id']}", f"CF 隧道 {name}", "tunnel", target, None,
                                "", {"state": st}))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    if rules.get("container"):
        try:
            import runtime
            for c in runtime.list_containers():
                st = c.get("state") or ""
                code = c.get("exit_code")
                name = c.get("name") or c.get("id", "")[:12]
                cid = f"auto:container:{c.get('id')}"
                if st == "exited":
                    # 0 = 正常退出；143 = SIGTERM，也就是 `docker stop` 的默认结果。
                    # 用户主动停的容器不该报「宕机」——每停一次就告警一次，比不告警更糟。
                    # 137（SIGKILL / 被 OOM 杀）仍然算故障，那是真出了事。
                    if code in (0, None, 143):
                        out.append((cid, f"容器 {name}", "container", c.get("image") or name,
                                    None, "", {"exit_code": code}))
                    else:
                        out.append((cid, f"容器 {name}", "container", c.get("image") or name,
                                    False, f"容器意外退出，退出码 {code}", {"exit_code": code}))
                elif st == "running":
                    out.append((cid, f"容器 {name}", "container", c.get("image") or name,
                                True, "", {"exit_code": code}))
                else:
                    out.append((cid, f"容器 {name}", "container", c.get("image") or name,
                                None, "", {"exit_code": code}))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    if rules.get("schedule"):
        try:
            import sched as sched_mod
            data = sched_mod.listing()
            latest = {}
            for r in data.get("runs") or []:
                tid = r.get("task_id")
                if tid and tid not in latest:
                    latest[tid] = r
            now_ts = int(time.time())
            # 以「当前还存在的任务」为准遍历，而不是以 runs 为准。
            # 按 runs 反推会给已删除任务留下幽灵监控项：它们永远停在
            # alerting，把「告警中」计数灌成一片红，点进去还查不到这个任务。
            for t in data.get("tasks") or []:
                tid = t.get("id")
                if not tid:
                    continue
                name = t.get("name") or tid
                target = t.get("human") or name
                iid = f"auto:schedule:{tid}"
                r = latest.get(tid)
                if not r:
                    out.append((iid, f"任务 {name}", "schedule", target, None, "", {}))
                    continue
                st = r.get("status")
                rid = r.get("id")
                if st in ("failed", "timeout"):
                    started = int(r.get("started") or 0)
                    if started and now_ts - started > SCHEDULE_STALE:
                        # 一天多以前那次失败不再算「当前故障」：任务可能已经
                        # 停用或改好，继续挂着红点只会让人习惯性忽略告警
                        out.append((iid, f"任务 {name}", "schedule", target, None,
                                    f"最近一次失败已超过 24 小时（{rid}），任务可能已停用",
                                    {"run_id": rid, "status": st}))
                    else:
                        out.append((iid, f"任务 {name}", "schedule", target, False,
                                    f"最近一次执行{('超时' if st == 'timeout' else '失败')}"
                                    f"（退出码 {r.get('exit')}）· {rid}",
                                    {"run_id": rid, "status": st}))
                elif st == "success":
                    out.append((iid, f"任务 {name}", "schedule", target, True, "",
                                {"run_id": rid, "status": st}))
                else:
                    out.append((iid, f"任务 {name}", "schedule", target, None, "",
                                {"run_id": rid, "status": st}))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    if rules.get("battery"):
        ok, err, detail = _check_battery({})
        out.append(("auto:battery", "设备电量", "battery", "battery", ok, err, detail))

    if rules.get("net"):
        ok, err, detail = _check_net({})
        out.append(("auto:net", "网络连接", "net", "network", ok, err, detail))

    return out


def _new_item(iid, name, kind, target, manual=False):
    return {
        "id": iid, "name": name, "kind": kind, "target": target,
        "enabled": True, "manual": bool(manual),
        "method": "GET", "expect": 200, "timeout": 10,
        "host": "127.0.0.1", "port_num": 0,
        "state": "healthy", "fails": 0,
        "last_check": 0, "last_ok": 0, "last_alert": 0, "silence_until": 0,
        "last_error": "", "alert_count": 0, "recover_count": 0,
        "dedup_key": "", "alerted_key": "",
        "last_detail": {},
    }


# ---------------- 状态机 ----------------

def _panel_url():
    try:
        port = int(core.cfg_get("port", default=8888))
    except (TypeError, ValueError):
        port = 8888
    ip = ""
    try:
        import metrics
        ip = metrics.lan_ip() or ""
    except Exception:  # noqa: BLE001
        ip = ""
    return f"http://{ip or '127.0.0.1'}:{port}"


def _mail_ctx(kind, it, reason, now, event_id=""):
    ktext = KIND_TEXT.get(it.get("kind"), it.get("kind"))
    if kind == "alert":
        title = f"{it.get('name')} 持续异常"
        subject = f"【{BRAND} 告警】{it.get('name')} 探测失败（连续 {it.get('fails')} 次）"
        note = ("该监控项已连续 %d 次探测失败，达到告警阈值。恢复后会自动发送一封恢复通知；"
                "%d 分钟内不重复提醒。" % (it.get("fails") or 0,
                                            _int_cfg(state(), "silence_min", SILENCE_MIN)))
    elif kind == "recover":
        title = f"{it.get('name')} 已恢复正常"
        subject = f"【{BRAND} 恢复】{it.get('name')} 已恢复正常"
        note = "该监控项探测已恢复正常，本次故障周期结束。"
    else:
        title = "测试邮件发送成功"
        subject = f"【{BRAND}】测试邮件 · 配置验证成功"
        note = "如果你在手机上看到了这封邮件，说明 SMTP 配置可用，告警通道已打通。"
    rows = [
        ("告警对象", it.get("name")),
        ("类型", ktext),
        ("目标", it.get("target")),
        ("发生时间", _fmt_ts(now)),
        ("故障原因", reason if kind == "alert" else ("已恢复" if kind == "recover" else "—")),
    ]
    if kind == "recover":
        rows.append(("恢复时间", _fmt_ts(now)))
    return {
        "kind": kind, "title": title, "subject": subject, "rows": rows,
        "note": note, "panel": _panel_url(), "ts": now, "event_id": event_id,
    }


def _evaluate(it, ok, err, now, cfg, detail=None):
    """把一个探测结果喂进状态机；返回本轮的处置说明。"""
    if ok is None:
        # 不参与判定（主动停止 / 读不到数据），保持原状态。
        # 但「原本在告警、现在变成不适用」是个例外：用户手动停了容器或隧道，
        # 那条告警就不代表任何现存问题了，就地安静闭环（不发恢复信）。
        it["last_check"] = now
        if it.get("state") == "alerting":
            it["state"] = "recovered"
            it["fails"] = 0
            it["last_error"] = ""
            it["recover_count"] = int(it.get("recover_count") or 0) + 1
            _close_item_events(it.get("id") or "", "监控对象已停止，告警自动闭环")
            return "closed"
        # 恢复正常后不该继续显示上一次的失败次数与错误（老数据里可能有残留）
        if it.get("fails") or it.get("last_error"):
            it["fails"] = 0
            it["last_error"] = ""
        return "skip"

    it["last_check"] = now
    if detail:
        it["last_detail"] = detail
    threshold = max(1, _int_cfg(cfg, "fail_threshold", FAIL_THRESHOLD))
    silence = max(0, _int_cfg(cfg, "silence_min", SILENCE_MIN)) * 60
    prev = it.get("state") or "healthy"

    if ok:
        it["last_ok"] = now
        it["fails"] = 0
        it["last_error"] = ""
        if prev == "alerting":
            it["state"] = "recovered"
            it["recover_count"] = int(it.get("recover_count") or 0) + 1
            _dispatch_mail("recover", it, "", now)
            return "recover"
        if prev == "recovered":
            it["state"] = "healthy"
            return "healthy"
        it["state"] = "healthy"
        return "healthy"

    # ---- 失败 ----
    it["last_error"] = str(err or "")[:200]
    dedup = str((detail or {}).get("run_id") or "")
    it["dedup_key"] = dedup

    # 事件型监控（计划任务）：同一个 run 只告警一次，避免同一笔失败被反复提醒
    if dedup and dedup == it.get("alerted_key"):
        if prev in ("alerting", "recovered"):
            it["state"] = "alerting"
        return "dedup"

    if prev == "alerting":
        # 静默期内不重复发信；静默期过了仍然失败，再发一封（周期提醒）
        if silence and now < int(it.get("silence_until") or 0):
            return "silenced"
        it["fails"] = int(it.get("fails") or 0) + 1
        it["state"] = "alerting"
        it["last_alert"] = now
        it["silence_until"] = now + silence
        it["alert_count"] = int(it.get("alert_count") or 0) + 1
        it["alerted_key"] = dedup
        _dispatch_mail("alert", it, err, now)
        return "alert"

    it["fails"] = int(it.get("fails") or 0) + 1
    if it["fails"] >= threshold:
        it["state"] = "alerting"
        it["last_alert"] = now
        it["silence_until"] = now + silence
        it["alert_count"] = int(it.get("alert_count") or 0) + 1
        it["alerted_key"] = dedup
        _dispatch_mail("alert", it, err, now)
        return "alert"

    it["state"] = "retrying"
    return "retrying"


def _close_item_events(item_id, note=""):
    """把一个「对象已消失」的监控项的未恢复告警就地闭环。

    不发信、不新增事件，只在历史里把那条标成已失效 —— 任务删了、容器删了、
    规则关了，那条告警就不再代表任何现存问题。
    """
    if not item_id:
        return False
    with _lock:
        for old in reversed(_events):
            if old.get("item_id") != item_id:
                continue
            if old.get("kind") == "alert" and not old.get("recovered"):
                old["recovered"] = True
                old["recovered_ts"] = int(time.time())
                old["auto_closed"] = note or "监控对象已不存在"
                _save_events()
                return True
    return False


def _dispatch_mail(kind, it, reason, now):
    """把邮件丢进队列。邮件本身失败只记事件，不影响状态机。"""
    if not (state().get("smtp") or {}).get("pwd_enc"):
        _log_event(kind, it, "未配置授权码，跳过发送", ok=False, error="未配置授权码")
        return
    ev = _log_event(kind, it, "", ok=True, detail=reason)
    it["last_event_id"] = ev["id"]
    ctx = _mail_ctx(kind, it, reason, now, ev["id"])

    def done(ok, msg):
        with _lock:
            for e in reversed(_events):
                if e.get("id") == ev["id"]:
                    e["ok"] = bool(ok)
                    if not ok:
                        e["error"] = str(msg)[:300]
                    e["subject"] = ctx["subject"]
                    break
            _save_events()

    _enqueue(ctx, done)


def _check_one(it, cfg):
    """单项探活。异常必须就地转成「失败」，不能抛给 tick ——
    一个配坏的监控项不该让其余所有监控这一轮全部跳过。"""
    kind = it.get("kind")
    try:
        if kind == "url":
            return _check_url(it)
        if kind == "port":
            return _check_port(it)
        if kind == "battery":
            return _check_battery(it)
        if kind == "net":
            return _check_net(it)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return False, f"探测异常：{type(e).__name__}: {e}", {}
    return None, "", {}


# ---------------- 主循环 ----------------

def tick(now=None, cfg=None):
    """跑一轮探测。返回摘要，供接口与测试用。"""
    _load()
    cfg = cfg or state()
    now = int(now or time.time())
    if not cfg.get("enabled", True):
        return {"ok": True, "skipped": "已停用", "checked": 0, "results": []}
    results = []
    with _lock:
        items = cfg["items"]
        auto = _auto_items(cfg)
        seen = set()

        # 自动项：就地更新状态机
        for iid, name, kind, target, ok, err, detail in auto:
            seen.add(iid)
            it = items.get(iid)
            if it is None:
                it = _new_item(iid, name, kind, target, manual=False)
                items[iid] = it
            it["name"], it["kind"], it["target"] = name, kind, target
            it["manual"] = False
            if not it.get("enabled", True):
                continue
            act = _evaluate(it, ok, err, now, cfg, detail)
            results.append({"id": iid, "name": name, "kind": kind,
                            "ok": ok, "err": err, "action": act})

        # 系统里已经不存在的自动项：清掉，别在列表里留幽灵
        for iid in [k for k, v in items.items()
                    if not v.get("manual") and k not in seen]:
            gone = items.pop(iid, None)
            if gone and gone.get("state") == "alerting":
                # 监控对象都没了（任务被删、容器被移除、规则被关），
                # 那条告警也就失效了。留一条永远「未恢复」的记录只会让
                # 历史列表永远红着，不如就地闭环，不发信。
                _close_item_events(iid, "监控对象已不存在，自动闭环")

        # 手动项：URL / 端口
        for iid, it in list(items.items()):
            if not it.get("manual"):
                continue
            if not it.get("enabled", True):
                continue
            try:
                ok, err, detail = _check_one(it, cfg)
                act = _evaluate(it, ok, err, now, cfg, detail)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                ok, err, act = False, f"内部错误：{e}", "error"
            results.append({"id": iid, "name": it.get("name"), "kind": it.get("kind"),
                            "ok": ok, "err": err, "action": act})
    _save()
    return {"ok": True, "checked": len(results), "ts": now,
            "alerted": sum(1 for r in results if r["action"] == "alert"),
            "recovered": sum(1 for r in results if r["action"] == "recover"),
            "results": results}


def _loop():
    while True:
        try:
            cfg = state()
            interval = max(15, _int_cfg(cfg, "poll_interval", POLL_INTERVAL))
            time.sleep(interval)
            tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(30)


def ensure_thread():
    global _ticker_started
    with _lock:
        if _ticker_started:
            return
        _ticker_started = True
        _load()
    _ensure_worker()
    threading.Thread(target=_loop, daemon=True, name="alert-monitor").start()


# ---------------- 测试发送 ----------------

def test_send_async():
    """异步发一封测试邮件；结果写进 _test_result 供前端轮询。"""
    _load()
    s = _smtp()
    if not s.get("host"):
        return {"started": False, "message": "请先填写 SMTP 服务器"}
    if not s.get("pwd_enc"):
        return {"started": False, "message": "请先填写邮箱授权码"}
    now = int(time.time())
    with _lock:
        _test_result.update(ts=now, ok=None, message="正在发送…", detail="")
    ctx = _mail_ctx("test", {"name": "测试", "kind": "url", "target": s.get("host")},
                    "", now, "%06x" % (now & 0xFFFFFF))

    def run():
        ok, msg = _send_now(ctx)
        with _lock:
            _test_result.update(ts=int(time.time()), ok=bool(ok),
                                message=msg if ok else f"发送失败：{msg}", detail=msg)
        _log_event("test", {"id": "", "name": "测试邮件", "kind": "test",
                            "target": s.get("host") or ""},
                   ctx["subject"], ok=bool(ok), error="" if ok else msg)
        if ok:
            with _lock:
                _send_stat["ok"] += 1
                _send_stat["last_ok_ts"] = int(time.time())
        else:
            with _lock:
                _send_stat["fail"] += 1
                _send_stat["last_error"] = msg

    threading.Thread(target=run, daemon=True, name="alert-test").start()
    return {"started": True, "message": "正在发送…"}


def test_result():
    with _lock:
        return dict(_test_result)


# ---------------- 监控项 CRUD ----------------

def items_view():
    _load()
    with _lock:
        out = [dict(v) for v in _state["items"].values()]
    out.sort(key=lambda x: (not x.get("manual"), x.get("name") or ""))
    for it in out:
        it["state_text"] = STATE_TEXT.get(it.get("state"), it.get("state"))
        it["kind_text"] = KIND_TEXT.get(it.get("kind"), it.get("kind"))
        # 静默期只在「告警中」有意义。恢复后 silence_until 仍留在数据里
        # （万一同一个静默窗口内再次故障，靠它压住重复发信），但对外
        # 不该暴露 —— 否则界面会出现「已恢复 · 静默 30m」这种自相矛盾的行。
        left = max(0, int(it.get("silence_until") or 0) - int(time.time()))
        it["silence_left"] = left if it.get("state") == "alerting" else 0
    return out


def upsert_item(body):
    """新增或修改一个手动监控项（URL / 端口）。

    先算完所有字段、全部校验通过，最后才写进 items / 落盘。
    之前的写法是「先建空项再逐字段校验」，一旦校验失败就会在列表里
    留下一条 target 为空的脏记录，而且它每轮探测都会失败、拖累整轮。
    """
    _load()
    kind = (body.get("kind") or "url").lower()
    if kind not in ("url", "port"):
        raise ValueError("只能手动添加 URL 或端口监控")
    iid = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()

    # ---- 先校验与归一，不碰 _state ----
    fields = {"kind": kind}
    if not name:
        name = (body.get("target") or "").strip() or "未命名"
    fields["name"] = name[:60]

    if kind == "url":
        target = (body.get("target") or "").strip()
        if not target.lower().startswith(("http://", "https://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        method = (body.get("method") or "GET").upper()
        if method not in ("GET", "HEAD"):
            raise ValueError("请求方法只能是 GET 或 HEAD")
        try:
            expect = int(body.get("expect") or 200)
        except (TypeError, ValueError):
            raise ValueError("期望状态码必须是数字")
        if not (100 <= expect <= 599):
            raise ValueError("期望状态码需在 100–599 之间")
        try:
            timeout = max(1, min(60, int(body.get("timeout") or 10)))
        except (TypeError, ValueError):
            raise ValueError("超时必须是数字")
        fields.update(target=target[:500], method=method, expect=expect, timeout=timeout)
    else:
        host = (body.get("host") or "127.0.0.1").strip()
        if not host:
            raise ValueError("主机不能为空")
        try:
            port = int(body.get("port_num") or 0)
        except (TypeError, ValueError):
            raise ValueError("端口必须是数字")
        if not (1 <= port <= 65535):
            raise ValueError("端口需在 1–65535 之间")
        try:
            timeout = max(1, min(30, int(body.get("timeout") or 5)))
        except (TypeError, ValueError):
            timeout = 5
        fields.update(host=host[:120], port_num=port,
                      target=f"{host}:{port}", timeout=timeout)

    # ---- 校验全过，才动状态 ----
    with _lock:
        items = _state["items"]
        if iid and iid in items:
            it = items[iid]
            if not it.get("manual"):
                raise ValueError("这是系统自动生成的监控项，不能编辑")
        else:
            iid = "m" + os.urandom(4).hex()
            it = _new_item(iid, fields["name"], kind, fields["target"], manual=True)
            items[iid] = it
        it.update(fields)
        if body.get("enabled") is not None:
            it["enabled"] = bool(body.get("enabled"))
        # 改了目标就重置状态机，避免沿用旧目标的失败计数
        it["state"] = "healthy"
        it["fails"] = 0
        it["last_error"] = ""
        it["silence_until"] = 0
        it["alerted_key"] = ""
    _save()
    return {"ok": True, "id": iid}


def delete_item(iid):
    _load()
    with _lock:
        it = _state["items"].get(iid)
        if not it:
            raise ValueError("监控项不存在")
        if not it.get("manual"):
            raise ValueError("系统自动生成的监控项不能删除，请在下方关掉对应联动规则")
        _state["items"].pop(iid, None)
    _save()
    return {"ok": True}


def toggle_item(iid, enabled):
    _load()
    with _lock:
        it = _state["items"].get(iid)
        if not it:
            raise ValueError("监控项不存在")
        it["enabled"] = bool(enabled)
        if not enabled:
            it["state"] = "healthy"
            it["fails"] = 0
    _save()
    return {"ok": True, "enabled": bool(enabled)}


# ---------------- 总览 ----------------

def status():
    _load()
    with _lock:
        cfg = _state
        items = [dict(v) for v in cfg["items"].values()]
        ev = list(_events)
        send_stat = dict(_send_stat)
        test_res = dict(_test_result)
    counts = {"healthy": 0, "retrying": 0, "alerting": 0, "recovered": 0}
    for it in items:
        st = it.get("state") or "healthy"
        if st in counts:
            counts[st] += 1
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "poll_interval": _int_cfg(cfg, "poll_interval", POLL_INTERVAL),
        "fail_threshold": _int_cfg(cfg, "fail_threshold", FAIL_THRESHOLD),
        "silence_min": _int_cfg(cfg, "silence_min", SILENCE_MIN),
        "battery_low": _int_cfg(cfg, "battery_low", BATTERY_LOW),
        "rules": dict(cfg.get("rules") or {}),
        "smtp": smtp_view(),
        "counts": counts,
        "item_total": len(items),
        "manual_total": sum(1 for i in items if i.get("manual")),
        "alert_total": sum(1 for i in items if i.get("state") == "alerting"),
        "event_total": len(ev),
        "unrecovered": sum(1 for e in ev
                           if e.get("kind") == "alert" and not e.get("recovered")),
        "send_stat": send_stat,
        "test": test_res,
        "server_time": int(time.time()),
    }


def save_settings(body):
    _load()
    if body.get("enabled") is not None:
        _state["enabled"] = bool(body.get("enabled"))
    if body.get("poll_interval") is not None:
        try:
            _state["poll_interval"] = max(15, min(3600, int(body.get("poll_interval"))))
        except (TypeError, ValueError):
            raise ValueError("探测间隔必须是数字")
    if body.get("fail_threshold") is not None:
        try:
            _state["fail_threshold"] = max(1, min(10, int(body.get("fail_threshold"))))
        except (TypeError, ValueError):
            raise ValueError("失败次数必须是数字")
    if body.get("silence_min") is not None:
        try:
            _state["silence_min"] = max(0, min(1440, int(body.get("silence_min"))))
        except (TypeError, ValueError):
            raise ValueError("静默期必须是数字")
    if body.get("battery_low") is not None:
        try:
            _state["battery_low"] = max(1, min(100, int(body.get("battery_low"))))
        except (TypeError, ValueError):
            raise ValueError("电量阈值必须是数字")
    if isinstance(body.get("rules"), dict):
        for k, v in body["rules"].items():
            if k in _state["rules"]:
                _state["rules"][k] = bool(v)
    _save()
    return status()


def reset_all_for_tests():
    """测试用：清空内存状态，强制重新从磁盘加载。"""
    global _loaded, _state, _events, _test_result
    with _lock:
        _loaded = False
        _state = None
        _events = []
        _test_result = {"ts": 0, "ok": None, "message": "尚未测试", "detail": ""}
