"""udockerd：基于 udocker 的 Docker Engine API 兼容守护进程（最小子集）。

为什么需要它
------------
面板要「走标准 Docker Engine API」，而 udocker 只有命令行。把 CLI 调用散在
面板各处，等于既没有 API 语义、也没法被别的客户端复用；所以这里做一层
**单进程、零依赖** 的 Engine API 子集，面板（以及 docker-py / curl）都通过
`tcp://127.0.0.1:2375` 访问。

能力边界（与设计文档一致，API 里也显式返回）
--------------------------------------------
* 网络恒为 host：不建 bridge、不做 NAT，端口映射只记录不生效；
* 无 cgroups：不做 CPU / 内存限制，`/containers/{id}/stats` 返回 501；
* 无 Volume 对象：`/volumes` 返回空 + 警告，持久化只靠 bind mount；
* 无构建：udocker 1.3.x 没有 build 子命令，`/build` 返回 501；
* `exec` 用 `udocker run <容器> <命令>` 实现（udocker 无 exec），是新进程
  进入同一 rootfs，不共享 PID 命名空间——host 网络下语义差异可接受。

状态
----
`<data>/state.json` 是容器元数据的唯一真相源（0600）。udocker 自己只保存
rootfs 与镜像仓库；容器名、环境变量、挂载、重启策略都在这里。

进程
----
容器进程 `start_new_session=True` 起在独立进程组，日志写 `<data>/logs/<id>.log`。
守护进程被 SIGTERM 时先停容器再退出；如果是被 kill -9，下次启动会按 pid 文件
与 /proc 命令行「接管」仍在跑的容器，而不是重复拉起。
"""
import argparse
import calendar
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
import fcntl
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

VERSION = "0.1.0"
API_VERSION = "1.41"
MIN_API = "1.24"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
ISO = "%Y-%m-%dT%H:%M:%SZ"

# 这些路径不允许被 bind mount 覆盖：容器启动依赖它们
FORBIDDEN_CONTAINER_PATHS = ("/", "/proc", "/sys", "/dev", "/etc/passwd",
                             "/etc/group", "/etc/hosts", "/etc/resolv.conf")


def _now_iso(ts=None):
    return time.strftime(ISO, time.gmtime(ts or time.time()))


def _parse_iso(s):
    try:
        return int(calendar.timegm(time.strptime(s, ISO)))
    except (ValueError, TypeError):
        return 0


def _log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    sys.stderr.flush()


def _termux_prefix():
    """Termux 的 $PREFIX。

    从 sys.executable 反推（Termux 上是 $PREFIX/bin/python3.x）比读环境变量
    可靠：runit 的服务环境里连 PREFIX 都没有，只有 HOME 和 PATH。
    """
    return os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))


def _udocker_argv0(binpath):
    """拼出「启动 udocker」的命令行前缀。

    Termux 里的 udocker 是个 `#!/usr/bin/env python` 的脚本，而 Android 上
    压根没有 /usr/bin —— 平常能跑，全靠 termux-exec 的 LD_PRELOAD 在 execve
    那一刻把路径改写掉。环境里少了这个变量（runit 起的服务就是这样），内核
    找不到解释器就返回 ENOENT，报出来的却是「No such file or directory:
    …/udocker」，看着像文件被删了，其实文件好好的。

    与其指望每个启动入口都记得 export，不如绕开 shebang：认出是 Python
    脚本就直接拿当前解释器去跑它。这样不依赖 LD_PRELOAD，也不会因为
    哪个服务少配一个变量就整体瘫痪。
    """
    try:
        real = os.path.realpath(binpath)
        if real.endswith(".py"):
            return [sys.executable, real]
    except OSError:
        pass
    return [binpath]


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 状态 ----------------

class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.RLock()
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(self.dir, "logs"), mode=0o700, exist_ok=True)
        self.data = {"containers": {}, "seq": 0}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("containers"), dict):
                self.data = d
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def containers(self):
        with self.lock:
            return [dict(c) for c in self.data["containers"].values()]

    def get(self, ref):
        """按完整 id / 短 id / 名称取容器。短 id 至少 4 位，避免歧义。"""
        ref = str(ref or "")
        with self.lock:
            items = self.data["containers"]
            if ref in items:
                return items[ref]
            if not ref:
                raise ApiError(404, "容器不存在")
            cands = [c for c in items.values()
                     if c["name"] == ref or (len(ref) >= 4 and c["id"].startswith(ref))]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                raise ApiError(409, f"容器标识不唯一：{ref}")
        raise ApiError(404, f"容器不存在：{ref}")

    def put(self, c):
        with self.lock:
            self.data["containers"][c["id"]] = c
            self.save()

    def drop(self, cid):
        with self.lock:
            self.data["containers"].pop(cid, None)
            self.save()


# ---------------- 事件 ----------------

class Events:
    def __init__(self):
        self.cond = threading.Condition()
        self.items = []
        self.max = 300

    def emit(self, action, c, extra=None):
        attrs = {"name": c.get("name", ""), "image": c.get("image", "")}
        attrs.update(extra or {})
        ev = {
            "Type": "container", "Action": action, "status": action,
            "id": c.get("id", ""),
            "from": c.get("image", ""),
            "Actor": {"ID": c.get("id", ""), "Attributes": attrs},
            "scope": "local", "time": int(time.time()),
            "timeNano": int(time.time() * 1e9),
        }
        with self.cond:
            self.items.append(ev)
            if len(self.items) > self.max:
                self.items = self.items[-self.max:]
            self.cond.notify_all()
        return ev

    def since(self, ts):
        with self.cond:
            return [e for e in self.items if e["time"] >= ts]


# ---------------- udocker 后端 ----------------

