"""任务类型注册中心（M08 8.7 的地基）。

设计目标：新增一种任务类型 = 在这里注册一份 schema + 执行器，前端表单自动出现、
调度/历史/通知全部复用，不碰调度代码也不碰前端路由。

注册项字段：
    type_id / name / icon / category      展示元数据
    fields[]                              参数 schema，见 _f() 的键说明
    supports_dry_run                      执行器支持只统计不落盘的预览
    async                                 耗时任务（前端提示用；执行本来就在线程里）
    validate(params) -> (clean, {field: err})
    run(params, ctx) -> dict              结构化结果；ctx = {timeout, task}
    human(params) -> str                  列表页动作描述，与周期描述拼接

Field 键：
    name/label/type(string|int|bool|enum|textarea|multiselect)
    default/required/min/max/max_len/options/options_from/placeholder/hint
    show_when={"field": x, "in": [...]} 或 {"not_in": [...]}   条件显示
    group                                                 表单分组（预留）

白名单原则：清理日志的所有路径都来自 CLEAN_TARGETS 的 roots 硬编码，
用户参数只能选"清哪些类、留多久"，任何参数都拼不出白名单之外的路径。
"""
import fnmatch
import json
import os
import re
import ssl
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import core
import messages

REGISTRY = {}
DATA = core.DATA
HOME = core.HOME
PREFIX = core.PREFIX

MAX_BODY = 64 * 1024
SNIPPET = 2048


class ValidationError(Exception):
    """字段级校验失败：errors 形如 {field: 消息}，可直接定位到表单控件。"""

    def __init__(self, errors, message=""):
        self.errors = errors or {}
        msg = message or "；".join(f"{v}" for v in self.errors.values()) or "参数校验失败"
        super().__init__(msg)


def _f(name, label, ftype="string", **kw):
    d = {"name": name, "label": label, "type": ftype}
    d.update(kw)
    return d


def register(spec):
    REGISTRY[spec["type_id"]] = spec


def get(type_id):
    spec = REGISTRY.get(type_id)
    if not spec:
        raise ValidationError({}, f"未知任务类型：{type_id}")
    return spec


# ================= 动态选项 =================

def _options_services():
    try:
        names = sorted(
            d for d in os.listdir(os.path.join(core.SVC_DIR))
            if os.path.isdir(os.path.join(core.SVC_DIR, d)) and not d.startswith(".")
        )
    except OSError:
        return []
    return [{"value": n, "label": n} for n in names]


def _options_sites():
    """站点名从约定日志目录的文件名前缀提取（{site}.access.log*）。"""
    sites = set()
    for root in CLEAN_SOURCES["site_logs"]["roots"]:
        if not os.path.isdir(root):
            continue
        for fn in os.listdir(root):
            m = re.match(r"^(.+?)\.(access|error)\.log", fn)
            if m:
                sites.add(m.group(1))
    return [{"value": x, "label": x} for x in sorted(sites)]


OPTION_SOURCES = {"services": _options_services, "sites": _options_sites}


def registry_view():
    """给前端的注册表：schema 齐全，动态选项就地填充。"""
    out = []
    for spec in REGISTRY.values():
        fields = []
        for f in spec["fields"]:
            f2 = dict(f)
            src = f2.pop("options_from", None)
            if src and not f2.get("options"):
                f2["options"] = OPTION_SOURCES[src]()
            if f2["type"] == "enum" and f2.get("options") and not f2.get("default"):
                f2["default"] = f2["options"][0] if isinstance(f2["options"][0], str) \
                    else f2["options"][0]["value"]
            fields.append(f2)
        out.append({
            "type_id": spec["type_id"], "name": spec["name"], "icon": spec["icon"],
            "category": spec["category"], "fields": fields,
            "supports_dry_run": spec.get("supports_dry_run", False),
            "async": spec.get("async", False),
        })
    return out


# ================= schema 级校验 =================

