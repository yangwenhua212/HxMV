"""视频生成 Provider 抽象（v0.2：接真实生成器的桥梁）。

设计要点：
- Provider 只管"把 Task 变成媒体文件/URL"——质量判定仍是 Critic 的事，两者不混淆。
- 真实 API 的失败（网络/超时/额度）≠ 质量 FAIL：
  前者由 Provider 抛 ProviderError 让 Controller 按"基础设施重试"处理；
  后者才走 Refiner 调参重投。闭环里两者路径不同，别揉在一起。
- 异步轮询语义：生成类 API 多是"提交任务 → 轮询结果"，Provider 封装为同步阻塞
  generate()（内部轮询），Executor 拿到 result 再走 Critic。
"""
from __future__ import annotations

import abc


class ProviderError(Exception):
    """基础设施级失败：网络/超时/鉴权/额度。由调用方决定重试策略（与质量 FAIL 分开）。"""

    def __init__(self, message: str, retryable: bool = True, cost_units: float = 0.0):
        super().__init__(message)
        self.retryable = retryable   # False = 永久失败（如鉴权错误），别傻重试
        self.cost_units = cost_units  # 已消耗的成本（部分 API 失败也计费）


class VideoProvider(abc.ABC):
    """一个真实生成服务的适配器。实现 3 个方法即可接入闭环。"""

    name: str = "provider"           # 服务名，用于日志/记忆
    action_map: dict = {}            # 本 provider 支持的 Task.action → API 动作

    @abc.abstractmethod
    def generate(self, task) -> dict:
        """执行一个 Task，返回 result dict（与 Mock 同构：media/defects/元数据/cost_units）。

        注意：
        - 任何 API 失败抛 ProviderError，不要吞
        - result["defects"] 若服务端能直接报告缺陷可填；不能就留空让 Critic 检测
          （真实视频会由 L1/L2/L3 从媒体本身检测，defects 只是"服务端已知"的加速通道）
        """
        raise NotImplementedError

    def supports(self, action: str) -> bool:
        return not self.action_map or action in self.action_map

    def estimate_cost(self, action: str) -> float:
        """执行前预估成本（Budget 用）。默认 1.0，子类按真实计价覆盖。"""
        return 1.0
