# 美股个股终端 (Stock Terminal US)

## 模块说明

美股个股终端的数据后端：**标的池搜索 / 个股概要 / 未复权日线 + 拆股标记 / 8 面板详情聚合 / 中文资讯**。
与 A 股 `routers/stock_terminal.py`（2174 行）、港股 `market_analysis_hk.quanthk_feed` 平级，
同属「独立市场目录 + 复用市场分析 feed」架构 —— 但**不往 A 股路由里加第三个 market 分支**。

数据来自本地 QuantUS parquet（`QM_QUANTUS_DATA_DIR`，默认 `data/quantus/`）+ 本地 PG
（`news_article_enrichment` 资讯标签）+ Huntly SQLite（兜底），**无外部实时行情依赖**。
端点前缀 `/api/v1/stock-terminal-us`，注册于 `backend/services/api/main.py`
（import + include_router 各 1 行）。

## ⚠️ 口径与限制（改代码前必读）

| 项 | 事实（实测 2026-09-12） | 对接口的影响 |
|----|------|-------------|
| 价格 | yfinance `auto_adjust=False` 的**原始未复权价**；库内**没有复权因子**（`l1_factors.adj_factor` 恒 1.0） | 只提供 `adjust="none"`；拆股日价格真实跳变（AAPL 2020-08-31 收 499.23 → 129.04），由 `splits` 标记，**不自算复权** |
| 成交额 | `amount` 是**美元原始成交额**（≈ close×volume 量级，无换算系数） | 展示须标 US$；`market_cap_yi` 单位是**亿美元**，`cap_display` 是美元展示串 |
| 日线分区 | 2001-01-02 起，最新 2026-09-10 | 所有报价口径的时间锚 |
| 标的池 | security_master 516 只；最新分区**有行情 484 只** | `/list` 默认只列 484 只；`include_delisted=true` 带出其余 32 只并标 `delisted=true` |
| 无报价标的 | 32 只：退市/并购残留（ATVI/ANSS/SBNY…）或 **yahoo 代码口径不一致**（BRK.B/BF.B 在 yahoo 侧是 BRK-B/BF-B，按点号查无行情） | 按代码直查 `/profile`、`/detail` 仍可用，报价字段为 null，**不 404** |
| 英文名 | `en_name` 在 security_master 里**全为空串**（516/516） | 详情 `overview.en_name` 出口转 null；`/list?q=` 实际只命中 symbol + cn_name |
| f10 快照 | 516 个文件；484 只在交易的全覆盖；**约 424 只有 PE、464 只有 52w_high**；没有 PS / EV / forward PE | 估值面板只有 PE/PB/股息率/52 周区间，别去找 PS/EV |
| 估值分区 | `5_technical_derived/valuation` 分区自 2026-08-28 起全 null（历史坏分区未回填） | **估值一律以 f10 为主路径**，不读该分区 |
| 财务 | **只有年报**（各 488 文件、每股 5 个财年，income 42 / balance 72 / cashflow 56 列，yfinance 英文长列名） | 不做季度趋势 / TTM |
| 公司行动 | `splits` 357 文件 / 1509 行（最新 2026-09-03）；`dividend` 404 文件（最新 2026-09-09，AAPL 92 条） | 分红取近 20 次；`/kline` 与分红拆股面板都给**全部历史拆股** |
| 机构持仓 | 13F 口径，披露滞后约一季；`mutual_fund_holders.Date Reported` 最新 2026-07-31 | 响应带 `reported_date`，面板标注滞后 |
| 内部人 | SEC Form 4 最近披露 2026-09-08 | 近 30 条 + 这批流水的净买卖 |
| 指数成分 | QuantUS **没有 index_weights / 指数成分** | 详情返回里没有「宽基归属」，前端也不要找 |
| 行业 | `2_base_sector/sector/{SYM}.parquet`（516 文件，GICS 11 类 + industry），29 只 sector 为空 | `overview.sector/industry` 空值出口转 null（前端显示 --） |
| 推理分数 | `engine_signal_scores` 目前没有 US 行 | `/list` **不含** fusion / side / position_score 字段，前端按空态处理 |

