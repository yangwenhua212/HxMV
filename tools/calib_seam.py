"""接缝阈值标定（一次性脚本，不入库）：好接缝 / 坏接缝 / 淡入淡出 各自的实测相似度。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hxmv.core import camera                       # noqa: E402
from hxmv.core.state import Task                   # noqa: E402
from hxmv.media import probe                       # noqa: E402
from hxmv.providers.local_render import LocalRenderProvider  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hxmv_seam2_")
os.makedirs(out, exist_ok=True)
p = LocalRenderProvider(outdir=out, project=None)


def shot(move, key, seed, strength=1.0):
    return p.generate(Task("GENERATE_SHOT",
                           input={"prompt": f"seam {key} {move} s{seed}", "duration": 4, "seed": seed,
                                  "resolution": "720p", "fps": 30, "trim_black": True},
                           constraints={"character": key, "camera": move,
                                        "camera_speed": "normal", "camera_amount": 0.5,
                                        "motion_scale": 0.6, "reference_strength": strength}))["media"]


def compose(shots, transition=None, tag=""):
    for i, s in enumerate(shots, 1):
        p._shots[f"k{i}"] = s
        p._shot_fp[i] = f"calib{i}"
    res = p.generate(Task("COMPOSE",
                          input={"shots": [f"k{i}" for i in range(1, len(shots) + 1)],
                                 "output": f"final_{tag}.mp4", "transition": transition},
                          constraints={}))
    return res["output"]


a = shot(camera.MOVE_PUSH_IN, "hero", 11, 1.0)      # 同场戏、同角色、状态一致 → 好接缝
b = shot(camera.MOVE_STATIC, "hero", 22, 1.0)
bad = shot(camera.MOVE_STATIC, "hero", 33, 0.15)    # 同场戏但参考强度极低 → 色调大幅漂移 → 坏接缝

cases = [("好接缝·硬切", [a, b], None), ("好接缝·淡入淡出", [a, b], "fade"),
         ("坏接缝·硬切", [a, bad], None), ("坏接缝·淡入淡出", [a, bad], "fade")]
print(f"输出目录 {out}")
for name, shots, trans in cases:
    film = compose(shots, trans, tag=name.replace("·", "_"))
    defects, seams = probe.seam_defects(film, shots, scenes=["s01", "s01"], transition=trans)
    dur = (probe.probe_container(film) or {}).get("duration")
    print(f"{name:16s} 时长={dur:6.2f} seams={seams} → defects={defects}")
