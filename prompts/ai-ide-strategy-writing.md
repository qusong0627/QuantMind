---
name: ai-ide-strategy-writing
title: AI 写量化策略
category: 策略·因子·模型·回测
description: AI 生成 Qlib 量化策略代码、Docker 容器执行、策略落库
outputs: 策略代码 + 落库结果
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请帮我写一个量化策略：{策略想法，如：低波动+高股息双因子选股，每周调仓}。

请读取 skills/ai-ide-strategy-writing/SKILL.md，按其规范生成 Qlib 策略代码，在 Docker runner 中执行验证可运行，然后落库保存。给我策略逻辑说明、代码位置和执行结果。
