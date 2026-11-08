"""计划任务（M08）：cron 解析、内置调度器、执行历史、失败通知。

为什么不用系统 crontab：设备上没有 crond，只能在本进程内做调度器。
-runit 保活进程，Termux 拿着唤醒锁时不会被冻结；息屏导致的长时间停顿
用「错过的分钟」检测兜住：恢复后扫描 (last_tick, now] 里命中的分钟，
最多补跑最近一次（miss 策略可选 run/skip），避免醒来后连环轰炸。

`*/N` 的语义提醒：cron 的匹配锚点是**系统分钟刻度**，不是任务创建时刻。
「每 5 分钟」在 14:07 创建，首次触发是 14:10。这个坑写进了前端表单的
提示文案和列表页顶部的说明条。
"""
import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta

import core
import messages
import tasktypes

TASKS_FILE = os.path.join(core.DATA, "schedules.json")
RUNS_FILE = os.path.join(core.DATA, "schedule_runs.json")
MAX_RUNS = 300
MAX_OUTPUT = 64 * 1024
ALIASES = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
}

_lock = threading.RLock()
_tasks = {}
_runs = []
_running = set()          # 正在执行的任务 id，防止上一次没跑完又触发
_started = False


# ================= cron 引擎 =================

FIELD_RE = re.compile(r"^(\*|\d+)(?:-(\d+))?(?:/(\d+))?$")
FIELDS = [("minute", "分钟", 0, 59), ("hour", "小时", 0, 23),
          ("dom", "日", 1, 31), ("month", "月", 1, 12), ("dow", "星期", 0, 7)]
DOW_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def _parse_field(raw, lo, hi):
    s = str(raw or "").strip()
    if s == "":
        return None, "不能为空"
    if s == "*":
        return None, None            # None = 任意值
    vals = set()
    for seg in s.split(","):
        if seg == "":
            return None, "存在空的取值段"
        m = FIELD_RE.match(seg)
        if not m:
            return None, f"无法识别 {seg}"
        step = int(m.group(3)) if m.group(3) else 1
        if step < 1:
            return None, "步长必须大于 0"
        if m.group(1) == "*":
            start, end = lo, hi
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) is not None else (
                hi if m.group(3) else start)
        if start < lo or start > hi or end < lo or end > hi:
            return None, f"取值应在 {lo}-{hi}"
        if start > end:
            return None, "起始值大于结束值"
        vals.update(range(start, end + 1, step))
    if not vals:
        return None, "没有有效取值"
    return vals, None


def normalize(expr):
    """别名展开 + 空白归一。"""
    e = str(expr or "").strip().lower()
    return ALIASES.get(e, e)


def parse(expr):
    """成功返回字段字典（None 表示 *），失败返回 (None, 错误说明)。"""
    parts = normalize(expr).split()
    if len(parts) != 5:
        return None, f"需要 5 个字段：分 时 日 月 周（当前 {len(parts)} 个）"
    f = {}
    for (key, label, lo, hi), raw in zip(FIELDS, parts):
        vals, err = _parse_field(raw, lo, hi)
        if err:
            return None, f"{label}：{err}"
        f[key] = vals
    if f["dow"] and 7 in f["dow"]:       # Vixie cron：7 也是周日
        f["dow"].discard(7)
        f["dow"].add(0)
    return f, None


def _day_ok(f, tm):
    dom_any = f["dom"] is None
    dow_any = f["dow"] is None
    dom_ok = dom_any or tm.tm_mday in f["dom"]
    cron_dow = (tm.tm_wday + 1) % 7     # Python 周一=0，cron 周日=0
    dow_ok = dow_any or cron_dow in f["dow"]
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow_ok
    if dow_any:
        return dom_ok
    return dom_ok or dow_ok             # 同时受限取 OR（Vixie cron）


def matches(expr, ts=None):
    """某个分钟刻度是否命中表达式。"""
    f, err = parse(expr)
    if err:
        return False
    tm = time.localtime(ts if ts is not None else time.time())
    if f["minute"] and tm.tm_min not in f["minute"]:
        return False
    if f["hour"] and tm.tm_hour not in f["hour"]:
        return False
    if f["month"] and tm.tm_mon not in f["month"]:
        return False
    return _day_ok(f, tm)


