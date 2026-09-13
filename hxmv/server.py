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
    GET  /api/health             → **自检 + 能力**（HxSync 等客户端用来"发现实例"）
    GET  /api/artifact?run_id=&name= → 取产物文件（成片/镜头/参考图）
    GET  /api/ref?project=…      → 项目参考图清单（角色/场景 + 缩略图地址）
    GET  /api/ref/image?project=&kind=&key= → 取参考图字节（面板缩略图）
    POST /api/ref                → **上传参考图**（含 auto 自动裁主视觉；preview=true 只预览不落库）
    DELETE /api/ref              → 注销一张参考图
    GET  /dl/<文件名>            → 分发包下载（客户端安装包等，放 ~/.hxmv/dl/，公开不带 token）

客户端友好（HxSync）：
    · /api/health 可被客户端扫描发现（本地 127.0.0.1 或远端域名都行）
    · 跑完可主动推送：设 HXMV_NOTIFY_URL（JSON webhook 或 feishu:<机器人地址>），
      见 core/notify.py——切后台/断长连接也不丢成品

架构：loop.run(goal, emit=cb) 的事件旁路。worker 单线程串行跑（Brain 单文件，
并发写会打架）；每个 run 事件落盘 ~/.hxmv/runs/<id>/events.jsonl 并广播给
活跃 SSE 订阅者。事件 = 观察层，daemon 不干预闭环任何判断。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from .core.brain import Brain
from .core.loop import run

RUNS_DIR = os.path.expanduser("~/.hxmv/runs")
DL_DIR = os.environ.get("HXMV_DL_DIR", os.path.expanduser("~/.hxmv/dl"))
HOOKS_DIR = os.environ.get("HXMV_HOOKS_DIR", os.path.expanduser("~/.hxmv/hooks"))
STARTED_AT = time.time()
WEB_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")

# 公网防护：设置 HXMV_WEB_TOKEN 后，所有 /api/* 请求需带 token
# （header X-Hxmv-Token 或 query ?token=，EventSource 只能用 query）
HXMV_WEB_TOKEN = os.environ.get("HXMV_WEB_TOKEN", "")

# 参考图上传：只收图片，硬限制体积（面板在手机上用，别让一张原图把内存撑爆）
MAX_REF_B64 = 16 * 1024 * 1024          # base64 字符串上限 ≈ 12MB 原图
REF_KINDS = ("character", "scene")
# 按磁盘列产物时认哪些后缀（面板的成片/镜头/参考图展示用）
MEDIA_EXTS = (".mp4", ".png", ".jpg", ".jpeg", ".webp")

