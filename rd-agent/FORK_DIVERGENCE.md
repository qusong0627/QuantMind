# 本地补丁登记（FORK_DIVERGENCE）

> 批次 0 交付物 ｜ 核实日期 2026-10-07 ｜ 与 `MAINTENANCE_PLAN.md` 配套使用
> **本文件是上游同步的唯一依据**：从上游取用任何改动前，先查这里。

---

## 1. 基线与方法

| 项 | 值 |
|---|---|
| 基线 commit | **`6762f84f9bc0`**（2026-08-04，`docs: add Agent² RL-Bench preprint`） |
| 判定规则 | 本地 `rdagent/` 与基线树比对，**凡不同者即本地补丁** |
| 取文件 | `gh api repos/microsoft/RD-Agent/contents/<path>?ref=6762f84f9bc0` |
| 取整树 | `gh api repos/microsoft/RD-Agent/tarball/6762f84f9bc0` |

**基线可靠性已用 5 个样本验证**（4 个已知补丁均「不同」，1 个已知上游漂移
`knowledge_management.py` 的 pickle **0 行差异**）。旁证：`rdagent/core/serialization.py`
在该 ref 返回 404 —— 签名 pickle 是 9 月才引入的。

**复现方法**：
```bash
BASE=/tmp/rda-base/microsoft-RD-Agent-6762f84
LOC=~/projects/quantmind/rd-agent
diff -rq "$BASE" "$LOC" | grep -v '__pycache__\|/build/\|egg-info'
```

**已排除的捷径**：mtime 分不开——527 个 `.py` 全是 `2026-08-12 10:14`，
补丁在快照生成前就已烙入。别再试。

---

## 2. 核定结果

| 类别 | 数量 |
|---|---|
| 内容不同 | 23 |
| ├ 其中换行符噪声（非补丁） | 2 |
| └ **实质本地补丁** | **21**（批次 0 核定 19 + 2026-10-07 embedding 修复新增 2） |
| 本地新增（文件/目录） | 9（含 1 个新 `.py`、若干构建产物与运行期数据） |
| 本地删除 | 1 |

---

## 3. 实质本地补丁（21 处）

### A1 · 去掉 conda 与 Docker（4 处）— **承载"能跑起来"**

| 文件 | 改了什么 |
|---|---|
| `rdagent/components/coder/factor_coder/config.py` | `CondaConf` → `LocalConf(bin_path="", retry_count=0, default_entry="python -m rdagent.app.cli")`；`enable_cache` 强制 `False` |
| `rdagent/scenarios/qlib/experiment/workspace.py` | 新增 `_run_cmd_in_current_env()`，`qrun` 走 `subprocess.run(shell=True)`；上游的 `env_type == "docker"/"conda"` 双分支**已不存在** |
| `rdagent/scenarios/qlib/experiment/utils.py` | `generate_data_folder_from_qlib()` 的**唯一调用点被注释掉**，改为早退返回硬编码描述 |
| `rdagent/utils/env.py` | `live_output: True → False`；PATH 增加硬编码 `/home/administrator/.local/bin/` |

**为什么不能加层**：这是把执行后端整体换掉，没有相应的配置槽位。
**注意**：`utils.py` 那处使 `generate_data_folder_from_qlib()` 成了死代码，
连带让函数内 3 个硬 assert（`utils.py:24-29`）永不触发。

### A2 · LLM 通道（3 处）— ✅ **假 embedding 已修复**（见 §5）

原 A2 是一处**危险补丁**：`create_embedding()` 被整体替换为确定性伪随机向量
（`sha256(文本)` 做种子 → `random.Random` → 1536 维高斯），注释称
`DeepSeek has no embedding model`。**2026-10-07 已移除并修复**，现为 3 处补丁：