### 拆股（为什么只做标记不复权）

`3_financial_data/splits/{SYM}.parquet` 是事件表，未复权价在拆股日必然跳变。
终端提供 `adjust="none"` + `splits`（**该股全部历史拆股**，前端按当前窗口过滤后画竖线），
让用户知道那是 4:1 拆股而不是闪崩 —— 与市场分析模块「跨期收益一律用中位数 +
个股级 ±100% 裁剪」的既定立场一致。

## 目录结构

```
backend/services/api/stock_terminal_us/     # 后端：自包含市场目录
├── router.py          # 5 个 GET + POST /refresh：鉴权 + to_thread + 信封 {success, data}
├── feed/
│   ├── base.py        # 薄封装：复用 market_analysis_us.feed.base 的目录/分区读/名称映射/缓存
│   │                  #   + 终端口径常量 + 单标的取数（谓词下推）+ 每股小表 TTL 缓存 + cap_display
│   ├── universe.py    # 标的池 / 搜索 / 分页列表（最新分区有行情者）/ 个股概要
│   ├── kline.py       # 未复权日线 + 全部历史拆股事件（显式分区 + 列裁剪）
│   ├── detail.py      # 聚合器（get_detail）+ overview/valuation/财务三表/分红拆股 + 空骨架
│   ├── research.py    # 分析师 / 财报（卖方覆盖）
│   ├── holdings.py    # 内部人 / 机构持仓（筹码披露）
│   ├── labels.py      # 财务中文标签映射（key=原始英文列名, label=中文）+ num() 出口
│   ├── news.py        # 资讯编排：匹配词规则 + PG enrichment 主路径 + 兜底调度
│   └── huntly.py      # Huntly SQLite 只读访问（元数据 + 标题 LIKE 兜底）
└── README.md
backend/tests/test_us_stock_terminal.py     # 零 mock，直连真实 parquet + PG + Huntly
```

前端（`electron/src/features/stock-terminal-us/`）形状契约见
`electron/src/features/stock-terminal-us/types.ts`，与本模块响应**逐键对齐**。

## 性能：三件事必须照做

1. **按日期取数走显式分区文件列表 + 列裁剪**（`daily_forward` 文件内自带 `dt` 列会
   遮蔽 DuckDB hive 分区列，全 glob 无法裁剪，耗时随库内分区数增长）。终端在此之上
   再加 **`symbol` 谓词下推**：500 日窗口 `WHERE symbol='AAPL'` 实测 0.20s、
   全市场读再 pandas 过滤 0.57s，且不物化 24 万行无关标的（`base._read_symbol_bars`）。
2. **单标的取数不走全市场截面表**。终端一律读每股一文件（`4_analyst/{表}/{SYM}.parquet`、
   `3_financial_data/{表}/{SYM}.parquet`），不调用 `_load_analyst_table`（那是市场分析模块的
   截面口径，`upgrades_downgrades` 单表 18 万行，为一只股票加载不值当）。
3. **资讯主路径走 PG 倒排窗口**：`tickers @> ARRAY[sym] OR title ILIKE %中文名%`
   按 `huntly_page_id DESC LIMIT 200` 取候选（主键后向扫描 + GIN/trgm 索引，
   实测 0.03-0.3s），再按 id 批量取 Huntly 标题/链接/时间。不要退回到
   「Huntly 全表 LIKE」——那是 47s 冷启动的兜底路径。

缓存：TTL 由 `market_analysis_shared.caching` 提供 —— 截面快照 5 分钟、
每股小表 / 标的池 30 分钟、K 线响应 5 分钟。`POST /refresh` 清空全部缓存。

## API 端点（前缀 `/api/v1/stock-terminal-us`）

