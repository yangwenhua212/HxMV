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
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hxmv.core import camera as cam
from hxmv.core import executor as exec_mod
from hxmv.core import planner as planner_mod
from hxmv.core import story
from hxmv.core.brain import Entry, similarity
from hxmv.core.critic import DEFECT_FIXES
from hxmv.core.executor import MockVideoExecutor, make_executor
from hxmv.core.project import _FP_KEYS, fingerprint, fp_params
from hxmv.core import refiner as refiner_mod
from hxmv.core.refiner import _ADJUST, _HUMAN_HINT, _apply
from hxmv.core.state import ExecutionState, Task, TaskStatus
from hxmv.media import probe
from hxmv.media.probe import THRESHOLDS, detect_defects
from hxmv.media.sheet import crop_box


# ------------------------------------------------- 「被打断的 run」要说得清原因
class MarkOrphansTest(unittest.TestCase):
    def test_mark_orphans_writes_reason_once(self):
        """服务重启带走正在跑的 loop：启动时要补一条带原因的 run.aborted。

        否则作品列表只剩「已中断」，用户会以为项目凭空消失（真机反馈过）。
        """
        import json as _json
        from hxmv import server as srv

        old = srv.RUNS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                srv.RUNS_DIR = tmp
                run_dir = os.path.join(tmp, "20260101-000000-aaaa")
                os.makedirs(run_dir)
                path = os.path.join(run_dir, "events.jsonl")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_json.dumps({"ts": 1, "type": "task.start", "goal": "半截"}) + "\n")
                srv.ACTIVE_RUNS = set()
                srv._mark_orphans()
                lines = open(path, encoding="utf-8").read().strip().splitlines()
                tail = _json.loads(lines[-1])
                self.assertEqual(tail["type"], "run.aborted")
                self.assertIn("重启", tail["reason"])
                srv._mark_orphans()          # 幂等：第二次不再重复追加
                self.assertEqual(len(open(path, encoding="utf-8").read().strip().splitlines()), 2)
        finally:
            srv.RUNS_DIR = old


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
        # 隔离开发机的真配置：config.py 按 ~/.hxmv/config.json 读 Key，
        # 不隔离的话「没配 Key 时回落 local」这条断言在配过 Key 的机器上**必挂**
        # （实测：本机配了智谱 Key → _auto_provider() 返回 zhipu → 测试红了）。
        self._tmp_home = tempfile.mkdtemp(prefix="hxmv-test-home-")
        self._saved_home = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "HXMV_CONFIG")}
        os.environ["HOME"] = self._tmp_home
        os.environ["USERPROFILE"] = self._tmp_home
        os.environ.pop("HXMV_CONFIG", None)
        # config.CONFIG_PATH 是**导入时**算好的常量：只改 HOME 挡不住它已经指向真实配置。
        # 必须显式重定向（不然「没配 Key 时回落 local」这条在有 Key 的机器上必红）。
        from hxmv.core import config as _cfg
        self._saved_cfg_path = _cfg.CONFIG_PATH
        os.environ["HXMV_CONFIG"] = os.path.join(self._tmp_home, "config.json")
        _cfg.CONFIG_PATH = os.environ["HXMV_CONFIG"]

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HXMV_PROVIDER", None)
        else:
            os.environ["HXMV_PROVIDER"] = self._saved
        for k, v in self._saved_home.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from hxmv.core import config as _cfg
        _cfg.CONFIG_PATH = self._saved_cfg_path
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_explicit_mock_is_never_downgraded(self):
        os.environ["HXMV_PROVIDER"] = "mock"
        self.assertIsInstance(make_executor(), MockVideoExecutor)

    def test_auto_provider_falls_back_to_local(self):
        # 没配任何 Key 时，"至少出一个真文件"比落回 mock 更符合预期
        self.assertEqual(exec_mod._auto_provider(), "local")

    def test_unspecified_env_still_reaches_a_provider(self):
        # 空/未设置 ≠ 无声降级；必须真的选到一个 provider
        self.assertNotEqual(exec_mod._auto_provider(), "")


