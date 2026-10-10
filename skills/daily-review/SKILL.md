---
name: daily-review
description: "A股每日复盘（专业版）— 基于 QuantDB 本地数据 + 当日新闻情绪 + 模型推理信号 + L1/L2 因子截面 + 板块资金流的盘后复盘：指数、涨跌结构与涨停梯队、量能、行业/概念轮动、资金面、L2 微观结构、当日有新闻股票匹配、模型推理信号复盘（昨日推理命中率 + 明日信号Top5）、次日走势方向研判（六维信号加权 → 明确方向+置信度）。用户说「复盘」「每日复盘」「复盘某天」时使用：跑取数脚本（daily_review + news_review）→ 按模板写复盘报告 → 转 PDF → 落盘股票报告目录 → 聊天回复速览。触发词：复盘、每日复盘、今日复盘、盘后复盘、复盘20260814"
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

# daily-review — A股每日复盘（专业版）

盘后固定动作：把 QuantDB 当天数据 + 当日新闻情绪 + L1/L2 因子截面变成一份结构固定的复盘报告（Markdown + PDF），落盘到前端「股票报告」页可见目录，并在聊天里回复速览。**报告末必含「次日走势研判」——给出明确方向（强烈看多/看多/震荡/看空/强烈看空）+ 置信度星级 + 每维依据。**

## ⚠️ 单位铁律（先查 [[quantdb-fields]]，最高优先级）

| 陷阱 | 正确口径 |
|---|---|
| 个股 volume=**股**、amount=**万元** | 指数 volume=**手**、amount=万元 |
| 复盘报告里金额一律换算为**亿元**（万元÷1e4） | 脚本输出的 `*_yi` 字段单位已是亿元 |
| `technical_indicators.pct_change` = **%**；`return_1d/20d` 全 NaN 别用 | 涨跌家数/涨停/连板全用它 |
| `index_daily.preClose` **全 NULL** | 指数涨跌幅用 close 序列自算（脚本已处理） |
| l2_factors 已恢复日更（202608+ 当日有数据，L1 主力资金可用）；两融通常滞后 1 日（偶与当日对齐，以 facts 为准）；北向只有季度快照 | 必须带「数据滞后声明」，滞后数据集禁止当当日数据写 |

## 执行流程（每次复盘固定 5 步）

### 第 1 步：跑取数脚本（两条：daily_review 取 L1/L2/方向 + 模型推理信号，news_review 取当日新闻情绪）

```bash
# ① 宿主机：daily_review.py 出 指数/广度/板块/资金面 + L1/L2 因子透视 + 板块资金流 + 模型推理信号 + 次日方向
cd <repo>/skills/daily-review/scripts
python3 daily_review.py --date 20260814              # 指定日；不带 --date 则取最新交易日
python3 daily_review.py --watch 601138.SH,600519.SH  # 可选：自选/持仓股必带
python3 daily_review.py --watch-note "持仓源停更说明"  # 盘后流水线注入：该段可只含注记（快照停更/缺失时）
python3 daily_review.py --model mdl_cn_train_xxx     # 可选：指定推理模型，默认每日推理模型(5eea5418)
#   ↑ 模型推理信号自动查询 PG：昨日推理→今日信号命中率复盘 + 今日推理→明日信号 Top5；
#     无推理 run 时脚本**自动补跑**（docker exec trigger_inference.py，10-30 秒），
#     补跑失败原因如实写入 facts（常见：特征 parquet 滞后——先跑
#     `docker exec quantmind python3 /app/backend/scripts/update_feature_parquet.py` 补特征再复盘）
#     无 PG / PG 挂时该章节降级为缺失（facts 无「十一、模型推理信号」）

# ② 容器内：news_review.py 聚合当日新闻情绪（有新闻股票匹配 + 板块聚焦 + 来源/时段质量）
#    .claude 目录未挂载进容器，须先 docker cp 到容器 /tmp 再跑
docker cp <repo>/skills/daily-review/scripts/news_review.py quantmind:/tmp/
docker exec quantmind python3 /tmp/news_review.py --date 20260814
```

