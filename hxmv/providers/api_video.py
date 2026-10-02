"""OpenAI 风格「异步两段式」AI 视频服务的通用实现（多 API 的公共部分）。

**一家 API 与另一家的差别其实只有五处**，子类各覆盖一处，其余全在这里：

| 差异点 | 子类提供 | 智谱 | Agnes |
|---|---|---|---|
| 视频任务请求体 | `_submit_body()` | `videos/generations` + size/fps/quality | `videos` + mode/seconds/aspect_ratio |
| 轮询取结果 | `_poll_url()` | `async-result/{id}`，`task_status` | `/agnesapi?video_id=…&model_name=…`，`status` |
| 出图请求体 | `_image_body()` | `size="1344x768"` | `size="1K"` + `ratio` |
| 产物下载要不要令牌 | `download_auth` | 要 | **不要**（Agnes 输出域名带了会 401） |
| 能力与档位 | `camera_support` / `COST_UNITS` / 首尾帧模型 | flash 只有提示词 | keyframe 首尾帧 |

其余——档案复用、参考图解析、首帧生成、尾帧派生、指纹与复用、工程修正
（音量/黑场真做在产物上）、结果登记——**全在基类，子类一行都不用重写**。
这是「加一家 API 只写一个文件、不堆代码」的代价上限。

失败分道（勿混）：网络/429/5xx → `ProviderError(retryable=True)`（基础设施重试）；
鉴权/参数错 → `retryable=False`（重试无用）；**任务本身 FAILED** 抛 retryable（换次尝试）。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from .. import USER_AGENT
from ..core import camera, config
from ..core.project import fingerprint, fp_params
from ..media import probe
from .base import SHARED_FILE_LOCK, ProviderError, VideoProvider
from . import registry


def _data_url(path: str) -> str:
    """本地图片 → base64 data URL。

    两家都吃 data URL（Agnes 的 `first_frame` 会先解码校验尺寸，说明它真读了 base64）
    → 不建图床、不开匿名取图路由。少一个对外暴露面。
    """
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
    # 与 local_render 共用同一把锁（SHARED_FILE_LOCK）：这条路径和那边**同名**，
    # 两个并发镜头会同时判定"文件不存在"、同时渲染、同时写同一个文件。
    # 锁必须覆盖"检查"那一刻，只在写的时候加锁挡不住 check-then-act。
    with SHARED_FILE_LOCK:
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


class ApiVideoProvider(VideoProvider):
    """真 API 生成服务的基类。子类只需：`spec_id`/`name` + 上表五处差异。"""

    spec_id: str = ""
    parallel_safe = True          # 无状态 HTTP → 多镜头可并行提交+轮询
    download_auth = True          # 下载产物带不带 Authorization
    video_path = ""               # 视频任务路径（相对 base）；空 = 子类自己拼
    image_path = "images/generations"
    FIRST_LAST_MODELS: set = set()   # 哪些档位支持首尾帧（可把运镜方向钉死）
    COST_UNITS: dict = {}            # 档位 → 元/次（免费档填 0）
    MAX_DURATION: dict = {}          # 档位 → 单镜头时长上限（秒）；执行器据此钳制"要多久"
    action_map = {"GENERATE_SHOT": "video", "GENERATE_CHARACTER": "image",
                  "GENERATE_SCENE": "image", "COMPOSE": "concat"}

    # ---------- 构造 ----------
    def __init__(self, project=None, outdir: str | None = None, api_key: str | None = None):
        spec = registry.get(self.spec_id) or registry.Spec(id=self.spec_id, label=self.spec_id)
        self.spec = spec
        self.project = project
        self.key = api_key or config.api_key(spec.id)
        if not self.key:
            raise ProviderError(f"未配置 {spec.label} Key：{spec.key_hint}", retryable=False)
        self.base = (os.environ.get(spec.env_base) if spec.env_base else "") or spec.base_url
        self.base = self.base.rstrip("/")
        # 档位来源：配置文件（面板设置页可切）→ 注册表默认
        self.model = config.option(spec.id, "video_model") or spec.default_model("video")
        self.image_model = config.option(spec.id, "image_model") or spec.default_model("image")
        self.max_duration = self.MAX_DURATION.get(self.model, 5.0)
        self.camera_support = {camera.CAP_PROMPT}
        if self.model in self.FIRST_LAST_MODELS:
            self.camera_support.add(camera.CAP_FIRST_LAST)
        self.timeout = float(os.environ.get("HXMV_API_TIMEOUT", "420"))
        self.outdir = outdir or os.environ.get("HXMV_ARTIFACTS") or (
            os.path.join(project.dir, "artifacts") if project else
            os.path.expanduser(f"~/.hxmv/artifacts/{time.strftime('%Y%m%d-%H%M%S')}"))
        os.makedirs(self.outdir, exist_ok=True)
        self.episode = None
        # 资产图兜底与成片拼接复用已测好的本地实现（真 API 只负责"让画面动起来"）
        from .local_render import LocalRenderProvider
        self._local = LocalRenderProvider(project=project, outdir=self.outdir)

    # ---------- HTTP ----------
    def _request(self, url: str, body: dict | None = None, auth: bool = True):
        headers = {"User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.key}"
        if body is not None:
            headers["Content-Type"] = "application/json;charset=utf-8"
        return urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                      headers=headers, method="POST" if body is not None else "GET")

    def _post(self, path: str, body: dict) -> dict:
        return self._send(self._request(f"{self.base}/{path}", body), "提交任务")

    def _get_json(self, path: str) -> dict:
        return self._send(self._request(f"{self.base}/{path}", None), "查询任务")

    def _get_abs(self, url: str) -> dict:
        """轮询绝对地址（Agnes 的 /agnesapi 不在 /v1 下）。"""
        return self._send(self._request(url, None), "查询任务")

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
            raise ProviderError(f"{what}失败 HTTP {e.code}: {detail}",
                                retryable=e.code not in (400, 401, 403))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"{what}网络异常: {e}", retryable=True)
        except json.JSONDecodeError as e:
            raise ProviderError(f"{what}返回非 JSON: {e}", retryable=True)

    def _download(self, url: str, dest: str) -> None:
        headers = {"User-Agent": USER_AGENT}
        if self.download_auth:
            headers["Authorization"] = f"Bearer {self.key}"
        for attempt in (1, 2, 3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
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

    # ---------- 出图（资产图 / 角色定妆首帧）----------
    def _image_body(self, prompt: str, ratio: str) -> dict:
        """出图请求体。`ratio` = "1:1"（角色设定表）或 "16:9"（场景定帧 / 首帧）。"""
        size = {"1:1": "1024x1024", "16:9": "1344x768"}.get(ratio, "1024x1024")
        return {"model": self.image_model, "prompt": prompt, "size": size}

    def _image_url(self, resp: dict) -> str | None:
        return ((resp.get("data") or [{}])[0] or {}).get("url") or None

    def _image_gen(self, prompt: str, ratio: str) -> str:
        resp = self._post(self.image_path, self._image_body(prompt, ratio))
        url = self._image_url(resp)
        if not url:
            raise ProviderError(f"出图未返回地址: {json.dumps(resp, ensure_ascii=False)[:160]}",
                                retryable=True)
        return url

    @staticmethod
    def _describe(desc: str, kind: str) -> str:
        """把「目标」变成**具体的视觉描述**再喂出图模型。

        实测踩过：直接把整句目标（「小石猴在花果山翻跟头，两个镜头，一个远景一个近景」）
        当描述交给出图模型，它会给你一张**金发男孩**设定表和一张**通用山景** ——
        因为那句话根本不是画面描述。所以先用文本模型写一段具体描述。
        没有可用的 LLM 时原样返回（宁可用粗描述，也不假装）。
        """
        text = (desc or "").strip()
        if not text:
            return text
        try:
            from ..core import llm
            if not llm.llm_available():
                return text
            if kind == "character":
                system = ("你在做美术设定。把用户的内容目标提炼成**一个角色**的具体外形描述："
                          "物种/体型/年龄感/毛色或配色/服饰/标志性特征，60 字以内。"
                          "只写外形，不要动作、不要剧情、不要分镜、不要镜头。直接给描述。")
            else:
                system = ("你在做美术设定。把用户的内容目标提炼成**一个场景**的具体描述："
                          "地点/地形/环境元素/光线/氛围，60 字以内。只写场景本身，"
                          "不要角色、不要剧情、不要分镜。直接给描述。")
            out = llm.chat([{"role": "system", "content": system},
                            {"role": "user", "content": text}],
                           temperature=0.4, max_tokens=200).strip()
            return out or text
        except Exception:
            return text

    def _asset_prompt(self, task, kind: str, desc: str | None = None) -> str:
        """设定表 / 定帧的提示词。

        角色为什么给「设定表」而不是单张图：标杆就是三视图 + 表情 + 动作那类设定表，
        单张图锁不住设计（换个角度就变样）。设定表是**设计基准**，
        所以它不当首帧用（网格画面喂给视频模型，片子里就会真的出现格子）。
        """
        desc = desc if desc is not None else self._describe(str(task.input.get("prompt") or "").strip(), kind)
        style = str(task.constraints.get("style") or "cinematic, film-like color").strip()
        if kind == "character":
            return (
                "A clean character design sheet (character reference sheet) on a plain light background, "
                f"art style: {style}. "
                "(1) Three full-body turnaround views of the SAME character: front view, side view, back view — "
                "identical proportions, identical design, consistent colors. "
                "(2) Six head-only expression studies: happy, surprised, angry, sad, laughing, curious. "
                "(3) Three action poses: running, jumping, sitting. "
                f"Character description: {desc}. "
                "Neat grid layout, illustration finish, no text, no watermark, no labels."
            )
        return (
            "A cinematic wide keyframe (the first frame of a video shot), 16:9 composition, "
            f"art style: {style}. Scene: {desc}. "
            "Natural lighting, film-like color grading, high detail, no text, no watermark."
        )

    def _generate_asset(self, task) -> dict:
        """用出图模型出资产图（角色设定表 / 场景定帧）。出图失败退回本地资产，但把原因标出来。"""
        kind = "character" if task.action == "GENERATE_CHARACTER" else "scene"
        key = str(task.constraints.get("asset_key") or task.constraints.get("scene_key") or task.task_id)
        desc = self._describe(str(task.input.get("prompt") or "").strip(), kind)
        prompt = self._asset_prompt(task, kind, desc)
        # 档案里已有**用户自己传的**参考图（非占位色卡、非系统设定表）→ 直接复用，绝不覆盖。
        # 实测踩过：系统自己又生成一张，把用户登记的那张顶掉了 —— 出的片子自然和用户给的无关。
        if self.project:
            have = self.project.asset(kind, key)
            if have and not have.get("placeholder") and not have.get("sheet"):
                return {"asset": have["path"], "asset_key": key, "kind": kind,
                        "reused": True, "user_ref": True, "cost_units": 0.0}

        fp = fingerprint({"prompt": f"asset|{kind}|{key}|{prompt}", "model": self.image_model})
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

        resp = None
        try:
            resp = self._post(self.image_path, self._image_body(prompt, "1:1" if kind == "character" else "16:9"))
        except ProviderError as e:
            print(f"⚠ 出图失败（{e}）→ 退回本地资产")
        url = self._image_url(resp) if resp else None
        if not url:
            out = self._local.generate(task)
            out["image_failed"] = True
            return out

        path = os.path.join(self.outdir, f"{kind}_{task.task_id}.png")
        self._download(url, path)
        result = {
            "asset": path, "asset_key": key, "kind": kind,
            "sheet": kind == "character",          # 设定表：设计基准，不当首帧
            "prompt": prompt, "image_model": self.image_model,
            "fingerprint": fp, "cost_units": self.estimate_cost(task.action),
        }
        if self.project:
            # 登记进项目档案（下游镜头会拿它当参考）。
            # sheet=True：角色设定表是**设计基准**，不是一帧画面 —— 下游据此跳过它、
            # 别把三视图网格喂给视频模型当首帧。
            self.project.register_asset(
                kind, key, path, name=key,
                style=self.project.style,
                sheet=(kind == "character"),
                image_model=self.image_model,
                desc=desc,                     # 美术描述留存：镜头首帧要拿它复现同一个角色/场景
            )
            self.project.register_shot(fp, path, episode=self.episode,
                                       result={k: v for k, v in result.items() if k != "cost_units"})
        return result

    def _project_desc(self, kind: str, key) -> str:
        """取档案里这个角色/场景的**美术描述**（出资产图时写下来的）→ 镜头首帧复用它。"""
        if not (self.project and key):
            return ""
        hit = self.project.asset(kind, str(key)) or {}
        return str(hit.get("desc") or "").strip()

    def _shot_keyframe(self, task, prompt: str) -> tuple[str | None, str | None]:
        """按镜头生成「角色定妆首帧」：角色 + 场景 + 这一镜的画面 → 一张静帧 → 再图生视频。

        为什么必须这么做（实测）：首帧只给场景图、或干脆不给图时，模型全凭文字自由发挥 →
        评审反复判 character_mismatch（大脑里 18 条修不掉的经验就是这个）。
        图生视频的**一致性抓手就是首帧本身**：首帧里角色对了，后面整段才有硬约束。
        """
        cons, inp = task.constraints, task.input
        char_desc = self._project_desc("character", cons.get("character"))
        scene_desc = self._project_desc("scene", cons.get("scene"))
        style = str(cons.get("style") or "").strip()
        bits = [f"Cinematic film still, the first frame of a video shot. {str(inp.get('prompt') or '').strip()}."]
        if char_desc:
            bits.append(f"Character (must match exactly): {char_desc}.")
        if scene_desc:
            bits.append(f"Setting: {scene_desc}.")
        if style:
            bits.append(f"Art style: {style}.")
        bits.append("Single frame, 16:9 composition, film-like color grading; "
                    "no text, no watermark, no grid, no multiple panels, no character sheet.")
        still = " ".join(bits)

        fp = fingerprint({"prompt": f"keyframe|{still}", "model": self.image_model})
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                cached = str((hit.get("result") or {}).get("asset") or "")
                if cached and os.path.isfile(cached):
                    return _data_url(cached), cached

        url = self._image_gen(still, "16:9")
        path = os.path.join(self.outdir, f"keyframe_{task.task_id}.png")
        self._download(url, path)
        if self.project:
            self.project.register_shot(fp, path, episode=self.episode,
                                       result={"asset": path, "kind": "keyframe", "prompt": still})
        return _data_url(path), path

    # ---------- 视频任务（子类五处差异）----------
    def _submit_body(self, task, prompt: str, frames: list) -> dict:
        raise NotImplementedError

    def _poll_url(self, task_id: str) -> str:
        """轮询到终态，返回视频地址。失败/超时抛 ProviderError。"""
        raise NotImplementedError

    def _submit(self, task, prompt: str, frames: list) -> dict:
        return self._post(self.video_path, self._submit_body(task, prompt, frames))

    def _finish(self, task, prompt: str, frames: list) -> tuple[str, str]:
        """提交 + 轮询 → (task_id, 视频地址)。"""
        submitted = self._submit(task, prompt, frames)
        task_id = submitted.get("id") or submitted.get("task_id") or submitted.get("request_id")
        if not task_id:
            raise ProviderError(f"提交未返回任务 id: {json.dumps(submitted, ensure_ascii=False)[:200]}",
                                retryable=True)
        return str(task_id), self._poll_url(str(task_id))

    def _postprocess(self, path: str, task) -> str:
        """把 Refiner 要求的工程修正**真做在产物上**（不是"改了参数就当修好了"）。

        真 API 只负责画面；音量增益、片头黑场这类修正直接对下载到的文件做——
        省额度、更快，Critic 复测时量到的是真变化。
        """
        gain = task.input.get("audio_gain_db")
        if gain and not (probe.probe_container(path) or {}).get("has_audio"):
            gain = None      # 没有音轨可增益：这刀下不去，交给 enable_audio（真让模型带音频）
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
        # 角色设定表 / 场景定帧：**真 AI 出图**（原来一律甩给本地 FFmpeg 出占位色卡，
        # 色卡又不能当首帧 → 角色一致性从头到尾没有抓手）。
        if task.action in ("GENERATE_CHARACTER", "GENERATE_SCENE"):
            return self._generate_asset(task)
        if task.action in ("STORYBOARD", "COMPOSE"):
            return self._local.generate(task)      # 分镜/成片：复用本地实现

        cons, inp = task.constraints, task.input
        # 参考图：一张首帧只能锁**一件事**——默认锁角色（身份最难保），
        # 可用 cons["ref_use"]="scene" 换成锁场景（空镜/环境更重要的镜头）。
        # 解析在指纹之前：**实际用了哪类参考图**要算进指纹，否则改了 ref_use
        # 指纹不变 → 复用旧画面（老坑，实测踩过三次）。
        image, ref_path, ref_kind = None, None, None
        if self.project:
            ref_use = str(cons.get("ref_use") or "character").lower()
            order = ("scene", "character") if ref_use == "scene" else ("character", "scene")
            for kind in order:
                key = cons.get(kind)
                if not key:
                    continue
                hit = self.project.asset(kind, str(key))
                if hit:
                    # 占位色卡 / 角色设定表（三视图网格）都不能当首帧：色卡等于让模型照色卡发挥；
                    # 设定表会把格子画进片子里。再看另一类有没有真图。
                    if hit.get("placeholder") or hit.get("sheet"):
                        continue
                    # 只给角色图时不必去比场景（否则真 AI 视频换个场景就被判"不一致"，
                    # 而它根本没拿到场景参考——修无可修）
                    ref_path, image, ref_kind = hit["path"], _data_url(hit["path"]), kind
                    break

        # 运镜落点先定、再算指纹：换了落点（提示词 vs 首尾帧）出来的画面不一样，
        # 不进指纹就会命中缓存复用旧片 → "改了运镜却没变"（老坑，与 ref_kind 同因）。
        camera_move = camera.normalize_move(cons.get("camera") or inp.get("camera"))
        camera_strategy = camera.pick_strategy(self, prefer_first_last=bool(camera_move))
        fp = fingerprint(fp_params(task, self.project, f"{self.name}/{self.model}",
                                   extra={"ref_kind": ref_kind,
                                          "camera_realization": camera_strategy}))
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

        prompt = self._build_prompt(task, ref_kind=ref_kind)
        # 首帧 = 一致性的硬抓手：档案里没有可用的真参考图时，**现生成一张「角色定妆首帧」**
        # （角色 + 场景 + 这一镜的画面）再拿它图生视频；出图失败才退回纯文生视频。
        if image is None:
            try:
                kf_url, kf_path = self._shot_keyframe(task, prompt)
            except ProviderError as e:
                print(f"⚠ 首帧生成失败（{e}）→ 退回纯文生视频")
                kf_url, kf_path = None, None
            if kf_url:
                image, ref_path, ref_kind = kf_url, kf_path, "keyframe"
        # 首尾帧锚定：模型支持时，用**派生尾帧**（从首帧按运镜方向裁/缩放出来）把运镜钉死。
        # 只给一张首帧时运镜全靠模型心情；给两张图它必须从 A 走到 B —— 这是"硬运镜"的落点。
        images: list = [image] if image else []
        realization = "prompt"
        if camera_strategy == camera.CAP_FIRST_LAST and camera_move and ref_path and image:
            tail = camera.derive_last_frame(
                ref_path, camera_move, os.path.join(self.outdir, f"tail_{task.task_id}.jpg"),
                amount=cons.get("camera_amount"))
            if tail:
                images = [image, _data_url(tail)]
                realization = "first_last"
        task_id, url = self._finish(task, prompt, images)

        path = os.path.join(self.outdir, f"shot_{task.task_id}.mp4")
        self._download(url, path)
        path = self._postprocess(path, task)       # 工程修正真做在产物上（音量/黑场）
        self._local._slot_bind(task, path, fp)     # 让成片层知道这个镜头在哪
        cont = probe.probe_container(path) or {}
        out = {
            "media": path, "reference": ref_path, "reference_kind": ref_kind,
            # 图生视频：首帧应当贴近参考图 → 一致性采样点取 0%，
            # 并用"同编码管线的参考帧"当基线（否则编码差异会被误判成不一致）
            "reference_baseline": _encoded_frame(ref_path, cont.get("width"), cont.get("height"),
                                                 self.outdir) if ref_path else None,
            "consistency_at_ratio": 0.0 if ref_path else None,
            "duration": cont.get("duration"), "fps": cont.get("fps"),
            "resolution": f"{cont.get('width')}x{cont.get('height')}" if cont.get("width") else None,
            "params": {"provider": self.name, "model": self.model, "task_id": task_id,
                       "prompt": prompt[:200], "image_to_video": bool(image),
                       "image_count": len(images), "camera": camera_move,
                       "camera_speed": cons.get("camera_speed"),
                       "camera_amount": cons.get("camera_amount"),
                       "camera_realization": realization,
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

    def _build_prompt(self, task, ref_kind: str | None = None) -> str:
        """把分镜 + 风格约束拼成给生成模型看的提示词（英文更稳，国内几家对英文最敏感）。"""
        inp, cons = task.input, task.constraints
        parts = [str(inp.get("prompt") or inp.get("goal") or "").strip()]
        if cons.get("style"):
            parts.append(f"style: {cons['style']}")
        if cons.get("character"):
            parts.append(f"keep the same character as the reference image ({cons['character']})")
        if cons.get("continuity"):
            parts.append("consistent look with previous shots, cinematic lighting")
        # 语义守卫：critic 指出**哪一类**不符，就往 prompt 里补那一条具体约束。
        # 由 refiner 的修正写进来（rewrite_prompt_*），也会被大脑学成"起手就带"。
        # 注意：这些开关改的就是**发给模型的提示词本身**，必须列进 _FP_KEYS，
        # 否则指纹不变 → 命中缓存 → 修了等于没修（老坑）。
        # 场景锁／角色锁：首帧那张图只锁一类，另一类必须靠文字——不写清楚，
        # 模型会把参考图的背景一起搬过来（实测：给草地上的柯基照片 → 出来还是草地）。
        if ref_kind == "character":
            parts.append("the reference image defines ONLY the character's appearance — "
                         "do not copy its background; the scene must be exactly as described above")
        elif ref_kind == "scene":
            parts.append("the reference image defines ONLY the environment — "
                         "the character must strictly match the description above")
        if inp.get("_guard_closer"):
            parts.append("strictly follow the storyboard above; do not add anything not described")
        if inp.get("_guard_action"):
            parts.append("the subject's action must exactly match the action described above")
        if inp.get("_guard_emotion"):
            parts.append("match the mood and atmosphere described above "
                         "(facial expression, lighting, color tone)")
        if inp.get("_guard_continuity"):
            parts.append("continue seamlessly from the previous shot: same character, "
                         "same scene, same costume, same lighting")
        # L1 用 blurdetect 量出"糊"、用 scdet 量出"模型自己剪了片"，
        # 这两类在生成端唯一的杠杆就是提示词本身（分辨率/帧率已有各自旋钮）。
        if inp.get("_guard_sharp"):
            parts.append("very sharp focus, crisp details, high clarity — not blurry, not soft")
        if inp.get("_guard_single_shot"):
            parts.append("one single continuous shot, no cuts, no scene change, no montage")
        # 角色/场景的**美术描述**也要进提示词：首帧图 + 文字双重锚定，是跨镜头一致性的两个抓手。
        char_desc = self._project_desc("character", cons.get("character"))
        if char_desc:
            parts.append(f"the character must look exactly like this: {char_desc}")
        scene_desc = self._project_desc("scene", cons.get("scene"))
        if scene_desc:
            parts.append(f"the setting must be exactly this: {scene_desc}")
        # 运动基线：能控运镜的杠杆是**提示词 + 首尾帧**（各家有没有原生运镜参数见 camera_support），
        # 提示词这条永远有效，所以无条件写。
        parts.append("the subject is clearly moving through the whole shot: the action progresses, "
                     "this is not a still image")
        move = camera.normalize_move(cons.get("camera") or inp.get("camera"))
        speed = str(cons.get("camera_speed") or inp.get("camera_speed") or camera.DEFAULT_SPEED)
        if move:
            parts.append(camera.phrase(move, speed, cons.get("camera_amount")))
        else:
            parts.append("camera work: gentle, steady movement, one continuous take")
        if inp.get("_guard_camera"):
            # 「运镜不符」的修正落点：把方向/节奏写死（这条真的改了提示词，不是空转）
            parts.append("the camera movement must match the described direction and pace exactly")
        try:
            motion = float(cons.get("motion_scale") or inp.get("motion_scale") or 0)
        except (TypeError, ValueError):
            motion = 0.0
        if inp.get("_guard_motion") or motion > 1.0:
            parts.append("strong, fast, dynamic movement — the subject travels across the frame, "
                         "large pose changes, energetic action")
        return "，".join(p for p in parts if p)

    def set_episode(self, n) -> None:
        self.episode = n
        self._local.set_episode(n)

    def estimate_cost(self, action: str) -> float:
        return self.COST_UNITS.get(self.model, 0.0) if action == "GENERATE_SHOT" else 0.0
