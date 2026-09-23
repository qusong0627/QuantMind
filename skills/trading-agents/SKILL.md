---
name: trading-agents
description: "个股深度投研分析（智能体自主版）— 拉取 QuantMind 本地数据（371维特征/风险评分/模型推理分数/新闻）→ 多空子代理辩论 → 综合研判 → 生成 md 报告 → 导出 PDF 到平台「股票报告」页。任何大模型（deepseek/qwen/glm/openai/minimax）都能执行，不依赖容器投研管线。在 QuantBot / Claude Code 中深度分析股票时使用。触发词：投研分析、深度分析、个股分析、股票报告、生成报告、多空分析、AI分析师"
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
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（`/data` 是 QwenPaw 与 quantmind 容器共享的可读写挂载，**QwenPaw 直接 mkdir/cp 即可，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① QwenPaw 本地直接跑 `python3 /app/backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（扩展镜像已内置 reportlab + 中文字体，研报级排版，首选）；
>    ② QwenPaw 环境缺依赖时，`docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（扩展镜像已含 docker CLI）；
>    ③ 以上都不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
>    ④ 全部失败则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# 个股深度投研分析（智能体自主版）

> **核心定位**：本技能由 **AI 智能体自己执行**（QuantBot / Claude Code 等），**不依赖**容器内 TradingAgents 管线，**任何大模型都可以跑**。
> 数据全部来自 QuantMind 本地 API（QuantDB 数据库），分析完成后智能体**自己组装 md 并导出 PDF**，报告自动出现在平台「股票报告」页。

## 一、完整流程总览

```
用户: 深度分析 600519
  ↓
① 确认标的 + 市场（默认 A股）
  ↓
② 拉取本地数据（并行）：
   - /research/features/{symbol}          371 维特征（估值/技术/动量/资金流/筹码/概念）
   - /risk/score/{symbol}                 6 维风险评分卡
   - /models/inference/stock/{symbol}/history?days=180   模型推理分数历史
   - /research/kline/{symbol}?days=120    K线 + 均线
   - /news/articles?tickers=xxx           RSS 新闻（利好/利空）
   - /selection/daily                     全市场排名（该股是否入选）
  ↓
③ 多空子代理辩论（至少 2 个子代理，不同立场，用不同数据视角）
  ↓
④ 综合研判（证据强度裁决，不是篇幅裁决）
  ↓
⑤ 组装 Markdown 报告（标题含股票名/日期/分析时间）
  ↓
⑥ 导出 PDF（容器内 md_to_pdf_report.py，TTF 内嵌中文字体）
  ↓
⑦ 保存到 /data/reports/trading_agents/{市场名}/{股票名}/  → 用户去「股票报告」页查看
```

## 二、认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 三、数据拉取（智能体必做）

### 3.1 个股 371 维特征

```bash
curl -s -H "$AUTH" "$BASE/api/v1/research/features/600519.SH"
# 返回 15 大类: valuation/technical/momentum/volatility/liquidity/fundFlow/
#            fundamental/style/industry/chip/concept/microstructure/sentiment 等
# 批量: POST /api/v1/research/batch-features {"symbols":["600519.SH","000858.SZ"]}
```

**symbol 格式**：A股 `600519.SH`；港股 `00700.HK`；美股 `AAPL`；区块链 `BTC`；期货 `Au99.99.FUT`。

### 3.2 风险评分卡（6 维）

```bash
curl -s -H "$AUTH" "$BASE/api/v1/risk/score/600519.SH"
# 流动性/波动/趋势/过热/基本面/状态 6 维度 + risk_level + veto 否决项
```

### 3.3 模型推理分数（历史趋势 + 最新 + 多模型）

```bash
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/600519.SH/history?days=180"
# 无 envelope 包裹，直接返回:
# {"symbol","name":"贵州茅台","industry","board","total","items":[...]}
# items[]: {trade_date, fusion_score, signal_side, score_rank, run_id, signal_model_id, ...}
# 规则: 分数上升→多方；高位回落→空方；最新 score_rank 越小越靠前
```

**多模型交叉（必做）**：平台有多个训练模型（不同训练期/周期 T3/T10/T15/融合 ensemble），**不只看默认模型**：
```bash
curl -s -H "$AUTH" "$BASE/api/v1/models"          # 模型列表（含 model_id/model_type/周期）
curl -s -H "$AUTH" "$BASE/api/v1/models/default"  # 默认模型
# 用 model_id 逐个拉该模型的历史序列（排名在该模型内计算）:
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/600519.SH/history?days=180&model_id=xxx"
```
**多模型分析三要素**：
1. **共识度**：多模型同方向 = 高置信；分歧 = 报告单独说明分歧及原因
2. **各自趋势**：每模型序列上升/回落/横盘（分数绝对值小 ≠ 无意义，看**变化方向**）
3. **模型背离最值钱**：基本面好但模型持续 SELL、资金流出 → "好公司 ≠ 好买点"

