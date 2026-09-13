# 基准集跑分

- 生成器：`mock` ｜ 规划器：`mock` ｜ 每轮 8 个固定目标 ｜ 共 6 轮
- 每轮**换新项目**（清画面缓存）、**共享大脑**（经验累加）——曲线涨了才是真学到东西
- 首轮通过率 = 一次都不用修正就过；平均尝试 = 闭环为每个目标花的平均尝试数

![跑分曲线](bench.svg)

| 轮次 | 最终通过率 | 首轮通过率 | 平均尝试次数 | 成本 | 耗时(s) |
| :--: | :--: | :--: | :--: | :--: | :--: |
| 第 1 轮 | 100% | 38% | 7.12 | 91.00 | 57.2 |
| 第 2 轮 | 100% | 12% | 7.12 | 91.00 | 61.0 |
| 第 3 轮 | 75% | 12% | 7.38 | 97.00 | 55.1 |
| 第 4 轮 | 100% | 50% | 6.75 | 82.00 | 50.8 |
| 第 5 轮 | 88% | 38% | 6.88 | 85.00 | 53.7 |
| 第 6 轮 | 75% | 38% | 7.12 | 91.00 | 55.9 |

## 结论（照实写，一项一项看）

- 最终通过率：100% → 75%（-25 pp）
- 平均尝试次数：7.12 → 7.12（+0.00）
- 成本：91 → 91（+0）
- 耗时：57.2s → 55.9s（-1.3s）
- **首轮通过率**：38% → 38%（+0 pp）

- 返工量/成本没降 → 经验在记但没进决策，或者这批目标起手就已经是对的（看下面的目标明细）。
- 但**首轮通过率没涨**：经验只覆盖了一部分缺陷类型，其余仍从默认起手。看「修正过」那列反复出现的键，那就是还没进起手参数的短板。
- 曲线**不保证单调**：生成器本身带随机性，单轮波动不代表退步，看多轮走向。
- 首轮失败最多的三类：character_inconsistency ×13、character_mismatch ×13、semantic_mismatch ×9。
- 天花板说明：mock 世界里 `semantic_mismatch` 是**固定概率**（每镜头 8%，与任何参数无关）→ 首轮通过率不可能到 100%；`character_inconsistency`/`scene_inconsistency` 与参考强度挂钩，所以靠「经验起手」能压下去——两类要分开看，别拿同一个目标衡量。

## 每轮首轮失败的原因分布（哪些参数能改、哪些改不掉）

- 第 1 轮：character_inconsistency ×3、character_mismatch ×3、scene_inconsistency ×3、plot_break ×2、emotion_wrong ×1、low_clarity ×1、low_volume ×1
- 第 2 轮：character_inconsistency ×4、character_mismatch ×4、plot_break ×2、black_frame ×1、scene_inconsistency ×1、semantic_mismatch ×1
- 第 3 轮：character_inconsistency ×3、character_mismatch ×3、semantic_mismatch ×3、motion_blur ×1
- 第 4 轮：plot_break ×2、scene_inconsistency ×2、semantic_mismatch ×2、motion_blur ×1
- 第 5 轮：motion_blur ×2、action_mismatch ×1、character_inconsistency ×1、character_mismatch ×1、emotion_wrong ×1、plot_break ×1、scene_inconsistency ×1、semantic_mismatch ×1
- 第 6 轮：character_inconsistency ×2、character_mismatch ×2、semantic_mismatch ×2、action_mismatch ×1

## 每轮目标明细

**第 1 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | increase_reference_strength | 6.01 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 4.06 |
| 城市夜景车流延时，8 秒 | ✅ | — | 7 | rewrite_prompt_emotion | 7.15 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | — | 10 | increase_reference_strength、rewrite_prompt_closer | 13.29 |
| 森林里的萤火虫，6 秒 | ✅ | — | 8 | increase_reference_strength | 9.78 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 4.56 |
| 花朵绽放特写，5 秒 | ✅ | — | 7 | increase_reference_strength | 8.52 |
| 街头滑板少年，8 秒 | ✅ | ✅ | 6 | — | 3.79 |

