"""AI 网关：Provider 管理、模型路由、四协议转发。

这个模块只做「聚合与转发」，不做任何场景化 AI。设计上最麻烦的三件事：

1. **同协议必须零拷贝**。20MB 的 base64 图片如果先 `json.loads` 再
   `json.dumps`，手机内存会瞬间翻两三倍。这里的做法是从 `rfile` 读前 4KB
   当探针，只改 `model` 字段，然后把「探针 + 剩余字节流」喂给
   `http.client`，全程 64KB 一块。
2. **跨协议才允许全量反序列化**，因此有独立的、更小的上限（默认 10MB）。
3. **进程不能因为一个请求死掉**。`MemoryError` 要变成 507，客户端断开要
   立刻掐掉上游连接，否则手机上的 Uvicorn/ThreadingHTTPServer 会被慢慢拖死。

项目本身是零第三方依赖的（Termux 上装不动 FastAPI/httpx），所以这里用
`http.client` + `ThreadingHTTPServer` 实现同等语义：
`request.stream()` → `BodyReader`，`httpx.AsyncClient` → `HTTPConnection`，
`request.is_disconnected()` → `ClientWatch`（`select + MSG_PEEK`）。
"""
import http.client
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import traceback
import urllib.parse

import core
import gateway_ir as G
import secretbox

MB = 1024 * 1024
DEFAULT_SETTINGS = {
    "max_body_mb": 50,
    "cross_max_mb": 10,
    "upstream_timeout": 300,
    "fallback_provider": "",
}

DATA_DIR = None          # 测试可覆盖；None 时跟 core.DATA
DB_NAME = "gateway.db"
SECRET_NAME = "secret.key"
CONNECT_TIMEOUT = 20
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": ("Authorization, Content-Type, x-api-key, "
                                     "anthropic-version, anthropic-beta, "
                                     "x-goog-api-key, openai-beta"),
    "Access-Control-Max-Age": "86400",
}

_lock = threading.RLock()
_db_ready = False


# =====================================================================
# 存储
# =====================================================================

def data_dir():
    d = DATA_DIR or core.DATA
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def db_path():
    return os.path.join(data_dir(), DB_NAME)


def secret_path():
    return os.path.join(data_dir(), SECRET_NAME)


def _master():
    return secretbox.load_key(secret_path())


def _conn():
    c = sqlite3.connect(db_path(), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=8000")
    return c


def ensure_ready():
    global _db_ready
    with _lock:
        if _db_ready:
            return
        data_dir()
        with _conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                key_enc TEXT NOT NULL DEFAULT '',
                protocols TEXT NOT NULL DEFAULT '[]',
                default_protocol TEXT NOT NULL DEFAULT 'openai_chat',
                models TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS usage (
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                protocol TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                err INTEGER NOT NULL DEFAULT 0,
                in_tok INTEGER NOT NULL DEFAULT 0,
                out_tok INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, provider, protocol)
            );
            CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);
            """)
            c.execute("PRAGMA user_version=1")
        _db_ready = True


def _row_provider(r):
    p = dict(r)
    try:
        p["protocols"] = json.loads(p.get("protocols") or "[]")
    except ValueError:
        p["protocols"] = []
    p["models"] = [x for x in (p.get("models") or "").splitlines() if x.strip()]
    p["enabled"] = bool(p.get("enabled"))
    return p


def _provider_raw(pid=None):
    ensure_ready()
    with _conn() as c:
        if pid is None:
            rows = c.execute("SELECT * FROM providers ORDER BY created ASC").fetchall()
            return [_row_provider(r) for r in rows]
        r = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        return _row_provider(r) if r else None


def _decrypt_key(p):
    enc = p.get("key_enc") or ""
    p["api_key"] = ""
    p["key_error"] = ""
    if not enc:
        return p
    try:
        p["api_key"] = secretbox.decrypt_str(_master(), enc)
    except (ValueError, OSError) as e:
        p["key_error"] = str(e)
    return p


def _masked_provider(p):
    key = p.get("api_key") or ""
    return {
        "id": p["id"], "name": p["name"], "base_url": p["base_url"],
        "protocols": p.get("protocols") or [],
        "default_protocol": p.get("default_protocol") or "",
        "models": p.get("models") or [],
        "enabled": bool(p.get("enabled")),
        "note": p.get("note") or "",
        "created": p.get("created") or 0,
        "updated": p.get("updated") or 0,
        "has_key": bool(p.get("key_enc")),
        "key_masked": secretbox.mask(key) if key else "",
        "key_error": p.get("key_error") or "",
    }


def list_providers():
    rows = _provider_raw()
    out = []
    for p in rows:
        _decrypt_key(p)
        out.append(_masked_provider(p))
    return out


def get_provider(pid):
    p = _provider_raw(pid)
    if not p:
        raise GatewayError(404, f"Provider {pid} 不存在")
    return _decrypt_key(p)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "p" + secrets.token_hex(3)
    return s[:40]


def _norm_protocols(v):
    raw = v if isinstance(v, list) else str(v or "").replace("，", ",").split(",")
    out = []
    for x in raw:
        p = G.canon_protocol(str(x))
        if p and p not in out:
            out.append(p)
    return out


def _norm_models(v):
    if isinstance(v, str):
        raw = re.split(r"[\n,]+", v)
    else:
        raw = v or []
    out = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s[:300])
    return out


def save_provider(payload):
    ensure_ready()
    p = payload or {}
    name = str(p.get("name") or "").strip()
    base = str(p.get("base_url") or "").strip().rstrip("/")
    if not name:
        raise GatewayError(400, "Provider 名称不能为空")
    if len(name) > 60:
        raise GatewayError(400, "名称最多 60 个字符")
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise GatewayError(400, "Base URL 要以 http:// 或 https:// 开头，并且带主机名")
    if u.username or u.password:
        raise GatewayError(400, "Base URL 里不要带用户名密码，Key 请单独填")
    protocols = _norm_protocols(p.get("protocols"))
    if not protocols:
        raise GatewayError(400, "至少选择一种协议")
    default = G.canon_protocol(str(p.get("default_protocol") or ""))
    if default not in protocols:
        default = protocols[0]
    models = _norm_models(p.get("models"))
    pid = _slug(p.get("id") or name)
    now = time.time()
    key = p.get("api_key")
    with _lock, _conn() as c:
        old = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        enc = old["key_enc"] if old else ""
        if p.get("clear_key"):
            enc = ""
        elif key not in (None, "") and "••" not in str(key):
            enc = secretbox.encrypt_str(_master(), str(key).strip())
        if old:
            c.execute("""UPDATE providers SET name=?, base_url=?, key_enc=?, protocols=?,
                         default_protocol=?, models=?, enabled=?, note=?, updated=?
                         WHERE id=?""",
                      (name, base, enc, json.dumps(protocols), default,
                       "\n".join(models), 1 if p.get("enabled", True) else 0,
                       str(p.get("note") or "")[:500], now, pid))
        else:
            c.execute("""INSERT INTO providers
                         (id,name,base_url,key_enc,protocols,default_protocol,models,
                          enabled,note,created,updated)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (pid, name, base, enc, json.dumps(protocols), default,
                       "\n".join(models), 1 if p.get("enabled", True) else 0,
                       str(p.get("note") or "")[:500], now, now))
    return _masked_provider(get_provider(pid))