def _coerce(f, raw):
    """返回 (ok, value)。类型不对/越界返回 (False, 错误消息)。"""
    t = f["type"]
    name = f.get("label") or f["name"]
    if raw is None or raw == "":
        if f.get("required"):
            return False, f"{name} 不能为空"
        return True, f.get("default")
    if t == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return False, f"{name} 必须是整数"
        if f.get("min") is not None and v < f["min"]:
            return False, f"{name} 不能小于 {f['min']}"
        if f.get("max") is not None and v > f["max"]:
            return False, f"{name} 不能大于 {f['max']}"
        return True, v
    if t in ("enum",):
        opts = [o["value"] if isinstance(o, dict) else o for o in (f.get("options") or [])]
        if str(raw) not in opts:
            return False, f"{name} 必须是：{'、'.join(map(str, opts))}"
        return True, str(raw)
    if t == "bool":
        return True, bool(raw)
    if t in ("string", "textarea"):
        v = str(raw)
        if f.get("max_len") and len(v) > f["max_len"]:
            return False, f"{name} 不能超过 {f['max_len']} 个字符"
        return True, v
    if t == "multiselect":
        if not isinstance(raw, (list, tuple)):
            raw = [raw] if raw not in (None, "") else []
        allowed = [o["value"] if isinstance(o, dict) else o for o in (f.get("options") or [])]
        bad = [x for x in raw if x not in allowed]
        if bad:
            return False, f"{name} 含不支持的选项：{'、'.join(map(str, bad))}"
        if f.get("required") and not raw:
            return False, f"{name} 至少选择一项"
        return True, list(raw)
    return True, raw


def validate_params(type_id, params):
    """schema 校验 + 类型自带校验。返回 (clean, errors)。"""
    spec = get(type_id)
    params = params or {}
    errors = {}
    clean = {}
    for f in spec["fields"]:
        sw = f.get("show_when")
        if sw and not _show_when_ok(sw, params):
            # 条件不满足的字段不参与校验，值丢弃
            continue
        ok, v = _coerce(f, params.get(f["name"]))
        if not ok:
            errors[f["name"]] = v
        else:
            clean[f["name"]] = v
    try:
        cleaned, extra = spec["validate"](clean)
        clean = cleaned or clean
        errors.update(extra or {})
    except ValidationError as e:
        errors.update(e.errors)
    return clean, errors


def _show_when_ok(sw, params):
    v = params.get(sw["field"])
    if "in" in sw:
        return v in sw["in"] or (isinstance(v, list) and bool(set(v) & set(sw["in"])))
    if "not_in" in sw:
        return v not in sw["not_in"]
    return True


def execute(type_id, params, ctx):
    """统一入口：先校验再执行。结果一定是结构化 dict。"""
    spec = get(type_id)
    clean, errors = validate_params(type_id, params)
    if errors:
        raise ValidationError(errors)
    ctx = dict(ctx or {})
    ctx["timeout"] = int((ctx or {}).get("timeout") or 600)
    try:
        return spec["run"](clean, ctx)
    except ValidationError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "fail": {"code": "internal", "message": str(e)}}


def humanize(type_id, params):
    try:
        spec = get(type_id)
    except ValidationError:
        return ""
    try:
        return spec["human"](params or {})
    except Exception:  # noqa: BLE001
        return ""


# =========================================================
# 类型 1：Shell（迁移自旧模型，行为保持不变）
# =========================================================

def _shell_validate(p, errors=None):
    return p, {}


