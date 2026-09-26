# QuantMind 缺陷清单 · 核实报告

核实对象：`quantmind-bug-list.md`（15 条）
核实日期：2026-09-26
核实结论：**15 条中 10 条成立，5 条不成立**（BUG-03、BUG-07、BUG-13、BUG-14、BUG-15）

---

## 0. 核实说明

| 项 | 内容 |
|---|---|
| 源码基准 | 本地检出 `HEAD = 165033e5` |
| 运行时基准 | 服务器 `quantmind` 容器，部署版本 `6b62b91`（`rev_count=133`） |
| 基准一致性 | 两版本间仅 2 个前端测试提交，后端源码一致；已抽查 `inference_parquet.py` 等文件逐行比对确认 |
| 关键依赖版本 | catboost **1.2.10**、xgboost **3.2.0**、lightgbm **4.7.0**（均为容器内实测） |
| 核实方式 | 源码定位（`grep`/逐行读）+ 容器内运行时实测 + 线上 DB 查询 + 线上模型产物读取 |
| 方法限制 | 仅对**只读**路径做了运行时实测；未对任何破坏性端点（删数据/改配置）发起真实请求 |

判定口径：**「成立」= 原清单陈述的代码事实与行为可复现**；**「不成立」= 陈述的事实或机制与代码/运行时不符**。是否算产品缺陷另作标注。

---

## 1. 结论总览

| 编号 | 标题（简） | 原级别 | **核实判定** | 一句话结论 |
|---|---|---|---|---|
| BUG-01 | `news` 路由端点无鉴权 | 严重 | ✅ **成立** | 端点数实测 32（非 34），但「全无鉴权」成立 |
| BUG-02 | API 端口绑定 `0.0.0.0` | 高 | ⚠️ **成立，但属设计** | 事实成立；同文件注释表明是有意放开 |
| BUG-03 | CatBoost 默认参数冲突致训练必败 | 高 | ❌ **不成立** | 线上写法实测成功；报错触发条件不同 |
| BUG-04 | 因子筛选全程 `abs()` | 中高 | ✅ **事实成立** | `abs()` 遍布 10+ 处；是否算缺陷属口径 |
| BUG-05 | WFA 对 9 种模型静默返回空 | 中 | ✅ **成立** | 确认 9 种返回 `None`，仅一行 warning |
| BUG-06 | WFA 预算只取 60% | 中 | ✅ **成立** | `wfa.py:211` 系数 `0.6` |
| BUG-07 | WFA `expanding` 首窗退化 | 中 | ❌ **不成立** | 首窗训练段 ≈1 年，非「接近 0 行」 |
| BUG-08 | 生产目录默认值硬编码 7 处 | 中 | ✅ **成立** | 精确计数 6+1=7 处 |
| BUG-09 | 推理管理端点缺归属校验 | 中 | ✅ **成立** | 3 个端点确无身份注入 |
| BUG-10 | `step_len` 键名两条路径不一致 | 低中 | ✅ **成立** | `:246` 单键 vs `:485` 双键 |
| BUG-11 | `l1_factors` 无 `is_st`，ST 过滤失效 | 中 | ✅ **成立** | 实测无该列；缺列静默跳过 |
| BUG-12 | 无硬删除接口，归档后文件滞留 | 低中 | ✅ **成立** | 仅 3 处 DELETE，均为初始化/运维 |
| BUG-13 | `pred.parquet` 无分段标记 | 中 | ❌ **不成立** | `split` 列存在且取值齐全 |
| BUG-14 | `model_params` 剔除超参致不可复现 | 低（存疑） | ❌ **不成立** | 完整 `dl_params` 同时返回 |
| BUG-15 | hub 模型无 train/valid 指标 | 低 | ❌ **不成立** | 线上实测三段指标全有值 |

---

## 2. 不成立条目专项（重点）

### BUG-03 · CatBoost 默认参数冲突 —— **不成立**

**原主张**：`DEFAULT_CATBOOST_PARAMS` 含 `od_wait: 100`，`_train_catboost` 又无条件传 `fit(early_stopping_rounds=...)`，二者共存导致任何未覆盖参数的 catboost 训练 100% 失败，报
`CatBoostError: only one of the parameters od_wait, early_stopping_rounds should be initialized`。

