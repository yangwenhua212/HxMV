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

from .. import USER_AGENT
from ..core import config
from ..media import probe
from .base import ProviderError, VideoProvider

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
# 内部语义分辨率 → 智谱合法 size（flash 免费且支持到 4K，统一给 16:9 高清，避免给非法枚举）
SIZE_MAP = {"480p": "1920x1080", "720p": "1920x1080", "1080p": "1920x1080",
            "4k": "3840x2160", "2160p": "3840x2160"}
COST_UNITS = {"cogvideox-flash": 0.0, "cogvideox-3": 1.05, "cogvideox-2": 0.7}  # 元/次（flash 免费）
# 各档位真实能给的时长（秒）：flash 只出 5 秒档；`_submit` 里 >7 才会要 10 秒档。
# 声明出来是为了让执行器"要不到就别要"，否则闭环会一直撞 too_short（实测撞了 4 轮）。
MAX_DURATION = {"cogvideox-flash": 5.0, "cogvideox-3": 10.0, "cogvideox-2": 10.0}
# 资产出图模型（角色设定表 / 场景定帧）：cogview-3-flash 免费；可换 cogview-4（按次计费）
IMAGE_MODEL = os.environ.get("HXMV_ZHIPU_IMAGE_MODEL", "cogview-3-flash")
IMAGE_COST = {"cogview-3-flash": 0.0, "cogview-4": 0.06, "cogview-4-250304": 0.06}


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
        # 档位来源：HXMV_ZHIPU_MODEL 环境变量 → ~/.hxmv/config.json（面板设置页可切）
        self.model = config.option("zhipu", "video_model") or "cogvideox-flash"
        self.max_duration = MAX_DURATION.get(self.model, 5.0)   # 执行器据此钳制"要多久"
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
                     "User-Agent": USER_AGENT,
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        return self._send(req, "提交任务")

    def _get_json(self, path: str) -> dict:
        req = urllib.request.Request(f"{self.base}/{path}",
                                     headers={"User-Agent": USER_AGENT,
                                              "Authorization": f"Bearer {self.key}"})
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
    # ---------- 资产图：角色设定表 / 场景定帧（真 AI 出图）----------
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

    def _asset_prompt(self, task, kind: str) -> str:
        """设定表 / 定帧的提示词。

        角色为什么给「设定表」而不是单张图：标杆就是三视图 + 表情 + 动作那类设定表，
        单张图锁不住设计（换个角度就变样）。设定表是**设计基准**，
        所以它不当首帧用（网格画面喂给视频模型，片子里就会真的出现格子）。
        """
        desc = self._describe(str(task.input.get("prompt") or "").strip(), kind)
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
        """用 CogView 出资产图（角色设定表 / 场景定帧）。出图失败退回本地资产，但把原因标出来。"""
        from ..core.project import fingerprint

        kind = "character" if task.action == "GENERATE_CHARACTER" else "scene"
        key = str(task.constraints.get("asset_key") or task.constraints.get("scene_key") or task.task_id)
        prompt = self._asset_prompt(task, kind)
        fp = fingerprint({"prompt": f"asset|{kind}|{key}|{prompt}", "model": IMAGE_MODEL})
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

        size = "1344x768" if kind == "scene" else "1024x1024"
        resp = None
        try:
            resp = self._post("images/generations",
                              {"model": IMAGE_MODEL, "prompt": prompt, "size": size})
        except ProviderError as e:
            print(f"⚠ 出图失败（{e}）→ 退回本地资产")
        url = ((resp.get("data") or [{}])[0] or {}).get("url") if resp else None
        if not url:
            out = self._local.generate(task)
            out["image_failed"] = True
            return out

        path = os.path.join(self.outdir, f"{kind}_{task.task_id}.png")
        self._download(url, path)
        result = {
            "asset": path, "asset_key": key, "kind": kind,
            "sheet": kind == "character",          # 设定表：设计基准，不当首帧
            "prompt": prompt, "image_model": IMAGE_MODEL,
            "fingerprint": fp, "cost_units": IMAGE_COST.get(IMAGE_MODEL, 0.0),
        }
        if self.project:
            # 登记进项目档案（下游镜头会拿它当参考）。
            # sheet=True：角色设定表是**设计基准**，不是一帧画面 —— 下游据此跳过它、
            # 别把三视图网格喂给视频模型当首帧。
            self.project.register_asset(
                kind, key, path, name=key,
                style=self.project.style,
                sheet=(kind == "character"),
                image_model=IMAGE_MODEL,
            )
            self.project.register_shot(fp, path, episode=self.episode,
                                       result={k: v for k, v in result.items() if k != "cost_units"})
        return result

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
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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

    def _postprocess(self, path: str, task) -> str:
        """把 Refiner 要求的工程修正**真做在产物上**（不是"改了参数就当修好了"）。

        真实 API 只负责画面；音量增益、片头黑场这类修正直接对下载到的文件做——
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
        # 色卡又不能当首帧 → 角色一致性从头到尾没有抓手，大脑里 18 条
        # character_inconsistency 修不掉就是这个原因）。
        if task.action in ("GENERATE_CHARACTER", "GENERATE_SCENE"):
            return self._generate_asset(task)
        if task.action in ("STORYBOARD", "COMPOSE"):
            return self._local.generate(task)      # 分镜/成片：复用本地实现

        # 复用在最前面：档案里已有这个画面 → 一张都不重新生成（也不消耗额度）
        from ..core.project import fingerprint, fp_params
        cons, inp = task.constraints, task.input

        # 参考图：一张首帧只能锁**一件事**——默认锁角色（身份最难保），
        # 可用 cons["ref_use"]="scene" 换成锁场景（空镜/环境更重要的镜头）。
        # 解析在指纹之前：**实际用了哪类参考图**要算进指纹，否则改了 ref_use
        # 指纹不变 → 复用旧画面（老坑，实测踩过三次）。
        image = None
        ref_path = None
        ref_kind = None
        if self.project:
            ref_use = str(cons.get("ref_use") or "character").lower()
            order = ("scene", "character") if ref_use == "scene" else ("character", "scene")
            for kind in order:
                key = cons.get(kind)
                if not key:
                    continue
                hit = self.project.asset(kind, str(key))
                if hit:
                    if hit.get("placeholder") or hit.get("sheet"):
                        # 占位色卡 / 角色设定表（三视图网格）都不能当首帧：
                        # 色卡等于让模型照色卡发挥；设定表会把格子画进片子里。
                        # 占位素材（系统造的色卡，不是用户传的参考图）→ **不要当首帧发给模型**：
                        # 实测喂色卡等于让模型照色卡发挥，还不如纯文字生成。再看另一类有没有真图。
                        continue
                    # 只给角色图时不必去比场景（否则真 AI 视频换个场景就被判"不一致"，
                    # 而它根本没拿到场景参考——修无可修）
                    ref_path, image, ref_kind = hit["path"], _data_url(hit["path"]), kind
                    break

        fp = fingerprint(fp_params(task, self.project, f"zhipu/{self.model}",
                                   extra={"ref_kind": ref_kind}))
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

        prompt = self._build_prompt(task, ref_kind=ref_kind)
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
            "media": path, "reference": ref_path, "reference_kind": ref_kind,
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
    def _build_prompt(task, ref_kind: str | None = None) -> str:
        """把分镜 + 风格约束拼成给生成模型看的提示词（英文更稳，CogVideoX 系对英文最敏感）。"""
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
        # 模型会把参考图的背景一起搬过来（实测：给草地上的柯基照片 → 出来还是草地，
        # 要的雪地没出现，L3 判"不符"判得有理）。
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
        return "，".join(p for p in parts if p)

    def set_episode(self, n) -> None:
        self.episode = n
        self._local.set_episode(n)

    def estimate_cost(self, action: str) -> float:
        return COST_UNITS.get(self.model, 0.0) if action == "GENERATE_SHOT" else 0.0
