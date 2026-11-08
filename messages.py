"""消息中心（M16 的最小落地）：后台事件的通知收集处。

计划任务完成/失败会写一条进来；之后压缩、上传这类长任务也可以复用。
上限 100 条，读过的标记后仍保留，手动清空才删。
"""
import json
import os
import secrets
import threading
import time

import core

FILE = os.path.join(core.DATA, "messages.json")
MAX_ITEMS = 100

_lock = threading.RLock()
_items = []
_loaded = False


def init():
    global _items, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _items = data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        _items = []


def _save():
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_items[:MAX_ITEMS], fh, ensure_ascii=False)
    os.replace(tmp, FILE)


def add(mtype, title, body=""):
    """后台事件入口：type ∈ info | ok | err。"""
    init()
    with _lock:
        _items.insert(0, {
            "id": secrets.token_hex(5), "ts": int(time.time()), "type": mtype,
            "title": str(title)[:80], "body": str(body)[:200], "read": False,
        })
        _save()
    return True


def listing():
    init()
    with _lock:
        items = list(_items)
    unread = sum(1 for m in items if not m.get("read"))
    return {"items": items[:60], "unread": unread}


def unread_count():
    init()
    with _lock:
        return sum(1 for m in _items if not m.get("read"))


def mark_read(ids=None, mark_all=False):
    init()
    with _lock:
        for m in _items:
            if mark_all or (ids and m.get("id") in ids):
                m["read"] = True
        _save()
    return True


def clear():
    init()
    with _lock:
        _items.clear()
        _save()
    return True