class Udocker:
    def __init__(self, binpath, home=None, repo=None, config_path=""):
        self.bin = binpath
        self.home = home or os.path.expanduser("~")
        self.repo = repo or os.path.join(self.home, ".udocker")
        self.config_path = config_path
        self.lock = threading.Lock()
        self.argv0 = _udocker_argv0(binpath)

    def env(self):
        env = {**os.environ, "HOME": self.home, "TERM": "dumb"}
        # 只有外部显式设了才透传：空值在部分 proot 版本里会被当成「开启」
        if "PROOT_NO_SECCOMP" not in os.environ:
            env.pop("PROOT_NO_SECCOMP", None)
        # Termux 的 /usr/bin 是个空壳，udocker 脚本第一行是
        # `#!/usr/bin/env python`，全靠 termux-exec 的 LD_PRELOAD 在
        # execve 时把它改写成 $PREFIX/bin/env。从 runit 起的服务环境里
        # 没有这个变量，于是 execve 直接 ENOENT —— 文件明明躺在那里，
        # subprocess 却报「No such file or directory」。这里兜底补上，
        # 就不用指望服务脚本记得 export。
        if not env.get("LD_PRELOAD"):
            lib = os.path.join(_termux_prefix(), "lib",
                               "libtermux-exec-ld-preload.so")
            if os.path.exists(lib):
                env["LD_PRELOAD"] = lib
        return env

    def call(self, args, timeout=180, input_data=None):
        try:
            p = subprocess.run(self.argv0 + list(args), capture_output=True,
                               timeout=timeout, env=self.env(), input=input_data)
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            return p.returncode, out
        except subprocess.TimeoutExpired:
            return 124, f"udocker 超时（{timeout}s）：{' '.join(args[:2])}"
        except OSError as e:
            return 127, f"无法执行 udocker：{e}"

    def version(self):
        code, out = self.call(["--version"], timeout=30)
        if code != 0:
            return ""
        for line in out.splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip()
        return out.splitlines()[0].strip() if out.strip() else ""

    # -- 镜像 --

    def images(self):
        out = []
        root = os.path.join(self.repo, "repos")
        if not os.path.isdir(root):
            return out
        for cur, _dirs, files in os.walk(root):
            if "manifest" not in files:
                continue
            rel = os.path.relpath(cur, root).replace(os.sep, "/")
            if "/" not in rel:
                continue
            repo, tag = rel.rsplit("/", 1)
            try:
                with open(os.path.join(cur, "manifest"), "r", encoding="utf-8") as fh:
                    man = json.load(fh)
            except (OSError, ValueError):
                man = {}
            cfg_digest = (man.get("config") or {}).get("digest", "")
            cfg = {}
            created = 0
            if cfg_digest:
                try:
                    with open(os.path.join(cur, cfg_digest), "r", encoding="utf-8") as fh:
                        cfg = json.load(fh)
                    created = _parse_iso(str(cfg.get("created", ""))[:19] + "Z")
                except (OSError, ValueError):
                    cfg = {}
            size = 0
            for f in files:
                if f.startswith("sha256:"):
                    try:
                        size += os.path.getsize(os.path.join(cur, f))
                    except OSError:
                        pass
            image_id = cfg_digest.replace("sha256:", "")[:64] or rel.replace("/", "_")
            out.append({
                "Id": "sha256:" + image_id if len(image_id) == 64 else image_id,
                "RepoTags": [f"{repo}:{tag}"],
                "Created": created or int(os.path.getmtime(os.path.join(cur, "manifest"))),
                "Size": size,
                "Architecture": cfg.get("architecture", ""),
                "Os": cfg.get("os", "linux"),
                "Config": cfg.get("config", {}),
                "_repo": repo, "_tag": tag,
            })
        out.sort(key=lambda x: x["Created"], reverse=True)
        return out

    def image(self, ref):
        ref = str(ref or "").strip()
        if not ref:
            raise ApiError(404, "镜像不存在")
        cands = []
        for im in self.images():
            for t in im["RepoTags"]:
                if t == ref or t.split(":")[0] == ref:
                    cands.append((im, t))
        if not cands:
            raise ApiError(404, f"镜像不存在：{ref}")
        return cands[0][0]

    def pull(self, ref, platform, on_line):
        args = ["pull"]
        if platform:
            args.append(f"--platform={platform}")
        cfg = _read_config(self.config_path or _config_path)
        reg = (cfg.get("registry") or "").strip()
        if reg:
            args.append(f"--registry={reg}")
        args.append(ref)
        try:
            p = subprocess.Popen(self.argv0 + args, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=self.env(),
                                 stdin=subprocess.DEVNULL)
        except OSError as e:
            raise ApiError(500, f"无法执行 udocker：{e}")
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                on_line(line)
        return p.wait()

    def create(self, name, image):
        return self.call(["create", f"--name={name}", image], timeout=600)

    def remove(self, name):
        return self.call(["rm", name], timeout=120)

    def remove_image(self, ref):
        return self.call(["rmi", ref], timeout=120)

    def load(self, path):
        return self.call(["load", "-i", path], timeout=900)

    def run_argv(self, c, cmd=None):
        argv = self.argv0 + ["run", "--nobanner", "--pull=never"]
        for b in c.get("binds") or []:
            argv.append(f"--volume={b['host']}:{b['container']}")
        # 必须带上 --publish，否则镜像里 EXPOSE 80 这类特权的镜像会被 udocker
        # 直接拒绝启动："this container exposes privileged TCP/IP ports"。
        # udocker 不做真正的端口映射（容器本来就在 host 网络里），它只是拿
        # 这个映射判断「宿主端口 ≥1024 就放行」，所以写 host:container 就行。
        for p in c.get("ports") or []:
            if p.get("host") and p.get("container"):
                argv.append(f"--publish={p['host']}:{p['container']}")
        for k, v in (c.get("env") or {}).items():
            argv.append(f"--env={k}={v}")
        ep = c.get("entrypoint")
        if ep is not None:
            # 空列表 → --entrypoint=""（清空），否则取第一个路径
            argv.append(f"--entrypoint={ep[0] if ep else ''}")
        if c.get("workdir"):
            argv.append(f"--workdir={c['workdir']}")
        if c.get("user"):
            argv.append(f"--user={c['user']}")
        argv.append(c["name"])
        argv.extend(cmd if cmd is not None else (c.get("cmd") or []))
        return argv


_config_path = ""

_ARCH_ALIAS = {"aarch64": "arm64", "armv8l": "arm64", "armv7l": "arm",
               "x86_64": "amd64", "i686": "386", "i386": "386"}


def _default_platform():
    """本机架构对应的镜像平台。

    udocker 自己探测会拿 `uname -m` 直接拼成 android/arm64（Termux 上），
    而镜像清单里只有 linux/arm64，于是任何镜像都报「no image found in
    manifest for platform」。这里显式推导，绕开它的探测。
    """
    m = (os.uname().machine or "").lower()
    return "linux/" + _ARCH_ALIAS.get(m, m or "arm64")


