# factor_research —— 因子研究（factor-lib-demo 改造版）

独立栏目「因子研究」（`/factor-research`）的后端：把开源项目
[fifamy/factor-lib-demo](https://github.com/fifamy/factor-lib-demo)（46 因子静态 Demo）
改造为 QuantMind 平台内的 82 因子多因子研究工作台，数据全部来自 QuantDB。

## 因子库（82 个，来源 data/factor_catalog.json @ 1c01589）

| 大类 (l1) | 小类 (l2) | 数量 |
|---|---|---|
| 市场交易信息 | 动量 / 波动 / 流动性 / Beta / 均值回复 / 价量 / 拥挤度 | 26 |
| 公司内生信息 | 估值 11 / 盈利能力 6 / 盈利质量 3 / 成长 9 / 财务稳健 10 / 市值 / 运营效率 5 | 45 |
| 投资者行为信息 | 分析师预期 6（✗ 缺数据） / 资金流 3（北向/主力，~ 待接入） / 筹码结构 3 | 12 |

- 可用性：快照构建后 `metrics.json.meta.n_factors_computed` 为准（当前 73 个可算，v1 缺 6 个分析师预期 + 3 个资金流）。
- 每个因子的方向（正/负）、公式、Wind 原始数据源见 `catalog.py`（提取自 demo 的 `factor_catalog.json`）。

## 口径（与 demo 对齐）

- **打分**：截面 pct rank → 正态分位（±4 截断），按 direction 调号 → 越高越好；
- **回测**：月末调仓、Top-N 等权、双边成本 0.2%（按换手计）、全 A 非 ST/退市池；
- **IC**：月频 Spearman（score vs 次月末收益）；排行榜综合分 = 0.5×|IC|分位 + 0.5×夏普分位；
- **价格**：daily_forward 前复权；amount 万元（换手 = amount×1e4/流通市值，名义基准一致）。

### 财报口径（实测确认，见 financials.py 头注释）

- `income` 是**单季值**（非 YTD）→ TTM = 最近 4 个连续单季求和；
- `cashflow` 是**年内累计（YTD）** → TTM = 上年年报 + 本年累计 − 上年同期累计；
- ⚠️ **income 的 `cost_of_goods_sold` / `net_profit_incl_min_int_inc` / `tot_profit` / `less_impair_loss_assets`
  在 QuantDB 中几乎全空（列存在但值为 None，2026-09-13 实测）**：
  - 毛利率/净利率/ROA/ROIC/存货周转/利息保障 → 改走 `pershare_index` 现成比率（实测满填充）；
  - 减值风险 → 用 `cashflow.asset_impairment_provision`（TTM）；
  - 应计 → 用归母净利近似；
- PIT：一律以 `m_anntime` 公告日做 as-of；同比/多年增长用「该日再往前 1/2/3 年的可见快照」；
- 亏损股 PE/扣非 PE、负现金流 PCF、负净资产 PB → NaN（rank 自然剔除）。

## 架构

```
catalog.py     82 因子元数据（代码生成自 demo JSON + 可用性标注）
data.py        QuantDB 读取（daily_forward/valuation/index/instrument）
engine.py      行情类 + 估值类因子 + 打分（rank→正态分位，Acklam ppf）
financials.py  PIT 财报面板（单季/YTD 双 TTM）+ 财务/行为因子
analysis.py    IC / Top-N 回测 / KPI / 相关矩阵 / 排行榜（构建脚本用）
scorecard.py   区间评分卡：名次面板 → 任意 N/任意区间的净值、KPI、超额、
               双标签（环境/时效）、综合分、持仓数扫描（服务层用）
store.py       快照读取（10 分钟 TTL 缓存）
service.py     目录/排行榜/单因子/对比/实时合成/最优权重
router.py      /api/v1/factor-research（engine 服务，经网关注册）
```

## 数据流

```
backend/scripts/build_factor_research.py        （构建，pandas，可宿主机裸跑）
    → <quantdb>/factor_research/*.parquet|json   （快照：打分/IC/净值/相关/KPI/持仓）
        · factor_panel.parquet   月末名次面板（每因子每期前 150 名：symbol/score/raw/fwd_ret）
        · stock_snapshot.parquet 最新截面个股元数据（名称/申万行业/市值/PE/PB/近一年日均成交额）
        · benchmarks.parquet     沪深300 / 中证800 / 中证500 净值（index_code 区分）
    → router.py 读取（compose/optimal 在线现算）  → 前端 features/factor-research/
```

构建命令（仓库根，全量约 25–45 分钟）：

```bash
python3 backend/scripts/build_factor_research.py                 # 2020-01 至今
python3 backend/scripts/build_factor_research.py --smoke 200     # 冒烟（可用 FACTOR_RESEARCH_OUT 改临时输出目录）
python3 backend/scripts/build_factor_research.py --skip-financial
```

## 区间口径（工作台）

- 区间（全部/近3年/近1年/各年/自定义）只影响**切片与重算**：净值在区间内重建（首月末=1.0，
  首月换手 100% 全额计费）；超额 = 组合年化 − 基准年化（demo 口径）；
- 综合分 = 0.5×有效性 z(mean(z(RankIC), z(IC_IR))) + 0.5×业绩 z(mean(z(年化), z(夏普), z(−回撤), z(月胜率)))，
  分项在全因子截面标准化——**换区间会整体重算**；
- Fitness（世坤 BRAIN 口径，**单列不并入综合分**）= IR × √(|Returns| / max(Turnover, 0.125))。
  公式只在 `factor_report.metrics.fitness_of` 实现；工作台月频序列按日频口径折算
  （Returns=月均毛收益×12、IR=月均/标准差×√12、Turnover=月均单边换手÷21），
  毛收益由净收益还原（`gross = net + to×COST_RATE`，成本只经换手项计一次）。
  五档分级见 `metrics_core.FITNESS_GRADES`。⚠️ 月频组合日均换手通常 <0.125 地板，
  换手项多数不参与区分（Fitness 近似 夏普×√(|年化|/0.125)）；粗筛分，不是验收门槛，
  做过参数搜索的因子仍须补 DSR/PBO；
- 标签（每个因子 2 个，随区间重算）：市场环境（沪深300 滚动 3 月分牛/熊/震荡 → 三环境月均超额 z 取最高，
  均 <0.5 为全天候型）；时效（近 12 月 RankIC 与区间全样本差 ±0.012，双低为持续低效）；
- 最优权重：非负、和为 1 的粗网格（≤900 组，目标=夏普/年化/超额），在「各因子月末前 150 名的并集」
  候选池上评估；应用后由 compose 全样本精确回测。

## API 端点（网关 :8000 → engine :8001）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/factor-research/catalog` | 因子目录（含分类顺序/基准）+ 快照元信息 |
| GET | `/api/v1/factor-research/leaderboard?start=&end=` | 排行榜：区间内全列 KPI/超额/RankIC + 双标签 + 综合分 |
| GET | `/api/v1/factor-research/factor/{code}?ns=5,10,30&start=&end=&stocks_n=` | 单因子：多档持仓数净值/KPI/超额 + N 扫描 + 个股表 + 行业/市值分布 |
| GET | `/api/v1/factor-research/compare?codes=a,b&start=&end=` | 对比（兼容形态，每因子 Top-30） |
| POST | `/api/v1/factor-research/compare` | 对比：`{items:[{code,n}], start, end}`（每因子独立持仓数） |
| POST | `/api/v1/factor-research/compose` | 合成：`{weights, top_n, thresholds?, threshold?, start?, end?}` |
| POST | `/api/v1/factor-research/optimal-weights` | 网格搜索最优权重：`{codes, top_n, start?, end?}`（夏普/年化/超额各一组） |
| GET | `/api/v1/factor-research/screening` | 筛选结果（保留清单/去重剔除/门槛剔除） |

> valuation 的 TTM 字段（net_profit/revenue/pe/ps）若在源侧回归中损坏，可本地兜底：
> `python3 backend/scripts/repair_valuation_ttm.py --survey 20260101`（体检）/ `--apply-from 20260101`（逐格修复）。

## 因子筛选（质量门槛 + 同源去重 · 五库联合）

```bash
python3 backend/scripts/screen_factors.py            # 默认 |IC|≥0.02 · |ICIR|≥0.2 · 去重 |ρ|≥0.9
python3 backend/scripts/screen_factors.py --skip-cross   # 只用库内矩阵（快，无跨库）
python3 backend/scripts/screen_factors.py --libraries alpha_library,tdxgs   # 只筛部分库
```

- 参与库：`alpha_library`(429) ∪ `tdxgs`(88) ∪ `jq110`(109) ∪ `alpha360`(360)
  ∪ `l1_factors`(110) ∪ `l2_factors`(211，2022 起，缺分区日期按无数据成对排除)
  ∪ 本模块(73)，共 1380（`l1_l2_factors` 为 L1/L2 拼接，不重复入筛）；
- 联合相关 = 各库分区 × 本模块月末打分截面 Spearman 均值（81 期，缓存 `screening/cross_corr.npz`，
  1380 列重算 ~27 分钟，`--refresh-cross` 强制）；
- **去重 = 贪心直接去重**：候选按 |ICIR| 降序，与已保留因子直接 |ρ|≥阈值 才剔除（不用
  并查集传递闭包——曾把链式中等相关误并成 318 成员巨簇）；
- 输出 `screening/`：`factor_selection.json`（kept/剔除原因/同源组）、`筛选报告_YYYYMMDD.md`、
  `kept_features.txt`（训练特征清单，一行一个）；前端「筛选」页签可视化同一份；
- 2026-09-14 全库首跑：1380 → 门槛后 874 → **保留 362**（512 个直接重复、110 组）。
  跨库同构精准命中：`MA20BIAS ≡ a158_MA20 ≡ JQ110_MAC_020 ≡ mom_ma_gap_20`（ρ=1.0）、
  `MOM20 ≡ a158_ROC20 ≡ JQ110_ROC_020 ≡ mom_ret_20d`（ρ≈1.0）、`amt_ma_20 ≡ AMOUNT20`
  （L1，ρ=1.0）、`amt_ratio_1_5 ≡ a158_VMA5`（ρ=0.996）等。
  保留按库：Alpha 库 134 / Alpha360 72 / L2 58 / 因子研究 31 / JQ110 29 / TDXGS 21 / L1 17。

## 测试

```bash
python3 -m pytest backend/tests/test_factor_research.py -q
```

## 与「因子报告」的差异

`factor_report`（技能中心页签）是 Alphalens 式因子体检（分位收益/IC/换手/相关），覆盖
`alpha_library / l1 / l2` 数据集；本模块是**可交互的多因子研究**（排行榜→单因子→对比→合成），
覆盖 demo 的 82 因子（含财务/行为类），二者互补。

> 免责声明同项目根 CLAUDE.md：仅供学习研究，不构成投资建议。
