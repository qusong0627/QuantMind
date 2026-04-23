# 模型训练特征方案（前端真实字段版，152维）

## 1. 目标与口径
- 本文档以前端训练页真实消费的特征目录为准。
- 前端数据源：`GET /api/v1/models/feature-catalog`（用户态），管理员同源接口为 `GET /api/v1/admin/models/feature-catalog`。
- 后端来源优先级：特征注册表（`qm_feature_set_*`）> 回退文件 `config/features/model_training_feature_catalog_v1.json`。
- 当前生效版本：`qm_feature_set_v1_20260402`，总维度：**152**。

## 2. 分类配额（与前端目录一致）

| 分类ID | 分类名 | 维度数 |
|---|---|---:|
| `ohlcv` | 基础行情 | 6 |
| `momentum` | 动量 | 24 |
| `volatility` | 波动率 | 22 |
| `volume` | 成交量 | 22 |
| `fund_flow` | 资金流 | 22 |
| `style` | 风格因子 | 16 |
| `industry` | 行业因子 | 20 |
| `microstructure` | 微观结构 | 20 |
| **合计** |  | **152** |

## 3. 训练页固定规则（来自前端）

- 训练最小基线特征（`TRAINING_BASE_FEATURES`，6项，后端可自动补齐）：
  `mom_ret_1d, mom_ret_5d, mom_ret_20d, liq_volume, liq_amount, liq_turnover_os`
- 前端预设特征（`PRESET_DEFAULT_FEATURES`，48项）：
  `open, high, low, close, volume, factor, mom_ret_1d, mom_ret_5d, mom_ret_10d, mom_ret_20d, mom_ma_gap_5, mom_ma_gap_20, mom_macd_hist, mom_rsi_14, mom_kdj_k, mom_breakout_20d, vol_std_20, vol_atr_14, vol_parkinson_20, vol_gk_20, vol_rs_20, vol_downside_20, vol_realized_rv, vol_jump_zadj, liq_volume_ma_20, liq_volume_ratio_5, liq_amount_ma_20, liq_amount_ratio_5, liq_mfi_14, liq_amihud_20, liq_amihud_60, liq_accdist_20, flow_net_amount, flow_net_amount_ratio, flow_large_net_amount, flow_vpin, flow_vpin_ma_5, flow_vpin_ma_20, style_ln_mv_total, style_ln_mv_float, style_beta_20, style_beta_60, style_idio_vol_20, style_residual_ret_20, ind_ret_1d, ind_ret_20d, ind_strength_20, ind_momentum_rank_20`

## 4. 字段清单（完整）

### 4.1 基础行情（`ohlcv`，6维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_ohlcv_001 | `open` | 开盘价（复权） | `OpenPrice` | `股票历史日行情信息表后复权` |
| feat_ohlcv_002 | `high` | 最高价（复权） | `HighPrice` | `股票历史日行情信息表后复权` |
| feat_ohlcv_003 | `low` | 最低价（复权） | `LowPrice` | `股票历史日行情信息表后复权` |
| feat_ohlcv_004 | `close` | 收盘价（复权） | `ClosePrice` | `股票历史日行情信息表后复权` |
| feat_ohlcv_005 | `volume` | 成交量 | `Volume` | `股票历史日行情信息表后复权` |
| feat_ohlcv_006 | `factor` | 复权因子 | `1.0` | `constant` |

