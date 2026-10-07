# RD-Agent fork 维护与升级规划

> 起草：2026-10-07 ｜ 适用范围：`QuantMind/rd-agent/`（vendored 副本）
> 状态：**待评审**——本文件只描述计划，不含任何代码改动。

---

## 0. 先读：本规划与最初设想的三处出入

起草过程中核对了本地副本的实际状态，有三处与"fork 一个上游仓库来维护"的默认设想不符，
规划据此改写。**这三条决定了后面所有批次的顺序。**

1. **这里不是一个独立 fork**：`rd-agent/` 已被 vendored 进 QuantMind 单仓库
   （`git ls-files rd-agent` = 891 文件；`git rev-parse --show-toplevel` 指向
   `/home/zbox/projects/quantmind`；目录内无 `.git`）。**没有 `upstream` remote 可以 rebase。**
2. **本地副本已被改造过**：不是上游快照，而是 ~2026-08-12 的快照 **+ 若干承载
   "在本环境能跑起来"的补丁**（见 §2）。直接覆盖成上游版本会让挖矿跑不动。
3. **没有补丁登记**：相对上游 v1.0.0 有 **55 个文件内容不同**，但仓库里没有任何记录
   区分「我们的补丁」与「上游这六周的新提交」。**这是当前最大的单点风险，
   也是本规划的第一个批次。**

---

## 1. 现状核实（本规划的事实基础）

| 事实 | 证据 | 影响 |
|---|---|---|
| vendored 进 QuantMind 单仓库 | `git ls-files rd-agent` = 891；无嵌套 `.git` | 无 remote 可 rebase，"同步上游" = 手工三方比对 |
| 快照时点 ≈ 2026-08-12 | 本地 527 个 `.py` 的 mtime 均为 2026-08-12 10:14 | 落后上游 v1.0.0（2026-09-23）约 6 周 |
| **基线 commit 已锁定 = `6762f84f`（2026-08-04）** | 用 5 个已知样本验证，全部分类正确：4 个已知补丁均「不同」，1 个已知上游漂移（`knowledge_management.py` 的 pickle）**0 行差异**；且 `core/serialization.py` 在该 ref 返回 404（9 月才引入） | **批次 0 由「逐条人工判定」降为机械 diff** |
| 55 文件差异、10 文件单边存在 | `diff -rq rd-agent/rdagent <上游 v1.0.0>/rdagent` | 无归属登记 → 不敢动 |
| 上游**整体放缓，量化场景基本冻结** | main 月提交数 2026-06 / 07 / 10 均为 0；`scenarios/qlib` 近 6 个月**仅 1 条**提交；`components/coder/factor_coder` 近 12 个月 1 条（末次 2025-11-03） | **冻结的代价比预期低，且在继续降** |
| 上游近期唯一活跃轴 = CI 钉版 + **安全加固** | #1497（server UI API 鉴权）、#1469（执行 / 归档加固）、#1471（反序列化前校验）、v1.0.0 发版 2026-09-23 | 定点取用候选池很小，但**有一条明确值得取** |
| 主包依赖不重 | 82 项直接依赖，**不含 torch / qlib**（torch 是 extra）；qlib 由 git commit 钉死，容器镜像 3.66 GB（压缩） | 本地方案已绕开容器，维护面主要在 Python 侧 |
| **改动的自动化防线几乎为零** | `test/` 30 文件 / 161 用例；`scenarios/qlib` 仅 1 文件 5 用例且全 `Mock`；`components/coder/factor_coder` **零覆盖**；执行链路测试因缺 `offline` 标记 CI 永不执行（全仓仅 55/161 带标记） | **改完没有自动化网**——验收只能靠端到端冒烟，正是 `factor_pipeline.py` 那条链路 |
| 打包机制是隐式的 | 上游 `rdagent/__init__.py` **不存在**（PEP 420 命名空间包）；决定 wheel 内容的是 setuptools-scm file finder + `include_package_data` 默认 True → **git 跟踪到的文件 = 包内容** | vendored 副本新增文件**必须 `git add`**，否则不进包 |
| 上游已加签名 pickle | 上游 `rdagent/core/serialization.py`（`RDAGENT_SIGNED_PICKLE_V1`，HMAC-SHA256，`UntrustedArtifactError`）在本地**不存在**；本地 `CoSTEER/knowledge_management.py` 用裸 `pickle.load(open(...))` | 批次 1 要打开知识库路径，先知情。上游编号 **#1471** |
| 扩展点在本地副本同样成立 | `components/workflow/rd_loop.py:34/39/50/56/59/63` 六个 `import_class(PROP_SETTING.x)` | 薄壳策略可行 |
| 记忆机制在本地副本同样成立 | `CoSTEER/__init__.py:29` `knowledge_self_gen: bool = True`；`CoSTEER/config.py:30/33` 有 `knowledge_base_path` / `new_knowledge_base_path` | 批次 1 零代码可行 |

