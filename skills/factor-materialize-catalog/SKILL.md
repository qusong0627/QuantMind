---
name: factor-materialize-catalog
description: "RD 挖掘因子后半程（挖掘产出→物化→训练目录→跨库训练→推理）：物化器用法与值级查重门（|ρ|≥0.9 判重拒收）、候选四桶（已物化/判重拒/无码孤儿/从未尝试）、目录草稿与发布（发布=替换不是合并）、跨库训练载荷解析与四道门（同市场、参与库均须已发布、enabled 校验、显式钉版本）、模型血缘与推理侧 schema 漂移（缺列才硬失败、漂移只提示）。在 QuantBot / Claude Code 中回答「因子挖完了怎么进训练」「有几个因子没加进来」「发布这份草稿会怎样」「跨库/多库训练怎么配」「库改了存量模型还跑不跑」时使用。触发词：物化、因子物化、rd_mined、因子没加进来、因子没进目录、值级查重、重复因子、因子目录、训练目录草稿、发布因子目录、注册因子、跨库训练、多库训练、factor_catalog_versions、因子血缘、schema 漂移、模型跑不出来、该日期无数据"
---

> ## ⚙️ 运行环境契约（先于本文其余内容执行）
>
> 本技能运行在 **QuantBot（dsh 容器）** 或**宿主机/本地 Claude Code**：
>
> 1. **重依赖脚本一律在 quantmind 容器内跑**（QuantBot 的 dsh 容器没有 pandas/sqlalchemy/backend 包）：
>    ```bash
>    docker cp skills/factor-materialize-catalog/scripts/catalog_status.py quantmind:/tmp/ && \
>    docker exec -w /app quantmind python3 /tmp/catalog_status.py
>    ```
>    技能脚本源 = `/quantmind/skills/factor-materialize-catalog/scripts/`（dsh 内路径）；`docker cp` 只是因为 `skills/` 不在容器的挂载表里。
> 2. **改库要重启才生效**：`quantmind` 容器**bind-mount 了仓库的 `backend/`→`/app/backend`（rw）**，所以改完**不需要重建镜像**，`docker restart quantmind` 即可（实测挂载表，2026-10-08）；不重启则运行中的进程仍持旧字节码（Python 不热重载）。验证路由要查**运行进程**的 openapi，别查本地文件。
> 3. **数据库**：`docker exec -i quantmind-db psql -U quantmind -d quantmind -c "<SQL>"`（`-U quantmind` 必需，`postgres` 角色不存在）。只做 SELECT；写库只走物化器/目录端点/脚本。
> 4. **admin API**：容器网络内 `http://quantmind:8000`（宿主 `http://127.0.0.1:8000`），需 admin 令牌——**不要自铸 token**，让用户在页面操作或复用已登录会话。
> 5. 本文 `~/.claude`、`cp -r ... ~/.claude/skills` 等仅适用于本地维护者，**QuantBot 不要执行**。

# 因子物化 → 训练目录 → 跨库训练（挖掘后半程）

上游是 `rd-agent-factor-mining`（挖掘/回测/导出），本技能从**挖掘产出落库之后**接手：

```
rd_agent_factors（挖掘登记）
   │  ① 物化  rd_mined_materialize.py（值级查重门）
   ▼
/data/quantcustom/6_ml_datasets/rd_mined （CUSTOM 市场 parquet 库 + _materialize_manifest.json）
   │  ② 注册  字段发现 → 建草稿 → 播种（全部已发现列 enabled=TRUE）
   ▼
训练目录草稿 ──③ 发布（= 替换线上，不是合并）──▶ published 版本
   │  ④ 跨库训练载荷解析（_resolve_quantdb_factor_payload）
   ▼
训练任务（跨库特征 = 锚库裸列名 + 副库 "库:列"）
```

**每步只碰下一层**：物化只写 parquet；注册才刷字段表/建草稿；发布才动线上；训练只读 published。
所以「还没发布」时，线上训练**完全不受影响**——这也是安全停靠点。

## 0. 状态体检（先跑这个，「有几个因子没加进来」的唯一权威口径）

```bash
docker cp skills/factor-materialize-catalog/scripts/catalog_status.py quantmind:/tmp/ && \
docker exec -w /app quantmind python3 /tmp/catalog_status.py [--json]
```