### 4.2 动量（`momentum`，24维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_001 | `mom_ret_1d` | 1日收益率动量 | `(C_t/C_{t-1})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_002 | `mom_ret_3d` | 3日收益率动量 | `(C_t/C_{t-3})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_003 | `mom_ret_5d` | 5日收益率动量 | `(C_t/C_{t-5})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_004 | `mom_ret_10d` | 10日收益率动量 | `(C_t/C_{t-10})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_005 | `mom_ret_20d` | 20日收益率动量 | `(C_t/C_{t-20})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_006 | `mom_ret_60d` | 60日收益率动量 | `(C_t/C_{t-60})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_007 | `mom_ret_120d` | 120日收益率动量 | `(C_t/C_{t-120})-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_008 | `mom_ma_gap_5` | 收盘价偏离5日均线 | `C_t/MA(C,5)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_009 | `mom_ma_gap_10` | 收盘价偏离10日均线 | `C_t/MA(C,10)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_010 | `mom_ma_gap_20` | 收盘价偏离20日均线 | `C_t/MA(C,20)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_011 | `mom_ma_gap_60` | 收盘价偏离60日均线 | `C_t/MA(C,60)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_012 | `mom_ma_gap_120` | 收盘价偏离120日均线 | `C_t/MA(C,120)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_013 | `mom_ema_gap_12` | 收盘价偏离12日EMA | `C_t/EMA(C,12)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_014 | `mom_ema_gap_26` | 收盘价偏离26日EMA | `C_t/EMA(C,26)-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_015 | `mom_macd_dif` | MACD-DIF | `EMA(C,12)-EMA(C,26)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_016 | `mom_macd_dea` | MACD-DEA | `EMA(DIF,9)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_017 | `mom_macd_hist` | MACD柱值 | `2*(DIF-DEA)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_018 | `mom_rsi_6` | RSI(6) | `100-100/(1+RS_6)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_019 | `mom_rsi_14` | RSI(14) | `100-100/(1+RS_14)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_020 | `mom_kdj_k` | KDJ-K | `SMA(RSV,3,1)` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_021 | `mom_kdj_d` | KDJ-D | `SMA(K,3,1)` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_022 | `mom_kdj_j` | KDJ-J | `3*K-2*D` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_023 | `mom_roc_12` | 12日变化率 | `C_t/C_{t-12}-1` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_024 | `mom_breakout_20d` | 20日突破强度 | `C_t/MAX(H,20)-1` | `股票历史日行情信息表后复权.HighPrice/ClosePrice` |

