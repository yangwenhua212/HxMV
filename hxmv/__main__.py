"""CLI 入口：python -m hxmv "你的视频目标"

示例：
    python -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟"
    python -m hxmv --fresh "从头开始（清空大脑经验）"
    python -m hxmv --brain /path/brain.json "指定大脑文件"
    python -m hxmv --provider local "雪地里的柯基"      # FFmpeg 真渲染：真出片 + 真检测
    python -m hxmv --provider local --out /tmp/film "…"  # 指定产物目录
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
    ap.add_argument("--provider", default=None,
                    choices=["mock", "local", "fake", "kling"],
                    help="生成器：mock=模拟世界（默认） local=FFmpeg 真渲染 fake=线上仿真 kling=可灵 API")
    ap.add_argument("--out", default=None, help="产物目录（local provider 用，默认 ~/.hxmv/artifacts/<时间戳>）")
    args = ap.parse_args()

    if args.fresh:
        path = args.brain or os.path.expanduser("~/.hxmv/brain.json")
        if os.path.exists(path):
            os.remove(path)
        print("🧹 已清空大脑，从零开始")
    if args.provider:
        os.environ["HXMV_PROVIDER"] = "" if args.provider == "mock" else args.provider
    if args.out:
        os.environ["HXMV_ARTIFACTS"] = os.path.abspath(args.out)
    brain = Brain(args.brain) if args.brain else Brain()

    goal = " ".join(args.goal) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    try:
        run(goal, brain=brain)
    except KeyboardInterrupt:
        print("\n⏹ 已手动停止（大脑已保存）")
        return 130
    out = os.environ.get("HXMV_ARTIFACTS")
    if out and os.path.isdir(out):
        print(f"  📁 产物目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