上游许可证：**MIT（纯，无附加条款，Copyright (c) Microsoft Corporation）**——
fork、修改、闭源、商用均允许，只需保留版权声明。`rd-agent/LICENSE` 已在仓库内。

**与既有指令的关系**：2026-09-02 的冻结指令（不同步上游）**继续有效**。
本规划不是"同步上游"，而是"在冻结前提下做本地升级，并让冻结这件事变得可控"——
冻结之所以一直是对的，是因为没有差异登记，动一下就不知道会碎什么。批次 0 就是为了解掉这个约束。

---

## 2. 本地补丁：它们不是可选项

以下 6 处已**逐行核对确认**是本地补丁（以上游基线 `6762f84f` 为参照）。
前 5 处做的是同一件事：**去掉 conda 与 Docker 依赖**。没有它们，这个环境里跑不起来。

| 文件 | 改了什么 | 原注释 |
|---|---|---|
| `components/coder/factor_coder/config.py` | `CondaConf` → `LocalConf(bin_path="", retry_count=0, default_entry="python -m rdagent.app.cli")`；`enable_cache` 同时被强制为 `False` | `# ... since conda may not be available`；`# disable cache to avoid stale results` |
| `scenarios/qlib/experiment/workspace.py` | 新增 `_run_cmd_in_current_env()`，`qrun` 直接走 `subprocess.run(shell=True)`；上游的 `env_type == "docker" / "conda"` 分支在本地已不存在 | `"""Run command directly in the current environment (no conda/docker)."""` |
| `scenarios/qlib/experiment/utils.py` | `generate_data_folder_from_qlib()` 被替换为硬编码返回的 `daily_pv.h5` 描述 | `# generate_data_folder_from_qlib()  # Disabled - data already available via Qlib` |
| `components/workflow/rd_loop.py` | 基础因子校验失败**不再阻断**加载 | `# validation may fail for non-standard field names like 'high' vs '$high'` |
| `pyproject.toml` | `[tool.setuptools.packages.find]` + `namespaces = true` + package-data globs | `本 fork 的 vendored 源码大量包目录缺 __init__.py` |
| `rdagent/__init__.py` | **新增文件**（上游无，PEP 420 命名空间包）。mtime 2026-08-21 = vendoring 当天 | `Top-level package init —— 使 rdagent 成为标准包` |

**推论**：这 5 处必须进差异登记、必须写"为什么不能加层"的注释。
上游 v1.0.0 在 `workspace.py` 上是 Docker/conda 双分支——**照搬上游等于让挖矿停摆**。

> 其余约 50 项差异（含 `knowledge_management.py` 的 pickle 回退）尚未归属，
> 可能是上游这六周的新提交、也可能是本地补丁。**这正是批次 0 要产出的东西。**

---

## 3. 改造分层：目标是把改动停在前三层

| 层 | 做法 | 改上游文件 | 例 |
|---|---|---|---|
| **L0** | 环境变量 | 0 | 三段式时间边界、知识库路径、成本参数 |
| **L1** | prompt YAML（外部文件，支持路径优先级覆盖） | 0 | 反馈措辞、假设生成的约束 |
| **L2** | 换类：`import_class(PROP_SETTING.x)` 指向自建模块 | 0 | runner / summarizer / hypothesis_gen |
| **L3** | 补丁 | **是** | 只在 L0–L2 都做不到时，且必须登记 |

