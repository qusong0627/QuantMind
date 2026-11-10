# 训练 / 推理缺陷清单（2026-11-10）

> **状态声明：以下均为仓库代码变更，尚未部署。线上推理 / 训练镜像未重建、未更新，
> 不能当作「线上已修」。** 本轮未 commit、未 push、未重启任何服务。
>
> 范围：只做训练 / 推理链路。交易桥、下单、撤单、实盘路由、QMT 开关、券商适配
> 未触碰；`ENABLE_REAL_TRADING`、`ENABLE_SIGNAL_STREAM_PUBLISH` 等配置未改。

---

## 一、已确认 Bug（本轮已修 + 已测）

### A1. 推理批量取价「请求 3272 只，命中 0 只」，`expected_price` 全 NULL（P0）

**证据**

- 2026-11-10 09:23–09:25 CST（11-09 20:23–20:25 ET）celery 日志：已连接
  `www.quantmindai.cn:6379 db=3`，随后「批量取价：请求 3272 只，命中 0 只」。
  不是连接失败。
- 当日 6 次 completed 推理约 19549 条 `engine_signal_scores` 的
  `expected_price` 全为 NULL，无 fallback / error。
- 旧实现 `backend/services/engine/inference/script_runner.py` `_fetch_expected_prices`
  只构造 `snapshot_key()` → `market:snapshot:{prefix.lower()}`，且只 `HGET "Now"`。
- 标准取价面 `backend/shared/decision_context_source.py` `read_snapshots`
  （L292–344）已同时发**后缀式**（`market:snapshot:600036.SH`）与**前缀式**
  （`market:snapshot:sh600036`）两套键，并走 `transaction=False` 管道；
  `snapshot_key_suffix` 见 L193–209。该修复此前未同步到推理取价。
- 2026-10-14 曾实测远端 5563/5563 为后缀键；**2026-11-10 未现场 HGET 复测**
  （按约束禁止用仓库口令访问公网 Redis）。因此修复按「prefix + suffix 双兼容」
  实现，不假设远端只有一种键形。

**修复**

- `backend/shared/decision_context_source.py`：新增 `snapshot_price(snap)`——
  依次认 `Now`、`now`；缺失 / 空串 / 非正 / 脏值一律返回 `None`（**不是 0**）。
  `quotes_from_snapshots` 也改用该函数，取价口径单源。
- `backend/services/engine/inference/script_runner.py` `_fetch_expected_prices`：
  改为复用 `read_snapshots` + `snapshot_price`。按 `_QUOTE_CHUNK=200` 分片
  （每片 200 只 × 2 键 = 400 条命令/批，与远端实测可承载量级一致）。
  任一符号形态（`sh600036` / `600036.SH` / `600036`）先归一到 suffix 再查询。
  取不到 / 0 价不入表，调用方写 NULL。异常仍 WARNING 留痕并返回已取到的部分。
- 未改行情 Redis 地址、未改 `ENABLE_SIGNAL_STREAM_PUBLISH`。

**测试**

- 新增 `backend/tests/test_inference_expected_price_keys.py`：仅 suffix 键、
  仅 prefix 键、双键并存（suffix 优先）、三种符号形态、缺失 / 0 / 负价不进表、
  `snapshot_price` 字段与取值契约。全部使用假 Redis。
- 更新 `backend/tests/test_inference_persist_perf.py` 的假管道为 `hgetall`，
  断言 500 只 → 1000 条双键命令（仍是 3 次 pipeline，不是逐键往返）。

**状态：已修 + 已测；未部署。**

---

### C1. `execution_price=open` 缺 `open` 列拖到 groupby 才 `KeyError`（P1）

**证据**

- 任务 `train_20261109123002_56a909dc`（2026-11-09 12:30–12:45 CST =
  11-08 23:30–23:45 ET），catboost，`label_formula: execution_price=open`：
  ```
  File "/app/data/loading.py", line 642, in load_data
      execution_close = narrow.groupby("symbol")[_entry_col].shift(-_lag)
  KeyError: 'Column not found: open'
  ```
  该任务 `admin_training_jobs.logs` 基本为空，错误行没有列清单。
