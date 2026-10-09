---
name: backtesting-bias-avoidance
description: "回测偏差审计（QuantDB 直读 CN/HK/US 指数）— 把回测当作待验证的证据：前视偏差量化（干净 vs 泄漏）、CSCV 过拟合概率 PBO、DSR 多重检验校正、HAC 夏普显著性（t/95%CI/N_eff）、成本与冲击敏感性、样本外切分与年度走查。用户问「这个回测可信吗」「是不是过拟合/数据窥探」「前视偏差虚高多少」「参数网格挑出的最优样本外还行吗」「扣费后还剩多少」时使用。触发词：回测偏差、过拟合、PBO、DSR、前视偏差、数据窥探、多重检验、样本外、走查、夏普显著性、回测审计"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--sanity` / `--demo` / `--input` 只需 numpy（宿主 `/usr/bin/python3` 与容器均有），随处可跑；`--quantdb` 另需 pyarrow，**规范在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/backtesting-bias-avoidance/scripts/backtest_bias_audit.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/backtest_bias_audit.py --quantdb --market CN \
>      --symbol 000300.SH --start 2016-01-01 --end 2026-09-30 --cost-bps 3 \
>      --out /data/reports/backtest-bias-audit/cn_000300_2016_2026.json
>    ```
> 3. **报告落盘**：容器内 `/data/reports/backtest-bias-audit/<market>_<symbol>_<窗口>.json`；改参数重跑时保留前后两份并说明差异。
> 4. **symbol 格式**：一律**后缀式**且与 parquet 内一致——CN `000300.SH`、HK `HSI.HK`、US `SPX.US`；前缀式（`SH000300`）会**静默查空**，脚本对 <100 行的结果直接报错并提示。

# backtesting-bias-avoidance — 回测偏差审计

**默认假设优势是假的**，直到它扛过「无前视 + 样本外 + 扣费后」的检验。本技能只做审计与量化：不预测、不荐股、不给买卖话术。报告把「已证实的问题」与「缺失证据」分开——算不出的检验如实标注，不得以「没查出问题」冒充通过。

## 能力总览

| 用法 | 内容 |
|---|---|
| `--sanity` | **结构校准**（已知真值，确定性种子）：纯噪声试验矩阵 → PBO≈0.5；强信号矩阵 → PBO 低、DSR 高。退出码 0=通过 / 1=失准 |
| `--demo` | 合成 AR(1) 价格 + MA(快,慢) 网格全流程（前视量化 / 成本敏感性 / 年度走查 / 下注口径一应俱全） |
| `--input trials.csv` | 试验矩阵审计：每列一条策略的每期收益（列名=策略名；首列可为 `YYYY-MM-DD` 日期）。收益视为已含各自成本口径，本模式不再扣费 |
| `--quantdb --market CN\|HK\|US` | 本地真实指数 MA 网格全流程：IS 选优 → OOS 头条（HAC t/CI）→ PBO → DSR → 成本扫描 → 锚定走查 → 前视量化 |

共性输出（JSON）：`headline`（样本外·扣费后·HAC，含措辞纪律）、`pbo`、`dsr`、`look_ahead`、`walk_forward`、`cost_sensitivity`、`segment`（下注数/换手/在场占比）、`scan`（全网格 IS/OOS）、`findings`（confirmed-issue 与 missing-evidence 分列）。

## 数据映射（QuantDB 本地，2026-10-07 实测）

| 市场 | 路径 | 可用指数（实测全量） | 覆盖 |
|---|---|---|---|
| CN | `quantdb/1_kline_data/index_daily` | 41→30 个（`000300.SH` 沪深300=宽基；`000001.SH` 上证；风格类 `000016.SH` 等） | 2016-01-04 ~ 2026-09-30 |
| HK | `quanthk/1_kline_data/index_daily` | 仅 4 个：`HSI.HK` `HSCEI.HK` `HSCCI.HK` `HSTECH.HK` | 2013-08-20 ~ 2026-10-05 |
| US | `quantus/1_kline_data/index_daily` | 仅 5 个：`SPX.US` `NDX.US` `DJI.US` `IXIC.US` `SOX.US` | 2004-01-02 ~ 2026-09-17 |

列：`symbol,time,open,high,low,close,volume,amount,Category`；分区 `dt=YYYYMMDD/data.parquet`（≈8KB/日，全库 21MB）。**close=指数点位（价格指数，不含分红）**，量/额列本技能不使用。

## 标准流程

