"""四种大模型协议 ⇄ 内部中间表示（IR）。

设计约束：

- **只做协议形状转换，不做语义改写。** 温度、top_p、stop 这类参数原样搬；
  上游不支持的字段该丢就丢，不猜。
- **流式转换必须事件级**。先把上游 SSE 解成 IR 事件（`start` / `text` /
  `tool_start` / `tool_args` / `tool_end` / `usage` / `finish`），再按目标协议
  重新编码。不能让「Anthropic 的 input_json_delta」这种碎片直接漏到 Chat
  客户端——那正是各家客户端解析失败的主要原因。
- **工具调用 id 必须闭环**。Chat/Anthropic/Responses 都有 id；Gemini 的
  `functionCall` 只有 name，所以转进 IR 时合成一个稳定 id，转回 Gemini 时
  再按 id 找回 name，保证 tool_result 不会张冠李戴。
- **对未知字段宽容**。客户端版本比网关新是常态，认不出的块跳过，不报错。

IR 结构：

```
{
  "system": "合并后的系统提示",
  "messages": [{"role": "user"|"assistant", "parts": [part, ...]}],
  "tools": [{"name", "description", "schema"}],
  "tool_choice": {"mode": "auto|none|required|tool", "name": ""},
  "max_tokens": int|None, "temperature": float|None, "top_p": float|None,
  "stop": [str], "stream": bool
}
```

part 类型：`text` / `image` / `tool_use` / `tool_result`。
"""
import json
import re
import time
import uuid

PROTOCOLS = {
    "openai_chat": "OpenAI Chat",
    "openai_responses": "OpenAI Responses",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
}
PROTOCOL_ORDER = list(PROTOCOLS)
PROTOCOL_ALIASES = {
    "openai": "openai_chat", "chat": "openai_chat", "openai-chat": "openai_chat",
    "chat_completions": "openai_chat",
    "responses": "openai_responses", "openai-responses": "openai_responses",
    "claude": "anthropic", "anthropic": "anthropic",
    "gemini": "gemini", "google": "gemini",
}

_STOP_MAP = {
    "stop": "stop", "end_turn": "stop", "end": "stop", "stop_sequence": "stop",
    "length": "length", "max_tokens": "length", "max_output_tokens": "length",
    "tool_calls": "tool_calls", "tool_use": "tool_calls", "function_call": "tool_calls",
    "content_filter": "content_filter", "safety": "content_filter", "refusal": "content_filter",
}


class ConversionError(ValueError):
    """请求/响应结构无法按目标协议表达。"""


