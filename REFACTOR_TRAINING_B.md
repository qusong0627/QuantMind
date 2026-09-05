# 训练管线重构方案 B —— Config Schema 化 + 模型注册表（Model Registry）

> 目标：在不拆除 Docker 训练编排、不改变训练产物契约的前提下，把 Qlib 官方入口的
> **声明式类型化配置**与**注册表式模型分派**引入当前训练管线，剔除 4900 行单文件的
> 手写 if/elif 与无类型 dict 配置，同时保证每一步可回退、可独立验收。
>
> 关联分析：本方案在《入口对比》中被评定为「长期维护性最高、即时稳定性中等」。
> 建议严格按「先 A（入口合并）→ 再 B1（schema）→ 再 B2-GBDT → 再 B2-DL」的顺序执行，
> B 的高危部分（DL/TFT）必须拆到最后单独验收。

---

## 1. 背景与目标

当前训练入口链路（已核实）：
```
POST /run-training（api/admin 两处）
  → submit_training_job            backend/services/api/routers/admin/admin_training_utils.py:905
  → _normalize_payload             admin_training_utils.py:328   （手工钳制/校验，返回 dict）
  → _build_config_yaml             backend/services/engine/training/local_docker_orchestrator.py:448 （手拼 dict）
  → docker run ... python /app/train.py --config config.yaml
  → main()  yaml.safe_load         docker/training/train.py:4062
  → train_model / _train_single_model / train_multi_models     train.py:2993 / 3527 / 3711
  → _train_lgb/_train_xgb/.../     train.py 的 if/elif 分派
  → _save_model / _predict_with_model / _get_model_framework    train.py:2929 / 2907 / 2970
  → result.json + metadata.json + model.* + pred.pkl
  → HTTP 回调 → complete_training_run → register_model_from_training_run    model_registry.py:1386
```

目标架构（与现状的唯一差异点）：

```
payload (dict) ──▶ pydantic 训练配置模型 ──▶ config.yaml（由模型序列化产出）
                                            │
   train.py ◀── yaml ──▶ 配置模型（同 schema，双端复用）
      │
      └─▶ MODEL_REGISTRY[model_type].trainer(...)     取代 train_model 的 if/elif
              ├─ Trainer(train_fn=…, predict_fn=…, save_fn=…, framework=…, tag=…)
              ├─ 由 @register_trainer("lightgbm") 装饰器注册
              └─ 旧 if/elif 保留为 fallback（A/B 对齐窗口）
```

### 1.1 为什么是「增量」而不是重写
- 产物契约（`model.lgb / model.xgb / model.cbm / model.pkl / model.pth / pred.pkl / metadata.json / result.json`）被 `model_registry.py` 与线上推理依赖，**逐字节不能变**。注册表必须复用 `_save_model` 单点，禁止各自落盘。
- Docker/AutoDL 编排、多周期 parent/child、进度流（stdout 正则）、前端超时预期——这些产品能力**本轮一律不碰**。

### 1.2 目标收益
| 问题 | 现状 | 重构后 |
|---|---|---|
| 无类型配置 | 手拼 dict、`payload.get(k, default)` 散落 10+ 处 | pydantic `model_validate` 一次校验，类型/枚举/clamp 集中 |
| 模型分派 | 4900 行单文件、if/elif 链 | `MODEL_REGISTRY` 查表，新增模型=加一条注册条目 |
| 配置双端漂移 | `_build_config_yaml` 手写 dict 与 `train.py` 读取的 key 靠人肉对齐 | 同一 pydantic 模型双端复用 |
| 产物不知名 | 落盘逻辑分散、命名靠约定 | `Trainer.save_fn` 单点统一、契约由测试锁住 |

---

## 2. 现状盘点（已核实的代码事实）

### 2.1 配置契约（`_build_config_yaml` 产出，train.py 消费）
现 top-level key（按 `local_docker_orchestrator.py:448-574` + `train.py` 校验）：
`run_id, job_name, seed, data, model, label, context, explain, split, wfa, drift, cache, output, callback`，另有可选 `optuna, preprocessing, factor_selection`。

