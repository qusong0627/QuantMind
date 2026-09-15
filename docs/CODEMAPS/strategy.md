# CODEMAP: strategy（策略 / 存储 / 创作）

> 用途：「策略存在哪、什么格式、怎么上线、为什么跑不起来」的定位地图。
> 现状：**5 种格式并存**（P3 收敛为 2 种：params / minibt），本页是迁移期的权威索引。

## 职责
策略的创建（模板/AI-IDE）、存储（PG+COS）、版本与状态、回测准入、上线（模拟/实盘绑定）、沙箱执行。

## 入口文件
| 文件 | 职责 |
|---|---|
| `shared/strategy_storage.py` | **名义唯一入口**：save/get/delete（`delete` async 曾被同步调用——P3 修）；实际仍有多处旁路（见下） |
| `engine/qlib_app/api/user_strategies.py` | REST CRUD + `/activate`（要求 is_verified） |
| `engine/qlib_app/services/strategy_templates.py` + `strategy_builder.py` | 文件系统模板（`strategy_templates/*.py`，87 个）与回测构建 |
| `engine/qlib_app/services/user_strategy_loader.py` | 加载 + **保存时 AST 闸门**（T-P0-02 起共享 `shared/strategy_code_gate.py`） |
| `engine/strategy_lab/`（sdk/runner/ast_checker） | Lab SDK（setup/on_bar）+ 强 AST 白名单（闸门内核） |
| `engine/routers/ai_ide/`（chat/workspace/executor） | AI 写策略 + 文件树 + 容器化执行（minibt 自动路由镜像） |
| `engine/ai_strategy/` | AI 生成服务（`ai_strategies` 镜像表，遗留） |
| `trade/sandbox/` | 托管/模拟盘的用户策略执行（on_tick 契约） |

## 五种格式（现状 → P3 收敛目标）
| 格式 | 代表 | 归属 |
|---|---|---|
| Qlib `STRATEGY_CONFIG` | 模板 + AI 生成 | → params 声明式 |
| minibt DSL（`Strategy.next`） | 11 模板 | → **保留**（第二种格式） |
| Lab SDK（`setup/on_bar`） | Strategy Lab | → 并入 params 或下架 |
| 沙箱 `on_tick` | trade/sandbox | → params 或下架（**当前无生成器产出，等于空转**） |
| 裸脚本 / AI 兜底模板 | ai_strategy service | → 转换器（P3），哑弹优先清理 |

## 对外契约
- `strategies` 表（`parameters` JSONB 存 topk/weight_mode/min_score/max_position_pct/lot_size/market/f_*）
- 状态：DRAFT→VERIFIED→SIM→LIVE（P3 状态机化；现状：is_verified + Redis active 键拼凑）
- 回测准入：Celery `qlib_app/tasks.py` 回测成功 → `mark_as_verified`

## 常见故障 top5
| 症状 | 根因/位置 |
|---|---|
| 策略删除不掉 | `strategy_storage.delete` 是 async，调用方未 await（P3 修） |
| 改了参数没生效 / 被重置 | `save()` 无条件重置 `execution_config`（P3 修）；min_score 死配置 |
| 模板启动模拟盘空转 | 沙箱要 `on_tick`，模板产出的是 STRATEGY_CONFIG |
| AI 生成的策略跑不起来 | 兜底模板 `handle_data()` 无执行器认识 |
| 回测结果不入证 | AI-IDE 同步回测不触发 mark_as_verified（P3 接统一引擎） |

## 禁区
- 新策略格式评审红线：**不允许第五种格式**（只能 params/minibt）；
- 写 `strategies` 表必须走 `strategy_storage`（现存旁路在逐步收编）；
- 策略代码执行前必须过 `validate_strategy_code`（保存 + 沙箱提交两道）。