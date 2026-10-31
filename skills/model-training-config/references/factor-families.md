# 因子家族与选因子指引

配置里的 `selectedFeatures` 是**因子 key 列表**。字段口径以
`skills/quantdb-fields` 与 QuantDB 官网字段字典（https://www.quantdb.cn/docs/fields.html）为准；
本仓库的本地目录 `config/features/model_training_feature_catalog_v1.json` 可查每个 key 的
`feature_name / explanation / measured_ic / measured_icir / markets / source`。

> 选因子前先确认：**该 key 在目标市场下存在**。CN 直读市场的目录由后端已发布版本驱动，
> 配置里写了目录没有的 key，导入时会被**静默过滤**。用 `python3
> skills/model-training-config/scripts/validate_training_config.py` 只能查 schema，
> 市场/目录级可用性要到训练页导入预览里看（或查上述本地目录的 `markets` 字段）。

## 一、训练宽表三来源

| 来源 | 说明 | 典型 factor_source |
|---|---|---|
| Features Daily | 技术指标 + 估值 + 未来收益标签 | `features_daily` |
| L1 日频（110 维） | 日线量价 + 财务 + 行业概念截面 | `l1_factors` |
| L2 高频（约 211 维） | 逐笔成交 + 十档盘口 + 逐笔委托 | `l1_l2_factors`（L2 因子现位于合并宽表） |
| 合并 | 上述 inner join，300+ 维 | `l1_l2_factors` |

## 二、家族一览（L1）

| 家族 | 代表 key | 语义要点 |
|---|---|---|
| 换手 Turnover | `turn_1` `turn_5` `turn_20` `turn_std_20` `turn_z_20` `turn_ratio_1_20` `turn_hl_pos_20` | 成交量/流通股本；关注放大与位置 |
| 资金量 Amount | `amt_log` `amt_ma_20` `amt_z_20` `amt_net_flow_20` `amt_up_ratio_20` `mfi_14` `obv_slope_20` `amt_vol_ratio_20` | 日频资金流代理（净流入用 OBV 近似） |
| 动量 Momentum | `mom_ret_1d` `mom_ret_5d` `mom_ret_20d` `mom_ret_60d` `mom_ma_gap_5/20` `mom_macd_dif` `mom_rsi_14` `mom_kdj_k` | 过去 N 日收益与摆动指标 |
| 波动 Volatility | `vol_std_5/20` `vol_atr_14` `vol_parkinson_20` `vol_gk_20` `vol_amp_20` | 收益波动与高低价估计量 |
| 技术 Technical | `tech_bb_width` `tech_bb_pos` `tech_cci_20` `tech_adx_14` | 趋势/区间形态 |
| 财务估值 Fundamental | `fun_pe` `fun_pb` `fun_bp` `fun_ep` `fun_value_zscore` `fun_roe` `fun_float_mv` `fun_total_mv` | 公告日对齐，市值用不复权真实价 |
| 筹码 Chip | `chip_profit_ratio_20` `chip_concentration_20` `chip_floating_ratio` | 成本分布获利/集中/浮动 |
| 风格 Style | `style_beta_20/60` `style_idio_vol_20/60` `style_residual_ret_20` | L1 基准为**中证 500** |
| 行业 Industry | `ind_strength_20` `ind_ret_20` `ind_dispersion_20` `ind_crowding_20` `ind_volume_ratio_20` | 行业分组后映射回个股 |
| 概念 Concept | `concept_hot_score` `concept_leader_score` `concept_exposure_top1` | 概念板块聚合 |

## 三、家族一览（L2，约 211 维）

价差微观结构 Spread、已实现波动 RV（`vol_realized_rv/rrv/rkurt/...`）、资金流 Flow
（`flow_net_ratio` `flow_buy_amount` `flow_large_pct` `flow_imbalance_*`）、信息不对称 VPIN
（`micro_vpin_8/20/50/100` `micro_vpin_ma_5/20` `micro_vpin_hurst` `micro_toxicity_persistence`）、
订单簿深度 Depth（`micro_depth_*`）、委托流 OrderFlow（`flow_cancel_*` `flow_order_*`）、
分时段 Segment（`micro_zone_*`）、跳跃与冲击 Jump（`micro_jump_*` `micro_impact_*`）、
成交序列 Sequence（`micro_trade_*`）、流动性 Liquidity（`micro_liquidity_*`）、
毒性补充 Toxicity（`micro_adverse_selection` `micro_vpin_entropy` `micro_informed_ratio`）、
成交量补充 Volume（`vol_turnover_*` `vol_price_divergence` `vol_persistence`）。

L2 依赖 Tick/盘口，**覆盖率与日期受快照限制**（部分标注「2023-2026 快照回填」），
划时间切分时要确保 test 段也有 L2 数据。

## 四、选因子 recipe（按需求挑一套，别堆全量）

| 需求 | 建议家族配比（示例数量） |
|---|---|
| 通用 A 股基线 | 动量 8 + 波动 6 + 换手 5 + 资金量 6 + 技术 3 + 财务 5 + 风格 3 + 行业 3 + 概念 2 + L2 微观 10 |
| 短周期 T+1/T+3（重微观） | 动量 4 + 波动 4 + L2 微观 20 + L2 资金流 8 + 换手 4 |
| 中低频稳健 | 财务估值 8 + 动量 6 + 波动 5 + 风格 5 + 行业 5 + 概念 3，少量 L2 |
| 纯 L1 轻量 | 只用 `*_factors = l1_factors`，挑 30~60 个 L1 key，无需 L2 |
| 纯 L2 微观 | 从 `l1_l2_factors` 中取 micro_*/flow_*/vol_realized_* 列子集 |

## 五、硬性禁忌

1. **不要选标签字段**：`return_*d` / `target_*` / `pct_change` / `future_*` 是未来收益标签，
   混入特征矩阵 = 前视偏差、回测虚高。校验脚本会直接判 error。
2. **不要跨市场硬塞 key**：HK/US 等市场的目录与 CN 不同。
3. **`n_top` 不要大于候选数**：候选 40 个却写 `n_top: 120`，筛选等于没做。
4. **L2 字段名有同义不同源**：如风格 Beta，L1 用中证 500，Features Daily 的 `beta_20`
   用沪深 300，别混。

## 六、可参考的成品配置

- 本技能 `templates/` 下可直接导入的配置：
  - 4 个轻量演示（便于改写）：`cn-l1l2-t3-lightgbm.yml`、`cn-l1l2-t5-lightgbm.yml`、
    `cn-l1l2-t5-gru.yml`、`hk-t5-lightgbm.yml`
  - `quantmind-training-L1L2-ICIR-100-T5.yml` — CN / ICIR 优选 100 特征 / T+5 / LGB 重正则（大 preset）
- 仓库根目录另有两个已验证的成品 preset（JSON 内容 + `.yml` 扩展名）：
  - `quantmind-training-L1L2-120-T3.yml` — CN / 120 特征 / T+3 / LGB 基线
  - `quantmind-training-L1L2-120-T5-opt.yml` — 同特征 / T+5 / LGB 重正则
