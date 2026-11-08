#!/usr/bin/env python3.11
"""tunnel API 集成测试（要求测试实例起在 127.0.0.1:8901）。

用法：
  HOME=/tmp/pp-home PP_PORT=8901 python3.11 -c "import app; app.main()" &
  PP_PASS=初始密码 python3 tests/test_tunnel_api.py
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("PP_BASE", "http://127.0.0.1:8901")
PASS = os.environ.get("PP_PASS", "dcqjbycdc5")

OK, FAIL = 0, 0
JAR = {}


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def req(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method)
    if JAR:
        r.add_header("Cookie", JAR["c"])
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(r, data=data, timeout=25)
    except urllib.error.HTTPError as e:
        resp = e
    sc = resp.headers.get("Set-Cookie")
    if sc:
        JAR["c"] = sc.split(";")[0]
    return json.loads(resp.read().decode() or "{}")


RAW_TOML = """serverAddr = "frp.freefrp.net"
serverPort = 7000

[auth]
token = "freefrp.net"

[[proxies]]
name = "test-ssh"
type = "tcp"
local_ip = "127.0.0.1"
local_port = 8022
remote_port = 26022
"""


def install_fake():
    """假 frpc：-v 出版本；verify 过；运行期常驻（可用文件触杀）。"""
    src = """#!/bin/sh
