# 数据边界与不做的事（source-boundary，本地版）

本技能只对**已存在的因子面板 + 本地 QuantDB** 做正交化与暴露诊断，不预测、不喊单；
正交化是研究处理，不改变「因子未来是否有效」的不确定性。

## 允许的数据来源

- **本地 QuantDB（quantdb 模式，主路径）**：只读本地 parquet——
  - 因子 / 规模 / 风格列：`quantdb/6_ml_datasets/features_daily`（CN 默认）、`l1_factors`/`l2_factors`；
    HK/US 为 `quanthk|quantus/6_ml_datasets/l1_factors`。
  - 收益：`quantdb|quanthk|quantus/1_kline_data/daily_forward`（CN 前复权；HK/US 不复权，
    口径差异写入报告 caveats；HK 双来源重复行按 `published_at` 去重保留 akshare 原始价）。
  - 行业分类：`quantdb/2_base_sector/instrument_detail`（CN，静态快照）、
    `quanthk/2_base_sector/akshare_profile`（HK）、`quantus/2_base_sector/sector`（US）——
    均为一次性读取，不随窗口变化。
  - 不连任何远程服务、不连 PG/Redis、不触发同步任务、**不写 `data/` 下任何数据**。
- **内置合成演示（demo 模式）**：确定性 LCG 面板，纯标准库，无外部数据依赖。

## 需要用户权利与明确提供才可用的

- 付费墙内 / 会员专享行情或因子库的未授权抓取。
- 在本技能内写券商 / 私有库连接器、SQL 适配器、自动爬取——**不内置**。

## 本技能不做的事

- 不构造因子表达式、不训练模型、不做组合回测（Sharpe / MDD / 组合换手不产出——
  需要时把残差因子交给回测类技能）。
- 不输出买卖指令与仓位建议；不把「残差因子」宣称为可交易信号。
- 不负责因子的时点正确性（因子列是否只用截至当日可知信息，由数据生产方保证）；
  脚本仅硬拒绝明显的前视标签列（`label_return` / `return_{n}d` / `future_return_{n}d`），
  拒绝把它们当因子或控制变量。
- 不做风险模型的完整估计（不做协方差、不做因子收益回归归因——那属于 Barra 类技能）。

## 输出边界

- 只输出：正交前后相关矩阵、基准暴露（规模 corr / 行业 R² / 行业 max|z 组均值|）、
  逐因子 IC 保真与保留率、逐日处理计数、独立校验（相关矩阵恒等 / 残差零暴露 /
  FWL vs numpy 对拍 / demo 金样），JSON + stdout 表格。
- 每个结论须标注：因子名、数据窗口、票池口径、观测数；数据缺口（HK 未填充列、
  低填充日、缺行业分类）如实计入跳过计数与 caveats，不补齐、不猜测。
- 必须声明：暴露清零是**恒等式**（回归的数学性质），IC 保真是**历史证据描述**，
  两者都不是未来表现的承诺。

## 已知局限（实测记录）

- CN 行业分类 `rs_hyname` 为静态快照（HqDate=20260720），行业重分类不回填历史——
  存在分类层面的前视/陈旧偏差，与 brinson-performance-attribution 同源同口径。
- HK 无可用市值列时自动退回「对数成交额」规模代理，口径为**规模+流动性混合**，
  与非代理市场（CN/US）的规模中性化不完全可比。
- 票池为「窗口末日成交额前 N」的事后选池（含轻微选择偏差，非逐日动态池）。
- IC 保留率仅在 |原始 mean IC| ≥ 0.005 时给出（分母低于此的比值是噪声）。
- 行业 R² 存在「哑变量个数 / 截面宽度」基数效应（见 methods.md），只看前后差。

## 许可边界

- 方法论移植自 [quantskills/skill-factor-orthogonalize](https://github.com/quantskills/skill-factor-orthogonalize)
  （**GPL-3.0-only**，Copyright (C) 2026 QuantSkills）。本地文件为本地化改写产物，
  如对外分发需遵循 GPL-3.0-only。
- 仅限本地研究使用；分析结论不构成投资建议。
