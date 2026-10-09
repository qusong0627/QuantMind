# QuantDB 本地数据地图与实测标定（HK CCASS 主市场）

来源：quantskills/skill-hk-us-institutional-concentration（源仓库许可为空/未声明）。
源技能的 api-map.md / runtime.md 描述 PandaAI API 与凭据；本文件替换为 QuantDB 本地数据地图。
全部标定于 2026-10-07 在本机容器实测。

## 主数据：CCASS 前 50 席位

路径：`quanthk/2_base_sector/ccass_top50/dt=YYYYMMDD/data.parquet`（宿主机
`/home/zbox/projects/quantmind/data/quanthk/...` ↔ 容器 `/data/quanthk/...`）。

| 列 | 类型 | 语义（已标定） |
|---|---|---|
| `stock_code` | string | 四位+.HK（`0700.HK`）；2024-11 起全库一致，未发现前缀式混入 |
| `participant_id` | string | 席位代码：`C00019`=香港上海汇丰（托管）、`A00003`/`A00004`=中国结算（南向通道）等 |
| `participant_name` | string | 席位名（繁中/英文） |
| `holding_quantity` | int64 | 席位持有**股数**（原始值） |
| `holding_percentage` | double | 占**公司总股本**的**分数（0-1）**——不是 0-100 百分比，也不是 CCASS 内部占比 |
| `query_date` | date32 | 分区日（与目录 dt 一致） |

每标的每日最多 50 行（按持有股数排序的前 50 席位）；`holding_percentage=0` 的行存在
（2026-09-11 实测 8522 行/日），"参与席位数"只计 `>0`。

### 语义标定证据（2026-10-07 实测）

1. **同日精确对账**：2025-12-15 `0700.HK` 中国结算两席位（A00003+A00004）
   `holding_quantity` 合计 = **1,006,948,064 股**，与 `hsgt_south/dt=20251215/data.parquet`
   同日记录完全相同（`holding_percentage` 均为 0.1101=11.01%，分数标度一致）。
2. **总股本复核**：2026-09-11 `0388.HK` 头号席位 579,122,694 股 / 0.4567 = 12.68 亿股，
   与港交所已发行股本 1,267,836,895 股吻合；同日 4 个席位互推总股本离散 <0.1%。
   `0700.HK` 互推 ≈91.05 亿股、`0005.HK` ≈171.7 亿股、`0001.HK` ≈38.3 亿股，均与公开股本一致。
3. 由此确认分母 = **公司总股本**（不是 CCASS 内占比）；`holding_percentage×100` 即可与
   hsgt_south 等官方口径直接对账。脚本内置两项在线检查：`denominator_check`
   （席位互推总股本离散 ≤2%）与 `southbound_check`（中国结算席位合计 vs hsgt_south，
   按**股数精确相等**判定，接受「同日」或「official(T)==CCASS(T-1)」双对齐）。
4. **官方源 T-1 竞态（2026-08-25 起）**：hsgt_south 主布局多数分区日存的是前一交易日
   快照。实测窗口 2026-06-12~09-11：08-24 前 5 标的逐日同日精确；08-25 起差异行
   54/54 全部满足 `official(T)==CCASS(T-1)`（含最大 +3.9% 的 1299.HK 单日），
   间或有个别日追平（08-26、08-28、09-09 同日对齐）。这是**源更新时点竞态**，
   不是数值偏差——把 hsgt_south 最新分区当「当日南向持股」用之前先核对对齐状态。
   另：hsgt_south 有零星缺失分区（如 20260618），脚本按 `partitions_missing_in_window` 列出。

### 覆盖与缺口（实测）

- 全库 459 个分区日：2024-11-22 起（早期每日行数少，2025 年起完整）。
- 2026-06-01~09-30 的 82 个分区日连续，唯一缺口为 2026-09-23~09-29（**回补任务进行中**，
  2026-10-07 时点尚未落盘）；2026-07-01 是香港休市日（非缺口）。
- 另有个别标的缺日：实测 1299.HK 缺 2026-08-24/08-25 两日（其余抽查标的齐全）——
  脚本按标的列出 `missing_dates` 并入覆盖门告警，不静默。
- 取窗纪律：先列分区再取窗（脚本自动做）；区间变化按**可用分区序列**计（`--window`
  是可用日期数而非自然日），避开未落盘日期，不要在缺口两侧直接算 Δ。

## 交叉源（可选，非本技能核心）

| 数据 | 路径 | 用途与缺口 |
|---|---|---|
| 南向持股 | **主布局** `quanthk/2_base_sector/hsgt_south/dt=YYYYMMDD/data.parquet`（每日全标的，symbol/holding_quantity/holding_percentage，分数标度）；旧布局 `{sym}.HK.parquet` 已于 2025-12-19 冻结 | 与 CCASS 中国结算席位按股数对账（双对齐：同日或 T-1，见上文第 4 条竞态）；分区自 2024-11-27 起连续（旧历史由 `quanthk_south_history_merge.py` 回填），**必须读 dt= 分区**——读旧按标的文件会静默拿到 ≤2025-12-19 的过时数据 |
| A/H 名单 | `quanthk/2_base_sector/ah_membership.parquet`（h_symbol/a_symbol/名称，159 行） | 发行人级去重（见 methodology）；A/H 不可互换，不得加总 |
| 证券主表 | `quanthk/2_base_sector/security_master/data.parquet` | 仅中英文名与来源时间（**无股本列**，总股本复核走上面第 2 条方法） |

## 明确不可用的源

- **US 13F**：本地无任何机构持仓数据 → `--market US` 显式拒绝。方法论保留 45 天可得性滞后
  （期末+45 日历日）供未来接入时执行；ADR/本地股去重规则同样保留。
- **CN holder_num**（`quantdb/3_financial_data/holder_num`，股东户数）：是**股东人数**的
  离散度指标，与机构托管集中度不是同一概念（户数多≠分散、少≠机构主导），本技能**不消费**，
  也不得与 CCASS 指标并列解读。

## 符号格式

- parquet 内：HK=`0700.HK`（四位+.HK）。前缀式（`HK0700`）会静默查空。
- `--input` 自备 CSV 无格式强制，但 CI/脚本比对用四位+.HK 最稳。
