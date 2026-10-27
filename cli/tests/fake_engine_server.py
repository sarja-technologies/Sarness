"""A real HTTP/SSE server speaking the engine's agent API, scripted per test."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-token-1234567890abcdef"
MODELS = [
    {"ref": "local:coder@Q4", "name": "Coder Q4", "provider": "Sarness Engine", "vision": False, "context_max": 65536, "default_ctx": 16384,
     "vram_mb": 20000, "size": 1, "local": True, "tools": None},
    {"ref": "local:small@Q8", "name": "Small Q8", "provider": "Sarness Engine", "vision": False, "context_max": 32768, "default_ctx": 8192,
     "vram_mb": 2000, "size": 1, "local": True, "tools": None},
]
REGISTRY = [{"id": "coder@Q4", "capabilities": {"reasoning": True}, "meta": {"architecture": "qwen3"}, "tags": [], "supported": True},
            {"id": "small@Q8", "capabilities": {"reasoning": False}, "meta": {"architecture": "qwen3"}, "tags": [], "supported": True}]


class FakeEngineServer:
    def __init__(self, turns, llm_status=None):
        self.turns = list(turns)             # each: list of SSE event dicts
        self.completions = []                # request bodies received
        self.llm = llm_status or {"state": "stopped", "model_id": None, "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, body, code=200):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def _authed(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._json({"detail": "unauthorized"}, 401)
                    return False
                return True

            def do_GET(self):
                if self.path == "/v1/health":
                    return self._json({"status": "ok", "service": "sarness-engine", "api_version": "1.1", "engine_version": "0.4.0"})
                if not self._authed():
                    return
                if self.path == "/v1/agent/models":
                    return self._json(MODELS)
                if self.path.startswith("/v1/models"):
                    return self._json(REGISTRY)
                if self.path == "/v1/llm/status":
                    return self._json(outer.llm)
                self._json({"detail": "nope"}, 404)

            def do_POST(self):
                if not self._authed():
                    return
                outer.completions.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                events = outer.turns.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev in events:
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                    self.wfile.flush()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def done(content="", calls=None, finish=None, reasoning=""):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"type": "done", "message": msg, "reasoning": reasoning, "finish_reason": finish or ("tool_calls" if calls else "stop"),
            "usage": {"prompt_tokens": 500, "completion_tokens": 40}, "timings": None}