**核实方式**：容器内 catboost 1.2.10，严格复刻 `trainers_gbdt.py:181-228` 的参数构造与调用形态，三组对照实测。

**实测结果**：

| 变体 | 构造 | 结果 |
|---|---|---|
| 1 | `od_wait` 与 `early_stopping_rounds` **同时放进构造函数** | ❌ 抛错，**错误信息与文档引用完全一致** |
| 2 | `od_wait` 在构造函数 + `early_stopping_rounds` 在 `fit()` ← **线上就是这个写法** | ✅ **成功** |
| 3 | 只给 `od_wait` | ✅ 成功 |

**结论**：文档引用的报错**真实存在**，但触发条件是「两者同时进入构造函数 params」。线上代码 `CatBoost(params)` 的 params 里只有 `od_wait`，`early_stopping_rounds` 是传给 `fit()` 的，CatBoost 接受该组合。**训练不会失败**。

**推测成因**：文档构造复现时把两个参数塞进了同一个 dict，与实际代码路径不符。

**建议**：撤销该条。若仍希望统一早停口径（可读性收益），可作为重构提出，但**不应标注为故障**。

---

### BUG-07 · WFA `expanding` 首窗退化 —— **不成立**

**原主张**：`expanding` 首窗偏移仅 `val_days`，导致首个窗口训练段「被压缩到接近 0 行（通常为空或几十行）」，污染跨窗统计。

**核实方式**：逐行读 `wfa.py:88-135` 的窗口构造逻辑，并结合参数默认值计算实际窗口长度。

**代码事实**：

```python
# wfa.py:93-96
if wfa["strategy"] == "rolling":
    first_anchor_offset = pd.Timedelta(days=train_years_days + val_days)
else:                                    # expanding
    first_anchor_offset = pd.Timedelta(days=val_days)

# wfa.py:106-110
if wfa["strategy"] == "expanding":
    train_start = all_dates[0]           # ← 训练起点是数据起点
else:
    train_start = ...                    # rolling：往前推 train_years
```

**关键**：`expanding` 分支中 `train_start = all_dates[0]`，而 `val_start ≈ base + val_days`。因此首窗训练段 = `[数据起点, val_start)`，长度 ≈ `val_days`。

按默认参数（`val_months=12` → `val_days = 360`），首窗训练段 ≈ **360 个自然日 ≈ 1 年**，不是「接近 0 行」。

**结论**：文档把公式读成了 `anchor - val_days`（该式在代码中不存在）。真实差异仅为「expanding 首窗 1 年 vs rolling 首窗 3 年」，这是 `expanding` 语义的正常表现（代码注释亦写明「expanding 首窗即可从数据起点开始」，属有意设计）。

**建议**：撤销该条，或降级为「建议在报告中标明首窗训练长度」的改进项。

---

### BUG-13 · `pred.parquet` 无分段标记 —— **不成立**

**原主张**：训练产物 `pred.parquet` 把 train/valid/test 写在同一张表且**没有任何列或元数据标识分段**，导致直接算全表 RankIC 会混入样本内成绩。

**核实方式**：读写出逻辑 + 读线上真实产物列名。

**证据 1（源码）**：`docker/training/train.py:399-411`

```python
full_pred_df = df[["symbol", "trade_date", "label"]].copy()
full_pred_df["pred"] = _predict_with_model(model, _fill(df), model_type, features)
full_pred_df["split"] = "train"                                   # ← 分段列
full_pred_df.loc[(...) , "split"] = "valid"
full_pred_df.loc[(...) , "split"] = "test"
```

**证据 2（线上产物）**：容器内读取实际文件，列名为
`['symbol', 'trade_date', 'label', 'pred', 'split']`

**结论**：分段标记列**存在**，名为 `split`，取值 `train`/`valid`/`test`。原主张的事实前提不成立。

**建议**：撤销「无标记」的判定。文档作者自述「实际踩过这个坑（算出 IC 高于平台值）」，更可能的原因是**分析脚本没有按 `split` 过滤**，而非产物缺标记。可保留的建议是：在 `<run_id>/` 下直接产出 `metrics.json`（降低下游重算出错的机会）。