**KPI：L3 文件数不再增长。** 现状是 5 处已确认 + 若干未归属；批次 0 之后
这个数字第一次变得可信。

**换类契约**（已核实）：
- Coder / Runner：`__init__(scen)` + `develop(exp) -> exp`，**原地改 exp**，
  要往下游传话就设 `exp.prop_dev_feedback`（`core/developer.py:16-17, 24-31`）
- Summarizer：`generate_feedback(exp, trace, exception=None) -> ExperimentFeedback`；
  返回的 `HypothesisFeedback.decision` **就是 SOTA 开关**（`core/proposal.py:178-185`
  取最后一个 `decision=True` 的当 SOTA）

---

## 4. 升级批次

### 批次 0 · 差异登记 ✅ 已完成（2026-10-07）

产出 **`FORK_DIVERGENCE.md`**。核定：**19 处实质本地补丁** + 2 处换行符噪声 +
1 处本地新增 `.py` + 30 个未受版本控制的文件。每处补丁已附"为什么不能加层"。

**两个计划外的收获**（详见 `FORK_DIVERGENCE.md` §5、§6）：
1. **批次 1 被阻断**——本地 `create_embedding` 被替换成伪随机向量，记忆层的语义检索
   跑在噪声上。见下方批次 1 的修订。
2. **30 个文件未受 git 跟踪**（RD-Agent **自带的** `.gitignore` 在 vendoring 时吃掉了它们），
   且 `deploy/portable/build/clean-src/` 是 git 导出、**已经丢了这些文件**。

### 批次 1 · 修 embedding + 开记忆

> ⚠️ **本节已修订（2026-10-07）**：原判断"零代码、半天"**作废**。
> 批次 0 发现本地 `create_embedding` 被替换成伪随机向量
> （`oai/backend/base.py`，`sha256(文本)` 播种 → 1536 维高斯），而记忆层的检索
> **正是走这条路**：`rag.query` → `graph_query_by_content` → `semantic_search` → `create_embedding`。
> 直接开记忆 = 往 LLM 上下文里灌与任务无关的"经验"，且不报错、日志正常。

**1a · 修 embedding 通道 ✅ 已完成（2026-10-07）**

选型结论：**(a) 与 (b) 是同一条代码路径**——远端供应商与本地模型只在
`base_url`/`model` 上不同，代码无分叉，所以「选型」这个问题本身消失了，
(c) 不再需要。详见 `FORK_DIVERGENCE.md` §3 A2 与 §5。

四处改动：`base.py` 还原上游 `create_embedding` + 新增 `_embedding_channel_ready()`
前置检查；`litellm.py` 把 `api_base`/`api_key` 转发给 `litellm.embedding()`
（**这一行缺失是整条通道断掉的根因**）；`oai/utils/embedding.py` 新增
`resolve_embedding_channel()`。

三个实测到的关键点：
1. **`.env` 里早就配好了 SiliconFlow 凭证**（`EMBEDDING_MODEL=BAAI/bge-m3` 等），
   但**全仓没有任何代码消费**——修复后才第一次真正生效。
2. **`openai/` 前缀是必需的**：litellm 把 `BAAI/bge-m3` 解析成 provider=`BAAI`
   并报 `LLM Provider NOT provided`，必须补成 `openai/BAAI/bge-m3`。
3. **报错必须放在重试循环之外**：外层会 catch-all 重试（30×5s），
   放里面会让清晰的信息被 ~150s 退避吞掉。

**DeepSeek 无 embedding 接口已独立验证**（`/v1/embeddings` 与 `/embeddings` 均 404）——
本地补丁的注释属实，但结论应是「换通道」而非「造伪向量」。

**用户级配置面已打通**（个人中心「AI 服务配置」新增向量检索区块 →
`user_profiles.embedding_{model,base_url,api_key}` → `LLMConfig.llm_env_overrides`
→ `EMBEDDING_*`）。留空的字段沿用容器级 `.env`，所以「只换模型、沿用容器 key」
也成立。回归防线：`backend/tests/test_embedding_config.py`（17 例）。