def _read_config(path):
    """读配置，并补上缺省项。

    配置文件在「用户没动过设置」时根本不存在，早先直接把空 dict 返回，
    调用方再去取 platform 就是空字符串 —— 在 Termux 上等于必挂。
    """
    cfg = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                cfg = d
        except (OSError, ValueError):
            cfg = {}
    if not (cfg.get("platform") or "").strip():
        cfg["platform"] = _default_platform()
    return cfg


# ---------------- 运行时 ----------------

class Runtime:
    def __init__(self, data_dir, udocker, config_path=""):
        global _config_path
        _config_path = config_path
        self.config_path = config_path
        self.store = Store(data_dir)
        self.events = Events()
        self.ud = udocker
        self.logdir = os.path.join(data_dir, "logs")
        self.lock = self.store.lock
        self.execs = {}          # exec_id -> {"container", "cmd", "tty", "fd", "started"}
        self.procs = {}          # container_id -> Popen（用于取真实退出码）
        self._stop = threading.Event()
        self._reconciled = False

    # -- 生命周期 --

    def ensure_monitor(self):
        threading.Thread(target=self._monitor, daemon=True,
                         name="udockerd-monitor").start()

    def reconcile(self):
        """启动时对账：接管还活着的容器，修正已死状态，处理重启策略。"""
        if self._reconciled:
            return
        self._reconciled = True
        for c in self.store.containers():
            if c.get("status") in ("running", "stopping"):
                pid = int(c.get("pid") or 0)
                if pid and _pid_alive(pid) and c["name"] in _cmdline_of(pid):
                    c["status"] = "running"
                    _log(f"接管容器 {c['name']} pid={pid}")
                else:
                    c["status"] = "exited"
                    c["pid"] = 0
                    if not c.get("finished_at"):
                        c["finished_at"] = int(time.time())
                    c["exit_code"] = c.get("exit_code") or 137
                self.store.put(c)
            if c.get("status") == "exited" and c.get("restart") == "always":
                # 单个容器起不来不能把整个守护进程带走。udocker 路径失效那次，
                # 这里抛异常 → 进程退出 → runit 立刻拉起 → 再抛，形成死循环，
                # 「容器」页面整个不可用，还看不出根因。改成记一笔、标记状态，
                # 继续对账下一个。
                try:
                    self.start(c["id"], from_reconcile=True)
                except Exception as e:  # noqa: BLE001
                    _log(f"对账时启动 {c.get('name')} 失败：{e}")
                    try:
                        cur = self.store.get(c["id"])
                        cur["status"] = "exited"
                        cur["last_error"] = str(e)
                        self.store.put(cur)
                    except Exception:  # noqa: BLE001
                        pass

    def shutdown(self):
        self._stop.set()
        for c in self.store.containers():
            if c.get("status") in ("running", "stopping"):
                self._stop_container(c, timeout=8, manual=True)

    # -- 查询 --

    def list_containers(self, all_=True):
        out = []
        for c in self.store.containers():
            if not all_ and c.get("status") != "running":
                continue
            out.append(self._summary(c))
        out.sort(key=lambda x: x.get("Created", 0), reverse=True)
        return out

    def _summary(self, c):
        running = c.get("status") == "running" and _pid_alive(int(c.get("pid") or 0))
        status = ("Up " + _human_since(c.get("started_at"))) if running else (
            f"Exited ({c.get('exit_code', 0)})" if c.get("status") == "exited" else "Created")
        return {
            "Id": c["id"], "Names": ["/" + c["name"]], "Image": c["image"],
            "ImageID": c.get("image_id", ""), "Command": " ".join(c.get("cmd") or []),
            "Created": int(c.get("created") or 0), "State": "running" if running else "exited",
            "Status": status, "Ports": [], "Labels": c.get("labels") or {},
            "Mounts": [{"Type": "bind", "Source": b["host"], "Destination": b["container"],
                        "Mode": "ro" if b.get("ro") else "rw", "RW": not b.get("ro")}
                       for b in c.get("binds") or []],
            "NetworkSettings": {"Networks": {"host": {"NetworkID": "host",
                                                      "Driver": "host"}}},
        }

    def inspect(self, c):
        running = c.get("status") == "running" and _pid_alive(int(c.get("pid") or 0))
        status = "running" if running else (
            "created" if c.get("status") == "created" else "exited")
        return {
            "Id": c["id"], "Name": "/" + c["name"], "Created": _now_iso(c.get("created")),
            "Path": (c.get("cmd") or [""])[0],
            "Args": (c.get("cmd") or [])[1:],
            "State": {
                "Status": status,
                "Running": running, "Paused": False, "Restarting": False,
                "Pid": int(c.get("pid") or 0) if running else 0,
                "ExitCode": int(c.get("exit_code") or 0),
                "StartedAt": _now_iso(c.get("started_at")) if c.get("started_at") else "",
                "FinishedAt": _now_iso(c.get("finished_at")) if c.get("finished_at") else "",
                "OOMKilled": False,
            },
            "Image": c.get("image_id") or c["image"],
            "Config": {
                "Image": c["image"], "Cmd": c.get("cmd") or [],
                "Env": [f"{k}={v}" for k, v in (c.get("env") or {}).items()],
                "WorkingDir": c.get("workdir") or "", "User": c.get("user") or "",
                "Tty": bool(c.get("tty")), "Labels": c.get("labels") or {},
                "ExposedPorts": {f"{p['container']}/{p.get('proto', 'tcp')}": {}
                                 for p in c.get("ports") or []},
            },
            "HostConfig": {
                "Binds": [f"{b['host']}:{b['container']}" for b in c.get("binds") or []],
                "NetworkMode": "host",
                "PortBindings": {},
                "RestartPolicy": {"Name": c.get("restart", "no"), "MaximumRetryCount": 0},
                "AutoRemove": False,
            },
            "Mounts": [{"Type": "bind", "Source": b["host"], "Destination": b["container"],
                        "Mode": "ro" if b.get("ro") else "rw", "RW": not b.get("ro"),
                        "Propagation": "rprivate"}
                       for b in c.get("binds") or []],
            "NetworkSettings": {
                "IPAddress": _host_ip(), "Gateway": "", "MacAddress": "",
                "Ports": {}, "Networks": {"host": {"NetworkID": "host", "Driver": "host",
                                                   "IPAddress": _host_ip()}},
            },
            "RestartCount": int(c.get("restarts") or 0),
            "Driver": "udocker",
            "Platform": "linux",
            # 非 Docker 标准字段，面板用它展示边界，不影响其他客户端
            "PocketPanel": {
                "ExecMode": c.get("execmode", "P1"),
                "Ports": c.get("ports") or [],
                "NetworkMode": "host",
                "ResourceLimits": False,
                "VolumeObjects": False,
                "BindMountROEnforced": False,
                # 面板据此区分「用户自己停的」和「自己崩的」，后者才报警
                "ManualStop": bool(c.get("manual_stop")),
            },
        }

    # -- 创建 / 启动 / 停止 --

    def create(self, body, name):
        image = (body.get("Image") or "").strip()
        if not image:
            raise ApiError(400, "缺少镜像名")
        name = (name or body.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise ApiError(400, "容器名只能包含字母、数字、_ . -，且不能以符号开头（1–64 位）")
        if os.path.lexists(os.path.join(self.ud.repo, "containers", name)):
            raise ApiError(409, f"udocker 中已存在同名容器：{name}")
        try:
            im = self.ud.image(image)
        except ApiError:
            raise ApiError(404, f"镜像不存在：{image}；请先拉取")
        hc = body.get("HostConfig") or {}
        binds, ports = _parse_binds(hc.get("Binds") or []), _parse_ports(body)
        cfg = im.get("Config") or {}
        cmd = _as_list(body.get("Cmd")) or _as_list(cfg.get("Cmd"))
        env = {}
        for kv in (cfg.get("Env") or []):
            k, _, v = str(kv).partition("=")
            if k:
                env[k] = v
        for kv in (body.get("Env") or []):
            k, _, v = str(kv).partition("=")
            if k:
                env[k] = v
        c = {
            "id": uuid.uuid4().hex[:12], "name": name, "image": image,
            "image_id": im.get("Id", ""), "cmd": cmd, "env": env,
            # Entrypoint 显式给了才覆盖：空列表表示清空镜像自带的入口，
            # None/缺省表示照镜像原样。有些镜像的入口是 tini，而 tini 需要的
            # 那个 /bin/sh 在镜像里根本没有，只能清掉入口直接跑二进制。
            "entrypoint": (_as_list(body.get("Entrypoint"))
                           if "Entrypoint" in body else None),
            "binds": binds, "ports": ports,
            "workdir": body.get("WorkingDir") or cfg.get("WorkingDir") or "",
            "user": body.get("User") or "", "tty": bool(body.get("Tty")),
            "labels": body.get("Labels") or {},
            "restart": _restart_name(hc.get("RestartPolicy")),
            "execmode": (body.get("PocketPanel") or {}).get("ExecMode")
                        or _read_config(_config_path).get("execmode") or "P1",
            "created": int(time.time()), "status": "created", "pid": 0,
            "started_at": 0, "finished_at": 0, "exit_code": 0,
            "restarts": 0, "manual_stop": False,
        }
        code, out = self.ud.create(name, image)
        if code != 0:
            # Android 的 /data 分区不允许 app 建硬链接，镜像里带硬链接的层
            # （zoneinfo 那堆时区别名）会让 tar 以 2 退出，udocker 据此判失败。
            # 实际上其它文件都解出来了，容器目录也建好了 —— 以目录为准。
            root = os.path.join(self.ud.repo, "containers", name)
            if not os.path.isdir(root):
                raise ApiError(500, f"udocker create 失败：{_last_lines(out, 3)}")
            _log(f"create 有告警但容器已生成 {name}：{_last_lines(out, 2)}")
        if c["execmode"] and c["execmode"] != "P1":
            scode, sout = self.ud.call(["setup", f"--execmode={c['execmode']}", name],
                                       timeout=120)
            if scode != 0:
                _log(f"设置执行模式失败 {name}: {_last_lines(sout, 2)}")
        self.store.put(c)
        self.events.emit("create", c)
        return {"Id": c["id"], "Warnings": []}

    def start(self, cid, from_reconcile=False):
        c = self.store.get(cid)
        with self.lock:
            if c.get("status") == "running" and _pid_alive(int(c.get("pid") or 0)):
                return
            argv = self.ud.run_argv(c)
            logpath = os.path.join(self.logdir, c["id"] + ".log")
            try:
                lf = open(logpath, "ab")
            except OSError as e:
                raise ApiError(500, f"无法打开日志：{e}")
            lf.write((f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 启动："
                      f"{' '.join(argv[1:])}\n").encode())
            lf.flush()
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf,
                                        stderr=subprocess.STDOUT, env=self.ud.env(),
                                        start_new_session=True)
            except OSError as e:
                lf.close()
                raise ApiError(500, f"启动失败：{e}")
            c.update({"status": "running", "pid": proc.pid, "started_at": int(time.time()),
                      "finished_at": 0, "exit_code": 0, "manual_stop": False})
            self.procs[c["id"]] = proc
            self.store.put(c)
            lf.close()
        self.events.emit("start" if not from_reconcile else "restart", c)

    def _stop_container(self, c, timeout=10, manual=True, sig=signal.SIGTERM):
        pid = int(c.get("pid") or 0)
        if c.get("status") != "running" or not pid:
            c["status"] = "exited" if c.get("status") != "created" else "created"
            self.store.put(c)
            return
        if manual:
            c["manual_stop"] = True
            c["status"] = "stopping"
            self.store.put(c)
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + max(1, int(timeout or 10))
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.15)
        killed = False
        if _pid_alive(pid):
            killed = True
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
        code = 137 if killed else (128 + int(sig))
        proc = self.procs.pop(c["id"], None)
        if proc is not None:
            try:
                proc.wait(timeout=3)
                if proc.returncode is not None:
                    code = _exit_code(proc.returncode)
            except subprocess.TimeoutExpired:
                pass
        c.update({"pid": 0, "status": "exited", "finished_at": int(time.time()),
                  "exit_code": code})
        self.store.put(c)
        self.events.emit("kill", c, {"signal": str(int(sig))})

    def stop(self, cid, timeout=10):
        c = self.store.get(cid)
        self._stop_container(c, timeout=timeout, manual=True)

    def restart(self, cid, timeout=10):
        c = self.store.get(cid)
        self._stop_container(c, timeout=timeout, manual=True)
        self.start(cid)

    def remove(self, cid, force=False):
        c = self.store.get(cid)
        if c.get("status") == "running":
            if not force:
                raise ApiError(409, "容器正在运行，请先停止或使用 force")
            self._stop_container(c, timeout=8, manual=True)
        code, out = self.ud.remove(c["name"])
        if code != 0:
            tail = _last_lines(out, 3)
            # 容器在 udocker 那边已经不在了：命令行手删过、或者上次删到一半
            # 只删了目录没删登记表。这时报错没有意义 —— 用户要的结果（容器
            # 不存在）已经达成，而登记表里的这条记录会让面板上永远挂着一个
            # 点「删除」就报 500、删也删不掉的幽灵容器。清掉记录直接返回成功。
            if not re.search(r"invalid container id|no such container|not found",
                             tail, re.I):
                raise ApiError(500, f"udocker rm 失败：{tail}")
        try:
            os.unlink(os.path.join(self.logdir, c["id"] + ".log"))
        except OSError:
            pass
        self.store.drop(c["id"])
        self.events.emit("destroy", c)
        return True

    def logs(self, cid, tail=200, timestamps=False):
        c = self.store.get(cid)
        path = os.path.join(self.logdir, c["id"] + ".log")
        try:
            size = os.path.getsize(path)
        except OSError:
            return ""
        start = max(0, size - 512 * 1024)
        with open(path, "rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", "replace")
        rows = text.rstrip("\n").split("\n")
        rows = rows[-max(1, min(int(tail or 200), 5000)):]
        return "\n".join(rows) + ("\n" if rows and rows != [""] else "")

    # -- exec --

    def exec_create(self, cid, body):
        c = self.store.get(cid)
        if c.get("status") != "running":
            raise ApiError(409, "容器未运行，无法 exec")
        cmd = _as_list(body.get("Cmd"))
        if not cmd:
            raise ApiError(400, "缺少命令")
        eid = uuid.uuid4().hex
        self.execs[eid] = {"container": c["id"], "cmd": cmd,
                           "tty": bool(body.get("Tty")), "fd": -1,
                           "started": 0, "env": body.get("Env") or [],
                           "workdir": body.get("WorkingDir") or ""}
        return {"Id": eid}

    def exec_start(self, eid, body, hijack, handler):
        e = self.execs.get(eid)
        if not e:
            raise ApiError(404, "exec 不存在")
        c = self.store.get(e["container"])
        if c.get("status") != "running":
            raise ApiError(409, "容器未运行")
        extra_env = dict(c.get("env") or {})
        for kv in e.get("env") or []:
            k, _, v = str(kv).partition("=")
            if k:
                extra_env[k] = v
        cc = dict(c)
        cc["env"] = extra_env
        if e.get("workdir"):
            cc["workdir"] = e["workdir"]
        argv = self.ud.run_argv(cc, cmd=e["cmd"])
        if e["tty"]:
            self._exec_pty(eid, e, argv, hijack, handler)
            return True
        return self._exec_pipe(eid, e, argv, hijack, handler)

    def _exec_pty(self, eid, e, argv, hijack, handler):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execve(argv[0], argv, self.ud.env())
            except BaseException:
                pass
            os._exit(127)
        e["pid"], e["fd"], e["started"] = pid, fd, time.time()
        _set_winsize(fd, 24, 80)
        if not hijack:
            out = b""
            deadline = time.time() + 300
            while time.time() < deadline:
                r, _, _ = select.select([fd], [], [], 0.2)
                if fd in r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                elif not _pid_alive(pid):
                    break
                if len(out) > 1 << 20:
                    break
            os.close(fd)
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            self.execs.pop(eid, None)
            handler.send_chunked(out)
            return
        handler.start_hijack()
        sock = handler.connection
        self._pump_pty(sock, fd, pid, eid)

    def _pump_pty(self, sock, fd, pid, eid):
        try:
            while True:
                r, _, _ = select.select([sock, fd], [], [], 0.5)
                if sock in r:
                    try:
                        data = sock.recv(65536)
                    except InterruptedError:
                        data = None
                    except OSError:
                        break
                    if data == b"":
                        break
                    if data:
                        try:
                            os.write(fd, data)
                        except OSError:
                            break
                if fd in r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    try:
                        sock.sendall(chunk)
                    except OSError:
                        break
                elif not _pid_alive(pid):
                    # 子进程已退出：把 PTY 里剩余的输出读完再收尾
                    try:
                        while True:
                            r2, _, _ = select.select([fd], [], [], 0.1)
                            if fd not in r2:
                                break
                            chunk = os.read(fd, 65536)
                            if not chunk:
                                break
                            sock.sendall(chunk)
                    except OSError:
                        pass
                    break
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            self.execs.pop(eid, None)
            try:
                sock.close()
            except OSError:
                pass

    def _exec_pipe(self, eid, e, argv, hijack, handler):
        """非 TTY：stdout 走管道。hijack 时把 socket 当 stdin/stdout 用。"""
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE if hijack else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=self.ud.env(), bufsize=0)
        except OSError as ex:
            raise ApiError(500, f"exec 启动失败：{ex}")
        e["pid"] = p.pid
        try:
            if hijack:
                handler.start_hijack()
                sock = handler.connection
                while True:
                    r, _, _ = select.select([sock, p.stdout], [], [], 0.5)
                    if sock in r:
                        data = sock.recv(65536)
                        if not data:
                            break
                        try:
                            p.stdin.write(data)
                            p.stdin.flush()
                        except (OSError, ValueError):
                            break
                    if p.stdout in r:
                        chunk = os.read(p.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        sock.sendall(chunk)
                    elif p.poll() is not None:
                        break
                    if not r:
                        continue
                try:
                    rest = p.stdout.read() or b""
                    if rest:
                        sock.sendall(rest)
                except OSError:
                    pass
            else:
                out, _ = p.communicate(timeout=300)
                handler.send_chunked(out or b"")
        except subprocess.TimeoutExpired:
            p.kill()
            raise ApiError(408, "exec 超时（300s）")
        finally:
            self.execs.pop(eid, None)

    def exec_resize(self, eid, h, w):
        e = self.execs.get(eid)
        if not e or e.get("fd", -1) < 0:
            return
        _set_winsize(e["fd"], h, w)

    # -- 监控 --

    def _monitor(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(2)

    def _tick(self):
        for snap in self.store.containers():
            if snap.get("status") != "running":
                continue
            pid = int(snap.get("pid") or 0)
            if pid and _pid_alive(pid):
                continue
            with self.lock:
                c = self.store.data["containers"].get(snap["id"])
                # 用户可能刚好在这两秒内 stop 了，别把状态覆盖回去
                if not c or c.get("status") != "running" or int(c.get("pid") or 0) != pid:
                    continue
                proc = self.procs.pop(c["id"], None)
                code = 137
                if proc is not None:
                    try:
                        proc.wait(timeout=2)
                        if proc.returncode is not None:
                            code = _exit_code(proc.returncode)
                    except subprocess.TimeoutExpired:
                        pass
                c.update({"status": "exited", "pid": 0, "exit_code": code,
                          "finished_at": int(time.time())})
                manual = bool(c.get("manual_stop"))
                policy = c.get("restart", "no")
                should = (not manual) and (policy == "always" or
                                           (policy == "on-failure" and code != 0))
                if should:
                    c["restarts"] = int(c.get("restarts") or 0) + 1
                    delay = min(60, 2 ** min(c["restarts"] - 1, 5))
                    c["next_start"] = int(time.time()) + delay
                    self.store.put(c)
                    self.events.emit("die", c, {"exitCode": str(code),
                                                "restarting": "true"})
                else:
                    self.store.put(c)
                    self.events.emit("die", c, {"exitCode": str(code),
                                                "manual": "true" if manual else "false"})
        now = int(time.time())
        for snap in self.store.containers():
            if snap.get("status") == "exited" and snap.get("next_start") and \
                    snap["next_start"] <= now:
                with self.lock:
                    c = self.store.data["containers"].get(snap["id"]) or {}
                    c.pop("next_start", None)
                    if c:
                        self.store.put(c)
                try:
                    self.start(snap["id"])
                    self.events.emit("restart", c,
                                     {"restartCount": str(c.get("restarts", 0))})
                except ApiError as e:
                    _log(f"自动重启失败 {snap['name']}: {e.message}")


# ---------------- 工具 ----------------

def _exit_code(code):
    """Popen 对信号退出返回负数；Docker 用 128+signal。"""
    return 128 - code if code is not None and code < 0 else int(code or 0)


def _pid_alive(pid):
    """僵尸进程不算活着——否则 wait 永远不会被调用，容器会「永远运行中」。"""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
        pos = raw.rfind(b")")
        if pos > 0 and raw[pos + 2:pos + 3] == b"Z":
            return False
        return True
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline_of(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\x00", " ")
    except OSError:
        return ""


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _restart_name(pol):
    if not isinstance(pol, dict):
        return "no"
    name = str(pol.get("Name") or "no").lower()
    return name if name in ("no", "always", "on-failure", "unless-stopped") else "no"


def _parse_binds(binds):
    out = []
    for raw in binds or []:
        s = str(raw)
        parts = s.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ApiError(400, f"挂载格式应为 宿主机路径:容器路径[:ro]：{s}")
        host, cont = parts[0], parts[1]
        ro = len(parts) > 2 and parts[2].startswith("ro")
        if not os.path.isabs(host) or not os.path.isabs(cont):
            raise ApiError(400, f"挂载路径必须是绝对路径：{s}")
        if os.path.normpath(cont) in FORBIDDEN_CONTAINER_PATHS:
            raise ApiError(400, f"不允许覆盖容器关键路径：{cont}")
        if not os.path.exists(host):
            raise ApiError(400, f"宿主机路径不存在：{host}")
        out.append({"host": os.path.normpath(host),
                    "container": os.path.normpath(cont), "ro": ro})
    return out


def _parse_ports(body):
    pp = body.get("PocketPanel") if isinstance(body.get("PocketPanel"), dict) else {}
    ports = pp.get("Ports")
    if ports is None:
        ports = body.get("Ports")
    out = []
    for p in ports or []:
        try:
            host = int(p.get("host") or p.get("HostPort") or 0)
            cont = int(p.get("container") or p.get("ContainerPort") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "端口必须是数字")
        if not (1 <= host <= 65535) or not (1 <= cont <= 65535):
            raise ApiError(400, "端口超出 1–65535")
        if host < 1024:
            raise ApiError(400, "PRoot 无 root，host 网络下无法监听 1024 以下端口")
        out.append({"host": host, "container": cont, "proto": p.get("proto") or "tcp"})
    # 兼容标准客户端：HostConfig.PortBindings = {"80/tcp": [{"HostPort": "8080"}]}
    hc = body.get("HostConfig") if isinstance(body.get("HostConfig"), dict) else {}
    for cont_key, binds in (hc.get("PortBindings") or {}).items():
        cport, _, proto = str(cont_key).partition("/")
        for b in binds or []:
            host = str((b or {}).get("HostPort") or "")
            if not host.isdigit():
                continue
            item = {"host": int(host), "container": int(cport or 0),
                    "proto": proto or "tcp"}
            if item["host"] < 1024:
                raise ApiError(400, "PRoot 无 root，host 网络下无法监听 1024 以下端口")
            if item not in out:
                out.append(item)
    return out


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(5, int(rows or 24)),
                                max(20, int(cols or 80)), 0, 0))
    except OSError:
        pass


def _last_lines(text, n=3):
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return " / ".join(rows[-n:])[:400] or "未知错误"


def _human_since(ts):
    if not ts:
        return "0s"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def _host_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------------- HTTP ----------------

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "udockerd/" + VERSION
    runtime = None

    def log_message(self, fmt, *args):
        if os.environ.get("UDOCKERD_VERBOSE"):
            _log(fmt % args)

    # -- 响应 --

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, code=200):
        body = text.encode() if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_chunked(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.docker.raw-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if data:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def chunk_open(self, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def chunk_write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def chunk_close(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def start_hijack(self):
        self.send_response(101, "UPGRADED")
        self.send_header("Content-Type", "application/vnd.docker.raw-stream")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "tcp")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    # -- 入口 --

    def _dispatch(self):
        path = self.path.split("?", 1)[0]
        path = re.sub(r"^/v\d+(?:\.\d+)?", "", path) or "/"
        query = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                k, _, v = part.partition("=")
                if k:
                    from urllib.parse import unquote_plus
                    query[unquote_plus(k)] = unquote_plus(v)
        for method, rx, fn in ROUTES:
            if method and method != self.command:
                continue
            m = rx.match(path)
            if not m:
                continue
            # 路径参数按 URL 规范解码：镜像名里的 : 和 / 客户端可能转义成 %3A / %2F
            args = [unquote(g) if g else g for g in m.groups()]
            try:
                return fn(self, query, *args)
            except ApiError as e:
                self.send_json({"message": e.message}, e.code)
                return
            except BrokenPipeError:
                return
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                try:
                    self.send_json({"message": f"内部错误: {e}"}, 500)
                except OSError:
                    pass
                return
        self.send_json({"message": "not found"}, 404)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "请求体不是合法 JSON")

    do_GET = _dispatch
    do_POST = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch
    do_PUT = _dispatch


# ---------------- 路由实现 ----------------

@route("GET", r"^/_ping$")
def ep_ping(h, q):
    h.send_text("OK")


@route("GET", r"^/(?:version|info)$")
def ep_version(h, q):
    rt = h.runtime
    cs = rt.store.containers()
    running = sum(1 for c in cs if c.get("status") == "running")
    if h.path.rstrip("/").endswith("info"):
        h.send_json({
            "ID": "udockerd", "Containers": len(cs), "ContainersRunning": running,
            "ContainersPaused": 0, "ContainersStopped": len(cs) - running,
            "Images": len(rt.ud.images()), "Driver": "udocker",
            "DriverStatus": [["exec.mode", "PRoot / Fakechroot"],
                             ["network", "host"], ["cgroups", "disabled"]],
            "OperatingSystem": "Termux", "OSType": "linux",
            "Architecture": os.uname().machine, "NCPU": os.cpu_count() or 1,
            "MemTotal": 0, "ServerVersion": VERSION,
            "KernelVersion": os.uname().release,
            "Warnings": ["host 网络", "无 cgroups", "无 Volume 对象", "无构建"],
        })
        return
    h.send_json({
        "Version": VERSION, "ApiVersion": API_VERSION, "MinAPIVersion": MIN_API,
        "GitCommit": "", "GoVersion": "python", "Os": "linux",
        "Arch": os.uname().machine, "KernelVersion": os.uname().release,
        "BuildTime": "", "Experimental": False,
    })


@route("GET", r"^/images/json$")
def ep_images(h, q):
    ims = h.runtime.ud.images()
    h.send_json([{k: v for k, v in im.items() if not k.startswith("_")} for im in ims])


@route("GET", r"^/images/(.+)/json$")
def ep_image_inspect(h, q, name):
    im = h.runtime.ud.image(name)
    h.send_json({
        "Id": im["Id"], "RepoTags": im["RepoTags"], "Created": _now_iso(im["Created"]),
        "Size": im["Size"], "VirtualSize": im["Size"],
        "Architecture": im.get("Architecture") or os.uname().machine,
        "Os": "linux", "Config": im.get("Config") or {}, "RootFS": {"Type": "layers"},
    })


@route("POST", r"^/images/create$")
def ep_image_pull(h, q):
    ref = q.get("fromImage") or ""
    tag = q.get("tag") or ""
    if tag and ":" not in ref.rsplit("/", 1)[-1]:
        ref = f"{ref}:{tag}"
    if not ref:
        raise ApiError(400, "缺少 fromImage")
    platform = q.get("platform") or _read_config(_config_path).get("platform") or ""
    h.chunk_open()
    state = {"n": 0}

    def on_line(line):
        state["n"] += 1
        h.chunk_write(json.dumps({"status": line}, ensure_ascii=False) + "\r\n")

    try:
        code = h.runtime.ud.pull(ref, platform, on_line)
    except ApiError as e:
        h.chunk_write(json.dumps({"error": e.message}, ensure_ascii=False) + "\r\n")
        h.chunk_close()
        return
    if code != 0:
        h.chunk_write(json.dumps({"error": f"udocker pull 失败（exit {code}）"},
                                 ensure_ascii=False) + "\r\n")
    else:
        h.chunk_write(json.dumps({"status": f"拉取完成：{ref}（{state['n']} 行输出）"},
                                 ensure_ascii=False) + "\r\n")
    h.chunk_close()


@route("DELETE", r"^/images/(.+)$")
def ep_image_rm(h, q, name):
    im = h.runtime.ud.image(name)
    tag = im["RepoTags"][0]
    code, out = h.runtime.ud.remove_image(tag)
    if code != 0:
        raise ApiError(409, f"删除镜像失败：{_last_lines(out, 2)}")
    h.send_json([{"Untagged": tag}, {"Deleted": im["Id"]}])


@route("POST", r"^/images/load$")
def ep_image_load(h, q):
    rt = h.runtime
    path = q.get("path") or ""
    tmp = ""
    if path:
        if not os.path.isfile(path):
            raise ApiError(400, "导入文件不存在")
    else:
        n = int(h.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ApiError(400, "缺少镜像数据")
        os.makedirs(os.path.join(rt.store.dir, "tmp"), mode=0o700, exist_ok=True)
        tmp = os.path.join(rt.store.dir, "tmp", f"load-{uuid.uuid4().hex[:8]}.tar")
        with open(tmp, "wb") as fh:
            remain = n
            while remain > 0:
                chunk = h.rfile.read(min(1 << 20, remain))
                if not chunk:
                    break
                fh.write(chunk)
                remain -= len(chunk)
        path = tmp
    h.chunk_open()
    try:
        code, out = rt.ud.load(path)
        for line in (out or "").splitlines():
            if line.strip():
                h.chunk_write(json.dumps({"stream": line + "\n"},
                                         ensure_ascii=False) + "\r\n")
        if code != 0:
            h.chunk_write(json.dumps({"error": f"udocker load 失败（exit {code}）"},
                                     ensure_ascii=False) + "\r\n")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        h.chunk_close()


@route("GET", r"^/containers/json$")
def ep_containers(h, q):
    all_ = q.get("all", "0") not in ("0", "false", "")
    h.send_json(h.runtime.list_containers(all_))


@route("POST", r"^/containers/create$")
def ep_container_create(h, q):
    body = h._body_json()
    name = q.get("name") or body.get("name") or ("pp_" + uuid.uuid4().hex[:8])
    h.send_json(h.runtime.create(body, name), 201)


@route("GET", r"^/containers/([^/]+)/json$")
def ep_container_inspect(h, q, cid):
    h.send_json(h.runtime.inspect(h.runtime.store.get(cid)))


@route("POST", r"^/containers/([^/]+)/start$")
def ep_container_start(h, q, cid):
    h.runtime.start(cid)
    h.send_response(204)
    h.send_header("Content-Length", "0")
    h.end_headers()


@route("POST", r"^/containers/([^/]+)/stop$")
def ep_container_stop(h, q, cid):
    try:
        t = int(q.get("t") or 10)
    except ValueError:
        t = 10
    h.runtime.stop(cid, timeout=t)
    h.send_response(204)
    h.send_header("Content-Length", "0")
    h.end_headers()


@route("POST", r"^/containers/([^/]+)/restart$")
def ep_container_restart(h, q, cid):
    try:
        t = int(q.get("t") or 10)
    except ValueError:
        t = 10
    h.runtime.restart(cid, timeout=t)
    h.send_response(204)
    h.send_header("Content-Length", "0")
    h.end_headers()


@route("DELETE", r"^/containers/([^/]+)$")
def ep_container_rm(h, q, cid):
    h.runtime.remove(cid, force=q.get("force") in ("1", "true", "True"))
    h.send_response(204)
    h.send_header("Content-Length", "0")
    h.end_headers()


@route("GET", r"^/containers/([^/]+)/logs$")
def ep_container_logs(h, q, cid):
    try:
        tail = int(q.get("tail") or 200)
    except (TypeError, ValueError):
        tail = 5000
    text = h.runtime.logs(cid, tail=tail,
                          timestamps=q.get("timestamps") in ("1", "true", "True"))
    h.send_text(text)


@route("POST", r"^/containers/([^/]+)/exec$")
def ep_exec_create(h, q, cid):
    h.send_json(h.runtime.exec_create(cid, h._body_json()), 201)


@route("POST", r"^/exec/([^/]+)/start$")
def ep_exec_start(h, q, eid):
    body = h._body_json()
    hijack = (h.headers.get("Upgrade") or "").lower() == "tcp"
    h.runtime.exec_start(eid, body, hijack, h)


@route("POST", r"^/exec/([^/]+)/resize$")
def ep_exec_resize(h, q, eid):
    try:
        hh, ww = int(q.get("h") or 24), int(q.get("w") or 80)
    except ValueError:
        raise ApiError(400, "窗口尺寸非法")
    h.runtime.exec_resize(eid, hh, ww)
    h.send_response(200)
    h.send_header("Content-Length", "0")
    h.end_headers()


@route("GET", r"^/events$")
def ep_events(h, q):
    """Docker 语义的事件流。`since` 为 Unix 秒；`until=1` 时只回放缓冲后关闭。

    实现上用「时间戳 + 去重集合」推进游标：同一秒内的多个事件不会因为
    cursor+1 被跳过（容器反复重启时一秒内可能产生多条 die/restart）。
    """
    rt = h.runtime
    try:
        since = int(q.get("since") or 0)
    except ValueError:
        since = 0
    once = q.get("until") in ("1", "true", "True")
    h.chunk_open("application/json")
    cursor = since
    seen = set()
    try:
        while True:
            evs = rt.events.since(cursor)
            for ev in evs:
                key = (ev["time"], ev["Action"], ev["Actor"]["ID"],
                       ev["Actor"]["Attributes"].get("exitCode", ""))
                if key in seen:
                    continue
                seen.add(key)
                h.chunk_write(json.dumps(ev, ensure_ascii=False) + "\n")
                cursor = max(cursor, ev["time"])
            if len(seen) > 500:
                seen.clear()
            if once:
                break
            r, _, _ = select.select([h.connection], [], [], 1.0)
            if r:
                try:
                    if h.connection.recv(1, socket.MSG_PEEK) == b"":
                        break
                except OSError:
                    break
    except (OSError, BrokenPipeError):
        pass
    finally:
        try:
            h.chunk_close()
        except OSError:
            pass


@route("GET", r"^/volumes$")
def ep_volumes(h, q):
    h.send_json({"Volumes": [], "Warnings": [
        "udocker 不支持 Docker Volume 对象；请使用宿主机目录 bind mount"]})


@route("GET", r"^/networks$")
def ep_networks(h, q):
    h.send_json([{"Name": "host", "Id": "host", "Created": _now_iso(),
                  "Driver": "host", "Scope": "local", "Internal": False,
                  "Attachable": False, "Labels": {}}])


@route("POST", r"^/build$")
def ep_build(h, q):
    raise ApiError(501, "udockerd 不支持构建：udocker 1.3.x 无 build 子命令，"
                        "BuildKit 亦不可用；请在其他机器 docker build 后 docker save 导入")


@route("GET", r"^/containers/([^/]+)/stats$")
@route("GET", r"^/containers/([^/]+)/top$")
def ep_stats(h, q, cid):
    raise ApiError(501, "无 cgroups，udockerd 不提供资源统计/进程列表")


@route("GET", r"^/system/df$")
def ep_df(h, q):
    ims = h.runtime.ud.images()
    h.send_json({"LayersSize": sum(i["Size"] for i in ims), "Images": ims,
                 "Containers": [], "Volumes": [], "BuildCache": []})


# ---------------- 启动 ----------------

def _find_udocker(explicit=""):
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    cands = [shutil.which("udocker"),
             os.path.join(os.environ.get("PREFIX", ""), "bin", "udocker"),
             os.path.expanduser("~/udocker/udocker"),
             os.path.expanduser("~/.local/bin/udocker")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="udockerd - Docker Engine API for udocker")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2375)
    ap.add_argument("--data", default=os.path.expanduser("~/pocketpanel/data/docker-runtime"))
    ap.add_argument("--udocker", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--config", default="")
    args = ap.parse_args(argv)

    udpath = _find_udocker(args.udocker)
    if not udpath:
        _log("未找到 udocker；请先安装（面板「容器 → 运行时」可一键安装）")
        return 2
    os.makedirs(args.data, mode=0o700, exist_ok=True)
    ud = Udocker(udpath, repo=args.repo or None)
    rt = Runtime(args.data, ud, config_path=args.config)
    Handler.runtime = rt
    # 对账只是「顺手把该起的容器拉起来」，它失败不该让守护进程起不来 ——
    # 否则 runit 会一直重启、日志刷屏，面板还连不上。
    try:
        rt.reconcile()
    except Exception as e:  # noqa: BLE001
        _log(f"启动对账失败（服务继续运行）：{e}")
    rt.ensure_monitor()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    def bye(signum, frame):  # noqa: ARG001
        _log("收到退出信号，停止容器…")
        try:
            rt.shutdown()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, bye)
        except (OSError, ValueError):
            pass

    _log(f"udockerd {VERSION} 监听 {args.host}:{args.port}（udocker={udpath}）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
