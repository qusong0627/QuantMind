---
name: barra-risk-model
description: "Barra 式多因子风险模型（QuantDB 直读 CN/HK/US）— 风格+行业暴露、日度截面 WLS 因子收益、Ledoit-Wolf 收缩协方差、组合风险分解（因子 vs 特异）与极小方差组合验证；用户问「我的组合风险有多大」「风险来自哪里」「因子暴露怎么样」「特异风险多少」「协方差能不能用来做优化」「做个风险模型」时使用。触发词：风险模型、组合风险、风险分解、因子暴露、特异风险、协方差估计、Ledoit-Wolf、极小方差、Barra"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` 为纯标准库，可在宿主机/dsh/Windows 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/barra-risk-model/scripts/barra_risk.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/barra_risk.py --quantdb --market CN \
>      --start 2024-01-01 --end 2025-12-31 --top-n 300 --validate 30 \
>      --out /data/reports/barra-risk-model/cn_2024_2025.json
>    ```
> 3. **报告落盘**：容器内 `/data/reports/barra-risk-model/<market>_<窗口>.json`（= 宿主机 `data/reports/...`）；stdout 同时输出中文表格。容器写出的文件属主是 root，宿主机删除受挫时用 `docker exec quantmind rm ...`。注意容器时钟比宿主机慢约 1 小时。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式/小写会静默查空，脚本以「票池在行情窗口中无数据」退出）。

# barra-risk-model — Barra 式多因子风险模型

