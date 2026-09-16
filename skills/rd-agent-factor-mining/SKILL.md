---
name: rd-agent-factor-mining
description: "RD-Agent A股因子挖掘端到端流水线：环境 preflight → 启动演化 → 轮询完成 → 批量回测评估 → IC/Sharpe 排序 → explain 解读 → export 入库 → Markdown 报告。在 QuantBot / Claude Code 中挖因子时使用，一条命令跑完整流程。触发词：挖因子、因子挖掘、挖新因子、因子演化、RD-Agent、alpha agent、自动挖因子、一键挖因子、因子回测、演化因子、启动因子任务"
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

# RD-Agent 因子挖掘（端到端流水线）

调用 RD-Agent（Alpha Agent）自动挖掘 A 股 alpha 因子，覆盖完整链路：
**preflight → evolve（LLM 演化）→ 轮询 → 批量回测 → 排名 → explain → export → 报告**。

## 0. 环境前置（必须，一分钟先过）

做任何挖掘前，先跑容器内三项健康检查。任一 FAIL 都要先修再挖。

```bash
cd ~/projects/quantmind && python3 scripts/alpha_agent/factor_pipeline.py --check-env
# 期望 3 项全 PASS: conda_shim / litellm_patch / deepseek_key
```

| 检查项 | 作用 | 失败处理 |
|---|---|---|
| `hardware` | 最低 **8 核 / 32GB**。RD-Agent 演化会把 CPU/内存打满，低于此规格直接 412 失败，避免整机卡死 | 换机器或关其他重负载；测试可设 `ALPHA_AGENT_SKIP_HW_LOCK=1`（生产勿开） |
| `conda_shim` | RD-Agent LocalEnv 硬编码 `rdagent4qlib` conda 环境，容器无 conda，靠 shim 映射到容器 python | 确认 `docker/conda-shim` 挂载 `/usr/local/bin/conda:ro` 且文件有 `+x` |
| `litellm_patch` | litellm 1.97 + pydantic 2.13 冲突（`Message is not fully defined`） | 确认 `docker/litellm_sitecustomize.py` 挂载为 `site-packages/sitecustomize.py:ro` |
| `deepseek_key` | 因子挖掘走 DeepSeek 通道（`llm_env.py` 优先级最高） | 更新 `~/projects/quantmind/.env` 的 `DEEPSEEK_API_KEY`，改后必须 `docker compose up -d quantmind` recreate |

> preflight 机制全在管道脚本内置；手动检修环境见文末「常见问题」。

## 1. 一键管线（推荐入口）

```bash
cd ~/projects/quantmind

# 最小示例：一个方向，演化+批量回测+排名（默认报告 /tmp/rd_agent_factor_report.md）
python3 scripts/alpha_agent/factor_pipeline.py --direction "连板高度递减与涨停回封率"

# 全流程：演化 + 回测 + top5 解读 + 最高 |IC| 导出
python3 scripts/alpha_agent/factor_pipeline.py \
  --direction "筹码集中度上行伴随低位换手放大" \
  --universe csi300 --loops 3 \
  --explain-top 5 --export --min-ic 0.03 \
  --out /tmp/factor_report.md

# 只看环境
python3 scripts/alpha_agent/factor_pipeline.py --check-env
```

