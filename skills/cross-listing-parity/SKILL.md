---
name: cross-listing-parity
description: "A/H 跨市场平价监控（QuantDB 本地直读）— 对同一发行人的 A+H 上市股票按 premium = A收盘价(CNY) ÷（H收盘价(HKD) × HKD→CNY汇率 × ratio）− 1 计算溢价，输出最新数据日全配对溢价表、Top/折价最深排行、250 交易日历史分位与序列统计，配对/价格/汇率缺口单列为数据质量 findings。用户问「A/H 溢价」「AH溢价率」「同一家公司 A 股比 H 股贵多少」「港股比 A 股折价多少」时使用。触发词：A/H溢价、AH溢价、A/H平价、交叉上市、跨市场价差、AH比价、AH股"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 或 `--data-root` 可覆盖）。
> 2. **执行位置**：`--demo` / `--input` 纯标准库，宿主机或 dsh 可直接跑；`--quantdb` 需要 pandas/duckdb/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/cross-listing-parity/scripts/ah_parity_audit.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/ah_parity_audit.py --quantdb \
>      --date 2026-08-27 --window 250 \
>      --out /data/reports/cross-listing-parity/ah_premium_20260827.json \
>      --md  /data/reports/cross-listing-parity/ah_premium_20260827.md
>    ```
> 3. **报告落盘**：容器内 `/data/reports/cross-listing-parity/<asof>.json|md`；改阈值/换数据日重跑时保留前后两份。Markdown 生成后必须过 `scripts/validate_report.py`。
> 4. **symbol 格式**：A 股后缀式 `601318.SH`、H 股四位+.HK `2318.HK`（与 `ah_membership.parquet` 及各 parquet 内格式一致；前缀式会静默查空）。

# cross-listing-parity — A/H 跨市场平价监控

对**显式配对表内的同一发行人 A/H 两地上市股票**做溢价折算观察：先冻结数据日、汇率与股数比口径，输出确定性排行与历史分位，再把「已证实的质量问题」与「缺失证据」分开报告。价差是观察，不是可执行套利信号——不建模结算、融券成本、股息税、资金管制与交易时段差异（边界见 `references/methodology.md`）。

## 能力总览

| 用法 | 内容 |
|---|---|
| `--demo` | 内置样本（纯标准库）冒烟测试，校验公式与报告链路 |
| `--input <csv>` | 任意配对 CSV 复核（列：`date,a_symbol,h_symbol,a_close,h_close,fx_hkd_cny[,premium_pct,ratio]`） |
| `--quantdb` | 本地直读：目标日全配对溢价表 + Top/折价最深排行 + 历史分位（250 交易日，原始价重算序列）+ 与数据集的逐对交叉校验 + 数据质量 findings |

常用参数：`--date`（默认=数据集最新分区日）、`--window`（默认 250）、`--fx`（数据集覆盖外的用户汇率）、`--top`（默认 10）、`--out` / `--md`。

## 本地数据映射（QuantDB）

| 数据 | 路径 | 口径/注意 |
|---|---|---|
| A/H 配对表 | `quanthk/2_base_sector/ah_membership.parquet` | 列 `h_symbol,a_symbol,名称,source,updated_at`；159 行 / 149 对（含 10 行内重）；名称匹配有缺口（见坑） |
| 溢价数据集（参考 + 汇率源） | `quanthk/2_base_sector/ah_premium/dt=YYYYMMDD/` | 每交易日一分区；列 `h_symbol,a_symbol,a_close,h_close,fx_hkd_cny,premium_pct`；**历史日 `a_close` 是构建时点前复权价**；分区止于 2026-08-27 |
| A 侧收盘价（重算源） | `quantdb/1_kline_data/daily_unadjusted` | 原始未复权价 |
| H 侧收盘价（重算源） | `quanthk/1_kline_data/daily_forward` | 不复权原始价；2024-09-02~2026-05-08 双来源重复行，去重取 akshare |
| 汇率 HKD→CNY | 数据集列 `fx_hkd_cny` | 中行折算价（akshare `currency_boc_sina`）；覆盖止于数据集末日，更晚日期需 `--fx`（脚本不联网抓汇率） |

## 标准流程

1. `--demo` 确认脚本可用。
2. `--quantdb` 跑目标日（缺省=数据集最新分区日），`--out` JSON、`--md` Markdown。
3. 读 JSON 依次核对：`summary`（截面分布）→ `top`/`bottom`（含窗口分位）→ `pairs[].diff_pp`（重算 vs 数据集）→ `quality_findings`（配对/价格/汇率/重复行各自单列）。
4. Markdown 过 `python3 scripts/validate_report.py <report.md>`；报告「数据说明」必须写清数据日与汇率来源。
5. 数据集覆盖日之后跑新日期：必须 `--fx`（如实记录来源），这是一份降级报告（`dataset_stale` + `fx_user_supplied` 会同时出现）。

## 常见坑（2026-10-07/08 实测标定）

- **数据集历史 `a_close` 是构建时点前复权价（基期=数据集末日 2026-08-27）**：基期当日与原始价逐对完全一致（149/149，diff=0）；越往回偏差越大——250 交易日窗口平均 |Δ|=1.92pp；60 交易日窗口至 2025-01-15 平均 7.58pp（当日 88/101 对 >0.05pp，最大 25.52pp）。**历史分位一律用重算（原始价）序列，不取数据集 `premium_pct`。**
- 公式复现：`premium_pct=(a_close/(h_close×fx_hkd_cny×ratio)−1)×100`，本表全部为同股同权 `ratio=1`；数据集自身列间公式核对 max diff=0.0。
- **H 侧双来源重复行（2024-09-02~2026-05-08）**：`paid_hk` 对部分标的施加过复权/缩放（实测 1211.HK 86.2667 vs akshare 258.7999 ≈×3、0788.HK 11.0 vs 1.1 =×10），**akshare release 才是原始成交价** → 去重取 akshare；两来源相对差 >0.5% 的 (日期,标的) 单列 finding（2025-01-15 窗口内 20 例）。
- 数据集行有 (日期,配对) 级重复：最近 250 分区共 12523 组，**组内取值全部相同（0 组分歧）→ 按值去重安全**；2025-08~12 约 90~110 组/日，2026-01 起固定 ≈10 组/日，且与配对表 10 组内重键集合逐一相同（构建器按配对表展开所致）。
- 配对表名称匹配缺口：比亚迪 `002594.SZ/1211.HK`、中国石油 `601857.SH/0857.HK`、中国石化 `600028.SH/0386.HK` 不在表内（akshare 清单缺）——报告如实标注，不要凭空补对。
- 数据集日期 = 两市同日交易且汇率存在的日子；H 假期缺分区。目标日无分区时按数据缺口处理，不插值。
- 本地无独立汇率源：数据集列即唯一汇率（中行折算价），止于 2026-08-27；更晚日期必须 `--fx`，否则 `fx_missing` 降级为不计算溢价。
- 价格缺失单列：2026-09-30 H 侧 2899.HK 缺收盘价；历史上 H 侧缺失多为尚未上市（2025-01-15 窗口 48 只）。

## 脚本与参考

- `scripts/ah_parity_audit.py`：离线确定性引擎（纯标准库）+ QuantDB 装配层（容器内 pandas/duckdb）；JSON 契约见 `references/output-contract.md`。
- `scripts/validate_report.py`：Markdown 报告结构校验（章节、数据来源/数据日/汇率/ratio/快照声明、禁用操作性表达）。
- `references/methodology.md`：公式、股数比、汇率方向、分位口径、套利边界与降级规则。
- `references/data-map.md`：本地数据结构与全部实测标定（含验证方法）。

## 来源与许可

方法论与报告契约移植自 [quantskills/skill-cross-listing-parity](https://github.com/quantskills/skill-cross-listing-parity)（**GPL-3.0-only**，commit `ca2230f2`），数据层由 PandaData 改为本地 QuantDB 直读；本地化删去 ADR 分支（本地无 ADR 配对表与 USD 汇率源，扩展条件见 `references/methodology.md`「ADR 延伸」）。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
