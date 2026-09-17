---
name: a-share-hypothesis-lab
description: "A股假设库实验室 — 把定性认知变成带胜率的可验证假设，walk-forward 滚动窗纪律（OOS 与 IS 同向 + 同向窗占比≥60% + n≥60 才算 verified）。用户说「验证一下这个想法」「假设库」「胜率复测」「这个规律还成立吗」时使用。触发词：假设、验证、胜率、walk-forward、OOS、复测、假设库"
---

# a-share-hypothesis-lab — 假设库实验室（walk-forward 纪律）

## ⚙️ 运行环境契约

1. **执行位置**：宿主机 `baymax` venv（需 pandas/duckdb）。
   入口：`/home/zbox/baymax/.venv/bin/python dsh/skills/a-share-hypothesis-lab/scripts/hypothesis_lab.py`
2. **数据源**：quantdb `daily_backward`（流动性前 N 只）。
3. **纪律**：全样本 pooled 只作参考；verified 须 OOS 与 IS 同向 + 同向窗占比 ≥60% + n≥60；
   contradicted（反向证据明确）维持。结果写回 `configs/hypotheses.json`（含 wf 块）。

## 能力地图

| 用法 | 用途 |
|---|---|
| （默认 60 只 × 260 日） | 全量复测 4 条价格代理假设 → 更新假设库 |
| `--symbols 30 --days 130 --wf-window 40` | 快速验证 |

## 边界

现有假设为价格代理（待事件数据升级）；pooled 胜率不得在提示词中当已验证结论使用。


## dsh/容器内执行契约（QwenPaw/dsh agent 必读）

dsh 容器无 python3，但有 docker CLI，且本工具与依赖已只读挂载在 `/app/quant-scripts`、
配置在 `/app/quant-configs`、事件缓存在 `/app/quant-events`。重依赖（duckdb/pandas）在
**quantmind 容器**内执行，两步：

```bash
# ① 拷工具+依赖+配置（tradability_audit 换成对应工具名即可）
docker exec quantmind mkdir -p /tmp/qskills/configs
docker cp /app/quant-scripts/ashare_rules.py /app/quant-scripts/trading_cal.py /app/quant-scripts/hypothesis_lab.py quantmind:/tmp/qskills/
docker cp /app/quant-configs/trading_days.json quantmind:/tmp/qskills/configs/trading_days.json
# ② 执行（quantmind 容器内 /data/quantdb 为完整数据，工具自动识别）
docker exec -w /tmp/qskills quantmind python3 hypothesis_lab.py [参数]
```

报告落容器 `/tmp/qskills/logs/`，用 `docker exec quantmind cat /tmp/qskills/logs/...` 取回。
事件雷达 tag 需额外：`docker cp /app/quant-events quantmind:/tmp/qskills/events` 并加
`-e EVENT_RADAR_DIR=/tmp/qskills/events`；collect 仅宿主机执行（需 akshare）。
