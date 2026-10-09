# 已知断点清单（known-breakpoints）

以下为 **2026-10-08 实测**确认的上游改版/数据现状。跑扫描时用 `--allow-removed/--allow-added` 把它们降级为 info，跨断点窗口的分布对比才可读。新增断点经确认后补进本表。

## CN features_daily —— 窗口内三次列集切换（2026-06-01~09-30 实测）

| 日期（分区切换） | 事件 | 处置 |
|---|---|---|
| 2026-09-11→09-14 | 移除 `Symbol_val,close_val`；新增 30 列（`in_hs300,is_hsgt,is_margin,is_kcb_creatable,is_st,is_quit_risk,is_hk,industry_code,industry_name,sector_code,region_area_code,region_area_name,main_business,list_date,total_cap_yi,float_mv_yi,free_float_shares,ipo_price,zt_price,dt_price,hs_turnover,seal_strength,zaf,beta_now,dyna_pe,static_pe_ttm,div_yield,pb_mrq,ever_zt_count,year_zt_days`） | 上游基本面/标志位改版；新增标志位列 `is_hk/is_quit_risk` 自引入即常量属预期 |
| 2026-09-14→09-15 | 仅列序变化（集合不变） | 按位置对齐读取的管线会错位；按列名读取无影响 |
| 2026-09-18→09-21 | 六个标签整体改名：`return_{1,3,5,10,20,60}d` → `future_return_{1,3,5,10,20,60}d` | 旧名标签下游训练样本会全空；当日标的数 5516→5221（−5.4%，上游过滤口径变更），不是故障 |

参考命令（白名单 09-14 断点后跑全窗）：

```bash
docker cp skills/factor-drift-monitor/scripts/factor_drift.py quantmind:/tmp/
docker exec -w /app quantmind python3 /tmp/factor_drift.py --quantdb --market CN \
  --dataset features_daily --start 2026-06-01 --end 2026-10-07 \
  --allow-removed Symbol_val,close_val \
  --allow-added in_hs300,is_hsgt,is_margin,is_kcb_creatable,is_st,is_quit_risk,is_hk,industry_code,industry_name,sector_code,region_area_code,region_area_name,main_business,list_date,total_cap_yi,float_mv_yi,free_float_shares,ipo_price,zt_price,dt_price,hs_turnover,seal_strength,zaf,beta_now,dyna_pe,static_pe_ttm,div_yield,pb_mrq,ever_zt_count,year_zt_days
```

注意：09-21 的标签改名是**标签类别**告警，不在 `--allow-removed` 覆盖范围内（标签单独监控），无法也不应白名单化——它必须持续可见（旧名标签一旦被下游训练引用，样本会全空）。

## CN l1_factors —— 两次切换（2026-06-01~09-30 实测）

| 日期 | 事件 | 处置 |
|---|---|---|
| 2026-08-25→08-26 | 移除 `release_id,published_at`（121→119 列） | `--allow-removed release_id,published_at` |
| 2026-09-14→09-15 | 仅列序变化（`fun_pe/fun_ep` 等移动） | info，无需白名单（不产生 critical） |

## US l1_factors —— 一次切换（最近 35 分区实测）

| 日期 | 事件 | 处置 |
|---|---|---|
| 2026-08-07→08-10 | 移除 6 个宏观列 `macro_pmi,macro_unemployment,macro_ppi,macro_retail_sales,macro_spcs20,macro_trade_balance`（182→176 列） | `--allow-removed macro_pmi,macro_unemployment,macro_ppi,macro_retail_sales,macro_spcs20,macro_trade_balance` |

## HK l1_factors

最近 35 个分区（2026-08-17~10-05）**无列集切换**（190 列稳定）。东假日（如 2026-10-01）表现为「工作日缺口」info，无需处置。

## 已知数据缺陷（未修，遇到按现状解读）

1. **US 行情与因子两侧均缺 2026-08-17~19**（周一至周三，美股正常交易日）——脚本报 info「工作日缺口（疑为节假日）」，实为数据缺口；2026-09-07 为劳动节，属正常休市。
2. **US 因子分区大幅落后行情**：实测 2026-09-17 vs 2026-10-06（落后 13 个分区）→ 报 critical 断更，属真实停更信号而非误报。
3. **CN features_daily `pe_static/pe_ttm/ps_ttm` 含 inf**：2026-09-07~09-14 各分区 81/4/5 个（窗口内共 540 处），脚本统计已过滤但会记 `profile.inf_cells`；复算这三列先自行过滤。
4. **HK/US 基本面列从未填充**：HK 28 列、US 14 列基线即常量（adj_factor/pe_ttm/pb/roe/bp/float_mv/total_mv 等）；HK 18 列 `ind_*` 行业聚合列基线缺失率 86%~100%。均为结构性现状，脚本聚合为 info；下游用到再谈填充。

## 维护约定

- 白名单只接「确认过的上游改版」；疑似故障（整列停填、断更、覆盖骤降）**不得**加白名单掩盖。
- 每次新增断点：记录日期 + 增删列清单 + 一句原因，并附一条可复制的 `--allow-removed/--allow-added` 命令。
- 断点日期变更（如再次改版）时，直接跑一次扫描核对 `schema.transitions` 输出，不要照抄旧清单。