**1b · 再开记忆 ✅ 已完成（2026-10-07）**

- 设 `FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH` + `FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH`
  （前缀来自 `factor_coder/config.py` 的 `model_config`；字段来自 `CoSTEER/config.py:30/33`）
- **验收**：不只是"KB 文件在增长"——还要**抽检检索结果的相关性**。
  增长只能证明它活着，不能证明它有用。
- **陷阱 1**：cwd 下有 `graph.pkl` 会被静默加载进知识库（`knowledge_management.py` 硬编码 `Path.cwd()`）
- **陷阱 2**：本地是**裸 pickle 加载**（上游已改为签名校验）。文件自产时风险低，
  但知识库路径**不要指向共享目录或可被第三方写入的位置**

**执行结果**（三处实测发现改变了本批次的判断）：

1. **记忆原本是「失忆」而非「关闭」**——`with_knowledge` / `knowledge_self_gen`
   默认就是 `True`（`CoSTEER/__init__.py:28-29`），单次任务内一直在生成与检索经验；
   缺的只是落盘路径。所以伪 embedding 之前就在污染上下文，只是污染范围限于单次任务。
2. **「增长」作为验收指标是无效的**——`UndirectedGraph.add_node` 的插入期语义去重
   （`graph.py:139-146`）**被上游自己注释掉了**（与基线 `6762f84f` 0 行差异），
   图谱必然越长越大。原计划的验收措辞因此不是"更严格"，而是"唯一可行"。
3. **两个陷阱都从静默改为响亮**——新增 `backend/services/engine/rd_agent/kb_env.py`，
   对相对路径直接 `ValueError`（子进程 cwd 是每任务独立的 `task_log_dir`，
   相对路径会把 KB 落进任务日志、下个任务读不到，而日志一切正常）。

产物：`kb_env.py`（新）+ `launcher.py` 接线 + 并发 filelock 默认开启 +
`backend/tests/test_kb_env.py`（10 例，配置契约）+
`backend/tests/test_kb_retrieval.py`（4 例，容器内真实 bge-m3 检索验收，全通过）。
默认落 `/data/rd_agent_kb/coasteer_kb.pkl`。

**注意**：首个真实 `graph.pkl` 要等一次实际挖掘任务才会产生；上述验收跑在受控种子图谱上。
真实的累积效果需在首次任务后回看（`FORK_DIVERGENCE.md` §5 已标注）。

### 批次 2 · 评测口径收口（重活，决定所有数字的真假）

- **L2 换 runner**（`PROP_SETTING.runner`）
- **本地已有的便利**：`qrun` 在本地副本里已经是 `subprocess` 直跑
  （`scenarios/qlib/experiment/workspace.py` 的本地补丁），**不需要 Docker**
- 要收的口：复权口径、真实成本、可交易性（涨跌停/停牌/ST）、三段式 + embargo
- **验收**：故意注入一个前视因子，确认评测器能抓到它（已知答案对照）
- **前置**：`QLIB_FACTOR_TRAIN_START` 等环境变量是否真的传导到 qlib 侧，
  目前**未验证**（见 §6）

### 批次 3 · 反馈通道扩容（小改动、高杠杆）

- **L2 换 summarizer**（`PROP_SETTING.summarizer`），从 `decision` 字段接管 SOTA 判定
- 现状：LLM 一场 run **只看到三个标量**（`scenarios/qlib/developer/feedback.py:17-21`）
- 要补：换手 + 成本后收益；与现有库的 `max|ρ|`；IC 时间序列摘要（稳定性/衰减，不是均值）
- **验收**：同一批候选，人工看 10 条 `new_hypothesis` 的文本质量变化

### 批次 4 · 经验记忆（FactorMiner 规格）

与批次 1 **互补**：CoSTEER 知识库管「代码怎么写」，这个管「往哪个方向挖」。
一个 JSON/SQLite 即可，不需训练、不需向量库。

| 存什么 | 结构 |
|---|---|
| Mining State | 库规模、近期准入日志、逻辑域饱和度 |
| `P_succ` | 推荐方向（High/Medium） |
| `P_fail` | 禁区：`模式名 \| 冲突因子编号 \| 相关系数` |
| Strategic Insights | 算子级教训 |

