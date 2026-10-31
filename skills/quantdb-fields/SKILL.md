---
name: quantdb-fields
description: "QuantDB 字段单位速查手册 — 全部数据集实测验证的单位、口径与陷阱（个股 volume=股/amount=万元、指数 volume=手、L2原始逐笔 l2_data/tick_data、technical % vs l1 小数、dividend_rate 百分数、PG 前缀 symbol）。用 QuantDB 数据做分析/回测/报告、判断成交量/成交额/股息率/换手率单位、读逐笔委托/成交/十档盘口、排查数据口径不一致时使用。触发词：字段单位、成交量单位、成交额单位、股还是手、万元、股息率、数据口径、L2、逐笔、委托、成交明细、十档盘口、tick_data、wind"
---

> ⚙️ 本技能遵循公共运行环境契约（最高优先级，先于本文其余内容执行）：
> 详见 [_shared/env-contract.md](../_shared/env-contract.md)，执行前先读它。

# quantdb-fields — QuantDB 字段单位速查手册

> 用 QuantDB 数据做任何分析/回测/报告前**必读**。所有单位均为
> **2026-08 直接读本地 parquet 实测验证**（不是照抄文档）。
> 单位搞错的后果：成交额差 1 万倍、股息率差 100 倍、换手率差 100 倍——
> 分析结论全部作废。

## 一、通用规则（先记这个）

| 规则 | 说明 |
|---|---|
| **个股 OHLC = 元** | 开高低收统一为人民币元（与市值同量纲） |
| **个股成交量 = 股** | A 股 1 手 = 100 股，但 QuantDB 个股 kline 的 `volume` 单位就是**股**，不是手 |
| **个股成交额 = 万元** | `amount = 5828.37` 表示 5828.37 **万元**（约 5828 万），不是元 |
| **指数成交量 = 手** | `index_daily.volume` 单位是**手**（1 手 = 100 股），与个股相反！ |
| **市值 = 元** | valuation 的 `total_mv/float_mv` 单位是**元**（工业富联 float_mv ≈ 1.31 万亿） |
| **比例字段 ≈ 小数或 %** | 没有统一约定，按数据集查下表；% 的字段值 = 百分数（4.06 即 4.06%） |
| **symbol 格式按数据源分** | parquet 用**后缀** `601138.SH`；PG 的 `stock_daily_latest` 用**前缀** `SH601138` |
| **验证公式** | 个股 `close*volume/amount ≈ 1e4`（股+万元）；指数 ≈ 2e4（手+万元） |

## 二、1_kline_data 日线（daily_forward / daily_backward / daily_unadjusted）

| 字段 | 单位 | 实测依据 |
|---|---|---|
| `volume` | **股** | 601138 20260814 volume=4704920 股，amount=5828.37 万元，close*volume/amount≈1e4 |
| `amount` | **万元** | 同上（close 66.19 × 470 万股 ≈ 3.1 亿 ≈ 31163 万元，与 amount 同量级吻合） |
| `open/high/low/close` | 元（forward=前复权，backward=**后复权**，unadjusted=不复权） | |
| 单位切换传闻 | **不存在**。20260721 前后 amount 均为万元、volume 均为股。曾有记忆说 20260721 切换到"手/元"，实测已无此切换，按股/万元统一处理 | |

**min1/min5**：同单位（volume=股、amount=万元），但数据**停滞在 2026-07-24**，用前先查最新日期。

## 三、L2 原始逐笔与十档盘口（1_kline_data/l2_data + tick_data，2026-08 新增）

万得(Wind) L2 数据，`backend/scripts/wind_l2_import.py` 从**逐日 7z 压缩包**（`20260511.7z`）导入。
**当前实测只有 1 个交易日 20260511**（order 1950 + trade 1721 = 3671 文件，部分标的仅其一），
用前先查日期覆盖：`ls data/quantdb/1_kline_data/l2_data/ | grep -oE '[0-9]{8}' | sort -u`。

文件命名：`order_{code}_{date}.parquet` / `trade_{code}_{date}.parquet`，code 为**下划线**格式
`000001_SZ`（对应后缀 `000001.SZ`）。`time` 单位统一为 **UTC 毫秒**（万得 hhmmssmmm 转 UTC，
北京 09:15:00 = 01:15:00 UTC），覆盖 09:15 集合竞价到 15:00 收盘。