把「我的组合风险有多大？风险来自因子还是个股？」变成可复算的结构化模型：风格 + 行业暴露 → 日度截面 WLS 因子收益 → 收缩协方差 → 组合风险分解与极小方差验证。**只测风险结构，不预测收益、不喊单**；因子收益的 t 值只描述历史样本，不能当交易信号。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` | 纯标准库 3 因子确定性小算例（正交设计，可手算复核；demo 金样见「实测校准」） |
| `--quantdb --market CN` | 暴露直读 `features_daily`（默认；`l1_factors`/`l2_factors` 可选），收益用 `daily_forward` 前复权自算；行业用 `instrument_detail.rs_hyname` |
| `--quantdb --market HK/US` | 暴露直读 `l1_factors`，收益=不复权价格收益；HK 自动按 `published_at` 去重；行业用 akshare_profile / yahoo sector |
| `--quantdb --list-columns` | 列清单 + 标签列警示 + 风格列选取预演（含 ⚠ 常量列标注），跑模型前先探查 |
| `--symbols a,b,c` | 显式票池（否则为窗口末日成交额前 `--top-n`） |
| `--weights <csv>` | 自定义组合（`symbol,weight`）的风险归因，默认等权末日截面 |
| `--styles` / `--no-industry` / `--max-industries` | 模型构成控制（风格子集、纯风格、行业数上限） |
| `--cov-method ewma` | EWMA 替代 Ledoit-Wolf 因子协方差（含半衰期参数） |

输出：因子收益表（年化收益/波动/t 值）、因子协方差（含 κ 与 PSD 自检）、特异波动分位、组合风险分解（因子 vs 特异、逐因子 Euler 贡献）、极小方差组合验证、独立校验块（对拍偏差）；JSON 报告 + stdout 表格。

## 本地数据映射（QuantDB）

| 市场 | 行情（`1_kline_data/`） | 因子面板（`6_ml_datasets/`） | 行业源 | 备注 |
|---|---|---|---|---|
| CN | `daily_forward`（**前复权**） | `features_daily`（默认）/ `l1_factors` / `l2_factors` | `instrument_detail.rs_hyname`（128 类，静态快照） | 列集两次断点，风格列自动适配（见坑 4） |
| HK | `daily_forward`（**不复权**） | `l1_factors`（190 列） | `akshare_profile.所属行业`（31 类） | 双来源重复行自动去重（见坑 2）；市值列全 0（见坑 1） |
| US | `daily_forward`（**不复权**） | `l1_factors`（176 列） | `sector.sector`（11 类） | 同上；因子分区可能落后行情（见坑 3） |

- **收益定义**：y = 标的自身收益序列的**次日**收益（`close.pct_change().shift(-1)`），暴露与 y 严格错开一日；读取范围自动前扩 480 个自然日（覆盖 252+21 交易日动量窗）并向窗口后多取 5 个行情分区。
- **票池**：窗口末日成交额降序前 `--top-n`（默认 300；0=全部），或 `--symbols`（前缀式会被自动转后缀）。
- **标签列护栏**：`return_{n}d` / `future_return_{n}d` / `label_return` 是收益标签，不得作为暴露；脚本不把它们列入任何风格候选，`--list-columns` 会点名警示。
- 行业映射复用本仓 Brinson 技能的既有口径（源文件/列名一致）。

## 标准流程

1. `--list-columns` 探查列与标签（先看风格列选取预演，⚠ 标常量/全 0 列）。
2. 跑模型：窗口建议 ≥1 年（<60 个回归期拒绝输出；60~251 期报「样本偏少」警告）；默认 `--weight-by sqrt_mcap`（CN=真实市值；HK/US=成交额代理，报告 `weight_scheme` 写明）；报告落 `/data/reports/barra-risk-model/`。
3. 读报告顺序：先 `model`（期数/截面宽度/跳过原因）与 `validation`（对拍偏差）→ `factor_stats`（年化波动量级是否合理）→ `factor_cov`+`specific`（κ、PSD、特异中位）→ `attribution`（因子/特异占比与逐因子贡献）→ `min_variance`（`le_equal_weight` 必须为 true）→ `caveats`（本次运行的去重/剔除计数）。
4. 多市场/多窗口结论并排时口径先对齐：CN 前复权、HK/US 不复权；HK/US 无真实市值；行业快照期不同——**跨市场因子收益不可直接比较**（报告已固定声明）。

## 实测校准（2026-10-08）

**CN 全窗**（`features_daily`，2024-01-01~2025-12-31，top-300@20251231）：n=485 期，中位 293 只/日，末日截面 294 只。风格年化波动：SIZE 10.66% / VALUE 4.01% / MOMENTUM 6.52% / SHORT_REV 7.65% / VOLATILITY 8.96% / BETA 8.92% / LIQUIDITY 8.82%；行业最高为元器件 44.2%（半导体 39.3%）。κ=0.0198，F 最小特征值 9.07e-04（PSD 通过）。特异波动中位 44.73%（P10 23.2% / P90 64.9%）。等权组合年化波动 31.16% = 因子 99.2% + 特异 0.8%；极小方差组合 23.89%（比值 0.767，`le_equal_weight=true`）。强共线实测：SIZE~LIQUIDITY −0.78（旧列集兜底口径所致，报告已提示）。运行时长约 40 秒。

**独立校验**（每份报告自动输出；CN 上述窗口 30 日抽样）：因子收益 numpy 正规方程 vs lstsq（SVD 路径）最大偏差 5.29e-08；vs 纯标准库高斯消元 7.01e-16；LW κ 双实现偏差 0.0；风险恒等式 \|factor+specific−total\|/total = 0.0；显式 N×N Σ 二次型 vs 分解式 ≈3e-16（两次运行 2.9e-16 / 5.7e-16，机器精度级随 BLAS 归约浮动）。

**demo 金样**（`--demo`，纯标准库）：前 24 日特异收益为 0 时 WLS 精确还原因子真值——实测最大偏差 2.59e-11；单因子闭式手算偏差 1.53e-12；含噪声段 5.01e-03；κ=1.0（T=47 分辨不出非对角结构，属真实现象）；极小方差比值 0.649。

**HK 冒烟**（2025 全年，top-200，n=243 期）：双来源重复行剔除 43884 行；5 个行业全窗未识别被剔除并列名；特异波动中位 25.23%；等权波动 22.07%，极小方差比值 0.821。**US 冒烟**（2025-07~2026-09，top-150，n=303 期）：因子分区止于 2026-09-17，其后 9 个交易日因暴露缺失被跳过并计数；等权波动 13.15%。

## 常见坑（2026-10-08 实测标定）

1. **HK/US `l1_factors` 的市值/估值列是「非空全 0」占位符**：`total_mv / float_mv / bp / pb / pe_ttm / turnover_rate / style_bp` 实测非空但 nunique=1（全 0.0）——直接引用会静默算出全 0 暴露。更隐蔽的是 `ln_mv_total / style_ln_mv_total`：**不是市值**，填充值 ≈ ln(当日成交额)（US 实测：dt=20251231 分区与 ln(amount) 逐行相等占比 100%、最新 20260917 分区中位偏差 7.3e-05；HK 实测：2025 多数分区为 0——dt=20251231 全 0——2026 起填充且逐行等于 ln(成交额)）。本脚本 SIZE/LIQUIDITY 走成交额代理、VALUE 在 HK/US 禁用，`--list-columns` 可复现该检查。
2. **HK `daily_forward` 双来源重复行，窗口精确为 2024-09-02 ~ 2026-05-08**：同 (symbol,date) 有 paid_hk + akshare 两行，脚本按 `published_at` 保留最新并报 caveat（2025 全年窗口实测剔除 43884 行）。不要按行序 keep='first'，部分标的会拿到复权价。
3. **US 因子分区可能落后行情**：实测因子止于 2026-09-17 而行情到 2026-10-06。窗口末端超出因子覆盖时，这些交易日无暴露可用、整日跳过（`skip_reasons.too_few_names` 计数，不崩）——跑完先对 `data_through` 与「最后回归日」是否早于 `--end`。
4. **CN `features_daily` 两次断点**：2026-09-14 换列（50→78 列）、09-21 标签改名 `return_{n}d`→`future_return_{n}d`（两者都是收益标签，禁作暴露）。风格列选取按分区全存在 + 末分区非常量自动落位（如 LIQUIDITY 在新列集用 `hs_turnover`、旧列集自动退 `amount_ma_5`）——旧列集下 SIZE 与 LIQUIDITY 实测相关 −0.78，解读时留意。
5. **薄行业哑变量会「吃光」个股残差**：某行业当日有效截面只有 1 只时，行业哑变量完全过拟合该股（残差≈1e-9 → 特异方差≈0 → Σ 近奇异，HK 实测 `0189.HK` 触发）。脚本已两层防护：逐日把 <3 只的行业折入「其他」、全窗恒零因子从模型剔除并列名——但正因如此，薄票池报告里出现 `factors_dropped_zero` 是预期行为。
6. **票池是「窗口末日成交额」事后选池**（含轻微选择偏差，非逐日动态池）——已写入每份报告 caveats。
7. **无显式市场因子**：行业因子吸收市场共同波动，等权组合里因子风险占比可高达 95~99%（CN/HK 实测）——这不是 bug，是模型结构使然；组合分散度低时该占比会被行业维度进一步放大（caveats 固定声明）。
8. **行业分类是静态/快照口径**（CN 通达信快照、HK akshare、US yahoo），期间调类不反映；跨窗口对比行业因子时留意快照期。
9. **HK/US 收益不复权**：不含分红的「价格收益」，与 CN 前复权口径不可直接比因子收益大小；报告已按市场标注。
10. **数据卫生计数看 caveats**：重复行剔除、停牌空收盘剔除、|日收益|>50% 剔除、因子缺列分区跳过——每项都有实测计数；为 0 才是「干净」，别默认干净。

## 脚本与参考

- `scripts/barra_risk.py`：模型引擎（纯标准库数值核心 + QuantDB 装配层；pandas/pyarrow/numpy 仅在 quantdb 模式加载）。
- `references/methodology.md`：方法论（暴露定义 / 截面 WLS / LW 闭式收缩 / EWMA 特异 / 风险分解 / Woodbury 极小方差 / 独立校验 / 校准记录）。
- `references/output-contract.md`：JSON 报告契约（全部键、demo 与 quantdb 字段差异、退出码语义）。

## 来源与许可

方法论移植自 [quantskills/skill-risk-model](https://github.com/quantskills/skill-risk-model)（**GPL-3.0-only**）：截面 WLS、Ledoit-Wolf 对角目标闭式收缩、特异风险 EWMA、风险分解与极小方差验证沿用其公式；数据层由 PandaData SDK 整体替换为 QuantDB 本地 parquet 直读（不携带源仓库的任何密钥/SDK 依赖），行业映射复用本仓 Brinson 技能口径。如对外分发本技能需遵循 GPL-3.0-only。仅限本地研究使用；分析结论不构成投资建议。
