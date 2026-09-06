"""CLI 入口：python -m hxmv "你的视频目标"

示例：
    python -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟"
    python -m hxmv --fresh "从头开始（清空大脑经验）"
    python -m hxmv --brain /path/brain.json "指定大脑文件"
    OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com/v1 \\
        HXMV_LLM_MODEL=deepseek-chat python -m hxmv "30 秒产品宣传片，现代极简风"
"""
import argparse
import os
import sys

from .core.brain import Brain
from .core.loop import run


def main() -> int:
    ap = argparse.ArgumentParser(prog="hxmv", description="HxMV 自主内容生产闭环")
    ap.add_argument("goal", nargs="*", help="内容生产目标（缺省用示例）")
    ap.add_argument("--fresh", action="store_true", help="清空大脑后从零跑（演示学习曲线用）")
    ap.add_argument("--brain", default=None, help="大脑文件路径（默认 ~/.hxmv/brain.json）")
    args = ap.parse_args()

    if args.fresh:
        path = args.brain or os.path.expanduser("~/.hxmv/brain.json")
        if os.path.exists(path):
            os.remove(path)
        print("🧹 已清空大脑，从零开始")
    brain = Brain(args.brain) if args.brain else Brain()

    goal = " ".join(args.goal) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    try:
        run(goal, brain=brain)
    except KeyboardInterrupt:
        print("\n⏹ 已手动停止（大脑已保存）")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
