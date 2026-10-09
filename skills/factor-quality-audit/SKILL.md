---
name: factor-quality-audit
description: "单因子质量裁决（QuantDB 直读 CN/HK/US）— 依次执行票池构造、时点对齐（PIT）、IC/IR 简版、换手与成本、行业+规模中性化五道检查，把因子裁决为 alpha / 行业暴露 / 泄漏 / 样本幻觉四类之一，每项附判据、证据数字与置信度；因子列直读 features_daily/l1_factors，收益由 daily_forward 自算。用户问「这个因子靠不靠谱」「是不是未来函数/数据泄漏」「是不是行业暴露冒充 alpha」「毛收益很高能扣费吗」「因子质检/体检怎么做」时使用。触发词：因子质检、因子体检、因子质量、数据泄漏、未来函数、行业暴露、样本幻觉、换手成本、中性化残差"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` / `--input` 为纯标准库，可在宿主机/dsh/Windows 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/factor-quality-audit/scripts/factor_quality_audit.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/factor_quality_audit.py --quantdb --market CN \
>      --factor ma_gap_20 --start 2024-01-01 --end 2025-12-31 --top-n 300 \
>      --out /data/reports/factor-quality-audit/cn_ma_gap_20_2024_2025.json
>    ```
> 3. **报告落盘**：容器内 `/data/reports/factor-quality-audit/<market>_<factor>_<窗口>.json`；stdout 同步输出中文裁决卡。容器内写的报告文件属 root，宿主机侧清理用 `docker exec`。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式会静默查空）。`--symbols` 显式票池也按此格式传入。

# factor-quality-audit — 单因子质量裁决

