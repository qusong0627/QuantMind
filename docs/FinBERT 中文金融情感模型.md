# FinBERT 中文金融情感模型

> QuantMind 用 FinBERT 对 RSS 资讯（Huntly 来源）做中文金融情感打分，输出 `bullish` / `bearish` / `neutral` 三类标签与置信度，
> 与本地词典法加权融合后写入 `news_article_enrichment` 表，供新闻资讯页、个股 RSS Tab、研究/回测平台使用。

本文档是 FinBERT 在 QuantMind 项目内的**完整介绍与部署指南**（更聚焦"是什么/在哪/怎么打通"）；
如需深入离线安装/补装 PyTorch 步骤，参考 [RSS情绪识别FinBERT依赖安装说明.md](./RSS情绪识别FinBERT依赖安装说明.md)。

***

## 1. 模型介绍

| 维度       | 详情                                                                                                          |
| -------- | ----------------------------------------------------------------------------------------------------------- |
| 名称       | **FinBERT 中文金融情感模型**                                                                                        |
| 选型       | `bardsai/finance-sentiment-zh-base`（RoBERTa-zh，≈100MB，6 层）                                                  |
| 训练语料     | 中文金融新闻 + 股吧研报混合语料，三分类情感                                                                                     |
| 输出 label | `positive` → **bullish** · `neutral` · `negative` → **bearish**                                             |
| 推理依赖     | `transformers` + `torch`（CPU 镜像默认关闭，GPU 镜像默认开启）                                                             |
| 文件位置（默认） | `<repo>/models/finbert-zh-base/`（容器内 `/app/models/finbert-zh-base`）                                         |
| 部署源码     | [`backend/services/api/news/sentiment.py`](../../backend/services/api/news/sentiment.py)                    |
| 权重下载     | [`backend/scripts/download_finbert.py`](../../backend/scripts/download_finbert.py)                          |
| 调度入口     | Celery `engine.tasks.news_enrich_recent`（每分钟一次）                                                             |
| 融合逻辑     | `0.6 × 词典法 + 0.4 × FinBERT(置信度 ≥ 0.55 时启用)`，详见 [`enricher.py`](../../backend/services/api/news/enricher.py) |
| 生效标记     | `news_article_enrichment.model_version` 字段值包含 `+finbert` 后缀                                                 |

### 1.1 与"词典法"的差异

| <br /> | 词典法（默认）               | FinBERT                       |
| ------ | --------------------- | ----------------------------- |
| 速度     | 微秒级                   | CPU 单条约 30–80ms，GPU 单条约 2–5ms |
| 准确度    | 依赖词表维护，召回低            | 中文金融语境，常见表达"业绩超预期""暴雷"等可识别    |
| 多义词    | 易误判（"利好兑现"被同时命中利好/利空） | 上下文建模，显著优于纯词法                 |
| 资源占用   | 几乎为零                  | 加载约 350MB 内存，CPU 推理可能占满单核     |

### 1.2 项目里的真实使用链路

```
Huntly RSS ─┐
            ├─→ enricher.run_enrichment_batch() ─→ enrich_article()
Huntly SQLite ┘                                       ├─ matcher.match()          (词典法)
                                                      └─ sentiment.score(title)   (FinBERT)
                                                            ↓
                                                  0.6 词法 + 0.4 FinBERT 融合
                                                            ↓
                                              news_article_enrichment (PG)
                                                            ↓
                              ┌────────────────┬─────────────────┬──────────────┐
                              ↓                ↓                 ↓              ↓
              NewsPanel (新闻资讯页)   NewsTab (个股 Tab)   research_service   market_analysis
                              ↓                ↓                 ↓              ↓
                  利好/利空/中性 标签    sentiment_score 展示   个股情绪因子     市场情绪面
```

***

## 2. 部署指南

### 2.1 默认状态（CPU 开源版镜像）

| 项                  | 默认        | 说明                                                     |
| ------------------ | --------- | ------------------------------------------------------ |
| `TORCH_DEVICE`     | `cpu`     | 镜像内安装 torch CPU 版，避免 24GB 完整包                          |
| `NEWS_USE_FINBERT` | `""`（未设置） | 当 `FINBERT_DEVICE<0`（CPU）时**自动关闭** FinBERT，避免 CPU 推理打满 |
| `FINBERT_DEVICE`   | `-1`      | 推理设备，-1=CPU，0=GPU0                                     |
| 词典法降级              | 始终可用      | FinBERT 不可用时情绪仍能产出，但精度偏低                               |

> CPU 镜像默认关闭是为了避免单条 RSS 推理 50ms 导致 celery worker 被打满。
> 启用 FinBERT 的两个条件：① `FINBERT_DEVICE=0`（GPU）或显式 `NEWS_USE_FINBERT=true`；② 权重已下载到 `FINBERT_ZH_MODEL` 指向的目录。

### 2.2 部署步骤

#### A. 在线一键部署（推荐）

