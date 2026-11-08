"""文件域：浏览、读写、回收站、历史版本、收藏、搜索、压缩、上传。"""
import hashlib
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request

import core
from core import DATA, HOME, sandbox, valid_name, unique_path, copy_tree, read_json, write_json

TRASH = os.path.join(DATA, "trash")
TRASH_ITEMS = os.path.join(TRASH, "items")
TRASH_INDEX = os.path.join(TRASH, "index.json")
VERSIONS = os.path.join(DATA, "versions")
VERSION_INDEX = os.path.join(VERSIONS, "index.json")
FAVS = os.path.join(DATA, "favorites.json")
UPLOADS = os.path.join(DATA, "uploads")

T_LOCK = threading.RLock()
V_LOCK = threading.RLock()
F_LOCK = threading.RLock()

VERSION_MAX = 8 << 20


# ---------------- 浏览 ----------------

def list_dir(path):
    abs_path = sandbox(path)
    out = []
    with os.scandir(abs_path) as it:
        for e in it:
            try:
                st = e.stat()
            except OSError:
                continue
            out.append({
                "name": e.name, "is_dir": e.is_dir(), "size": st.st_size,
                "mode": _mode_str(st.st_mode), "mtime": int(st.st_mtime),
            })
    out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": abs_path, "entries": out}


def _mode_str(m):
    import stat
    return stat.filemode(m)


def read_text(path):
    abs_path = sandbox(path)
    st = os.stat(abs_path)
    if os.path.isdir(abs_path):
        raise ValueError("不是文件")
    if st.st_size > 512 * 1024:
        raise ValueError("文件超过 512KB，请用其他方式编辑")
    with open(abs_path, "rb") as f:
        return {"path": abs_path, "content": f.read().decode("utf-8", "replace")}


def open_file(path):
    """返回 (绝对路径, 文件名, 大小, mtime)，供 app 层流式输出"""
    abs_path = sandbox(path)
    if not os.path.isfile(abs_path):
        raise ValueError("不是文件")
    st = os.stat(abs_path)
    return abs_path, os.path.basename(abs_path), st.st_size, st.st_mtime


# ---------------- 历史版本 ----------------

def _path_key(p):
    return hashlib.sha1(p.encode()).hexdigest()[:16]


def _version_file(vid):
    parts = vid.split("/")
    if len(parts) != 2 or not parts[1].isdigit():
        raise ValueError("版本 ID 非法")
    return os.path.join(VERSIONS, parts[0], parts[1] + ".bak")


def _load_versions():
    return read_json(VERSION_INDEX, [])


def _save_versions(items):
    write_json(VERSION_INDEX, items)


