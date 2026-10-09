# 方法论

## 核心原则

1. 现金分红与拆并股分开处理。
2. 并购、代码变更、供股（配股）需要稳定证券标识符与专门建模；**未建模的事件不得当作已通过**（单列为已知缺口）。
3. 无法解释的跳点不得自动平滑。

## 收益核对公式

对同一证券相邻两行，以当前行 `t` 为事件日（除权除息日）：

- 原始总收益：`(close_t × split_factor_t + cash_dividend_t) / close_(t-1) - 1`
- 复权收益：`adj_close_t / adj_close_(t-1) - 1`

其中 `split_factor_t` 是每一旧股对应的新股数（1=无事件），`cash_dividend_t` 是每一旧股现金分红（除息日当天记在当前行）。两者的事件时点、币种和单位必须与价格序列一致。

拆股/送股对齐检查（无复权序列市场使用）：事件日应有 `close_t × ratio / close_(t-1) ≈ 1`（容忍当日真实波动）；显著偏离意味着价格已提前复权（双重调整）或漏调整。

## 本地口径速查（QuantDB，2026-10 标定）

| 市场 | 原始序列 | 复权序列 | 事件源 |
|---|---|---|---|
| CN | `daily_unadjusted` | `daily_forward`（前复权） | `dividend_factors`（interest/10 派息，stockBonus/10 送转；配股未建模） |
| HK | `daily_forward`（不复权） | 无全量复权序列 | `{dividend,splits}`（yahoo，trade_date=除净日） |
| US | `daily_forward`（不复权） | 无 | `{dividend,splits}`（yahoo） |

HK/US 无复权序列时，等式核对**不可执行**——如实报 `insufficient-evidence` 语义，不得以「跳点检查通过」冒充整体通过。

## 推荐执行顺序

1. 冻结输入快照、时间窗、时区、单位和标识符。
2. 运行脚本并保存 JSON，不在原始文件上就地修改。
3. 人工复核所有高严重度发现，区分确定性错误与启发式风险。
4. 改变参数时保留前后版本并解释原因。
5. 在独立样本或压力场景复算，不用单一历史窗口证明稳健。

## 解释规则

- `pass` 只表示已执行的检查未发现问题，不代表策略有效或未来盈利。
- `fail` 必须附具体记录、字段或计算证据。
- `insufficient-evidence` 用于关键字段、历史版本或真实执行信息缺失。

## 主要参考

- [S&P Index Mathematics Methodology](https://www.spglobal.com/spdji/en/methodology/article/index-mathematics-methodology/)
- [Nasdaq Corporate Actions Manual](https://indexes.nasdaq.com/docs/Corporate_Actions_and_Events_Manual_Equities.pdf)

> 来源：quantskills/skill-corporate-action-adjustment-auditor（GPL-3.0-only），本地化改写（数据层换 QuantDB）。