### 逐笔委托 order_
| 字段 | 单位/格式 | 说明 |
|---|---|---|
| `time` | UTC ms | 09:15 集合竞价 → 15:00 收盘 |
| `order_id` | int | 交易所委托号（可与 trade_.ask_order_id/bid_order_id 配对） |
| `channel` | str | 委托编号 |
| `order_type` | str | `'0'`=普通委托、`'U'`=撤单、`'1'`=其余 |
| `direction` | str | `'B'`=买 / `'S'`=卖 |
| `price` | **元** | 万得 ×10000 → 元（已归一）；集合竞价未定价委托 price=0 |
| `volume` | **股** | |

### 逐笔成交 trade_
| 字段 | 单位/格式 | 说明 |
|---|---|---|
| `time` | UTC ms | 同上 |
| `trade_id` | int | 成交编号 |
| `trade_type` | str | `'C'`=**集合竞价**成交 / `'0'`=连续竞价 |
| `direction` | str | `'B'`/`'S'`/`' '`（集合竞价段=空格） |
| `price` | **元，未复权** | 与 `daily_unadjusted` 对齐（实测 000001.SZ 20260511 末笔 11.28 = unadjusted close 11.28）；与 `daily_forward` 前复权 close 有除权差（有分红送转者差数十%） |
| `volume` | **股** | ⚠️ 沪≈日线量、深≈2×日线量（见坑1） |
| `ask_order_id` / `bid_order_id` | int | 叫卖/叫买序号 → 配对逐笔↔委托 |

### tick_data 十档盘口快照（18 列）
| 列 | 单位 | 说明 |
|---|---|---|
| `lastPrice/open/high/low/lastClose` | 元 | |
| `amount` | **混源** | ⚠️ 见坑2 |
| `volume` | **混源** | 当日累计成交量 |
| `pvolume` | 笔 | 连续竞价成交笔数（wind 导入有值）；旧导入=0（**可作来源判据**） |
| `askPrice/bidPrice` | array(10) | 十档价，元 |
| `askVol/bidVol` | array(10) | 十档量，股 |
| `stockStatus` | int | BS 标志 |
| `openInt/settlementPrice/lastSettlementPrice` | — | 期货占位字段，A股多为 0 |

### ⚠️ 三个必踩的坑
1. **沪/深成交量双口径**：逐笔成交 volume 求和，**沪 SH = 日线 volume（实测 600714.SH 求和 34825501≈日线 34825500）**；
   **深 SZ ≈ 1.8~2.1× 日线 volume（实测 002830 1.92×、300521 2.11×、000999 1.79×、000001 1.88×）**。
   → 逐笔求和**不能**当当日成交量，深市除 ~2 或直接与日线对账。
2. **tick_data 单位混源**：同一目录内**单位不统一**——wind_l2_import 导入的（如 20260511）volume=**股**、amount=**万元**；
   更早 QuantDB tick 同步的（如 20260720）volume=**手**、amount=**元**。
   实测：20260511 最后快照 volume 93186521≈日线 93186520 股、amount 104743.53≈104743.54 万；
   20260720 volume 1567304 手×100=156730400 股、amount 1713460189 元=171346.02 万。
   → 用前**必须**拿最后一条快照对账日线，或按 `pvolume`（0=旧/手·元，>0=wind/股·万元）区分。
3. **价格=未复权真实成交**：有除权除息时与前复权日线差一个复权因子
   （实测 002830.SZ 20260511 L2 末笔 24.08 vs 前复权 close 17.06）；算收益需先统一复权口径。

### 导入命令（手动增量，**不吃 quantdb_daily_sync**）
```bash
python backend/scripts/wind_l2_import.py --archive /path/to/20260511.7z                      # 全市场
python backend/scripts/wind_l2_import.py --archive ... --symbols 000001.SZ,600519.SH         # 指定标的
python backend/scripts/wind_l2_import.py --archive ... --force                               # 覆盖重导
```
文件名即日期（`20260511.7z` → 20260511），流式逐股解压导入、可断点续跑（已存在三件套自动跳过）。
容器内 7z 默认 `/opt/p7zip-legacy/bin/7z`，数据目录自动探测 `/data/quantdb` 或本地 `data/quantdb`。

## 四、1_kline_data/index_daily 指数日线（⚠️ 与个股相反）

| 字段 | 单位 | 实测依据 |
|---|---|---|
| `volume` | **手**（×100=股） | 上证 000001.SH 20260814 volume=499525600 手 = 499.5 亿股（合理）；close*volume/amount=19808≈2e4 |
| `amount` | **万元** | 99037192 万元 = 9903.7 亿元 ≈ 上证单日成交额 ✓ |
| `close` | 点位 | 3927.18 |

