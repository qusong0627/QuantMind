# 公司行为(除权除息)数据获取脚本使用说明

> 脚本：`fetch_corporate_actions.py`
> 数据源：通达信 TQ `get_divid_factors` 接口
> 输出：CSV 文件，遵循 [AGENTS.md](AGENTS.md) 规范

---

## 1. 用途

拉取 A 股市场的**除权除息事件**（派息 / 送股 / 转增 / 配股），保存为 CSV 供后续复权因子检测、历史重算、人工核查等场景使用。

**注意**：本脚本输出的是**独立 CSV 文件**，**不进入** QuantDB `features_daily`（旧 `fundamental_aligned.parquet` 已废弃）。主流程（`full_fetch.py` / `daily_update_pipeline.py`）的复权因子直接从 K 线 `factor` 字段获取，与本脚本互不依赖。

---

## 2. 运行环境

| 项 | 要求 |
|----|------|
| Python | 3.10+ |
| 依赖包 | `pandas`, `numpy` |
| 客户端 | 通达信客户端已启动并登录 |
| 网络 | 与通达信服务器连通 |

---

## 3. 快速开始

```bash
# 拉取 2026 年 6 月单月数据
python fetch_corporate_actions.py --start 202606 --end 202606

# 拉取 2025-2026 两年全量
python fetch_corporate_actions.py --start 202501 --end 202612 -o corporate_actions_2025_2026.csv

# 断点续传 (跨月大范围推荐)
python fetch_corporate_actions.py --start 202001 --end 202612 -o corporate_actions_all.csv --resume
```

---

## 4. CLI 参数

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `--start` | ✅ | — | 起始月份，格式 `YYYYMM` |
| `--end` | ✅ | — | 结束月份，格式 `YYYYMM` |
| `--output` / `-o` | ❌ | `corporate_actions.csv` | 输出 CSV 路径 |
| `--resume` | ❌ | False | 断点续传：跳过已存在的 `(symbol, yyyymm)` |
| `--include-bj` | ❌ | False | 包含北交所(BJ)股票。默认排除（与系统其他部分一致）|
| `--exclude-b-share` | ❌ | True | 排除 B 股（默认开启）|
| `--no-exclude-b-share` | ❌ | — | 包含 B 股（与 `--exclude-b-share` 互斥）|
| `--retries` | ❌ | 2 | 单只股票失败重试次数 |
| `--progress-step` | ❌ | 100 | 进度打印步长（每 N 只股票打印一次）|

---

## 5. 输出格式

CSV 文件（UTF-8 with BOM，Excel 友好），固定 7 列：

| 列名 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `symbol` | str | 大写市场前缀 + 6 位数字 | `SH600000` |
| `date` | str | 除权除息日，格式 `YYYY-MM-DD` | `2026-06-01` |
| `type` | int | 事件类型编码（见下表） | `1` |
| `bonus` | float | 每股现金分红（元） | `0.45` |
| `allot_price` | float | 配股价（元），无配股时为 `0.0` | `0.0` |
| `share_bonus` | float | 每股送股数（10 送 N → 写 N/10） | `4.0` |
| `allotment` | float | 每股配股比例，无配股时为 `0.0` | `0.0` |

**排序**：按 `(date, symbol)` 升序。
**去重**：按 `(symbol, date, type)` 去重，保留最后一次。
**symbol 格式**：严格遵循 [AGENTS.md](AGENTS.md) 股票代码规范（`SH` / `SZ` / `BJ` + 6 位数字）。

### 5.1 字段详解

#### `symbol` — 股票代码

- **格式**：大写市场前缀 + 6 位数字
  - `SH` = 上交所（含主板、科创板）
  - `SZ` = 深交所（含主板、创业板）
  - `BJ` = 北交所（默认排除，需 `--include-bj` 才会出现）
- **示例**：`SH600000`（浦发银行）、`SZ000001`（平安银行）、`SZ300750`（宁德时代，创业板）、`SH688981`（中芯国际，科创板）
- **来源**：从 TQ 返回的 code 前缀推断（见 §7.1 `code_to_suffix` 规则）

#### `date` — 除权除息日

- **格式**：`YYYY-MM-DD`
- **含义**：这一天股价开始按除权除息后的新基准价交易。**股权登记日**（R 日）通常是前一天
- **示例**：`2026-06-01` 表示 6 月 1 日为除权除息日
- **注意**：周末/节假日不会成为除权除息日，事件会顺延

#### `type` — 事件类型