**顺序：先跑 ② 再跑 ①** —— daily_review.py 读到 `data/reports/daily_review/{YYYY-MM-DD}_news.json` 后，「新闻情绪」维度才加权、方向置信度提到 **★★★★★**；只跑 ① 时新闻维度中性、置信度降到 ★★★（facts「六、新闻情绪」会提示补跑 news_review）。

输出 `data/reports/daily_review/{YYYY-MM-DD}_stats.json` + `{YYYY-MM-DD}_facts.md`（宿主机 ①）；`{YYYY-MM-DD}_news.json`（容器 ②，写同一共享目录）。
脚本兼容宿主机与容器内（数据目录自动探测；容器内 QuantDB 路径 `/data/quantdb`、报告路径 `/data/reports/daily_review`，宿主机 `data/quantdb`、`data/reports/daily_review`）。

### 第 2 步：读 facts.md 写复盘报告（Markdown 模板）

**报告 = facts.md 的事实 + 你的解读。facts.md 没有的数字禁止出现在报告里。**

```markdown
# A股每日复盘 2026-08-14（周五）

> **报告日期**：2026-08-14
> **数据截至**：2026-08-14

## 一、盘面速览（结论先行）
2-4 句：指数表现 → 涨跌结构 → 量能 → 主线板块 → 一句话定性（强势/震荡/弱势 + 依据）→ **一句话次日方向**（引自 facts 十，如「六维合成：强烈看空 -4.6/11，置信 ★★★★★」）

## 二、指数与量能
指数表（facts 一、）+ 两市成交额解读（环比/5 日均对照，放量 or 缩量）

## 三、涨跌结构与情绪
涨停/跌停/炸板数量、最高连板与连板梯队、涨跌分布表（facts 二、四）
情绪读数 + 解读：买压/卖压对比、早盘上涨占比 → 追高意愿强还是弱

## 四、板块与主线
行业一级 Top/Bottom（facts 三）、概念 Top；指出当日主线与杀跌方向；
涨停个股聚集在哪些板块（从涨幅榜的 industry 列归纳）

## 五、资金面
两融（注明截至日与滞后天数）、北向季度快照（注明季度口径）；L2 主力净额见七

## 六、新闻情绪（当日有新闻的股票匹配）
当日新闻总数/涉及股票数、利好/利空/中性、净情绪；来源质量（高质量源 vs 反向源）；
黄金时段利好占比；新闻聚焦板块（哪些行业消息面热）；有新闻个股 Top（篇数/净情绪/事件标签）。
结论：今日消息面是偏多/偏空/中性 + 焦点板块。

## 七、L1/L2 因子透视（facts 七）
L1 换手/动量/波动均值对照前日；L2 微观结构：正向因子强信号股占比（越高=知情资金越扩散）、
VPIN 家族分位、量价背离、超级大单净额及前日对比；板块超级大单净额表（净流入 vs 净流出）。
结论：微观结构是扩散/收敛 + 资金流入/流出方向。

## 八、模型推理信号（对应 facts 十一，模型预测复盘）
两块：
1. **昨日推理 → 今日验证**：昨日模型推理(推理{data}→信号{pred})对今日的 Top10 信号对照今日实际涨跌——
   命中率/平均涨幅/相对全市场超额/涨停跌停数。写清信号平均涨幅跑赢还是跑输市场。
2. **明日信号 Top5**：今日推理(基于{data})预测明日最强 5 只（名称/代码/信号分/方向）；若今日推理未跑，标注「取最近一次推理」。

结论句点明模型风格：信号股是否集中强势板块、与当日主线是否一致、与次日方向研判是否呼应。
推理信号来自 PG（`engine_signal_scores` fusion_score 降序），数字必须照抄 facts 十一，禁止臆造。

## 九、个股榜
涨幅/跌幅/成交额/换手榜解读（facts 八），挑 3-5 只有代表性的说原因判断（无新闻佐证时只描述数据，不编原因）

## 十、自选/持仓复盘（自带 --watch 或注入 --watch-note 时才有）
逐只：涨跌幅、量能、技术位（MA20 上下）、当日状态（涨停/炸板/大涨/异动）
**若该段只有一行 ⚠️ 注记（持仓源停更/缺失/读取失败）：照实转述注记，禁止编造持仓表现**
（盘后流水线只在实盘快照覆盖复盘日时才注入 --watch 名单）

## 十一、昨日复盘回顾（复盘闭环，连续性的核心）
读上一份复盘（同目录 {上一交易日}.md 或 PDF 前的 md）的「要点与明日关注」，
逐条对照今日实际：命中几条 / 未命中几条 / 打脸的原因是什么（禁止含糊带过）
**若上一份复盘有「次日走势研判」，先对照它给出的方向是否兑现**，再对照要点。

## 十二、次日走势研判（六维方向表，直接引用 facts 十）
方向（强烈看多/看多/震荡/看空/强烈看空）+ 得分 + 置信度星级 = 六维（趋势/量能、情绪/结构、L2 微观、新闻情绪、板块/资金流）评分。
逐维解读：哪些维度在看多/看空/中性；多空矛盾点在哪；结合「有新闻股票 + L2 强信号股」交集给 2-4 条可验证的次日明确预期。
**禁止把方向当承诺**——沿 facts 口径：方向只是六维信号的可解释合成，明日以指数/广度/涨停数验证。

## 十三、要点与明日关注
- 要点：今日市场最重要的 3 条事实
- 明日关注：从「次日走势研判」的可验证预期里挑 2-4 条（明天能判断对错的才算，禁止「关注成交量变化」这类废话）

## 数据说明
滞后数据集声明 + 单位说明（从 facts 的数据说明复制）
```