输出四段：挖掘产出分桶 / 物化库面 / 训练目录（线上+每份草稿的差集）/ 从未尝试的因子清单。
页面入口等价：物化面板 `GET /api/v1/admin/training-data/rd-mined/materialize/status`（10s 轮询，只读）。

**候选四桶的含义**（panel 与 CLI 共用一个实现，口径不会漂）：

| 桶 | 含义 | 还能不能进目录 |
|---|---|---|
| `already_materialized` | 已物化（进了 parquet 和清单） | 已在目录（或等发布草稿） |
| `rejected_duplicate` | 跑过、被值级查重拒（清单里有 `corr` 与 `corr_against`） | `--force` 重跑才有机会；同值因子本身就不该重进 |
| `no_code` | `factor_code` 为空的**孤儿登记**（源码在盘上也不存在） | 不能，别伪造、别删行；要就重挖 |
| 从未尝试 | completed + 有码 + 不在清单 | 可以物化，`--factor-ids` 直接喂 |

## 1. 物化（写 parquet + 清单）

```bash
# 演练：列目标列名（有列名冲突会标「已消歧」）
docker exec -w /app quantmind python backend/scripts/rd_mined_materialize.py \
    --factor-ids <id1,id2> [--task-id <task>] [--limit N] --dry-run
# 写入（不带 --register：只物化，不碰训练目录）
docker exec -w /app quantmind python backend/scripts/rd_mined_materialize.py --factor-ids <ids>
```

- ⚠️ **`--limit` 是在 SQL 里按 `created_at` 升序截断**（全表最老的先出），不是"待物化里的前 N 个"。它适合全量回填，不适合抽查；抽查用 `--factor-ids`。
- 值级查重门：候选因子值与**对照库**（l1_factors + rd_mined 现有列）近 60 天采样日的最大 |ρ| ≥ **0.9 拒收**（`rejected_duplicate`）、≥ **0.8 告警但仍收**。拒绝理由与对比列写进清单，可复查。
- 清单 `_materialize_manifest.json` 是**唯一权威记录**（`materialized` / `rejected_duplicate` / `error`），幂等判据就是它——已物化/已拒绝的默认跳过，`--force` 才重做。
- 尾部自动跑「分区 schema 对齐」（把新列补进历史分区），别在物化跑一半时 kill：中途中断会留下列漂移的中间态，让它跑完收尾。
- 并发：同一时刻只允许一个物化进程（flock）；面板按钮与此口径一致，重复点击 409。

## 2. 注册：草稿是「发布」的输入

注册 = `record_source_fields`（刷新字段表）→ `create_catalog_draft`（**空**草稿）→ `seed_catalog_mappings`（把**全部已发现列**按 `enabled=TRUE` 播种）。

两条路：

| 入口 | 行为 |
|---|---|
| 物化面板「开始物化」/ CLI `--register` | 物化 + **自动发布**（建草稿→播种→发布一气呵成），无需人工点 |
| 后台「模型训练数据集」页 | 新建草稿 → 勾选/调整 → **用户点发布**；「复制为草稿」可把任一版本再复制回来 |

> **发布 = 替换**：把旧 published 转 archived、把草稿原样扶正，**中间没有合并**。草稿里有什么，发布后线上就只剩什么。所以**不要在有存量草稿的库里建"只含新因子"的底**——注册路径建草稿时必须从线上播种（`seed_draft_from_published`/`clone_version_mappings`）。
>
> 存量草稿缺口体检与补种：`backend/scripts/backfill_draft_from_published.py [--market XX] [--apply]`（默认演练）。判据是「草稿启用集 ⊇ 线上启用集」，只写草稿，不碰 published。

发布前先看差集（体检脚本第二段会给）：草稿少了线上有的特征 ⇒ 发布即缩，页面确认框会标红。

## 3. 跨库训练载荷（最后一步：让模型用上多个因子库）

提交入口 `POST /api/v1/models/run-training`（admin 面同路径 `/api/v1/admin/models/run-training`）。载荷关键键：

```json
{
  "factor_source": "l1_factors",                 // 锚库（必需）
  "factor_catalog_version": "qdb-...-<锚库 published>",
  "factor_catalog_versions": {"rd_mined": "qdb-custom-rd_mined-<published>"},
  "features": ["<锚库裸名>", "rd_mined:rd_amihud_20d"]   // 裸名=锚库→声明序首个命中；"库:列"=显式限定
}
```

