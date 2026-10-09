---
name: ml-purged-cv
description: "Purged/Embargo 防泄漏交叉验证（López de Prado 口径，QuantDB 直读 CN）— 为带重叠标签的金融时序机器学习生成区间级 Purge + Embargo 折划分，并与 vanilla K 折实测对比测试集 IC 落差（含 CPCV 路径分解、因果 Walk-Forward）。用户问「交叉验证泄漏」「K 折可不可信」「OOF 是不是高估」「purge/embargo 怎么设」时使用。触发词：purged CV、purge、embargo、防泄漏交叉验证、CPCV、purged kfold、walk-forward、重叠标签、OOF 高估"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` 只需 numpy，宿主机/容器皆可；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/ml-purged-cv/scripts/purged_cv.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/purged_cv.py --quantdb --market CN \
>      --start 2023-10-01 --end 2026-09-30 --out /tmp/purged_cv_full.json
>    ```
> 3. **报告落盘**：建议容器内 `/tmp/purged_cv_<窗口>.json` 或 `/data/reports/purged-cv/<market>_<窗口>.json`；改参数重跑时保留前后两份对比。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`（与 parquet 内一致）；`--market` 目前只支持 CN（HK/US 的 `daily_forward` 是不复权序列，前视收益口径需另定，脚本会直接拒绝）。
> 5. **窗口终点**：默认取 features_daily 最新分区（数据驱动），不看本机/容器时钟——容器时钟比宿主慢 1 小时不影响结果。

# ml-purged-cv — Purged/Embargo 防泄漏交叉验证

把交叉验证划分当作需要审计的对象：只要样本标签的「信息区间」互相重叠，任何随机或朴素时序切分都会让训练集携带测试标签的信息。本技能按 López de Prado 口径做**区间级** Purge + Embargo（不是「切块前后固定删 N 行」），并提供与 vanilla K 折的同数据同模型实测对比。产出的 IC 是诊断量，不是盈利证明。

## 能力总览

| 用法 | 内容 |
|---|---|
| `--demo` | 合成数据（AR(1) 潜状态 + 重叠标签）烟雾测试：四通道对比 + CPCV/Walk-Forward 结构不变量自检（需 numpy） |
| `--quantdb --market CN` | 读 `6_ml_datasets/features_daily` 因子 + 由 `1_kline_data/daily_forward`（前复权）自算 h 日前视收益标签；四通道实测 IC 对比，JSON 报告 |
| `import purged_cv` | 纯标准库索引生成器：`PurgedKFold` / `CombinatorialPurgedCV` / `CausalWalkForward`（返回位置索引与排除账目）；有 sklearn 时另提供 `SklearnPurgedKFold(BaseCrossValidator)`，可直接进 `cross_val_score`（已冒烟；`GridSearchCV` 同接口未单独验） |

四通道对比（同数据同模型）：`vanilla-shuffled-kfold`（乱序，错）、`vanilla-chronological-kfold`（时序但无排除，半错）、`purged-kfold`、`purged-kfold-embargo`。模型默认 fold-local 闭式 Ridge（numpy，无需 sklearn），`--model hgb` 可换 sklearn `HistGradientBoostingRegressor`。

## 数据映射（QuantDB 本地，CN）

| 用途 | 位置 | 口径 |
|---|---|---|
| 因子 | `quantdb/6_ml_datasets/features_daily/dt=YYYYMMDD/data.parquet` | 默认 `ma_gap_5,rsi_14,vol_std_20,macd_hist`（两代 schema 都存在、NaN<0.5%）；**不要用 `close`/`return_*`/`future_return_*` 列做标签**（见坑 1–3） |
| 标签 | `quantdb/1_kline_data/daily_forward/dt=.../data.parquet` | `label = close.shift(-h)/close − 1`（按 symbol 分组），前复权口径；脚本自算 |
| 交易日历 | 数据自身分区（features_daily ∪ daily_forward 的交集会话） | session 序号从 0 递增，不依赖外部日历 |
| 默认资产池 | 锚日（窗口终点）单日成交额的 top-N | **演示用确定性选样，非 PIT 资产池**，报告里会显式告警；正式研究请自备 PIT 池并传 `--symbols` |

信息区间口径：`interval_start = session − (lookback−1)`，`interval_end = session + horizon`（闭区间，单位=交易日）。默认 `--horizon 5 --lookback 20 --embargo 5`。

## 标准流程

1. 先 `--demo` 确认脚本与结构不变量自检全部通过（`leakage_control_status=PASS`）。
2. 冻结窗口/因子/参数，容器内 `--quantdb` 跑目标窗口，JSON 落盘。
3. 读报告：`channels.*.retained_train_overlapping`（安全通道必须为 0）→ `calibration` 块（purged ≤ vanilla？）→ `warnings`（含 NO_INCREMENTAL_EXCLUSION 提示）。
4. **接入任何本地训练管线前**：先核对集成点（见下）并保持「同数据同模型」对照原则，不允许只报 purged 数字而不报 vanilla 对照。

## 常见坑（2026-10-08 实测标定）

1. **`features_daily.close` ≡ `daily_backward.close`（逐点 0 差）**——即它站在已知损坏的后复权序列上。所以该表的 `return_Nd`/`future_return_Nd` 收益列**不做标签**；标签一律自 `daily_forward` 重算。
2. **旧列 `return_5d` 是「前视」收益且单位是百分数**：`4.33071` ↔ 自算 forward-5 收益 `0.0433071`（corr 0.9859 vs 未来 5 日；对过去 5 日 corr −0.1399）。若历史脚本按小数读取会差 100 倍。
3. **features_daily schema 漂移**：列数 50→78 发生在 `dt=20260911` 与 `dt=20260914` 之间；`return_Nd`→`future_return_Nd` 改名发生在 `dt=20260918` 与 `dt=20260921` 之间。脚本按每分区 `pq.read_schema` 取列交集读取，任何窗口都安全。
4. **标签列已停填**：`future_return_*`/`return_Nd` 最后有值日 `20260828`，`20260831` 起全空（新 schema 下 100% NaN）。这也是「标签自算」的另一硬理由。
5. **NO_INCREMENTAL_EXCLUSION_AFTER_FULL_INTERVAL_PURGE**：`lookback=20` 时完整区间 Purge 已覆盖 Embargo 会剔除的全部样本（实测 3 年全窗：purged 14765 / embargoed 0）——Embargo 已执行但增量=0，**不是失效**。`--lookback 1` 探针下 embargoed=1536、IC 0.0431→0.0424，可见增量。来源仓库文档记录同一现象。
6. **校准实测（3 年 × 78 只，ridge，h=5，embargo=5）**：vanilla-shuffled IC 0.0552 / vanilla-chronological 0.0436 / purged 0.0371 / purged+embargo 0.0371；rank IC 0.0306→0.0027；保留训练样本重叠数 221396→0。方向与「泄漏被堵住指标下降」一致，但 **IC 差不是纯泄漏因果量**（通道间还混有训练集构成差异）；硬证据是结构不变量。
7. **模型越灵活，泄漏落差越大**：同一合成数据，Ridge 落差 0.003、HGB 落差 0.020。用演示落差量级推断真实落差前先对齐模型类。
8. 默认资产池是按「窗口终点成交额 top-N」选的确定性名单（非 PIT），有轻微成分前视；结论里必须带上这条告警。

## 与本地训练管线的集成点（只读指引，本技能不改 backend/）

- `docker/training/train.py::_generate_oof_predictions`：堆叠 OOF 用单边 purge（固定删 horizon 行）+ 无 embargo；可换成 `PurgedKFold` 的区间重叠判定（`interval_start = s−lookback+1`，`interval_end = s+horizon`）并补 `embargo` 参数。
- `docker/training/data/splits.py::_split_data`：时序三段切分，尾部 embargo = `horizon + _EXECUTION_LAG_DAYS`；若特征回看窗口 > horizon，purge 应按区间而非固定行数。
- 回归测试参照 `backend/tests/test_training_oof_purge.py` 的既有口径，改切分器时同步更新金样。

## 脚本与参考

- `scripts/purged_cv.py`：stdlib 核心（区间合并 + bisect 重叠判定 + CPCV 路径边着色 + 因果 Walk-Forward + sklearn 适配层）+ numpy 评估层 + QuantDB 装配层 + 合成演示。
- `references/methodology.md`：方法论（信息区间/三件排除器/折内拟合/证据通道解释规则）。
- `references/output-contract.md`：JSON 报告与可导入 API 契约。

## 来源与许可

方法论与证据通道设计移植自 [quantskills/skill-ml-purged-cv](https://github.com/quantskills/skill-ml-purged-cv)（**MIT**，Copyright (c) 2026 quantskills contributors），**数据层未移植**：来源的行情/因子装配整层废弃，改为本地 QuantDB 直读（features_daily + daily_forward 自算标签）；核心切分器重写为纯标准库实现（来源依赖 numpy）。仅限本地研究使用；如需对外分发，保留 MIT 许可与版权声明。审计结论不构成投资建议。
