---
name: a-share-event-radar
description: "A股事件雷达（akshare→东财）— 解禁/股权质押/回购的采集缓存与标的标签。用户说「解禁」「质押」「回购」「事件风险」「持仓有什么事件」「候选池事件标注」时使用。触发词：解禁、质押、回购、事件雷达、限售股、增减持"
---

# a-share-event-radar — A股事件雷达

## ⚙️ 运行环境契约

1. **执行位置**：宿主机 `baymax` venv（需 pandas/pyarrow/akshare 1.18+）。
   入口：`/home/zbox/baymax/.venv/bin/python dsh/skills/a-share-event-radar/scripts/event_radar.py <子命令>`
2. **数据源**：akshare → 东方财富数据中心（免费无 token）。字段可能随上游变动——
   解析失败静默降级为空标签，绝不阻塞研究/复盘主流程。
3. **缓存**：`data/events/{kind}_{yyyymmdd}.parquet`，当日已采集默认跳过（`--refresh` 强制）。
4. **代码口径**：akshare 无后缀 ↔ 本系统带后缀，按 6 位前缀匹配。

## 能力地图

| 子命令 | 用途 |
|---|---|
| `collect [--refresh]` | 采集解禁（未来30日）/质押（最近周五·周频）/回购（全量）落缓存 |
| `tag --codes A,B [--horizon 10]` | 标的标签：未来N日解禁(占流通盘%)、质押比例≥50%、回购进行中 |
| `summary` | 缓存状态 |

## 消费方

- `night_pool_agent`：候选池自动带【事件】字段（建仓前风险核对）
- `post_review`：复盘 facts 附【持仓事件雷达】

## 边界

事件标签是风险提示不是买卖指令；akshare 免费源无 SLA，字段变更需人工修脚本。


## dsh/容器内执行契约（QwenPaw/dsh agent 必读）

dsh 容器无 python3，但有 docker CLI，且本工具与依赖已只读挂载在 `/app/quant-scripts`、
配置在 `/app/quant-configs`、事件缓存在 `/app/quant-events`。重依赖（duckdb/pandas）在
**quantmind 容器**内执行，两步：

```bash
# ① 拷工具+依赖+配置（tradability_audit 换成对应工具名即可）
docker exec quantmind mkdir -p /tmp/qskills/configs
docker cp /app/quant-scripts/ashare_rules.py /app/quant-scripts/trading_cal.py /app/quant-scripts/event_radar.py quantmind:/tmp/qskills/
docker cp /app/quant-configs/trading_days.json quantmind:/tmp/qskills/configs/trading_days.json
# ② 执行（quantmind 容器内 /data/quantdb 为完整数据，工具自动识别）
docker exec -w /tmp/qskills quantmind python3 event_radar.py [参数]
```

报告落容器 `/tmp/qskills/logs/`，用 `docker exec quantmind cat /tmp/qskills/logs/...` 取回。
事件雷达 tag 需额外：`docker cp /app/quant-events quantmind:/tmp/qskills/events` 并加
`-e EVENT_RADAR_DIR=/tmp/qskills/events`；collect 仅宿主机执行（需 akshare）。