PROVIDERS = {"mock": "", "fake": "fake", "local": "local", "zhipu": "zhipu", "kling": "kling"}


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
        self._active: set[str] = set()
        self.lock = threading.Lock()
        # 正在跑/排队中的 run_id——用来分辨「进行中」和「被中断」
        # （服务重启/进程被杀时，磁盘上那些没有 run.done 的 run 会一直显示"进行中"，
        #  真机反馈过：面板说进行中，其实早就断了，白等。ACTIVE_RUNS 由 submit 登记）
        global ACTIVE_RUNS
        ACTIVE_RUNS = self._active
        threading.Thread(target=self._worker_loop, daemon=True).start()

    @property
    def active(self) -> set[str]:
        return self._active

    def submit(self, goal: str, provider: str = "", project: str = "") -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        rec = RunRecorder(run_id)
        with self.lock:
            self.recorders[run_id] = rec
            self._active.add(run_id)
        self.queue.put({"run_id": run_id, "goal": goal, "provider": provider,
                        "project": project})
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
            # 产物落到本 run 自己的目录：面板就能按 run 取回真文件（local provider 用）
            os.environ["HXMV_ARTIFACTS"] = os.path.join(RUNS_DIR, run_id, "artifacts")
            proj = None
            if job.get("project"):
                from .core.project import Project
                proj = Project.load(job["project"])
            try:
                run(job["goal"], brain=Brain(), project=proj,
                    verbose=False, emit=rec.emit)
            except Exception as e:  # 内核异常也要把 run 收尾，别让订阅者挂死
                rec.emit({"type": "run.done", "phase": "ERROR",
                          "error": str(e), "completed": [], "failed": [], "attempts": 0,
                          "cost_units": 0, "iterations": 0, "budget_exhausted": False,
                          "brain": {}, "outputs": []})
            finally:
                with self.lock:
                    self._active.discard(run_id)
                rec.done = True
                self._notify_done(run_id, job, rec)

    @staticmethod
    def _notify_done(run_id: str, job: dict, rec) -> None:
        """跑完主动把成品推给客户端（HxSync）。通知失败绝不影响生产。"""
        from .core import notify as _notify
        if not os.environ.get("HXMV_NOTIFY_URL"):
            return
        done = next((e for e in reversed(rec.events) if e.get("type") == "run.done"), {})
        base = os.environ.get("HXMV_PUBLIC_BASE", "")
        payload = _notify.build_payload(job.get("goal", ""), done,
                                       os.path.join(RUNS_DIR, run_id, "artifacts"),
                                       base=base, run_id=run_id)
        payload["project"] = job.get("project") or None
        payload["provider"] = job.get("provider") or "mock"
        rec.emit({"type": "notify", "result": _notify.notify(payload)})