- 当前 `docker/training/data/loading.py`：
  - `required_columns`（L216–230）此前含 `open`，**不含 `close`**；
  - `narrow`（L676–692）只收录 `df.columns` 里已有的列；
  - 标签分支仍在 `narrow.groupby(...)[_entry_col]`（本轮修复前 L733）才第一次用
    `open`/`close`。
- `backend/services/engine/data_platform/quantdb_factor_reader.py`：`read_range`
  默认 `include_ohlcv=True`，但仅当因子源自带 OHLCV 或存在同目录 `l1_factors`
  补给表时才输出 6 列（L727–759）；`CUSTOM` 市场在 L519–521 明确跳过 OHLCV
  完备性检查，注释自己写着「标签构建仍需数据源自带 close 列，否则训练时按缺列
  报错」——即仍可能拖到 groupby。
- **历史成因不作断言**：当时训练镜像/读列清单是否未选出 `open`，没有逐行实锤
  （该任务 logs 为空）；本条只按「当前仓库仍存在延迟 KeyError 路径」定性为缺陷。

**修复**

- `required_columns` 增加 `"close"`，确保可用时被读出。
- 直读分支在两次 `groupby` 之前校验 `(_entry_col, "close")`，缺列抛
  `RuntimeError`，消息列出缺失列、`factor_source`、`execution_price` 与已加载列
  前 40 列。不再落到 pandas `KeyError`。

**测试**

- 新增 `backend/tests/test_training_loading_label_columns.py`：
  假 reader + 小帧；无 `open` + `execution_price=open` → 明确 `RuntimeError`；
  无 `close` + `execution_price=close` → 明确 `RuntimeError`；
  两列齐全时正常构建标签（新闸不误杀正路径）。

**状态：已修 + 已测；未部署（训练镜像未重建）。**

---

### F1. 训练失败归档只有 `ExitCode`，未记录 `OOMKilled` / 内存上限（P1 诊断）

**证据**

- `backend/services/engine/training/local_docker_orchestrator.py` 失败分支此前只写
  `[FAILED] ExitCode={exit_code}`。
- Exit 137 只证明 SIGKILL，不能默认写 OOM：2026-11-10 实测残留容器
  `qm-train-train_20261110082420_aea54a07` `Exited 137` 但
  `docker inspect .State.OOMKilled=false`（用户取消）；另有物化 peak≈22G
  （容器上限 53G）仍在 catboost 迭代中被杀的任务，缺少 inspect 证据。
- 宿主机 `MemTotal` ≈60.42 GB，`_host_mem_limit_gb()` 按 88% → 53g。

**修复**

- 新增纯函数 `_format_failure_diag(exit_code, state, host_config)`：输出
  `ExitCode` + `OOMKilled` + `MemoryLimitBytes`；仅 `exit_code==137` 且
  `OOMKilled=false` 时追加「不可判为 OOM」。
- 失败分支用 `c.reload()` 后已有的 `c.attrs["State"]` / `c.attrs["HostConfig"]`
  填充；未增加运行时 `stats()` 采样、未加资源、未重启服务。
- `rss/peak` 未记录：容器停止后 inspect 不保留，采样属额外开销，按小改边界不做。

**测试**

- 新增 `backend/tests/test_training_failure_diagnostics.py`：
  `OOMKilled=true` 正常上报；`137 + false` 明确不可判 OOM；非 137 不带 OOM 结论。

**状态：已修 + 已测；未部署。**

---

## 二、仓库已修但缺回归（本轮补回归）

### B1. `top_tail` 全帧掩码与块内子集不等长广播（P1）

**证据**

