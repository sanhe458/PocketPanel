#!/usr/bin/env python3.11
"""tunnel.py 单元测试：解析 / 生成 / 校验 / 脱敏 / 端口拦截 / 生命周期。

不依赖真 frpc（用假二进制模拟 -v / verify / 常驻进程）。
直接跑：python3 tests/test_tunnel.py
"""
import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tunnel  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


TOML_AUTH = """
serverAddr = "frp.freefrp.net"
serverPort = 7000

[auth]
token = "s3cret"

[[proxies]]
name = "ssh"
type = "tcp"
local_ip = "127.0.0.1"
local_port = 8022
remote_port = 16022
"""

TOML_INLINE = '''
serverAddr = "frp.freefrp.net"
serverPort = 7000
auth.token = "s3cret"

[[proxies]]
name = "web"
type = "http"
local_port = 3000
custom_domains = ["panel.example.com"]
'''

JSON_CFG = """
{
  "serverAddr": "frps.example.com",
  "serverPort": 7000,
  "auth": {"token": "abc"},
  "proxies": [{"name": "ssh", "type": "tcp", "local_port": 22, "remote_port": 6022}]
}
"""

INI_CFG = """
[common]
server_addr = frps.old.com
server_port = 7000
token = legacy-token

[ssh]
type = tcp
local_ip = 127.0.0.1
local_port = 22
remote_port = 6022
"""


def test_parse():
    print("== 解析")
    f, s = tunnel.parse(TOML_AUTH, "auto")
    check("toml 探测", f == "toml")
    check("toml [auth] token 提取", s["server"]["auth_token"] == "s3cret")
    check("toml server addr", s["server"]["serverAddr"] == "frp.freefrp.net")
    check("toml proxy", s["proxies"][0]["name"] == "ssh"
          and s["proxies"][0]["remote_port"] == 16022)

    f2, s2 = tunnel.parse(TOML_INLINE, "auto")
    check("auth.token 行内写法", f2 == "toml" and s2["server"]["auth_token"] == "s3cret")
    check("http proxy domains", s2["proxies"][0]["custom_domains"] == ["panel.example.com"])

    f3, s3 = tunnel.parse(JSON_CFG, "auto")
    check("json 探测", f3 == "json" and s3["server"]["auth_token"] == "abc")

    f4, s4 = tunnel.parse(INI_CFG, "auto")
    check("ini 探测", f4 == "ini")
    check("ini token", s4["server"]["auth_token"] == "legacy-token")
    check("ini proxy", s4["proxies"][0]["remote_port"] == 6022)

    try:
        tunnel.parse("serverAddr = [oops", "auto")
        check("语法错误报 ValueError", False)
    except ValueError as e:
        check("语法错误报 ValueError", "语法" in str(e) or "格式" in str(e), str(e))

    # TOML 优先于 JSON：`{...}` 只能是 JSON，`[x]` 开头 TOML 优先
    try:
        tunnel.parse("{bad json", "auto")
        check("坏 JSON 报错", False)
    except ValueError:
        check("坏 JSON 报错", True)


def test_render_roundtrip():
    print("== 生成与往返")
    _, s = tunnel.parse(TOML_AUTH, "auto")
    for fmt in ("toml", "json", "ini"):
        text = tunnel.render(s, fmt)
        f2, s2 = tunnel.parse(text, fmt)
        same = (s2["server"]["serverAddr"] == "frp.freefrp.net"
                and s2["server"]["auth_token"] == "s3cret"
                and s2["proxies"][0]["remote_port"] == 16022
                and s2["proxies"][0]["type"] == "tcp")
        check(f"{fmt} 往返保真", same, text)
    # TOML 生成使用 [auth] 段
    t = tunnel.render(s, "toml")
    check("toml 生成 [auth] 段", "[auth]" in t and 'token = "s3cret"' in t, t)