**参数**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--direction` | 必填 | 挖掘方向/假设，中文优先（见方向建议库） |
| `--universe` | `csi300` | csi300/csi500/csi1000/sse50/gem/star/csi800/all_a |
| `--loops` | 3 | 演化轮数，实际以任务详情为准（可能出现归一值） |
| `--check-env` | off | 只跑环境健康检查 |
| `--no-backtest` | off | 演化完成即停，不批量回测 |
| `--backtest-start` | 2025-01-01 | 回测起始日（到当天） |
| `--backtest-universe` | =universe | 回测股票池 |
| `--explain-top N` | 0 | 对 |IC| 排名前 N 因子调 LLM 解读 |
| `--export` / `--min-ic` | off / 0.0 | 对 |IC|≥min 的最高因子 export 进生产特征库 |
| `--out` | /tmp/rd_agent_factor_report.md | 报告路径 |
| `--show-log` | off | 轮询时打印任务日志 |

**管线阶段**（脚本自动执行）：
1. preflight 三项健康检查
2. `evolve` 启动演化 → 拿 `task_id`
3. 轮询 `tasks/{id}` 直到 `completed`（打印 phase/loop/error）
4. 收集本次 `task_id` 的因子（`metadata.task_id` 过滤）
5. 逐个 `factor/{id}/backtest` 触发 → 轮询全部 completed
6. 按 `|IC|` 排序打印排行榜
7. 对 `--explain-top` 因子 `explain`（LLM 解读，写入报告）
8. `--export` 最高分因子
9. 生成 Markdown 报告到 `--out`

一个方向跑完约 **30–90 分钟**（数据管线 + LLM 演化 + 逐因子回测）。

## 1.5 因子工厂（QuantDB 富字段 × 算子 × 窗口 → 成千上万表达式因子）

R&D-Agent 是「LLM 提假设 → 逐因子回测」；**因子工厂**是「系统性批量衍生」：拿 QuantDB 的几百个数值字段
（`l1_factors` / `features_daily`）做算子 × 窗口 × 二元组合，全量算 IC/ICIR、相关性去重，产出可训练 parquet。
适合「数据面已知、想从海量式子里筛好货」。两者互补，可都跑。

```bash
# 容器内执行（重依赖 duckdb/pandas，QwenPaw 本地 venv 跑不了）
docker exec -w /app quantmind python /app/backend/scripts/factor_factory.py \
  --start-date 2025-09-01 --end-date 2026-08-31 --top-n 200
# 冒烟（小样本，先验证环境）
docker exec -w /app quantmind python /app/backend/scripts/factor_factory.py --smoke
```

- **产物**：`data/quantcustom/6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet`（列：`symbol`(后缀式) + `date` + OHLCV + 因子 float32）、`MANIFEST.csv`、`PROPOSALS.json`。
- **关键参数**：`--windows 5,10,20,60`、`--ops tsrank,tsstd,roc,zscore,delta,decay,slope`、`--cs-ops csrank,cszscore`、`--binary-ops csdiff,csratio,tscorr`、`--top-n 200`、`--pool-factor 3.0`、`--corr-threshold 0.85`、`--jobs 0`（自动并行）、`--compression zstd`、`--out`。
- **只读展示**：`GET /api/v1/alpha-agent/factory-factors`（读 MANIFEST，前端因子库加「工厂」徽标，仅展示不回测/训练操作）。
- **训练侧读取**：`QuantDBFactorReader(mode="CUSTOM")`（`QM_QUANTCUSTOM_DATA_DIR`）。
- 落盘/目录口径见 [[quantdb-data-structure]] 的 `quantcustom` 小节。

## 2. 手动分步（需要精细控制时用 API）

### 认证
```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
```

### 池子 / 类别 / 数据健康
```bash
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/universes"        # 支持的股票池
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/markets"          # 支持的市场
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/factor-categories" # 挖掘类别参考
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/data-summary"      # 数据覆盖（日期/股票数）
```

### 启动演化
```bash
curl -s -X POST "$BASE/api/v1/alpha-agent/evolve" -H "$AUTH" \
  --data-urlencode "market=a_share" \
  --data-urlencode "universe=csi300" \
  --data-urlencode "loop_n=3" \
  --data-urlencode "direction=低换手率高动量" \
  -w "\nHTTP %{http_code}\n"
# 返回 task_id（后续所有步骤用）
```

### 轮询 / 取消
```bash
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/tasks/{task_id}"      # 状态 progress/phase/loop/error
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/tasks/{task_id}/log"  # 实时日志（含失败原因）
curl -s -X POST -H "$AUTH" "$BASE/api/v1/alpha-agent/tasks/{task_id}/cancel"
# 状态机: pending → running → backtesting → completed | failed
```

### 因子回测（注意是 query 参数，不是 form）
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/alpha-agent/factors/{factor_id}/backtest?start_date=2025-01-01&end_date=2026-08-21&universe=csi300&data_source=qlib_bin"
# factors 列表里 ic_value/sharpe_ratio/rank_ic 回测完成后回填；status 变 completed
```

