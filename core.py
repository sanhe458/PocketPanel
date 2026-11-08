"""核心层：路径、配置、会话、任务、通用工具。

与 Go 版保持数据和文件格式完全一致，因此旧的 data/ 目录可以直接复用：
config.json 的 salt/hash 算法不变（sha256(salt + ":" + 密码)），
回收站、版本、分享、收藏的 JSON 结构不变。
"""
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import hashlib

APP_VERSION = "0.3.0"
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
SVC_DIR = os.path.join(PREFIX, "var/service")
HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")
DATA = os.path.join(HOME, "pocketpanel", "data")

DEFAULT_USER = "admin"

_lock = threading.RLock()


# ---------------- 配置 ----------------

def _config_path():
    return os.path.join(DATA, "config.json")


def load_config():
    p = _config_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not cfg.get("hash"):
        salt = secrets.token_hex(8)
        pwd = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
        cfg = {
            "port": 8888, "username": DEFAULT_USER, "salt": salt,
            "hash": hash_pwd(salt, pwd), "init_pwd": pwd, "created_at": int(time.time()),
        }
        save_config(cfg)
        print("=" * 40)
        print(" 首次启动，已生成登录账号")
        print(" 用户名:", cfg["username"])
        print(" 密  码:", pwd)
        print(" 请登录后立即修改")
        print("=" * 40)
    if not cfg.get("username"):
        cfg["username"] = DEFAULT_USER
        save_config(cfg)
    return cfg


def save_config(cfg):
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    tmp = _config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _config_path())


def hash_pwd(salt, pwd):
    return hashlib.sha256((salt + ":" + pwd).encode()).hexdigest()


CFG = {}
CFG_LOCK = threading.RLock()


def cfg_get(*keys, default=None):
    with CFG_LOCK:
        v = CFG
        for k in keys:
            if not isinstance(v, dict):
                return default
            v = v.get(k)
        return default if v is None else v


def cfg_update(**kw):
    with CFG_LOCK:
        CFG.update(kw)
        save_config(CFG)


def cfg_reload():
    global CFG
    CFG = load_config()


# ---------------- 会话 ----------------

SESSIONS = {}
SESS_LOCK = threading.Lock()


def new_session(user):
    tok = secrets.token_hex(24)
    with SESS_LOCK:
        SESSIONS[tok] = {"user": user, "exp": time.time() + 86400}
    return tok


def check_session(token):
    if not token:
        return False
    with SESS_LOCK:
        s = SESSIONS.get(token)
        if not s:
            return False
        if s["exp"] < time.time():
            SESSIONS.pop(token, None)
            return False
        return True


def drop_session(token):
    with SESS_LOCK:
        SESSIONS.pop(token, None)


def drop_all_sessions():
    with SESS_LOCK:
        SESSIONS.clear()


# ---------------- 路径沙箱 ----------------

def sandbox(p):
    """返回绝对路径；越界抛 ValueError。允许 $HOME 与 storage 挂载点。"""
    if not p:
        p = HOME
    if not os.path.isabs(p):
        p = os.path.join(HOME, p)
    p = os.path.normpath(p)
    if p == HOME:
        return p
    if p.startswith(HOME + os.sep):
        return p
    storage = os.path.join(HOME, "storage")
    if p == storage or p.startswith(storage + os.sep):
        return p
    raise ValueError("路径超出允许范围")


def valid_name(name):
    if not name or name in (".", ".."):
        raise ValueError("名称非法")
    if any(c in name for c in ("/", "\\", "\x00")):
        raise ValueError("名称不能包含斜杠")


def human_size(n):
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def dir_size(path, limit=50000):
    total = 0
    cnt = 0
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                cnt += 1
                if cnt > limit:
                    return total
    except OSError:
        pass
    return total


def unique_path(dst):
    if not os.path.exists(dst):
        return dst
    d, name = os.path.split(dst)
    base, ext = os.path.splitext(name)
    for i in range(1, 1000):
        cand = os.path.join(d, f"{base} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
    return dst


def copy_tree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def sh(cmd):
    """执行 shell 命令并返回 stdout（失败返回空串）。"""
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, timeout=20)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- 任务队列 ----------------

TASKS = {}
TASK_ORDER = []
TASK_LOCK = threading.RLock()
TASK_MAX = 50


def new_task(kind, title):
    t = {
        "id": secrets.token_hex(6), "kind": kind, "title": title,
        "cmd": "", "cwd": "", "status": "running", "output": "",
        "progress": 0, "result": None,
        "started": int(time.time()), "ended": 0, "exit": 0,
        "_cancel": False,
    }
    with TASK_LOCK:
        TASKS[t["id"]] = t
        TASK_ORDER.insert(0, t["id"])
        while len(TASK_ORDER) > TASK_MAX:
            TASKS.pop(TASK_ORDER.pop(), None)
    return t


def task_log(t, line):
    with TASK_LOCK:
        if len(t["output"]) < 200 * 1024:
            t["output"] += str(line) + "\n"


def task_progress(t, p):
    with TASK_LOCK:
        t["progress"] = max(0, min(100, int(p)))


def task_result(t, obj):
    with TASK_LOCK:
        t["result"] = obj


def task_finish(t, err=None):
    with TASK_LOCK:
        if t["status"] != "running":
            return
        t["ended"] = int(time.time())
        if err:
            t["status"] = "failed"
            t["exit"] = 1
            if len(t["output"]) < 200 * 1024:
                t["output"] += f"错误: {err}\n"
        else:
            t["status"] = "success"
            t["progress"] = 100


def task_canceled(t):
    with TASK_LOCK:
        return t["_cancel"] or t["status"] == "canceled"


def run_job(kind, title, fn):
    t = new_task(kind, title)

    def runner():
        try:
            fn(t)
            task_finish(t)
        except Exception as e:  # noqa: BLE001
            task_finish(t, e)

    threading.Thread(target=runner, daemon=True).start()
    return t


def task_view(t):
    with TASK_LOCK:
        return {k: v for k, v in t.items() if not k.startswith("_")}


def list_tasks():
    with TASK_LOCK:
        return [task_view(TASKS[i]) for i in TASK_ORDER if i in TASKS]


def run_shell_task(t, argv, cwd=None, env=None):
    """执行外部命令，把输出逐行喂给任务。返回 (code, 输出)"""
    try:
        p = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", **(env or {})},
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"无法启动 {argv[0]}: {e}")
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            task_log(t, line)
        if task_canceled(t):
            p.kill()
            raise RuntimeError("已取消")
    code = p.wait()
    return code


def run_cmd_task(t, cmd, cwd=None, timeout=600):
    """兼容旧的任务模型：单条命令交给 shell。"""
    try:
        p = subprocess.Popen(
            ["sh", "-c", cmd], cwd=cwd or HOME, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env={**os.environ, "TERM": "dumb"},
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e))
    deadline = time.time() + timeout
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            task_log(t, line)
        if task_canceled(t):
            p.kill()
            raise RuntimeError("已取消")
        if time.time() > deadline:
            p.kill()
            raise RuntimeError("执行超时")
    code = p.wait()
    with TASK_LOCK:
        t["exit"] = code
    return code


# ---------------- 文件设置 ----------------

FILE_SETTINGS = os.path.join(DATA, "file-settings.json")


def file_settings():
    s = read_json(FILE_SETTINGS, {})
    return {
        "trash_keep_days": int(s.get("trash_keep_days", 7)),
        "version_keep": int(s.get("version_keep", 10)),
    }


def save_file_settings(s):
    write_json(FILE_SETTINGS, s)