def canon_protocol(name):
    n = (name or "").strip().lower()
    return PROTOCOL_ALIASES.get(n, n if n in PROTOCOLS else "")


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _text_of(content):
    """把 content（字符串 / 块数组 / 嵌套对象）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, dict):
        for k in ("text", "content", "value", "result"):
            if k in content:
                return _text_of(content[k])
        return json.dumps(content, ensure_ascii=False)
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t in ("text", "input_text", "output_text", "thinking"):
                out.append(str(b.get("text") or ""))
            elif "text" in b:
                out.append(_text_of(b["text"]))
            elif t == "image":
                out.append("[图片]")
            else:
                out.append(_text_of(b))
    return "\n".join(x for x in out if x)


def _json_obj(raw, default=None):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (int, float, bool)) or raw is None:
        return {} if default is None else default
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else (default if default is not None else {"value": v})
    except (ValueError, TypeError):
        return default if default is not None else {}


def _arg_text(v):
    if isinstance(v, str):
        return v
    if v is None:
        return "{}"
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _image_part(url="", data="", mime=""):
    p = {"type": "image"}
    if data:
        p["data"] = data
        p["mime"] = mime or "image/png"
    if url:
        p["url"] = url
        if url.startswith("data:") and not data:
            m = re.match(r"data:([^;,]+)?;base64,(.*)", url, re.S)
            if m:
                p["mime"] = m.group(1) or "image/png"
                p["data"] = m.group(2)
                p.pop("url", None)
    return p


def _usage(inp=None, out=None, total=None, cached=None):
    i = int(inp or 0)
    o = int(out or 0)
    return {"input": i, "output": o,
            "total": int(total) if total is not None else i + o,
            "cached": int(cached or 0)}


def _merge_usage(a, b):
    if not b:
        return a or {}
    if not a:
        return dict(b)
    out = dict(a)
    for k in ("input", "output", "cached"):
        out[k] = max(int(out.get(k) or 0), int(b.get(k) or 0))
    out["total"] = max(int(out.get("total") or 0),
                       int(b.get("total") or 0),
                       int(out["input"]) + int(out["output"]))
    return out


# =====================================================================
# 请求：下游 → IR
# =====================================================================

def parse_request(proto, obj):
    if not isinstance(obj, dict):
        raise ConversionError("请求体必须是 JSON 对象")
    fn = {
        "openai_chat": _req_chat,
        "openai_responses": _req_responses,
        "anthropic": _req_anthropic,
        "gemini": _req_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    ir = fn(obj)
    ir.setdefault("system", "")
    ir.setdefault("messages", [])
    ir.setdefault("tools", [])
    ir.setdefault("tool_choice", {"mode": "auto", "name": ""})
    ir.setdefault("max_tokens", None)
    ir.setdefault("temperature", None)
    ir.setdefault("top_p", None)
    ir.setdefault("stop", [])
    ir.setdefault("stream", bool(obj.get("stream")))
    return ir


def _chat_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in _as_list(content):
        if isinstance(b, str):
            out.append({"type": "text", "text": b})
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type") or ""
        if t in ("text", "input_text", "output_text"):
            out.append({"type": "text", "text": str(b.get("text") or "")})
        elif t in ("image_url", "input_image", "image"):
            iu = b.get("image_url") or b.get("image") or b.get("url")
            if isinstance(iu, dict):
                iu = iu.get("url") or iu.get("data") or ""
            if iu:
                out.append(_image_part(url=str(iu)))
        elif "text" in b:
            out.append({"type": "text", "text": _text_of(b["text"])})
    return out


def _req_chat(o):
    system, msgs = [], []
    for m in _as_list(o.get("messages")):
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").lower()
        if role in ("system", "developer"):
            txt = _text_of(m.get("content"))
            if txt:
                system.append(txt)
            continue
        if role == "assistant":
            parts = _chat_parts(m.get("content"))
            for tc in _as_list(m.get("tool_calls")):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                parts.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": _json_obj(raw),
                    "arguments_raw": raw if isinstance(raw, str) else None,
                })
            msgs.append({"role": "assistant", "parts": parts})
        elif role in ("tool", "function"):
            msgs.append({"role": "user", "parts": [{
                "type": "tool_result",
                "id": str(m.get("tool_call_id") or m.get("name") or ""),
                "name": str(m.get("name") or ""),
                "content": _text_of(m.get("content")),
                "is_error": False,
            }]})
        else:
            msgs.append({"role": "user", "parts": _chat_parts(m.get("content"))})

    tools = []
    for t in _as_list(o.get("tools")):
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if name:
            tools.append({"name": str(name),
                          "description": str(fn.get("description") or ""),
                          "schema": fn.get("parameters") or {"type": "object", "properties": {}}})

    tc = o.get("tool_choice")
    choice = {"mode": "auto", "name": ""}
    if isinstance(tc, str):
        choice["mode"] = {"required": "required", "any": "required",
                          "none": "none"}.get(tc, "auto")
    elif isinstance(tc, dict):
        typ = tc.get("type")
        fn = tc.get("function") or {}
        if typ == "function" or fn:
            choice = {"mode": "tool", "name": str(fn.get("name") or tc.get("name") or "")}
            if not choice["name"]:
                choice["mode"] = "auto"
        else:
            choice["mode"] = {"required": "required", "none": "none"}.get(typ, "auto")

    return {
        "system": "\n\n".join(system),
        "messages": msgs, "tools": tools, "tool_choice": choice,
        "max_tokens": _int_or_none(o.get("max_completion_tokens") or o.get("max_tokens")),
        "temperature": _float_or_none(o.get("temperature")),
        "top_p": _float_or_none(o.get("top_p")),
        "stop": _stop_list(o.get("stop")),
        "stream": bool(o.get("stream")),
    }


def _req_responses(o):
    system, msgs = [], []
    ins = o.get("instructions")
    if isinstance(ins, str):
        system.append(ins)
    elif isinstance(ins, list):
        system.append(_text_of(ins))

    inp = o.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "parts": [{"type": "text", "text": inp}]})
    else:
        for item in _as_list(inp):
            if not isinstance(item, dict):
                if isinstance(item, str):
                    msgs.append({"role": "user", "parts": [{"type": "text", "text": item}]})
                continue
            typ = item.get("type") or ""
            if typ == "function_call":
                msgs.append({"role": "assistant", "parts": [{
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "input": _json_obj(item.get("arguments")),
                    "arguments_raw": item.get("arguments") if isinstance(item.get("arguments"), str) else None,
                }]})
                continue
            if typ == "function_call_output":
                msgs.append({"role": "user", "parts": [{
                    "type": "tool_result",
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "content": _text_of(item.get("output")),
                    "is_error": False,
                }]})
                continue
            if typ == "reasoning":
                continue
            role = (item.get("role") or "user").lower()
            if role in ("system", "developer"):
                system.append(_text_of(item.get("content")))
                continue
            parts = _responses_parts(item.get("content"))
            msgs.append({"role": "assistant" if role == "assistant" else "user", "parts": parts})

    tools = []
    for t in _as_list(o.get("tools")):
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if name:
            tools.append({"name": str(name),
                          "description": str(fn.get("description") or ""),
                          "schema": fn.get("parameters") or {"type": "object", "properties": {}}})

    tc = o.get("tool_choice")
    choice = {"mode": "auto", "name": ""}
    if isinstance(tc, str):
        choice["mode"] = {"required": "required", "any": "required", "none": "none"}.get(tc, "auto")
    elif isinstance(tc, dict):
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        if name:
            choice = {"mode": "tool", "name": str(name)}
        else:
            choice["mode"] = {"required": "required", "none": "none"}.get(tc.get("type"), "auto")

    return {
        "system": "\n\n".join(x for x in system if x),
        "messages": msgs, "tools": tools, "tool_choice": choice,
        "max_tokens": _int_or_none(o.get("max_output_tokens") or o.get("max_tokens")),
        "temperature": _float_or_none(o.get("temperature")),
        "top_p": _float_or_none(o.get("top_p")),
        "stop": _stop_list(o.get("stop")),
        "stream": bool(o.get("stream")),
    }


def _responses_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in _as_list(content):
        if isinstance(b, str):
            out.append({"type": "text", "text": b})
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type") or ""
        if t in ("input_text", "output_text", "text", "summary_text"):
            out.append({"type": "text", "text": str(b.get("text") or "")})
        elif t in ("input_image", "image_url", "image"):
            u = b.get("image_url") or b.get("url")
            if isinstance(u, dict):
                u = u.get("url") or ""
            if u:
                out.append(_image_part(url=str(u)))
        elif "text" in b:
            out.append({"type": "text", "text": _text_of(b["text"])})
    return out


def _req_anthropic(o):
    system = ""
    sys = o.get("system")
    if isinstance(sys, str):
        system = sys
    elif isinstance(sys, list):
        system = "\n\n".join(_text_of(b) for b in sys if _text_of(b))

    msgs = []
    for m in _as_list(o.get("messages")):
        if not isinstance(m, dict):
            continue
        role = "assistant" if (m.get("role") or "").lower() == "assistant" else "user"
        parts = []
        content = m.get("content")
        if isinstance(content, str):
            parts.append({"type": "text", "text": content})
        else:
            for b in _as_list(content):
                if isinstance(b, str):
                    parts.append({"type": "text", "text": b})
                    continue
                if not isinstance(b, dict):
                    continue
                t = b.get("type") or ""
                if t in ("text", "thinking"):
                    if t == "text":
                        parts.append({"type": "text", "text": str(b.get("text") or "")})
                elif t == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        parts.append(_image_part(data=str(src.get("data") or ""),
                                                 mime=str(src.get("media_type") or "image/png")))
                    elif src.get("type") == "url":
                        parts.append(_image_part(url=str(src.get("url") or "")))
                elif t == "tool_use":
                    parts.append({"type": "tool_use",
                                  "id": str(b.get("id") or ""),
                                  "name": str(b.get("name") or ""),
                                  "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                                  "arguments_raw": None})
                elif t == "tool_result":
                    parts.append({"type": "tool_result",
                                  "id": str(b.get("tool_use_id") or ""),
                                  "name": str(b.get("name") or ""),
                                  "content": _text_of(b.get("content")),
                                  "is_error": bool(b.get("is_error"))})
                elif t == "document":
                    parts.append({"type": "text", "text": _text_of(b.get("source") or b)})
        msgs.append({"role": role, "parts": parts})

    tools = []
    for t in _as_list(o.get("tools")):
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append({"name": str(t["name"]),
                      "description": str(t.get("description") or ""),
                      "schema": t.get("input_schema") or {"type": "object", "properties": {}}})

    tc = o.get("tool_choice") or {}
    choice = {"mode": "auto", "name": ""}
    if isinstance(tc, dict):
        typ = tc.get("type")
        if typ == "tool":
            choice = {"mode": "tool", "name": str(tc.get("name") or "")}
        elif typ == "any":
            choice["mode"] = "required"
        elif typ == "none":
            choice["mode"] = "none"
    elif isinstance(tc, str):
        choice["mode"] = {"any": "required", "none": "none"}.get(tc, "auto")

    return {
        "system": system, "messages": msgs, "tools": tools, "tool_choice": choice,
        "max_tokens": _int_or_none(o.get("max_tokens")),
        "temperature": _float_or_none(o.get("temperature")),
        "top_p": _float_or_none(o.get("top_p")),
        "stop": _stop_list(o.get("stop_sequences")),
        "stream": bool(o.get("stream")),
    }


def _req_gemini(o):
    system = ""
    si = o.get("systemInstruction") or o.get("system_instruction")
    if isinstance(si, dict):
        system = _text_of(si.get("parts") or si.get("content"))
    elif si:
        system = _text_of(si)

    # Gemini 的 functionCall 没有 id，用 name + 序号合成，再按 name 回填 result
    used = {}
    pending = {}
    msgs = []
    for c in _as_list(o.get("contents")):
        if not isinstance(c, dict):
            continue
        role = "assistant" if (c.get("role") or "").lower() in ("model", "assistant") else "user"
        parts = []
        for p in _as_list(c.get("parts")):
            if not isinstance(p, dict):
                continue
            if "text" in p and p.get("text") is not None:
                parts.append({"type": "text", "text": str(p.get("text") or "")})
            elif "inlineData" in p or "inline_data" in p:
                d = p.get("inlineData") or p.get("inline_data") or {}
                parts.append(_image_part(data=str(d.get("data") or ""),
                                         mime=str(d.get("mimeType") or d.get("mime_type") or "image/png")))
            elif "fileData" in p or "file_data" in p:
                d = p.get("fileData") or p.get("file_data") or {}
                parts.append(_image_part(url=str(d.get("fileUri") or d.get("file_uri") or ""),
                                         mime=str(d.get("mimeType") or "")))
            elif "functionCall" in p or "function_call" in p:
                fc = p.get("functionCall") or p.get("function_call") or {}
                name = str(fc.get("name") or "")
                n = used.get(name, 0)
                used[name] = n + 1
                tid = f"call_{name}_{n}"
                pending.setdefault(name, []).append(tid)
                parts.append({"type": "tool_use", "id": tid, "name": name,
                              "input": fc.get("args") if isinstance(fc.get("args"), dict) else {},
                              "arguments_raw": None})
            elif "functionResponse" in p or "function_response" in p:
                fr = p.get("functionResponse") or p.get("function_response") or {}
                name = str(fr.get("name") or "")
                ids = pending.get(name) or []
                tid = ids.pop(0) if ids else f"call_{name}_0"
                resp = fr.get("response")
                txt = _text_of(resp.get("result")) if isinstance(resp, dict) and "result" in resp else _text_of(resp)
                parts.append({"type": "tool_result", "id": tid, "name": name,
                              "content": txt, "is_error": False})
        msgs.append({"role": role, "parts": parts})

    tools = []
    for t in _as_list(o.get("tools")):
        if not isinstance(t, dict):
            continue
        for fd in _as_list(t.get("functionDeclarations") or t.get("function_declarations")):
            if not isinstance(fd, dict) or not fd.get("name"):
                continue
            tools.append({"name": str(fd["name"]),
                          "description": str(fd.get("description") or ""),
                          "schema": fd.get("parameters") or {"type": "object", "properties": {}}})

    choice = {"mode": "auto", "name": ""}
    cfg = ((o.get("toolConfig") or o.get("tool_config") or {})
           .get("functionCallingConfig") or
           (o.get("toolConfig") or o.get("tool_config") or {}).get("function_calling_config") or {})
    mode = (cfg.get("mode") or "AUTO").upper()
    names = cfg.get("allowedFunctionNames") or cfg.get("allowed_function_names") or []
    if mode == "ANY":
        choice = {"mode": "tool", "name": str(names[0])} if names else {"mode": "required", "name": ""}
    elif mode == "NONE":
        choice = {"mode": "none", "name": ""}

    gc = o.get("generationConfig") or o.get("generation_config") or {}
    return {
        "system": system, "messages": msgs, "tools": tools, "tool_choice": choice,
        "max_tokens": _int_or_none(gc.get("maxOutputTokens") or gc.get("max_output_tokens")),
        "temperature": _float_or_none(gc.get("temperature")),
        "top_p": _float_or_none(gc.get("topP") or gc.get("top_p")),
        "stop": _stop_list(gc.get("stopSequences") or gc.get("stop_sequences")),
        "stream": bool(o.get("stream")),
    }


# =====================================================================
# 请求：IR → 上游
# =====================================================================

def build_request(proto, ir, model):
    fn = {
        "openai_chat": _build_chat,
        "openai_responses": _build_responses,
        "anthropic": _build_anthropic,
        "gemini": _build_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    body = fn(ir, model)
    # Gemini 的 model 在 URL 路径里，body 带 model 会被判成未知字段；
    # stream 同理，走 :streamGenerateContent 而不是 body 开关。
    if proto != "gemini":
        body["model"] = model
        if ir.get("stream"):
            body["stream"] = True
    return body


def _build_chat(ir, model):
    msgs = []
    system = (ir.get("system") or "").strip()
    if system:
        msgs.append({"role": "system", "content": system})
    for m in ir.get("messages") or []:
        parts = m.get("parts") or []
        results = [p for p in parts if p.get("type") == "tool_result"]
        uses = [p for p in parts if p.get("type") == "tool_use"]
        content = [p for p in parts if p.get("type") in ("text", "image")]

        handled = False
        # assistant(tool_calls) 必须排在 tool 消息之前，否则上游会拒
        if uses:
            msg = {"role": "assistant", "content": _chat_content_out(content) or None}
            msg["tool_calls"] = [{
                "id": p.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": p.get("name") or "",
                             "arguments": p.get("arguments_raw") or _arg_text(p.get("input"))},
            } for i, p in enumerate(uses)]
            msgs.append(msg)
            handled = True
        if results:
            for p in results:
                msgs.append({"role": "tool",
                             "tool_call_id": p.get("id") or "call_0",
                             "content": p.get("content") or "(空)"})
            # tool 消息不能夹带文本，剩下的内容另起一条 user
            if content:
                c = _chat_content_out(content)
                if c:
                    msgs.append({"role": "user", "content": c})
            handled = True
        if not handled and content:
            c = _chat_content_out(content)
            if c:
                msgs.append({"role": m.get("role") or "user", "content": c})

    body = {"messages": msgs, "model": model}
    _put(body, "max_tokens", ir.get("max_tokens"))
    _put(body, "temperature", ir.get("temperature"))
    _put(body, "top_p", ir.get("top_p"))
    if ir.get("stop"):
        body["stop"] = ir["stop"]
    if ir.get("tools"):
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description") or "",
            "parameters": t.get("schema") or {"type": "object", "properties": {}},
        }} for t in ir["tools"]]
        body["tool_choice"] = _chat_tool_choice(ir.get("tool_choice") or {})
    if ir.get("stream"):
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def _chat_content_out(parts):
    if not parts:
        return ""
    if all(p.get("type") == "text" for p in parts):
        return "\n".join(str(p.get("text") or "") for p in parts)
    out = []
    for p in parts:
        if p.get("type") == "text":
            out.append({"type": "text", "text": str(p.get("text") or "")})
        elif p.get("type") == "image":
            url = p.get("url") or ""
            if not url and p.get("data"):
                url = f"data:{p.get('mime') or 'image/png'};base64,{p['data']}"
            if url:
                out.append({"type": "image_url", "image_url": {"url": url}})
    return out


def _chat_tool_choice(tc):
    mode = tc.get("mode") or "auto"
    if mode == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    if mode == "required":
        return "required"
    if mode == "none":
        return "none"
    return "auto"


def _build_anthropic(ir, model):
    msgs = []
    for m in ir.get("messages") or []:
        role = m.get("role") or "user"
        blocks = []
        for p in m.get("parts") or []:
            t = p.get("type")
            if t == "text":
                if p.get("text") != "":
                    blocks.append({"type": "text", "text": str(p.get("text") or "")})
            elif t == "image":
                src = None
                if p.get("data"):
                    src = {"type": "base64", "media_type": p.get("mime") or "image/png",
                           "data": p["data"]}
                elif p.get("url"):
                    if str(p["url"]).startswith("data:"):
                        m2 = re.match(r"data:([^;,]+)?;base64,(.*)", p["url"], re.S)
                        if m2:
                            src = {"type": "base64", "media_type": m2.group(1) or "image/png",
                                   "data": m2.group(2)}
                    if src is None:
                        src = {"type": "url", "url": p["url"]}
                if src:
                    blocks.append({"type": "image", "source": src})
            elif t == "tool_use":
                blocks.append({"type": "tool_use",
                               "id": p.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                               "name": p.get("name") or "",
                               "input": p.get("input") or {}})
            elif t == "tool_result":
                c = p.get("content") or ""
                blocks.append({"type": "tool_result",
                               "tool_use_id": p.get("id") or "",
                               "content": c if c else "(空)",
                               "is_error": bool(p.get("is_error"))})
        if not blocks:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": blocks})
    if not msgs:
        msgs = [{"role": "user", "content": [{"type": "text", "text": ""}]}]

    body = {"model": model, "messages": msgs,
            "max_tokens": int(ir.get("max_tokens") or 4096)}
    if ir.get("system"):
        body["system"] = ir["system"]
    _put(body, "temperature", ir.get("temperature"))
    _put(body, "top_p", ir.get("top_p"))
    if ir.get("stop"):
        body["stop_sequences"] = ir["stop"]
    if ir.get("tools"):
        body["tools"] = [{"name": t["name"], "description": t.get("description") or "",
                          "input_schema": t.get("schema") or
                          {"type": "object", "properties": {}}} for t in ir["tools"]]
        body["tool_choice"] = _anthropic_tool_choice(ir.get("tool_choice") or {})
    if ir.get("stream"):
        body["stream"] = True
    return body


def _anthropic_tool_choice(tc):
    mode = tc.get("mode") or "auto"
    if mode == "tool" and tc.get("name"):
        return {"type": "tool", "name": tc["name"]}
    if mode == "required":
        return {"type": "any"}
    if mode == "none":
        return {"type": "none"}
    return {"type": "auto"}


def _build_responses(ir, model):
    items = []
    for m in ir.get("messages") or []:
        role = m.get("role") or "user"
        parts = m.get("parts") or []
        content = [p for p in parts if p.get("type") in ("text", "image")]
        uses = [p for p in parts if p.get("type") == "tool_use"]
        results = [p for p in parts if p.get("type") == "tool_result"]

        # Responses 的 input 是「按发生顺序排列的 item」。assistant 一轮里
        # 文本在前、function_call 在后；工具结果属于下一轮 user，先输出。
        if results:
            for p in results:
                items.append({"type": "function_call_output",
                              "call_id": p.get("id") or "",
                              "output": p.get("content") or ""})
        if content:
            blocks = []
            for p in content:
                if p.get("type") == "text":
                    blocks.append({"type": "output_text" if role == "assistant" else "input_text",
                                   "text": str(p.get("text") or "")})
                else:
                    url = p.get("url") or ""
                    if not url and p.get("data"):
                        url = f"data:{p.get('mime') or 'image/png'};base64,{p['data']}"
                    if url:
                        blocks.append({"type": "input_image", "image_url": url})
            if blocks:
                items.append({"type": "message", "role": role, "content": blocks})
        for p in uses:
            items.append({"type": "function_call",
                          "call_id": p.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                          "name": p.get("name") or "",
                          "arguments": p.get("arguments_raw") or _arg_text(p.get("input"))})

    body = {"model": model, "input": items, "store": False}
    if ir.get("system"):
        body["instructions"] = ir["system"]
    _put(body, "max_output_tokens", ir.get("max_tokens"))
    _put(body, "temperature", ir.get("temperature"))
    _put(body, "top_p", ir.get("top_p"))
    if ir.get("tools"):
        body["tools"] = [{"type": "function", "name": t["name"],
                          "description": t.get("description") or "",
                          "parameters": t.get("schema") or
                          {"type": "object", "properties": {}}} for t in ir["tools"]]
        body["tool_choice"] = _responses_tool_choice(ir.get("tool_choice") or {})
    if ir.get("stream"):
        body["stream"] = True
    return body


def _responses_tool_choice(tc):
    mode = tc.get("mode") or "auto"
    if mode == "tool" and tc.get("name"):
        return {"type": "function", "name": tc["name"]}
    if mode == "required":
        return "required"
    if mode == "none":
        return "none"
    return "auto"


def _build_gemini(ir, model):
    id2name = {}
    for m in ir.get("messages") or []:
        for p in m.get("parts") or []:
            if p.get("type") == "tool_use":
                id2name[p.get("id")] = p.get("name") or ""

    contents = []
    for m in ir.get("messages") or []:
        role = "model" if m.get("role") == "assistant" else "user"
        parts = []
        for p in m.get("parts") or []:
            t = p.get("type")
            if t == "text":
                if p.get("text") != "":
                    parts.append({"text": str(p.get("text") or "")})
            elif t == "image":
                if p.get("data"):
                    parts.append({"inlineData": {"mimeType": p.get("mime") or "image/png",
                                                 "data": p["data"]}})
                elif p.get("url"):
                    if str(p["url"]).startswith("data:"):
                        m2 = re.match(r"data:([^;,]+)?;base64,(.*)", p["url"], re.S)
                        if m2:
                            parts.append({"inlineData": {"mimeType": m2.group(1) or "image/png",
                                                         "data": m2.group(2)}})
                    else:
                        parts.append({"fileData": {"mimeType": p.get("mime") or "image/png",
                                                   "fileUri": p["url"]}})
            elif t == "tool_use":
                parts.append({"functionCall": {"name": p.get("name") or "",
                                               "args": p.get("input") or {}}})
            elif t == "tool_result":
                name = p.get("name") or id2name.get(p.get("id")) or "tool"
                txt = p.get("content") or ""
                resp = None
                if txt.strip().startswith("{") and txt.strip().endswith("}"):
                    try:
                        v = json.loads(txt)
                        if isinstance(v, dict):
                            resp = v
                    except ValueError:
                        pass
                parts.append({"functionResponse": {
                    "name": name,
                    "response": resp if resp is not None else {"result": txt},
                }})
        if not parts:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]

    body = {"contents": contents}
    if ir.get("system"):
        body["systemInstruction"] = {"parts": [{"text": ir["system"]}]}
    if ir.get("tools"):
        body["tools"] = [{"functionDeclarations": [{
            "name": t["name"], "description": t.get("description") or "",
            "parameters": _clean_schema(t.get("schema")),
        } for t in ir["tools"]]}]
        body["toolConfig"] = {"functionCallingConfig": _gemini_tool_choice(
            ir.get("tool_choice") or {})}
    gc = {}
    if ir.get("max_tokens"):
        gc["maxOutputTokens"] = int(ir["max_tokens"])
    _put(gc, "temperature", ir.get("temperature"))
    _put(gc, "topP", ir.get("top_p"))
    if ir.get("stop"):
        gc["stopSequences"] = ir["stop"]
    if gc:
        body["generationConfig"] = gc
    return body


def _gemini_tool_choice(tc):
    mode = tc.get("mode") or "auto"
    if mode == "tool" and tc.get("name"):
        return {"mode": "ANY", "allowedFunctionNames": [tc["name"]]}
    if mode == "required":
        return {"mode": "ANY"}
    if mode == "none":
        return {"mode": "NONE"}
    return {"mode": "AUTO"}


def _clean_schema(schema):
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = {}
    for k, v in schema.items():
        if k in ("$schema", "additionalProperties", "strict", "title"):
            continue
        if isinstance(v, dict):
            out[k] = _clean_schema(v)
        elif isinstance(v, list):
            out[k] = [_clean_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    if "type" not in out and "properties" in out:
        out["type"] = "object"
    return out


# =====================================================================
# 响应：上游 → IR
# =====================================================================

def parse_response(proto, obj):
    if not isinstance(obj, dict):
        raise ConversionError("上游返回的不是 JSON 对象")
    fn = {
        "openai_chat": _resp_chat,
        "openai_responses": _resp_responses,
        "anthropic": _resp_anthropic,
        "gemini": _resp_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    return fn(obj)


def _resp_chat(o):
    parts = []
    choices = [c for c in _as_list(o.get("choices")) if isinstance(c, dict)]
    msg = (choices[0].get("message") if choices else {}) or {}
    if not isinstance(msg, dict):
        msg = {}
    c = msg.get("content")
    if isinstance(c, str) and c:
        parts.append({"type": "text", "text": c})
    elif isinstance(c, list):
        parts.extend(_chat_parts(c))
    for tc in _as_list(msg.get("tool_calls")):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        parts.append({"type": "tool_use",
                      "id": str(tc.get("id") or ""),
                      "name": str(fn.get("name") or ""),
                      "input": _json_obj(fn.get("arguments"))})
    fin = (choices[0].get("finish_reason") if choices else "") or "stop"
    return {"id": str(o.get("id") or ""), "model": str(o.get("model") or ""),
            "created": int(o.get("created") or time.time()),
            "parts": parts, "stop": _STOP_MAP.get(fin, fin),
            "usage": _usage_from_chat(o.get("usage"))}


def _resp_anthropic(o):
    parts = []
    for b in _as_list(o.get("content")):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": str(b.get("text") or "")})
        elif t == "tool_use":
            parts.append({"type": "tool_use", "id": str(b.get("id") or ""),
                          "name": str(b.get("name") or ""),
                          "input": b.get("input") if isinstance(b.get("input"), dict) else {}})
    return {"id": str(o.get("id") or ""), "model": str(o.get("model") or ""),
            "created": int(time.time()), "parts": parts,
            "stop": _STOP_MAP.get(str(o.get("stop_reason") or "end_turn"),
                                  str(o.get("stop_reason") or "stop")),
            "usage": _usage_from_anthropic(o.get("usage"))}


def _resp_responses(o):
    parts = []
    for item in _as_list(o.get("output")):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "message":
            for b in _as_list(item.get("content")):
                if isinstance(b, dict) and b.get("type") in ("output_text", "text"):
                    parts.append({"type": "text", "text": str(b.get("text") or "")})
        elif t == "function_call":
            parts.append({"type": "tool_use", "id": str(item.get("call_id") or item.get("id") or ""),
                          "name": str(item.get("name") or ""),
                          "input": _json_obj(item.get("arguments"))})
    status = str(o.get("status") or "completed")
    stop = "length" if status == "incomplete" else "stop"
    if any(p["type"] == "tool_use" for p in parts):
        stop = "tool_calls"
    return {"id": str(o.get("id") or ""), "model": str(o.get("model") or ""),
            "created": int(o.get("created_at") or time.time()), "parts": parts,
            "stop": stop, "usage": _usage_from_responses(o.get("usage"))}


def _resp_gemini(o):
    parts = []
    cands = _as_list(o.get("candidates"))
    cand = cands[0] if cands and isinstance(cands[0], dict) else {}
    content = cand.get("content") or {}
    for p in _as_list(content.get("parts")):
        if not isinstance(p, dict):
            continue
        if "text" in p and p.get("text") is not None:
            parts.append({"type": "text", "text": str(p.get("text") or "")})
        elif "functionCall" in p or "function_call" in p:
            fc = p.get("functionCall") or p.get("function_call") or {}
            parts.append({"type": "tool_use", "id": f"call_{fc.get('name')}_{len(parts)}",
                          "name": str(fc.get("name") or ""),
                          "input": fc.get("args") if isinstance(fc.get("args"), dict) else {}})
    fr = str(cand.get("finishReason") or cand.get("finish_reason") or "STOP")
    stop = "tool_calls" if any(p["type"] == "tool_use" for p in parts) else {
        "STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
        "RECITATION": "content_filter", "OTHER": "stop",
    }.get(fr, "stop")
    return {"id": str(o.get("responseId") or o.get("modelVersion") or ""),
            "model": str(o.get("modelVersion") or ""), "created": int(time.time()),
            "parts": parts, "stop": stop,
            "usage": _usage_from_gemini(o.get("usageMetadata") or o.get("usage_metadata"))}


def _usage_from_chat(u):
    u = u or {}
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or (u.get("prompt_cache_hit_tokens")))
    return _usage(u.get("prompt_tokens"), u.get("completion_tokens"),
                  u.get("total_tokens"), cached)


def _usage_from_anthropic(u):
    u = u or {}
    cached = u.get("cache_read_input_tokens") or u.get("cache_creation_input_tokens")
    return _usage(u.get("input_tokens"), u.get("output_tokens"), None, cached)


def _usage_from_responses(u):
    u = u or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
    return _usage(u.get("input_tokens"), u.get("output_tokens"),
                  u.get("total_tokens"), cached)


def _usage_from_gemini(u):
    u = u or {}
    return _usage(u.get("promptTokenCount") or u.get("prompt_token_count"),
                  u.get("candidatesTokenCount") or u.get("candidates_token_count"),
                  u.get("totalTokenCount") or u.get("total_token_count"),
                  u.get("cachedContentTokenCount") or u.get("cached_content_token_count"))


# =====================================================================
# 响应：IR → 下游
# =====================================================================

def build_response(proto, ir, model=None):
    fn = {
        "openai_chat": _out_chat,
        "openai_responses": _out_responses,
        "anthropic": _out_anthropic,
        "gemini": _out_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    return fn(ir, model or ir.get("model") or "")


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _out_chat(ir, model):
    texts = [p.get("text") for p in ir.get("parts") or [] if p.get("type") == "text"]
    tools = [p for p in ir.get("parts") or [] if p.get("type") == "tool_use"]
    msg = {"role": "assistant", "content": "\n".join(t for t in texts if t) or None}
    if tools:
        msg["tool_calls"] = [{"id": p.get("id") or f"call_{i}", "type": "function",
                              "function": {"name": p.get("name") or "",
                                           "arguments": _arg_text(p.get("input"))}}
                             for i, p in enumerate(tools)]
        if not msg["content"]:
            msg["content"] = None
    u = ir.get("usage") or _usage()
    return {"id": ir.get("id") or _new_id("chatcmpl"), "object": "chat.completion",
            "created": ir.get("created") or int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": _chat_finish(ir.get("stop"))}],
            "usage": {"prompt_tokens": u.get("input", 0),
                      "completion_tokens": u.get("output", 0),
                      "total_tokens": u.get("total", 0)}}


def _out_anthropic(ir, model):
    blocks = []
    for p in ir.get("parts") or []:
        if p.get("type") == "text":
            blocks.append({"type": "text", "text": str(p.get("text") or "")})
        elif p.get("type") == "tool_use":
            blocks.append({"type": "tool_use", "id": p.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                           "name": p.get("name") or "", "input": p.get("input") or {}})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    u = ir.get("usage") or _usage()
    return {"id": ir.get("id") or _new_id("msg"), "type": "message", "role": "assistant",
            "model": model, "content": blocks,
            "stop_reason": _anthropic_finish(ir.get("stop")), "stop_sequence": None,
            "usage": {"input_tokens": u.get("input", 0),
                      "output_tokens": u.get("output", 0)}}


def _out_responses(ir, model):
    output = []
    texts = [str(p.get("text") or "") for p in ir.get("parts") or [] if p.get("type") == "text"]
    if texts or not any(p.get("type") == "tool_use" for p in ir.get("parts") or []):
        output.append({"type": "message", "id": _new_id("msg"), "status": "completed",
                       "role": "assistant",
                       "content": [{"type": "output_text", "text": "\n".join(texts),
                                    "annotations": []}]})
    for p in ir.get("parts") or []:
        if p.get("type") == "tool_use":
            output.append({"type": "function_call", "id": _new_id("fc"),
                           "call_id": p.get("id") or _new_id("call"),
                           "name": p.get("name") or "",
                           "arguments": _arg_text(p.get("input")), "status": "completed"})
    u = ir.get("usage") or _usage()
    return {"id": ir.get("id") or _new_id("resp"), "object": "response",
            "created_at": ir.get("created") or int(time.time()), "status": "completed",
            "model": model, "output": output,
            "usage": {"input_tokens": u.get("input", 0),
                      "output_tokens": u.get("output", 0),
                      "total_tokens": u.get("total", 0)}}


def _out_gemini(ir, model):
    parts = []
    for p in ir.get("parts") or []:
        if p.get("type") == "text":
            parts.append({"text": str(p.get("text") or "")})
        elif p.get("type") == "tool_use":
            parts.append({"functionCall": {"name": p.get("name") or "",
                                           "args": p.get("input") or {}}})
    if not parts:
        parts = [{"text": ""}]
    u = ir.get("usage") or _usage()
    return {"candidates": [{"content": {"parts": parts, "role": "model"},
                            "finishReason": _gemini_finish(ir.get("stop")), "index": 0}],
            "usageMetadata": {"promptTokenCount": u.get("input", 0),
                              "candidatesTokenCount": u.get("output", 0),
                              "totalTokenCount": u.get("total", 0)},
            "modelVersion": model}


def _chat_finish(stop):
    return {"stop": "stop", "length": "length", "tool_calls": "tool_calls",
            "content_filter": "content_filter"}.get(stop or "stop", "stop")


def _anthropic_finish(stop):
    return {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "content_filter": "refusal"}.get(stop or "stop", "end_turn")


def _gemini_finish(stop):
    return {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "STOP",
            "content_filter": "SAFETY"}.get(stop or "stop", "STOP")


# =====================================================================
# 流式：SSE 解析
# =====================================================================

class SSEError(ValueError):
    pass


def iter_sse(chunks, max_line=1024 * 1024):
    """把任意切分的字节流解析成 (event, data)。

    `data:` 可以多行；空行派发。注释行与 `id:`/`retry:` 忽略。
    单行上限 1MB，防止恶意上游用超长行把内存顶爆。
    """
    buf = bytearray()
    data_lines = []
    event = None
    for chunk in chunks:
        if not chunk:
            continue
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n")
            if i < 0:
                if len(buf) > max_line:
                    raise SSEError("上游 SSE 单行超过 1MB")
                break
            line = bytes(buf[:i]).rstrip(b"\r")
            del buf[:i + 1]
            if not line:
                if data_lines:
                    yield event, "\n".join(data_lines)
                data_lines, event = [], None
                continue
            if line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                event = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip().decode("utf-8", "replace"))
    if data_lines:
        yield event, "\n".join(data_lines)


def parse_stream(proto, chunks):
    fn = {
        "openai_chat": _stream_chat,
        "openai_responses": _stream_responses,
        "anthropic": _stream_anthropic,
        "gemini": _stream_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    yield from fn(iter_sse(chunks))


def _stream_chat(events):
    began = False
    finish = None
    tools = {}
    usage = None
    for _ev, data in events:
        data = (data or "").strip()
        if not data:
            continue
        if data == "[DONE]":
            break
        try:
            o = json.loads(data)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("error"):
            yield {"type": "error", "message": _err_text(o.get("error"))}
            return
        if not began:
            began = True
            yield {"type": "start", "id": str(o.get("id") or ""),
                   "model": str(o.get("model") or "")}
        if o.get("usage"):
            usage = _merge_usage(usage, _usage_from_chat(o.get("usage")))
        for ch in _as_list(o.get("choices")):
            if not isinstance(ch, dict):
                continue
            d = ch.get("delta") or {}
            c = d.get("content")
            if isinstance(c, str) and c:
                yield {"type": "text", "text": c}
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("text"):
                        yield {"type": "text", "text": str(b["text"])}
            for tc in _as_list(d.get("tool_calls")):
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index") or 0)
                fn = tc.get("function") or {}
                if idx not in tools:
                    tools[idx] = {"id": str(tc.get("id") or f"call_{idx}"),
                                  "name": str(fn.get("name") or "")}
                    yield {"type": "tool_start", "index": idx,
                           "id": tools[idx]["id"], "name": tools[idx]["name"]}
                elif fn.get("name") and not tools[idx]["name"]:
                    tools[idx]["name"] = str(fn["name"])
                args = fn.get("arguments")
                if args:
                    yield {"type": "tool_args", "index": idx, "text": str(args)}
            if ch.get("finish_reason"):
                finish = _STOP_MAP.get(str(ch["finish_reason"]), str(ch["finish_reason"]))
    for idx in tools:
        yield {"type": "tool_end", "index": idx}
    if usage:
        yield {"type": "usage", "usage": usage}
    yield {"type": "finish", "reason": finish or ("tool_calls" if tools else "stop")}


def _stream_anthropic(events):
    began = False
    usage = None
    stop = None
    block_tool = {}
    tool_ord = 0
    saw_tool = False
    for ev, data in events:
        data = (data or "").strip()
        if not data:
            continue
        try:
            o = json.loads(data)
        except ValueError:
            continue
        t = o.get("type") or ev or ""
        if t == "error":
            yield {"type": "error", "message": _err_text(o.get("error") or o)}
            return
        if t == "message_start":
            began = True
            m = o.get("message") or {}
            usage = _merge_usage(usage, _usage_from_anthropic(m.get("usage")))
            yield {"type": "start", "id": str(m.get("id") or ""),
                   "model": str(m.get("model") or ""), "usage": usage}
        elif t == "content_block_start":
            idx = int(o.get("index") or 0)
            b = o.get("content_block") or {}
            if b.get("type") == "tool_use":
                saw_tool = True
                block_tool[idx] = tool_ord
                yield {"type": "tool_start", "index": tool_ord,
                       "id": str(b.get("id") or f"call_{idx}"),
                       "name": str(b.get("name") or "")}
                inp = b.get("input")
                if isinstance(inp, dict) and inp:
                    yield {"type": "tool_args", "index": tool_ord,
                           "text": json.dumps(inp, ensure_ascii=False, separators=(",", ":"))}
                tool_ord += 1
        elif t == "content_block_delta":
            idx = int(o.get("index") or 0)
            d = o.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                yield {"type": "text", "text": str(d["text"])}
            elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                if idx in block_tool:
                    yield {"type": "tool_args", "index": block_tool[idx],
                           "text": str(d["partial_json"])}
        elif t == "content_block_stop":
            idx = int(o.get("index") or 0)
            if idx in block_tool:
                yield {"type": "tool_end", "index": block_tool[idx]}
                block_tool.pop(idx, None)
        elif t == "message_delta":
            d = o.get("delta") or {}
            if d.get("stop_reason"):
                stop = _STOP_MAP.get(str(d["stop_reason"]), str(d["stop_reason"]))
            if o.get("usage"):
                usage = _merge_usage(usage, _usage_from_anthropic(o.get("usage")))
                yield {"type": "usage", "usage": usage}
        elif t == "message_stop":
            break
    if not began:
        yield {"type": "start", "id": "", "model": ""}
    yield {"type": "finish", "reason": stop or ("tool_calls" if saw_tool else "stop")}


def _stream_responses(events):
    began = False
    usage = None
    stop = None
    tools = set()
    for ev, data in events:
        data = (data or "").strip()
        if not data:
            continue
        try:
            o = json.loads(data)
        except ValueError:
            continue
        t = o.get("type") or ev or ""
        if t in ("error", "response.failed"):
            resp = o.get("response") or o
            yield {"type": "error", "message": _err_text(o.get("error") or resp.get("error") or o)}
            return
        if t == "response.created":
            began = True
            r = o.get("response") or {}
            yield {"type": "start", "id": str(r.get("id") or ""),
                   "model": str(r.get("model") or "")}
        elif t == "response.output_item.added":
            item = o.get("item") or {}
            if item.get("type") == "function_call":
                idx = int(o.get("output_index") or 0)
                tools.add(idx)
                yield {"type": "tool_start", "index": idx,
                       "id": str(item.get("call_id") or item.get("id") or f"call_{idx}"),
                       "name": str(item.get("name") or "")}
        elif t == "response.output_text.delta":
            if o.get("delta"):
                yield {"type": "text", "text": str(o["delta"])}
        elif t == "response.function_call_arguments.delta":
            idx = int(o.get("output_index") or 0)
            if o.get("delta"):
                yield {"type": "tool_args", "index": idx, "text": str(o["delta"])}
        elif t == "response.output_item.done":
            item = o.get("item") or {}
            if item.get("type") == "function_call":
                idx = int(o.get("output_index") or 0)
                if idx in tools:
                    yield {"type": "tool_end", "index": idx}
                    tools.discard(idx)
        elif t == "response.completed":
            r = o.get("response") or {}
            usage = _merge_usage(usage, _usage_from_responses(r.get("usage")))
            if r.get("status") == "incomplete":
                stop = "length"
            if any(i.get("type") == "function_call" for i in _as_list(r.get("output"))):
                stop = "tool_calls"
            yield {"type": "usage", "usage": usage}
            break
    if not began:
        yield {"type": "start", "id": "", "model": ""}
    for idx in tools:
        yield {"type": "tool_end", "index": idx}
    yield {"type": "finish", "reason": stop or "stop"}


def _stream_gemini(events):
    began = False
    usage = None
    stop = None
    tool_i = 0
    for _ev, data in events:
        data = (data or "").strip()
        if not data or data == "[DONE]":
            continue
        try:
            o = json.loads(data)
        except ValueError:
            continue
        if isinstance(o, list):
            objs = o
        else:
            objs = [o]
        for o in objs:
            if isinstance(o, dict) and o.get("error"):
                yield {"type": "error", "message": _err_text(o["error"])}
                return
            if not began:
                began = True
                yield {"type": "start", "id": str(o.get("responseId") or ""),
                       "model": str(o.get("modelVersion") or "")}
            for cand in _as_list(o.get("candidates")):
                if not isinstance(cand, dict):
                    continue
                for p in _as_list((cand.get("content") or {}).get("parts")):
                    if not isinstance(p, dict):
                        continue
                    if "text" in p and p.get("text") is not None:
                        yield {"type": "text", "text": str(p.get("text") or "")}
                    elif "functionCall" in p or "function_call" in p:
                        fc = p.get("functionCall") or p.get("function_call") or {}
                        args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                        idx = tool_i
                        tool_i += 1
                        yield {"type": "tool_start", "index": idx,
                               "id": f"call_{fc.get('name')}_{idx}",
                               "name": str(fc.get("name") or "")}
                        yield {"type": "tool_args", "index": idx,
                               "text": json.dumps(args, ensure_ascii=False, separators=(",", ":"))}
                        yield {"type": "tool_end", "index": idx}
                fr = cand.get("finishReason") or cand.get("finish_reason")
                if fr:
                    stop = _STOP_MAP.get(str(fr), "stop")
            if o.get("usageMetadata") or o.get("usage_metadata"):
                usage = _merge_usage(usage, _usage_from_gemini(
                    o.get("usageMetadata") or o.get("usage_metadata")))
                yield {"type": "usage", "usage": usage}
    if not began:
        yield {"type": "start", "id": "", "model": ""}
    yield {"type": "finish", "reason": stop or "stop"}


def _err_text(e):
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or e)
    return str(e)


# =====================================================================
# 流式：IR → 目标协议
# =====================================================================

def build_stream(proto, events, model, sse=True):
    fn = {
        "openai_chat": _emit_chat,
        "openai_responses": _emit_responses,
        "anthropic": _emit_anthropic,
        "gemini": _emit_gemini,
    }.get(proto)
    if not fn:
        raise ConversionError(f"不支持的协议 {proto}")
    yield from fn(events, model, sse)


def _sse(obj, event=None):
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


def _emit_chat(events, model, sse=True):
    rid = _new_id("chatcmpl")
    created = int(time.time())
    started = False
    finish = None
    usage = None

    def head(delta=None, fin=None, extra=None):
        o = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": fin}]}
        if extra:
            o.update(extra)
        return o

    for ev in events:
        t = ev.get("type")
        if t == "start":
            rid = ev.get("id") or rid
            model = ev.get("model") or model
            started = True
            yield _sse(head({"role": "assistant", "content": ""}))
        elif t == "text":
            if not started:
                started = True
                yield _sse(head({"role": "assistant", "content": ""}))
            yield _sse(head({"content": ev.get("text") or ""}))
        elif t == "tool_start":
            if not started:
                started = True
                yield _sse(head({"role": "assistant", "content": ""}))
            yield _sse(head({"tool_calls": [{
                "index": ev.get("index", 0), "id": ev.get("id") or f"call_{ev.get('index', 0)}",
                "type": "function",
                "function": {"name": ev.get("name") or "", "arguments": ""}}]}))
        elif t == "tool_args":
            yield _sse(head({"tool_calls": [{
                "index": ev.get("index", 0),
                "function": {"arguments": ev.get("text") or ""}}]}))
        elif t == "usage":
            usage = ev.get("usage") or usage
        elif t == "finish":
            finish = ev.get("reason") or "stop"
        elif t == "error":
            yield _sse({"error": {"message": ev.get("message") or "上游错误",
                                  "type": "upstream_error"}})
            yield b"data: [DONE]\n\n"
            return
    final = head({}, _chat_finish(finish))
    if usage:
        final["usage"] = {"prompt_tokens": usage.get("input", 0),
                          "completion_tokens": usage.get("output", 0),
                          "total_tokens": usage.get("total", 0)}
    yield _sse(final)
    yield b"data: [DONE]\n\n"


def _emit_anthropic(events, model, sse=True):
    rid = _new_id("msg")
    started = False
    idx = -1
    open_kind = None
    stop = None
    usage = _usage()
    tool_names = {}

    def start_msg():
        return _sse({"type": "message_start", "message": {
            "id": rid, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage.get("input", 0), "output_tokens": 0}}},
            "message_start")

    def close_block():
        nonlocal open_kind
        if open_kind is None:
            return None
        open_kind = None
        return _sse({"type": "content_block_stop", "index": idx}, "content_block_stop")

    for ev in events:
        t = ev.get("type")
        if t == "start":
            rid = ev.get("id") or rid
            model = ev.get("model") or model
            if ev.get("usage"):
                usage = _merge_usage(usage, ev["usage"])
            if not started:
                started = True
                yield start_msg()
                yield _sse({"type": "ping"}, "ping")
        elif t == "text":
            if not started:
                started = True
                yield start_msg()
            if open_kind != "text":
                b = close_block()
                if b:
                    yield b
                idx += 1
                open_kind = "text"
                yield _sse({"type": "content_block_start", "index": idx,
                            "content_block": {"type": "text", "text": ""}},
                           "content_block_start")
            yield _sse({"type": "content_block_delta", "index": idx,
                        "delta": {"type": "text_delta", "text": ev.get("text") or ""}},
                       "content_block_delta")
        elif t == "tool_start":
            if not started:
                started = True
                yield start_msg()
            b = close_block()
            if b:
                yield b
            idx += 1
            tid = ev.get("id") or f"toolu_{uuid.uuid4().hex[:20]}"
            tool_names[ev.get("index", idx)] = tid
            open_kind = "tool"
            yield _sse({"type": "content_block_start", "index": idx,
                        "content_block": {"type": "tool_use", "id": tid,
                                          "name": ev.get("name") or "", "input": {}}},
                       "content_block_start")
        elif t == "tool_args":
            if open_kind == "tool":
                yield _sse({"type": "content_block_delta", "index": idx,
                            "delta": {"type": "input_json_delta",
                                      "partial_json": ev.get("text") or ""}},
                           "content_block_delta")
        elif t == "tool_end":
            b = close_block()
            if b:
                yield b
        elif t == "usage":
            usage = _merge_usage(usage, ev.get("usage"))
        elif t == "finish":
            stop = ev.get("reason") or "stop"
        elif t == "error":
            yield _sse({"type": "error", "error": {"type": "api_error",
                                                   "message": ev.get("message") or "上游错误"}},
                       "error")
            return
    if not started:
        started = True
        yield start_msg()
    b = close_block()
    if b:
        yield b
    yield _sse({"type": "message_delta",
                "delta": {"stop_reason": _anthropic_finish(stop), "stop_sequence": None},
                "usage": {"input_tokens": usage.get("input", 0),
                          "output_tokens": usage.get("output", 0)}}, "message_delta")
    yield _sse({"type": "message_stop"}, "message_stop")


def _emit_responses(events, model, sse=True):
    rid = _new_id("resp")
    created = int(time.time())
    seq = 0
    output = []
    text = ""
    msg_id = None
    msg_index = None
    fc_id = None
    fc_index = None
    fc_name = ""
    fc_args = ""
    usage = _usage()
    stop = "stop"

    def event(name, obj):
        nonlocal seq
        obj = {"type": name, "sequence_number": seq, **obj}
        seq += 1
        if sse:
            return _sse(obj, name)
        return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    def close_text():
        nonlocal msg_id, msg_index, text
        if msg_id is None:
            return []
        out = [
            event("response.output_text.done",
                  {"item_id": msg_id, "output_index": msg_index,
                   "content_index": 0, "text": text}),
            event("response.content_part.done",
                  {"item_id": msg_id, "output_index": msg_index, "content_index": 0,
                   "part": {"type": "output_text", "text": text, "annotations": []}}),
            event("response.output_item.done",
                  {"output_index": msg_index,
                   "item": {"type": "message", "id": msg_id, "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text,
                                         "annotations": []}]}}),
        ]
        output.append({"type": "message", "id": msg_id, "status": "completed",
                       "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
        msg_id, msg_index, text = None, None, ""
        return out

    def close_tool():
        nonlocal fc_id, fc_index, fc_name, fc_args
        if fc_id is None:
            return []
        item = {"type": "function_call", "id": fc_id,
                "call_id": fc_id.replace("fc_", "call_"), "name": fc_name,
                "arguments": fc_args, "status": "completed"}
        out = [
            event("response.function_call_arguments.done",
                  {"item_id": fc_id, "output_index": fc_index, "arguments": fc_args}),
            event("response.output_item.done", {"output_index": fc_index, "item": item}),
        ]
        output.append(item)
        fc_id, fc_index, fc_name, fc_args = None, None, "", ""
        return out

    for ev in events:
        t = ev.get("type")
        if t == "start":
            rid = ev.get("id") or rid
            model = ev.get("model") or model
            yield event("response.created", {"response": {
                "id": rid, "object": "response", "created_at": created,
                "status": "in_progress", "model": model, "output": []}})
            yield event("response.in_progress", {"response": {
                "id": rid, "object": "response", "created_at": created,
                "status": "in_progress", "model": model, "output": []}})
        elif t == "text":
            if msg_id is None:
                for b in close_tool():
                    yield b
                msg_id = _new_id("msg")
                msg_index = len(output)
                yield event("response.output_item.added", {
                    "output_index": msg_index,
                    "item": {"type": "message", "id": msg_id, "status": "in_progress",
                             "role": "assistant", "content": []}})
                yield event("response.content_part.added", {
                    "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []}})
            text += ev.get("text") or ""
            yield event("response.output_text.delta", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                "delta": ev.get("text") or ""})
        elif t == "tool_start":
            for b in close_text():
                yield b
            fc_id = _new_id("fc")
            fc_index = len(output)
            fc_name = ev.get("name") or ""
            fc_args = ""
            yield event("response.output_item.added", {
                "output_index": fc_index,
                "item": {"type": "function_call", "id": fc_id,
                         "call_id": ev.get("id") or fc_id.replace("fc_", "call_"),
                         "name": fc_name, "arguments": "", "status": "in_progress"}})
        elif t == "tool_args":
            if fc_id is not None:
                fc_args += ev.get("text") or ""
                yield event("response.function_call_arguments.delta", {
                    "item_id": fc_id, "output_index": fc_index,
                    "delta": ev.get("text") or ""})
        elif t == "tool_end":
            for b in close_tool():
                yield b
        elif t == "usage":
            usage = _merge_usage(usage, ev.get("usage"))
        elif t == "finish":
            stop = ev.get("reason") or "stop"
        elif t == "error":
            yield event("error", {"message": ev.get("message") or "上游错误"})
            return
    for b in close_text():
        yield b
    for b in close_tool():
        yield b
    yield event("response.completed", {"response": {
        "id": rid, "object": "response", "created_at": created, "status": "completed",
        "model": model, "output": output,
        "usage": {"input_tokens": usage.get("input", 0),
                  "output_tokens": usage.get("output", 0),
                  "total_tokens": usage.get("total", 0)}}})


def _emit_gemini(events, model, sse=True):
    usage = _usage()
    stop = None
    buf = {}
    order = []

    def chunk(obj):
        if sse:
            return _sse(obj)
        return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    for ev in events:
        t = ev.get("type")
        if t == "text":
            yield chunk({"candidates": [{"content": {"parts": [{"text": ev.get("text") or ""}],
                                                     "role": "model"}, "index": 0}]})
        elif t == "tool_start":
            idx = ev.get("index", 0)
            order.append(idx)
            buf[idx] = {"name": ev.get("name") or "", "args": ""}
        elif t == "tool_args":
            idx = ev.get("index", 0)
            if idx in buf:
                buf[idx]["args"] += ev.get("text") or ""
        elif t == "tool_end":
            idx = ev.get("index", 0)
            it = buf.pop(idx, None)
            if it:
                try:
                    args = json.loads(it["args"]) if it["args"] else {}
                except ValueError:
                    args = {}
                yield chunk({"candidates": [{"content": {
                    "parts": [{"functionCall": {"name": it["name"], "args": args}}],
                    "role": "model"}, "index": 0}]})
        elif t == "usage":
            usage = _merge_usage(usage, ev.get("usage"))
        elif t == "finish":
            stop = ev.get("reason") or "stop"
        elif t == "error":
            yield chunk({"error": {"code": 502, "message": ev.get("message") or "上游错误",
                                   "status": "INTERNAL"}})
            return
    for idx in order:
        it = buf.pop(idx, None)
        if it:
            try:
                args = json.loads(it["args"]) if it["args"] else {}
            except ValueError:
                args = {}
            yield chunk({"candidates": [{"content": {
                "parts": [{"functionCall": {"name": it["name"], "args": args}}],
                "role": "model"}, "index": 0}]})
    yield chunk({"candidates": [{"content": {"parts": [], "role": "model"},
                                 "finishReason": _gemini_finish(stop), "index": 0}],
                 "usageMetadata": {"promptTokenCount": usage.get("input", 0),
                                   "candidatesTokenCount": usage.get("output", 0),
                                   "totalTokenCount": usage.get("total", 0)},
                 "modelVersion": model})


# =====================================================================
# 错误体
# =====================================================================

def error_payload(proto, code, message, err_type=""):
    msg = str(message)
    if proto == "anthropic":
        return {"type": "error", "error": {"type": err_type or _anthropic_err(code),
                                           "message": msg}}
    if proto == "gemini":
        return {"error": {"code": int(code), "message": msg,
                          "status": _gemini_status(code)}}
    return {"error": {"message": msg, "type": err_type or _openai_err(code),
                      "code": int(code)}}


def _openai_err(code):
    return {400: "invalid_request_error", 401: "authentication_error",
            403: "permission_error", 404: "not_found_error", 413: "request_too_large",
            429: "rate_limit_error", 500: "server_error", 502: "upstream_error",
            503: "service_unavailable", 507: "insufficient_storage"}.get(code, "api_error")


def _anthropic_err(code):
    return {400: "invalid_request_error", 401: "authentication_error",
            403: "permission_error", 404: "not_found_error", 413: "request_too_large",
            429: "rate_limit_error", 500: "api_error", 502: "api_error",
            503: "overloaded_error", 507: "api_error"}.get(code, "api_error")


def _gemini_status(code):
    return {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
            404: "NOT_FOUND", 413: "INVALID_ARGUMENT", 429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL", 502: "INTERNAL", 503: "UNAVAILABLE",
            507: "RESOURCE_EXHAUSTED"}.get(code, "UNKNOWN")


# =====================================================================
# 小工具
# =====================================================================

def _put(d, k, v):
    if v is not None and v != "":
        d[k] = v


def _int_or_none(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _stop_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in _as_list(v) if str(x)]


def stream_text_of(events):
    """测试/调试用：把 IR 事件里的文本拼起来。"""
    return "".join(e.get("text") or "" for e in events if e.get("type") == "text")
