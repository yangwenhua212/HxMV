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
                    choices=["mock", "local", "fake", "zhipu", "kling"],
                    help="生成器：mock=模拟世界（默认） local=FFmpeg 真渲染 "
                         "zhipu=智谱 CogVideoX-Flash（真 AI 视频，免费） fake=线上仿真 kling=可灵 API")
    ap.add_argument("--set-key", nargs=2, default=None, metavar=("PROVIDER", "KEY"),
                    help="写入 API Key 到 ~/.hxmv/config.json（例：--set-key zhipu <你的key>）")
    ap.add_argument("--key-status", action="store_true", help="查看各 provider 的 Key 是否已配置")
    ap.add_argument("--doctor", action="store_true",
                    help="部署自检：Python/ffmpeg/编码器/Key/目录/端口，缺什么给什么修复命令")
    ap.add_argument("--out", default=None, help="产物目录（local provider 用，默认 ~/.hxmv/artifacts/<时间戳>）")
    ap.add_argument("--project", default=None, help="项目名：跨 run 记住风格/角色/已生成画面，续做时不重新生成")
    ap.add_argument("--episode", type=int, default=None, help="第几集（默认自动递增）")
    ap.add_argument("--style", default=None, help="项目风格（首次创建项目时用，默认 cinematic）")
    ap.add_argument("--list-projects", action="store_true", help="列出已有项目档案")
    args = ap.parse_args()

    from .core import config
    if args.set_key:
        provider, key = args.set_key[0].strip().lower(), args.set_key[1].strip()
        if not key:
            print("⚠ Key 不能为空")
            return 1
        shown = config.set_api_key(provider, key)
        print(f"✅ 已保存 {provider} 的 API Key：{shown}")
        print(f"   位置：{config.CONFIG_PATH}（权限 600，只本机可读）")
        return 0
    if args.key_status:
        print(f"配置文件：{config.CONFIG_PATH}")
        for prov in ("zhipu", "kling"):
            k = config.api_key(prov)
            print(f"  {prov:8s} {'✅ 已配置 ' + config.mask(k) if k else '❌ 未配置'}")
        return 0
    if args.doctor:
        from .core import doctor
        checks = doctor.run_checks()
        print(doctor.render(checks))
        hard = [c for c in checks if not c["ok"] and c["name"] in
                ("Python", "ffmpeg/ffprobe", "数据目录可写")]
        return 1 if hard else 0

    if args.list_projects:
        from .core.project import Project, PROJECTS_DIR
        rows = Project.list_all()
        print(f"项目档案目录：{PROJECTS_DIR}")
        if not rows:
            print("（还没有项目——用 --project 名字 跑一次就会建档）")
            return 0
        for r in rows:
            print(f"  {r['id']:18s} 风格={r['style']:12s} 角色 {r['characters']} 场景/画面 {r['shots']} "
                  f"分集 {r['episodes']}")
        return 0

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

    project = None
    if args.project:
        from .core.project import Project
        project = Project.load(args.project)
        if not project.episodes and not project.characters:
            project.title = args.project
        if args.style:
            project.style = args.style
        project.save()

    goal = " ".join(args.goal) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    done: dict = {}

    def _capture(event: dict) -> None:
        if event.get("type") == "run.done":
            done.update(event)

    try:
        run(goal, brain=brain, project=project, episode=args.episode, emit=_capture)
    except KeyboardInterrupt:
        print("\n⏹ 已手动停止（大脑已保存）")
        return 130
    out = os.environ.get("HXMV_ARTIFACTS")
    if out and os.path.isdir(out):
        print(f"  📁 产物目录：{out}")
    # 命令行跑完也能把成品推给客户端（HXSync/飞书）——服务器跑的那条路在 server.py 里
    if done and os.environ.get("HXMV_NOTIFY_URL"):
        from .core import notify as _notify
        arts = out or (os.path.join(project.dir, "artifacts") if project else "")
        payload = _notify.build_payload(goal, done, arts,
                                        base=os.environ.get("HXMV_PUBLIC_BASE", ""))
        print(f"  📤 推送成品：{_notify.notify(payload)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
