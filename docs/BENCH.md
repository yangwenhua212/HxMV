# 基准集跑分

- 生成器：`mock` ｜ 规划器：`mock` ｜ 每轮 8 个固定目标 ｜ 共 6 轮
- 每轮**换新项目**（清画面缓存）、**共享大脑**（经验累加）——曲线涨了才是真学到东西
- 首轮通过率 = 一次都不用修正就过；平均尝试 = 闭环为每个目标花的平均尝试数

![跑分曲线](bench.svg)

| 轮次 | 最终通过率 | 首轮通过率 | 平均尝试次数 | 成本 | 耗时(s) |
| :--: | :--: | :--: | :--: | :--: | :--: |
| 第 1 轮 | 62% | 25% | 8.75 | 130.00 | 88.2 |
| 第 2 轮 | 88% | 12% | 7.62 | 103.00 | 57.2 |
| 第 3 轮 | 88% | 50% | 6.62 | 79.00 | 44.5 |
| 第 4 轮 | 88% | 38% | 6.88 | 85.00 | 55.1 |
| 第 5 轮 | 100% | 50% | 7.12 | 91.00 | 59.1 |
| 第 6 轮 | 100% | 38% | 6.88 | 85.00 | 52.5 |

## 结论（照实写，一项一项看）

- 最终通过率：62% → 100%（+38 pp）
- 平均尝试次数：8.75 → 6.88（-1.87）
- 成本：130 → 85（-45）
- 耗时：88.2s → 52.5s（-35.7s）
- **首轮通过率**：25% → 38%（+12 pp）

- 返工量/成本在降 → 大脑的经验进了规划（起手参数带着上次的教训），同类失败被少踩了一遍。
- 曲线**不保证单调**：生成器本身带随机性，单轮波动不代表退步，看多轮走向。
- 首轮失败最多的三类：scene_inconsistency ×13、character_inconsistency ×12、character_mismatch ×9。
- 天花板说明：mock 世界里 `semantic_mismatch` 是**固定概率**（每镜头 8%，与任何参数无关）→ 首轮通过率不可能到 100%；`character_inconsistency`/`scene_inconsistency` 与参考强度挂钩，所以靠「经验起手」能压下去——两类要分开看，别拿同一个目标衡量。

## 每轮首轮失败的原因分布（哪些参数能改、哪些改不掉）

- 第 1 轮：character_inconsistency ×4、scene_inconsistency ×4、character_mismatch ×2、emotion_wrong ×2、motion_blur ×2、semantic_mismatch ×2、black_frame ×1、plot_break ×1
- 第 2 轮：action_mismatch ×2、character_inconsistency ×2、character_mismatch ×2、plot_break ×2、scene_inconsistency ×2、fps_too_low ×1、low_clarity ×1、semantic_mismatch ×1
- 第 3 轮：emotion_wrong ×2、motion_blur ×2、action_mismatch ×1、character_inconsistency ×1、character_mismatch ×1
- 第 4 轮：character_inconsistency ×2、character_mismatch ×2、scene_inconsistency ×2、action_mismatch ×1、plot_break ×1、semantic_mismatch ×1
- 第 5 轮：emotion_wrong ×2、motion_blur ×2、plot_break ×2、scene_inconsistency ×2、character_inconsistency ×1、low_volume ×1、semantic_mismatch ×1
- 第 6 轮：scene_inconsistency ×3、character_inconsistency ×2、character_mismatch ×2、plot_break ×2、emotion_wrong ×1、motion_blur ×1、semantic_mismatch ×1

## 每轮目标明细

**第 1 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 8 | increase_reference_strength | 9.3 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 3.87 |
| 城市夜景车流延时，8 秒 | ❌ | — | 10 | — | 15.82 |
| 海边日落，一只海鸥飞过，5 秒 | ❌ | — | 13 | — | 21.34 |
| 森林里的萤火虫，6 秒 | ✅ | — | 8 | increase_reference_strength | 11.27 |
| 雪山航拍，10 秒 | ❌ | — | 10 | — | 10.69 |
| 花朵绽放特写，5 秒 | ✅ | ✅ | 6 | — | 4.53 |
| 街头滑板少年，8 秒 | ✅ | — | 9 | increase_reference_strength | 11.43 |

**第 2 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | rewrite_prompt_closer | 5.65 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 2.45 |
| 城市夜景车流延时，8 秒 | ✅ | — | 7 | rewrite_prompt_closer | 5.87 |
| 海边日落，一只海鸥飞过，5 秒 | ❌ | — | 7 | — | 6.39 |
| 森林里的萤火虫，6 秒 | ✅ | — | 8 | increase_reference_strength、rewrite_prompt_closer | 10.32 |
| 雪山航拍，10 秒 | ✅ | — | 7 | rewrite_prompt_closer | 4.74 |
| 花朵绽放特写，5 秒 | ✅ | — | 10 | increase_reference_strength、rewrite_prompt_closer | 11.89 |
| 街头滑板少年，8 秒 | ✅ | — | 9 | increase_reference_strength | 9.94 |

**第 3 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | rewrite_prompt_closer | 7.31 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 5.56 |
| 城市夜景车流延时，8 秒 | ❌ | — | 7 | — | 8.91 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | ✅ | 6 | — | 4.04 |
| 森林里的萤火虫，6 秒 | ✅ | ✅ | 6 | — | 4.23 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 3.62 |
| 花朵绽放特写，5 秒 | ✅ | — | 7 | rewrite_prompt_closer | 5.44 |
| 街头滑板少年，8 秒 | ✅ | — | 8 | increase_reference_strength、rewrite_prompt_closer | 5.4 |

**第 4 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | increase_reference_strength | 7.62 |
| 小猫在窗台上看雨，5 秒 | ✅ | — | 9 | increase_reference_strength、rewrite_prompt_closer | 11.87 |
| 城市夜景车流延时，8 秒 | ❌ | — | 7 | — | 6.76 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | ✅ | 6 | — | 4.42 |
| 森林里的萤火虫，6 秒 | ✅ | — | 7 | increase_reference_strength | 6.81 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 8.34 |
| 花朵绽放特写，5 秒 | ✅ | — | 7 | rewrite_prompt_closer | 4.03 |
| 街头滑板少年，8 秒 | ✅ | ✅ | 6 | — | 5.2 |

**第 5 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | increase_reference_strength | 6.94 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 3.78 |
| 城市夜景车流延时，8 秒 | ✅ | — | 7 | rewrite_prompt_closer | 6.79 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | ✅ | 6 | — | 6.16 |
| 森林里的萤火虫，6 秒 | ✅ | ✅ | 6 | — | 6.42 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 3.61 |
| 花朵绽放特写，5 秒 | ✅ | — | 11 | increase_reference_strength、rewrite_prompt_closer | 14.44 |
| 街头滑板少年，8 秒 | ✅ | — | 8 | increase_reference_strength、rewrite_prompt_closer | 10.97 |

**第 6 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | ✅ | 6 | — | 4.28 |
| 小猫在窗台上看雨，5 秒 | ✅ | — | 8 | increase_reference_strength | 14.82 |
| 城市夜景车流延时，8 秒 | ✅ | ✅ | 6 | — | 4.37 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | — | 7 | increase_reference_strength | 5.2 |
| 森林里的萤火虫，6 秒 | ✅ | — | 7 | rewrite_prompt_closer | 4.55 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 3.76 |
| 花朵绽放特写，5 秒 | ✅ | — | 7 | increase_reference_strength | 5.67 |
| 街头滑板少年，8 秒 | ✅ | — | 8 | increase_reference_strength | 9.84 |

