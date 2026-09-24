# 全局股票池（Global Stock Pool）— v2（TXT 即事实源）

回测 / 模型训练 / 推理 / 模拟盘 / 实盘 / 因子挖掘 / Strategy Lab 共用的
**股票池唯一事实源**。

> v2 设计原则：**简单**。一个池 = 一行元信息（PG）+ 一个成员 TXT（磁盘）。
> 编辑保存 → 重写 TXT → 立即生效，没有草稿/发布/版本/回滚。

## 模型

```
qm_stock_pool（PG 单表，元信息）          /data/stock_pool/（成员，唯一事实源）
├── pool_id / code / name / market       ├── <code>.txt（全局池，根目录扁平）
├── scope（global / tenant / user）      ├── u<user_id>/<code>.txt（用户池目录隔离）
├── pool_type（system_index/static/imported）  └── t<tenant_id>/<code>.txt（租户池目录隔离）
├── status（active / archived）          SH600036          ← 前缀式，一行一个
├── file_path / symbol_count / checksum  SH600519          ← # 开头为注释，可手改
└── is_system / source_*                 内置池成分由 QuantDB 指数权重
                                         启动 + 每日自动刷新
                                         （旧版根目录 u<uid>_<code>.txt 仅读兼容，
                                          下次保存自动迁移到隔离子目录）

qm_stock_pool_binding（引用守卫：被引用的池不可归档/删除）
```

- **TXT 人可读可手改**，回测引擎、训练容器、其他模块可以直接按路径读。
- **口径强制**：TXT / 前端 / API = 前缀式 `SH600036`；进程内解析后统一转
  后缀式 `600036.SH`（DB / Qlib / parquet 层）。转换只经 `normalize.py`
  （委托 `StockCodeUtil`），**禁止手写切片**。
- **checksum**：成员集合的 sha256 前 16 位（排序后），回测 / 训练结果落库，
  成分变了能看出来。

## 内置池

`builtins.py` 是唯一目录（收敛原三份白名单）：
csi300 / csi500 / csi1000 / sse50 / gem / star / csi800 / all_a /
hs300_ext\* / hk_main\* / us_sp500\*（带 \* 的 `optional_source=True`，
数据源未接入时解析返回空 + 显式告警，不静默）。

- 启动期：seed 元信息 → 从 QuantDB 拉成分写 TXT（`main_oss._ensure_stock_pool`）。
- 运行期：engine 进程内 worker 每日刷新（`run_builtin_pool_refresh_worker`）；
  resolver 读到内置池 TXT 缺失/为空时还会**自愈重拉**一次。
- 内置池成员**不可手工编辑**（409），后台有「立即刷新」按钮。

## ref 语法（消费方唯一入口 PoolResolver）

| ref | 含义 |
|---|---|
| `pool:csi300` | 按 code 解析（读其 TXT） |
| `pool_id:sp_xxx_ab12cd34` | 按 pool_id |
| `csi300` / `all_a` | 裸 code：先查库，再回内置目录 |
| `list:SH600036,SZ000001` | 内联列表 |
| `file:/abs/x.txt` 或裸路径 | 本地文件（txt 每行一个 / csv 带表头） |
| `all` | **不过滤**（unfiltered，对应旧 universe='all'） |
| `cos://...` / `user_strategies/...` | 旧路径透传告警，不重复实现 |

```python
from backend.shared.stock_pool import resolve_pool, resolve_pool_sync

snap = await resolve_pool("pool:csi300", tenant_id=tid, user_id=uid)
snap.api_symbols  # ['SH600036', ...] 前缀式，给 API/前端/信号过滤
snap.symbols      # ['600036.SH', ...] 后缀式，给 parquet/Qlib
snap.checksum     # 结果可复现凭证
snap.warnings     # 空池 / 缺文件 / 未接入 的显式原因（绝不静默）
```

**严格语义**（回测 / 训练 / 推理 / 模拟盘 / 实盘共用 `filters.py`）：
池为空或信号零命中 → 消费方显式失败，**绝不退化为全市场**。

## 目录结构

