# HxSync ↔ HxMV 接入施工图（客户端契约）

目标（老大的原话）：**HxSync 一边能帮忙把 HxMV 装到手机上，一边也能让它跑在远端，不管在哪跑，成品都回到 HxSync。**

HxMV 侧已经就绪（本文档末尾列了实测证据），HxSync 侧按下面的契约加一个页面即可。

---

## 一、三种模式（同一个 App 支持，用户选）

| 模式 | HxMV 跑在哪 | 怎么接 | 现状 |
|---|---|---|---|
| **A. 远端**（推荐默认） | 服务器 / 家里机器 | 填地址 + token，走 HTTP/SSE | ✅ 服务端全就绪（`https://hxmv.eraherm.com`） |
| **B. 端侧 Termux** | 手机自己的 Termux 里 | 一键安装脚本 → 连 `http://127.0.0.1:8668` | ✅ 脚本已就绪（`scripts/install-termux.sh`） |
| **C. 内置内核** | 手机 App 进程内 | Chaquopy 嵌 Python + 打包 ffmpeg | ⚠ 未做（要 1-3 天，见第五节） |

**A/B 用同一套接口**，客户端代码完全一样，只有 base URL 不同 → 先做 A+B，C 以后再说。

---

## 二、客户端契约（HxMV 提供，客户端照接）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/health`（公开免令牌） | GET | **发现实例 + 能力自检**：版本、ffmpeg、编码器、各后端是否配好 Key、项目列表、是否需要 token |
| `/api/run` | POST `{goal, provider, project}` | 提交生产任务 → `{run_id}` |
| `/api/stream?run_id=&token=` | GET (SSE) | 实时事件流（先回放已落盘事件，再推送新事件） |
| `/api/run/<id>` | GET | 全量事件 + `status: running\|done`（**状态看事件里有没有 run.done，不靠最后一条**） |
| `/api/runs` | GET | 制片历史摘要列表 |
| `/api/brain` | GET | 大脑条目（做过什么、学到什么） |
| `/api/artifact?run_id=&name=` | GET | 取产物字节（mp4/png，`Content-Disposition: inline`） |
| `/dl/<文件名>` | GET | 分发包下载（客户端安装包等，放 `~/.hxmv/dl/`；公开、免 token、支持断点续传） |

**认证**：`X-Hxmv-Token` 头（或 `?token=`，SSE 只能用 query）。健康检查也需要 token（除非服务端没设 `HXMV_WEB_TOKEN`）。

**事件类型**（SSE 与 `/api/run/<id>` 同构，渲染这几类就够）：

| type | 关键字段 | 客户端怎么显示 |
|---|---|---|
| `run.start` | `goal`, `phase` | 开始，显示目标 |
| `task.start` | `action`, `task_id`, `kind` | 任务卡片：动作名 + 第几次尝试 |
| `critic` | `action`, `score`, `failures[]`, `measured.metrics` | **实测指标**（分辨率/帧率/时长/音量/黑帧/一致度）+ 分数 |
| `decision` | `decision: PASS\|RETRY\|FAIL`, `note` | 通过 / 修正 / 失败 |
| `infra.retry` | `error`, `retryable` | "服务端抖动，自动重试"（和服务无关的错误） |
| `run.done` | `completed`, `attempts`, `cost_units`, **`artifacts[]`（本次全部成品的绝对路径）**, `n_reused` | 收尾 + 产物列表（**含复用自项目档案的旧文件**） |
| `notify` | `result` | 推送是否成功（客户端可忽略） |

**主动推送**（客户端不在线也不丢成品）：服务端设 `HXMV_NOTIFY_URL` 后，每次跑完 POST 一份自足负载：

```json
{
  "event": "run.done", "run_id": "...", "goal": "第2集：柯基跑到海边看浪",
  "phase": "DONE", "ok": true, "completed": 6, "failed": 0, "attempts": 6, "cost_units": 0.0,
  "artifacts": [
    {"name": "final_4f201adb.mp4", "kind": "final", "size": 411337,
     "url": "https://hxmv.eraherm.com/api/artifact?run_id=...&name=final_4f201adb.mp4"}
  ],
  "needs_token": true,
  "text": "✅ 完成｜第2集…｜任务 6 项完成｜共 6 次尝试｜成本 0.0\n成片：final_4f201adb.mp4"
}
```
- 目标可以是**任意 JSON 端点**，也可以是 `feishu:<飞书机器人 webhook>`（服务端会转成飞书文本卡片）。
- URL 里**故意不带 token**（别让凭据跟着通知到处飞）；客户端自己补 `&token=`。

---

## 三、HxSync 要加的东西（复用现有件，新代码约 300-500 行）