`model` 子项含：
- `type`（见 `_ALLOWED_MODEL_TYPES`）、`types`、`ensemble`、`prediction_mode`
- `num_boost_round` / `early_stopping_rounds` / `val_ratio`
- `params`（lgb）、`xgb_params`、`catboost_params`、`dl_params`

### 2.2 模型类型全集
- `_ALL_MODEL_TYPES` / `_DL_MODEL_TYPES` 已在 train.py 定义。
- 框架映射：`_get_model_framework`（train.py:2970）——lightgbm/xgboost/catboost/sklearn/pytorch。
- 落盘文件名：`_save_model`（train.py:2929）——单点、已按 type 映射到 `model.lgb/xgb/cbm/pkl/pth`。

### 2.3 分派点（待重构 if/elif）
- `train_model`（:2993）：入口路由 + GBDT 的 optuna 预搜索。
- `_train_single_model`（:3527）：单模型训练，含 OOF 守卫 `need_full_pred`。
- `train_multi_models`（:3711）：ensemble/多模型并行。
- `train_stacking`（:3851）：stacking 胶水，调 base 训练器 + `_generate_oof_predictions`（:3790）。
- `select_top_factors`（:3209）、`_tune_tree_hyperparams`（:3449）。

### 2.4 训练器签名分布（重构的机械性来源）
| type | 函数 | 签名 | 特殊点 |
|---|---|---|---|
| lightgbm/xgboost/catboost | `:1631/:1784/:1822` | `(cfg, features, X_train, y_train, X_val, y_val)` | catboost 概率化、各有 quantile 分支 |
| linear/random_forest | `:1875/:1891` | 同上 | sklearn 存 pkl |
| mlp | `:1921` | 同上 | sklearn 封装的 pytorch，存 pkl |
| dl (gru/lstm/…) | `:2104` + `_predict_dl:2626` | DataFrame/lazy dataset | 自存 `model.pth`，独立预测 |
| nativetft | `:2395` + `_predict_nativetft:2794` | DataFrame | 自存、自预测、返回对齐 DataFrame |

> 结论：**5 个 GBDT/sklearn 分组签名高度统一，可机械转入注册表；DL 组（mlp/dl/nativetft）签名分裂，需独立 adapter 包一层。** 这是 B2 拆两期（先 GBDT/sklearn、后 DL）的事实依据。

---

## 3. 目标接口设计

### 3.1 配置模型（`backend/shared/training/schemas.py`）
单一定义，双端复用，**依托「api 容器与训练容器共享 backend/shared 包」这一已确认前提**：

- schema 落在 `backend/shared/training/schemas.py`（单源）。
- api 侧：`_build_config_yaml` 实例化同一 `TrainingConfig` 并序列化产 config.yaml。
- 训练侧：`train.py main()` 用同一 `TrainingConfig.from_dict(...)` 解析。
- 两端 import 同一个包 → key 天然一致，**无需额外契约测试桥接**。

> B0 阶段一次性确认：训练容器镜像的 rootfs 确实会发布 `backend/shared/`（train.py 位于 `docker/training/`，
> 需核实 Dockerfile 是否把 backend/shared 一并拷入 /app 或 PYTHONPATH 能到达）。若镜像不发布该目录，
> 则退化为上一版方案：schema 放 `docker/training/schemas.py` + 契约往返 diff 测试兜底（见 §5 对应风险行）。

