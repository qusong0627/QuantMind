# QuantMind 策略编写手册 v2.0 (基本面过滤与对齐版)

## 1. 核心理念：统一对齐 (Single Source of Truth)

为了彻底解决“回测完美，实盘踩坑”的指标不一致问题，QuantMind 引入了**基本面统一对齐器 (Fundamental Aligner)**。
- **回测时**：强制读取离线快照，模拟当日真实的决策环境。
- **实盘时**：读取同样的对齐快照，确保拦截逻辑与回测完全一致。

## 2. 简洁模式：参数化过滤 (Mixin 2.0)

你不再需要编写复杂的 Python 逻辑，只需在 `STRATEGY_CONFIG` 的 `kwargs` 中通过 `f_` 前缀直接配置过滤条件。

### 2.1 参数语法规则

| 语法示例 | 逻辑意义 | 内部操作符 |
| :--- | :--- | :--- |
| `f_{field}_max` | 指标**小于等于**该值 | `<= (le)` |
| `f_{field}_min` | 指标**大于等于**该值 | `>= (ge)` |
| `f_{field}_not` | 指标**不等于**该值 | `!= (ne)` |
| `f_{field}_in` | 指标**属于**该集合 | `in (isin)` |
| `{field}` (无后缀) | 指标**等于**该值 | `== (eq)` |

### 2.2 基础字段映射 (常用项)

你可以直接调用《实盘交易因子筛选手册》中的 88 个字段。常用字段如下：

- **估值类**: `pe_ttm`, `pb`, `total_mv` (总市值), `float_mv` (流通市值), `roe`
- **状态类**: `is_st` (1为ST), `listed_days` (上市天数)
- **分类类**: `industry`, `listing_market`, `idx_hs300` (1为沪深300成分)

---

## 3. 全量因子速查表 (88 维)

| 分类 | 字段代码 | 含义 | 单位/格式 |
| :--- | :--- | :--- | :--- |
| **基础信息** | `listed_days` | 上市天数 | 天 |
| | `is_st` | ST 状态 | 0/1 |
| | `industry` | 所属行业 | 字符串 (申万一级) |
| **估值指标** | `pe_ttm` | 动态市盈率 | 倍 |
| | `pb` | 市净率 | 倍 |
| | `total_mv` | 总市值 | 元 (CNY) |
| | `roe` | 净资产收益率 | 百分比 (12 = 12%) |
| **收益动量** | `return_5d` | 5日收益率 | 比率 (0.05 = 5%) |
| | `return_20d` | 20日收益率 | 比率 (0.1 = 10%) |
| **技术指标** | `rsi_6` | RSI 6 | 0 - 100 |
| | `ma_gap_20` | 20日均线偏离 | 比率 |
| | `vol_std_20` | 20日波动率 | 比率 |
| **量能资金** | `turnover_rate` | 换手率 | 百分比 (5 = 5%) |
| | `volume_ratio_5`| 5日量比 | 倍 |
| | `inst_ownership`| 机构持仓 | 比率 (0.3 = 30%) |
| **指数状态** | `idx_hs300` | 沪深300成分 | 0/1 |
| | `limit_up_today`| 今日涨停 | 0/1 |

> [!TIP]
> 完整字段清单请参考 [实盘交易因子筛选手册](file:///Users/qusong/git/quantmind/docs/%E5%AE%9E%E7%9B%98%E4%BA%A4%E6%98%93%E5%9B%A0%E5%AD%90%E7%AD%9B%E9%80%89%E6%89%8B%E5%86%8C.md)。在编写 `f_` 参数时，请务必注意**比率**与**百分比**的单位区别。

---

## 4. 策略模板示例

### 3.1 价值成长策略 (Value Growth)
剔除 ST，市值 10 亿-500 亿，PE 低于 25，ROE 高于 12%。

```python
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 5,
        
        # --- 基本面过滤参数 (自动识别 f_ 前缀) ---
        "f_is_st_not": 1,           # 剔除 ST
        "f_total_mv_min": 1e9,      # 市值下限 10亿
        "f_total_mv_max": 5e10,     # 市值上限 500亿
        "f_pe_ttm_max": 25,         # PE 低于 25
        "f_roe_min": 0.12,          # ROE 高于 12%
        "f_listed_days_min": 365    # 上市满一年
    }
}
```

### 3.2 行业专注策略 (Industry Specific)
只在“医药”或“电子”行业中选股。

```python
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 10,
        
        # --- 行业动态过滤 ---
        "f_industry_in": ["医药", "电子"],
        "f_idx_zz1000": 1,          # 只看中证 1000 成分股
        "f_is_st_not": 1
    }
}
```

---

## 4. 底层保障机制

### 4.1 原子性更新
数据同步脚本 `sync_factors_to_parquet.py` 采用 **Atomic Write** 机制。在实盘运行期间更新因子文件，不会造成策略读取中断或文件损坏。

### 4.2 新鲜度校验 (Freshness Guard)
基本面对齐数据来自 QuantDB `features_daily`，跟随每日同步更新即为最新。
- 确保已执行 `quantdb_daily_sync.py`，使 `features_daily` 覆盖到最近交易日。
- 基于最新基本面快照做决策。

### 4.3 性能优化
对齐器在初次加载后会将数据驻留内存。对于全市场 5000+ 股票的 100 个指标过滤，耗时通常控制在 **10ms** 以内。

---

## 5. 常见问题 (FAQ)

**Q: 我填了参数但是没生效？**
A: 请检查 QuantDB `features_daily` 中是否存在该列（如 `revenue_ttm`、`total_mv`、`pe_ttm` 等）。缺失的列会被对齐器 **静默跳过**（不报错也不生效），见对齐器 `fundamental_aligner.filter_instruments`。常用字段若干：revenue_ttm、net_profit_ttm、annual_net_profit、equity、total_mv、float_mv、pe_ttm、pe_static、pb、ps_ttm、dividend_rate、vol_std_20/60、vol_atr_14、beta_20、ma_gap_5/20、rsi_14、kdj_j、macd_hist、pct_change、vol_to_ma5。注意：features_daily 不含 roe、is_st。

**Q: 盘中实时涨幅能用这个过滤吗？**
A: 建议仅对静态或低频因子（PE、市值、ST、行业）使用 `f_` 参数。对于盘中实时涨幅，建议在策略的 `generate_target_weight_position` 中动态调用行情接口。

**Q: 如果没有对齐文件会怎样？**
A: 系统会记录一条 Warning 日志并“放行”所有股票，不会导致回测或实盘进程崩溃。

---
*QuantMind 研发团队 - 2026.05.09 更新*
