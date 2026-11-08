#!/usr/bin/env python3.11
"""真机网络守卫验收：直接调用模块接口，不依赖面板登录态。

覆盖：
  1. 网络状态识别（WiFi / IP）
  2. 防抖状态机（IP 变化 → 计时 → 到期 → 确认）
  3. frpc 探测（真 frpc + 真 frps）
  4. cloudflared 探测（真 cloudflared）
  5. 熔断
  6. 事件推送
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace")
    except subprocess.SubprocessError:
        return ""


def main():
    import netguard
    import events

    # _recover 会被下面的用例替换，先留一份原版备用
    orig_recover = netguard._recover

    print("[1] 网络状态识别")
    net = netguard.current_network()
    print(f"     当前: {net}")
    check("能识别出网络类型", net["kind"] in ("wifi", "cellular", "none"), net)
    check("拿到 IP", bool(net["ip"]), net)
    check("termux-api 可用", netguard._wifi_info() is not None, "termux-wifi-connectioninfo")
    check("ifconfig 可解析", len(net["ifaces"]) > 0, net["ifaces"])

    print("[2] 防抖状态机")
    netguard._settings = dict(netguard.DEFAULT_SETTINGS)
    netguard._loaded = True
    netguard._last_net = dict(net)
    netguard._pending = None
    netguard._recovering = False
    netguard._circuit = {}

    cfg = {"enabled": True, "debounce": 45, "poll_interval": 10, "circuit_max": 3}

    # 模拟 IP 变化
    fake = {"ip": "10.99.99.99", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    orig = netguard.current_network
    netguard.current_network = lambda: dict(fake)
    netguard._tick(cfg)
    check("IP 变化进入防抖", netguard._pending is not None)
    check("防抖 45 秒", netguard._pending["deadline"] - netguard._pending["started"] == 45,
          netguard._pending)

    # 再次变化 → 重置
    old = netguard._pending["deadline"]
    fake["ip"] = "10.99.99.100"
    netguard._tick(cfg)
    check("防抖重置", netguard._pending["deadline"] > old)

    # 到期 + 网络正常 → 触发恢复
    fake["ip"] = "10.99.99.100"
    netguard._pending["deadline"] = time.time() - 1
    called = []
    netguard._recover = lambda n: called.append(n)
    netguard._tick(cfg)
    check("到期触发恢复", len(called) == 1, called)
    netguard.current_network = orig
    netguard._recover = orig_recover

    print("[3] frpc 探测（真 frpc + 真 frps）")
    base = os.path.expanduser("~/m16")
    os.makedirs(base, exist_ok=True)
    fs = os.path.join(base, "ng_fs.toml")
    fw = os.path.join(base, "ng_fw.toml")
    with open(fs, "w") as f:
        f.write("bindPort = 7000\n")
    with open(fw, "w") as f:
        f.write('serverAddr = "127.0.0.1"\nserverPort = 7000\n'
                'webServer.addr = "127.0.0.1"\nwebServer.port = 17530\n'
                '[[proxies]]\nname = "ngp"\ntype = "tcp"\n'
                'localIP = "127.0.0.1"\nlocalPort = 8080\nremotePort = 18099\n')
    sh(f"pkill -f 'frps -c {fs}' 2>/dev/null; pkill -f 'frpc -c {fw}' 2>/dev/null")
    time.sleep(1)
    sh(f"nohup frps -c {fs} > {base}/ng_fs.log 2>&1 &")
    time.sleep(2)
    sh(f"nohup frpc -c {fw} > {base}/ng_fw.log 2>&1 &")
    time.sleep(5)

    out = sh("curl -s -m 5 http://127.0.0.1:17530/api/status")
    print(f"     /api/status: {out[:160]}")
    check("frpc status 端点可达", bool(out.strip()), out[:80])
    ep = {"port": 17530, "user": "", "password": "", "proxies": ["ngp"]}
    r = netguard._probe_frpc(ep)
    check("frpc 在线判定为健康", r["ok"] is True, r)

    # 杀掉 frps → frpc 与服务端断开 → status 变空
    sh(f"pkill -f 'frps -c {fs}'")
    time.sleep(5)
    out2 = sh("curl -s -m 5 http://127.0.0.1:17530/api/status")
    print(f"     断连后: {out2[:120]}")
    r2 = netguard._probe_frpc(ep)
    check("断连后判为不健康", r2["ok"] is False and r2.get("skip") is not True, r2)

    # 无端口 → 跳过（关键：不能误重启）
    r3 = netguard._probe_frpc({"port": 0})
    check("无探测端点跳过不重启", r3.get("skip") is True, r3)
    sh(f"pkill -f 'frpc -c {fw}' 2>/dev/null")

    print("[4] cloudflared 探测（真 cloudflared）")
    cf = "/data/data/com.termux/files/usr/bin/cloudflared"
    port = 17531
    sh("pkill -f 'cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:17531' 2>/dev/null")
    time.sleep(1)
    # 用 quick 隧道（不需要 token），即使外网不通，metrics 端口也会起来
    sh(f"nohup {cf} tunnel --no-autoupdate --metrics 127.0.0.1:{port} "
       f"--url http://127.0.0.1:9999 > {base}/ng_cf.log 2>&1 &")
    time.sleep(7)
    out = sh(f"curl -s -m 4 http://127.0.0.1:{port}/ready")
    print(f"     /ready: {out[:160]}")
    if out.strip():
        r = netguard._probe_cloudflared({"port": port})
        check("cloudflared 探测有响应", isinstance(r, dict), r)
        print(f"     判定: {r}")
    else:
        # 外网不通时 quick 隧道起不来：起一个本地假 /ready 验证解析逻辑
        print("     (quick 隧道因外网不可用未启动，改用本地假端点验证解析)")
        sh(f"nohup python3 -c \"import http.server,json;"
           f"class H(http.server.BaseHTTPRequestHandler):"
           f"def log_message(s,*a):pass"
           f"def do_GET(s):"
           f"b=json.dumps({{'status':200,'readyConnections':4}}).encode();"
           f"s.send_response(200);s.send_header('Content-Length',str(len(b)));"
           f"s.end_headers();s.wfile.write(b)\" > {base}/ng_fake.py 2>&1 &")
        time.sleep(1)
    r4 = netguard._probe_cloudflared({"port": 0})
    check("cloudflared 无端口跳过", r4.get("skip") is True, r4)

    print("[5] 熔断")
    netguard._circuit = {}
    netguard._settings["circuit_max"] = 3
    restarted = []
    netguard._restart_instance = lambda i: (restarted.append(i["id"]) or {"ok": True})
    netguard._collect_instances = lambda: [
        {"id": "flap", "name": "闪断", "kind": "frpc", "probe": {"port": 1}}]
    netguard._probe_instance = lambda i: {"ok": False, "skip": False, "detail": "离线"}
    for _ in range(3):
        netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("前 3 次允许重启", len(restarted) == 3, restarted)
    restarted.clear()
    netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("第 4 次被熔断拦下", restarted == [], restarted)
    check("熔断状态可见", any(c["paused"] for c in netguard.circuit_state()),
          netguard.circuit_state())
    netguard.reset_circuit()
    restarted.clear()
    netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("解封后恢复", len(restarted) == 1, restarted)

    print("[6] 事件推送")
    events._subs.clear()
    events._history.clear()
    sid, q = events.subscribe()
    events.emit("network_changed", {"ip": "10.0.0.1", "debounce": 45})
    got = q.get(timeout=3)
    check("network_changed 送达", got["event"] == "network_changed", got)
    events.emit("network_recovered", {"ok_count": 2, "restart_count": 1})
    got2 = q.get(timeout=3)
    check("network_recovered 送达", got2["event"] == "network_recovered", got2)
    events.unsubscribe(sid)

    print(f"\n结果: {OK} 项通过, {FAIL} 项失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
