---
name: factor-orthogonalization
description: "因子正交化 / 中性化（QuantDB 直读 CN/HK/US）— 对称正交 Löwdin、Gram-Schmidt、行业+规模+风格基准逐日截面回归残差化（FWL 等价实现）；输出相关矩阵清零、暴露清零、IC 保真（保留率）与独立校验（含 numpy 对拍）。用户问「这几个因子长得太像」「帮我去掉行业和市值暴露」「中性化这个因子」「要残差因子」「这个因子是不是旧因子的变体」「正交后 IC 还剩多少」时使用。触发词：因子正交化、因子中性化、行业中性、市值中性、残差因子、Löwdin 正交、Gram-Schmidt、因子相关性去重、风格暴露剥离、基准暴露清零"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` 为纯标准库，宿主机/dsh/Windows 可直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**。容器内 pyarrow 读 parquet 偶发解释器退出崩溃（会落 ~2GB core 文件），执行一律关 core：
>    ```bash
>    docker cp skills/factor-orthogonalization/scripts/orthogonalize.py quantmind:/tmp/
>    docker exec -w /app quantmind bash -c 'ulimit -c 0 && python3 /tmp/orthogonalize.py --quantdb --market CN \
>      --factors ma_gap_20,rsi_14,vol_std_20,dividend_rate --start 2024-01-01 --end 2025-12-31 --top-n 300 \
>      --out /data/reports/factor-orthogonalization/cn_2024_2025.json'
>    ```
> 3. **报告落盘**：容器内 `/data/reports/factor-orthogonalization/<market>_<窗口>.json`（容器写出为 root 属主，删除用 `docker exec`）；stdout 同时输出中文表格。改口径/窗口重跑时保留前后两份 JSON。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式会静默查空）。

# factor-orthogonalization — 因子正交化 / 中性化工具箱

回答三类任务：**因子之间太像（相关性去重）？因子带着行业/市值/风格暴露（基准中性化）？新因子是不是旧因子的变体？** 输出正交后因子矩阵的诊断（暴露清零 + IC 保真）——不构造因子、不回测组合、不喊单；暴露清零是回归恒等式，IC 保真是历史证据，都不是未来表现的承诺。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` | 纯标准库：10 条手算金样（2×2 Löwdin 闭式 / Gram-Schmidt 闭式 / FWL 4 股手算残差 β=1.6 / 正交性断言）+ 确定性合成面板三方法演示 |
| `--quantdb --market CN` | 因子与规模列直读 `features_daily`（默认）/`l1_factors`/`l2_factors`；行业=instrument_detail `rs_hyname`；收益用 `daily_forward` 前复权自算 |
| `--quantdb --market HK/US` | 直读 `l1_factors`；HK 双来源自动去重、市值列不可用时自动退回对数成交额代理；行业=akshare_profile / sector |
| `--quantdb --list-columns` | 列清单 + 最新分区填充度（非空计数 / nunique，⚠ 标注常量/未填充列），选因子前先跑 |

方法三选（可同跑对比）：**sym** 对称正交 Löwdin（相关矩阵清零、顺序无关、输出为线性混合）；**gs** Gram-Schmidt（顺序敏感、首因子原样保留，适合「主因子+增量」）；**resid** 行业+规模+风格基准逐日截面回归残差化（逐因子独立、保留语义）。输出（每因子）：正交前后相关矩阵（逐日日均 + max\|非对角\|）、基准暴露（规模 corr / 行业 R² / 行业 max\|z 组均值\|，前后对比）、IC 保真（原始 vs 各方法，同池同行 + 保留率）、逐日处理计数、独立校验（相关矩阵恒等 / 残差零暴露 / FWL vs numpy 对拍）；JSON 报告 + stdout 表格。

## 本地数据映射（QuantDB）

| 市场 | 因子面板（`6_ml_datasets/`） | 规模代理 | 行业分类 | 收益源（`1_kline_data/`） |
|---|---|---|---|---|
| CN | `features_daily`（默认）、`l1_factors`、`l2_factors` | `ln(float_mv)`（实测全填充） | instrument_detail `rs_hyname` 128 类（静态快照） | `daily_forward`（**前复权**） |
| HK | `l1_factors`（190 列） | **自动退回** `ln(20 日均成交额)`（市值列全 0 未填充） | akshare_profile「所属行业」31 类 | `daily_forward`（**不复权**，双来源去重） |
| US | `l1_factors`（176 列） | `ln(ln_mv_total)`（实测全填充） | sector 11 类（`--industry-level industry` 更细） | `daily_forward`（**不复权**） |

