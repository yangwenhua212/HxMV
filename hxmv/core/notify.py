"""把"做完了"主动发出去——客户端（HxSync）不常驻也能收到成品。

为什么要有它：HxSync 在手机上，SSE 长连接不一定能一直挂着（切后台、省电策略、
网络切换）。所以除了"客户端来拉"，还要有"服务端主动推"这条路：

    HXMV_NOTIFY_URL=http://...  或  https://...      → POST JSON（自建 / n8n / 飞书机器人网关都行）
    HXMV_NOTIFY_URL=feishu:<webhook>                → 飞书群机器人卡片（中文可读）
    HXMV_PUBLIC_BASE=https://hxmv.eraherm.com       → 产物下载链接用这个域名拼（不设则用请求 Host）

推的内容是**自足的**：干了什么、成不成、几次尝试、花了多少、成片在哪（可直接下载的 URL）。
收到就能直接播/存，不需要再回头查接口。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .. import USER_AGENT

ARTIFACT_EXT = (".mp4", ".png", ".jpg", ".webp")
MAX_ARTIFACTS = 8


def collect_artifacts(artifacts_dir: str, base: str = "", run_id: str = "") -> list[dict]:
    """从产物目录收出可下载清单（成片排前面，按时间倒序）。"""
    if not artifacts_dir or not os.path.isdir(artifacts_dir):
        return []
    files = []
    for name in os.listdir(artifacts_dir):
        if not name.lower().endswith(ARTIFACT_EXT) or name.startswith("base_"):
            continue        # base_* 是内部一致性基线帧，不是给人看的成品
        path = os.path.join(artifacts_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append({"name": name, "path": path, "size": st.st_size, "mtime": st.st_mtime,
                      "kind": "final" if name.startswith("final") else
                              ("asset" if name.startswith("asset") else "shot")})
    files.sort(key=lambda f: (0 if f["kind"] == "final" else 1, -f["mtime"]))
    out = files[:MAX_ARTIFACTS]
    for f in out:
        if base and run_id:
            # 与服务端 /api/artifact 契约一致：run_id + name 两个参数都要
            f["url"] = (f"{base.rstrip('/')}/api/artifact?run_id={urllib.parse.quote(run_id)}"
                        f"&name={urllib.parse.quote(f['name'])}")
    return out


def build_payload(goal: str, done: dict, artifacts_dir: str, base: str = "",
                  run_id: str = "") -> dict:
    """拼一条自足的"完工通知"。"""
    outputs = [str(o) for o in (done.get("outputs") or [])]
    return {
        "event": "run.done",
        "run_id": run_id,
        "goal": goal,
        "phase": done.get("phase", "DONE"),
        "ok": done.get("phase") == "DONE" and not done.get("failed"),
        "completed": len(done.get("completed") or []),
        "failed": len(done.get("failed") or []),
        "attempts": done.get("attempts"),
        "cost_units": done.get("cost_units"),
        "iterations": done.get("iterations"),
        "artifacts": collect_artifacts(artifacts_dir, base, run_id),
        "outputs": outputs[:3],
        # 实例若开了 token，客户端取产物时自己补 &token=（别把凭据塞进通知里到处飞）
        "needs_token": bool(os.environ.get("HXMV_WEB_TOKEN")),
        "text": _human(goal, done, outputs),
    }


def _human(goal: str, done: dict, outputs: list[str]) -> str:
    verdict = "✅ 完成" if done.get("phase") == "DONE" and not done.get("failed") else "⚠ 有未完成项"
    final = next((o for o in outputs if str(o).endswith(".mp4")), outputs[-1] if outputs else "")
    return (f"{verdict}｜{goal}\n"
            f"任务 {len(done.get('completed') or [])} 项完成"
            f"{('/ ' + str(len(done.get('failed'))) + ' 项失败') if done.get('failed') else ''}"
            f"｜共 {done.get('attempts')} 次尝试｜成本 {done.get('cost_units')}\n"
            f"成片：{os.path.basename(final) if final else '（无）'}")


def notify(payload: dict, url: str = "") -> str:
    """推出去。返回一句人话结果（"已推送" / "跳过" / 失败原因），**永不抛异常**。

    通知失败绝不能影响生产本身——这是原则。
    """
    url = (url or os.environ.get("HXMV_NOTIFY_URL", "")).strip()
    if not url:
        return "未配置 HXMV_NOTIFY_URL，跳过推送"
    try:
        if url.startswith("feishu:"):
            body, target = _feishu_body(payload), url.split("feishu:", 1)[1].strip()
        else:
            body, target = payload, url
        req = urllib.request.Request(
            target, data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json;charset=utf-8",
                     "User-Agent": USER_AGENT}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"已推送（HTTP {resp.status}）"
    except urllib.error.HTTPError as e:
        return f"推送失败 HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"推送失败 {e}"
    return "已推送"


def _feishu_body(payload: dict) -> dict:
    """飞书自定义机器人消息卡片（中文可读，带产物链接）。"""
    lines = [payload.get("text", ""), ""]
    for a in payload.get("artifacts", [])[:4]:
        if a.get("url"):
            lines.append(f"🎬 [{a['name']}]({a['url']})  {a['size'] // 1024} KB")
    return {"msg_type": "text", "content": {"text": "\n".join(x for x in lines if x is not None)}}
