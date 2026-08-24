"""Mock OpenAI-compatible server for offline pipeline tests.

Serves:
  GET  /v1/models            -> one model, id "mock-model"
  POST /v1/chat/completions  -> canned responses, sniffed from prompt content:
       - asks for a "verdict"      -> faithfulness check (SUCCESS)
       - asks for "notes"          -> one translation note (term 测试)
       - merge prompt ("Merge them into ONE canonical entry") -> merged glossary entry
       - asks for "style_summary"  -> mock style profile (summary + background)
       - "Flagged Terms" prompt     -> glossary cleanup: remove every flagged term
       - "Glossary Review" prompt   -> glossary review: one fixable warn finding
                                       (mistranslation) for the first listed entry
       - asks for "terms"          -> no new glossary terms
       - otherwise                 -> translation: fake lines matching the
                                      source array found in the prompt
  GET  /novel                -> HTML page with og:image pointing at /cover.jpg
  GET  /cover.jpg            -> tiny PNG (tests the scrape + PIL re-encode path)

Thinking simulation: a chat/completions request WITHOUT
chat_template_kwargs.enable_thinking=false mimics a hybrid-thinking model
whose reasoning eats the output budget -- content comes back empty with the
reasoning parked in reasoning_content and finish_reason "stop" (the failure
mode the client's per-provider `thinking` toggle guards against). Requests
that disable thinking get the normal canned behavior above.

Run: python tests/mock_server.py [port]   (default 8901)
"""
import base64
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _balanced_arrays(text: str):
    """Yield every balanced [...] span's parsed value (any element type)."""
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        depth = 0
        in_str = esc = False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    try:
                        val = json.loads(text[i : j + 1])
                    except Exception:
                        break
                    if isinstance(val, list):
                        yield val
                    break


def mock_reply(prompt: str) -> str:
    if "verdict" in prompt:
        return json.dumps({"verdict": "SUCCESS", "reasons": []})
    if '"notes"' in prompt or '"note"' in prompt:
        return json.dumps(
            {"notes": [{"line": 1, "term": "测试", "note": "A test note about 测试."}]}
        )
    if "Merge them into ONE canonical entry" in prompt:
        return json.dumps(
            {
                "source": "测试",
                "translation": "Test",
                "definition": "Merged definition.",
                "category": "other",
            }
        )
    if "style_summary" in prompt:
        return json.dumps({"style_summary": "A mock literary style.", "background": "A mock novel background."})
    if "Flagged Terms" in prompt:
        # glossary cleanup on balance drift signals: remove every flagged term
        terms = re.findall(r"- (\S+) translates to", prompt)
        return json.dumps(
            {"decisions": [{"source": t, "keep": False, "reason": "mundane mock term"} for t in terms]}
        )
    if "Glossary Review" in prompt:
        # review glossary model tier: flag the first listed entry as fixable
        m = re.search(r'"source":\s*"([^"]+)"', prompt)
        first = m.group(1) if m else "测试"
        return json.dumps({"findings": [{
            "source": first, "kind": "mistranslation", "severity": "warn",
            "reason": "mock glossary review finding",
            "suggestion": "Mock Fix",
        }]})
    if '"terms"' in prompt:
        return json.dumps({"terms": []})
    # translation: mirror the last line array in the prompt — numbered
    # objects ({"i", "t"}) under the numbered-line protocol, or plain strings
    for arr in reversed(list(_balanced_arrays(prompt))):
        if arr and all(
            isinstance(x, dict) and isinstance(x.get("i"), int) and isinstance(x.get("t"), str)
            for x in arr
        ):
            lines = [
                {"i": x["i"], "t": ("Translated line %d." % x["i"]) if x["t"].strip() else ""}
                for x in arr
            ]
            return json.dumps({"title": "Mock Chapter Title", "lines": lines}, ensure_ascii=False)
        if arr and all(isinstance(x, str) for x in arr):
            out = ["Translated line %d." % i if ln.strip() else "" for i, ln in enumerate(arr)]
            return json.dumps({"title": "Mock Chapter Title", "lines": out}, ensure_ascii=False)
    return json.dumps({"title": "Mock Chapter Title", "lines": []})


def mock_message(body: dict) -> dict:
    """Assistant message for a parsed /v1/chat/completions request body.

    Unless the request disables thinking via
    chat_template_kwargs.enable_thinking=false, simulate the hybrid-thinking
    failure mode: empty content with the reasoning in reasoning_content.
    """
    if body.get("chat_template_kwargs", {}).get("enable_thinking") is False:
        prompt = ""
        for msg in body.get("messages", []):
            prompt += str(msg.get("content", ""))
        return {"role": "assistant", "content": mock_reply(prompt)}
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": "<think>mock reasoning</think>",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            payload = {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}
            self._send(200, json.dumps(payload).encode(), "application/json")
        elif self.path == "/novel":
            html = (
                '<html><head><meta property="og:image" content="http://127.0.0.1:%d/cover.jpg">'
                "</head><body>mock novel page</body></html>" % PORT
            )
            self._send(200, html.encode(), "text/html")
        elif self.path == "/cover.jpg":
            self._send(200, PNG_1X1, "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # Accept any base path (e.g. /v1 or a hosted-style /api/paas/v4) so
        # providers with non-/v1 bases can be simulated; /models stays
        # /v1-only, letting tests exercise the ping chat-probe fallback.
        if not self.path.endswith("/chat/completions"):
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        payload = {
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": mock_message(req), "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json")


if __name__ == "__main__":
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print("mock server on http://127.0.0.1:%d/v1" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