> 反推：指数平均股价 = close×100/(close×volume/amount) = close×100/19808 ≈ 19.8 元/股，符合 A 股平均股价，故 volume 必为手。

## 五、5_technical_derived 技术衍生

### valuation（估值）
| 字段 | 单位 | 注意 |
|---|---|---|
| `close` | 元，**不复权** | 601138 close=66.19（与 technical_indicators 的后复权 close=70.05 不同） |
| `total_mv / float_mv` | **元** | float_mv=1313480468277.96 ≈ 1.31 万亿 ✓ |
| `total_capital / circulating_capital` | 股 | 19844092284 股 ≈ 198.4 亿股 |
| `net_profit_ttm / revenue_ttm / equity / annual_net_profit` | 元 | |
| `pe_ttm / pe_static / pb / ps_ttm` | 倍 | |
| `dividend_rate` | **%（百分数值）** | 0.148 = **0.148%**！公式 = 近一年每10股派息/10/close×100。601138: 0.98/10/66.19×100=0.1481 ✓；600519: 51.98/10/1341.99×100=0.3873 ✓。**把它当小数会差 100 倍** |
| `dividend_rate` 口径切换 | **20260814 起** | 此前为小数口径（每10股派息/不复权close，如 0.98/65.60=0.01494）；20260814 起切换为百分数口径（×100）。同字段历史不连续，跨 20260814 分析需 ×10 归一 |

### technical_indicators（技术指标）
| 字段 | 单位 | 陷阱 |
|---|---|---|
| `close` | **后复权** | 601138=70.05，与不复权 66.19 不同；凡基于 close 算的指标都是后复权口径 |
| `volume_ma_3/5` | 股 | |
| `amount_ma_5` | 万元 | |
| `pct_change` | **%** | 1.4717 = 1.47% |
| `future_return_1d / future_return_20d` | **未来 N 日收益（标签）** | 2026-09 由 `return_Nd` 改名；勿当历史动量或过滤条件（标签泄漏）。历史动量用 `pct_change` / l1 `mom_ret_*` |
| `vol_std_20` | **%** | 4.0578 = 4.06%（l1 里同名字段是小数 0.0406，差 100 倍！） |
| `vol_atr_14` | **元** | 3.58 元（l1 同名字段也是元，一致） |
| `macd_hist / rsi_14 / kdj_*` | 原始指标值 | 与 l1 一致 |

### market_sentiment（市场情绪）
| 字段 | 单位 |
|---|---|
| `close` | 不复权 |
| `turnover_rate` 等比率 | 小数（0.02 = 2%） |
| `momentum_*` | %（百分数值） |

## 六、6_ml_datasets 因子

### features_daily（技术+估值合并表，**78 列**，2026-09 起新增 30 列）
基础 46 个数值列单位与 technical_indicators / valuation 一致；**新增 30 列全部以字符串存储**，用前先转数值：

| 字段 | 单位 | 实测/注意 |
|---|---|---|
| `dividend_rate` | **小数口径（无 ×100）** | 与 valuation 不同！公式 = 每10股派息/不复权close（0.98/66.19=0.0148），**从未切换口径**。valuation 20260814 起是它的 10 倍。特征快照 generate_feature_snapshots.py 已 ×10 归一到百分数口径 |
| `total_mv / float_mv / pe / pb 等` | 与 valuation 完全一致 | 实测 601138 全部 ✓ |
| `total_cap_yi / float_mv_yi` | **亿元** | 实测 `×1e8/total_mv=1.000`、`×1e8/float_mv=1.000`（与本表 `total_mv/float_mv`（元）差 1e8） |
| `free_float_shares` | **万股**（自由流通股本） | `×1e4` 转股；000001.SZ=816056.58 万股≈81.6 亿股（**自由流通 < 流通股本**，勿与 `circulating_capital` 混用） |
| `hs_turnover` | **%**（换手率） | 如 0.43 / 1.71，max≈76 |
| `zaf` | **%**（当日涨跌幅） | 与 `pct_change` 高度一致（corr=0.986，-0.93 vs -0.928） |
| `seal_strength` | 无量纲（封板强度，≈1.0 封住） | 如 1.02 |
| `ever_zt_count` / `year_zt_days` | 计数（整数） | 曾涨停次数 / 年内涨停天数 |
| `ipo_price` | 元 | 发行价 |
| `zt_price` / `dt_price` | **元（基于不复权价）** | 涨停价/跌停价；⚠️ 与表内**前复权** close 不同口径（勿直接比；000001.SZ: zt=13.04/dt=10.67 vs 前复权 close=18.16） |
| `beta_now` | 无量纲（beta） | 与 `beta_20` corr≈0.71（窗口/口径不同） |
| `dyna_pe` / `static_pe_ttm` | 倍 | ⚠️ 含负哨兵（实测 -686 / -897），亏损股勿直接用 |
| `div_yield` | **%（百分数值）** | 5.14=5.14%；与本表 `dividend_rate`（小数 0.0508）**差 100 倍** |
| `pb_mrq` | 倍 | ≈`pb`（`pb_mrq/pb` median=1.000） |
| `list_date` | 字符串 `YYYYMMDD` | 如 '19910403' |
| `industry_code / industry_name` | 字符串 | 128 细分行业（X5001 / 全国性银行） |
| `sector_code` | 字符串 | 如 881386 |
| `region_area_code / region_area_name` | 字符串 | 32 地区板块（18 / 深圳板块） |
| `main_business` | 中文文本 | 主营业务 |
| `in_hs300 / is_hsgt / is_margin / is_kcb_creatable / is_st / is_quit_risk / is_hk` | 字符串 `'0'/'1'` | 标记位（转 int 后再过滤/统计） |

