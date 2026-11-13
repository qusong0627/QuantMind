"""arena 数据面 · 族 6（A4 余量）：桥与券商读取面。

移植自隔壁 `api_server.py` L727–860 + `backend/services/tdx_live.py`（上游
`/api/qmt/*`、`/api/tdx/*`、`/api/broker-config/{broker}`、`/api/real-trading/status`）。
四条 tdx/broker 端点由路由层**原样转发** QM trade 原生实现（`/api/v1/tdx/config`、
`/api/v1/tdx/overview`、`/api/v1/broker-config/{broker}`、`/api/v1/real-trading/status`
——trade 侧响应形状与上游逐字段一致，见 A4 执行记录），本模块只管两处口径适配：

1. ``/broker-config/qmt`` → trade 的 ``qmt_exec``（同一实体两处命名，见
   ``BROKER_ALIASES``）；label 换回上游文案「迅投 QMT」（trade 侧叫
   「大 QMT(执行端)」——上游 tdx_live.BROKER_LABELS 原话）。
2. ``/qmt/account``、``/qmt/status``：进程内直连大 QMT 执行端
   （``QmtExecClient``，与 trade 的账户同步任务走同一条 big-convert Redis RPC
   ——上游 baymax 也是进程内直连 ``QmtBridgeBroker``）。数据源改写记账：

   - 上游 ``QmtBridgeBroker._account_query()`` → QM ``get_asset()``＋
     ``get_positions()`` 重排回上游形状（``currency``/``balance`` 上游 TDX 桥
     同口径补齐；``last_price`` 上游是 ``round(mv/volume, 4)``，「0=拿不到价」
     是消费方约定，QM 持仓面没有现价字段，按同公式回填）。
   - 上游 ``QmtBridgeBroker.bridge_status()`` → 本仓新增
     ``QmtExecClient.bridge_status()``（同语义：不受本侧总闸约束的只读 ping）。
   - ``allow_trading`` = 本侧总闸：上游是 ``config/qmt_bridge.json`` 的
     ``allow_trading``；QM 侧对应执行端 ``enabled``（页面开关/env）。
   - **配置刷新是必须的**：页面配置 ``broker:config:qmt_exec`` 由
     ``refresh_settings()`` 拉取，trade 进程靠常驻任务每轮刷新，api 进程无人
     刷——本模块每次请求前自己刷一次（一次 Redis GET，代价可忽略）；否则
     页面开关开了也读 env 的旧值（env 无 QMT_EXEC_* 时恒定 enabled=false）。

错误语义照抄上游：桥失败不让路由层抛 HTTP——data 函数只负责形状，
``qmt_account``/``qmt_status`` 抛出的异常由路由层降级 ``{success:false,error}``。
"""

from __future__ import annotations

from typing import Any

# 上游 broker 名 → QM trade 原生 broker 名（/broker-config 面）
BROKER_ALIASES: dict[str, str] = {"qmt": "qmt_exec"}

# 回显 label 覆盖：只在两处命名不一致时改写（上游文案）
BROKER_LABEL_OVERRIDES: dict[str, str] = {"qmt": "迅投 QMT"}

# 上游 /broker-config/{broker} 暴露的券商集合（tdx_live.BROKER_FIELDS 四键；
# trade 侧另有 tdx/qmt_exec 等原生名，不在本面暴露）
_UPSTREAM_BROKERS: frozenset[str] = frozenset({"tiger", "futu", "ib", "qmt"})