def delete_provider(pid):
    ensure_ready()
    with _lock, _conn() as c:
        n = c.execute("DELETE FROM providers WHERE id=?", (pid,)).rowcount
    if not n:
        raise GatewayError(404, f"Provider {pid} 不存在")
    return {"ok": True}


# =====================================================================
# 设置 / Token
# =====================================================================

def _get_setting(k, default=None):
    ensure_ready()
    with _conn() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _set_setting(k, v):
    ensure_ready()
    with _lock, _conn() as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def _int_setting(k):
    try:
        return int(_get_setting(k, DEFAULT_SETTINGS[k]))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[k])


def access_token():
    enc = _get_setting("access_token", "") or ""
    if not enc:
        return ""
    try:
        return secretbox.decrypt_str(_master(), enc)
    except (ValueError, OSError):
        return ""


def rotate_token():
    tok = "sk-pp-" + secrets.token_urlsafe(30)
    _set_setting("access_token", secretbox.encrypt_str(_master(), tok))
    return tok


def settings_view():
    tok = access_token()
    return {
        "access_token": tok,
        "token_masked": secretbox.mask(tok) if tok else "",
        "has_token": bool(tok),
        "max_body_mb": _int_setting("max_body_mb"),
        "cross_max_mb": _int_setting("cross_max_mb"),
        "upstream_timeout": _int_setting("upstream_timeout"),
        "fallback_provider": _get_setting("fallback_provider", "") or "",
    }


def save_settings(p):
    p = p or {}
    for k, lo, hi in (("max_body_mb", 1, 100), ("cross_max_mb", 1, 50),
                      ("upstream_timeout", 10, 3600)):
        if k in p:
            try:
                v = int(p[k])
            except (TypeError, ValueError):
                raise GatewayError(400, f"{k} 必须是数字")
            if v < lo or v > hi:
                raise GatewayError(400, f"{k} 需在 {lo}–{hi} 之间")
            _set_setting(k, v)
    if "fallback_provider" in p:
        fp = str(p.get("fallback_provider") or "").strip()
        if fp:
            row = _provider_raw(fp)
            if not row:
                raise GatewayError(400, "兜底 Provider 不存在")
        _set_setting("fallback_provider", fp)
    if p.get("rotate_token"):
        rotate_token()
    return settings_view()


# =====================================================================
# 用量
# =====================================================================

def record_usage(provider, protocol, ok, usage=None, calls=1):
    try:
        ensure_ready()
        day = time.strftime("%Y-%m-%d")
        u = usage or {}
        with _lock, _conn() as c:
            c.execute("""INSERT INTO usage(day,provider,protocol,calls,ok,err,in_tok,out_tok)
                         VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(day,provider,protocol) DO UPDATE SET
                           calls=calls+excluded.calls, ok=ok+excluded.ok, err=err+excluded.err,
                           in_tok=in_tok+excluded.in_tok, out_tok=out_tok+excluded.out_tok""",
                      (day, provider, protocol, calls, 1 if ok else 0, 0 if ok else 1,
                       int(u.get("input") or 0), int(u.get("output") or 0)))
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def usage_report(days=14):
    ensure_ready()
    days = max(1, min(90, int(days or 14)))
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with _conn() as c:
        rows = c.execute("""SELECT day, SUM(calls) calls, SUM(ok) ok, SUM(err) err,
                                   SUM(in_tok) in_tok, SUM(out_tok) out_tok
                            FROM usage WHERE day>=? GROUP BY day ORDER BY day""",
                         (start,)).fetchall()
        by_p = c.execute("""SELECT provider, SUM(calls) calls, SUM(ok) ok, SUM(err) err,
                                   SUM(in_tok) in_tok, SUM(out_tok) out_tok
                            FROM usage WHERE day>=? GROUP BY provider
                            ORDER BY calls DESC""", (start,)).fetchall()
        total = c.execute("""SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(in_tok),0) in_tok,
                                    COALESCE(SUM(out_tok),0) out_tok
                             FROM usage""").fetchone()
    m = {r["day"]: r for r in rows}
    series = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        r = m.get(d)
        series.append({"day": d, "calls": r["calls"] if r else 0,
                       "ok": r["ok"] if r else 0, "err": r["err"] if r else 0,
                       "in_tok": r["in_tok"] if r else 0,
                       "out_tok": r["out_tok"] if r else 0})
    names = {p["id"]: p["name"] for p in _provider_raw()}
    prov = [dict(r) | {"name": names.get(r["provider"], r["provider"])} for r in by_p]
    today = series[-1] if series else {}
    return {"days": series, "by_provider": prov, "today": today,
            "total": dict(total) if total else {"calls": 0, "in_tok": 0, "out_tok": 0}}


