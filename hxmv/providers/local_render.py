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
import sys
import time

from ..media import probe
from .base import SHARED_FILE_LOCK, ProviderError, VideoProvider
from ..core import camera
from ..core.project import Project, fingerprint, fp_params

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
MOTION_ZOOM = 1.15       # 推/拉的中点缩放（也是"有运动"时的默认中点）
PAN_ZOOM = 1.35          # 平移类运镜的裁切缩放：给横移/俯仰留出足够的位移余量
                         # （1.15 只留 6.5% 画面宽，量出来不到 1px，判据会误判"没动"）
BLACK_HEAD_SECONDS = 0.3  # 片头全黑段（未被 trim_black 修掉时真的会出现黑帧）

# 并发镜头会同时要**同一张**参考图 / 基线帧 / 漂移图（同名同路径），
# 两个线程同时判定"文件不存在"→ 同时渲染 → 同时写同一个文件（可能写出坏文件，
# 或者一个线程读到写了一半的 PNG）。锁必须覆盖"检查"那一刻——check-then-act
# 的竞态就出在检查上；只在写的时候加锁是挡不住的。
_FILE_LOCK = SHARED_FILE_LOCK   # 与 zhipu_video 共用同一把（两者写的是同名基线帧）


def font_file() -> str | None:
    """挑一个 drawtext 能用的系统字体，找不到返回 None。

    为什么必须有这一步（实测踩过）：不指定 fontfile 时 ffmpeg 走 fontconfig 找默认字体，
    而 Windows（尤其 winget/Gyan 版）与精简 Linux 镜像里没有 fontconfig 配置 →
    drawtext 直接报 "Fontconfig error: Cannot load default config file"，
    整条渲染链全挂。显式给字体文件就绕开 fontconfig。
    """
    candidates: list[str] = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts = os.path.join(windir, "Fonts")
        candidates = [os.path.join(fonts, n) for n in
                      ("msyh.ttc", "msyhbd.ttc", "arial.ttf", "segoeui.ttf", "simhei.ttf")]
    elif sys.platform == "darwin":
        candidates = ["/System/Library/Fonts/Helvetica.ttc",
                      "/System/Library/Fonts/Supplemental/Arial.ttf",
                      "/Library/Fonts/Arial Unicode.ttf"]
    else:
        candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                      "/usr/share/fonts/TTF/DejaVuSans.ttf",
                      "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def drawtext(text: str, tail: str, fontsize: int = 44) -> str:
    """拼一段 drawtext 滤镜：能指字体就指，避免整条渲染依赖系统 fontconfig。

    Windows 盘符的冒号在滤镜表达式里会被当成参数分隔符，必须转义成 `\\:`。
    """
    font = font_file()
    base = f"fontsize={fontsize}:fontcolor=white@0.9:{tail}"
    if font:
        escaped = font.replace("\\", "/").replace(":", r"\:")
        return f"drawtext=fontfile='{escaped}':text='{text}':{base}"
    return f"drawtext=text='{text}':{base}"

# 中文色板：参考图（角色/场景）用确定性颜色，同一 key 永远同一张——可复现
_PALETTE = [
    ("0x2E7D6B", "0x0E2B26"), ("0x3D9B7A", "0x123A32"),
    ("0xD98E4A", "0x3A2412"), ("0x8E6BD9", "0x241A3A"),
    ("0xD95A6B", "0x3A1219"), ("0x4A8ED9", "0x12243A"),
]


