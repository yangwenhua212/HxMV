"""运镜（camera movement）：内核的一等能力 —— 规格 / 提示词 / 落点声明 / 首尾帧派生。

为什么要单独一层（老大 2026-09 定的方向）：

- 「会运镜」不能绑死在某一家生成服务上。智谱视频 API **没有**运镜参数（字段只有
  model/prompt/image_url/quality/with_audio/size/duration/fps），唯一杠杆是提示词；
  可灵有**原生** `camera_control`（simple 下六轴）；本地渲染可以自己画。
  把**规格**放内核、把**落点**交给 provider 声明：以后换成更强的模型，只需要加一个
  适配器 + 一行能力声明，规划 / 判据 / 指纹 / 闭环一个字都不用改。

- 判据必须与模型无关：抽帧估全局位移与缩放（`media/probe.measure_camera`），
  谁的片子都能量。这是「真会运镜」和「写了句推送」的区别。

三层约定：

    规格（内核算：本模块） → 落点（provider 声明） → 判据（probe 量）

落点四档 `CAPABILITIES`：

    native      原生参数（可灵 camera_control）—— 最硬
    first_last  首尾帧锚定（CogVideoX-3）—— 用首帧 + 派生尾帧把运镜钉死
    prompt      只能写在提示词里（CogVideoX-Flash）—— 弱，但比一句一刀切强
    render      自己渲染（local_render）—— 本地能精确画出推/拉/摇/移
"""
from __future__ import annotations

import os
import re
import subprocess

# 规范运镜（内核/指纹/判据统一用这一组字符串；别在别处另造名字）
MOVE_PUSH_IN = "push_in"
MOVE_PULL_OUT = "pull_out"
MOVE_PAN_LEFT = "pan_left"
MOVE_PAN_RIGHT = "pan_right"
MOVE_TRACK_LEFT = "track_left"
MOVE_TRACK_RIGHT = "track_right"
MOVE_TILT_UP = "tilt_up"
MOVE_TILT_DOWN = "tilt_down"
MOVE_CRANE_UP = "crane_up"
MOVE_CRANE_DOWN = "crane_down"
MOVE_ORBIT = "orbit"
MOVE_HANDHELD = "handheld"
MOVE_FOLLOW = "follow"
MOVE_STATIC = "static"

MOVES = (MOVE_PUSH_IN, MOVE_PULL_OUT, MOVE_PAN_LEFT, MOVE_PAN_RIGHT,
         MOVE_TRACK_LEFT, MOVE_TRACK_RIGHT, MOVE_TILT_UP, MOVE_TILT_DOWN,
         MOVE_CRANE_UP, MOVE_CRANE_DOWN, MOVE_ORBIT, MOVE_HANDHELD, MOVE_FOLLOW,
         MOVE_STATIC)

# 需要**方向**才判得出的运镜（判据用；orbit/handheld 只判"有没有动"）
DIRECTIONAL = {
    MOVE_PUSH_IN: ("zoom", +1), MOVE_PULL_OUT: ("zoom", -1),
    MOVE_PAN_LEFT: ("pan", -1), MOVE_PAN_RIGHT: ("pan", +1),
    MOVE_TRACK_LEFT: ("pan", -1), MOVE_TRACK_RIGHT: ("pan", +1),
    MOVE_TILT_UP: ("tilt", -1), MOVE_TILT_DOWN: ("tilt", +1),
    MOVE_CRANE_UP: ("tilt", -1), MOVE_CRANE_DOWN: ("tilt", +1),
}

SPEEDS = ("slow", "normal", "fast")
DEFAULT_SPEED = "normal"
DEFAULT_AMOUNT = 0.5

# 落点能力
CAP_NATIVE = "native"
CAP_FIRST_LAST = "first_last"
CAP_PROMPT = "prompt"
CAP_RENDER = "render"
CAPABILITIES = (CAP_NATIVE, CAP_FIRST_LAST, CAP_PROMPT, CAP_RENDER)

