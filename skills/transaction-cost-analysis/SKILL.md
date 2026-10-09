---
name: transaction-cost-analysis
description: "交易成本分析（TCA）：IS 五分解（延迟/冲击/择时/机会/费用）+ VWAP/TWAP/到达价基准对标与超越基准率 + 参与率-√滑点校准；本地 QuantDB 分钟数据直读（CN）或自备成交明细（CN/HK/US）。用户问「滑点是多少」「成交质量好不好」「交易成本多少 bps」「执行得比 VWAP 好还是差」「冲击成本多大」时使用。触发词：交易成本、实施缺口、IS 分解、滑点、VWAP 基准、TWAP、到达价、市场冲击、参与率、成交复盘"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**（先拷贝再执行）；`--demo`/`--input` 为纯标准库，可在宿主机/dsh/Windows 直接跑：
>    ```bash
>    docker cp skills/transaction-cost-analysis/scripts/tca.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/tca.py --quantdb --symbol 600036.SH \
>      --date 2026-08-25 --out /data/reports/transaction-cost-analysis/cn_600036.SH_2026-08-25.json
>    ```
> 3. **报告落盘**：容器内 `/data/reports/transaction-cost-analysis/<market>_<SYMBOL>_<date>.json`；stdout 同时输出中文表格。改参数重跑时保留前后两份 JSON。
> 4. **symbol 格式**：CN 后缀式 `600036.SH`/`000001.SZ`（HK 四位 `0001.HK`、US 大写 Ticker `NVDA` 仅用于 --input）；前缀式（如 SH600036）会静默查空。

# transaction-cost-analysis — 交易成本分析与成交复盘

