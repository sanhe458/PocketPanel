"""docker-compose.yml 的受限解析与编排（P2，非原生 Compose）。

目标不是实现 Compose 规范，而是让「一份常见的 compose 文件」能在手机上
跑起来。支持：

* `services` / `image` / `command` / `entrypoint`（拼接）/ `environment`
  （列表与映射两种写法）/ `env_file` / `volumes`（**只支持宿主机目录**）/
  `ports` / `restart` / `depends_on`（只用于启动顺序）/ `working_dir` / `user` /
  `container_name` / `name`；
* `${VAR}`、`${VAR:-default}` 插值，变量来自界面填写的 .env 内容；
* JSON 格式的 compose 文件（等价于 YAML 子集）。

明确拒绝：`build`、`networks`、命名卷、`deploy`、锚点/别名、`extends`，
错误信息会直接说明原因，而不是静默忽略。

YAML 解析器只覆盖 compose 常用的「映射 + 序列 + 标量 + 块文本」，标准库没有
YAML，引第三方库又违背零依赖底线。解析失败时给出出错行号。
"""
import json
import os
import re

NAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
PORT_RE = re.compile(r"^(?:(?P<ip>[0-9a-fA-F.:]+):)?(?P<host>\d+):(?P<cont>\d+)(?:/(?P<proto>tcp|udp))?$")
SINGLE_PORT_RE = re.compile(r"^(?P<port>\d+)(?:/(?P<proto>tcp|udp))?$")

SUPPORTED = ("image", "command", "entrypoint", "environment", "env_file",
             "volumes", "ports", "restart", "depends_on", "working_dir", "user",
             "container_name", "labels", "stdin_open", "tty")
REJECTED = {
    "build": "不支持构建（udocker 无 build）；请先构建镜像再 push / 导入",
    "networks": "不支持自定义网络（host 模式）",
    "deploy": "不支持 deploy（无 cgroups / Swarm）",
    "extends": "不支持 extends",
    "secrets": "不支持 secrets 对象",
    "configs": "不支持 configs 对象",
}


class ComposeError(ValueError):
    pass


# ---------------- YAML 子集 ----------------

def _strip_comment(line):
    out, quote = [], ""
    prev = " "
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and prev in (" ", "\t"):
            break
        out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _scalar(text):
    t = text.strip()
    if not t:
        return None
    if t[0] == "[" and t[-1] == "]":
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_inline(inner)]
    if t[0] == "{" and t[-1] == "}":
        inner = t[1:-1].strip()
        out = {}
        if not inner:
            return out
        for part in _split_inline(inner):
            k, _, v = part.partition(":")
            out[str(_scalar(k))] = _scalar(v)
        return out
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        body = t[1:-1]
        if t[0] == '"':
            body = body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            body = body.replace("''", "'")
        return body
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if re.match(r"^-?\d+$", t):
        try:
            return int(t)
        except ValueError:
            return t
    if re.match(r"^-?\d+\.\d+$", t):
        try:
            return float(t)
        except ValueError:
            return t
    if t[0] in ("&", "*", "!"):
        raise ComposeError("不支持 YAML 锚点/别名/标签，请展开后再导入")
    return t


def _split_inline(text):
    parts, buf, quote, depth = [], [], "", 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def _map_pair(text):
    """按 YAML 规则找「映射冒号」：必须是 `: ` 或行尾的 `:`。

    不是所有冒号都算：`- ./pg:/var/lib/postgres` 里冒号后面直接跟路径，
    整行是标量（bind mount），不是映射。用 `":" in item` 判断会把它解析成
    {"./pg": "/var/lib/postgres"}，然后绑定目录就丢了。
    """
    quote = ""
    depth = 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip(), text[i + 1:]
    return None


