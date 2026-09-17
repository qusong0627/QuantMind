---
name: a-share-tradability-audit
description: "A股可交易性约束审计 — 把交易流放回历史行情，逐笔判定涨停封板买不进/跌停卖不出/停牌/T+1违约/裸卖空/碎股手数非法/参与率超限/新股窗口。用户问「回测收益是不是真的能成交」「这些成交合不合法」「涨停买不进」「幽灵收益」或要审计账本成交质量时使用。触发词：可交易性、可成交、幽灵收益、封板、碎股、T+1审计"
---

# a-share-tradability-audit — 可交易性约束审计

## ⚙️ 运行环境契约

1. **执行位置**：宿主机 `baymax` venv（需 duckdb/pandas）。
   入口：`/home/zbox/baymax/.venv/bin/python dsh/skills/a-share-tradability-audit/scripts/tradability_audit.py <子命令>`
2. **行情面板**：默认 quantdb `daily_backward`（后复权→比例法判封板，跨除权日可能漏判）；
   `--source bridge` 用通达信桥未复权日K（可算精确涨跌停价，逐票限流 1/s，仅小集合）。
3. **制度规则**：`scripts/ashare_rules.py`（板块带宽按日期解析，含 2026-07-06 主板 ST ±10% 沿革）。
4. **交易流**：默认 `logs/live_trade_*.jsonl`（剔除拒单、order_id 去重）；也支持通用 JSONL。

## 能力地图

| 用法 | 用途 |
|---|---|
| （无参） | 审计账本全部真实成交 → 逐笔判定 + 分规则汇总 + JSON 报告 logs/tradability_audit/ |
| `--selftest` | 合成用例自检（封板买/裸卖空/封板卖/T+1/碎股） |
| `--trades x.jsonl --start --end --participation 0.05` | 审计回测/信号交易流 |

## 边界

复权序列上不输出精确涨跌停价；回答"多少成交是市场不会给你的"，不做收益归因。


## dsh/容器内执行契约（QwenPaw/dsh agent 必读）

dsh 容器无 python3，但有 docker CLI，且本工具与依赖已只读挂载在 `/app/quant-scripts`、
配置在 `/app/quant-configs`、事件缓存在 `/app/quant-events`。重依赖（duckdb/pandas）在
**quantmind 容器**内执行，两步：

```bash
# ① 拷工具+依赖+配置（tradability_audit 换成对应工具名即可）
docker exec quantmind mkdir -p /tmp/qskills/configs
docker cp /app/quant-scripts/ashare_rules.py /app/quant-scripts/trading_cal.py /app/quant-scripts/tradability_audit.py quantmind:/tmp/qskills/
docker cp /app/quant-configs/trading_days.json quantmind:/tmp/qskills/configs/trading_days.json
# ② 执行（quantmind 容器内 /data/quantdb 为完整数据，工具自动识别）
docker exec -w /tmp/qskills quantmind python3 tradability_audit.py [参数]
```

报告落容器 `/tmp/qskills/logs/`，用 `docker exec quantmind cat /tmp/qskills/logs/...` 取回。
事件雷达 tag 需额外：`docker cp /app/quant-events quantmind:/tmp/qskills/events` 并加
`-e EVENT_RADAR_DIR=/tmp/qskills/events`；collect 仅宿主机执行（需 akshare）。