### l1_factors（一级因子，decimal 为主）
| 字段 | 单位 |
|---|---|
| 收益率类 `mom_ret_*` | 小数（0.0147 = 1.47%） |
| `vol_std_*` | **小数**（0.0406 = 4.06%；与 technical_indicators 的 % 版本差 100 倍） |
| `vol_atr_14` | **元**（3.55 元） |
| `fun_total_mv` | **ln(市值元)** —— 用时要 exp() |
| 分位数/percentile 字段 | 0~1 小数 |
| `liq_* / fun_turnover / fun_mv` | 部分日期为 None，注意补缺 |

### l1_l2_factors（L2 因子现位于合并宽表，flow 金额注意）
| 字段 | 单位 | 实测依据 |
|---|---|---|
| `flow_net_amount / flow_buy/sell / flow_super/large/medium/small_net` | **万元**（2026-09 起） | 与同表 `amount` 同量纲：`flow_net_amount/amount≈flow_net_ratio`；此前为**元**（`flow/(amount×1e4)≈ratio`） |
| `flow_*_ratio` | 小数 | |
| `vol_turnover_total` | **股** | 与 kline volume 完全相等 ✓ |
| 分区 | l2 独立目录已下线，读 l1_l2 合并宽表 | ⚠️ top 少数净流入可能厂商同值封顶 |

**读入归一**：`backend/shared/quantdb_flow_units.py` 自动把万元→元（兼容旧分区），下游再 `/1e8`→亿、`×1e-6`→百万元。

**flow 灌入口径**（update_sdl_complete_pipeline.py）：假定源为元时 `/1e6`→百万元；新版万元源须先 ×1e4。

## 七、PG 表 stock_daily_latest（API 服务数据源）

| 列 | 单位 / 格式 | 实测依据 |
|---|---|---|
| `symbol` | **前缀格式** `SH601138`（不是 601138.SH） | suffix 查询 0 行，prefix 查询 1072 万行 |
| `volume` | 股 | 601138 max_volume=633217088 股 |
| `amount` | **万元** | 601138 amount=5828.37，max_amount=3306339（万元） |
| `float_mv / total_mv` | 元 | float_mv=7095151630 元 |
| `turnover_rate / flow_net_amount / main_flow` | **NULL**（未灌） | 风险评分里"缺少换手率"由此而来 |
| `volume_ratio_5` | 倍（0.934） | |

## 八、research API 换算表（/research/features 等接口返回）

API 层 `_UNIT_SCALES` 把部分字段缩放后输出（L2 金额先归一为元）：

| 输出字段 | 缩放 | 输出单位 | 例 |
|---|---|---|---|
| `totalMv / floatMv` | ×1e-8 | 亿元 | |
| `mainFlow / flowNetAmount / flowLargeNet / flowMediumNet / flowSmallNet / flowSuperNet` | ×1e-6 | 百万元 | 统一口径 |
| `turnoverRate` | — | % 小数 | |

## 九、3_financial_data 财务数据