| 文件 | 改了什么 | 备注 |
|---|---|---|
| `rdagent/oai/backend/base.py` | `create_embedding()` **还原为上游形状**（委托 `_try_create_chat_completion_or_embedding`）；新增可覆盖的 `_embedding_channel_ready()` 前置检查 | 与上游 `6762f84f` 的 `base.py:493-503` 逐行一致 |
| `rdagent/oai/backend/litellm.py` | `_create_embedding_inner_function` 把 `api_base`/`api_key` **转发给 `litellm.embedding()`**；实现 `_embedding_channel_ready()`；`LITELLM_SETTINGS` 快照打日志前经 `_redact_settings()` 脱敏 | 前两项：**`api_base`/`api_key` 缺失是整条通道断掉的根因**（不传时 litellm 只读全局 `OPENAI_*`）。第三项修上游的密钥泄漏，见下方 ⚠️ |
| `rdagent/oai/utils/embedding.py` | 新增 `resolve_embedding_channel()` + `_has_known_provider_prefix()`；补 `import os` | 配置解析的唯一出处；独立可 import/可测（铁律三） |

> ⚠️ **上游密钥泄漏（2026-10-07 修）**：`LiteLLMAPIBackend.__init__` 原样打印整个
> `LITELLM_SETTINGS`（`logger.info(f"{LITELLM_SETTINGS}")` + `log_object(model_dump())`
> 各一次），把 `openai_api_key` **明文**写进 stdout 与落盘 artifact。
> 这是**基线自带**的（`6762f84f` 逐字相同），不是本地引入。
> 实测：一次探针在 `/app/log/<session>/LITELLM_SETTINGS/<pid>/*.pkl` 留下 13 份含明文 key 的
> 快照，涉及真实生效的讯飞 MaaS 凭证。现改为脱敏后打印（模型/端点/重试策略等排查信息全保留，
> 只有 `*_key` 变 `***`），`backend/tests/test_embedding_config.py` 有端到端用例锁死。

**为什么 `_embedding_channel_ready` 必须在重试循环之外**：`_try_create_chat_completion_or_embedding`
会 **catch 所有异常并重试**（`max_retry=30` × `retry_wait_seconds=5`）。把「未配置」
的报错放在内部函数里，清晰的信息会被 ~150s 的退避吞掉。实测：修复前反例卡死超时，
修复后 **4.3s 内抛 `ValueError`**（全是 Python 启动开销）。

**配置优先级（刻意的）**：`EMBEDDING_MODEL`/`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY`
（标准环境变量）**优先于** settings 的 `LITELLM_EMBEDDING_*`。理由有二：
① 上游 `app/utils/health_check.py` 与 QuantMind `.env`/`docker-compose.yml` 用的
就是这组裸名字；② CLI / skill 路径不经过 QuantMind 的 env 构造，只有直读 env
才能让容器与 CLI 两条路都通。

**`openai/` 前缀是必需的，不是风格问题**：litellm 会把 `BAAI/bge-m3` 解析成
provider=`BAAI` 并抛 `BadRequestError: LLM Provider NOT provided`。实测确认：
`BAAI/bge-m3` 失败、`openai/BAAI/bge-m3` 成功（dim=1024）。因此当 `api_base`
非空且 model 无 litellm 认识的 provider 前缀时自动补 `openai/`。

### A3 · 打包与包结构（2 处）

| 文件 | 改了什么 |
|---|---|
| `pyproject.toml` | `[tool.setuptools.packages.find]` + `namespaces = true` + package-data globs |
| `rdagent/__init__.py` | **新增文件**（上游无，PEP 420 命名空间包）。mtime `2026-08-21` = vendoring 当天 |

### A4 · 依赖与镜像（7 处）

| 文件 | 改了什么 |
|---|---|
| `requirements.txt` | `litellm>=1.73` → `>=1.73,<1.98`（附长注释指向 `docker/litellm_sitecustomize.py`）；**`pydantic-ai-slim[...]==1.66.0` 的版本钉被去掉** |
| `requirements/lint.txt` | `ruff==0.15.12` → `ruff`（**钉被去掉**） |
| 5 个 Dockerfile<br>（qlib / data_science sing / kaggle DS / kaggle kaggle / kaggle mle_bench） | `FROM` 换成国内镜像 `docker.1ms.run/...`；插入 `sed` 把 apt 源改成 `mirrors.aliyun.com` |

