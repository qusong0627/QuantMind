# 输出契约

建议 JSON 顶层字段：

```json
{
  "status": "pass | fail | warning | insufficient-evidence",
  "input_summary": {},
  "assumptions": {},
  "metrics": {},
  "findings": [],
  "limitations": [],
  "next_actions": []
}
```

每个 finding 至少包含：`id`、`severity`、`evidence`、`impact`、`recommended_fix`。

严重度使用：`critical`、`high`、`medium`、`low`、`info`。没有可定位证据时，不得输出 `critical` 或 `high`。

状态语义：

- `pass`：已执行的检查未发现问题（不代表策略有效或数据未来可用）。
- `fail`：存在已证实的冲突（如原始总收益与复权收益超容忍偏差，且证据行可定位）。
- `warning`：存在启发式异常或低严重度发现（如未解释跳点、来源重复行）。
- `insufficient-evidence`：关键字段、事件源或历史版本缺失，任何定量结论都不可给出。

> 来源：quantskills/skill-corporate-action-adjustment-auditor（GPL-3.0-only），本地化照搬。