### 3.4 全市场排名（该股在候选池的位置）

```bash
curl -s -H "$AUTH" "$BASE/api/v1/selection/daily"
# 返回 {"status":"success","meta":{...},"market_state":{...},"candidates":[...]}
# meta.total_signals = 当日信号总数；market_state 含市场状态（牛/熊市、仓位建议）
# candidates 空 = 当天无入选（熊市空仓正常），报告要说明"市场状态 + 该股未入选"
```

### 3.5 K 线 + 均线

```bash
curl -s -H "$AUTH" "$BASE/api/v1/research/kline/600519.SH?days=120"
# 近期走势位置：均线多空排列、支撑/压力、放量/缩量
```

### 3.6 新闻（RSS）

```bash
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&limit=20"
# 按利好/利空/中性分类；时间线排序
```

**新闻深度参数**（`/news/articles` 全量过滤能力）：
```bash
# 个股 + 行业双通道（个股没新闻 ≠ 行业没新闻）
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&industries=白酒&limit=30"
# 强信号（|score|>=0.5）+ 情感过滤 + 事件标签
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&strong_only=true&sentiment=bullish"
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&event_tags=解禁,减持"
# 最快定位最强多空新闻
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&sort=sentiment_bullish&limit=10"
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&sort=sentiment_bearish&limit=10"
```

**新闻用法三条**：
1. **事件要后续印证**：公告增持 → 查资金流是否真流入；解禁 → 查筹码/大宗
2. **价值排序**：政策 > 公司重大事件 > 行业动态 > 分析师观点 > 情绪文
3. **禁止编造**：无新闻标 `[数据缺失]` 并提醒加 RSS 源

**⚠️ 若无新闻**：报告里标注 `[数据缺失: 个股新闻 0 条]`，并在回复中**提醒用户去后台「RSS 管理」或 Huntly 添加该股相关新闻源**（财联社、证券时报、新浪财经个股页等）。

## 四、多空子代理（智能体必做）

**至少组织 2 个子代理**（能 3~4 个更好），并行、立场相反：

| 子代理 | 立场 | 数据视角 |
|---|---|---|
| 多方代理 | 找买入理由 | 估值低、技术转强、资金流入、行业催化、推理分数上升 |
| 空方代理 | 找卖出/回避理由 | 估值高、动量衰竭、筹码松动、解禁减持、推理分数走弱 |
| 综合代理（可选） | 证据强度裁决 | 对比两方论据质量，用数据强度而非篇幅下结论 |

**每个子代理必须引用具体数据**（PE 值、RSI、资金净流入、推理分数变化），禁止空泛形容词。

## 五、模型推理分数交叉验证（必做）

分析结论必须结合平台模型对这只股票的**推理分数/排名/历史趋势**（端点返回见 3.3）：

- `fusion_score` **持续上升 + score_rank 靠前** → 支持多方（技术面再确认）
- 分数**高位回落 / 排名下滑** → 支持空方（警惕追高）
- 分数与多空辩论结论**背离**时，报告中单独说明分歧及原因
- `signal_side` 是模型当时给出的操作信号（BUY/HOLD/SELL），一并引用
- **多模型**：按 3.3 拉全部模型（含 ensemble）逐个比对——共识度高则置信度高；模型间分歧本身就是信息

> 更深的全方位分析（估值历史分位/财务三表/资金筹码多口径/行业共振分类），见 [[stock-market-analysis]] 技能的 REFERENCES/quantdb-full-analysis-design.md 七层框架。

## 六、组装 Markdown 报告（版式规范，必遵守）

```markdown
# {股票名}({ticker}) 投研分析报告

> **交易日期**: {YYYY-MM-DD}　|　**分析时间**: {YYYY-MM-DD HH:MM}　|　**市场**: {A股/港股/美股/区块链/期货}
> **最终评级**: **{买入/增持/持有/减持/卖出}**

---

## 一、综合结论
（3~5 句话：评级 + 核心逻辑 + 关键风险）

## 二、模型推理信号
- 最新分数: {x.xx}　|　180 天趋势: {上升/回落/横盘}
- 全市场排名: {第 N 名 / 未入选}
- 与多空辩论一致性: {一致/背离 + 说明}

## 三、多空辩论
### 多方观点
（数据支撑的买入理由）
### 空方观点
（数据支撑的风险/卖出理由）
### 辩论裁决
（综合代理结论）

## 四、基本面分析
（PE/PB/ROE/市值/行业地位，来自 features 的 valuation + fundamental）

## 五、技术面分析
（K线形态/均线/RSI/MACD/量能，来自 kline + technical）

## 六、资金与筹码
（fundFlow 资金流向 + chip 筹码集中度）

## 七、风险提示
（risk/score 的 6 维评分 + veto 否决项 + 风险等级）

## 八、新闻舆情
（利好/利空/中性分类表格；无新闻则标注数据缺失并提醒加新闻源）

---

> 本报告由 AI 智能体基于 QuantMind 本地数据自动生成（{分析时间}），仅供学习研究，不构成投资建议。
```

