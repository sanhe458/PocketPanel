#!/usr/bin/env python3.11
"""gateway / gateway_ir / secretbox 单元测试。

不联网、不建容器、不需要真上游：协议转换用合成的 SSE，存储用临时目录。
直接跑：python3 tests/test_gateway.py
"""
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gateway  # noqa: E402
import gateway_ir as G  # noqa: E402
import secretbox  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def setup_store():
    d = tempfile.mkdtemp(prefix="pp-gw-")
    gateway.DATA_DIR = d
    gateway._db_ready = False
    gateway.ensure_ready()
    return d


def sse(objs):
    out = []
    for o in objs:
        if isinstance(o, str):
            out.append(o.encode())
        else:
            out.append(b"data: " + json.dumps(o, ensure_ascii=False).encode() + b"\n\n")
    return out


def sse_events(objs):
    out = []
    for ev, o in objs:
        out.append(f"event: {ev}\n".encode() +
                   b"data: " + json.dumps(o, ensure_ascii=False).encode() + b"\n\n")
    return out


class FakeH:
    def __init__(self, body=b"", headers=None):
        self.rfile = io.BytesIO(body)
        self.headers = headers or {}


def main():
    global OK, FAIL

    print("[1] 加密存储")
    d = tempfile.mkdtemp(prefix="pp-secret-")
    kp = os.path.join(d, "secret.key")
    master = secretbox.load_key(kp)
    check("secret.key 32 字节", len(master) == 32)
    check("secret.key 权限 0600", (os.stat(kp).st_mode & 0o777) == 0o600)
    tok = secretbox.encrypt_str(master, "sk-abc-123")
    check("加解密往返", secretbox.decrypt_str(master, tok) == "sk-abc-123")
    check("同一明文两次密文不同", secretbox.encrypt_str(master, "x") != secretbox.encrypt_str(master, "x"))
    bad = tok[:-2] + ("AA" if not tok.endswith("AA") else "BB")
    try:
        secretbox.decrypt_str(master, bad)
        check("篡改密文应失败", False)
    except ValueError:
        check("篡改密文应失败", True)
    try:
        secretbox.decrypt_str(b"x" * 32, tok)
        check("换 key 应失败", False)
    except ValueError:
        check("换 key 应失败", True)
    check("掩码只留尾部", secretbox.mask("sk-1234567890") == "••••••••7890")
    check("空值掩码为空", secretbox.mask("") == "")

    print("[2] Provider 存储")
    setup_store()
    p = gateway.save_provider({
        "name": "测试上游", "id": "testup", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-plain-secret-123456", "protocols": ["openai_chat"],
        "default_protocol": "openai_chat", "models": ["a", "b=up-b"], "enabled": True,
    })
    check("创建返回掩码", p["key_masked"].endswith("3456") and "plain" not in p["key_masked"])
    raw = open(gateway.db_path(), "rb").read()
    check("DB 内无明文 Key", b"sk-plain-secret-123456" not in raw)
    check("解密后能拿到 Key", gateway.get_provider("testup")["api_key"] == "sk-plain-secret-123456")
    p2 = gateway.save_provider({"name": "测试上游", "id": "testup",
                                "base_url": "http://127.0.0.1:9/v1",
                                "protocols": ["openai_chat"], "models": ["a"],
                                "enabled": True, "api_key": ""})
    check("留空 Key 不覆盖", gateway.get_provider("testup")["api_key"] == "sk-plain-secret-123456")
    gateway.save_provider({"name": "测试上游", "id": "testup",
                           "base_url": "http://127.0.0.1:9/v1",
                           "protocols": ["openai_chat"], "models": ["a"],
                           "enabled": True, "clear_key": True})
    check("clear_key 能清空", gateway.get_provider("testup")["api_key"] == "")
    try:
        gateway.save_provider({"name": "x", "base_url": "ftp://x", "protocols": ["openai_chat"]})
        check("非法 Base URL 被拒", False)
    except gateway.GatewayError as e:
        check("非法 Base URL 被拒", e.code == 400)
    try:
        gateway.save_provider({"name": "x", "base_url": "http://x", "protocols": []})
        check("空协议被拒", False)
    except gateway.GatewayError as e:
        check("空协议被拒", e.code == 400)

    print("[3] 设置与 Token")
    t1 = gateway.rotate_token()
    check("Token 前缀", t1.startswith("sk-pp-"))
    check("Token 落盘加密", b"sk-pp-" not in open(gateway.db_path(), "rb").read())
    check("Token 可读回", gateway.access_token() == t1)
    gateway.save_settings({"max_body_mb": 30, "cross_max_mb": 8, "upstream_timeout": 120})
    s = gateway.settings_view()
    check("设置生效", (s["max_body_mb"], s["cross_max_mb"], s["upstream_timeout"]) == (30, 8, 120))
    try:
        gateway.save_settings({"max_body_mb": 0})
        check("越界设置被拒", False)
    except gateway.GatewayError:
        check("越界设置被拒", True)

    print("[4] 模型路由")
    setup_store()
    gateway.save_provider({"name": "chatonly", "id": "chatonly",
                           "base_url": "http://127.0.0.1:9/v1", "api_key": "k1",
                           "protocols": ["openai_chat"], "models": ["*"], "enabled": True})
    gateway.save_provider({"name": "claudeup", "id": "claudeup",
                           "base_url": "http://127.0.0.1:9", "api_key": "k2",
                           "protocols": ["anthropic"], "models": ["claude-x=claude-real"],
                           "enabled": True})
    r = gateway.resolve("claude-x", "openai_chat")
    check("精确模型优先于 * 兜底", r["provider"]["id"] == "claudeup", r["provider"]["id"])
    check("跨协议需要转换", r["passthrough"] is False and r["protocol"] == "anthropic")
    check("别名映射到上游名", r["upstream_model"] == "claude-real")
    r2 = gateway.resolve("chatonly/whatever", "anthropic")
    check("provider/model 前缀", r2["provider"]["id"] == "chatonly" and r2["upstream_model"] == "whatever")
    r3 = gateway.resolve("gpt-4o", "openai_chat")
    check("裸模型走 * 兜底", r3["provider"]["id"] == "chatonly")
    # 把 chatonly 的 * 去掉，才测得出「真的匹配不到」
    gateway.save_provider({"name": "chatonly", "id": "chatonly",
                           "base_url": "http://127.0.0.1:9/v1", "api_key": "k1",
                           "protocols": ["openai_chat"], "models": ["gpt-4o"], "enabled": True})
    try:
        gateway.resolve("不存在", "openai_chat")
        check("匹配不到应 404", False)
    except gateway.GatewayError as e:
        check("匹配不到应 404", e.code == 404)
    gateway.save_settings({"fallback_provider": "claudeup"})
    r4 = gateway.resolve("any-model", "openai_chat")
    check("兜底 Provider 生效", r4["provider"]["id"] == "claudeup")

    print("[5] 请求体探针")
    head = b'{"model":"gpt-4o","stream":true,"messages":[]}'
    check("探针取 model", gateway._probe_model(head) == "gpt-4o")
    check("探针取 stream", gateway._probe_stream(head) is True)
    patched = gateway._patch_model(head, "gpt-4o", "up-model")
    check("替换 model 保留其余字节", patched.startswith(b'{"model":"up-model"') and patched.endswith(b'"messages":[]}'))
    check("缺 model 时替换返回 None", gateway._patch_model(b'{"x":1}', "a", "b") is None)
    check("model 不变则原样返回", gateway._patch_model(head, "gpt-4o", "gpt-4o") == head)

    print("[6] 请求转换")
    chat_req = {
        "model": "gpt-4o", "stream": False,
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "查一下", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "weather", "arguments": '{"city":"北京"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "weather", "parameters": {"type": "object", "properties": {}}}}],
    }
    ir = G.parse_request("openai_chat", chat_req)
    check("system 提取", ir["system"] == "你是助手")
    check("图片转 base64", ir["messages"][0]["parts"][1].get("data") == "AAAA")
    check("工具调用入 IR", ir["messages"][1]["parts"][1]["input"] == {"city": "北京"})
    check("工具结果 id 对应", ir["messages"][2]["parts"][0]["id"] == "call_1")
    anth = G.build_request("anthropic", ir, "claude")
    check("Anthropic system 独立字段", anth["system"] == "你是助手")
    check("Anthropic tool_use", anth["messages"][1]["content"][1]["type"] == "tool_use")
    check("Anthropic tool_result", anth["messages"][2]["content"][0]["tool_use_id"] == "call_1")
    resp = G.build_request("openai_responses", ir, "gpt")
    check("Responses instructions", resp["instructions"] == "你是助手")
    check("Responses function_call 顺序在文本后",
          [x["type"] for x in resp["input"]] ==
          ["message", "message", "function_call", "function_call_output"])
    gem = G.build_request("gemini", ir, "gem")
    check("Gemini 请求体不带 model/stream", "model" not in gem and "stream" not in gem)
    check("Gemini systemInstruction", gem["systemInstruction"]["parts"][0]["text"] == "你是助手")
    fr = [p for p in gem["contents"][2]["parts"] if "functionResponse" in p][0]
    check("Gemini functionResponse 按 id 找回 name", fr["functionResponse"]["name"] == "weather")

    anth_req = {
        "model": "claude", "max_tokens": 10, "system": [{"type": "text", "text": "sys"}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BB"}}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"x": 1}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "ok"}]}]},
        ],
    }
    ir2 = G.parse_request("anthropic", anth_req)
    out = G.build_request("openai_chat", ir2, "gpt")
    check("Anthropic→Chat system", out["messages"][0] == {"role": "system", "content": "sys"})
    check("Anthropic→Chat tool 消息", out["messages"][3]["role"] == "tool" and
          out["messages"][3]["tool_call_id"] == "toolu_1")
    check("Anthropic→Chat assistant 在 tool 之前", out["messages"][2]["role"] == "assistant" and
          out["messages"][2]["content"] is None)
    check("Anthropic→Chat 工具参数", out["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"x":1}')

    resp_req = {
        "model": "gpt", "instructions": "sys", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_9", "name": "f", "arguments": '{"a":2}'},
            {"type": "function_call_output", "call_id": "call_9", "output": "done"},
        ]}
    ir3 = G.parse_request("openai_responses", resp_req)
    check("Responses input 转 IR", ir3["messages"][0]["parts"][0]["text"] == "hi")
    check("Responses function_call 转 tool_use", ir3["messages"][1]["parts"][0]["id"] == "call_9")
    check("Responses function_call_output 转 tool_result", ir3["messages"][2]["parts"][0]["content"] == "done")

    gem_req = {
        "systemInstruction": {"parts": [{"text": "sys"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "CC"}}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {"a": 3}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"result": "ok"}}}]},
        ],
    }
    ir4 = G.parse_request("gemini", gem_req)
    check("Gemini inlineData 转图片", ir4["messages"][0]["parts"][1]["data"] == "CC")
    tid = ir4["messages"][1]["parts"][0]["id"]
    check("Gemini functionResponse id 闭环", ir4["messages"][2]["parts"][0]["id"] == tid,
          f"{tid} != {ir4['messages'][2]['parts'][0]['id']}")

    print("[7] 响应转换")
    chat_resp = {"id": "c1", "model": "gpt", "choices": [{"message": {
        "role": "assistant", "content": "好的",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "f", "arguments": '{"a":1}'}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}
    rir = G.parse_response("openai_chat", chat_resp)
    check("Chat 响应文本", rir["parts"][0]["text"] == "好的")
    check("Chat 响应工具", rir["parts"][1]["input"] == {"a": 1})
    check("Chat 响应 usage", rir["usage"]["total"] == 7)
    ar = G.build_response("anthropic", rir, "claude")
    check("→ Anthropic stop_reason", ar["stop_reason"] == "tool_use")
    check("→ Anthropic usage", ar["usage"]["input_tokens"] == 3)
    rr = G.build_response("openai_responses", rir, "gpt")
    check("→ Responses output 两项", len(rr["output"]) == 2 and rr["output"][1]["type"] == "function_call")
    gr = G.build_response("gemini", rir, "gem")
    check("→ Gemini functionCall", gr["candidates"][0]["content"]["parts"][1]["functionCall"]["args"] == {"a": 1})
    check("错误体 Anthropic", G.error_payload("anthropic", 413, "x")["type"] == "error")
    check("错误体 Gemini", G.error_payload("gemini", 413, "x")["error"]["status"] == "INVALID_ARGUMENT")

    print("[8] 流式转换")
    chat_stream = sse([
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"content": "你"}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"content": "好"}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_9", "type": "function",
             "function": {"name": "f", "arguments": ""}}]}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"a":'}}]}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '1}'}}]}}]},
        {"id": "c1", "model": "gpt", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"id": "c1", "model": "gpt", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        "data: [DONE]\n\n",
    ])
    ev = list(G.parse_stream("openai_chat", chat_stream))
    check("Chat 流文本", "".join(e.get("text", "") for e in ev if e["type"] == "text") == "你好")
    check("Chat 流工具参数分片", "".join(e.get("text", "") for e in ev if e["type"] == "tool_args") == '{"a":1}')
    check("Chat 流 finish", [e["reason"] for e in ev if e["type"] == "finish"] == ["tool_calls"])

    anth_out = b"".join(G.build_stream("anthropic", iter(ev), "claude"))
    check("Chat→Anthropic 有 message_start", b"event: message_start" in anth_out)
    check("Chat→Anthropic 有 input_json_delta", b"input_json_delta" in anth_out and b'"partial_json":"{\\"a\\":' in anth_out)
    ev2 = list(G.parse_stream("anthropic", anth_out.splitlines(True)))
    check("Anthropic 流回解文本", "".join(e.get("text", "") for e in ev2 if e["type"] == "text") == "你好")
    check("Anthropic 流回解工具", "".join(e.get("text", "") for e in ev2 if e["type"] == "tool_args") == '{"a":1}')
    check("Anthropic 流回解 stop", [e["reason"] for e in ev2 if e["type"] == "finish"] == ["tool_calls"])

    chat_out = b"".join(G.build_stream("openai_chat", iter(ev), "gpt"))
    check("→ Chat 流有 [DONE]", chat_out.rstrip().endswith(b"data: [DONE]"))
    check("→ Chat 流工具名", b'"name":"f"' in chat_out and b'"id":"call_9"' in chat_out)
    resp_out = b"".join(G.build_stream("openai_responses", iter(ev), "gpt"))
    check("→ Responses 有 response.completed", b"event: response.completed" in resp_out)
    check("→ Responses 有函数参数增量", b"response.function_call_arguments.delta" in resp_out)
    gem_out = b"".join(G.build_stream("gemini", iter(ev), "gem"))
    check("→ Gemini 有 functionCall", b"functionCall" in gem_out and b'"name":"f"' in gem_out)

    anth_stream = sse_events([
        ("message_start", {"type": "message_start", "message": {
            "id": "m1", "model": "claude", "usage": {"input_tokens": 2}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "hi"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "tool_use", "id": "toolu_1",
                                                   "name": "f", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"a":2}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ])
    ev3 = list(G.parse_stream("anthropic", anth_stream))
    check("Anthropic 流工具 id", [e["id"] for e in ev3 if e["type"] == "tool_start"] == ["toolu_1"])
    check("Anthropic 流 usage 合并", [e["usage"]["input"] for e in ev3 if e["type"] == "usage"][-1] == 2)

    resp_stream = sse_events([
        ("response.created", {"type": "response.created", "response": {"id": "r1", "model": "gpt"}}),
        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                        "item": {"type": "function_call", "call_id": "call_5",
                                                 "name": "f", "arguments": ""}}),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                                    "output_index": 0, "delta": '{"x":'}),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                       "item": {"type": "function_call"}}),
        ("response.completed", {"type": "response.completed", "response": {
            "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2,
                                             "total_tokens": 3}}}),
    ])
    ev4 = list(G.parse_stream("openai_responses", resp_stream))
    check("Responses 流工具 id", [e["id"] for e in ev4 if e["type"] == "tool_start"] == ["call_5"])

    gem_stream = sse([
        {"candidates": [{"content": {"parts": [{"text": "双"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "子"}]}}]},
        {"candidates": [{"content": {"parts": [{"functionCall": {"name": "f", "args": {"k": 1}}}]},
                         "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3}},
    ])
    ev5 = list(G.parse_stream("gemini", gem_stream))
    check("Gemini 流文本", "".join(e.get("text", "") for e in ev5 if e["type"] == "text") == "双子")
    check("Gemini 流工具", [e["name"] for e in ev5 if e["type"] == "tool_start"] == ["f"])

    print("[9] BodyReader")
    body = b'{"model":"x","a":1}'
    h = FakeH(body, {"Content-Length": str(len(body))})
    br = gateway.BodyReader(h, 1024)
    head = br.probe(4)
    check("探针只读 4 字节", head == b'{"mo')
    br.drop_pending()  # 透传语义：head 已取出，由调用方改名后重发
    rest = b"".join(br.iter_rest())
    check("探针 + 剩余等于全文", head + rest == body)
    check("字节计数正确", br.consumed == len(body))

    h1b = FakeH(body, {"Content-Length": str(len(body))})
    br1b = gateway.BodyReader(h1b, 1024)
    br1b.probe(4)
    check("转换语义下 read_all 含探针字节", br1b.read_all() == body)

    h2 = FakeH(b"5\r\nhello\r\n0\r\n\r\n", {"Transfer-Encoding": "chunked"})
    br2 = gateway.BodyReader(h2, 1024)
    check("chunked 读取", br2.read_all() == b"hello")
    h3 = FakeH(b"x" * 100, {"Content-Length": "100"})
    br3 = gateway.BodyReader(h3, 10)
    try:
        br3.probe(100)
        check("超限应 413", False)
    except gateway.BodyTooLarge as e:
        check("超限应 413", e.code == 413)

    print("[10] 用量统计")
    setup_store()
    gateway.record_usage("p1", "openai_chat", True, {"input": 10, "output": 5})
    gateway.record_usage("p1", "openai_chat", False, None)
    rep = gateway.usage_report(2)
    check("按天聚合", rep["today"]["calls"] == 2 and rep["today"]["in_tok"] == 10)
    check("失败计数", rep["today"]["err"] == 1)
    check("按 Provider 聚合", rep["by_provider"][0]["provider"] == "p1")
    check("天数补齐", len(rep["days"]) == 2)

    print("[11] 用量嗅探")
    sn = gateway.UsageSniffer()
    sn.feed(b'data: {"usage":{"prompt_tokens":12,"completion_tokens":34}}\n\n')
    u = sn.result()
    check("OpenAI usage 嗅探", u["input"] == 12 and u["output"] == 34 and u["total"] == 46)
    sn2 = gateway.UsageSniffer()
    sn2.feed(b'{"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":4}}\n')
    check("Gemini usage 嗅探", sn2.result()["input"] == 3 and sn2.result()["output"] == 4)

    print("[12] 统计口径")
    check("协议别名", G.canon_protocol("claude") == "anthropic" and G.canon_protocol("responses") == "openai_responses")
    check("stop 映射", G._chat_finish("tool_calls") == "tool_calls" and G._anthropic_finish("length") == "max_tokens")
    check("Gemini 请求不带 model", "model" not in G.build_request("gemini", G.parse_request("gemini", {
        "contents": [{"role": "user", "parts": [{"text": "x"}]}]}), "m"))


if __name__ == "__main__":
    main()
    print(f"\n{OK} 项通过，{FAIL} 项失败")
    sys.exit(1 if FAIL else 0)