# =====================================================================
# 模型目录与路由
# =====================================================================

def _model_entries(p):
    out = []
    for line in p.get("models") or []:
        s = str(line).strip()
        if not s:
            continue
        alias, _, target = s.partition("=")
        out.append((alias.strip(), target.strip()))
    return out


def model_catalog():
    bare, prefixed, detail = [], [], []
    for p in _provider_raw():
        if not p.get("enabled"):
            continue
        for alias, target in _model_entries(p):
            if not alias:
                continue
            real = target or alias
            if real == "*":
                real = alias
            pid = f"{p['id']}/{alias}"
            if alias == "*":
                # 通配兜底：不进裸模型名列表（`*` 不是能调用的模型名），
                # 但要登记成 provider/*，否则只配了通配的 Provider 在
                # 模型清单和测试页里会「隐形」
                if pid not in prefixed:
                    prefixed.append(pid)
                detail.append({"id": "*", "provider": p["id"], "provider_name": p["name"],
                               "upstream": real, "protocols": p.get("protocols") or [],
                               "route": pid, "wildcard": True})
                continue
            if alias not in bare:
                bare.append(alias)
            prefixed.append(pid)
            detail.append({"id": alias, "provider": p["id"], "provider_name": p["name"],
                           "upstream": real, "protocols": p.get("protocols") or [],
                           "route": pid})
    return {"bare": bare, "prefixed": prefixed, "items": detail}


def _map_model(p, model):
    """返回 (上游模型名, 是否精确匹配)；未匹配返回 (None, False)。"""
    for alias, target in _model_entries(p):
        if alias == "*" or alias.lower() == model.lower():
            real = target or model
            if real == "*":
                real = model
            real = real.replace("{model}", model)
            return real, alias != "*"
    return None, False


def resolve(model, source_proto):
    model = (model or "").strip()
    if not model:
        raise GatewayError(400, "请求里缺少 model")
    rows = _provider_raw()
    enabled = [p for p in rows if p.get("enabled")]

    # 1) provider/model 显式前缀（也认名称转成的 slug）
    if "/" in model:
        pre, _, rest = model.partition("/")
        for p in rows:
            if p["id"] == pre or _slug(p["name"]) == pre:
                if not p.get("enabled"):
                    raise GatewayError(403, f"Provider {p['id']} 已禁用")
                if not rest:
                    raise GatewayError(400, "前缀后面缺少模型名")
                real, _ = _map_model(p, rest)
                return _route(p, real or rest, source_proto)

    # 2) 裸模型名：优先选原生支持该协议的 Provider（同协议透传）
    best = None
    for i, p in enumerate(enabled):
        real, exact = _map_model(p, model)
        if real is None:
            continue
        # 精确模型名优先于泛匹配：`*` 兜底的 Provider 不应该抢走别家
        # 明确声明过的模型。同精度下再优先同协议透传。
        score = (0 if exact else 1,
                 0 if source_proto in (p.get("protocols") or []) else 1, i)
        if best is None or score < best[0]:
            best = (score, p, real)
    if best:
        return _route(best[1], best[2], source_proto)

    # 3) 兜底 Provider / 只有一家时可省略前缀
    fp = _get_setting("fallback_provider", "") or ""
    if fp:
        for p in enabled:
            if p["id"] == fp or _slug(p["name"]) == fp:
                return _route(p, model, source_proto)
    if len(enabled) == 1:
        return _route(enabled[0], model, source_proto)

    names = [x["id"] for x in model_catalog()["items"][:12]]
    hint = f"可用模型：{', '.join(names)}" if names else "还没有 Provider 配置模型列表"
    raise GatewayError(404, f"没有 Provider 能处理模型 {model}。{hint}")


def _route(p, upstream_model, source_proto):
    # resolve() 拿的是库里未解密的行，这里补一次，且用副本避免把明文
    # 留在长时间驻留的 provider 列表里。
    p = dict(p)
    if p.get("key_enc") and not p.get("api_key"):
        _decrypt_key(p)
    protocols = p.get("protocols") or []
    up = source_proto if source_proto in protocols else (
        p.get("default_protocol") if p.get("default_protocol") in protocols else
        (protocols[0] if protocols else source_proto))
    return {"provider": p, "upstream_model": upstream_model,
            "protocol": up, "passthrough": up == source_proto}


# =====================================================================
# HTTP 小工具
# =====================================================================

class GatewayError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


class BodyTooLarge(GatewayError):
    def __init__(self, limit, actual=None):
        msg = f"请求体超过 {limit // MB if limit >= MB else limit}MB 上限"
        if actual:
            msg += f"（实际 {actual / MB:.1f}MB）"
        super().__init__(413, msg)
        self.limit = limit


class UpstreamError(Exception):
    def __init__(self, message, code=502, status=0, retry_after=""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def _cors(h, extra=None):
    for k, v in CORS_HEADERS.items():
        h.send_header(k, v)
    for k, v in (extra or {}).items():
        h.send_header(k, v)


def send_json(h, code, obj, extra=None):
    if getattr(h, "_gw_started", False):
        h.close_connection = True
        return
    body = _json_bytes(obj)
    try:
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        if code == 401:
            h.send_header("WWW-Authenticate", "Bearer")
        _cors(h, extra)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)
        h._gw_started = True
    except (BrokenPipeError, ConnectionResetError, OSError):
        h.close_connection = True


def _preflight(h):
    try:
        h.send_response(204)
        _cors(h)
        h.send_header("Content-Length", "0")
        h.end_headers()
    except OSError:
        pass