**写作铁律**：每个数字带单位；涨停梯队/连板高度以脚本 stats 的 `market.streaks` 为准；涨跌停判定规则见 REFERENCES/review-methods.md；ST 涨跌幅与新股规则别记错（主板 ST 2026-07-06 起 ±10%）。

### 第 3 步：Markdown → PDF（研报风，复用 stock-market-analysis §7.4 管线）

转换脚本 `backend/scripts/md_to_pdf_report.py`（reportlab，封面/红涨绿跌语义着色/斑马纹表格自动生效）。

**环境分支**（先探测：容器内 `test -d /app/backend` 为真）：

```bash
# —— 宿主机（Claude Code）——
docker cp /tmp/复盘.md quantmind:/tmp/review.md
docker exec quantmind bash -lc "cd /app && python3 backend/scripts/md_to_pdf_report.py /tmp/review.md /tmp/review.pdf"
docker cp quantmind:/tmp/review.pdf /tmp/复盘.pdf

# —— 容器内（QuantBot / QwenPaw）——
python3 /app/backend/scripts/md_to_pdf_report.py /tmp/review.md /tmp/review.pdf
```

### 第 4 步：落盘股票报告目录（必做，只发 /tmp = 未交付）

文件名固定：`每日复盘_{YYYY-MM-DD}.md` / `.pdf`，放 `db/trading_agents_results/每日复盘/`。

```bash
# —— 宿主机：宿主机直接 cp 会 EACCES（目录 owner 是容器 root），必须 docker cp ——
docker cp 复盘.md quantmind:/data/reports/trading_agents/每日复盘/每日复盘_2026-08-14.md
docker cp 复盘.pdf quantmind:/data/reports/trading_agents/每日复盘/每日复盘_2026-08-14.pdf

# —— 容器内：直接 cp ——
cp 复盘.md /data/reports/trading_agents/每日复盘/每日复盘_2026-08-14.md
cp 复盘.pdf /data/reports/trading_agents/每日复盘/每日复盘_2026-08-14.pdf
```