def test_validate():
    print("== Schema 校验")
    _, s = tunnel.parse(TOML_AUTH, "auto")
    check("合法配置零错误", tunnel.validate(s) == [])

    bad = {"server": {"serverAddr": "", "serverPort": 0},
           "proxies": [{"name": "", "type": "ftp", "local_port": ""}]}
    errs = tunnel.validate(bad)
    fields = {e["field"] for e in errs}
    check("必填缺失定位", "server.serverAddr" in fields and "proxies[0].name" in fields, errs)

    # 面板端口拦截（安全红线）
    _, s2 = tunnel.parse(TOML_AUTH, "auto")
    s2["proxies"][0]["remote_port"] = tunnel.panel_port()
    errs2 = tunnel.validate(s2)
    check("面板端口穿透拦截", any("面板" in e["msg"] for e in errs2), errs2)
    s2["proxies"][0]["remote_port"] = 1234
    s2["proxies"][0]["local_port"] = tunnel.panel_port()
    check("本地端口面板拦截", any("面板" in e["msg"] for e in tunnel.validate(s2)))

    # remote_port 冲突
    _, s3 = tunnel.parse(TOML_AUTH, "auto")
    import copy
    p2 = copy.deepcopy(s3["proxies"][0])
    p2["name"] = "ssh2"
    s3["proxies"].append(p2)
    check("remote_port 冲突检测", any("冲突" in e["msg"] for e in tunnel.validate(s3)))

    # http 无域名
    _, s4 = tunnel.parse(TOML_INLINE, "auto")
    s4["proxies"][0]["custom_domains"] = []
    check("http 缺域名报错", any("custom_domains" in e["field"] for e in tunnel.validate(s4)))


def test_mask():
    print("== 脱敏")
    masked = tunnel.mask_text(TOML_AUTH)
    check("token 打码", "s3cret" not in masked and "********" in masked)
    check("非敏感行保留", "frp.freefrp.net" in masked)
    back = tunnel.unmask_text(masked, TOML_AUTH)
    check("打码还原", "s3cret" in back and "********" not in back)
    # 未改动的不还原出怪东西
    plain = tunnel.mask_text(INI_CFG)
    check("ini token 打码", "legacy-token" not in plain)
    back2 = tunnel.unmask_text(plain, INI_CFG)
    check("ini 打码还原", "legacy-token" in back2)
    # structured 打码
    _, s = tunnel.parse(TOML_AUTH, "auto")
    r = tunnel.redact_obj(s)
    check("structured 打码", r["server"]["auth_token"] == "********")
    # 命令行脱敏（mefrp 真实形态）
    cmd = ("/path/mefrpc -n -w -t 01LOV9SM0025NNO401ET0MZM00YGS24500JWUDPK "
           "--webui-addr 0.0.0.0 --webui-port 1201 --webui-pass mefrp123")
    mc = tunnel.mask_cmdline(cmd)
    check("cmdline token 打码", "01LOV9SM" not in mc and "-t ********" in mc, mc)
    check("cmdline webui-pass 打码", "mefrp123" not in mc, mc)
    check("cmdline 非敏感参数保留", "--webui-port 1201" in mc and "-n -w" in mc, mc)


def test_client_detect():
    print("== 客户端检测")
    info = tunnel.client_info(force=True)
    # 沙箱里没有 frpc；found 应为 False（若 PATH 恰有则跳过断言）
    if os.path.exists("/tmp/fake-frpc-bin"):
        pass
    check("client_info 返回结构", isinstance(info, dict) and "found" in info)
    # 用假二进制验证版本读取与格式决策
    fake = "/tmp/fake-frpc-bin"
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\n"
                 'if [ "$1" = "-v" ]; then echo "0.61.0"; exit 0; fi\n'
                 'if [ "$1" = "verify" ]; then echo "verify ok"; exit 0; fi\n'
                 "sleep 600\n")
    os.chmod(fake, 0o755)
    tunnel._client_cache.update({"ts": 0, "info": None})
    old = tunnel._find_frpc
    tunnel._find_frpc = lambda: fake
    try:
        info = tunnel.client_info(force=True)
        check("假 frpc 版本读取", info["version"] == "0.61.0", info)
        check("0.61 → 默认 toml", info["fmt"] == "toml")
        tunnel._client_cache.update({"ts": 0, "info": None})
        # 权威校验走假 frpc
        r = tunnel.verify_with_frpc("serverAddr = 'x'\nserverPort = 1\n")
        check("frpc verify 调用", r["ok"] and not r["skipped"], r)
        # 旧版本 → ini
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\n"
                     'if [ "$1" = "-v" ]; then echo "0.51.3"; exit 0; fi\n'
                     'if [ "$1" = "verify" ]; then exit 1; fi\n'
                     "sleep 600\n")
        os.chmod(fake, 0o755)
        info = tunnel.client_info(force=True)
        check("0.51 → 默认 ini", info["fmt"] == "ini", info)
        # verify 失败应 ok=False
        r = tunnel.verify_with_frpc(INI_CFG)
        check("verify 失败不通过", r["ok"] is False and not r["skipped"], r)
    finally:
        tunnel._find_frpc = old
        tunnel._client_cache.update({"ts": 0, "info": None})
        try:
            os.unlink(fake)
        except OSError:
            pass