class Down:
    """下游响应写出器。需要流式时用 chunked，避免提前算长度。"""

    def __init__(self, h, status, headers=None, chunked=True):
        self.h = h
        self.status = status
        self.headers = headers or {}
        self.chunked = chunked and h.request_version != "HTTP/1.0"
        self.nbytes = 0
        self.started = False

    def start(self):
        h = self.h
        h.send_response(self.status)
        for k, v in self.headers.items():
            h.send_header(k, v)
        _cors(h)
        if self.chunked:
            h.send_header("Transfer-Encoding", "chunked")
        else:
            h.send_header("Connection", "close")
            h.close_connection = True
        h.end_headers()
        h._gw_started = True
        self.started = True

    def write(self, data, flush=True):
        if not data:
            return
        h = self.h
        try:
            if self.chunked:
                h.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            else:
                h.wfile.write(data)
            if flush:
                h.wfile.flush()
            self.nbytes += len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ClientGone()

    def end(self):
        if not self.started:
            return
        try:
            if self.chunked:
                self.h.wfile.write(b"0\r\n\r\n")
                self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class ClientGone(Exception):
    pass


class ClientWatch:
    """客户端断开检测：只看 socket 是否 EOF，不消费数据。"""

    def __init__(self, sock, on_abort):
        self.sock = sock
        self.on_abort = on_abort
        self.stop = threading.Event()
        self.aborted = False
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        import select
        while not self.stop.wait(0.35):
            try:
                r, _, _ = select.select([self.sock], [], [], 0)
                if not r:
                    continue
                if self.sock.recv(1, socket.MSG_PEEK) == b"":
                    self.aborted = True
                    try:
                        self.on_abort()
                    except Exception:  # noqa: BLE001
                        pass
                    return
            except (OSError, ValueError):
                self.aborted = True
                return

    def close(self):
        self.stop.set()


# =====================================================================
# 请求体读取
# =====================================================================

class BodyReader:
    """按块读取请求体；支持 Content-Length 与 chunked。

    `probe(4096)` 之后，那 4KB 会存在 `pending` 里，`iter_rest()` 会先吐它。
    已知长度时不会预读超过 Content-Length，chunked 则按分块边界读。
    """

    def __init__(self, h, limit):
        self.h = h
        self.rfile = h.rfile
        self.limit = int(limit)
        self.consumed = 0
        self.pending = b""
        self.finished = False
        self._chunk_left = 0
        te = (h.headers.get("Transfer-Encoding") or "").lower()
        self.chunked = "chunked" in te
        cl = h.headers.get("Content-Length")
        try:
            self.length = int(cl) if cl is not None and not self.chunked else None
        except ValueError:
            self.length = 0
        if self.length is None and not self.chunked:
            self.length = 0

    def _raw(self, n):
        d = self.rfile.read(n)
        return d or b""

    def _read_chunked(self, n):
        if self._chunk_left == 0:
            line = self.rfile.readline(65536)
            if not line:
                self.finished = True
                return b""
            line = line.strip()
            if not line:
                return b""
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                raise GatewayError(400, "chunked 编码错误")
            if size == 0:
                while True:
                    t = self.rfile.readline(65536)
                    if t in (b"\r\n", b"\n", b""):
                        break
                self.finished = True
                return b""
            self._chunk_left = size
        d = self._raw(min(n, self._chunk_left))
        self._chunk_left -= len(d)
        if self._chunk_left == 0:
            self._raw(2)
        return d

    def read_some(self, n=65536):
        if self.pending:
            d, self.pending = self.pending[:n], self.pending[n:]
            return d
        if self.finished:
            return b""
        if self.chunked:
            d = self._read_chunked(n)
        else:
            left = (self.length or 0) - self.consumed
            if left <= 0:
                return b""
            d = self._raw(min(n, left))
        if d:
            self.consumed += len(d)
            if self.consumed > self.limit:
                raise BodyTooLarge(self.limit, self.consumed)
        return d

    def probe(self, n=4096):
        """预读前 n 字节，用于在不解完整 JSON 的前提下捞 model / stream。

        返回的字节**仍留在 pending 里**，即后续 `read_all()` 会从它开始读，
        天然拿到完整请求体（转换路径用这个）。
        透传路径要把这 n 字节换掉再原样转发，取走后就调 `drop_pending()`，
        否则 `iter_rest()` 会把原件又吐一遍（重复 4KB）。
        """
        buf = b""
        while len(buf) < n:
            d = self.read_some(min(8192, n - len(buf)))
            if not d:
                break
            buf += d
        self.pending = buf
        return buf

    def drop_pending(self):
        """丢弃待发缓冲 —— 调用方已持有 probe 返回值并会自己重发时用。"""
        self.pending = b""

    def iter_rest(self, size=65536):
        while True:
            d = self.read_some(size)
            if not d:
                break
            yield d

    def read_all(self, limit=None):
        cap = int(limit or self.limit)
        if self.length is not None and self.length > cap:
            raise BodyTooLarge(cap, self.length)
        buf = bytearray()
        for d in self.iter_rest():
            buf.extend(d)
            if len(buf) > cap:
                raise BodyTooLarge(cap, len(buf))
        return bytes(buf)

    def discard(self, limit=32 * 1024 * 1024, timeout=5.0):
        """读掉并丢弃剩余请求体。

        返回 413 时如果直接把连接一关了事，客户端还在往里写数据，
        内核会回 RST，客户端缓冲区里已经收到的响应内容会一起丢掉——
        表现就是「状态码 413，但响应体是空的」。
        先把剩下的读完（丢掉），客户端能写完、也能完整拿到错误响应。

        有上限：最多丢 limit 字节或 timeout 秒，防止被超大 body 拖住。
        """
        deadline = time.time() + timeout
        total = 0
        while total < limit and time.time() < deadline:
            if self.chunked:
                d = self._read_chunked(65536)
            else:
                left = (self.length or 0) - self.consumed
                if left <= 0:
                    break
                d = self._raw(min(65536, left))
                self.consumed += len(d)
            if not d:
                break
            total += len(d)
        self.finished = True
        return total


