---
name: batch-inference-analysis
description: "批量推理结果分析 — 用 QuantMind 选股策略方法论分析每日信号、行业轮动、个股分数区间、负分参考。在 QuantBot / Claude Code 中分析批量推理结果、解读每日信号、判断市场状态、选股决策、做空参考时使用。触发词：分析批量推理、解读信号、每日选股、市场状态判断、行业轮动分析、负分参考、信号分析、批次分析、选股决策"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：
>
> 1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
> 2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
>    ```
>    脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（QwenPaw 对 `/app/db` 有写权限，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（研报级排版，首选）；
>    ② docker 不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
>    ③ 两者都不可用则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# 批量推理结果分析技能

基于 QuantMind 选股策略方法论，分析批量推理产出的每日信号，做出入场/选股/买卖/做空决策。

## 数据基础

批量推理产出：
- **batch**：多个交易日的推理集合（`/models/inference/batch/{batch_id}`）
  - `member_runs`：每个交易日的 run 记录（run_id/trade_date/signals_count）
- **单日 run**：`/models/inference/runs/{run_id}`
  - `items`：5377+ 只股票信号，每只含 `fusion_score`（分数）/`board`（板块）/`industry`（行业）/`market_cap_tier`（市值分档）/`trend`（趋势）/`prev_score`/`prev2_score`/`next_score`

## 认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
```

## 1. 拉取批量推理数据

```bash
# 1. 批量推理历史
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batches?page=1&page_size=10"

# 2. 某批次详情（含 member_runs 每日记录）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batch/{batch_id}"

# 3. 单日 run 信号（分析数据源）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/runs/{run_id}"
```

**分析入口**：用 batch 的 `member_runs` 拿到每日 run_id，逐个拉取信号分析，或用最近一个 run 分析今日。

### 批量聚合（推荐，一次拿全）

`/models/inference/batch/{batch_id}/aggregate` 一次性返回整批的聚合分析（免去逐日拉取）：
```bash
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batch/{batch_id}/aggregate"
# 返回: {per_symbol, groups, movers, daily, meta}
# per_symbol: 每只股票跨日分数/排名/IC
# groups: 行业/板块分组聚合
# movers: 分数变动最大的标的
# daily: 每日 TopN/分布
# meta: 批次数/区间/IC/共识带（Mann-Kendall 趋势）
```

## 2. 市场状态判断（三层过滤）

### 第1层：行业信号强度 — 决定是否入场

用 Python 计算行业 avg Top1：
```python
import json, urllib.request

def fetch_run(run_id):
    req = urllib.request.Request(f"{BASE}/api/v1/models/inference/runs/{run_id}",
        headers={"Authorization": f"Bearer {TOKEN}"})
    return json.load(urllib.request.urlopen(req))  # 顶层直接含 items/total

def market_signal(items, top_n=20):
    # 取 Top20 按分数
    top = sorted(items, key=lambda x: -(x.get("fusion_score") or 0))[:top_n]
    # 按行业分组，取每行业最高分
    ind_top = {}
    for it in top:
        ind = it.get("industry", "") or "未知"
        s = it.get("fusion_score") or 0
        if ind not in ind_top or s > ind_top[ind]:
            ind_top[ind] = s
    ind_avg_top1 = sum(ind_top.values()) / max(1, len(ind_top))
    strong = sum(1 for s in ind_top.values() if s >= 0.10)
    return {"ind_avg_top1": ind_avg_top1, "strong_industries": strong, "top_industries": dict(sorted(ind_top.items(), key=lambda x:-x[1])[:5])}
```

**入场阈值**（参数扫描优化）：
| 策略 | 入场线 | 空仓线 | 强行业数 |
|------|--------|--------|---------|
| 保守 | ≥0.10 | <0.10 | ≥5 |
| **平衡（推荐）** | **≥0.09** | **<0.06** | **≥2** |
| 激进 | ≥0.07 | <0.06 | ≥1 |

**判断**：
- `ind_avg_top1 < 0.06` → 绝对空仓
- `0.06-0.09` → 观望/极轻仓
- `≥0.09` → 正常选股