# ------------------------------------------------- L1 扩展判据（v0.8 新增）
class DetectDefectsTest(unittest.TestCase):
    """守住 v0.8 用 ffmpeg 直接量出来的三类判据，以及一条关键的区分：
    **没测量 ≠ 测出来是静音**——老 run 记录里没有 lufs 键，不能被判成 silent_audio。
    """

    def _base(self, **extra) -> dict:
        m = {"width": 1280, "height": 720, "fps": 30, "duration": 5.0,
             "blur_mean": 5.1, "blur_max": 5.4, "scene_cuts": 0, "has_audio": False}
        m.update(extra)
        return m

    def test_clean_metrics_produce_no_defects(self):
        self.assertEqual(detect_defects(self._base()), [])

    def test_blurry_is_separate_from_low_clarity(self):
        """模糊与分辨率不足是两种病：修正方向不同（改提示词 vs 升分辨率），不能混。"""
        m = self._base(blur_mean=20.0, blur_max=25.0)
        self.assertIn("blurry", detect_defects(m))
        self.assertNotIn("low_clarity", detect_defects(m))

    def test_unmeasurable_blur_counts_as_blurry(self):
        # blurdetect 对极模糊输出 nan → 解析成 blur_unmeasurable，不能当成"没测到"
        m = self._base(blur_mean=None, blur_max=THRESHOLDS["blur_unmeasurable"])
        self.assertIn("blurry", detect_defects(m))

    def test_scene_cuts_flagged_only_for_single_shot_tasks(self):
        """成片（COMPOSE）本来就是多镜头拼的，判它 multi_shot 等于把每部成片都判死。"""
        m = self._base(scene_cuts=2)
        self.assertIn("multi_shot", detect_defects(m, expect_single_shot=True))
        self.assertNotIn("multi_shot", detect_defects(m, expect_single_shot=False))

    def test_silence_only_judged_when_audio_expected(self):
        m = self._base(has_audio=True, mean_volume_db=-30.0, silence_ratio=0.97, lufs=-60.0)
        self.assertIn("silent_audio", detect_defects(m, expect_audio=True))
        self.assertNotIn("silent_audio", detect_defects(m, expect_audio=False))

    def test_missing_lufs_key_is_not_treated_as_silence(self):
        m = self._base(has_audio=True, mean_volume_db=-30.0)
        audio_defects = [d for d in detect_defects(m, expect_audio=True) if "audio" in d]
        self.assertEqual(audio_defects, [])

    def test_loudnorm_minus_inf_means_silent(self):
        # loudnorm 对全静音音轨输出 -inf → 解析成 None，代表"有音轨但没声音"
        m = self._base(has_audio=True, mean_volume_db=-30.0, silence_ratio=0.0, lufs=None)
        self.assertIn("silent_audio", detect_defects(m, expect_audio=True))

    def test_low_lufs_alone_triggers_low_volume(self):
        # EBU R128 响度比 mean_volume 更贴近人耳：平均音量正常但响度偏低也要判
        m = self._base(has_audio=True, mean_volume_db=-20.0, silence_ratio=0.1, lufs=-50.0)
        self.assertIn("low_volume", detect_defects(m, expect_audio=True))


