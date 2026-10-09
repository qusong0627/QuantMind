---
name: factor-ic-decay
description: "因子 IC 衰减诊断（QuantDB 直读 CN/HK/US）— 日度截面 Spearman IC、ICIR、Newey-West 显著性、60 日滚动/分段稳定性、多周期 IC 衰减曲线与半衰期；因子列直读 features_daily/l1_factors，收益由 daily_forward 前复权自算。用户问「因子 IC 衰减快不快」「IC 半衰期」「因子稳不稳」「ICIR 怎么样」「预测力是不是在衰退」「因子还能用多久」时使用。触发词：IC 衰减、IC 半衰期、ICIR、因子稳定性、预测力衰退、Newey-West、因子有效期"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--input` CSV 模式为纯标准库，可在宿主机/dsh/Windows 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/factor-ic-decay/scripts/ic_decay.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/ic_decay.py --quantdb --market CN \
>      --factors ma_gap_20,rsi_14 --start 2024-01-01 --end 2025-12-31 --top-n 300 \
>      --out /data/reports/factor-ic-decay/cn_2024_2025.json
>    ```
> 3. **报告落盘**：建议容器内 `/data/reports/factor-ic-decay/<market>_<窗口>.json`；stdout 同时输出中文表格。改窗口/票池重跑时保留前后两份 JSON。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式会静默查空）。

# factor-ic-decay — 因子 IC 衰减与稳定性诊断