- **类型**：`int`（TQ 内部编码）
- **常见值**：
  - `1` = 派息（现金分红） — 同时可携带 `share_bonus`
  - `15` = 转增（公积金转增股本）— 必有 `share_bonus`
  - 其他值视 TQ 实现而定（配股、拆分等）
- **判断"必有字段"的规则**：
  - 若 `type=1`：`bonus` 必非 0，`share_bonus` 可为 0 也可非 0
  - 若 `type=15`：`share_bonus` 必非 0
  - 出现配股事件时：`allot_price` 和 `allotment` 必有值
- **示例**：
  - `SH600857 type=1 bonus=0.45 share_bonus=0` → 现金分红 0.45 元/股，无送股
  - `SH603236 type=1 bonus=3.8 share_bonus=4.0` → 现金分红 3.8 元/股 + 10 送 4 股
  - `SZ000793 type=15 bonus=0 share_bonus=12.0` → 公积金 10 转 12 股，无现金分红

#### `bonus` — 每股现金分红

- **单位**：元/股
- **取值范围**：`>= 0`，0 表示无现金分红
- **换算**：中国公告惯例为"**每 10 股派 X 元**"，CSV 中 `bonus = X / 10`
  - 例：公告"10 派 5 元" → CSV 中 `bonus = 0.5`
- **示例**：
  - `bonus = 0.45` → 每股 0.45 元现金分红（公告：10 派 4.5 元）
  - `bonus = 3.8` → 每股 3.8 元
  - `bonus = 0.0` → 该事件无现金分红

#### `allot_price` — 配股价

- **单位**：元/股
- **取值范围**：`> 0` 当且仅当发生配股事件；否则 `0.0`
- **含义**：配股时股东认购新股的价格（通常低于市价）
- **示例**：
  - `allot_price = 0.0` → 2026 年 6 月**无配股事件**
  - `allot_price = 8.5` → 配股价 8.5 元/股
- **注意**：2026-06 的所有事件均为派息/送股/转增，**没有配股**，所以本批数据此列全 0

#### `share_bonus` — 每股送股数

- **单位**：股/股（无量纲，表示"每持有 1 股送多少股"）
- **取值范围**：`>= 0`
- **换算**：中国公告惯例为"**每 10 股送 Y 股**"，CSV 中 `share_bonus = Y / 10`
  - 例：公告"10 送 4 股" → CSV 中 `share_bonus = 0.4`（**注意是 0.4 不是 4**）
- **示例**（实际数据）：
  - `share_bonus = 4.0` → 公告"10 送 40 股"（科创板/创业板常见高送转）
  - `share_bonus = 12.0` → 公告"10 送 120 股"
  - `share_bonus = 0.0` → 无送股
- **业务含义**：送股后股价会除权下调（例：10 送 4 后，每股价格 ≈ 除权前 × 1/(1+0.4)）

#### `allotment` — 每股配股比例

- **单位**：股/股（无量纲）
- **取值范围**：`>= 0`，0 表示无配股
- **换算**：中国公告惯例为"**每 10 股配 Z 股**"，CSV 中 `allotment = Z / 10`
  - 例：公告"10 配 3 股" → CSV 中 `allotment = 0.3`
- **示例**：
  - `allotment = 0.0` → 2026 年 6 月无配股
  - `allotment = 0.3` → 每股可配 0.3 股新股
- **关联字段**：配股事件时 `allot_price` 和 `allotment` 同时非 0，`allot_price` 决定认购价

### 5.2 字段间的关系速查

| 业务事件 | type | bonus | share_bonus | allot_price | allotment |
|---------|------|-------|-------------|-------------|-----------|
| 纯派息 | 1 | 非 0 | 0 | 0 | 0 |
| 派息+送股 | 1 | 非 0 | 非 0 | 0 | 0 |
| 纯转增 | 15 | 0 | 非 0 | 0 | 0 |
| 派息+配股 | — | 非 0 | 0 | 非 0 | 非 0 |
| 纯配股 | — | 0 | 0 | 非 0 | 非 0 |

> **2026-06 数据特点**（1489 条事件分布）：
> - 纯派息（type=1, bonus>0, share_bonus=0）：**1332** 条
> - 派息+送股（type=1, bonus>0, share_bonus>0）：**141** 条
> - 纯送股（type=1, bonus=0, share_bonus>0）：**15** 条
> - 转增（type=15, share_bonus>0）：**1** 条
> - 配股列（allot_price / allotment）**全 0**

---

## 6. `type` 字段说明

TQ 接口返回的事件类型编码（不完整列表，以实际数据为准）：

| 值 | 含义 | 必有字段 |
|----|------|----------|
| 1 | 派息（现金分红） | `bonus` |
| 15 | 转增股本（公积金转增） | `share_bonus` |
| 其他 | 配股、拆分等 | 视情况 |