- **收益定义**：`ret_h(t) = close(t+h)/close(t) − 1`，h∈{1,5}（`--horizons` 可改），h 为同一标的序列的后续第 h 个交易日；读取范围自动向窗口末端之后多取 40 个交易日。CN 前复权 / HK、US 不复权（口径写入报告）。
- **票池**：窗口末日成交额降序前 `--top-n`（默认 300；0=全部），或 `--symbols` 显式指定（事后选池，含轻微选择偏差）。
- **标签列护栏**：`return_{n}d` / `future_return_{n}d` / `label_return` 是内置未来收益标签，脚本拒绝当因子或控制变量（防前视泄漏），命中即报错退出。
- **规模代理 auto 体检**：按市场候选列抽查 3 个分区（非空 ≥80% 且零值 ≤20% 才采用），不合格自动退回对数成交额代理并在报告 caveats 说明；`--size-col` 可显式指定（显式指定但体检不合格会报错，防静默吃 0 值）。
- **残差最终标准化**：默认 `--resid-final-scale affine`（纯仿射 z-score，严格保零暴露到 1e-15）；`winsorize_zscore` 为源技能口径（截尾非线性，会重新引入暴露，见坑 7）。

## 标准流程

1. `--list-columns` 探查列名与填充度，避开常量/未填充列（HK/US 大量基本面列为全 0）；注意它只反映最新分区，跨年窗口还需抽查窗口内分区。
2. 明确要剥离什么：行业（默认 `rs_hyname`/akshare_profile/sector）、规模（auto 代理）、风格（`--controls`，如 CN `beta_20`）；不需要行业时加 `--no-industry`。同一列不能既当因子又当控制（脚本报错）。
3. 跑正交化：选方法（库去重=sym；主因子+增量=gs；基准中性=resid；默认三法同跑对比）；窗口建议 ≥252 交易日，多因子逗号分隔一次跑完（共享同一收益面板）；`--gs-order reverse` 做顺序敏感性检查。
4. 读报告的顺序：先 `self_checks`（sym max\|非对角\|、残差 \|corr\|、numpy 对拍是否通过）→ `day_counts`（覆盖与跳过原因）→ `exposure`（暴露是否清零）→ `ic.retention`（保真）——顺序颠倒容易被 IC 数字带偏。结论须标注数据窗口、票池口径、观测数。
5. 残差 IC 下降是预期行为（剥离的暴露若含收益预测成分，残差必降）；保留率低不等于因子失效，独立性与保真是两个维度。

## 实测校准（2026-10-08，CN features_daily，2024-01-01~2025-12-31，485 个交易日，top-300@20251231，每日截面中位 290 只）

基准：行业=`rs_hyname`（128 类，覆盖率 100%）；规模=`ln(float_mv)`；无风格列。

**相关矩阵（Löwdin sym，全窗口日均）：**

| 因子对 | 正交前 | sym 后 | gs 后（asis 序） |
|---|---|---|---|
| ma_gap_20 ~ rsi_14 | **+0.8615** | ~0 | −0.0004 |
| ma_gap_20 ~ vol_std_20 | +0.2227 | ~0 | −0.0009 |
| vol_std_20 ~ dividend_rate | **−0.4073** | ~0 | −0.0031 |

**基准暴露（全窗口日均）：**

| 因子 | 规模 corr 前→后 | 行业 R² 前→后 | 行业 max\|z 组均值\| 前→后 |
|---|---|---|---|
| ma_gap_20 | +0.032 → 8e-19 | 0.382 → 1.5e-32 | 2.73 → 0 |
| rsi_14 | +0.080 → ~0 | 0.451 → 0 | 2.53 → 0 |
| vol_std_20 | −0.298 → ~0 | 0.437 → 0 | 2.62 → 0 |
| dividend_rate | +0.389 → ~0 | 0.559 → 0 | 2.65 → 0 |