```python
# docker/training/schemas.py
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

Market = Literal["CN", "HK", "US", "CRYPTO"]
ModelType = Literal["lightgbm", "xgboost", "catboost", "linear",
                    "random_forest", "mlp", "gru", "lstm", "alstm",
                    "transformer", "tra", "hist", "tabnet", "tcn",
                    "nativetft", "hybrid_gru_tree"]

class ModelCfg(BaseModel):
    type: ModelType = "lightgbm"
    types: Optional[list[ModelType]] = None
    ensemble: str = "none"
    prediction_mode: Literal["point", "quantile"] = "point"
    num_boost_round: int = 1000
    early_stopping_rounds: int = 100
    val_ratio: Optional[float] = 0.15
    n_folds: Optional[int] = None            # stacking 折叠数（现状未发射，见 §3.3）
    meta_alpha: Optional[float] = None       # momentum/blending 系数（现状未发射，见 §3.3）
    monitor_rank_ic: bool = True             # rank IC 早停监控
    params: dict = Field(default_factory=dict)          # lgb
    xgb_params: dict = Field(default_factory=dict)
    catboost_params: dict = Field(default_factory=dict)
    dl_params: dict = Field(default_factory=dict)
    # max_depth=-1 对 xgboost 非法：在 schema 里拦截而不是靠散落 if
    @field_validator("xgb_params")
    @classmethod
    def _drop_invalid_xgb_depth(cls, v): ...

class DataCfg(BaseModel):
    train_start: str
    train_end: str
    features: list[str] = Field(default_factory=list)
    source_mode: Literal["LOCAL", "COS"] = "LOCAL"
    local_dir: Optional[str] = None
    factor_source: Optional[str] = None
    factor_catalog_version: Optional[str] = None
    factor_schema_hash: Optional[str] = None
    factor_field_sources: dict = Field(default_factory=dict)
    factor_catalog_published_at: Optional[str] = None
    factor_coverage: dict = Field(default_factory=dict)
    quantdb_dir: Optional[str] = None

class LabelCfg(BaseModel):
    target_horizon_days: int = 1
    target_mode: Literal["return", "classification"] = "return"
    label_formula: str = ""
    effective_trade_date: str = ""
    training_window: str = ""

class ContextCfg(BaseModel):
    initial_capital: float = 1_000_000
    benchmark: str = "SH000300"
    commission_rate: float = 0.00025
    slippage: float = 0.0005
    deal_price: Literal["open", "close"] = "close"
    market: Market = "CN"
    industry_as_feature: bool = False

class SplitCfg(BaseModel):
    train: list[str] = Field(min_length=2, max_length=2)
    valid: list[str] = Field(min_length=2, max_length=2)
    test:  list[str] = Field(min_length=2, max_length=2)

class OutputCfg(BaseModel):
    result_path: str = "/workspace/result.json"
    required_artifacts: list[str] = Field(
        # 以 _build_config_yaml 的默认值(不含 config.yaml)为准；§3.3
        default_factory=lambda: ["model.lgb", "pred.pkl", "metadata.json", "result.json"]
    )

class CallbackCfg(BaseModel):
    url: str
    secret: str = ""

class TrainingConfig(BaseModel):
    run_id: str
    job_name: str = "unnamed"
    seed: int = 42
    max_time_minutes: int = 120              # train.py:1512 阶段超时（_build_config_yaml:591 发射）
    data: DataCfg
    model: ModelCfg = ModelCfg()
    label: LabelCfg = LabelCfg()
    context: ContextCfg = ContextCfg()
    split: Optional[SplitCfg] = None          # 显式时间段切分（优先于 val_ratio）
    explain: dict = Field(default_factory=dict)
    output: OutputCfg = OutputCfg()
    callback: CallbackCfg = CallbackCfg(url="")
    cache: Optional[dict] = None
    optuna: Optional[dict] = None             # 现状不发射（死配置，见 §3.3）
    preprocessing: Optional[dict] = None
    factor_selection: Optional[dict] = None
    wfa: Optional[dict] = None
    drift: Optional[dict] = None              # 现状不发射（死配置，见 §3.3）

    @classmethod
    def from_dict(cls, raw: dict) -> "TrainingConfig":
        return cls.model_validate(raw)
```

**校验规则集中点**（现状散落，重构后收进 validator）：
- `target_mode ∈ {return, classification}`（对应 `_ALLOWED_TARGET_MODE`，admin_training_utils.py:29）。
- `deal_price ∈ {open, close}`（`_ALLOWED_DEAL_PRICE`，:30）。
- `model.type ∈ _ALLOWED_MODEL_TYPES`（:31）。
- 显式 split 给出时强制 `val_ratio = None`（现状交给 `_build_config_yaml`:576 的后置 if，容易漏）。

