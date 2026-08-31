---
name: stock-picks
description: "每日复盘后的股票推荐（多维度选股）— 综合 A股每日复盘（市场方向/板块/资金/新闻情绪）+ 个股深度分析（9层），用多维度筛选条件（L2微观结构为主/模型融合分/仓位信号/板块强度/新闻情绪）从全市场挑出未来几天大概率走强的股票，支持跨多日聚合（--window），输出候选榜 + 综合复盘 + Top 个股深度分析，落盘 PDF 到股票报告目录。用户说「选股」「推荐股票」「每日推荐」「明日看好」「选股推荐」「复盘后选股」时使用：跑复盘取数 → pick_candidates 多维打分 → Top N 深分 → 综合报告 → PDF。触发词：选股、股票推荐、每日推荐、明日看好、推荐股票、今日选股"
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

# stock-picks — 每日复盘后的股票推荐（多维度选股）

把「每日复盘」（市场广度）和「个股深度分析」（个股深度）两张皮缝起来：先用复盘产物定市场环境（该不该进场、主线在哪、资金方向），再用多维度筛选条件从全市场挑候选，对 Top N 做 9 层深分，最后输出**一份报告**：综合复盘 + 候选榜 + Top 个股深分。报告 Markdown + PDF，落盘前端「股票报告」页可见目录，聊天回复速览。

**定位**：`[[daily-review]]` 是广度（今天市场发生了什么），`[[stock-market-analysis]]` 是深度（某只股票值不值得看），本 skill 是**两者合一的推荐**——先选再深挖，产出可执行的候选池，不是复盘报告。

## ⚠️ 铁律（先读，最高优先级）