# =====================================================================
# 探针
# =====================================================================

_MODEL_RE = re.compile(rb'"model"\s*:\s*"((?:[^"\\]|\\.)*)"')
_STREAM_RE = re.compile(rb'"stream"\s*:\s*(true|false)')


def _probe_model(head):
    m = _MODEL_RE.search(head or b"")
    if not m:
        return ""
    try:
        return json.loads(b'"' + m.group(1) + b'"')
    except ValueError:
        return m.group(1).decode("utf-8", "replace")


def _probe_stream(head):
    try:
        obj = json.loads(head or b"{}")
        if isinstance(obj, dict):
            return bool(obj.get("stream"))
    except ValueError:
        pass
    m = _STREAM_RE.search(head or b"")
    return bool(m and m.group(1) == b"true")


def _patch_model(head, old, new):
    if not new or new == old:
        return head
    m = _MODEL_RE.search(head or b"")
    if not m:
        return None
    enc = json.dumps(new, ensure_ascii=False)[1:-1].encode()
    return head[:m.start(1)] + enc + head[m.end(1):]


# =====================================================================
# 上游
# =====================================================================

def _join(base, path):
    b = base.rstrip("/")
    if b.endswith("/v1") and path.startswith("/v1/"):
        return b[:-3] + path
    if b.endswith("/v1beta") and path.startswith("/v1beta/"):
        return b[:-7] + path
    return b + path


def upstream_url(p, proto, model, stream=False):
    base = p["base_url"].rstrip("/")
    if proto == "openai_chat":
        return _join(base, "/chat/completions")
    if proto == "openai_responses":
        return _join(base, "/responses")
    if proto == "anthropic":
        return _join(base, "/v1/messages")
    if proto == "gemini":
        b = base[:-7] if base.endswith("/v1beta") else base
        action = "streamGenerateContent" if stream else "generateContent"
        q = "?alt=sse" if stream else ""
        return f"{b}/v1beta/models/{urllib.parse.quote(model, safe='/-_.')}:{action}{q}"
    raise GatewayError(500, f"未知上游协议 {proto}")


def _provider_headers(p, proto, downstream=None):
    key = p.get("api_key") or ""
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "Accept-Encoding": "identity", "User-Agent": "PocketPanel-AI/1.0",
         "Connection": "close"}
    if proto in ("openai_chat", "openai_responses"):
        h["Authorization"] = f"Bearer {key}"
        beta = (downstream or {}).get("openai-beta")
        if beta:
            h["OpenAI-Beta"] = beta
    elif proto == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = (downstream or {}).get("anthropic-version") or "2023-06-01"
        beta = (downstream or {}).get("anthropic-beta")
        if beta:
            h["anthropic-beta"] = beta
    elif proto == "gemini":
        h["x-goog-api-key"] = key
    return h


def _http_conn(url, timeout=CONNECT_TIMEOUT):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UpstreamError("上游地址不合法")
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    return conn, path


def _request_upstream(p, proto, model, stream, body_iter, length, timeout,
                      downstream=None, chunked=False):
    url = upstream_url(p, proto, model, stream)
    conn, path = _http_conn(url)
    hdr = _provider_headers(p, proto, downstream)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in hdr.items():
            conn.putheader(k, v)
        if chunked:
            conn.putheader("Transfer-Encoding", "chunked")
        else:
            conn.putheader("Content-Length", str(length or 0))
        conn.endheaders()
        if conn.sock:
            conn.sock.settimeout(None)      # 上传不设写超时，慢网络传大图是常态
        for chunk in body_iter:
            if not chunk:
                continue
            if chunked:
                conn.send(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            else:
                conn.send(chunk)
        if chunked:
            conn.send(b"0\r\n\r\n")
        if conn.sock:
            conn.sock.settimeout(timeout)
        resp = conn.getresponse()
        return conn, resp
    except (socket.timeout, TimeoutError) as e:
        conn.close()
        raise UpstreamError(f"上游超时：{e}", 504) from e
    except ssl.SSLError as e:
        conn.close()
        raise UpstreamError(f"上游 TLS 失败：{e}", 502) from e
    except (ConnectionError, OSError, http.client.HTTPException) as e:
        conn.close()
        if isinstance(e, (BrokenPipeError, ConnectionResetError)) and \
                getattr(conn, "sock", None) is None:
            raise ClientGone() from e
        raise UpstreamError(f"连不上上游：{e}", 502) from e


def _read_body(resp, cap=32 * MB):
    buf = bytearray()
    while True:
        d = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not d:
            break
        buf.extend(d)
        if len(buf) > cap:
            raise UpstreamError(f"上游响应超过 {cap // MB}MB")
    return bytes(buf)


def _upstream_message(raw):
    try:
        o = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw[:500].decode("utf-8", "replace")
    if isinstance(o, dict):
        e = o.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("type") or e)
        if isinstance(e, str):
            return e
        if o.get("message"):
            return str(o["message"])
    return raw[:500].decode("utf-8", "replace")


# =====================================================================
# 用量嗅探（透传时不解析 JSON，只在滚动窗口里找数字）
# =====================================================================

class UsageSniffer:
    def __init__(self):
        self.tail = bytearray()
        self.usage = {"input": 0, "output": 0, "cached": 0}

    def feed(self, data):
        if not data:
            return
        self.tail.extend(data)
        if len(self.tail) > 16384:
            del self.tail[:-16384]
        t = bytes(self.tail)
        for pat, key in ((rb'"prompt_tokens"\s*:\s*(\d+)', "input"),
                         (rb'"input_tokens"\s*:\s*(\d+)', "input"),
                         (rb'"promptTokenCount"\s*:\s*(\d+)', "input"),
                         (rb'"completion_tokens"\s*:\s*(\d+)', "output"),
                         (rb'"output_tokens"\s*:\s*(\d+)', "output"),
                         (rb'"candidatesTokenCount"\s*:\s*(\d+)', "output"),
                         (rb'"cached_tokens"\s*:\s*(\d+)', "cached")):
            for m in re.finditer(pat, t):
                try:
                    self.usage[key] = max(self.usage[key], int(m.group(1)))
                except ValueError:
                    pass

    def result(self):
        u = dict(self.usage)
        u["total"] = u["input"] + u["output"]
        return u


