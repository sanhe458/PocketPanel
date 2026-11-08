"""frpc 二进制管理（M15 15.19）：一键安装 / 升级 / 卸载。

两个来源：
  pkg     Termux 官方源 `pkg install -y frpc`
  github  fatedier/frp Release 预编译包（自动选架构、SHA256 校验、tar 解压）

国内访问 GitHub 不稳定，内置一组镜像前缀（ghproxy 风格：
{mirror}{完整 github URL}），支持并发测速选最快，也允许自定义镜像。
安装全程走 core 异步任务，前端用通用 /api/exec/{id} 轮询日志与进度。
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import core
import tunnel

PREFIX = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
INSTALL_PATH = os.path.join(PREFIX, "bin", "frpc")
REPO = "fatedier/frp"

# 内置镜像：url 为空表示直连；拼接规则 {mirror}{github_url}
MIRRORS = [
    {"id": "direct", "label": "GitHub 直连", "url": "", "builtin": True},
    {"id": "ghfast", "label": "ghfast.top", "url": "https://ghfast.top/", "builtin": True},
    {"id": "ghproxy", "label": "gh-proxy.com", "url": "https://gh-proxy.com/", "builtin": True},
    {"id": "ghpnet", "label": "ghproxy.net", "url": "https://ghproxy.net/", "builtin": True},
    {"id": "moeyy", "label": "github.moeyy.xyz", "url": "https://github.moeyy.xyz/", "builtin": True},
]

UA = {"User-Agent": "PocketPanel/1.0"}


def arch():
    """uname 架构 → frp release 的 GOARCH 命名。"""
    m = (platform.machine() or os.uname().machine or "").lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m.startswith("armv7") or m.startswith("armv8l"):
        return "arm"
    if m.startswith("arm"):
        return "arm"
    return m or "arm64"


def is_pkg_installed():
    """frpc 是否来自 Termux 包管理器（官方包名是 frp，内含 frpc/frps）。"""
    try:
        return subprocess.run(["dpkg", "-s", "frp"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def status():
    """给前端的状态视图。busy 为进行中任务的 id（空串 = 空闲）。

    latest 走缓存并后台刷新：绝不让 GitHub API 的超时阻塞本接口
    （否则前端 Promise.all 会把整个穿透页拖住 8–16s）。
    """
    cli = tunnel.client_info(force=True)
    src = ""
    if cli.get("found"):
        src = "pkg" if is_pkg_installed() else "github"
    threading.Thread(target=latest_version, daemon=True).start()  # 后台续缓存
    return {
        "installed": bool(cli.get("found")),
        "version": cli.get("version", ""),
        "path": cli.get("path", ""),
        "fmt": cli.get("fmt", "toml"),
        "source": src,
        "arch": arch(),
        "install_path": INSTALL_PATH,
        "latest": _latest_cache["v"],
        "busy": bool(_installing()),
        "task_id": _installing(),
    }


_installing_flag = {"id": ""}


def _installing():
    tid = _installing_flag["id"]
    if not tid:
        return ""
    t = core.TASKS.get(tid)
    if not t or t.get("status") != "running":
        _installing_flag["id"] = ""
        return ""
    return tid


# ---------------- 版本与 URL ----------------

_latest_cache = {"v": "", "ok_ts": 0.0, "fail_ts": 0.0}


def latest_version(timeout=8, force=False):
    """最新版本号（去 v 前缀）。结果缓存 10 分钟、失败缓存 2 分钟——

    这个接口在 status 里同步返回，若无缓存每次刷新页面都要等 GitHub
    API 8–16s 超时，整个穿透页会被拖住（实测即「页面没反应」的元凶）。
    """
    now = time.time()
    if not force:
        if _latest_cache["v"] and now - _latest_cache["ok_ts"] < 600:
            return _latest_cache["v"]
        if not _latest_cache["v"] and now - _latest_cache["fail_ts"] < 120:
            return ""
    ver = ""
    for url in (f"https://api.github.com/repos/{REPO}/releases/latest",
                f"https://github.com/{REPO}/releases/latest"):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if "api.github.com" in url:
                    tag = __import__("json").loads(r.read().decode()).get("tag_name", "")
                else:
                    # releases/latest 302 到 tag 页
                    tag = r.url.rstrip("/").rsplit("/", 1)[-1] if hasattr(r, "url") else ""
            m = re.match(r"^v?(\d+\.\d+\.\d+)$", tag.strip())
            if m:
                ver = m.group(1)
                break
        except Exception:  # noqa: BLE001
            continue
    if ver:
        _latest_cache.update({"v": ver, "ok_ts": now})
    else:
        _latest_cache["fail_ts"] = now
    return ver


def build_urls(version, mirror_url=""):
    """版本 + 镜像 → (包 URL, checksums URL)。"""
    a = arch()
    ver = version.lstrip("v")
    base = (f"https://github.com/{REPO}/releases/download/"
            f"v{ver}/frp_{ver}_linux_{a}.tar.gz")
    sums = (f"https://github.com/{REPO}/releases/download/"
            f"v{ver}/frp_sha256_checksums.txt")
    if mirror_url:
        p = mirror_url if mirror_url.endswith("/") else mirror_url + "/"
        base, sums = p + base, p + sums
    return base, sums


# ---------------- 测速 ----------------

def speedtest(mirror_urls=None, timeout=6):
    """并发 HEAD 测速：对 release latest 的 302 探测，返回 [{id, url, ms, ok}]。"""
    targets = []
    for m in MIRRORS:
        if mirror_urls is not None and m["url"] not in mirror_urls:
            continue
        targets.append(m)
    probe_path = f"https://github.com/{REPO}/releases/latest"

    def one(m):
        url = (m["url"] + probe_path) if m["url"] else probe_path
        t0 = time.time()
        try:
            # 用 GET（Range 只取 1 字节）而不是 HEAD：部分线路/代理会吞 HEAD
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
            opener = urllib.request.build_opener(_NoRedirect)
            r = opener.open(req, timeout=timeout)
            r.read(64)
            r.close()
            return {"id": m["id"], "label": m["label"], "url": m["url"],
                    "ms": int((time.time() - t0) * 1000), "ok": True}
        except _RedirectCatched:
            # 302/301 都算可达
            return {"id": m["id"], "label": m["label"], "url": m["url"],
                    "ms": int((time.time() - t0) * 1000), "ok": True}
        except Exception as e:  # noqa: BLE001
            return {"id": m["id"], "label": m["label"], "url": m["url"],
                    "ms": int((time.time() - t0) * 1000), "ok": False,
                    "err": str(e)[:80]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(one, targets))
    rows.sort(key=lambda r: (not r["ok"], r["ms"]))
    return rows


class _RedirectCatched(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise _RedirectCatched


# ---------------- 下载 ----------------

def _download(url, dest=None, t=None, label="", max_mb=64, deadline=None):
    """流式下载。dest=None 时进内存返回 bytes；否则写文件返回路径。

    deadline 为墙钟总时长（秒）——socket timeout 只管单次 read，
    慢速但不断的链路会永久挂着，必须加总闸。
    """
    req = urllib.request.Request(url, headers=UA)
    started = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        if dest:
            fh = open(dest, "wb")
        else:
            buf = b""
        done = 0
        while True:
            if deadline and time.time() - started > deadline:
                raise ValueError(f"{label or '下载'}超过 {deadline}s 总时长限制，已中止（可换镜像重试）")
            chunk = r.read(1 << 18)
            if not chunk:
                break
            done += len(chunk)
            if done > max_mb << 20:
                raise ValueError(f"{label or '下载'}超过 {max_mb}MB 上限，中止")
            if dest:
                fh.write(chunk)
            else:
                buf += chunk
            if t is not None and total:
                core.task_progress(t, done * 100 / total)
                if int(done >> 20) != int((done - len(chunk)) >> 20):
                    rate = int(done / max(1, time.time() - started) / 1024)
                    core.task_log(t, f"{label} 已下载 {done >> 20} / {total >> 20} MB（{rate} KB/s）")
        if dest:
            fh.close()
            return dest
        return buf


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------- 安装 ----------------

def install_github(t, version, mirror_url=""):
    """GitHub Release 安装：下载 → 校验 → 解压 → 落位 → frpc -v 验证。"""
    ver = (version or "").strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        raise ValueError(f"版本号不合法：{version or '(空)'}（如 0.61.0，或先点「获取最新版本」）")
    pkg_url, sums_url = build_urls(ver, mirror_url)
    a = arch()
    tmp = os.path.join(core.DATA, "tunnels", f".dl-{int(time.time())}.tar.gz")
    os.makedirs(os.path.dirname(tmp), mode=0o700, exist_ok=True)
    try:
        core.task_log(t, f"架构 {a} · 版本 v{ver}")
        core.task_log(t, f"下载 {pkg_url}")
        _download(pkg_url, dest=tmp, t=t, label="安装包", deadline=900)
        size = os.path.getsize(tmp) >> 20
        core.task_log(t, f"下载完成 {size} MB")
        core.task_progress(t, 0)

        # SHA256 校验（15.19）。镜像没缓存 checksums 时给出明确警告。
        expected = ""
        try:
            body = _download(sums_url, max_mb=2).decode("utf-8", "replace")
            m = re.search(rf"^([a-f0-9]{{64}})\s+\*?frp_{ver}_linux_{a}\.tar\.gz$",
                          body, re.M)
            if m:
                expected = m.group(1)
        except Exception as e:  # noqa: BLE001
            core.task_log(t, f"警告：checksums 下载失败（{str(e)[:80]}），跳过哈希校验")
        if expected:
            actual = _sha256(tmp)
            if actual != expected:
                raise ValueError(f"SHA256 校验失败：期望 {expected}，实际 {actual}")
            core.task_log(t, "SHA256 校验通过")
        else:
            core.task_log(t, "警告：未取得官方 checksum，本次安装未做哈希校验")

        # 解压（防路径穿越：只取常规文件）
        core.task_log(t, "解压…")
        got = None
        with tarfile.open(tmp, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isdir() or not member.isfile():
                    continue
                base_name = os.path.basename(member.name)
                if base_name in ("frpc", "frpc.exe"):
                    out = os.path.join(os.path.dirname(tmp), base_name)
                    with tf.extractfile(member) as src, open(out, "wb") as dst:
                        dst.write(src.read())
                    got = out
                    break
        if not got:
            raise ValueError("压缩包里没找到 frpc 可执行文件")

        # 落位 $PREFIX/bin
        os.makedirs(os.path.dirname(INSTALL_PATH), exist_ok=True)
        if os.path.exists(INSTALL_PATH):
            core.task_log(t, f"覆盖旧版 {INSTALL_PATH}")
        shutil.move(got, INSTALL_PATH)
        os.chmod(INSTALL_PATH, 0o755)

        # 验证
        r = subprocess.run([INSTALL_PATH, "-v"], capture_output=True, text=True, timeout=15)
        out = (r.stdout or r.stderr).strip().splitlines()
        ver_out = out[0].strip() if out else "?"
        if r.returncode != 0:
            raise ValueError(f"安装后 frpc -v 失败：exit {r.returncode}")
        core.task_log(t, f"frpc {ver_out} 安装成功 → {INSTALL_PATH}")
        tunnel._client_cache.update({"ts": 0, "info": None})
        core.task_result(t, {"version": ver_out, "path": INSTALL_PATH})
    finally:
        for p in (tmp,):
            try:
                os.unlink(p)
            except OSError:
                pass


# Termux 主仓库镜像（按国内可用性排序；用于自动修复死源）
TERMUX_MIRRORS = [
    ("清华 TUNA", "https://mirrors.tuna.tsinghua.edu.cn/termux/apt/termux-main"),
    ("北京外国语 BFSU", "https://mirrors.bfsu.edu.cn/termux/apt/termux-main"),
    ("官方 packages.termux.dev", "https://packages.termux.dev/apt/termux-main"),
]


def _apt_update(t, timeout=150):
    """apt update；返回是否成功。"""
    try:
        code = core.run_shell_task(t, ["apt", "update"])
    except RuntimeError as e:
        core.task_log(t, f"apt update 启动失败：{e}")
        return False
    return code == 0


def _switch_mirror(t, mirrors):
    """备份 sources.list 后依次尝试镜像，成功即返回所用镜像名。"""
    sl = os.path.join(PREFIX, "etc", "apt", "sources.list")
    bak = sl + ".bak-pocketpanel"
    try:
        orig = open(sl, "r", encoding="utf-8").read()
    except OSError:
        orig = ""
    if not os.path.exists(bak) and orig:
        with open(bak, "w", encoding="utf-8") as fh:
            fh.write(orig)
        os.chmod(bak, 0o644)
        core.task_log(t, f"原 sources.list 已备份 → {bak}")
    for label, url in mirrors:
        core.task_log(t, f"尝试切换到 {label}: {url}")
        try:
            with open(sl, "w", encoding="utf-8") as fh:
                fh.write(f"deb {url} stable main\n")
            os.chmod(sl, 0o644)
        except OSError as e:
            core.task_log(t, f"写入失败：{e}")
            continue
        if _apt_update(t):
            core.task_log(t, f"{label} 可用")
            return label
    return ""


def _ensure_repo(t):
    """确保 apt 源可用；死源自动切换国内镜像。返回 (ok, 修复说明)。"""
    core.task_log(t, "apt update 检查软件源…")
    if _apt_update(t, timeout=90):
        return True, ""
    core.task_log(t, "当前软件源不可用（可能为失效的第三方镜像），自动修复…")
    label = _switch_mirror(t, TERMUX_MIRRORS)
    if label:
        return True, f"已自动切换到 {label}"
    # 全部失败：还原备份
    sl = os.path.join(PREFIX, "etc", "apt", "sources.list")
    bak = sl + ".bak-pocketpanel"
    if os.path.exists(bak):
        try:
            shutil.copy(bak, sl)
            core.task_log(t, "所有镜像均不可用，已还原原 sources.list")
        except OSError:
            pass
    return False, "所有 Termux 镜像均不可用（请检查设备网络）"


def install_pkg(t):
    """Termux 官方源安装。死源自动修复（用户设备曾指向失效镜像）。"""
    if shutil.which("pkg") is None:
        raise ValueError("找不到 pkg（非 Termux 环境？）")
    ok, note = _ensure_repo(t)
    if not ok:
        raise ValueError(f"软件源不可用：{note}；建议改用 GitHub Release 源安装")
    if note:
        core.task_log(t, note)
    # Termux 官方仓库的包名是 frp（同时提供 frpc 与 frps 二进制）
    core.task_log(t, "pkg install -y frp …（Termux 包名 frp，含 frpc）")
    code = core.run_shell_task(t, ["pkg", "install", "-y", "frp"])
    if code != 0:
        raise ValueError(f"pkg 安装失败 exit {code}（当前仓库没有 frp 包，建议改用 GitHub 源）")
    tunnel._client_cache.update({"ts": 0, "info": None})
    cli = tunnel.client_info(force=True)
    core.task_log(t, f"frpc {cli.get('version', '?')} 安装成功 → {cli.get('path')}")
    core.task_result(t, {"version": cli.get("version", ""), "path": cli.get("path", "")})


def start_install(source, version="", mirror_url=""):
    """启动安装任务；返回 task id。同一时刻只允许一个安装。

    注意：busy 标记只在 job 线程内部设置/清除——主线程若在 job 结束后
    再补设一次，会把 finally 里的清空覆盖掉，busy 永久卡死。
    """
    if _installing():
        raise RuntimeError("已有安装任务在进行中")
    if source not in ("pkg", "github"):
        raise ValueError(f"未知来源 {source}")
    if source == "github" and not (version or "").strip():
        raise ValueError("请填写版本号或点「获取最新版本」")

    def job(t):
        _installing_flag["id"] = t["id"]
        try:
            if source == "pkg":
                install_pkg(t)
            else:
                install_github(t, version, mirror_url)
        finally:
            _installing_flag["id"] = ""

    t = core.run_job("frpc-install", f"安装 frpc（{source}）", job)
    return t["id"]


def uninstall():
    """按来源卸载。"""
    cli = tunnel.client_info(force=True)
    if not cli.get("found"):
        raise ValueError("frpc 未安装")
    if is_pkg_installed():
        r = subprocess.run(["pkg", "uninstall", "-y", "frpc"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ValueError("pkg 卸载失败：" + (r.stderr or "")[:200])
        return {"removed": "pkg", "path": cli["path"]}
    if os.path.realpath(cli["path"]) != os.path.realpath(INSTALL_PATH) \
            and os.path.realpath(cli["path"]) != os.path.expanduser("~/frpc"):
        raise ValueError(f"frpc 在 {cli['path']}，不是面板安装的，请手动处理")
    os.unlink(cli["path"])
    tunnel._client_cache.update({"ts": 0, "info": None})
    return {"removed": "github", "path": cli["path"]}