### 第2层：大盘均线过滤 — 防崩盘
模型在暴跌时给高分是反向信号。用指数 K 线判断：
```bash
# 上证指数 K 线（QuantDB index_daily）
curl -s -H "$AUTH" "$BASE/api/v1/market/kline?symbol=000001.SH&market=A&period=daily&days=30"
```
**规则**：上证指数 < MA20 → 强制空仓（避开 2024 年 2 月微盘崩盘）。

### 第3层：强行业数仓位管理
| 强行业数 | 市场状态 | 仓位 |
|---------|---------|------|
| ≥5 | 强势 | 100% 满仓 |
| 3-5 | 震荡偏强 | 50% 半仓 |
| 2-3 | 震荡 | 30% 轻仓 |
| <1.5 | 弱势 | 空仓 |

## 3. 个股选股（分数区间）

### 核心分数区间
| 个股分数 | 操作 | 理由 |
|---------|------|------|
| **0.10-0.12** | **首选（黄金区间）** | 胜率64.8%，均收+1.19%，最大亏损-10.4% |
| 0.12-0.15 | 可选（主板优先） | 胜率79%但样本少，警惕追高 |
| 0.15-0.20 | 谨慎 | 仅强市有效 |
| ≥0.20 | 极谨慎 | 趋势加速，样本少 |
| <0.10 | 不买 | 信号太弱 |

**注意**：0.12-0.14 是"追高陷阱"（动量特征强，易买山顶）；0.10-0.11 假信号区必须配合行业确认。
**重要**：以上绝对阈值基于特定模型分布，换模型后先看 `score_distribution`（第7节）用分位数映射。

```python
def pick_stocks(items, market_sig):
    picks = []
    for it in items:
        s = it.get("fusion_score") or 0
        if not (0.10 <= s <= 0.12): continue
        if market_sig["ind_avg_top1"] < 0.09 and s < 0.11: continue  # 假信号区
        # 主板优先
        board = it.get("board", "")
        if "创业" in board or "科创" in board: continue
        # 排除 ST
        name = it.get("stock_name", "")
        if "ST" in name or "退" in name: continue
        picks.append(it)
    picks.sort(key=lambda x: -(x.get("fusion_score") or 0))
    return picks[:5]  # 每天选3-5只
```

### 3天趋势（决定买点）
用 `prev_score`/`next_score` 判断：
| 趋势 | 模式 | 操作 |
|------|------|------|
| **先升后降** | 低→高→降 | **最佳买点**（胜率78%） |
| 连续上升 | 低→高→更高 | 过热不追 |
| 连续下降 | 高→降→降 | 信号衰退不买 |

**反直觉**：分数下降比上升好——高位回落说明绝对分仍高。

## 4. 负分参考（做空/回避）

### 精确做空分数线
| 条件 | 下跌概率 | 做空均收 |
|------|---------|---------|
| **微盘(<30亿) + 分数≤-0.20** | 72.0% | +5.14% |
| 微盘 + 分数≤-0.15 | 68.6% | +3.17% |
| 小盘 + 分数≤-0.20 | 65.5% | +2.62% |
| 中盘 + 分数≤-0.17 | 60.4% | +1.16% |
| 大盘/超大盘 + 任何负分 | ≤52% | 不做空 |

### 负分也会涨（错杀）
- **超大盘负分 -0.13~-0.14**：下跌概率仅 42%，均收 +3%——错杀最佳例证
- 大盘负分 -0.11：上涨概率 51%
- **科创板负分 -0.06~-0.15**：均收全为正，做空价值最低
- 警戒线 -0.22：即使大盘股低于此分也会崩

### 一句话落地
**做空只做微盘/小盘 + 分数≤-0.15；大盘/超大盘/科创板的负分是错杀，-0.22 以下才真危险；轻负分(>-0.06)无信息。**