全部 `Depends(get_current_user)`；响应信封 `{"success": true, "data": ...}`
（与 A 股终端一致，前端共享服务读 `resp.data.data`）。
错误码：非法代码 400、标的不存在 404（`未找到美股标的 XXX（不在标的池且最新交易日无成交）`）、
参数越界 422、内部异常 500。

| 端点 | 参数 | 说明 |
|------|------|------|
| `GET /list` | `q`（代码/中文名）、`page`≥1、`page_size` 1-600、`include_delisted` | 标的池分页（市值降序）；默认只含最新分区有行情者 |
| `GET /profile` | `symbol` | 头部信息（名称/行业/最新收盘/涨跌幅/市值/52周高低） |
| `GET /kline` | `symbol`、`days` 30-2000（默认 500）、`start`/`end`（闭区间，给定时忽略 days） | 未复权日线 + 全部历史拆股事件 |
| `GET /detail` | `symbol` | 8 面板聚合（见下，形状与前端 types.ts 对齐） |
| `GET /news` | `symbol`、`limit` 1-100（默认 20） | 个股资讯（enrichment 主路径，带情绪标签） |
| `POST /refresh` | — | 清空终端 + 美股市场分析缓存 |

### 响应形状（前端按此对接）

```jsonc
// GET /list -> data
{"items": [{"symbol": "NVDA", "name": "英伟达", "cn_name": "英伟达", "en_name": "",
            "display_name": "英伟达", "sector": "Technology", "sector_cn": "信息技术",
            "industry": "Semiconductors", "market_cap": 5.27e12, "market_cap_yi": 52727.39,
            "cap_display": "$5.27万亿", "pe_ratio": 27.57, "pb_ratio": 23.03,
            "close": 218.36, "pct_change": -2.37, "has_quote": true, "delisted": false}],
 "total": 484, "page": 1, "page_size": 50, "pages": 10,
 "trade_date": "2026-09-10", "adjust": "none", "notes": {...}}

// GET /profile -> data（键始终存在，缺值 null）
{"symbol": "AAPL", "cn_name": "苹果", "display_name": "苹果", "sector_cn": "信息技术",
 "trade_date": "2026-09-10", "open": 316.79, "high": 326.68, "low": 316.57,
 "close": 326.57, "prev_close": 315.34, "pct_change": 3.56, "volume": 69820744,
 "amount": 22801360879.46, "market_cap_yi": 47660.22, "cap_display": "$4.77万亿",
 "pe_ratio": 37.41, "pb_ratio": 44.37, "dividend_yield": 0.34,
 "high_52w": 344.57, "low_52w": 226.65, "dist_from_52w_high_pct": -5.22,
 "has_quote": true, "adjust": "none", "notes": {...}}

// GET /kline -> data
{"symbol": "AAPL", "name": "苹果", "adjust": "none", "count": 500,
 "start_date": "2024-09-09", "end_date": "2026-09-10", "truncated": false,
 "items": [{"date": "2026-09-10", "open": 316.79, "high": 326.68, "low": 316.57,
            "close": 326.57, "volume": 69820744, "amount": 22801360879.46}],
 "splits": [{"date": "1987-06-16", "ratio": 2.0}, ..., {"date": "2020-08-31", "ratio": 4.0}],
 "notes": {...}}   // splits = 全部历史（前端按窗口过滤后画竖线）

// GET /news -> data
{"symbol": "AAPL", "name": "苹果", "keywords": ["苹果", "AAPL"],
 "provider": "enrichment", "available": true, "total": 20,
 "items": [{"id": 610132, "title": "苹果iPhone 18 Pro系列预售：…",
            "link": "https://m.thepaper.cn/detail/34058192",
            "published_at": "2026-09-12 22:04:54", "source": "澎湃新闻 - 首页头条",
            "sentiment_score": 0.205, "sentiment_label": "neutral",
            "event_tags": [], "industries": [], "key_terms": [], "countries": [],
            "matched_by": "ticker"}],
 "note": "主路径 news_article_enrichment（ticker 精确 + 标题中文名），Huntly 兜底"}
```