---

### BUG-14 · `model_params` 剔除超参致不可复现 —— **不成立**

**原主张**：`trainers_dl.py:410` 过滤掉 `n_epochs/lr/batch_size/early_stop/metric`，若注册表 metadata 取自该返回值，则训练超参不在模型记录里，模型不可复现。

**证据**：同一函数紧邻位置同时返回了**未过滤的完整 `dl_params`**：

```python
# trainers_dl.py:410
"model_params": {k: v for k, v in model_params.items()
                 if k not in ("GPU", "n_epochs", "lr", "batch_size", "early_stop", "metric")},
# trainers_dl.py:413
"dl_params": {k: v for k, v in dl_params.items()},     # ← 全量，无过滤
```

**结论**：`model_params` 的过滤是**有意的**（这些是训练期参数，`model_params` 用于推理时重建模型构造，传入反而会出错）；训练超参完整保留在 `dl_params` 中。文档自己也已核实 `metadata.json` 的 `dl_params` 完整。**该条应撤销**（文档原本已标「存疑」，现可结案）。

---

### BUG-15 · hub 模型无 train/valid 指标 —— **不成立**

**原主张**：注册表中 `hub_*` 系列只有 test 窗成绩，`train`/`valid` 段指标为空；而 `bl_*` 三段齐全。

**核实方式**：线上 DB 实查 `qm_user_models`。

**实测结果**：

| model_id | train_ic | val_ic | test_ic | metrics 键数 |
|---|---|---|---|---|
| `mdl_cn_train_20260926061028_...` | 0.14495 | 0.12670 | 0.11179 | 10 |
| `mdl_cn_hub_LightGBM_L1L2_ICIR优选_...` | 0.16229 | 0.14468 | 0.12318 | 10 |
| `mdl_cn_hub_GRU_L2_41_...` | 0.15799 | 0.13260 | 0.09600 | 10 |
| `mdl_cn_hub_T_10_LightGBM_选股_...` | 0.19161 | 0.16653 | 0.14218 | 10 |
| `mdl_cn_hub_XGBoost基线模型_...` | 0.13108 | 0.11861 | 0.09943 | 10 |
| `mdl_cn_hub_LightGBM_LGB_06_T5_...` | 0.15595 | 0.13742 | 0.11549 | 10 |

**结论**：全部 6 个模型（含**所有** `hub_*`）train/val/test 三段指标齐全，`metrics` 均为 10 个键（`train_ic`/`train_rank_ic`/`train_rank_icir`/`val_*`×3/`test_*`×3/`score_direction`）。`hub_*` 与新建模型**无差别**，衰减比可直接计算。

**建议**：撤销该条。`metrics_scope` 的建议（标注指标口径版本）可独立保留为增强项。

---

## 3. 成立条目 · 修复优先级

### P0 —— 安全，建议立即处理

**BUG-01（无鉴权）+ BUG-02（端口公网可达）叠加**

未授权访问者可经 `/api/v1/news/admin/*` 执行：删除新闻数据（`purge-old`）、增删改数据源与连接器配置（字段可能含第三方凭证）、无界触发 GPU 推理与全量重建（算力 DoS）、经 `/rsshub/{path:path}` 探测内网。

修复建议（按性价比排序）：
1. router 构造处加全局依赖，而非逐端点补：
   ```python
   router = APIRouter(prefix="/api/v1/news", tags=["News"],
                      dependencies=[Depends(get_current_user)])
   ```
2. `/admin/*`、`/enrichment/run`、`/enrichment/rebuild-all`、`/admin/purge-old` 提升为 `Depends(require_admin)`。
3. `/rsshub/{path:path}` 增加目标 host 白名单，禁止任意转发。
4. 补回归测试：遍历 `app.routes`，断言所有 `/api/v1/news/*` 端点 `dependencies` 非空。

**BUG-02 的定性需先澄清**：`docker-compose.yml:81-82` 注释写明「业务端口保持公网可达（登录/仪表盘用）」，说明是有意放开。请先确认该设计意图，再决定是保留（并写入文档的风险说明）还是改为回环绑定。**不应描述为「与其余容器口径不一致的遗漏」。**

