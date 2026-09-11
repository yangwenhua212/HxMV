"""智谱视频 API 的本地仿真端点——**没有 Key 也能验证整条接入链路**。

用法：
    python3 tools/fake_zhipu_api.py 8799 /path/to/some.mp4
    FAKE_EXPECT_KEY=test-key-123 HXMV_PROVIDER=zhipu \\
    HXMV_ZHIPU_KEY=test-key-123 HXMV_ZHIPU_BASE=http://127.0.0.1:8799/api/paas/v4 \\
    python3 -m hxmv --provider zhipu "雪地里的柯基"

行为与真实接口同构（用来验证参数投影/轮询/下载/事后修正是否都对）：
    POST /api/paas/v4/videos/generations   → {"id": "...", "task_status": "PROCESSING"}
    GET  /api/paas/v4/async-result/<id>    → 前两次 PROCESSING，之后 SUCCESS + video_result[0].url
    GET  /<file>                           → 返回给定的 mp4（验证下载与后续 L1/L2 实测）

它会把收到的请求体打印出来（含"是否图生视频、首帧 base64 多大"），方便核对。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO = sys.argv[2] if len(sys.argv) > 2 else ""
EXPECT_KEY = os.environ.get("FAKE_EXPECT_KEY", "test-key-123")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
STATE = {"polls": 0, "submits": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):   # noqa: A002 - 保持与基类签名兼容
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {EXPECT_KEY}":
            print(f"[fake] 鉴权失败: {auth[:20]}", flush=True)
            return self._json({"error": {"code": "1002", "message": "Authorization Token 非法"}}, 401)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": {"message": "bad json"}}, 400)
        STATE["submits"].append(body)
        print(f"[fake] 收到提交 #{len(STATE['submits'])}：{json.dumps(body, ensure_ascii=False)[:400]}",
              flush=True)
        img = body.get("image_url")
        print(f"[fake] 图生视频? {'是（首帧 ' + str(len(img)) + ' 字节 base64）' if img else '否（纯文生）'}",
              flush=True)
        self._json({"id": f"task-{len(STATE['submits'])}", "request_id": "req-1",
                    "model": body.get("model"), "task_status": "PROCESSING"})

    def do_GET(self):
        if self.path.startswith("/api/paas/v4/async-result/"):
            STATE["polls"] += 1
            if STATE["polls"] < 3:
                print(f"[fake] 第 {STATE['polls']} 次轮询：PROCESSING", flush=True)
                return self._json({"task_status": "PROCESSING", "id": "task-1"})
            print(f"[fake] 第 {STATE['polls']} 次轮询：SUCCESS", flush=True)
            return self._json({"task_status": "SUCCESS", "id": "task-1",
                               "video_result": [{"url": f"http://127.0.0.1:{PORT}/fake.mp4",
                                                 "cover_image_url": ""}]})
        if self.path.startswith("/fake.mp4") and VIDEO and os.path.isfile(VIDEO):
            with open(VIDEO, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    if not VIDEO or not os.path.isfile(VIDEO):
        print("用法: python3 tools/fake_zhipu_api.py <端口> <要返回的 mp4 路径>")
        raise SystemExit(2)
    print(f"[fake] 智谱仿真端点 → http://127.0.0.1:{PORT}/api/paas/v4 （期望 Key: {EXPECT_KEY}）",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