**硬性要求**：
1. 标题 = **股票名 + ticker + 日期 + 分析时间**（缺一不可）
2. 多空双方必须都有（不能只有一方）
3. 模型推理分数/排名/趋势必须写进报告
4. 数据要有具体数值，禁止只说"较高/较低"

## 七、导出 PDF（智能体自己执行）

### 7.1 保存 md 到报告目录

```bash
# 目录结构: 市场文件夹 / 股票名文件夹（A股市场 / 美股市场 / 港股市场 / 区块链市场 / 期货市场）
mkdir -p "/data/reports/trading_agents/A股市场/贵州茅台"
# 文件名: {股票名}{代码}_{trade_date}_投研分析报告.md（股票名查不到时省略股票名）
# 例: 贵州茅台600519_2026-08-15_投研分析报告.md
```

> QwenPaw 对 `/data` 有直接读写权限（与 quantmind 容器共享挂载），md 直接写入即可，**无需 docker cp**；仅宿主机本地 Claude Code 场景目录 owner 是容器内 root 时，才用 `docker exec quantmind python` 落盘（见 FAQ）。

### 7.2 转 PDF（首选 QwenPaw 本地，reportlab + 中文字体已内置）

```bash
python3 /app/backend/scripts/md_to_pdf_report.py \
  "/data/reports/trading_agents/A股市场/贵州茅台/贵州茅台600519_2026-08-15_投研分析报告.md" \
  "/data/reports/trading_agents/A股市场/贵州茅台/贵州茅台600519_2026-08-15_投研分析报告.pdf"
# 备选（本地缺依赖时）：
# docker exec quantmind python /app/backend/scripts/md_to_pdf_report.py <同上路径>
```

**PDF 特性**：A4、TTF 内嵌中文字体（任何 PDF 阅读器含浏览器 pdfjs 都正常渲染）、**粗体真实加粗**（正文文泉驿 MicroHei + 粗体 ZenHei 独立字重）、h1 居中大标题、h2 蓝色小节、表格深蓝表头 + 隔行底色。

**字体回退链**（脚本自动探测）：`/app/docker/training/fonts/WQYMicroHei.ttf`（+ `WQYZenHei.ttf`）→ `NotoSansCJK.ttf` → arphic/uming → NotoSerifCJK → STSong-Light（CID 不内嵌，仅兜底）。WQY ttf 放在宿主机 `docker/training/fonts/` 即可，**bind mount 自动进容器，无需重建镜像**。

**验证字体真正内嵌**（⚠️ reportlab 给子集字体加 `AAAAAA+` 前缀，直接 `grep 'WQY'` 字节串会误报 False，必须用 BaseFont 正则解析）：

```bash
docker exec quantmind python -c "
import re
data = open('/data/reports/trading_agents/A股市场/贵州茅台/贵州茅台600519_2026-08-15_投研分析报告.pdf','rb').read()
bf = sorted(set(m.group(1).decode() for m in re.finditer(rb'/BaseFont\s*/([A-Za-z0-9+_.-]+)', data)))
print('字体:', bf)
print('粗体 OK:', any('ZenHei' in f or 'CJK-Bold' in f for f in bf))
print('正文 OK:', any('MicroHei' in f or 'CJK' in f for f in bf))
"
```

**md 排版注意**：
- 副标题分隔符用全角空格 `　`（`{date}　|　{time}`），脚本会将其替换为普通空格（reportlab 段落排版会丢弃全角空格）
- 表格单元格内不要写 `|` 转义（脚本会移除）
- 代码块三反引号正常转义渲染

### 7.3 查股票名（标题需要）

```bash
# A股: QuantDB instrument_detail parquet（Symbol 格式 600519.SH）
docker exec quantmind python -c "
import pandas as pd
df = pd.read_parquet('/data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet', columns=['Symbol','Name'])
print(df[df['Symbol']=='600519.SH']['Name'].iloc[0])
"
# 港股/美股: 板块 parquet
docker exec quantmind python -c "
import pandas as pd
df = pd.read_parquet('/data/quanthk/2_base_sector/sector/00700.HK.parquet')
print(df.columns.tolist())   # 找 name 列
"
```