# 正在跑/排队中的 run_id（RunManager 启动时把它指向自己的活跃集合，供 _scan_runs 判断
# 「进行中」还是「被中断」——没有它，服务重启后残留的 run 会永久显示"进行中"）
ACTIVE_RUNS: set[str] = set()

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
        # 三种状态：跑完 / 正在跑 / 被中断（没 run.done 又不在活跃集合里 = 服务重启等打断的残留）
        if tail and tail.get("type") == "run.done":
            status = "done"
        elif name in ACTIVE_RUNS:
            status = "running"
        else:
            status = "aborted"
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
    server_version = "HxMV/0.6"

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

    @staticmethod
    def _token_cookie(tok: str) -> str:
        """给手机浏览器种的长期 Cookie（一年）——只走 HTTPS，同站请求自动带上。"""
        return f"hxmv_token={quote(tok)}; Path=/; Max-Age=31536000; Secure; SameSite=Lax"

    def _cookie_token(self) -> str:
        """从 Cookie 里取令牌——手机浏览器 localStorage 会丢（换入口/被清），Cookie 能续上。"""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hxmv_token":
                return unquote(v).strip()
        return ""

    def _authed(self) -> bool:
        """公网 token 校验（未设置 HXMV_WEB_TOKEN 时全放行，保持本地零配置）。

        三种来源任一命中即可：`X-Hxmv-Token` 头 / `?token=` 查询串 / `hxmv_token` Cookie。
        空值一律当作没带——客户端在没令牌时会发 `X-Hxmv-Token: ""`，别把它当成"带了错的"。
        """
        if not HXMV_WEB_TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        tok = ((self.headers.get("X-Hxmv-Token") or "").strip()
               or ((q.get("token") or [""])[0]).strip()
               or self._cookie_token())
        return tok == HXMV_WEB_TOKEN

    @staticmethod
    def _fix_mojibake(s: str) -> str:
        """中文查询串的老坑：http.server 按 latin-1 解请求行，非 ASCII 会变乱码。

        浏览器会百分号编码所以正常；但手搓客户端/某些 App 直传 UTF-8 字节时，
        这里把它修回来，否则项目名"石猴出世"永远查不到。
        """
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s

    @staticmethod
    def _safe_pid(pid: str) -> str:
        """项目名 → 安全目录名（只允许中英文/数字/-_）。空串=不合法，防路径穿越。

        外来输入只在这里过一道闸：Project.load 直接用项目名拼路径。
        """
        p = Handler._fix_mojibake(pid or "").strip()
        if not p or len(p) > 64:
            return ""
        return p if all(c.isalnum() or c in "-_" for c in p) else ""

    @staticmethod
    def _decode_image(raw: str) -> bytes | None:
        """接受 data URL 或裸 base64；解不出/超限返回 None。"""
        raw = (raw or "").strip()
        if raw.startswith("data:") and "," in raw[:64]:
            raw = raw.split(",", 1)[1]
        if not raw or len(raw) > MAX_REF_B64:
            return None
        try:
            blob = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return None
        return blob or None

    def log_message(self, *args) -> None:  # 静音默认访问日志
        pass

    # ---- routes ----
    def do_GET(self) -> None:
        u = urlparse(self.path)
        p = u.path

        if p.startswith("/k/"):
            # 可收藏的私人入口：/k/<令牌> → 带上令牌的面板地址。
            # 手机书签用：地址栏里永远带着令牌，不怕 localStorage 被清。
            tok = unquote(p[3:]).strip()
            if tok and tok == HXMV_WEB_TOKEN:
                self.send_response(302)
                self.send_header("Location", "/?token=" + quote(tok))
                self.send_header("Set-Cookie", self._token_cookie(tok))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_err(404, "入口不对")
            return

        if p == "/":
            try:
                with open(WEB_HTML, encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # 面板改动要立刻生效，别让手机浏览器吃老缓存（真机上踩过：改了看不到）
                self.send_header("Cache-Control", "no-store")
                q = parse_qs(urlparse(self.path).query)
                tok = ((q.get("token") or [""])[0]).strip()
                if tok and tok == HXMV_WEB_TOKEN:
                    # 顺手种一年 Cookie：下次不带 ?token= 打开也认
                    self.send_header("Set-Cookie", self._token_cookie(tok))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self._send_err(500, f"面板文件缺失: {WEB_HTML}")
            return

        if p == "/api/config":
            # 接口配置（面板设置页用）：只回**脱敏**状态，明文 Key 永不回传
            if not self._authed():
                self._send_err(401, "unauthorized：需要 ?token= 或 X-Hxmv-Token 头")
                return
            from .core import config, llm
            providers = []
            for pid, name in (("zhipu", "智谱 AI"), ("kling", "可灵")):
                k = config.api_key(pid)
                providers.append({"id": pid, "name": name,
                                  "configured": bool(k), "masked": config.mask(k) if k else ""})
            self._send_json({
                "providers": providers,
                "options": {"video_model": config.option("zhipu", "video_model") or "cogvideox-flash",
                            "vlm_model": config.option("zhipu", "vlm_model") or llm.ZHIPU_VISION_MODEL},
                "vision": {"ready": llm.vision_available(), "model": llm.vision_model()},
            })
            return

        if p.startswith("/dl/"):
            # 分发包（客户端安装包等）：放固定目录、按文件名白名单取，防穿越。
            # 走 nginx 前面时不需要 token——这是公开的安装包，不是实例数据。
            name = p[len("/dl/"):]
            if (not name or "/" in name or "\\" in name or name.startswith(".")
                    or not all(c.isalnum() or c in "-._" for c in name)):
                self._send_err(400, "文件名不合法")
                return
            path = os.path.join(DL_DIR, name)
            if not os.path.isfile(path):
                self._send_err(404, f"没有这个分发包: {name}")
                return
            ctype = ("application/vnd.android.package-archive" if name.endswith(".apk")
                     else "application/octet-stream")
            size = os.path.getsize(path)
            start = 0                      # 支持断点续传（手机下大包常断）
            m = re.match(r"bytes=(\d+)-", self.headers.get("Range", ""))
            if m:
                start = min(int(m.group(1)), max(size - 1, 0))
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", ctype)
            if m:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.send_header("Content-Length", str(size - start))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                while True:
                    chunk = f.read(1 << 16)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if p in ("/api/health", "/healthz"):
            # 客户端"发现实例"用：**故意不要令牌**——否则客户端分不清"连不上"和"缺令牌"，
            # 只会一律报"连不上"。项目名这类信息只有带令牌才给。
            authed = self._authed()
            from .media import probe
            from .core import config, llm
            from . import __version__ as _v
            from .core.project import Project
            projects = []
            if authed:
                try:
                    projects = Project.list_all()
                except Exception:
                    pass
            self._send_json({
                "ok": True, "name": "hxmv", "version": _v, "server": self.server_version,
                "ffmpeg": probe.has_ffmpeg(),
                "encoder": probe.encoder_name() if probe.has_ffmpeg() else None,
                # 视觉评审是否真的开着（L2 身份判定 / L3 语义评审靠它；没开=结果里会标注未做视觉检查）
                "vision": {"ready": llm.vision_available(), "model": llm.vision_model()},
                "providers": {name: {"ready": True if name == "mock" or name == "local"
                                     else config.configured(name)}
                              for name in PROVIDERS},
                "projects": [{"id": x.get("id"), "episodes": x.get("episodes")} for x in projects]
                            if authed else [],
                "runs": len(_scan_runs()),
                "needs_token": bool(HXMV_WEB_TOKEN),
                "authed": authed,
                "notify": bool(os.environ.get("HXMV_NOTIFY_URL")),
                "uptime_s": round(time.time() - STARTED_AT, 1),
                "run_url": "/api/run", "stream_url": "/api/stream",
            })
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

        if p == "/api/ref":
            # 项目参考图清单（面板"参考图"卡片）——路径全部来自项目档案，不接受外来路径
            if not self._authed():
                self._send_err(401, "unauthorized")
                return
            from .core import project as project_mod
            qs = parse_qs(u.query)
            pid = self._safe_pid(str((qs.get("project") or [""])[0]))
            data = {"project": pid, "characters": [], "scenes": []}
            if pid:
                proj = project_mod.Project.load(pid)
                for kind, book, bucket in (("character", proj.characters, "characters"),
                                           ("scene", proj.scenes, "scenes")):
                    for key, hit in book.items():
                        path = hit.get("path", "")
                        ok = bool(path) and os.path.isfile(path)
                        data[bucket].append({
                            "key": key, "name": hit.get("name") or key, "exists": ok,
                            # placeholder=系统造的占位素材（色卡），不是用户传的参考图——
                            # 面板要标出来，否则用户以为自己的参考图变成色卡了
                            "placeholder": bool(hit.get("placeholder")),
                            "size": os.path.getsize(path) if ok else 0,
                            "updated": hit.get("updated"),
                            "url": "/api/ref/image?project=" + quote(pid) +
                                   "&kind=" + kind + "&key=" + quote(key)})
            self._send_json(data)
            return

        if p == "/api/ref/image":
            if not self._authed():
                self._send_err(401, "unauthorized")
                return
            from .core import project as project_mod
            qs = parse_qs(u.query)
            pid = self._safe_pid(str((qs.get("project") or [""])[0]))
            kind = str((qs.get("kind") or ["character"])[0]).strip().lower()
            key = self._fix_mojibake(str((qs.get("key") or [""])[0]))
            hit = project_mod.Project.load(pid).asset(kind, key) if (pid and key) else None
            if kind not in REF_KINDS or not hit:
                self._send_err(404, "参考图不存在")
                return
            with open(hit["path"], "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg" if hit["path"].endswith(".jpg")
                             else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/artifact":
            # 取回某个 run 的真实产物（local provider 渲出来的 mp4/png）。
            # 只看 run 目录下的 artifacts/，文件名做白名单校验，防穿越。
            qs = parse_qs(u.query)
            run_id = (qs.get("run_id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            if (not run_id or not name or "/" in name or "\\" in name
                    or name.startswith(".") or not run_id.replace("-", "").isalnum()):
                self._send_err(400, "run_id/name 不合法")
                return
            path = os.path.join(RUNS_DIR, run_id, "artifacts", name)
            if not os.path.isfile(path):
                self._send_err(404, f"产物不存在: {name}")
                return
            ctype = "video/mp4" if name.endswith(".mp4") else \
                "image/png" if name.endswith(".png") else "application/octet-stream"
            size = os.path.getsize(path)
            # Range 支持：手机浏览器里的 <video> 要按段取（拖动进度/快速起播都靠它），
            # 只回整文件时播放器容易一直转圈（真机反馈「视频看不了」）
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", (self.headers.get("Range") or "").strip())
            if m and size > 0 and (m.group(1) or m.group(2)):
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Disposition", f'inline; filename="{name}"')
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
                return
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f'inline; filename="{name}"')
            self.end_headers()
            self.wfile.write(body)
            return

        if p.startswith("/api/run/") and p.endswith("/files"):
            # 该 run 产物目录里**真实存在**的文件（磁盘为准）。为什么单独一个口：
            # 有的任务把片子写出来了却收尾判失败（实测 COMPOSE），只按 run.done.outputs
            # 展示就会漏掉成片——真机反馈「生成的视频看不了」就是这个。
            run_id = p[len("/api/run/"):-len("/files")]
            if not run_id or not run_id.replace("-", "").isalnum():
                self._send_err(400, "run_id 不合法")
                return
            adir = os.path.join(RUNS_DIR, run_id, "artifacts")
            files = []
            try:
                for name in sorted(os.listdir(adir)):
                    fp = os.path.join(adir, name)
                    if os.path.isfile(fp) and name.lower().endswith(MEDIA_EXTS):
                        files.append({"name": name, "size": os.path.getsize(fp)})
            except OSError:
                self._send_err(404, f"run 不存在: {run_id}")
                return
            self._send_json({"run_id": run_id, "files": files})
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
            # 状态要看"事件里有没有 run.done"，而不是"最后一条是不是 run.done"——
            # 收尾之后还会有 notify 之类的事件（实测踩过：notify 一加，状态永远停在 running）
            status = "done" if any(e.get("type") == "run.done" for e in events) else "running"
            self._send_json({"run_id": run_id, "status": status, "events": events})
            return

        if p.startswith("/api/stream"):
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

    def do_DELETE(self) -> None:
        u = urlparse(self.path)
        if u.path != "/api/ref":
            self._send_err(404, f"未知路径: {u.path}")
            return
        if not self._authed():
            self._send_err(401, "unauthorized")
            return
        from .core import project as project_mod
        qs = parse_qs(u.query)
        body = self._read_body()
        pid = self._safe_pid(str(body.get("project") or (qs.get("project") or [""])[0]))
        kind = str(body.get("kind") or (qs.get("kind") or ["character"])[0]).strip().lower()
        key = self._fix_mojibake(str(body.get("key") or (qs.get("key") or [""])[0])).strip()
        if not pid or kind not in REF_KINDS or not key:
            self._send_err(400, "需要 project / kind / key")
            return
        removed = project_mod.Project.load(pid).unregister_asset(kind, key)
        self._send_json({"ok": removed, "removed": removed,
                         "detail": "已注销" if removed else "档案里没有这个键"})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path.startswith("/hook/"):
            # 诊断/回调上报口：CI（比如 GitHub Actions）把结果 POST 回来，落到 ~/.hxmv/hooks/
            # 公开可写但做了硬限制（只允许白名单文件名 + 每次最多 64KB + 只追加），
            # 用途单一：出包/部署链路的"回传眼镜"，方便在没有日志权限时排障。
            name = u.path[len("/hook/"):]
            if (not name or "/" in name or "\\" in name or name.startswith(".")
                    or not all(c.isalnum() or c in "-._" for c in name)):
                self._send_err(400, "名字不合法")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length <= 0 or length > 65536:
                self._send_err(413, "上报体大小不合法")
                return
            body = self.rfile.read(length)
            os.makedirs(HOOKS_DIR, exist_ok=True)
            line = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "from": self.headers.get("User-Agent", ""),
                               "data": body.decode("utf-8", "ignore")}, ensure_ascii=False)
            with open(os.path.join(HOOKS_DIR, name + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._send_json({"ok": True})
            return
        if u.path == "/api/config":
            # 保存接口配置（面板设置页用）。Key 只写进 ~/.hxmv/config.json(600)，不回传明文。
            if not self._authed():
                self._send_err(401, "unauthorized：需要 ?token= 或 X-Hxmv-Token 头")
                return
            from .core import config, llm
            body = self._read_body()
            provider = str(body.get("provider", "")).strip().lower()
            if provider not in ("zhipu", "kling"):
                self._send_err(400, "不支持的 provider")
                return
            if str(body.get("key", "")).strip():
                config.set_api_key(provider, str(body["key"]))
            for opt in ("video_model", "vlm_model"):
                if opt in body:
                    config.set_option(provider, opt, str(body.get(opt, "")))
            self._send_json({"ok": True, "vision": {"ready": llm.vision_available(),
                                                   "model": llm.vision_model()}})
            return
        if u.path == "/api/ref":
            # **上传参考图**（面板用）：图片走 base64 JSON（std lib 解析 multipart 太脏）。
            # 自动裁主视觉由 media/sheet.py 干；preview=true 只回预览不落库，手机上先看一眼再存。
            if not self._authed():
                self._send_err(401, "unauthorized")
                return
            import tempfile

            from .core import project as project_mod
            from .media import sheet
            body = self._read_body()
            pid = self._safe_pid(str(body.get("project", "")))
            if not pid:
                self._send_err(400, "项目名不合法（只允许中英文、数字、- 和 _）")
                return
            kind = str(body.get("kind", "character")).strip().lower()
            if kind not in REF_KINDS:
                self._send_err(400, "kind 只能是 character/scene")
                return
            blob = self._decode_image(str(body.get("image", "")))
            if not blob:
                self._send_err(400, "图片数据不合法或超过 12MB（支持 JPEG/PNG）")
                return
            mode = str(body.get("mode", "auto")).strip().lower()
            if mode not in sheet.MODES:
                self._send_err(400, f"裁切模式只能是 {'/'.join(sheet.MODES)}")
                return
            src = os.path.join(tempfile.mkdtemp(prefix="hxmv-ref-"), "src.bin")
            with open(src, "wb") as f:
                f.write(blob)
            try:
                if body.get("preview"):
                    out = src + ".jpg"
                    info = sheet.normalize(src, out, mode)
                    with open(out, "rb") as f:
                        preview = base64.b64encode(f.read()).decode()
                    self._send_json({"ok": True, "preview": "data:image/jpeg;base64," + preview,
                                     "box": info["box"], "src_size": info["src_size"],
                                     "out_size": info["out_size"], "mode": info["mode"],
                                     "engine": info["engine"]})
                    return
                key = str(body.get("key", "")).strip()
                if not key:
                    self._send_err(400, "需要给一个键名（镜头约束里的角色名/场景名）")
                    return
                proj = project_mod.Project.load(pid)
                info = sheet.save_reference(proj, kind, key, src, mode,
                                            name=str(body.get("name", "")).strip() or key,
                                            style=proj.style)
                self._send_json({"ok": True, "project": pid, "key": info["key"],
                                 "kind": info["kind"], "box": info["box"],
                                 "src_size": info["src_size"], "out_size": info["out_size"],
                                 "mode": info["mode"], "engine": info["engine"],
                                 "path": info["path"]})
            except (ValueError, OSError, RuntimeError) as e:
                self._send_err(400, f"处理失败: {e}")
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass
            return

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
        project = str(body.get("project", "")).strip()
        run_id = MANAGER.submit(goal, provider, project)
        self._send_json({"run_id": run_id, "provider": provider or "mock",
                         "project": project or None}, 202)


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
