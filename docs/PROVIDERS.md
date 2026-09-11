# Provider 接入指南

把 HxMV 接到真实视频生成服务（智谱/可灵/Veo/Runway/即梦…）的说明书。

## 已接好的真实服务：智谱 CogVideoX-Flash（免费）

`hxmv/providers/zhipu_video.py`，开箱可用：

```bash
# 1) 拿 Key：https://bigmodel.cn → 注册/登录 → API Keys → 新建
# 2) 存 Key（写入 ~/.hxmv/config.json，权限 600；环境变量 HXMV_ZHIPU_KEY 优先）
python3 -m hxmv --set-key zhipu <你的KEY>
python3 -m hxmv --key-status           # 确认已配置

# 3) 跑真视频（带项目档案：角色参考图会被当首帧喂进去 → 图生视频锁角色）
python3 -m hxmv --provider zhipu --project 柯基短剧 --episode 1 "第1集：柯基在雪地里打滚"
```

要点：

| 项 | 说明 |
|---|---|
| 模型 | 默认 `cogvideox-flash`（**免费**）；`HXMV_ZHIPU_MODEL=cogvideox-3` 可切付费高质版 |
| 图生视频 | 项目档案里的角色/场景参考图 → `image_url`（base64 data URL），是**角色一致性的硬约束**；没有参考图时退化为纯文生视频 |
| 异步 | `POST /videos/generations` → 轮询 `GET /async-result/{id}` → 下载 mp4 落盘 |
| 失败分道 | 401/403/400 → `retryable=False`（不重试）；429/5xx/超时/任务 FAILED → 可重试 |
| 事后修正 | 音量增益、片头黑场这类修正**直接做在下载到的文件上**（`_postprocess`）——省额度且 Critic 复测能量到真变化 |
| 分辨率 | 内部 `720p/1080p` → API `size=1920x1080`（避免给非法枚举值）；`HXMV_ZHIPU_SPEED=1` 切"速度优先" |
| 一致性基线 | 用"参考图经同分辨率同 CRF 编码后的帧"当基线，采样点取首帧（图生视频首帧应贴近输入图） |
| 额度护栏 | flash 免费 `cost_units=0`；切付费模型时计价进 Budget，烧到上限立即停 |

**没 Key 时的本地端到端验证**（不花钱、不注册）：仓库外起一个同构仿真端点，把 base 指过去即可——

```bash
# 造一个假视频当"上游返回的片子"（用 local provider 渲一个就行），然后：
python3 tools/fake_zhipu_api.py 8799 /path/to/some.mp4
FAKE_EXPECT_KEY=test-key-123 HXMV_PROVIDER=zhipu HXMV_ZHIPU_KEY=test-key-123 \
HXMV_ZHIPU_BASE=http://127.0.0.1:8799/api/paas/v4 python3 -m hxmv --provider zhipu "..."
# 已验证：提交(含图生视频 base64) → 轮询 → 下载 → L1/L2 实测 → 修正 → 通过
```

## 三步接入其它服务

### 1. 写适配器（继承 `VideoProvider`）

```python
# hxmv/providers/my_service.py
from .base import ProviderError, VideoProvider

class MyService(VideoProvider):
    name = "myservice"                      # HXMV_PROVIDER=myservice 启用
    action_map = {"GENERATE_SHOT": "text2video"}   # 支持的闭环动作

    def generate(self, task) -> dict:
        # task.input / task.constraints 已是 API 参数格式（ProviderExecutor 投影过）
        url = self._submit_and_poll(task)   # 你自己的 API 调用
        return {
            "media": url,          # 媒体 URL/路径（Critic 从这里抽帧）
            "duration": task.input.get("duration", 5),
            "defects": [],         # 服务端已知问题可填；否则留空让 Critic 检测
            "cost_units": 2.0,     # 真实计价！
        }

    def estimate_cost(self, action: str) -> float:
        return 2.0
```

### 2. 注册进工厂

`hxmv/core/executor.py` 的 `make_executor()` 加一行：

```python
if name == "myservice":
    from ..providers.my_service import MyService
    return ProviderExecutor(MyService())
```

### 3. 启用

```bash
HXMV_PROVIDER=myservice HXMV_MYSERVICE_KEY=sk-xxx python3 -m hxmv "你的目标"
```

## 关键桥接逻辑（别踩坑）

| 事项 | 规则 |
|---|---|
| **API 失败 ≠ 质量 FAIL** | 网络/超时/额度抛 `ProviderError`（Loop 自动重试最多 4 次）；只有"片子出来了但不好"才走 Refiner 调参。两者路径完全不同 |
| **retryable=False** | 永久性错误（鉴权失败/不支持的 action）——别傻重试，直接放弃该任务 |
| **cost_units 必须真实** | Budget 靠它防烧钱。失败也计费的 API，在 ProviderError 里带上 cost_units |
| **参数投影** | Refiner 调的是内部语义（reference_strength/motion_scale），`ProviderExecutor` 里的 `_PROJECTION` 表负责投影成 API 参数（cfg_scale 等）。换 API 改这张表，别动闭环 |
| **参考图/首尾帧** | 一致性真正的抓手是角色参考图（见 kling_example 的 TODO），只靠 prompt+强度是弱约束。有 Asset Manager 后在这里接资产 |
| **媒体交给 Critic** | 服务端不报告缺陷就留空 defects——真实视频由 L1(FFmpeg)/L2(关键帧比对)/L3(LLM) 从媒体本身检测 |

## 计价参考（2026-09，以各家官网为准）

| 服务 | 5s 视频量级 | 备注 |
|---|---|---|
| **智谱 CogVideoX-Flash** | **免费** | 已接入；支持图生视频（首帧/首尾帧）、最高 4K |
| 智谱 CogVideoX-3 | ~¥1/次 | 同接口，质量更高 |
| 可灵 Kling | ~¥1-2/次 (pro) | 有图片/首尾帧参考能力 |
| Veo 3 | 按积分/时长 | Google AI Studio/Vertex |
| Runway Gen | 按 credit | 帧率分辨率定价不同 |
| 即梦/海螺 | 会员/点数 | 中文友好 |

生成失败率真实世界约 5-15%（不含质量不达标）——闭环的 Refiner 重试 + 质量记忆正是为这个设计的：同样的问题第二次不会用同样的参数再踩一遍。

## Fake provider（不花钱验证整条链路）

```bash
HXMV_PROVIDER=fake python3 -m hxmv "雪地里的柯基"
```

`hxmv/providers/fake_api.py` 仿真真实服务：两段式提交/轮询、按 cfg_scale 决定一致性缺陷、12% 概率上游 503（验证基础设施重试）。跑通它 = 你的适配器接口写对了。
