---
name: quantmind-operations
title: 平台运营总指南
category: 平台运营
description: 模型训练（5 步流程、特征字典、AutoDL 节点）、模型管理、数据同步、RSS 新闻
outputs: 视具体操作而定
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我需要执行平台运营操作：{操作内容，如：训练 lightgbm / 查特征字典 / 更新今日数据 / 对接 RSS}。

请读取 skills/quantmind-operations/SKILL.md，按对应章节执行（训练走 5 步：特征选择→训练目标→参数→执行→入库；特征类别以 /api/v1/models/feature-catalog 动态返回为准，勿硬编码）。遵守顶部运行环境契约；涉及数据同步请确认脚本执行结果；完成后给我操作结果速览。