- 任务 `train_20261109143427_60b2845c`（2026-11-09 14:34–14:47 CST =
  01:34–01:47 AM ET），`label_transform=top_tail`：
  ```
  File "/app/preprocessing.py", line 291, in cross_sectional_label_transform
      out[m & ~np.isfinite(sub_x)] = np.nan
  ValueError: operands could not be broadcast together with shapes (10703603,) (72634,)
  ```
  当时 `m` 为全帧掩码，`sub_x` 为块内子集。
- 当前仓库 `docker/training/preprocessing.py:319–337` 已改为
  `sub_x = xa[m]`、`out[m] = np.where(...)`、最后 `out[~np.isfinite(xa)] = np.nan`。
  注意：现在 L291 是 `size_excess_*` 分支，旧 traceback 行号已错位。
- 11-10 成功任务走的是 `size_excess_rank`，未覆盖 `top_tail` 路径。

**本轮处理**

- 未重写实现（仓库已修）。
- 在 `backend/tests/test_training_label_preprocessing.py` 新增
  `test_label_top_tail_multi_chunk_no_unequal_broadcast_nan_passthrough`：
  40 个交易日 × 5 只，跨 `_date_chunks` 的 30/10 两块（`m` 非全帧），旧写法
  必现不等长广播；同时断言 NaN 透传、每日恰好 1 只入尾。

**状态：仓库源码已修；回归已补；未在真实训练镜像 / 千万行数据上复跑
（约束禁止触发训练）。**

---

## 三、疑似问题 / 待验证（本轮不改）

### D1. 推理缺列填 0 与 `fill_values` 兼容（不改，用户已选保守）

- 实际模板：`backend/services/engine/inference/templates/inference_parquet.py`
  `preprocess`（L781–837）：缺特征列警告后填 0（L806–811）；无 `fill_values`
  时仍 `fillna(0.0)`（L825–829）；新模型走 `preprocessing.enabled` 截面预处理后
  `fillna(0.0)`（L819–823）。
- 训练端 `cross_sectional_preprocess` 为「截面中位数填充 → zscore」，缺失在
  标准化后正好等于 0；新模型的推理路径与此一致。
- Ensemble 父包无 `fill_values` **不构成本次故障**：
  `inference_ensemble_src.py` 对每个成员 `load_source_model(m_dir)` 读**成员
  metadata**（L384–408），`predict_with_model` 用成员 `fill_values`（L573–601）；
  DL 成员强制 `MODEL_DIR=member_dir`（L436–454）。融合脚本按成员目录推理。
- 风险：旧包若在某日缺少整个特征列，会静默填 0（在训练分布里等于截面均值）；
  改成强制失败会影响现网合法旧包。按用户选择**保持兼容，不改**。
- 待确认项：是否需要给「缺列数」加显式指标 / 质量字段（不属于本轮范围）。

### D2. `feat_norm` 与截面预处理顺序

- `inference_parquet.py` 的 `_cross_sectional_preprocess_window` 已按日复现
  winsor + zscore，再套 `feat_norm`（2026-10-25 修复记录）。本轮只读核对，
  未发现新的已证实不一致；无代码变更。

### E1. `eval_report` 顶层混合 IC —— 仓库已按样本外展示，不改

- `docker/training/model_trainers/metrics.py`：顶层 `rank_ic` / `ic_curve`
  （L146–148）从 2016 起混合 train+val+test；`by_split` 在 L211–216 单独给
  各段（`test` 才是 OOS）。
- UI 已处理：`electron/src/pages/training/trainingUtils.tsx`
  `resolveEvalIcHeadline`（L348–379）**测试段优先**；无 test 段回落全窗但标签写
  「全窗含训练」，不冒充样本外。`TrainingResultView.tsx` L714–715 使用该函数；
  `electron/src/pages/training/__tests__/trainingUtils.test.ts` L39–84 已钉住。
- 未发现把混合 IC 当 OOS 展示或用于选模的路径（early stopping 用 valid）。
- **不改。**

### P2-1. `framework=pytorch` 与 `model_type=nativetft` 字段不一致