class _Yaml:
    def __init__(self, text):
        self.raw = text.replace("\t", "    ").splitlines()
        self.lines = []
        for no, ln in enumerate(self.raw, 1):
            if not ln.strip():
                continue
            stripped = _strip_comment(ln)
            if not stripped.strip():
                continue
            if stripped.strip() in ("---", "..."):
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            self.lines.append((indent, stripped.strip(), no))
        self.i = 0

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self):
        if not self.lines:
            raise ComposeError("文件为空")
        val = self._block(self.lines[0][0])
        if self.peek():
            _ind, txt, no = self.peek()
            raise ComposeError(f"第 {no} 行缩进不正确：{txt}")
        return val

    def _block(self, indent):
        cur = self.peek()
        if not cur:
            return None
        if cur[1].startswith("- "):
            return self._list(indent)
        return self._map(indent)

    def _list(self, indent):
        out = []
        while True:
            cur = self.peek()
            if not cur or cur[0] < indent:
                break
            if cur[0] > indent:
                _i, txt, no = cur
                raise ComposeError(f"第 {no} 行缩进不正确：{txt}")
            if not cur[1].startswith("- "):
                break
            _i, txt, no = cur
            self.i += 1
            item = txt[2:].strip()
            if not item:
                nxt = self.peek()
                out.append(self._block(nxt[0]) if nxt and nxt[0] > indent else None)
                continue
            pair = None if item.startswith(("'", '"', "[", "{")) else _map_pair(item)
            if pair:
                # "- key: value" 起一个映射，后续同级键在 indent+2
                k, v = pair
                d = {k: self._value(v, indent + 2, no)}
                nxt = self.peek()
                while nxt and nxt[0] == indent + 2 and not nxt[1].startswith("- "):
                    _i2, t2, n2 = nxt
                    p2 = _map_pair(t2)
                    if not p2:
                        break
                    self.i += 1
                    k2, v2 = p2
                    d[k2] = self._value(v2, indent + 4, n2)
                    nxt = self.peek()
                out.append(d)
            else:
                out.append(_scalar(item))
        return out

    def _map(self, indent):
        out = {}
        while True:
            cur = self.peek()
            if not cur or cur[0] < indent:
                break
            if cur[0] > indent:
                _i, txt, no = cur
                raise ComposeError(f"第 {no} 行缩进不正确：{txt}")
            if cur[1].startswith("- "):
                break
            _i, txt, no = cur
            pair = _map_pair(txt)
            if not pair:
                raise ComposeError(f"第 {no} 行缺少冒号：{txt}")
            key, val = pair
            if not key:
                raise ComposeError(f"第 {no} 行为空键")
            self.i += 1
            out[key] = self._value(val, indent, no)
        return out

    def _value(self, val, indent, no):
        val = val.strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            return self._block_text(indent, folded=val.startswith(">"))
        if val:
            return _scalar(val)
        nxt = self.peek()
        if nxt and nxt[0] > indent:
            return self._block(nxt[0])
        return None

    def _block_text(self, indent, folded=False):
        rows = []
        while True:
            cur = self.peek()
            if not cur or cur[0] < indent:
                break
            _i, txt, _no = cur
            self.i += 1
            rows.append(txt)
        return (" " if folded else "\n").join(rows)


def parse_yaml(text):
    t = text.strip()
    if not t:
        raise ComposeError("文件为空")
    if t[0] in "{[":
        try:
            return json.loads(t)
        except ValueError as e:
            raise ComposeError(f"JSON 解析失败：{e}")
    return _Yaml(text).parse()


# ---------------- 插值 / 规范化 ----------------

def interpolate(value, env):
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m):
        name = m.group(1) or m.group(3)
        default = m.group(2)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        return ""

    return VAR_RE.sub(repl, value)


def parse_env_text(text):
    env = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            env[k] = v
    return env


def _env_list(raw):
    out = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            out.append({"key": str(k), "value": "" if v is None else str(v)})
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                for k, v in it.items():
                    out.append({"key": str(k), "value": "" if v is None else str(v)})
            else:
                k, _, v = str(it).partition("=")
                out.append({"key": k.strip(), "value": v})
    return out


def _ports(raw):
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append({"host": item.get("published"), "container": item.get("target"),
                        "proto": item.get("protocol") or "tcp"})
            continue
        s = str(item).strip()
        m = PORT_RE.match(s)
        if m:
            out.append({"host": int(m.group("host")), "container": int(m.group("cont")),
                        "proto": m.group("proto") or "tcp"})
            continue
        m = SINGLE_PORT_RE.match(s)
        if m:
            p = int(m.group("port"))
            out.append({"host": p, "container": p, "proto": m.group("proto") or "tcp"})
            continue
        raise ComposeError(f"端口写法不支持：{s}（应形如 8080:80）")
    return out


def _volumes(raw):
    """只接受宿主机目录。返回 [{host, container, ro}]，相对路径稍后由 base_dir 解析。"""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            src = item.get("source")
            dst = item.get("target")
            ro = bool(item.get("read_only"))
        else:
            parts = str(item).split(":")
            if len(parts) < 2:
                raise ComposeError(f"卷写法不支持：{item}（需要 宿主机目录:容器路径）")
            src, dst = parts[0], parts[1]
            ro = len(parts) > 2 and "ro" in parts[2].split(",")
        if not src or not dst:
            raise ComposeError(f"卷写法不完整：{item}")
        is_path = src.startswith(("/", "./", "../", "~"))
        if not is_path:
            raise ComposeError(
                f"不支持命名卷 {src}：udocker 没有 Volume 对象，请改成宿主机目录（如 ./{src}）")
        out.append({"host": src, "container": dst, "ro": ro})
    return out


