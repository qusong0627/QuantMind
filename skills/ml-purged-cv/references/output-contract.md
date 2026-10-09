# 输出契约：JSON 报告 + 可导入 API

## 1. CLI 行为

```bash
python3 purged_cv.py --demo                                             # 表格摘要到 stdout
python3 purged_cv.py --quantdb --market CN --start ... --end ... --out r.json
python3 purged_cv.py --quantdb --market CN --print-json                 # 完整 JSON 到 stdout
```

- 成功=正常退出并打印摘要表；参数/数据错误=SystemExit + 中文报错（不产半份报告）。
- `--out` 落盘的 JSON 与 `--print-json` 内容一致（`_out_written` 字段仅在落盘时附加）。
- `--market` 非 CN 直接拒绝：HK/US 的 `daily_forward` 不复权，前视收益口径需先另定。

## 2. JSON 报告 schema（schema_version="1"）

```jsonc
{
  "schema_version": "1",
  "tool": "ml-purged-cv",
  "script": "purged_cv.py",
  "status": "success",                 // 仅完成时落盘；失败走异常
  "mode": "demo" | "quantdb",
  "generated_at": "2026-10-08T12:00:00Z",   // UTC；容器时钟可能偏，仅记录用
  "elapsed_sec": 13.1,
  "config": { "n_splits": 5, "embargo_sessions": 5, "model": "ridge",
              "alpha": 1.0, "seed": 42, "horizon": 5, "lookback": 20 },
  "dataset": { "market": "CN", "sessions": 721, "symbols": 78,
               "observations": 55466, "start": "2023-10-09", "end": "2026-09-22",
               "features": ["ma_gap_5", "..."],
               "label_definition": "daily_forward(前复权) close 的 5 日前视收益",
               "interval_definition": "[session-19, session+5]（闭区间，单位=交易日序号）" },
  "channels": {
    "<channel_name>": {
      "ic_pooled": 0.055201,           // 各折 OOS 预测拼接后的 Pearson IC
      "rank_ic_pooled": 0.030621,      // Spearman（平均秩，含并列）
      "mse_pooled": 0.00061,           // 同上拼接口径
      "ic_mean_fold": 0.064453,        // 逐折 IC 的均值（折间等权，与 pooled 不同）
      "per_fold_ic": [0.07, 0.05, -0.01, 0.08, 0.09],
      "oos_rows": 221864,
      "evidence": "safe" | "unsafe",   // safe=Purge/Embargo 通道；unsafe=对照通道
      "retained_train_overlapping": 0, // 保留训练样本中信息区间仍与测试相交的个数
      "exclusions": { "purged": 14765, "embargoed": 0, "retained": 207099 }
    }
  },
  "leakage_control_status": "PASS" | "FAIL",
  "calibration": {
    "claim": "同数据同模型下 purged+embargo 测试集 IC 应 ≤ vanilla（泄漏被堵住通常使指标下降）",
    "vanilla_ic_pooled": 0.055201,
    "purged_embargo_ic_pooled": 0.037137,
    "purged_le_vanilla": true,         // null=某通道缺数（未比对）
    "vanilla_retained_train_overlapping": 221396,
    "note": "如实报告：IC 差不是纯泄漏因果量；结构不变量（安全通道重叠=0）才是硬证据"
  },
  "structural_checks": { /* 仅 --demo：CPCV/Walk-Forward 全组合重叠审计 */ },
  "warnings": [ "..." ]                // 含选样口径、NO_INCREMENTAL_EXCLUSION、免责声明
}
```

通道名固定四个：`vanilla-shuffled-kfold`、`vanilla-chronological-kfold`、`purged-kfold`、`purged-kfold-embargo`。

### 语义

- `leakage_control_status=PASS` ⇔ 所有 `evidence="safe"` 通道 `retained_train_overlapping==0`。它断言的是**被审计的划分本身干净**，不是「模型无泄漏」、更不是「可上线」。
- `exclusions` 为各折求和：`purged`=区间重叠剔除数，`embargoed`=Embargo 增量剔除数，`retained`=保留训练样本数。`embargoed` 求和为 0 且走的是 purged-kfold-embargo 通道 → 报告附加 NO_INCREMENTAL_EXCLUSION 告警（见 methodology §3）。
- `ic_mean_fold`（折间等权）与 `ic_pooled`（样本等权拼接）不同：长短折不等权时两者会分离，报告同时给出，禁止只挑好看的报。
- `structural_checks` 仅 demo 模式：CPCV（N=6,k=2）全组合重叠=0、Walk-Forward 因果性断言、vanilla 时序通道重叠对照。

## 3. 可导入 API 契约

```python
from purged_cv import PurgedKFold, CombinatorialPurgedCV, CausalWalkForward
```

- **坐标**：`sessions: list[int]`（每样本的整数 session 序号）、`interval_start/end: list[int]`（同长度，闭区间）。三个数组用**位置**索引对齐；返回的 `FoldAssignment.train/test/purged/...` 全部是**位置索引元组**（不是 session 号）。
- `PurgedKFold(n_splits=5, embargo_sessions=0).split(sessions, interval_start, interval_end)` → 生成器，每折一个 `FoldAssignment`（`fold_index, train, test, purged, embargoed, pre_test_gapped, test_blocks`，含 `exclusion_counts` 属性）。
- `CombinatorialPurgedCV(n_groups=6, n_test_groups=2, embargo_sessions=0)`：`combination_count` / `path_count` / `.split(...)`（每组合一个 FoldAssignment，多出 `combination_index, test_group_indices`）/ `.path_decomposition()`（tuple[tuple[(组合号, 组号)]]）。
- `CausalWalkForward(n_splits=5, test_sessions=20, pre_test_gap_sessions=0, max_train_sessions=None).split(...)` → 测试块在轴末端，保留训练样本信息终点严格早于块起点。
- 纯函数：`cpcv_path_decomposition(n_groups, n_test_groups)`；`merged_intervals`；`overlapping_positions`（bisect 近线性）；`apply_exclusions`；`retained_overlap_count`；`spearman_ic`。
- numpy 评估层（需 numpy）：`fold_local_fit_predict(x_train, y_train, x_test, model="ridge"|"ols"|"hgb", alpha, seed)`；`evaluate_folds(folds, features, targets, ...)`。

### sklearn 适配层（需 sklearn）

```python
from purged_cv import SklearnPurgedKFold
cv = SklearnPurgedKFold(n_splits=5, embargo_sessions=5,
                        interval_start=starts, interval_end=ends)  # 缺省 start=end=session
cross_val_score(model, X, y, groups=sessions, cv=cv)
```

- `groups=` 必须传每样本 session 序号（`X` 行序对齐）；信息区间缺省退化为瞬时标签。
- 已在 sklearn 1.7.2 冒烟：适配层产出的 train/test 索引与核心 `PurgedKFold` 逐折全等。

## 4. 与来源输出的差异（缺口清单）

来源契约含逐样本排除轨迹（`include_exclusion_trace`）、`InvalidFold` 原因码、canonical digest 与更细的通道枚举（cpcv / causal-walk-forward 也进评估）。本实现刻意收窄：

- 无排除轨迹与 digest → 不能做「同一划分哈希复现」审计；
- 折有效性不自判（InvalidFold 未移植）→ 调用方需自查空折；
- CLI 评估通道只跑 4 个 K 折通道，CPCV/Walk-Forward 在 demo 里以结构断言（非 IC 通道）呈现。

需要上述能力时按本清单逐项补，不要默认「等同来源」。
