---
name: a-share-pit-financial
description: "A股财务 PIT 快照与防前视审计 — 按公告日构建任意历史时点的财务快照（T+1 交易日可用），派生营收/净利 TTM、ROA、负债率、同比；全市场 naive-vs-PIT 泄漏审计。用户做财务因子回测、问「这个因子是不是偷看未来财报」「重述泄漏」时使用。触发词：PIT、防前视、前视偏差、重述、泄漏、财务快照、TTM"
---

# a-share-pit-financial — 财务 PIT 快照 / 防前视审计

## ⚙️ 运行环境契约

1. **执行位置**：宿主机 `baymax` venv（需 duckdb）。
   入口：`/home/zbox/baymax/.venv/bin/python dsh/skills/a-share-pit-financial/scripts/pit_financial.py <子命令>`
2. **数据源**：quantdb `3_financial_data/{balance,income,cashflow}/{code}.parquet`
   （m_timetag 报告期 + m_anntime 公告日；income 为**年初至今累计**，TTM 由累计→单季推算，
   Q1 单季=Q1 累计，不得减上年 12 月）。
3. **PIT 规则**：公告日 + 1 个交易日（`--lag-days`）后可用；同报告期取可用范围内最新版本。

## 能力地图

| 用法 | 用途 |
|---|---|
| `--asof 2026-09-03 --codes 600309.SH,688183.SH` | 指定标的 PIT 快照 + TTM/ROA/负债率/同比 |
| `--asof ... --audit-naive` | 全市场 naive-vs-PIT 泄漏审计（2025-06-01 实测 naive 100% 家数泄漏） |
| `--asof ... --full-market` | 全市场快照落 logs/pit/ |

## 边界

只保证"输入只含当时可见数据"；因子经济含义与收益预测不在本技能范围。


## dsh/容器内执行契约（QwenPaw/dsh agent 必读）

dsh 容器无 python3，但有 docker CLI，且本工具与依赖已只读挂载在 `/app/quant-scripts`、
配置在 `/app/quant-configs`、事件缓存在 `/app/quant-events`。重依赖（duckdb/pandas）在
**quantmind 容器**内执行，两步：

```bash
# ① 拷工具+依赖+配置（tradability_audit 换成对应工具名即可）
docker exec quantmind mkdir -p /tmp/qskills/configs
docker cp /app/quant-scripts/ashare_rules.py /app/quant-scripts/trading_cal.py /app/quant-scripts/pit_financial.py quantmind:/tmp/qskills/
docker cp /app/quant-configs/trading_days.json quantmind:/tmp/qskills/configs/trading_days.json
# ② 执行（quantmind 容器内 /data/quantdb 为完整数据，工具自动识别）
docker exec -w /tmp/qskills quantmind python3 pit_financial.py [参数]
```

报告落容器 `/tmp/qskills/logs/`，用 `docker exec quantmind cat /tmp/qskills/logs/...` 取回。
事件雷达 tag 需额外：`docker cp /app/quant-events quantmind:/tmp/qskills/events` 并加
`-e EVENT_RADAR_DIR=/tmp/qskills/events`；collect 仅宿主机执行（需 akshare）。