**注意**：两处**去掉版本钉**是反向操作（降低可复现性），与新增的 litellm 上界方向相反。
**镜像源**是环境特定的，换网络环境或镜像站挂掉会失败；对本地非 Docker 路径无影响。

### A5 · 静默降级兜底（2 处）— ⚠️

| 文件 | 改了什么 |
|---|---|
| `rdagent/components/workflow/rd_loop.py` | 基础因子校验失败**不再阻断**加载 |
| `rdagent/scenarios/shared/get_runtime_info.py` | 正则匹配失败时不再抛异常，改返回 `{"python_version": "unknown", ...}` 桩数据 |

**共性风险**：两处都是「失败 → 假数据 / 继续跑」。上游是 fail-fast，本地改成了 fail-silent。
`get_runtime_info` 的结果若进入 prompt，就是往模型上下文里灌假信息。

### A6 · CI 与文档（4 处）

| 文件 | 改了什么 |
|---|---|
| `.github/workflows/ci.yml` | 删除 `fail-fast: false` |
| `.github/workflows/pr.yml` | `node-version: '22'` → `'16'`（降级） |
| `README.md` | 删除 25 行（上游 Agent² RL-Bench 宣传段） |
| `rdagent/scenarios/rl/autorl_bench/README.md` | 8 行实质差异 |

> 本仓库的 GitHub Actions 早已不在用量内（vendored），A6 属可忽略项。

---

## 4. 换行符噪声（登记但不算补丁，2 处）

| 文件 | 实情 |
|---|---|
| `SUPPORT.md` | CRLF → LF，**实质差异 0 行** |
| `docs/_static/RD2bench.json` | CRLF → LF，**实质差异 0 行** |

复核方法：`diff --strip-trailing-cr <基线> <本地> | grep -c '^[<>]'` → 0。

---

## 5. ★ 批次 1 的红灯：假 embedding 会静默污染记忆层 —— ✅ 已排除（2026-10-07）

> **状态：已修复并实测。** 下方诊断保留作为记录与回归依据；
> 修复内容见 §3 A2，验收见本节末尾「修复验收」。

**事实链（已逐段核实）**：
```
evolving_agent.py:151        self.rag.query(evo, self.evolving_trace)
  → CoSTEERRAGStrategyV2.query              (knowledge_management.py:431)
  → graph_query_by_content                  (knowledge_management.py:932)
  → graph.query_by_content                  (graph.py:356)
  → semantic_search                         (graph.py:402)
  → vector_base.search                      (graph.py:306)
  → APIBackend().create_embedding           → **本地：伪随机向量**
```

（链路由 grep 追踪确认；`CoSTEERRAGStrategyV2.query` 内部 431–500 行未逐行通读。）

**后果**：`create_embedding` 返回的是 SHA256 播种的高斯噪声。随机 1536 维向量的余弦相似度≈0，因此：

1. **错误节点永不归并**：`get_node_by_content`（`graph.py:179-192`）用
   `similarity_threshold=0.999` 做语义查重，被 `knowledge_management.py:760/909`
   用于错误节点匹配。相似度≈0 时永远匹配不上 → 同一类报错各自成节点，图谱里堆满重复
2. **相似节点检索返回任意节点**：`query_by_content`（`graph.py:356`）以
   `similarity_threshold=0.0` 调 `semantic_search`，噪声向量的相似度在 0 附近正负抖动
   → 命中谁基本随机，这些节点经 `query_by_node` 扩展后进入 LLM 上下文