回答三个问题：**这笔交易贵不贵？贵在哪一项？执行得比基准好还是差？** 只做执行后复盘——不预测、不喊单、不下单。所有 bps 以「订单名义额@决策价」折算，正=对本方不利。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` | 合成明细演练：IS 五分解 + 手算对账（12 项，Fraction 精确复算），纯标准库 |
| `--input <csv>` | 成交明细复核：决策价/到达价/成交价量/费用逐笔分解；可选 `--bars` 分钟线回填到达价/期末价/σ/ADV；`--calibrate-k` 拟合参与率-滑点 |
| `--quantdb --symbol --date` | 某票某日一键复盘（**仅 CN**）：全日/连续 VWAP、TWAP、09:25 到达价、半小时量占比、到达后均匀执行成本估计、与日线口径核对；`--qty` 加执行规模冲击、`--tick-check` 对真实盘口 |

输出（每单）：五分解 bps（延迟/冲击/择时/机会/费用）+ 合计 + 对账残差（恒为 0）；逐笔 × VWAP/TWAP/到达价滑点与超越基准率（按成交额加权）；JSON 报告 + stdout 中文表格。

## 本地数据映射（QuantDB）

| 数据 | 路径 | 覆盖 / 备注 |
|---|---|---|
| CN 分钟线 | `quantdb/1_kline_data/min1_kline/<SYM>.parquet`（5289 只） | `time,open,high,low,close,volume,amount,volinstock,forwardfactor`；**价格=不复权原始价，volume=股，amount=万元**（装载时 ×1e4 归一） |
| CN 分钟线（5 分钟） | `quantdb/1_kline_data/min5_kline/` | 仅 2026-07-20 ~ 07-24 五天（`--freq 5`） |
| CN 日线（对账用） | `quantdb/1_kline_data/daily_unadjusted/dt=<YYYYMMDD>/data.parquet` | 分钟价格与其一致；amount 亦为万元 |
| 真实盘口 tick | `quantdb/1_kline_data/tick_data/<SYM>_<date>.parquet` | 仅 20260511 / 20260720 两个日期（含 5 档盘口；`--tick-check`） |
| HK / US | quanthk / quantus 仅有日线 | **无分钟数据** → 只能 `--input` 自备明细；`--quantdb` 对港美股报明确错误 |

## 标准流程

1. **成交明细复核（`--input`）**：准备 CSV——必填 `symbol,side,datetime,price,qty`；可选 `order_id,decision_price,arrival_price,end_price,order_qty,adv,sigma_day,fees_bps,benchmark_price`（多笔同 `order_id` 或同 symbol+side+日期 自动聚为一张单）。有分钟线给 `--bars`（`symbol,datetime,open,high,low,close,volume[,amount]`；**amount 须与价格同量纲**——QuantDB 万元要先 ×1e4，脚本会做量纲体检告警）。
2. 先看 `warnings` / `degraded`：滑点 >500 bps 的量纲/口径告警、到达价/期末价缺省代理都在这两处；未解释的降级不要出结论。
3. 读五分解：残差须为 0（恒等式自检）；「择时」是残差项（含行情漂移+冲击模型误差），不要单独当执行员水平引用。超越基准率应结合当日行情方向解释。
4. **某票某日一键复盘（`--quantdb`）**：核对段 `vwap_in_low_high` 必须 ✓、量/额偏差须在浮点舍入量级；再读半小时桶与均匀执行场景；给定 `--qty`（股）才有参与率与冲击项。
5. 报告落盘 `/data/reports/transaction-cost-analysis/`，`--out` 处建议带 market/标的/日期/参数标记。

## 实测校准（2026-10-08）

**demo 手算对账（Fraction 精确，12/12 一致）**：买入单 延迟 40.0000 / 冲击 3.5777 / 择时 36.4223 / 机会 40.0000 / 费用 2.1008 / 合计 122.1008；卖出单 25.0000 / 5.5902 / 9.4098 / 0 / 7.5696 / 合计 47.5696；两份残差均为 0；逐笔滑点 0.0 / 11.5645 / 19.7824 bps，加权 9.297 bps，超越基准率 37.4%。

**QuantDB 真实标定（3 个交易日 × 3 只标的，VWAP 必须落于当日 low~high 且与日线 amount/volume 核对）**：

| 标的/日期 | 分钟 bar | VWAP(分钟) | VWAP(日线) | 量偏差 | 额偏差 | ∈[low, high] |
|---|---|---|---|---|---|---|
| 600036.SH 2026-08-25 | 239 | 39.612873 | 39.612875 | +3.2e-06% | −2.9e-06% | ✓ [39.42, 39.92] |
| 600036.SH 2026-07-09 | 263（24 根 15:06–15:30 盘后） | 37.555891 | 37.555887 | −3.9e-06% | +6.0e-06% | ✓ |
| 600036.SH 2026-07-20 | 239 | 38.603075 | 38.603079 | +4.9e-06% | −5.4e-06% | ✓ [37.89, 38.91] |
| 000001.SZ 2026-07-20 | 241（14:58 重复量） | 10.932533 | 10.932533 | −4.5e-06% | −6.1e-07% | ✓ |
| 601398.SH 2026-08-25 | 239 | 7.906860 | 7.906860 | +5.1e-06% | +2.9e-06% | ✓ |

（偏差为浮点舍入量级；600036.SH 08-25 场景示例：漂移 −4.34 / 冲击 +0.59（参与率 0.343%）/ 费用 +2.60，合计 −1.15 不含点差、+3.91 含代理点差 bps。）

**`--input` 真实价格回放（600036.SH 2026-08-25，6 笔 TWAP 式成交 6.9 万股）**：延迟 −30.2262 / 冲击 0.3624 / 择时 −0.2534 / 机会 0 / 费用 2.5922 / 合计 −27.5250 bps，残差 0；平均滑点 5.4091 bps、超越基准率 43.4%。另以独立 pandas 实现（不复用工具函数）重算 16 项关键数字 + 6 笔逐笔滑点，**逐项一致**。校准路径实测：参与率 [4.73e-05, 3.31e-04] → slope 198.33 bps/√参与率、截距 6.02、**R²=0.0319**——回放样本的滑点被行情漂移主导，implied k 不可解释（工具按纪律照报 R²/区间并警示，全同量样本判「拟合退化」拒绝拟合）。

**点差代理校准**：600036.SH 2026-07-20 真实盘口 4740 条快照，报价点差中位 2.588 bps（半价差 1.294）；分钟 H-L 半幅代理中位 6.473 bps → **代理 ≈ 5.0× 高估**，只作相对比较。

## 常见坑（2026-10-08 实测标定）

1. **分钟数据是不复权原始价**（min1 收盘 == `daily_unadjusted`，≠ `daily_forward` 前复权）：与复权日线混用会静默错 5%+（口径双重缩放陷阱）。单位：volume=**股**、amount=**万元**；订单量按「股」，用「手」输入须自行 ×100（脚本不猜单位）。
2. **CN 标准日 239 根**：09:25 竞价 1 + 09:31–11:30 共 120 + 13:01–14:57 共 117 + 15:00 竞价 1。异常 bar：深市 **14:58/14:59 重复**（000001.SZ 2026-07-20 实测 14:58 重复 11,300 股、14:59 零量）、沪市 **15:06–15:30 盘后固定价格交易**（600036.SH 2026-07-09 实测 24 根，不进日线）。二者均列为 anomalous：展示但不计入对账（14:58 重复计入会把量对出 2 倍）。
3. **H-L 半幅点差代理高估 ~5×**；tick 快照只有 20260511/20260720 两天，日常复盘用代理须按「上限」口径标注（`--tick-check` 有数据时自动给倍数）。
4. **HK/US 无分钟数据**（quanthk/quantus 仅日线）——`--quantdb` 对港美股直接报错；港美股成交复核用 `--input` 自备明细与基准价。min5 本地也仅 2026-07-20~24 五天，`--freq 5` 会因缺日报错（错误信息带覆盖范围）。
5. **费用是常量假设不是数据**：`broker`=万2.5（真单口径，默认）/ `matching`=万3（回测撮合，刻意保守）两档并存；CN 卖出另加印花税 5 bps、过户费 0.1 bps；港美股结构未内置（`--commission-bps` 自行给）。引用报告必须带口径。
6. **校准纪律**：全同参与率样本拒绝拟合（含解释器浮点 1-ulp 噪声的退化判据，Py3.10/3.12 行为一致）；R² 低的 implied k 不要引用；勿外推到观测参与率区间之外。
7. **HK `daily_forward` 双来源重复行**，边界精确为 2024-09-02 ~ 2026-05-08（akshare+paid_hk 两行/标的/日）：自备 HK 日线估 σ/ADV 时按 `published_at` 最新去重（keep='last'）；`daily_backward` 已损坏，任何收益/波动计算都不要用它。
8. **缺失代理链**：半日市/停牌导致分钟 bar 缺失时，到达价/期末价取最近 bar、ADV 按覆盖比例外推——全部进 `degraded`，结论必须带上代理说明。

## 脚本与参考

- `scripts/tca.py`：分析引擎（纯标准库统计核心 + QuantDB 装配层；pandas/pyarrow 仅在 `--quantdb` 模式延迟导入）。
- `references/methodology.md`：方法定义（五分解公式与符号、费用常量、基准约定、√冲击与校准纪律、会话窗口与对账恒等式、局限）。
- `references/output-contract.md`：JSON 契约（schema `quantmind-tca/1`，三种模式的字段表、舍入与正负号约定、质量字段语义、退出码）。

## 来源与许可

方法论移植自 [quantskills/skill-transaction-cost-analysis](https://github.com/quantskills/skill-transaction-cost-analysis)（**GPL-3.0-only**），并合并兄弟仓库 [quantskills/skill-transaction-cost-calibration](https://github.com/quantskills/skill-transaction-cost-calibration)（**GPL-3.0-only**）的参与率-√滑点校准。本地化改写：数据层由 PandaData SDK 换为本地 QuantDB 直读（CN 分钟线；HK/US 走 `--input`），源的 qsh-form 声明与 HTML 报告未移植，输出改为 JSON + stdout 中文表格；IS 五分解可溯源到 Perold (1988)。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
