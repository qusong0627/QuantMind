# minibt vendor 说明（v1.3.3 裁剪版）

来源：https://github.com/MiniBtMaster/minibt
固定 commit：`0ea790e2c3c79bd3393444cc381908dfc6648243`（2026-10-16，`__version__ = 1.3.3`，MIT）
上游仓库无 tag；2026-10-16 之后无新提交（截至 2026-10-26 核对）。

## 为什么用 GitHub 源码而不是 PyPI

PyPI 上 1.3.2（最新）**只发布了 Windows wheel，没有 sdist**，Linux 无法从 PyPI
构建；GitHub 源码包含 1.3.2 的全部修复，且多一个 1.3.3 的小修复（纯指标策略
不再多出空结果页）。因此固定 commit 比固定 PyPI 版本更可复现。

## 裁剪内容（升级时需重置）

删除：`elegantrl/`（torch 强化学习 fork）、`tradingstrategy/`（示例策略）、
`liveplot/`（上游已移除的旧实时绘图）、`build/`、`logs/`、`__pycache__`、
`*.pyd`。`.c` 与 `.pyx` 保留（镜像构建期现场编译）。

## 补丁：**本次升级后不再需要任何源码补丁**

1.2.7 时代的两个裁剪补丁已作废——上游已把重依赖改为惰性导入：

- `elegantrl`：PEP 562 `__getattr__` 按需加载；目录裁掉后 `import minibt`
  正常，只有访问 `minibt.Agents` 才会报「请安装 torch」。
- `tqsdk`：`utils.py` 内改为函数级导入；不装 tqsdk 只影响实盘天勤链路。

## 构建（`docker/Dockerfile.minibt-runner`）

- 基础镜像 `python:3.12-slim-bookworm`（minibt 1.3.x 要求 Python ≥ 3.12）。
- 编译 **5 个** Cython 扩展（`minibt/_build_exts.py`）：
  `zigzag/core`、`cython_functions/{backtest_engine, backtrader_from_signals,
  backtrader_pair_from_signals, signal_profit}`。1.3.2 的 `from_signals`
  逐笔统计修复依赖新增的 `signal_profit`。
- 预建 `/root/.minibt/logs` 与 `/root/.minibt/datas`：1.3.x 的 `Logger` 首次
  取 logger 时 `mkdir(exist_ok=True)`（**不带 parents**），目录缺失会直接抛
  `FileNotFoundError`。
- 额外装 `redis>=5,<9`：仅供 `backend/shared/minibt_sim_agent.py`（模拟盘
  侧车）读写请求/目标键，不影响回测。

## 依赖锁定（沿用 1.2.7 试点实测组合）

numpy==2.2.6 / pandas==2.3.3 / pandas-ta==0.4.71b0 / numba==0.61.2 / duckdb /
networkx==3.6.1 / numpy-ext==0.9.9（**必须 --no-deps**）/ TA-Lib==0.7.1 /
finta / quantstats / statsmodels / scipy / arch / pykalman / rich / colorlog。
**不装**：tqsdk（天勤实盘链路）、tulipy（gcc15 编译失败，功能被 TA-Lib 覆盖）、
PyQt6/pylightcharts/plotly（服务端一律 `isplot=False`，均为惰性导入）。

## 升级验收（2026-10-26，1.2.7 → 1.3.3 A/B）

同一批 11 个 A 股模板、同一份 QuantDB 数据、同一份 `minibt_result` 提取器：

- `result.json` 的 **metrics / 逐笔成交 / 权益曲线完全一致**（11/11）；
- `[RESULT]` 标量指标逐项一致（含 `cum_return / sharpe / max_drawdown /
  win_rate / n_trades / profit_factor / final_equity`）；
- 唯一差异是 `backtest_elapsed_sec`：约 0.3s → 2.1s（2595 根日线），
  对「每日一次决策」的模拟盘侧车无影响；回测中心会感知为约 6 倍耗时。

## 回测口径差异（写策略前必读，与 1.2.7 相同）

- **信号当根 K 线收盘价成交**（默认 `OrderType.Close`）；`OrderType.Market` 才是下根开盘。
- 信号 `.new` 单次消费：持多时 `sell()` 只平不开空，反手需显式两步。
- **无 A 股规则**：T+1、涨跌停、整手、ST 均不建模；结果与实盘存在系统性口径差。
- **默认手续费=0**，需显式配 `self.percent_commission`。
- minibt 自带报告的「最终收益」**不含未平仓浮盈**（保证金式记账）；结构化统计用
  `backend/shared/minibt_result.py::run_and_report`。
- 桌面功能（PyQt 实时图表 / light_chart 回放 / bokeh）在服务端一律禁用：
  `Bt().run(isplot=False, isreport=False)`。

## 模拟盘侧车（本 vendor 新增用途）

`backend/shared/minibt_sim_agent.py` 在 runner 镜像内运行，按调度器请求跑一次
策略并回写目标持仓股数；数据补今日 K 线的口径见
`backend/shared/minibt_sim_data.py`（volume 手→股、amount 万元与 QuantDB 同尺）。