> **订正（2026-10-07）**：本节初版把第 1 条写成「`graph.py:192` 的 0.999 去重判定是知识库
> 唯一的去重，所以近重复节点会持续累积」——**归因错了**。
> `UndirectedGraph.add_node`（`graph.py:139-146`）里那段插入期语义去重
> （`same_node_threshold=0.95`）**是被上游自己注释掉的**（与基线 `6762f84f` 比对
> **0 行差异**，非本地补丁），所以：
>
> ```
> # same_node = self.semantic_search(node=node.content, similarity_threshold=same_node_threshold, topk_k=1)
> # if len(same_node):
> #     node = same_node[0]
> # else:
> node.create_embedding()          # ← 这段永远执行，去重分支不存在
> ```
>
> 即**近重复节点累积是上游行为，与 embedding 真假无关**，修好 embedding 也不会改善。
> 这一点直接影响批次 1b 的验收：**「KB 文件在增长」连弱证据都算不上**，必须看检索相关性。

**失效模式是最坏的那种**：不报错、日志正常、知识库文件照常增长，
但注入 prompt 的"经验"与当前任务无关。

**因此 `MAINTENANCE_PLAN.md` 批次 1 的"零代码、半天"结论作废**——
必须先解决 embedding 通道，否则开记忆等于往上下文里灌噪声。

### 修复验收（2026-10-07）

方案选 **(a) 与 (b) 同一条代码路径**：远端供应商与本地模型只在 `base_url`/`model`
上不同，代码无分叉。这消除了「选型」本身——(c) 不再需要。

**量化的修复效果**（同一组中文因子名，余弦相似度）：

| 因子对 | 修复前（伪随机） | 修复后（bge-m3） |
|---|---|---|
| 动量 ↔ 波动率 | 0.0354 | **0.72** |
| 动量 ↔ 换手率 | 0.0303 | **0.615** |
| 波动率 ↔ 换手率 | −0.0415 | **0.68** |

非对角项从 ≈0 变成有语义区分度，这正是 `graph.py:192` 的 0.999 去重判定与
`graph.py:402` 相似节点检索能工作的前提。

**已实测的路径**：
- 远端 SiliconFlow `BAAI/bge-m3`（dim=1024）— 容器内 `.env` 凭证，此前从未被消费
- 本地 ollama `bge-m3` — 宿主侧 OpenAI 兼容端点可用，且**向量与 SiliconFlow 一致**
  （`v0=-0.06405` 两边相同），意味着知识库可在两种 provider 间平移而不破坏向量空间
- 未配置时**立即**抛 `ValueError`（4.3s，含 Python 启动），不再静默降级

**尚未解决**：容器访问不到宿主 ollama——`ollama` 只绑 `127.0.0.1:11434`，而
`quantmind` 容器在自定义 bridge 网络（`quantmind_quantmind-net`）里。要用本地模型
跑容器路径，需 `OLLAMA_HOST=0.0.0.0` + 网关地址，或把 embedding 服务放进 compose 网络。
**宿主 CLI 路径不受此限**，现在就能用。

**回归防线**：`backend/tests/test_embedding_config.py`（17 例，含真实连通性冒烟——
纯 mock 测不出「配置齐全但通道不通」这类静默失效，而那正是本次要根除的模式）。

### 批次 1b · 打开记忆（2026-10-07）

**先纠正一个默认认知**：记忆并非「关着」——`CoSTEER/__init__.py:28-29` 的
`with_knowledge` / `knowledge_self_gen` 默认都是 `True`，所以**单次任务内**一直在
生成与检索经验。缺的只是两个路径设置，导致 `dump_knowledge_base()` 只打一行
warning 就跳过（`knowledge_management.py:83`）→ **跨任务不累积**，跑完即丢。

**实情是「失忆」，不是「没开」。** 这也是为什么伪 embedding 之前就在污染上下文，
只是污染范围被限制在单次任务内。

环境变量名（容器内实测，非照抄文档）：