class ExtendedProbeIntegrationTest(unittest.TestCase):
    """真跑 ffmpeg 验证 measure_extended 能解析出指标（没装 ffmpeg 自动跳过）。

    这是"参数真的影响可测指标"的回归防线：模糊度必须随 gblur 增大而上升，
    否则判据就是摆设（闭环会拿着一个永远不变的数去修正）。
    """

    @classmethod
    def setUpClass(cls):
        if not probe.has_ffmpeg():
            raise unittest.SkipTest("没有 ffmpeg")
        cls.tmp = tempfile.mkdtemp(prefix="hxmv_test_")
        cls.clip = os.path.join(cls.tmp, "clip.mp4")
        rc, _ = probe._run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
             cls.clip], timeout=120)
        cls.built = rc == 0 and os.path.isfile(cls.clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        if not self.built:
            self.skipTest("测试视频生成失败（本机 ffmpeg 缺 libx264/aac？）")

    def test_measures_blur_loudness_and_cuts(self):
        ext = probe.measure_extended(self.clip, duration=1.0)
        self.assertIsNotNone(ext.get("blur_mean"))
        self.assertEqual(ext.get("scene_cuts"), 0)
        self.assertIsNotNone(ext.get("lufs"))

    def test_blurring_actually_raises_the_blur_metric(self):
        blurred = os.path.join(self.tmp, "blurred.mp4")
        rc, _ = probe._run(["ffmpeg", "-y", "-v", "error", "-i", self.clip,
                            "-vf", "gblur=sigma=6", blurred], timeout=120)
        if rc != 0:
            self.skipTest("gblur 重编码失败")
        clean = probe.measure_extended(self.clip, duration=1.0)
        blurry = probe.measure_extended(blurred, duration=1.0)
        self.assertGreater(blurry["blur_mean"], clean["blur_mean"])


# ------------------------------------------- 三层评审：并行与异常隔离（v0.8）
class PipelineCriticTest(unittest.TestCase):
    """守住三层评审的两个新保证：
    ① 并行执行但**返回顺序固定**（合并报告按顺序拼 detail，乱序会让人读不懂）；
    ② 单层异常不再拖垮整次评审（原来一次 VLM 抖动 = 整次生产作废）。
    """

    @staticmethod
    def _critic_with(layers):
        from hxmv.core.critic import PipelineCritic
        c = PipelineCritic()
        c.layers = layers
        return c

    @staticmethod
    def _layer(name, score, delay=0.0, boom=False):
        from hxmv.quality import QualityReport

        class _L:
            layer = name

            def evaluate(self, task, result):
                if boom:
                    raise RuntimeError("评审层挂了")
                if delay:
                    time.sleep(delay)
                return QualityReport(layer=name, score=score)

        return _L()

    def test_layers_run_in_parallel_but_stay_ordered(self):
        # 第一层故意最慢：并行时总耗时≈最慢那层，且返回顺序仍按声明顺序
        c = self._critic_with([self._layer("L1_PHYSICS", 0.9, delay=0.30),
                               self._layer("L2_VISUAL", 0.8, delay=0.30),
                               self._layer("L3_SEMANTIC", 0.7, delay=0.30)])
        t0 = time.time()
        out = c.evaluate_layers(Task("GENERATE_SHOT"), {})
        elapsed = time.time() - t0
        self.assertEqual([r.layer for r in out], ["L1_PHYSICS", "L2_VISUAL", "L3_SEMANTIC"])
        # 串行需要 0.9s；并行应远低于它（留足余量，避免慢 CI 上抖动误报）
        self.assertLess(elapsed, 0.75)

    def test_one_broken_layer_does_not_kill_the_review(self):
        c = self._critic_with([self._layer("L1_PHYSICS", 0.9),
                               self._layer("L2_VISUAL", 0.0, boom=True)])
        out = c.evaluate_layers(Task("GENERATE_SHOT"), {})
        self.assertEqual(len(out), 2)
        self.assertIn("未完成判定", out[1].detail)
        # 异常层不得静默消失：合并报告的 detail 里必须留下证据
        merged = c.evaluate(Task("GENERATE_SHOT"), {})
        self.assertIn("未完成判定", merged.detail)


# ------------------------------------------- 批次并行：结果必须与串行等价（v0.8）
class BatchExecutionTest(unittest.TestCase):
    """并行的唯一正当理由是"快"，**绝不能改变结论**。这个类守的就是这条线。

    注意为什么必须用**固定 task_id**：mock 世界的缺陷是按 task_id 哈希抽的，
    两次独立 run 的 task_id 不同 → 缺陷不同 → 根本没法对比。
    固定 id 之后，"逐个串行执行"与"并发批次"必须给出逐字段一致的结果。
    """

    @staticmethod
    def _tasks(n: int = 2):
        return [Task("GENERATE_SHOT", task_id=f"fixed{i:04d}",
                     input={"prompt": f"镜头{i}", "duration": 5, "seed": 100 + i},
                     constraints={"character": "cat", "scene": "s01",
                                  "reference_strength": 0.4})
                for i in range(n)]

    def test_parallel_matches_serial_one_by_one(self):
        from hxmv.core.executor import MockVideoExecutor
        from hxmv.core.loop import _execute_batch, _execute_one
        from hxmv.core.state import ExecutionState

        tasks = self._tasks(2)
        serial_state = ExecutionState(goal="g")
        serial = {t.task_id: _execute_one(MockVideoExecutor(), t, serial_state,
                                         lambda e: None, False, 4, threading.Lock())
                  for t in tasks}

        par_state = ExecutionState(goal="g")
        parallel = _execute_batch(MockVideoExecutor(), list(tasks), par_state,
                                  lambda e: None, False, 4, threading.Lock())

        self.assertEqual(set(serial), set(parallel))
        for key, (res_serial, _) in serial.items():
            res_par, _err = parallel[key]
            self.assertEqual(res_serial["defects"], res_par["defects"], f"{key} 缺陷集不一致")
            self.assertEqual(res_serial["media"], res_par["media"])
            self.assertEqual(res_serial["fingerprint"], res_par["fingerprint"])

    def test_budget_is_not_lost_under_concurrent_failures(self):
        """并发失败任务的计费一次都不能少。

        `state.budget.used += cost` 不是原子操作：没有锁时并发会丢增量，
        而预算护栏护的正是"真花钱"的地方（丢计费 = 护栏形同虚设）。
        """
        from hxmv.core.executor import Executor
        from hxmv.core.loop import _execute_batch
        from hxmv.core.state import ExecutionState
        from hxmv.providers.base import ProviderError

        class _AlwaysFailing(Executor):
            parallel_safe = True

            def execute(self, task):
                raise ProviderError("服务不可用", retryable=False, cost_units=1.0)

            def for_concurrent(self):
                return self

        state = ExecutionState(goal="g")
        out = _execute_batch(_AlwaysFailing(), self._tasks(8), state,
                             lambda e: None, False, 4, threading.Lock())
        self.assertEqual(len(out), 8)
        self.assertTrue(all(result is None for result, _ in out.values()))
        self.assertAlmostEqual(state.budget.used, 8.0, places=6)   # 8 次失败，一次不漏

    def test_project_file_survives_concurrent_registration(self):
        """并发登记镜头后，project.json 必须仍是合法 JSON 且**一条不丢**。

        无锁时两个线程同时 open(path, "w") 会把内容写成交错的坏 JSON，
        下次 load 直接判成空档案——整个项目的记忆一次性归零（最难查的那种事故）。
        """
        import hxmv.core.project as proj_mod
        from concurrent.futures import ThreadPoolExecutor as Pool

        old_dir = proj_mod.PROJECTS_DIR
        tmp = tempfile.mkdtemp(prefix="hxmv_proj_")
        proj_mod.PROJECTS_DIR = tmp
        try:
            project = proj_mod.Project("parallel")
            with Pool(max_workers=8) as pool:
                list(pool.map(
                    lambda i: project.register_shot(f"fp{i:04d}", f"/tmp/s{i}.mp4"),
                    range(40)))
            reloaded = proj_mod.Project.load("parallel")     # load 失败会返回空档案
            self.assertEqual(len(reloaded.shots), 40)
        finally:
            proj_mod.PROJECTS_DIR = old_dir
            shutil.rmtree(tmp, ignore_errors=True)

    def test_provider_needs_both_declaration_and_factory(self):
        """ProviderExecutor 的并行许可 = provider 声明 + 能重建实例，缺一不可。"""
        from hxmv.core.executor import ProviderExecutor

        class _Safe:
            parallel_safe = True

        class _Unsafe:
            pass

        self.assertFalse(ProviderExecutor(_Safe()).parallel_safe)          # 有声明、无工厂
        self.assertTrue(ProviderExecutor(_Safe(), factory=_Safe).parallel_safe)
        self.assertFalse(ProviderExecutor(_Unsafe(), factory=_Unsafe).parallel_safe)
        # 不能派生 → for_concurrent 明确返回 None（调用方据此退回串行）
        self.assertIsNone(ProviderExecutor(_Safe()).for_concurrent())


# ------------------------------------------- 跨 provider 共享路径的并发安全（v0.8）
class SharedPathConcurrencyTest(unittest.TestCase):
    """`base_<w>x<h>_<stem>.png` 这类基线帧，local_render 与 zhipu_video 会写**同名同路径**。

    各持一把锁等于没锁：两个 provider 的并发任务照样会同时渲染、同时写同一个文件
    （写出坏 PNG，下游表现成"参考图读不出来"，很难定位）。所以断言两件事：
    ① 两边用的是**同一把锁**；② 8 线程并发要同一张基线帧时，产物仍是有效图片。
    """

    def test_both_providers_share_the_same_lock(self):
        from hxmv.providers import base, local_render, zhipu_video
        self.assertIs(local_render._FILE_LOCK, base.SHARED_FILE_LOCK)
        self.assertIs(zhipu_video.SHARED_FILE_LOCK, base.SHARED_FILE_LOCK)

    def test_concurrent_local_render_shares_asset_and_baseline(self):
        """真跑 ffmpeg：4 个并发任务要同一张参考图与同一条基线帧。

        不共享锁时这里会出两种事故：同一路径被并发写（坏 PNG，探不到宽高），
        或者临时文件 `_basetmp_*` 被另一个线程删掉导致渲染失败。
        """
        from concurrent.futures import ThreadPoolExecutor
        from hxmv.providers.local_render import LocalRenderProvider

        if not probe.has_ffmpeg():
            self.skipTest("没有 ffmpeg")
        tmp = tempfile.mkdtemp(prefix="hxmv_par_")
        try:
            provider = LocalRenderProvider(outdir=tmp)      # project=None → 不走档案复用
            assets: list[str] = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: assets.append(provider._render_asset(None, "cat", "character")),
                              range(4)))
            self.assertEqual(len(set(assets)), 1, "同一 key 应产出同一个资产路径")
            info = probe.probe_container(assets[0])
            self.assertEqual((info.get("width"), info.get("height")), (640, 360), "资产图被并发写坏")

            baselines: list[str] = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: baselines.append(provider._baseline_frame(assets[0], 640, 360)),
                              range(4)))
            self.assertEqual(len(set(baselines)), 1, "同一参考+分辨率应复用同一条基线帧")
            base = probe.probe_container(baselines[0])
            self.assertEqual((base.get("width"), base.get("height")), (640, 360), "基线帧被并发写坏")
            # 临时文件不应残留（并发下最容易漏删或互相删）
            self.assertEqual([n for n in os.listdir(tmp) if n.startswith("_basetmp_")], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_encoded_frame_yields_valid_png(self):
        from concurrent.futures import ThreadPoolExecutor
        from hxmv.providers.zhipu_video import _encoded_frame

        if not probe.has_ffmpeg():
            self.skipTest("没有 ffmpeg")
        tmp = tempfile.mkdtemp(prefix="hxmv_lock_")
        try:
            ref = os.path.join(tmp, "ref.png")
            rc, _ = probe._run([probe.ffmpeg_path("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
                                "-i", "testsrc2=s=640x360:d=1", "-frames:v", "1", ref], timeout=60)
            if rc != 0 or not os.path.isfile(ref):
                self.skipTest("参考图生成失败")

            results: list[str | None] = []
            guard = threading.Lock()

            def work():
                got = _encoded_frame(ref, 320, 180, tmp)
                with guard:
                    results.append(got)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: work(), range(8)))

            paths = [p for p in results if p]
            self.assertEqual(len(paths), 8, "有线程拿不到基线帧")
            self.assertEqual(len(set(paths)), 1, "同一份输入应当复用同一个文件")
            # 关键断言：并发写坏的 PNG 在这里会暴露（探不到宽高）
            info = probe.probe_container(paths[0])
            self.assertTrue(info, "基线帧不是可读的有效图片（并发写坏了）")
            self.assertEqual((info.get("width"), info.get("height")), (320, 180))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------- 运镜（v0.9）