```bash
# 1. 拉代码
cd /opt/quantmind
sudo git pull origin master

# 2. 一键启动，默认带 torch CPU + FinBERT 关闭
sudo docker compose up -d

# 3. （可选）下载 FinBERT 权重到容器 /app/models/finbert-zh-base
sudo docker exec quantmind python3 /app/backend/scripts/download_finbert.py
```

#### B. GPU 部署（启用 FinBERT 推理）

```bash
# 1. 重建带 CUDA 完整版 torch 的镜像（约 24GB）
sudo TORCH_DEVICE=gpu \
  PIP_INDEX_URL="https://mirrors.cloud.tencent.com/pypi/simple/" \
  PIP_TRUSTED_HOST="mirrors.cloud.tencent.com" \
  docker compose build --pull=false \
    --build-arg PIP_INDEX_URL="https://mirrors.cloud.tencent.com/pypi/simple/" \
    --build-arg PIP_TRUSTED_HOST="mirrors.cloud.tencent.com" \
    quantmind

sudo docker compose up -d

# 2. 开启 FinBERT（GPU 环境默认就开）
sudo docker exec quantmind \
  sh -c 'echo "export NEWS_USE_FINBERT=true" >> /etc/profile.d/quantmind.sh'

# 3. 拉取权重
sudo docker exec quantmind python3 /app/backend/scripts/download_finbert.py

# 4. 重启 celery 让环境变量生效
sudo docker compose restart celery-worker quantmind
```

#### C. 离线包部署

```bash
# 1. 先检查包内镜像是否已带 torch
sudo docker run --rm quantmind-oss:latest python3 -c "import torch; print(torch.__version__)"

# 2a. 带 torch：直接运行权重下载脚本
sudo docker exec quantmind python3 /app/backend/scripts/download_finbert.py

# 2b. 不带 torch：在线重建带 torch 的镜像（见 A）或临时补装
sudo docker exec quantmind pip install "torch==2.9.1+cpu" \
  --index-url https://download.pytorch.org/whl/cpu
```

### 2.3 关键环境变量

| 变量                    | 默认                                  | 作用                                  |
| --------------------- | ----------------------------------- | ----------------------------------- |
| `FINBERT_ZH_MODEL`    | `bardsai/finance-sentiment-zh-base` | 模型路径（支持本地目录或 HF repo）               |
| `FINBERT_DEVICE`      | `-1`                                | 推理设备（-1=CPU，0=GPU0）                 |
| `NEWS_USE_FINBERT`    | `""`                                | 强制开启/关闭，缺省按 `FINBERT_DEVICE≥0` 自动判断 |
| `FINBERT_RETRY_AFTER` | `300`                               | 加载失败后冷却重试间隔（秒）                      |
| `FINBERT_BATCH`       | `96`                                | 全量重建时的批量推理窗口                        |
| `MODELSCOPE_ENDPOINT` | `https://www.modelscope.cn`         | 国内下载主源，可走内网代理                       |
| `TORCH_DEVICE`        | `cpu`                               | 镜像构建时是否装 torch / 装 CPU 还是完整版        |

***

## 3. 验证 FinBERT 是否生效

### 3.1 一行命令查健康状态

```bash
curl -s http://<api-host>:8000/api/v1/news/enrichment/finbert-status | python3 -m json.tool
```

返回示例（GPU 镜像 + 权重已就绪）：

```json
{
  "available": true,
  "use_finbert": true,
  "model": "bardsai/finance-sentiment-zh-base",
  "device": 0,
  "sample_inference": { "label": "bullish", "confidence": 0.8923 },
  "db_total_24h": 137,
  "db_finbert_ratio_24h": 1.0,
  "tip": "FinBERT 已就绪，可在新闻资讯/标签管理中观察带 +finbert 后缀的 model_version。"
}
```

### 3.2 查数据库标记

```bash
sudo docker exec quantmind-db psql -U quantmind -d quantmind -c \
  "SELECT model_version, COUNT(*) FROM news_article_enrichment GROUP BY model_version;"
```

`model_version` 含 `+finbert` 后缀 → 该批 enrich 已使用 FinBERT。

### 3.3 容器内实跑

```bash
sudo docker exec quantmind python3 -c "
from backend.services.api.news.sentiment import score
print(score('公司发布重大利好公告，净利润大幅增长'))
"
# → ('bullish', 0.89) 表示 FinBERT 工作正常
```

***

## 4. 前后端打通情况

> **结论：完全打通。** 后端每次 enrich 都跑 FinBERT（若启用），前端新闻资讯页 / 个股 Tab 实时展示情感标签。

### 4.1 后端链路（已实现）

