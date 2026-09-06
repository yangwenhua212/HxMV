"""质量报告：Critic 的统一输出结构。

设计要点（来自架构评审）：
- 三层 Critic（L1 物理 / L2 视觉 / L3 语义）各自产出 QualityReport，
  PipelineCritic 合并成单一报告交给 Controller。
- report 必须带 failures（机器可判）与 suggestions（可执行修正），
  而不是只有一个分数——分数只能告诉你"不好"，failures/suggestions 才告诉你怎么修。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QualityReport:
    """一次检测的统一输出。score ∈ [0, 1]，越高越好。"""

    layer: str            # 哪个检测层产出：L1_PHYSICS / L2_VISUAL / L3_SEMANTIC / PIPELINE
    score: float = 1.0
    failures: list[str] = field(default_factory=list)   # 机器可判定的失败原因（kebab-case）
    suggestions: list[str] = field(default_factory=list)  # 可执行的修正建议
    detail: str = ""      # 给人看的说明

    @property
    def passed(self) -> bool:
        return not self.failures

    def merge(self, other: "QualityReport") -> "QualityReport":
        """合并另一层报告：取低分、并 failures/suggestions（去重保序）。"""
        def _uniq(items: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for it in items:
                if it not in seen:
                    seen.add(it)
                    out.append(it)
            return out

        return QualityReport(
            layer="PIPELINE",
            score=min(self.score, other.score),
            failures=_uniq(self.failures + other.failures),
            suggestions=_uniq(self.suggestions + other.suggestions),
            detail=(self.detail + " / " + other.detail).strip(" / "),
        )

    def __str__(self) -> str:  # 给终端日志用的单行摘要
        if self.passed:
            return f"[{self.layer}] ✓ 通过 (score={self.score:.2f})"
        return (
            f"[{self.layer}] ✗ 失败 score={self.score:.2f} "
            f"failures={self.failures[:3]} → {self.suggestions[:2]}"
        )
