---
name: quantmind-deploy
title: 部署与运维
category: 平台运营
description: 一键部署、快速部署、数据库初始化、部署问题排查、服务健康检查
outputs: 服务状态报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我需要部署或排查 QuantMind 平台：{部署新服务器 / 排查部署失败 / 检查服务健康}。

请读取 skills/quantmind-deploy/SKILL.md，按其中对应章节执行；服务健康检查可用 docker compose ps 与各服务 /health 端点。发现问题给出根因和修复步骤，重大变更操作前先向我确认。