**最值得抄的机制——降级重分类**：某因子被准入但与已有因子相关 0.82，
就把它从 `P_succ` 移到 `P_fail`。更新时机为**每个 mining batch 结束后统一写**。

> ⚠️ 论文数字（有记忆 60.0% vs 无记忆 20.0% 高质量候选）成色有限：
> 单次消融、单数据集、无置信区间、无第三方复现，且"高质量"定义为 `|IC| > 0.02`。
> **当方向看，别当承诺。无官方代码仓库，只能照论文重写。**

### 批次 5 · 多重检验与池卫生

PBO / DSR（CSCV 为公开算法，按论文重写）+ BH-FDR + 入库一刀
（代数归一化、相关性剪枝**用训练段算**、符号标定）。

---

## 5. 上游同步规则（取代"季度 rebase"）

因为已经不是独立 fork，**没有 rebase 这个动作**。规则改为**定点取用**：

1. 上游改动逐条过 §4 批次 0 的差异表，只挑**安全修复**与**明确需要**的功能
2. **铁律**：任何从上游取用的改动，必须先通过本地回归（能干净跑完一轮 RDLoop）
3. 取用后立即更新 `FORK_DIVERGENCE.md`，标注来源提交号
4. 触发取用的典型场景：安全修复（如签名 pickle）、依赖坏死、上游修了我们踩过的坑

**注意**：上游 quant 场景不是投入重点（近 6 个月 `scenarios/qlib` 仅 1 条提交，
新工作去了 Kaggle / DataScience / FT-Agent），因此"定点取用"的候选池会长期很小——这对你是好事。

**首选定点取用目标已确定**：上游 **#1471「verify persisted artifacts before deserialization」**。
理由：批次 1 正好要打开知识库路径（本地是裸 pickle 加载），而这是上游近半年
在 qlib 场景的**唯一实质改动**——需求与供给正好对上。

---

## 6. 风险与未验证项

| | 内容 | 状态 |
|---|---|---|
| ⚠️ | 上游把 qlib 钉在 git commit `2fb9380b`（conda 与 docker 两条路都是），而本地补丁**绕过了 conda/docker** → **本地实际跑的是哪个 qlib，未知** | **未验证**，批次 2 必须先确认 |
| ⚠️ | `QLIB_FACTOR_TRAIN_START` 等是否真的传导到 qlib 侧生效 | **未验证** |
| ⚠️ | `CoSTEER/config.py:10` 用 pydantic v1 风格 `env_prefix`（子类已用 `model_config` 覆盖，影响未实测） | 部分核实 |
| ⚠️ | 55 项差异中约 50 项无归属 | 待批次 0 |
| ⚠️ | 裸 pickle 加载知识库 | 已知，见批次 1 陷阱 2 |
| ⚠️ | **30 个文件未受 git 跟踪**（RD-Agent 自带 `.gitignore` 吃掉的）；`deploy/portable/build/clean-src/` 是 git 导出，**已丢这些文件**。目前无实际后果，但打包（setuptools-scm：git 跟踪到的文件 = 包内容）会持续丢 | 见 `FORK_DIVERGENCE.md` §6，待决策是否加白名单 |
| ✅ | ~~**假 embedding** 让记忆层语义检索跑在噪声上~~ | **2026-10-07 已修复**，见批次 1a 与 `FORK_DIVERGENCE.md` §5 |
| ⚠️ | **容器访问不到宿主 ollama**（`ollama` 只绑 `127.0.0.1`，容器在自定义 bridge 网络）→ 本地 embedding 模型的**容器路径**暂不可用，宿主 CLI 路径可用 | 要用本地模型需 `OLLAMA_HOST=0.0.0.0` + 网关地址，或把服务放进 compose 网络 |
| ⚠️ | **rd-agent 改动是烘焙进镜像的**（`install_rdagent.sh` 装完即 `rm -rf` 源码目录），仓库改动**必须 `docker cp`** 或重建镜像才生效——容器重建会静默回退 | 每次改 rd-agent 后确认部署方式，见下 |
| ⚠️ | 上游 `constraints/3.10.txt` 钉 `litellm==1.97.0`——**正是本地踩过坑、需要 sitecustomize patch 才能跑的那个版本**；`requirements.txt` 只写 `>=1.73` | 已知；这类抽风只能自己接，别指望上游 |
| ❓ | 换 `scen` 槽位需连带适配所有部件 | 未验证 |

