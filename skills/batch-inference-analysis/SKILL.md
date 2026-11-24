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

## 推理侧 Target Contract（2026-11-24 起）

- 模型目录 `metadata.json` 新增 `target_id/target_version/target_hash/target_base_hash/target_overrides/target_spec/label_contract_version/label_stats/contract_flags`；推理/回填只读模型目录，不重算训练目标。
- 分类模型推理输出正类概率（LightGBM/XGBoost 的 binary objective `predict`、CatBoost `predict_proba[:,1]`、sklearn `predict_proba[:,1]`）；DL 分类训练已被禁止，不要按分类模型消费 `.pth`。
- 成绩单、横评、组合回测按 `target_hash` 分组：**不同 target_hash 的模型不可合并比较**（持有期/标签口径可能不同）；没有该字段的老模型按 `target_id=legacy` + 合成 hash 处理。
- `target_mode` / `target_horizon_days` 仍保留在 metadata 供旧推理模板读取；新代码优先用 `target_id/target_hash/target_spec` 追溯口径。
- 若推理结果异常，先核对 `metadata.label_contract_version` 与 `contract_flags`（`capability_gate` / `diagnostic_gate`），再查 `label_stats` 的 NaN/正类率/每日截面。

说明：`docs/训练目标契约_使用说明_20261124.md`。

## 数据基础

批量推理产出：
- **batch**：多个交易日的推理集合（`/models/inference/batch/{batch_id}`）
  - `member_runs`：每个交易日的 run 记录（run_id/trade_date/signals_count）
- **单日 run**：`/models/inference/runs/{run_id}`
  - `items`：5377+ 只股票信号，每只含 `fusion_score`（分数）/`board`（板块）/`industry`（行业）/`market_cap_tier`（市值分档）/`trend`（趋势）/`prev_score`/`prev2_score`/`next_score`

> **缺失值口径（勿跳）**：`fusion_score` 为 `null`/缺失 **≠ 0**（0 是模型的「中性」分；缺失=该模型没给这只票打分）。缺失标的绝不按 0 参与排序/统计——下文所有代码一律**先显式过滤缺失再分析**，并如实报告被过滤的数量。

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

def market_signal(items_raw, top_n=20):
    # 缺失分 ≠ 0：先显式过滤（缺失=未打分，不参与排序）
    items = [it for it in items_raw if it.get("fusion_score") is not None]
    # 取 Top20 按分数
    top = sorted(items, key=lambda x: -x["fusion_score"])[:top_n]
    # 按行业分组，取每行业最高分
    ind_top = {}
    for it in top:
        ind = it.get("industry", "") or "未知"
        s = it["fusion_score"]
        if ind not in ind_top or s > ind_top[ind]:
            ind_top[ind] = s
    ind_avg_top1 = sum(ind_top.values()) / max(1, len(ind_top))
    strong = sum(1 for s in ind_top.values() if s >= 0.10)
    return {"ind_avg_top1": ind_avg_top1, "strong_industries": strong,
            "scored": len(items), "missing": len(items_raw) - len(items),
            "top_industries": dict(sorted(ind_top.items(), key=lambda x:-x[1])[:5])}
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

### 核心分数区间（历史观察——实盘阈值一律以第 8 节校准为准）
| 个股分数 | 操作 | 理由 |
|---------|------|------|
| **0.10-0.12** | **历史观察的「黄金区间」（旧模型）** | 胜率64.8%，均收+1.19%，最大亏损-10.4%（另一套模型的样本内回测） |
| 0.12-0.15 | 可选（主板优先） | 胜率79%但样本少，警惕追高 |
| 0.15-0.20 | 谨慎 | 仅强市有效 |
| ≥0.20 | 极谨慎 | 趋势加速，样本少 |
| <0.10 | 不买 | 信号太弱 |

**注意**：0.12-0.14 是"追高陷阱"（动量特征强，易买山顶）；0.10-0.11 假信号区必须配合行业确认。
**重要（与 §8 收敛，勿矛盾使用）**：下表阈值来自**旧模型**的样本内回测；对当前模型用同一口径实测，0.10~0.12 档均收为 **-0.387%**（负，见 §8.3）——**两处数字必须以同模型校准结果为准**：先跑 `score-calibration`，用 `recommended_band` 替换这里的绝对阈值（当前模型实测最优档为 0.05~0.08）；分位数映射见第 7 节。换模型不校准直接用下表，是已知会出错的做法。