# 中文/英文运镜词 → 规范名。**长词优先**（"推近" 要先于 "推"）。
_MOVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"推近|推镜|推进|推上|zoom\s*in|dolly\s*in|push\s*in", MOVE_PUSH_IN),
    (r"拉远|拉镜|后拉|拉开|zoom\s*out|dolly\s*out|pull\s*out", MOVE_PULL_OUT),
    (r"左摇|向左摇|摇向左边|pan\s*left", MOVE_PAN_LEFT),
    (r"右摇|向右摇|摇向右边|pan\s*right", MOVE_PAN_RIGHT),
    (r"左移|向左移|左横移|truck\s*left|track\s*left", MOVE_TRACK_LEFT),
    (r"右移|向右移|右横移|truck\s*right|track\s*right", MOVE_TRACK_RIGHT),
    (r"上摇|仰摇|向上摇|tilt\s*up", MOVE_TILT_UP),
    (r"下摇|俯摇|向下摇|tilt\s*down", MOVE_TILT_DOWN),
    (r"升起|升镜|上升|抬升|上移机位|crane\s*up|boom\s*up", MOVE_CRANE_UP),
    (r"下降|降镜|下沉|俯冲|crane\s*down|boom\s*down", MOVE_CRANE_DOWN),
    (r"环绕|绕拍|旋转镜头|转圈|orbit|arc\s*shot", MOVE_ORBIT),
    (r"手持|晃动镜头|轻微抖动|handheld|shaky\s*cam", MOVE_HANDHELD),
    (r"跟拍|跟随|跟着|follow\s*(?:shot|cam)?", MOVE_FOLLOW),
    (r"固定镜头|固定机位|静止镜头|静镜|镜头固定|机位固定|static\s*shot|locked[\s-]*off|固定", MOVE_STATIC),
)

# 简写（"镜头推"/"运镜推近"）：必须带上下文才认，避免"推门""拉车""升起火"这类误判
_CONTEXT_PATTERN = re.compile(
    r"(?:镜头|运镜|机位|镜别|摄影机)\s*(?:缓慢地?|慢慢地?|缓缓地?|快速|迅速地?|急速)?\s*(推|拉|摇|移|升|降|跟)")
_CONTEXT_MAP = {"推": MOVE_PUSH_IN, "拉": MOVE_PULL_OUT, "移": None, "摇": None,
                "升": MOVE_CRANE_UP, "降": MOVE_CRANE_DOWN, "跟": None}

_SLOW = re.compile(r"缓慢|慢慢|缓缓|慢速|慢镜|slow")
_FAST = re.compile(r"快速|急速|迅速|急促|加快|\bfast\b")


def normalize_move(value) -> str | None:
    """把任意写法（中文/英文/别名）收敛成规范运镜名；认不出返回 None（别猜）。"""
    if not value:
        return None
    text = str(value).strip().lower()
    if text in MOVES:
        return text
    for pattern, move in _MOVE_PATTERNS:
        if re.search(pattern, text, flags=re.I):
            return move
    return None


def parse(text: str) -> dict | None:
    """从一句镜头描述里认出运镜：返回 `{"move", "speed", "raw"}`；认不出返回 None。

    认不出**不猜方向**：只写"摇镜"却没有左右时返回 None（判据要方向，猜错等于判错）。
    """
    if not text:
        return None
    found: str | None = None
    raw = ""
    for pattern, move in _MOVE_PATTERNS:
        m = re.search(pattern, str(text), flags=re.I)
        if m:
            found, raw = move, m.group(0)
            break
    if found is None:
        m = _CONTEXT_PATTERN.search(str(text))
        if m:
            found, raw = _CONTEXT_MAP.get(m.group(1)), m.group(0)
    if found is None:
        return None
    speed = DEFAULT_SPEED
    if _SLOW.search(str(text)):
        speed = SPEEDS[0]
    elif _FAST.search(str(text)):
        speed = SPEEDS[2]
    return {"move": found, "speed": speed, "raw": raw}


