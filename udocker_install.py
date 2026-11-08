"""udocker 一键安装（容器运行时的前置依赖）。

装两样东西，缺一不可：

1. **udocker 客户端**：一个 Python 包（~80KB），从 GitHub Release 取；
2. **englib 引擎**：PRoot / Fakechroot 的二进制（~46MB），udocker 官方放在
   download.a.incd.pt，同时有 GitHub 镜像仓库。

国内直连 GitHub 经常只有几十 KB/s，46MB 会下到天荒地老。所以镜像前缀与
frpc 安装复用同一套（tunnel_install.MIRRORS），并且**用 englib 本体测速**，
不是拿一个小文件推测。安装走异步任务，前端能看进度与每秒速率。

有意不做的事：不校验签名（上游没有发布 englib 的 SHA256），只做 tar 结构与
体积校验；不删除 `~/.udocker`（里面是镜像与容器，删了等于清库）。
"""
import glob
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import core
import tunnel_install

REPO = "indigo-dc/udocker"
DEFAULT_VERSION = "1.3.17"
ENGLIB_NAME = "udocker-englib-1.2.11.tar.gz"
ENGLIB_URL_GH = ("https://github.com/jorge-lip/udocker-builds/raw/"
                 "master/tarballs/" + ENGLIB_NAME)
ENGLIB_URL_OFFICIAL = "https://download.a.incd.pt/udocker/" + ENGLIB_NAME

INSTALL_DIR = os.path.join(core.DATA, "runtime", "udocker")
UDOCKER_DIR = os.path.join(core.HOME, ".udocker")
UA = {"User-Agent": "PocketPanel/1.0"}

MIRRORS = tunnel_install.MIRRORS
_installing = {"id": ""}
_lock = threading.RLock()
_ver_cache = {"at": 0, "path": "", "version": ""}


def arch():
    m = (platform.machine() or os.uname().machine or "").lower()
    if m in ("aarch64", "arm64", "armv8l"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return "arm" if m.startswith("arm") else m


def udocker_bin():
    p = os.path.join(INSTALL_DIR, "udocker", "udocker")
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else ""


def englib_ok():
    return bool(glob.glob(os.path.join(UDOCKER_DIR, "bin", "proot-*")))


def _argv(path):
    """启动 udocker 的命令行前缀。

    udocker 客户端是 `#!/usr/bin/env python` 的脚本，而 Android 上没有
    /usr/bin —— 平时能跑全靠 termux-exec 的 LD_PRELOAD 改写路径。从 runit
    起的服务（面板、udockerd 都是）环境里没有这个变量，execve 就 ENOENT，
    报出来是「No such file or directory」，看着像文件被删了。认出 .py 就
    用当前解释器跑它，不跟 shebang 较劲。
    """
    try:
        real = os.path.realpath(path)
        if real.endswith(".py"):
            return [sys.executable, real]
    except OSError:
        pass
    return [path]


def version_of(path, force=False):
    now = time.time()
    with _lock:
        if (not force and _ver_cache["path"] == path
                and now - _ver_cache["at"] < 30):
            return _ver_cache["version"]
    ver = ""
    try:
        r = subprocess.run(_argv(path) + ["--version"], capture_output=True,
                           timeout=8)
        for line in (r.stdout + r.stderr).decode("utf-8", "replace").splitlines():
            if line.lower().startswith("version:"):
                ver = line.split(":", 1)[1].strip()
                break
    except (OSError, subprocess.TimeoutExpired):
        pass
    with _lock:
        _ver_cache.update(at=now, path=path, version=ver)
    return ver


def status():
    path = udocker_bin()
    return {
        "installed": bool(path), "path": path, "version": version_of(path) if path else "",
        "englib": englib_ok(), "englib_dir": UDOCKER_DIR,
        "busy": bool(_installing["id"]), "task_id": _installing["id"],
        "mirrors": MIRRORS, "arch": arch(), "default_version": DEFAULT_VERSION,
    }


def build_urls(version, mirror_url=""):
    ver = (version or DEFAULT_VERSION).lstrip("v")
    pkg = f"https://github.com/{REPO}/releases/download/{ver}/udocker-{ver}.tar.gz"
    eng = ENGLIB_URL_GH
    if mirror_url:
        p = mirror_url if mirror_url.endswith("/") else mirror_url + "/"
        pkg, eng = p + pkg, p + eng
    return pkg, eng


def speedtest(mirror_urls=None, timeout=7):
    """拿 englib 本体做 Range 探测：小文件测速会骗人，46MB 的包才反映真实链路。"""
    targets = [m for m in MIRRORS
               if mirror_urls is None or m["url"] in mirror_urls]

    def one(m):
        url = (m["url"] + ENGLIB_URL_GH) if m["url"] else ENGLIB_URL_GH
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-65535"})
            opener = urllib.request.build_opener(tunnel_install._NoRedirect)
            r = opener.open(req, timeout=timeout)
            r.read(65536)
            r.close()
            return {"id": m["id"], "label": m["label"], "url": m["url"],
                    "ms": int((time.time() - t0) * 1000), "ok": True}
        except tunnel_install._RedirectCatched:
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


def _safe_extract(path, dest):
    """只允许相对路径，防止 tar 穿越；返回解出的顶层名字集合。"""
    os.makedirs(dest, mode=0o700, exist_ok=True)
    tops = set()
    with tarfile.open(path, "r:*") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"压缩包包含非法路径：{name}")
            tops.add(name.split("/")[0])
        try:
            tf.extractall(dest, filter="data")  # py3.12+
        except TypeError:
            tf.extractall(dest)
    return tops


