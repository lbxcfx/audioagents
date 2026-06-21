import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import torch


class SileroVadRuntime:
    def __init__(self, threshold: float):
        self.threshold = threshold
        self.model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            trust_repo=True,
        )

    def predict(self, pcm_bytes: bytes, sample_rate: int):
        if not pcm_bytes:
            return 0.0, False
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return 0.0, False
        tensor = torch.from_numpy(audio)
        with torch.no_grad():
            prob = float(self.model(tensor, sample_rate).item())
        return prob, prob >= self.threshold


def make_handler(runtime: SileroVadRuntime):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not self.path.startswith("/vad"):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            sample_rate = 16000
            if "sample_rate=" in self.path:
                try:
                    sample_rate = int(self.path.split("sample_rate=", 1)[1].split("&", 1)[0])
                except Exception:
                    sample_rate = 16000
            prob, speech = runtime.predict(body, sample_rate)
            payload = json.dumps({"speech": speech, "probability": prob}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    runtime = SileroVadRuntime(args.threshold)
    server = HTTPServer((args.host, args.port), make_handler(runtime))
    print(f"silero vad server listening on http://{args.host}:{args.port}/vad")
    server.serve_forever()


if __name__ == "__main__":
    main()
