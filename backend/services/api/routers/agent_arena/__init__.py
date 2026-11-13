"""QuantMind 原生 arena 路由 —— `/api/v1/agent-arena/*`（2026-09-29 起替换代理）。

「实盘交易」栏里 arena 页面（智能体交易 / 实况 / 行情回测 / 总控 / 数据 / 关于）
的数据面。历史：这些页面原先经 `agent_arena_proxy.py` 转发到本机
quant-Trader（baymax-api:8091）；上游退役后代理全线 503，用户裁决**不恢复隔壁、
直接迁进 QuantMind**——本包按 `arena/api/client.ts` 的类型契约逐族原生实现，
baseURL 与响应形状（`{success, data}` 信封）与代理时代逐位相同，前端零改动。

族划分（与 `docs/local/arena-ui-native-migration-plan.md` §3 对齐）：
- `agents_family` 族 1：agent 台账 / 总控聚合 / 价格 / 名称 / token
- `market_lab_family` 族 2：行情实验室（K线/策略库/批量产物只读 + 单标的回测执行）
- `live_family` 族 3（A4）：实况全套（账户/成交/分账/净值/平仓/实盘快照/台账/L2/指数/新闻）
- `bridge_family` 族 6（A4 余量 + A5 写面收口）：桥与券商面（tdx/config GET+POST、
  tdx/overview、qmt/account、qmt/status、broker-config GET/PUT/test、real-trading/status）
- `config_family` 配置族：comp-config + broker-market
- `static_family` 族 8：数据根静态文件
- `data_panel` 族 9（A5）：数据面板（/data-platform/* 6 端点，多市场 parquet 浏览/预览）
- `quantmind_family` 族 10（A5）：/quantmind/* 受限透传（tdx 配置面 → trade；
  admin/data-platform → 回环 api）
- `analysis_family` 族 11（2026-10-08）：分析触发（/analysis/trigger、/analysis/jobs）
  ——「立即分析」两枚按钮的台账写入与回显，消费端是宿主 cron worker
- `futu_family` 族 12（2026-10-08）：富途港股直连（/futu/* 7 端点——双卡账户、
  委托、已平仓、快照、下单/撤单），旧栈 `/api/futu/*` 的恢复实现；执行端是
  本机容器化 FutuOpenD，REAL 写面 fail-closed（闸门 + 解锁凭据）

本包与 `backend/services/agent_arena/`（服务层）以及
`electron/src/features/local-live/` 自 2026-11-13 起**随仓分发**（见 .gitignore）；
运行期数据仍不入仓。main.py 的 try/except 注册保留给老检出/裁剪产物：缺包时
静默跳过，端点树与引入前逐位相同。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.services.agent_arena.arena_config import data_root
from backend.services.api.routers.agent_arena import (
    agents_family,
    analysis_family,
    bridge_family,
    config_family,
    data_panel,
    futu_family,
    live_family,
    market_lab_family,
    quantmind_family,
    static_family,
)
from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter(prefix="/api/v1/agent-arena", tags=["AgentArena"])
router.include_router(agents_family.router)
router.include_router(market_lab_family.router)
router.include_router(live_family.router)
router.include_router(bridge_family.router)
router.include_router(config_family.router)
router.include_router(static_family.router)
router.include_router(data_panel.router)
router.include_router(quantmind_family.router)
router.include_router(analysis_family.router)
router.include_router(futu_family.router)


@router.get("/_native/health")
def arena_native_health(_current_user: dict = Depends(get_current_user)):
    """原生数据面自检（排障用）：数据根解析到哪、落盘封印在不在。

    路由在 `_native/` 下：`/api/v1/agent-arena/health` 是 arena 自己的
    路由空间（若上游曾有），不能被我们占位。
    """
    root = data_root()
    return {
        "success": True,
        "data": {
            "mode": "native",
            "data_root": str(root),
            "data_root_exists": root.is_dir(),
            "manifest_ok": (
                root / "arena" / "quanttrader" / "MANIFEST.jsonl"
            ).is_file(),
        },
    }