| 变量 | 字段 | 语义 |
|---|---|---|
| `FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH` | `knowledge_base_path` | 构造期**读**一次 |
| `FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH` | `new_knowledge_base_path` | 每演化步 load→generate→dump **读写** |
| `FACTOR_CoSTEER_ENABLE_FILELOCK` / `_FILELOCK_PATH` | `enable_filelock` / `filelock_path` | 并发任务串行化 |

前缀是 `FACTOR_CoSTEER_` 而非基类的 `CoSTEER_`——因为
`factor_coder/__init__.py:19-23` 把 `FACTOR_COSTEER_SETTINGS` 交给 `CoSTEER.__init__`。

**两个陷阱的处置**：
- *陷阱 1（cwd 静默加载）*：`CoSTEERKnowledgeBaseV2.__init__`（`knowledge_management.py:857`）
  把 `path` 参数**完全忽略**，硬编码 `Path.cwd()/"graph.pkl"`。设了读路径就不会走到这个
  分支；但**只设 NEW_ 不设读路径就会**。处置：`kb_env.py` 用 `_require_absolute()`
  把「相对路径」从静默失败改成**立即 `ValueError`**（子进程 cwd 是每任务独立的
  task_log_dir，相对路径会把 KB 落进任务日志、下个任务读不到）。
- *陷阱 2（裸 pickle）*：默认落 `/data/rd_agent_kb/`（compose 持久卷，非共享/第三方可写）。
  签名校验的定点取用见 §5 所述的 #1471，排在本地回归跑通之后。

**验收（本批次的核心）**：不看「KB 文件在增长」，看检索相关性。
`backend/tests/test_kb_retrieval.py`（4 例，容器内跑，真实 bge-m3）：

| 用例 | 断言 |
|---|---|
| 最近邻排序 | 问「因子全是空值」→ 命中 NaN 那条经验 |
| 不同查询不同结果 | 三条不同查询各自命中对应经验（伪向量做不到） |
| pickle 往返 | 落盘再读回，检索结果不变 |
| 通道缺失 | `_embedding_channel_ready()` 返回 False + 可操作理由 |

**首个 `graph.pkl` 尚未产生**——需要一次真实挖掘任务才会写入。本文件所列为
受控种子图谱上的验收；真实积累度要在首次任务后回看。

---

## 6. 版本控制缺口：30 个文件未受跟踪（已在咬）

`git ls-files rd-agent` = **891**，磁盘实有 **921**。差的 30 个文件**不是疏忽，是 `.gitignore` 吃掉**——
RD-Agent **自带** `.gitignore`，vendoring 时 `git add rd-agent/` 让这些规则对整个子树生效：

| 被吃掉 | 规则 |
|---|---|
| `rdagent/scenarios/rl/env/**`（含 `__init__.py`、`conf.py`） | `rd-agent/.gitignore:5` → `env/` |
| **`scripts/rd_agent_quantmind_hook.py`**（287 行，QuantMind 集成钩子，**纯本地文件**） | `rd-agent/.gitignore:190` → `scripts/` |
| `.env.example` | `rd-agent/.gitignore:116` → `.env*` |
| `factor_data_template/daily_pv_all.h5`、`daily_pv_debug.h5` | `rd-agent/.gitignore:147` → `*.h5` |
| `test/utils/env_tpl/**` | `rd-agent/.gitignore:167` → `env_tpl` |
| `rdagent/scenarios/finetune/env/**`（6 个） | 同上 `env/` |

**已经在咬**：`deploy/portable/build/clean-src/rd-agent/` 文件数 = **891** = 受控文件数，
即**它是 git 导出的**。实测其中：
- `scripts/` **不存在**
- `rdagent/scenarios/rl/env/` **不存在**
- `.env.example` **不存在**