**查不到名字时**：标题回退纯代码 `600519 投研分析报告`，不阻塞流程。

### 7.4 验证导出成功

```bash
ls -la "/data/reports/trading_agents/A股市场/{股票名}/" | grep {ticker}
# 或调用列表接口确认（报告档案页同源）:
curl -s "$BASE/api/v1/trading-agents/files/list" | python3 -m json.tool | grep {ticker}
```

## 八、告知用户（收尾话术）

分析完成后，智能体回复用户必须包含：
1. **最终评级 + 一句话核心逻辑**
2. **多空分歧要点**（双方各自最强论据）
3. **模型推理分数印证**（当前分数/趋势/排名）
4. **"报告已导出 PDF，请到「股票报告」页查看"** ← 用户要求：最后提示导出 PDF

## 九、股票报告页（文件管理 API）

```bash
# 列出所有报告（市场文件夹 → 股票名子文件夹 → 文件，二级结构）
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/files/list"
# 返回 folders[]: {name: 市场名, files[]: 市场目录直属文件, subfolders[]: {name: 股票名, files[]}}

# PDF 预览（浏览器 iframe 内联）: $BASE/api/v1/trading-agents/files/pdf/{filename}
# filename 只需文件名（如 贵州茅台600519_2026-08-15_投研分析报告.pdf），
# 后端递归搜索任意层级，同名取修改时间最新

# 删除文件（可多选，递归搜索任意层级）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/files/delete" \
  -d '{"files":["贵州茅台600519_2026-08-15_投研分析报告.pdf"]}'

# 新建文件夹 / 移动文件 / 删除文件夹（folder 支持「市场/股票名」两级路径）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/files/create-folder" -d '{"folder":"重点观察"}'
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/files/move" \
  -d '{"files":["贵州茅台600519_2026-08-15_投研分析报告.pdf"],"target_folder":"A股市场/贵州茅台"}'
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/files/delete-folder" -d '{"folder":"重点观察"}'
```

## 十、可选的容器投研管线（备用模式）

> 平台还有一条**容器内 TradingAgents 管线**（7 AI 分析师 → 质量门控 → 多空辩论 → 风控 → 最终决策），但**依赖容器内 LLM Key 配置**（minimax/openai 等），Key 没配好会 401。
> 管线可正常运行时，分析完成**自动导出** md+PDF（后端 report_exporter.py 已内置）。

```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/analyze" \
  -d '{"ticker":"600519","trade_date":"2026-08-15","market":"CN"}'
# → analysis_id，然后循环:
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/progress/{analysis_id}"   # 直到 is_complete
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/report/{analysis_id}"     # 拿 stage_reports
```

**智能体选择模式**：
- 默认用**自主模式**（本技能第 2~8 节）—— 任何大模型都能跑，不依赖管线 Key
- 管线 Key 可用且用户明确要"7 分析师全管线"时，用备用模式，取 stage_reports 后仍按本技能第 6 节版式组装 md + 导出 PDF

## 十一、相关技能

- **[[stock-market-analysis]]** — 量化因子深度分析（371 字段 + 风险评分 + 数据导出）
- **[[batch-inference-analysis]]** — 模型推理信号选股
- **[[quantmind-operations]]** — RSS 新闻、模型推理
- **[[quantdb-sdk]]** — QuantDB 数据查询（28 数据集）
- **[[simulation-trading]]** — 分析结论落地模拟/实盘交易

## 十二、常见问题

| 现象 | 处理 |
|---|---|
| features 接口返回空 | 检查 symbol 格式（A股必须带 .SH/.SZ/.BJ）；用 batch-features 批量试 |
| 新闻为空 | 报告标注数据缺失 + 提醒用户加 RSS 新闻源（后台 RSS 管理/Huntly） |
| 股票名查不到 | 标题回退纯代码；US/HK 查 sector parquet 的 name 列 |
| PDF 转不出来 | 确认容器 `docker exec quantmind python -c "import reportlab"`；字体回退链见 7.2（WQY 在宿主机 `docker/training/fonts/`，bind mount 进容器 `/app/docker/training/fonts/`） |
| 股票报告页找不到报告 | 目录名必须用 **`A股市场`（无空格）**，与后端 `report_exporter.py::_MARKET_NAMES` 一致；新结构为「市场文件夹/股票名文件夹/{股票名}{代码}_{日期}_投研分析报告.{md,pdf}」 |
| 报告目录写不进 | 宿主机目录 owner 是容器内 root：md 先写 `/tmp` 再 `docker cp` 进容器，或用 `docker exec quantmind python` 直接落盘 |
| 推理分数接口 404 | 该股近期无推理记录，报告中注明"无最近推理数据" |