### P1 —— 正确性，影响训练结论

- **BUG-11**：`l1_factors` 直读模式无 `is_st`，ST 过滤静默失效 → 污染评估指标。建议缺列时**显式报错**，或至少在训练报告落 `st_filter_applied: false`。同类「按列存在与否决定行为」的过滤步骤建议一并排查。
- **BUG-05 + BUG-06**：WFA 静默返回空 + 预算隐式打 6 折。建议 WFA 入口做能力检查并返回结构化 `skipped` 状态随结果落库；返回值携带 `windows_planned/completed/budget_exhausted`。
- **BUG-09**：推理管理端点缺归属校验，可跨租户卸载他人模型。按同文件 `/predict` 的写法补齐 `get_authenticated_identity`。

### P2 —— 可维护性

- **BUG-08**：7 处硬编码默认路径收敛到单一配置源（纯机械改动，无行为变更）。
- **BUG-10**：`trainers_dl.py` 统一取值函数，消除键名不一致的静默回落。
- **BUG-12**：提供 `hard_delete_model()`（先返回引用清单，再删记录+目录）。
- **BUG-04**：`abs()` 口径问题建议先明确产品语义。若确认「优选」应含方向约束，再引入 `sign_policy` 配置（默认 `abs` 保持向后兼容），并在训练报告输出被选因子的 IC 符号分布。

---

## 4. 与原清单的事实性差异（勘误）

| 项 | 原清单 | 实测 | 影响 |
|---|---|---|---|
| BUG-01 端点数 | 34 | **32** | 不影响结论 |
| BUG-11 `l1_factors` 列数 | 119 | **120** | 不影响结论 |
| BUG-02 定性 | 「口径不一致」的遗漏 | 注释表明**有意放开** | **需改定性** |
| BUG-04 定性 | 缺陷 | 事实成立，**是否缺陷属产品口径** | 建议改措辞 |
| BUG-03 复现条件 | 默认参数即触发 | 仅「两者同进构造函数」时触发 | **判定翻转** |
| BUG-13 分段标记 | 无 | `split` 列存在 | **判定翻转** |
| BUG-15 指标完整性 | hub 缺 train/valid | 三段齐全 | **判定翻转** |

---

## 5. 核实方法（可复现）

```bash
# 源码定位（本地）
grep -c "Depends" backend/services/api/routers/news.py                 # → 0
grep -n "@router\." backend/services/api/routers/news.py | wc -l       # → 32
grep -n "budget_min \* 60 \* 0.6" docker/training/diagnostics/wfa.py   # → :211
grep -rn "DELETE FROM qm_user_models" backend/                         # → 3 处

# 运行时（quantmind 容器）
docker exec quantmind python -c "import catboost; print(catboost.__version__)"   # → 1.2.10
docker exec quantmind python /tmp/_cat_probe3.py   # CatBoost 三变体对照

# 线上产物（pred.parquet 列名）
docker exec quantmind python -c "
import duckdb,glob
for p in glob.glob('/app/models/users/*/*/*/pred.parquet')[:1]:
    print([c[0] for c in duckdb.connect().execute(f\"SELECT * FROM read_parquet('{p}') LIMIT 0\").description])"

# 注册表指标（DB）
docker exec quantmind-db psql -U quantmind -d quantmind -c \
  "SELECT model_id, metadata_json->'metrics'->>'train_ic', metadata_json->'metrics'->>'val_ic',
          metadata_json->'metrics'->>'test_ic' FROM qm_user_models ORDER BY updated_at DESC;"
```

---

## 6. 总结

原清单的**代码定位精度很高**（行号、函数名、参数名基本准确），10 条成立条目均可直接采信并进入修复排期。

5 条不成立集中在两类：
- **对运行时行为的推断未经实测**（BUG-03 CatBoost 冲突、BUG-15 指标缺失）—— 实测即可推翻；
- **对代码语义的读解偏差**（BUG-07 窗口公式、BUG-13 分段列、BUG-14 过滤范围）—— 逐行读即可纠正。

建议后续提交上游前，对涉及「运行时必然失败 / 必然缺失」的断言补一次实测，可显著降低误报率。