- `backend/services/engine/inference/model_loader.py` 当前按 `framework` 进
  `_load_pytorch`，再按 `model_type` 含 `nativetft` 调
  `load_native_tft_state_dict`，活跃包可正常加载。
- 未改；建议后续统一 metadata 字段，避免只认 `framework=="nativetft"` 的新路径踩坑。

### P2-2. `label_formula` 覆盖后磁盘 `config.yaml` 与真实标签不一致

- `docker/training/train.py`：`label_formula` 与同名配置同时出现时以 formula 为准；
  11-10 成功任务磁盘配置为 `label_transform: rank`，日志/metadata 为
  `size_excess_rank`。
- 未改（属配置落盘口径，不是本轮 A/B/C 范围）；建议落盘写覆盖后的最终值。

### V1. 远端行情键形待现场验证

- 2026-11-10 未 HGET 复测公网行情 Redis；本修复不依赖单一键形
  （suffix + prefix 双发）。部署后验收应以「下一次推理 `expected_price` 非 NULL
  比例」为准。

---

## 四、配置选择（不是 Bug，不改）

| 项 | 现状 | 处理 |
|---|---|---|
| `ENABLE_SIGNAL_STREAM_PUBLISH` | 默认 `false`（`backend/services/engine/services/event_stream.py` L17、L47–57）；celery 未设置 | 配置选择，不打开 |
| `REMOTE_QUOTE_REDIS_*` 未设置 | `resolve_remote_quote_redis()` 回落内置 `www.quantmindai.cn` / db3（`backend/shared/remote_quote_config.py` L31–34、L99–120） | 配置选择，不改环境变量 |
| `ENABLE_REAL_TRADING=true` / QMT 通道 | 实盘开关与通道状态 | 属交易面，完全不碰 |
| 137 内存限额 | `_host_mem_limit_gb()` 按宿主 88% | 不加资源、不改限额 |

---

## 五、验证记录

命令（本机项目 venv；conftest 已隔离 Redis/DB，无真实连接）：

```bash
/home/zbox/projects/quantmind/.venv/bin/python -m pytest \
  backend/tests/test_inference_expected_price_keys.py \
  backend/tests/test_inference_persist_perf.py \
  backend/tests/test_training_label_preprocessing.py \
  backend/tests/test_training_loading_label_columns.py \
  backend/tests/test_training_failure_diagnostics.py \
  backend/tests/test_decision_context_source.py \
  backend/tests/test_script_runner_async_push.py \
  -q -p no:cacheprovider --no-cov
```

结果：`87 passed`。

`loading.py` 相邻既有测试（`test_train_cached_load.py`、
`test_training_spill_and_feature_view.py`）另行单跑：`9 passed`。

说明：仓库 pytest 配置默认带全局覆盖率门槛（fail-under=5），跑单个测试文件会因
「全仓覆盖率 1%」报 `Coverage failure`——这是配置口径，不是业务失败；上表结果为
`--no-cov` 的业务信号。

另外发现两组**既有失败**（与本次改动无关，未修）：`test_training_preprocessing.py`
4 例因 int64 帧 + 当前 pandas 拒写 float 抛 `TypeError`（`preprocessing.py:192`，
该文件本轮未改）；`test_training_design_guards.py` 2 例断言 `train.py` /
`admin_training_utils.py` 中已迁移的旧源码片段（P2 拆包后失效，本轮未改这两个文件）。

**未部署。** 部署后验收建议：

1. 下一次推理日志不再出现「请求 N 只，命中 0 只」；`engine_signal_scores.expected_price`
   非 NULL 比例显著上升（远端键形为 suffix 时应接近全量命中）。
2. 训练镜像重建后，用小样本 `top_tail` 任务复跑（10.7M 行全量不在本轮验证范围）。
3. 触发一次失败训练（可取消或构造非零退出），确认日志含 `OOMKilled` 与
   `MemoryLimitBytes`。