def _shell_run(p, ctx):
    timeout = int(ctx.get("timeout") or 600)
    started = time.time()
    proc = subprocess.Popen(
        ["sh", "-c", p["cmd"]], cwd=core.HOME,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "TERM": "dumb"})
    try:
        raw, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
        status = "success" if code == 0 else "failed"
        fail = None if code == 0 else {"code": "exit", "message": f"退出码 {code}"}
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        code, status, fail = -1, "timeout", {"code": "timeout", "message": f"超过 {timeout}s 已终止"}
        raw = b""
    out = (raw or b"").decode("utf-8", "replace")
    if len(out) > MAX_BODY:
        out = out[:MAX_BODY] + f"\n…（输出超过 {MAX_BODY // 1024}KB 已截断）"
    return {"ok": status == "success", "status": status, "exit": code,
            "duration_s": round(time.time() - started, 1), "stdout": out, "fail": fail}


register({
    "type_id": "shell", "name": "Shell 命令", "icon": ">_", "category": "通用",
    "supports_dry_run": False, "async": False,
    "fields": [_f("cmd", "命令", "textarea", required=True, max_len=4000,
                  placeholder="sh 语法，如 tar -czf ~/backup/$(date +%F).tgz ~/photos",
                  hint="在面板 HOME 目录下执行，超时取任务设置")],
    "validate": _shell_validate,
    "run": _shell_run,
    "human": lambda p: "执行命令 " + (p.get("cmd", "").strip().splitlines()[0][:28] if p.get("cmd") else ""),
})


# =========================================================
# 类型 2：重启服务（演示注册机制的最小类型）
# =========================================================

def _svc_validate(p, errors=None):
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,63}$", p.get("service") or ""):
        return p, {"service": "服务名不合法"}
    path = os.path.join(core.SVC_DIR, p["service"])
    if not os.path.isdir(path):
        return p, {"service": "服务不存在"}
    return p, {}


def _svc_run(p, ctx):
    started = time.time()
    try:
        out = core.sh(f"SVDIR={core.SVC_DIR} sv {p['action']} {p['service']} 2>&1")
        code = 0
    except Exception as e:  # noqa: BLE001
        out, code = str(e), 1
    return {"ok": code == 0, "exit": code,
            "duration_s": round(time.time() - started, 1),
            "stdout": out.strip(), "status": "success" if code == 0 else "failed",
            "fail": None if code == 0 else {"code": "exit", "message": out.strip()[:200]}}


_SVC_LABEL = {"restart": "重启服务", "start": "启动服务", "stop": "停止服务"}

register({
    "type_id": "service", "name": "服务控制", "icon": "⚙", "category": "运维",
    "supports_dry_run": False, "async": False,
    "fields": [
        _f("service", "服务", "select", required=True, options_from="services"),
        _f("action", "动作", "enum", default="restart",
           options=["restart", "start", "stop"]),
    ],
    "validate": _svc_validate,
    "run": _svc_run,
    "human": lambda p: _SVC_LABEL.get(p.get("action"), "服务操作") + " " + str(p.get("service") or ""),
})


# =========================================================
# 类型 3：清理日志（白名单 + 保留策略 + dry-run）
# =========================================================

CLEAN_SOURCES = {
    "panel_logs": {
        "label": "面板日志",
        "roots": [os.path.join(DATA, "logs")],
        "patterns": ["@*.s", "*.log.*", "*.log.old", "*.gz"],
        "exclude": ("current", "lock", "config", "*.json"),
    },
    "task_logs": {
        "label": "任务执行历史",
        "kind": "schedule_runs",
    },
    "site_logs": {
        "label": "站点日志（Nginx 等）",
        "roots": [os.path.join(HOME, "wwwlogs"), os.path.join(PREFIX, "var/log/nginx")],
        "patterns": ["*.log.[0-9]*", "*.log-2*", "*.gz", "*.old"],
        "exclude": ("*.log",),          # 正在写的当前日志一律不碰
    },
    "system_logs": {
        "label": "系统日志（白名单目录）",
        "roots": [os.path.join(PREFIX, "var/log")],
        "patterns": ["*.log.[0-9]*", "*.log-2*", "*.gz", "*.old", "@*.s"],
        "exclude": ("current", "lock", "*.json"),
    },
    "backups": {
        "label": "旧备份文件",
        "roots": [os.path.join(HOME, "backups"), os.path.join(DATA, "backups")],
        "patterns": ["*.tar.gz", "*.tgz", "*.zip", "*.bak", "*.sql.gz"],
        "exclude": (),
    },
    "pkg_cache": {
        "label": "pkg / pip 缓存",
        "roots": [os.path.join(PREFIX, "var/cache/apt/archives"),
                  os.path.join(HOME, ".cache", "pip")],
        "patterns": ["*"],
        "exclude": ("lock", "*.lock", "noaugidx*"),
    },
}