**影响面（已核）**：
- 那个 QuantMind 钩子**无任何引用**（全仓 grep 0 命中）→ 目前是死文件，丢失无实际后果
- `rl/env/**` 只被 RL 场景用，因子挖掘（qlib 场景）不受影响
- `daily_pv_*.h5`：**目前也无实际后果**——`generate_data_folder_from_qlib()` 的唯一调用点
  已被本地补丁注释掉（§3 A1），函数内那 3 个硬 assert 永不触发；
  `git_ignore_folder/factor_implementation_source_data*` 在磁盘上确实不存在

**但打包会丢**：上游打包靠 setuptools-scm file finder（`git 跟踪到的文件 = 包内容`）
→ 这类文件**不会进 wheel**。新增文件务必 `git add`。

**处置建议（需决策，未执行）**：对上述路径在 `rd-agent/.gitignore` 或 quantmind 根
`.gitignore` 加白名单（`!rd-agent/scripts/`、`!rd-agent/.env.example`、`!rd-agent/**/env/`）。
**注意**：`.env` 必须继续排除，只放 `.env.example`。

---

## 7. 本地新增 / 删除（其余）

**本地新增**：`MAINTENANCE_PLAN.md`、`FORK_DIVERGENCE.md`（本文件）、`rdagent/__init__.py`（§3 A3）、
`build/`、`rdagent.egg-info/`、`.env`、`factor_data_template/{logs,workspace_cache}`（运行期产物）

**本地删除**：`web/src/assets/playground-images/loop-loading.gif`（上游有，本地无）

---

## 8. 未验证项（不要当已知事实用）

| 项 | 说明 |
|---|---|
| `CoSTEERRAGStrategyV2.query` 内部 | 431–500 行未逐行读，链路靠 grep 追踪 |
| ~~DeepSeek 无 embedding 接口~~ | ✅ **2026-10-07 已独立验证**：`POST /v1/embeddings` 与 `/embeddings` 均返回 **404**。本地补丁注释属实——但结论应是「换个 embedding 通道」，而非「造伪向量」 |
| 因子 coder 实际读的数据从哪来 | `data_folder` 默认指向不存在的路径，生成函数已死 → 大概率由 QuantMind 容器 env 覆盖，**未确认** |
| `.h5` 在便携包中丢失的实际后果 | 目前判断"无实际后果"，但依赖 §3 A1 的本地补丁持续存在 |

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-07 | 初版。基线 `6762f84f`；19 处实质补丁 + 30 个未受跟踪文件；发现假 embedding 阻断批次 1 |
| 2026-10-07 | **A2 假 embedding 已修复**（`base.py` 还原上游 + `litellm.py` 转发凭证 + `embedding.py` 新增解析）。补丁数 19 → 21；§5 转为已排除并附量化验收；DeepSeek 无 embedding 接口已独立验证（404） |
| 2026-10-07 | **批次 1b 打开记忆**：新增 `rd_agent/kb_env.py`（拒绝相对路径）+ launcher 接线 + 4 例真实检索验收。§5 新增 1b 小节，并**订正**初版的一处错误归因——插入期语义去重是上游自己注释掉的（与基线 0 行差异），近重复累积与 embedding 真假无关 |
| 2026-10-07 | **1a/1b 代码审查修复**：① HIGH —— 用户级 embedding 配置在 `launcher.py` 断链（`llm_env_overrides()` 写出 `EMBEDDING_*`，唯一消费者没读，界面显示"已保存"而挖掘始终用容器级 .env），已接线 + 端到端回归测试；② `embedding_channel_is_explicit()` 修掉 `LLM_SETTINGS.embedding_model` 非空默认值导致的守卫恒真；③ 订正 §3 三处计数（19→21、A2 4→3，按基线全树重算核对）；④ 前端保存改条件提交、请求/响应日志脱敏（密钥不再进控制台）。测试 31 → 48。**另修一条基线自带的密钥泄漏**：`LiteLLMAPIBackend.__init__` 明文打印 `LITELLM_SETTINGS`（含 `openai_api_key`）到 stdout 与落盘 `.pkl`，见 §3 A2 的 ⚠️ 注 |