### 3.2 模型注册表（train.py 内或 `docker/training/model_trainers.py`）
```python
# 设计要点：复用现有 _save_model/_predict_with_model/_get_model_framework 单点，
# 禁止新 Trainer 自行落盘，以锁死产物契约。

@dataclass
class Trainer:
    name: str
    train_fn: Callable                       # 兼容 6 参签名 (cfg, features, X, y, Xv, yv)
    predict_fn: Callable | None = None       # 缺省回退 _predict_with_model
    save_fn: Callable | None = None          # 缺省回退 _save_model
    framework: str = "unknown"
    is_dl: bool = False
    # adapter 用：DL 训练器不走 6 参签名，走 (cfg, train_df, val_df, features, out_dir, hardware)

MODEL_REGISTRY: dict[str, Trainer] = {}

def register_trainer(name: str, *, framework: str = "unknown", is_dl: bool = False,
                     predict_fn=None, save_fn=None):
    """装饰器：集中注册，取代 train_model 内 if/elif 链。"""
    def deco(fn):
        MODEL_REGISTRY[name] = Trainer(
            name=name, train_fn=fn, predict_fn=predict_fn,
            save_fn=save_fn, framework=framework, is_dl=is_dl,
        )
        return fn
    return deco

# 用法：把 _train_lgb 等就地加一行装饰器，签名不变。
@register_trainer("lightgbm", framework="lightgbm")
def _train_lgb(cfg, features, X_train, y_train, X_val, y_val): ...
```

`train_model`（train.py:2993）改为：
```python
trainer = MODEL_REGISTRY.get(model_type)
if trainer is None:
    raise ValueError(f"Unsupported model_type: {model_type}")
# 可选 A/B：CONFIG_FLAG_OLD_DISPATCH=1 时仍走旧 if/elif（对齐窗口用）
```

**分发分支的等价迁移表**（`_train_single_model`/`train_multi_models` 内）：
| 现状 if/elif | 重构后 | 备注 |
|---|---|---|
| `if type in gbdt: _train_lgb/_xgb/_cbm(...)` | `MODEL_REGISTRY["lightgbm"].train_fn(cfg, features, X, y, Xv, yv)` | 6 参签名 |
| `elif linear/rf/mlp` | 同上查表 | mlp 是 sklearn 封装，存 pkl |
| `elif nativetft` | `DLAdapter.train(cfg, train_df, val_df, features, out_dir, hardware)` | DataFrame 专属 |
| `elif dl (gru/lstm/…)` | `DLAdapter.train(...)` + `_predict_dl` | model.pth |

`_save_model` / `_predict_with_model` 保持单点，`Trainer.save_fn/predict_fn` 仅在 DL adapter 内部特化，缺省回退到这两个单点。

---

## 4. 分阶段实施（每阶段可独立上线、可回退）

### 阶段 B0 —— 不动代码，建回归基线
1. 用 `config_example.yaml` 跑通一次本地 Docker 训练，把落盘产物（`model.lgb / pred.pkl / metadata.json / result.json`）与 `register_model_from_training_run` 注册成功的记录存档为**黄金基线**。
2. 检查 `docker-compose` / 容器镜像是否发布 `docker/training/schemas.py`（若不一致，先补 build）。
- **验收**：基线产物哈希可复现；`docker/expose` 含新增 schema 文件。
- **回滚点**：无代码改动，天然安全。

### 阶段 B1 —— Config Schema（纯配置层，不碰训练逻辑）
范围：`_normalize_payload`（admin_training_utils.py:328）、`_build_config_yaml`（local_docker_orchestrator.py:448）、`train.py main()`（:4062）的 yaml 解析。

1. 新建 `backend/shared/training/schemas.py`（§3.1 全量，单源）。
2. `_build_config_yaml` 改为实例化 `TrainingConfig`，`config.yaml` 一律由 `model_dump_json`/`model_dump` 序列化；删除手拼 dict 与后置 `if split: val_ratio=None` 修正。
3. `train.py main()` 从 `yaml.safe_load` + 逐 key `cfg.get(...)` 改成 `TrainingConfig.from_dict(cfg)`，内部统一 `cfg.data.train_start` 式强访问。
4. `_normalize_payload` 收口：现存的 `_clamp_int/_coerce_float/_parse_date/枚举` 校验迁入 schema validator；多余默认值判定删除。
5. **死配置边界**：`optuna/drift/n_folds/meta_alpha` 本轮**只建类型、不修发射逻辑**（§3.3.2①）。
   禁止顺手把 optuna 激活——那会显著拉长训练时长，属行为变更，需单独评审。
