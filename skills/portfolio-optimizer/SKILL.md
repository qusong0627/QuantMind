---
name: portfolio-optimizer
description: "组合优化工具箱（QuantDB 直读 CN/HK/US）——五种凸目标（最小方差/均值-方差/最大夏普/风险平价 ERC/最大分散化）× 约束（单票上限/行业目标或中性/因子暴露中性/换手上限），Σ 用 Ledoit-Wolf 收缩并自带独立验证；输出最优权重、事前风险分解（逐资产 RC + GLS 因子拆分）、换手与约束绑定清单；可与 barra-risk-model 类技能经 JSON 协议互通。用户问「权重怎么分」「最小方差组合怎么配」「风险平价/ERC 怎么做」「加行业中性/换手约束后权重变多少」「组合事前波动多大」时使用。触发词：组合优化、最优权重、最小方差、最大夏普、风险平价、ERC、最大分散化、行业中性、换手约束、因子暴露中性"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **数据目录**：宿主机 `/home/zbox/projects/quantmind/data` ↔ quantmind 容器 `/data` ↔ dsh `/quantmind/data`。脚本自动探测（环境变量 `QM_DATA_ROOT` 可覆盖）。
> 2. **执行位置**：`--demo`/`--input` CSV/JSON 模式为纯标准库，可在宿主机/dsh/Windows 直接跑；`--quantdb` 需要 pandas/pyarrow/sklearn，**在 quantmind 容器内跑**：
>    ```bash
>    docker cp skills/portfolio-optimizer/scripts/portfolio_optimizer.py quantmind:/tmp/
>    docker exec -w /app quantmind python3 /tmp/portfolio_optimizer.py --quantdb \
>      --market CN --end 2025-12-31 --cov-window 120 --top-n 60 \
>      --objective min_variance --weight-cap 0.10 \
>      --out /data/reports/portfolio-optimizer/cn_minvar.json
>    ```
> 3. **报告落盘**：建议容器内 `/data/reports/portfolio-optimizer/<用途>.json`；stdout 同时输出中文诊断。容器写出的文件属 root，删除用 `docker exec`。
> 4. **symbol 格式**：CN 后缀式 `000001.SZ`；HK 四位+.HK `0001.HK`；US 大写 Ticker `NVDA`（与各 parquet 内一致，前缀式会静默查空）。

# portfolio-optimizer — 组合优化与风险约束工具箱

回答四类问题：**权重怎么分（五种目标选一）？约束加多少（上限/行业/暴露/换手）？事前风险多少、谁在贡献？换手多大、哪条约束在绑定？** 只做单期组合优化与事前（ex-ante）诊断——不回测、不预测收益、不输出交易指令。

## 能力总览

| 用法 | 内容 |
|---|---|
| `--demo` | 内置确定性演示（纯标准库）：2 资产闭式金样、五目标对比、行业中性多空+换手、暴露中性、RC 等分校验、三类不可行报错语义 |
| `--input <csv>` | 宽收益面板（date × symbol）纯标准库优化；可另附 mu/行业/暴露/上期权重 CSV |
| `--input <json>` | 完整问题规格（barra-risk-model 类技能互通协议，见 references/methodology.md §7） |
| `--cov <csv>` | 直接给协方差矩阵（原样使用，跳过估计） |
| `--quantdb` | CN/HK/US 直读：Σ 由 daily_forward 收益自估（Ledoit-Wolf），票池=窗口末日成交额前 N；μ 用 --mu-file / --mu-from-mean；行业映射自动（--sectors auto）；暴露列从因子数据集读（--exposure-cols） |
| `--quantdb --list-columns` | 列出因子数据集列并标注标签列/常量列，选暴露列前先跑 |

五目标：`min_variance` / `mean_variance`（λ=`--risk-aversion`）/ `max_sharpe` / `risk_parity`（ERC）/ `max_diversification`。
约束：`--weight-cap` 单票上限、`--long-short`、`--budget`、`--sector-targets`/`--sector-neutral` 行业等式、`--exposures`+`--exposure-neutral` 因子中性、`--prev-weights`+`--turnover-limit` 换手上限。
输出：最优权重 + 事前风险分解（逐资产 RC 份额 / GLS 因子-特质拆分）+ 换手（L1/单边）+ 约束绑定清单 + 独立校验块；JSON 报告同时落盘。

## 本地数据映射（QuantDB）

