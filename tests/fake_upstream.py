"""测试用上游：记录收到的请求，能流式/非流式/慢速响应。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"requests": [], "disconnects": 0}
LOCK = threading.Lock()


def record(**kw):
    with LOCK:
        STATE["requests"].append(kw)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        buf = bytearray()
        while len(buf) < n:
            d = self.rfile.read(min(65536, n - len(buf)))
            if not d:
                break
            buf.extend(d)
        return bytes(buf)

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
        self.wfile.flush()

    def _sse(self, obj, event=None):
        head = (f"event: {event}\n" if event else "").encode()
        self._chunk(head + b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n")

    def do_GET(self):
        if self.path == "/stats":
            return self._json(STATE)
        return self._json({"ok": True, "path": self.path})

    def do_POST(self):
        raw = self._body()
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            obj = {}
        path = self.path.split("?")[0]
        record(path=path, size=len(raw), model=(obj.get("model") or ""),
               auth=self.headers.get("Authorization") or "",
               xkey=self.headers.get("x-api-key") or "",
               gkey=self.headers.get("x-goog-api-key") or "",
               stream=bool(obj.get("stream")),
               body=raw[:2000].decode("utf-8", "replace"))

        if path.endswith("/chat/completions"):
            return self._chat(obj)
        if path.endswith("/messages"):
            return self._anthropic(obj)
        if path.endswith("/responses"):
            return self._responses(obj)
        if ":generateContent" in path or ":streamGenerateContent" in path:
            return self._gemini(path, obj)
        if path == "/slow":
            return self._slow()
        return self._json({"ok": True, "path": path})

    def _chat(self, obj):
        if not obj.get("stream"):
            return self._json({
                "id": "chatcmpl-fake", "object": "chat.completion", "created": 1,
                "model": obj.get("model") or "fake",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "非流式回复",
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            })
        self._sse_start()
        if obj.get("slow"):
            try:
                for i in range(200):
                    self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                               "model": obj.get("model") or "fake",
                               "choices": [{"index": 0, "delta": {"content": str(i)},
                                            "finish_reason": None}]})
                    time.sleep(0.15)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                with LOCK:
                    STATE["disconnects"] += 1
            return
        self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                   "model": obj.get("model") or "fake",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None}]})
        for t in ("你", "好"):
            self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                       "model": obj.get("model") or "fake",
                       "choices": [{"index": 0, "delta": {"content": t},
                                    "finish_reason": None}]})
            time.sleep(0.02)
        self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                   "model": obj.get("model") or "fake",
                   "choices": [{"index": 0, "delta": {"tool_calls": [{
                       "index": 0, "id": "call_fake_1", "type": "function",
                       "function": {"name": "get_weather", "arguments": ""}}]},
                       "finish_reason": None}]})
        for part in ('{"city":', '"杭州"}'):
            self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                       "model": obj.get("model") or "fake",
                       "choices": [{"index": 0, "delta": {"tool_calls": [{
                           "index": 0, "function": {"arguments": part}}]},
                           "finish_reason": None}]})
        self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                   "model": obj.get("model") or "fake",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                   "model": obj.get("model") or "fake", "choices": [],
                   "usage": {"prompt_tokens": 21, "completion_tokens": 9,
                             "total_tokens": 30}})
        self._chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _anthropic(self, obj):
        if not obj.get("stream"):
            return self._json({
                "id": "msg_fake", "type": "message", "role": "assistant",
                "model": obj.get("model") or "claude",
                "content": [{"type": "text", "text": "Anthropic 非流式"},
                            {"type": "tool_use", "id": "toolu_fake", "name": "lookup",
                             "input": {"q": "x"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 6},
            })
        self._sse_start()
        self._sse({"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant",
            "model": obj.get("model") or "claude", "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 0}}}, "message_start")
        self._sse({"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text", "text": ""}}, "content_block_start")
        for t in ("流", "式"):
            self._sse({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": t}}, "content_block_delta")
        self._sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
        self._sse({"type": "content_block_start", "index": 1,
                   "content_block": {"type": "tool_use", "id": "toolu_fake",
                                     "name": "lookup", "input": {}}},
                  "content_block_start")
        for part in ('{"q":', '"杭州"}'):
            self._sse({"type": "content_block_delta", "index": 1,
                       "delta": {"type": "input_json_delta", "partial_json": part}},
                      "content_block_delta")
        self._sse({"type": "content_block_stop", "index": 1}, "content_block_stop")
        self._sse({"type": "message_delta",
                   "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                   "usage": {"output_tokens": 8}}, "message_delta")
        self._sse({"type": "message_stop"}, "message_stop")
        self.wfile.write(b"0\r\n\r\n")

    def _responses(self, obj):
        if not obj.get("stream"):
            return self._json({
                "id": "resp_fake", "object": "response", "created_at": 1,
                "status": "completed", "model": obj.get("model") or "gpt",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Responses 非流式"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            })
        self._sse_start()
        self._sse({"type": "response.created", "response": {
            "id": "resp_fake", "object": "response", "status": "in_progress",
            "model": obj.get("model") or "gpt", "output": []}}, "response.created")
        self._sse({"type": "response.output_item.added", "output_index": 0,
                   "item": {"type": "message", "id": "msg_1", "status": "in_progress",
                            "role": "assistant", "content": []}},
                  "response.output_item.added")
        self._sse({"type": "response.output_text.delta", "item_id": "msg_1",
                   "output_index": 0, "content_index": 0, "delta": "流式 R"},
                  "response.output_text.delta")
        self._sse({"type": "response.completed", "response": {
            "id": "resp_fake", "status": "completed", "model": "gpt",
            "output": [], "usage": {"input_tokens": 3, "output_tokens": 4,
                                    "total_tokens": 7}}}, "response.completed")
        self.wfile.write(b"0\r\n\r\n")

    def _gemini(self, path, obj):
        if "streamGenerateContent" in path:
            self._sse_start()
            for t in ("双", "子"):
                self._sse({"candidates": [{"content": {"parts": [{"text": t}],
                                                       "role": "model"}, "index": 0}]})
            self._sse({"candidates": [{"content": {"parts": [], "role": "model"},
                                       "finishReason": "STOP", "index": 0}],
                       "usageMetadata": {"promptTokenCount": 2,
                                         "candidatesTokenCount": 3,
                                         "totalTokenCount": 5}})
            self.wfile.write(b"0\r\n\r\n")
            return
        return self._json({
            "candidates": [{"content": {"parts": [{"text": "Gemini 非流式"}],
                                        "role": "model"}, "finishReason": "STOP",
                            "index": 0}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3,
                              "totalTokenCount": 5},
            "modelVersion": obj.get("model") or "gemini-fake",
        })

    def _slow(self):
        self._sse_start()
        try:
            for i in range(100):
                self._sse({"i": i})
                time.sleep(0.1)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            with LOCK:
                STATE["disconnects"] += 1


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19100
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    print("fake upstream on %d" % port, flush=True)
    srv.serve_forever()