- **不改任何产物**：config.yaml 的 key 序列化结果必须与现状 byte 级一致（以 B0 基线的 config.yaml 对照）。
- **验收**：B0 config.yaml 能经新 schema 往返序列化且 diff 为空；`TestTrainingConfig` 覆盖枚举非法/max_depth<0/显式 split 三例；§3.3.2③ 的默认值口径写进快照测试。
- **回滚**：仅回滚 schemas.py + 三处调用点，训练逻辑零改动。

### 阶段 B2-GBDT —— 注册表覆盖 GBDT/sklearn 组（低风险）
范围：`train_model:2993`、`_train_single_model:3527`、`train_multi_models:3711`、`train_stacking:3851`，训练器 `_train_lgb/_xgb/_cbm/_linear/_rf/_mlp`。

1. 引入 `MODEL_REGISTRY` + `@register_trainer`（§3.2），给 6 个训练器打装饰器（签名不变）。
2. `train_model`/`_train_single_model` 内只替换 lightgbm/xgboost/catboost/linear/rf/mlp 这 6 段 if/elif 为查表；`_tune_tree_hyperparams`、quantile 分支、optuna 合并逻辑原样保留。
3. 保留旧 if/elif 作 `fallback`（env 开关），对上面 6 模型各跑一次 A/B，校验 `result.json` 各指标与产物文件名**逐一相等**。
- **验收**：6 模型 A/B 产物 diff 为空；新注册一个 stub 模型验证注册失败报错路径。
- **回滚**：整个 B2-GBDT 用 env fallback 一键关闭回到旧分派。

### 阶段 B3-DL —— 注册表覆盖 DL/TFT 组（高风险，最后一期）
范围：`_train_mlp`（若已在 B2 则跳过）、`_train_dl`（:2104）、`_train_nativetft`（:2395）及 `_predict_dl`/`_predict_nativetft`。

1. 定义 `DLAdapter`，把 DataFrame/lazy-dataset/自存 `model.pth`/自预测的三套逻辑各包一层，暴露统一接口接入 `MODEL_REGISTRY`；`mlp` 归类到 B2（sklearn pkl），不在此期。
2. 时序预测回归专项：`_predict_nativetft` 返回的 `(symbol, trade_date, pred)` 对齐、split 归属、`_dl_metrics/_tft_metrics` 逐字段对比基线。
- **验收**：dl 各类 + nativetft 产物与基线 diff 为空；OOF/stacking 集成路径可用。
- **回滚**：DL 三类保持 fallback 最久；合入前留独立分支 + 产物 diff 报告。
- **收益**：至此 `MODEL_REGISTRY` 覆盖全部 `_ALL_MODEL_TYPES`，`train_model` 内 if/elif 清零，可删除 fallback。

### 阶段 B4 —— 收尾清理（可选，不在本轮强制）
- 拆分 `docker/training/model_trainers/*.py` 包，`train.py` 降为编排入口（依赖容器 build 已验证的新路径）。
- 新增 `docker/training/tests/` 对齐测试（产物契约 snapshot 测试）。

---

## 5. 风险与护栏

| 风险 | 等级 | 护栏 | 归属阶段 |
|---|---|---|---|
| 产物契约变（被 model_registry 与线上推理依赖） | **高** | `_save_model` 单点禁止旁落盘；每期 A/B 产物 diff；B0 黄金基线 | B0 起全程 |
| DL/TFT 时序预测回归静默退化 | **高** | B3 独立验收 + 字段级 diff + 独立分支 | B3 |
| 配置 key 漂移（api 写 vs train.py 读） | 中 | 同一 `TrainingConfig` 双端复用 + 往返 diff 测试 | B1 |
| 循环 import（A + B 叠加期） | 中 | 先完成方案 A 收敛入口，B1 在单一链路上做 | B1 前置 |
| optuna/quantile/stacking 分支被查表误伤 | 中 | B2 保留这些分支原逻辑，只换 GBDT 6 段；A/B 覆盖 stacking/OOF | B2 |
| schema 跨容器可见性 | 中 | 已确认共享 backend/shared，schema 单源；仅需 B0 核实训练镜像实际发布该目录 | B0 |

---

## 3.3 字段核对矩阵（现状 100% 对账，含差异与决策）

> 本节是「schema 与现状逐字段核对」的产物。三个消费者均已读源码验证下来源与落点。