落盘后 `ls` 确认 md + pdf 都在（前端「股票报告」页 → 每日复盘 文件夹）。

### 第 5 步：聊天回复速览（QuantBot/Claude 直接回答用户用这个格式）

```markdown
**A股复盘 2026-08-14（周五）**

一句话总结：…

指数：上证 +0.01% / 深成 +0.45% / 创业板 +1.12% / 科创50 -0.00% / 北证50 -0.94%
广度：涨 2400 / 跌 2970（涨跌比 0.81）；涨停 64 / 跌停 14 / 炸板 22；最高 5 连板
量能：两市成交额 21,565.76 亿元（环比上一交易日 1.04x；5 日均 21,xxx 亿，量比 x.xx）
主线：行业 Top3 …；概念 Top3 …
资金：两融 xxx 亿（截至 08-13，+xx 亿）；北向 2026Q2 持仓市值 30,685.68 亿元；超级大单 ±xx 亿
情绪：买压 0.49 / 卖压 0.51；早盘上涨占比 40.68%
新闻：命中 xxx 篇 / 有新闻股票 xxx 只；净情绪 ±xx%；聚焦板块 …
L2：强信号股占比 xx%；VPIN 分位 xx；超级大单 ±xx 亿
模型推理：昨日 TopN 信号今日命中率 xx%（平均 ±xx%，超额 ±xx%）；明日信号 Top5：新易盛/兆易创新/…
自选：…（有 --watch 才有）
**次日方向：看空/看多…（得分 xx/11.0，置信 ★★☆）**

→ 完整复盘报告已落盘「股票报告 → 每日复盘」目录
```

## 复盘日期标注规则

- 用户说「复盘」不带日期 → 最新交易日（脚本默认行为）
- 「复盘 20260814」/「复盘 8月14日」 → `--date 20260814`；非交易日脚本自动取 ≤ 该日期的最近交易日并在报告封面注明**实际复盘日**
- 报告文件名、封面`报告日期/数据截至`、聊天速览标题三处日期必须一致

## 维护

- 单测：`cd scripts && python3 -m pytest tests/ -q -c tests/pytest.ini`（覆盖涨跌停判定/连板/分布/板块加权/单位换算/除权检测/方向引擎/模型推理命中率与 run 选择）
- 核心脚本：
  - `daily_review.py` — 攻全市场基本面/技术/资金/L1/L2/方向 + 模型推理信号（含 `direction_engine.py` 六维评分）
  - `news_review.py` — 当日新闻情绪聚合（**须在容器内跑**：`.claude` 未挂载进容器，先 `docker cp` 脚本到 `/tmp`；输出写到共享的 `/data/reports/daily_review/`）；PG 富集表是活数据，同一日重跑结果可能因 enrichment 继续更新而略有变化
  - `inference_signals.py` — 模型推理信号 PG 查询（`engine_signal_scores` + `qm_model_inference_runs`）：昨日推理→今日验证 / 今日推理→明日 Top5；默认每日推理模型 `5eea5418`，可用 `--model` 覆盖；PG 挂则降级缺失
  - `direction_engine.py` — 纯函数六维评分器，无 I/O；方向只是信号的可解释合成，**不是预测承诺**
- 涨跌停规则**复用** `backend/services/trade/simulation/services/local_market_data.py`（经 ZTPrice/DTPrice 交叉验证 99.71%），禁止在本 skill 里另写一套

## 相关技能

- **[[quantdb-fields]]** — 必读：单位/口径速查（本 skill 计算正确性前提）
- **[[news-sentiment-research]]** — 新闻情绪研究方法论（来源白/黑名单、时段质量、事件标签的来源；六维里的「新闻情绪」维度据此加权）
- **[[stock-market-analysis]]** — 盘后想对某只股票深挖时用（复盘是广度，它是深度）
- **[[quantdb-sdk]]** — QuantDB 数据源背景