# =====================================================================
# 鉴权
# =====================================================================

def _presented_token(h):
    auth = h.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for k in ("x-api-key", "x-goog-api-key"):
        v = h.headers.get(k)
        if v:
            return v.strip()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    if q.get("key"):
        return q["key"][0]
    return ""


def check_token(h):
    saved = access_token()
    if not saved:
        raise GatewayError(503, "网关还没有生成访问 Token，请到「AI 网关 → 设置」里生成")
    got = _presented_token(h)
    if not got or not secrets.compare_digest(got, saved):
        raise GatewayError(401, "访问 Token 无效")
    return True


# =====================================================================
# 路径与入口
# =====================================================================

_GEMINI_RE = re.compile(r"^/v1beta/models/(.+):(generateContent|streamGenerateContent)$")


def _match_path(path):
    if path == "/v1/chat/completions":
        return "openai_chat", "", False
    if path == "/v1/responses":
        return "openai_responses", "", False
    if path == "/v1/messages":
        return "anthropic", "", False
    m = _GEMINI_RE.match(path)
    if m:
        return "gemini", urllib.parse.unquote(m.group(1)), m.group(2) == "streamGenerateContent"
    return None


def handle(h):
    t0 = time.time()
    path = urllib.parse.urlsplit(h.path).path
    body = None
    try:
        if h.command == "OPTIONS":
            _preflight(h)
            return
        if h.command == "GET" and path in ("/v1/models", "/v1beta/models"):
            check_token(h)
            _model_list(h, path)
            return
        hit = _match_path(path)
        if not hit:
            send_json(h, 404, G.error_payload("openai_chat", 404, f"未知接口 {path}"))
            return
        proto, path_model, gemini_stream = hit
        check_token(h)
        max_body = _int_setting("max_body_mb") * MB
        body = BodyReader(h, max_body)
        if body.length is not None and body.length > max_body:
            h.close_connection = True
            body.discard()
            raise BodyTooLarge(max_body, body.length)

        head = body.probe(4096)
        model = path_model or _probe_model(head)
        if not model:
            raise GatewayError(400, "请求体前 4KB 里找不到 model 字段")
        stream_req = gemini_stream or _probe_stream(head)
        route = resolve(model, proto)
        p = route["provider"]
        upstream_model = route["upstream_model"]

        if route["passthrough"]:
            _do_passthrough(h, body, head, proto, route, model, upstream_model,
                            stream_req, t0)
        else:
            cross = _int_setting("cross_max_mb") * MB
            if body.length is not None and body.length > cross:
                h.close_connection = True
                body.discard()
                raise GatewayError(413, "跨协议转换暂不支持超大请求体"
                                        f"（{body.length / MB:.1f}MB，上限 {cross // MB}MB）")
            raw = body.read_all(cross)
            _do_convert(h, raw, proto, route, model, upstream_model, stream_req, t0)
    except ClientGone:
        h.close_connection = True
    except BodyTooLarge as e:
        h.close_connection = True
        if body is not None:
            body.discard()
        send_json(h, 413, G.error_payload(_guess_proto(path), 413, e.message))
    except GatewayError as e:
        if e.code in (400, 401, 403, 413, 503):
            h.close_connection = True
        if e.code == 413 and body is not None:
            body.discard()
        send_json(h, e.code, G.error_payload(_guess_proto(path), e.code, e.message))
    except MemoryError:
        send_json(h, 507, G.error_payload(_guess_proto(path), 507,
                                          "内存不足，请求已中止。请降低图片体积或 max_body_mb"))
        h.close_connection = True
    except UpstreamError as e:
        send_json(h, e.code, G.error_payload(_guess_proto(path), e.code, e.message))
    except (BrokenPipeError, ConnectionResetError):
        h.close_connection = True
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        send_json(h, 500, G.error_payload(_guess_proto(path), 500, f"网关内部错误：{e}"))
        h.close_connection = True
    finally:
        if path.startswith("/v1"):
            print(f"[ai] {h.command} {path} {getattr(h, '_gw_provider', '-')} "
                  f"{getattr(h, '_gw_model', '-')} {getattr(h, '_gw_status', '-')} "
                  f"{int((time.time() - t0) * 1000)}ms {getattr(h, '_gw_bytes', 0)}B",
                  flush=True)


def _guess_proto(path):
    hit = _match_path(urllib.parse.urlsplit(path).path)
    return hit[0] if hit else "openai_chat"


def _model_list(h, path):
    cat = model_catalog()
    if path == "/v1/models":
        data = [{"id": x, "object": "model", "created": 0,
                 "owned_by": "pocketpanel"} for x in cat["prefixed"]]
        send_json(h, 200, {"object": "list", "data": data})
        return
    models = []
    for it in cat["items"]:
        models.append({"name": f"models/{it['id']}", "displayName": it["id"],
                       "description": f"{it['provider_name']} · {it['upstream']}"})
    send_json(h, 200, {"models": models})


# =====================================================================
# 同协议透传
# =====================================================================