1. 先 `--sanity` 确认统计机械可信（应 exit 0）——校准不过就不要用后面的数字下结论。
2. 冻结设定：标的、窗口、参数网格、成本假设全部写进命令（`--fast 5:40:5 --slow 20:120:10 --cost-bps 3 --impact 0.0005`），不要依赖默认值下结论。
3. 跑审计并落盘 JSON；先用 `--demo` 复现流程再上真实数据。
4. 读报告：先看 `findings` 中 `type=confirmed-issue` 的 high/medium（每条附证据与触发阈值，逐条人工复核），再把 `type=missing-evidence` 整理成补证据清单——**不得当通过**。
5. 对外结论只引用 `headline` 口径；不显著时写「无显著净边际」，不写「策略无效」。PBO 低也不等于策略好（它只度量选择的稳定性）。
6. 人读版报告按 `references/methodology.md` 的蓝图组装（头条/设定/引擎/前视/过拟合/成本/绩效/审计清单/附录），JSON 是唯一机读产物。

## 常见坑（2026-10-07 实测标定）

- **单路径 PBO 方差极大，别拿一个数字当铁证**：1500 日合成序列（纯随机、无真实边际）+ 79 列 MA 网格，20 条独立路径的 PBO 中位 0.45、范围 **0.12~0.96**——单个 PBO 只是该分布的一次抽样，轻微越过 0.25/0.5 阈值不要过度解读；要分布就多路径重跑（合成路径可用 `--demo --phi 0.05` 换种子批量做）。
- **结构校准的参考值**（`--sanity`，种子 20261007，T=2000/N=64/S=10）：噪声→PBO 0.433、噪声里挑最优的 DSR 0.319（正确判不显著）；单列 +15bp/日强信号→PBO 0.000、DSR 1.000。若你的真实数据上 PBO/DSR 与直觉相反，先重跑 `--sanity` 排除机械问题。
- **前视虚高幅度由信号翻转频率决定，慢信号「数字小」不等于没有前视**：实测同框架下 CN `fast=30,slow=70`（换手 0.017/日）忘记滞后仅虚高 0.08（0.26→0.34）；US `fast=5,slow=20`（换手 0.051/日）虚高 0.63（0.97→1.60）。MA 类慢信号只在翻仓 bar 有泄漏收益，幅度天然小。
- **N_eff 可以大于 T**：MA 策略翻仓亏损造成负自相关，实测 CN 样本外方差膨胀因子 0.74 → `N_eff = 784/0.74 ≈ 1054 > 784`，属正常现象不是 bug；vif<1 时不要把 N_eff 当成「样本变多了」。
- **000300.SH 数据本身干净**：2016-01-04~2026-09-30 共 2611 个交易日，0 重复、0 缺失；最大间隔 11 天（2020-02-03，春节后疫情休市，非缺数据）。脚本仍带同日去重（保留最后一行）并在 `input_summary.dups_dropped` 计数。
- **HAC 带宽=选中慢线窗口**（input 模式用 Newey-West 自动带宽），上限 T/3——`--hac-lag` 是研究自由度，改它必须重报。
- **成本扫描实测**：CN/HK/US 三个指数在 0→10bp 内净夏普缓降、无穿零点（换手 0.008~0.05/日）；`--cost-bps` 要改变结论，需要换手高一个量级的策略。冲击项默认 0.0005 系风格化近似（无 ADV），`--impact 0` 可隔离纯线性口径。
- **DSR 试验数只含显式网格**（去重后列数，可 `--dsr-trials` 覆盖）；真实研究自由度更多 → **DSR 是显著性上界、PBO 是过拟合下界**，报告里必须原样转述。

## 脚本与参考

- `scripts/backtest_bias_audit.py`：自包含实现（numpy 数学 + pyarrow 读 QuantDB，无 pandas 依赖）。PBO(CSCV)/DSR/HAC/下注口径/成本扫描/走查/前视量化/校准，一次调用全出。
- `references/methodology.md`：方法论移植改写（引擎前视检查表、统计公式、成本/冲击、OOS/走查纪律、审计阈值、报告纪律）。
- `references/output-contract.md`：JSON 契约（status 语义、findings 两类、各模式可用段）。

## 来源与许可

方法论移植改写自 [quantskills/skill-backtesting-bias-avoidance](https://github.com/quantskills/skill-backtesting-bias-avoidance)。该仓库 GitHub license 元数据为 **NOASSERTION**（仓库内 LICENSE 又是截断的 GPL-3.0 声明文本，两者不一致，按未明确授权处理，**源仓库未授权再分发**）。本技能**仅做方法论改写、未复制源代码**，实现为本地独立重写（统计口径来自公开文献：Lo 2002；Bailey & López de Prado 2014）。数据层由在线行情改为本地 QuantDB 直读。仅限本地研究使用；分析结论不构成投资建议。
