---
name: copilot-advice
description: "副驾驶实时上下文与建议卡（QuantBot 工具化查询，T-P6-16）——查持仓/当日信号/近期告警/市场状态（/api/v1/copilot/context），生成建议卡（/api/v1/copilot/advice，带依据 context_refs），由用户在交易台一键执行或拒绝（执行走 OrderRouter，来源 co_pilot 留痕）。用户说「实时上下文」「我的持仓/信号/告警」「给个操作建议」「建议卡」「副驾驶」时使用。触发词：副驾驶、实时上下文、操作建议、建议卡、持仓快照、当日信号、今日告警"
---

> ## ⚙️ 运行环境契约（最高优先级）
>
> 1. **平台接口走内部调用头**：`X-Internal-Call: <共享密钥>` + `X-User-Id: <用户业务ID>`（与
>    dsh AGENTS.md 约定一致）；基础地址 = QuantMind API 服务（容器内 `http://quantmind:8000`，
>    浏览器环境用站点同源 `/api/v1`）。
> 2. **纪律：QuantBot 只提建议、不代下单**——执行与否由用户在交易台「副驾驶」面板决定
>    （一键执行走 OrderRouter，来源 `co_pilot` 全程留痕）；接口校验失败一律 400，不静默降级。
> 3. **块级可用性如实**：context 各块带 `available/source/reason`；某块不可用时如实说明，
>    不要拿旧数据当实时数据。
> 4. **身份约定**：内部调用用 `X-User-Id: qwenpaw`（平台默认）——以此身份创建的建议卡落
>    **租户共享（user_id=0）**，交易台全员可见并可决策；若需归属到具体用户，在 body 里带
>    `context_refs.target_user_id` 并在会话上下文中说明。

# copilot-advice — 副驾驶实时上下文 + 建议卡

## 1. 实时上下文（工具化查询，非 prompt 拼接）

```bash
curl -s -H "X-Internal-Call: $QM_INTERNAL_KEY" -H "X-User-Id: qwenpaw" \
  http://quantmind:8000/api/v1/copilot/context | jq .data
```

返回块：`positions`（模拟账户持仓+现金+总资产）、`signals`（`engine_signal_scores` 最新日
Top20 + side_counts）、`alerts`（近 24h 哨兵告警：类型/级别/命中/标注）、`market_state`
（热集规模 + 日内 regime）、每块 `source`；不可用块 `available=false + reason`。

## 2. 生成建议卡

```bash
curl -s -X POST http://quantmind:8000/api/v1/copilot/advice \
  -H "X-Internal-Call: $QM_INTERNAL_KEY" -H "X-User-Id: qwenpaw" -H 'Content-Type: application/json' \
  -d '{
    "title": "止盈：600036.SH 触及目标价",
    "rationale": "信号仍为正但涨幅达目标；参考当日新闻中性",
    "actions": [{"symbol": "600036.SH", "side": "sell", "quantity": 200}],
    "context_refs": {"signal": {"trade_date": "2026-09-15", "symbol": "600036.SH"},
                     "alert_id": "…（可选：下钻到具体总线事件）"}
  }'
```

- `actions` 每条：`symbol`（A 股，可写 `600036` / `SH600036` / `600036.SH`，服务端归一）、
  `side`（buy|sell）、`quantity`、`order_type`（market|limit）、`price`（限价必填）；
  重复动作/非法字段 → 400（资金相关不静默）。
- `context_refs`：**依据下钻**（信号、告警 alert_id、行情快照键）——用户点建议卡可回溯原始数据。
- v1 仅支持 A 股（CN）建议；港股/美股建议会被 400 拒绝（后续版本放开）。

## 3. 用户决策（交易台「副驾驶」面板）

- 面板展示事件流（可标注 真实/误报，进误报率口径 T-P6-15）与建议卡；
- **一键执行**：`POST /api/v1/copilot/advice/{advice_id}/execute` —— 逐动作走 OrderRouter
  唯一入口（来源 `co_pilot`、幂等键 `cop-{advice}-{sym}-{side}`，重复点击不重复下单）；
- **拒绝**：`POST /api/v1/copilot/advice/{advice_id}/reject`（理由留痕）；
- 建议卡状态：`pending → executed | partial | failed | rejected`（定局后不可再改）。

## 4. 写建议前先读什么（机构口径）

1. `context.signals`：当日信号方向分布与 Top——建议应可追溯到具体信号；
2. `context.alerts`：若标的在近 24h 有 `critical` 风险告警（news:risk_event 等），
   **不要**给买入建议（当日已有 veto 标记，订单会被风控拦截）；
3. `context.positions`：卖出建议的数量不得超过可用持仓；
4. `market_state.regime`：regime 为 bear 时买入建议应显著收敛。