def _do_passthrough(h, body, head, proto, route, model, upstream_model, stream_req, t0):
    p = route["provider"]
    h._gw_provider, h._gw_model = p["id"], upstream_model
    patched = _patch_model(head, model, upstream_model)
    if patched is None:
        raise GatewayError(400, "model 字段不在请求体前 4KB，无法做零拷贝改名；"
                                "请把 model 放到 JSON 开头")
    delta = len(patched) - len(head)
    length = body.length + delta if body.length is not None else None
    body.drop_pending()

    def gen():
        yield patched
        for d in body.iter_rest():
            yield d

    ds = {"content-type": h.headers.get("Content-Type") or "application/json",
          "anthropic-version": h.headers.get("anthropic-version") or "",
          "anthropic-beta": h.headers.get("anthropic-beta") or "",
          "openai-beta": h.headers.get("OpenAI-Beta") or ""}
    abort = {"conn": None}
    watch = ClientWatch(h.connection, lambda: _kill_conn(abort.get("conn")))
    watch.start()
    conn = None
    try:
        conn, resp = _request_upstream(
            p, route["protocol"], upstream_model, stream_req, gen(), length,
            _int_setting("upstream_timeout"), ds, chunked=body.chunked)
        abort["conn"] = conn
        if watch.aborted:
            return
        ct = resp.getheader("Content-Type") or ("text/event-stream" if stream_req
                                               else "application/json")
        headers = {"Content-Type": ct}
        for k in ("Cache-Control", "X-Request-Id", "Retry-After", "Content-Encoding"):
            v = resp.getheader(k)
            if v:
                headers[k] = v
        if "event-stream" in ct.lower():
            headers["Cache-Control"] = "no-cache"
            headers["X-Accel-Buffering"] = "no"
        down = Down(h, resp.status, headers)
        down.start()
        h._gw_status = resp.status
        sniff = UsageSniffer()
        ok = resp.status < 400
        try:
            while True:
                d = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not d:
                    break
                sniff.feed(d)
                down.write(d)
        except (BrokenPipeError, ConnectionResetError):
            raise ClientGone()
        except (AttributeError, ValueError, OSError, http.client.HTTPException) as e:
            # 客户端断开时 watcher 会主动 close 上游连接，http.client 在
            # 「连接已被外部关闭」的路径上会二次 _close_conn，抛
            # AttributeError: 'NoneType' object has no attribute 'close'。
            # 这不是上游故障，不能当 500 报。
            if watch.aborted or h.close_connection:
                return
            raise UpstreamError(f"读取上游响应失败：{e}")
        down.end()
        h._gw_bytes = down.nbytes
        usage = sniff.result()
        if usage["input"] or usage["output"]:
            record_usage(p["id"], proto, ok, usage)
        else:
            record_usage(p["id"], proto, ok, None)
    finally:
        watch.close()
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _kill_conn(conn):
    if not conn:
        return
    try:
        if conn.sock:
            conn.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


# =====================================================================
# 跨协议转换
# =====================================================================

def _do_convert(h, raw, src_proto, route, model, upstream_model, stream_req, t0):
    p = route["provider"]
    h._gw_provider, h._gw_model = p["id"], upstream_model
    try:
        req = json.loads(raw or b"{}")
    except ValueError:
        raise GatewayError(400, "请求体不是合法 JSON")
    if not isinstance(req, dict):
        raise GatewayError(400, "请求体必须是 JSON 对象")
    try:
        ir = G.parse_request(src_proto, req)
        ir["stream"] = bool(stream_req)
        up_body = G.build_request(route["protocol"], ir, upstream_model)
    except G.ConversionError as e:
        raise GatewayError(400, f"协议转换失败：{e}")
    raw_up = _json_bytes(up_body)
    ds = {"anthropic-version": h.headers.get("anthropic-version") or "",
          "anthropic-beta": h.headers.get("anthropic-beta") or "",
          "openai-beta": h.headers.get("OpenAI-Beta") or ""}
    abort = {"conn": None}
    watch = ClientWatch(h.connection, lambda: _kill_conn(abort.get("conn")))
    watch.start()
    conn = None
    try:
        conn, resp = _request_upstream(
            p, route["protocol"], upstream_model, stream_req, [raw_up], len(raw_up),
            _int_setting("upstream_timeout"), ds)
        abort["conn"] = conn
        if watch.aborted:
            return
        if resp.status >= 400:
            err_raw = _read_body(resp, 2 * MB)
            msg = _upstream_message(err_raw)
            record_usage(p["id"], src_proto, False, None)
            h._gw_status = resp.status
            raise GatewayError(resp.status if resp.status < 600 else 502, msg)

        if stream_req:
            _convert_stream(h, resp, src_proto, route, model, p, t0)
        else:
            resp_raw = _read_body(resp)
            try:
                obj = json.loads(resp_raw)
                ir_resp = G.parse_response(route["protocol"], obj)
                out = G.build_response(src_proto, ir_resp, model)
            except (ValueError, G.ConversionError) as e:
                raise UpstreamError(f"上游返回无法解析：{e}")
            body = _json_bytes(out)
            headers = {"Content-Type": "application/json; charset=utf-8",
                       "Content-Length": str(len(body))}
            down = Down(h, 200, headers, chunked=False)
            down.start()
            down.write(body)
            down.end()
            h._gw_status = 200
            h._gw_bytes = len(body)
            record_usage(p["id"], src_proto, True, ir_resp.get("usage"))
    finally:
        watch.close()
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _events_from_response(ir_resp):
    """把非流式响应包成 IR 事件，用于「请求了 stream、上游却回 JSON」的兜底。"""
    yield {"type": "start", "id": ir_resp.get("id") or "",
           "model": ir_resp.get("model") or "", "usage": ir_resp.get("usage")}
    for part in ir_resp.get("parts") or []:
        if part.get("type") == "text":
            yield {"type": "text", "text": part.get("text") or ""}
        elif part.get("type") == "tool_use":
            yield {"type": "tool_start", "index": 0,
                   "id": part.get("id") or "call_0", "name": part.get("name") or ""}
            yield {"type": "tool_args", "index": 0,
                   "text": json.dumps(part.get("input") or {}, ensure_ascii=False,
                                      separators=(",", ":"))}
            yield {"type": "tool_end", "index": 0}
    yield {"type": "usage", "usage": ir_resp.get("usage") or {}}
    yield {"type": "finish", "reason": ir_resp.get("stop") or "stop"}