_CLEAN_TARGET_FIELD = _f(
    "targets", "清理对象", "multiselect", required=True,
    options=[{"value": k, "label": v["label"]} for k, v in CLEAN_SOURCES.items()],
    hint="只能清理下方白名单内定义的类别，无法指定任意路径")


def _target_files(target_id, keep_ts, keep_count, sites):
    """收集某类对象里按保留策略应当删除的文件。返回 [(path, size, mtime)]。"""
    src = CLEAN_SOURCES[target_id]
    if src.get("kind") == "schedule_runs":
        return None          # 特殊处理，见 _clean_schedule_runs
    found = []
    for root in src["roots"]:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                if any(fnmatch.fnmatch(fn, ex) for ex in src["exclude"]):
                    continue
                if not any(fnmatch.fnmatch(fn, pat) for pat in src["patterns"]):
                    continue
                if target_id == "site_logs" and sites:
                    if not any(fn.startswith(s + ".") for s in sites):
                        continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not os.path.isfile(p):
                    continue
                # 正在写入的活跃文件（1 分钟内改过）不清理
                if time.time() - st.st_mtime < 60:
                    continue
                if keep_ts is not None and st.st_mtime >= keep_ts:
                    continue
                found.append((p, st.st_size, st.st_mtime))
    if keep_ts is None and keep_count is not None:
        # 按份数：每个 root 各自保留最新 N 份
        by_root = {}
        for item in found:
            by_root.setdefault(os.path.dirname(item[0]), []).append(item)
        keep = []
        for _root, items in by_root.items():
            items.sort(key=lambda x: -x[2])
            keep.extend(items[:keep_count])
        found = keep
    return found


def _clean_schedule_runs(p, dry):
    """任务执行历史裁剪：不是文件，改 schedule_runs.json 的条目。"""
    import sched as _sched                     # 延迟导入避免循环
    with _sched._lock:
        runs = list(_sched._runs)
    cutoff = None
    if p.get("keep_policy") == "days":
        cutoff = time.time() - int(p.get("keep_days") or 7) * 86400
        stale = [r for r in runs if r.get("started", 0) < cutoff]
        keep = [r for r in runs if r.get("started", 0) >= cutoff]
    else:
        n = int(p.get("keep_count") or 5)
        stale = runs[:-n] if len(runs) > n else []
        keep = runs[len(stale):]
    size = sum(len(json.dumps(r, ensure_ascii=False)) for r in stale)
    if not dry and stale:
        with _sched._lock:
            _sched._runs = keep
            _sched._save_runs()
    return {"files": len(stale), "size": size, "note": "已从执行历史中移除" if stale else ""}


def _safe_unlink(path, roots):
    """删除前二次校验：realpath 必须落在白名单 root 内。"""
    rp = os.path.realpath(path)
    for r in roots:
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            os.remove(rp)
            return
    raise PermissionError(f"路径不在白名单内：{path}")


def _log_clean_validate(p, errors=None):
    errors = {}
    if "site_logs" in (p.get("targets") or []) and p.get("sites") is None:
        p["sites"] = []
    return p, errors


