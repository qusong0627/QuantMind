---
name: rd-agent-factor-mining
title: 因子挖掘（RD-Agent）
category: 策略·因子·模型·回测
description: factor_pipeline 一键管线：preflight → 演化 → 回测 → IC 排序 → explain → export 至 quantcustom
outputs: 因子报告 + quantcustom 入库
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请帮我挖掘新因子：方向「{挖掘假设，如：筹码集中度上行伴随低位换手放大}」，股票池 {csi300 / 自定义全局池 code}，市场 {a_share 等}。

请读取 skills/rd-agent-factor-mining/SKILL.md：先跑 scripts/alpha_agent/factor_pipeline.py --check-env；再用 --direction / --universe / --loops 走一键管线（演化→回测→排名→可选 --explain-top / --export）。universe 支持内置指数与全局自定义池 code。产物落 /data/quantcustom（勿写 quantdb）。耗时长，分段汇报；最后给 Top 因子 IC/Sharpe 与是否 export 成功。
