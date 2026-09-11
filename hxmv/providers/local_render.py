"""LocalRenderProvider：用系统 FFmpeg **真渲染**文件的\"仿真生成器\"。

它是什么（别误解，也别对外吹）：

- 它是**真实的媒体生产管线**：真的出 .mp4/.png 文件、真的拼接成片、参数真的改变成品——
  所以闭环可以在**真实媒体**上跑完整的「执行 → 测量 → 修正 → 复测」。
- 它**不是** AI 生成模型：画面是 FFmpeg 合成（参考图 + 运动 + 色彩漂移），
  用于在没有付费生成 API 的情况下端到端验证\"真产物 + 真眼睛\"这条路。
  接真实 AI 服务请用 kling 等 provider——Provider 接口是同一个，闭环不用改。

参数 → 可测质量 的**真实**因果（这是闭环能收敛的前提，不是装饰）：

| 内部语义参数            | 渲染行为                       | 被哪层量出来                     |
|------------------------|--------------------------------|----------------------------------|
| input.resolution       | 输出分辨率（默认 640x360）     | L1 清晰度（< 720p → low_clarity）|
| input.fps              | 输出帧率（默认 15）            | L1 帧率（< 24 → fps_too_low）    |
| input.audio_gain_db    | 音轨增益（基线 -24dB）         | L1 音量（mean_volume < -40dB）   |
| input.trim_black       | 是否去掉片头黑场渐入           | L1 黑帧（blackdetect）           |
| constraints.motion_scale | 运动幅度（0 = 静止）         | L1 静止（freezedetect）          |
| constraints.reference_strength | 色彩/噪声漂移幅度（1=零漂移） | L2 一致性（与参考图的外观距离） |
| input.duration         | 片段时长                       | L1 时长（too_short/too_long）    |

出厂默认刻意是个\"低端生成器\"（640x360@15fps、音轨偏轻、参考强度 0.4 有漂移），
Refiner 调参重生成后这些指标会**真的**变好——闭环的\"修正有效\"是可复现的，不是演的。
"""
from __future__ import annotations

import hashlib
import os
import random
import time

from ..media import probe
from .base import ProviderError, VideoProvider

# 低端生成器基线（低于 L1 阈值 → 首轮必然被量出真实缺陷）
BASE_RESOLUTION = (640, 360)
BASE_FPS = 15
BASE_AUDIO_DB = -24.0    # 叠加在 sine 原始 -21.1dB 上 → mean_volume ≈ -45dB（低于 -40 阈值）
RESOLUTIONS = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}
DEFAULT_STRENGTH = 0.4   # 参考强度：0.4 → 明显漂移（一致度 ≈0.86，低于 0.88 阈值）
DRIFT_HUE_DEG = 70.0     # (1-strength) × 该值 = hue 旋转角度
DRIFT_SAT_RANGE = 0.8    # (1-strength) × 该值 = 饱和度衰减
DRIFT_NOISE = 50.0       # (1-strength) × 该值 = 噪点强度
DRIFT_BRIGHT = 0.12      # (1-strength) × 该值 = 亮度偏移（保证任何配色都能被量出漂移）
DRIFT_CONTRAST = 0.25    # (1-strength) × 该值 = 对比度衰减
MOTION_ZOOM = 1.15       # 有运镜时的固定推镜倍数（给平移留出余量）
BLACK_HEAD_SECONDS = 0.3  # 片头全黑段（未被 trim_black 修掉时真的会出现黑帧）

# 中文色板：参考图（角色/场景）用确定性颜色，同一 key 永远同一张——可复现
_PALETTE = [
    ("0x2E7D6B", "0x0E2B26"), ("0x3D9B7A", "0x123A32"),
    ("0xD98E4A", "0x3A2412"), ("0x8E6BD9", "0x241A3A"),
    ("0xD95A6B", "0x3A1219"), ("0x4A8ED9", "0x12243A"),
]