def map_broker(name: str) -> str | None:
    """上游 broker 名 → trade 原生名；未知返回 None（路由层 404，学上游）。"""
    key = str(name or "").strip().lower()
    if key not in _UPSTREAM_BROKERS:
        return None
    return BROKER_ALIASES.get(key, key)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def qmt_account_data(
    asset: dict[str, Any], positions: list[dict[str, Any]]
) -> dict[str, Any]:
    """上游 ``/api/qmt/account`` 的 ``data`` 形状（api_server.py L737–743）。

    ``asset`` 是 dict（前端 ``QmtAccountRaw.asset`` 直接读四键）；QM
    ``get_asset()`` 只有平铺五键，这里补齐 currency/balance（``balance`` ≡
    ``total_asset``，上游 TDX 桥同口径）。持仓行按上游过滤清仓残留
    （``volume <= 0``）并同公式回填现价。
    """
    total = _num(asset.get("total_asset"))
    rows: list[dict[str, Any]] = []
    for p in positions or []:
        volume = _num(p.get("volume"))
        if volume <= 0:
            continue  # 清仓残留行：不算持仓（上游同款过滤）
        market_value = _num(p.get("market_value"))
        rows.append(
            {
                "stock_code": str(p.get("stock_code") or ""),
                "stock_name": str(p.get("instrument_name") or ""),
                # avg_price（摊薄成本）可为负——上游同取 avg_price→open_price
                "cost_price": _num(p.get("avg_price")),
                "total_volume": volume,
                "available_volume": _num(p.get("can_use_volume")),
                "market_value": market_value,
                # 现价兜底：0 = 拿不到（消费方据此跳过盈亏），上游同公式
                "last_price": (
                    round(market_value / volume, 4)
                    if volume > 0 and market_value > 0
                    else 0
                ),
            }
        )
    return {
        "asset": {
            "currency": "CNY",
            "asset": total,
            "balance": total,
            "cash": _num(asset.get("cash")),
            "market_value": _num(asset.get("market_value")),
            "frozen_cash": _num(asset.get("frozen_cash")),
        },
        "positions": rows,
        "position_count": len(rows),
        "broker": "qmt",
        "account_id": str(asset.get("account_id") or ""),
    }


def qmt_status_data(cfg: dict[str, Any], pong: dict[str, Any]) -> dict[str, Any]:
    """上游 ``/api/qmt/status`` 的 ``data`` 形状（api_server.py L761–772 逐字段）。

    ``allow_trading`` = 本侧总闸（上游 ``allow_trading`` ↔ QM 执行端
    ``enabled``）；``allow_order_methods`` 桥报不出来就回 None——把「不知道」
    说成「确认为关」会带偏排查（上游注释原话）。
    """
    result = (pong or {}).get("result") or {}
    raw_allow = result.get("allow_order_methods")
    return {
        "account_id": (pong or {}).get("account_id") or result.get("account_id"),
        "allow_trading": bool(cfg.get("enabled")),
        "allow_order_methods": None if raw_allow is None else bool(raw_allow),
        "version": result.get("version"),
        "account_type": result.get("account_type"),
        "server_time": result.get("server_time"),
        "rpc_revision": result.get("rpc_revision"),
    }


def _client():
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    return get_qmt_exec_client()


#: 桥账户客户端单例：/qmt/account 每 30s 被前端轮询，逐次新建会漏 httpx 连接
_bridge_account_client = None


def _account_client():
    """账户读取客户端：优先走桥（所有外部取数经 8550），桥未配置才回退执行端。

    `/qmt/account` 是前端「QMT 通道卡」的在线探针：它若直连执行端，而页面
    开关 ``broker:config:qmt_exec.enabled=false``，探针会失败 → 通道卡显示
    「离线」，可账户页签（走 PG 快照、由桥同步）却有实时数据。两处必须同源。
    """
    global _bridge_account_client
    from backend.services.live_trading.services.qmt_bridge_client import (
        QmtBridgeClient,
    )

    if _bridge_account_client is None:
        _bridge_account_client = QmtBridgeClient()
    if _bridge_account_client.configured:
        return _bridge_account_client
    return _client()


async def qmt_account() -> dict[str, Any]:
    """QMT 账户资产+持仓（只读）。异常交给路由层降级为 ``success:false``。"""
    client = _account_client()
    await client.refresh_settings()  # api 进程无人常驻刷新，见模块注头
    asset = await client.get_asset()
    positions = await client.get_positions()
    return qmt_account_data(asset, positions)


async def qmt_status() -> dict[str, Any]:
    """QMT 桥自述（只读 ping，不受本侧总闸约束）。异常同上。"""
    client = _client()
    await client.refresh_settings()
    cfg = client.effective_config()
    pong = await client.bridge_status()
    return qmt_status_data(cfg, pong)