```python
def short_candidates(items):
    shorts = []
    for it in items:
        s = it.get("fusion_score") or 0
        if s > -0.15: continue
        tier = it.get("market_cap_tier", "")
        if tier in ("大盘", "超大盘"): continue  # 错杀
        if "科创" in it.get("board", ""): continue  # 抗跌
        shorts.append(it)
    shorts.sort(key=lambda x: x.get("fusion_score") or 0)
    return shorts[:10]
```

## 5. 行业轮动分析

```python
def sector_rotation(batch_runs):
    # 跨多个交易日统计强行业出现频次
    from collections import Counter
    ind_days = Counter()
    for run in batch_runs[:30]:  # 最近30个交易日
        items = fetch_run(run["run_id"]).get("items", [])
        top = sorted(items, key=lambda x: -(x.get("fusion_score") or 0))[:20]
        for it in top:
            if (it.get("fusion_score") or 0) >= 0.10:
                ind_days[it.get("industry", "")] += 1
    return ind_days.most_common(10)
```
**判断**：
- 行业信号持续 ≥3 天 = 真轮动（非一日游）
- 新行业出现 Top1 ≥0.10 = 资金切换方向
- 强行业数 ≥3/天 = 有行情；≤1.5/天 = 空仓

## 6. 完整分析流程

当用户要求"分析批量推理结果"时：
1. **确认批次**：`/models/inference/batches` 找最近 completed 的 batch
2. **拿 member_runs**：`/models/inference/batch/{id}` 得到每日 run_id
3. **拉今日信号**：取最新 run 的 items
4. **市场状态**：行业 avg Top1 + 强行业数 + 大盘均线
5. **选股**：分数 0.10-0.12 + 主板 + 3天趋势"先升后降"
6. **负分参考**：微盘/小盘极端负分列出做空候选
7. **行业轮动**：跨日统计强行业
8. **输出决策**：入场/仓位/个股清单/做空清单/规避

## 7. 不同模型分数范围的处理（关键）

**每个模型训练后分数范围不同**（实测：这个融合模型 fusion_score 范围 -0.996 ~ 0.965，而方法论假设 0-0.2 是另一套模型）。**绝对阈值不能直接套用**，必须先了解当前模型的分数分布。

```python
def score_distribution(items):
    scores = sorted(x.get("fusion_score") or 0 for x in items)
    n = len(scores)
    return {
        "min": round(scores[0], 3),
        "max": round(scores[-1], 3),
        "p10": round(scores[int(n*0.1)], 3),
        "p25": round(scores[int(n*0.25)], 3),
        "p50": round(scores[int(n*0.5)], 3),
        "p75": round(scores[int(n*0.75)], 3),
        "p90": round(scores[int(n*0.9)], 3),
    }
```

### 模型分数范围 → 方法论阈值映射

方法论阈值（0.10-0.12 黄金区间等）基于特定模型的分布。使用时：
1. **先跑 `score_distribution`** 了解当前模型分布
2. **用分位数替代绝对分数**判断高低：
   - 黄金区间 ≈ 高分位带（如 p75-p90，需结合回测验证）
   - 追高区 ≈ 最高分位（p90+）
   - 负分做空 ≈ 最低分位（p10 以下）
3. **跨模型对比时**，统一用百分位排名，不用原始分数

### 模型选择（多模型择优）

不同模型分数分布和预测能力不同，分析时可对比：
```bash
# 各模型推理历史分数（单股）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/{symbol}/history"
# 模型列表（含元数据）
curl -s -H "$AUTH" "$BASE/api/v1/models"
```

**择优标准**：
- 分数分布合理（不过度集中、有区分度）
- 该模型回测胜率/IC 高（见 [[backtest-center]]）
- 黄金区间候选数量适中（太多=无区分度，太少=过严）

### 融合模型特殊处理
融合模型已按百分位加权合成，`fusion_score` 可直接用，但分数范围仍因源模型而异——同样先看分布再定阈值。

## 8. 模型分数校准回测（核心新增）

**每个模型训练后分数范围不同**，用历史信号自动回测找出该模型最适合的分数区间。

### 8.1 调用校准接口（异步任务 + 进度）

校准是**后台任务**，提交立即返回 `task_id`，轮询查进度（避免阻塞引擎）：

