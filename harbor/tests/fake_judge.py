"""Local deterministic endpoint for Docker integration checks, not real grading."""

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeJudgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert payload["model"] == "jobbench-smoke-judge"
        prompt = payload["messages"][-1]["content"]
        if isinstance(prompt, list):
            prompt = "\n".join(part["text"] for part in prompt if part["type"] == "text")
        match = re.search(r"criteria_results array must have exactly (\d+) items", prompt)
        assert match is not None
        criterion_count = int(match.group(1))
        verdict = {
            "rubric_passed": True,
            "overall_reasoning": "Synthetic fixture response; not a benchmark judgment.",
            "criteria_results": [
                {"passed": True, "reasoning": "Synthetic fixture", "evidence": "fixture"}
                for _ in range(criterion_count)
            ],
        }
        body = json.dumps({
            "id": "jobbench-smoke",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(verdict)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), FakeJudgeHandler)
    print(f"Fake judge listening on port {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
