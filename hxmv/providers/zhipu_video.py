"""智谱 AI（BigModel）视频生成 provider——**真 API 接入**。

- 模型：`cogvideox-flash`（**免费**）默认；`cogvideox-3` / `cogvideox-2` 可切（付费）
- 文生视频 / **图生视频**：给首帧图（image_url，支持 base64）——
  图生视频才是"角色一致性"的真抓手：HxMV 把项目档案里那张角色参考图喂进去，
  跨镜头同一个角色就有了硬约束（比调 prompt 强度实在）。
- 异步两段式：`POST /videos/generations` 提交 → `GET /async-result/{id}` 轮询 → 下载 mp4 落盘
- 失败分道：网络/429/5xx → `ProviderError(retryable=True)`（loop 按基础设施重试）；
  鉴权/参数错 → `retryable=False`（别傻重试）；**任务本身 FAILED** 也抛 retryable（换个尝试再来）
- 产物**不带任何缺陷标签**：真实视频的物理质量交给 L1/L2 从像素里量（v0.4 的真眼睛），
  闭环"观察→判断→修正"在真 API 上照样跑。

配置（环境变量优先，其次 ~/.hxmv/config.json）：
    HXMV_ZHIPU_KEY / ZHIPUAI_API_KEY   密钥（`python3 -m hxmv --set-key zhipu <KEY>` 可写入配置）
    HXMV_ZHIPU_MODEL                   默认 cogvideox-flash
    HXMV_ZHIPU_BASE                    默认 https://open.bigmodel.cn/api/paas/v4（测试可改指向本地仿真端点）
    HXMV_ZHIPU_TIMEOUT                 单任务轮询上限秒数（默认 420）
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from ..core import config
from ..media import probe
from .base import ProviderError, VideoProvider

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
# 内部语义分辨率 → 智谱合法 size（flash 免费且支持到 4K，统一给 16:9 高清，避免给非法枚举）
SIZE_MAP = {"480p": "1920x1080", "720p": "1920x1080", "1080p": "1920x1080",
            "4k": "3840x2160", "2160p": "3840x2160"}
COST_UNITS = {"cogvideox-flash": 0.0, "cogvideox-3": 1.05, "cogvideox-2": 0.7}  # 元/次（flash 免费）


def _data_url(path: str) -> str:
    """本地图片 → base64 data URL（智谱的 image_url 接受 base64，免去自建图床）。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


def _encoded_frame(image: str, w: int | None, hgt: int | None, outdir: str) -> str | None:
    """把参考图走一遍**和视频一样的编码管线**（同分辨率/同 CRF）再取出帧。

    为什么必须有它：真实 AI 视频没法像本地渲染那样造"零漂移基线"，但 PNG 参考图
    和 h264 解出来的帧之间天然差 0.03 左右（高频被压掉）。拿原图直接比会把"忠实还原"
    也判成不一致。用同管线基线，量出来的才是"模型到底有没有听参考图的话"。
    """
    if not (w and hgt):
        return None
    stem = os.path.splitext(os.path.basename(image))[0]
    path = os.path.join(outdir, f"base_{w}x{hgt}_{stem}.png")
    if os.path.exists(path):
        return path
    tmp = os.path.join(outdir, f"_basetmp_{w}x{hgt}_{stem}.mp4")
    rc, _ = probe._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1",
                        "-i", image, "-frames:v", "1", "-vf", f"scale={w}:{hgt},format=yuv420p",
                        *probe.encoder_args(26), tmp])
    if rc != 0:
        return None
    rc, _ = probe._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", tmp, "-frames:v", "1", path])
    try:
        os.remove(tmp)
    except OSError:
        pass
    return path if rc == 0 and os.path.isfile(path) else None