**IC 保真（秩 IC，原始与残差在完全相同的一天同一批行上各算一次；括号=残差保留率）：**

| 因子 | H1 原始→残差（保留） | H5 原始→残差（保留） |
|---|---|---|
| ma_gap_20 | −0.0313 → −0.0296（0.95） | −0.0387 → −0.0395（1.02） |
| rsi_14 | −0.0346 → −0.0291（0.84） | −0.0402 → −0.0358（0.89） |
| vol_std_20 | −0.0370 → −0.0299（0.81） | −0.0500 → −0.0452（0.91） |
| dividend_rate | **+0.0159 → +0.0077（0.48）** | +0.0205 → +0.0089（0.43） |

加风格（`--controls beta_20`）：vol_std_20 的 beta 暴露 +0.359 → 0，H1 保留率 0.81→0.78；dividend_rate 0.48→0.41（红利因子的收益更多绑在 beta 方向上）。

**独立校验（三市场全部通过）：**

| 校验项 | 预期 | 实测 |
|---|---|---|
| sym 后相关矩阵 max\|非对角\| | ≤1e-12 | CN 2.2e-14 / HK 8.4e-14 / US 6.5e-14（485/246/420 日逐日最大） |
| gs 后 max\|非对角\|（顺序敏感对照） | — | CN 0.092 / HK 0.053 / US 0.042（最坏单日）；日均 0.002~0.004 |
| 残差-规模 \|corr\|（标准化前） | ≤1e-12 | CN 1.2e-15 / HK 5.0e-16 / US 6.9e-16 |
| 残差-规模 \|corr\|（**最终产出**，affine） | ≤1e-12 | CN 1.08e-15（日均 8.1e-17）/ HK 4.6e-16 / US 6.7e-16 |
| FWL 残差 vs `numpy.linalg.lstsq`（显式哑变量矩阵，SVD） | — | max\|diff\| CN 4.4e-14 / HK 6.2e-13 / US 1.5e-13，均通过 |
| `--demo` 手算金样 | 全过 | **10/10**（含 2×2 Löwdin 闭式 a=1.185854、GS 闭式、4 股 2 行业手算残差 β=1.6） |

跨技能互证：本工具全样本原始 IC（H1：ma_gap_20 −0.0314、rsi_14 −0.0346、vol_std_20 −0.0370、dividend_rate +0.0159）与 factor-ic-decay 标定表**逐位一致**——两套独立实现、同一数据口径互证。

**HK 冒烟**（2025 全年，246 日，top-200@20251231，截面中位 133 只）：行业=akshare_profile 31 类；规模=对数成交额代理（`ln_mv_total` 体检不通过，见坑 3）；3 个低填充日自动跳过（20250128/20251224/20251231）；双来源去重 483,737 行。mom_ret_5d H1 保留 0.87；rsi_14 / liq_turnover_os 原始 IC 本身 ≈0（|IC|≈0.003，top-200 池），保留率按口径不给出。

**US 冒烟**（2025-01-01~2026-09-10，420 日，top-300@20260910，截面中位 295 只）：行业=sector 11 类；规模=ln(ln_mv_total)。保留率 0.10~0.91：liq_turnover_os H1 仅 0.10——换手类因子的信息基本位于被剥离的规模/流动性方向里。

## 常见坑（2026-10-08 实测标定）

