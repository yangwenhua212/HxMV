"""运镜标定（一次性脚本，不入库）：把每种运镜真渲染出来，量位移/缩放/一致度/静止。

用法：python3 /tmp/calib_camera.py [输出目录]
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hxmv.core import camera                       # noqa: E402
from hxmv.core.project import Project              # noqa: E402
from hxmv.core.state import Task                   # noqa: E402
from hxmv.media import probe                       # noqa: E402
from hxmv.providers.local_render import LocalRenderProvider  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hxmv_calib_")
os.makedirs(out, exist_ok=True)
# 不挂项目档案：标定要的是"每次都真渲染"，别让指纹复用把某一档直接抄过来
provider = LocalRenderProvider(outdir=out, project=None)

# 先造一张参考图（占位资产：有纹理，能真的量到平移/缩放）
ref_task = Task("GENERATE_CHARACTER", input={"prompt": "标定角色"},
                constraints={"asset_key": "hero"}, quality={"min_score": 0.0})
provider.generate(ref_task)
ref = os.path.join(out, "asset_hero.png")

moves = [camera.MOVE_STATIC, camera.MOVE_PUSH_IN, camera.MOVE_PULL_OUT,
         camera.MOVE_PAN_RIGHT, camera.MOVE_PAN_LEFT, camera.MOVE_TILT_DOWN,
         camera.MOVE_ORBIT, camera.MOVE_HANDHELD]
print(f"输出目录 {out}")
print(f"{'规格':12s} {'实测缩放':>8s} {'横移px':>7s} {'俯仰px':>7s} {'一致度':>7s} "
      f"{'静止s':>6s} {'切镜':>4s} {'缺陷'}")
rows = []
for i, move in enumerate(moves):
    task = Task("GENERATE_SHOT",
                input={"prompt": f"标定镜头 {move}", "duration": 5, "seed": 100 + i,
                       "resolution": "720p", "fps": 30, "trim_black": True},
                constraints={"character": "hero", "scene": "s01", "style": "cinematic",
                             "camera": move, "camera_speed": "normal", "camera_amount": 0.5,
                             "motion_scale": 0.6, "reference_strength": 1.0},
                quality={"min_score": 0.5})
    res = provider.generate(task)
    m = probe.inspect(res["media"], expect_duration=5)
    cam = (m or {}).get("camera") or {}
    cons = probe.appearance_consistency(res["media"], res["reference_baseline"])
    defects = [d for d in ((m or {}).get("defects") or [])]
    rows.append((move, cam, cons, m, defects))
    print(f"{move:12s} {cam.get('zoom', 0):8.3f} {cam.get('pan', 0):+7.1f} "
          f"{cam.get('tilt', 0):+7.1f} {(cons if cons is not None else -1):7.3f} "
          f"{(m or {}).get('freeze_seconds', -1):6.2f} {(m or {}).get('scene_cuts', -1):4d} "
          f"{defects}")

# 判据准不准：把实测拿去和规格对
print("\n判据校验（camera_defects）：")
for move, cam, cons, m, defects in rows:
    got = probe.camera_defects(cam, move)
    print(f"  {move:12s} → {got or '符合'}")