| 市场 | 收益/协方差源（`1_kline_data/`） | 暴露列数据集（`6_ml_datasets/`） | 备注 |
|---|---|---|---|
| CN | `quantdb/.../daily_forward`（**前复权**，正确口径） | `features_daily`（默认）/ `l1_factors` / `l2_factors` | 行业=instrument_detail.rs_hyname（--sectors auto） |
| HK | `quanthk/.../daily_forward`（**不复权**，收益未含分红） | `l1_factors` | 双来源重复行按 (symbol,date,published_at) 去重保 akshare 原始价；行业=akshare_profile |
| US | `quantus/.../daily_forward`（**不复权**） | `l1_factors` | 行业=sector/*.parquet；因子分区进度可能落后行情 |

- **收益定义**：close 逐日 pct_change（**fill_method=None**，缺失日不伪造 0 收益），年化 ×252；窗口 `--cov-window`（默认 120 交易日）。
- **票池**：窗口末日成交额降序前 `--top-n`（默认 60），或 `--symbols` 显式指定（后缀式）。事后池含轻微选择偏差（已入 caveats）。
- **暴露**：`--exposure-cols total_mv,vol_std_20` 从数据集读窗口均值（`--exposure-window`，默认 20 日）；标签列（label_return / return_Nd / pred.*）会被拒绝当暴露。
- **Σ 口径**：Ledoit-Wolf 收缩（complete-case，丢弃含缺失行并计数）；无 sklearn 回退成对完整样本协方差；估值前对称化 + PSD 修复，严重不正定直接拒绝。

## 标准流程

1. `--demo` 先跑一遍自检（纯标准库、秒级）。
2. 定票池：`--quantdb --market CN --end <日> --top-n <N>`，或显式 `--symbols`；要暴露中性/因子拆分时先 `--list-columns` 挑列。
3. 选目标：有可靠 μ 才用 mean_variance/max_sharpe；μ 无来源时用 min_variance/risk_parity/max_diversification。**一律先加 --weight-cap**（μ 噪声会造极端权重）。
4. 加约束并**读约束绑定清单**：确认想让绑的绑了、不想绑的没绑（cap 0.10 下 min-var 的绑定数随窗口/票池变化，实测 1~6 只；RP 通常不绑）。
5. 看校验块（闭式解偏差 / RC 等分）与 caveats，尤其确认 Σ 丢行数与市场复权口径。
6. 结果落 JSON 报告，记录命令行与窗口——复跑换窗口时保留前后两份。

## 实测校准（2026-10-08，quantmind 容器，QuantDB CN/HK/US）

CN = 20251231 窗口末日成交额前 60、窗口 120 交易日（2025-07-09~2025-12-31）、单票上限 0.10、Σ=Ledoit-Wolf（complete-case 丢 12 行），除注明外：

| 运行 | 后端 | 年化波动 | 持仓 | 最大权重 | 有效 N | 关键约束/校验 |
|---|---|---|---|---|---|---|
| min_variance | scipy | 16.32% | 33 | 0.1000 | 19.0 | 上限绑定 1 只（600519.SH=0.1000）；闭式解偏差 1.44e-08 |
| risk_parity | auto→stdlib | 19.56% | 60 | 0.0767 | 44.3 | 上限未绑；RC max−min 相对差 4.51e-08 |
| risk_parity（对照） | scipy | 19.56% | 60 | 0.0767 | 44.3 | 同问题 RC 差 6.6e-03（SLSQP 早停，见坑 1） |
| max_sharpe（μ=历史均值） | scipy | 20.96% | 29 | 0.1000 | 17.5 | 上限被压住 |
| max_sharpe 无上限（对照） | scipy | 21.43% | 25 | **0.1456** | 15.2 | μ 噪声集中度证据 |
| min_variance + 暴露中性（total_mv, vol_std_20） | scipy | 17.07% | 33 | 0.1000 | 18.1 | 暴露实现 ≈0（−4.8e-16 / 7.8e-16）；中性代价 +0.75pp |
| min_variance 仅展示暴露 | scipy | 16.32% | 33 | 0.1000 | 19.0 | 暴露 total_mv 0.472 / vol_std_20 −0.347；因子风险占比 3.4%（其余特质） |
| 两阶段① @2025-06-30 min_var | scipy | 17.84% | 24 | 0.1000 | 13.1 | 换手基准（同票池） |
| 两阶段② @2025-12-31 换手上限 0.30 | scipy | 13.19% | 27 | 0.1000 | 13.8 | 换手紧绑 L1=0.3000（不限则 0.9480） |
| HK 前 30 @2026-05-08 | scipy | 17.53% | 19 | 0.1000 | 13.3 | 双源去重 207,820 行（读取窗口 121 分区口径）；闭式 1.21e-07 |
| US 前 30 @2026-10-06 | scipy | 14.70% | 22 | 0.1000 | 15.9 | 闭式 6.33e-08 |
| CN 行业中性多空（budget 0，±5%，MVO λ=8，μ=历史均值） | scipy | 14.41% | 39（gross 1.45） | ±0.0500 绑 25 只 | — | 24 条行业等式残差 ≤4e-15；闭式 5.71e-08 |

引擎金样（`--demo`，纯标准库）：2 资产 Σ=[[0.04,0.006],[0.006,0.09]]、μ=(8%,12%) 年化——min_variance 手算 (42/59, 17/59)、risk_parity 逆波动 (0.6, 0.4)、max_sharpe 切点 (0.6, 0.4)，与脚本输出偏差 ≤2e-11；闭式解公式两实现（高斯-约当 vs numpy）互差 ≤4.1e-16。
全部真实数据运行的闭式解校验偏差落在 1.4e-08~1.6e-07、RC 等分校验 4.5e-08——两后端在数值级一致，独立实现复核通过。

## 常见坑（2026-10-08 实测标定）

1. **SLSQP 在风险平价目标上早停**：RC 目标在最优附近非常平坦，SLSQP 会在「收敛」状态下停住——同一问题 RC max−min 相对差 6.6e-03，而投影梯度 4.5e-08。`--solver auto` 对 risk_parity 强制走纯标准库投影梯度；手动 `--solver scipy` 跑 RP 会拿到偏的 RC。
2. **「约束不可行」与「求解器故障」是两类错误**：结果违约束时脚本先用相位一探测（箱内最小二乘最小化约束违背、多起点）——残余大才报「联合不可行」并列最突出的矛盾行；残余小才报数值问题。多约束叠加（行业目标 + 暴露中性 + 多头）可能真的空集，别当 bug 重跑。
3. **min_variance 配美元中性（--budget 0）会塌缩**：零组合方差 0 可行且最优，脚本直接拒绝——美元中性书用 mean_variance/max_sharpe。
4. **pct_change 默认 pad 是静默炸弹**：缺失收盘被前收顶替 → 伪 0 收益 → Σ 系统性偏低（实测同一 CN 窗口年化波动 17.83%→16.32%）。脚本已强制 fill_method=None；自己拿 pandas 复算时要带同样的参数。
5. **Ledoit-Wolf complete-case 会丢行**：任一标的缺一天该行全丢（实测不同窗口/票池丢 6~24 行：标准 @1231 top-60 口径丢 12/120，同池两阶段②口径丢 24/120）。丢行数与回退口径都写进 caveats，读报告先看一眼。
6. **μ 噪声 → 极端权重（教科书陷阱，实测可见）**：μ=历史均值的 max_sharpe 无上限时单票到 14.6%；加 cap 0.10 被压住。MVO/MaxSharpe 恒配 --weight-cap，或改用无 μ 目标。
7. **行业目标和之和恰等于预算时最后一行等式冗余**：会致等式矩阵秩亏、求解退化——脚本自动去掉最后一行保持满秩（源技能同款坑）。
8. **HK 双来源重复行去重必须按 (symbol,date,published_at) 保最后**：双来源期 2024-09-02~2026-05-08 内同 (symbol,date) 有 paid_hk+akshare 两行，约 0.5%~5% 标的的 paid_hk 行带（前）复权缩放——按行序 keep='first/last' 会让部分标的拿到错误价格；本次 HK 读取（121 分区=120 交易日窗口+缓冲）去重 207,820 行；**全表口径为 688,942 行**（11615 分区，2026-10-08 实测，重复全部落在 2024-09-02~2026-05-08 内）。
9. **HK/US 的 daily_forward 是不复权原始价**（收益未含分红，口径偏保守）；CN 用前复权，且 daily_backward 有已知损坏——收益一律取 daily_forward。
10. **换手是 L1 口径**（单边=L1/2），不含成本与冲击；限 0.30 实测把组合从「不限时的 L1=0.948 / 波动 12.11%」推到「L1=0.3000 / 波动 13.19%」——换手上限有真实代价。
11. **事后票池**：quantdb 模式按窗口末日成交额取前 N，含轻微选择偏差（已入 caveats）。
12. **闭式解校验的对照问题**：对照单跑「仅预算等式、允许做空」的 min-var；其偏差反映求解精度，不是主问题最优性的证明。

## 脚本与参考

- `scripts/portfolio_optimizer.py`：优化引擎（纯标准库线性代数 + 投影梯度核心；scipy 可选加速；QuantDB 装配层 pandas/pyarrow/sklearn 仅 quantdb 模式加载）。
- `references/methodology.md`：五目标定义与梯度、Σ 估计与 PSD 修复、约束投影数学、GLS 风险分解、求解器设计与 auto 路由、JSON 互通协议、标定记录。

## 来源与许可

方法论移植自 [quantskills/skill-portfolio-optimize](https://github.com/quantskills/skill-portfolio-optimize)（**GPL-3.0-only**）。本地化改写：PandaData SDK 适配层（scripts/data_source.py）与有效前沿扫描（frontier.py）未移植，数据层换为 QuantDB 直读；求解后端扩为 scipy SLSQP + 纯标准库投影梯度双实现，并新增独立校验（闭式解对拍、RC 等分、相位一可行性探测）。仅限本地研究使用；如对外分发本技能需遵循 GPL-3.0-only。分析结论不构成投资建议。