def _log_clean_run(p, ctx):
    dry = bool(p.get("dry_run", True))
    keep_policy = p.get("keep_policy") or "days"
    keep_days = int(p.get("keep_days") or 7)
    keep_count = int(p.get("keep_count") or 5)
    max_mb = int(p.get("max_mb") or 500)
    max_files = int(p.get("max_files") or 1000)
    keep_ts = time.time() - keep_days * 86400 if keep_policy == "days" else None
    keep_n = keep_count if keep_policy == "count" else None
    sites = p.get("sites") or []

    per = {}
    total = {"files": 0, "size": 0, "freed": 0}
    truncated = False

    # 先收集所有待删除项，统一按“最旧优先”排序，再施加单次上限
    plan = []
    for tid in p["targets"]:
        if tid == "task_logs":
            with_lock = _clean_schedule_runs(dict(p, dry_run=True), True)
            plan.append({"target": tid, "special": "schedule_runs",
                         "items": with_lock, "roots": []})
            continue
        items = _target_files(tid, keep_ts, keep_n, sites)
        if items is None:
            items = []
        roots = CLEAN_SOURCES[tid]["roots"]
        plan.append({"target": tid, "items": items, "roots": roots})

    budget_files = max_files if max_files > 0 else 10 ** 9
    budget_bytes = (max_mb * 1024 * 1024) if max_mb > 0 else 10 ** 12

    for entry in plan:
        tid = entry["target"]
        info = {"files": 0, "size": 0, "errors": [], "note": ""}
        if entry.get("special") == "schedule_runs":
            if not dry:
                r = _clean_schedule_runs(p, False)
                info["files"], info["size"] = r["files"], r["size"]
                info["note"] = r["note"]
            else:
                info["files"], info["size"] = entry["items"]["files"], entry["items"]["size"]
                info["note"] = "dry-run：仅统计" if info["files"] else ""
            per[tid] = info
            total["files"] += info["files"]
            total["size"] += info["size"]
            total["freed"] += info["size"] if not dry else 0
            continue

        items = sorted(entry["items"], key=lambda x: x[2])   # 最旧优先
        note = ""
        if not items:
            roots_live = [r for r in entry["roots"] if os.path.isdir(r)]
            if not roots_live:
                note = "目录不存在，跳过"
        for path, size, _mt in items:
            if total["files"] + 1 > budget_files:
                truncated = True
                note = f"达到单次文件数上限 {max_files}，剩余未处理"
                break
            if total["size"] + size > budget_bytes:
                truncated = True
                note = f"达到单次容量上限 {max_mb}MB，剩余未处理"
                break
            info["files"] += 1
            info["size"] += size
            if not dry:
                try:
                    _safe_unlink(path, entry["roots"])
                    total["freed"] += size
                except OSError as e:
                    info["errors"].append({"path": path, "reason": str(e)})
                    info["files"] -= 1
                    info["size"] -= size
        if dry and info["files"]:
            info["note"] = "dry-run：仅统计"
        per[tid] = {"files": info["files"], "size": info["size"],
                    "errors": info["errors"][:10], "note": note or info["note"]}
        total["files"] += info["files"]
        total["size"] += info["size"]
        total["freed"] += info["size"] if not dry else 0

    ok = all(not v["errors"] for v in per.values())
    return {
        "ok": ok, "dry_run": dry, "targets": per, "total": total,
        "truncated": truncated,
        "text": (f"{'预览' if dry else '已清理'} {total['files']} 个文件 / "
                 f"{total['size'] / 1048576:.2f} MB" + ("（达到单次上限，已截断）" if truncated else "")),
    }