def next_run(expr, from_ts=None):
    """下一次执行的时间戳（本地时区），最多向后找两年。"""
    f, err = parse(expr)
    if err:
        return None
    base = datetime.fromtimestamp(from_ts if from_ts is not None else time.time())
    base = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    start_min = base.hour * 60 + base.minute
    day = base.replace(hour=0, minute=0)
    for _ in range(730):
        tm = day.timetuple()
        if f["month"] and tm.tm_mon not in f["month"]:
            day = (day + timedelta(days=32)).replace(day=1)
            continue
        if _day_ok(f, tm):
            for h in range(24):
                if f["hour"] and h not in f["hour"]:
                    continue
                for mi in range(60):
                    if f["minute"] and mi not in f["minute"]:
                        continue
                    if h * 60 + mi >= start_min:
                        return time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday,
                                            h, mi, 0, 0, 0, -1))
        day = day + timedelta(days=1)
        start_min = 0
    return None


def _join(nums):
    return "、".join(str(x) for x in sorted(nums))


def describe(expr):
    """表达式的人话描述，覆盖简易模式能生成的全部形态。"""
    e = normalize(expr)
    if e == "@reboot":
        return "面板启动时"
    if e in ALIASES.values():
        names = {"0 * * * *": "每小时整点", "0 0 * * *": "每天 00:00",
                 "0 0 * * 0": "每周日 00:00", "0 0 1 * *": "每月 1 日 00:00",
                 "0 0 1 1 *": "每年 1 月 1 日 00:00"}
        return names.get(e, e)
    f, err = parse(e)
    if err:
        return "表达式无效"
    mi, ho, dom, mo, dow = e.split()
    is_num = lambda v: re.match(r"^\d+$", v) is not None  # noqa: E731
    time_text = f"{int(ho):02d}:{int(mi):02d}" if is_num(mi) and is_num(ho) else None
    others_all = dom == "*" and mo == "*" and dow == "*"

    if re.match(r"^\*/\d+$", mi) and ho == "*" and dom == "*" and mo == "*" and dow == "*":
        return f"每 {mi[2:]} 分钟"
    if re.match(r"^\*/\d+$", ho) and dom == "*" and mo == "*" and dow == "*":
        tail = f"第 {int(mi)} 分" if is_num(mi) and int(mi) else "整点"
        return f"每 {ho[2:]} 小时（{tail}）"
    if time_text and others_all:
        return f"每天 {time_text}"
    if time_text and dom == "*" and mo == "*" and dow != "*":
        days = "、".join(DOW_NAMES[int(x)] if 0 <= int(x) <= 6 else x
                         for x in sorted(f["dow"]))
        return f"每{days} {time_text}"
    if time_text and dom != "*" and mo == "*" and dow == "*":
        return f"每月 {_join(f['dom'])} 日 {time_text}"
    if time_text and mo != "*":
        return f"每年 {_join(f['month'])} 月 {_join(f['dom']) if f['dom'] else '每'} 日 {time_text}"
    if others_all and mi != "*" and ho != "*":
        # 复杂的小时/分钟集合：把取值点摊开说
        h_desc = "每小时" if f["hour"] is None else f"{_join(f['hour'])} 点"
        if f["minute"] is None:
            m_desc = "整点"
        else:
            m_desc = f"第 {_join(f['minute'])} 分"
        return f"每天 {h_desc}{m_desc}"
    return f"自定义（{e}）"


def preview(expr, count=3):
    """给表单预览用：下次 N 次 + 描述。"""
    f, err = parse(expr)
    if err:
        return {"valid": False, "err": err, "human": "表达式无效", "next": [], "no_hit": False}
    ts = time.time()
    out = []
    for _ in range(count):
        nxt = next_run(expr, ts)
        if nxt is None:
            break
        out.append(int(nxt))
        ts = nxt + 60
    human = describe(expr)
    if not out and expr.strip().lower() != "@reboot":
        human += "（未来两年内没有可触发的日期，请检查日/月组合）"
    return {"valid": True, "err": "", "human": human, "next": out}


# ================= 存储 =================

def _load():
    global _tasks, _runs
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        tasks = {}
        for t in data:
            if not (isinstance(t, dict) and t.get("id")):
                continue
            # 旧模型迁移：{cmd} → type=shell + params。旧数据无需手动升级。
            if "type" not in t:
                t["type"] = "shell"
                t["params"] = {"cmd": t.pop("cmd", "")} if t.get("cmd") else {}
            t.setdefault("params", {})
            t.setdefault("tz", "system")     # 方案 8.11：显式 TZ 字段，本期不做漂移处理
            tasks[t["id"]] = t
        _tasks = tasks
    except Exception:  # noqa: BLE001
        _tasks = {}
    try:
        with open(RUNS_FILE, "r", encoding="utf-8") as fh:
            _runs = json.load(fh)
    except Exception:  # noqa: BLE001
        _runs = []


