"""PTY 终端：本地 shell（bash / sh）与 SSH 远程登录共用一套会话模型。

设计要点（都是被真机教出来的）：

* 用 `pty.fork()` 而不是 `Popen(stdin=pty)`——只有 forkpty 会在 C 层做
  setsid + TIOCSCTTY + dup2，Ctrl+C 才会变成 SIGINT 发给前台进程组；
  用管道或裸 Popen 的话 `top`、`vim`、`less` 全是残废。
* 输出走环形缓冲 + 单调递增字节序号。手机切后台、锁屏、WebView 被杀，
  回来带上 seq 就能续上；序号落后太多就返回 reset，前端清屏重画。
* 服务端长轮询（Condition 等待）代替固定间隔轮询：有输出立刻返回，
  没输出挂 20 秒。空闲时零请求、零 CPU。
* 每会话一个读线程，`select` 0.5s 超时，读到 EOF 即收尸。

SSH 不引第三方库，直接用设备上的 OpenSSH 客户端：
终端里跑 `ssh` 二进制，密码通过 `SSH_ASKPASS` 脚本喂进去
（OpenSSH 8.4+ 的 `SSH_ASKPASS_REQUIRE=force` 强制启用），
这样密钥、known_hosts、~/.ssh/config、ProxyJump 全部原生可用。
"""
import base64
import errno
import fcntl
import json
import os
import pty
import re
import secrets
import select
import shutil
import socket
import signal
import struct
import termios
import threading
import time
import traceback

import core

BUF_MAX = 256 * 1024          # 单会话回滚缓冲
MAX_SESSIONS = 8              # 安卓上 8 个 PTY 足够，再多是浪费内存
IDLE_TTL = 3 * 3600           # 3 小时无操作自动回收
EXIT_KEEP = 600               # 已退出会话保留 10 分钟供前端看最后输出
ASKPASS_DIR = os.path.join(core.DATA, "askpass")
HOSTS_FILE = os.path.join(core.DATA, "ssh_hosts.json")
KEYS_DIR = os.path.join(core.DATA, "keys")

HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

_sessions = {}
_lock = threading.RLock()


# ---------------- 环境 ----------------

def _base_env(shell_path=None):
    """继承面板进程环境（runit 的 run 脚本已导好 HOME/PREFIX/PATH）。"""
    env = dict(os.environ)
    prefix = env.get("PREFIX") or core.PREFIX
    env.update({
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "LANG": env.get("LANG") or "en_US.UTF-8",
        "LC_ALL": env.get("LC_ALL") or "en_US.UTF-8",
        "PATH": env.get("PATH") or f"{prefix}/bin:{prefix}/bin/applets",
        "TMPDIR": env.get("TMPDIR") or f"{prefix}/tmp",
        "TERMUX_VERSION": env.get("TERMUX_VERSION", ""),
    })
    if shell_path:
        env["SHELL"] = shell_path
    env.pop("LD_PRELOAD", None)
    return env


def _which(name, fallback):
    p = shutil.which(name)
    if p:
        return p
    guess = os.path.join(core.PREFIX, "bin", name)
    return guess if os.path.exists(guess) else fallback


def _winsize(fd, cols, rows):
    cols = max(20, min(int(cols or 80), 500))
    rows = max(5, min(int(rows or 24), 300))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    return cols, rows


# ---------------- 会话 ----------------