| 陷阱 | 正确口径 |
|---|---|
| **推荐不是承诺** | 所有候选是「多维信号合成的相对强势」，不是「明天必涨」。报告必须带风险声明 + 数据滞后声明 |
| **ST 股默认排除** | ST/*ST/退 有 5% 涨跌幅限制 + 退市风险，`pick_candidates.py` 默认排除（`--keep-st` 才保留） |
| **信号日取最近「全量」推理日** | 默认 = distinct symbol ≥ 1000 的那天（`engine_signal_scores`），避免最近只推理了几十只的残日；`--date` 可显式指定 |
| **分数单位** | fusion_score 是模型预测分（非涨跌幅）；position_score 0~1 是半凯利仓位；pct_industry 是行业截面百分位 |
| **L2 是 T+5/T+10 信号** | VPIN 族正 IC（高分偏多）、vol_persistence 等负 IC（高分偏空）——看状态分位，别当单日信号 |
| **趋势不纳入** | 模型分数趋势维度按需求移除——打分只用 L2/融合/仓位/板块/新闻，不参与排名 |
| **L2 主导** | L2 权重(40%) > 融合分(30%)：L2 是 T+5/T+10 信号，先看订单簿微结构，再看模型预测 |

## 执行流程（固定 6 步）

### 第 1 步：跑每日复盘取数（复用 [[daily-review]]）

```bash
# ① 宿主机：daily_review.py 出 指数/广度/板块/资金/L1/L2 + 模型推理信号 + 次日方向
cd <repo>/skills/daily-review/scripts
python3 daily_review.py --date 20260821              # 不带 --date 取最新交易日

# ② 容器内：news_review.py 聚合当日新闻情绪（先跑这个，新闻维度才能加权）
docker cp <repo>/skills/daily-review/scripts/news_review.py quantmind:/tmp/
docker exec quantmind python3 /tmp/news_review.py --date 20260821
```

产出：`data/reports/daily_review/{YYYY-MM-DD}_stats.json` + `{YYYY-MM-DD}_facts.md` + `{YYYY-MM-DD}_news.json`。
这一步给推荐提供：市场方向（六维）、主线板块、资金流向、新闻聚焦板块、L2 微观结构状态——**推荐必须和市场环境自洽**（大盘空仓日不该推满仓，杀跌板块的个股即使分数高也要警惕）。

### 第 2 步：多维度选股（本 skill 脚本）

```bash
python3 <repo>/skills/stock-picks/scripts/pick_candidates.py --data-date 20260821 --window 3 --top 30 --json   # 跨3日聚合
python3 <repo>/skills/stock-picks/scripts/pick_candidates.py --data-date 20260821 --window 1 --top 30 --json   # 严格单日（无未来视觉）
python3 <repo>/skills/stock-picks/scripts/pick_candidates.py --top 30 --json   # 默认最近全量推理日
python3 <repo>/skills/stock-picks/scripts/pick_candidates.py --top 30 --json --no-l2  # 跳过 L2（更快）
```

产出：`data/reports/stock_picks/{YYYYMMDD}_picks.json`（全量候选 + 每维分解）+ `{YYYYMMDD}_picks.md`（排名表骨架）。
**多维度打分 = 六维加权**（满分 1.0，L2 主导）：L2 40% / 融合 25% / L1动量 15% / 仓位 10% / 板块 5% / 新闻 5%。趋势不纳入。**单模型铁律**：只用默认日推模型（5eea5418）的单一 run 分数，杜绝多模型融合。**默认精选 5 只**。
**跨日聚合**：`--window N` 从数据日起往前 N 个推理日，每股取跨日复合分均值后排名（`--window 1` = 严格单日无未来视觉）。
**硬过滤**：① 无融合分数剔除；② 仓位门 `position_score>0 或 行业百分位≥80%`（避免大盘空仓日推满仓）；③ 默认排除 ST。
未含维度（如当日无 news.json、L2 分区缺失）时该维中性 0.5，`picks.md` 头部会标注「未含：L2, 新闻」——报告里要声明，不能假装都有。

### 第 2.5 步：候选 × 新闻增强（本 skill 脚本，推荐必做）

对候选池逐只拉 Huntly/RSS 聚合新闻（近 24h，含 LLM 情感标注），
产出「分数 + 新闻 + 情绪」合并表，**新闻直接修正推荐**：

```bash
python3 <repo>/skills/stock-picks/scripts/picks_with_news.py --top 10                  # 最新日期
python3 <repo>/skills/stock-picks/scripts/picks_with_news.py --date 20260831 --top 15 # 指定日期
python3 <repo>/skills/stock-picks/scripts/picks_with_news.py --json                    # 供上游脚本解析
```

产出：`data/reports/stock_picks/{YYYYMMDD}_picks_news.md`。
标注规则：**★ 新闻强化** = 近 24h ≥2 利好且多于利空（报告里可上调推荐）；**⚠ 利空警示** = ≥2 利空且多于利好（必须下调或剔除，哪怕分数高）；无新闻 = 分数为准不渲染情绪。
**写报告时新闻详情逐条引用**（标题/来源/北京时间/利好利空），且与第 3 步深分的 L7 新闻层互相印证——两边都有的消息是重点。

### 第 3 步：Top N 深分（复用 [[stock-market-analysis]]）

对候选榜前 5~10 只跑 9 层深分，取 `--json` 输出供报告引用：

```bash
python3 <repo>/scripts/stock_9layer_fetch.py 001237.SZ --json   # 宿主机
# 输出 /tmp/001237_9layer.json（23 因子 vs 全市场截面分位 + IC 方向）
```

深分重点核对（和候选维度互相印证，**发现矛盾要写进报告**）：
- **L3 技术位**：候选时点 vs MA20/前高——分数高但跌破 MA20 的是矛盾项
- **L4b 微观结构**：正 IC 因子（VPIN 族）是否处于健康分位
- **L6 模型**：该股历史推理信号 vs 当日融合分，是否一致
- **L7 新闻**：有没有个股直接消息/相关行业消息，和三步纵深结论

### 第 4 步：综合报告写作（Markdown，模板见下）

**报告 = facts.md 的事实 + picks 骨架的数字 + 深分的数值 + 你的解读。facts/picks/深分没有的数字禁止出现。** 推荐榜数字必须照抄 `picks.json`，深分数字必须照抄 `/tmp/{code}_9layer.json`，禁止臆造。

### 第 5 步：Markdown → PDF + 落盘（必做，只发 /tmp = 未交付）

```bash
# Markdown → PDF（研报风，复用 md_to_pdf_report.py）
docker cp 选股推荐.md quantmind:/tmp/picks.md
docker exec quantmind bash -lc "cd /app && python3 backend/scripts/md_to_pdf_report.py /tmp/picks.md /tmp/picks.pdf"
docker cp quantmind:/tmp/picks.pdf 选股推荐.pdf

# 落盘股票报告目录（宿主机必须 docker cp，目录 owner 是容器 root）
docker cp 选股推荐.md quantmind:/data/reports/trading_agents/每日选股/每日选股推荐_2026-08-21.md
docker cp 选股推荐.pdf quantmind:/data/reports/trading_agents/每日选股/每日选股推荐_2026-08-21.pdf
```

落盘后 `ls` 确认 md + pdf 都在（前端「股票报告」页 → 每日选股 文件夹）。

### 第 6 步：聊天回复速览

```markdown
**每日选股推荐 2026-08-21（周五）**

一句话：…（市场环境 → 推荐逻辑 → 候选池特征）

市场：上证 +0.01% / 深成 +0.45%；涨停 64 / 跌停 14；主线 …；资金 …；次日方向 看多/看空（xx/11，★★★）

Top5 候选（五维综合，L2 主导）：
1. 惠康科技 001237 — L2 0.80 / 融合 0.0365 / 仓位 84% / 行业分位 100%
2. 今天国际 300532 — …（每只 1 行，标注最强维度 + 该股风险点）
3. …

深分亮点：Top3 中 X 只技术位健康、L2 VPIN 分位 >70%……（哪些候选通过了深分、哪些有矛盾）

⚠️ 以上为模型信号合成的相对强势候选，非投资建议；数据截至 2026-08-21（两融/北向滞后见数据说明）

→ 完整报告已落盘「股票报告 → 每日选股」目录
```

## 多维度筛选条件详解（写报告时逐维引用）

| 维度 | 权重 | 数据源 | 怎么判读 |
|---|---|---|---|
| L2 微观结构 | **40%** | `l2_factors` 正 IC 因子 + 负 IC 因子（负 IC 反转） | 正 IC 高=知情资金活跃；负 IC 低=毒性/波动小；健康分 = 0.5×正IC分位 + 0.5×(1−负IC分位) |
| 模型融合分数 | 30% | `engine_signal_scores.fusion_score` 幅度归一 | 融合分越高=模型预测收益越强；用默认单模型（日推模型） |
| 仓位信号 | 15% | `quality.position.position_score` | 0.8+ = 强行业地位 + 半凯利高仓位；<0.3 弱；数据缺失不拦 |
| 板块强度 | 10% | `industry_top10_avg` 截面分位 + 板块超级大单净额 | 行业头部强度 + 当日板块大单净流入（跌市抄底方向反推） |
| 新闻情绪 | 5% | `news_review.py` 产物 `news.json` | net_ratio>0 偏多；有直接个股新闻的优先 |

**报告里每只候选必须能说清「它强在哪几个维度」+「弱在哪」**，禁止只贴数字不解读。候选若和市场主线、次日方向冲突，必须明说。

## 报告模板

```markdown
# 每日选股推荐 2026-08-21（周五）

> **报告日期**：2026-08-21
> **数据截至**：2026-08-21（信号日）
> **口径**：候选来自最近全量推理日 engine_signal_scores + l2_factors + 当日新闻情绪；五维加权（L2 40/融合 30/仓位 15/板块 10/新闻 5，趋势不纳入）

## 一、市场环境（综合复盘，从 facts.md 提炼）
指数/广度/量能/主线/资金 2-4 句 + 次日方向（六维合成）+ 一句话「该不该进场/什么风格占优」。
**这里定推荐基调**：市场偏强推进攻型，震荡降仓位，弱势只列观察不推荐。

## 二、候选榜（Top10，照抄 picks.md）
表格：# / 代码 / 名称 / 综合分 / L2 / 融合 / 仓位 / 行业分位 / 覆盖日 / 行业 / 板块大单
每只标注最强维度 + 一句话依据。

## 三、Top 个股深度分析（前 3-5 只，用 stock_9layer_fetch --json 结果）
每只分节：
### {名称}（{代码}）
- **候选维度**：最强维度 + 分数
- **9层核对**：L3 技术位（vs MA20）、L4b 微观结构（VPIN 分位）、L6 模型一致性、L7 新闻（三步纵深结论）
- **风险点**：弱维度 / 技术矛盾 / 板块杀跌风险
- **综合判断**：推荐 / 观察 / 剔除（剔除要写原因）

## 四、推荐逻辑与风险声明
- 候选池与市场主线一致性：命中/偏离
- 数据滞后声明（从 facts.md 复制）
- ⚠️ 本报告为模型信号合成的相对强势候选，非投资建议。股市有风险，投资需谨慎。

## 五、明日验证清单
2-4 条可验证预期（明天能判断对错的）：如「Top5 平均跑赢全市场」「候选集中板块继续走强」等
```

## 维护

- 核心脚本：`scripts/pick_candidates.py`（宿主机跑；PG `engine_signal_scores` + `stock_aliases`，QuantDB `l2_factors`，`news_review.py` 产物）
- 依赖技能：`[[daily-review]]`（复盘取数）、`[[stock-market-analysis]]`（深分）、`[[quantdb-fields]]`（单位口径）
- 权重/阈值改 `pick_candidates.py` 顶部常量（`W_*`、`_POSITION_GATE`、`_MIN_SIGNAL_COVERAGE`）
- 单测：`cd scripts && python3 -m pytest tests/ -q`（覆盖打分/过滤/趋势/单位）