> 注意：TQ 对 type=1（派息）的事件也会同时填充 `share_bonus`（如"10 派 X 元 + 10 送 Y 股"合并事件）。

---

## 7. 与系统其他部分的关系

### 7.1 遵循的规范

- **AGENTS.md 股票代码规范**：symbol 严格用 `SH600000` 形式
- **AGENTS.md Parquet 清洗规则**：默认排除北交所（`symbol` 以 `bj` 开头）和 B 股
- **UTF-8 with BOM**：CSV 用 `utf-8-sig` 编码，Excel 直接打开不乱码

### 7.2 独立文件

- 本脚本输出**不进** QuantDB `features_daily`（旧 `fundamental_aligned.parquet` 已废弃）
- 主流程（`full_fetch.py` / `daily_update_pipeline.py`）**不读**本 CSV
- 用途：复权因子人工核查、AGENTS.md 复权因子检测规则的辅助数据源

### 7.3 上下游依赖

```
[通达信 TQ] ──→ fetch_corporate_actions.py ──→ corporate_actions_YYYYMM.csv
                         ↓
                  (人工核查/复权因子检测)
```

---

## 8. 常见使用场景

### 8.1 拉取单月数据

```bash
python fetch_corporate_actions.py --start 202606 --end 202606
# 输出: corporate_actions.csv
```

### 8.2 跨月大范围 + 断点续传

```bash
# 第一次跑（可能被 Ctrl+C 中断）
python fetch_corporate_actions.py --start 202001 --end 202612 -o ca_all.csv --resume

# 中断后重跑，自动跳过已拉取的 (symbol, yyyymm)
python fetch_corporate_actions.py --start 202001 --end 202612 -o ca_all.csv --resume
```

### 8.3 包含北交所（特殊场景）

```bash
# 默认排除北交所；如需包含（如人工核查）
python fetch_corporate_actions.py --start 202606 --end 202606 --include-bj
```

> ⚠️ 包含 BJ 的输出**不能**直接与 QuantDB 数据合并（旧 `fundamental_aligned.parquet` 已清洗 BJ，且该源已废弃）。

---

## 9. 故障排查

| 症状 | 原因 | 解决 |
|------|------|------|
| `[ERR] market=5 返回空` | TQ 未连接 | 启动通达信客户端并登录 |
| `ModuleNotFoundError: No module named 'pandas'` | 当前 venv 缺包 | 切换到含 pandas 的 Python，或 `pip install pandas` |
| 全部 `share_bonus=0` | 旧版（v1）脚本 bug | 升级到 v2：本脚本已修复 `allotprice` / `sharebonus` 列名重命名 |
| 单只股票反复失败 | TQ 临时异常 / 该股票无数据 | 查看 `<output>.errors.csv` 失败清单，等待后用 `--resume` 重跑 |
| 拉取很慢 | 单线程顺序调用 TQ，~13 只/秒 | 正常；1 年 12 个月约 70 分钟。建议按月分批 |
| 提示日期格式错误 | `--start` / `--end` 不是 `YYYYMM` | 改为 6 位数字月份，如 `202606` |

---

## 10. 性能参考

| 规模 | 估算耗时 |
|------|----------|
| 1 只股票 | ~13 ms |
| 1 个月（~5200 只 A 股） | ~70 秒 |
| 1 年（12 个月） | ~14 分钟 |
| 5 年（60 个月） | ~70 分钟 |

**优化建议**：
- 大范围拉取务必加 `--resume`，可随时中断恢复
- 不要并发多实例跑同一 TQ，会触发服务端限流

---

## 11. 相关文件

- 脚本：[`fetch_corporate_actions.py`](fetch_corporate_actions.py)
- 数据源封装：[`tqcenter.py`](tqcenter.py) — `get_divid_factors` 接口
- 规则文档：[`AGENTS.md`](AGENTS.md) — 股票代码格式、清洗规则
- 数据样例：[`corporate_actions_202606.csv`](corporate_actions_202606.csv) — 2026 年 6 月数据
- 备份（修复前）：`corporate_actions_202606.csv.bak`（含旧版 v1 格式数据）

---

## 12. 变更历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-17 | 初版：单月拉取，`sh600857.SH` 小写前缀格式，share_bonus 列因列名不匹配被静默丢弃 |
| v2 | 2026-06-23 | 重写：跨月范围、CLI、断点续传；`SH600857` 大写前缀；修复 `allotprice` / `sharebonus` 列名重命名；默认排除 BJ/B 股；market=5 + code 前缀推断市场后缀 |