def _convert_stream(h, resp, src_proto, route, model, p, t0):
    def chunks():
        while True:
            try:
                d = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            except (AttributeError, ValueError, OSError, http.client.HTTPException):
                # 同上：客户端断开后上游连接被 watcher 关掉，读端会抛
                # http.client 的内部 AttributeError；当作正常 EOF。
                return
            if not d:
                break
            yield d

    gemini_alt = True
    if src_proto == "gemini":
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
        gemini_alt = (q.get("alt") or ["sse"])[0].lower() == "sse"

    ct = (resp.getheader("Content-Type") or "").lower()
    if "event-stream" not in ct:
        # 有些兼容服务无视 stream:true 直接回整包 JSON，不能让它变成空回复
        raw = _read_body(resp)
        try:
            obj = json.loads(raw)
            ir_resp = G.parse_response(route["protocol"], obj)
        except (ValueError, G.ConversionError) as e:
            raise UpstreamError(f"上游返回无法解析：{e}")
        events = _events_from_response(ir_resp)
    else:
        events = G.parse_stream(route["protocol"], chunks())

    seen = {"usage": None, "finish": None}

    def tap(it):
        for ev in it:
            if ev.get("type") == "usage":
                seen["usage"] = G._merge_usage(seen["usage"], ev.get("usage"))
            elif ev.get("type") == "finish":
                seen["finish"] = ev.get("reason")
            yield ev

    frames = G.build_stream(src_proto, tap(events), model, sse=gemini_alt)
    headers = {"Content-Type": "text/event-stream; charset=utf-8",
               "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if src_proto == "gemini" and not gemini_alt:
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Cache-Control": "no-cache"}
    down = Down(h, 200, headers)
    down.start()
    h._gw_status = 200
    if src_proto == "gemini" and not gemini_alt:
        down.write(b"[")
        first = True
        for f in frames:
            s = f.rstrip()
            if not s:
                continue
            if not first:
                down.write(b",")
            down.write(s)
            first = False
        down.write(b"]")
    else:
        for f in frames:
            down.write(f)
    down.end()
    h._gw_bytes = down.nbytes
    record_usage(p["id"], src_proto, True, seen["usage"])


# =====================================================================
# 连通性测试
# =====================================================================

def _pick_test_model(p):
    for alias, target in _model_entries(p):
        if alias != "*":
            return target or alias
    proto = p.get("default_protocol") or (p.get("protocols") or ["openai_chat"])[0]
    return {"anthropic": "claude-3-5-haiku-latest",
            "gemini": "gemini-2.0-flash",
            "openai_responses": "gpt-4o-mini"}.get(proto, "gpt-4o-mini")


def _redact(text, p):
    s = str(text or "")
    key = p.get("api_key") or ""
    if key:
        s = s.replace(key, "••••")
    return re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-••••", s)[:600]


def test_provider(pid):
    p = get_provider(pid)
    if p.get("key_error"):
        return {"ok": False, "error": p["key_error"]}
    if not p.get("api_key"):
        return {"ok": False, "error": "还没有填 API Key"}
    proto = p.get("default_protocol") or (p.get("protocols") or ["openai_chat"])[0]
    model = _pick_test_model(p)
    t0 = time.time()

    # 先试 /models：便宜、不消耗 token；很多兼容服务也实现了
    try:
        if proto == "gemini":
            url = _join(p["base_url"], "/v1beta/models")
        elif proto == "anthropic":
            url = _join(p["base_url"], "/v1/models")
        else:
            url = _join(p["base_url"], "/models")
        conn, path = _http_conn(url, timeout=15)
        hdr = _provider_headers(p, proto)
        conn.putrequest("GET", path, skip_accept_encoding=True)
        for k, v in hdr.items():
            if k not in ("Content-Type",):
                conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        raw = _read_body(resp, 2 * MB)
        ct = resp.getheader("Content-Type") or ""
        conn.close()
        if resp.status < 400 and ("json" in ct or raw[:1] in (b"{", b"[")):
            obj = json.loads(raw or b"{}")
            n = len(obj.get("data") or obj.get("models") or [])
            return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                    "models": n, "via": "models", "model": model,
                    "message": f"连通，/models 返回 {n} 个模型"}
        if resp.status not in (404, 405, 400):
            return {"ok": False, "status": resp.status,
                    "error": _redact(_upstream_message(raw), p),
                    "latency_ms": int((time.time() - t0) * 1000)}
    except Exception:  # noqa: BLE001
        pass

    # 再用 1 token 的最小请求确认推理可用
    try:
        ir = {"system": "", "messages": [{"role": "user",
                                         "parts": [{"type": "text", "text": "ping"}]}],
              "tools": [], "tool_choice": {"mode": "auto", "name": ""},
              "max_tokens": 1, "temperature": 0, "top_p": None, "stop": [],
              "stream": False}
        payload = _json_bytes(G.build_request(proto, ir, model))
        conn, resp = _request_upstream(p, proto, model, False, [payload], len(payload), 30)
        raw = _read_body(resp, 1 * MB)
        status = resp.status
        conn.close()
        if status < 400:
            return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                    "models": 0, "via": "chat", "model": model,
                    "message": f"连通，{model} 返回 {status}"}
        return {"ok": False, "status": status, "model": model,
                "error": _redact(_upstream_message(raw), p),
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(str(e), p),
                "latency_ms": int((time.time() - t0) * 1000)}


# =====================================================================
# 管理接口数据
# =====================================================================

def admin_overview():
    providers = list_providers()
    cat = model_catalog()
    rep = usage_report(14)
    return {"providers": providers, "settings": settings_view(),
            "models": cat, "usage": rep,
            "token_set": bool(access_token()), "db": db_path()}


def admin_models():
    return model_catalog()


def admin_usage(days=14):
    return usage_report(days)