---

## 7. 维护判据：什么时候该放弃这套方案

1. L3 改动持续增长，且无法收敛 → 说明扩展点不够用，考虑自立门户
2. 上游半年不动 + 需求未被满足 → 冻结变成纯负担，重新评估
3. 依赖坏死（litellm / qlib pin 冲突且无解）
   —— 注：本地 litellm 版本锁定是**本地策略**，非临时手段（见踩坑史）

**退出路径**：L2 自建模块必须能**独立 import、独立测试**（不启动 RD-Agent 就能跑）。
这条铁律保证上述任一情况触发时，成果能整体搬走。

---

## 附：本文件自身也是 L3

新增本文件即产生一处与上游的差异。这是**有意为之**——差异登记与维护规则
必须和代码放在一起，否则下次同步时没人会记得去看它。
建议在批次 0 产出的 `FORK_DIVERGENCE.md` 中把它一并登记。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-07 | 初稿。核对本地副本后发现 vendored 结构 + 未登记补丁，据此把"差异登记"提为批次 0 |
| 2026-10-07 | 补入上游活跃度实测（量化场景近 6 个月仅 1 条提交）→ 修正"冻结成本上升"的误判；锁定基线 commit `6762f84f` 并验证分类法；补入测试覆盖、打包机制、litellm 版本钉三处事实 |
| 2026-10-07 | **批次 0 执行完毕**（产出 `FORK_DIVERGENCE.md`）。据此修订批次 1：原"零代码、半天"作废，前置为修 embedding 通道；新增 2 条风险 |
| 2026-10-07 | **批次 1a 执行完毕**（embedding 通道修复 + 用户级配置面 + 17 例回归测试）。补丁数 19 → 21；新增 2 条风险（容器够不到宿主 ollama / rd-agent 烘焙进镜像需 docker cp） |
| 2026-10-07 | **批次 1b 执行完毕**（记忆落地：`kb_env.py` + launcher 接线 + filelock + 14 例测试）。三处实测发现改写本批次判断：记忆原是「失忆」非「关闭」；插入期去重是上游自己注释掉的（0 行差异）故「增长」不可作验收指标；相对路径陷阱改为响亮 `ValueError`。批次 1 全部完成，下一个定点取用目标 #1471 解锁 |
| 2026-10-07 | **1a/1b 代码审查修复**。HIGH：用户级向量检索配置在 launcher 处断链（`llm_env_overrides()` 产出 `EMBEDDING_*` 无人消费 → 个人中心显示已保存、挖掘始终用容器级 .env），已在 `llm_env.embedding_overrides()` 接线并补「生产者→消费者」端到端回归测试。MEDIUM×2：守卫被 `LLM_SETTINGS.embedding_model` 的非空默认值挡死；前端「配置加载失败后只填 Key 保存」会静默清掉已存 model/base_url。LOW×4：明文密钥三处落日志已脱敏、embedding 端点补 `/v1` 与 chat 对齐、`FORK_DIVERGENCE` 计数订正。测试 31 → 48 |
| 2026-10-07 | **🔴 上游密钥泄漏已修**（基线自带，非本地引入）：`LiteLLMAPIBackend.__init__` 原样打印 `LITELLM_SETTINGS`，把 `openai_api_key` 明文写进 stdout 与 `/app/log/*/LITELLM_SETTINGS/*.pkl`。实测一次探针落 13 份含真实讯飞 MaaS key 的快照。已改为脱敏打印 + 端到端测试锁死；容器已重启加载新模块。**待办：① 轮换该 key（旧值已落盘）② 清理存量 .pkl（删除请求被权限分类器拦下，需用户确认）③ rd-agent 烘焙进镜像，改动需 docker cp（bind-mount 可行性已验证、待决策）** |