register({
    "type_id": "log_clean", "name": "清理日志", "icon": "🧹", "category": "运维",
    "supports_dry_run": True, "async": True,
    "fields": [
        _CLEAN_TARGET_FIELD,
        _f("keep_policy", "保留策略", "enum", default="days",
           options=[{"value": "days", "label": "按天数"}, {"value": "count", "label": "按份数"}]),
        _f("keep_days", "保留天数", "int", default=7, min=1, max=365, hint="mtime 早于该天数的文件会被清理",
           show_when={"field": "keep_policy", "in": ["days"]}),
        _f("keep_count", "保留份数", "int", default=5, min=1, max=999, hint="每个目录各自保留最新 N 份",
           show_when={"field": "keep_policy", "in": ["count"]}),
        _f("sites", "站点筛选", "multiselect", options_from="sites",
           hint="留空表示全部站点",
           show_when={"field": "targets", "in": ["site_logs"]}),
        _f("max_mb", "单次容量上限 (MB)", "int", default=500, min=0, max=102400,
           hint="0 = 不限制；超出后本次截断，下次继续"),
        _f("max_files", "单次文件数上限", "int", default=1000, min=0, max=1000000,
           hint="0 = 不限制"),
        _f("dry_run", "只预览不删除", "bool", default=True,
           hint="建议首次开启，确认统计无误后再关闭"),
    ],
    "validate": _log_clean_validate,
    "run": _log_clean_run,
    "human": lambda p: _log_clean_human(p),
})


def _log_clean_human(p):
    names = {v["label"]: k for k, v in CLEAN_SOURCES.items()}
    chosen = []
    for label, tid in names.items():
        if tid in (p.get("targets") or []):
            chosen.append(label.replace("（Nginx 等）", "").replace("（白名单目录）", ""))
    if "task_logs" in (p.get("targets") or []):
        chosen.append("执行历史")
    if not chosen:
        return "清理日志"
    if p.get("keep_policy") == "count":
        policy = f"保留 {p.get('keep_count', 5)} 份"
    else:
        policy = f"保留 {p.get('keep_days', 7)} 天"
    dry = "（预览）" if p.get("dry_run") else ""
    return f"清理{'、'.join(chosen)}，{policy}{dry}"


# =========================================================
# 类型 4：访问 URL（单次请求 + 基础校验）
# =========================================================

SENSITIVE_KEYS = ("authorization", "cookie", "set-cookie", "token", "secret",
                  "password", "api-key", "apikey", "proxy-authorization")
_URL_RE = re.compile(r"^https?://[^\s]+$")
_METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"]


class _RedirectCounter(urllib.request.HTTPRedirectHandler):
    count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _mask_key(k):
    lk = k.lower()
    return any(s in lk for s in SENSITIVE_KEYS)


def _mask_headers(hd):
    return {k: ("***" if _mask_key(k) else v) for k, v in hd.items()}


def _mask_body(body, ctype):
    if not body:
        return ""
    if ctype == "application/x-www-form-urlencoded":
        pairs = []
        for kv in body.split("&"):
            k, _, v = kv.partition("=")
            k = urllib.parse.unquote_plus(k)
            pairs.append(f"{k}={'***' if _mask_key(k) else urllib.parse.unquote_plus(v)}")
        return "&".join(pairs)
    if ctype == "application/json":
        try:
            data = json.loads(body)

            def walk(o):
                if isinstance(o, dict):
                    return {k: ("***" if _mask_key(k) else walk(v)) for k, v in o.items()}
                if isinstance(o, list):
                    return [walk(x) for x in o]
                return o
            return json.dumps(walk(data), ensure_ascii=False)[:SNIPPET]
        except ValueError:
            pass
    return body[:SNIPPET]


def _parse_expect(s):
    s = (s or "").strip()
    if not s:
        return lambda c: 200 <= c < 400
    okset = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            okset.update(range(int(a), int(b) + 1))
        else:
            okset.add(int(part))
    return lambda c: c in okset


