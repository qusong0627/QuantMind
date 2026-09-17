---
name: factor-report
title: 因子体检报告（分位·换手·相关性）
category: 策略·因子·模型·回测
description: Alpha 库 429 因子的 Alphalens 式体检：分位收益单调性、换手率、相关性去重
outputs: 因子体检结论 + 入选/剔除清单
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请给因子做体检：范围 {全部 429 个 / 指定因子如 a158_ROC20 / 某个库 alpha101}，前瞻期 T+{1/2/5/10/20}。

数据源是技能中心「因子报告」页签背后的接口 /api/v1/factor-report（快照文件由 backend/scripts/build_factor_report.py 生成）。请按下面四步走：

1. 先看快照元数据（窗口起止、样本天数、生成时间）：快照缺失或超过一周，先在服务器执行 `docker exec quantmind python3 backend/scripts/build_factor_report.py`；
2. 按 |IC| / ICIR 列出候选因子，逐个核对三件事：分位收益是否单调（单调性接近 ±1）、多空价差 Q10−Q1 有多大、单边换手率多少（换手 >50% 的因子要把交易成本算进去）；
3. 用相关性矩阵找出 |ρ|>0.9 的重复因子（同一簇只保留 ICIR 最高的一个，其余剔除并说明与谁重复）；
4. 输出 Markdown 体检报告：入选因子清单（每个附 IC/ICIR/多空/换手/相关性证据）、剔除理由、以及建议的组合权重思路，落到 /data/reports/。