1. **features_daily 列集两次断点**：2026-09-14 换列（−2/+30，50→78 列，新增 `industry_name`/`industry_code`/`sector_code` 128 类）、09-21 标签整体改名 `return_{n}d`→`future_return_{n}d`——脚本逐分区读取、缺列分区跳过并计数，两类标签名都在黑名单；窗口跨越断点的分区统一走 instrument_detail 行业源（2024-2025 分区的 features_daily 没有行业列）。
2. **行业分类是静态快照**：`instrument_detail.parquet` HqDate=20260720（128 类），行业重分类不回填历史——存在分类层面的前视/陈旧偏差，与 brinson-performance-attribution 同源同口径，报告 caveats 已注明。
3. **HK `l1_factors` 市值列不可用**：turnover_rate/pe_ttm/pb/roe/bp/ep_ttm/float_mv/total_mv 全 0（nunique=1）；`ln_mv_total` 在 2025 各分区约 **1/3 标的为 0**（dt=20251231 分区 100% 为 0），直到最新分区 dt=20261005 起才全填充——auto 体检因此退回对数成交额代理（规模+流动性混合口径，自动写入 caveats）。
4. **HK `daily_forward` 双来源重复行（2024-09-02 ~ 2026-05-08）**：2025 窗口 + 前视尾部实测去重 **483,737 行**；必须按 `published_at` 排序 keep='last'（保 akshare 原始价；paid_hk 个别标的带 ×3/×10 复权缩放），不能按行序 keep='first'。
5. **HK 因子列低填充日**：20250128 / 20251224 / 20251231 整列未填充（style_beta_20/vol_std_20 仅 8~9 只有值）——当日自动跳过并计入 `day_counts`；抽查填充度必须覆盖窗口内分区，只看最新分区会误判。
6. **HK/US `l1_factors` 无日期列**：日期取分区名 `dt=`；US 因子分区进度落后行情（因子止于 2026-09-17，行情到 2026-10-06）——脚本自动把 `end` 收敛到两者较早者并在 stdout 提示。
7. **源技能的残差最终 winsorize_zscore 会破坏零暴露**：截尾是非线性操作，实测（CN 2024-11-01~12-31 首跑）个别交易日把 ≤7.4e-2 的规模相关重新引入最终产出，而标准化前的残差是 1.1e-15——本工具默认改为纯仿射 z-score（`affine`，最终产出 ≤1.1e-15）；需要逐位复刻源实现时才加 `--resid-final-scale winsorize_zscore`（报告会自动附口径 caveat）。
8. **Gram-Schmidt 顺序敏感 + 近共线日数值不稳**：首因子原样保留（demo 里 F1 的 gs IC 与原始逐位相同），后续因子只保留增量（F2 gs IC 从 0.32 掉到 0.008 是顺序效应而非 bug）；当日因子近共线时最坏单日 max\|非对角\| 可达 ~0.09（CN）——相关去重优先用 sym（顺序无关），gs 是「主因子+增量」的专门工具，并用 `--gs-order reverse` 做敏感性检查。
9. **行业 R² 有「哑变量个数 / 截面宽度」基数效应**：K 个类别在 N 只股票上的期望 R²≈(K−1)/(N−1)——CN 128 类 / 300 只的基线就有 ≈0.43，所以 R²=0.45 不代表因子带行业暴露，**看前后差与 max\|z 组均值\|**；本工具用中心化 R²（不中心化的版本会把因子均值算进解释力，实测出现 0.985 的假 R² 并已修正）。
10. **容器里 pyarrow 读 parquet 偶发解释器退出崩溃**（`terminate called without an active exception` → 落 ~2GB core 文件，曾撑爆根分区）——所有 `docker exec` 一律 `bash -c 'ulimit -c 0 && python3 …'`；定期 `ls /app/core.*` 检查。

## 脚本与参考

- `scripts/orthogonalize.py`：正交化引擎（纯标准库数值核心：Jacobi 特征分解 / MGS / FWL 残差 / 高斯消元；pandas/pyarrow/numpy 仅在 quantdb 模式延迟加载，缺三方库时 `--demo` 与 import 均不受影响）。
- `references/methods.md`：三种方法的数学定义、预处理与诊断口径公式、接受标准、源技能反模式清单、JSON 报告契约。
- `references/source-boundary.md`：数据边界与不做的事（本地版）。

## 来源与许可

方法论移植自 [quantskills/skill-factor-orthogonalize](https://github.com/quantskills/skill-factor-orthogonalize)（**GPL-3.0-only**，Copyright (C) 2026 QuantSkills）。本地化改写：数据层由 PandaData/API 换为 QuantDB 直读；源的 qsh-form/validate 脚本与 `panda_data` 依赖未移植；接口由 factor-dir 批处理改为 CLI（`--demo` / `--quantdb`）；新增对称正交（Löwdin）与 Gram-Schmidt 两条方法线、FWL 等价残差实现 + numpy 对拍、手算金样自检。仅限本地研究使用；分析结论不构成投资建议。如对外分发本技能需遵循 GPL-3.0-only。