def _url_validate(p, errors=None):
    errors = {}
    url = (p.get("url") or "").strip()
    if url:
        if not _URL_RE.match(url):
            errors["url"] = "仅支持 http/https 的完整 URL"
        else:
            scheme = urllib.parse.urlparse(url).scheme.lower()
            if scheme not in ("http", "https"):
                errors["url"] = f"不允许 {scheme or '空'} 协议"
    hs = p.get("headers") or ""
    for i, line in enumerate(hs.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            errors.setdefault("headers", f"第 {i} 行缺少冒号，应为“Key: Value”")
    method = p.get("method") or "GET"
    if method not in ("GET", "HEAD") and p.get("body"):
        ct = p.get("body_type") or "json"
        if ct == "json":
            try:
                json.loads(p["body"])
            except ValueError:
                errors["body"] = "请求体不是合法 JSON（或把类型改为表单/纯文本）"
    return p, errors


def _build_request(p):
    headers = {}
    for line in (p.get("headers") or "").splitlines():
        line = line.strip()
        if line and ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
    data = None
    ctype = None
    if (p.get("method") or "GET") not in ("GET", "HEAD") and p.get("body"):
        bt = p.get("body_type") or "json"
        if bt == "json":
            data = p["body"].encode("utf-8")
            ctype = "application/json"
        elif bt == "form":
            pairs = []
            for line in p["body"].splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    pairs.append(f"{urllib.parse.quote_plus(k.strip())}={urllib.parse.quote_plus(v)}")
            data = "&".join(pairs).encode("utf-8")
            ctype = "application/x-www-form-urlencoded"
        else:
            data = p["body"].encode("utf-8")
            ctype = "text/plain"
    return headers, data, ctype


def _single_request(p):
    url = p["url"]
    headers, data, ctype = _build_request(p)
    if data is not None and ctype:
        headers.setdefault("Content-Type", ctype)
    verify = bool(p.get("verify_tls", True))
    timeout = int(p.get("timeout") or 30)
    expect = _parse_expect(p.get("expect_status"))
    keyword = (p.get("expect_keyword") or "").strip()
    started = time.time()

    ssl_ctx = None
    if not verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    redir = _RedirectCounter()
    handlers = [redir]
    if ssl_ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=data, headers=headers, method=p["method"])
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)
            code = int(resp.code)
            final_url = resp.geturl()
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也算“拿到了响应”，交给状态码判定，而不是网络错误
        code = int(e.code)
        try:
            raw = e.read(MAX_BODY + 1)
        except Exception:  # noqa: BLE001
            raw = b""
        final_url = url
        resp_headers = dict(e.headers.items()) if e.headers else {}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            fail = {"code": "timeout", "message": f"连接超时（>{timeout}s）"}
        else:
            fail = {"code": "network", "message": f"连接失败：{reason}"}
        return {"ok": False, "fail": fail, "status": None,
                "duration_ms": round((time.time() - started) * 1000),
                "text": fail["message"]}
    except socket.timeout:
        return {"ok": False, "fail": {"code": "timeout", "message": f"连接超时（>{timeout}s）"},
                "status": None, "duration_ms": round((time.time() - started) * 1000),
                "text": "超时"}
    duration = round((time.time() - started) * 1000)
    body = raw[:MAX_BODY]
    truncated_resp = len(raw) > MAX_BODY
    try:
        text = body.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        text = ""

    if not expect(code):
        return {"ok": False, "status": code, "duration_ms": duration,
                "fail": {"code": "status_mismatch",
                         "message": f"状态码 {code} 不符合期望（{p.get('expect_status') or '2xx-3xx'}）"},
                "text": text}
    if keyword and keyword not in text:
        where = "" if not truncated_resp else "（仅在前 64KB 响应中检测）"
        return {"ok": False, "status": code, "duration_ms": duration,
                "fail": {"code": "keyword_miss",
                         "message": f"响应体未包含关键字“{keyword}”{where}"},
                "text": text}

    size = len(raw)
    declared = resp_headers.get("Content-Length")
    try:
        if declared and int(declared) > size:
            size = int(declared)
    except ValueError:
        pass
    return {
        "ok": True, "status": code, "reason": resp_headers.get("Reason") or "",
        "duration_ms": duration, "size": size, "redirects": redir.count,
        "final_url": final_url, "truncated": truncated_resp,
        "body_snippet": text[:SNIPPET] if p.get("save_body", True) else "",
        "text": f"HTTP {code} · {duration}ms · {size}B · {redir.count} 次重定向",
    }


