"""可灵(Kling)风格视频 API 接入示例（v0.2 骨架）。

说明：
- 以"提交任务 → 轮询 → 拿视频 URL"的通用流程为蓝本，请求/响应按可灵 v1 文档结构；
  接其他服务（Veo/Runway/即梦）时照 docs/PROVIDERS.md 的映射表改即可。
- 没有 API Key 也能看懂接入点：三个 TODO 处填你自己的鉴权与请求细节。
- 未配置时不参与闭环——executor 工厂只在 HXMV_PROVIDER=kling 时启用本类。

关键桥接逻辑（与 Mock 不同的地方）：
1. Task.constraints 里的参考强度/风格 → API 参数（角色参考图、首尾帧等）
2. 轮询期间的失败 → ProviderError（基础设施层，retryable 按错误码）
3. 服务端已知的质量问题（如审核失败）→ 放进 result["defects"] 走 Critic 的既有通道
4. cost_units 用真实计价（每生成一次的价格），Budget 才有意义
"""
from __future__ import annotations

import os
import time

from .base import ProviderError, VideoProvider

KLING_API = os.environ.get("HXMV_KLING_API", "https://api.klingai.com/v1/videos")
TEXT_TO_VIDEO_MODEL = "kling-v1"   # TODO: 换成你账号可用的模型

# Task.action → Kling 端点（本示例只接文生视频）
ACTION_TO_ENDPOINT = {
    "GENERATE_SHOT": "text2video",     # POST /videos/text2video
}


class KlingStyleProvider(VideoProvider):
    """以可灵为蓝本的接入示例。未填 API Key 时构造即抛错，由工厂捕获后落 Mock。"""

    name = "kling"
    action_map = ACTION_TO_ENDPOINT

    def __init__(self, project=None):
        self.api_key = os.environ.get("HXMV_KLING_KEY", "")
        self.project = project
        if not self.api_key:
            raise ProviderError("未配置 HXMV_KLING_KEY", retryable=False)

    # ---- 任务 1：Task → API 请求 ----
    def _build_request(self, task) -> dict:
        duration = int(task.input.get("duration", 5))
        # constraints 里的工程参数 → 生成器能懂的东西
        # （character 参考强度在各家的实现里通常是"参考图+权重"，此处示意）
        prompt = task.input.get("prompt", "")
        if task.constraints.get("semantic_guard"):
            prompt += "。（严格贴合分镜描述，不添加未描述元素）"
        return {
            "model_name": TEXT_TO_VIDEO_MODEL,
            "prompt": prompt,
            "duration": str(duration),
            "mode": "pro",
            "cfg_scale": float(task.constraints.get("reference_strength", 0.4)) * 10,
            # TODO: 角色参考图/首尾帧（一致性真正的抓手）：
            #   "image_tail": [{"id": asset_ref}],  # 来自 Asset Manager
        }

    # ---- 任务 2：轮询直到出片 ----
    def _poll(self, task_id: str, timeout_s: int = 180) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(5)
            # TODO: GET /videos/{task_id} 查询任务状态
            resp = self._api_get(task_id)
            status = resp.get("task_status")  # submitted / processing / succeed / failed
            if status == "succeed":
                return resp
            if status == "failed":
                raise ProviderError(f"服务端生成失败: {resp.get('task_status_msg')}",
                                    retryable=True, cost_units=self.estimate_cost("GENERATE_SHOT"))
        raise ProviderError("轮询超时", retryable=True)

    def _api_get(self, task_id: str) -> dict:
        # TODO: 带鉴权的 GET 请求
        raise NotImplementedError("填你的 API 鉴权与轮询实现")

    def _api_post(self, body: dict) -> dict:
        # TODO: 带鉴权的 POST 请求
        raise NotImplementedError("填你的 API 提交实现")

    # ---- 任务 3：把服务端结果桥接回闭环 result 结构 ----
    def generate(self, task) -> dict:
        if not self.supports(task.action):
            raise ProviderError(f"{self.name} 不支持 {task.action}", retryable=False)

        body = self._build_request(task)
        submitted = self._api_post(body)                      # 提交
        task_id = submitted.get("task_id")
        if not task_id:
            raise ProviderError("提交失败：无 task_id", retryable=True)
        done = self._poll(task_id)                            # 轮询
        data = done.get("task_result", {}).get("videos", [{}])[0]

        defects: list[str] = []
        # 服务端能直接报告的已知问题走 defects 通道（比如审核/内容警告）
        if done.get("task_status_msg") and "审核" in str(done.get("task_status_msg")):
            defects.append("content_policy")

        return {
            "media": data.get("url", ""),        # 真实媒体 URL（L1/L2/L3 从这里抽帧检测）
            "duration": task.input.get("duration", 5),
            "defects": defects,
            "params": {"task_id": task_id, "model": TEXT_TO_VIDEO_MODEL,
                       "provider": self.name},
            "cost_units": self.estimate_cost(task.action),
        }

    def estimate_cost(self, action: str) -> float:
        # TODO: 按你账号的真实计价（如 5s 视频 pro 模式约 ¥2/次）
        return 2.0