def _save_tasks():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(list(_tasks.values()), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, TASKS_FILE)


def _save_runs():
    tmp = RUNS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_runs[-MAX_RUNS:], fh, ensure_ascii=False)
    os.replace(tmp, RUNS_FILE)


def _task_view(t):
    ttype = t.get("type", "shell")
    params = t.get("params") or {}
    cron_desc = "面板启动时" if t["expr"].strip().lower() == "@reboot" else describe(t["expr"])
    action = tasktypes.humanize(ttype, params)
    return {
        "id": t["id"], "name": t["name"], "expr": t["expr"],
        "human": (cron_desc + (" " + action if action else "")).strip(),
        "type": ttype, "params": params, "tz": t.get("tz", "system"),
        "cmd": (params or {}).get("cmd", ""),
        "enabled": bool(t.get("enabled", True)),
        "timeout": t.get("timeout", 600),
        "miss": t.get("miss", "run"),
        "created": t.get("created", 0),
        "last_run": t.get("last_run"),
        "last_status": t.get("last_status"),
        "last_exit": t.get("last_exit"),
        "next_run": int(next_run(t["expr"])) if t.get("enabled", True) and next_run(t["expr"]) else None,
        "running": t["id"] in _running,
    }


# ================= 执行 =================

def _record(task_id, task_name, trigger, started, ended, status, exit_code, output,
            missed=1, ttype="shell", result=None):
    with _lock:
        _runs.append({
            "id": secrets.token_hex(5), "task_id": task_id, "task_name": task_name,
            "trigger": trigger, "started": int(started), "ended": int(ended),
            "duration": round(ended - started, 1), "status": status,
            "exit": exit_code, "output": output, "missed": missed,
            "type": ttype, "result": result,
        })
        _save_runs()
        t = _tasks.get(task_id)
        if t:
            t["last_run"] = int(started)
            t["last_status"] = status
            t["last_exit"] = exit_code
            _save_tasks()
    ok = status == "success"
    label = {"failed": "失败", "timeout": "超时", "skipped": "跳过"}.get(status, status)
    messages.add("ok" if ok else "err",
                 f"计划任务{'完成' if ok else label}",
                 f"{task_name} · {trigger} · 耗时 {round(ended - started, 1)}s"
                 + (f" · 退出码 {exit_code}" if exit_code not in (None, 0) else ""))