class CameraSpecTest(unittest.TestCase):
    """运镜规格：认词、收敛、剥词、投影。这一层错了，后面判据再准也是错的。"""

    def test_chinese_move_words_are_recognized(self):
        self.assertEqual(cam.normalize_move("镜头缓慢推近"), "push_in")
        self.assertEqual(cam.normalize_move("向左摇"), "pan_left")
        self.assertEqual(cam.normalize_move("环绕主角一圈"), "orbit")
        self.assertEqual(cam.normalize_move("机位固定"), "static")
        self.assertEqual(cam.normalize_move("dolly out"), "pull_out")

    def test_directionless_move_is_not_guessed(self):
        # 只写"摇"没写方向 → 不许猜（判据要方向，猜错等于判错）
        self.assertIsNone(cam.parse("镜头缓慢摇过全场"))

    def test_speed_is_parsed(self):
        self.assertEqual(cam.parse("镜头缓缓推近")["speed"], "slow")
        self.assertEqual(cam.parse("镜头快速推近")["speed"], "fast")
        self.assertEqual(cam.parse("镜头推近")["speed"], "normal")

    def test_strip_only_when_a_move_was_found(self):
        # 认出过运镜才剥词：不然会误删正文里的"镜头"（"镜头里只有一只鸟"）
        self.assertNotIn("缓慢", cam.strip("镜头缓慢推近，猴子站在崖顶"))
        self.assertEqual(cam.strip("镜头里只有一只鸟在飞"), "镜头里只有一只鸟在飞")

    def test_phrase_is_english_camera_language(self):
        self.assertIn("pushes in", cam.phrase("push_in", "slow"))
        self.assertIn("pulls back", cam.phrase("pull_out"))
        self.assertIn("locked off", cam.phrase("static"))

    def test_native_params_map_to_kling_axes(self):
        cfg = cam.native_params("pan_right", "slow", 1.0)["config"]
        self.assertGreater(cfg["pan"], 0)          # 右摇 → pan 正
        self.assertEqual(cfg["zoom"], 0.0)
        self.assertLess(cam.native_params("pull_out", "normal", 1.0)["config"]["zoom"], 0)

    def test_capability_pick_prefers_hard_levers(self):
        class P:
            camera_support = {"prompt", "render"}
        self.assertEqual(cam.pick_strategy(P()), "render")
        class Q:
            camera_support = {"prompt", "first_last"}
        self.assertEqual(cam.pick_strategy(Q(), prefer_first_last=True), "first_last")
        # 没声明 = 最弱的 prompt（不许吹能力）
        self.assertEqual(cam.pick_strategy(object()), "prompt")