```bash
# 1. 提交校准任务（秒回 task_id）
curl -s -X POST -H "$AUTH" "$BASE/api/v1/selection/score-calibration?days=180&horizons=1,3,5,10&top_n=50"
# 返回: {"status":"submitted","task_id":"calib_xxx", ...}

# 2. 轮询任务进度（progress: 5→100，message 显示阶段）
curl -s -H "$AUTH" "$BASE/api/v1/selection/score-calibration/{task_id}"
# 返回: {"status":"running/completed","progress":85,"message":"回测中... 45/60 天","result":{...}}
```

**参数**：
- `days`：回测历史交易日数（30~478）
- `horizons`：未来 N 日列表（1,3,5,10 / 1,5,20 等）
- `top_n`：重点标注排名前 N

**流程**：POST 提交 → 每 2 秒轮询 GET → 状态 completed 后取 result。后台任务阶段：读取信号(5%)→分组(15%)→价格面板(30%)→市值(45%)→回测(55%+逐日推进)→汇总(95%)→完成(100%)

### 8.2 输出解读

**score_summary（分数档 × 多周期）**：
| 字段 | 含义 |
|---|---|
| `score_band` | 分数档（≤-0.25 ~ ≥0.20，正负对称） |
| `n` | 该档样本数 |
| `top50_count` | 该档内排名前50样本数 |
| `avg_rank` | 该档股票平均绝对排名 |
| `horizons` | 每个 T+N 的：胜率/下跌概率/均收/中位收益 |

**matrix（分数档 × 市值 × 下跌概率）**：主周期的市值分档下跌概率

**neg_industry_avg（负分行业 avg）**：负分最深行业排序
**neg_board_avg（板块负分 avg）**：主板/创业板/科创板/北交所

**recommended_band**：系统推荐的最优分数档

### 8.3 关键洞察（当前模型实测）

用 `days=60&horizons=1,3,5,10` 实测（94万样本）：
- **正分最优档：0.05~0.08** → T+5 胜率 53%、下跌 46.1%、均收 +0.753%
- **方法论黄金区 0.10~0.12 在此模型反而均收 -0.387%**（负！）→ 必须按模型校准
- **极端负分 ≤-0.25** → T+5 下跌 62.1%、T+10 下跌 67.5%、均收 -4.971%（做空信号）
- **负分行业 avg**：林业/油服/种植业/农产品加工负分最深（做空首选）
- **板块负分**：创业板 -0.50 最深、科创板 -0.38 抗跌

### 8.4 校准方法
1. **训练完新模型** → 跑 `score-calibration`
2. **看 `recommended_band`** → 该模型的最佳分数区间
3. **对比不同模型** → 用各自 calibrated 区间，不用统一阈值
4. **动态更新选股阈值** → 用该校准结果替换方法论默认值

## 9. 完整分析流程（含模型适配）

当用户要求"分析批量推理结果"时：
1. **确认批次**：`/models/inference/batches` 找最近 completed
2. **拿每日 run**：`/models/inference/batch/{id}` 的 member_runs
3. **拉最新信号**：`/models/inference/runs/{run_id}` 的 items
4. **分数校准**：`/selection/score-calibration` 看该模型最优分数档
5. **市场状态**：行业 avg Top1（用校准档映射阈值）+ 强行业数 + 大盘均线
6. **选股**：按校准档定位黄金区间 + 主板 + 3天趋势
7. **负分参考**：校准出的极端负分档 + 微盘/小盘
8. **行业轮动**：跨日统计强行业
9. **输出决策**：入场/仓位/个股/做空/规避

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| 信号分数普遍偏低/偏高 | 不同模型分布不同，先 score-calibration 再定阈值 |
| 黄金区间选不出 | 用校准档，或放宽到 p70-p90 |
| 行业为空 | 部分 run 行业缺失，按 board 分组替代 |
| 跨模型对比 | 统一用百分位排名，不用原始分数 |
| 想选最优模型 | 对比各模型校准结果 + 回测指标 |
| 校准接口慢 | 减少 days 或 horizons，或缓存结果 |
