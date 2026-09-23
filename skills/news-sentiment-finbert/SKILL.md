---
name: news-sentiment-finbert
description: "RSS 新闻情绪识别（FinBERT 中文金融情感）安装与运维 — 情绪管线架构、transformers 安装、FinBERT 权重下载、字典法扩充、全量重算、情绪筛选/条形图/个股资讯标签的使用。在 QuantBot / Claude Code 中排查新闻情绪不生效、重新安装 FinBERT、扩充情绪词、触发新闻情绪重算时使用。触发词：新闻情绪、情绪识别、FinBERT、情绪不生效、情绪都是中性、字典法、新闻重算、news sentiment"
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

# 新闻情绪识别（FinBERT）安装与运维技能

QuantMind 的 RSS 新闻情绪识别管线：Huntly 抓取 → celery 每分钟 enrich → 股票/行业/事件标签 + 情绪分 → `news_article_enrichment` 表 → 前端 RSS 面板/个股资讯 Tab 展示利好/利空。

本技能覆盖**情绪层**的安装、验证、重算、排查、词库增强。

## 1. 架构与数据流

```
Huntly (RSS 抓取, 381 源, 41.5 万文章)
  → celery-beat 每 60s: news-enrich-recent (run_enrichment_batch)
  → NewsMatcher (Aho-Corasick + finance_lexicon 5 万词: 股票/行业/事件/情感)
  → sentiment.score() (两级融合)
      字典法 dict_score (finance_lexicon sentiment_pos/neg 词权重差)
      + FinBERT (bardsai/finance-sentiment-zh-base, CPU, conf≥0.55 才融合)
  → 写入 news_article_enrichment 表 (model_version 带 +finbert 后缀表示 FinBERT 生效)
  → 前端:
      /rss-news  NewsPanel: 情绪筛选/利好利空强度排序/统计栏情绪分布条形图
      个股终端「个股资讯」Tab: 每篇利好/利空标签 (stock_news 关联 enrichment)
```

**情绪词库**：finance_lexicon 已从最初的 230 词扩充到 **51,887 词**（sentiment_pos 27,227 / sentiment_neg 24,089 / event 571），来源见第 6 节。

## 6. 情绪词库增强（从 230 词 → 5 万词）

情绪词分两级来源，统一存 finance_lexicon：

### 6.1 现成情感词典（通用，可复现导入）
| 来源 | 词数 | 说明 |
|---|---|---|
| **DLUT 情感本体库** (github.com/yizhanmiao/DLUT-Emotionontology) | 22,001 | 中文情感词汇本体，强度 1-9，极性 1正/2负 |
| **pysenti** (github.com/shibing624/pysenti) | 9,870 | 内置情感词典，连续分数 -7~+7 |
| **NTUSD 台大词典** (github.com/ntunlplab/NTUSD) | 20,485 | 繁体，Big5 编码，需转简体 |

### 6.2 RSS 标题提炼（金融语境，最精准）
用 41.5 万条 Huntly 标题 + enrichment 已有情绪标签，做**共现统计**（对数似然比 LLR 筛选）：
- bearish 标题高频词 → 负向金融情绪词（下跌/暴跌/跌破/违规/立案/警示）
- bullish 标题高频词 → 正向金融情绪词（涨超/涨停/新高/买入/上调）
- 产出 5,705 词，贴合 A股/监管/业绩语境，通用词典覆盖不到的（如"暂停开户""被重锤"）都在这

### 6.3 复现导入
```bash
# 词表已固化: backend/scripts/data/finance_sentiment_lexicon.tsv (51,213 词)
# 导入脚本: backend/scripts/import_sentiment_lexicon.py
# 本地
python3 backend/scripts/import_sentiment_lexicon.py
# 容器
docker exec quantmind python3 /app/backend/scripts/import_sentiment_lexicon.py
```

