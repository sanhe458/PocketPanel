"""服务端事件总线：后台任务 → 前端 WebSocket 推送。

终端那条字节流通道（ws.py）是「一对一传字节」，不适合广播。这里做的是
「一对多推事件」：网络守卫、任务完成、隧道告警都往这里 emit，
前端每个页面订阅一条连接，收到就更新 UI。

为什么不用现成的 ws.py：
  ws.pump 的读循环绑在终端会话上（sess.read），没有会话就没有东西可推。
  事件通道是纯服务端主动写、客户端只读，模型完全不同，所以另起一套。

连接模型：
  每个订阅者一条连接，服务端往连接里写 JSON 文本帧。
  写侧用「队列 + 独立写线程」：主线程只管从队列取事件，
  避免一次慢写入把整个事件循环卡住。

断线重连由前端负责（指数退避），服务端不做状态。
"""
import json
import queue
import threading
import time

import ws

# 订阅者：id -> {"q": queue.Queue, "lock": 写锁}
_subs = {}
_lock = threading.RLock()
MAX_QUEUE = 64
_seq = 0

# 最近事件的历史环形缓冲：前端刚连上时补发最近的几条，
# 免得「连上之前发生的网络切换」被漏掉
_history = []
HISTORY_MAX = 20


def _next_id():
    global _seq
    with _lock:
        _seq += 1
        return _seq


def subscribe():
    """订阅。返回 (sub_id, queue)。调用方负责 unsubscribe。"""
    sid = _next_id()
    q = queue.Queue(maxsize=MAX_QUEUE)
    with _lock:
        _subs[sid] = {"q": q}
    return sid, q


def unsubscribe(sid):
    with _lock:
        _subs.pop(sid, None)


def emit(event, data=None):
    """广播一个事件给所有订阅者。

    订阅者队列满时丢弃最旧的一条 —— 网络守卫的事件是「状态快照」
    而非流水账，丢旧留新比直接丢弃更合理。
    """
    payload = {"event": event, "ts": int(time.time()), "data": data or {}}
    with _lock:
        _history.append(payload)
        if len(_history) > HISTORY_MAX:
            del _history[:len(_history) - HISTORY_MAX]
        targets = list(_subs.items())
    for sid, sub in targets:
        q = sub["q"]
        try:
            q.put_nowait(payload)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass


def history():
    with _lock:
        return list(_history)


def subscriber_count():
    with _lock:
        return len(_subs)


# ---------------- WebSocket 通道 ----------------

def pump(sock, reader, writer, origin_ok=True):
    """一条事件订阅连接。

    读：客户端发来的帧只认 ping / close，其余忽略。
    写：主线程从队列取事件，写线程负责实际写帧。
    """
    sid, q = subscribe()
    stop = threading.Event()
    wlock = threading.Lock()

    def write_json(obj):
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with wlock:
            ws.write_frame(writer, 0x1, data)

    def reader_loop():
        # 客户端一般只发 ping/close；读到东西就回 pong，close 就结束
        try:
            while not stop.is_set():
                fin, opcode, payload = ws.read_frame(reader)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with wlock:
                        ws.write_frame(writer, 0xA, payload)
        except Exception:  # noqa: BLE001
            pass
        finally:
            stop.set()

    t = threading.Thread(target=reader_loop, daemon=True, name=f"ev-{sid}")
    t.start()

    # 补发最近的历史事件，让刚连上的前端不至于漏掉刚才的切换
    for h in history():
        try:
            write_json(h)
        except Exception:  # noqa: BLE001
            break

    try:
        while not stop.is_set():
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                # 没有事件时定期发 ping，保住连接不被中间设备掐掉
                if not _alive(sock):
                    break
                continue
            if stop.is_set():
                break
            try:
                write_json(item)
            except Exception:  # noqa: BLE001
                break
    finally:
        stop.set()
        unsubscribe(sid)
        try:
            sock.shutdown(2)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _alive(sock):
    """粗判连接是否还在：能取到 socket 就算活着。"""
    try:
        sock.getpeername()
        return True
    except OSError:
        return False