解析产出 `factor_field_sources`：锚库写**裸列名**、副库写 `"库:列"`，读取端（`QuantDBFactorReader`）按此语法拼接。

**四道门**（任一不过都是 422，报文即原因）：

1. **同市场**：每个参与库的目录版本必须与训练请求的 market 相同。`rd_mined` 挂在 **CUSTOM**，所以它与 `CUSTOM/l1_factors` 组合训练时 market 必须解析成 CUSTOM；默认（benchmark SH000300）会解析成 CN，配 `rd_mined` 副库必挂。
2. **参与库都必须有已发布版本**：显式钉了非 published 的版本 → `factor_catalog_version for {库} is not the active published source version`（**草稿 id、跨市场 id 都报这句**，报文不区分，先查市场再查状态）。没钉 → 自动取该库 `published_at` 最新。
3. **enabled 校验**：`features` 里出现的列必须在该库 published 版本的 enabled 映射里，否则 `Features are not enabled in pinned QuantDB catalog: {库:列}`。**这是"物化了但没发布"的直接症状**。
4. 锚库 `factor_source` 数据必须 ready（`QuantDBFactorReader.assert_ready`，按训练起止日期查 parquet 覆盖）。

## 4. 推理侧：模型记住什么、库变了谁受影响

训练时把血缘钉进模型 `metadata.json`（`admin_training_utils.py:600-630`）：

| 键 | 内容 | 钉谁 |
|---|---|---|
| `factor_field_sources` | 逻辑名 → 物理引用（锚库裸列名 / 副库 `"库:列"`） | 全部参与库 |
| `factor_catalog_versions` | `{库: 版本 id}`，只含**实际用到列**的库 | 全部参与库 |
| `factor_coverage` | 每库 version_id + published_at + min/max_date | 全部参与库 |
| `factor_schema_hash` | 锚库**列名集合**的 sha256 | **仅锚库** |
| `factor_catalog_published_at` | 锚库发布时刻 | **仅锚库** |

**推理不查目录**：读侧按**列名**从 parquet 直取（`QuantDBFactorReader.read_range`），
目录版本只当标签（`realtime_service.py:447` 的 `feature_version`）。所以
**发布新版本 / 建草稿 / 物化新列，对已有模型零影响**——与 §0 的「安全停靠点」是同一件事。
`factor_schema_hash` 只钉锚库 ⇒ **副库增列永远不影响任何模型**，只有锚库增列才会漂移。

三条链的闸门不同，别混：

| 链 | 缺列 | 整库哈希漂移 |
|---|---|---|
| 实时 / 回放（`realtime_core`） | 交 `fill_values` 兜底，**不报错**（静默降级） | 无闸门 |
| 批量**预检**（`script_runner._query_quantdb_readiness`） | 硬失败，指名到列 | 放行 + `schema_drift` 标记 |
| 批量**执行**（模板 / `data_loader.load_date_data`） | 硬失败（`read_range` 按名报） | 放行 + WARNING |

漂移政策 2026-09-20 定（`b3e3a61b`：缺列才硬失败、漂移只提示），**2026-10-08 才补齐执行侧**
——模板与 `data_loader` 当时漏改，症状是**预检绿灯、跑起来 exit 2「该日期无数据」**
（实测 `mdl_cust_train_20260914130341_887a7a0d_c2e90650`，锚库 `/data/quantcustom/l1_factors`
273→282 列）。回归测试：`backend/tests/test_inference_schema_drift_execution.py`。

**同族第二个坑（2026-10-08 修）**：实时装配的缺失判据原本是裸
`isinstance(val, float)`，而 QuantDB 因子列是 DuckDB 显式 CAST 出来的 **float32**
（float64 会撑爆训练容器内存），`np.float32` **不是** Python `float` 的子类
（`np.float64` 是）⇒ 判据对 QuantDB 列恒为 False ⇒ **NaN 静默绕过 `fill_values`
直接进 ONNX 输入矩阵**，且 `missing` 计数不涨（运维看不见）。实测：模型
`mdl_cust_train_20260914130341_887a7a0d_c2e90650` 在 2026-09-11 基线上 600519 有
3 个、000001 有 2 个 float32 NaN，旧判据识别到 **0** 个。判据已单源到
`backend/shared/feature_incremental.is_missing`（实时装配与增量覆盖共用；增量侧
还有个连带后果：float32 NaN 基线被判成「可用的 t1 值」）。回归：
`backend/tests/test_realtime_nan_fill.py`。**教训：判「值是不是缺失」永远别用裸
`isinstance(x, float)`——跨库/直读的列是 float32。**

