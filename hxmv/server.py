"""HxMV Web 控制台 daemon（零第三方依赖：纯 stdlib）。

用法：
    python3 -m hxmv.server [--host 127.0.0.1] [--port 8668]

端点：
    GET  /                       → Web 面板（hxmv/web/index.html）
    POST /api/run {goal,provider}→ 提交生产任务，返回 run_id（后台串行执行）
    GET  /api/stream?run_id=…    → SSE 事件流（先回放已落盘事件，再实时推送）
    GET  /api/run/<id>           → 该 run 的全部事件（历史回放）
    GET  /api/runs               → run 历史摘要列表
    GET  /api/brain              → 大脑条目（只读展示）

架构：loop.run(goal, emit=cb) 的事件旁路。worker 单线程串行跑（Brain 单文件，
并发写会打架）；每个 run 事件落盘 ~/.hxmv/runs/<id>/events.jsonl 并广播给
活跃 SSE 订阅者。事件 = 观察层，daemon 不干预闭环任何判断。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .core.brain import Brain
from .core.loop import run

RUNS_DIR = os.path.expanduser("~/.hxmv/runs")
WEB_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")

# 公网防护：设置 HXMV_WEB_TOKEN 后，所有 /api/* 请求需带 token
# （header X-Hxmv-Token 或 query ?token=，EventSource 只能用 query）
HXMV_WEB_TOKEN = os.environ.get("HXMV_WEB_TOKEN", "")

PROVIDERS = {"mock": "", "fake": "fake", "kling": "kling"}


class RunRecorder:
    """一个 run 的事件收容：append 内存 + 落盘 jsonl + 广播给 SSE 订阅者。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = os.path.join(RUNS_DIR, run_id)
        self.path = os.path.join(self.dir, "events.jsonl")
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.subs: set[queue.Queue] = set()
        self.done = False
        os.makedirs(self.dir, exist_ok=True)

    def emit(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self.lock:
            self.events.append(event)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if event.get("type") == "run.done":
                self.done = True
            subs = list(self.subs)
        for q in subs:
            q.put(event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            # 先回放已落盘事件，再实时
            for e in self.events:
                try:
                    q.put_nowait(e)
                except queue.Full:
                    break
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)


class RunManager:
    """串行 worker：一次只跑一个 run（Brain 单文件禁并发写）。"""

    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.recorders: dict[str, RunRecorder] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def submit(self, goal: str, provider: str = "") -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        rec = RunRecorder(run_id)
        with self.lock:
            self.recorders[run_id] = rec
        self.queue.put({"run_id": run_id, "goal": goal, "provider": provider})
        return run_id

    def get(self, run_id: str) -> RunRecorder | None:
        with self.lock:
            return self.recorders.get(run_id)

    def _worker_loop(self) -> None:
        while True:
            job = self.queue.get()
            run_id = job["run_id"]
            rec = self.recorders.get(run_id)
            if rec is None:
                continue
            os.environ["HXMV_PROVIDER"] = job["provider"] or ""  # 串行 worker，安全
            try:
                run(job["goal"], brain=Brain(), verbose=False, emit=rec.emit)
            except Exception as e:  # 内核异常也要把 run 收尾，别让订阅者挂死
                rec.emit({"type": "run.done", "phase": "ERROR",
                          "error": str(e), "completed": [], "failed": [],
                          "attempts": 0, "cost_units": 0, "iterations": 0,
                          "budget_exhausted": False, "brain": {}, "outputs": []})
            finally:
                rec.done = True


MANAGER = RunManager()


def _scan_runs() -> list[dict]:
    """扫描磁盘 runs 目录，读每个 events.jsonl 首/尾行出摘要。"""
    out = []
    if not os.path.isdir(RUNS_DIR):
        return out
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        path = os.path.join(RUNS_DIR, name, "events.jsonl")
        if not os.path.isfile(path):
            continue
        head = tail = None
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline()
                head = json.loads(first) if first.strip() else None
                # 尾行：从文件尾向前找最后一个非空行
                f.seek(0, 2)
                size = f.tell()
                buf = b""
                pos = size
                while pos > 0:
                    pos = max(0, pos - 4096)
                    f.seek(pos)
                    chunk = f.read(min(4096, size - pos))
                    buf = chunk.encode("utf-8", "ignore") + buf
                    lines = buf.decode("utf-8", "ignore").strip().splitlines()
                    if len(lines) > 1:
                        tail = json.loads(lines[-1])
                        break
                if tail is None and head is not None:
                    tail = head
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if head is None:
            continue
        status = "running" if not (tail and tail.get("type") == "run.done") else "done"
        out.append({
            "run_id": name,
            "goal": head.get("goal", ""),
            "ts": head.get("ts"),
            "brain_size": (head.get("brain") or {}).get("size", 0),
            "status": status,
            "tail": tail,
        })
    return out[:50]


class Handler(BaseHTTPRequestHandler):
    server_version = "HxMV/0.1"

    # ---- helpers ----
    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_err(self, code: int, msg: str) -> None:
        self._send_json({"error": msg}, code)

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _authed(self) -> bool:
        """公网 token 校验（未设置 HXMV_WEB_TOKEN 时全放行，保持本地零配置）。"""
        if not HXMV_WEB_TOKEN:
            return True
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        tok = self.headers.get("X-Hxmv-Token") or (q.get("token") or [""])[0]
        return tok == HXMV_WEB_TOKEN

    def log_message(self, *args) -> None:  # 静音默认访问日志
        pass

    # ---- routes ----
    def do_GET(self) -> None:
        u = urlparse(self.path)
        p = u.path

        if p == "/":
            try:
                with open(WEB_HTML, encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self._send_err(500, f"面板文件缺失: {WEB_HTML}")
            return

        if not self._authed():
            self._send_err(401, "unauthorized：需要 ?token= 或 X-Hxmv-Token 头")
            return

        if p == "/api/brain":
            brain = Brain()
            entries = []
            for e in sorted(brain.entries, key=lambda x: x.importance, reverse=True)[:80]:
                entries.append({"content": e.content, "kind": e.kind,
                                "importance": round(e.importance, 2),
                                "hit_count": e.hit_count, "hits_3": e.hits_3,
                                "meta": e.meta})
            self._send_json({"size": brain.size, "stats": brain.stats(), "entries": entries})
            return

        if p == "/api/runs":
            self._send_json({"runs": _scan_runs()})
            return

        if p.startswith("/api/run/") and not p.endswith("/events"):
            run_id = p[len("/api/run/"):]
            path = os.path.join(RUNS_DIR, run_id, "events.jsonl")
            if not os.path.isfile(path):
                self._send_err(404, f"run 不存在: {run_id}")
                return
            events = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        events.append(json.loads(line))
            self._send_json({"run_id": run_id, "status": "done" if events and events[-1].get("type") == "run.done" else "running", "events": events})
            return

        if p.startswith("/api/stream"):
            from urllib.parse import parse_qs
            qs = parse_qs(u.query)
            run_id = (qs.get("run_id") or [""])[0]
            rec = MANAGER.get(run_id)
            if rec is None:
                self._send_err(404, f"run 不存在: {run_id}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            sub = rec.subscribe()
            try:
                while True:
                    try:
                        ev = sub.get(timeout=15)
                        line = json.dumps(ev, ensure_ascii=False)
                        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")  # 心跳
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                rec.unsubscribe(sub)
            return

        self._send_err(404, f"未知路径: {p}")

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path != "/api/run":
            self._send_err(404, f"未知路径: {u.path}")
            return
        if not self._authed():
            self._send_err(401, "unauthorized：需要 ?token= 或 X-Hxmv-Token 头")
            return
        body = self._read_body()
        goal = str(body.get("goal", "")).strip()
        if not goal:
            self._send_err(400, "goal 不能为空")
            return
        provider = PROVIDERS.get(str(body.get("provider", "mock")), "")
        run_id = MANAGER.submit(goal, provider)
        self._send_json({"run_id": run_id, "provider": provider or "mock"}, 202)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="hxmv.server", description="HxMV Web 控制台 daemon")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8668)
    args = ap.parse_args()

    os.makedirs(RUNS_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"HxMV Web 控制台 → http://{args.host}:{args.port}")
    print(f"run 存档目录: {RUNS_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