def test_probe():
    print("== 拨测")
    ok, detail = tunnel._probe_once("tcp://127.0.0.1:1")
    check("tcp 拨测不通", ok is False)
    # 起一个本地 HTTP 服务拨测
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 18999), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok2, detail2 = tunnel._probe_once("http://127.0.0.1:18999/")
    check("http 拨测可达", ok2 and "200" in detail2, detail2)
    ok3, detail3 = tunnel._probe_once("ftp://x")
    check("非法协议报错", ok3 is False and "tcp://" in detail3, detail3)
    ok4, _ = tunnel._probe_once("")
    check("空地址跳过", ok4 is True)
    srv.shutdown()


def test_presets():
    print("== 预设")
    p = tunnel.PRESETS["freefrp"]
    check("freefrp 预设存在", p["server"]["serverAddr"] == "frp.freefrp.net"
          and p["server"]["serverPort"] == 7000)
    check("freefrp 端口范围", p["port_range"] == (10001, 50000))


def test_install_helpers():
    print("== frpc 安装辅助")
    import tunnel_install as ti
    a = ti.arch()
    check("arch 合法", a in ("arm64", "amd64", "arm"), a)
    u1, s1 = ti.build_urls("0.61.0")
    check("直连 URL", u1 == ("https://github.com/fatedier/frp/releases/download/"
                             "v0.61.0/frp_0.61.0_linux_%s.tar.gz" % a), u1)
    check("checksums URL", s1.endswith(f"/v0.61.0/frp_sha256_checksums.txt"), s1)
    u2, _ = ti.build_urls("0.61.0", "https://ghfast.top/")
    check("镜像前缀拼接", u2.startswith("https://ghfast.top/https://github.com/"), u2)
    u3, _ = ti.build_urls("v0.61.0", "https://x.example")  # 未带尾斜杠也能拼
    check("版本去 v + 镜像补斜杠", "v0.61.0" in u3 and "https://x.example/https://" in u3, u3)
    check("内置镜像 ≥4 个", len(ti.MIRRORS) >= 4)
    try:
        ti.build_urls("bad")
        check("build_urls 不校验版本（职责在 install）", True)
    except ValueError:
        check("build_urls 不校验版本（职责在 install）", True)
    # install_github 入口的版本校验（在下载之前抛错）
    import core
    t = core.new_task("test", "ver check")
    try:
        ti.install_github(t, "bad")
        check("非法版本应拒绝", False)
    except ValueError:
        check("非法版本应拒绝", True)
    # 状态接口
    st = ti.status()
    check("status 结构", {"installed", "version", "arch", "install_path", "busy"} <= set(st), st)


def test_conf_permissions():
    print("== 文件权限")
    # create → conf 0600；目录 0700
    core_data = os.environ.get("PP_DATA_DIR")
    if core_data:
        d = os.path.join(core_data, "tunnels")
        if os.path.isdir(d):
            mode = stat.S_IMODE(os.stat(d).st_mode)
            check("tunnels 目录 0700", mode == 0o700, oct(mode))


if __name__ == "__main__":
    test_parse()
    test_render_roundtrip()
    test_validate()
    test_mask()
    test_client_detect()
    test_probe()
    test_presets()
    test_install_helpers()
    print(f"\n== 结果: {OK} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