**第 2 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 7 | rewrite_prompt_continuity | 6.92 |
| 小猫在窗台上看雨，5 秒 | ✅ | — | 8 | increase_reference_strength | 12.17 |
| 城市夜景车流延时，8 秒 | ✅ | — | 8 | rewrite_prompt_closer、rewrite_prompt_emotion | 9.03 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | — | 7 | increase_reference_strength | 7.05 |
| 森林里的萤火虫，6 秒 | ✅ | — | 7 | increase_reference_strength | 6.39 |
| 雪山航拍，10 秒 | ✅ | — | 7 | increase_reference_strength | 9.43 |
| 花朵绽放特写，5 秒 | ✅ | ✅ | 6 | — | 2.44 |
| 街头滑板少年，8 秒 | ✅ | — | 7 | increase_reference_strength | 7.55 |

**第 3 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 8 | increase_reference_strength、rewrite_prompt_closer | 7.65 |
| 小猫在窗台上看雨，5 秒 | ❌ | — | 8 | — | 3.38 |
| 城市夜景车流延时，8 秒 | ❌ | — | 8 | — | 10.19 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | — | 7 | rewrite_prompt_closer | 4.75 |
| 森林里的萤火虫，6 秒 | ✅ | — | 8 | increase_reference_strength | 11.79 |
| 雪山航拍，10 秒 | ✅ | — | 7 | rewrite_prompt_closer | 4.83 |
| 花朵绽放特写，5 秒 | ✅ | ✅ | 6 | — | 5.29 |
| 街头滑板少年，8 秒 | ✅ | — | 7 | increase_reference_strength | 7.24 |

**第 4 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | ✅ | 6 | — | 5.72 |
| 小猫在窗台上看雨，5 秒 | ✅ | — | 8 | reduce_motion_scale、rewrite_prompt_closer | 7.26 |
| 城市夜景车流延时，8 秒 | ✅ | — | 7 | increase_reference_strength | 8.89 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | ✅ | 6 | — | 3.05 |
| 森林里的萤火虫，6 秒 | ✅ | — | 7 | rewrite_prompt_closer | 5.24 |
| 雪山航拍，10 秒 | ✅ | — | 8 | increase_reference_strength | 9.11 |
| 花朵绽放特写，5 秒 | ✅ | ✅ | 6 | — | 5.42 |
| 街头滑板少年，8 秒 | ✅ | ✅ | 6 | — | 6.13 |

**第 5 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | — | 8 | increase_reference_strength、rewrite_prompt_closer | 4.17 |
| 小猫在窗台上看雨，5 秒 | ✅ | — | 7 | increase_reference_strength | 9.52 |
| 城市夜景车流延时，8 秒 | ❌ | — | 8 | rewrite_prompt_action | 11.87 |
| 海边日落，一只海鸥飞过，5 秒 | ✅ | — | 7 | increase_reference_strength | 7.03 |
| 森林里的萤火虫，6 秒 | ✅ | ✅ | 6 | — | 4.38 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 3.92 |
| 花朵绽放特写，5 秒 | ✅ | ✅ | 6 | — | 5.48 |
| 街头滑板少年，8 秒 | ✅ | — | 7 | rewrite_prompt_emotion | 7.28 |

**第 6 轮**

| 目标 | 通过 | 首轮通过 | 尝试 | 修正过 | 耗时(s) |
| :--- | :--: | :--: | :--: | :--- | :--: |
| 一只柯基在雪地里奔跑，5 秒短片 | ✅ | ✅ | 6 | — | 4.05 |
| 小猫在窗台上看雨，5 秒 | ✅ | ✅ | 6 | — | 5.46 |
| 城市夜景车流延时，8 秒 | ❌ | — | 7 | — | 7.25 |
| 海边日落，一只海鸥飞过，5 秒 | ❌ | — | 8 | — | 6.62 |
| 森林里的萤火虫，6 秒 | ✅ | — | 10 | increase_reference_strength、rewrite_prompt_closer、rewrite_prompt_emotion | 9.2 |
| 雪山航拍，10 秒 | ✅ | ✅ | 6 | — | 9.13 |
| 花朵绽放特写，5 秒 | ✅ | — | 7 | increase_reference_strength | 6.97 |
| 街头滑板少年，8 秒 | ✅ | — | 7 | increase_reference_strength | 7.17 |

