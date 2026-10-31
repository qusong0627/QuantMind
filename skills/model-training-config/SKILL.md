---
name: model-training-config
description: "QuantMind 模型训练配置文件生成器：把自然语言需求（市场/周期/模型/因子/超参/时间切分）转成前端『模型训练 → 导入配置』可直接导入的 quantmind-model-training-config 文件（YAML/JSON，.yml 扩展名），并内置 schema 校验脚本与 5 个可直接导入的演示/预设配置。当用户要生成/编写/导出训练配置文件、批量造训练 preset、或需要可直接导入的训练参数模板时使用。触发词：训练配置文件、生成训练配置、导入配置、训练配置模板、训练 preset、训练参数文件、配置文件 schema"
---

> ⚙️ 本技能遵循公共运行环境契约（最高优先级，先于本文其余内容执行）：
> 详见 [_shared/env-contract.md](../_shared/env-contract.md)，执行前先读它。

# 模型训练配置生成器（QuantMind）

把「用户想要什么模型」翻译成前端**模型训练页可以一键导入**的配置文件。
本技能**不调用训练 API**（纯文件生成 + 本地/容器内校验）；生成物交给用户在前端点导入。

- 事实源（若可读源码）：`electron/src/pages/training/trainingUtils.tsx`、
  `electron/src/pages/training/__tests__/trainingConfigFile.test.ts`
- 字段全表：[references/schema.md](references/schema.md)
- 因子家族与选因子：[references/factor-families.md](references/factor-families.md)
- 可直接导入的演示：[templates/](templates/)

## 0. 交付物与导入方式

- 交付**一个** `.yml` 文件（内容可为 YAML，也可为 JSON —— 导入用的 js-yaml 认 JSON）。
  文件名建议 `模型训练配置_<名称>_<YYYYMMDD>.yml`。
- 前端导入路径：**模型训练页 → 右上角「导入配置」→ 选择文件 → 预览 → 确认**。
  文件选择器只接受 `.yml / .yaml / .txt`，**不要用 `.json` 扩展名**。
- 导入会**整体覆盖**当前表单；市场/因子源会自动切换（见 §4）。

## 1. 文件骨架（照抄结构，再改值）

```yaml
schema_version: 1                                  # 固定 1
kind: quantmind-model-training-config              # 固定值
exported_at: "2026-09-22T00:00:00.000Z"            # 元数据
market: CN                                         # CN|HK|US|CRYPTO|FUTURES
factor_source: l1_l2_factors                       # 可选：QuantDB 直读源
factor_catalog_version: qdb-cn-l1_l2_factors-xxxx  # 可选：仅作版本提示
factor_filter:
  enabled: true
  n_top: 80
  ic_threshold: 0.01
  icir_threshold: 0.15
  correlation_threshold: 0.9
configuration:
  displayName: MY_MODEL_T5
  displayNameMode: manual                          # auto|manual
  selectedFeatures: [mom_ret_20d, vol_std_20, ...] # 非空、去重、禁止标签字段
  timePeriods:
    train: ["2018-01-02", "2023-06-18"]
    val:   ["2023-06-26", "2025-01-21"]
    test:  ["2025-01-28", "2026-08-28"]
  target: { mode: return, horizonDays: 5 }
  params:
    model_type: lightgbm                           # 13 选 1
    # …见 references/schema.md 的白名单键
  context:
    initialCapital: 1000000
    benchmark: SH000300                            # CN=SH000300 HK=HSI US=SPX CRYPTO=BTC FUTURES=CL.FUT
    commissionRate: 0.00025
    slippage: 0.0005
    dealPrice: open                                # open|close
    market: CN
    industry_as_feature: false
  wfa: { enabled: false, strategy: rolling, nWindows: 4, trainYears: 3, valMonths: 12, stepMonths: 12 }
```

## 2. 生成工作流（按序执行）

1. **收集需求**：市场？预测周期 T+N？模型类型？偏因子方向（动量/微观结构/基本面…）？
   训练/验证/测试时间范围？股票池？若用户没说，用 §3 的默认值并**在交付时说明所选默认**。
2. **定市场与因子源**：CN/HK/US/FUTURES/CRYPTO 是 QuantDB 直读市场。
   要 L2 微观特征就 `l1_l2_factors`；只要日频用 `l1_factors`；纯高频从 `l1_l2_factors` 中取 micro_*/flow_* 列子集。
3. **选因子**：按 [references/factor-families.md](references/factor-families.md) 的 recipe 挑
   40~120 个，家族均衡；优先用其中列出的 key（已在本地目录验证）。
4. **定时间切分**：三段都要，且 `train_end < val_start`、`val_end < test_start`；
   **间隔 ≥ horizonDays + 1 天**，否则后端会把 val/test 起点悄悄后移。
5. **定模型与超参**：树模型给 `learning_rate/num_leaves/max_depth/...`；DL 给 `dl_*`。
   多模型 Stacking 才写 `model_types`（≥2）+ `ensemble_method: stacking`。
6. **组装**：按 §1 骨架填值，`context.market` 与顶层 `market` 保持一致。
7. **自检**（必做）：跑 §6 的校验脚本，error 清零、warning 逐条确认后再交付。
8. **交付**：把文件给用户，并附「导入后要检查什么」（§4）。

