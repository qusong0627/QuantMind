---
name: factor-drift-monitor
description: "因子面板漂移监测（QuantDB 直读 CN/HK/US）— 逐分区扫描 6_ml_datasets 因子面板，四类检查：断更与分区连续性、覆盖与缺失率、列集增删与列序变化、数值分布漂移（PSI/KS/均值平移）；标签列（return_Nd/future_return_Nd）单列监控含回填前沿。回答「特征面板今天还在更新吗」「哪一列悄悄停填了」「列集什么时候变的」「分布有没有漂」。用户问「面板断更了吗」「列集变更」「缺失率飙升」「常量列」「PSI 漂移」「标签停填」「数据哨兵」时使用。触发词：因子面板漂移、数据断更、列集变更、缺失率、常量列、PSI、标签填充、回填前沿、数据哨兵"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` 为纯标准库，宿主机/dsh/Windows 可直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/factor-drift-monitor/scripts/factor_drift.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/factor_drift.py --quantdb --market CN \
>      --dataset features_daily --start 2026-06-01 --end 2026-10-07 \
>      --out /data/reports/factor-drift-monitor/cn_features_daily_20260601_20261007.json
>    ```
> 3. **报告落盘**：容器内 `/data/reports/factor-drift-monitor/<market>_<dataset>_<窗口>.json`；stdout 同时输出中文告警表。容器内写出的报告属主是 root，删除需 `docker exec`。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式会静默查空）。本技能不按 symbol 输出，格式仅用于解读「标的数」等覆盖指标。

# factor-drift-monitor — 因子面板漂移监测