| 环节     | 文件                                                                           | 状态                                 |
| ------ | ---------------------------------------------------------------------------- | ---------------------------------- |
| 权重下载   | `backend/scripts/download_finbert.py`                                        | ✅ ModelScope → hf-mirror → HF 三源回退 |
| 模型懒加载  | `backend/services/api/news/sentiment.py`                                     | ✅ 失败冷却重试，CPU/GPU 自动适配              |
| 调度入口   | `backend/services/engine/tasks/celery_tasks.py:702-715`                      | ✅ `news_enrich_recent_task` 每分钟    |
| 富化融合   | `backend/services/api/news/enricher.py:121-149`                              | ✅ 0.6 词法 + 0.4 FinBERT，置信度阈值 0.55  |
| 持久化    | `news_article_enrichment` 表（PG）                                              | ✅ 幂等 ON CONFLICT DO UPDATE         |
| API 暴露 | `routers/news.py:1647-` `/enrichment/run`、`/rebuild-all`、`/rebuild-progress` | ✅                                  |
| 健康检查   | `routers/news.py` `/enrichment/finbert-status`                               | ✅ 新增，含 DB 真实占比                     |

### 4.2 前端消费（已实现）

| 位置            | 展示                              | 文件                                                                                               |
| ------------- | ------------------------------- | ------------------------------------------------------------------------------------------------ |
| 新闻资讯页列表       | 利好/利空/中性 Tag + score            | [`NewsPanel.tsx`](../../electron/src/features/news/components/NewsPanel.tsx#L972-L1139)          |
| 个股 RSS Tab    | 利好/利空 icon + 强度                 | [`NewsTab.tsx`](../../electron/src/features/stock-terminal/components/tabs/NewsTab.tsx#L98-L110) |
| 标签管理          | 本页底部新增"FinBERT 模型介绍"一节 + 健康状态卡片 | [`AdminTagManagement.tsx`](../../electron/src/features/admin/components/AdminTagManagement.tsx)  |
| Skills Center | `news-sentiment-finbert` 技能     | `prompts.generated.ts:128-134`                                                                   |

### 4.3 端到端验证脚本

```bash
# 1. 后端发一条 enrich 请求
curl -X POST 'http://<api-host>:8000/api/v1/news/enrichment/run?limit=10'

# 2. 看 FinBERT 健康
curl -s 'http://<api-host>:8000/api/v1/news/enrichment/finbert-status' | jq

# 3. 前端打开「新闻资讯」或「标签管理」页：
#    - 列表里新闻条目若带"利好/利空"彩色 Tag = 已消费
#    - 「标签管理」页底部"FinBERT 模型健康状态"卡片显示"已就绪"= 链路正常
```

***

## 5. 常见问题

| 现象                                          | 原因                               | 处理                                                                       |
| ------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| `available:false, use_finbert:true`         | 权重未下载或 torch 缺失                  | `docker exec quantmind python3 /app/backend/scripts/download_finbert.py` |
| `available:false, use_finbert:false`        | CPU 镜像默认关闭，符合预期                  | GPU 部署或显式 `NEWS_USE_FINBERT=true`                                        |
| `db_finbert_ratio_24h=0` 但 `available:true` | 数据库里是历史无 +finbert 的旧记录           | 触发 `POST /news/enrichment/rebuild-all?force=true` 全量重算                   |
| 全是 neutral                                  | 词法分近 0 + FinBERT conf<0.55 被压成中性 | 检查 model 路径、是否完整下载了 6 个文件                                                |
| 加载日志反复刷 `FinBERT 加载失败…300s 后重试`             | 镜像无 torch                        | 参考 [RSS情绪识别FinBERT依赖安装说明.md](./RSS情绪识别FinBERT依赖安装说明.md) 离线补装             |

***

## 6. 关联文件

- 后端：

  - [`backend/services/api/news/sentiment.py`](../../backend/services/api/news/sentiment.py) — FinBERT 加载与推理

  - [`backend/services/api/news/enricher.py`](../../backend/services/api/news/enricher.py) — 情感融合 + 持久化

  - [`backend/scripts/download_finbert.py`](../../backend/scripts/download_finbert.py) — 权重下载

  - [`backend/services/engine/tasks/celery_tasks.py`](../../backend/services/engine/tasks/celery_tasks.py) — 调度入口

  - [`backend/services/api/routers/news.py`](../../backend/services/api/routers/news.py) — `/enrichment/*` API

- 前端：

  - [`electron/src/features/news/components/NewsPanel.tsx`](../../electron/src/features/news/components/NewsPanel.tsx) — 列表展示

  - [`electron/src/features/stock-terminal/components/tabs/NewsTab.tsx`](../../electron/src/features/stock-terminal/components/tabs/NewsTab.tsx) — 个股展示

  - [`electron/src/features/admin/components/AdminTagManagement.tsx`](../../electron/src/features/admin/components/AdminTagManagement.tsx) — 标签管理 + FinBERT 介绍与健康卡片

  - [`electron/src/features/news/services/newsService.ts`](../../electron/src/features/news/services/newsService.ts) — API 客户端

- 文档：

  - [RSS情绪识别FinBERT依赖安装说明.md](./RSS情绪识别FinBERT依赖安装说明.md) — 离线 PyTorch 补装

  - [QuantMind2.0功能指南.md](./QuantMind2.0功能指南.md) — 总览