## 3. 常用默认值（用户未指定时）

| 项 | 默认 |
|---|---|
| 时间切分（T+5） | train `2018-01-02~2023-06-18` / val `2023-06-26~2025-01-21` / test `2025-01-28~2026-08-28` |
| 时间切分（T+3） | 同上，但 val 起 `2023-06-23`、test 起 `2025-01-26`（满足 gap≥4） |
| factor_filter | `enabled:true, n_top:80, ic:0.01, icir:0.15, corr:0.9` |
| LightGBM 稳健档 | `lr 0.01 / num_leaves 15 / max_depth 6 / min_data 500 / l1 2 / l2 5 / ff 0.5 / bag 0.7 / rounds 3000 / es 100` |
| 基准 | CN `SH000300`、HK `HSI`、US `SPX`、CRYPTO `BTC`、FUTURES `CL.FUT` |

## 4. 导入后前端会做什么（务必转告用户）

1. 校验通过后弹预览：会标出「当前目录缺失的特征 / 市场变化 / 目录版本变化」。
2. 确认后整体覆盖表单；**不在当前因子目录里的特征会被丢弃**。
3. `factor_catalog_version` 只是提示，**实际训练用页面当前已发布版本**，不是回放旧版本。
4. QuantDB 直读市场还需满足「目录已发布 + 数据覆盖就绪 + 训练节点就绪」，否则「开始训练」按钮不可用。
5. 若日期间隔不足 `horizon+1`，后端会自动平移 val/test 起点并在 `system_notices` 提示。

## 5. 演示配置（可直接导入）

| 文件 | 场景 |
|---|---|
| [templates/cn-l1l2-t5-lightgbm.yml](templates/cn-l1l2-t5-lightgbm.yml) | CN / L1L2 / T+5 / LightGBM 稳健档（推荐起手） |
| [templates/cn-l1l2-t3-lightgbm.yml](templates/cn-l1l2-t3-lightgbm.yml) | CN / L1L2 / T+3 / LightGBM 基线（与上一份同特征，做周期对照） |
| [templates/cn-l1l2-t5-gru.yml](templates/cn-l1l2-t5-gru.yml) | CN / L1L2 / T+5 / GRU 深度学习 |
| [templates/hk-t5-lightgbm.yml](templates/hk-t5-lightgbm.yml) | HK / T+5 / LightGBM 跨市场 |
| [templates/quantmind-training-L1L2-ICIR-100-T5.yml](templates/quantmind-training-L1L2-ICIR-100-T5.yml) | CN / ICIR 优选 100 特征 / T+5 / LGB 重正则（大 preset） |

改这些文件比重头写更稳：它们已通过校验脚本、且特征 key 全部存在于本地目录。
仓库根目录另有两个规模更大的成品 preset（`quantmind-training-L1L2-120-*.yml`）可作参考。

## 6. 校验脚本（交付前必跑）

纯标准库优先（JSON 可无需依赖；YAML 需 PyYAML）：

```bash
# 本地直接跑（本机有 PyYAML 时）
python3 skills/model-training-config/scripts/validate_training_config.py <你的配置.yml>

# 无 PyYAML 时进容器跑（env-contract §2）
docker cp <你的配置.yml> quantmind:/tmp/cfg.yml
docker cp skills/model-training-config/scripts/validate_training_config.py quantmind:/tmp/
docker exec -w /app quantmind python3 /tmp/validate_training_config.py /tmp/cfg.yml
```

脚本检查：kind/schema_version、市场、日期顺序、`horizon+1` 间隔、模型类型白名单、
params 未知键、factor_filter 钳制区间、标签字段混入、factor_source 与直读市场一致性。
退出码 0=通过（可含 warning）、1=有 error。**error 必须清零**；warning 要逐条判断是否符合预期。

## 7. 相关技能

- **[[model-train-infer-backtest-report]]** — 提交训练/推理/回测/出报告（本技能只负责「生成可导入的配置」）。
- **[[quantmind-operations]]** — 平台运营总指南（训练 5 步流程、模型管理）。
- **[[quantdb-fields]]** — 字段单位与口径速查（选因子前必读）。
- **[[quantdb-data-structure]]** — QuantDB 目录/分区/代码格式。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 导入报「不是受支持的 QuantMind 模型训练配置文件」 | `kind` / `schema_version` 写错 |
| 导入报「训练、验证、测试时间段必须按先后顺序」 | 改成 `train_end < val_start`、`val_end < test_start` |
| 导入报「配置中的模型类型不受支持」 | `model_type` 不在 13 种里 |
| 导入后特征少了很多 | 不在当前目录的 key 被过滤；对照预览提示换用存在的 key |
| 超参没生效 | 用了非白名单键（如 `lgb_learning_rate`），被静默丢弃；改用共享键 |
| 训练时 val/test 起点和配置不一致 | 间隔 < `horizon+1`，后端自动平移 |
| Start 按钮灰着 | 直读市场目录未发布/覆盖未就绪，或训练节点未就绪 |
| 文件选择器看不到文件 | 扩展名不是 `.yml/.yaml/.txt` |