### 4.3 波动率（`volatility`，22维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_025 | `vol_std_5` | 5日收益率标准差 | `STD(ret,5)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_026 | `vol_std_10` | 10日收益率标准差 | `STD(ret,10)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_027 | `vol_std_20` | 20日收益率标准差 | `STD(ret,20)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_028 | `vol_std_60` | 60日收益率标准差 | `STD(ret,60)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_029 | `vol_atr_14` | ATR(14) | `MA(TR,14)` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_030 | `vol_atr_20` | ATR(20) | `MA(TR,20)` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_031 | `vol_true_range` | 真实波幅TR | `MAX(H-L,\|H-PC\|,\|L-PC\|)` | `股票历史日行情信息表后复权.HighPrice/LowPrice/ClosePrice` |
| feat_032 | `vol_parkinson_10` | Parkinson波动率10日 | `sqrt(sum((ln(H/L))^2)/(4nln2))` | `股票历史日行情信息表后复权.HighPrice/LowPrice` |
| feat_033 | `vol_parkinson_20` | Parkinson波动率20日 | `同上窗口=20` | `股票历史日行情信息表后复权.HighPrice/LowPrice` |
| feat_034 | `vol_gk_10` | Garman-Klass波动率10日 | `GK公式窗口10` | `股票历史日行情信息表后复权.OpenPrice/HighPrice/LowPrice/ClosePrice` |
| feat_035 | `vol_gk_20` | Garman-Klass波动率20日 | `GK公式窗口20` | `股票历史日行情信息表后复权.OpenPrice/HighPrice/LowPrice/ClosePrice` |
| feat_036 | `vol_rs_10` | Rogers-Satchell波动率10日 | `RS公式窗口10` | `股票历史日行情信息表后复权.OpenPrice/HighPrice/LowPrice/ClosePrice` |
| feat_037 | `vol_rs_20` | Rogers-Satchell波动率20日 | `RS公式窗口20` | `股票历史日行情信息表后复权.OpenPrice/HighPrice/LowPrice/ClosePrice` |
| feat_038 | `vol_downside_20` | 下行波动率20日 | `STD(min(ret,0),20)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_039 | `vol_upside_20` | 上行波动率20日 | `STD(max(ret,0),20)` | `股票历史日行情信息表后复权.ClosePrice` |
| feat_040 | `vol_realized_rv` | 已实现波动率RV (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `HF RV当日值 (建议 Lag 1)` | `个股已实现指标表日.RV` |
| feat_041 | `vol_realized_rrv` | 稳健已实现波动率RRV (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `HF RRV当日值 (建议 Lag 1)` | `个股已实现指标表日.RRV` |
| feat_042 | `vol_realized_rskew` | 已实现偏度RSkew (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `HF RSkew当日值 (建议 Lag 1)` | `个股已实现指标表日.RSkew` |
| feat_043 | `vol_realized_rkurt` | 已实现峰度RKurt (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `HF RKurt当日值 (建议 Lag 1)` | `个股已实现指标表日.RKurt` |
| feat_044 | `vol_jump_zadj` | 跳跃显著性Z值 (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `HF Z_Adj当日值 (建议 Lag 1)` | `个股跳跃指标表日.Z_Adj` |
| feat_045 | `vol_jump_rjv_ratio` | 跳跃波动占比(RJV/RV) (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `RJV/(RV+eps) (建议 Lag 1)` | `个股跳跃指标表日.RJV,RV` |
| feat_046 | `vol_jump_sjv_ratio` | 符号跳跃占比(SJV/RV) (高频当日值，建议使用 Ref(...,1) 以防未来函数) | `SJV/(RV+eps) (建议 Lag 1)` | `个股跳跃指标表日.SJV,RV` |

### 4.4 成交量（`volume`，22维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_047 | `liq_turnover_os` | 流通换手率 | `ToverOs` | `个股换手率表日.ToverOs` |
| feat_048 | `liq_turnover_tl` | 总股本换手率 | `ToverTl` | `个股换手率表日.ToverTl` |
| feat_049 | `liq_volume` | 当日成交量 | `Volume_t` | `股票历史日行情信息表后复权.Volume` |
| feat_050 | `liq_volume_ma_5` | 5日平均成交量 | `MA(Volume,5)` | `股票历史日行情信息表后复权.Volume` |
| feat_051 | `liq_volume_ma_10` | 10日平均成交量 | `MA(Volume,10)` | `股票历史日行情信息表后复权.Volume` |
| feat_052 | `liq_volume_ma_20` | 20日平均成交量 | `MA(Volume,20)` | `股票历史日行情信息表后复权.Volume` |
| feat_053 | `liq_volume_ratio_5` | 量比(5日) | `Volume/MA(Volume,5)` | `股票历史日行情信息表后复权.Volume` |
| feat_054 | `liq_volume_ratio_20` | 量比(20日) | `Volume/MA(Volume,20)` | `股票历史日行情信息表后复权.Volume` |
| feat_055 | `liq_amount` | 当日成交额 | `Amount_t` | `股票历史日行情信息表后复权.Amount` |
| feat_056 | `liq_amount_ma_5` | 5日平均成交额 | `MA(Amount,5)` | `股票历史日行情信息表后复权.Amount` |
| feat_057 | `liq_amount_ma_10` | 10日平均成交额 | `MA(Amount,10)` | `股票历史日行情信息表后复权.Amount` |
| feat_058 | `liq_amount_ma_20` | 20日平均成交额 | `MA(Amount,20)` | `股票历史日行情信息表后复权.Amount` |
| feat_059 | `liq_amount_ratio_5` | 额比(5日) | `Amount/MA(Amount,5)` | `股票历史日行情信息表后复权.Amount` |
| feat_060 | `liq_amount_ratio_20` | 额比(20日) | `Amount/MA(Amount,20)` | `股票历史日行情信息表后复权.Amount` |
| feat_061 | `liq_trade_count` | 成交笔数 | `Toltrdtims` | `日交易统计文件.Toltrdtims` |
| feat_062 | `liq_avg_trade_size` | 平均每笔成交额 | `Tolstknva/Toltrdtims` | `日交易统计文件.Tolstknva,Toltrdtims` |
| feat_063 | `liq_obv_20` | OBV趋势(20日) | `OBV窗口20` | `股票历史日行情信息表后复权.ClosePrice,Volume` |
| feat_064 | `liq_obv_60` | OBV趋势(60日) | `OBV窗口60` | `股票历史日行情信息表后复权.ClosePrice,Volume` |
| feat_065 | `liq_mfi_14` | 资金流量指标MFI14 | `MFI窗口14` | `股票历史日行情信息表后复权.HighPrice,LowPrice,ClosePrice,Amount` |
| feat_066 | `liq_accdist_20` | A/D累积线20日 | `AD窗口20` | `股票历史日行情信息表后复权.HighPrice,LowPrice,ClosePrice,Amount` |
| feat_067 | `liq_amihud_20` | Amihud非流动性20日 | `MA(\|ret\|/Amount,20)` | `股票历史日行情信息表后复权.ClosePrice,Amount` |
| feat_068 | `liq_amihud_60` | Amihud非流动性60日 | `MA(\|ret\|/Amount,60)` | `股票历史日行情信息表后复权.ClosePrice,Amount` |

### 4.5 资金流（`fund_flow`，22维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_069 | `flow_net_amount` | 总净流入额 | `B_Amount-S_Amount` | `个股买卖不平衡指标表日.B_Amount,S_Amount` |
| feat_070 | `flow_net_amount_ratio` | 总净流入占比 | `(B_Amount-S_Amount)/(B_Amount+S_Amount)` | `个股买卖不平衡指标表日.B_Amount,S_Amount` |
| feat_071 | `flow_large_net_amount` | 大单净流入额 | `B_Amount_L-S_Amount_L` | `个股买卖不平衡指标表日.B_Amount_L,S_Amount_L` |
| feat_072 | `flow_large_net_ratio` | 大单净流入占比 | `(B_Amount_L-S_Amount_L)/(B_Amount_L+S_Amount_L)` | `个股买卖不平衡指标表日.B_Amount_L,S_Amount_L` |
| feat_073 | `flow_medium_net_amount` | 中单净流入额 | `B_Amount_M-S_Amount_M` | `个股买卖不平衡指标表日.B_Amount_M,S_Amount_M` |
| feat_074 | `flow_medium_net_ratio` | 中单净流入占比 | `(B_Amount_M-S_Amount_M)/(B_Amount_M+S_Amount_M)` | `个股买卖不平衡指标表日.B_Amount_M,S_Amount_M` |
| feat_075 | `flow_small_net_amount` | 小单净流入额 | `B_Amount_S-S_Amount_S` | `个股买卖不平衡指标表日.B_Amount_S,S_Amount_S` |
| feat_076 | `flow_small_net_ratio` | 小单净流入占比 | `(B_Amount_S-S_Amount_S)/(B_Amount_S+S_Amount_S)` | `个股买卖不平衡指标表日.B_Amount_S,S_Amount_S` |
| feat_077 | `flow_net_order_count` | 净买入委托笔数 | `B_Num-S_Num` | `个股买卖不平衡指标表日.B_Num,S_Num` |
| feat_078 | `flow_net_order_ratio` | 净买入委托占比 | `(B_Num-S_Num)/(B_Num+S_Num)` | `个股买卖不平衡指标表日.B_Num,S_Num` |
| feat_079 | `flow_large_net_order` | 大单净委托笔数 | `B_Num_L-S_Num_L` | `个股买卖不平衡指标表日.B_Num_L,S_Num_L` |
| feat_080 | `flow_large_order_ratio` | 大单净委托占比 | `(B_Num_L-S_Num_L)/(B_Num_L+S_Num_L)` | `个股买卖不平衡指标表日.B_Num_L,S_Num_L` |
| feat_081 | `flow_vpin` | VPIN当日值 | `VPIN_t` | `个股知情交易概率指标表日.VPIN` |
| feat_082 | `flow_vpin_ma_5` | VPIN五日均值 | `MA(VPIN,5)` | `个股知情交易概率指标表日.VPIN` |
| feat_083 | `flow_vpin_ma_20` | VPIN二十日均值 | `MA(VPIN,20)` | `个股知情交易概率指标表日.VPIN` |
| feat_084 | `flow_vpin_delta_5` | VPIN五日变化 | `VPIN_t-VPIN_{t-5}` | `个股知情交易概率指标表日.VPIN` |
| feat_085 | `flow_qsp` | 报价价差Qsp | `Qsp_equal` | `个股买卖价差表日.Qsp_equal` |
| feat_086 | `flow_esp` | 有效价差Esp | `Esp_equal` | `个股买卖价差表日.Esp_equal` |
| feat_087 | `flow_aqsp` | 加权报价价差AQsp | `AQsp_equal` | `个股买卖价差表日.AQsp_equal` |
| feat_088 | `flow_qsp_time` | 时间加权报价价差 | `Qsp_time` | `个股买卖价差表日.Qsp_time` |
| feat_089 | `flow_esp_time` | 时间加权有效价差 | `Esp_time` | `个股买卖价差表日.Esp_time` |
| feat_090 | `flow_pressure_index` | 资金压力指数 | `z(flow_net_amount)+z(VPIN)+z(Esp)` | `个股买卖不平衡指标表日 + 个股知情交易概率指标表日 + 个股买卖价差表日` |

### 4.6 风格因子（`style`，16维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_091 | `style_ln_mv_total` | 总市值对数 | `ln(MarketValue)` | `股票历史日行情信息表后复权.MarketValue` |
| feat_092 | `style_ln_mv_float` | 流通市值对数 | `ln(CirculatedMarketValue)` | `股票历史日行情信息表后复权.CirculatedMarketValue` |
| feat_093 | `style_bp` | 账面市净率倒数(B/P) | `1/PB` | `相对价值指标.pb_ratio` |
| feat_094 | `style_ep_ttm` | 盈利收益率(E/P) | `1/PE_TTM` | `相对价值指标.pe_ratio_ttm` |
| feat_099 | `style_smb` | 小盘风格因子暴露 | `SMB1` | `三因子模型指标日.SMB1` |
| feat_100 | `style_hml` | 价值风格因子暴露 | `HML1` | `三因子模型指标日.HML1` |
| feat_101 | `style_mkt_premium` | 市场风险溢价 | `RiskPremium1` | `三因子模型指标日.RiskPremium1` |
| feat_102 | `style_beta_20` | 20日市场Beta | `cov(ret,mkt)/var(mkt),window=20` | `股票历史日行情信息表后复权.ChangeRatio + 指数文件.Retindex` |
| feat_103 | `style_beta_60` | 60日市场Beta | `window=60` | `股票历史日行情信息表后复权.ChangeRatio + 指数文件.Retindex` |
| feat_104 | `style_beta_120` | 120日市场Beta | `window=120` | `股票历史日行情信息表后复权.ChangeRatio + 指数文件.Retindex` |
| feat_105 | `style_idio_vol_20` | 20日特质波动 | `STD(ret-beta*mkt,20)` | `股票历史日行情信息表后复权 + 指数文件` |
| feat_106 | `style_idio_vol_60` | 60日特质波动 | `STD(ret-beta*mkt,60)` | `股票历史日行情信息表后复权 + 指数文件` |
| feat_107 | `style_residual_ret_20` | 20日残差收益 | `sum(ret-beta*mkt,20)` | `股票历史日行情信息表后复权 + 指数文件` |
| feat_108 | `style_valuation_composite` | 估值复合分 | `rank(BP)+rank(EP)+rank(CFP)` | `相对价值指标` |
| feat_109 | `style_size_percentile` | 规模分位数 | `cs_percentile(ln_mv_total)` | `股票历史日行情信息表后复权.MarketValue` |
| feat_110 | `style_value_percentile` | 价值分位数 | `cs_percentile(valuation_composite)` | `相对价值指标` |

### 4.7 行业因子（`industry`，20维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_111 | `ind_ret_1d` | 所属行业1日收益 | `industry_mean(ret,1d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_112 | `ind_ret_5d` | 所属行业5日收益 | `industry_mean(ret,5d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_113 | `ind_ret_10d` | 所属行业10日收益 | `industry_mean(ret,10d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_114 | `ind_ret_20d` | 所属行业20日收益 | `industry_mean(ret,20d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_115 | `ind_vol_20` | 行业20日波动率 | `industry_std(ret,20d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_116 | `ind_turnover_20` | 行业20日平均换手率 | `industry_mean(ToverOs,20d)` | `个股换手率表日.ToverOs + 公司文件.Indcd` |
| feat_117 | `ind_amount_20` | 行业20日平均成交额 | `industry_mean(Amount,20d)` | `股票历史日行情信息表后复权.Amount + 公司文件.Indcd` |
| feat_118 | `ind_strength_20` | 个股相对行业强度20日 | `ret_20d-ind_ret_20d` | `股票历史日行情信息表后复权 + 公司文件.Indcd` |
| feat_119 | `ind_strength_60` | 个股相对行业强度60日 | `ret_60d-ind_ret_60d` | `股票历史日行情信息表后复权 + 公司文件.Indcd` |
| feat_120 | `ind_dispersion_20` | 行业横截面离散度 | `industry_std(stock_ret,20d)` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_121 | `ind_up_breadth_20` | 行业上涨广度 | `up_stock_count/stock_count` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_122 | `ind_down_breadth_20` | 行业下跌广度 | `down_stock_count/stock_count` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_123 | `ind_relative_volume_20` | 个股相对行业量比 | `stock_vol_ma20/industry_vol_ma20` | `股票历史日行情信息表后复权.Volume + 公司文件.Indcd` |
| feat_124 | `ind_relative_volatility_20` | 个股相对行业波动 | `stock_vol_20/industry_vol_20` | `股票历史日行情信息表后复权.ChangeRatio + 公司文件.Indcd` |
| feat_125 | `ind_relative_flow_20` | 个股相对行业资金流 | `stock_flow_20-industry_flow_20` | `个股买卖不平衡指标表日 + 公司文件.Indcd` |
| feat_126 | `ind_momentum_rank_20` | 行业动量排名 | `rank(ind_ret_20)` | `股票历史日行情信息表后复权 + 公司文件.Indcd` |
| feat_127 | `ind_value_rank` | 行业价值排名 | `rank(industry_mean(BP))` | `相对价值指标 + 公司文件.Indcd` |
| feat_128 | `ind_size_rank` | 行业规模排名 | `rank(industry_mean(MarketValue))` | `股票历史日行情信息表后复权.MarketValue + 公司文件.Indcd` |
| feat_129 | `ind_code_l1` | 一级行业编码数值化 | `hash_or_dict_encode(Indcd level1)` | `公司文件.Indcd/Nindcd` |
| feat_130 | `ind_code_l2` | 二级行业编码数值化 | `hash_or_dict_encode(Indcd level2)` | `公司文件.Nindcd/Nnindcd` |

### 4.8 微观结构（`microstructure`，20维）

| 编号 | 字段Key | 字段解释 | 计算口径（示例） | 原始来源表/字段 |
|---|---|---|---|---|
| feat_131 | `micro_qsp_equal` | 报价价差(等权) | `Qsp_equal` | `个股买卖价差表日.Qsp_equal` |
| feat_132 | `micro_esp_equal` | 有效价差(等权) | `Esp_equal` | `个股买卖价差表日.Esp_equal` |
| feat_133 | `micro_aqsp_equal` | 报价价差(成交量加权) | `AQsp_equal` | `个股买卖价差表日.AQsp_equal` |
| feat_134 | `micro_qsp_time` | 报价价差(时间加权) | `Qsp_time` | `个股买卖价差表日.Qsp_time` |
| feat_135 | `micro_esp_time` | 有效价差(时间加权) | `Esp_time` | `个股买卖价差表日.Esp_time` |
| feat_136 | `micro_qsp_volume` | 报价价差(成交量加权版本) | `Qsp_Volume` | `个股买卖价差表日.Qsp_Volume` |
| feat_137 | `micro_esp_volume` | 有效价差(成交量加权版本) | `Esp_Volume` | `个股买卖价差表日.Esp_Volume` |
| feat_138 | `micro_qsp_amount` | 报价价差(成交额加权版本) | `Qsp_Amount` | `个股买卖价差表日.Qsp_Amount` |
| feat_139 | `micro_esp_amount` | 有效价差(成交额加权版本) | `Esp_Amount` | `个股买卖价差表日.Esp_Amount` |
| feat_140 | `micro_effective_spread` | 有效点差代理 | `2*\|trade_price-mid_quote\|/mid_quote` | `个股买卖价差表日 + 股票历史日行情信息表后复权` |
| feat_141 | `micro_quoted_spread` | 报价点差代理 | `(ask-bid)/mid_quote` | `个股买卖价差表日相关字段` |
| feat_142 | `micro_spread_vol_20` | 点差波动20日 | `STD(Esp_equal,20)` | `个股买卖价差表日.Esp_equal` |
| feat_143 | `micro_imbalance_volume` | 成交量不平衡 | `(B_Volume-S_Volume)/(B_Volume+S_Volume)` | `个股买卖不平衡指标表日.B_Volume,S_Volume` |
| feat_144 | `micro_imbalance_amount` | 成交额不平衡 | `(B_Amount-S_Amount)/(B_Amount+S_Amount)` | `个股买卖不平衡指标表日.B_Amount,S_Amount` |
| feat_145 | `micro_imbalance_count` | 委托笔数不平衡 | `(B_Num-S_Num)/(B_Num+S_Num)` | `个股买卖不平衡指标表日.B_Num,S_Num` |
| feat_146 | `micro_imbalance_large` | 大单不平衡 | `(B_Amount_L-S_Amount_L)/(B_Amount_L+S_Amount_L)` | `个股买卖不平衡指标表日.B_Amount_L,S_Amount_L` |
| feat_147 | `micro_imbalance_medium` | 中单不平衡 | `(B_Amount_M-S_Amount_M)/(B_Amount_M+S_Amount_M)` | `个股买卖不平衡指标表日.B_Amount_M,S_Amount_M` |
| feat_148 | `micro_imbalance_small` | 小单不平衡 | `(B_Amount_S-S_Amount_S)/(B_Amount_S+S_Amount_S)` | `个股买卖不平衡指标表日.B_Amount_S,S_Amount_S` |
| feat_149 | `micro_jump_flag` | 跳跃事件标记 | `ISJump(0/1)` | `个股跳跃指标表日.ISJump` |
| feat_150 | `micro_pressure_score` | 微观结构压力分 | `z(Esp)+z(VPIN)+z(imbalance_amount)` | `个股买卖价差表日 + 个股知情交易概率指标表日 + 个股买卖不平衡指标表日` |

## 5. 治理与验收
- 所有训练、回测、推理请求必须携带并落盘：`feature_set_version`、`feature_columns`、`schema_checksum`。
- 前端不硬编码字段全集，以目录接口返回为准；字段顺序以接口 `order/order_no` 为准。
- 新增字段进入新版本，旧版本保留至少一个发布周期用于回放。
- 训练前执行白名单校验：提交特征必须属于当前生效目录。