def _install(t, mirror_url="", version=""):
    with _lock:
        _installing["id"] = t["id"]
    try:
        _install_inner(t, mirror_url, version)
    finally:
        with _lock:
            _installing["id"] = ""


def _install_inner(t, mirror_url, version):
    if not mirror_url:
        core.task_log(t, "未指定镜像，先并发测速（englib 46MB，Range 探测）")
        rows = [r for r in speedtest() if r["ok"]]
        if rows:
            mirror_url = rows[0]["url"]
            core.task_log(t, f"选用 {rows[0]['label']}（{rows[0]['ms']}ms）")
        else:
            core.task_log(t, "所有镜像都不可达，尝试官方源")
    pkg_url, eng_url = build_urls(version, mirror_url)
    core.task_log(t, f"udocker 包：{pkg_url}")

    os.makedirs(INSTALL_DIR, mode=0o700, exist_ok=True)
    tmp_pkg = os.path.join(INSTALL_DIR, "udocker.tar.gz")
    core.task_progress(t, 3)
    try:
        tunnel_install._download(pkg_url, tmp_pkg, t=t, label="udocker",
                                 max_mb=16, deadline=240)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"下载 udocker 失败：{e}。可换镜像重试")
    _safe_extract(tmp_pkg, INSTALL_DIR)
    os.unlink(tmp_pkg)
    # Release 包的顶层是 udocker-<ver>/udocker/，这里统一收敛成 INSTALL_DIR/udocker
    target = os.path.join(INSTALL_DIR, "udocker")
    if not os.path.isdir(target):
        for cand in glob.glob(os.path.join(INSTALL_DIR, "*", "udocker")):
            if os.path.isdir(cand):
                shutil.move(cand, target)
                break
    if not os.path.isdir(target):
        raise ValueError("压缩包结构异常：未找到 udocker 目录")
    binp = udocker_bin()
    if not binp:
        cand = os.path.join(INSTALL_DIR, "udocker", "maincmd.py")
        if os.path.isfile(cand):
            os.chmod(cand, 0o755)
            try:
                os.symlink(cand, os.path.join(INSTALL_DIR, "udocker", "udocker"))
            except OSError:
                pass
        binp = udocker_bin()
    if not binp:
        raise ValueError("udocker 可执行文件缺失")
    core.task_log(t, f"客户端已就位：{binp}（{version_of(binp) or '版本未知'}）")
    core.task_progress(t, 12)

    if englib_ok():
        core.task_log(t, f"englib 已存在（{UDOCKER_DIR}/bin），跳过下载")
        ensure_android_proot(t)
        core.task_progress(t, 100)
        _reset_cache()
        return {"path": binp, "englib": True, "skipped": True}
    core.task_log(t, f"englib：{eng_url}")
    tmp_eng = os.path.join(INSTALL_DIR, ENGLIB_NAME)
    try:
        tunnel_install._download(eng_url, tmp_eng, t=t, label="englib",
                                 max_mb=160, deadline=1800)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"下载 englib 失败：{e}。请换镜像后重试")
    stage = os.path.join(INSTALL_DIR, "englib-stage")
    shutil.rmtree(stage, ignore_errors=True)
    _safe_extract(tmp_eng, stage)
    os.unlink(tmp_eng)
    src = os.path.join(stage, "udocker_dir")
    if not os.path.isdir(src):
        src = stage
    for sub in ("bin", "lib", "doc"):
        s = os.path.join(src, sub)
        if not os.path.isdir(s):
            continue
        d = os.path.join(UDOCKER_DIR, sub)
        os.makedirs(d, mode=0o700, exist_ok=True)
        for name in os.listdir(s):
            sp, dp = os.path.join(s, name), os.path.join(d, name)
            if os.path.isdir(sp):
                shutil.copytree(sp, dp, dirs_exist_ok=True)
            else:
                shutil.copy2(sp, dp)
    for f in glob.glob(os.path.join(UDOCKER_DIR, "bin", "*")):
        try:
            os.chmod(f, 0o755)
        except OSError:
            pass
    shutil.rmtree(stage, ignore_errors=True)
    core.task_progress(t, 100)
    if not englib_ok():
        raise ValueError("englib 解压后未找到 proot，请重装或在终端执行 udocker install")
    core.task_log(t, f"englib 就绪：{UDOCKER_DIR}/bin")
    ensure_android_proot(t)
    _reset_cache()
    return {"path": binp, "englib": True, "skipped": False}


