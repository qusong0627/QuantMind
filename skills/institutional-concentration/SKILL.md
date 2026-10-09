---
name: institutional-concentration
description: "机构持股集中度诊断（CCASS 本地直读，HK 主市场）— 按日构建每标的的托管席位结构面板：参与广度、top1/top5/top10 份额、席位 HHI、参与席位数、区间变化，配研究门禁 + 证据台账 + 阈值敏感性 + 时点可用性纪律。用户问「机构持股集中度」「CCASS 前 50 席位」「头部席位主导还是分散」「HHI/广度变化」「13F 滞后」时使用。触发词：机构持股、持股集中度、CCASS、前十席位、席位集中、HHI、头部主导、持股广度、南向席位、13F"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data/quanthk/...` ↔ quantmind 容器 `/data/quanthk/...`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖；dsh 容器见 `/quantmind/data`）。
> 2. **执行位置**：`--demo` / `--input` 纯标准库，可在宿主机或 dsh 直接跑；`--quantdb` 需要 pandas/pyarrow，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/institutional-concentration/scripts/inst_concentration.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/inst_concentration.py --quantdb --market HK \
>      --symbols 0700.HK,0005.HK --start 2026-06-12 --end 2026-09-11 --out /data/reports/inst-concentration/hk_20260911
>    ```
> 3. **报告落盘**：容器内 `/data/reports/inst-concentration/<market>_<末日>/`（`quality_report.json` + 面板 CSV + 席位明细）；**交付前跑** `verify_report.py --out <同目录>`，未 PASS 不得交付。
> 4. **symbol 格式**：四位+.HK（`0700.HK`），前缀式会静默查空。**US 本地无 13F 源（显式拒绝）**；CN holder_num 是股东户数，与机构集中度不是同一概念，本技能不消费。
> 5. **取窗避让**：CCASS 有回补缺口时先列分区再取窗（脚本自动列）；不要跨未落盘日期算区间变化。冒烟测试用小样本（≤10 标的）避免与后台回补抢资源。

# institutional-concentration — 机构持股集中度诊断（CCASS 本地化）

把"机构持股比例高"当作入口而不是结论：拆成**参与广度 / 头部席位主导 / 证据置信度**三分量，
先冻结口径、跑确定性检查、过证据台账，再把「已证实结构」与「缺失证据」分开报告。
输出是截面结构诊断，不是收益预测。

## 能力总览

| 用法 | 做什么 |
|---|---|
| `--demo` | 离线确定性引擎自检（纯标准库；覆盖全部五种结构标签 + 一例阈值翻转） |
| `--input <csv>` | 任意持股明细复核（列：`symbol,date,holder_id,holding_pct[,holder_name][,holding_shares]`；百分比 0-1/0-100 自动识别） |
| `--quantdb --market HK --symbols ... --start --end` | **本地 CCASS 直读**：每标的每日 参与广度 / top1/5/10 / HHI / 席位数 / 20 交易日 Δ，含总股本互推与南向对账两项在线校准 |

产出：`quality_report.json`（含证据台账 7 门、敏感性、异常）+ `institutional_concentration_panel.csv`
（标的×日序列）+ `participant_detail.csv`（席位明细，`--no-detail` 可关）+ `harness_report.json`（验收）。

**三个必守口径**：① `holding_percentage` 是**占公司总股本**的 0-1 分数（脚本已 ×100），
不是 0-100 也不是 CCASS 内部占比；② 广度与 HHI 是**前 50 席位下界口径**（50 名以外席位不可见）；
③ 头部席位 = 托管行/结算通道（汇丰托管、中国结算南向），**是托管集中不是实益持有人主导**。

## 数据映射（QuantDB 本地）

| 市场 | 数据 | 口径 | 状态 |
|---|---|---|---|
| HK | `quanthk/2_base_sector/ccass_top50/dt=YYYYMMDD/data.parquet` | 每标的每日前 50 席位：股数 + 占**总股本**分数（0-1） | 主源；2024-11-22 起 459 个分区日 |
| HK 对账 | `quanthk/2_base_sector/hsgt_south/dt=YYYYMMDD/data.parquet` | 南向持股（分数标度，每日全标的） | 与 CCASS 中国结算席位同日精确对账（**读 dt= 分区**；旧的 `{sym}.parquet` 布局已冻结在 2025-12-19，勿读） |
| HK 名单 | `quanthk/2_base_sector/ah_membership.parquet` | A/H 同发行人映射 | 发行人级去重用；A/H 不可互换，不得加总 |
| US | 无 | 13F 45 天滞后知识保留在 references | **不可用**（脚本显式拒绝） |
| CN | `quantdb/3_financial_data/holder_num` | 股东**户数** | 概念不同，仅备注，不消费 |