class Session:
    def __init__(self, sid, kind, title, subtitle, argv, env, cwd, cols, rows,
                 host_id=None):
        self.id = sid
        self.kind = kind
        self.title = title
        self.subtitle = subtitle
        self.host_id = host_id
        self.argv = argv
        self.created = time.time()
        self.last = time.time()
        self.buf = bytearray()
        self.total = 0                 # 累计输出字节数，即前端持有的 seq
        self.cond = threading.Condition()
        self.alive = True
        self.exit = None
        self.cols = cols
        self.rows = rows
        self.pid = 0
        self.fd = -1
        self._wlock = threading.Lock()
        self._closing = False
        self._spawn(env, cwd, cols, rows)

    # -- 生命周期 --

    def _spawn(self, env, cwd, cols, rows):
        b_argv = [a.encode() for a in self.argv]
        b_env = {str(k).encode(): str(v).encode() for k, v in env.items()}
        b_cwd = (cwd or core.HOME).encode()
        pid, fd = pty.fork()
        if pid == 0:
            # 子进程：只做 exec，任何 Python 异常都不要往回跑。
            #
            # exec 会保留 SIG_IGN 处置，所以这里必须把父进程（runsv 启动，
            # 按惯例忽略 SIGINT/SIGQUIT）留下的处置恢复成默认。否则 bash
            # 会把"忽略 SIGINT"一代代传下去：终端里按 Ctrl+C 只回显 ^C，
            # 前台进程纹丝不动，`kill -INT` 也杀不掉。
            # 顺带清空信号屏蔽字，避免工作线程继承下来的 blocked 掩码。
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, set())
            except Exception:  # noqa: BLE001
                pass
            for _s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
                       signal.SIGHUP, signal.SIGPIPE):
                try:
                    signal.signal(_s, signal.SIG_DFL)
                except Exception:  # noqa: BLE001
                    pass
            try:
                os.chdir(b_cwd)
                os.execve(b_argv[0], b_argv, b_env)
            except BaseException:
                pass
            os._exit(127)
        self.pid, self.fd = pid, fd
        self.cols, self.rows = _winsize(fd, cols, rows)
        threading.Thread(target=self._reader, daemon=True,
                         name=f"term-{self.id}").start()

    def _reader(self):
        fd = self.fd
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if fd not in ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            if not chunk:
                break
            self._append(chunk)
        self._finish()

    def _append(self, chunk):
        with self.cond:
            self.buf += chunk
            self.total += len(chunk)
            over = len(self.buf) - BUF_MAX
            if over > 0:
                del self.buf[:over]
            self.last = time.time()
            self.cond.notify_all()

    def _finish(self):
        code = None
        for _ in range(20):
            try:
                _, status = os.waitpid(self.pid, 0)
                code = os.waitstatus_to_exitcode(status)
                break
            except ChildProcessError:
                code = -1
                break
            except InterruptedError:
                continue
            except OSError:
                code = -1
                break
        with self.cond:
            if not self.alive:
                return
            self.alive = False
            self.exit = code
            self.last = time.time()
            self.cond.notify_all()
        try:
            os.close(self.fd)
        except OSError:
            pass
        _cleanup_askpass(self.id)

    # -- 读写 --

    def write(self, data):
        with self._wlock:
            if not self.alive:
                raise RuntimeError("会话已结束")
            try:
                os.write(self.fd, data)
            except OSError as e:
                raise RuntimeError(f"写入失败：{e.strerror}")
        self.last = time.time()

    def read(self, seq, wait=0.0):
        deadline = time.time() + max(0.0, min(float(wait or 0), 25.0))
        data = b""
        reset = False
        with self.cond:
            start = self.total - len(self.buf)
            if seq < start or seq > self.total:
                # 前端落后到缓冲之外，或序号异常：全量重发
                data, reset = bytes(self.buf), True
            else:
                data = bytes(self.buf[seq - start:])
            while not data and self.alive and time.time() < deadline:
                self.cond.wait(max(0.05, deadline - time.time()))
                start = self.total - len(self.buf)
                if seq < start:
                    data, reset = bytes(self.buf), True
                elif seq <= self.total:
                    data = bytes(self.buf[seq - start:])
            return {
                "seq": self.total,
                "start": self.total - len(self.buf),
                "data": base64.b64encode(data).decode() if data else "",
                "reset": reset,
                "alive": self.alive,
                "exit": self.exit,
                "cols": self.cols,
                "rows": self.rows,
            }

    def resize(self, cols, rows):
        if not self.alive:
            return
        self.cols, self.rows = _winsize(self.fd, cols, rows)

    def signal(self, name):
        """给前台进程组发信号：Ctrl+C / Ctrl+Z / Ctrl+\\ 之类。"""
        if not self.alive:
            return
        sig = {"int": signal.SIGINT, "term": signal.SIGTERM, "quit": signal.SIGQUIT,
               "hup": signal.SIGHUP, "kill": signal.SIGKILL}.get(name)
        if sig is None:
            raise ValueError("未知信号")
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except OSError:
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass

    def close(self):
        if self._closing:
            return
        self._closing = True
        for sig in (signal.SIGHUP, signal.SIGTERM):
            if not self.alive:
                break
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except OSError:
                try:
                    os.kill(self.pid, sig)
                except OSError:
                    pass
            for _ in range(15):
                if not self.alive:
                    break
                time.sleep(0.01)
        if self.alive:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGKILL)
            except OSError:
                pass
        # 读线程可能还没收尸，这里不强等
        _cleanup_askpass(self.id)

    # -- 描述 --

    def info(self):
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "subtitle": self.subtitle, "created": int(self.created),
            "alive": self.alive, "exit": self.exit,
            "cols": self.cols, "rows": self.rows, "host_id": self.host_id,
            "bytes": self.total,
        }