| 数据集 | 字段 | 单位 |
|---|---|---|
| `balance / income / cashflow` | 各科目 | 元 |
| `capital` | 股本 | 股 |
| `holder_num` | 股东户数 | 户 |
| `dividend_factors` | `interest` | **每10股派息（元）**——600519 每10股派 51.98 元，601138 派 0.98 元。算每股股息要 /10 |
| `dividend_factors` | `stockBonus / stockGift / allotNum` | 每10股送/转/配股数 |
| `dividend_factors` | `dr` | 除权因子（复权用） |

## 十、2_base_sector

| 数据集 | 字段 | 单位 | 注意 |
|---|---|---|---|
| `instrument_detail` | `Symbol` | 后缀 601138.SH | HqDate 停滞 **20260720**，市值/估值滞后 |
| | `J_zgb / FreeLtgb` | **万股**（1984409.25 万股 = 198.4 亿股） | 与 valuation 的 circulating_capital(股) 差 1e4 |
| | `J_yysy / J_jly / J_zzc` 等 J_* | **万元**（25107808 万 = 251 亿） | 与 financials 的元差 1e4 |
| | `Zsz / Ltsz` | **亿元**（11211.91 亿 ✓） | |
| | `J_mgsy` | 元（2.14 元/股） | |
| | `fHSL` | 不明（0.56，非换手率%，与自算 0.74% 不符），**别当换手率用** | |
| | `TotalBVol` | 不明（43555，量级像手，但远小于全天量） | L2 快照字段，非全天 |
| | `DYRatio` | 不可靠（600519=4.15 vs 真实 0.39%），**别当股息率**，用 valuation.dividend_rate | |
| | `Yield` | 不明（6078.44），勿用 | |
| `index_weights` | `Weight` | **%** | 文件名 `000300.SH.parquet` 不是 `000300.parquet` |
| `trading_calendar` | `TradingDate` | YYYYMMDD int | |

## 十一、其他数据集

| 数据集 | 字段 | 单位 | 状态 |
|---|---|---|---|
| `margin_trading` | `finance_*` | 万元 | |
| | `slo_volume / slo_net` | 股 | |
| `hsgt_north` | `holding_quantity` 股 / `holding_value` 元 / 比率 % | | **停滞 2024-08**（北向改季度披露后） |

## 十二、已知数据缺口（2026-08 实测）

1. **min1/min5** 分钟线停更（最新 2026-07-24）
2. ~~l2 分区停更~~ —— l2 独立目录已下线，读 l1_l2 合并宽表
3. **hsgt_north** 北向明细停更（2024-08，改季度披露所致）
4. **instrument_detail** HqDate 滞后（20260720）
5. **dt=20260729~20260802** 个股日线有同步缺口（非交易日+同步中断）
5b. **valuation dt=20260813** 只有 101 行（同步缺口，dividend_rate 全 NaN），features_daily 同日 5543 行正常
6. **technical_indicators / features_daily 的 `future_return_*`（旧 `return_*`）是未来收益标签**，勿当历史动量过滤
7. **stock_daily_latest** 的 turnover_rate/flow_net_amount/main_flow 常为 NULL
8. **l2_data 原始逐笔仅 20260511 单日** —— 万得按日 7z 手动导入（`wind_l2_import.py`），非自动日更
9. **tick_data 单位混源** —— wind=股/万元，旧同步=手/元，对账前勿直接用

## 十三、分析前检查清单

- [ ] 确认数据源是 parquet 还是 PG/API（单位体系不同）
- [ ] symbol 格式匹配数据源（parquet 后缀 / PG 前缀）
- [ ] 成交量：个股=股，指数=手
- [ ] 成交额：万元（API 输出已缩放为亿/百万）
- [ ] 波动率：technical_indicators 是 %，l1 是小数（差 100 倍）
- [ ] 股息率：valuation.dividend_rate 是 % 百分数值（0.148=0.148%），不是小数
- [ ] close 口径：technical_indicators 是后复权，valuation 是不复权
- [ ] 市值：parquet 是元，instrument_detail 是亿元/万股，API 是亿元
- [ ] 财务：financials 是元，instrument_detail J_* 是万元
- [ ] 用前查数据最新日期（分钟线、l2、hsgt 都可能已停更）
- [ ] L2 原始逐笔（l2_data）：先查日期覆盖（当前仅 20260511 单日），价格是未复权，逐笔求和沪≈日线量/深≈2×，别当成交量
- [ ] tick_data：先拿最后一条快照对账日线（wind=股/万元 vs 旧同步=手/元，单位混源）