def _reset_cache():
    try:
        import runtime
        runtime._uck["at"] = 0
        runtime._uck["data"] = {}
    except Exception:  # noqa: BLE001
        pass


def _arch_name():
    m = (platform.machine() or os.uname().machine or "").lower()
    if m in ("aarch64", "arm64", "armv8l"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return "arm" if m.startswith("arm") else m


def is_android():
    return bool(os.environ.get("ANDROID_ROOT")
                or "com.termux" in (os.environ.get("PREFIX") or "")
                or os.path.isdir("/system/app"))


def ensure_android_proot(t=None):
    """Android 上把 udocker 自带的 proot 换成 Termux 版。

    udocker 打包的是通用 proot 4.8.0，Android 10 起的 seccomp 策略会把它的
    子进程打成 SIGSYS：容器能 create、能"启动"，但任何命令都静默退出，
    日志里只剩一行 Warning。Termux 的 proot 带 Android 补丁，换过去才正常。
    幂等，可重复执行。
    """
    if not is_android():
        return {"swapped": [], "reason": "not-android"}
    termux_proot = os.path.join(core.PREFIX, "bin", "proot")
    if not os.path.isfile(termux_proot):
        return {"swapped": [], "reason": "termux-proot-missing"}
    bin_dir = os.path.join(UDOCKER_DIR, "bin")
    if not os.path.isdir(bin_dir):
        return {"swapped": [], "reason": "no-bin"}
    arch = _arch_name()
    targets = [os.path.join(bin_dir, f"proot-{arch}")]
    targets += glob.glob(os.path.join(bin_dir, f"proot-{arch}-*"))
    swapped = []
    for p in targets:
        if not os.path.isfile(p) or os.path.islink(p) or p.endswith(".orig"):
            continue
        try:
            os.replace(p, p + ".orig")
            os.symlink(termux_proot, p)
            swapped.append(os.path.basename(p))
        except OSError:
            continue
    if swapped and t:
        core.task_log(t, f"已把 proot 换成 Termux 版：{', '.join(swapped)}")
    return {"swapped": swapped, "reason": "" if swapped else "already"}


def start_install(mirror_url="", version=""):
    if _installing["id"]:
        return _installing["id"]
    t = core.run_job("udocker-install", "安装容器运行时 udocker",
                     lambda task: _install(task, mirror_url, version))
    with _lock:
        _installing["id"] = t["id"]
    return t["id"]


def uninstall():
    """只删客户端目录；~/.udocker（镜像/容器）保留，避免误删数据。"""
    if _installing["id"]:
        raise ValueError("正在安装，请稍后")
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    _reset_cache()
    return {"removed": INSTALL_DIR, "kept": UDOCKER_DIR}
