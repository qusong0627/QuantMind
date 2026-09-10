# bigqmt_signal_trader

大 QMT 信号交易包的核心骨架。当前版本只完成可替换包边界和 dry-run 运行入口，不会发送真实委托。

## 已完成

- `TradeSignal`、`OrderRequest`、`PositionSnapshot`、`AccountSnapshot` 等核心数据模型。
- `SignalSource`、`MarketDataProvider`、`PositionProvider`、`OrderGateway`、`PositionSyncSink`、`StateStore` 等替换接口。
- `SignalTradingApp.tick()` 编排流程：
  1. 读取信号。
  2. 原子 claim。
  3. 读取持仓。
  4. 计算买卖数量。
  5. 生成价格。
  6. 调用可替换 `OrderGateway`。
  7. 写回状态。
  8. 同步持仓快照。
- `DryRunOrderGateway`：记录委托请求，不调用真实 `passorder`。
- `bigqmt_signal_trader_strategy.py`：大 QMT 运行文件骨架，响应 `init`、`adjust`、`order_callback`、`deal_callback`、`sync_positions`。

## 当前安全状态

默认 `adapter_factory.build_app()` 使用：

- 空信号源。
- 空行情源。
- 空持仓源。
- dry-run 下单 gateway。
- no-op 状态存储。
- 内存持仓同步 sink。

因此即使大 QMT 加载该运行文件，也不会真实下单。

## 后续接入顺序

1. 实现 `BigQmtMarketDataProvider` 和 `BigQmtPositionProvider`。
2. 实现 `BigQmtOrderGateway(passorder/cancel/get_trade_detail_data)`。
3. 实现 Redis Stream / MySQL outbox 信号源。
4. 实现 Redis / MySQL 状态写回。
5. 实现 Redis / MySQL 持仓同步 sink。
6. dry-run 跑通后，再按账户灰度切换真实下单。

## 测试

```powershell
cd <REPO_ROOT>
python -m unittest discover -s tests\bigqmt_signal_trader
```