**两层模型（重要边界）**：`_normalize_payload` 返回的是**入参/编排层**（含 `TrainingConfig`
不含的 `pause_others / deploy_to_production / generated_at / auto_feature_filter / horizons /
system_notices / valid_start|end / test_start|end / feature_categories` 等只在 API→DB→编排器流动），
由 `_build_config_yaml` 二次投影为**config.yaml 跨容器契约**（Train.py 客端）。
B1 的 schema 只把 `TrainingConfig` 类型化（config.yaml 那份）；编排字段保持现状。
**文档早前「payload → TrainingConfig」的写法不严谨，以本节为准：`TrainingRequest`（入参）与
`TrainingConfig`（config.yaml）是两个模型，不可混为一个。**

### 3.3.1 逐字段来源/落点核对

| config.yaml 字段 | 生产者 `_build_config_yaml` | 消费者 train.py | schema 覆盖 |
|---|---|---|---|
| `data.*`（train_start/end, features, source_mode, local_dir, factor_*, quantdb_dir） | :511-526 | `load_data`/`main` | ✅ DataCfg |
| `model.type / types / ensemble / prediction_mode` | :528-531 | `:1442/:1686`:2112 | ✅ ModelCfg |
| `model.num_boost_round / early_stopping_rounds / val_ratio` | :532-534 | `:1646-1657` | ✅ |
| `model.params / xgb_params / catboost_params / dl_params` | :535-543 | `:1635/:1789/:1827` | ✅ |
| `model.n_folds` | ❌ **未发射** | stacking `:4216`(默认3) | ✅ Optional（死配置①） |
| `model.meta_alpha` | ❌ **未发射** | multi/stacking | ✅ Optional（死配置①） |
| `model.monitor_rank_ic` | ❌ 未发射 | `:1657`(默认true) | ✅ bool（死配置①） |
| `label.*` | :545-551 | `:1289/:1636` | ✅ LabelCfg |
| `context.*` | :552-560 | `:1684` 等 | ✅ ContextCfg |
| `explain` | :561 | `:712-738` | ✅ dict |
| `output.result_path / required_artifacts` | :562-568 | callback/result | ✅ OutputCfg |
| `callback.*` | :569-572 | main | ✅ CallbackCfg |
| `cache.dir` | :573 | `load_data` | ✅ |
| `split`（显式 valid/test） | :576-583 | `:1214-1261` | ✅ SplitCfg（强制 val_ratio=None） |
| `wfa` | :586-587 | `:1499` | ✅ |
| `max_time_minutes` | :591 | `:1512` | ✅（顶层） |
| `factor_selection` | :597-607 | `main` | ✅ |
| `preprocessing` | :611-615 | `:1451/:1582` | ✅ |
| `optuna` | ❌ **未发射** | `:3021/:3470` | ✅ Optional（死配置②） |
| `drift` | ❌ **未发射** | `:4181` | ✅ Optional（死配置②） |
| `seed` | ❌ 未发射 | `:1643` 等(默认42) | ✅ 顶层=42（行为=恒定42，非死） |

### 3.3.2 核对发现的差异与待决策项

**① 死配置（读者有、生产者无 —— 现状即失效/恒定，需拍板是否本轮修）**
- `optuna`：`_normalize_payload:526` 归一化后**两个编排器都不写 config** → 前端选「自动超参搜索」
  实际永不触发（`optuna.enabled` 永远读不到，走 False）。
- `drift`：train.py:4181 `enabled is False` 才关，而 config 永远空 dict → **PSI 漂移检测永远开启、无法关**。
- `n_folds/meta_alpha/monitor_rank_ic`：train.py 读、编排器不写 → stacking 折叠数恒 3、rank IC 监控恒开。

> **B1 稳定优先的做法**：schema 只建类型 & 在 trainer 读取侧强类型化，**不改编排器发射逻辑**
> （中心思想：schema 是契约，behavior 不变）。「修复这些死配置」是**显式、独立、可开关的一项**，
> 缺省不开——因为激活 optuna 会显著拉长训练时长，属于行为变更，需单独评审。

**② 模型输入 vs 落盘，两层不混**：见本节开头「两层模型」。B1 只做 `TrainingConfig`（config.yaml）类型化；
`TrainingRequest`（若要做）是后续单独项，别在 B1 里扩。

