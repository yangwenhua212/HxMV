#!/usr/bin/env python3
"""仿真视觉评审端点（OpenAI 兼容 `/chat/completions`）——**没 Key 也能自测「真看图」链路**。

为什么需要它：L2/L3 真不真，全看**帧有没有真的发出去**。这个端点会数 `image_url` 的个数，
一张都没有就直接 400（= 假闭环当场暴露），并把每次请求记进 jsonl 供核对。

用法（三个终端 / 三段命令）：

    # ① 起仿真端点（默认 8799）
    python3 tools/fake_vlm.py 8799

    # ② 用零记忆大脑跑一轮，视觉评审指向它
    export OPENAI_API_KEY=test-key
    export OPENAI_BASE_URL=http://127.0.0.1:8799/v1
    export HXMV_VLM_MODEL=fake-vlm
    python3 -m hxmv "雪地里的柯基" --provider local --brain /tmp/brain.json --fresh

    # ③ 核对：L2/L3 每次请求都带图了吗？判定有没有真的驱动修正？
    cat /tmp/vlm_log.jsonl

模式（`/tmp/vlm_mode.txt`，改文件即生效；换模式自动重开计数）：
    ok         —— 一律判通过（验证正常路径）
    bad_once   —— 第一次 L2 判「角色不一致」（验证：判不通过 → Refiner 改 reference_strength → 重投通过）
    always_bad —— 一直判不通过（验证：终态 FAIL 而不是假装通过）

规划器（Planner）的请求不带图，会被这个端点 400 掉 → 闭环自动降级 MockPlanner，属预期。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = "/tmp/vlm_log.jsonl"
MODE = "/tmp/vlm_mode.txt"
_state: dict = {"l2_calls": 0, "mode_seen": None}


def _mode() -> str:
    try:
        with open(MODE) as f:
            return f.read().strip() or "ok"
    except OSError:
        return "ok"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # 静音访问日志
        pass

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        images, text = 0, ""
        for msg in body.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        images += 1
                    elif part.get("type") == "text":
                        text += str(part.get("text", ""))
            elif isinstance(content, str):
                text += content
        with open(LOG, "a") as f:
            f.write(json.dumps({"model": body.get("model"), "images": images,
                                "text": text[:120]}, ensure_ascii=False) + "\n")
        if images == 0:
            # 没有图 = 假闭环：直接报错，让验证当场抓到（规划器请求也走这里 → 触发降级 MockPlanner）
            self._json(400, {"error": "no image parts"})
            return

        mode = _mode()
        if mode != _state["mode_seen"]:
            _state["l2_calls"] = 0
            _state["mode_seen"] = mode
        is_l2 = "L2" in text
        if is_l2:
            _state["l2_calls"] += 1
        bad = mode == "always_bad" or (mode == "bad_once" and is_l2 and _state["l2_calls"] == 1)
        if is_l2:
            payload = ({"same_character": False, "same_scene": True, "reason": "主体换人了"} if bad
                       else {"same_character": True, "same_scene": True, "reason": "同一角色同一场景"})
        else:
            payload = ({"failures": ["action_mismatch"], "score": 0.35, "reason": "动作与分镜不符"} if bad
                       else {"failures": [], "score": 0.95, "reason": "符合分镜"})
        self._json(200, {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    print(f"仿真视觉评审端点 → http://127.0.0.1:{port}/v1/chat/completions")
    print(f"请求日志 {LOG} ｜ 模式文件 {MODE}（ok / bad_once / always_bad）")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
