# 输出契约（JSON 报告）

`scripts/backtest_bias_audit.py` 唯一机读产物为 JSON（`--out`；缺省打到 stdout）。

## 顶层字段

```json
{
  "status": "pass | fail | warning | insufficient-evidence",
  "input_summary": {},
  "assumptions": {},
  "headline": {},
  "pbo": {}, "dsr": {}, "look_ahead": {}, "walk_forward": {},
  "cost_sensitivity": {}, "segment": {},
  "performance_full_sample": {}, "performance_full_hac": {},
  "scan": [], "metrics": {},
  "findings": [], "limitations": [], "next_actions": [], "disclaimer": "..."
}
```

## 状态语义

- `pass`：已执行的检查未触发任何 high/medium 规则（**不代表策略有效**）。
- `fail`：存在**已证实**的高风险问题（PBO>50% / DSR<95% / 扣费归零 / 前视虚高>0.5）。
- `warning`：仅中/低风险发现（如样本外 CI 含 0、下注数不足、PBO 中等）。
- `insufficient-evidence`：核心检验无法执行（有效试验列 <2、样本 <100 行、数据缺失）——任何定量结论都不可给出。

## findings 条目

每个 finding 含：`id`、`severity`、`type`、`evidence`、`impact`、`recommended_fix`。

- `type = "confirmed-issue"`：已证实的问题（severity ∈ `high` / `medium` / `low` / `info`）。
- `type = "missing-evidence"`：**缺失证据**（severity 固定 `insufficient-evidence`）——不得当通过，
  需按 `recommended_fix` 补齐后重跑。

**阅读顺序**：先读全部 `confirmed-issue` 的 high/medium（附证据数值与触发阈值）；
再读 `missing-evidence` 整理补证据清单；两者不得混同、也不得互相替代。

## 各模式可用段（其余段为 `{"available": false, "reason": ...}`）

| 段 | demo / quantdb | input |
|---|---|---|
| pbo / dsr / scan / headline / walk_forward（需日期列） | ✅ | ✅（walk_forward 需日期列） |
| look_ahead（干净 vs 泄漏量化） | ✅ | ❌ 无持仓序列 |
| cost_sensitivity / segment（下注数、换手、在场占比） | ✅ | ❌ 收益已含成本口径，不再扣费 |
| performance_full_sample / performance_full_hac | ✅ | ✅ |

## 数值口径速记

- `headline`：样本外·扣费后·HAC，`wording` 已按措辞纪律生成（不显著时为「无显著净边际」）。
- `pbo.pbo` 为该矩阵的单路径点值，`ci95` 为 λ 自助区间（因划分重叠而偏窄）；单路径方差大（见 SKILL「常见坑」）。
- `dsr.n_trials` 为显式网格试验数（= 去重后列数或 `--dsr-trials`），DSR 为乐观上界。
- `scan[].is_sharpe / oos_sharpe`：价格模式为**净口径**；input 模式为输入序列自身口径。
- 非有限值一律输出 `null`（如无下注、无穿零点、Sortino 分母为 0）。

> 来源：quantskills/skill-backtesting-bias-avoidance（GitHub license 元数据 NOASSERTION），
> 输出契约按其报告蓝图本地化改写为 JSON；与本地技能家族（corporate-action-adjustment-auditor）同构。
