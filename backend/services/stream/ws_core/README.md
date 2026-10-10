# QuantMind WebSocket 推送核心（ws_core）

`ws_core` 是 Stream Gateway 中负责**实时双向通信**的推送引擎，为前端提供低延迟的行情与指标数据推送。

## 近期更新

- 2026-04-02：Bridge 会话校验能力改为复用 `backend.shared.qmt_bridge_auth`，不再直接依赖 trade 服务内部鉴权模块。

---

## 模块说明

| 文件 | 职责 |
|------|------|
| `server.py` | 服务器生命周期管理；`/ws` 端点；消息路由（subscribe / ping 等）|
| `manager.py` | 连接管理器：建立/断开连接、订阅主题、向 topic 发布消息、心跳超时清理 |
| `message_queue.py` | 异步优先级消息队列：背压控制（阈值 5000）、批量消费（10条/批）、3次重试 |
| `quote_pusher.py` | 行情推送器：读取远程 Redis 快照，推送前写入时序并落库 |
| `notification_pusher.py` | 通知推送器：消费 Redis Stream `notification_events`，按 `notification.{user_id}` 推送 |
| `indicator_pusher.py` | 指标推送器：每2秒计算并推送 MACD / RSI / BOLL / KDJ / EMA / TRIX |
| `indicators.py` | 技术指标计算类（占位符，待实现） |
| `ws_config.py` | WebSocket 服务配置（心跳间隔、最大连接数等）|

---

## 数据源（quote_pusher）

**当前版本使用远程 Redis 行情快照作为主数据来源：**

```
远程行情 Redis（resolve_remote_quote_redis：env > 根 .env > 官方公共免费服）
  market:snapshot:{symbol} (主路径)
  stock:{code}.{market} (兼容回退)
                         →  RemoteRedisDataSource.fetch_quotes()（透传快照 source）
                         →  QuotePusher._centralized_push_loop()
                         →  quotes 表落库（PostgreSQL，data_source 标注来源）
                         →  manager.publish("stock.{code}", {...含 data_source})
                         →  已订阅的 WebSocket 客户端

兜底：快照缺失的标的回退 QuantDB 本地日线（data_source=quantdb，仅落库/推送）。

T4-4（2026-10-10，审计 H14）：不再回写 market:series——此前 QuantDB 兜底
（当日零点时间戳 + is_stale=false）被当盘中时序点写进行情 Redis 市场键，与真实
席位点混杂（伪实时、全链零提示）。market:series 的唯一写侧 = 行情席位
（tdx_hot_set_feed / qmt_quote_backup / tdx_aidata），本服务只是消费方。
```

当前采用中心化轮询，不再按股票创建独立协程；默认 `push_interval=2.0秒`，仅当价格变化时才推送。

---

## WebSocket 协议

### 连接地址
```
ws://localhost:8003/ws
```

### 消息类型

#### 客户端 → 服务端

| type | 参数 | 说明 |
|------|------|------|
| `ping` | - | 心跳保活，服务端回 `pong` |
| `subscribe` | `"topic": "stock.600519.SH"` | 订阅行情/指标推送 |
| `unsubscribe` | `"topic": "stock.600519.SH"` | 取消订阅 |

#### Topic 命名规范

| Topic | 说明 |
|-------|------|
| `stock.{code}.{market}` | 行情推送，如 `stock.600519.SH` |
| `notification.{user_id}` | 用户通知推送，如 `notification.1001` |
| `indicator.{code}.{market}.{name}` | 指标推送，如 `indicator.600519.SH.MACD` |

#### 服务端 → 客户端

```jsonc
// 连接成功
{ "type": "welcome", "connection_id": "uuid", "message": "连接成功" }

// 订阅确认
{ "type": "subscribed", "topic": "stock.600519.SH" }

// 行情推送
{
  "type": "quote",
  "stock_code": "600519.SH",
  "data": {
    "price": 1485.30,     // 当前价（远程 Redis Now 字段）
    "open": 1486.60,      // 开盘价
    "high": null,         // 最高价（数据源暂不提供）
    "low": null,          // 最低价（数据源暂不提供）
    "volume": 41679,      // 成交量（手）
    "amount": 6216379400, // 成交额（元）
    "timestamp": "2026-02-19T14:10:08"
  },
  "timestamp": 1739938208.0
}

// 心跳响应
{ "type": "pong" }
```

---

## 消息队列参数

| 参数 | 值 | 说明 |
|------|----|------|
| 队列上限 | 10,000 条 | 超限丢弃 |
| 背压阈值 | 5,000 条 | 触发后丢弃 LOW 优先级消息 |
| 批消费大小 | 10 条/批 | 降低 WebSocket send 次数 |
| 失败重试 | 最多 3 次 | 超限断开连接 |

---

## 心跳配置

| 参数 | 值 |
|------|----|
| 检测间隔 | 30 秒 |
| 超时阈值 | 90 秒无心跳则断开 |

---

## 已知问题

- `indicators.py` 中 MACD / RSI / BOLL / KDJ / EMA / TRIX 的 `calculate()` 均返回空字典，**待实现真实计算逻辑**。
- `indicator_pusher` 历史 K 线获取路径有 `TODO`，当前使用随机模拟数据。

---

*最后更新：2026-03-02*
