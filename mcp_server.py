#!/usr/bin/env python3
"""HxMV MCP server —— 把小忺自己的内容生产能力（HxMV）变成原生工具。

为什么走 HTTP 而不是直接 import 内核：
  HxMV 内核（RunManager / 记忆库 / 项目档案）是**单实例有状态**的（一人一实例，见 docs/DESIGN.md），
  面板进程已经持着这些状态。MCP 再开一个进程去自己跑 run，会和面板抢状态、抢产物目录。
  所以这里只做「转发」：MCP 进程 ~30MB，形态和 eraherm-memory 的 MCP 一样。

工具语义（重要）：
  - hxmv_chat  ：**不落盘、不开工、不花钱**。说需求 → 回话 + 可执行 goal。
  - hxmv_make  ：**真开工**。会调用外部视频模型，并占用一个 run。
  - hxmv_watch ：看进度，别空转。
  - hxmv_files ：按磁盘为准列产物；**返回本地绝对路径**，可直接当飞书附件发出去。
  - hxmv_discard：不满意就删（删产物 + 撤销项目档案登记）。

安全闸门（这是「能力」也是「风险」，所以硬性约束写在这里，不靠调用方自觉）：
  1. run_id 一律白名单校验（`YYYYMMDD-HHMMSS-xxxx`）—— 挡住 `../../` 之类的路径穿越。
  2. 付费档位默认拒绝：必须显式 allow_paid=true 才按次出片（防的是提示注入/循环调用烧钱）。
  3. 付费出片有**每日上限**（默认 5 次，HXMV_MAX_PAID_PER_DAY 可调）—— 防跑飞。
  4. 每次开工/删片写审计日志 ~/.hxmv/mcp_audit.log（谁在什么时候干了什么）。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HXMV_BASE", "http://127.0.0.1:8668")
ENV_FILE = os.path.expanduser(os.environ.get("HXMV_PANEL_ENV", "~/.hxmv/panel.env"))
RUNS_DIR = os.path.expanduser("~/.hxmv/runs")
SPEND_FILE = os.path.expanduser(os.environ.get("HXMV_SPEND_FILE", "~/.hxmv/mcp_spend.json"))
AUDIT_FILE = os.path.expanduser(os.environ.get("HXMV_AUDIT_FILE", "~/.hxmv/mcp_audit.log"))

# 只有这些档位是「不花钱」的；未知档位一律当付费（fail-safe：宁可不跑，不许悄悄烧钱）
FREE_VIDEO_MODELS = {"cogvideox-flash"}
KNOWN_PROVIDERS = {"zhipu", "kling", "local"}
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def _token() -> str:
    tok = os.environ.get("HXMV_WEB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("HXMV_WEB_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _req(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    """调面板 API。GET 传 None，POST 传 payload。"""
    url = BASE.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-Hxmv-Token", _token())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HxMV API {exc.code} {path}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"连不上 HxMV 面板（{BASE}）：{exc}") from exc


def _out(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _bad_run_id(run_id: str) -> bool:
    return not RUN_ID_RE.match((run_id or "").strip())


def _local_paths(run_id: str) -> list[dict]:
    """产物磁盘路径（按磁盘为准，不按「任务成功」）。run_id 必须已过白名单校验。"""
    if _bad_run_id(run_id):
        return []
    adir = os.path.join(RUNS_DIR, run_id, "artifacts")
    real = os.path.realpath(adir)
    if not real.startswith(os.path.realpath(RUNS_DIR) + os.sep):
        return []          # 双保险：解析后必须仍在 runs 目录内
    if not os.path.isdir(real):
        return []
    items = []
    for name in sorted(os.listdir(real)):
        p = os.path.join(real, name)
        if os.path.isfile(p):
            items.append({"name": name, "path": p, "bytes": os.path.getsize(p)})
    return items


def _video_model() -> str:
    """当前视频档位。查不到就返回空串 —— 调用方把空串当「付费」处理（fail-safe）。"""
    try:
        cfg = _req("/api/config", timeout=15)
        return str(((cfg.get("options") or {}).get("video_model") or "")).strip()
    except RuntimeError:
        return ""


def _spend_today() -> int:
    try:
        with open(SPEND_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return int(d.get("paid_runs", 0)) if d.get("date") == time.strftime("%Y-%m-%d") else 0
    except (OSError, ValueError):
        return 0


def _bump_spend() -> int:
    n = _spend_today() + 1
    try:
        os.makedirs(os.path.dirname(SPEND_FILE), exist_ok=True)
        with open(SPEND_FILE, "w", encoding="utf-8") as fh:
            json.dump({"date": time.strftime("%Y-%m-%d"), "paid_runs": n}, fh)
        os.chmod(SPEND_FILE, 0o600)
    except OSError:
        pass
    return n


def _audit(tool: str, detail: dict) -> None:
    """审计（只记事实，不记令牌）。失败不打断主流程。"""
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        line = json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, **detail},
                          ensure_ascii=False)
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def create_mcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("需要 mcp SDK：pip install 'mcp>=1.9.0'") from exc

    mcp = FastMCP(
        "hxmv",
        instructions=(
            "HxMV 自主内容生产闭环工具（经 HTTP 转发到本机面板，不依赖面板之外的状态）。"
            "hxmv_chat 提需求/改需求（不花钱不落档）；hxmv_make 真开工出片（付费档必须 allow_paid=true，且有每日上限）；"
            "hxmv_watch 看进度；hxmv_files 拿产物本地路径（可直接当附件发）；hxmv_discard 删掉不满意的片。"
        ),
    )

    @mcp.tool()
    def hxmv_chat(message: str, project: str = "", history: str = "") -> str:
        """跟 HxMV 说需求（不落盘/不开工/不花钱）。

        message  : 用户原话，如「一条金毛在海边追浪，8 秒」「刚才那条改成夜晚」。
        project  : 可选，项目名（同一项目共享角色/场景设定）。
        history  : 可选，最近几轮对话（JSON 字符串，形如 [{"role":"user","text":"..."}]），带上才能改准。
        返回 JSON：reply（它回你的话）/ goal（可执行目标，非空才值得开工）/ llm（是否真的通了模型）。
        """
        try:
            return _out(_req("/api/chat", {"message": message, "project": project, "history": history}))
        except RuntimeError as exc:
            return f"失败：{exc}"

    @mcp.tool()
    def hxmv_make(goal: str, project: str = "", provider: str = "", allow_paid: bool = False) -> str:
        """**真开工**出片：提交一个生产任务。

        goal       : 可执行目标（建议先 hxmv_chat 拿到 goal 再传进来）。
        project    : 可选，项目名。
        provider   : 可选，视频后端（zhipu / kling / local）。
        allow_paid : 付费档位必须显式传 true 才开工（免费档不用传）。付费档有每日上限。

        返回 JSON：run_id。之后用 hxmv_watch / hxmv_files 跟进。
        """
        if provider and provider not in KNOWN_PROVIDERS:
            return f"失败：provider 只能是 {sorted(KNOWN_PROVIDERS)} 之一"
        model = _video_model()
        paid = model not in FREE_VIDEO_MODELS          # 未知档位也算付费（fail-safe）
        if paid and not allow_paid:
            return _out({
                "stopped": True,
                "reason": f"当前视频档位是 {model or '未知'}（按次计费），未开工",
                "how_to_proceed": "确认要按次出片就再调一次并带 allow_paid=true（免费档 cogvideox-flash 不用）",
                "paid_today": _spend_today(),
                "daily_cap": int(os.environ.get("HXMV_MAX_PAID_PER_DAY", "5")),
            })
        if paid:
            cap = int(os.environ.get("HXMV_MAX_PAID_PER_DAY", "5"))
            if _spend_today() >= cap:
                return _out({"stopped": True, "reason": f"今天付费出片已达上限 {cap} 次", "paid_today": _spend_today()})
        payload = {"goal": goal}
        if project:
            payload["project"] = project
        if provider:
            payload["provider"] = provider
        try:
            d = _req("/api/run", payload, timeout=60)
        except RuntimeError as exc:
            return f"失败：{exc}"
        if paid:
            _bump_spend()
        _audit("hxmv_make", {"goal": goal[:120], "project": project, "provider": provider or model,
                             "paid": paid, "run_id": d.get("run_id")})
        return _out(d)

    @mcp.tool()
    def hxmv_watch(run_id: str = "", limit: int = 12) -> str:
        """看进度：给 run_id 看这一次；不给就看最近的片（状态 done/running/aborted + 最近事件）。"""
        if run_id and _bad_run_id(run_id):
            return "失败：run_id 格式不对（应为 YYYYMMDD-HHMMSS-xxxx）"
        try:
            if run_id:
                d = _req(f"/api/run/{run_id}", timeout=30)
                events = d.get("events") or []
                return _out({
                    "run_id": run_id,
                    "status": d.get("status"),
                    "events": events[-max(1, int(limit)):],
                    "files": [f["name"] for f in _local_paths(run_id)],
                })
            d = _req("/api/runs?limit=8", timeout=30)
            return _out([
                {"run_id": r.get("run_id"), "goal": r.get("goal"), "ts": r.get("ts"),
                 "status": r.get("status"), "poster": r.get("poster")}
                for r in (d.get("runs") or [])
            ])
        except RuntimeError as exc:
            return f"失败：{exc}"

    @mcp.tool()
    def hxmv_files(run_id: str) -> str:
        """列这次任务的产物（以磁盘为准）。返回本地绝对路径 —— 可直接当附件发给用户。"""
        if _bad_run_id(run_id):
            return "失败：run_id 格式不对（应为 YYYYMMDD-HHMMSS-xxxx）"
        try:
            d = _req(f"/api/run/{run_id}/files", timeout=30)
            served = d.get("files") or []
        except RuntimeError:
            served = []
        return _out({"run_id": run_id, "on_server": served, "local": _local_paths(run_id)})

    @mcp.tool()
    def hxmv_discard(run_id: str) -> str:
        """删掉不满意的片：删产物文件 + 撤销它在项目档案里的登记（别让它继续当设定影响下一部）。"""
        if _bad_run_id(run_id):
            return "失败：run_id 格式不对（应为 YYYYMMDD-HHMMSS-xxxx）"
        try:
            d = _req("/api/discard", {"run_id": run_id}, timeout=60)
        except RuntimeError as exc:
            return f"失败：{exc}"
        _audit("hxmv_discard", {"run_id": run_id})
        return _out(d)

    return mcp


if __name__ == "__main__":
    create_mcp().run()
