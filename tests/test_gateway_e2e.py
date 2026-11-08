#!/usr/bin/env python3
"""M16 AI 网关端到端验收：真起面板 + 假上游，跑完整协议链路。

单测（test_gateway.py）覆盖的是纯函数与数据结构；这份脚本覆盖的是
「HTTP 进 → 网关路由 → 上游 → 响应回来」的真实链路，包括：
  - 访问 Token 鉴权（401 + WWW-Authenticate）
  - OpenAI Chat 流式**字节级透传**（含 [DONE]）
  - Anthropic → OpenAI Chat 跨协议转换（含工具调用分片）
  - OpenAI Responses 流式事件
  - Gemini generateContent
  - 模型清单
  - 20MB Base64 图片透传的内存占用
  - 15MB 跨协议 / 60MB 同协议的 413 与响应体完整性
  - 客户端中途取消 → 上游连接被关闭
  - 用量统计落库

用法：
    python3 tests/test_gateway_e2e.py
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL_PORT = int(os.environ.get("PP_E2E_PORT", "18099"))
UP_PORT = int(os.environ.get("PP_E2E_UP_PORT", "19101"))
PANEL = f"http://127.0.0.1:{PANEL_PORT}"
UP = f"http://127.0.0.1:{UP_PORT}"
PANEL_PASS = "e2e-local-test"

ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("  ok  " if cond else "  FAIL ") + name
          + (("   " + str(extra)[:300]) if (extra and not cond) else ""), flush=True)
    if cond:
        ok += 1
    else:
        fails.append(name)


def wait_port(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def write_config(home):
    """造一个已知密码的配置，免去猜随机初始密码。"""
    import hashlib
    d = os.path.join(home, "pocketpanel", "data")
    os.makedirs(d, exist_ok=True)
    salt = "0123456789abcdef"
    cfg = {"username": "admin", "salt": salt,
           "hash": hashlib.sha256((salt + ":" + PANEL_PASS).encode()).hexdigest()}
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(cfg, f)
    return d


def api(path, obj=None, method=None, cookie=None):
    url = PANEL + path
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as f:
            raw = f.read().decode("utf-8", "replace")
            return f.status, raw, f.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Set-Cookie") or ""


def gw(path, body=None, headers=None, method="POST", timeout=180):
    c = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    payload = body if isinstance(body, bytes) else (
        json.dumps(body).encode() if body is not None else None)
    c.request(method, path, payload, h)
    r = c.getresponse()
    data = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, hdrs, data


def rss_kb(pid=None):
    """读面板进程的 RSS。pid 已知就直接用，否则用 pgrep 兜底。"""
    if not pid:
        out = subprocess.run(["pgrep", "-f", "[p]ython3 app.py"],
                             capture_output=True, text=True).stdout.split()
        if not out:
            return -1
        pid = out[0]
    try:
        with open("/proc/%s/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return -1
    return -1


def up_stats():
    with urllib.request.urlopen(UP + "/stats", timeout=20) as f:
        return json.loads(f.read().decode())


def main():
    home = tempfile.mkdtemp(prefix="pp-gw-e2e-")
    up_proc = panel_proc = None
    try:
        write_config(home)
        env = dict(os.environ, HOME=home, PP_PORT=str(PANEL_PORT))

        up_proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_upstream.py"),
                                    str(UP_PORT)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not wait_port(UP_PORT):
            print("假上游没起来")
            return 1

        panel_proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                                      env=env, cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not wait_port(PANEL_PORT):
            print("面板没起来")
            return 1
        time.sleep(1.2)

        # ---- 登录 + 配 Provider + 发 Token ----
        st, body, ck = api("/api/login", {"username": "admin", "password": PANEL_PASS})
        cookie = ck.split(";")[0]
        check("面板登录", st == 200 and cookie, st)
        st, body, _ = api("/api/ai/providers", {
            "name": "假上游", "id": "fakeup", "base_url": f"{UP}/v1",
            "api_key": "sk-fake-local-123456", "protocols": ["openai_chat"],
            "models": ["*"], "enabled": True}, cookie=cookie)
        check("创建 Provider", st == 200, body[:200])
        st, body, _ = api("/api/ai/token", {}, cookie=cookie)
        token = (json.loads(body) or {}).get("access_token") if st == 200 else ""
        check("生成访问 Token", bool(token and token.startswith("sk-pp-")), body[:120])
        auth = {"Authorization": "Bearer " + (token or "")}

        print("[1] 访问控制")
        c = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=30)
        c.request("POST", "/v1/chat/completions", b'{"model":"x","messages":[]}',
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        check("无 Token 返回 401", r.status == 401, r.status)
        check("401 带 WWW-Authenticate: Bearer",
              "Bearer" in (r.getheader("WWW-Authenticate") or ""),
              r.getheader("WWW-Authenticate"))
        c.close()

        print("[2] OpenAI Chat 流式透传")
        st, hd, data = gw("/v1/chat/completions",
                           {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                            "stream": True}, auth)
        txt = data.decode("utf-8", "replace")
        check("状态 200", st == 200, st)
        check("content-type 是 SSE", "text/event-stream" in (hd.get("Content-Type") or ""),
              hd.get("Content-Type"))
        check("收到 data: 事件", "data:" in txt, txt[:120])
        check("收到 [DONE]", "[DONE]" in txt, txt[-120:])
        check("原样透传（未解析再序列化）", '"chat.completion.chunk"' in txt, txt[:200])

        print("[3] Anthropic → OpenAI Chat 跨协议转换")
        st, hd, data = gw("/v1/messages",
                           {"model": "claude-3-5-sonnet", "max_tokens": 1024,
                            "messages": [{"role": "user", "content": "北京天气"}],
                            "stream": True}, {"x-api-key": token})
        txt = data.decode("utf-8", "replace")
        check("状态 200", st == 200, st)
        check("有 message_start", "message_start" in txt, txt[:200])
        check("有 content_block_delta", "content_block_delta" in txt)
        check("有 input_json_delta", "input_json_delta" in txt)
        raw = re.findall(r'"partial_json":"((?:[^"\\]|\\.)*)"', txt)
        deltas = [json.loads('"' + x + '"') for x in raw]
        joined = "".join(deltas)
        check("工具参数两片都在", len(deltas) == 2 and joined == '{"city":"杭州"}', deltas)
        check("两片拼起来是合法 JSON", json.loads(joined) == {"city": "杭州"}, joined)
        check("stop_reason=tool_use", "tool_use" in txt)

        print("[4] OpenAI Responses 流式")
        st, hd, data = gw("/v1/responses",
                           {"model": "gpt-4.1",
                            "input": [{"role": "user",
                                       "content": [{"type": "input_text", "text": "hi"}]}],
                            "stream": True}, auth)
        txt = data.decode("utf-8", "replace")
        check("状态 200", st == 200, st)
        check("有 response.created", "response.created" in txt, txt[:200])
        check("有 function_call_arguments.delta", "response.function_call_arguments.delta" in txt)
        check("有 response.completed", "response.completed" in txt)

        print("[5] Gemini generateContent")
        st, hd, data = gw("/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse",
                           {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
                           {"x-goog-api-key": token})
        txt = data.decode("utf-8", "replace")
        check("状态 200", st == 200, st)
        check("返回 functionCall", "functionCall" in txt, txt[:300])

        print("[6] 模型清单")
        st, hd, data = gw("/v1/models", method="GET", headers=auth)
        j = json.loads(data.decode("utf-8", "replace")) if data else {}
        ids = [x.get("id") for x in (j.get("data") or [])]
        check("状态 200", st == 200, st)
        check("列出 provider/model", any("/" in (i or "") for i in ids), ids[:5])

        print("[7] 20MB Base64 图片透传（内存）")
        big = "A" * (20 * 1024 * 1024)
        body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + big}}]}]}).encode()
        before = rss_kb(panel_proc.pid)
        t0 = time.time()
        st, hd, data = gw("/v1/chat/completions", body, auth)
        dt = time.time() - t0
        after = rss_kb(panel_proc.pid)
        check("状态 200", st == 200, st)
        check("内存无明显上涨", after - before < 8 * 1024,
              f"{before}KB -> {after}KB (+{after-before}KB)")
        print(f"      上传 {len(body)/1048576:.1f}MB  耗时 {dt:.2f}s  "
              f"RSS {before}KB -> {after}KB (+{after-before}KB)")

        print("[8] 15MB 跨协议 → 413")
        big15 = "A" * (15 * 1024 * 1024)
        body15 = json.dumps({"model": "claude-3-5-sonnet", "max_tokens": 1024,
                             "messages": [{"role": "user", "content": [
                                 {"type": "text", "text": "看图"},
                                 {"type": "image", "source": {
                                     "type": "base64", "media_type": "image/png",
                                     "data": big15}}]}]}).encode()
        before = rss_kb(panel_proc.pid)
        st, hd, data = gw("/v1/messages", body15, {"x-api-key": token})
        after = rss_kb(panel_proc.pid)
        check("返回 413", st == 413, st)
        check("提示跨协议不支持超大请求体",
              "跨协议转换暂不支持超大请求体" in data.decode("utf-8", "replace"), data[:200])
        check("响应体完整送达（不是空的）", len(data) > 20, len(data))
        check("内存无明显上涨", after - before < 8 * 1024,
              f"{before}KB -> {after}KB (+{after-before}KB)")
        print(f"      上传 {len(body15)/1048576:.1f}MB  RSS {before}KB -> {after}KB "
              f"(+{after-before}KB)")

        print("[9] 60MB 同协议 → 413（超 50MB 上限）")
        big60 = "A" * (60 * 1024 * 1024)
        body60 = json.dumps({"model": "gpt-4o",
                             "messages": [{"role": "user", "content": big60}]}).encode()
        st, hd, data = gw("/v1/chat/completions", body60, auth)
        check("返回 413", st == 413, st)
        check("提示超过 50MB 上限", "50MB 上限" in data.decode("utf-8", "replace"), data[:200])
        check("响应体完整送达（不是空的）", len(data) > 20, len(data))

        print("[10] 中途取消 → 上游连接被关闭")
        before_d = up_stats()["disconnects"]
        c = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=30)
        c.request("POST", "/v1/chat/completions",
                  json.dumps({"model": "gpt-4o",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": True, "slow": True}).encode(),
                  {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        r = c.getresponse()
        r.read(200)
        c.close()
        time.sleep(3)
        after_d = up_stats()["disconnects"]
        check("上游 disconnects 递增", after_d > before_d, f"{before_d} -> {after_d}")

        print("[11] 用量统计落库")
        time.sleep(1)
        st, body, _ = api("/api/ai/usage?days=7", cookie=cookie)
        u = json.loads(body) if body else {}
        total = u.get("total") or {}
        check("统计到调用次数", (total.get("calls") or 0) > 0, total)
        print("      total:", json.dumps(total, ensure_ascii=False))

        print(f"\n=== {ok} 项通过，{len(fails)} 项失败")
        if fails:
            print("失败项：", fails)
        return 1 if fails else 0
    finally:
        for p in (panel_proc, up_proc):
            if p:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
