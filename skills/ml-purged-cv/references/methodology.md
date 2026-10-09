# 方法论：Purge / Embargo / CPCV / 因果 Walk-Forward

移植自 [quantskills/skill-ml-purged-cv](https://github.com/quantskills/skill-ml-purged-cv)（MIT）的方法学文档，按本地实现（整数 session 坐标、纯标准库切分器）重写。原始出处为 López de Prado《Advances in Financial Machine Learning》的 Purged K-Fold 与 Combinatorial Purged CV。

## 1. 问题：重叠标签 + 持久特征 = 泄漏

金融时序监督学习里，样本标签很少是「瞬时」的：h 日前视收益的标签需要占用 h 个未来交易日。相邻样本的标签窗口互相重叠（重叠 h−1 天），加上特征持久（20 日动量今天和昨天几乎一样），于是：

- **随机 K 折**：训练集和测试集在时间上交错，训练样本的标签窗口与测试样本的标签窗口大量重叠。模型可以「记住」邻近时刻的标签实现，测试 IC 被系统性高估。
- **朴素时序 K 折/固定切分**：切块之间没有隔离带，边界处同样相邻重叠。
- **Walk-Forward**：方向是对的（只用过去），但测试块之前的训练样本若信息区间伸进测试块，仍然泄漏。

真正的泄漏量取决于：重叠天数 / 总 session 数 × 相邻样本特征相似度 × 模型灵活度。本仓实测：同一合成数据 Ridge 落差 0.003、HGB 落差 0.020；真实数据 3 年全窗 Ridge 落差 0.018。**不能用一个小落差的实验宣称「没有泄漏」**。

## 2. 信息区间（Information Interval）

每个样本声明一个闭区间 `[interval_start, interval_end]`（单位=session 序号）：

- `interval_start`：该样本**最早使用**的信息时间。用 20 日动量 → `session − 19`；只用当日 → `session`。
- `interval_end`：决定该样本**标签**所需的最后时间。h 日前视收益 → `session + h`；标签瞬时（如当日涨跌分类）→ `session`。

两个样本「相关」当且仅当信息区间相交：`start ≤ other.end and other.start ≤ end`（含端点，来源 domain.py 同口径）。所有排除逻辑都建立在这一个谓词上，而不是任何「固定删 K 行」的近似。

## 3. 三件排除器 + 因果约束

执行顺序（本实现与来源一致）：**先 Purge，再 Embargo / Pre-Test Gap / 因果约束**，后者不重复计算已被前者剔除的样本。

1. **Purge（区间重叠剔除）**：候选训练样本的信息区间与任一测试样本区间相交 → 剔除。实现：把测试区间合并成不相交区间表，对每个候选用 bisect 判定（近线性，而非 O(候选×测试)）。
2. **Embargo（尾部缓冲区）**：对每个**连续**测试块，取其「最晚信息终点」`latest_end`，再把 session 落在 `(latest_end, latest_end + E]` 的候选剔除。职责：兜住 Purge 不知道的、样本对测试块尾部的声明式依赖。
3. **Pre-Test Gap（测试前隔离带，只用于 Walk-Forward）**：测试块**之前** G 个 session 的候选剔除，方向与 Embargo 相反。
4. **因果约束（Causal，只用于 Walk-Forward）**：候选的信息终点必须严格早于测试块起点（`interval_end < block.start`，实现为 `end < require_information_before`），保证训练侧不看未来。

**NO_INCREMENTAL_EXCLUSION_AFTER_FULL_INTERVAL_PURGE**：当 `interval_start` 前移量（lookback）足够大时，Purge 已经覆盖了 Embargo 的全部目标样本，Embargo 增量=0。来源文档明确记录该现象（"full interval-aware purge already covers everything embargo would exclude"）。本仓实测：`lookback=20` 时 embargoed=0；`lookback=1` 时 embargoed=1536/全窗。**增量=0 不代表 Embargo 失效，而是当前区间声明下它没有新东西可剔**——报告会显式给出该告警而不是静默。

## 4. 会话轴（session axis）与同日截面

排除与隔离带一律以**交易日**计（不是自然日——长假会让「日历日」失真）。同一 session 的多个样本（多只股票）永远落在切分的同一侧：先把交易日历分成连续 session 块，再按块的归属把同 session 全部样本整体划入训练或测试。这样同一天的横截面市场状态不会跨侧。

本实现直接用整数 session 序号（0..S−1）作为坐标：来源用 numpy datetime64，整数化后 `bisect` 与算术都退化为最朴素的形式，且对「区间单位=交易日」的语义表达更直白。

## 5. Fold-Local 原则

任何**学习型**预处理（缺失填充、标准化、PCA/特征筛选、target encoding）必须在**每折训练侧**重新拟合再应用到测试侧。全样本标准化一次再切分同样是泄漏。本实现的评估层（`fold_local_fit_predict`）演示：每折先中位数填充（统计量仅来自训练侧）→ 标准化（同理）→ 拟合 → 预测；`evaluate_folds` 汇总各折 OOS 预测后计算 pooled IC。

## 6. CPCV（Combinatorial Purged CV）

- 把 session 轴等分为 N 个连续组，任取 k 组做测试 → `C(N,k)` 个组合；每个组合内部按区间口径做 Purge + Embargo。
- 路径分解：把「组合×测试组」二部图做**正常边着色**（proper edge-coloring），把 `C(N,k)` 个组合重组为 `C(N−1,k−1)` 条完整路径——每条路径恰好覆盖每个组一次，成为一条可评估的「完整回测路径」。来源用贪心着色 + 冲突时沿交替分量重染色；本实现逐行移植同一算法（`cpcv_path_decomposition`，纯标准库）。
- 属性（演示模式每轮断言）：路径数 = `C(N−1,k−1)`；每条路径覆盖每组恰一次；同一组合的 k 个测试组落在 k 条不同路径。

## 7. 因果 Walk-Forward

测试块固定在时间轴**末端**，按 `n_splits × test_sessions` 从尾部往前排；候选训练集只取测试块之前的 session（可用 `max_train_sessions` 限制回看长度，退化到滑动窗口）。隔离手段：Purge + Pre-Test Gap + 因果约束，`embargo_sessions=0`（来源同口径：Embargo 与 Pre-Test Gap 语义相反，Walk-Forward 用后者）。断言：保留训练样本的信息终点严格早于测试块起点。

## 8. 证据通道与解释规则

| 通道 | 性质 | 含义 |
|---|---|---|
| `vanilla-shuffled-kfold` | unsafe | 随机乱序 K 折：必须显示出大量保留重叠（对照组） |
| `vanilla-chronological-kfold` | unsafe | 时序 K 折无排除：边界仍有重叠（对照组） |
| `purged-kfold` | safe | 区间 Purge，`retained_train_overlapping` 必须=0 |
| `purged-kfold-embargo` | safe | 再加 Embargo，重叠必须=0 |

解释规则（来源文档与本地实测一致）：

1. **硬证据是结构不变量**：安全通道保留训练集与测试区间的重叠数必须为 0；`leakage_control_status=PASS` 只声明这一点。
2. **IC 通道差不是纯泄漏因果量**：不同通道的训练集构成、测试集构成都不同，来源在其真实数据 canary 中明确拒绝把通道差直接归因于泄漏。可以报告「purged ≤ vanilla」的实测方向，不可以宣称「差值就是泄漏量」。
3. 校准声明（本仓冒烟内置）：同数据同模型，purged+embargo 的测试 IC 应 ≤ vanilla；如实报告数字，不夸大，方向不符时报告不符。

## 9. 来源仓库的完整审计框架（概念层，本地未脚本化）

来源技能把防泄漏放进一个更大的「验证审计」框架，这些概念值得在本地研究流程中沿用，但本技能**没有**实现对应脚本：

- **PIT（Point-in-Time）资产池与数据**：选股池、成分、财务字段都必须带可用时点；本技能的默认 top-N 选样不是 PIT，报告会告警。
- **Governed Holdout**：留出的最终检验集要有治理（登记、访问记录、不得反复调参），不是随手 split 出来的第二测试集。
- **Temporal Forward Evidence**：先登记预测、等标签成熟、再结算——把「前向验证」变成登记-结算流程而不是事后回看。
- **Feature Manifest**：特征声明表（来源、变换、时点），让信息区间可以从特征定义机械推导而不是逐一拍脑袋。

## 10. 本实现与来源的差异（诚实清单）

| 项 | 来源 | 本实现 |
|---|---|---|
| 坐标 | datetime64 会话 + numpy | 整数 session 序号 + 纯标准库 |
| 依赖 | numpy（核心）+ pandas/pyarrow（数据层） | 核心零依赖；演示需 numpy；quantdb 模式需 pandas/pyarrow |
| 排除轨迹 | `include_exclusion_trace` 逐样本记录 | 仅保留聚合账目（purged/embargoed/gapped 位置元组） |
| InvalidFold | 折有效性原因（最少 session/样本数）不满足则标 InvalidFold | 直接产出划分，调用方自查空折（演示与 CLI 均有断言/审计） |
| 数据层 | 来源自带行情装配 | 废弃，改 QuantDB 直读（features_daily + daily_forward 自算标签） |
| 摘要 digest | 每划分带 canonical digest | 未移植（不做可复现性哈希登记） |

未移植的项不改变排除语义；需要逐样本排除轨迹或 digest 登记时以上表为对照，明确这是缺口而非等同。