# ---------------- 会话表 ----------------

def _reap(now=None):
    now = now or time.time()
    dead = []
    with _lock:
        for sid, s in list(_sessions.items()):
            if s.alive and now - s.last > IDLE_TTL:
                dead.append(s)
            elif not s.alive and now - s.last > EXIT_KEEP:
                dead.append(s)
        for s in dead:
            _sessions.pop(s.id, None)
    for s in dead:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def _reaper():
    while True:
        time.sleep(60)
        try:
            _reap()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _new_sid():
    return secrets.token_urlsafe(9)


def _register(sess):
    with _lock:
        if len(_sessions) >= MAX_SESSIONS:
            # 先踢掉已经退出的，再考虑拒绝——用户连开连关几次不该被拦住
            for sid, s in list(_sessions.items()):
                if not s.alive:
                    _sessions.pop(sid, None)
                    if len(_sessions) < MAX_SESSIONS:
                        break
            if len(_sessions) >= MAX_SESSIONS:
                raise RuntimeError(f"会话数已达上限（{MAX_SESSIONS}），请先关闭不用的标签")
        _sessions[sess.id] = sess
    return sess


class StreamSession:
    """非 PTY 会话：字节流来自一个已建立的 socket。

    容器 exec 走 Docker 的劫持通道（101 Upgrade 后是裸流），没有本地 PTY。
    这里实现与 Session 相同的读写/环形缓冲接口，WebSocket 泵与长轮询不用改。
    """

    def __init__(self, sid, title, subtitle, sock, resize_fn=None,
                 close_fn=None, kind="exec"):
        self.id = sid
        self.kind = kind
        self.title = title
        self.subtitle = subtitle
        self.host_id = None
        self.created = time.time()
        self.last = time.time()
        self.buf = bytearray()
        self.total = 0
        self.cond = threading.Condition()
        self.alive = True
        self.exit = None
        self.cols = 80
        self.rows = 24
        self.pid = 0
        self.transport = "ws"
        self._sock = sock
        self._resize_fn = resize_fn
        self._close_fn = close_fn
        self._closing = False
        threading.Thread(target=self._reader, daemon=True,
                         name=f"stream-{sid}").start()

    def _reader(self):
        sock = self._sock
        while True:
            try:
                ready, _, _ = select.select([sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            self._append(chunk)
        self._finish()

    def _append(self, chunk):
        with self.cond:
            self.buf += chunk
            self.total += len(chunk)
            over = len(self.buf) - BUF_MAX
            if over > 0:
                del self.buf[:over]
            self.last = time.time()
            self.cond.notify_all()

    def _finish(self):
        with self.cond:
            if not self.alive:
                return
            self.alive = False
            self.last = time.time()
            self.cond.notify_all()

    def write(self, data):
        if not self.alive:
            raise RuntimeError("会话已结束")
        try:
            # _sock 可能是裸 socket，也可能是 dockerclient.Hijack 这类包装
            send = getattr(self._sock, "sendall", None) or self._sock.send
            send(data)
        except OSError as e:
            raise RuntimeError(f"写入失败：{e.strerror or e}")
        self.last = time.time()

    def read(self, seq, wait=0.0):
        deadline = time.time() + max(0.0, min(float(wait or 0), 25.0))
        data = b""
        reset = False
        with self.cond:
            start = self.total - len(self.buf)
            if seq < start or seq > self.total:
                data, reset = bytes(self.buf), True
            else:
                data = bytes(self.buf[seq - start:])
            while not data and self.alive and time.time() < deadline:
                self.cond.wait(max(0.05, deadline - time.time()))
                start = self.total - len(self.buf)
                if seq < start:
                    data, reset = bytes(self.buf), True
                elif seq <= self.total:
                    data = bytes(self.buf[seq - start:])
            return {
                "seq": self.total, "start": self.total - len(self.buf),
                "data": base64.b64encode(data).decode() if data else "",
                "reset": reset, "alive": self.alive, "exit": self.exit,
                "cols": self.cols, "rows": self.rows,
            }

    def resize(self, cols, rows):
        self.cols = max(20, min(int(cols or 80), 500))
        self.rows = max(5, min(int(rows or 24), 300))
        if self._resize_fn and self.alive:
            try:
                self._resize_fn(self.cols, self.rows)
            except Exception:  # noqa: BLE001
                pass

    def signal(self, name):
        if not self.alive:
            return
        if name == "int":
            self.write(b"\x03")
        elif name == "quit":
            self.write(b"\x1c")
        elif name in ("hup", "term", "kill"):
            self.close()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._close_fn:
            try:
                self._close_fn()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            # AttributeError：容器 exec 的通道是包装对象，可能没实现 shutdown
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._finish()

    def info(self):
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "subtitle": self.subtitle, "created": int(self.created),
            "alive": self.alive, "exit": self.exit, "cols": self.cols,
            "rows": self.rows, "host_id": None, "bytes": self.total,
        }


def create_stream(title, subtitle, sock, resize_fn=None, close_fn=None, kind="exec"):
    sess = StreamSession(_new_sid(), title, subtitle, sock,
                         resize_fn=resize_fn, close_fn=close_fn, kind=kind)
    return _register(sess)


def create_local(shell="bash", cols=80, rows=24, cmd=None):
    shell = (shell or "bash").strip().lower()
    if shell not in ("bash", "sh"):
        raise ValueError("只支持 bash 或 sh")
    env = _base_env()
    if cmd:
        argv = [_which("sh", "/system/bin/sh"), "-c", cmd]
        title = cmd.split()[0][:20]
        subtitle = "自定义命令"
    else:
        name = shell
        path = _which(name, f"{core.PREFIX}/bin/{name}")
        if not os.path.exists(path):
            raise RuntimeError(f"未找到 {name}，请先 pkg install {name}")
        argv = [path, "-l"]
        title = name
        subtitle = path
        env["SHELL"] = path
    sess = Session(_new_sid(), "local", title, subtitle, argv, env,
                   core.HOME, cols, rows)
    return _register(sess)


def create_ssh(host, port, user, auth="password", password="", key_path="",
               cols=80, rows=24, name="", strict=False, host_id=None):
    host = (host or "").strip()
    user = (user or "").strip()
    if not HOST_RE.match(host):
        raise ValueError("主机地址不合法")
    if not USER_RE.match(user):
        raise ValueError("用户名不合法")
    try:
        port = int(port or 22)
    except ValueError:
        raise ValueError("端口必须是数字")
    if not (1 <= port <= 65535):
        raise ValueError("端口超出范围")

    sid = _new_sid()
    ssh = _which("ssh", f"{core.PREFIX}/bin/ssh")
    if not os.path.exists(ssh):
        raise RuntimeError("未安装 ssh，请先 pkg install openssh")

    ssh_dir = os.path.join(core.HOME, ".ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)

    argv = [ssh, "-tt", "-p", str(port),
            "-o", "StrictHostKeyChecking=" + ("yes" if strict else "accept-new"),
            "-o", "UserKnownHostsFile=" + os.path.join(ssh_dir, "known_hosts"),
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "LogLevel=ERROR"]

    env = _base_env()
    if auth == "key":
        kp = (key_path or "").strip()
        if not kp:
            raise ValueError("请选择或填写私钥")
        if not os.path.isabs(kp):
            kp = os.path.join(ssh_dir, kp)
        if not os.path.isfile(kp):
            raise ValueError("私钥文件不存在")
        argv += ["-i", kp, "-o", "IdentitiesOnly=yes",
                 "-o", "PreferredAuthentications=publickey"]
    else:
        if not password:
            raise ValueError("请输入密码")
        d = _askpass_setup(sid, password)
        env.update({
            "SSH_ASKPASS": os.path.join(d, "ask.sh"),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or ":0",
        })
        argv += ["-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=password,keyboard-interactive"]

    argv += ["-l", user, host]

    title = (name or "").strip() or f"{user}@{host}"
    sess = Session(sid, "ssh", title, f"{user}@{host}:{port}", argv, env,
                   core.HOME, cols, rows, host_id=host_id)
    return _register(sess)


def get(sid):
    with _lock:
        s = _sessions.get(sid)
    if not s:
        raise KeyError("会话不存在或已关闭")
    return s


def close(sid):
    with _lock:
        s = _sessions.pop(sid, None)
    if s:
        s.close()
        return True
    return False


def close_all():
    with _lock:
        items = list(_sessions.values())
        _sessions.clear()
    for s in items:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def listing():
    with _lock:
        items = list(_sessions.values())
    items.sort(key=lambda s: s.created)
    return [s.info() for s in items]


# ---------------- askpass ----------------

def _askpass_setup(sid, password):
    """把密码写进 600 临时文件，脚本只输出一次。

    密码不进 argv（`ps` 看得到）、不进环境变量（/proc/pid/environ 看得到），
    只在磁盘上存在一个 700 目录里的 600 文件，会话结束即删。
    """
    d = os.path.join(ASKPASS_DIR, sid)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    pw = os.path.join(d, "pass")
    fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    sh = os.path.join(d, "ask.sh")
    body = (
        f'#!{_which("sh", "/system/bin/sh")}\n'
        f'D="{d}"\n'
        'n=$(cat "$D/n" 2>/dev/null || echo 0)\n'
        'n=$((n + 1))\n'
        'echo "$n" > "$D/n"\n'
        'if [ "$n" -eq 1 ]; then cat "$D/pass"; fi\n'
        'exit 0\n'
    )
    fd = os.open(sh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return d


def _cleanup_askpass(sid):
    d = os.path.join(ASKPASS_DIR, sid)
    shutil.rmtree(d, ignore_errors=True)


def clean_askpass():
    """清理上次进程崩溃残留的密码目录。"""
    if not os.path.isdir(ASKPASS_DIR):
        return
    for name in os.listdir(ASKPASS_DIR):
        p = os.path.join(ASKPASS_DIR, name)
        if os.path.isdir(p) and time.time() - os.path.getmtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)


# ---------------- SSH 主机簿 ----------------

def hosts():
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _save_hosts(items):
    os.makedirs(core.DATA, mode=0o700, exist_ok=True)
    tmp = HOSTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HOSTS_FILE)
    try:
        os.chmod(HOSTS_FILE, 0o600)
    except OSError:
        pass


def save_host(body):
    host = (body.get("host") or "").strip()
    user = (body.get("user") or "").strip()
    if not HOST_RE.match(host):
        raise ValueError("主机地址不合法")
    if not USER_RE.match(user):
        raise ValueError("用户名不合法")
    port = int(body.get("port") or 22)
    if not (1 <= port <= 65535):
        raise ValueError("端口超出范围")
    item = {
        "id": body.get("id") or secrets.token_hex(5),
        "name": (body.get("name") or "").strip()[:40] or f"{user}@{host}",
        "host": host, "port": port, "user": user,
        "auth": "key" if body.get("auth") == "key" else "password",
        "key": (body.get("key") or "").strip()[:200],
        "strict": bool(body.get("strict")),
        "created": int(time.time()),
    }
    items = hosts()
    for i, it in enumerate(items):
        same = (it.get("host") == item["host"] and it.get("port") == item["port"]
                and it.get("user") == item["user"])
        if it.get("id") == item["id"] or same:
            # 同一台机器重复保存就地更新，否则勾几次"保存到主机簿"就堆一串重复项
            item["id"] = it.get("id")
            item["created"] = it.get("created") or item["created"]
            items[i] = item
            break
    else:
        items.append(item)
    _save_hosts(items)
    return item


def delete_host(hid):
    items = [it for it in hosts() if it.get("id") != hid]
    _save_hosts(items)
    return True


# ---------------- 密钥 ----------------

def list_keys():
    d = os.path.join(core.HOME, ".ssh")
    out = []
    if not os.path.isdir(d):
        return out
    skip = {"known_hosts", "known_hosts.old", "config", "authorized_keys", "environment"}
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name in skip or name.endswith(".pub") or not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) > 65536:
                continue
            with open(p, "rb") as f:
                head = f.read(64)
        except OSError:
            continue
        if b"PRIVATE KEY" in head:
            out.append({"name": name, "path": p})
    return out


def import_key(name, content):
    name = re.sub(r"[^A-Za-z0-9._\-]", "", (name or "imported").strip())[:40] or "imported"
    if "PRIVATE KEY" not in (content or ""):
        raise ValueError("不是有效的私钥内容（缺少 PRIVATE KEY 头）")
    d = os.path.join(core.HOME, ".ssh")
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content if content.endswith("\n") else content + "\n")
    return {"name": name, "path": p}


def ensure_reaper():
    threading.Thread(target=_reaper, daemon=True, name="term-reaper").start()
    clean_askpass()