导入后重载 matcher：
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/news/enrichment/run"  # 触发重算
```

### 6.4 新增情绪词的完整流程
1. 从新数据源提取词（如再跑 RSS 统计）
2. 写入 `backend/scripts/data/finance_sentiment_lexicon.tsv`（`term\tpos|neg\tweight`）
3. 跑 `import_sentiment_lexicon.py` 导入
4. 触发 `/enrichment/run` 或 rebuild 让新词生效

## 2. 情绪不生效 / 全是中性 的排查（最常见）

**症状**：RSS 面板里利好/利空占比极低（<5%），几乎全 neutral，置信度恒 0.3。

**根因**：FinBERT 模型没装上（容器缺 `transformers`），回退纯字典法，而字典法词太少打不出分。

**判断方法**：查 `model_version` 有没有 `+finbert` 后缀。
```bash
docker exec quantmind python3 -c "
import asyncio
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text
async def main():
    async with get_session() as s:
        r = await s.execute(text(\"SELECT sentiment_label, COUNT(*) FROM news_article_enrichment WHERE model_version LIKE '%finbert%' GROUP BY sentiment_label ORDER BY 2 DESC\"))
        print('+finbert 分布:', r.fetchall())
        r = await s.execute(text(\"SELECT COUNT(*) FROM news_article_enrichment WHERE model_version NOT LIKE '%finbert%'\"))
        print('无 finbert 条数:', r.fetchone()[0])
asyncio.run(main())
"
```
- 有 `+finbert` 且有 bullish/bearish 分布 → FinBERT 正常
- 全 neutral / 无 `+finbert` → FinBERT 没生效，按第 3 节处理

## 3. 安装 / 修复 FinBERT

### 3.1 检查 transformers 是否在容器里
```bash
docker exec quantmind python3 -c "import transformers; print(transformers.__version__)"
# celery worker 也要有（enrich/rebuild 在 celery 里跑）
docker exec quantmind-celery python3 -c "import transformers; print(transformers.__version__)"
```

### 3.2 缺的话手动装（临时修复，镜像重建后固化）
```bash
docker exec quantmind pip install --no-cache-dir transformers
docker exec quantmind-celery pip install --no-cache-dir transformers
```

### 3.3 下载/同步 FinBERT 权重（~100MB，离线可用）
```bash
# 触发下载（首次）
docker exec quantmind python3 -c "
from backend.services.api.news import sentiment as s
s._model_ready=False; s._model_failed=False
s._try_load(); print('ready=', s._model_ready)
"
# 权重缓存位置: /root/.cache/huggingface/hub/models--bardsai--finance-sentiment-zh-base
# celery worker 复用（主容器导出 → tar 管道 → celery 导入）
docker exec quantmind tar -C /root/.cache/huggingface/hub -cf - models--bardsai--finance-sentiment-zh-base \
  | docker exec -i quantmind-celery sh -c 'mkdir -p /root/.cache/huggingface/hub && tar -C /root/.cache/huggingface/hub -xf -'
```

### 3.4 验证 FinBERT 可用
```bash
docker exec quantmind-celery python3 -c "
import os
os.environ['HF_HUB_OFFLINE']='1'
from transformers import pipeline
p = pipeline('sentiment-analysis', model='bardsai/finance-sentiment-zh-base', device=-1)
print(p('公司业绩暴雷，股价暴跌')[0])   # 期望 label=negative, score>0.9
"
```

### 3.5 重启 celery worker 让新代码/新依赖生效
```bash
docker restart quantmind-celery
sleep 8
# 确认后台加载成功
docker exec quantmind-celery python3 -c "
from backend.services.api.news import sentiment as s
s._ensure_loading()
import time
for _ in range(40):
    if s.is_available(): break
    time.sleep(1)
print('FinBERT ready:', s.is_available())
"
```

## 4. 触发全量重算（历史情绪重打）

`model_version` 变了（比如补上 `+finbert` 后）需要重算历史才能让旧文章带上情绪。force=true 全量重算 41 万条 Huntly 文章，约 3-4 小时后台跑。

```bash
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

# 全量重算（force=true 覆盖已 enrich 的）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/news/enrichment/rebuild-all?force=true"
# 或增量（跳过 model_version 已是最新的）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/news/enrichment/rebuild-all"

# 查进度
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/news/enrichment/rebuild-progress"
```

> 注意：rebuild 在 celery worker 的后台线程跑，**重启 celery 会中断 rebuild**。重算期间别重启 celery。

## 5. 字典法增强（FinBERT 不可用时的兜底）

情绪是两级融合，FinBERT 置信度 <0.55 或不可用时用字典法。字典词在 `finance_lexicon` 表（kind=sentiment_pos/neg，weight 为强度）。

扩充方式：
1. 改仓库 `backend/scripts/seed_a_share_stocks.py` 的 `_BUILTIN_SENTIMENT_POS/NEG` 常量（已含 2026-08 扩充的 ~80 个词）
2. 重跑 seed：
```bash
docker exec quantmind python3 /app/backend/scripts/seed_a_share_stocks.py
```
3. 或直接 SQL 插入（幂等，先查后插）：
```bash
docker exec quantmind python3 -c "
import asyncio
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text
async def main():
    async with get_session() as s:
        await s.execute(text(\"INSERT INTO finance_lexicon (term, kind, weight, enabled) VALUES ('涨停','sentiment_pos',1.0,true)\"))
        await s.commit()
asyncio.run(main())
"
```
4. 词表改动立即生效（NewsMatcher 每 600s 重载 / 可触发 `/enrichment/run` 重算新文章）

## 6. 情绪数据怎么用（前端已就绪）

| 位置 | 说明 |
|---|---|
| `/rss-news` NewsPanel | 情绪筛选（利好/利空/中性）、利好/利空强度排序、统计栏情绪分布条形图（红利好/绿利空/灰中性） |
| 个股终端 → 个股资讯 Tab | 每篇标题前利好/利空标签（stock_news 按 huntly_page_id join enrichment） |
| `/news/articles` API | `sentiment=bullish/bearish/neutral` 过滤 + `sort=sentiment_bullish/sentiment_bearish` |
| `/news/enrichment/stats` | 当前筛选下的情绪分布计数（前端统计栏数据源） |

## 7. 相关代码文件

- `backend/services/api/news/sentiment.py` — FinBERT 懒加载/后台加载/失败重试（`_RETRY_AFTER` 冷却 300s）
- `backend/services/api/news/enricher.py` — enrich 管线，0.6字典+0.4FinBERT 融合
- `backend/services/api/news/matcher.py` — Aho-Corasick 匹配 + 字典分
- `backend/scripts/seed_a_share_stocks.py` — finance_lexicon 内置词（含情绪词）
- `backend/scripts/import_sentiment_lexicon.py` — **5 万情绪词批量导入脚本（可复现）**
- `backend/scripts/data/finance_sentiment_lexicon.tsv` — **合并情绪词表 51,213 词（RSS提炼+DLUT+pysenti+NTUSD）**
- `backend/services/api/routers/news.py` — /news/* 路由（articles/enrichment/stats/sources）
- `backend/services/api/routers/stock_terminal.py` — /stock-terminal/news 个股资讯（带情绪标签）
- `electron/src/features/news/components/NewsPanel.tsx` — RSS 面板（情绪分布条形图）
- `electron/src/features/stock-terminal/components/tabs/NewsTab.tsx` — 个股资讯情绪标签
- `docker/Dockerfile.oss` + `requirements/ai.txt` — transformers 依赖 + FinBERT 权重预下载

## 8. 相关技能

- **[[quantmind-operations]]** — RSS 新闻对接与分析（文章拉取/过滤/富化统计）
- **[[quantdb-fields]]** — 数据字段口径
