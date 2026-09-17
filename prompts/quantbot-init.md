---
name: quantbot-init
title: QuantBot 环境初始化
category: 平台运营
description: 首次使用 QuantBot 时，检查技能、人格与平台对接是否就绪
outputs: dsh 技能/人格就绪清单
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请完成 QuantBot（dsh / DeepSeek Harness）与 QuantMind 平台的对接检查：

1. 检查技能：列出你能看到的技能（应来自 /root/.dsh/skills，含 daily-review、stock-research、quantdb-fields 等）；
2. 检查人格与规则：确认 /root/.dsh/AGENTS.md（技能路由表、平台 API、挂载地图、术语映射）已生效，复述你的身份与关键挂载路径；
3. 环境自检：确认你能访问后端 API（http://quantmind:8000，内部认证见 AGENTS.md）、能读写 /data/reports/、能 `docker exec quantmind` 跑重依赖脚本；
4. 输出一份环境就绪清单：哪些能力可用、哪些缺失、如何补齐（缺失时给出修复命令，如 `docker compose restart dsh`）。
