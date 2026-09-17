---
name: quantdb-data-structure
title: QuantDB 数据结构
category: 平台运营
description: 数据目录组织、Hive 分区、parquet 路径、代码格式、quantdb_hub 读取入口、quantdb vs quantcustom
outputs: 结构说明 / 查询路径
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我需要了解 QuantDB 本地数据结构或排查查不到数据：{问题，如：l1_factors 在哪 / dt 分区怎么写 / 600519.SH 还是 SH600519 / 因子挖掘产物落哪}。

请读取 skills/quantdb-data-structure/SKILL.md（数据在哪、怎么组织、怎么读）；字段单位另查 skills/quantdb-fields/SKILL.md。说明 quantdb（官方只读）与 quantcustom（用户/挖掘产出）边界，给出可直接用的路径或 DuckDB 视图名。