```python
def pick_stocks(items, market_sig, lo=0.10, hi=0.12):
    # lo/hi 用 score-calibration 的 recommended_band 替换（§8），此处默认值仅示例
    picks = []
    for it in items:
        s = it.get("fusion_score")
        if s is None:  # 缺失≠0：未打分的标的不参与
            continue
        if not (lo <= s <= hi): continue
        if market_sig["ind_avg_top1"] < 0.09 and s < 0.11: continue  # 假信号区
        # 主板优先
        board = it.get("board", "")
        if "创业" in board or "科创" in board: continue
        # 排除 ST
        name = it.get("stock_name", "")
        if "ST" in name or "退" in name: continue
        picks.append(it)
    picks.sort(key=lambda x: -x["fusion_score"])
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
        s = it.get("fusion_score")
        if s is None:  # 缺失≠0：未打分的标的不参与
            continue
        if s > -0.15: continue
        tier = it.get("market_cap_tier", "")
        if tier in ("大盘", "超大盘"): continue  # 错杀
        if "科创" in it.get("board", ""): continue  # 抗跌
        shorts.append(it)
    shorts.sort(key=lambda x: x["fusion_score"])
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
        scored = [it for it in items if it.get("fusion_score") is not None]  # 缺失≠0
        top = sorted(scored, key=lambda x: -x["fusion_score"])[:20]
        for it in top:
            if it["fusion_score"] >= 0.10:
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
5. **选股**：按校准档（§8 `recommended_band`）定位黄金区间 + 主板 + 3天趋势"先升后降"（模板代码里的 0.10-0.12 是旧模型的历史观察，用校准结果替换）
6. **负分参考**：微盘/小盘极端负分列出做空候选
7. **行业轮动**：跨日统计强行业
8. **输出决策**：入场/仓位/个股清单/做空清单/规避

## 7. 不同模型分数范围的处理（关键）

**每个模型训练后分数范围不同**（实测：这个融合模型 fusion_score 范围 -0.996 ~ 0.965，而方法论假设 0-0.2 是另一套模型）。**绝对阈值不能直接套用**，必须先了解当前模型的分数分布。

```python
def score_distribution(items_raw):
    # 缺失≠0：缺失标的不进分布；全缺失时返回 None（不得凭空给分布）
    scores = sorted(
        it["fusion_score"] for it in items_raw if it.get("fusion_score") is not None
    )
    if not scores:
        return None
    n = len(scores)
    return {
        "scored": n,
        "missing": len(items_raw) - n,
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

## 11. Top20 离线评估（选股准确性 / 容量 / 可投资性）

用途：批量推理补齐后，评估「每个模型每日 Top20」在样本外（`split='test'`）的
真实选股能力，并拆解 ST、板块、市值、流动性。与 §1-§10 的「单日信号解读」不同，
这一节是**跨 19 个月、逐日 Top20 组合**的回测。

脚本（本技能 `scripts/`）：

| 脚本 | 作用 |
|------|------|
| `top20_model_eval.py` | 主评估：IC / Top20 超额 / 胜率 / 分位 / 组合年化回撤 / 容量；含等权融合与共识投票 |
| `model_rank_corr.py` | 同日预测秩相关（Spearman），判断模型冗余、选融合成员 |
| `top20_composition.py` | Top20 的 ST / 板块 / 市值 / ADV 拆解，及「非ST」「ADV≥0.5亿」变体 |

运行（容器内，需 pandas/duckdb）：

```bash
docker cp skills/batch-inference-analysis/scripts/top20_model_eval.py quantmind:/tmp/
docker cp skills/batch-inference-analysis/scripts/model_rank_corr.py quantmind:/tmp/
docker cp skills/batch-inference-analysis/scripts/top20_composition.py quantmind:/tmp/

docker exec -w /app quantmind python /tmp/top20_model_eval.py \
  --output-dir /tmp/top20_eval_20261020
docker exec -w /app quantmind python /tmp/top20_model_eval.py \
  --output-dir /tmp/top20_eval_adv05 --min-adv-yi 0.5
docker exec -w /tmp quantmind python /tmp/top20_composition.py \
  --output-dir /tmp/top20_eval_20261020/composition
```

> `top20_composition.py` 会 `import top20_model_eval`，必须 `-w /tmp`（两个脚本
> 同目录）。`--model-map` 是 `{model_id, model_type, horizon}` 的 JSON 列表；
> 脚本自己拼 `pred_path = PRED_ROOT/<model_id>/pred.parquet`。

口径（踩过的坑）：

- `pred.parquet` 只取 `split='test'`；`trade_date` 是**数据日 T**，收益
  `close(T+1+h)/close(T+1)-1`（前复权 `daily_forward`）。
- 买入日用 `trade_mask.blocked_long` 剔除 T+1 涨停/停牌；`blocked_long` 不含 ST。
- 组合每 h 个交易日等权换仓，往返成本 0.2%；换手 ~80-90%/期。
- `daily_forward.amount` 单位是**万元**，转亿元 ×1e-4（脚本常量 `AMOUNT_TO_YI`）。
- **`precision20`（与事后 Top20 重合率）不是模型好坏指标**：事后 Top20 被
  涨停/北交所彩票票主导，模型命中率只有 0.1-0.6%、lift 0.3-1.6x，但 Top20
  组合照样有 0.4-2.2%/期的超额。评价看 IC、Top20 超额、日胜率、年化、回撤。
- **共识行（`ensemble_summary.csv` 的 `scope=consensus`）的 IC / `pct_med` /
  `top_decile_rate` 是在共识子集内部算的**，不可与全市场口径比较；只有
  `top_ret` / `top_excess` / `win_day` / `port_*` / `avg_selected` 有意义。
- ST 名单来自 `instrument_detail` 的**当前快照**（静态），历史回测有前视；
  实盘规则「不买 ST」本身是免费改进（见 composition 的「非ST」行）。
- 1% ADV 容量 = 20 只 × 1% × ADV 中位。树模型 Top20 通常只有 700-900 万，
  TFT 1800-3300 万，T+10 TCN 约 4800 万——容量是这类微盘模型最硬的约束。

产出（`--output-dir`）：`REPORT.md`、`summary_common.csv`、`summary_yearly.csv`、
`monthly_ic.csv`、`ensemble_summary.csv`、`top20_latest_T*.csv`、
`composition/composition_T*.csv`、`model_rank_corr_T*.csv`。

本次 13 模型（T+5/T+10）与 5 模型（T+20）的结论与选股设计见
`data/reports/top20_eval_20261020/`。

## 12. 共识阈值 / 周期 / 大盘过滤（2026-10-20 新增）

用途：回答「按分数 > X 数票能不能共识」「T+5/T+10/T+20 哪个好」「加大盘过滤会不会更好」。
脚本：`scripts/consensus_threshold_probe.py`（复用 `top20_model_eval.py` 的取数口径）。

```bash
docker cp skills/batch-inference-analysis/scripts/consensus_threshold_probe.py quantmind:/tmp/
docker exec -w /tmp quantmind python /tmp/consensus_threshold_probe.py \
  --model-map /tmp/model_map_31.json --horizon 10 \
  --start 2025-02-01 --end 2026-08-31 --min-adv-yi 0.5 \
  --output-dir /tmp/consensus_threshold_20261020 --memory 4GB --threads 2
```

结论（T+10，2025-02~2026-08，测试集）：

- **统一绝对分数阈值不可用**：同一天 `pred>0.10`，RF 0 只、三棵树各 1 只，
  LSTM 449、MLP 377。分数尺度不可比，固定阈值=让深模型垄断票数。
- 可比的票 = 每个模型自己的当日 **Top20**（约 99.6 分位）。13 模型 Top20
  投票 ≥3 票：16.7 只，每 10 日超额 0.798%，年化 41.3%，回撤 −17.5%。
- 放宽到 P99：超额 0.814% 接近，但年化 30.9%、回撤 −22.0%；P95 更差
  （0.621% / 27.1%）。**票的质量比票的数量重要**：Top20 的 3 票平均
  1.64%/10d，P95 的 10 票反而更低。
- T+10 优于 T+5（13 个模型里 12 个 IC 更高；T+5 2026 年化仅 0.4%）。
  T+20 只有 5 个模型，Top20 ≥3 票只剩 8.3 只，不能做 13 模型共识；
  单 CAT T+20 年化 49.7%、回撤 −13.0%，适合大资金单模型核心。
- 大盘过滤（MA10/20/60、breadth20/60）全部把年化砍半（40.4%→14–25%），
  只有 breadth20 风险调整后更好（21.3%/−3.9%），但 51% 时间空仓、样本里
  只有一段熊市。**不作为核心规则。**
- 过拟合警告：T+10 只有 39 个非重叠 10 日周期、2026 只有 16 个；本轮试了
  51 个策略-周期配置 + 72 个过滤组合。P95 在 2025 年化 51.1%、2026 −0.4%，
  就是阈值过拟合的样本。用 2025 设计、2026 验证、纸面跟踪 2–3 个周期再上真钱。

产出：`threshold_grid.csv`（全变体）、`threshold_yearly.csv`（2025/2026）、
`market_filter.csv`、`picks_*.csv`；本次结果已复制到
`data/reports/top20_eval_20261020/threshold_20261020/`。

## 13. 单票止损 / 移动止盈（2026-10-20 新增）

用途：回答「-7% 止损 + 移动止盈能不能控回撤」「2025 vs 2026 是否都有效」。
脚本：`scripts/stop_loss_probe.py`（读取第 12 节的 `picks_*.csv`，复用
`top20_model_eval.py` 的 DuckDB / 成本口径）。

```bash
docker cp skills/batch-inference-analysis/scripts/stop_loss_probe.py quantmind:/tmp/
docker exec quantmind python /tmp/stop_loss_probe.py \
  --picks /tmp/consensus_threshold_20261020/picks_Top20投票_k=3.csv \
  --strategy "Top20投票 k=3" --horizon 10 \
  --out-dir /tmp/stop_trail_consensus
```

实现要点（改脚本前必读）：

- 入场 = `buy_d` 收盘，持有 `horizon` 日；基线校验必须打印
  `baseline fwd max abs diff`，正常应 <1e-15。
- 停牌日（close NaN）用上一有效收盘前填，标记 `tradable=False`，当日不触发止损；
  只有入场/到期价缺失才丢弃该笔。
- 止损：`entry*(1-sl)`，日线 `low` 触发，跳空按 `open` 成交；
  收盘模式只看 `close`，按当日收盘退出。
- 移动止盈：浮盈 ≥ 激活线后启动，`high_water*(1-trail)` 触发；
  high-water 当日只更新，次日才参与触发。
- 提前卖出的现金留到下一次换仓，年化按等权组合计算（保守）；
  年度指标在年内重新采样并重置换手，与全期不可相加。

结论（Top20 投票 k=3，T+10，2025-02~2026-08）：

- **-7% 固定止损不可用**：全期 41.3%→24.6%、回撤 -17.5%→-24.5%；
  2025 74.2%→65.1%，2026 18.4%→**-1.9%**。叠加 +10%/5% 移动止盈后
  2026 仍为 -4.2%。CAT / P95 两套口径同向。
- 原因：22.0% 的持仓曾跌破 -7%，这批票持有到期平均 **-4.7%**，
  止损卖在 -7.0%，单笔负期望 -2.3pp；-5%/-10%/-15% 各档同理
  （`stop_minus_hold` 全为正）。2026 触发率 34.1% vs 2025 14.1%，
  频率翻倍把 2026 拖到负收益。
- 移动止盈单独用接近中性：+10%/5% 盘中 39.1%/-17.0%，收盘触发
  41.8%/-18.3%（基线 41.3%/-17.5%）。+15%/3% 收盘 41.7%/-18.3%，
  2025/2026 都不恶化，可作为轻量锁定盈利单的叠加，但不要期待增收益。
- 回撤控制应放组合层（仓位、市场过滤、集中度），不要逐票固定百分比砍仓。

产出：`stop_trail_grid.csv`、`stop_trail_yearly.csv`、
`stop_trail_breach_counterfactual.csv`（跌破阈值的事后反事实）、
`stop_trail_exit_diagnostics.csv`、`stop_trail_mfe_mae.csv`、逐笔 picks、
`REPORT.md`；本次结果在
`data/reports/top20_eval_20261020/stop_trail_20261020/`。

## 14. 周度买卖：周一买 / 周五清仓（2026-10-20 新增）

用途：回答「周一买、周五全平、下周一再买」这套周度节奏在 2025–2026 的表现，
以及「能上实盘的是 T+5 还是 T+10」。**注意这与第 12 节的 10 日持有结论不同**：
10 日持有下 T+10 共识最优（41.3%）；周度 4 日持有下共识要选 T+5，单模型选
T+10 CAT。

脚本：

- `scripts/weekly_schedule_probe.py`：读第 12 节的 `picks_*.csv`，跑三个变体
  「周一买/周五卖」「周一买/持 5 日」「双周买/持 10 日」，输出全期/年度。
- `scripts/per_model_weekly.py`：读 `--model-map`（`{model_id, model_type,
  horizon}` JSON），对 13 个 T+5 + 13 个 T+10 逐模型跑「周一买/周五卖」，
  每个模型自己在 ADV≥0.5 亿池内取 Top20。

```bash
docker cp skills/batch-inference-analysis/scripts/weekly_schedule_probe.py quantmind:/tmp/
docker cp skills/batch-inference-analysis/scripts/per_model_weekly.py quantmind:/tmp/
docker exec -w /tmp quantmind python /tmp/weekly_schedule_probe.py \
  --picks-t5 /tmp/consensus_threshold_T5_20261020/picks_Top20投票_k=3.csv \
  --picks-t10 /tmp/consensus_threshold_20261020/picks_Top20投票_k=3.csv \
  --picks-t5-cat /tmp/consensus_threshold_T5_20261020/picks_单CAT_ADV_Top20.csv \
  --picks-t10-cat /tmp/consensus_threshold_20261020/picks_单CAT_ADV_Top20.csv \
  --out-dir /tmp/weekly_schedule
docker exec -w /tmp quantmind python /tmp/per_model_weekly.py \
  --model-map /tmp/model_map_31.json \
  --start 2025-02-01 --end 2026-08-31 \
  --out-dir /tmp/per_model_weekly
```

口径 / 实现要点：

- `week_windows(dates)` 按 ISO 年/周切分交易日，返回
  `(signal_d=上一周最后一日, buy_d=本周第一日, sell_d=本周最后一日)`；
  `signal_d` 通常是周五，`buy_d` 是周一，`sell_d` 是周五。
- `build_rows(..., hold=0)`：`hold=0` 在 `sell_d` 收盘卖；`hold=5/10` 从
  `buy_d` 起算第 5/10 个交易日卖。双周变体用 `windows[::2]`，保证资金不重叠。
- `window_return`：区间内第一个有效收盘买入、最后一个有效收盘卖出；
  少于 2 个有效点返回 NaN（停牌/退市）。
- 等权、逐周转仓，成本 `0.2% × turnover`；`summarize` 的年化按每周期
  5/252 年；`yearly_rows` 在年内重新采样并重置换手，**与全期不可相加**。
- `per_model_weekly.py` 的 ADV 用 `amount` 20 日均值（`amount` 单位万元，
  阈值 `5000` = 0.5 亿），在 ADV-ok 池内按 `pred` 排序取 Top20，再 join
  `next_map` / `blocked_long` / `instrument_detail`；必须 `-w /tmp`（与
  `top20_model_eval.py`、`weekly_schedule_probe.py` 同目录）。
- 主结果**不剔除 ST**；`--drop-st` 用 `instrument_detail.IsSTGP` 当前快照，
  历史回测属前视，**不要把 ex-ST 数字当历史证据**。

结论（2025-02~2026-08，80–81 周）：

- **单模型：T+10 CAT。** 周一收盘买 2025 21.2% / 2026 46.2%（全期 30.9%，
  回撤 -16.8%）；周一开盘买 37.7% / 44.9%（全期 40.6%，回撤 -15.1%）；
  约 19.8 只，ADV≥0.5 亿，容量约 1370 万。两年、两种买入价都为正。
- **共识：T+5 共识，不是 T+10 共识。** 收盘买 19.2% / 18.0%（全期 18.7%）；
  开盘买 40.4% / 28.4%（全期 35.3%）。T+10 共识只有 9.8% / 10.2%（收盘）、
  34.2% / 22.7%（开盘）。但 T+5 共识 ADV 容量只有 700–900 万。
- **T+5 CAT 不能上实盘**：2026 收盘 -4.3%、开盘 -0.9%，回撤 -22.7%~-24.0%。
- **逐模型 T+10 只赢 6/13（2025 4/13、2026 6/13）**：树模型 CAT/XGB 受益于
  T+10；NativeTFT/MLP/ALSTM/Transformer 在 T+5 更好；LSTM 两个周期都差。
  没有全局「T+10 一定好」。
- **4 日持有只捕获 T+10 的 45–59%**：T+5 的 4 日约等于其 5 日的 81%；
  T+10 的 4 日只有其 10 日的 45–59%。T+10 CAT 赢是因为 2026 picks 更强
  （4 日 0.53% vs T+5 CAT 0.10%），不是周期匹配。
- **开盘买的大部分是市场 beta**：全市场等权周一开盘→收盘 2025 约 +0.54%/周、
  2026 约 +0.19%/周，与 picks 的周一盘中同量级；小盘股开盘集合竞价容量有限，
  保守预算用收盘买。
- **周五信号本身翻转**：2025 周五信号是四策略里最差的星期几，2026 是最好的；
  周度年度强弱带有星期几择时，2026 的 46% 不要外推。
- **成本/集中度**：周换手 ~0.78，0.2% 往返每年拖 ~9pp；0.3% 时 T+10 CAT
  25.9%、T+5 共识 14.0%。T+10 CAT 2026 最好 5 周 +47.5%，去掉后 28 周合计
  -18.7%；2025 更均衡（最好 5 周 +16.9%，去掉后 +1.8%）。

产出：`weekly_summary.csv`、`weekly_yearly.csv`、`open_entry_summary.csv`、
`per_model_weekly_summary.csv`、`per_model_weekly_yearly.csv`、
`cost_sensitivity.csv`、`term_structure.csv`、`weekday_signal.csv`、
`monday_intraday.csv`；本次报告与 CSV 在
`data/reports/top20_eval_20261020/周度买卖_20261020.md` 与
`data/reports/top20_eval_20261020/weekly_20261020/`。

## 15. 滚动持仓模拟：TopN × T+5/T+10 × 分数均值 + 每日替补（2026-10-20 新增）

用途：回答「如果上实盘，2025–2026 怎么跑、Top20 还是 Top50、T+5 还是 T+10、
1000 万怎么分、分数弱了怎么用替补滚动」。这是第 11–14 节的组合层收口：
第 11 节看单模型 Top20 准确性，第 12 节看共识阈值，第 13 节看止损，第 14 节看
周度节奏；本节把「每日可换 + 分数均值过滤 + 分数退出 + 等权 50 槽」放在一起跑。

脚本：`scripts/rolling_portfolio_sim.py`（依赖同目录 `top20_model_eval.py`）。

### 15.1 预注册口径

- **综合分 S**：13 个同周期模型每日在 **ADV≥2000 万** 池内的截面百分位均值；
  ≥8 个模型有分才有效（回测里恒 13，门槛从未触发，实盘保留作推理失败保护）。
- **A**：该股过去 20 个交易日 S 的均值，**不含当日**，至少 10 个观测。
- **入场**：`S_t >= A_t`；排序 `S_t` 降序；TopN（20 / 50）。
- **执行**：t 收盘决策，**t+1 收盘**成交；买入日 `blocked_long` 跳过，
  卖出日 `blocked_short` 顺延；卖出与买入同一收盘时点。
- **持有**：H 个交易日（T+5 用 T+5 综合分，T+10 用 T+10 综合分）。
- **提前退出**：`S_t < A_t - score_buffer`（默认 0.03）；可选收盘价止损。
- **替补**：退出后的空位用当日符合条件的下一名补上，继续滚动，不空等。
- **资金**：1000 万，N 个等权槽位；单票目标 `NAV*(1-2%)/N`；1% ADV 上限；
  现金不足时部分成交。
- **成本**：单边 0.1%（压力 0.15%）。
- **建仓**：前 H 个交易日每天 `ceil(N/H)` 只，避免一次性全仓。

### 15.2 运行

```bash
docker cp skills/batch-inference-analysis/scripts/rolling_portfolio_sim.py quantmind:/tmp/
docker cp skills/batch-inference-analysis/scripts/top20_model_eval.py quantmind:/tmp/

# 主网格 + 2025 Calmar 选优 + 扩展 + 容量
docker exec -w /tmp quantmind python rolling_portfolio_sim.py \
  --mode all --memory 4GB --threads 3 --out-dir /tmp/rolling_sim_full

# 补充网格：min_hold / 无均值过滤 / Top20 止损 / Top50 最短持有
docker exec -w /tmp quantmind python rolling_portfolio_sim.py \
  --mode supp --memory 4GB --threads 3 --out-dir /tmp/rolling_supp2
```

- `pilot` 小样本冒烟；`main` 12 主配置；`supp` 补充网格；`all` = 主网格 →
  **只用 2025 Calmar** 选优 → 扩展 + 容量。
- 内存 4GB / 线程 3 是为了与回填等任务共存；单独跑可用 8GB / 4 线程。
- 必须 `-w /tmp`，且 `top20_model_eval.py` 与脚本同目录。

### 15.3 主结果（2025-02-19 ~ 2026-08-26，371 天，净 0.1% 单边）

| 配置 | 2025 年化 | 2026 年化 | 全期年化 | 全期回撤 | Sharpe | Calmar |
|---|---:|---:|---:|---:|---:|---:|
| Top20 H5 分数 | 35.4% | 5.6% | 21.9% | -26.8% | 1.01 | 0.82 |
| Top20 H10 分数 | 40.6% | 7.3% | 25.4% | -24.8% | 1.11 | 1.02 |
| Top20 H10 分数+止损8 | 40.7% | 11.6% | 27.6% | -23.2% | 1.16 | 1.19 |
| Top50 H5 分数 | 48.6% | 10.6% | 31.2% | -24.0% | 1.31 | 1.30 |
| **Top50 H10 分数** | **58.7%** | **18.4%** | **40.2%** | **-23.8%** | **1.56** | **1.69** |
| Top50 H10 时间 | 47.5% | 18.2% | 34.3% | -23.1% | 1.41 | 1.48 |

- 推荐：**Top50 / T+10 / S≥20 日均值 / 分数退出 buffer 0.03 / 每日替补 /
  1000 万 50 等权（每只约 19.6 万）/ 2% 现金 / 1% ADV**。
- 基准（同池等权）：2025 37.45%、2026 -2.94%、全期 18.63%；策略超额
  +21.28 / +21.33 / +21.58pp；alpha 年化 +21.3 / +20.8 / +18.8%
  （Newey-West t 1.88 / 1.03 / 1.71）。
- **Top50 > Top20**（全期 6/6 胜、2026 5/6；2026 18.4% vs 7.3%）；**T+10 > T+5**
  （全期 5/6 胜；换手 33.4x vs 56.9x，成本 6.6% vs 11.3%/年）。
- **口径更正（2026-10-20）**：分数均值过滤单独只 +0.1~0.2pp（Top50 全期
  34.2%→34.3%；2025 0、2026 +0.3）；**+5.9pp 来自分数退出**（2025 +11.3、
  2026 +0.2），不要再把 +6.0pp 记在入场过滤上。两者主要是 2025 的增益，2026 不伤害。
- **大盘过滤明确有害**：2026 只有 1.8%；**extend/min_hold 不稳健**；
  **价格止损非单调**（Top50 -6% 最优 41.2%，-8% 反而 39.1%；Top20 则 -8% 最优）。
- 容量：50 万/500 万 41.6%、1000 万 40.2%、**2000 万 30.3%**、5000 万 30.4%、
  1 亿 21.4%；**1000 万是 50 只等权的甜点，2000 万是悬崖**。
- 诚实边界：2026 是半 OOS（选择规则只用 2025）；DSR 0.90；bootstrap 年化
  95% 区间 [-7.7%, +109.5%]；2026 alpha t 1.0–1.3；2026 回撤 -23.8%、下行捕获
  0.97；先模拟盘/小资金 1–3 个月，不要直接上 1000 万。

### 15.3.1 前向扩展（2026-08-27 ~ 2026-10-19，32 个交易日）

回填 30/30 完成后，同一脚本只改 `--end`，参数全部不动：

```bash
docker exec -w /tmp quantmind python rolling_portfolio_sim.py \
  --mode all --start 2025-02-05 --end 2026-10-19 \
  --design-end 2025-12-31 --memory 4GB --threads 3 \
  --out-dir /tmp/rolling_sim_oct
```

- Top50 H10 分数：前向段 **+3.08%**（基准 **-6.15%**，超额 +9.23pp）；
  2026 到 10/19 年化 **19.96%**、全期 **39.20%**、DD 仍 **-23.83%**；2025 不变。
- 扩展后 DSR **0.887**，2026 alpha t_NW **1.69**、全期 **2.20**；
  2025→2026 排序相关 **0.71**。方向（Top50 > Top20、T+10 > T+5、分数 > 时间）
  没变；**32 天只做前向观察，不据此调 N/H/buffer/stop**。
- 新开仓 241 笔平均仅 +0.20%，说明这段补位质量一般；净值上涨主要来自存量持仓。

### 15.4 产出

`rolling_summary.csv`、`rolling_yearly.csv`、`rolling_robustness.csv`、
`rolling_rank_stability.csv`、`model_capability.csv`、`nav_*.csv`、
`trades_*.csv`、`supp_rolling_summary.csv`、`supp_rolling_yearly.csv`。
本次报告：`data/reports/top20_eval_20261020/滚动实盘_20261020.md`；
CSV：`data/reports/top20_eval_20261020/rolling_20261020/`；
前向扩展 CSV：`data/reports/top20_eval_20261020/rolling_oct_20261020/`（67 个文件，
含 summary/yearly/robustness/nav/trades/容量）。

### 15.5 测试

```bash
docker cp skills/batch-inference-analysis/scripts/. quantmind:/tmp/probe_tests/
docker exec -w /tmp/probe_tests quantmind \
  python -m pytest test_rolling_portfolio_sim.py test_top100_score_probe.py -q
# 14 passed
```

- 宿主没有 scipy，测试必须在容器里跑（且要拷全 scripts 目录，探针之间有 import）。
- 测试锁住：时间退出的成交日与净收益、`blocked_long` 顺延、分数退出、
  排名退出、两日入场/退出确认、开盘成交价、行业上限跳过与缺参报错、
  建仓节奏（`ramp_days=1` 首日买满 / 0 报错）。
- 合成数据不碰数据库；改动 `simulate` 的时点/成本/组合约束逻辑前后都要跑。

### 15.6 Top100 打分探针：平均分、9–10 月实测、top10 模型集（2026-10-20 新增）

用途：回答「模型选出来的股票怎么打分、怎么取前 100、前 100 平均分怎么算；
9–10 月按这个打分法交易效果如何；换成 IC/超额前 10 个模型会不会更好」。

脚本：`scripts/top100_score_probe.py`（复用 `rolling_portfolio_sim.py` 的
`simulate` / `load_panels` / `market_daily`，只加 TopN 平均分与模型集切换）。

```bash
docker cp skills/batch-inference-analysis/scripts/top100_score_probe.py quantmind:/tmp/
docker cp data/reports/top20_eval_20261020/rolling_20261020/model_capability.csv \
  quantmind:/tmp/capability_prereg.csv

# 9–10 月前向（冷启动；scores 从 6/1 起算 20 日均值预热）
docker exec -w /tmp quantmind python top100_score_probe.py \
  --model-map /tmp/model_map_31.json --capability-csv /tmp/capability_prereg.csv \
  --start 2026-09-01 --end 2026-10-19 --score-start 2026-06-01 \
  --out-dir /tmp/top100_probe --memory 4GB --threads 3

# 全期模型集对照（IC10/tail10 的选模型也用这段数据，属样本内选择）
docker exec -w /tmp quantmind python top100_score_probe.py \
  --model-map /tmp/model_map_31.json --capability-csv /tmp/capability_prereg.csv \
  --start 2025-02-20 --end 2026-08-26 --score-start 2025-02-05 \
  --out-dir /tmp/top100_full --memory 5GB --threads 3
```

**打分口径**：单模型当日 ADV≥2000 万池内 `percent_rank(pred)` → 13 个百分位
等权平均（≥8 个模型）→ S∈[0,1]，×100 即“分数”。Top100 = 每日 S 前 100；
前 100 平均分 = 这 100 个 S 的均值。平均分是**相对排名共识**，不是胜率/预期收益。

**9–10 月结果（29 天，1000 万，净 0.1%/边）**：

| 配置 | 总收益 | 基准 -8.53% 的超额 | 回撤 | 稳态只数 | 入选平均分 |
|---|---:|---:|---:|---:|---:|
| Top50 all13 H10 score | +2.11% | +10.64pp | -4.65% | 48.9 | 93.38 |
| Top100 all13 H10 score | +1.83% | +10.36pp | -4.65% | 98.2 | 91.81 |
| Top50 IC10 H10 score | +4.22% | +12.76pp | -4.29% | 49.2 | 94.74 |
| Top100 IC10 H10 score | +2.34% | +10.88pp | -4.71% | 98.3 | 93.40 |
| Top50 tail10 H10 score | +3.15% | +11.68pp | -4.42% | 49.1 | 93.79 |
| Top100 tail10 H10 score | +2.28% | +10.81pp | -4.60% | 98.1 | 92.23 |

- **全期（2025-02-20~2026-08-26）**：Top50 all13 年化 40.21%、Top100 34.33%；
  Top50 tail10 41.11%、Top50 IC10 36.51%。**Top50 > Top100** 的方向不变；
  **tail10 全期最一致**（Top50/Top100、2025/2026 都是正增益）；IC10 在 Top50
  的 2025/2026 都落后 all13，9–10 月的好看不能当结论。
- **分数退出是主增益**：Top100 score +1.83% vs time -0.71%（+2.53pp）；
  入场过滤单独约 -0.05pp；全期 Top50 入场过滤单独仅 +0.23pp。别再重复计算 +6pp。
- **平均分**：10/19 前 100 平均 92.35、前 50 平均 94.35；名次 1→100 平滑衰减
  96.74→89.24；持仓平均分比入选名单低 3~4 分（买入后分数下滑）。
- **诚实边界**：29 天、一条路径、下跌市、冷启动；前 10 天平均仓位仅 72.9%，
  建仓期少亏约 1.6pp，但满仓后仍 +3.3%~+3.6% vs 基准 -5.69%；单笔胜率
  Top100 仅 47.6%、中位数 -0.20%，靠右尾；分数不是止损，亏损股也可能高分。
- **产出**：`data/reports/top20_eval_20261020/top100_20261020/`（summary、
  benchmark、daily_top、nav、trades、top100_20261019.csv），全期模型集在
  `full_period/model_sets/`；报告
  `data/reports/top20_eval_20261020/Top100打分与9-10月实测_20261020.md`。
- **不要用 29 天调 N/持有期/模型集**；tail10 只做影子组合，前向 1–3 个月再判。

### 15.7 买卖机制 / 周期共识 / 行业上限：全部未过预注册门槛（2026-10-20 新增）

用途：回答「除了当前 Top50/T+10/分数退出，还有没有更好的买卖办法；
怎么保证不是在同一窗口调参」。**结论：换买卖机制没有稳定增益；
真正的杠杆是成交价与交易成本，不是再加规则。**

脚本：

- `scripts/entry_exit_probe.py` — 12 个单变量机制变体 + 基线，全期与 9–10 月两窗口。
- `scripts/horizon_combo_probe.py` — T+10 / T+5 / 双周期 26 模型对照。
- `scripts/industry_cap_probe.py` — 行业上限，两套固定口径：
  `fine`（128 个细分行业）/ `l1`（代码前两位聚合的 30 个一级行业）。
- `rolling_portfolio_sim.py` 的 `ramp_days` 旋钮：默认 `None` 沿用 `hold`
  （TopN=10/H10 → 每天 1 只，10 天建满），`1` = 首日一次性买满。
  100 万 Top10 补测：一次性买满全期 47.0%、9–10 月 +0.3%；每天 1 只
  43.0%、+0.6%。一次性买满只赢 2025，前向窗口反而略差，且是单日入场
  押注，**不据此改默认**。

**预注册门槛（先写死再看结果）**：

1. 全期年化 ≥ 基线 +1.0pp；
2. 2025、2026 各自不低于基线 0.5pp 以上；
3. 2026-09-01~10-19 总收益不低于基线；
4. 全期 Sharpe 不低于基线 0.02，最大回撤不恶化超过 1.0pp；
5. 排名退出族 rank100 / rank150 必须同向。

第 4 条是看到 `open_buy_close_sell` 用「多拿半天敞口」换来 +2.6pp 后补上的；
补上后它（Sharpe 1.48 vs 1.56、回撤 -27.4% vs -23.8%、9–10 月 0.7% vs 2.1%）
被否决——这是门槛在起作用，不是事后挑结果。

**全期结果（2025-02-20~2026-08-26，371 天，净 0.1%/边）**：

| 变体 | 年化 | 相对基线 | 判定 |
|---|---:|---:|---|
| base（Top50/T+10/分数退出/收盘成交） | 40.2% | — | 基线 |
| rank75 / rank100 / rank150 退出 | 28.0% / 31.7% / 34.5% | -12.2 / -8.5 / -5.7pp | 否决 |
| 入场确认 2 日 / 退出确认 2 日 | 36.1% / 37.0% | -4.1 / -3.2pp | 否决 |
| 开盘成交 / VWAP 成交 | 36.5% / 33.2% | -3.7 / -7.0pp | 否决 |
| 卖开买收 / 买开卖收 | 34.7% / 42.8% | -5.5 / +2.6pp | 后者风险调整后否决 |
| 买开卖收但持有 9 天 | 37.1% | -3.1pp | 否决（收益来自多拿半天） |
| 持有 8 / 15 / 20 天 | 34.9% / 36.8% / 37.4% | 全负 | 否决（H10 与模型周期一致最好） |

**关键诊断**：

- **排名退出不是“更直接”，而是把仍很强的票卖掉**：换手 33.4x→69.2x，
  全期年化掉 6–12pp；排名阈值族也没有同向增益。
- **成交时点**：同日匹配交易拆解，入场日隔夜 +0.078%、日内 +0.090%；
  出场日隔夜 +0.109%、日内 +0.160%。买在开盘比买在收盘差约 0.087%/笔，
  卖在开盘比卖在收盘差约 0.16%/笔；净效果 open/open ≈ -0.073%/笔。
  **收盘（集合竞价）成交是当前最优口径**。
- **周期共识**：T+5 单用全期 30.5%、9–10 月 0.9%；T+5+T+10 共 26 模型
  全期 35.0%、2026 年 9.1%（T+10 为 18.4%）。多周期共识只在 9–10 月
  略好（3.2% vs 2.1%），2025/2026 都更差，否决。
- **行业上限没有可压的集中度**：fine 口径单行业平均 5.7 只、最多 11 只；
  l1 口径平均 9.2 只、最多 20 只。20%/30% 上限全期年化 -2.0pp / -0.8pp，
  回撤反而略差（-0.3~-0.4pp）；40% 上限从不触发。**不要加行业上限**。
- **成本敏感度（来自 §15）**：单边成本 +0.05pp ≈ 年化 -3.3pp。按单笔
  净收益约 1.3% 计算，滑点/费用差 0.1% 就吃掉约 8% 的边际。与其加规则，
  不如把执行做好（收盘竞价、限价单、拆单、避免开盘追价）。

**诚实边界**：9–10 月这个“前向窗口”已被多轮实验反复使用，不再算干净的
样本外；真正干净的检验只有未来的实盘/纸面跟踪。行业分类是当前快照
（覆盖 99.2%），只用作风险约束，不提供 point-in-time 历史分类。

### 15.8 指定起始日建仓预演：2026-10-08 / Top20 / 200 万（2026-10-21 新增）

用途：回答「我某一天要建仓，按当前规则会买什么、买多少、现在什么状态」。
脚本 `scripts/recent_build_plan.py` 复用 §15 的滚动模拟，额外输出：
逐日买入清单（含决策日排名、前 N 名分数线、100 股整手股数）、期末持仓、
已平仓、下一交易日预览。价格用 `daily_forward`（前复权）；一手金额超过
单槽的票标记 `one_lot_over_slot=True`，实盘应跳过。

```bash
python recent_build_plan.py \
  --model-map /tmp/model_map_31.json \
  --start 2026-10-08 --end 2026-10-19 \
  --capital 2000000 --top-n 20 --hold 10 \
  --out-dir /tmp/recent_build_plan
```

**2026-10-08 是国庆后首个交易日**：9/30 收盘出分，10/8 收盘买；2 只/日
（`ramp_days=hold=10`），单槽 9.8 万（200 万 × 98% / 20），单边 0.1% 成本。

| 项目 | 结果 |
|---|---:|
| 买入 | 16 笔 |
| 分数提前退出 | 6 笔（全部亏损，已实现 -2.04 万） |
| 期末持仓 | 10 只，市值 93.8 万（47.9%） |
| 期末现金 | 102.0 万（52.1%） |
| 期末净值 | 195.89 万（-2.06%；模拟口径 -2.07%） |
| 同期等权市场 | -5.05% |
| 平均仓位 | 28.0% |
| 同暴露基准 | -1.97% |
| 现金校正超额 | 约 -0.1% |
| 逐笔毛/净超额 | +0.02% / -0.17% |
| 逐笔胜率 | 31.2%（5/16） |
| 区间最大回撤 | -2.35% |

读法：这 8 天市场跌 5%，组合平均只有 28% 仓位，-2.06% 主要是市场 beta
被低仓位缓冲；按同暴露基准只差约 -0.1%，**这个窗口没有选股 alpha**。
6 笔分数止损在 1–4 个交易日内触发；10/15 一天触发 4 笔，但建仓期每天
最多买 2 只，现金没有马上补回，所以 10/19 仍有 52% 现金。
**10/22 是建仓第 11 个交易日**：ramp 上限解除，10/8 那批（301503、300387，
当前 -8.20%/-5.11%）同时到期，规则会把当时空位一次补满，可能单日买
10 只以上；不想这样跳，需要另设“补仓节奏”旋钮（当前回测没有）。

**同口径一年参照**（2025-10-09 起；2025-10-08 不是交易日）：
+26.71%，2025 +2.97%、2026 +23.06%，最大回撤 -19.77%
（2026-02-10→2026-07-30），平均仓位 94.9%，逐笔毛/净超额
+1.07%/+0.87%，胜率 53.8%（372/692），692 笔。月度最差：2026-03
-8.55%、2026-05 -10.24%、2026-06 -5.75%；最好：2026-08 +23.38%。
这是 long-only 高 beta 策略的真实路径，不能把 8 天 -2% 当模型失效，
也不能把一年 +26.7% 当未来收益。

**一次性买满对照（同 10/8 窗口，`ramp_days=1`）**：-2.36%、最大回撤
-5.46%、平均仓位 87.3%、同暴露基准 -4.94%、现金校正 +2.58%、逐笔
毛/净 +1.35%/+1.15%、胜率 66.7%（30/45）。8 天单路径里一次性买满的
20 只更分散、选股超额更好，但绝对亏损和回撤更大；全期 Top20/100 万
仍是 2 只/日 44.1% > 一次性 43.6%，9–10 月 2.0% > 0.8%。**默认不改**。
这次 2 只/日恰好先买了后来跌得最多的两只，是路径依赖，不是规则优劣的
证明。

测试：`scripts/test_recent_build_plan.py` 6 个纯函数用例（整手取整、
一手超槽、无效价格、候选排名与 top_n 分数线）；与
`test_rolling_portfolio_sim.py` 合计 17 passed。

### 15.9 逐模型「选股后第二天上涨」概率（2026-10-21 新增）

用途：回答「哪些模型选出来的票，第二天上涨的概率更高」。脚本
`scripts/model_next_day_hit.py`，对 31 个模型（T+5/T+10 各 13 个 +
T+20 5 个）逐日取 Top20 / Top100，统计次日命中率与基准的差。

```bash
python model_next_day_hit.py \
  --model-map /tmp/model_map_31.json \
  --start 2025-02-05 --end 2026-10-19 \
  --top-ks 20,100 --out-dir /tmp/model_next_day_hit
```

**两个口径必须分开**：

| 口径 | 定义 | 含义 |
|---|---|---|
| `sig` 信号次日 | `close[t+1]/close[t]-1` | t 日收盘出分，假设 t 日收盘就能买；含 t+1 涨停/停牌票 |
| `exec` 可执行首日 | `close[t+2]/close[t+1]-1` | t 日决策、t+1 日收盘成交后的第一个持有日；剔除 t+1 `blocked_long` |

基准 = 同日、同模型可投池（ADV≥0.2 亿）全部有效票的等权命中率。
模型之间比**超额**（TopK − 基准），因为原始命中率随大盘涨跌漂移。
统计上同时给日频 t（受连任自相关放大，仅参考）、月频 t（先按月平均
再检验）和月胜率；2026 窗口对 31 个模型做 BH 多重检验校正。

**全期（2025-02-05 ~ 2026-10-19，基准命中率 48.4%/48.2%）Top20**：

| 模型 | 信号次日 | 超额 | 月频 t | 月胜率 | 可执行首日 | 超额 |
|---|---:|---:|---:|---:|---:|---:|
| T+5 TabNet | 52.8% | +4.4pp | 3.41 | 17/21 | 51.5% | +3.3pp |
| T+10 XGBoost | 52.6% | +4.2pp | 3.32 | 16/21 | 52.2% | +4.0pp |
| T+10 CatBoost | 52.5% | +4.1pp | 3.01 | 14/21 | 51.2% | +3.0pp |
| T+20 RandomForest | 52.4% | +4.2pp | 2.81 | 14/21 | 51.8% | +3.7pp |
| T+20 XGBoost | 52.0% | +3.8pp | 3.36 | 15/21 | 51.4% | +3.3pp |
| T+5 XGBoost | 52.2% | +3.8pp | 2.80 | 14/21 | 51.0% | +2.8pp |
| T+5 RandomForest | 52.1% | +3.7pp | 2.49 | 14/21 | 51.3% | +3.2pp |
| T+20 CatBoost | 51.9% | +3.7pp | 3.15 | 16/21 | 51.4% | +3.3pp |
| **T+10 LSTM（最差）** | **47.8%** | **-0.7pp** | **-0.86** | 8/21 | 47.4% | -0.8pp |

**2026 年（基准 46.8%，188 天）Top20 信号次日**：T+5 TabNet 50.0%
（+3.2pp，7/10 月）、T+10 CatBoost 49.6%（+2.8pp，5/10）、T+10 XGBoost
49.3%（+2.5pp，6/10；可执行首日 49.9%，+3.3pp，全期可执行口径最好）、
T+20 RandomForest 49.3%（+2.5pp）、T+5 TCN 49.2%、T+20 XGBoost 49.1%、
T+20 LightGBM 49.1%、T+5 ALSTM 49.0%。T+10 LSTM 46.3%（-0.5pp）。

**近 3 个月（2026-07-01 ~ 10-19，基准 46.9%，72 天）**：T+20 RF 54.1%
（+7.2pp）、T+5 RF 53.9%、T+10 RF 53.6%、T+20 LightGBM 53.6%、
T+5 XGBoost 53.3%、T+10 CatBoost 53.2%、T+10 XGBoost 53.1%
（可执行 51.8%，+5.0pp）。

**组合（同日同周期等权平均分位）Top20**：

| 组合 | 全期信号超额 | 2026 信号超额 | 近期信号超额 | 2026 可执行超额 |
|---|---:|---:|---:|---:|
| T+10 全 13 模型 | +2.4pp | +1.8pp | +5.3pp | +1.7pp |
| T+10 tree4（LGB/XGB/CAT/RF） | +4.0pp | +2.2pp | +6.5pp | +1.4pp |
| T+10 tree5（+TabNet） | +3.9pp | +2.6pp | +5.6pp | +2.0pp |
| T+5 全 13 模型 | +3.7pp | +2.4pp | +4.6pp | +1.7pp |
| T+5 tree5 | +4.1pp | +3.2pp | +7.4pp | +1.9pp |

**Top100 不差于 Top20**：T+5 tree5 2026 的 Top20 信号 +3.2pp / 可执行
+1.9pp，Top100 信号 +3.1pp / 可执行 +2.7pp；近期 Top20 +7.4/+4.8pp，
Top100 +6.8/+5.3pp。**边际不集中在最头部 20 只**，扩到 100 只反而更稳。

**必须写明的统计边界**：

- 全期 BH 校正后 16/31 个模型 q<0.05，2025 年 15/31；但 **2026 年
  0/31（最小 q≈0.66）、近 3 个月 0/31（最小 q≈0.45）**。10 个月 /
  4 个月对 2–3pp 的效应量检验力不足，2026 的模型排名只能当参考，
  不能当「谁更强」的定论。
- 平均收益口径：2026 年 T+10 XGBoost 可执行首日超额 +0.16%，而双边
  成本约 0.2% → 按日换手做「第二天涨就卖」净收益约 -0.1%。
  这个概率优势只能用于**多日持有的选股**，不能支撑日频交易。
- 模型排序有中等稳定性：全期 vs 2026 的 Spearman 0.86，2026 vs 近期
  0.68，2025 vs 2026 0.70。树模型（XGB/CatBoost/RF/LGB/TabNet）在
  各窗口都为正；LSTM/GRU/ALSTM/TCN/Transformer/MLP/Linear 接近 0 或为负。
- 大盘状态过滤没有简单解：20 日市场收益分桶后各桶超额都为正，
  5 日急涨后最弱（>5% 桶 -2.9~+1.5pp，仅 19 天），但样本小、秩相关
  接近 0，**不要据此加过滤规则**。

**结论**：次日上涨概率最高的是一致性最好的树族——T+5 TabNet、T+10
XGBoost、T+10 CatBoost、T+20 RandomForest；T+10 XGBoost 在可执行
口径最稳。神经网络族（尤其 T+10 LSTM）没有次日 alpha。2026 单独看
并不显著，不要按 2026 排名再细挑模型（那是二次过拟合）。

**但次日命中率高 ≠ 10 日持有收益高（2026-10-21 实测，重要反例）**：
把组合换成 tree5（LGB/XGB/CAT/RF/TabNet，`min_models=5`）跑同一套
10 日策略，Top50 H10 score 全期年化 **28.7%**、2026 **9.3%**，低于
全 13 的 **39.2% / 20.0%**；Top20 也从 25.4%/6.6% 降到 21.2%/4.0%。
回撤相近（-22.2% vs -23.8%），但收益明显更差。说明树模型赢的是
“方向命中率”，10 日收益还取决于涨幅幅度与组合分散；**不要把实盘
组合按次日命中率换成 tree5**，全 13 继续作为 10 日策略基线。次日
分析只用于理解模型能力边界，不直接作为组合选择依据。要解释这个
反差，需要做“持有期收益分解”（t+1..t+20 的平均收益/IC 衰减）与
“留一模型增量”，不能只看命中率。

测试：`scripts/test_model_next_day_hit.py` 8 个纯函数用例（信号/可执行
双口径、窗口过滤、月频聚合、t 值边界、BH q 值、收益对齐）。