def strip(text: str) -> str:
    """把运镜词从画面描述里剥掉（运镜归 camera 规格，别混进画面 prompt 当噪声）。

    只有**认出过运镜**才动手：否则会误删正文里的"镜头"（比如"镜头里只有一只鸟"）。
    """
    out = str(text or "")
    hit = False
    for pattern, _ in _MOVE_PATTERNS:
        if re.search(pattern, out, flags=re.I):
            hit = True
            out = re.sub(pattern, "", out, flags=re.I)
    if hit:
        out = re.sub(r"(?:镜头|运镜|机位|摄影机)\s*(?:的)?\s*", "", out)
        out = re.sub(r"(?:缓慢地?|慢慢地?|缓缓地?|快速地?|迅速地?|急速地?|轻轻地?|缓缓)\s*", "", out)
    out = re.sub(r"[，。；,;、]{2,}", "，", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ，。；,;、")


def clamp_amount(value) -> float:
    try:
        return max(0.1, min(1.0, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_AMOUNT


def phrase(move: str | None, speed: str | None = None, amount=None) -> str:
    """规格 → 发给生成模型的**镜头语言**（英文，真实模型对英文镜头术语更敏感）。"""
    move = normalize_move(move) or MOVE_STATIC
    speed = speed if speed in SPEEDS else DEFAULT_SPEED
    how = {"slow": "slowly", "normal": "steadily", "fast": "quickly"}[speed]
    table = {
        MOVE_PUSH_IN: f"camera work: {how} pushes in toward the subject (dolly in), steady",
        MOVE_PULL_OUT: f"camera work: {how} pulls back away from the subject (dolly out)",
        MOVE_PAN_LEFT: f"camera work: {how} pans left in a smooth horizontal move",
        MOVE_PAN_RIGHT: f"camera work: {how} pans right in a smooth horizontal move",
        MOVE_TRACK_LEFT: f"camera work: {how} moves left alongside the subject (truck left)",
        MOVE_TRACK_RIGHT: f"camera work: {how} moves right alongside the subject (truck right)",
        MOVE_TILT_UP: f"camera work: {how} tilts up",
        MOVE_TILT_DOWN: f"camera work: {how} tilts down",
        MOVE_CRANE_UP: f"camera work: {how} rises upward (crane up)",
        MOVE_CRANE_DOWN: f"camera work: {how} descends downward (crane down)",
        MOVE_ORBIT: f"camera work: {how} orbits around the subject, revealing new angles",
        MOVE_HANDHELD: "camera work: handheld with subtle natural shake",
        MOVE_FOLLOW: f"camera work: {how} follows the subject's movement, staying behind it",
        MOVE_STATIC: "camera work: the camera is locked off and does not move",
    }
    return table[move]


def capabilities(provider=None) -> set[str]:
    """provider 声明自己有哪些运镜落点；没声明按最弱的 `prompt` 处理（不吹能力）。"""
    declared = getattr(provider, "camera_support", None)
    if not declared:
        return {CAP_PROMPT}
    return {c for c in declared if c in CAPABILITIES} or {CAP_PROMPT}


def pick_strategy(provider, prefer_first_last: bool = False) -> str:
    """按 provider 的真实能力挑落点：native > render > first_last > prompt（从硬到软）。"""
    caps = capabilities(provider)
    for cap in (CAP_NATIVE, CAP_RENDER):
        if cap in caps:
            return cap
    if prefer_first_last and CAP_FIRST_LAST in caps:
        return CAP_FIRST_LAST
    return CAP_PROMPT


def native_params(move: str | None, speed: str | None = None, amount=None) -> dict:
    """投影到**原生运镜参数**（可灵 `camera_control` 的形状）。

    可灵：`{"type": "simple", "config": {"horizontal": ..., "vertical": ..., "pan": ...,
    "tilt": ..., "roll": ..., "zoom": ...}}`，每轴 -1..1。没有原生参数的服务忽略它。
    """
    move = normalize_move(move) or MOVE_STATIC
    amount = clamp_amount(amount)
    speed = speed if speed in SPEEDS else DEFAULT_SPEED
    mag = amount * {"slow": 0.5, "normal": 0.8, "fast": 1.0}[speed]
    cfg = {"horizontal": 0.0, "vertical": 0.0, "pan": 0.0, "tilt": 0.0, "roll": 0.0, "zoom": 0.0}
    if move == MOVE_PUSH_IN:
        cfg["zoom"] = mag
    elif move == MOVE_PULL_OUT:
        cfg["zoom"] = -mag
    elif move in (MOVE_PAN_LEFT, MOVE_PAN_RIGHT, MOVE_TRACK_LEFT, MOVE_TRACK_RIGHT):
        cfg["pan"] = mag if move in (MOVE_PAN_RIGHT, MOVE_TRACK_RIGHT) else -mag
    elif move == MOVE_TILT_UP:
        cfg["tilt"] = mag
    elif move == MOVE_TILT_DOWN:
        cfg["tilt"] = -mag
    elif move == MOVE_CRANE_UP:
        cfg["vertical"] = mag
    elif move == MOVE_CRANE_DOWN:
        cfg["vertical"] = -mag
    elif move == MOVE_ORBIT:
        cfg["roll"] = mag
    return {"type": "simple", "config": cfg}


def derive_last_frame(first_frame: str, move: str | None, out_path: str,
                      amount=None, width: int = 0, height: int = 0) -> str | None:
    """首尾帧锚定：用 FFmpeg 从**首帧**派生一张尾帧，把运镜钉死。

    支持的落点：推近（裁中心放大）/ 拉远（缩进留边）/ 左右平移与上下俯仰（裁偏一侧）。
    不支持的（环绕/手持/固定）返回 None —— 交给提示词，**不假装能锚**。

    为什么这一招是真硬约束：图生视频只吃一张首帧时，运镜全靠模型心情；给两张图（首 + 尾），
    模型必须从 A 走到 B，位移/缩放就是**被钉住**的（智谱 CogVideoX-3 支持 image_url 传两张）。
    """
    move = normalize_move(move)
    if not first_frame or not os.path.isfile(first_frame) or move in (None, MOVE_STATIC,
                                                                     MOVE_ORBIT, MOVE_HANDHELD):
        return None
    k = 0.06 + 0.20 * clamp_amount(amount)          # 位移/缩放比例（尾帧相对首帧）
    w, h = int(width or 0), int(height or 0)
    geo = f"scale={w}:{h}" if (w and h) else ""
    crop_w, crop_h = f"iw*{1 - k:.4f}", f"ih*{1 - k:.4f}"
    if move == MOVE_PUSH_IN:                        # 放大：裁中心
        crop = f"crop={crop_w}:{crop_h}:(iw-ow)/2:(ih-oh)/2"
    elif move == MOVE_PULL_OUT:                     # 缩进：整帧缩小后居中，四周补边
        crop = f"crop=iw:{'ih'}:0:0,scale=iw*{1 - k:.4f}:ih*{1 - k:.4f},pad=iw/{1 - k:.4f}:ih/{1 - k:.4f}:(ow-iw)/2:(oh-ih)/2"
    elif move in (MOVE_PAN_RIGHT, MOVE_TRACK_RIGHT):  # 镜头右移 = 视野取左侧
        crop = f"crop={crop_w}:ih:0:(ih-oh)/2"
    elif move in (MOVE_PAN_LEFT, MOVE_TRACK_LEFT):
        crop = f"crop={crop_w}:ih:iw-ow:(ih-oh)/2"
    elif move in (MOVE_TILT_DOWN, MOVE_CRANE_DOWN):   # 镜头下移 = 视野取上方
        crop = f"crop=iw:{crop_h}:(iw-ow)/2:0"
    else:                                            # 上摇/升镜
        crop = f"crop=iw:{crop_h}:(iw-ow)/2:ih-oh"
    chain = ",".join(x for x in (crop, geo) if x)
    args = ["ffmpeg", "-v", "error", "-y", "-i", first_frame, "-vf", chain,
            "-frames:v", "1", "-q:v", "3", out_path]
    try:
        subprocess.run(args, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out_path if os.path.isfile(out_path) and os.path.getsize(out_path) > 0 else None