回答三个问题：**预测力衰减有多快？IC 稳不稳？半衰期大概多久？** 只做截面预测力的时序与跨周期诊断——不构造因子、不回测组合、不喊单；半衰期是对历史衰减形状的拟合，不是「还能用 N 天」的承诺。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--input <csv>` | 任意长面板（`date,symbol,factor,fwd_ret`，可选 `fwd_ret_{n}` 多周期）纯标准库诊断 |
| `--quantdb --market CN` | 因子列直读 `features_daily`（默认）/`l1_factors`/`l2_factors`；收益用 `daily_forward` 前复权自算 |
| `--quantdb --market HK/US` | 因子列直读 `l1_factors`；HK 自动按 (symbol,date) 去重；收益=不复权原始价 |
| `--quantdb --list-columns` | 列清单 + 最新分区填充度（非空计数 / nunique，⚠ 标注常量列），选因子前先跑 |

输出（每因子）：均值 IC、IC 标准差、ICIR（raw/年化）、命中率、Newey-West t、60 日滚动均值 IC 与年化 ICIR、非重叠分段均值 IC 与胜率、多周期衰减曲线、指数衰减半衰期拟合；JSON 报告 + stdout 表格。

## 本地数据映射（QuantDB）

| 市场 | 因子面板（`6_ml_datasets/`） | 收益源（`1_kline_data/`） | 备注 |
|---|---|---|---|
| CN | `features_daily`（默认）、`l1_factors`、`l2_factors` | `daily_forward`（**前复权**） | features_daily 列集两次断点（实测）：09-14 换列（−2/+30，50→78 列）、09-21 标签改名 `return_{n}d`→`future_return_{n}d` |
| HK | `l1_factors`（190 列） | `daily_forward`（**不复权**） | 无日期列→日期取分区名 `dt=`；双来源重复行自动去重 |
| US | `l1_factors`（176 列） | `daily_forward`（**不复权**） | 同上；因子分区进度可能落后行情（实测 US 因子止于 2026-09-17，行情到 2026-10-06） |

- **收益定义**：`ret_h(t) = close(t+h)/close(t) − 1`，h∈{1,5,10,20}（`--horizons` 可改），h 为同一标的序列的后续第 h 个交易日；读取范围自动向窗口末端之后多取 40 个交易日。
- **票池**：窗口末日成交额降序前 `--top-n`（默认 300；0=全部），或 `--symbols` 显式指定。
- **标签列护栏**：`return_{n}d` / `future_return_{n}d` / `label_return` 是内置未来收益标签，脚本拒绝当因子（防前视泄漏），命中即报错退出。

## 标准流程

1. `--list-columns` 探查列名与填充度，避开常量/未填充列（HK/US 大量基本面列为全 0）。
2. 跑诊断：窗口建议 ≥252 交易日（<60 拒绝输出，60~251 报「样本偏少」警告），多个因子逗号分隔一次跑完（共享同一收益面板）。
3. 读报告：先看 `n_ic` 与截面宽度，再看均值 IC / NW-t / 分段胜率，最后看衰减曲线与半衰期；结论须标注数据窗口、观测数，事实与推断分离。
4. 单因子因数据不足（常数列/截面过窄）不会拖垮整批：记入 JSON `failed_factors` 并在 stdout 列出原因。

## 实测校准（2026-10-08，CN features_daily，2024-01-02~2025-12-31，top-300@20251231，n_ic=485）

| 因子 | 均值 IC | ICIR 年化 | NW-t | 强度标签 | 半衰期 |
|---|---|---|---|---|---|
| ma_gap_20 | −0.0314 | −2.33 | −3.26 | strong | 负 IC 不拟合 |
| rsi_14 | −0.0346 | −2.88 | −3.80 | strong | 负 IC 不拟合 |
| vol_std_20 | −0.0370 | −2.33 | −3.24 | strong | 负 IC 不拟合 |
| dividend_rate | **+0.0159** | +1.46 | +1.97 | moderate | **17.1 日**（τ=24.7，R²=0.73） |
| pb | −0.0284 | −2.38 | −3.27 | moderate | 负 IC 不拟合 |
| pe_ttm | −0.0064 | −1.02 | −1.39 | weak_or_noise | — |

方向合理性：趋势位置类（ma_gap_20）、超买类（rsi_14）、波动率（vol_std_20）均为负 IC——与 A 股短周期反转、低波效应一致；红利为正 IC 且唯一可拟合半衰期（衰减曲线峰在 H5 后回落，故 R² 仅 0.73）。HK/US 冒烟（2024-2025 l1_factors，top-200）：HK rsi_14 +0.0060 / vol_std_20 −0.0186（覆盖仅 134 只，见坑 3）；US（默认窗口 2024-10~2026-09）vol_std_20 +0.0201 / mom_ret_5d −0.0140（弱）。

引擎金样：源技能自带 demo 面板经本脚本输出与源 `examples/output/ic_decay.txt` 完全一致（均值 IC +0.9047、ICIR +451.002、NW-t +609.67、半衰期 5.56 日）；CN ma_gap_20/H1 另经 pandas 原生 spearman 独立复算：−0.031374 vs 工具 −0.031369（n=485，一致到 4 位以上小数）。

## 常见坑（2026-10-08 实测标定）

1. **features_daily 列集两次断点**：2026-09-14 换列（−2/+30，50→78 列，标签名仍为 `return_{n}d`）、09-21 标签整体改名 `return_{n}d`→`future_return_{n}d`——脚本逐分区读取、缺列分区跳过并计数，两类标签名都在黑名单。
2. **HK `daily_forward` 双来源重复行，边界精确为 2024-09-02 ~ 2026-05-08**（2024-08-26 与 2026-05-15 均干净；重复期内单日 70–75% 的行成对）：同 (symbol,date) 有 `paid_hk` + `akshare` 两行。多数标的 close 相差 ≤ 舍入级（43.900002 vs 43.900000），**但约 0.5%~5% 的标的 `paid_hk` 带（前）复权缩放**（2025-01-15 实测 70/1523 只 >0.5%：1211.HK ×3、0788.HK ×10、0755.HK ×100）。脚本按 `published_at` 排序后保留 akshare（最新发布、原始价）并写入 caveats——**不能按行序 keep='first'**，否则部分标的拿到复权价。
3. **HK/US `l1_factors` 填充残缺**：turnover_rate/pe_ttm/pb/roe/bp/ep_ttm/float_mv/total_mv 实测 nunique=1（全 0，未填充）；技术类列填充逐日不均（HK 2024-01-02 有 1414/2200，20241231 分区仅 8~9 只）——用前 `--list-columns` 抽查，且它只反映最新分区，跨年还需抽查窗口内分区。
4. **HK/US `daily_forward` 不复权**：收益未含分红调整，IC 口径偏保守；CN 用前复权。
5. **票池是「窗口末日成交额」事后选池**（含轻微选择偏差，非逐日动态池）——已写入每份报告的 caveats。
6. **半衰期只对正均值 IC 周期拟合**（源方法论）：负 IC 因子（反转/低波类）不产出半衰期是**预期行为**，不是失败；衰减曲线非单调时（如 dividend_rate 峰在 H5）R² 会低，须如实注明。
7. **重叠收益**：多周期 `ret_h` 由同一价格滚动生成，相邻日高度重叠会抬高 IC 自相关——NW-t 校正方向正确但 lag 仍是经验值（默认 5），衰减曲线解释需谨慎。
8. **截面护栏**：当日有效标的 <3 跳过该日；报告 `cross_section` 给出每日标的中位数与跳过天数，截面过窄时 IC 噪声大。
9. **防未来函数**：脚本只拦标签列；因子本身的时点正确性（因子只用截至当日可知信息）由数据生产方保证。

## 脚本与参考

- `scripts/ic_decay.py`：诊断引擎（纯标准库统计核心 + QuantDB 装配层，pandas/pyarrow 仅在 quantdb 模式加载）。
- `references/ic-methods.md`：方法定义（Spearman IC / ICIR / NW-t / 滚动与分段 / 半衰期拟合 / 标本护栏 / JSON 契约）。
- `references/source-boundary.md`：数据边界与不做的事（本地版）。

## 来源与许可

方法论移植自 [quantskills/skill-factor-ic-decay](https://github.com/quantskills/skill-factor-ic-decay)（**GPL-3.0-only**），本地化改写（数据层由 PandaData 换为 QuantDB 直读；源的 HTML 报告与 qsh-form/validate 脚本未移植，输出改为 JSON + stdout 表格）。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