```
backend/shared/stock_pool/
├── constants.py      枚举与阈值
├── schemas.py        Pydantic DTO（StockPool / PoolSnapshot / 写入请求）
├── normalize.py      代码口径归一（委托 StockCodeUtil）
├── builtins.py       内置池目录（唯一清单）
├── repository.py     PG 单表 CRUD + save_members/read_members
├── resolver.py       ★ PoolResolver —— 唯一解析入口
├── parser.py         ★ 上传解析引擎（CSV/TXT ↔ stocks_index.json）
├── materializer.py   成员 TXT 读写 + Qlib instruments 物化
├── filters.py        信号池过滤（推理/模拟盘/实盘共用）
├── legacy_bridge.py  旧 stock_pool_files 写侧登记为 scope=user 池
├── seed.py           启动 seed + 内置池 TXT 刷新 + 每日 worker
└── migrations/001_create_stock_pool.sql   幂等 DDL（ensure_tables 执行）
```

## API

后台管理（`require_admin`，`/api/v1/admin/stock-pools`）：

```
GET    /meta                     枚举与阈值
GET    /resolve?ref=             解析调试（排障第一入口）
GET    /health                   健康检查（缺文件/空池/无引用告警）
GET    /  POST /                 列表 / 新建（仅 global）
GET    /{pool_id}  PATCH  /{pool_id}
POST   /{pool_id}/archive        归档（被引用 409）
DELETE /{pool_id}                删除（仅已归档，连带删 TXT）
GET    /{pool_id}/members        成员（读 TXT）
PUT    /{pool_id}/members        覆盖成员（symbols 或粘贴文本）→ 写 TXT → 立即生效
POST   /{pool_id}/members/import csv/txt 文本导入（覆盖）
GET    /{pool_id}/export         下载 TXT
GET    /{pool_id}/preview        预览（含最新行情指标）
POST   /{pool_id}/refresh        内置池：从 QuantDB 重拉成分覆盖 TXT
POST   /parse                    上传解析（只出报告不落库）
POST   /create-from-members      解析确认后建池（保存即可用）
GET    /{pool_id}/usages         引用情况
POST   /{pool_id}/bindings       登记引用 / DELETE 解除
GET    /bindings/by-target       反查目标绑了哪些池
POST   /bindings/reconcile       从模型 metadata 回填引用
```

用户态只读（engine `/api/v1/stock-pools`）：
`/options`、列表、`/resolve`、`/{pool_id}`、`/{pool_id}/members`。

前端：管理后台「推理引擎 → 全局股票池」，两个页签
（**股票池列表**：成员编辑即保存；**上传解析导入**：解析报告勾选确认建池）。

## 消费方接入现状

| 消费方 | 入口 | 语义 |
|---|---|---|
| 回测 | `QlibBacktestRequest.pool_id` → 解析 → 物化 instruments 覆盖 universe | 空池拒绝回测 |
| 训练 | 编排器（有 DB）解析 → `config.yaml` 传 `pool_symbols` → 容器内过滤 | 池非空零命中直接报错 |
| 推理 | `script_runner.execute(pool_id=...)` | 空池/零命中显式失败 |
| 模拟盘 | `run_cycle(pool_id=...)`；调度器从 `trade:active_strategy` 读配置 | 零命中终止本轮 |
| 实盘 | `live_trade_config.pool_id` / 请求 `pool_id` → `_load_signal_rows` 裁剪 | 零命中拒单 |
| SDK/因子 | `_ALLOWED_UNIVERSES` / alpha_agent 白名单从 builtins 派生 | 不再三份各写 |

## 数据库落地

- 结构唯一入口：`backend/shared/db_init.sql`
  - 第 64 节：新装建全（元信息表 + binding 表，直接含 `file_path`）。
  - 第 66.3 节：存量收敛（合并自 `upgrade_v1.0.4/1.0.5`）——补 `file_path`、
    DROP v1 遗留的 version/member 表、状态归一 draft/published → active。
- 启动期 `ensure_tables` 也会执行 `migrations/001_create_stock_pool.sql`
  （与上面一致，幂等）。

## 边界（明确不做）

- **无版本/回滚**：TXT 覆盖即生效；历史可复现性由结果里落库的
  `pool_checksum` 承担。要留档就 `GET /{id}/export` 存一份。
- dynamic 规则池 / HK·US 指数成分 / 用户自助建池配额：未做，需要时再立项。
- 北交所等索引外标的：上传解析落在 `not_in_index` 并明确提示，不静默丢。
- `stock_pool_files` 旧链路完全不动，仅写侧登记桥接（legacy_bridge）。
