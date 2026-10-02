"""智谱 AI（BigModel）适配器 —— **只写与别人不同的那几处**，通用逻辑全在 `api_video.py`。

- 模型：`cogvideox-flash`（**免费**）默认；`cogvideox-3` / `cogvideox-2` 可切（付费）
- 文生视频 / **图生视频**：给首帧图（`image_url`，支持 base64）——
  图生视频才是"角色一致性"的真抓手：把项目档案里那张角色参考图喂进去，
  跨镜头同一个角色就有了硬约束（比调 prompt 强度实在）。
- 异步两段式：`POST videos/generations` 提交 → `GET async-result/{id}` 轮询 → 下载 mp4
- `cogvideox-3` 额外支持**首尾帧**（image_url 传两张：第一张首帧、第二张尾帧）→ 运镜方向被钉死

配置（环境变量优先，其次 ~/.hxmv/config.json）：
    HXMV_ZHIPU_KEY / ZHIPUAI_API_KEY   密钥（`python3 -m hxmv --set-key zhipu <KEY>` 可写入）
    HXMV_ZHIPU_MODEL                   视频档位，默认 cogvideox-flash
    HXMV_ZHIPU_BASE                    默认 https://open.bigmodel.cn/api/paas/v4（可指向仿真端点）
    HXMV_API_TIMEOUT                   单任务轮询上限秒数（默认 420）
"""
from __future__ import annotations

import json
import os
import time

from .base import ProviderError
from .api_video import SHARED_FILE_LOCK, ApiVideoProvider, _data_url, _encoded_frame  # noqa: F401（对外仍是老入口）

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
# 内部语义分辨率 → 智谱合法 size（flash 免费且支持到 4K，统一给 16:9 高清，避免给非法枚举）
SIZE_MAP = {"480p": "1920x1080", "720p": "1920x1080", "1080p": "1920x1080",
            "4k": "3840x2160", "2160p": "3840x2160"}


class ZhipuVideoProvider(ApiVideoProvider):
    name = "zhipu"
    spec_id = "zhipu"
    video_path = "videos/generations"
    image_path = "images/generations"
    # 只有 cogvideox-3 支持首尾帧（flash 只能靠提示词）
    FIRST_LAST_MODELS = {"cogvideox-3"}
    COST_UNITS = {"cogvideox-flash": 0.0, "cogvideox-3": 1.05, "cogvideox-2": 0.7}   # 元/次
    # 各档位真实能给的时长（秒）：flash 只出 5 秒档；>7 才会要 10 秒档。
    # 声明出来是为了让执行器"要不到就别要"，否则闭环会一直撞 too_short（实测撞了 4 轮）。
    MAX_DURATION = {"cogvideox-flash": 5.0, "cogvideox-3": 10.0, "cogvideox-2": 10.0}

    def _image_body(self, prompt: str, ratio: str) -> dict:
        return {"model": self.image_model, "prompt": prompt,
                "size": "1344x768" if ratio == "16:9" else "1024x1024"}

    def _submit_body(self, task, prompt: str, frames: list) -> dict:
        duration = int(task.input.get("duration") or 5)
        body = {"model": self.model, "prompt": prompt,
                "quality": "speed" if os.environ.get("HXMV_ZHIPU_SPEED") else "quality",
                "with_audio": bool(task.input.get("with_audio", False)),
                "size": SIZE_MAP.get(str(task.input.get("resolution") or ""), "1920x1080"),
                "fps": int(task.input.get("fps") or 30),
                "duration": 10 if duration > 7 else 5}
        if frames:
            # 单张 = 首帧（锁一致性）；两张 = [首帧, 尾帧]（顺带把**运镜方向**钉死，
            # 官方字段说明：第一张作首帧、第二张作尾帧）
            body["image_url"] = frames
        return body

    def _poll_url(self, task_id: str) -> str:
        deadline = time.time() + self.timeout
        delay = 5.0
        while time.time() < deadline:
            data = self._get_json(f"async-result/{task_id}")
            status = str(data.get("task_status") or "").upper()
            if status == "SUCCESS":
                videos = data.get("video_result") or []
                url = (videos[0].get("url") if videos else None) or data.get("url")
                if not url:
                    raise ProviderError(f"任务成功但没给视频地址: "
                                        f"{json.dumps(data, ensure_ascii=False)[:200]}", retryable=True)
                return str(url)
            if status in ("FAIL", "FAILED"):
                raise ProviderError(f"上游任务失败: {json.dumps(data, ensure_ascii=False)[:300]}",
                                    retryable=True)
            time.sleep(delay)
            delay = min(delay * 1.4, 15.0)
        raise ProviderError(f"任务 {task_id} 超时（{self.timeout:.0f}s）", retryable=True)