class LocalRenderProvider(VideoProvider):
    name = "local"
    action_map = {"GENERATE_SHOT": "render", "GENERATE_SCENE": "render",
                  "GENERATE_CHARACTER": "render", "COMPOSE": "concat"}

    def __init__(self, outdir: str | None = None):
        if not probe.has_ffmpeg():
            raise ProviderError("系统缺少 ffmpeg/ffprobe，无法真渲染", retryable=False)
        self.outdir = outdir or os.environ.get("HXMV_ARTIFACTS") or \
            os.path.expanduser(f"~/.hxmv/artifacts/{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(self.outdir, exist_ok=True)
        self._assets: dict[str, str] = {}   # asset_key/scene_key → 参考图路径
        self._shots: dict[str, str] = {}    # shot#1 / task_id → 镜头文件
        self._order: list[str] = []         # COMPOSE 兜底用：按镜头位的最新版本
        self._shot_slots: list[str] = []    # 镜头位 → task_id（重试不新增镜头位）
        self._slot_of: dict[str, int] = {}  # task_id → 镜头位序号

    # ---------- 工具 ----------
    def _rng(self, task, salt: str = "", stable: bool = False) -> random.Random:
        """确定性随机源。

        stable=True 时**不含 attempts**：同一任务跨\"重生成\"保持一致——用于建模
        \"生成器固有的坏习惯\"（如片头黑场），只有显式修正才消除；
        默认含 attempts：模拟\"重生成一版\"的随机性。
        """
        parts = [task.task_id, f"{task.input.get('seed')}", salt]
        if not stable:
            parts.insert(1, str(task.retry_policy.get("attempts", 0)))
        h = hashlib.md5(":".join(parts).encode()).hexdigest()
        return random.Random(int(h[:8], 16))

    def _ff(self, args: list[str]) -> None:
        rc, out = probe._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args)
        if rc != 0:
            raise ProviderError(f"ffmpeg 渲染失败: {out.strip()[:200]}", retryable=True)

    # ---------- 资产：角色 / 场景参考图（真 PNG） ----------
    def _render_asset(self, task, key: str) -> str:
        """参考图 = 有**真实纹理细节**的确定性图案 + 该 key 的专属色调。

        用 testsrc2 而不是纯渐变：纯渐变太\"平\"，一平移每帧像素几乎不变，
        静止检测（正确地）会把它判成 frozen——实测踩过这个坑。
        有细节的画面才像真实素材：平移/漂移都能在像素上量出来。
        """
        if key in self._assets:
            return self._assets[key]
        h = int(hashlib.md5(key.encode()).hexdigest()[:6], 16)
        c0, c1 = _PALETTE[h % len(_PALETTE)]
        w, hgt = BASE_RESOLUTION
        path = os.path.join(self.outdir, f"asset_{key}.png")
        hue = (h % 12) * 30                              # 每个 key 一个专属色调
        sat = 0.6 + (h % 5) * 0.08
        x1 = 0.2 + (h % 70) / 100.0
        y1 = 0.2 + ((h // 7) % 70) / 100.0
        self._ff([
            "-f", "lavfi", "-i", f"testsrc2=s={w}x{hgt}:d=1",
            "-f", "lavfi", "-i",
            f"gradients=s={w}x{hgt}:duration=1:c0={c0}:c1={c1}:x0=0.1:y0=0.1:x1={x1:.2f}:y1={y1:.2f}",
            "-filter_complex",
            f"[0:v]hue=h={hue}:s={sat}[a];[1:v]format=rgb24[b];"
            f"[a][b]blend=all_mode=softlight:all_opacity=0.85,"
            f"drawtext=text='{key}':fontsize=44:fontcolor=white@0.9:"
            f"x=(w-text_w)/2:y=h-text_h-16,format=rgb24[out]",
            "-map", "[out]", "-frames:v", "1", path,
        ])
        self._assets[key] = path
        return path

    def _reference_for(self, task) -> tuple[str | None, str | None]:
        """镜头用到的参考图：角色优先，其次场景。"""
        for field, kind in (("character", "character"), ("scene", "scene")):
            key = task.constraints.get(field)
            if key:
                return self._render_asset(task, str(key)), kind
        return None, None

    def _baseline_frame(self, ref: str, w: int, hgt: int) -> str:
        """零漂移基线帧：参考图走**和镜头一样的编码管线**（同分辨率/同 CRF）后的画面。

        为什么不能直接拿参考图当基线：PNG 参考图 vs h264 解出来的帧之间有一层编码差异
        （高频细节被压掉），实测\"零漂移\"也能差出 0.12——那 0.12 会被误算成\"不一致\"。
        走同一条管线，量出来的距离才真正反映漂移本身。
        """
        path = os.path.join(self.outdir, f"base_{w}x{hgt}_{os.path.basename(ref)}.png")
        if os.path.exists(path):
            return path
        tmp = os.path.join(self.outdir, f"_basetmp_{w}x{hgt}_{os.path.basename(ref)}.mp4")
        # 居中裁切（x/y 都取正中）= 运镜在 50% 时刻的画面几何，与一致性采样点对齐
        self._ff(["-loop", "1", "-i", ref, "-frames:v", "1",
                  "-vf", (f"zoompan=z='{MOTION_ZOOM}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                          f":d=1:s={w}x{hgt},format=yuv420p"),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", tmp])
        self._ff(["-i", tmp, "-frames:v", "1", path])
        os.remove(tmp)
        return path

    # ---------- 漂移参考图：色彩/噪声偏离参考图（一致性缺陷的真实来源） ----------
    def _drift_image(self, ref: str, strength: float) -> str:
        """按参考强度生成\"漂移版参考图\"（确定性、可缓存）。

        为什么不逐帧加 hue/noise：噪声 filter 是逐像素熵，1080p 逐帧跑既慢又把码率打到
        50Mbps（实测 5s 片段 34MB）。漂移本质是**参考图本身偏了**，在图上做一次即可——
        结果等价（帧都是这张图的运动），成本从\"每帧\"降到\"一次\"。
        """
        drift = 1.0 - max(0.0, min(1.0, strength))
        path = os.path.join(self.outdir, f"drift{round(strength * 100):03d}_{os.path.basename(ref)}")
        if os.path.exists(path) or drift <= 0.001:
            return path if os.path.exists(path) else ref
        self._ff([
            "-i", ref, "-frames:v", "1", "-vf",
            f"hue=h={drift * DRIFT_HUE_DEG:.1f}:s={1 - drift * DRIFT_SAT_RANGE:.3f},"
            f"eq=brightness={drift * DRIFT_BRIGHT:.3f}:contrast={1 - drift * DRIFT_CONTRAST:.3f},"
            f"noise=alls={max(1, round(drift * DRIFT_NOISE))}:allf=t,"
            f"format=yuv420p,format=rgb24", path,
        ])
        return path

    # ---------- 镜头：真渲染（参数真的决定质量） ----------
    def _render_shot(self, task) -> dict:
        inp, cons = task.input, task.constraints
        res = RESOLUTIONS.get(str(inp.get("resolution") or ""), BASE_RESOLUTION)
        w, hgt = res
        fps = int(float(inp.get("fps") or BASE_FPS))
        duration = float(inp.get("duration") or 5)
        strength = max(0.0, min(1.0, float(cons.get("reference_strength", DEFAULT_STRENGTH))))
        motion = max(0.0, float(cons.get("motion_scale", 0.4)))
        gain_db = float(inp.get("audio_gain_db") or 0.0)

        ref, kind = self._reference_for(task)
        if ref is None:
            raise ProviderError("镜头缺少参考资产（character/scene）", retryable=False)
        source = self._drift_image(ref, strength)   # 强度越低 → 漂移越大 → L2 能真的量到

        # 运镜：**平移为主 + 固定小推镜**（不是从零开始的余弦推镜——那种起步 1 秒内几乎
        # 不动，会被 freezedetect 正确地判成"画面静止"，实测踩过）。
        # 平移的每帧位移 ∝ 幅度×周期数，稳定高于检测阈值；motion_scale=0 → 真静止（可被检出）。
        frames = max(2, int(duration * fps))
        if motion > 0.05:
            cycles = max(2, int(round(duration / 1.5)))     # 5s 视频 ≈ 3 个来回
            amp = min(1.0, motion) * 0.4                    # 占可用平移余量的比例
            room_x, room_y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
            z = f"{MOTION_ZOOM}"
            # 两条正弦都从 0 出发，且周期成整数倍 → 在 0%、50%、100% 处偏移刚好归零。
            # 这点很关键：一致性检测在 50% 处采样，此时画面正好是\"居中裁切\"，
            # 与零漂移基线帧的几何完全对齐——量出来的差就只剩漂移本身（实测踩过不对齐的坑）。
            x = f"iw/2-(iw/zoom/2)+{room_x}*{amp:.3f}*sin(2*PI*{cycles}*on/{frames})"
            y = f"ih/2-(ih/zoom/2)+{room_y}*{amp:.3f}*sin(2*PI*{2 * cycles}*on/{frames})"
            chain = [f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{hgt}:fps={fps}"]
        else:
            chain = [f"zoompan=z='1.0':d={frames}:s={w}x{hgt}:fps={fps}"]
        # 片头黑场：低端生成器的坏习惯；Refiner 要求 trim_black 时就不再产生（真修好）。
        # 判据只与 (task_id, seed) 有关、不含 attempts：同一任务的坏习惯跨重生成是**稳定**的，
        # 必须显式要求 trim_black 才消除（否则缺陷在重试间忽有忽无，闭环学不到因果）。
        fade = (not inp.get("trim_black")) and self._rng(task, "fade", stable=True).random() < 0.5
        if fade:
            chain.append(f"tpad=start_duration={BLACK_HEAD_SECONDS}:start_mode=add:color=black")
        chain.append("format=yuv420p")

        path = os.path.join(self.outdir, f"shot_{task.task_id}.mp4")
        freq = 220 + (int(hashlib.md5(f"{task.input.get('seed')}".encode()).hexdigest()[:2], 16))
        self._ff([
            "-loop", "1", "-i", source,
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-vf", ",".join(chain), "-t", f"{duration}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-af", f"volume={BASE_AUDIO_DB + gain_db:.1f}dB", "-c:a", "aac", "-shortest", path,
        ])
        # 同一个 task_id 的重试占**同一个镜头位**：成片要拼\"每个镜头位当前最新的那一版\"，
        # 否则会把最早那版废片拼进去（实测踩过：重试产生 shot#3/#4，成片却拿 shot#1）。
        if task.task_id in self._slot_of:
            slot = self._slot_of[task.task_id]
        else:
            slot = len(self._shot_slots) + 1
            self._slot_of[task.task_id] = slot
            self._shot_slots.append(task.task_id)
        self._shots[f"shot#{slot}"] = path
        self._shots[task.task_id] = path
        self._order = [self._shots[t] for t in self._shot_slots]
        return {
            "media": path, "reference": ref, "reference_kind": kind,
            "reference_baseline": self._baseline_frame(ref, w, hgt),
            "duration": duration, "fps": fps, "resolution": f"{w}x{hgt}",
            "params": {"provider": self.name, "seed": task.input.get("seed"),
                       "reference_strength": strength, "motion_scale": motion,
                       "audio_gain_db": gain_db, "black_fade": fade,
                       "trim_black": bool(task.input.get("trim_black"))},
            "cost_units": self.estimate_cost(task.action),
        }

    # ---------- 成片：真拼接 ----------
    def _compose(self, task) -> dict:
        keys = task.input.get("shots") or []
        paths = [self._shots.get(str(k)) for k in keys]
        paths = [p for p in paths if p] or list(self._order)
        if not paths:
            raise ProviderError("没有可拼接的镜头", retryable=False)

        first = probe.probe_container(paths[0]) or {}
        w, hgt = first.get("width") or BASE_RESOLUTION[0], first.get("height") or BASE_RESOLUTION[1]
        fps = int(first.get("fps") or BASE_FPS)
        has_audio = all((probe.probe_container(p) or {}).get("has_audio") for p in paths)
        # trim_black 在本层是**真的动作**：量出片头黑场时长，拼接时把它裁掉。
        # （黑场来自镜头自带的渐入——镜头层没修掉的，成片层还能补一刀。）
        trim = probe.leading_black_seconds(paths[0]) if task.input.get("trim_black") else 0.0

        parts, vs, as_ = [], [], []
        for i, p in enumerate(paths):
            cut_v = f"trim=start={trim},setpts=PTS-STARTPTS," if (i == 0 and trim > 0) else ""
            parts.append(f"[{i}:v]{cut_v}scale={w}:{hgt}:force_original_aspect_ratio=decrease,"
                         f"pad={w}:{hgt}:-1:-1,setsar=1,fps={fps}[v{i}]")
            vs.append(f"[v{i}]")
            if has_audio:
                cut_a = f"atrim=start={trim},asetpts=PTS-STARTPTS," if (i == 0 and trim > 0) else ""
                parts.append(f"[{i}:a]{cut_a}aresample=44100[a{i}]")
                as_.append(f"[a{i}]")
        n = len(paths)
        parts.append(f"{''.join(vs)}concat=n={n}:v=1:a=0[vout]")
        if has_audio:
            parts.append(f"{''.join(as_)}concat=n={n}:v=0:a=1[aout]")
        graph = ";".join(parts)

        out = os.path.join(self.outdir, f"final_{task.task_id}.mp4")
        args = []
        for p in paths:
            args += ["-i", p]
        args += ["-filter_complex", graph, "-map", "[vout]"]
        if has_audio:
            args += ["-map", "[aout]", "-c:a", "aac"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", out]
        self._ff(args)
        cont = probe.probe_container(out) or {}
        return {"output": out, "shots": list(keys), "files": paths,
                "duration": cont.get("duration"), "trimmed_black": trim,
                "cost_units": self.estimate_cost(task.action)}

    # ---------- 入口 ----------
    def generate(self, task) -> dict:
        if task.action == "STORYBOARD":
            goal = task.input.get("goal", "")
            return {"storyboard": [f"镜头 {i}: {goal[:20]}" for i in (1, 2)],
                    "cost_units": self.estimate_cost(task.action)}
        if task.action in ("GENERATE_CHARACTER", "GENERATE_SCENE"):
            key = task.constraints.get("asset_key") or task.constraints.get("scene_key") or task.task_id
            path = self._render_asset(task, str(key))
            return {"asset": path, "asset_key": str(key),
                    "cost_units": self.estimate_cost(task.action)}
        if task.action == "GENERATE_SHOT":
            return self._render_shot(task)
        if task.action == "COMPOSE":
            return self._compose(task)
        return {"cost_units": 0.0}

    def estimate_cost(self, action: str) -> float:
        # 本地渲染不花钱：只记 0 元（真实 API provider 才计价）——预算表照样能跑
        return 0.0