class CameraCriticTest(unittest.TestCase):
    """运镜判据：方向要对、幅度要够；没规格/固定机位不查。"""

    def test_expected_direction_is_enforced(self):
        self.assertEqual(probe.camera_defects({"zoom": 1.2, "pan_ratio": 0.0, "tilt_ratio": 0.0},
                                              "push_in"), [])
        self.assertEqual(probe.camera_defects({"zoom": 0.95, "pan_ratio": 0.0, "tilt_ratio": 0.0},
                                              "push_in"), ["camera_mismatch"])
        self.assertEqual(probe.camera_defects({"zoom": 1.0, "pan_ratio": 0.12, "tilt_ratio": 0.0},
                                              "pan_right"), [])
        self.assertEqual(probe.camera_defects({"zoom": 1.0, "pan_ratio": 0.12, "tilt_ratio": 0.0},
                                              "pan_left"), ["camera_mismatch"])

    def test_static_and_specless_are_not_judged(self):
        moved = {"zoom": 1.4, "pan_ratio": 0.3, "tilt_ratio": 0.2}
        self.assertEqual(probe.camera_defects(moved, "static"), [])
        self.assertEqual(probe.camera_defects(moved, None), [])
        self.assertEqual(probe.camera_defects(None, "push_in"), [])

    def test_approximate_moves_only_require_real_motion(self):
        self.assertEqual(probe.camera_defects({"zoom": 1.0, "pan_ratio": 0.0, "tilt_ratio": 0.0},
                                              "orbit"), ["camera_mismatch"])
        self.assertEqual(probe.camera_defects({"zoom": 1.03, "pan_ratio": 0.0, "tilt_ratio": 0.0},
                                              "orbit"), [])

    def test_new_defect_keys_are_wired_everywhere(self):
        """新增缺陷键要同时落五处（probe / critic / refiner 两张表 + 人类提示）。

        漏任何一处 = 判得出来却修不动、或者排到最后白等（技能里写过的规矩）。
        """
        for key in ("camera_mismatch", "seam_jump"):
            with self.subTest(key=key):
                self.assertIn(key, probe.DEFECT_KEYS)
                self.assertIn(key, DEFECT_FIXES)
                self.assertIn(DEFECT_FIXES[key][0], _ADJUST)
                self.assertIn(key, refiner_mod._FIX_PRIORITY)