if [ "$1" = "-v" ]; then echo "0.61.0"; exit 0; fi
if [ "$1" = "verify" ]; then exit 0; fi
echo tunnel-fake-running
T=/tmp/pp-fake-stop
while [ ! -f $T ]; do sleep 1; done
exit 3
"""
    dst = "/tmp/pp-home/frpc"
    with open(dst, "w") as fh:
        fh.write(src)
    os.chmod(dst, 0o755)


def kill_fake():
    open("/tmp/pp-fake-stop", "w").close()


def clear_fake_stop():
    try:
        os.unlink("/tmp/pp-fake-stop")
    except OSError:
        pass


def main():
    login = req("POST", "/api/login", {"username": "admin", "password": PASS})
    check("登录", login.get("token") or login.get("ok") or "error" not in login, login)
    install_fake()
    clear_fake_stop()

    # clients
    j = req("GET", "/api/tunnel/clients", None)
    check("clients: 假 frpc 检出", j["frpc"]["found"] is True and j["frpc"]["version"] == "0.61.0", j)
    check("clients: 面板端口字段", isinstance(j["panel_port"], int))
    check("clients: external 列表", isinstance(j["external"], list))

    # presets
    j = req("GET", "/api/tunnel/presets", None)
    check("presets 含 freefrp", "freefrp" in j["presets"])

    # parse
    j = req("POST", "/api/tunnel/parse", {"text": RAW_TOML})
    check("parse ok", j["ok"] is True, j)
    check("parse 格式识别 toml", j["format"] == "toml")
    check("parse structured 打码", j["structured"]["server"]["auth_token"] == "********", j)
    check("parse verify 走假 frpc", j["verify"]["ok"] is True and not j["verify"]["skipped"], j)
    j = req("POST", "/api/tunnel/parse", {"text": "bad [[ toml"})
    check("parse 坏配置报错", j["ok"] is False and j.get("error"), j)
    j = req("POST", "/api/tunnel/parse", {"text": RAW_TOML.replace("26022", str(int(BASE.rsplit(':', 1)[1])))})
    check("parse 面板端口拦截", any("面板" in e["msg"] for e in j["errors"]), j)

    # 创建（raw）
    j = req("POST", "/api/tunnel/instance",
            {"name": "test-1", "text": RAW_TOML, "mode": "semi",
             "autostart": False, "probe_url": "tcp://127.0.0.1:" + BASE.rsplit(':', 1)[1]})
    check("创建返回 id", bool(j.get("id")), j)
    tid = j["id"]

    # 列表
    j = req("GET", "/api/tunnel/instances", None)
    insts = {i["id"]: i for i in j["instances"]}
    check("列表含新实例", tid in insts)
    check("实例 mode=semi", insts[tid]["mode"] == "semi")

    # 详情
    j = req("GET", f"/api/tunnel/instance/{tid}", None)
    check("详情 raw 打码", "********" in j["raw_masked"]
          and 'token = "freefrp.net"' not in j["raw_masked"], j)
    check("详情 structured 打码", j["structured"]["server"]["auth_token"] == "********")
    check("详情格式 toml", j["format"] == "toml")

    # 启动
    req("POST", f"/api/tunnel/instance/{tid}/start", {})
    time.sleep(1.5)
    j = req("GET", "/api/tunnel/instances", None)
    inst = {i["id"]: i for i in j["instances"]}[tid]
    check("启动后 running", inst["status"] == "running", inst)

    # 拨测（目标是面板自身端口，一定可达）
    j = req("POST", f"/api/tunnel/instance/{tid}/probe", {})
    check("拨测可达", j["ok"] is True, j)

    # structured 更新（running 状态 → 自动重启）
    j = req("GET", f"/api/tunnel/instance/{tid}", None)
    st = j["structured"]
    st["proxies"][0]["remote_port"] = 27022
    j = req("POST", f"/api/tunnel/instance/{tid}", {"struct": st})
    check("structured 更新", j.get("ok") in (True, None, "1"), j)
    j = req("GET", f"/api/tunnel/instance/{tid}", None)
    check("更新后 remote_port 生效", j["structured"]["proxies"][0]["remote_port"] == 27022, j)
    # structured 返回里 token 必须是打码（真实 token 不能出现在 API 响应）
    check("更新后 structured token 打码",
          j["structured"]["server"]["auth_token"] == "********", j)

    time.sleep(1.2)
    j = req("GET", "/api/tunnel/instances", None)
    inst = {i["id"]: i for i in j["instances"]}[tid]
    check("配置更新后自动重启 running", inst["status"] == "running", inst)

    # raw 更新：只带打码 token → 旧 token 回填
    j = req("GET", f"/api/tunnel/instance/{tid}", None)
    j = req("POST", f"/api/tunnel/instance/{tid}", {"text": j["raw_masked"]})
    check("raw 打码更新接受", j.get("ok") in (True, None, "1"), j)

    # 停止
    req("POST", f"/api/tunnel/instance/{tid}/stop", {})
    time.sleep(0.5)
    j = req("GET", "/api/tunnel/instances", None)
    inst = {i["id"]: i for i in j["instances"]}[tid]
    check("停止后 stopped", inst["status"] == "stopped", inst)

    # manual stop 不被 monitor 复活
    time.sleep(7)
    j = req("GET", "/api/tunnel/instances", None)
    inst = {i["id"]: i for i in j["instances"]}[tid]
    check("manual stop 不复活", inst["status"] == "stopped", inst)

    # 意外退出 → monitor 指数退避拉起（轮询 5s + 退避 1s，留足窗口）
    clear_fake_stop()
    req("POST", f"/api/tunnel/instance/{tid}/start", {})
    time.sleep(1.2)
    kill_fake()  # 触发假 frpc 退出（exit 3）
    time.sleep(14)
    j = req("GET", "/api/tunnel/instances", None)
    inst = {i["id"]: i for i in j["instances"]}[tid]
    check("意外退出被拉起或重试", inst["status"] in ("running", "stopped"), inst)
    check("重启计数 >= 1", inst["restarts"] >= 1, inst)

    # 清理：再杀 + 删除
    kill_fake()
    time.sleep(1)
    req("POST", f"/api/tunnel/instance/{tid}/stop", {})
    req("POST", f"/api/tunnel/instance/{tid}/delete", {})
    j = req("GET", "/api/tunnel/instances", None)
    check("删除后不存在", tid not in [i["id"] for i in j["instances"]])

    # render
    j = req("POST", "/api/tunnel/render", {
        "struct": {"server": {"serverAddr": "a.com", "serverPort": 7000, "auth_token": "tk"},
                   "proxies": [{"name": "x", "type": "tcp", "local_port": 22, "remote_port": 6022}]},
        "fmt": "toml"})
    check("render 生成 [auth]", j["ok"] and "[auth]" in j["text"] and 'token = "tk"' in j["text"], j)

    print(f"\n== 结果: {OK} 通过, {FAIL} 失败")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