回答一个问题：**这个因子配不配叫 alpha？** 默认立场（沿源技能）：任何因子先按嫌疑人处理——依次过票池构造、时点对齐、IC/IR、换手成本、中性化五道检查，最终裁决 ∈ {alpha / 行业暴露 / 泄漏 / 样本幻觉}，每项附判据、证据数字与置信度。只做质检裁决：不构造因子、不调参、不喊单。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` | 内置确定性合成面板（泄漏 / 纯噪声 / 行业暴露 / 真 alpha 四场景）+ 断言自检，全过才打印「demo 自检：全部通过」；纯标准库 |
| `--input <csv>` | 任意长面板 `date,symbol,factor[,fwd_ret]`（无收益列可用 `close` 现算）；纯标准库，宿主机可跑 |
| `--quantdb --market CN --factor <col>` | 因子列直读 `features_daily`（默认）/`l1_factors`/`l2_factors`；收益用 `daily_forward` 前复权自算 |
| `--quantdb --market HK/US --factor <col>` | 因子列直读 `l1_factors`；HK 自动按 (symbol,date) 去重；收益=不复权原始价 |
| `--out <json>` | 报告落盘（契约见 `references/output-contract.md`） |

输出（每因子）：① 票池构造检查 → ② 时点对齐（PIT）→ ③ IC/IR 简版 → ④ 换手与成本 → ⑤ 中性化残差（行业+规模 FWL）→ 裁决 + 置信度 + 保留意见；stdout 中文裁决卡 + JSON 报告。

## 本地数据映射（QuantDB）

| 数据 | CN | HK | US |
|---|---|---|---|
| 因子面板（`6_ml_datasets/`） | `features_daily`（默认）、`l1_factors`、`l2_factors` | `l1_factors`（190 列） | `l1_factors`（176 列） |
| 收益源（`1_kline_data/`） | `daily_forward`（**前复权**） | `daily_forward`（**不复权**） | 同 HK |
| 行业映射 | `instrument_detail.rs_hyname`（静态快照，后缀式 Symbol） | `akshare_profile/{symbol}.parquet` 所属行业 | `sector/{symbol}.parquet` sector |
| 规模列（中性化用） | `total_mv`（自动取自然对数）或 `ln_mv_total` | `ln_mv_total`（`total_mv` 实测全 0） | 同 HK |

- **收益定义**：同日口径 `ret_h(t)=close(t+h)/close(t)−1`；次日执行口径 `ret_lag(t)=close(t+h+1)/close(t+1)−1`，h 默认 1（`--horizon` 可改）。读取范围自动向窗口末端之后多取 h+3 个交易日。
- **票池**：窗口末日成交额降序前 `--top-n`（默认 300；0=全部），或 `--symbols` 显式指定。
- **标签列护栏**：`return_{n}d` / `future_return_{n}d` / `label_return` 命中泄漏黑名单——**故意传入会被裁决为「泄漏」**（质检演示路径），不会静默当因子用。
- **阈值可配**：显著性/泄漏/中性化保留率/成本/分位等全部为常量并开放 CLI（`--sig-t`、`--leak-ic`、`--neutral-retention`、`--cost-bp`、`--quantile` 等），默认见 `references/methodology.md`。

## 标准流程

1. 先跑 `--demo` 自检环境（宿主机即可），应打印 6 条断言全过。
2. 选定候选因子（列名与填充度可先用 factor-ic-decay 的 `--list-columns` 探查，HK/US 大量基本面列未填充）。
3. `--quantdb` 运行，读卡顺序：① 截面宽度/覆盖 → ② 时点保留率 → ③ 显著性（NW-t、分段方向）→ ④ 可执行端毛/净与换手 → ⑤ 中性化保留率 → 裁决与保留意见。任何一步不过线都会体现为降置信、降档或换裁决，具体判据见方法文档。
4. 报告 JSON 落盘，改窗口/票池重跑后保留前后两份做稳健性对比。
5. 按裁决分派动作：**泄漏** → 先修因子定义/时点再重取数，不继续回测；**行业暴露** → 做行业中性口径或分行业切片重验；**样本幻觉** → 换窗口/票池/频率重验（该裁决不等于因子经济上无效）；**alpha** → 看保留意见（成本、PIT、票池口径）再决定下一步。

## 实测校准（2026-10-08）

| 场景（配方） | 关键数字 | 裁决 |
|---|---|---|
| CN `ma_gap_20`（2024-01-02~2025-12-31，top-300，`n_ic=485`） | IC −0.0314、NW-t −3.26、ICIR 年化 −2.33、命中率 55.9%；PIT 保留 97.2%；中性保留 0.94；可执行端（低分位）毛 −1.0 bp/日、换手 25.6%/日、净 −2.6 bp/日；理论多空（信号方向）净 −25.9 bp/日 | alpha（负向），置信中 |
| CN `dividend_rate`（同配方） | IC +0.0159、NW-t +1.97（未过 2.0 线）、ICIR +1.46；PIT 保留 97.1%；中性保留 0.54（行业方差解释中位 57.8%）；可执行端（高分位）毛 −6.8 bp/日、换手 2.4%/日 | 样本幻觉，置信中 |
| CN `return_20d` 当因子传入（同配方，泄漏演示） | 名称规则直接命中；同日 IC +0.2018（|IC|<0.9，数据规则不兜底，只有名称规则能抓） | 泄漏，置信高 |
| HK `rsi_14`（top-200，2024-01-01~2025-12-31） | IC +0.0041、NW-t +0.46；截面中位 132 只/日、覆盖 83.6%；行情去重 551063 行 | 样本幻觉，置信高 |
| US `rsi_14`（top-300，同窗口） | IC −0.0053、NW-t −0.76、n=502；行业覆盖 99%（295 键） | 样本幻觉，置信高 |
| `--demo` 断言 | mystery_signal→泄漏；random_noise→样本幻觉；sector_beta_proxy→行业暴露（中性保留 −0.01）且扣费后净<0；idio_alpha_demo→alpha（净 +43.3 bp/日） | 6/6 全过 |

交叉验证：CN `ma_gap_20` 的 IC −0.0314 / NW-t −3.26 与 `dividend_rate` 的 +0.0159 / +1.97 与既存 factor-ic-decay 技能标定表**完全一致**（两技能独立实现同一 IC 口径互验）；可执行端与多空数字另经独立 pandas 复算逐日对齐（曾踩中尾部取片陷阱，修正后 diff=0，见常见坑 2）。单因子全量运行约 22 秒（485 日 × 300 只）。

## 常见坑（2026-10-08 实测标定）

1. **秩 IC 与两端十分位价差可反号**（实测两例方向相反都出现过）：`ma_gap_20` 截面秩 IC −0.0314，但 top−bot 十分位价差 +23.0 bp/日（高分位端相对全池 +21.9 bp/日；低分位端 −1.0 bp/日）；`dividend_rate` 秩 IC +0.0159，但高分位端 −6.8 bp/日。含义：秩相关由宽截面中段主导，尾部极值是另一个问题——**不要拿 IC 一个数推可执行收益，也不要因一端无超额就断言 IC 是假的**；两者可以同时为真。
2. **分区目录名必须 sorted**：`Path.glob("dt=*")` 返回顺序不保证有序（实测踩中）。窗口末端要取「下一个交易日」行情算前向收益时，未排序取 `[:k]` 可能拿到更晚的分区，把末日收益静默变成多日收益（实测污染量级 ~1.5 bp/日 的均值偏差）。本工具全部经 `sorted()` 的分区遍历。
3. **HK `daily_forward` 双来源重复行**（边界 2024-09-02 ~ 2026-05-08）：本次窗口实测 551063 行成对重复；须按 `published_at` 排序保留 akshare（`keep='last'`），不能按行序 `keep='first'`（部分标的会拿到 `paid_hk` 的复权缩放价）。工具自动去重并写入 ① 票池检查。
4. **HK/US `l1_factors` 大量列未填充**：`total_mv` 实测全 0，规模中性化只能用 `ln_mv_total`（工具自动探测候选列，缺失即跳过规模项并降置信）；其他基本面列同前，用前先抽查。
5. **features_daily 列集两次断点**：50→78 列在 2026-09-14，标签整体改名 `return_{n}d`→`future_return_{n}d` 在 2026-09-21；两代标签列名都在黑名单；`list_date` 仅 78 列版有——本窗口无此列，上市天数检查降级为说明项。
6. **标签黑名单要靠名称规则兜底**：`return_20d` 当因子实测 |IC| 仅 0.2018，低于数据规则阈值 0.9（重叠窗口标签自相关被平滑），只有名称规则能抓现行；数据规则（|IC|≥0.9）只兜移名改名的确定性泄漏。
7. **CN 行业/ST 均为静态快照**（`rs_hyname` / `IsSTGP`，HqDate=20260720）：用它过滤历史样本含前视偏差，工具只作现状说明；行业中性化同样吃快照口径（保留意见已注）。
8. **票池=窗口末日成交额事后选池**（轻微选择偏差，非逐日动态池）——已写入每份报告 caveats；需要动态池请用 `--symbols` 显式提供。
9. **HK 截面稀疏**：top-200 实测中位 132 只/日（最小 1 只）、覆盖 83.6%——HK 结论的样本基础天然更薄；单日 <10 只不进统计。
10. **运行环境分档**：`--quantdb` 需 pandas/pyarrow（容器内跑）；`--demo` 与 `--input` 纯标准库（宿主机/dsh/Windows 可跑）。

## 脚本与参考

- `scripts/factor_quality_audit.py`：裁决引擎（纯标准库统计核心 + QuantDB 装配层，pandas/pyarrow 仅在 quantdb 模式加载）。
- `references/methodology.md`：五道检查的方法、公式、阈值与裁决决策顺序（含与源方法论对照）。
- `references/output-contract.md`：stdout 裁决卡与 JSON 报告的字段契约、裁决枚举与 demo 断言契约。

## 来源与许可

方法论移植自 [quantskills/skill-factor-mason](https://github.com/quantskills/skill-factor-mason)（**GPL-3.0-only**）：「任何因子先按嫌疑人处理」立场、四步工作流（时点审计 / 样本清洗 / 因子变换 / 验证指标）、输出契约与测试用例为主要来源。本地化改写：数据层由 PandaData SDK 换为 QuantDB 直读；输出由文字报告改为中文裁决卡 + JSON；源测试用例 6 例改写为可执行的 demo 断言与实测坑清单；源的 agents/ 多运行时入口与 assets 未移植。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