def _url_run(p, ctx):
    attempts = int(p.get("retries") or 0) + 1
    interval = int(p.get("retry_interval") or 2)
    last = None
    used = 0
    for attempt in range(1, attempts + 1):
        last = _single_request(p)
        used = attempt - 1                 # 第几次尝试前用掉的重试数
        if last.get("ok"):
            break
        # 只有网络层失败才值得重试；状态码/关键字重试也不会变
        if last["fail"]["code"] not in ("network", "timeout"):
            break
        if attempt < attempts:
            time.sleep(interval)
    if not last.get("ok"):
        url_masked = re.sub(r"(//[^/]+:)[^@]+(@)", r"\1***\2", p["url"])
        messages.add("err", f"URL 访问失败：{url_masked}",
                     f"{p['method']} · {last['fail']['message']} · 尝试 {attempts} 次")
    return dict(last, retries_used=used)


register({
    "type_id": "url_visit", "name": "访问 URL", "icon": "↗", "category": "运维",
    "supports_dry_run": False, "async": False,
    "fields": [
        _f("url", "URL", "string", required=True, max_len=2000,
           placeholder="https://example.com/health", hint="仅允许 http/https，其他协议直接拒绝"),
        _f("method", "方法", "enum", default="GET", options=_METHODS),
        _f("headers", "请求头", "textarea", max_len=2000,
           placeholder="Authorization: Bearer xxx\nX-Trace: demo",
           hint="每行一条 Key: Value；日志中敏感键会被脱敏"),
        _f("body_type", "请求体类型", "enum", default="json",
           options=[{"value": "json", "label": "JSON"}, {"value": "form", "label": "表单"},
                    {"value": "text", "label": "纯文本"}],
           show_when={"field": "method", "not_in": ["GET", "HEAD"]}),
        _f("body", "请求体", "textarea", max_len=8000,
           placeholder='{"ok": true}',
           show_when={"field": "method", "not_in": ["GET", "HEAD"]}),
        _f("timeout", "超时（秒）", "int", default=30, min=1, max=300),
        _f("retries", "重试次数", "int", default=0, min=0, max=3,
           hint="仅对连接失败/超时重试"),
        _f("retry_interval", "重试间隔（秒）", "int", default=2, min=1, max=60,
           show_when={"field": "retries", "not_in": [0]}),
        _f("verify_tls", "校验 TLS 证书", "bool", default=True,
           hint="自签证书可关闭，但仅建议对内网地址关闭"),
        _f("expect_status", "期望状态码", "string", max_len=40,
           placeholder="留空 = 2xx/3xx；如 200、200-299、200,204"),
        _f("expect_keyword", "响应包含关键字", "string", max_len=200,
           hint="在响应前 64KB 内检测"),
        _f("save_body", "记录响应摘要", "bool", default=True,
           hint="记录前 2KB，敏感头自动脱敏"),
    ],
    "validate": _url_validate,
    "run": _url_run,
    "human": lambda p: f"访问 {p.get('url') or ''}（{p.get('method') or 'GET'}）",
})


# ================= 对外 =================

def dry_run(type_id, params):
    """不落历史、不建任务的即时预览。只对 supports_dry_run 的类型有意义，
    其他类型等价于“跑一次但结果不进历史”，因此这里只放行 log_clean。"""
    spec = get(type_id)
    if not spec.get("supports_dry_run"):
        raise ValidationError({}, "该类型不支持预览")
    clean, errors = validate_params(type_id, params)
    if errors:
        raise ValidationError(errors)
    return spec["run"](clean, {"timeout": 120, "dry": True})
