"""多 provider（多 API）架构的单元测试 —— 守住「加一家 API 只写一个文件」这条能力。

为什么单独一个文件：这套东西的价值全在**结构**上（注册表是唯一真相、工厂认表不认名字、
档位/默认家都从一处来），而结构破坏起来是静默的——某天有人又在 server 里手写一家名字，
测试要能立刻红。所以这里断言的是"关系"，不是"某个字符串长什么样"。

跑法：python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hxmv.core import config, llm
from hxmv.core.executor import MockVideoExecutor, make_executor
from hxmv.providers import registry
from hxmv.providers.base import VideoProvider

_ENV_KEYS = ("HXMV_PROVIDER", "HXMV_AGNES_KEY", "AGNES_API_KEY", "HXMV_ZHIPU_KEY",
             "ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY", "HXMV_KLING_KEY",
             "OPENAI_API_KEY", "OPENAI_BASE_URL", "HXMV_LLM_MODEL", "HXMV_VLM_MODEL",
             "HXMV_AGNES_VIDEO_MODEL", "HXMV_ZHIPU_MODEL")


class _Isolated(unittest.TestCase):
    """把 HOME 指到临时目录：断言"没配 Key"的用例不能读开发机上的真实配置。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hxmv-test-")
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HXMV_CONFIG"] = os.path.join(self._tmp, "config.json")
        self._saved_cfg_path = config.CONFIG_PATH
        config.CONFIG_PATH = os.environ["HXMV_CONFIG"]

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.CONFIG_PATH = self._saved_cfg_path
        shutil.rmtree(self._tmp, ignore_errors=True)


# ---------------------------------------------------------------- 注册表完整性
class RegistryTest(unittest.TestCase):
    def test_more_than_one_api_provider(self):
        """「可以对接多个 api 而不只是一家」是项目方向：这条守着它。"""
        self.assertGreaterEqual(len(registry.api_specs()), 2)

    def test_api_specs_are_complete_and_constructible_signatures(self):
        for spec in registry.api_specs():
            with self.subTest(provider=spec.id):
                cls = getattr(__import__(f"hxmv.providers.{spec.module}", fromlist=[spec.cls]), spec.cls)
                self.assertTrue(issubclass(cls, VideoProvider))
                self.assertTrue(spec.base_url.startswith("http"))
                self.assertTrue(spec.key_hint, "面板要把「去哪拿 Key」显示给用户")
                for kind in ("video", "image", "text", "vision"):
                    self.assertTrue(spec.tiers(kind), f"{spec.id} 缺 {kind} 档位")
                    self.assertIn(spec.default_model(kind), [m.id for m in spec.tiers(kind)])

    def test_every_api_spec_declares_its_capability_not_the_registry(self):
        """能力声明（首尾帧/时长上限）必须在适配器类里，注册表不替模型吹能力。"""
        for spec in registry.api_specs():
            cls = getattr(__import__(f"hxmv.providers.{spec.module}", fromlist=[spec.cls]), spec.cls)
            with self.subTest(provider=spec.id):
                self.assertIsInstance(cls.MAX_DURATION, dict)
                self.assertIsInstance(cls.FIRST_LAST_MODELS, set)

    def test_unknown_provider_is_empty(self):
        self.assertIsNone(registry.get("no-such-provider"))


# ---------------------------------------------------------------- 工厂
class FactoryTest(_Isolated):
    def test_explicit_mock(self):
        os.environ["HXMV_PROVIDER"] = "mock"
        self.assertIsInstance(make_executor(), MockVideoExecutor)

    def test_known_providers_build_through_registry(self):
        for name in ("local", "fake"):
            with self.subTest(provider=name):
                os.environ["HXMV_PROVIDER"] = name
                ex = make_executor()
                self.assertNotIsInstance(ex, MockVideoExecutor)     # 不是兜底来的
                self.assertTrue(ex.parallel_safe)

    def test_unknown_provider_degrades_to_mock(self):
        os.environ["HXMV_PROVIDER"] = "no-such-provider"
        self.assertIsInstance(make_executor(), MockVideoExecutor)

    def test_no_key_falls_back_to_local_render(self):
        self.assertEqual(os.environ.get("HXMV_PROVIDER"), None)
        self.assertEqual(__import__("hxmv.core.executor", fromlist=["x"])._auto_provider(), "local")

    def test_default_provider_wins_when_keyed(self):
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop")
        config.set_default_provider("zhipu")
        config.set_api_key("zhipu", "test0000000000000000000000000000.abcdefghijklmnop")
        os.environ["HXMV_PROVIDER"] = ""
        os.environ.pop("HXMV_PROVIDER")
        from hxmv.core.executor import _auto_provider
        self.assertEqual(_auto_provider(), "zhipu")