def _depends(raw):
    if isinstance(raw, dict):
        return [str(k) for k in raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _cmd(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return str(raw)


def plan(text, env_text="", project="", base_dir=""):
    """解析 + 规范化。base_dir 用于解析 ./ 相对路径与 env_file。"""
    doc = parse_yaml(text)
    if not isinstance(doc, dict):
        raise ComposeError("顶层必须是映射（services: …）")
    env = parse_env_text(env_text)
    doc = interpolate(doc, env)
    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeError("缺少 services（或为空）")
    if "version" in doc and not isinstance(doc["version"], (str, int, float)):
        raise ComposeError("version 字段格式不正确")
    project = str(project or doc.get("name") or "pp").strip()
    project = NAME_SAFE.sub("-", project)[:24] or "pp"
    out, errors = {}, []
    for sname, svc in services.items():
        sname = str(sname)
        if not isinstance(svc, dict):
            errors.append(f"{sname}: 定义必须是映射")
            continue
        for bad, why in REJECTED.items():
            if bad in svc:
                errors.append(f"{sname}: {why}")
        unknown = [k for k in svc if k not in SUPPORTED and k not in REJECTED]
        if unknown:
            errors.append(f"{sname}: 暂不支持的字段 {', '.join(unknown)}")
        if "image" not in svc:
            errors.append(f"{sname}: 缺少 image（不支持 build）")
            continue
        cmd = _cmd(svc.get("command"))
        entry = _cmd(svc.get("entrypoint"))
        if entry:
            cmd = entry + (cmd if isinstance(cmd, list) else ([cmd] if cmd else []))
        env_items = _env_list(svc.get("environment"))
        for ef in ([svc.get("env_file")] if isinstance(svc.get("env_file"), str)
                   else (svc.get("env_file") or [])):
            path = os.path.join(base_dir or ".", str(ef))
            path = os.path.realpath(path)
            root = os.path.realpath(base_dir or ".")
            if not (path == root or path.startswith(root + os.sep)):
                errors.append(f"{sname}: env_file 超出工作目录：{ef}")
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for k, v in parse_env_text(fh.read()).items():
                        if not any(e["key"] == k for e in env_items):
                            env_items.append({"key": k, "value": v})
            except OSError:
                errors.append(f"{sname}: env_file 不存在：{ef}")
        mounts = []
        for m in _volumes(svc.get("volumes")):
            host = m["host"]
            if host.startswith("~"):
                host = os.path.expanduser(host)
            if not host.startswith("/"):
                # ./pg 与 base 拼接后会带出 "base/./pg"，normalize 掉再往下走
                host = os.path.normpath(os.path.join(base_dir or ".", host))
            mounts.append({"host": host, "container": m["container"], "ro": m["ro"]})
        try:
            ports = _ports(svc.get("ports"))
        except ComposeError as e:
            errors.append(f"{sname}: {e}")
            ports = []
        restart = str(svc.get("restart") or "no")
        if restart == "unless-stopped":
            restart = "always"
        if restart not in ("no", "always", "on-failure"):
            errors.append(f"{sname}: restart 只支持 no / always / on-failure")
        name = str(svc.get("container_name") or f"{project}-{sname}")
        name = NAME_SAFE.sub("-", name)[:56]
        out[sname] = {
            "name": name, "image": str(svc.get("image")),
            "cmd": cmd, "env": env_items, "mounts": mounts, "ports": ports,
            "restart": restart, "depends_on": _depends(svc.get("depends_on")),
            "workdir": str(svc.get("working_dir") or ""),
            "user": str(svc.get("user") or ""),
        }
    for sname, svc in out.items():
        for dep in svc["depends_on"]:
            if dep not in out:
                errors.append(f"{sname}: depends_on 指向不存在的服务 {dep}")
    if errors:
        raise ComposeError("；".join(errors))
    return {"project": project, "services": out, "order": _topo(out),
            "warnings": _warnings(out)}


def _topo(services):
    deps = {k: [d for d in v["depends_on"] if d in services] for k, v in services.items()}
    order, seen, stack = [], set(), set()

    def visit(name):
        if name in seen:
            return
        if name in stack:
            raise ComposeError(f"depends_on 存在循环依赖：{name}")
        stack.add(name)
        for d in deps.get(name, []):
            visit(d)
        stack.discard(name)
        seen.add(name)
        order.append(name)

    for name in services:
        visit(name)
    return order


def _warnings(services):
    out = []
    for name, svc in services.items():
        for m in svc["mounts"]:
            if m["ro"]:
                out.append(f"{name}: {m['host']} 标记为只读，但 PRoot 无法强制，实际可写")
        for p in svc["ports"]:
            if p["host"] < 1024:
                out.append(f"{name}: 端口 {p['host']} < 1024，启动会失败")
    return out
