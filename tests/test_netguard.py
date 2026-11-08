#!/usr/bin/env python3.11
"""netguard 单元测试：防抖状态机、双客户端探测、熔断。

不联网、不碰真设备：网络状态用假的探测函数注入，探测端点用本地
临时 HTTP 服务模拟 frpc 的 /api/status 与 cloudflared 的 /ready。

直接跑：python3 tests/test_netguard.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netguard  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


# ---------------- 假的探测端点 ----------------

class FakeHandler(http.server.BaseHTTPRequestHandler):
    """按 url_path 返回预设内容。"""

    routes = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        entry = self.routes.get(path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        status, body = entry
        if status == 0:          # 模拟连接失败：直接断开
            try:
                self.connection.close()
            except OSError:
                pass
            return
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake(routes):
    FakeHandler.routes = routes
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ---------------- 测试替身 ----------------

class FakeNet:
    """可编程的网络状态源。"""

    def __init__(self):
        self.cur = {"ip": "10.0.0.1", "kind": "wifi", "ssid": "", "ifaces": [], "iface": ""}
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.cur)


# 原始实现，用于恢复被测试替身覆盖掉的模块属性
_ORIG = {
    "current_network": netguard.current_network,
    "_wifi_info": netguard._wifi_info,
    "_ifconfig_ips": netguard._ifconfig_ips,
    "_recover": netguard._recover,
    "_probe_instance": netguard._probe_instance,
    "_restart_instance": netguard._restart_instance,
    "_collect_instances": netguard._collect_instances,
}


def fresh_netguard():
    """重置 netguard 的模块级状态，回到干净起点。

    同时把被上一个用例替换掉的函数还原 —— 否则测试之间会互相污染
    （比如 current_network 变成 FakeNet 后，后面的解析用例全部失真）。
    """
    for k, v in _ORIG.items():
        setattr(netguard, k, v)
    netguard._settings = dict(netguard.DEFAULT_SETTINGS)
    netguard._loaded = True
    netguard._last_net = None
    netguard._pending = None
    netguard._recovering = False
    netguard._circuit = {}
    netguard._last_report = None
    netguard._log_lines = []


def main():
    global OK, FAIL
    tmp = tempfile.mkdtemp(prefix="pp-ng-")
    netguard.SETTINGS_FILE = os.path.join(tmp, "netguard.json")

    # ============ [1] 设置 ============
    print("[1] 设置")
    fresh_netguard()
    s = netguard.settings()
    check("默认防抖 45 秒", s["debounce"] == 45, s)
    check("默认启用", s["enabled"] is True)
    check("默认轮询 10 秒", s["poll_interval"] == 10)
    netguard.save_settings(debounce=10)
    check("防抖下限夹到 30", netguard.settings()["debounce"] == 30,
          netguard.settings())
    netguard.save_settings(debounce=90)
    check("防抖上限夹到 60", netguard.settings()["debounce"] == 60,
          netguard.settings())
    netguard.save_settings(debounce=45)
    check("防抖可设 45", netguard.settings()["debounce"] == 45)
    netguard.save_settings(enabled=False)
    check("可停用", netguard.settings()["enabled"] is False)
    netguard.save_settings(enabled=True)
    check("落盘后重载一致", _reload_ok(netguard.SETTINGS_FILE), "设置文件")

    # ============ [2] 网络状态解析 ============
    print("[2] 网络状态")
    fresh_netguard()
    netguard._wifi_info = lambda: {"ip": "192.168.1.7", "ssid": "Home",
                                   "state": "COMPLETED", "rssi": -50}
    netguard._ifconfig_ips = lambda: [("wlan0", "192.168.1.7")]
    n = netguard.current_network()
    check("识别 WiFi", n["kind"] == "wifi" and n["ip"] == "192.168.1.7", n)

    netguard._wifi_info = lambda: None
    netguard._ifconfig_ips = lambda: [("rmnet_data0", "10.20.30.40")]
    n = netguard.current_network()
    check("识别蜂窝", n["kind"] == "cellular" and n["ip"] == "10.20.30.40", n)

    netguard._ifconfig_ips = lambda: [("lo", "127.0.0.1")]
    n = netguard.current_network()
    check("全断网判定 none", n["kind"] == "none" and n["ip"] == "", n)

    # 蜂窝优先于 WiFi（切 4G 时 WiFi 数据可能还是旧的）
    netguard._wifi_info = lambda: {"ip": "192.168.1.7", "ssid": "Home",
                                    "state": "COMPLETED", "rssi": -50}
    netguard._ifconfig_ips = lambda: [("wlan0", "192.168.1.7"),
                                      ("rmnet_data0", "10.20.30.40")]
    n = netguard.current_network()
    check("蜂窝优先于残留 WiFi", n["kind"] == "cellular", n)

    # ============ [3] 防抖状态机 ============
    print("[3] 防抖状态机")
    fresh_netguard()
    net = FakeNet()
    netguard.current_network = net
    cfg = {"enabled": True, "debounce": 45, "poll_interval": 10, "circuit_max": 3}
    netguard._last_net = dict(net.cur)
    netguard._pending = None

    # 3.1 IP 变化 → 进入防抖
    net.cur = {"ip": "10.0.0.9", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    netguard._tick(cfg)
    check("IP 变化进入防抖", netguard._pending is not None)
    check("防抖时长正确", netguard._pending["deadline"] - netguard._pending["started"] == 45,
          netguard._pending)

    # 3.2 防抖期间再变 → 重置计时
    old_deadline = netguard._pending["deadline"]
    net.cur = {"ip": "10.0.0.10", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    netguard._tick(cfg)
    check("防抖重置", netguard._pending["deadline"] > old_deadline,
          (old_deadline, netguard._pending["deadline"]))
    check("重置次数累加", netguard._pending["resets"] == 2, netguard._pending)

    # 3.3 没到时间不恢复
    netguard._tick(cfg)
    check("未到时不触发恢复", netguard._pending is not None)

    # 3.4 到时间但网络仍断 → 取消
    # 场景：IP 变了开始计时，等稳定期间链路彻底断了（断网本身也算一次变动，
    # 计时器会重置），等到新计时器到期时网络仍然不通 → 取消本次恢复。
    net.cur = {"ip": "", "kind": "none", "ssid": "", "ifaces": [], "iface": ""}
    netguard._tick(cfg)          # 断网 → 重置计时
    netguard._pending["deadline"] = time.time() - 1
    netguard._tick(cfg)          # 到期确认：仍然不通
    check("断网时取消恢复", netguard._pending is None, netguard._pending)

    # 3.5 到时间且网络正常 → 触发恢复
    fresh_netguard()
    net = FakeNet()
    netguard.current_network = net
    netguard._last_net = dict(net.cur)
    net.cur = {"ip": "10.0.0.20", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    netguard._tick(cfg)
    check("变化后进入防抖", netguard._pending is not None)
    netguard._pending["deadline"] = time.time() - 1
    called = []
    netguard._recover = lambda net: called.append(net)
    netguard._tick(cfg)
    check("倒计时结束触发恢复", len(called) == 1, called)
    check("防抖清空", netguard._pending is None)

    # 3.6 倒计时结束 IP 又变了 → 重置而非恢复
    fresh_netguard()
    net = FakeNet()
    netguard.current_network = net
    netguard._last_net = dict(net.cur)
    net.cur = {"ip": "10.0.0.30", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    netguard._tick(cfg)
    netguard._pending["deadline"] = time.time() - 1
    net.cur = {"ip": "10.0.0.31", "kind": "cellular", "ssid": "", "ifaces": [], "iface": ""}
    called = []
    netguard._recover = lambda net: called.append(net)
    netguard._tick(cfg)
    check("倒计时末又变则重置", netguard._pending is not None and not called,
          (netguard._pending, called))

    # ============ [4] frpc 探测 ============
    print("[4] frpc 探测")
    fresh_netguard()
    body = json.dumps({"tcp": [{"name": "p1", "type": "tcp", "status": "running",
                                 "err": "", "local_addr": "127.0.0.1:8080"}]})
    srv, port = start_fake({"/api/status": (200, body)})
    try:
        ep = {"port": port, "user": "", "password": "", "proxies": ["p1"]}
        r = netguard._probe_frpc(ep)
        check("frpc 在线判定", r["ok"] is True, r)
        check("frpc 详情", "在线" in r["detail"], r)

        FakeHandler.routes["/api/status"] = (200, "{}")
        r = netguard._probe_frpc(ep)
        check("frpc 空响应判离线", r["ok"] is False and r.get("skip") is not True, r)

        FakeHandler.routes["/api/status"] = (
            200, json.dumps({"tcp": [{"name": "p1", "status": "offline"}]}))
        r = netguard._probe_frpc(ep)
        check("frpc offline 判离线", r["ok"] is False and r.get("skip") is not True, r)

        FakeHandler.routes["/api/status"] = (0, "")
        r = netguard._probe_frpc(ep)
        check("frpc 连接失败判离线", r["ok"] is False and r.get("skip") is not True, r)

        # 无端口 → 跳过，不当成故障
        r = netguard._probe_frpc({"port": 0})
        check("frpc 无端口跳过", r.get("skip") is True, r)
    finally:
        srv.shutdown()

    # frpc 鉴权：401 属于「探测配置不当」，不该触发重启
    fresh_netguard()
    srv, port = start_fake({"/api/status": (401, "unauthorized")})
    try:
        r = netguard._probe_frpc({"port": port, "user": "u", "password": "p",
                                  "proxies": ["p1"]})
        check("frpc 401 视为跳过", r.get("skip") is True, r)
    finally:
        srv.shutdown()

    # ============ [5] cloudflared 探测 ============
    print("[5] cloudflared 探测")
    fresh_netguard()
    srv, port = start_fake({
        "/ready": (200, json.dumps({"status": 200, "readyConnections": 4,
                                    "connectorId": "abc"}))})
    try:
        r = netguard._probe_cloudflared({"port": port})
        check("cloudflared 就绪判定", r["ok"] is True, r)
        check("连接数在详情里", "4" in r["detail"], r)

        FakeHandler.routes["/ready"] = (
            200, json.dumps({"status": 200, "readyConnections": 0}))
        r = netguard._probe_cloudflared({"port": port})
        check("连接数 0 判离线", r["ok"] is False and r.get("skip") is not True, r)

        FakeHandler.routes["/ready"] = (503, "")
        r = netguard._probe_cloudflared({"port": port})
        check("503 判离线", r["ok"] is False and r.get("skip") is not True, r)

        r = netguard._probe_cloudflared({"port": 0})
        check("无 metrics 端口跳过", r.get("skip") is True, r)
    finally:
        srv.shutdown()

    # ============ [6] 恢复流程：只重启坏的 ============
    print("[6] 恢复流程")
    fresh_netguard()
    restarted = []
    netguard._restart_instance = lambda inst: (
        restarted.append(inst["id"]) or {"ok": True, "detail": "已重启"})
    netguard._collect_instances = lambda: [
        {"id": "good", "name": "好的", "kind": "frpc", "probe": {"port": 1}},
        {"id": "bad", "name": "坏的", "kind": "frpc", "probe": {"port": 2}},
    ]
    netguard._probe_instance = lambda inst: (
        {"ok": True, "detail": "正常"} if inst["id"] == "good"
        else {"ok": False, "detail": "无在线代理", "skip": False})
    netguard._recover({"ip": "10.0.0.5", "kind": "cellular"})
    check("只重启不健康的那个", restarted == ["bad"], restarted)
    rep = netguard._last_report
    check("报告记正常数", rep["ok_count"] == 1, rep)
    check("报告记重启数", rep["restart_count"] == 1, rep)
    check("报告含跳过数", rep.get("skip_count") == 0, rep)

    # 探测不了 → 跳过，绝不重启
    fresh_netguard()
    restarted = []
    netguard._restart_instance = lambda inst: (
        restarted.append(inst["id"]) or {"ok": True})
    netguard._collect_instances = lambda: [
        {"id": "noprob", "name": "无端点", "kind": "frpc", "probe": None}]
    netguard._probe_instance = lambda inst: {"ok": False, "skip": True,
                                             "detail": "无探测端点"}
    netguard._recover({"ip": "10.0.0.5", "kind": "cellular"})
    check("无探测端点不重启", restarted == [], restarted)
    check("计入跳过", netguard._last_report["skip_count"] == 1, netguard._last_report)

    # ============ [7] 熔断 ============
    print("[7] 熔断")
    fresh_netguard()
    restarted = []
    netguard._restart_instance = lambda inst: (
        restarted.append(inst["id"]) or {"ok": True})
    netguard._collect_instances = lambda: [
        {"id": "flap", "name": "闪断", "kind": "frpc", "probe": {"port": 1}}]
    netguard._probe_instance = lambda inst: {"ok": False, "skip": False,
                                             "detail": "离线"}
    # 前 3 次：允许重启
    for _ in range(3):
        netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("前 3 次都重启了", len(restarted) == 3, restarted)
    # 第 4 次：熔断，应跳过
    restarted.clear()
    netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("超阈值后不再重启", restarted == [], restarted)
    check("熔断状态可见", any(c["paused"] for c in netguard.circuit_state()),
          netguard.circuit_state())

    # 解封后恢复
    netguard.reset_circuit()
    restarted.clear()
    netguard._recover({"ip": "1.1.1.1", "kind": "wifi"})
    check("解封后恢复重启", len(restarted) == 1, restarted)

    # 窗口外的旧计数不计入
    fresh_netguard()
    netguard._circuit["old"] = [time.time() - 400] * 5
    check("过期计数不计入", netguard._circuit_count("old") == 0,
          netguard._circuit_count("old"))

    # ============ [8] 事件推送 ============
    print("[8] 事件推送")
    import events
    events._subs.clear()
    events._history.clear()
    sid, q = events.subscribe()
    events.emit("network_changed", {"ip": "10.0.0.1", "debounce": 45})
    got = q.get(timeout=2)
    check("事件送达订阅者", got["event"] == "network_changed", got)
    check("事件带时间戳", got["ts"] > 0, got)
    events.emit("network_recovered", {"ok_count": 2, "restart_count": 1})
    got2 = q.get(timeout=2)
    check("恢复事件送达", got2["event"] == "network_recovered", got2)
    check("恢复事件带计数", got2["data"]["ok_count"] == 2, got2)
    events.unsubscribe(sid)
    check("退订后无订阅者", events.subscriber_count() == 0)

    # 历史补发
    sid2, q2 = events.subscribe()
    h = events.history()
    check("保留历史事件", len(h) >= 2, len(h))
    events.unsubscribe(sid2)

    # ============ [9] 状态视图 ============
    print("[9] 状态视图")
    fresh_netguard()
    netguard._last_net = {"ip": "10.0.0.1", "kind": "wifi"}
    st = netguard.status()
    check("状态含网络信息", st["network"]["ip"] == "10.0.0.1", st)
    check("状态含防抖配置", st["debounce"] == 45, st)
    check("状态含日志数组", isinstance(st["log"], list), st)
    netguard._pending = {"ip": "10.0.0.2", "kind": "cellular",
                         "deadline": time.time() + 30, "started": time.time()}
    st = netguard.status()
    check("防抖剩余秒数", 25 <= st["pending"]["remaining"] <= 30, st["pending"])

    print(f"\n结果: {OK} 项通过, {FAIL} 项失败")
    return 1 if FAIL else 0


def _reload_ok(path):
    """设置是否真的落盘了。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return isinstance(json.load(fh), dict)
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(main())