# ---------------------------------------------------------------- 分镜（编剧 / 剧本）
class StoryboardTest(unittest.TestCase):
    def test_normalize_keeps_cameras_and_drops_moves_from_prompt(self):
        plan = story.normalize({"title": "测试", "shots": [
            {"prompt": "镜头缓慢推近，柯基在雪地奔跑", "camera": "push_in", "duration": 5,
             "cast": ["柯基"], "scene": "s1"},
            {"prompt": "全景雪原", "camera": "未知运镜", "duration": 5, "cast": []},
        ]}, "柯基追蝴蝶")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["shots"][0]["camera"], "push_in")
        self.assertNotIn("推近", plan["shots"][0]["prompt"])     # 运镜归 camera，不进画面描述
        self.assertEqual(plan["shots"][1]["camera"], "static")   # 认不出的运镜落回固定
        self.assertEqual(plan["shots"][1]["cast"], [])

    def test_too_few_shots_is_rejected(self):
        self.assertIsNone(story.normalize({"shots": [{"prompt": "一个镜头"}]}, "目标"))

    def test_fallback_is_deterministic_and_labelled(self):
        a, b = story.fallback("雪地柯基"), story.fallback("雪地柯基")
        self.assertEqual(a, b)
        self.assertEqual(a["written_by"], "fallback")
        self.assertGreaterEqual(len(a["shots"]), 2)

    def test_from_task_uses_user_plan_without_calling_llm(self):
        task = Task("STORYBOARD", input={"plan": {"shots": [{"prompt": "x"}], "written_by": "user"}})
        self.assertEqual(story.from_task(task, None)["written_by"], "user")


