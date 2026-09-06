"""CLI 入口：python -m hxmv "你的视频目标"

示例：
    python -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟"
    OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com/v1 \\
        HXMV_LLM_MODEL=deepseek-chat python -m hxmv "30 秒产品宣传片，现代极简风"
"""
import sys

from .core.loop import run


def main() -> int:
    goal = " ".join(sys.argv[1:]) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    try:
        run(goal)
    except KeyboardInterrupt:
        print("\n⏹ 已手动停止")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
