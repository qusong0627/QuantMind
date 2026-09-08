# minibt vendor 说明(v1.2.7 裁剪版)

来源:https://github.com/MiniBtMaster/minibt (MIT)。本目录是**裁剪后 vendor**,上游单 commit 仓库无 pyproject/无法直接 pip 安装,故随镜像构建期编译。

## 裁剪补丁(升级时需重打,均带 `pilot-prune` 标记)

1. `__init__.py`:`from .elegantrl.agents import Agents, BestAgents` 改注释——elegantRL 是 vendored torch fork,回测场景不需要 torch(elegantrl/ 目录已删除)。
2. `utils.py`(~L40):tqsdk 模块级导入包 try/except(仅实盘天勤链路使用)。

其余依赖(ta-lib/tulipy/bokeh/PyQt)在上游就是惰性导入,缺失只在使用时报错,无需改源码。

## 已删除内容

`build/`、`logs/`、`elegantrl/`、所有 `*.pyd`(Windows 编译产物)、`*copy 2*`、`__pycache__`。`.c` 与 `.pyx` 保留(镜像构建期用 Cython 编译 4 个扩展:`zigzag/core` + `cython_functions/{backtest_engine,backtrader_from_signals,backtrader_pair_from_signals}`,构建脚本 `_build_exts.py`)。

注:`data/test/*.csv` 内置样本数据因仓库全局 `*.csv` 忽略规则不入 git;已实测 `LocalDatas` 在缺该目录时 import 与回测均正常,镜像重建不受影响。

## 依赖锁定(试点 venv 实测组合)

numpy==2.2.6 / pandas==2.3.3 / pandas-ta==0.4.71b0 / numba==0.61.2 / duckdb / networkx==3.6.1 / numpy-ext==0.9.9(**必须 --no-deps 安装**,否则会把 numpy 拖到 1.x)/ TA-Lib==0.7.1(官方 wheel 自带 C)/ finta==1.3 / quantstats==0.0.81 / statsmodels / scipy / joblib / cython>=3。可选保险:arch、pykalman(惰性使用)。**不装**:tqsdk(TqTa/TqFunc 指标库随之不可用)、tulipy(gcc15 编译失败,功能被 TA-Lib 覆盖)。

## 回测口径差异(写策略前必读)

- **信号当根 K 线收盘价成交**(默认 `OrderType.Close`);`OrderType.Market` 才是下根开盘
- 信号 `.new` 单次消费:持多时 `sell()` 只平不开空,反手需显式两步
- **无 A 股规则**:T+1、涨跌停、整手、ST 均不建模;结果与实盘存在系统性口径差
- **默认手续费=0**,需显式配 `self.percent_commission` 等(StrategyBase setter)
- minibt 自带报告的"最终收益"**不含未平仓浮盈**(保证金式记账);结构化统计用 `backend/shared/minibt_result.py::run_and_report`
- 桌面功能(PyQt 实时图表/light_chart 回放/bokeh)在服务端一律禁用:`Bt().run(isplot=False, isreport=False)`

## 引擎行为基准(试点验证 2026-09-08)

双均线策略(600036.SH 全量 2595 根日线)与独立 pandas 复现逐笔 67/67 一致、持仓序列零偏差;近 4 年窗口 27/27 一致。复现脚本在 `/tmp/minibt-pilot/replica.py`(会话级,不随仓库)。
