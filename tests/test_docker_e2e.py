#!/usr/bin/env python3
"""容器运行时（M17）端到端验证。

用 tests/fake_udocker.py 顶替真 udocker —— PRoot 要 ptrace，CI 容器里通常没
权限；除此之外全是真代码：udockerd 真起在 TCP 上，面板 HTTP 层真跑，Engine
API 客户端真发请求，ServiceManager 真 fork 进程并保活。

覆盖：
  1. 状态与四条能力边界
  2. 服务注册 → 启动 → 可达 → 停止
  3. 拉镜像（异步任务）→ 列表
  4. 创建容器（bind mount / env / 端口）→ 启动 → 列表 → 日志 → 详情
  5. 端口 <1024、越权目录、面板数据目录、整 HOME 映射的拦截
  6. 停止 → 退出码 143；always 策略下异常退出触发 M09 告警
  7. exec 会话（PTY 桥）
  8. Compose 子集解析与不支持的语法报错
  9. Engine API 直连：/build、/stats 返回 501，/volumes 返回空
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = "/tmp/pp-docker-e2e"
HOME = os.path.join(ROOT, "home")
PREFIX = os.path.join(ROOT, "prefix")
PORT = 8907
PWD = "e2e-test-pass"
BASE = f"http://127.0.0.1:{PORT}"

shutil.rmtree(ROOT, ignore_errors=True)
DATA = os.path.join(HOME, "pocketpanel", "data")
os.makedirs(DATA, mode=0o700, exist_ok=True)
os.makedirs(PREFIX, exist_ok=True)
os.environ["HOME"] = HOME
os.environ["PREFIX"] = PREFIX          # 没有 var/service → ServiceManager 走直管适配器

_salt = "e2esalt"
with open(os.path.join(DATA, "config.json"), "w", encoding="utf-8") as _fh:
    json.dump({"port": PORT, "username": "admin", "salt": _salt,
               "hash": hashlib.sha256((_salt + ":" + PWD).encode()).hexdigest(),
               "init_pwd": PWD, "created_at": int(time.time())}, _fh)

# 先把 Engine API 端口写成 2398（第 3 段再改成 2399，顺带验证「端口可改」）。
# 不能留默认的 2375：万一这台机器上已经有别的 udockerd 在跑，事件监控线程
# 会连上「别人」，容器崩了也收不到事件，告警断言就会偶发失败。
with open(os.path.join(DATA, "docker.json"), "w", encoding="utf-8") as _fh:
    json.dump({"port": 2398}, _fh)

# 把假客户端摆到面板「一键安装」的落地位置，_find_udocker() 才能自动发现
_ud_dir = os.path.join(DATA, "runtime", "udocker", "udocker")
os.makedirs(_ud_dir, exist_ok=True)
shutil.copy2(os.path.join(APP_DIR, "tests", "fake_udocker.py"),
             os.path.join(_ud_dir, "udocker"))
os.chmod(os.path.join(_ud_dir, "udocker"), 0o755)

sys.path.insert(0, APP_DIR)
import app  # noqa: E402
import core  # noqa: E402
import dockerclient  # noqa: E402
import messages  # noqa: E402
import runtime  # noqa: E402
import svc  # noqa: E402

COOKIE = {"v": ""}
FAILED = []
PASSED = 0


def ok(cond, label, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {label}")
        return True
    FAILED.append(label + (f" — {extra}" if extra else ""))
    print(f"  ✗ {label}" + (f" — {extra}" if extra else ""))
    return False


def http(method, path, body=None, raw=False):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    rq = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and not isinstance(body, bytes):
        rq.add_header("Content-Type", "application/json")
    if COOKIE["v"]:
        rq.add_header("Cookie", COOKIE["v"])
    try:
        with urllib.request.urlopen(rq, timeout=40) as rs:
            payload = rs.read()
            if raw:
                return rs.status, payload
            return rs.status, (json.loads(payload or b"{}"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload
        try:
            return e.code, json.loads(payload or b"{}")
        except ValueError:
            return e.code, {"error": payload.decode("utf-8", "replace")}


def wait_task(tid, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        st, d = http("GET", "/api/exec/" + tid)
        if st == 200 and d.get("status") != "running":
            return d
        time.sleep(0.25)
    return {"status": "timeout", "output": ""}


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    core.cfg_reload()
    svc.init()
    runtime.ensure_monitor()
    srv = serve()

    print("\n[1] 登录与会话")
    st, d = http("POST", "/api/login", {"username": "admin", "password": PWD})
    ok(st == 200, "登录成功", f"{st} {d}")
    # 拿 Set-Cookie 得再发一次，这里直接从响应头取不方便，改为复用服务端会话表
    tok = next(iter(core.SESSIONS))
    COOKIE["v"] = f"pp_token={tok}"

    print("\n[2] 运行时状态与能力边界")
    st, d = http("GET", "/api/docker/status")
    ok(st == 200, "status 200", str(st))
    caps = d.get("capabilities") or {}
    ok(caps.get("network", {}).get("mode") == "host", "边界 1：网络恒为 host")
    ok(caps.get("resources", {}).get("cpu") is False, "边界 2：无 cgroups / 无资源限制")
    ok(caps.get("volumes", {}).get("objects") is False, "边界 3：无 Volume 对象")
    ok(caps.get("build", {}).get("buildkit") is False, "边界 4：无 BuildKit")
    ok((d.get("udocker") or {}).get("found") is True, "自动发现 udocker")
    ok((d.get("daemon") or {}).get("registered") is False, "初始未注册守护进程")
    ok(d.get("home") == HOME, "status.home 供目录选择器起始")

    print("\n[2b] udocker 的启动方式（Termux 的 shebang 坑）")
    # Termux 上 udocker 是 `#!/usr/bin/env python` 的脚本，而 Android 根本没有
    # /usr/bin：平时能跑全靠 termux-exec 的 LD_PRELOAD 改写路径，runit 起的
    # 服务没有这个变量，execve 就 ENOENT —— 报错长成「文件不存在」，实际文件
    # 好好的。所以认出 .py 就改用当前解释器跑，绕开 shebang。
    import udockerd  # noqa: PLC0415

    fake_bin = os.path.join(DATA, "runtime", "udocker", "udocker", "udocker")
    u0 = udockerd.Udocker(fake_bin)
    ok(u0.argv0 == [fake_bin], "普通可执行文件原样调用", u0.argv0)

    py_bin = os.path.join(DATA, "udocker_maincmd.py")
    shutil.copy2(os.path.join(APP_DIR, "tests", "fake_udocker.py"), py_bin)
    u1 = udockerd.Udocker(py_bin)
    ok(u1.argv0 == [sys.executable, os.path.realpath(py_bin)],
       "Python 脚本改用解释器执行", u1.argv0)

    link_bin = os.path.join(DATA, "udocker-link")
    if not os.path.lexists(link_bin):
        os.symlink(py_bin, link_bin)
    u2 = udockerd.Udocker(link_bin)
    ok(u2.argv0[0] == sys.executable and u2.argv0[1].endswith("udocker_maincmd.py"),
       "符号链接也能认出来（真机是 udocker -> maincmd.py）", u2.argv0)

    # env() 在沙箱里不该凭空塞 LD_PRELOAD（没有 termux 的库），也不该崩
    e = u0.env()
    pre = e.get("LD_PRELOAD")
    ok(pre is None or pre.endswith("libtermux-exec-ld-preload.so"),
       "env 不塞不存在的预加载库", pre)
    ok(e.get("TERM") == "dumb", "env 保留 TERM=dumb（udocker 要求）", e.get("TERM"))

    print("\n[3] 注册并启动 udockerd")
    # 换端口：2375 是默认值，真机上可能已经有别的 udockerd 在跑，
    # 用默认端口测会被「其实是别人在答」骗过去
    st, d = http("POST", "/api/docker/settings", {"port": 2399})
    ok(st == 200 and d.get("settings", {}).get("port") == 2399, "Engine API 端口可改")

    st, d = http("POST", "/api/docker/daemon/ensure")
    ok(st == 200, "注册成功", str(d)[:200])
    spec = svc.spec("udockerd")
    ok(bool(spec), "注册表里有 udockerd")
    ok(spec.get("adapter") == "direct", "无 runit 时回落到直管适配器",
       str(spec.get("adapter")))
    ok(os.path.isfile(os.path.join(DATA, "services.json")), "注册表落盘")

    st, d = http("POST", "/api/docker/daemon/start")
    ok(st == 200 and (d.get("reachable") is True), "守护进程可达", str(d)[:200])
    api_ver = d.get("api") or ""
    ok(api_ver.startswith("1.41"), "Engine API 版本 1.41", api_ver)

    st, d = http("GET", "/api/docker/daemon/logs?lines=50")
    ok(st == 200 and isinstance(d.get("lines"), list), "守护进程日志可读")

    print("\n[4] 能力边界在 Engine API 上也是真的")
    c = runtime.client()
    try:
        c.json("POST", "/build")
        ok(False, "/build 应拒绝")
    except dockerclient.EngineError as e:
        ok(e.status == 501, "/build 返回 501", str(e.status))
    try:
        c.json("GET", "/containers/x/stats")
        ok(False, "/stats 应拒绝")
    except dockerclient.EngineError as e:
        ok(e.status == 501, "/stats 返回 501", str(e.status))
    vols = c.json("GET", "/volumes")
    ok((vols.get("Volumes") or []) == [], "/volumes 返回空列表")
    ok(bool(vols.get("Warnings")), "/volumes 附带说明")

    print("\n[5] 拉取镜像（异步任务）")
    st, d = http("POST", "/api/docker/images/pull",
                 {"ref": "alpine:3.19", "platform": "linux/arm64"})
    ok(st == 202 and d.get("id"), "拉取任务已受理", str(d)[:160])
    t = wait_task(d["id"])
    ok(t.get("status") == "success", "拉取任务成功",
       str(t.get("output"))[-300:].replace("\n", " | "))
    st, d = http("GET", "/api/docker/images?refresh=1")
    names = [i.get("name") for i in (d.get("images") or [])]
    ok("alpine:3.19" in names, "镜像列表包含 alpine:3.19", str(names))
    im = next((i for i in d.get("images") or [] if i.get("name") == "alpine:3.19"), {})
    ok(im.get("arch") == "arm64", "镜像架构识别为 arm64", str(im.get("arch")))

    print("\n[6] 创建容器：绑定校验")
    st, d = http("POST", "/api/docker/containers",
                 {"image": "alpine:3.19", "name": "lowport",
                  "ports": [{"host": 80, "container": 80}]})
    ok(st == 400 and "1024" in (d.get("error") or ""),
       "端口 <1024 被拦截且原因可读", str(d.get("error")))

    st, d = http("POST", "/api/docker/containers",
                 {"image": "alpine:3.19", "name": "escape",
                  "mounts": [{"host": "/etc", "container": "/data"}]})
    ok(st == 400 and ("不允许" in (d.get("error") or "") or "范围" in (d.get("error") or "")),
       "越权宿主机目录被拦截", str(d.get("error")))

    st, d = http("POST", "/api/docker/containers",
                 {"image": "alpine:3.19", "name": "dirmap",
                  "mounts": [{"host": DATA, "container": "/data"}]})
    ok(st == 400 and "面板数据" in (d.get("error") or ""),
       "面板数据目录不许映射进容器", str(d.get("error")))

    st, d = http("POST", "/api/docker/containers",
                 {"image": "alpine:3.19", "name": "homemap",
                  "mounts": [{"host": HOME, "container": "/h"}]})
    ok(st == 400 and "HOME" in (d.get("error") or ""),
       "整个 HOME 不许映射进容器", str(d.get("error")))

    st, d = http("POST", "/api/docker/containers", {"image": "nope:1", "name": "noimg"})
    ok(st == 400 and "未拉取" in (d.get("error") or ""),
       "镜像不存在时提示先拉取", str(d.get("error")))

    print("\n[7] 创建并启动容器")
    site = os.path.join(HOME, "site")
    os.makedirs(site, exist_ok=True)
    st, d = http("POST", "/api/docker/containers", {
        "name": "web", "image": "alpine:3.19", "cmd": "",
        "restart": "no", "execmode": "P1", "workdir": "/app", "user": "root",
        "env": [{"key": "TZ", "value": "Asia/Shanghai"},
                {"key": "API_TOKEN", "value": "s3cr3t-value"},
                {"key": "FAKE_RUN_LONG", "value": "1"},
                {"key": "FAKE_RUN_ECHO", "value": "1"}],
        "mounts": [{"host": site, "container": "/data"}],
        "ports": [{"host": 8080, "container": 8080}],
        "start": True,
    })
    ok(st == 201 and d.get("started") is True, "创建并启动", str(d)[:220])
    cid = d.get("id") or ""
    ok(bool(cid), "拿到容器 id")

    st, d = http("GET", "/api/docker/containers?refresh=1")
    cs = d.get("containers") or []
    row = next((x for x in cs if x.get("name") == "web"), {})
    ok(row.get("state") == "running", "列表里状态为 running", str(row.get("state")))
    ok(row.get("execmode") == "P1", "运行模式 P1")
    ok(row.get("mounts") and row["mounts"][0]["container"] == "/data", "挂载已记录")
    ok(row.get("ports") and row["ports"][0]["host"] == 8080, "端口已记录")
    ok(row.get("image") == "alpine:3.19", "镜像名正确")

    print("\n[8] 日志 / 详情 / 敏感值打码")
    time.sleep(0.8)   # 子进程输出要落盘
    st, d = http("GET", f"/api/docker/container/{cid}/logs?tail=200")
    text = "\n".join(d.get("lines") or [])
    ok(st == 200 and "fake-container" in text, "日志有内容", text[-200:])

    st, d = http("GET", f"/api/docker/container/{cid}")
    ok(st == 200, "详情 200", str(st))
    ok(d.get("network") == "host", "详情标注 host 网络", str(d.get("network")))
    ok(d.get("ro_enforced") is False, "详情标注 ro 无法强制")
    ok(d.get("execmode") == "P1", "详情标注执行模式")
    envmap = {e["key"]: e for e in (d.get("env") or [])}
    ok(envmap.get("TZ", {}).get("value") == "Asia/Shanghai", "普通环境变量原样返回")
    ok(envmap.get("API_TOKEN", {}).get("masked") is True, "敏感环境变量已打码")
    ok("s3cr3t" not in json.dumps(d), "响应里不含明文敏感值")

    print("\n[9] exec 进入容器（PTY 桥）")
    st, d = http("POST", f"/api/docker/container/{cid}/exec",
                 {"shell": "sh", "cols": 100, "rows": 30})
    ok(st == 200 and d.get("id"), "exec 会话已建立", str(d)[:200])
    sid = d.get("id") or ""
    ok(any(x.get("id") == sid for x in (http("GET", "/api/term/list")[1].get("sessions") or [])),
       "会话登记在终端管理器里")

    # 真走一遍双向字节流：面板输入 → 容器进程 → 面板输出
    time.sleep(0.6)
    _st, out = http("GET", f"/api/term/{sid}/output?since=0&wait=2")
    seen = base64.b64decode(out.get("data") or "").decode("utf-8", "replace")
    http("POST", f"/api/term/{sid}/input",
         {"data": base64.b64encode(b"hello-exec\n").decode()})
    time.sleep(1.2)
    _st, out2 = http("GET", f"/api/term/{sid}/output?since={out.get('seq', 0)}&wait=3")
    got = seen + base64.b64decode(out2.get("data") or "").decode("utf-8", "replace")
    ok("hello-exec" in got, "exec 通道把输入送进了容器", got[-160:].replace("\n", "⏎"))
    http("POST", f"/api/term/{sid}/input", {"data": base64.b64encode(b"exit\n").decode()})
    time.sleep(0.8)

    print("\n[10] 停止 → 退出码 143")
    st, d = http("POST", f"/api/docker/container/{cid}/stop")
    ok(st == 200, "停止接口 200", str(d)[:160])
    time.sleep(1.2)
    st, d = http("GET", f"/api/docker/container/{cid}")
    ok(d.get("state") == "exited", "状态变为 exited", str(d.get("state")))
    ok(d.get("exit_code") == 143, "SIGTERM 记为 143", str(d.get("exit_code")))

    # 面板每次重连守护进程都会按状态补一次账。用户自己停的容器必须被认出来，
    # 否则每停一次就多一条「异常退出」告警。
    runtime._catch_up(runtime.client())
    titles = [m.get("title") or "" for m in (messages.listing().get("items") or [])]
    ok(not any("web" in t and "异常退出" in t for t in titles),
       "补账不把手动停止误报成异常退出", str(titles)[:160])

    st, d = http("POST", f"/api/docker/container/{cid}/start")
    ok(st == 200, "重新启动 200", str(d)[:160])

    print("\n[10b] 容器在外部被删掉 → 删除接口要能收尾")
    # 真实遇到过的场景：用户在命令行自己 `udocker rm` 过，或者上一次删除
    # 只删掉了目录没删登记表。这时登记表里那条记录会让面板上挂一个点
    # 「删除」就报 500、怎么点都删不掉的幽灵容器。
    st, d = http("POST", "/api/docker/containers", {
        "name": "ghost", "image": "alpine:3.19", "start": False})
    ok(st == 201, "建一个待删的容器", str(d)[:160])
    ghost = (d or {}).get("id") or ""
    gdir = os.path.join(HOME, ".udocker", "containers", "ghost")
    if os.path.islink(gdir):
        os.unlink(gdir)
    else:
        shutil.rmtree(gdir, ignore_errors=True)
    st, d = http("POST", f"/api/docker/container/{ghost}/remove")
    ok(st == 200, "容器已不在时删除返回 200", str(d)[:160])
    st, d = http("GET", "/api/docker/containers")
    ids = [x.get("id") for x in (d.get("items") or [])]
    ok(ghost not in ids, "幽灵记录已从列表清掉", str(ids)[:160])

    print("\n[11] 重启策略 always + 异常退出 → M09 告警")
    st, d = http("POST", "/api/docker/containers", {
        "name": "crasher", "image": "alpine:3.19", "restart": "always",
        "env": [{"key": "FAKE_RUN_EXIT", "value": "7"}],
        "env_note": "", "start": True,
    })
    # 假客户端的退出码钩子走面板 env 传进去会被 udocker 拒（非白名单键名也合法），
    # 直接用容器环境变量即可：FAKE_RUN_EXIT 对 fake_udocker 生效。
    ok(st == 201, "always 容器创建成功", str(d)[:200])
    crash_id = (d or {}).get("id") or ""
    deadline = time.time() + 25
    restarts = 0
    while time.time() < deadline:
        _s, det = http("GET", f"/api/docker/container/{crash_id}")
        restarts = det.get("restarts") or 0
        if restarts >= 2:
            break
        time.sleep(1.0)
    ok(restarts >= 2, "always 策略下反复重启", f"restarts={restarts}")
    # 事件流有 1 秒级延迟，这里轮询等告警落地，别抢跑
    msgs = {"items": []}
    end = time.time() + 15
    while time.time() < end:
        msgs = messages.listing()
        if any("异常退出" in (m.get("title") or "")
               for m in (msgs.get("items") or [])):
            break
        time.sleep(0.5)
    hit = [m for m in (msgs.get("items") or [])
           if m.get("type") == "err" and "crasher" in (m.get("title") or "")]
    ok(bool(hit) and "异常退出" in hit[0]["title"], "异常退出推送了消息中心告警",
       f"items={str(msgs.get('items'))[:120]} last_event={runtime._last_event} "
       f"alert={runtime._alert_at} "
       f"threads={[t.name for t in threading.enumerate()]}")

    # 容器每几秒崩一次，第一条「异常退出」会一直占着 5 分钟冷却。「频繁重启」
    # 必须走自己的节流，否则永远排在冷却后面发不出来。
    flap = {}
    end = time.time() + 25
    while time.time() < end:
        flap = messages.listing()
        if any("频繁重启" in (m.get("title") or "")
               for m in (flap.get("items") or [])):
            break
        time.sleep(1.0)
    ok(any("频繁重启" in (m.get("title") or "") for m in (flap.get("items") or [])),
       "频繁重启单独提示", f"restarts={restarts} "
       f"titles={[m.get('title') for m in (flap.get('items') or [])][:6]}")

    print("\n[11b] 事件空窗补账（守护进程重启 / 面板重启期间崩掉的容器）")
    st, d = http("POST", "/api/docker/containers", {
        "name": "onerun", "image": "alpine:3.19", "restart": "no",
        "env": [{"key": "FAKE_RUN_EXIT", "value": "9"}],
        "env_note": "", "start": True,
    })
    ok(st == 201, "一次性容器创建成功", str(d)[:200])
    one_id = (d or {}).get("id") or ""
    deadline = time.time() + 20
    state = ""
    while time.time() < deadline:
        _s, det = http("GET", f"/api/docker/container/{one_id}")
        state = det.get("state") or ""
        if state == "exited":
            break
        time.sleep(1.0)
    ok(state == "exited", "容器自行退出", state)

    def _gap_count():
        return len([m for m in (messages.listing().get("items") or [])
                    if "onerun" in (m.get("title") or "")
                    and "异常退出" in (m.get("title") or "")])

    # 事件流只活在 udockerd 内存里，守护进程一重启就没了。把「已经报过」的
    # 痕迹抹掉，等价于「监控当时不在线」——此时只能靠状态对账把告警补出来。
    end = time.time() + 12
    while time.time() < end and not _gap_count():
        time.sleep(0.5)          # 先等实时事件落地，避免和补账混在一起数
    base_n = _gap_count()
    runtime._alert_at.pop(one_id, None)
    runtime._catch_up(runtime.client())
    ok(_gap_count() == base_n + 1, "空窗期崩掉的容器仅凭状态也能补出告警",
       f"{base_n} → {_gap_count()}")
    runtime._catch_up(runtime.client())
    runtime._catch_up(runtime.client())
    ok(_gap_count() == base_n + 1, "补账重复执行不刷屏", f"{base_n} → {_gap_count()}")

    print("\n[12] Compose 子集")
    yml = (
        "services:\n"
        "  db:\n"
        "    image: alpine:3.19\n"
        "    command: sleep 600\n"
        "    environment:\n"
        "      - POSTGRES_PASSWORD=${DB_PASS:-changeme}\n"
        "    volumes:\n"
        "      - ./pg:/var/lib/postgresql/data\n"
        "  web:\n"
        "    image: alpine:3.19\n"
        "    depends_on:\n"
        "      - db\n"
        "    ports:\n"
        "      - \"8081:80\"\n"
        "    restart: always\n"
    )
    st, d = http("POST", "/api/docker/compose/preview",
                 {"text": yml, "env": "DB_PASS=hunter2", "project": "demo", "base_dir": ""})
    ok(st == 200, "compose 解析 200", str(d)[:200])
    ok(d.get("order") == ["db", "web"], "按 depends_on 排出启动顺序", str(d.get("order")))
    db = next((s for s in (d.get("services") or []) if s.get("service") == "db"), {})
    ok("hunter2" in json.dumps(db), ".env 插值生效")
    ok((db.get("mounts") or [{}])[0].get("container") == "/var/lib/postgresql/data",
       "相对路径挂载被解析")
    ok(d.get("base_dir", "").startswith(HOME), "工作目录落在 HOME 内", d.get("base_dir"))

    st, d = http("POST", "/api/docker/compose/preview",
                 {"text": "services:\n  a:\n    build: .\n", "project": "x"})
    ok(st == 400 and "build" in (d.get("error") or ""),
       "build 语法明确报错", str(d.get("error")))

    st, d = http("POST", "/api/docker/compose/preview",
                 {"text": "services:\n  a:\n    image: x\n    volumes:\n      - pgdata:/var/lib\n",
                  "project": "x"})
    ok(st == 400 and ("命名卷" in (d.get("error") or "") or "宿主机" in (d.get("error") or "")),
       "命名卷被拒绝并解释原因", str(d.get("error")))

    print("\n[13] 设置与端口校验")
    st, d = http("POST", "/api/docker/settings", {"platform": "linux/amd64"})
    ok(st == 200 and d.get("settings", {}).get("platform") == "linux/amd64", "设置可保存")
    st, d = http("POST", "/api/docker/settings", {"platform": "windows/386"})
    ok(st == 400, "非法平台被拒绝", str(d.get("error")))
    http("POST", "/api/docker/settings", {"platform": "linux/arm64"})

    print("\n[14] 守护进程停止")
    st, d = http("POST", "/api/docker/daemon/stop")
    ok(st == 200, "停止接口 200")
    time.sleep(1.5)
    ok(runtime.client().ping(timeout=1.0) is False, "停止后 Engine API 不可达")
    st, d = http("GET", "/api/docker/status")
    ok((d.get("daemon") or {}).get("reachable") is False, "状态接口反映已停止")

    srv.shutdown()
    print("\n" + "=" * 54)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for f in FAILED:
        print("  ✗", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            svc.action("udockerd", "stop")
        except Exception:  # noqa: BLE001
            pass
