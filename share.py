"""分享链接：只读、有效期、提取码、可撤销。"""
import os
import secrets
import threading
import time

import core
from core import DATA, sandbox, read_json, write_json

SHARES = os.path.join(DATA, "shares.json")
L = threading.RLock()
FAILS = {}


def rand_code(n=4):
    return "".join(secrets.choice("23456789abcdefghjkmnpqrstuvwxyz") for _ in range(n))


def load():
    return read_json(SHARES, [])


def create(path, hours=24, need_code=True):
    abs_path = sandbox(path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError("文件不存在")
    if os.path.isdir(abs_path):
        raise ValueError("暂不支持分享目录，请先压缩为 zip 再分享")
    hours = int(hours or 24)
    if hours <= 0:
        hours = 24
    if hours > 24 * 30:
        hours = 24 * 30
    s = {
        "token": secrets.token_hex(12), "path": abs_path,
        "name": os.path.basename(abs_path), "size": os.path.getsize(abs_path),
        "created_at": int(time.time()), "expires_at": int(time.time()) + hours * 3600,
        "code": rand_code(4) if need_code else "", "downloads": 0,
    }
    with L:
        items = load()
        items.append(s)
        write_json(SHARES, items)
    return s


def listing():
    now = int(time.time())
    out = []
    for s in load():
        out.append({**s, "expired": s["expires_at"] < now})
    return out


def revoke(token):
    with L:
        write_json(SHARES, [s for s in load() if s["token"] != token])
    return {"ok": "1"}


def find(token):
    for s in load():
        if s["token"] == token:
            return s
    return None


def unlock(token, code, bump=False):
    """校验有效期与提取码；成功返回分享记录，失败抛异常"""
    s = find(token)
    if not s:
        raise FileNotFoundError("分享不存在或已撤销")
    if time.time() > s["expires_at"]:
        raise TimeoutError("分享已过期")
    if s["code"]:
        with L:
            f = FAILS.get(token)
            if f and f[0] >= 10 and time.time() < f[1]:
                raise PermissionError("提取码错误次数过多，请 10 分钟后再试")
        if code != s["code"]:
            with L:
                cnt = (FAILS.get(token, (0, 0))[0] + 1) if FAILS.get(token) else 1
                FAILS[token] = (cnt, time.time() + 600)
            raise PermissionError("提取码错误")
        with L:
            FAILS.pop(token, None)
    if bump:
        with L:
            items = load()
            for it in items:
                if it["token"] == token:
                    it["downloads"] += 1
                    break
            write_json(SHARES, items)
    return s


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
 background:#080b11;color:#e7eef9;font-family:-apple-system,"PingFang SC",sans-serif}
.card{width:100%%;max-width:380px;background:#101722;border:1px solid #1e2a3a;border-radius:16px;padding:24px}
.logo{width:40px;height:40px;border-radius:11px;background:linear-gradient(140deg,#4d8dff,#22d3ee);
 display:grid;place-items:center;color:#05121f;font-weight:800;margin-bottom:14px}
h1{font-size:16px;margin:0 0 6px;word-break:break-all}
p{margin:0 0 18px;font-size:12px;color:#5f7089}
input{width:100%%;height:40px;border-radius:9px;border:1px solid #1e2a3a;background:#0c1118;color:#e7eef9;
 padding:0 12px;outline:none;font-size:15px;letter-spacing:4px;text-align:center}
button{width:100%%;height:40px;margin-top:12px;border:0;border-radius:9px;background:#4d8dff;color:#fff;
 font-weight:600;font-size:14px}
.meta{font-size:11.5px;color:#5f7089;margin-top:14px;line-height:1.7}
</style></head><body>
<div class="card">
  <div class="logo">P</div>
  <h1>__NAME__</h1>
  <p>PocketPanel 分享 · __SIZE__</p>
  __BODY__
  <div class="meta">有效期至 __EXP__<br>只读分享，链接可被分享者随时撤销。</div>
</div>
</body></html>"""


def page(token):
    s = find(token)
    if not s or time.time() > s["expires_at"]:
        return PAGE.replace("__NAME__", "分享已失效").replace("__SIZE__", "-") \
            .replace("__BODY__", "<p>链接不存在或已过期，请联系分享者重新生成。</p>") \
            .replace("__EXP__", "—")
    size = core.human_size(s["size"])
    if s["code"]:
        body = (f'<input id="code" maxlength="4" placeholder="提取码" autocomplete="off">'
                f'<button onclick="location.href=\'/api/public/share/{token}/download?code=\'+'
                f'encodeURIComponent(document.getElementById(\'code\').value.trim())">下载</button>')
    else:
        body = f'<button onclick="location.href=\'/api/public/share/{token}/download\'">下载文件</button>'
    return (PAGE.replace("__NAME__", s["name"]).replace("__SIZE__", size)
            .replace("__BODY__", body)
            .replace("__EXP__", time.strftime("%Y-%m-%d %H:%M", time.localtime(s["expires_at"]))))