## 标准流程

1. 冻结四元组：市场 / 样本池 / 观察窗口 / 持有人定义（CCASS=前 50 托管席位）。
2. `--demo` 确认引擎可用 → `--quantdb` 跑目标窗口（先列分区；避开回补缺口）。
3. 读 `quality_report.json`：先看 `ledger` 7 门与 `semantics`（标度、分母校准、南向对账），
   再看 `sensitivity.label_flips`（标签是否稳定），最后才读结构标签。
4. `anomalies` 与 `insufficient_data` 一律按**不可排名**处理；缺失不得当 0。
5. 交付前 `verify_report.py --out <同目录>` 必须 PASS；改阈值重跑时保留前后两份报告。

## 常见坑（2026-10-07 实测标定）

- **标度**：`holding_percentage` 是 0-1 分数（同日与 hsgt_south 精确互证：11.01% 对 0.1101）；直接当百分比用会差 100×。
- **分母是总股本**：0388.HK 席位 579,122,694 / 0.4567 = 12.68 亿股 = 港交所已发行股本；同日多席位互推离散 <0.1%。脚本对离散 >2% 报 inconsistent。
- **头部≠实益**：0700.HK 最大席位是汇丰托管 32.5%（名义持有池），解读为"托管集中"，不得写成"某机构控股"。
- **广度是下界**：前 50 席位合计（0700.HK 2026-09-11 为 76.3%）不含 50 名以外席位与非 CCASS 登记股份。
- **缺口语义**：2026-07-01 时点是香港休市（非缺口）；2026-09-23~09-29 是回补进行中的真缺口——区间 Δ 只按**可用分区序列**计。
- **零份额行**：日日约 8.5k 行 `holding_percentage=0`（在榜但空仓席位），"参与席位数"只计 >0。
- **南向双席位 + T-1 竞态**：中国结算为 A00003 与 A00004 两个席位，对账必须相加（2025-12-15 两席合计与官方南向股数逐股相同）。官方源自 **2026-08-25 起存在更新时点竞态**：多数分区日官方值是**前一交易日**的 T-1 快照（实测 54/54 差异行满足 `official(T)==CCASS(T-1)`，08-24 前则逐日同日精确）。对账按「同日**或** T-1」双对齐判定；把 hsgt_south 最新分区当「今日南向持股」使用前先核对是否 T-1。
- **hsgt_south 双布局陷阱**：目录里同时存在 `dt=YYYYMMDD/data.parquet`（主布局，每晚同步，2024-11-27 起完整）与旧 `{sym}.HK.parquet`（2025-12-19 冻结）。**消费必须读 dt= 分区**——读旧文件会静默拿到 2025-12-19 前的过时数据，看起来"该源已停更"。`quanthk_south_history_merge.py` 负责把旧历史回填进分区。
- **标签本地化**：`dominant_seat`/`broad_participation` 分别对应源技能 `dominant_holder`/`broad_institutional`；改了名字是为了避免在 CCASS 语境下过度承诺"机构主导"。
- **敏感性必做**：主阈值（top1≥20% 或 HHI≥0.10 判主导）之外至少复判紧/松各一档；边缘样本（如 top1≈17.5%、HHI≈0.05）会在两档间翻转——翻转清单必须随报告给出。

## 脚本与参考

- `scripts/inst_concentration.py`：离线引擎 + CSV 复核 + QuantDB CCASS 装配（quantdb 模式需容器 pandas/pyarrow）。
- `scripts/verify_report.py`：交付验收闸门（纯标准库，13 项检查，FAIL 退出码 1）。
- `references/methodology.md`：研究门禁 10 条、证据台账 7 门、时点纪律（HK T+1 / 13F 45 天）、A/H 发行人身份、局限。
- `references/quantdb-ccass-map.md`：CCASS 列语义与实测标定证据、覆盖缺口、交叉源、不可用源清单。

## 来源与许可

方法论移植自 [quantskills/skill-hk-us-institutional-concentration](https://github.com/quantskills/skill-hk-us-institutional-concentration)——
**源仓库许可为空（未声明许可证）**：本技能**仅做方法论改写**（研究门禁、证据台账、结构三分、
阈值敏感性、13F 45 天滞后知识点），**未搬运其数据层代码**（源用 PandaAI/PandaData API 与凭据，
本地全部替换为 QuantDB 直读）；仅限本地研究使用，不对外分发、不做商业使用；如需对外使用请先
与源仓库权利方确认授权。分析结论不构成投资建议。