`notes` 是口径说明字典（`universe` / `price_adjust` / `amount_unit` / `financials` /
`valuation` / `institutional` / `news`），页面可直接取用，不要在前端硬编码。

### `/detail` 的 8 个面板

每个面板**独立 try/except**：某段失败或无数据返回空骨架（键齐全、值为 null/空数组），
整体不 500。顶层 `{symbol, name, trade_date, overview, valuation, financials,
analysts, earnings, insiders, holdings, corporate_actions, notes}`。

| 面板 | 关键字段 | 数据源 |
|------|---------|--------|
| `overview` | `cn_name/en_name/sector/industry` / `close/pct_change` / `market_cap/cap_display` / `week52_high·low` / `avg_volume` / `trade_date` | security_master + f10 + 日线 |
| `valuation` | `pe_ratio/pb_ratio/dividend_yield/market_cap` / `week52_high·low` / `source`(f10 快照)/`asof`；附加 `size_tier`、`stale_warning`（陈旧快照门槛与市场分析同源） | `2_base_sector/f10` |
| `financials` | `periods[]`（近 5 个财年，倒序）+ `income/balance/cashflow[]`，每行 `{key: 原始英文列名, label: 中文, values: 与 periods 等长同序的美元原值}` | `3_financial_data/{income,balance,cashflow}` |
| `analysts` | `target{current/high/low/mean/median}`（0 占位转 null）/ `ratings[]`（0m 在前，强买/买/持有/卖出/强卖）/ `upgrades[]`（近 30 条倒序，`action` ∈ up\|down\|init\|reiterated\|other） | `4_analyst/{analyst_price_targets,recommendations,upgrades_downgrades}` |
| `earnings` | `history[]`（近 8 期季度 EPS，`surprise_pct` 已归一到百分数）/ `upcoming[]`（calendar 口径的下一财报日 + EPS/营收预期） | `4_analyst/{earnings_history,earnings_dates,calendar}` |
| `insiders` | `items[]`（近 30 条：`type` ∈ buy\|sell\|other，类型由 `Text` 前缀解析）/ `net{buy_value/sell_value/net_value/buy_count/sell_count}`（按这批流水计算） | `4_analyst/insider_transactions` |
| `holdings` | `insiders_pct/institutions_pct/institutions_float_pct/institutions_count` / `funds[]`（头部机构，含 `pct_held/pct_change/date_reported`）/ `reported_date` | `4_analyst/{major_holders,mutual_fund_holders}` |
| `corporate_actions` | `dividends[]`（近 20 次倒序，`{date, amount}`）/ `splits[]`（全部历史升序，`{date, ratio}`） | `3_financial_data/{dividend,splits}` |

## 数据源要点（都是踩过的坑）

- **内部人**：`Transaction` 列**恒为空串**，类型必须从 `Text` 前缀解析
  （`Sale at price...` / `Purchase at price...`）；AAPL 78 行里 40 行 `Text` 为空
  （授予/行权等无价格事件）→ 归 `other`（前端显示「其他」），绝不当作买卖信号。
- **机构持仓**：`major_holders` 是 4 行**无标签宽表**，顺序固定 = 内部人占比 /
  机构占比 / 机构流通股占比 / 机构家数（前 3 行是小数需 ×100，第 4 行是整数家数）。
- **评级动作**：`upgrades_downgrades.Action` 全样本只有 main/reit/down/up/init；
  归一为 up|down|init|reiterated|other，其中 up/down **复用**
  `market_analysis_us.feed.analysts._grade_direction`（评级词档位比较）——
  Action 说「main」但档位从 Equal-Weight 升到 Overweight 时按 up 处理（以档位为准）。
- **目标价占位**：`currentPriceTarget/priorPriceTarget` 的 0 是 yahoo 的「无值」，
  出口一律转 null（否则前端显示 0）。