def snapshot(abs_path):
    """写入前保存旧内容，返回版本 ID（空表示未快照）"""
    try:
        st = os.stat(abs_path)
    except OSError:
        return ""
    if st.st_size == 0 or st.st_size > VERSION_MAX:
        return ""
    with V_LOCK:
        key = _path_key(abs_path)
        d = os.path.join(VERSIONS, key)
        os.makedirs(d, mode=0o700, exist_ok=True)
        nano = time.time_ns()
        dst = os.path.join(d, f"{nano}.bak")
        shutil.copy2(abs_path, dst)
        items = _load_versions()
        items.append({"id": f"{key}/{nano}", "path": abs_path,
                      "ts": int(nano // 1_000_000_000), "nano": nano, "size": st.st_size})
        keep = core.file_settings()["version_keep"]
        # 同一路径只留最近 N 份
        same = [i for i in items if i["path"] == abs_path]
        same.sort(key=lambda i: i.get("nano", i["ts"] * 10**9), reverse=True)
        drop = {i["id"] for i in same[keep:]}
        for vid in drop:
            try:
                os.remove(_version_file(vid))
            except OSError:
                pass
        items = [i for i in items if i["id"] not in drop]
        _save_versions(items)
        return f"{key}/{nano}"


def versions_of(path):
    abs_path = sandbox(path)
    rows = [v for v in _load_versions() if v["path"] == abs_path]
    rows.sort(key=lambda v: v.get("nano", v["ts"] * 10**9), reverse=True)
    return {"path": abs_path, "versions": rows,
            "keep": core.file_settings()["version_keep"]}


def write_text(path, content):
    abs_path = sandbox(path)
    if len(content.encode()) > VERSION_MAX:
        raise ValueError("内容超过 8MB，请用其他方式上传")
    vid = snapshot(abs_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": "1", "version": vid}


def version_read(vid):
    p = _version_file(vid)
    if not os.path.isfile(p):
        raise FileNotFoundError("版本不存在")
    with open(p, "rb") as f:
        data = f.read()
    if len(data) > 512 * 1024:
        raise ValueError("版本超过 512KB，请直接恢复后用编辑器查看")
    return {"content": data.decode("utf-8", "replace")}


def version_restore(vid):
    p = _version_file(vid)
    if not os.path.isfile(p):
        raise FileNotFoundError("版本不存在")
    target = ""
    for v in _load_versions():
        if v["id"] == vid:
            target = v["path"]
            break
    if not target:
        raise FileNotFoundError("版本记录不存在")
    with open(p, "rb") as f:
        data = f.read()
    snapshot(target)  # 恢复前也留一份，避免不可逆
    with open(target, "wb") as f:
        f.write(data)
    return {"ok": "1", "path": target}


def version_delete(vid):
    p = _version_file(vid)
    try:
        os.remove(p)
    except OSError:
        pass
    with V_LOCK:
        _save_versions([v for v in _load_versions() if v["id"] != vid])
    return {"ok": "1"}


def gc_orphan_versions():
    """原文件已消失且超过 7 天的快照，回收掉"""
    now = int(time.time())
    keep = []
    removed = 0
    for v in _load_versions():
        if not os.path.exists(v["path"]) and now - v["ts"] > 7 * 86400:
            try:
                os.remove(_version_file(v["id"]))
            except OSError:
                pass
            removed += 1
            continue
        keep.append(v)
    if removed:
        with V_LOCK:
            _save_versions(keep)


# ---------------- 回收站 ----------------

def _load_trash():
    return read_json(TRASH_INDEX, [])


def _save_trash(items):
    write_json(TRASH_INDEX, items)


def to_trash(abs_path):
    """移入回收站：同分区 rename，跨分区回退复制"""
    st = os.stat(abs_path)
    with T_LOCK:
        os.makedirs(TRASH_ITEMS, mode=0o700, exist_ok=True)
        tid = f"{time.time_ns()}{os.urandom(2).hex()}"
        dst = os.path.join(TRASH_ITEMS, tid)
        try:
            os.rename(abs_path, dst)
        except OSError:
            copy_tree(abs_path, dst)
            if st.st_mode & 0o40000:
                shutil.rmtree(abs_path, ignore_errors=True)
            else:
                os.remove(abs_path)
        items = _load_trash()
        items.append({
            "id": tid, "name": os.path.basename(abs_path), "orig_path": abs_path,
            "size": core.dir_size(dst), "is_dir": st.st_mode & 0o40000 != 0,
            "deleted_at": int(time.time()),
        })
        _save_trash(items)
        return tid


def delete_path(path):
    abs_path = sandbox(path)
    if abs_path == HOME:
        raise ValueError("不能删除主目录")
    if abs_path == DATA or abs_path.startswith(DATA + os.sep):
        raise ValueError("面板数据目录不可删除")
    if not os.path.exists(abs_path):
        raise FileNotFoundError("文件不存在")
    tid = to_trash(abs_path)
    return {"ok": "1", "trashed": True, "id": tid, "name": os.path.basename(abs_path)}


def delete_permanent(path):
    abs_path = sandbox(path)
    if abs_path == HOME:
        raise ValueError("不能删除主目录")
    if abs_path == DATA or abs_path.startswith(DATA + os.sep):
        raise ValueError("面板数据目录不可删除")
    if os.path.isdir(abs_path):
        shutil.rmtree(abs_path)
    elif os.path.exists(abs_path):
        os.remove(abs_path)
    return {"ok": "1", "trashed": False}


def trash_list():
    items = _load_trash()
    items.sort(key=lambda i: i["deleted_at"], reverse=True)
    return {"items": items, "count": len(items),
            "total_size": sum(i["size"] for i in items),
            "keep_days": core.file_settings()["trash_keep_days"]}


def trash_restore(ids):
    restored, failed = 0, []
    with T_LOCK:
        items = _load_trash()
        for tid in ids:
            idx = next((i for i, it in enumerate(items) if it["id"] == tid), -1)
            if idx < 0:
                failed.append(f"{tid}: 条目不存在")
                continue
            it = items[idx]
            if os.path.exists(it["orig_path"]):
                failed.append(f"{it['name']}: 原位置已存在同名文件")
                continue
            os.makedirs(os.path.dirname(it["orig_path"]), mode=0o755, exist_ok=True)
            src = os.path.join(TRASH_ITEMS, tid)
            try:
                os.rename(src, it["orig_path"])
            except OSError:
                copy_tree(src, it["orig_path"])
                shutil.rmtree(src, ignore_errors=True)
            items.pop(idx)
            restored += 1
        _save_trash(items)
    return {"restored": restored, "failed": failed}


def trash_purge(ids=None, all_=False):
    with T_LOCK:
        items = _load_trash()
        if all_:
            shutil.rmtree(TRASH_ITEMS, ignore_errors=True)
            os.makedirs(TRASH_ITEMS, mode=0o700, exist_ok=True)
            _save_trash([])
            return {"ok": "1", "purged": len(items)}
        ids = ids or []
        for tid in ids:
            shutil.rmtree(os.path.join(TRASH_ITEMS, tid), ignore_errors=True)
        _save_trash([i for i in items if i["id"] not in set(ids)])
        return {"ok": "1", "purged": len(ids)}


def trash_autoclean():
    days = core.file_settings()["trash_keep_days"]
    if days <= 0:
        return
    deadline = time.time() - days * 86400
    with T_LOCK:
        items = _load_trash()
        keep = []
        for it in items:
            if it["deleted_at"] < deadline:
                shutil.rmtree(os.path.join(TRASH_ITEMS, it["id"]), ignore_errors=True)
                continue
            keep.append(it)
        if len(keep) != len(items):
            _save_trash(keep)


# ---------------- 新建 / 改名 / 复制 ----------------

def mkdir(path, name):
    valid_name(name)
    base = sandbox(path)
    target = os.path.join(base, name)
    os.mkdir(target, 0o755)
    return {"ok": "1", "path": target}


def touch(path, name):
    valid_name(name)
    base = sandbox(path)
    target = os.path.join(base, name)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    return {"ok": "1", "path": target}


def rename(path, name):
    valid_name(name)
    abs_path = sandbox(path)
    parent = os.path.dirname(abs_path)
    if parent == abs_path:
        raise ValueError("不能重命名根目录")
    target = os.path.join(parent, name)
    if os.path.exists(target):
        raise ValueError("目标已存在")
    os.rename(abs_path, target)
    return {"ok": "1", "path": target}


def copy_move(paths, dest, move=False):
    dest_dir = sandbox(dest)
    if not os.path.isdir(dest_dir):
        raise ValueError("目标不是目录")
    done, failed = 0, []
    for p in paths:
        try:
            abs_path = sandbox(p)
        except ValueError:
            failed.append(f"{p}: 路径非法")
            continue
        # 拒绝把目录复制进它自己
        if abs_path == dest_dir or dest_dir.startswith(abs_path + os.sep):
            failed.append(f"{os.path.basename(abs_path)}: 不能复制到自身或其子目录")
            continue
        target = unique_path(os.path.join(dest_dir, os.path.basename(abs_path)))
        try:
            if move:
                try:
                    os.rename(abs_path, target)
                except OSError:
                    copy_tree(abs_path, target)
                    if os.path.isdir(abs_path):
                        shutil.rmtree(abs_path, ignore_errors=True)
                    else:
                        os.remove(abs_path)
            else:
                copy_tree(abs_path, target)
            done += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{os.path.basename(abs_path)}: {e}")
    return {"done": done, "failed": failed}


def chmod(path, mode, recursive=False):
    abs_path = sandbox(path)
    try:
        perm = int(str(mode), 8)
    except ValueError:
        raise ValueError("权限需为 0–777 的八进制数")
    if perm > 0o777:
        raise ValueError("权限需为 0–777 的八进制数")
    if not os.path.exists(abs_path):
        raise FileNotFoundError("文件不存在")
    if os.path.isdir(abs_path) and recursive:
        n = 0
        for root, dirs, files in os.walk(abs_path):
            os.chmod(root, perm)
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), perm)
                except OSError:
                    pass
                n += 1
                if n > 20000:
                    break
        return {"ok": "1", "changed": n, "recursive": True}
    os.chmod(abs_path, perm)
    return {"ok": "1", "changed": 1}


def stat(path):
    abs_path = sandbox(path)
    st = os.stat(abs_path)
    return {
        "path": abs_path, "name": os.path.basename(abs_path),
        "is_dir": os.path.isdir(abs_path), "size": st.st_size,
        "dir_size": core.dir_size(abs_path) if os.path.isdir(abs_path) else st.st_size,
        "mode": f"{st.st_mode & 0o777:04o}", "mode_str": _mode_str(st.st_mode),
        "mtime": int(st.st_mtime), "owner": "termux",
        "versions": len([v for v in _load_versions() if v["path"] == abs_path]),
    }


# ---------------- 收藏 ----------------

def fav_list():
    rows = []
    for p in read_json(FAVS, []):
        rows.append({"path": p, "name": os.path.basename(p), "valid": os.path.exists(p)})
    return rows


def fav_toggle(path):
    abs_path = sandbox(path)
    with F_LOCK:
        favs = read_json(FAVS, [])
        if abs_path in favs:
            favs.remove(abs_path)
            added = False
        else:
            if len(favs) >= 30:
                raise ValueError("收藏夹最多 30 项")
            favs.append(abs_path)
            added = True
        write_json(FAVS, favs)
        return {"added": added, "count": len(favs)}


# ---------------- 搜索 ----------------

def search_task(t, root, q, suffixes, recursive, limit, timeout_ms, with_content=True):
    deadline = time.time() + timeout_ms / 1000.0
    hits = []
    truncated = False
    ql = q.lower()
    core.task_log(t, f"目录: {root}")
    core.task_log(t, f"条件: {q!r} · 后缀 {suffixes} · 递归 {recursive}")

    def match(p, name, st):
        nonlocal truncated
        if time.time() > deadline:
            truncated = True
            return False
        if core.task_canceled(t):
            return False
        if len(hits) >= limit:
            truncated = True
            return False
        is_dir = os.path.isdir(p)
        if ql in name.lower():
            hits.append({"path": p, "name": name, "is_dir": is_dir,
                         "size": st.st_size, "line": 0, "text": "", "match": "name"})
            if len(hits) >= limit:
                truncated = True
                return False
        if is_dir or not with_content or st.st_size > 2 << 20:
            return True
        if suffixes:
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext not in suffixes:
                return True
        try:
            with open(p, "rb") as f:
                head = f.read(512)
            if b"\x00" in head:
                return True
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if ql in line.lower():
                        txt = line.rstrip()[:200]
                        hits.append({"path": p, "name": name, "is_dir": False,
                                     "size": st.st_size, "line": i, "text": txt,
                                     "match": "content"})
                        if len(hits) >= limit:
                            truncated = True
                            return False
                    if i > 200000:
                        break
        except OSError:
            pass
        return len(hits) < limit

    if recursive:
        for root_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                p = os.path.join(root_dir, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not match(p, fn, st):
                    break
            if truncated or len(hits) >= limit or time.time() > deadline:
                break
            for dn in list(dirs):
                p = os.path.join(root_dir, dn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not match(p, dn, st):
                    break
    else:
        for e in os.scandir(root):
            try:
                st = e.stat()
            except OSError:
                continue
            if not match(e.path, e.name, st):
                break

    core.task_result(t, {"hits": hits, "truncated": truncated, "root": root,
                         "limit": limit, "timeout_ms": timeout_ms})
    core.task_log(t, f"命中 {len(hits)} 条" + ("（已截断）" if truncated else ""))


def start_search(path, q, suffix, sub, limit, timeout_ms, with_content=True):
    root = sandbox(path)
    if not q.strip():
        raise ValueError("搜索内容不能为空")
    limit = 200 if not limit or limit > 500 else limit
    timeout_ms = 8000 if not timeout_ms or timeout_ms > 30000 else timeout_ms
    suffixes = [s.strip().lower().lstrip(".") for s in (suffix or "").split(",") if s.strip()]
    t = core.run_job("search", f"搜索 {q}",
                     lambda job: search_task(job, root, q, suffixes, sub, limit, timeout_ms, with_content))
    return core.task_view(t)


# ---------------- 压缩 / 解压 / 远程下载 ----------------

def start_archive(paths, fmt, dest=""):
    if not paths:
        raise ValueError("未选择文件")
    abs_paths, parent = [], ""
    for p in paths:
        a = sandbox(p)
        if not os.path.exists(a):
            raise ValueError(f"文件不存在: {os.path.basename(a)}")
        pa = os.path.dirname(a)
        if not parent:
            parent = pa
        elif parent != pa:
            raise ValueError("请选择同一目录下的文件，跨目录请分多次压缩")
        abs_paths.append(a)
    fmt = "tar.gz" if fmt == "tar.gz" else "zip"
    if dest:
        dest = sandbox(dest)
    else:
        base = os.path.splitext(os.path.basename(abs_paths[0]))[0] if len(abs_paths) == 1 else "archive"
        dest = unique_path(os.path.join(parent, base + (".tar.gz" if fmt == "tar.gz" else ".zip")))
    names = [os.path.basename(a) for a in abs_paths]

    def job(t):
        core.task_log(t, f"目标: {dest}")
        core.task_progress(t, 30)
        if fmt == "zip":
            code = core.run_shell_task(t, ["zip", "-r", "-q", dest] + names, cwd=parent)
        else:
            code = core.run_shell_task(t, ["tar", "-czf", dest, "-C", parent] + names)
        if code != 0:
            raise RuntimeError(f"归档命令退出码 {code}")
        core.task_progress(t, 100)
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        core.task_result(t, {"dest": dest, "size": size})
        core.task_log(t, f"完成: {dest} ({size} 字节)")

    return core.task_view(core.run_job("archive", f"压缩 {len(abs_paths)} 项", job))


def start_extract(src, dest=""):
    src = sandbox(src)
    if not os.path.isfile(src):
        raise ValueError("不是压缩文件")
    if not dest:
        base = src
        for ext in (".tar.gz", ".tgz", ".tar", ".zip", ".gz", ".xz"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        dest = unique_path(base)
    else:
        dest = sandbox(dest)
    low = src.lower()

    def job(t):
        os.makedirs(dest, exist_ok=True)
        core.task_log(t, f"解压到: {dest}")
        core.task_progress(t, 20)
        if low.endswith(".zip"):
            code = core.run_shell_task(t, ["unzip", "-o", "-q", src, "-d", dest])
        elif low.endswith((".tar.gz", ".tgz")):
            code = core.run_shell_task(t, ["tar", "-xzf", src, "-C", dest, "--no-same-owner"])
        elif low.endswith(".tar"):
            code = core.run_shell_task(t, ["tar", "-xf", src, "-C", dest, "--no-same-owner"])
        elif low.endswith(".tar.xz") or low.endswith(".xz"):
            code = core.run_shell_task(t, ["tar", "-xJf", src, "-C", dest, "--no-same-owner"])
        elif low.endswith(".gz"):
            import gzip
            out_path = os.path.join(dest, os.path.basename(src)[:-3])
            with gzip.open(src, "rb") as fi, open(out_path, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            code = 0
            core.task_log(t, f"解压完成: {out_path}")
        else:
            raise RuntimeError("不支持的压缩格式（zip / tar / tar.gz / tgz / gz / tar.xz）")
        if code != 0:
            raise RuntimeError(f"解压命令退出码 {code}")
        core.task_result(t, {"dest": dest})

    return core.task_view(core.run_job("extract", f"解压 {os.path.basename(src)}", job))


def remote_download(url, dest_dir, name=""):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("只支持 http / https 链接")
    d = sandbox(dest_dir)
    if not os.path.isdir(d):
        raise ValueError("目标不是目录")
    if name:
        valid_name(name)

    def job(t):
        req = urllib.request.Request(url, headers={"User-Agent": f"PocketPanel/{core.APP_VERSION}"})
        core.task_log(t, f"GET {url}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            fname = name or _guess_name(u, resp)
            target = unique_path(os.path.join(d, fname))
            tmp = target + ".part"
            total = int(resp.headers.get("Content-Length") or 0)
            core.task_log(t, f"保存到: {target}（远端大小 {total} 字节）")
            written, last = 0, 0.0
            with open(tmp, "wb") as f:
                while True:
                    if core.task_canceled(t):
                        raise RuntimeError("已取消")
                    chunk = resp.read(128 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        core.task_progress(t, written * 100 // total)
                        if time.time() - last > 0.5:
                            last = time.time()
                            core.task_log(t, f"已下载 {core.human_size(written)} / {core.human_size(total)}")
            if total and written != total:
                os.remove(tmp)
                raise RuntimeError(f"大小不一致：期望 {total}，实际 {written}")
            os.replace(tmp, target)
        core.task_progress(t, 100)
        core.task_result(t, {"path": target, "size": written})
        core.task_log(t, f"完成: {target} ({core.human_size(written)})")

    return core.task_view(core.run_job("download", f"下载 {url}", job))


def _guess_name(u, resp):
    cd = resp.headers.get("Content-Disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            n = part[9:].strip("\"'")
            if n:
                return os.path.basename(n)
    base = os.path.basename(u.path)
    return base or f"download_{int(time.time())}"


# ---------------- 分片上传 ----------------

def upload_meta(uid):
    valid_name(uid)
    p = os.path.join(UPLOADS, uid, "meta.json")
    if not os.path.isfile(p):
        raise FileNotFoundError("上传任务不存在或已过期")
    return read_json(p, {})


def upload_init(path, name, size, chunk_size=0, uid=""):
    valid_name(name)
    d = sandbox(path)
    if not os.path.isdir(d):
        raise ValueError("目标不是目录")
    size = int(size)
    if size <= 0:
        raise ValueError("文件大小无效")
    cs = int(chunk_size or 0)
    if cs <= 0 or cs > 8 << 20:
        cs = 1 << 20
    total = (size + cs - 1) // cs
    if total > 20000:
        raise ValueError("分片数量过多，请增大分片大小")
    uid = uid if uid and uid.replace("_", "").isalnum() else os.urandom(10).hex()
    meta = {"id": uid, "path": os.path.join(d, name), "name": name,
            "size": size, "chunk_size": cs, "total": total,
            "created_at": int(time.time())}
    os.makedirs(os.path.join(UPLOADS, uid), mode=0o700, exist_ok=True)
    write_json(os.path.join(UPLOADS, uid, "meta.json"), meta)
    return {"id": uid, "total": total, "chunk_size": cs, "uploaded": uploaded_parts(uid)}


def uploaded_parts(uid):
    d = os.path.join(UPLOADS, uid)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        if n.endswith(".part") and n[:-5].isdigit():
            out.append(int(n[:-5]))
    return sorted(out)


def upload_chunk(uid, idx, data):
    meta = upload_meta(uid)
    if idx < 0 or idx >= meta["total"]:
        raise ValueError("分片序号超出范围")
    if len(data) > 8 << 20:
        raise ValueError("分片超过 8MB")
    with open(os.path.join(UPLOADS, uid, f"{idx}.part"), "wb") as f:
        f.write(data)
    return {"ok": "1", "index": idx, "size": len(data)}


def upload_complete(uid):
    meta = upload_meta(uid)
    d = os.path.join(UPLOADS, uid)
    parts = uploaded_parts(uid)
    if len(parts) != meta["total"]:
        missing = [i for i in range(meta["total"]) if i not in parts]
        raise ValueError(f"分片 {missing[0]} 缺失，请继续上传")
    total = sum(os.path.getsize(os.path.join(d, f"{i}.part")) for i in range(meta["total"]))
    if total != meta["size"]:
        raise ValueError(f"分片总大小 {total} 与声明 {meta['size']} 不一致")
    target = unique_path(meta["path"])
    tmp = target + ".uploading"
    with open(tmp, "wb") as out:
        for i in range(meta["total"]):
            with open(os.path.join(d, f"{i}.part"), "rb") as fi:
                shutil.copyfileobj(fi, out)
    os.replace(tmp, target)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": "1", "path": target, "size": meta["size"]}


def upload_abort(uid):
    valid_name(uid)
    shutil.rmtree(os.path.join(UPLOADS, uid), ignore_errors=True)
    return {"ok": "1"}


def clean_uploads():
    try:
        names = os.listdir(UPLOADS)
    except OSError:
        return
    deadline = time.time() - 86400
    for n in names:
        p = os.path.join(UPLOADS, n)
        try:
            if os.path.getmtime(p) < deadline:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
