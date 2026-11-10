"""Minimal OpenAI-compatible server: replays a scripted list of tool calls, then answers 'done'.
Logs every request body to requests.jsonl."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
SCRIPT = json.load(open(sys.argv[2]))
LOG = sys.argv[3]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "fake", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(LOG, "a") as f: f.write(json.dumps(req) + "\n")
        n_tool = sum(1 for m in req.get("messages", []) if m.get("role") == "tool")
        if n_tool < len(SCRIPT):
            name, args = SCRIPT[n_tool]
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{n_tool}", "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": "done"}, "stop"
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            delta = dict(msg)
            if "tool_calls" in delta and delta["tool_calls"]:
                delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
            for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}):
                chunk.update({"id": "x", "object": "chat.completion.chunk", "created": int(time.time()), "model": "fake"})
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "fake",
                               "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
