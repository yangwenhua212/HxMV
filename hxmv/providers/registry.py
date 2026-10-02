"""provider 注册表 —— **多 API 的单一真相**。

加一家 API = 写一个适配器文件 + 这里加一条 SPEC。工厂、面板设置页、CLI、
doctor、健康检查全从这张表读——别在别处再写 provider 名字
（老代码把 "zhipu" 散在 8 个文件里，加一家要改 8 处，这就是这条表存在的理由）。

分工：
- **注册表**管「有哪些家、怎么建、怎么配、有哪些档位」（会变的是配置）；
- **适配器类**管「怎么跟这家说话」和能力声明（`camera_support` / `max_duration` 是
  模型自己的事实，注册表不猜）。

一条 SPEC 的字段都从「面板要显示什么」倒推：中文名、Key 从哪拿、可选档位、
当前单价——用户看到的就是这些。
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Model:
    """一个可选档位（面板下拉里的一项）。cost 是中文口径，直接显示给用户。"""

    id: str
    cost: str = ""          # 例："免费" / "1.05 元/次" / "0.025 美元/秒"；空 = 不显示


@dataclasses.dataclass(frozen=True)
class Spec:
    id: str
    label: str                                  # 中文名（面板/CLI/日志都用它）
    module: str = ""                            # 适配器模块名（相对 hxmv.providers）
    cls: str = ""                               # 适配器类名；空 = 不走真 API（local/fake）
    env_keys: tuple[str, ...] = ()              # 凭据环境变量（优先于配置文件）；空 = HXMV_<ID>_KEY
    base_url: str = ""                          # OpenAI 兼容 base（LLM/视觉/生成共用同一家）
    env_base: str = ""                          # base 的环境变量覆盖名（测试指仿真端点用）
    key_hint: str = ""                          # 去哪拿 Key（面板/doctor 显示，别写文档链接）
    key_pattern: str = ""                       # 那家 Key 的形状（正则）。守一道门：别把 A 家的 key 写进 B 家
                                                # （真出过事故：智谱槽位被写成了 Agnes 的 key → 智谱整条链 401）
    needs_key: bool = True
    stub: bool = False                          # 适配器还没写完（只有骨架）：不进面板/自检，别让用户选了它
    models: dict[str, tuple[Model, ...]] = dataclasses.field(default_factory=dict)
    default: dict[str, str] = dataclasses.field(default_factory=dict)

    # ---------- 构造 ----------
    def create(self, **kw):
        """按注册表建实例（工厂与并发副本共用这一个入口）。"""
        from importlib import import_module
        if not (self.module and self.cls):
            raise ValueError(f"{self.id} 没有适配器类，不能直接构造")
        return getattr(import_module(f"hxmv.providers.{self.module}"), self.cls)(**kw)

    def tiers(self, kind: str) -> tuple[Model, ...]:
        return tuple(self.models.get(kind, ()))

    def default_model(self, kind: str) -> str:
        return self.default.get(kind) or (self.tiers(kind)[0].id if self.tiers(kind) else "")

    @property
    def api(self) -> bool:
        """是不是「真 API 生成服务」（要 Key、有 base、有适配器）。"""
        return bool(self.module and self.cls and self.needs_key)

    # 面板设置页暴露的档位：加一档就在这加一行（面板按数据渲染，不用改前端）
    TIERS = (("video_model", "视频档位", "video"),
             ("image_model", "图像档位", "image"),
             ("vision_model", "视觉评审", "vision"))

    def panel(self) -> dict:
        """这家在面板设置页要显示什么（Key 状态由调用方填，明文永不回传）。"""
        return {
            "id": self.id, "name": self.label, "key_hint": self.key_hint,
            "needs_key": self.needs_key,
            "options": [{"name": n, "label": lb, "default": self.default_model(kind),
                         "choices": [{"id": m.id, "cost": m.cost} for m in self.tiers(kind)]}
                        for n, lb, kind in self.TIERS if self.tiers(kind)],
        }


_ZH = Spec(
    id="zhipu", label="智谱 AI",
    module="zhipu_video", cls="ZhipuVideoProvider",
    env_keys=("HXMV_ZHIPU_KEY", "ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY"),
    base_url="https://open.bigmodel.cn/api/paas/v4",
    env_base="HXMV_ZHIPU_BASE",
    key_hint="bigmodel.cn → 控制台 → API Keys（免费申请）",
    key_pattern=r"^[0-9a-zA-Z]{32}\.[0-9a-zA-Z]{16}$",   # 智谱是「id.secret」点分形状
    models={
        "video": (Model("cogvideox-flash", "免费"),
                  Model("cogvideox-3", "1.05 元/次"),
                  Model("cogvideox-2", "0.7 元/次")),
        "image": (Model("cogview-3-flash", "免费"), Model("cogview-4", "0.06 元/张")),
        "text": (Model("glm-4-flash", "免费"), Model("glm-4-plus", "付费")),
        "vision": (Model("glm-4v-flash", "免费"),),
    },
    default={"video": "cogvideox-flash", "image": "cogview-3-flash",
             "text": "glm-4-flash", "vision": "glm-4v-flash"},
)

_AGNES = Spec(
    id="agnes", label="Agnes AI",
    module="agnes_video", cls="AgnesVideoProvider",
    env_keys=("HXMV_AGNES_KEY", "AGNES_API_KEY"),
    base_url="https://apihub.agnes-ai.com/v1",
    env_base="HXMV_AGNES_BASE",
    key_hint="platform.agnes-ai.com → API Keys",
    key_pattern=r"^sk-[A-Za-z0-9_\-]{16,}$",            # Agnes 是 sk- 开头的长串
    models={
        # flash 只出 720P、限免中；2.5 按秒计费（720P 0.025 美元/秒 ≈ 5 秒 ¥0.9）
        "video": (Model("agnes-video-2.5-flash", "限免 · 720P"),
                  Model("agnes-video-2.5", "0.025 美元/秒")),
        "image": (Model("agnes-image-2.5-flash", "限免"),
                  Model("agnes-image-2.1-flash", "限免")),
        "text": (Model("agnes-2.5-flash", "免费"), Model("agnes-3.0-flash", "免费"),
                 Model("agnes-2.5-pro", "付费")),
        "vision": (Model("agnes-2.5-flash", "免费"),),
    },
    default={"video": "agnes-video-2.5-flash", "image": "agnes-image-2.5-flash",
             "text": "agnes-2.5-flash", "vision": "agnes-2.5-flash"},
)

_LOCAL = Spec(id="local", label="本地 FFmpeg 渲染", needs_key=False,
              module="local_render", cls="LocalRenderProvider",
              key_hint="不联网、不花钱：把分镜真渲染成 mp4")
_FAKE = Spec(id="fake", label="仿真端点（自测）", needs_key=False, module="fake_api", cls="FakeApiProvider")
_KLING = Spec(id="kling", label="可灵 AI", needs_key=True, base_url="https://api.klingai.com",
              env_keys=("HXMV_KLING_KEY",), module="kling_example", cls="KlingStyleProvider",
              stub=True, key_hint="klingai.com → 开放平台（适配器仍是骨架，未进面板）")

SPECS: dict[str, Spec] = {s.id: s for s in (_ZH, _AGNES, _LOCAL, _FAKE, _KLING)}


def get(name: str) -> Spec | None:
    return SPECS.get((name or "").strip().lower())


def api_specs() -> tuple[Spec, ...]:
    """真能出片的 API 服务（面板设置页一张卡一家；只有骨架的不算）。"""
    return tuple(s for s in SPECS.values() if s.api and not s.stub)


def configured(provider: str) -> bool:
    """这家能不能用（要 Key 的看 Key，不要 Key 的永远可用）。"""
    from ..core import config          # 延迟导入：config 反过来要读注册表
    spec = get(provider)
    if not spec:
        return False
    return bool(config.api_key(spec.id)) or not spec.needs_key