**第三个坑（同日修）**：预检的缺列检查遍历 `factor_field_sources`，而执行侧读的是
`feature_columns`；映射为空的存量模型（实测 87 个里 **16** 个：HK `l1_factors`，
各 8 特征）整个检查被跳过 ⇒ 又是「预检绿灯、跑起来 exit 2」。判据已对齐执行侧要读的列。

体检（只读，实测 2026-10-08：87 个 QuantDB 直读模型 / 漂移 1 / 跨库 6 库组合 2）：

```bash
docker cp skills/factor-materialize-catalog/scripts/model_lineage_drift.py quantmind:/tmp/ && \
docker exec -w /app quantmind python3 /tmp/model_lineage_drift.py [--json]
```

> **已知盲区**（代码自己承认，别指望这道门）：哈希只看列名，**列名不变而口径变**
> （量纲 / 复权 / 单位）抓不到，推理会静默出错——只能靠字段单位契约与金样回归守。
> 同族还有一条：漂移放行后，若源库**新增了自己的 OHLCV 列**，`read_range` 的取值
> 会从 donor（如 `daily_backward` 后复权）**静默切到源库自身的价格列**，行过滤
> （`close>0`/`volume>0`）与前瞻标签口径随之改变（2026-10-08 评审实测：当前没有
> 模型锚在无 OHLCV 的库上，属潜在暴露而非现存故障）。

## 5. 常见问题速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 「因子挖出来了但训练里选不到」 | 物化了但**没发布**（或压根没物化） | 先跑体检脚本；补物化或发布草稿 |
| 提交训练 422 `Features are not enabled...` | 引用了未发布/未勾选的列 | 查该列在哪个版本、是否 enabled，发布对应草稿 |
| 提交训练 422 `not the active published source version` | 版本 id 不是 published，**或 market 不匹配** | 查 `SELECT market,status FROM qm_training_factor_catalog_version WHERE version_id=...` |
| 「发布这份草稿会怎样」 | 发布=替换 | 用体检脚本看差集；只看「+」是加、有「-」是缩 |
| 同批因子有的没进目录 | 值级查重拒收（与现有因子 |ρ|≥0.9） | 清单里查 `corr`/`corr_against`；确实要重判用 `--force` |
| 一批登记没有 `factor_code` | 孤儿登记，源码不存在 | 不能物化；重挖或清理需用户决定，**不要擅删生产行** |
| 「注册表 ready，点下去『该日期无数据』」 | 模型锚库新增列 → 哈希漂移（执行侧旧硬闸门，2026-10-08 已修）；或**映射为空**的模型被预检跳过（同日已修） | 跑 §4 体检脚本看漂移；若仍报错，是**要用的列真没了**（看 `read_range` 的报错指名） |
| 实时信号出现 NaN / 排名乱 | 值的缺失判据用了裸 `isinstance(x, float)`，漏掉 float32（2026-10-08 已修为 `is_missing`） | 新写判据一律用 `feature_incremental.is_missing`；回归 `test_realtime_nan_fill.py` |

## 6. 验证清单（改完/跑完对着查）

- 物化后：库面 `factor_columns` +N、清单新增 `materialized` 条目、分区对齐日志无 error。
- 注册后：字段表新增列；草稿 enabled = 线上 + 本次新增；线上版本仍是旧 id（未动）。
- 发布后：`status='published'` 的新版本 enabled = 发布前草稿；旧版本转 archived 仍在库里（可复制回草稿）。
- 训练前：`catalog.up_to_date=true`（库面列集 == 线上启用集）才说明目录已追上库面。
- 训练后（尤其跨库）：`model_lineage_drift.py` 里该模型的「记录 hash == live hash」；
  锚库日后增列会显示为漂移——**不阻断推理**，但它是「模型看的数据已换版」的唯一信号。

## 相关技能

- 上游：`rd-agent-factor-mining`（挖掘→回测→导出）
- 下游：`factor-train-pipeline`（筛选集合并自定义市场→发布→训练）、`model-train-infer-backtest-report`（训练→推理→回测→报告）
