"""QuantMind 原生 arena 数据面（「实盘交易」栏的 arena 页面后端）。

**缘起（2026-09-29）**：quant-Trader（baymax-api:8091）随退役停机，实盘栏的
arena 页面（智能体交易/实况/行情回测/总控/数据/关于）全部 503。用户裁决
**不恢复隔壁、直接迁进 QuantMind**——本包就是那次迁移的服务层。

移植纪律（2026-11-13 起随仓分发；运行期数据仍不入仓）
--------------------------------------------------------------------
* 从 `/home/zbox/quant-Trader/` 搬来的模块是**逐字节副本 + 补丁头**：
  除文件顶部「移植注」列出的改写点外，与源逐字节一致（`diff` 可验）。
  上游目录已只读封存，改逻辑前先对 diff 确认没有第二处漂移。
* 新增模块（`arena_config` / `ttl_cache`）是本仓自写的胶水，不来自隔壁。
* 数据落点：QuantMind 数据根（容器 `/data` = 宿主 `<repo>/data`，A1 已用
  `legacy_assets` 工具逐字节搬入，`arena/quanttrader/MANIFEST.jsonl` 可复验）。
* 路由层在 `backend/services/api/routers/agent_arena/`；本包只管数据与业务，
  不 import FastAPI。
"""
