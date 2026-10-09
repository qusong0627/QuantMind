---
name: corporate-action-adjustment-auditor
description: "公司行动复权审计（QuantDB 直读 CN/HK/US）— 核对原始价、复权价与分红/送转/拆股事件的一致性：除权日总收益等式、未解释跳点、复权因子断点、多来源重复行、拆股价格对齐。用户问「复权数据对不对」「除权日价格跳变」「复权因子有没有断」「回测收益是不是被复权口径坑了」时使用。触发词：复权审计、除权核对、复权因子、除权跳点、公司行动、送转、拆股、adjusted close"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` / `--input` 纯标准库，可在宿主机或 dsh 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/corporate-action-adjustment-auditor/scripts/audit_adjustments.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/audit_adjustments.py --quantdb \
>      --market CN --symbols 000001.SZ --start 2026-01-01 --end 2026-09-30 --out /data/reports/adjustment-audit/cn_000001_sz.json
>    ```
> 3. **报告落盘**：建议容器内 `/data/reports/adjustment-audit/<market>_<窗口>.json`；改阈值重跑时保留前后两份。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内格式一致，前缀式会静默查空）。

# corporate-action-adjustment-auditor — 公司行动复权审计

把复权数据当作需要验证的证据而非默认可信：先冻结口径，跑确定性检查，再把「已证实的问题」与「缺失证据」分开报告。不预测公司行动，不提供事件信号，只审计数据处理。

## 能力总览

| 用法 | 检查内容 |
|---|---|
| `--demo` / `--input <csv>` | 离线引擎烟雾测试 / 任意 CSV 复核（列：symbol, date, close, adj_close, split_factor, cash_dividend） |
| `--quantdb --market CN` | **收益等式核对**：`(close_t×split+div)/close_{t-1}-1`（daily_unadjusted+事件表）vs `daily_forward` 前复权收益；配股单列「未建模」告警 |
| `--quantdb --market HK` | 跳点 + 拆股/红股对齐（yahoo splits，`c_t×ratio/c_{t-1}≈1`）+ 双来源重复行 + `adjust_factors` 因子断点/覆盖抽查 |
| `--quantdb --market US` | 跳点 + 拆股对齐 + 重复行（本地无复权序列，等式核对不可用） |

## 数据映射（QuantDB 本地）

| 市场 | 原始价 | 复权价 | 事件源（除净/除权日列） | 口径 |
|---|---|---|---|---|
| CN | `quantdb/1_kline_data/daily_unadjusted` | `daily_forward`（前复权） | `quantdb/3_financial_data/dividend_factors/{sym}.parquet`（`time`） | `interest`=每10股派息含税→÷10；`stockBonus`=每10股送转→split=1+bonus/10 |
| HK | `quanthk/1_kline_data/daily_forward`（**不复权**） | 无（`daily_backward` 为稀疏派生物，不可用） | `quanthk/3_financial_data/{dividend,splits}/{sym}.parquet`（`trade_date`，yahoo） | `split_ratio`=每股拆为几股（含红股送股） |
| US | `quantus/1_kline_data/daily_forward`（不复权） | 无 | `quantus/3_financial_data/{dividend,splits}/{sym}.parquet` | 同上 |

## 标准流程

1. 冻结窗口/代码/口径；先 `--demo` 确认脚本可用。
2. `--quantdb` 跑目标窗口（建议覆盖至少一个已知除权事件），JSON 落盘。
3. 逐条读 findings：`high/critical` 优先人工复核；`insufficient-evidence` 的条目整理成补数清单，**不得当通过**。
4. 修复后重跑同窗口对比（改变阈值时保留两版并说明原因）。

## 常见坑（2026-10-07 实测标定）

- **CN 对照用 `daily_forward`（前复权），绝不用 `daily_backward`**：后者在长假拼接缝上有损坏段。
- CN 等式标定：事件日 |diff|<0.001、非事件日 <1e-12（000001.SZ 2026 全窗）。配股（`allotment>0`）未建模，会以「已知缺口」单列而非数据错误。
- **HK `daily_forward` 是原始价（不复权）**；`2024-09-02~2026-05-08` 存在 `paid_hk`+`akshare` **双来源重复行（411 个交易日）**，读侧不主动去重会被重复计数。**且重复行不都近等**：约 0.5%~5% 的标的 `paid_hk` 行带（前）复权缩放（实测 1211.HK ×3、0788.HK ×10、0755.HK ×100；2025-01-15 为 70/1523 只），去重必须保留 akshare（published_at 最新、原始价）——本脚本按 `(time, published_at)` 排序取最新并告警。
- **HK `adjust_factors` 不能当复权序列**：多数活跃标的覆盖止于 2026-05-08、2021 年后分红未纳入、2020-04-21 存在多标的同日伪 step。脚本只用它做因子断点一致性抽查。
- US 本地无复权序列：只能拆股对齐 + 跳点 + 重复行；等式核对如实报 `insufficient-evidence` 语义（`check_return_mismatch=false`）。
- 阈值语义：`--return-tolerance` 默认 0.02（原始总收益 vs 复权收益允许偏差）；`--jump-threshold` 默认 0.40（无事件原始跳点）；拆股对齐容忍度内置 0.20（当日真实波动）。

## 脚本与参考

- `scripts/audit_adjustments.py`：离线确定性引擎 + QuantDB 装配层（纯标准库引擎，quantdb 模式用容器 pandas）。
- `references/methodology.md`：核对公式、执行顺序与解释规则。
- `references/output-contract.md`：JSON 报告契约（status/findings/severity 语义）。

## 来源与许可

方法论与输出契约移植自 [quantskills/skill-corporate-action-adjustment-auditor](https://github.com/quantskills/skill-corporate-action-adjustment-auditor)（**GPL-3.0-only**），数据层由 PandaData 改为本地 QuantDB 直读。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