def _execute(task_id, trigger, missed=1):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        if task_id in _running:
            # 并发保护：上一次还没跑完，这次记一条 skipped 就走
            _record(task_id, t["name"], trigger, time.time(), time.time(),
                    "skipped", None, "", missed)
            return
        _running.add(task_id)
    name = t["name"]
    ttype = t.get("type", "shell")

    def work():
        started = time.time()
        result, status, code, text = None, "failed", None, ""
        try:
            # 统一分发到注册中心；shell 类型在这里面跑 sh -c，行为与旧版一致
            result = tasktypes.execute(
                ttype, t.get("params") or {},
                {"timeout": int(t.get("timeout") or 600),
                 "task": {"id": task_id, "name": name}})
            status = "success" if result.get("ok") else "failed"
            code = result.get("exit")
            text = result.get("text") or ""
            if not text:
                text = json.dumps(result, ensure_ascii=False)[:2000]
        except tasktypes.ValidationError as e:
            text = str(e)
            result = {"ok": False, "fail": {"code": "invalid_params", "message": str(e)},
                      "field_errors": e.errors}
        except Exception as e:  # noqa: BLE001
            text = str(e)
            result = {"ok": False, "fail": {"code": "internal", "message": str(e)}}
        ended = time.time()
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…（输出超过 {MAX_OUTPUT // 1024}KB，已截断）"
        _record(task_id, name, trigger, started, ended, status, code, text, missed,
                ttype=ttype, result=result)
        with _lock:
            _running.discard(task_id)

    threading.Thread(target=work, daemon=True, name=f"sched-{task_id}").start()


def run_now(task_id, trigger="manual"):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        if task_id in _running:
            raise RuntimeError("上一次执行还未结束")
    # 不在这里预占 _running：线程启动有一拍延迟，预占会让 _execute
    # 误判"上一次未结束"而记 skipped，且再也没人释放（实测踩过）。
    threading.Thread(target=_execute, args=(task_id, trigger), daemon=True).start()


# ================= 调度循环 =================

def _fire(task, minute_ts, missed):
    if task["expr"].strip().lower() == "@reboot":
        _execute(task["id"], "reboot", missed)
        return
    if not matches(task["expr"], minute_ts):
        return
    _execute(task["id"], "catchup" if missed > 1 else "cron", missed)


def _loop():
    last_min = int(time.time() // 60)
    while True:
        time.sleep(15)
        try:
            now_min = int(time.time() // 60)
            if now_min <= last_min:
                continue
            with _lock:
                enabled = [t for t in _tasks.values() if t.get("enabled", True)]
            hit = []
            for m in range(last_min + 1, now_min + 1):
                mt = m * 60
                for t in enabled:
                    if t["expr"].strip().lower() == "@reboot":
                        continue
                    if matches(t["expr"], mt):
                        hit.append((t, mt))
            last_min = now_min
            if hit:
                # 同一任务在同一批里只触发一次，missed 记录跳过了几轮
                per_task = {}
                for t, mt in hit:
                    per_task.setdefault(t["id"], (t, mt, 0))
                    per_task[t["id"]] = (t, mt, per_task[t["id"]][2] + 1)
                for t, mt, n in per_task.values():
                    _fire(t, mt, n)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def ensure_scheduler():
    global _started
    if _started:
        return
    _started = True
    _load()
    messages.init()
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
    # @reboot 任务：面板每次启动执行一次
    for t in list(_tasks.values()):
        if t.get("enabled", True) and t["expr"].strip().lower() == "@reboot":
            _execute(t["id"], "reboot")


# ================= 对外接口 =================

def listing():
    with _lock:
        tasks = [_task_view(t) for t in _tasks.values()]
    tasks.sort(key=lambda x: x["created"])
    with _lock:
        runs = list(reversed(_runs[-60:]))
    return {"tasks": tasks, "runs": runs, "now": int(time.time()),
            "miss_semantics": "cron 的 */N 以系统分钟刻度为锚点，不是任务创建时刻"}


def task_runs(task_id, limit=30):
    with _lock:
        runs = [r for r in reversed(_runs) if r["task_id"] == task_id]
    return runs[:limit]


def validate(body):
    name = (body.get("name") or "").strip()
    expr = (body.get("expr") or "").strip()
    if not name:
        raise ValueError("任务名称不能为空")
    if len(name) > 60:
        raise ValueError("名称太长（≤60 字符）")
    if expr.strip().lower() != "@reboot":
        f, err = parse(expr)
        if err:
            raise ValueError(f"表达式无效：{err}")
    timeout = int(body.get("timeout") or 600)
    if not (5 <= timeout <= 86400):
        raise ValueError("超时应在 5–86400 秒")
    miss = body.get("miss") if body.get("miss") in ("run", "skip") else "run"

    # 类型与参数：统一走注册中心校验。旧客户端只传 {cmd} 时自动映射为 shell。
    ttype = (body.get("type") or "shell").strip().lower()
    params = body.get("params")
    if not isinstance(params, dict):
        params = {"cmd": body.get("cmd") or ""} if body.get("cmd") else {}
    clean, errors = tasktypes.validate_params(ttype, params)
    if errors:
        raise tasktypes.ValidationError(errors)
    if ttype == "shell" and not (clean.get("cmd") or "").strip():
        raise ValueError("执行命令不能为空")
    return name, normalize(expr), ttype, clean, timeout, miss


def create(body):
    name, expr, ttype, params, timeout, miss = validate(body)
    with _lock:
        task = {
            "id": secrets.token_hex(5), "name": name, "expr": expr,
            "type": ttype, "params": params,
            "enabled": body.get("enabled", True) is not False,
            "timeout": timeout, "miss": miss, "created": int(time.time()),
        }
        _tasks[task["id"]] = task
        _save_tasks()
        return _task_view(task)


def update(task_id, body):
    name, expr, ttype, params, timeout, miss = validate(body)
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        t.update({"name": name, "expr": expr, "type": ttype, "params": params,
                  "timeout": timeout, "miss": miss,
                  "enabled": body.get("enabled", t.get("enabled", True)) is not False})
        _save_tasks()
        return _task_view(t)


def delete(task_id):
    with _lock:
        if _tasks.pop(task_id, None) is None:
            raise KeyError("任务不存在")
        _save_tasks()
    return True


def toggle(task_id):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        t["enabled"] = not t.get("enabled", True)
        _save_tasks()
        return _task_view(t)
