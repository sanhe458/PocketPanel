"""Docker Engine API 客户端（标准库实现）。

面板与 udockerd 之间只走 Engine API，不拼 udocker 命令。这里用 `http.client`
实现用到的那部分接口：镜像 / 容器 / 日志 / exec 劫持 / 事件流。

为什么不用 docker-py：它依赖 requests + urllib3，Termux 上要 pip 装一整套，
与「后端零第三方依赖」的底线冲突（README 首屏就写着这条）。本模块覆盖的
REST 契约与 docker-py 完全一致，任何标准客户端都可以直接连 udockerd。

连接地址取自 DOCKER_HOST，支持 `tcp://host:port`，默认 `tcp://127.0.0.1:2375`。
"""
import http.client
import json
import os
import socket
import urllib.parse

API_VERSION = "v1.41"
DEFAULT_HOST = "tcp://127.0.0.1:2375"


class EngineError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def host_from_env():
    raw = (os.environ.get("DOCKER_HOST") or DEFAULT_HOST).strip()
    if raw.startswith("tcp://"):
        raw = raw[6:]
    host, _, port = raw.partition(":")
    return host or "127.0.0.1", int(port or 2375)


class Hijack:
    """exec 的原始字节通道：握手完成后 socket 上就是裸流。"""

    def __init__(self, sock):
        self.sock = sock

    def send(self, data):
        self.sock.sendall(data)

    # 和 socket 同名，term.StreamSession 之类的通用代码可以直接用
    sendall = send

    def recv(self, n=65536):
        return self.sock.recv(n)

    def fileno(self):
        # term.StreamSession 用 select() 驱动读取，需要真正的 fd
        return self.sock.fileno()

    def shutdown(self, how=socket.SHUT_RDWR):
        # 和 socket 同名：StreamSession.close() 会直接对通道调 shutdown()
        try:
            self.sock.shutdown(how)
        except OSError:
            pass

    def settimeout(self, value):
        try:
            self.sock.settimeout(value)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class EngineClient:
    def __init__(self, base=None, timeout=8.0):
        self.host, self.port = host_from_env() if not base else _split(base)
        self.timeout = timeout

    # -- 基础 --

    def _conn(self, timeout=None):
        return http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout or self.timeout)

    def _path(self, path):
        if not path.startswith("/"):
            path = "/" + path
        if path.startswith("/v"):
            return path
        return f"/{API_VERSION}{path}"

    def request(self, method, path, body=None, params=None, timeout=None,
                headers=None):
        url = self._path(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn = self._conn(timeout)
        try:
            conn.request(method, url, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), raw
        except ConnectionRefusedError:
            raise EngineError(503, "无法连接容器运行时：udockerd 未启动")
        except socket.timeout:
            raise EngineError(504, "容器运行时响应超时")
        finally:
            conn.close()

    def json(self, method, path, body=None, params=None, timeout=None):
        status, _h, raw = self.request(method, path, body, params, timeout)
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {}
        if status >= 400:
            msg = data.get("message") if isinstance(data, dict) else ""
            raise EngineError(status, msg or f"引擎返回 HTTP {status}")
        return data

    # -- 探活 --

    def ping(self, timeout=1.5):
        # 探活不能抛：udockerd 没起来时 request() 会抛 503，这里一律当「不可达」
        try:
            _s, _h, raw = self.request("GET", "/_ping", timeout=timeout)
            return raw.strip() == b"OK"
        except (EngineError, OSError, http.client.HTTPException):
            return False

    def version(self):
        return self.json("GET", "/version", timeout=3)

    def info(self):
        return self.json("GET", "/info", timeout=3)

    # -- 镜像 --

    def images(self):
        return self.json("GET", "/images/json", params={"all": "1"})

    def image_inspect(self, ref):
        return self.json("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")

    def remove_image(self, ref):
        return self.json("DELETE", f"/images/{urllib.parse.quote(ref, safe='')}",
                         params={"force": "1"})

    def pull(self, ref, platform=None, on_progress=None, timeout=900):
        """流式拉取；on_progress(dict) 逐条回调。返回最后一条状态。"""
        url = self._path("/images/create")
        q = {"fromImage": ref}
        if platform:
            q["platform"] = platform
        url += "?" + urllib.parse.urlencode(q)
        conn = self._conn(timeout)
        last = {}
        try:
            conn.request("POST", url, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status >= 400:
                raw = resp.read()
                try:
                    msg = json.loads(raw.decode()).get("message", "")
                except ValueError:
                    msg = raw.decode("utf-8", "replace")[:200]
                raise EngineError(resp.status, msg or f"拉取失败 HTTP {resp.status}")
            while True:
                line = resp.readline()
                if not line:
                    break
                obj = _loads_line(line)
                if not obj:
                    continue
                if obj.get("error"):
                    raise EngineError(500, str(obj["error"]))
                last = obj
                if on_progress:
                    on_progress(obj)
        finally:
            conn.close()
        return last

    def load_image(self, path, on_progress=None, timeout=900):
        """导入 docker save 产物。文件与守护进程同机，直接给路径，避免二次搬运。"""
        url = self._path("/images/load") + "?" + urllib.parse.urlencode({"path": path})
        conn = self._conn(timeout)
        try:
            conn.request("POST", url, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status >= 400:
                raise EngineError(resp.status, _err_text(resp))
            while True:
                line = resp.readline()
                if not line:
                    break
                obj = _loads_line(line)
                if not obj:
                    continue
                if obj.get("error"):
                    raise EngineError(500, str(obj["error"]))
                if on_progress:
                    on_progress(obj)
        finally:
            conn.close()

    # -- 容器 --

    def containers(self, all_=True):
        return self.json("GET", "/containers/json", params={"all": "1" if all_ else "0"})

    def container_inspect(self, cid):
        return self.json("GET", f"/containers/{urllib.parse.quote(cid, safe='')}/json")

    def create_container(self, name, body):
        # udocker create 要把所有层解到容器目录，几百 MB 的镜像解一两分钟很正常。
        # 这里必须给足时间：默认 8 秒会报「容器运行时响应超时」，可容器其实
        # 已经建好了，用户以为失败再点一次又是「容器名已存在」。
        return self.json("POST", "/containers/create",
                         body=body, params={"name": name}, timeout=300)

    def start(self, cid):
        return self._no_content(
            "POST", f"/containers/{urllib.parse.quote(cid, safe='')}/start", timeout=120)

    def stop(self, cid, timeout=10):
        return self._no_content("POST", f"/containers/{urllib.parse.quote(cid, safe='')}/stop",
                                params={"t": str(int(timeout))}, timeout=timeout + 15)

    def restart(self, cid, timeout=10):
        return self._no_content("POST", f"/containers/{urllib.parse.quote(cid, safe='')}/restart",
                                params={"t": str(int(timeout))}, timeout=timeout + 15)

    def remove_container(self, cid, force=True):
        return self._no_content("DELETE", f"/containers/{urllib.parse.quote(cid, safe='')}",
                                params={"force": "1" if force else "0", "v": "1"})

    def _no_content(self, method, path, params=None, timeout=None):
        status, _h, raw = self.request(method, path, params=params, timeout=timeout)
        if status >= 400:
            try:
                msg = json.loads(raw.decode("utf-8", "replace")).get("message", "")
            except ValueError:
                msg = raw.decode("utf-8", "replace")[:200]
            raise EngineError(status, msg or f"HTTP {status}")
        return True

    def logs(self, cid, tail=300):
        status, _h, raw = self.request(
            "GET", f"/containers/{urllib.parse.quote(cid, safe='')}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(int(tail))}, timeout=10)
        if status >= 400:
            raise EngineError(status, f"读取日志失败 HTTP {status}")
        return raw.decode("utf-8", "replace")

    # -- exec --

    def exec_create(self, cid, cmd, env=None, tty=True):
        body = {"Cmd": cmd, "Tty": bool(tty), "AttachStdin": True,
                "AttachStdout": True, "AttachStderr": True}
        if env:
            body["Env"] = env
        return self.json("POST", f"/containers/{urllib.parse.quote(cid, safe='')}/exec",
                         body=body, timeout=10)["Id"]

    def exec_resize(self, eid, cols, rows):
        try:
            self.request("POST", f"/exec/{eid}/resize",
                         params={"h": str(int(rows)), "w": str(int(cols))}, timeout=4)
        except (OSError, http.client.HTTPException):
            pass

    def exec_hijack(self, eid, tty=True, timeout=20):
        """POST /exec/{id}/start 并升级为裸流。返回 Hijack。"""
        conn = self._conn(timeout)
        body = json.dumps({"Tty": bool(tty), "Detach": False}).encode()
        path = self._path(f"/exec/{eid}/start")
        conn.putrequest("POST", path)
        conn.putheader("Host", f"{self.host}:{self.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Connection", "Upgrade")
        conn.putheader("Upgrade", "tcp")
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        if resp.status != 101:
            raw = resp.read()
            conn.close()
            try:
                msg = json.loads(raw.decode()).get("message", "")
            except ValueError:
                msg = raw.decode("utf-8", "replace")[:200]
            raise EngineError(resp.status, msg or "exec 通道建立失败")
        sock = conn.sock
        conn.sock = None
        try:
            conn.close()
        except OSError:
            pass
        return Hijack(sock)

    # -- 事件 --

    def events(self, since=0, timeout=95):
        """长连接读事件，逐条 yield；超时/断开由调用方重连。"""
        url = self._path("/events") + "?" + urllib.parse.urlencode({"since": str(int(since))})
        conn = self._conn(timeout)
        try:
            conn.request("GET", url, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            if resp.status >= 400:
                raise EngineError(resp.status, _err_text(resp))
            while True:
                line = resp.readline()
                if not line:
                    break
                obj = _loads_line(line)
                if obj:
                    yield obj
        finally:
            conn.close()


def _split(base):
    raw = base
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    host, _, port = raw.partition(":")
    return host or "127.0.0.1", int(port or 2375)


def _loads_line(line):
    text = line.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _err_text(resp):
    try:
        raw = resp.read()
        return json.loads(raw.decode()).get("message", "") or raw.decode()[:200]
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status}"