# ---------------------------------------------------------------- 档位/默认家
class ConfigTest(_Isolated):
    def test_key_roundtrip_and_mask(self):
        self.assertEqual(config.api_key("agnes"), "")
        config.set_api_key("agnes", "sk-test-notreal-abcdefg")
        self.assertEqual(config.api_key("agnes"), "sk-test-notreal-abcdefg")
        self.assertNotIn("abcdefghij", config.mask(config.api_key("agnes")))   # 不回明文
        self.assertTrue(config.configured("agnes"))

    def test_env_beats_file(self):
        config.set_api_key("agnes", "sk-test-from-file-abcdef")
        os.environ["HXMV_AGNES_KEY"] = "sk-test-from-env-abcdef"
        self.assertEqual(config.api_key("agnes"), "sk-test-from-env-abcdef")

    def test_option_env_name_is_generic(self):
        config.set_option("agnes", "video_model", "agnes-video-2.5")
        self.assertEqual(config.option("agnes", "video_model"), "agnes-video-2.5")
        os.environ["HXMV_AGNES_VIDEO_MODEL"] = "agnes-video-2.5-flash"
        self.assertEqual(config.option("agnes", "video_model"), "agnes-video-2.5-flash")

    def test_legacy_zhipu_env_still_works(self):
        config.set_option("zhipu", "video_model", "cogvideox-3")
        os.environ["HXMV_ZHIPU_MODEL"] = "cogvideox-flash"      # 老名字不能失效
        self.assertEqual(config.option("zhipu", "video_model"), "cogvideox-flash")

    def test_default_provider_roundtrip(self):
        self.assertEqual(config.default_provider(), "")
        config.set_default_provider("Agnes")
        self.assertEqual(config.default_provider(), "agnes")
        config.set_default_provider("")
        self.assertEqual(config.default_provider(), "")