class ZhipuVideoProvider(VideoProvider):
    name = "zhipu"
    action_map = {"GENERATE_SHOT": "videos/generations", "GENERATE_CHARACTER": "image",
                  "GENERATE_SCENE": "image", "COMPOSE": "concat"}

    def __init__(self, project=None, outdir: str | None = None, api_key: str | None = None):
        self.project = project
        self.key = api_key or config.api_key("zhipu")
        if not self.key:
            raise ProviderError(
                "未配置智谱 API Key（用 `python3 -m hxmv --set-key zhipu <KEY>` 或设 HXMV_ZHIPU_KEY）",
                retryable=False)
        self.base = os.environ.get("HXMV_ZHIPU_BASE", DEFAULT_BASE).rstrip("/")
        self.model = os.environ.get("HXMV_ZHIPU_MODEL", "cogvideox-flash")
        self.timeout = float(os.environ.get("HXMV_ZHIPU_TIMEOUT", "420"))
        self.outdir = outdir or os.environ.get("HXMV_ARTIFACTS") or (
            os.path.join(project.dir, "artifacts") if project else
            os.path.expanduser(f"~/.hxmv/artifacts/{time.strftime('%Y%m%d-%H%M%S')}"))
        os.makedirs(self.outdir, exist_ok=True)
        self.episode = None
        # 资产图与成片拼接复用已测好的本地实现（真 API 只负责"让画面动起来"）：省额度也少一份代码
        from .local_render import LocalRenderProvider
        self._local = LocalRenderProvider(project=project, outdir=self.outdir)

    # ---------- HTTP ----------
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json;charset=utf-8",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        return self._send(req, "提交任务")

    def _get_json(self, path: str) -> dict:
        req = urllib.request.Request(f"{self.base}/{path}",
                                     headers={"Authorization": f"Bearer {self.key}"})
        return self._send(req, "查询任务")

    def _send(self, req, what: str) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            # 401/403 = 鉴权；400 = 参数错（重试无用）；429/5xx = 可重试
            retryable = e.code not in (400, 401, 403)
            raise ProviderError(f"{what}失败 HTTP {e.code}: {detail}", retryable=retryable)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"{what}网络异常: {e}", retryable=True)
        except json.JSONDecodeError as e:
            raise ProviderError(f"{what}返回非 JSON: {e}", retryable=True)

    # ---------- 任务 ----------
    def _submit(self, task, prompt: str, image: str | None) -> dict:
        size = SIZE_MAP.get(str(task.input.get("resolution") or ""), "1920x1080")
        duration = int(task.input.get("duration") or 5)
        body = {"model": self.model, "prompt": prompt,
                "quality": "speed" if os.environ.get("HXMV_ZHIPU_SPEED") else "quality",
                "with_audio": bool(task.input.get("with_audio", False)),
                "size": size, "fps": int(task.input.get("fps") or 30),
                "duration": 10 if duration > 7 else 5}
        if image:
            body["image_url"] = image      # 图生视频：首帧 = 项目档案里的角色/场景参考图
        return self._post("videos/generations", body)

    def _poll(self, task_id: str) -> dict:
        deadline = time.time() + self.timeout
        delay = 5.0
        while time.time() < deadline:
            data = self._get_json(f"async-result/{task_id}")
            status = str(data.get("task_status") or "").upper()
            if status == "SUCCESS":
                return data
            if status in ("FAIL", "FAILED"):
                raise ProviderError(f"上游任务失败: {json.dumps(data, ensure_ascii=False)[:300]}",
                                    retryable=True)
            time.sleep(delay)
            delay = min(delay * 1.4, 15.0)
        raise ProviderError(f"任务 {task_id} 超时（{self.timeout:.0f}s）", retryable=True)

    def _download(self, url: str, dest: str) -> None:
        for attempt in (1, 2, 3):
            try:
                with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                if os.path.getsize(dest) > 1024:
                    return
                raise ProviderError("下载到的文件过小", retryable=True)
            except (urllib.error.URLError, OSError) as e:
                if attempt == 3:
                    raise ProviderError(f"下载产物失败: {e}", retryable=True)
                time.sleep(2 * attempt)

    def _postprocess(self, path: str, task) -> str:
        """把 Refiner 要求的工程修正**真做在产物上**（不是"改了参数就当修好了"）。

        真实 API 只负责画面；音量增益、片头黑场这类修正直接对下载到的文件做——
        省额度、更快，Critic 复测时量到的是真变化。
        """
        gain = task.input.get("audio_gain_db")
        if gain:
            tmp = path.replace(".mp4", "_gain.mp4")
            rc, _ = probe._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                                "-c:v", "copy", "-af", f"volume={float(gain):.1f}dB",
                                "-c:a", "aac", tmp])
            if rc == 0 and os.path.isfile(tmp):
                os.replace(tmp, path)
        if task.input.get("trim_black"):
            lead = probe.leading_black_seconds(path)
            if lead > 0.05:
                tmp = path.replace(".mp4", "_trim.mp4")
                rc, _ = probe._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                    "-ss", f"{lead}", "-i", path, *probe.encoder_args(26),
                                    "-c:a", "aac", "-pix_fmt", "yuv420p", tmp])
                if rc == 0 and os.path.isfile(tmp):
                    os.replace(tmp, path)
        return path

    # ---------- 入口 ----------
    def generate(self, task) -> dict:
        if task.action in ("GENERATE_CHARACTER", "GENERATE_SCENE", "STORYBOARD", "COMPOSE"):
            return self._local.generate(task)      # 资产图/分镜/成片：复用本地实现

        # 复用在最前面：档案里已有这个画面 → 一张都不重新生成（也不消耗额度）
        from ..core.project import fingerprint
        cons, inp = task.constraints, task.input
        fp = fingerprint({"prompt": inp.get("prompt"), "duration": inp.get("duration"),
                          "resolution": inp.get("resolution"), "fps": inp.get("fps"),
                          "seed": inp.get("seed"),
                          "reference_strength": cons.get("reference_strength"),
                          "motion_scale": cons.get("motion_scale"),
                          "audio_gain_db": inp.get("audio_gain_db"),
                          "trim_black": bool(inp.get("trim_black")),
                          "character": cons.get("character"), "scene": cons.get("scene"),
                          "style": cons.get("style") or (self.project.style if self.project else None)})
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

        # 参考图：项目档案里的角色参考图（图生视频的首帧）→ 角色一致性硬约束
        image = None
        ref_path = None
        if self.project:
            for field, kind in (("character", "character"), ("scene", "scene")):
                key = cons.get(field)
                if key:
                    hit = self.project.asset(kind, str(key))
                    if hit:
                        ref_path, image = hit["path"], _data_url(hit["path"])
                        break

        prompt = self._build_prompt(task)
        submitted = self._submit(task, prompt, image)
        task_id = submitted.get("id") or submitted.get("request_id")
        if not task_id:
            raise ProviderError(f"提交未返回任务 id: {json.dumps(submitted, ensure_ascii=False)[:200]}",
                                retryable=True)
        result = self._poll(str(task_id))
        videos = result.get("video_result") or []
        url = (videos[0].get("url") if videos else None) or result.get("url")
        if not url:
            raise ProviderError(f"任务成功但没给视频地址: {json.dumps(result, ensure_ascii=False)[:200]}",
                                retryable=True)

        path = os.path.join(self.outdir, f"shot_{task.task_id}.mp4")
        self._download(url, path)
        path = self._postprocess(path, task)       # 工程修正真做在产物上（音量/黑场）
        self._local._slot_bind(task, path, fp)     # 让成片层知道这个镜头在哪
        cont = probe.probe_container(path) or {}
        out = {
            "media": path, "reference": ref_path,
            # 图生视频：首帧应当贴近参考图 → 一致性采样点取 0%，
            # 并用"同编码管线的参考帧"当基线（否则编码差异会被误判成不一致）
            "reference_baseline": _encoded_frame(ref_path, cont.get("width"), cont.get("height"),
                                                 self.outdir) if ref_path else None,
            "consistency_at_ratio": 0.0 if ref_path else None,
            "duration": cont.get("duration"), "fps": cont.get("fps"),
            "resolution": f"{cont.get('width')}x{cont.get('height')}" if cont.get("width") else None,
            "params": {"provider": self.name, "model": self.model, "task_id": str(task_id),
                       "prompt": prompt[:200], "image_to_video": bool(image),
                       "reference_strength": cons.get("reference_strength")},
            "reused": False, "fingerprint": fp,
            "cost_units": self.estimate_cost(task.action),
        }
        if self.project:
            self.project.register_shot(
                fp, path, episode=getattr(self, "episode", None),
                result={k: v for k, v in out.items() if k != "cost_units"},
                params=out["params"], prompt=inp.get("prompt"),
                task_input={k: inp.get(k) for k in ("prompt", "duration", "seed", "resolution",
                                                    "fps", "audio_gain_db", "trim_black")},
                task_constraints={k: cons.get(k) for k in ("character", "scene", "style",
                                                           "reference_strength", "scene_strength",
                                                           "motion_scale")})
        return out

    @staticmethod
    def _build_prompt(task) -> str:
        """把分镜 + 风格约束拼成给生成模型看的提示词（英文更稳，CogVideoX 系对英文最敏感）。"""
        inp, cons = task.input, task.constraints
        parts = [str(inp.get("prompt") or inp.get("goal") or "").strip()]
        if cons.get("style"):
            parts.append(f"style: {cons['style']}")
        if cons.get("character"):
            parts.append(f"keep the same character as the reference image ({cons['character']})")
        if cons.get("continuity"):
            parts.append("consistent look with previous shots, cinematic lighting")
        return "，".join(p for p in parts if p)

    def set_episode(self, n) -> None:
        self.episode = n
        self._local.set_episode(n)

    def estimate_cost(self, action: str) -> float:
        return COST_UNITS.get(self.model, 0.0) if action == "GENERATE_SHOT" else 0.0