**③ 默认值口径不一致（非行为影响，仅记录）**
- `required_artifacts` 默认：`_normalize_payload:632` 含 `"config.yaml"`，`_build_config_yaml:566` 不含。
  schema 以 config 那份为准（已在上文 OutputCfg 注释注明）。
- `train_start`/`train_end` 兜底默认：payload 侧 `2023-01-11/2024-12-31`，config 侧 `2022-01-01/2024-12-31`。
  因 payload 恒先归一化，config 兜底几乎不触发；schema 从 `_normalize_payload` 取实际默认值。

### 3.3.3 对文档其余章节的影响
- §5「不变量」补充：config.yaml 的 key 集合以 §3.3.1 为准；`optuna/drift/n_folds` 属「读者有、生产者无」，
  判定为 B1 不修，避免激活 optuna 的行为漂移。
- §6 B1 工作量含「把 §3.3.2 的差异写成快照测试」：用 `config_example.yaml` + 一次真实训练产出的 config，
  锁住 key 集合与默认值，防止未来漂移。

**不变量（重构全程不可破坏）**：
1. 落盘文件名与字段：`model.lgb/xgb/cbm/pkl/pth` + `pred.pkl` + `metadata.json` + `result.json`。
2. `build_model_id_from_run`（model_registry.py:1355）依赖的 run 信息结构。
3. Docker/AutoDL 编排、多周期 parent/child、进度流、前端超时契约。
4. config.yaml 的 key 命名与默认值语义。

---

## 6. 工作量、排序与里程碑

| 阶段 | 内容 | 工作量 | 风险 | 里程碑 |
|---|---|---|---|---|
| B0 | 黄金基线 + 镜像发布确认 | 0.5 d | — | 产物可复现 |
| B1 | Config schema（配置层） | 2–3 d | 低 | config 往返 diff 为空 |
| B2 | 注册表覆盖 GBDT/sklearn | 2–3 d | 低-中 | 6 模型 A/B 产物 diff 为空 |
| B3 | 注册表覆盖 DL/TFT | 2–3 d | 高 | DL 产物 diff + 独立分支 |
| B4 | 收尾拆分/测试 | 0.5–1 d | 低 | 可选 |
| **合计** | | **约 7–10.5 人日** | — | ≈2–2.5 周单线程 |

**前置依赖**：必须先完成方案 A（入口合并，2–3.5 人日）——B1 的 schema 落在 A 收敛后的单一链路上，避免在 admin 私有模块上反复横跳。

**执行顺序（必须串行，不可跳跃）**：
```
方案A（入口合并，~2-3.5d）
   └─▶ B0（基线，0.5d）
         └─▶ B1（schema，2-3d）
               └─▶ B2-GBDT（2-3d）
                     └─▶ B3-DL（2-3d，独立验收）
                           └─▶（可选）B4
```

---

## 7. 验收标准（全程）

1. **产物契约**：每一阶段后对选定的策略跑一次训练，`result.json` 指标与命名、产物文件与 `register_model_from_training_run` 成功注册，均与 B0 黄金基线一致。
2. **A/B 等价**：B2/B3 各模型在 `fallback=on` 与 `fallback=off` 下指标完全一致（bit-level 不要求，数值级相等）。
3. **配置往返**：B0 config.yaml → `TrainingConfig` → 重新序列化，diff 为空。
4. **注册体系**：新增模型只需一条 `@register_trainer` 即可在 `_ALL_MODEL_TYPES` 内可用（用 stub 验证）。
5. **回归**：`backend/run_tests.py unit` 通过；前端训练页（指标、切页恢复、多周期进度）无回归。

---

## 8. 明确不做（本轮边界）

- ❌ 不拆 Docker/AutoDL 编排，不改多周期 parent/child。
- ❌ 不把进度从 stdout 正则改成结构化事件（那是独立方案，可后续单列）。
- ❌ 不引入 Qlib Recorder/mlflow 作为默认实验跟踪（产品契约当前依赖业务 DB 行 + 文件 artifact，改动面超纲）。
- ❌ 不合并 train.py 内回测与 qlib_app 回测（指标口径对齐风险，另立方案 C）。