class LocalRenderProvider(VideoProvider):
    name = "local"
    # 运镜落点：本地自己渲染 —— 推/拉/摇/移/升降都能真画出来（免费、可判据标定）
    camera_support = {camera.CAP_RENDER}
    # 产物按 task_id 命名互不冲突；共用路径（参考图/基线帧/漂移图）由 _FILE_LOCK 串行化
    parallel_safe = True
    action_map = {"GENERATE_SHOT": "render", "GENERATE_SCENE": "render", "GENERATE_IMAGE": "render",
                  "GENERATE_CHARACTER": "render", "COMPOSE": "concat"}

    def __init__(self, outdir: str | None = None, project: Project | None = None):
        if not probe.has_ffmpeg():
            raise ProviderError("系统缺少 ffmpeg/ffprobe，无法真渲染", retryable=False)
        self.project = project
        self.outdir = outdir or os.environ.get("HXMV_ARTIFACTS") or (
            os.path.join(project.dir, "artifacts") if project else
            os.path.expanduser(f"~/.hxmv/artifacts/{time.strftime('%Y%m%d-%H%M%S')}"))
        os.makedirs(self.outdir, exist_ok=True)
        self._assets: dict[str, str] = {}   # asset_key/scene_key → 参考图路径
        self._shots: dict[str, str] = {}    # shot#1 / task_id → 镜头文件
        self._order: list[str] = []         # COMPOSE 兜底用：按镜头位的最新版本
        self._shot_slots: list[str] = []    # 镜头位 → task_id（重试不新增镜头位）
        self._slot_of: dict[str, int] = {}  # task_id → 镜头位序号
        self._shot_fp: dict[int, str] = {}  # 镜头位 → 画面指纹（COMPOSE 复用的依据）
        self._reused_assets: list[str] = []
        self._reused_shots = 0
        self.episode: int | None = None

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
    def _render_asset(self, task, key: str, kind: str = "character") -> str:
        """参考图 = 有**真实纹理细节**的确定性图案 + 该 key 的专属色调。

        用 testsrc2 而不是纯渐变：纯渐变太\"平\"，一平移每帧像素几乎不变，
        静止检测（正确地）会把它判成 frozen——实测踩过这个坑。
        有细节的画面才像真实素材：平移/漂移都能在像素上量出来。

        **项目档案优先**：这片子已经有这个角色/场景的参考图 → 直接复用同一张
        （复用的不只是图，是\"这个角色长这样\"的设定），不重新生成。
        """
        if key in self._assets:
            return self._assets[key]
        path = os.path.join(self.outdir, f"asset_{key}.png")
        with _FILE_LOCK:
            # 锁内**复查**两处：别的并发镜头可能刚登记了档案、或刚把这张图渲染出来。
            # 不复查的话，两个线程会同时渲染同一张图、写同一个文件（写出坏 PNG，
            # 而"坏 PNG"在下游表现为"参考图读不出来"，很难定位到这里）。
            if self.project:
                hit = self.project.asset(kind, key)
                if hit:
                    self._assets[key] = hit["path"]
                    self._reused_assets.append(f"{kind}:{key}")
                    return hit["path"]
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                self._assets[key] = path
                return path
            h = int(hashlib.md5(key.encode()).hexdigest()[:6], 16)
            c0, c1 = _PALETTE[h % len(_PALETTE)]
            w, hgt = BASE_RESOLUTION
            hue = (h % 12) * 30                          # 每个 key 一个专属色调
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
                f"{drawtext(key, 'x=(w-text_w)/2:y=h-text_h-16')},"
                f"format=rgb24[out]",
                "-map", "[out]", "-frames:v", "1", path,
            ])
            self._assets[key] = path
            if self.project:
                # placeholder=True 很关键：这是**系统造的占位素材**（色卡 + 键名），不是用户传的
                # 参考图。真实模型 provider（智谱/可灵）拿到它会当首帧发给模型 → 等于让模型照色卡
                # 发挥，一致性检查也是拿色卡当基准（等于没检查）。标记出来，下游据此跳过。
                self.project.register_asset(kind, key, path, name=key,
                                            style=self.project.style, placeholder=True)
        return path

    def _reference_for(self, task) -> tuple[str | None, str | None]:
        """镜头用到的参考图：角色优先，其次场景。"""
        for field in ("character", "scene"):
            key = task.constraints.get(field)
            if key:
                return self._render_asset(task, str(key), field), field
        return None, None

    def _baseline_frame(self, ref: str, w: int, hgt: int, zoom: float = MOTION_ZOOM) -> str:
        """零漂移基线帧：参考图走**和镜头一样的编码管线**（同分辨率/同 CRF）后的画面。

        为什么不能直接拿参考图当基线：PNG 参考图 vs h264 解出来的帧之间有一层编码差异
        （高频细节被压掉），实测\"零漂移\"也能差出 0.12——那 0.12 会被误算成\"不一致\"。
        走同一条管线，量出来的距离才真正反映漂移本身。

        `zoom` = **这个镜头自己的中点缩放**（推/拉=1.15，平移/俯仰=1.35）：一致性在 50% 处
        采样，那一刻的裁切窗口是居中的，缩放即中点值 —— 基线按同一个值做，几何才严格对齐
        （不对齐时同样差 0.12，实测踩过）。
        """
        tag = "" if abs(zoom - MOTION_ZOOM) < 1e-6 else f"_z{round(zoom * 100)}"
        path = os.path.join(self.outdir,
                            f"base_{w}x{hgt}{tag}_{os.path.splitext(os.path.basename(ref))[0]}.png")
        if os.path.exists(path):
            return path
        with _FILE_LOCK:            # 锁内复查：并发的另一个镜头可能刚把这张基线做好
            if os.path.exists(path):
                return path
            tmp = os.path.join(self.outdir,
                               f"_basetmp_{w}x{hgt}{tag}_{os.path.splitext(os.path.basename(ref))[0]}.mp4")
            # 居中裁切（x/y 都取正中）= 运镜在 50% 时刻的画面几何，与一致性采样点对齐
            self._ff(["-loop", "1", "-i", ref, "-frames:v", "1",
                      "-vf", (f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                              f":d=1:s={w}x{hgt},format=yuv420p"),
                      *probe.encoder_args(26), tmp])
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
        with _FILE_LOCK:            # 两个镜头用同一强度时，文件名相同 → 锁内复查再渲染
            if os.path.exists(path):
                return path
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

        # ---- 画面指纹：档案里已有同参数的画面 → 直接复用文件，**跳过生成** ----
        fp = fingerprint(fp_params(task, self.project, "local"))
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                self._reused_shots += 1
                saved = dict(hit["result"])
                saved["reused"] = True
                saved["fingerprint"] = fp
                saved["cost_units"] = 0.0
                self._slot_bind(task, saved.get("media"), fp)
                return saved

        source = self._drift_image(ref, strength)   # 强度越低 → 漂移越大 → L2 能真的量到

        # 运镜（v0.9）：按**镜头规格**真画出推/拉/摇/移/升降/环绕/手持/固定 ——
        # 不再是"所有镜头同一个固定平移 + 小推镜"。三条不变量（破坏任一条，既有标定全部作废）：
        #  ① 中点（50%）几何 = 居中裁切 + 中点缩放 MOTION_ZOOM（base 帧按它做，L2 在 50% 采样）；
        #  ② 平移/俯仰走**线性单向**（不是正弦来回）——判据要方向，来回摆的符号会自相矛盾；
        #  ③ motion_scale≈0 或 camera=static → 真静止（刻意如此：freezedetect 要能量出来）。
        move = camera.normalize_move(cons.get("camera")) or camera.MOVE_PUSH_IN
        speed = str(cons.get("camera_speed") or camera.DEFAULT_SPEED)
        if speed not in camera.SPEEDS:
            speed = camera.DEFAULT_SPEED
        amount = camera.clamp_amount(cons.get("camera_amount"))
        if motion <= 0.05:
            move = camera.MOVE_STATIC                       # 没给运动量 → 真的是静止镜头
        frames = max(2, int(duration * fps))
        # 幅度 ∝ motion_scale（Refiner 的 increase_motion_scale 靠它修 frozen_frame） × amount
        amp = min(1.0, motion) * (0.4 + 0.6 * amount)
        # 中点缩放：推/拉在中点正好是 MOTION_ZOOM（线性斜坡）；平移类用更大的 PAN_ZOOM
        # 换位移余量。base 帧按**每个镜头自己的中点缩放**做，50% 处的几何才严格对齐。
        z_mid = MOTION_ZOOM if move in (camera.MOVE_PUSH_IN, camera.MOVE_PULL_OUT,
                                        camera.MOVE_STATIC) else PAN_ZOOM
        z_lo, z_hi = 1.0, 2 * MOTION_ZOOM - 1.0
        cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        room_x, room_y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
        t = f"(on/{frames})"
        realization = "render"
        if move == camera.MOVE_STATIC:
            # 固定机位 ≠ 一帧不变：真实拍摄里还有人手呼吸、被摄体自身的动静。
            # 完全静止会被 freezedetect（正确地）判成 frozen_frame，而这个缺陷在固定机位下
            # 修无可修 → 白挨一轮重试。给 ±1.5% 的呼吸式缩放（2 个周期，每帧变化够快才不会被
            # 判静止；**不能低于 1.0**：zoompan 的 z<1 会被钳住 → 反而真的静止，实测踩过）。
            # 中点仍是 z_mid，与 baseline 帧的几何严格对齐（0.5 处 sin=0）。
            z_expr = f"{z_mid}+0.015*sin(2*PI*2*{t})"
            x_expr, y_expr = cx, cy
            realization = "render"
        else:
            ramp = f"({t}-0.5)*2"                            # -1 → 0 → +1（中点归零）
            # 推/拉的缩放走 **smoothstep**（中点处斜率最大 1.5×线性、两端平缓）：
            # 线性斜坡每帧只变 0.2%，freezedetect 会（正确地）判成"画面静止"→ 白挨一轮 frozen_frame。
            # smoothstep 中点仍是 1.15（与基线对齐），但中段每帧 0.6% → 量得出来。
            ease = f"({t})*({t})*(3-2*({t}))"
            z_expr = {"push_in": f"{z_lo}+{z_hi - z_lo:.3f}*{ease}",
                      "pull_out": f"{z_hi}-{z_hi - z_lo:.3f}*{ease}"}.get(move, f"{z_mid}")
            sign_x = {"pan_right": 1, "pan_left": -1, "track_right": 1, "track_left": -1}.get(move)
            sign_y = {"tilt_down": 1, "tilt_up": -1, "crane_down": 1, "crane_up": -1}.get(move)
            swing = f"sin(2*PI*{t})"
            shake = f"sin(2*PI*4*{t})"
            x_expr = cx
            y_expr = cy
            if sign_x:
                x_expr = f"{cx}+{room_x}*{amp:.3f}*({sign_x})*({ramp})"
            elif move in (camera.MOVE_ORBIT, camera.MOVE_FOLLOW):
                # 环绕/跟拍没有真三维信息可用 → 用"横移 + 轻微缩放"近似，并**如实标注**是近似
                z_expr = f"{z_mid}+0.05*{swing}" if move == camera.MOVE_ORBIT else f"{z_mid}"
                x_expr = f"{cx}+{room_x}*{amp:.3f}*({swing})"
                realization = "render_approx"
            elif move == camera.MOVE_HANDHELD:
                x_expr = f"{cx}+{room_x}*{amp:.3f}*0.6*({shake})"
                y_expr = f"{cy}+{room_y}*{amp:.3f}*0.6*({shake})"
                realization = "render_approx"
            if sign_y:
                y_expr = f"{cy}+{room_y}*{amp:.3f}*({sign_y})*({ramp})"
        chain = [f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={w}x{hgt}:fps={fps}"]
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
            *probe.encoder_args(26), "-pix_fmt", "yuv420p",
            "-af", f"volume={BASE_AUDIO_DB + gain_db:.1f}dB", "-c:a", "aac", "-shortest", path,
        ])
        self._slot_bind(task, path, fp)
        result = {
            "media": path, "reference": ref, "reference_kind": kind,
            "reference_baseline": self._baseline_frame(ref, w, hgt, zoom=z_mid),
            "duration": duration, "fps": fps, "resolution": f"{w}x{hgt}",
            "params": {"provider": self.name, "seed": task.input.get("seed"),
                       "reference_strength": strength, "motion_scale": motion,
                       "audio_gain_db": gain_db, "black_fade": fade,
                       "camera": move, "camera_speed": speed,
                       "camera_amount": amount, "camera_realization": realization,
                       "trim_black": bool(task.input.get("trim_black"))},
            "reused": False, "fingerprint": fp,
            "cost_units": self.estimate_cost(task.action),
        }
        if self.project:      # 落档：下一次同参数直接复用这一版画面
            self.project.register_shot(
                fp, path, episode=self.episode,
                result={k: v for k, v in result.items() if k != "cost_units"},
                params=result["params"], prompt=task.input.get("prompt"),
                task_input={k: inp.get(k) for k in ("prompt", "duration", "seed", "resolution",
                                                    "fps", "audio_gain_db", "trim_black")},
                task_constraints={k: cons.get(k) for k in ("character", "scene", "style",
                                                           "reference_strength", "scene_strength",
                                                           "motion_scale", "camera",
                                                           "camera_speed", "camera_amount")})
        return result

    def _slot_bind(self, task, path: str | None = None, fp: str | None = None) -> None:
        """同一个 task_id 的重试占**同一个镜头位**：成片要拼"每个镜头位当前最新的那一版"，
        否则会把最早那版废片拼进去（实测踩过：重试产生 shot#3/#4，成片却拿 shot#1）。"""
        if task.task_id in self._slot_of:
            slot = self._slot_of[task.task_id]
        else:
            slot = len(self._shot_slots) + 1
            self._slot_of[task.task_id] = slot
            self._shot_slots.append(task.task_id)
        if path:
            self._shots[task.task_id] = path
            self._shots[f"shot#{slot}"] = path
        if fp:
            self._shot_fp[slot] = fp
        self._order = [self._shots[t] for t in self._shot_slots if t in self._shots]

    def set_episode(self, n: int | None) -> None:
        """标记当前在第几集（镜头落档时带上，便于"做到哪了"）。"""
        self.episode = n

    def reuse_report(self) -> dict:
        """本次运行复用了什么（给日志/事件用）：证明"没重新生成画面"。"""
        return {"assets": list(self._reused_assets), "shots": self._reused_shots}

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

        # ---- 成片指纹：镜头位画面 + 裁剪要求 + 转场一致 → 复用已有成片，不重新拼接 ----
        # 转场必须进指纹：加了淡入淡出却没有重拼，等于"修了等于没修"（同 with_audio 那类老坑）。
        fp = fingerprint({"prompt": "compose|" + "|".join(
            self._shot_fp.get(i + 1, os.path.basename(p)) for i, p in enumerate(paths)),
            "trim_black": bool(task.input.get("trim_black")),
            "transition": str(task.input.get("transition") or "")})
        if self.project:
            hit = self.project.shot(fp)
            if hit:
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved

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
        aout = ""                     # 只有 has_audio 才会被赋值/使用（下面两个分支各赋一次）
        # 转场（v0.9）：同场戏接缝被判 seam_jump 时，Refiner 会要求 transition=fade。
        # 这是**真的动作**：剪辑点从硬切变成 0.4s 交叉淡化，量出来的接缝相似度真的会上去。
        fade = 0.4 if (str(task.input.get("transition") or "") == "fade" and n > 1) else 0.0
        durations = [float((probe.probe_container(p) or {}).get("duration") or 0.0) for p in paths]
        if fade > 0.1:
            parts.append(f"[v0][v1]xfade=transition=fade:duration={fade}:offset={max(0.0, durations[0] - fade):.3f}[vx1]")
            offset = durations[0] - fade
            for i in range(2, n):
                offset += max(0.0, durations[i - 1] - fade)
                parts.append(f"[vx{i - 1}][v{i}]xfade=transition=fade:duration={fade}"
                             f":offset={offset:.3f}[vx{i}]")
            vout = f"[vx{n - 1}]"
        else:
            parts.append(f"{''.join(vs)}concat=n={n}:v=1:a=0[vout]")
            vout = "[vout]"
        if has_audio:
            if fade > 0.1:
                parts.append(f"[a0][a1]acrossfade=d={fade}[ax1]")
                for i in range(2, n):
                    parts.append(f"[ax{i - 1}][a{i}]acrossfade=d={fade}[ax{i}]")
                aout = f"[ax{n - 1}]"
            else:
                parts.append(f"{''.join(as_)}concat=n={n}:v=0:a=1[aout]")
                aout = "[aout]"
        graph = ";".join(parts)

        out = os.path.join(self.outdir, f"final_{task.task_id}.mp4")
        args = []
        for p in paths:
            args += ["-i", p]
        args += ["-filter_complex", graph, "-map", vout]
        if has_audio:
            args += ["-map", aout, "-c:a", "aac"]
        args += [*probe.encoder_args(24), "-pix_fmt", "yuv420p", out]
        self._ff(args)

        # 音画交付：剧本里的「旁白/音效」在这一步真做进成片（视频模型本身不带音轨）
        audio_info = None
        if task.input.get("narration") or task.input.get("sfx"):
            from ..media import audio as audio_mod
            audio_info = audio_mod.apply(out, {"narration": task.input.get("narration"),
                                               "sfx": task.input.get("sfx")}, self.outdir)

        cont = probe.probe_container(out) or {}
        result = {"output": out, "shots": list(keys), "files": paths,
                  "duration": cont.get("duration"), "trimmed_black": trim,
                  "reused": False, "fingerprint": fp,
                  "audio": audio_info,
                  "transition": "fade" if fade > 0.1 else None,
                  "cost_units": self.estimate_cost(task.action)}
        if fade > 0.1:
            # 交叉淡化会吃掉 (n-1)×fade 的时长：预期时长要按**实际**算，否则白挨一轮 too_short
            result["expected_duration"] = round(max(0.5, sum(durations) - fade * (n - 1)), 2)
        if audio_info and audio_info.get("expected_duration"):
            result["expected_duration"] = audio_info["expected_duration"]
        if self.project:
            self.project.register_shot(
                fp, out, episode=self.episode,
                result={k: v for k, v in result.items() if k != "cost_units"},
                params={"shots": list(keys), "trim_black": bool(task.input.get("trim_black"))},
                task_input={"prompt": "compose|" + "|".join(keys)},
                task_constraints={})
        return result

    # ---------- 入口 ----------
    def generate(self, task) -> dict:
        if task.action == "STORYBOARD":
            # 真编剧（不在 provider 里另写一套）：provider 直连时也走同一个 story 模块
            from ..core import story
            return {"storyboard": story.from_task(task, self.project),
                    "cost_units": self.estimate_cost(task.action)}
        if task.action in ("GENERATE_CHARACTER", "GENERATE_SCENE"):
            kind = "character" if task.action == "GENERATE_CHARACTER" else "scene"
            key = task.constraints.get("asset_key") or task.constraints.get("scene_key") or task.task_id
            path = self._render_asset(task, str(key), kind)
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