| 新增 | 复用 | 说明 |
|---|---|---|
| `data/network/HxmvApiClient.kt` | `SharedHttpClients.streamingApi()`、`OpenAiCompatClient` 的 SSE 解析套路 | health / run / stream / artifact 四个调用 |
| `data/local/HxmvPrefs.kt` | `SecurePrefs` | 存 base URL + token + 默认项目名 |
| `viewmodel/HxmvViewModel.kt` | 现有 ViewModel 风格 | 提交、订阅事件、聚合状态、完成后通知 |
| `ui/screens/HxmvScreen.kt` | `ui/screens/` 现有页面 | 目标输入 + 后端选择 + 项目名 + 实时进度 + 产物列表/播放 |
| 部署向导（可选） | Termux 安装脚本 | 见第四节 |

**通知**：跑完发本地通知（`视频好了：第2集…`），点通知直接播成片。这解决了"手机端长连接会被系统杀"的问题——
服务端推 + 客户端拉两条路都在。

**产物播放**：直接给播放器 `url + "&token="`（或先把字节下到缓存再 fed）。下载用 `SharedHttpClients.download`。

---

## 四、"HxSync 帮忙部署 HxMV 到手机"（模式 B 的落地）

1. 检查是否装了 Termux（`PackageManager.getPackageInfo("com.termux")`）。
2. 没装 → 引导装（F-Droid / GitHub Releases，小米商店没有）；**不能静默装**，必须用户点。
3. 装了 → 用 Termux 的外部命令意图把一键脚本喂进去（需在 HxSync 声明 `com.termux.permission.RUN_COMMAND`，
   且用户在 Termux 里允许外部应用调用）：
   ```
   Intent("com.termux.RUN_COMMAND")
     .setClassName("com.termux", "com.termux.app.RunCommandService")
     .putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash")
     .putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf("-lc",
         "curl -sL https://raw.githubusercontent.com/yangwenhua212/HxMV/main/scripts/install-termux.sh | bash"))
     .putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
   ```
4. 装完用 `/api/health` 探活 `http://127.0.0.1:8668`，通了就切到模式 B。
5. 部署前/后都能跑 `python3 -m hxmv --doctor` 自检（结构化输出，可直接渲染成"部署向导"页面）。

---

## 五、模式 C（内置内核）为什么先不做

- 需要 Chaquopy（内嵌 CPython，Gradle 插件，APK +15~25MB）**外加** ffmpeg 二进制（每个 ABI +30~40MB）
- HxMV 零第三方依赖（纯标准库）这点帮了大忙，但 ffmpeg 躲不掉（真测量/拼接都要它）
- 构建必须在有资源的机器上跑（**本机 1.6G 跑不了 Gradle，走 OVH**），且要处理 Android 上的
  `libffmpeg.so` 落盘 + `chmod +x` + SELinux 执行限制（可行，但有坑）
- 收益：不用 Termux、没有"pkg install"这一步，体验最干净 → **等 A/B 用顺了再评估**

---

## 六、发版与合规红线（别踩）

- 覆盖安装：**必须 release 签名**（`hermchat-release.jks` 在老大的电脑上）+ **versionCode 递增**（现 35 → 36），
  详见 `hermchat/docs/RELEASE.md`
- 构建走 OVH（java21 现成）；本机 1.6G 只用来改代码
- **不代老大操作任何账号**（智谱注册/实名、Termux 安装点击都必须他本人）

---

## 七、已实测的接入证据（HxMV 侧，非设计稿）

用真实客户端流程跑过一遍（`/tmp/hxmv_client_e2e.py`，等价于 HxSync 会做的调用序列）：

```
① /api/health → hxmv 0.6.0 | ffmpeg=true | 项目 ['柯基短剧'] | needs_token=true | notify=true
② POST /api/run {goal, provider: local, project: 柯基短剧} → run_id
③ 轮询 /api/run/<id> → 10s 完成 6 项、6 次尝试全一次过（项目档案命中 → 复用旧画面）
④ 收到服务端主动推送 → 5 个产物（成片/2 镜头/2 参考图）带公网可下载 URL
⑤ 用推送里的 URL 直接下成片 → HTTP 200、video/mp4、411KB、文件头 ftyp（真 mp4）
```

顺带修掉的两个真问题（都是这次接入才暴露的）：
1. **全是复用时本次产物目录是空的** → 现在会把本次真正用到的成品（含复用）硬链接归拢进本次 run 目录，
   面板/客户端/分集产物清单都齐
2. **Cloudflare 403 掉 `Python-urllib` 的默认 UA** → HxMV 所有对外请求统一带 `HxMV/<版本>` UA；
   客户端（OkHttp/浏览器）天然没这个问题，但用 Python 写脚本调试时要注意
