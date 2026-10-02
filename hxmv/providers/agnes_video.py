"""Agnes AI 适配器 —— **只写与别人不同的那几处**，通用逻辑全在 `api_video.py`。

- 网关 `https://apihub.agnes-ai.com/v1`（OpenAI 兼容），同一把 Key 管**画面 + 视频 + 大脑**
- 出图：`POST images/generations`（`size` 是档位字符串 1K/2K… + `ratio`；16:9 1K = 1312×736）
- 视频：`POST videos` 提交（`mode` 必填：text / keyframe / reference）→
  `GET /agnesapi?video_id=…&model_name=…` 轮询（**不在 /v1 下**）→ 顶层 `url` 就是 mp4
- **图生视频走 `mode=keyframe`**：`first_frame` / `last_frame` 吃 data URL 或公网 URL
  → 与智谱一样可以直接喂 base64，**不需要图床、不需要开匿名取图路由**
- flash 档：只认 `size="720P"`、时长 4–12 秒、`images` ≤5、不支持参考视频；限免中
- 免费档限流严格（文本 10 次/分钟、视频排队）→ 撞 429/503 由 ProviderError(retryable) 兜

配置（环境变量优先，其次 ~/.hxmv/config.json）：
    HXMV_AGNES_KEY / AGNES_API_KEY    密钥（`python3 -m hxmv --set-key agnes <KEY>` 可写入）
    HXMV_AGNES_MODEL                  视频档位，默认 agnes-video-2.5-flash
    HXMV_AGNES_BASE                   默认 https://apihub.agnes-ai.com/v1
    HXMV_API_TIMEOUT                  单任务轮询上限秒数（默认 420；免费档排队久，可调大）
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from .base import ProviderError
from .api_video import ApiVideoProvider


class AgnesVideoProvider(ApiVideoProvider):
    name = "agnes"
    spec_id = "agnes"
    video_path = "videos"
    image_path = "images/generations"
    # 产物 CDN 是另一个域名：**带 Authorization 反而 401**（实测 AuthenticationRequired）
    download_auth = False
    # keyframe 模式支持首帧+尾帧 → 运镜能钉死（与智谱 cogvideox-3 同一档能力）
    FIRST_LAST_MODELS = {"agnes-video-2.5", "agnes-video-2.5-flash"}
    # 元/次（2.5 按 0.025 美元/秒 × 5 秒 ≈ 0.9 元；flash 限免）
    COST_UNITS = {"agnes-video-2.5-flash": 0.0, "agnes-video-2.5": 0.9}
    # 官方允许 4–12 秒（字符串）；按能力如实声明，执行器据此钳制"要多久"
    MAX_DURATION = {"agnes-video-2.5-flash": 12.0, "agnes-video-2.5": 12.0}
    # 免费档排队实测要 10 分钟上下才出片（旧默认 420s 会在排队中就被判超时、白重投一次）
    DEFAULT_TIMEOUT = 900.0
    # 免费档视频队列常满（实测连投十几次才挤进去）——老大拍板「免费就行不怕等」，
    # 所以这里默认耐心等：排队就退避重试，最多 30 分钟，不因为一次 503 就把镜头判死。
    SUBMIT_WAIT = 1800.0

    def _image_body(self, prompt: str, ratio: str) -> dict:
        # size 是**档位**不是像素；不给 ratio 会按 1:1 出图
        return {"model": self.image_model, "prompt": prompt, "size": "1K", "ratio": ratio,
                "extra_body": {"response_format": "url"}}

    def _submit_body(self, task, prompt: str, frames: list) -> dict:
        # seconds 必须是 **4–12 的字符串**；mode 必填，错了直接 400
        seconds = max(4, min(12, int(task.input.get("duration") or 5)))
        body = {"model": self.model, "prompt": prompt,
                "mode": "keyframe" if frames else "text",
                "seconds": str(seconds),
                # flash 只认 720P（传别的 400）；付费档给 1080P
                "size": "720P" if "flash" in self.model else "1080P",
                "aspect_ratio": "16:9", "n": 1}
        if frames:
            body["first_frame"] = frames[0]
            if len(frames) > 1:
                body["last_frame"] = frames[1]
        if task.input.get("seed"):
            body["seed"] = int(task.input["seed"])
        return body

    def _poll_url(self, task_id: str) -> str:
        p = urlsplit(self.base)
        url = (f"{p.scheme}://{p.netloc}/agnesapi?video_id={task_id}"
               f"&model_name={self.model}")     # 检索必须带 model_name（不带只对 text 模式有效）
        deadline = time.time() + self.timeout
        delay = 3.0
        while time.time() < deadline:
            data = self._get_abs(url)
            status = str(data.get("status") or "").lower()
            if status == "completed":
                if not data.get("url"):
                    raise ProviderError(f"任务完成但没给视频地址: "
                                        f"{json.dumps(data, ensure_ascii=False)[:200]}", retryable=True)
                return str(data["url"])
            if status == "failed":
                raise ProviderError(f"上游任务失败: {json.dumps(data, ensure_ascii=False)[:300]}",
                                    retryable=True)
            time.sleep(delay)                     # queued / in_progress / pending 都继续等
            delay = min(delay * 1.5, 12.0)
        raise ProviderError(f"任务 {task_id} 超时（{self.timeout:.0f}s）", retryable=True)
