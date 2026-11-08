"""WebSocket（RFC 6455）最小实现，只服务终端这条字节流。

为什么手写而不是装 `websockets`：`http.server` 是同步多线程模型，引异步库
要么换掉整个服务器，要么加一层线程/事件循环桥接。终端只需要「双向传字节」
这一件事，RFC 6455 的握手 + 六种帧加起来不到 200 行，比胶水代码还短。

约定：http.server 的 rfile 带缓冲，握手时它可能已经把客户端紧随其后的
首帧读进用户态缓冲了。所以读一律走 rfile 而不是裸 socket，否则会丢字节。

线程模型：一个连接两个线程——读线程阻塞收帧（输入、resize、心跳），
主线程挂在自己的 Condition 上等 PTY 输出（零轮询，出字即推）。两边
共用一个写锁，避免帧交织。
"""
import base64
import hashlib
import json
import select
import socket
import struct
import threading
import time
import traceback

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PAYLOAD = 1 << 20          # 单帧上限 1MB，终端输入不可能这么大
PING_EVERY = 30                # 服务端心跳间隔
IDLE_LIMIT = 95                # 这么久没收到任何客户端帧就判死
WRITE_WAIT = 6                 # 客户端 6 秒不读就断开，避免推送线程永久卡死


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _read_exact(reader, n):
    buf = b""
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def read_frame(reader):
    """返回 (fin, opcode, payload)。客户端帧必须带 mask，这里一并解开。"""
    b1, b2 = _read_exact(reader, 2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(reader, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(reader, 8))[0]
    if length > MAX_PAYLOAD:
        raise ValueError("帧过大")
    mask = _read_exact(reader, 4) if masked else b""
    data = _read_exact(reader, length) if length else b""
    if masked and data:
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return fin, opcode, data


def write_frame(writer, opcode, data=b""):
    if isinstance(data, str):
        data = data.encode()
    n = len(data)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    writer.write(bytes(head) + data)
    writer.flush()


def pump(sock, reader, writer, sess):
    """一条已握手的终端连接，阻塞到任一侧关闭。"""
    stop = threading.Event()
    wlock = threading.Lock()
    seen = [time.time()]

    def send(opcode, payload=b""):
        if stop.is_set():
            return
        if not select.select([], [sock], [], WRITE_WAIT)[1]:
            # 对端长时间不读：TCP 发送窗口塞满，再等下去没有意义
            stop.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        with wlock:
            write_frame(writer, opcode, payload)

    def send_json(obj):
        send(0x1, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def handle(text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        t = msg.get("t")
        try:
            if t == "in":
                data = base64.b64decode(msg.get("d") or "", validate=True)
                if data:
                    sess.write(data)
            elif t == "resize":
                sess.resize(msg.get("cols") or 80, msg.get("rows") or 24)
            elif t == "sig":
                sess.signal(msg.get("sig") or "int")
            elif t == "ping":
                send_json({"t": "pong"})
        except Exception:  # noqa: BLE001
            # 会话已结束、分包非法之类，不值得断开整条连接
            pass

    def reader_loop():
        frag_op = None
        frag = b""
        try:
            while not stop.is_set():
                fin, opcode, data = read_frame(reader)
                seen[0] = time.time()
                if opcode == 0x8:                      # close
                    send(0x8, struct.pack(">H", 1000))
                    break
                if opcode == 0x9:                      # ping
                    send(0xA, data)
                    continue
                if opcode == 0xA:                      # pong
                    continue
                if opcode in (0x1, 0x2):
                    frag_op, frag = opcode, data
                    if not fin:
                        continue
                elif opcode == 0x0:
                    frag += data
                    if not fin:
                        continue
                else:
                    continue
                if frag_op == 0x1:
                    handle(frag.decode("utf-8", "replace"))
                frag_op, frag = None, b""
        except Exception:  # noqa: BLE001
            pass
        finally:
            stop.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=reader_loop, daemon=True,
                     name=f"ws-{sess.id}").start()

    seq = 0
    last_ping = 0.0
    try:
        while not stop.is_set():
            r = sess.read(seq, 0.5)
            if r["data"] or r["reset"] or not r["alive"]:
                msg = {"t": "out"}
                msg.update(r)
                send_json(msg)
                seq = r["seq"]
            if not r["alive"]:
                send_json({"t": "exit", "code": r["exit"]})
                send(0x8, struct.pack(">H", 1000))
                break
            now = time.time()
            if now - last_ping >= PING_EVERY:
                send(0x9)
                last_ping = now
            if now - seen[0] > IDLE_LIMIT:
                break
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        stop.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