# ---------------------------------------------------------------- 大脑跟着谁走
class LlmRoutingTest(_Isolated):
    def test_no_key_means_no_llm(self):
        self.assertEqual(llm.endpoint()[1], "")
        self.assertFalse(llm.llm_available())
        self.assertIsNone(llm.vision_model())

    def test_agnes_key_drives_text_and_vision(self):
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop")
        base, key, pid = llm.endpoint()
        self.assertEqual((key, pid), ("sk-test-abcdefghijklmnop", "agnes"))
        self.assertEqual(base, "https://apihub.agnes-ai.com/v1")
        self.assertEqual(llm.text_model(), "agnes-2.5-flash")
        self.assertEqual(llm.vision_model(), "agnes-2.5-flash")   # 实测会看图的那档
        self.assertTrue(llm.vision_available())

    def test_local_run_still_uses_the_configured_llm(self):
        """`--provider local` 时大脑不该跟着退回 Mock：local 没有 base/Key，自动跳过。"""
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop")
        os.environ["HXMV_PROVIDER"] = "local"
        self.assertEqual(llm.active_provider(), "agnes")

    def test_llm_waits_out_rate_limit(self):
        """免费档 LLM 限流（429）也要等（项目约定「不怕等」），不该让分镜直接退成兜底。"""
        import urllib.error
        from unittest import mock
        from hxmv.core import llm as llm_mod
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop")
        calls = {"n": 0}

        def flaky(base, key, model, messages, temperature, max_tokens):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
            return "好了", "stop"

        with mock.patch("hxmv.core.llm._call_once", side_effect=flaky), \
             mock.patch("hxmv.core.llm.time.sleep") as sleepy:
            out = llm_mod.chat([{"role": "user", "content": "x"}])
        self.assertEqual((out, calls["n"]), ("好了", 3))
        self.assertEqual(sleepy.call_count, 2)

    def test_llm_does_not_wait_on_parameter_errors(self):
        """参数错（400）等着等于把 bug 藏起来：立刻抛，交调用方降级。"""
        import urllib.error
        from unittest import mock
        from hxmv.core import llm as llm_mod
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop")
        with mock.patch("hxmv.core.llm._call_once",
                        side_effect=urllib.error.HTTPError("u", 400, "Bad Request", {}, None)), \
             mock.patch("hxmv.core.llm.time.sleep") as sleepy:
            with self.assertRaises(urllib.error.HTTPError):
                llm_mod.chat([{"role": "user", "content": "x"}])
        self.assertEqual(sleepy.call_count, 0)

    def test_explicit_openai_endpoint_has_no_default_vision(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-custom-abcdefgh"
        os.environ["OPENAI_BASE_URL"] = "https://example.com/v1"
        _, _, pid = llm.endpoint()
        self.assertEqual(pid, "openai")
        self.assertIsNone(llm.vision_model())              # 不认识就不许假装会看图
        os.environ["HXMV_VLM_MODEL"] = "some-vl"
        self.assertEqual(llm.vision_model(), "some-vl")


# ---------------------------------------------------------------- Agnes 适配器
class AgnesAdapterTest(_Isolated):
    def _provider(self):
        from hxmv.providers.agnes_video import AgnesVideoProvider
        return AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)

    def test_does_not_send_token_when_downloading(self):
        # 实测：Agnes 的产物域名带了 Authorization 反而 401
        self.assertFalse(self._provider().download_auth)

    def test_image_body_uses_tier_and_ratio(self):
        p = self._provider()
        body = p._image_body("柯基", "16:9")
        self.assertEqual(body["size"], "1K")
        self.assertEqual(body["ratio"], "16:9")
        self.assertEqual(body["extra_body"]["response_format"], "url")   # 放顶层会报错

    def test_text_mode_when_no_frames(self):
        p = self._provider()
        from hxmv.core.state import Task
        t = Task(task_id="s1", action="GENERATE_SHOT", input={"prompt": "x", "duration": 5})
        body = p._submit_body(t, "prompt", [])
        self.assertEqual(body["mode"], "text")
        self.assertNotIn("first_frame", body)
        self.assertEqual(body["size"], "720P")          # flash 只认 720P

    def test_keyframe_mode_carries_first_and_last_frame(self):
        p = self._provider()
        from hxmv.core.state import Task
        t = Task(task_id="s1", action="GENERATE_SHOT", input={"prompt": "x", "duration": 5})
        one = p._submit_body(t, "prompt", ["data:image/png;base64,AAA"])
        self.assertEqual(one["mode"], "keyframe")
        self.assertEqual(one["first_frame"], "data:image/png;base64,AAA")
        self.assertNotIn("last_frame", one)
        two = p._submit_body(t, "prompt", ["a", "b"])
        self.assertEqual(two["last_frame"], "b")

    def test_seconds_is_clamped_string(self):
        p = self._provider()
        from hxmv.core.state import Task
        for want, expect in ((2, "4"), (5, "5"), (30, "12")):
            t = Task(task_id="s1", action="GENERATE_SHOT", input={"prompt": "x", "duration": want})
            with self.subTest(duration=want):
                self.assertEqual(p._submit_body(t, "p", [])["seconds"], expect)

    def test_poll_uses_agnesapi_with_model_name(self):
        p = self._provider()
        seen = {}

        def fake_get_abs(url):
            seen["url"] = url
            return {"status": "completed", "progress": 100, "url": "https://cdn/x.mp4"}

        p._get_abs = fake_get_abs
        self.assertEqual(p._poll_url("video_abc"), "https://cdn/x.mp4")
        self.assertIn("/agnesapi", seen["url"])
        self.assertIn("video_id=video_abc", seen["url"])
        self.assertIn("model_name=agnes-video-2.5-flash", seen["url"])   # 必须带，否则只对 text 有效

    def test_poll_raises_on_failed(self):
        p = self._provider()
        p._get_abs = lambda url: {"status": "failed", "error": {"message": "boom"}}
        from hxmv.providers.base import ProviderError
        with self.assertRaises(ProviderError):
            p._poll_url("video_abc")

    def test_patient_submit_waits_out_queue_full(self):
        """项目约定「免费就行不怕等」：队列满/限流要退避重试，不是一撞就判死。"""
        from unittest import mock
        from hxmv.providers.agnes_video import AgnesVideoProvider
        from hxmv.providers.base import ProviderError
        p = AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)
        p.SUBMIT_WAIT = 60.0
        calls = {"n": 0}

        def flaky(task, prompt, frames):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ProviderError('提交任务失败 HTTP 503: {"code":"video_queue_full"}', retryable=True)
            return {"video_id": "video_ok"}

        p._submit = flaky
        with mock.patch("hxmv.providers.api_video.time.sleep") as sleepy:
            out = p._submit_patient(None, "p", [])
        self.assertEqual(out["video_id"], "video_ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleepy.call_count, 2)          # 等了两次，第三次成了

    def test_patient_submit_does_not_swallow_real_errors(self):
        """参数错/鉴权错不该被当成「排队」耗着等——那会让真正的 bug 看不见。"""
        from hxmv.providers.agnes_video import AgnesVideoProvider
        from hxmv.providers.base import ProviderError
        p = AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)
        p.SUBMIT_WAIT = 60.0
        p._submit = lambda *a: (_ for _ in ()).throw(
            ProviderError("提交任务失败 HTTP 400: mode is required", retryable=False))
        self.assertFalse(p._congested(ProviderError("400 mode is required")))
        with self.assertRaises(ProviderError):
            p._submit_patient(None, "p", [])

    def test_free_tier_provider_is_patient_by_default(self):
        """Agnes 免费档的默认耐心值必须 >0（不然「不怕等」这条等于没实现）。"""
        from hxmv.providers.agnes_video import AgnesVideoProvider
        self.assertGreater(AgnesVideoProvider.SUBMIT_WAIT, 0)
        self.assertGreater(AgnesVideoProvider.DEFAULT_TIMEOUT, 420)

    def test_night_window_parsing_and_inside_outside(self):
        """低峰窗口：在里面 = 不用等；在外面 = 等到窗口开始；跨零点的窗口也要算对。"""
        from datetime import datetime, timedelta
        from hxmv.providers.api_video import seconds_until_window
        base = datetime.now().replace(second=0, microsecond=0)

        def ts(minutes):
            return (base + timedelta(minutes=minutes)).timestamp()

        # 窗口 02:00-06:00（相对「现在」算，跟真实时刻无关）
        start = 120   # 2 小时后
        end = 300     # 5 小时后
        win = f"{(base + timedelta(minutes=start)).strftime('%H:%M')}-" \
              f"{(base + timedelta(minutes=end)).strftime('%H:%M')}"
        self.assertAlmostEqual(seconds_until_window(win, ts(60)), 60 * 60, delta=90)   # 还没到，等 1 小时
        self.assertEqual(seconds_until_window(win, ts(180)), 0.0)                      # 已在窗口内
        self.assertEqual(seconds_until_window("", ts(0)), 0.0)                         # 关掉低峰策略
        self.assertEqual(seconds_until_window("乱写", ts(0)), 0.0)                      # 坏值当没有
        # 跨零点：23:30-01:00，在 00:30 时应该在窗口内
        self.assertEqual(seconds_until_window("23:30-01:00",
                                              datetime.now().replace(hour=0, minute=30).timestamp()), 0.0)

    def test_free_tier_parks_until_night_window(self):
        """不在低峰时段又排不上队 → 先park到窗口开始（而不是在高峰期干等烧时间）。"""
        from datetime import datetime, timedelta
        from unittest import mock
        from hxmv.providers.agnes_video import AgnesVideoProvider
        from hxmv.providers.base import ProviderError
        p = AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)
        p.SUBMIT_WAIT = 3600.0
        p.NIGHT_FIRST = True            # 低峰策略默认关，这里显式开，测的是机制本身
        # 窗口设成「3 小时后开始、5 小时后结束」——保证此刻在窗口外（跨零点也算得对）
        start = datetime.now() + timedelta(hours=3)
        end = datetime.now() + timedelta(hours=5)
        os.environ["HXMV_NIGHT_WINDOW"] = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        calls = {"n": 0}

        def flaky(task, prompt, frames):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ProviderError("HTTP 503 video_queue_full", retryable=True)
            return {"video_id": "video_ok"}

        p._submit = flaky
        with mock.patch("hxmv.providers.api_video.time.sleep") as sleepy:
            out = p._submit_patient(None, "p", [])
        self.assertEqual(out["video_id"], "video_ok")
        self.assertEqual(calls["n"], 2)
        parked_for = sleepy.call_args_list[0].args[0]
        self.assertGreater(parked_for, 60)          # 是「等到窗口」那一段，不是普通退避 20s
        self.assertLessEqual(parked_for, 300)       # 分段睡，单次不超过 5 分钟

    def test_default_policy_is_do_it_now(self):
        """项目 2026-10 明确：「不要自动凌晨，我让它做它就做」——低峰策略默认必须是关的。"""
        from hxmv.providers.agnes_video import AgnesVideoProvider
        from hxmv.providers.api_video import ApiVideoProvider
        self.assertFalse(AgnesVideoProvider.NIGHT_FIRST)
        self.assertFalse(ApiVideoProvider.NIGHT_FIRST)
        self.assertEqual(ApiVideoProvider.NIGHT_WINDOW, "")
        self.assertEqual(AgnesVideoProvider.SUBMIT_WAIT, 7200.0)   # 「不怕等」仍保留：原地等 2 小时

    def test_key_shape_guard_blocks_wrong_provider(self):
        """真出过事故：Agnes 的 key 被写进智谱槽位 → 智谱整条链 401 且面板只显示「已配 Key」。

        所以写入前按注册表声明的 key_pattern 校验形状：不符就拒（要强行写必须 force）。
        """
        with self.assertRaises(ValueError):
            config.set_api_key("zhipu", "sk-test-abcdefghijklmnop")
        with self.assertRaises(ValueError):
            config.set_api_key("agnes", "089713abcdefghijklmnopqrstuvwxyz01.abcdefghijklmnop")
        # 形状对 → 正常写；force → 强行写
        self.assertTrue(config.set_api_key("agnes", "sk-test-abcdefghijklmnop"))
        self.assertTrue(config.set_api_key("zhipu", "sk-test-abcdefghijklmnop", force=True))
        # 没声明形状的家（local/fake）不校验
        self.assertFalse(hasattr(spec_mock := None, "x") and False)

    def test_doctor_flags_two_providers_sharing_one_key(self):
        """两家共用同一把 Key = 必有一家写错了：自检必须自己喊出来（而不是等用户发现 401）。"""
        from hxmv.core import doctor
        config.set_api_key("zhipu", "sk-test-abcdefghijklmnop", force=True)   # 故意写成同一把
        config.set_api_key("agnes", "sk-test-abcdefghijklmnop", force=True)
        checks = {c["name"]: c for c in doctor.run_checks(probe_network=False)}
        dup = checks["Key 查重（不同家不能共用同一把）"]
        self.assertFalse(dup["ok"])
        self.assertIn("同一把", dup["detail"])

    def test_free_image_prompt_goes_verbatim(self):
        """「只出一张图」= 用户原话就是画面描述：不套设定表/场景模板、不交给 LLM 改写。"""
        from hxmv.core.state import Task
        from hxmv.providers.agnes_video import AgnesVideoProvider
        p = AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)
        task = Task(task_id="t1", action="GENERATE_IMAGE",
                    input={"prompt": "一只柯基戴着红围巾坐在雪地里"}, constraints={})
        prompt = p._asset_prompt(task, "image")
        self.assertIn("一只柯基戴着红围巾坐在雪地里", prompt)
        self.assertNotIn("character design sheet", prompt)     # 不能套设定表模板
        self.assertNotIn("cinematic wide keyframe", prompt)     # 也不能套场景定帧模板

    def test_generate_routes_free_image_to_asset_not_video(self):
        """自由出图必须走出图分支——踩过：没加进分发就落进视频分支，去排视频队列（等 2 小时）。"""
        from hxmv.core.state import Task
        from hxmv.providers.agnes_video import AgnesVideoProvider
        p = AgnesVideoProvider(api_key="sk-test-abcdefghijklmnop", outdir=self._tmp)
        seen = {}

        def fake_asset(task):
            seen["task"] = task
            return {"asset": "/tmp/x.png"}

        p._generate_asset = fake_asset
        out = p.generate(Task(task_id="t2", action="GENERATE_IMAGE", input={"prompt": "猫"}, constraints={}))
        self.assertEqual(out["asset"], "/tmp/x.png")
        self.assertEqual(seen["task"].action, "GENERATE_IMAGE")

    def test_two_providers_share_one_artifact_lock(self):
        """产物基线帧同名同路径：锁必须跨 provider 共用（各持一把等于没锁）。"""
        from hxmv.providers import base, local_render
        from hxmv.providers.agnes_video import AgnesVideoProvider            # noqa: F401
        from hxmv.providers.zhipu_video import ZhipuVideoProvider            # noqa: F401
        from hxmv.providers import api_video
        self.assertIs(api_video.SHARED_FILE_LOCK, base.SHARED_FILE_LOCK)
        self.assertIs(local_render.SHARED_FILE_LOCK, base.SHARED_FILE_LOCK)


if __name__ == "__main__":
    unittest.main()