class PlannerCameraTest(unittest.TestCase):
    """分镜 → 任务：运镜/场次/角色/空镜都要真的落到任务约束上。"""

    PLAN = {"title": "雪地柯基", "written_by": "llm", "total_duration": 10.0,
            "scenes": [{"key": "s1", "name": "雪原"}, {"key": "s2", "name": "小径"}],
            "shots": [{"n": 1, "prompt": "柯基在雪原奔跑", "duration": 5, "camera": "push_in",
                       "speed": "slow", "cast": ["柯基"], "scene": "s1"},
                      {"n": 2, "prompt": "蝴蝶停在鼻尖", "duration": 5, "camera": "static",
                       "speed": "normal", "cast": [], "scene": "s2"}]}

    def test_shots_carry_camera_and_scene(self):
        state = ExecutionState(goal="雪地柯基")
        tasks = planner_mod.tasks_from_plan(self.PLAN, state, None, include_storyboard=False)
        shots = [t for t in tasks if t.action == "GENERATE_SHOT"]
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].constraints["camera"], "push_in")
        self.assertEqual(shots[0].constraints["camera_speed"], "slow")
        self.assertEqual(shots[0].constraints["scene"], "s1")
        self.assertEqual(shots[1].constraints["scene"], "s2")
        self.assertEqual(shots[1].constraints["cast"], "none")   # 空镜：不查角色
        scenes = {t.constraints.get("scene_key") for t in tasks if t.action == "GENERATE_SCENE"}
        self.assertEqual(scenes, {"s1", "s2"})                   # 两场戏各出一张场景

    def test_compose_knows_shot_scenes(self):
        tasks = planner_mod.tasks_from_plan(self.PLAN, ExecutionState(goal="x"), None,
                                       include_storyboard=False)
        compose = next(t for t in tasks if t.action == "COMPOSE")
        self.assertEqual(compose.input["scenes"], ["s1", "s2"])

    def test_include_storyboard_adds_exactly_one(self):
        tasks = planner_mod.tasks_from_plan(self.PLAN, ExecutionState(goal="x"), None)
        self.assertEqual(len([t for t in tasks if t.action == "STORYBOARD"]), 1)

    def test_story_planner_two_phases(self):
        state = ExecutionState(goal="雪地柯基")
        sp = planner_mod.StoryPlanner("雪地柯基")
        first = sp.next_task(state)                     # 第一相：先写分镜
        self.assertEqual(first.action, "STORYBOARD")
        first.result = {"storyboard": self.PLAN}
        first.status = TaskStatus.PASS
        rest = []
        while (t := sp.next_task(state)) is not None:
            rest.append(t)
        self.assertTrue(any(t.action == "GENERATE_SHOT" for t in rest))
        self.assertFalse(any(t.action == "STORYBOARD" for t in rest))   # 不重复跑分镜

    def test_story_planner_hands_over_when_storyboard_fails(self):
        class Fallback:
            used = False
            def next_task(self, state):
                self.used = True
                return Task("COMPOSE")
        fb = Fallback()
        sp = planner_mod.StoryPlanner("目标", fallback=fb)
        state = ExecutionState(goal="目标")
        sb = sp.next_task(state)
        sb.status = TaskStatus.FAIL
        self.assertEqual(sp.next_task(state).action, "COMPOSE")
        self.assertTrue(fb.used)


# ---------------------------------------------------------------- 产物下载头
class ContentDispositionTest(unittest.TestCase):
    """中文文件名必须走 RFC 5987：http.server 的头部只能 latin-1。

    踩过的坑（真机）：`filename="柯基.png"` 会在 send_header 里抛 UnicodeEncodeError，
    产物在面板里直接打不开（项目名/角色键是中文时必然触发）。
    """

    def test_chinese_name_is_percent_encoded(self):
        from hxmv.server import content_disposition
        header = content_disposition("asset_柯基.png")
        self.assertIn("filename*=UTF-8''", header)
        header.encode("latin-1")          # 关键：这个头必须能进 HTTP 响应

    def test_ascii_name_keeps_a_readable_fallback(self):
        from hxmv.server import content_disposition
        header = content_disposition("final_ab12.mp4")
        self.assertIn('filename="final_ab12.mp4"', header)
        header.encode("latin-1")

    def test_weird_chars_do_not_break_the_header(self):
        from hxmv.server import content_disposition
        for name in ("我 的 片.mp4", "a\"b.mp4", "../x.mp4", ""):
            with self.subTest(name=name):
                header = content_disposition(name)
                header.encode("latin-1")
                self.assertIn("filename=", header)