回答三个问题：**面板还在正常更新吗？哪一列悄悄坏了？分布有没有漂？** 这是**数据哨兵**：只读面板做结构性与统计性体检，不构造因子、不回测、不喊单、不修改任何数据。补充背景：本项目曾因「特征面板列集停更」造成一个月的信号中断——本技能即为补上这道哨兵。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` | 内置确定性合成面板 + 四种注入断言（①整列缺失 ②列变常量 ③列集减列 ④均值平移 +2σ）；纯标准库，宿主机可直接跑 |
| `--quantdb --market CN` | 直读 `features_daily`（默认）/`l1_factors`/`l2_factors`：①覆盖/断更（分区数、每分区标的数、日期连续性、最新分区 vs 行情分区）②缺失与常量（逐列非空率、nunique、全 0 列）③列集漂移（相对基线增删 + 相邻分区切换，含仅列序变化）④分布漂移（PSI/KS/均值平移σ/σ比，基线取窗口前段稳定期） |
| `--quantdb --market HK/US` | 同上，因子面板为 `l1_factors`；US 分区滞后本身是一类一等告警 |
| `--allow-removed/--allow-added` | 已知断点白名单：命中的列增删降级为 info（先记录进 `references/known-breakpoints.md`） |
| `--last-partitions N` | 大窗口只取最近 N 个分区（HK/US 全量分区多时用，实测 35 个分区已足够报出结构变化） |
| `--columns/--baseline-partitions/--recent-partitions/--max-dist-alerts/--out` | 列子集、窗口与输出控制 |

输出：按严重度排序的中文告警表（critical/warning/info，每条含对象、基线 vs 当前实测数字、可能原因提示）+ 完整 JSON 报告（含逐列缺失/分布明细与全部告警）。

## 本地数据映射（QuantDB）

| 市场 | 因子面板（`6_ml_datasets/`） | 行情参照（`1_kline_data/`） | 备注 |
|---|---|---|---|
| CN | `features_daily`（默认）、`l1_factors`、`l2_factors` | `daily_forward` | features_daily 列集两次断点（实测）：09-14 换列（−2/+30，50→78 列）、09-21 标签改名 `return_{n}d`→`future_return_{n}d` |
| HK | `l1_factors`（190 列） | `daily_forward` | 无日期列→日期取分区名 `dt=`；28 列基线即常量（含 adj_factor/pe_ttm/pb/roe 等基本面列，实测） |
| US | `l1_factors`（176 列） | `daily_forward` | 因子分区可能大幅落后行情（实测 2026-09-17 vs 2026-10-06，落后 13 个分区→critical） |

- **分区读数**：`<root>/<market>/6_ml_datasets/<dataset>/dt=YYYYMMDD/data.parquet`，逐分区读取，只读不写。
- **基线/对比口径**：基线=窗口**前** N 个分区（默认 20，应取稳定期），近期=窗口**后** N 个分区（默认 5）；列集切换在全部相邻分区上检测（列序变化也报，因为按列位置读取的管线会静默错位）。
- **断更判据**：以同市场行情分区的最新日期为主要参照（避免国庆/中秋长假误报），自然日兜底；缺分区（目录缺失）与空分区（目录在但 0 行）分开报告。
- **标签列**：`label_return` / `return_{n}d` / `future_return_{n}d` 单列监控——出现区间、中位填充率、**回填前沿**（末次 ≥50% 填充的分区）；标签变化只报不提级（影响训练口径，不影响实时信号）。
- 阈值全部可配（PSI 0.10/0.25、KS 0.20、均值平移 1σ、缺失率 20%/Δ10pp 等），出处与本地修正见 `references/drift-methods.md`。

## 标准流程

1. 先自检引擎：宿主机 `python3 skills/factor-drift-monitor/scripts/factor_drift.py --demo`，四个注入断言应全 PASS、干净面板零告警。
2. 容器内跑扫描（见环境契约命令）；CN 全窗、HK/US 可用 `--last-partitions 35`。
3. 读报告顺序：**断更/空分区**（最致命——面板停了后面都白算）→ **覆盖**（标的数骤降）→ **列集**（增删/改名/列序）→ **缺失/常量**（哪列坏了）→ **分布**（PSI/KS 看清是数据故障还是真实行情变化）。
4. 确认是「已知上游改版」的告警，用 `--allow-removed/--allow-added` 加白并补进 `references/known-breakpoints.md`；确认是故障的，把原报告 JSON 路径一并交给数据管线负责人。
5. 结论必须带数据窗口与分区数；分布漂移不区分「数据故障」与「真实市场状态变化」，报告已内置该声明。

## 实测校准（2026-10-08，quantmind 容器内实跑，四份报告在 `/data/reports/factor-drift-monitor/`）

| 运行 | 窗口 | 分区 | 标的数 | critical / warning / info |
|---|---|---|---|---|
| CN `features_daily` | 2026-06-01~09-30 | 86 | 5221~5566 | 3 / 10 / 9 |
| CN `l1_factors` | 2026-06-01~09-30 | 86 | 5192~5210 | 1 / 15 / 3 |
| HK `l1_factors` | 2026-08-17~10-05（最近 35 分区） | 35 | 2078~2252 | 0 / 8 / 11 |
| US `l1_factors` | 2026-07-27~09-17（最近 35 分区） | 35 | 470~491 | 3 / 15 / 2 |

真实漂移点（脚本实际报出的，节选）：

- **CN features_daily 三次列集切换全部命中**：09-11→09-14 移除 `Symbol_val,close_val`、新增 30 列（`is_hs300/is_hsgt/is_margin/is_st/is_quit_risk` 等）→ critical，且新增的 `is_hk/is_quit_risk` 自引入即常量再报 critical（标志位全 0 属预期，需白名单）；09-14→09-15 **仅列序变化** → info（集合不变但按位置读取会错位）；09-18→09-21 六个标签整体改名 `return_Nd→future_return_Nd` → label warning「旧名标签下游训练样本会全空」。
- **覆盖告警**：CN 09-21 起 7 个分区标的数由 5516 中位降至 5221（−5.4%），与同日列集切换并发，提示词判为上游改版；HK 09-25 单分区 2213→2078（−6.1%）单日即回。
- **US 断更 critical**：因子分区止于 09-17，行情分区已到 10-06，落后 13 个分区；CN 两会话因子=行情=09-30，落后 0 个分区（长假期间不误报）。
- **标签回填前沿（CN features_daily）**：`return_1d` 末次填充 09-16、`return_3d` 09-01、`return_5d` 08-28、`return_10d` 08-21、`return_20d` 08-07、`return_60d` 06-11——填充前沿随 horizon 变长而逐级前移，提示 9 月中旬后标签回填停跑；新代 `future_return_*` 自 09-21 引入后 7 个分区**从未填充**（`return_60d` 中位填充仅 0%）。
- **分布漂移**：CN l1_factors 近期段 `ind_momentum_decay` 均值平移 −1.12σ（PSI 3.916、KS 0.567，σ比 0.70）、`concept_momentum_top3` PSI 3.951；US 段 `kdj_d` PSI 0.932 / −0.98σ；HK 出现纯 KS 触发（`kline_kup` PSI 仅 0.040 但 KS 0.475——离散计数列的典型形态）。这些不能直接判故障，价格水平/动量类在趋势行情中必然漂移（报告内已注明）。
- **常量与稀疏（HK/US）**：HK 28 列基线即常量（含 adj_factor/pe_ttm/pb/roe/bp/float_mv/total_mv 等基本面列——从未填充）；18 列 `ind_*` 基线缺失率 ≥50%（实测 86.3%~100%）且未恶化 → 聚合为一条 info，不刷屏；US `vol_realized_rrv` 近段突变为常量 → critical。
- **demo 断言（宿主机实跑）**：①末日整列缺失→missing/amt_log critical ②列变常量→constant/rsi_14 critical ③列集减列→column-set/chip_conc_20 critical ④均值平移+2σ→distribution/vol_std_20 warning（PSI=4.662、KS=0.714、+1.95σ、σ比=1.03）；未注入面板 critical=0/warning=0/info=0。**五项全部 PASS**。

## 常见坑（均为 2026-10-08 实测验证）

1. **断更别用自然日判**：CN 因子与行情在国庆/中秋长假期间同步停更（0619 端午、0925 中秋、1001~1007 均两侧无分区），只报 info「工作日缺口（疑为节假日）」；主判据是「因子最新分区 vs 同市场行情最新分区」的滞后分区数（≥2 warning、≥5 critical）。US 实测落后 13 个分区即 critical。
2. **features_daily 列集有两代三断点**：09-14 换列（−2/+30，50→78 列）、09-15 仅列序、09-21 标签改名。跨代窗口跑指标会看到巨量「分布漂移」——先把三个断点加白名单再对比分布（见 `references/known-breakpoints.md`）。新增标志位列（`is_hk` 等）全 0 是正常语义，不是故障。
3. **标签缺失不等于故障**：`return_60d` 中位填充 0%、`future_return_*` 从未填充都没有被提级为 critical——标签只影响训练口径；但要盯「回填前沿」：前沿停滞=标签重算任务停跑，旧名标签被下游训练引用时样本会全空。
4. **US 行情分区有真实缺口**：2026-08-17~19（周一到周三，美股正常交易日）因子与行情两侧均无分区——脚本按设计报为 info「工作日缺口（疑为节假日）」，实为已知数据缺陷（0907 劳动节属正常休市）。修数据前不要把它当节假日放过。
5. **pe 类列可能出现 inf**：CN features_daily `pe_static/pe_ttm/ps_ttm` 在 09-07~09-14 各分区分别有 81/4/5 个 inf（窗口内共 540 处，已记入 JSON `profile.inf_cells`）；脚本已把非有限值从均值/标准差/PSI 中过滤（否则统计量会被污染并刷 numpy 警告），近段命中时另报 integrity warning。要复算这几列先自行过滤 inf。
6. **HK/US 大量常量/稀疏列是现状而非漂移**：HK 28 列、US 14 列基线即常量（基本面列未接入）；`ind_*` 行业聚合列基线缺失率 86%~100%（结构性稀疏）。脚本把「基线即缺且未恶化」聚合为一条 info；若下游用这些列，先与数据管线确认填充口径。
7. **空分区与缺分区分开报**：目录在但 data.parquet 0 行（空分区，critical）与目录缺失（缺分区，warning/info 分流）是两种故障；实测四份报告均无空分区。
8. **分布统计是等距抽样**（每分区每列 ≤800、每段 ≤20000 个点，确定性），小票池/小样本自动降级为低置信度 info——HK/US 只有几百只标的时结论要按低置信度读。

## 脚本与参考

- `scripts/factor_drift.py`：监测引擎（纯标准库检查核心 + QuantDB 装配层，pandas/pyarrow 仅在 quantdb 模式加载；`--demo` 与 `--quantdb` 共用同一套检查代码）。
- `references/drift-methods.md`：四类检查的定义、阈值表与出处（源技能默认 + 本地修正）、严重度语义、JSON 契约。
- `references/known-breakpoints.md`：实测已知断点清单与白名单用法（CN features_daily 09-14/09-21、CN l1 08-26、US l1 08-10）。
- `references/source-boundary.md`：数据边界与不做的事（本地版）。

## 来源与许可

方法论移植自 [quantskills/skill-factor-drift-monitor](https://github.com/quantskills/skill-factor-drift-monitor)（**GPL-3.0-only**）。本地化改写：数据层由 PandaData SDK 换为 QuantDB 本地 parquet 直读（逐分区扫描），新增断更/覆盖/列集/标签四类本地检查与已知断点白名单；源的 HTML 报告未移植，输出改为中文告警表 + JSON。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