- **超预期口径**：`earnings_history.surprisePercent` 是**小数**（0.0452 = 4.52%），
  `earnings_dates.Surprise(%)` 是**百分数**（6.74）—— 出口已分别归一，勿再缩放。
- **财报日历**：`calendar.Earnings Date` 在 parquet 里是**列表列**
  （`array([datetime.date(2026,10,30)], dtype=object)`），取首个元素解析；
  它是下一财报日的权威口径（AAPL 2026-10-30），`earnings_dates` 的东八区时间戳
  会差一天（2026-10-29 16:00-04:00），只在 calendar 缺失/过期时兜底。
- **财务标签**：yfinance 原始英文长列名 → 中文标签子集，**列不存在整条不展示**，
  不做 0 值兜底；`values` 原样给美元数值，格式化交给前端。
- **资讯主路径**：`news_article_enrichment`（约 58.8 万行）按 `tickers` 数组精确
  匹配（GIN 索引）+ 标题中文名 ILIKE 兜召回；匹配词 = 中文名 + 代码，
  **长度 ≤1 的代码不参与**（实测 `F` 在 Huntly 标题里命中 9.4 万条）；
  候选窗口按 `huntly_page_id` 倒序 200 条，再按发布时间倒序切 limit。
  同一张表的 `title` 可作为 Huntly 不可用时的标题兜底。
- **资讯兜底**：Huntly SQLite 用 `file:{path}?immutable=1` 只读连接
  （`mode=ro` 会被 Huntly Java 写锁阻塞），只匹配 `title`，代码关键词限定最近
  20 万行（`page` 行内含正文大字段，冷启动全表扫一次 47s）。已知风险：
  `immutable=1` 与 Huntly 并发写可能抛 `database disk image is malformed`，
  失败重试一次后降级为 `available=false` + 空 items。

## 测试

```bash
# 在容器内（后端依赖在容器里）
docker exec -w /app/backend quantmind python -m pytest tests/test_us_stock_terminal.py -q --no-cov
```

`backend/tests/test_us_stock_terminal.py`：**零 mock** 直连本地 parquet + PG + Huntly，
断言取向是契约与健康度（防停更 / 防口径失真 / 防 NaN 串 JSON / 防单段失败拖垮整体 /
防响应形状与前端 types.ts 漂移）。

覆盖点：分区时效（≥今日 14 天内）、列裁剪与分区裁剪、标的池规模与中文名覆盖、
**默认池 = 最新分区有行情者（484）且退市标的不出现、include_delisted 带出且无报价**、
搜索与分页、AAPL 日线量级与美元成交额比例、4:1 拆股与原始价腰斩、
**splits 为全部历史且不随窗口变化**、区间窗口与 truncation、**详情 12 键契约与
8 面板键集合**、财务 periods 对齐（values 等长同序、key 为原始英文列名）、
分析师（0m 在前 / action 枚举 / 目标价 0→null）、财报（单位归一 / calendar 口径）、
内部人（类型归一 + 净额=买入-卖出 + 计数一致）、持仓（major_holders 行序 + 13F 披露日）、
分红拆股条数与排序、**退市标的详情降级为骨架**、未知标的 404/400、
**PG enrichment 主路径（ticker + 标题、情绪标签、发布时间倒序）**、
Huntly 兜底路径、单字符代码只用中文名、**5 端点 + refresh 的 {success,data} 信封 200**。

## 相关

- 口径 / 性能原始依据：`backend/services/api/market_analysis_us/README.md`、`docs/美股市场分析模块_设计方案.md`
- 数据基座：`backend/services/api/market_analysis_us/feed/base.py`
- 共享层：`backend/services/api/market_analysis_shared/`（caching / market_days / display / names）
- 资讯标签：`backend/services/api/routers/news.py`（enrichment 表查询口径）、`backend/services/api/news/`
- 数据平台：`backend/services/engine/data_platform/quantus_hub.py`、同步 `backend/scripts/quantus_daily_sync.py`