### 解读 / 导出 / 统计
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/alpha-agent/factors/{factor_id}/explain"   # LLM 解读因子逻辑
curl -s -X POST -H "$AUTH" "$BASE/api/v1/alpha-agent/factors/{factor_id}/export"    # 加入生产特征库
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/factors"                               # 全部因子（含本次 task 的）
curl -s -H "$AUTH" "$BASE/api/v1/alpha-agent/stats"                                 # avg_ic/best_sharpe 等
```

## 3. 方向建议库（direction 直接用）

优先挖 78 核心因子集之外的空白区（筹码/微观结构/连板情绪/隔夜/资金流持续性）：

```text
1. 连板情绪承继   连板高度递增与涨停回封率，捕捉题材情绪承接力由弱转强的启动票
2. 筹码分布       筹码集中度上行伴随低位换手放大，获利盘充分消化的突破信号
3. 隔夜/日内背离  隔夜收益与日内收益背离，捕捉大单隔夜布局意图
4. 资金流持续性   主力大单净流入的天数持续性与金额强度共振
5. 下行波动偏度   低下行风险与负偏度修正，挖掘低波动异象的非对称变体
6. 量价微观结构   开盘跳空幅度与量能共振，叠加尾盘动量延续
7. 动量质量       趋势斜率 R2 与收益动量叠加，过滤高噪音动量
8. 波动聚簇修正   波动率自相关的反转信号（高低波动切换）
9. 流动性衰减     换手率衰减速度与跌幅对比
10. 行业相对强度   个股相对行业指数 20 日超额与行业轮动方向一致
```

每批建议跑 1–3 个方向（串行排队），避免队列过载。

## 4. 验收标准（工具完成后自查）

- [ ] preflight 三项 PASS
- [ ] 演化任务 `completed`（非 failed）
- [ ] 有因子入库且完成批量回测（`ic_value` 非空）
- [ ] 排行榜上 |IC| 高、ICIR 明显 > 0 的因子受关注
- [ ] 高分因子完成 `explain`，解读与 direction 假设一致（非噪声）
- [ ] 确认有效的因子已 `export`（日志有导出记录）
- [ ] 报告落盘（默认 /tmp/rd_agent_factor_report.md）

## 5. 常见问题与踩坑

| 现象 | 根因 | 处理 |
|---|---|---|
| `timeout: failed to run command 'python'` + `conda: not found` | 容器无 conda，RD-Agent 需 `rdagent4qlib` env | conda shim 挂载 `/usr/local/bin/conda`；宿主机文件记得 `chmod +x` |
| `Message is not fully defined` / `ChatCompletionReasoningSummaryTextBlock is not defined` | litellm 1.97 + pydantic 2.13 冲突 | `docker/litellm_sitecustomize.py` 挂载为 sitecustomize |
| `Authentication Fails ... api key is invalid` | DEEPSEEK_API_KEY 失效 | 换 key 到 `.env`，`docker compose up -d quantmind` recreate 才生效 |
| 因子 `status=pending` 且 `ic_value=null` | 还没跑回测 | 触发 `backtest`（pipeline `/--no-backtest` 时更是如此） |
| 任务秒 failed | 先看 `tasks/{id}/log` 尾部具体错误 | 常见上面两类，按表修 |
| `engine upstream unavailable`(503) | 回测并发把 engine 挤忙 | 稍等重试；少并行任务 |
| 传 `universe=all_a` 详情显示 `csi300` | 后端对部分池归一 | 以任务详情 universe 为准 |

## 6. 参考文件

- 一键管线：`scripts/alpha_agent/factor_pipeline.py`
- 环境修复：`docker/conda-shim`、`docker/litellm_sitecustomize.py`（compose 挂载固化）
- RD-Agent Runner 入口：`scripts/alpha_agent/run_rd_agent.py`
- 因子工厂：`backend/scripts/factor_factory.py`（+ `backend/scripts/alpha_library_factors.py` 复用算子/写盘）