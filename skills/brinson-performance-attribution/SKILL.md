---
name: brinson-performance-attribution
description: "Brinson 业绩归因（Fachler/BHB + HHI + Carino 多期链接）— 把组合相对基准的主动收益拆成配置/选股/交互三效应并核对残差；支持行业级 CSV 输入与 QuantDB 本地直读（US/HK/CN 行业映射 + 全集等权基准）。用户问「超额收益从哪来」「配置和选股各贡献多少」「组合的行业暴露对不对」「多期主动收益怎么归因」时使用。触发词：Brinson、业绩归因、配置效应、选股效应、交互效应、Fachler、BHB、Carino、超额收益分解、HHI"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo` / `--input` 纯标准库，可在宿主机或 dsh 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/brinson-performance-attribution/scripts/brinson.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/brinson.py --quantdb --market US \
>      --portfolio /tmp/pf.csv --periods 2026-04-01:2026-06-30,2026-07-01:2026-09-30 \
>      --out /data/reports/brinson/us_q2q3.json
>    ```
> 3. **报告落盘**：建议容器内 `/data/reports/brinson/<market>_<窗口>.json`；改参数重跑时保留前后两份。
> 4. **symbol 格式**：US 大写 Ticker `NVDA`；HK 四位+.HK `0001.HK`；CN 后缀式 `000001.SZ`（与各 parquet 内格式一致，前缀式会静默查空）。

# brinson-performance-attribution — Brinson 业绩归因

把主动收益拆成配置 / 选股 / 交互，先核对残差恒等式，再解释效应来源；支持多期 Carino 几何链接。只做行业维度的效应分解，不预测收益、不给交易建议。

## 能力总览

| 用法 | 内容 |
|---|---|
| `--demo` | 内置三期样例（源 run_demo 数据），验证脚本可用；期望 R_p=15.8146%、allocation_linked=2.6096%、residual_linked≈0 |
| `--input <csv>` | 行业级输入（`sector,w_p,w_b,r_p,r_b`，每行业一行）；含 `period` 列 → 自动 Carino 多期链接 |
| `--quantdb --market US` | 个股组合 + yahoo 行业映射（`--level sector` 11 类 / `industry` 108 类）→ 行业级归因 |
| `--quantdb --market HK` | 行业=akshare_profile「所属行业」（31 类；`sector/` 目录 sector 列全空不可用） |
| `--quantdb --market CN` | 行业=instrument_detail `rs_hyname`（128 类，静态快照）；收益=前复权（含分红） |

`--method fachler|bhb` 切换配置效应定义；`--benchmark <csv>` 覆盖缺省基准（行业映射全集等权）；`--text` 打印人读摘要。

## 输入契约（CSV）

- 行业级：`sector,w_p,w_b,r_p,r_b`；可选 `period` 列（多期）。行业名期内唯一、数值有限、权重和 1±`--weight-tolerance`（默认 0.02），违者 `insufficient-evidence` 不出数。
- 组合/基准：`symbol,weight`（两列，权重 >0）。权重和超出容差直接拒绝（防把「百分比写法」当权重归一化）；容差内自动归一化并在 findings 记录原始和。

## 本地数据映射（QuantDB，2026-10-07 标定）

| 市场 | 行业映射 | 粒度/覆盖 | 区间收益（daily_forward） |
|---|---|---|---|
| US | `quantus/2_base_sector/sector/*.parquet` | yahoo：sector 11 类 / industry 108 类 | 不复权（价格收益，不含分红） |
| HK | `quanthk/2_base_sector/akshare_profile/*.parquet` | 「所属行业」31 类（2784→去重 2779 键） | 不复权；双来源重复行读取侧去重 |
| CN | `quantdb/2_base_sector/instrument_detail/instrument_detail.parquet` | `rs_hyname` 128 类（快照 HqDate=20260720） | **前复权（含分红再投）** |

- 基准（缺省）= 行业映射 ∩ 窗口内有行情 的标的等权；缺行情的标的数以 info finding 报告（US 487→~474/期；HK 2779→2621；CN 5535 全覆盖）。
- 收益 = 窗口内首/末有效收盘之比 − 1（非日历首末日）。组合缺某行业时 r_p=r_b（选股/交互=0）；基准缺组合所持行业时 r_b=基准总收益（中性约定，medium finding）。

## 标准流程

1. 明确区间与两套权重（或组合 CSV）；先 `--demo` 确认脚本可用。
2. `--quantdb`（或 `--input`）跑目标窗口，JSON 落盘，读 `status / gates / findings`。
3. **先看残差**：`metrics.residual`（多期为 `domain_result.linked.residual_linked`）必须 ≈0（1bp 门禁）再看三效应分解；不为 0 先查输入权重和与精度。
4. 多期用 `--periods a:b,c:d`（期序=列出顺序）；headline 为 Carino 链接后几何值，行业明细/HHI 为最后一期快照（源契约）。

## 常见坑（2026-10-07 实测标定）

- **HK `sector/` 目录是空的**（2818/2818 sector 列全空）——行业必须走 `akshare_profile.所属行业`；CN 没有 `sector/` 目录，行业在 `instrument_detail.rs_hyname`。别按「2_base_sector/sector」想当然取数。
- **收益口径三市场不同，禁止混比**：CN=前复权（含分红），HK/US=不复权（价格收益）。HK `daily_forward` 在 2024-09-02~2026-05-08 有 `paid_hk`+`akshare` 双来源重复行（2026-04 窗口实测 63,726 行重复），读取侧已按 `published_at` 去重（finding 报 info）。
- **残差恒等式**：权重和恰为 1 时单期残差恒等为 0（实测 |残差| ≤ 5.6e-17；多期 Carino residual_linked ≈ 7e-17）。若 >1bp，先怀疑 CSV 权重和/精度，而不是分解公式。
- **HHI 是行业级**（Σw²，非个股级）；基准等权时行业 HHI 反映的是行业映射的行业数量结构（US ≈0.111，CN ≈0.018）。
- 组合权重**超出 ±0.02 容差直接拒绝**（`insufficient-evidence`），不会静默归一化——权重写成百分数（和=100）会在这里被抓。
- 行业映射是快照：CN `instrument_detail` 静态（HqDate=20260720）、US/HK 随数据同步更新；期间行业调整不反映。
- 校准证据：与源实现独立对拍 worst |diff|=6.9e-18（200 组随机 × 2 方法 0 失配）；US 2026Q2 独立复算基准收益 10.255979% 与脚本一致。

## 脚本与参考

- `scripts/brinson.py`：离线确定性引擎（纯标准库：单期 Fachler/BHB、Carino、门禁、报告封装）+ QuantDB 装配层（pandas）。
- `references/methodology.md`：公式、Carino 链接、解释规则与校准记录。
- `references/output-contract.md`：JSON 报告契约（status / findings / domain_result 字段语义）。

## 来源与许可

方法论与输出契约移植自 [quantskills/skill-brinson-performance-attribution](https://github.com/quantskills/skill-brinson-performance-attribution)（源为 draft，**GPL-3.0-only**），数据层由外部数据服务/用户自备改为本地 QuantDB 直读，并按本仓 CLI 家族约定重写。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
