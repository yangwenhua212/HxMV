# Provider 接入指南

把 HxMV 接到真实视频生成服务的说明书。**HxMV 天生多 API**：有哪几家由
`hxmv/providers/registry.py` 一张表说了算，工厂/面板设置页/CLI/自检/健康检查全从它读。

## Key 写错家？两道防线（真实事故后加的）

事故：**Agnes 的 Key 被写进了智谱槽位** → 智谱整条链（视频/出图/视觉）全 401，而面板/健康检查
只显示「已配 Key」「ready」，**表面完全看不出来**。教训是「配了 Key」≠「Key 对」。

1. **形状校验**：注册表里每家可声明 `key_pattern`（智谱 = `32位.16位` 点分；Agnes = `sk-` 开头）。
   `--set-key` 与面板设置页写入前先比对，不符**直接拒绝**并说清期望形状；确认无误要强行写，
   CLI 加 `--force`、面板传 `force: true`。
2. **自检查重**：`python3 -m hxmv --doctor` 会列出各家 Key 指纹，发现**两家共用同一把**就报错
   （同一把 Key 出现在两家 = 一定有一家写错了）。

写 Key 之后的规矩：**打到那家的真实接口验证过（要带对照组）再算数**，别只看「已保存」。

## 加一家 API 要改什么（就这五处 + 注册表一条）

一家 API 与另一家的差别其实只有五处，其余（档案复用、参考图解析、首帧生成、尾帧派生、
指纹与缓存复用、工程修正、结果登记）都在 `providers/api_video.py` 里，子类**一行都不用重写**：

| # | 差异点 | 子类提供 | 智谱 | Agnes |
|---|---|---|---|---|
| 1 | 视频任务请求体 | `_submit_body()` | `videos/generations` + size/fps/quality | `videos` + mode/seconds/aspect_ratio |
| 2 | 轮询取结果 | `_poll_url()` | `async-result/{id}`，`task_status` | `/agnesapi?video_id=…&model_name=…`，`status` |
| 3 | 出图请求体 | `_image_body()` | `size="1344x768"` | `size="1K"` + `ratio` |
| 4 | 产物下载带不带令牌 | `download_auth` | 要 | **不要**（Agnes 产物域名带了会 401） |
| 5 | 能力与档位 | `camera_support` / `COST_UNITS` / `MAX_DURATION` / `FIRST_LAST_MODELS` | flash 只有提示词 | keyframe 首尾帧 |

```python
# 1) 写适配器：hxmv/providers/<你的>.py，继承 ApiVideoProvider，填上面五处（几十行）
# 2) 注册：hxmv/providers/registry.py 的 SPECS 里加一条 Spec(...)（中文名/Key 从哪拿/有哪些档位/默认档）
# 3) 完事 —— 工厂、`--provider <名字>`、`--set-key <名字>`、面板设置页、doctor、/api/config 自动认它
```

**能力别吹**：`camera_support`/`MAX_DURATION` 声明错了，闭环就会按它挑落点（声明了首尾帧却做不到 →
判据判死好片）。声明放在适配器类里，注册表不替模型吹。

## 已接好的真实服务①：智谱 CogVideoX-Flash（免费）

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

## 已接好的真实服务②：Agnes AI（flash 档限免）

`hxmv/providers/agnes_video.py`，网关 `https://apihub.agnes-ai.com/v1`（OpenAI 兼容；
同一把 Key 同时管画面/视频/大脑）：

```bash
python3 -m hxmv --set-key agnes <你的KEY>          # platform.agnes-ai.com → API Keys
python3 -m hxmv --key-status
python3 -m hxmv --provider agnes --project 柯基短剧 "柯基在雪地里奔跑"
python3 -m hxmv --default agnes                    # 设为「默认用哪家」（面板设置页里也能点）
```

| 项 | 说明 |
|---|---|
| 视频档位 | `agnes-video-2.5-flash`（限免，只出 720P 1280×704）/ `agnes-video-2.5`（按秒计费） |
| 图像档位 | `agnes-image-2.5-flash`（限免；16:9 1K = 1312×736） |
| 图生视频 | `mode=keyframe`：`first_frame`/`last_frame` 吃 data URL → 与智谱一样**不需要图床** |
| 时长 | 4–12 秒（字符串），执行器按 `MAX_DURATION` 钳制，不会要不到硬要 |
| 检索 | 必须带 `model_name`（不带只对 text 模式有效）；轮询地址不在 `/v1` 下 |
| 下载 | **不能带 Authorization**（产物 CDN 带了返回 401） |
| 免费档限制 | 文本 **10 次/分钟**、出图 1K 10 次/分、**视频排队+限流**（实测连投会 429/`video_queue_full`） |
| 提速 | 只有官方 Token Plan = **订阅制** → 本项目按「只接受按量充值」的红线**不买**，用免费档就接受排队 |
| **排队怎么办（免费档）** | 项目约定「免费就行不怕等」+「**我让它做，它就做**」：提交被按回（429/队列满）就**在原地退避重试**到 `SUBMIT_WAIT`（默认 **2 小时**）用尽——**不会自己改到别的时段**。已受理后轮询上限 `DEFAULT_TIMEOUT=900`。等待期间打印「已等/上限」，不会看着像卡死 |
| 低峰策略（默认**关**） | 机制留在代码里：家显式设 `NIGHT_FIRST=True` + `HXMV_NIGHT_WINDOW`（如 `02:00-06:00`，支持跨零点）后，队列满且不在窗口内时才会 park 到窗口开始再试（`HXMV_PARK_MAX` 默认 12h）。默认关的理由见 `DESIGN.md` |
| 旋钮 | `HXMV_SUBMIT_WAIT` / `HXMV_API_TIMEOUT` 覆盖默认等待；排队时那条任务会**占住面板工位**（面板 worker 是串行的），后面的任务排在它后面 |
| 实测代价 | 一轮闭环：分镜/角色/场景 ≈ 1 分钟内（图档限免、稳），**视频那一镜在队列里要等 5–15 分钟**（高峰期更久，等的就是它） |
| 大脑也会等 | 模型端 429/5xx 退避重试（上限 `HXMV_LLM_WAIT`，默认 120s）；免费档文本 10 次/分钟，撞一次就让分镜退成兜底是白丢质量 |

没 Key 的链路验证同上（`tools/fake_zhipu_api.py` 改指各家 base 即可）。

## 只有骨架的：可灵（`kling_example.py`，`stub=True`）

类还在仓库里当**新适配器的模板**，但没实现生成，所以注册表标了 `stub=True`：
不进面板设置页、不进 doctor、不会被自动挑中（想试就显式 `--provider kling`）。
