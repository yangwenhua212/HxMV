"""基准集跑分：固定目标 × 多轮，只让"大脑"累积经验，看闭环是不是越跑越顺。

为什么这么设计（别的做法都会测出假曲线）：
- **每轮换新项目**：清掉画面指纹缓存，否则第 2 轮开始全是"复用旧文件"，分数暴涨但是假的
- **大脑跨轮共享**：项目清了、经验留着 → 测的才是"经验"这一项
- **规划器锁死**（HXMV_PLANNER=mock）+ mock provider：秒级、0 成本、可重复；
  真媒体用 `--provider local` 另跑（慢，但走真 ffmpeg 产物）

用法：
    python3 tools/bench.py                      # 4 轮 × 基准集，mock provider
    python3 tools/bench.py --rounds 6 --provider local
    python3 tools/bench.py --fresh              # 丢掉旧大脑，从零看完整曲线
产物：docs/BENCH.md（表格 + 曲线）与 docs/bench_results.json（原始数据）
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIR = os.path.join(os.path.expanduser("~/.hxmv"), "bench")


def _prepare_env(provider: str, brain_path: str, projects_dir: str) -> None:
    """必须在 import hxmv 之前设好：这几个路径是在模块导入时读的。"""
    os.environ["HXMV_BRAIN"] = brain_path
    os.environ["HXMV_PROJECTS"] = projects_dir
    os.environ["HXMV_PLANNER"] = "mock"          # 规划器锁死，分数才有可比性
    os.environ["HXMV_PROVIDER"] = "" if provider == "mock" else provider
    sys.path.insert(0, ROOT)


def run_one(goal: str, project_id: str, brain, provider: str) -> dict:
    """跑一个目标，回收指标。闭环自身的 stdout 全部吞掉（跑分只留数据）。"""
    from hxmv.core.loop import run
    from hxmv.core.project import Project

    done: dict = {}
    t0 = time.time()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        state = run(goal, brain=brain, project=Project.load(project_id),
                    emit=lambda e: done.update(e) if e.get("type") == "run.done" else None,
                    verbose=False)
    seconds = round(time.time() - t0, 2)
    completed = done.get("completed", [])
    # 首轮通过 = **整体通过**且每一项都没用过修正（refine_history 空）。
    # 少了前半句就会出现"首轮通过 ✅ / 最终失败 ❌"这种自相矛盾的行（实测踩过）。
    first_pass = bool(completed) and not done.get("failed") and \
        all(not c.get("refine_history") for c in completed)
    return {
        "attempts": int(done.get("attempts", 0)),
        "completed": len(completed),
        "failed": len(done.get("failed", [])),
        "reused": int(done.get("n_reused", 0)),
        "cost_units": float(done.get("cost_units", 0.0)),
        "first_pass": first_pass,
        "pass": bool(completed) and not done.get("failed"),
        "fixes": sorted({f.split(":")[0].strip() for c in completed
                         for f in (c.get("refine_history") or [])}),
        "seconds": seconds,
        "output": buf.getvalue().strip().split("\n")[-1] if buf.getvalue() else "",
        "state_ok": state is not None,
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    return {
        "goals": n,
        "pass_rate": round(sum(1 for r in rows if r["pass"]) / n, 3) if n else 0.0,
        "first_pass_rate": round(sum(1 for r in rows if r["first_pass"]) / n, 3) if n else 0.0,
        "avg_attempts": round(sum(r["attempts"] for r in rows) / n, 2) if n else 0.0,
        "cost_units": round(sum(r["cost_units"] for r in rows), 3),
        "seconds": round(sum(r["seconds"] for r in rows), 1),
    }


def svg_curve(rounds: list[dict], width: int = 760, height: int = 280) -> str:
    """纯 Python 画两条曲线：首轮通过率（%）与平均尝试次数。不引依赖，产物可直接嵌文档。"""
    pad_l, pad_r, pad_t, pad_b = 46, 46, 30, 40
    n = len(rounds)
    xs = [pad_l + (width - pad_l - pad_r) * (i / max(1, n - 1)) for i in range(n)]
    def y_of(v, vmax):
        return pad_t + (height - pad_t - pad_b) * (1 - (v / vmax if vmax else 0))
    max_att = max([r["avg_attempts"] for r in rounds] + [1.0]) * 1.15
    p1 = [(x, y_of(r["first_pass_rate"] * 100, 100)) for x, r in zip(xs, rounds)]
    p2 = [(x, y_of(r["avg_attempts"], max_att)) for x, r in zip(xs, rounds)]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
           f'<rect width="{width}" height="{height}" fill="#0A1210"/>']
    for i in range(5):                                     # 横向网格（左轴 0-100%）
        y = pad_t + (height - pad_t - pad_b) * i / 4
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                   f'stroke="#1F3A31" stroke-width="1"/>')
        out.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" fill="#8FA89E" font-size="11" '
                   f'text-anchor="end">{100 - i * 25}</text>')
    for x, r in zip(xs, rounds):
        out.append(f'<text x="{x:.1f}" y="{height - 16}" fill="#8FA89E" font-size="11" '
                   f'text-anchor="middle">第 {r["round"]} 轮</text>')
    def poly(pts, color, label, dash=""):
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        seg = [f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2.5"{dash}/>']
        n_pts = len(pts)
        for i, ((x, y), r) in enumerate(zip(pts, rounds)):
            v = (f'{r["first_pass_rate"] * 100:.0f}%' if color == "#4CC38A"
                 else f'{r["avg_attempts"]:.2f}')
            # 首尾点的数值标签往里收，不然会压到纵轴刻度上（实测第一轮的 8.38 会盖住 100）
            anchor = "start" if i == 0 else ("end" if i == n_pts - 1 else "middle")
            dx = 10 if i == 0 else (-10 if i == n_pts - 1 else 0)
            seg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
            seg.append(f'<text x="{x + dx:.1f}" y="{y - 9:.1f}" fill="{color}" font-size="11" '
                       f'text-anchor="{anchor}">{v}</text>')
        return seg
    out += poly(p1, "#4CC38A", "首轮通过率")
    out += poly(p2, "#E0B25C", "平均尝试次数", ' stroke-dasharray="5 4"')
    out.append(f'<text x="{pad_l}" y="20" fill="#4CC38A" font-size="12">'
               f'— 首轮通过率（%，左轴）</text>')
    out.append(f'<text x="{pad_l + 190}" y="20" fill="#E0B25C" font-size="12">'
               f'- - 平均尝试次数</text>')
    out.append('</svg>')
    return "\n".join(out)


def render_md(results: dict, path: str) -> None:
    rounds = results["rounds"]
    lines = ["# 基准集跑分", "",
             f"- 生成器：`{results['provider']}` ｜ 规划器：`{results['planner']}` ｜ "
             f"每轮 {results['goals']} 个固定目标 ｜ 共 {len(rounds)} 轮",
             "- 每轮**换新项目**（清画面缓存）、**共享大脑**（经验累加）——曲线涨了才是真学到东西",
             "- 首轮通过率 = 一次都不用修正就过；平均尝试 = 闭环为每个目标花的平均尝试数", "",
             "![跑分曲线](bench.svg)", "",
             "| 轮次 | 最终通过率 | 首轮通过率 | 平均尝试次数 | 成本 | 耗时(s) |",
             "| :--: | :--: | :--: | :--: | :--: | :--: |"]
    for r in rounds:
        lines.append(f"| 第 {r['round']} 轮 | {r['pass_rate'] * 100:.0f}% | "
                     f"{r['first_pass_rate'] * 100:.0f}% | {r['avg_attempts']:.2f} | "
                     f"{r['cost_units']:.2f} | {r['seconds']:.1f} |")
    first, last = rounds[0], rounds[-1]
    d1 = last["first_pass_rate"] - first["first_pass_rate"]
    d2 = last["avg_attempts"] - first["avg_attempts"]
    dcost = last["cost_units"] - first["cost_units"]
    dsec = last["seconds"] - first["seconds"]
    dpass = last["pass_rate"] - first["pass_rate"]
    lines += ["", "## 结论（照实写，一项一项看）", "",
              f"- 最终通过率：{first['pass_rate'] * 100:.0f}% → {last['pass_rate'] * 100:.0f}%"
              f"（{dpass * 100:+.0f} pp）",
              f"- 平均尝试次数：{first['avg_attempts']:.2f} → {last['avg_attempts']:.2f}"
              f"（{d2:+.2f}）",
              f"- 成本：{first['cost_units']:.0f} → {last['cost_units']:.0f}（{dcost:+.0f}）",
              f"- 耗时：{first['seconds']:.1f}s → {last['seconds']:.1f}s（{dsec:+.1f}s）",
              f"- **首轮通过率**：{first['first_pass_rate'] * 100:.0f}% → "
              f"{last['first_pass_rate'] * 100:.0f}%（{d1 * 100:+.0f} pp）", ""]
    if d2 < 0 and dcost < 0:
        lines.append("- 返工量/成本在降 → 大脑的经验进了规划（起手参数带着上次的教训），"
                     "同类失败被少踩了一遍。")
    else:
        lines.append("- 返工量/成本没降 → 经验在记但没进决策，或者这批目标起手就已经是对的"
                     "（看下面的目标明细）。")
    if d1 <= 0:
        lines.append("- 但**首轮通过率没涨**：经验只覆盖了一部分缺陷类型，其余仍从默认起手。"
                     "看「修正过」那列反复出现的键，那就是还没进起手参数的短板。")
    lines.append("- 曲线**不保证单调**：生成器本身带随机性，单轮波动不代表退步，看多轮走向。")
    lines += ["", "## 每轮目标明细", ""]
    for r in rounds:
        lines.append(f"**第 {r['round']} 轮**")
        lines.append("")
        lines.append("| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |")
        lines.append("| :--- | :--: | :--: | :--: | :--- | :--: |")
        for g in r["items"]:
            lines.append(f"| {g['goal']} | {'✅' if g['pass'] else '❌'} | "
                         f"{'✅' if g['first_pass'] else '—'} | {g['attempts']} | "
                         f"{'、'.join(g['fixes']) or '—'} | {g['seconds']} |")
        lines.append("")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="HxMV 基准集跑分")
    ap.add_argument("--set", default=os.path.join(ROOT, "tools", "bench_set.json"))
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--provider", default="mock", help="mock（默认，秒级） / local（真媒体，慢）")
    ap.add_argument("--brain", default=os.path.join(BENCH_DIR, "brain.json"))
    ap.add_argument("--fresh", action="store_true", help="丢掉旧大脑，从零跑完整曲线")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "BENCH.md"))
    ap.add_argument("--json", default=os.path.join(ROOT, "docs", "bench_results.json"))
    ap.add_argument("--render-only", action="store_true",
                    help="不跑分，拿已有 --json 重新生成报告与曲线（改文案/改图表时用）")
    args = ap.parse_args()

    if args.render_only:
        with open(args.json, encoding="utf-8") as f:
            results = json.load(f)
        render_md(results, args.out)
        svg_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "bench.svg")
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg_curve(results["rounds"]))
        print(f"已重渲染：{args.out} / {svg_path}")
        return 0

    with open(args.set, encoding="utf-8") as f:
        spec = json.load(f)
    goals = spec["goals"]
    projects_dir = os.path.join(BENCH_DIR, "projects")
    if args.fresh:
        for p in (args.brain,):
            if os.path.exists(p):
                os.remove(p)
    if not args.fresh and os.path.isdir(projects_dir):
        shutil.rmtree(projects_dir, ignore_errors=True)     # 每轮都换项目，旧的一律不留

    _prepare_env(args.provider, args.brain, projects_dir)
    from hxmv.core.brain import Brain
    from hxmv.core.project import Project, PROJECTS_DIR

    brain = Brain(args.brain)
    print(f"基准集：{len(goals)} 个目标 × {args.rounds} 轮 ｜ provider={args.provider} "
          f"｜ 大脑={args.brain}（{'空' if not brain.size else brain.stats()}）")
    rounds = []
    for rnd in range(1, args.rounds + 1):
        items = []
        for g in goals:
            pid = f"bench-{g['id']}-r{rnd}"
            res = run_one(g["goal"], pid, brain, args.provider)
            res["goal"] = g["goal"]
            res["id"] = g["id"]
            items.append(res)
            flag = "首轮过" if res["first_pass"] else ("通过" if res["pass"] else "失败")
            print(f"  第{rnd}轮 {g['id']}: {flag}（尝试 {res['attempts']}，"
                  f"修正 {'、'.join(res['fixes']) or '无'}，{res['seconds']}s）")
        s = summarize(items)
        s["round"] = rnd
        s["items"] = items
        s["brain"] = brain.stats()
        rounds.append(s)
        print(f"→ 第{rnd}轮合计：通过 {s['pass_rate'] * 100:.0f}% ｜ "
              f"首轮通过 {s['first_pass_rate'] * 100:.0f}% ｜ 平均尝试 {s['avg_attempts']} ｜ "
              f"{s['seconds']}s ｜ 大脑 {brain.stats()}")

    results = {"provider": args.provider, "planner": "mock", "goals": len(goals),
               "projects_dir": PROJECTS_DIR, "rounds": rounds}
    os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    render_md(results, args.out)
    svg_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "bench.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(svg_curve(rounds))
    print(f"\n✅ 报告：{args.out}\n✅ 曲线：{svg_path}\n✅ 原始数据：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
