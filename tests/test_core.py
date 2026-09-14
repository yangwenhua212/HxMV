"""HxMV 纯函数单元测试（标准库 unittest，零第三方依赖，和项目本身一致）。

为什么补这些用例：项目里的关键不变量一直靠"跑一遍看曲线"守着，而这些恰恰是
最容易在改动中被静默破坏、又最难从运行结果里看出来的东西：

- 指纹漏了一个参数 → 修正悄悄失效、命中缓存给了旧画面（实测踩过三次）
- Critic 建议的修正 Refiner 不认识 → 重试白烧一次（同样踩过）
- drawtext / provider 路由在别的平台上悄悄退化

跑法：
    python -m unittest discover -s tests -v
    python tests/test_core.py            # 也可以直接跑
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hxmv.core import executor as exec_mod
from hxmv.core.brain import Entry, similarity
from hxmv.core.critic import DEFECT_FIXES
from hxmv.core.executor import MockVideoExecutor, make_executor
from hxmv.core.project import _FP_KEYS, fingerprint, fp_params
from hxmv.core.refiner import _ADJUST, _HUMAN_HINT, _apply
from hxmv.core.state import Task
from hxmv.media.sheet import crop_box


# ---------------------------------------------------------------- 首帧裁切
class CropBoxTest(unittest.TestCase):
    def test_keep_returns_full_image(self):
        self.assertEqual(crop_box((1000, 500), "keep"), (0, 0, 1000, 500))

    def test_auto_keeps_wide_image(self):
        # 已经够宽（>=16:9）的图本身就是一帧画面，不该再裁
        self.assertEqual(crop_box((1920, 1080), "auto"), (0, 0, 1920, 1080))

    def test_auto_near_square_crops_to_16_9(self):
        left, top, right, bottom = crop_box((1000, 1000), "auto")
        self.assertEqual((left, top, right), (0, 0, 1000))
        self.assertEqual(bottom, round(1000 / (16 / 9)))   # = 562

    def test_auto_tall_sheet_takes_top_half(self):
        left, top, right, bottom = crop_box((800, 2000), "auto")
        self.assertEqual((left, top, right), (0, 0, 800))
        self.assertEqual(bottom, 1000)

    def test_crop_top_forces_16_9(self):
        left, top, right, bottom = crop_box((1920, 1080), "crop_top")
        self.assertEqual((left, top, right, bottom), (0, 0, 1920, 1080))

    def test_rejects_invalid_size(self):
        with self.assertRaises(ValueError):
            crop_box((0, 100))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            crop_box((100, 100), "sideways")


# ---------------------------------------------------------------- 画面指纹
class FingerprintTest(unittest.TestCase):
    def _params(self) -> dict:
        task = Task("GENERATE_SHOT",
                    input={"prompt": "柯基雪地奔跑", "duration": 5, "seed": 7},
                    constraints={"character": "corgi", "reference_strength": 0.6})
        return fp_params(task, provider="local")

    def test_is_deterministic(self):
        self.assertEqual(fingerprint(self._params()), fingerprint(self._params()))

    def test_is_12_hex_chars(self):
        fp = fingerprint(self._params())
        self.assertEqual(len(fp), 12)
        int(fp, 16)   # 不是 16 进制会抛错

    def test_ignores_key_order(self):
        p = self._params()
        shuffled = dict(reversed(list(p.items())))
        self.assertEqual(fingerprint(p), fingerprint(shuffled))

    def test_every_whitelisted_key_actually_matters(self):
        """回归防线：**每个**参与指纹的参数都必须真的影响指纹。

        老坑：provider 手写指纹字典，新加的参数（with_audio、_guard_*）没被列进去
        → 改了参数但指纹不变 → 命中档案返回旧画面 → 修正等于没修。
        """
        base = self._params()
        for key in _FP_KEYS:
            with self.subTest(key=key):
                changed = dict(base)
                changed[key] = f"__changed_{key}__"
                self.assertNotEqual(fingerprint(base), fingerprint(changed),
                                    f"{key} 的变化没有反映到指纹里")

    def test_fp_params_ignores_extra_keys(self):
        task = Task("GENERATE_SHOT", input={"prompt": "p", "not_in_fingerprint": 123})
        unknown = fp_params(task, provider="local")
        changed = dict(unknown)
        changed["not_in_fingerprint"] = "whatever"
        self.assertEqual(fingerprint(unknown), fingerprint(changed))


# ---------------------------------------------------------------- 调参旋钮
class RefinerTest(unittest.TestCase):
    def test_reference_strength_is_capped_at_one(self):
        task = Task("GENERATE_SHOT", constraints={"reference_strength": 0.9})
        _apply(task, "increase_reference_strength")
        self.assertEqual(task.constraints["reference_strength"], 1.0)

    def test_reduce_motion_respects_lower_bound(self):
        task = Task("GENERATE_SHOT", constraints={"motion_scale": 0.25})
        _apply(task, "reduce_motion_scale")
        self.assertGreaterEqual(task.constraints["motion_scale"], 0.2)

    def test_boolean_fix_sets_value_directly(self):
        task = Task("GENERATE_SHOT", input={"resolution": "480p"})
        _apply(task, "increase_resolution")
        self.assertEqual(task.input["resolution"], "720p")

    def test_numeric_result_is_rounded(self):
        # 浮点数不 round 会在日志里出现 0.6000000000000001（踩过两次）
        task = Task("GENERATE_SHOT", constraints={"reference_strength": 0.30000000000000004})
        _apply(task, "increase_reference_strength")
        self.assertEqual(task.constraints["reference_strength"], 0.5)

    def test_unknown_action_is_a_noop(self):
        task = Task("GENERATE_SHOT")
        self.assertEqual(_apply(task, "do_something_impossible"), "")
        self.assertEqual(task.input, {})

    def test_every_adjust_has_a_human_hint(self):
        self.assertEqual(set(_ADJUST), set(_HUMAN_HINT))

    def test_every_critic_suggestion_is_actionable(self):
        """Critic 点名的每条修正，Refiner 都必须有对应的旋钮。

        否则会出现"报了缺陷 → 重试一次 → 什么都没改"的空转（白烧一次生成）。
        """
        for defect, (suggestion, _desc) in DEFECT_FIXES.items():
            with self.subTest(defect=defect):
                self.assertIn(suggestion.split(":", 1)[0], _ADJUST)


# ---------------------------------------------------------------- 大脑检索
class BrainTest(unittest.TestCase):
    def test_identical_text_scores_one(self):
        self.assertAlmostEqual(similarity("柯基在雪地奔跑", "柯基在雪地奔跑"), 1.0)

    def test_unrelated_text_scores_low(self):
        self.assertLess(similarity("柯基在雪地奔跑", "Spring Boot 连接 MySQL"), 0.1)

    def test_empty_input_scores_zero(self):
        self.assertEqual(similarity("", "anything"), 0.0)
        self.assertEqual(similarity("anything", ""), 0.0)

    def test_freshly_created_entry_does_not_decay(self):
        e = Entry(content="x", importance=0.8)
        e.decay()
        self.assertEqual(e.importance, 0.8)

    def test_stale_entry_decays_but_never_to_zero(self):
        e = Entry(content="x", importance=0.8, last_hit=1.0)   # 很久以前命中过
        e.decay()
        self.assertLess(e.importance, 0.8)
        self.assertGreaterEqual(e.importance, 0.1)


# ------------------------------------------------------- provider 路由（P0 回归）
class ProviderRoutingTest(unittest.TestCase):
    """守的是这两条曾经同时坏过的规则。

    历史事故：CLI 把 --provider mock 写成空串表示"默认"，于是自动选择把空串
    当成"用户没选"，一律落到 local → 模拟世界永远进不去。
    """

    def setUp(self):
        self._saved = os.environ.get("HXMV_PROVIDER")
        os.environ.pop("HXMV_PROVIDER", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HXMV_PROVIDER", None)
        else:
            os.environ["HXMV_PROVIDER"] = self._saved

    def test_explicit_mock_is_never_downgraded(self):
        os.environ["HXMV_PROVIDER"] = "mock"
        self.assertIsInstance(make_executor(), MockVideoExecutor)

    def test_auto_provider_falls_back_to_local(self):
        # 没配任何 Key 时，"至少出一个真文件"比落回 mock 更符合预期
        self.assertEqual(exec_mod._auto_provider(), "local")

    def test_unspecified_env_still_reaches_a_provider(self):
        # 空/未设置 ≠ 无声降级；必须真的选到一个 provider
        self.assertNotEqual(exec_mod._auto_provider(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
