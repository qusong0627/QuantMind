"""
TdxPushService - QuantMind ↔ 通达信 双向推送服务

推送 (Q→T): 选股信号写板块 / 预警信号 / 文本消息 / 策略源码 / 交易下单
拉取 (T→Q): 账户资产 / 持仓明细 / 当日委托 / 单笔委托状态 / 盈亏计算

所有请求经 Windows 桥 (TDX_BRIDGE_URL:8550) 转发到通达信客户端。
受 ENABLE_TDX_PUSH 开关控制, 未配置 token 时安全降级不报错。
"""

import asyncio
import logging
import math
import os
import uuid
from typing import Any, NamedTuple, Optional

from backend.services.live_trading.services.tdx_cancel_verify import (
    verify_cancel_terminal,
)
from backend.services.live_trading.services.trading_session import (
    TZ,
    real_order_session_refusal,
)
from backend.shared.agent_ledger_fill import (
    derive_cumulative_fill_key,
    post_fill_for_order,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.order_contract import build_bridge_plan_id
from backend.shared.trade_contract import CUMULATIVE_TRADE_PREFIX

logger = logging.getLogger(__name__)

TDX_BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://127.0.0.1:8550")
TDX_BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")
TIMEOUT = 10.0

# tqcenter 市价单的 WtPrice 哨兵值（2026-09-18 实测：市价卖单 319893 返回 1.0）——
# 落库时须归 MARKET，否则交易记录显示"限价 1.00 元"
MARKET_ORDER_PRICE_SENTINEL = 1.0


def estimate_order_fee(filled_value: float, side: str = "buy") -> float:
    """估算 A股单笔委托总费用 = 佣金 + 印花税(仅卖出) + 过户费。

    **费率与算法都不在这里**（T-P2-02 单实现）：本模块曾自带一份
    ``COMMISSION_RATE``/``COMMISSION_MIN``/``STAMP_TAX_RATE``/``TRANSFER_FEE_RATE``
    ——四个费率三个与 ``CN_RULES`` 同值、佣金一项不同，是分叉的孤儿副本。
    桥回执确实不含费用字段（``tools/bridge-windows`` 全仓无 fee/commission 字段），
    所以这里仍要估，但估的**依据**必须走 ``CN_RULES``。

    口径用 ``compute_real_order_fee`` 而非 ``compute_commission``：真单记的是
    **券商实收的估计**（万2.5），撮合默认（万3）是计划口径的保守值。两者混用会让
    真单成本单向高估，而 UI 把真单与模拟盘放在同一张表里比。
    """
    if filled_value <= 0:
        return 0.0
    from backend.services.simulation.services.market_rules import CN_RULES

    return CN_RULES.compute_real_order_fee(float(filled_value), 1.0, side)


def _fill_transitioned(
    prior_status: str, order_status: str, filled_volume: float
) -> bool:
    """是否「首次」转为全量成交（决定推不推 QQ 成交回执）。

    桥是成交权威源，30s 轮询会反复刷新同一行——只有首次（prior 非 filled → filled）
    才算新成交事件；prior 缺失 = 首次见到该委托（补插），已成即算。
    部分成交不推（一次委托推两条是噪音，成交回执只报落定的事）。
    """
    if str(order_status or "").lower() != "filled":
        return False
    if float(filled_volume or 0) <= 0:
        return False
    return str(prior_status or "").lower() != "filled"


def _sell_roundtrip_fields(outcome: Any) -> dict[str, Any]:
    """``ApplyOutcome`` → 回执里带得走的成本口径（无成本/买入 → 空 dict）。

    只搬**账本已经算好**的三个值，不在这里重算：盈亏与持有天数的口径只有
    ``record_sell``/``_roundtrip_row`` 一处（成交回执不该长出第二份算术）。
    """
    try:
        cost = float(getattr(outcome, "cost_price", None) or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        return {}
    fields: dict[str, Any] = {"cost_price": cost}
    days = getattr(outcome, "holding_days", None)
    if days is not None:
        fields["holding_days"] = float(days)
    closed = getattr(outcome, "closed", None)
    if closed is not None:
        fields["closed"] = bool(closed)
    return fields


def _sell_pnl_segment(fill: dict, volume: int, price: float) -> str:
    """卖出回执的**已实现盈亏 + 持有天数**一段（2026-10-20 用户要求）。

    数据来自分账回合行（``agent_ledger_store._roundtrip_of`` 随成交同事务落库，
    经 ``ApplyOutcome`` 带回）：成本取持仓成本价，持有天数取「买入时刻 → 成交时刻」，
    ``closed`` 区分清仓与减仓。**没有成本（人工单/无 agent 归属）就返回空串**——
    宁可少一段，也不按 0 成本编一个假 P&L（同「宁缺数字不端 0 元假账」纪律）。
    """
    try:
        cost = float(fill.get("cost_price") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0 or volume <= 0 or price <= 0:
        return ""
    pnl = (price - cost) * volume
    pnl_pct = (price / cost - 1.0) * 100.0
    sign = "+" if pnl >= 0 else "-"
    seg = f"盈亏 {sign}¥{abs(pnl):,.0f}（{pnl_pct:+.2f}%）"
    try:
        days = float(fill.get("holding_days"))
    except (TypeError, ValueError):
        days = None
    if days is not None and math.isfinite(days):
        seg += f" · 持有 {days:g} 天"
    if fill.get("closed") is True:
        seg += " · 清仓"
    elif fill.get("closed") is False:
        seg += " · 减仓"
    return seg


def _notify_fill_qq(fill: dict) -> None:
    """真单成交 → QQ 回执（执行面用词保留「买入/卖出」；失败只降级绝不反噬）。

    触发点：``_sync_orders_to_pg`` 里订单首次转 FILLED 的那次同步。QM 原生栈的
    成交确认走 notifications ``level=success``，而 ``qq_notify.alert_async`` 只
    放行 warning/error——**成交回执从来到不了 QQ**（2026-10-09 用户实感「交易
    信息都没有」）；本函数把这层补回真单落库权威点。

    ``qq_notify.notify_async`` 内部走守护线程，永不阻塞同步循环；这里额外兜一层
    异常：通知面坏了不许带走成交落库。
    """
    try:
        from backend.shared.qq_notify import notify_async
        from backend.shared.stock_utils import StockCodeUtil

        side = str(fill.get("side") or "buy").strip().lower()
        is_sell = side in ("sell", "s")
        side_cn = "卖出" if is_sell else "买入"
        arrow = "▼" if is_sell else "▲"
        prefix = StockCodeUtil.to_prefix(str(fill.get("symbol") or "").strip())
        name = ""
        try:
            from backend.shared.stock_name_mapper import resolve_name

            name = str(resolve_name(prefix) or "").strip()
        except Exception:  # noqa: BLE001 名称缺失只影响可读性，照推
            name = ""
        label = f"{name}({prefix})" if name else (prefix or "?")
        volume = int(float(fill.get("filled_volume") or 0))
        price = float(fill.get("filled_price") or 0)
        if price > 0:
            amount = volume * price
            title = f"{arrow} {side_cn} {label} {volume}股 @¥{price:.2f}"
            body = f"金额 ¥{amount:,.0f}"
        else:
            # 市价单成交价未回报：宁缺数字不端 0 元假账（MED-3，2026-10-09）
            title = f"{arrow} {side_cn} {label} {volume}股"
            body = "成交价待回报"
        exchange_id = str(fill.get("exchange_order_id") or "").strip()
        if exchange_id:
            body += f" · 委托 {exchange_id}"
        if is_sell:
            pnl_seg = _sell_pnl_segment(fill, volume, price)
            if pnl_seg:
                body += "\n" + pnl_seg
        notify_async(title, body)
    except Exception:  # noqa: BLE001 通知失败不许反噬成交落库
        logger.warning("[TdxPush] 成交 QQ 回执降级跳过", exc_info=True)


class TdxPushError(Exception):
    """通达信推送失败"""


class _LocalOrderState(NamedTuple):
    """预载的本地订单行快照（``_sync_orders_to_pg`` 用）。

    一次查询把三样东西都带回来，而不是各写一个查询：既有状态（判「首次转
    FILLED」，轮询刷新不许重复当新成交推）、成交增量所需的前值（已成交量/均价）、
    分账归属（``agent``——成交回报里没有它，只能从订单行读回）。
    """

    order_id: str
    status: Any
    portfolio_id: Any
    filled_quantity: float
    average_price: float | None
    agent: str


class _LedgerOrderRef(NamedTuple):
    """喂给 ``post_fill_for_order`` 的订单引用（只带它读的几个字段）。

    不复用 ORM ``Order``：本模块的订单读写全走裸 SQL（表结构/列名在这条链上是
    显式契约），把 ORM 实例混进来的话，它会是 session 身份表里一份**跟不上的**
    副本（裸 SQL 改了库，ORM 属性还是旧的），哪天一次 autoflush 就能把旧值写回去。
    ``post_fill_for_order`` 本来也只 ``getattr`` 这几个字段（见其 docstring）。
    """

    order_id: str
    tenant_id: str
    user_id: str
    symbol: str
    side: Any
    agent: str


def _marginal_fill_price(
    *,
    delta_qty: float,
    prior_qty: float,
    prior_avg: float | None,
    cum_qty: float,
    cum_avg: float,
    fallback_price: float,
) -> float:
    """累计口径 → 本台阶的成交价（增量边际价）；取不到给 0（调用方不落该行）。

    桥只给「委托级累计成交量 + 柜面累计均价」（通达信 ``CjVol``/``CjPrice``）。
    直接拿 CjPrice 当每一档的成交价，会把前面几档的钱重复摊进后面的档
    （Σ价×量 ≠ 柜面累计金额）⇒ 成交成本虚高、TCA 读数失真。边际价让
    ``Σ(边际价×增量) == 累计均价×累计量``，与柜面口径**逐分对齐**：

    ``p = (累计均价×累计量 − 前值均价×前值量) / 增量``

    前值缺失或前值均价非正（首次见到该单）→ 前值量必为 0，两种取法本来就相等，
    退化为直接取累计均价。推导出的价非正（桥的均价坏了）→ 退 ``fallback_price``
    （订单限价行），再不行给 0：**宁可不落这一行**，也不落 0 元成交
    （TCA/分账按价算金额，0 元=白记一笔，还会污染成本口径）。
    """
    if delta_qty <= 0:
        return 0.0
    prior_value = float(prior_avg or 0.0) * float(prior_qty or 0.0)
    marginal = (float(cum_avg or 0.0) * float(cum_qty or 0.0) - prior_value) / float(
        delta_qty
    )
    if marginal > 0:
        return marginal
    if float(cum_avg or 0.0) > 0:
        return float(cum_avg)
    if float(fallback_price or 0.0) > 0:
        return float(fallback_price)
    return 0.0


def _batch_quantdb_last_close(symbols: list[str]) -> dict[str, float]:
    """批量从 QuantDB 本地日线读取最近交易日收盘价（与模拟撮合同源）。"""
    if not symbols:
        return {}
    result: dict[str, float] = {}
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )
        from backend.shared.stock_utils import StockCodeUtil

        # 进程内共享实例：复用交易日枚举与按日行情缓存
        market_data = get_local_market_data()
        latest_date = market_data.latest_trade_date()
        if latest_date is None:
            return result
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if not suffix:
                continue
            bar = market_data.get_bar(suffix, latest_date)
            if bar is not None and bar.close > 0:
                result[symbol] = float(bar.close)
    except Exception as exc:
        logger.warning("[TdxSync] QuantDB 收盘价补全失败: %s", exc)
    return result


def normalize_bridge_positions(
    positions_raw: list[dict[str, Any]] | None,
    price_map: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """桥 ``/api/v1/account/query`` 的持仓 → 落库持仓（**0 股模板行丢弃**）。

    桥在清仓后仍会把该代码留成 ``total_volume=0`` 的模板行（2026-10-20 实测：
    9 只全清后仍回 9 条 0 股行）。0 股不是持仓——计入 ``position_count`` /
    写进 ``payload_json`` 会把「持仓 9 只 / 市值 ¥0.00」带进日终账本，收盘
    收益报表原样展示。价格缺失时用 QuantDB 最近收盘价补（与模拟撮合同源）。
    """
    positions: list[dict[str, Any]] = []
    for p in positions_raw or []:
        symbol = str(p.get("stock_code") or "").strip()
        if not symbol:
            continue
        volume = int(p.get("total_volume") or 0)
        if volume <= 0:
            continue
        price = float(p.get("current_price") or p.get("price") or 0)
        if price <= 0:
            price = float((price_map or {}).get(symbol) or 0.0)
        positions.append(
            {
                "symbol": symbol,
                "name": str(p.get("stock_name") or "").strip(),
                "volume": volume,
                "available_volume": int(p.get("available_volume") or 0),
                "cost_price": float(p.get("cost_price") or 0),
                "price": price,
                "market_value": round(volume * price, 2),
            }
        )
    return positions


class TdxPushService:
    def __init__(self, bridge_url: str = "", bridge_token: str = ""):
        self.bridge_url = str(bridge_url or TDX_BRIDGE_URL).rstrip("/")
        self.bridge_token = str(bridge_token or TDX_BRIDGE_TOKEN).strip()
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.bridge_url) and bool(self.bridge_token)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.bridge_token}",
        }

    async def _client_http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.enabled:
            logger.warning("[TdxPush] TDX_BRIDGE_URL/TOKEN 未配置, 跳过 %s", path)
            raise TdxPushError("TDX_BRIDGE_URL/TOKEN 未配置")
        client = await self._client_http()
        resp = await client.post(
            f"{self.bridge_url}{path}", json=payload, headers=self._headers()
        )
        if resp.status_code != 200:
            raise TdxPushError(f"桥返回 HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    # ============ 推送 (Q→T) ============

    async def push_signals_to_block(
        self,
        stocks: list,
        block_code: str = "",
        block_name: str = "QuantMind今日选股",
        show: bool = True,
    ) -> dict:
        """把选股结果写入通达信自定义板块."""
        return await self._post(
            "/api/v1/push/block",
            {"block_code": block_code, "stocks": stocks, "show": show},
        )

    async def push_warnings(self, signals: list[dict]) -> dict:
        """推送买卖预警信号 (通达信支持双击闪电下单).

        signals 元素: {symbol, side(buy/sell), price, close, volume, reason}
        """
        stock_list = [s.get("symbol", "") for s in signals]
        bs_map = {"buy": "0", "sell": "1"}
        return await self._post(
            "/api/v1/push/warnings",
            {
                "stock_list": stock_list,
                "price_list": [str(s.get("price", "0")) for s in signals],
                "close_list": [str(s.get("close", "0")) for s in signals],
                "volum_list": [str(s.get("volume", "0")) for s in signals],
                "bs_flag_list": [
                    bs_map.get(str(s.get("side", "")).lower(), "2") for s in signals
                ],
                "warn_type_list": ["1"] * len(signals),
                "reason_list": [s.get("reason", "")[:25] for s in signals],
                "count": len(signals),
            },
        )

    async def push_message(self, msg: str) -> dict:
        """推送文本消息到通达信界面."""
        return await self._post("/api/v1/push/message", {"msg": msg})

    async def push_source(self, py_code: str, handle_type: int = 0) -> dict:
        """推送策略源码到通达信云回测平台."""
        return await self._post(
            "/api/v1/push/source", {"py_code": py_code, "handle_type": handle_type}
        )

    async def place_order(
        self,
        stock_code: str,
        side: str,
        volume: int,
        price: float | None = None,
        price_type: int | None = None,
        plan_id: str = "",
    ) -> dict:
        """通过桥下单到通达信.

        **真单时段闸门（咽喉点）**：A 股委托只可能在交易时段有效
        （09:15–11:35 / 12:55–15:05，与 QMT 执行端、真单镜像、时段判据同一口径，
        见 ``trading_session``）。其余时间提交的委托要么被柜台拒，要么被客户端
        **挂成次日单**——后者尤其坏：一笔"现在"的决定变成了明天的无主委托。

        闸门放在这里而不是各调用点：本函数是滚动单/L2 主单/L2 在途重挂三条路的
        汇合点，而 L2 实时循环是 24/7 常驻的——调用点漂移一次就是一次真钱事故。
        撤单不走此闸门：撤单是减小风险的动作，任何时间都该放行（见 ``cancel_order``）。

        ⚠️ 「真单**唯一**的物理出口」是 2026-09-24 之前的说法，**不成立**：
        ``broker_client`` 的五个真券商通道（TdxBroker/RedisBroker/QMTBroker/
        QMTBridgeBroker/QmtExecBroker）走的是同一个 Windows 桥，此前全无闸门。
        现在两边共用 ``trading_session.real_order_session_refusal`` 的同一句拒因
        （见 ``test_broker_session_gate.py``）。
        """
        refusal = real_order_session_refusal()
        if refusal:
            logger.warning(
                "[TdxPush] 非交易时段拒发真单: %s %s %s股", stock_code, side, volume
            )
            # 形状与桥的拒单一致（status=error）：调用方按"失败"处理并留痕，
            # 绝不能被当成"已下单"——滚动/L2 都按 status 分拣 placed/failed。
            return {
                "status": "error",
                "skipped": "out_of_session",
                "message": refusal,
                "orders": [],
            }
        order = {
            "stock_code": stock_code,
            "side": side,
            "volume": volume,
            "order_type": "limit" if price else "market",
            "price_type": price_type if price_type is not None else (0 if price else 1),
            "price": price,
        }
        return await self._post(
            "/api/v1/plans/execute",
            {
                # 缺省号同样必须纳秒（秒精度会让同秒两笔单撞号 → 桥去重整单丢弃）
                "plan_id": build_bridge_plan_id(plan_id),
                "orders": [order],
            },
        )

    # ============ 拉取 (T→Q) ============

    async def pull_account(self) -> dict:
        """拉取通达信账户资产."""
        data = await self._post("/api/v1/account/query", {})
        return data.get("asset", {})

    async def pull_positions(self) -> list:
        """拉取通达信持仓明细."""
        data = await self._post("/api/v1/account/query", {})
        return data.get("positions", [])

    async def pull_orders(self, stock_code: str = "") -> list:
        """拉取通达信当日委托."""
        data = await self._post("/api/v1/orders/query", {"stock_code": stock_code})
        return data.get("orders", [])

    async def cancel_order(self, stock_code: str, order_id: str) -> dict:
        """撤单（桥 /api/v1/orders/cancel → 通达信 cancel_order_stock）。

        返回 {"success": bool, "message": str}；撤单成功不代表原委托一定未成交,
        调用方应随后 query 一次当日委托确认最终状态。

        桥握手指失败/异常时按当日委托终态复核（2026-10-12 实测桥撤单只认
        Value=="1"，直连账号成功码为 "2"，实际已撤却报 success=false）——
        复核确认已撤则纠正为 success=True + verified=True。
        """
        payload = {"stock_code": stock_code, "order_id": order_id}
        try:
            resp = await self._post("/api/v1/orders/cancel", payload)
        except Exception:
            # 桥不可达/HTTP 错 = 撤单结果未知；复核确认已撤则如实上报，否则原样重抛
            state, _ = await verify_cancel_terminal(
                lambda: self.pull_order_status(order_id)
            )
            if state == "cancelled":
                logger.warning(
                    "[TdxPush] 撤单握手异常但终态复核确认已撤: %s %s",
                    stock_code,
                    order_id,
                )
                return {
                    "success": True,
                    "verified": True,
                    "message": "撤单已生效（桥握手异常，经委托终态复核确认）",
                }
            raise
        if resp.get("success"):
            return resp
        state, _order = await verify_cancel_terminal(
            lambda: self.pull_order_status(order_id)
        )
        if state == "cancelled":
            logger.info("[TdxPush] 撤单假失败纠正: %s %s", stock_code, order_id)
            return {
                "success": True,
                "verified": True,
                "message": "撤单已生效（桥握手报失败系误报，经委托终态复核确认）",
                "bridge_message": resp.get("message", ""),
            }
        if state == "filled":
            return {
                "success": False,
                "verified": True,
                "message": "撤销前已成交，无法撤单",
                "bridge_message": resp.get("message", ""),
            }
        if state == "dead":
            return {
                "success": False,
                "verified": True,
                "message": "该委托已终结（废单/已拒绝），无需撤销",
                "bridge_message": resp.get("message", ""),
            }
        return resp

    async def tdx_call(
        self, method: str, params: dict | None = None, source: str | None = None
    ) -> dict:
        """通用 JSON-RPC 透传（桥 /api/v1/tdx/call，白名单方法）。

        用于实时行情（get_market_snapshot 等）拉取。返回桥 result 字典。
        ``source="qmt"`` 走桥的同机 QMT 伴生渠道（只读；``get_full_tick`` 五档），
        不传则仍走通达信客户端。
        """
        payload: dict = {"method": method, "params": params or {}}
        if source:
            payload["source"] = source
        resp = await self._post("/api/v1/tdx/call", payload)
        result = resp.get("result") if isinstance(resp, dict) else resp
        return result if isinstance(result, dict) else {"result": result}

    async def pull_order_status(self, wtbh: str) -> dict:
        """按委托编号查单笔委托状态."""
        orders = await self.pull_orders()
        for o in orders:
            if str(o.get("order_id", "")) == str(wtbh):
                return o
        return {}

    async def pull_pnl(self) -> dict:
        """拉取账户并计算盈亏.

        返回: {total_asset, cash, market_value, balance, positions, pnl_by_pos, total_pnl}
        """
        data = await self._post("/api/v1/account/query", {})
        asset = data.get("asset", {})
        positions = data.get("positions", [])
        pnl_list = []
        total_pnl = 0.0
        for p in positions:
            cost = float(p.get("cost_price", 0) or 0)
            # 成本价 x 总持仓 与 持仓市值 的差
            market_value = float(p.get("market_value", 0) or 0)
            pnl = market_value - cost * float(p.get("total_volume", 0) or 0)
            pnl_list.append(
                {
                    "stock_code": p.get("stock_code", ""),
                    "cost_price": cost,
                    "volume": p.get("total_volume", 0),
                    "market_value": market_value,
                    "pnl": round(pnl, 2),
                }
            )
            total_pnl += pnl
        return {
            "total_asset": asset.get("asset", 0),
            "cash": asset.get("cash", 0),
            "market_value": asset.get("market_value", 0),
            "balance": asset.get("balance", 0),
            "positions": pnl_list,
            "total_pnl": round(total_pnl, 2),
        }

    async def sync_account_to_pg(
        self,
        *,
        tenant_id: str = "default",
        user_id: str = "",
    ) -> dict[str, Any]:
        """拉取通达信账户/持仓并落库到 real_account_snapshots，供前端 /account 读取。

        返回: {success, account_id, total_asset, cash, market_value, position_count}
        """
        if not self.enabled:
            return {"success": False, "error": "TDX_BRIDGE_URL/TOKEN 未配置"}
        data = await self._post("/api/v1/account/query", {})
        asset = data.get("asset", {}) or {}
        positions_raw = data.get("positions", []) or []
        # 桥返回的持仓缺少现价/市值，用 QuantDB 最近收盘价补全（与模拟撮合同源）
        price_map = await asyncio.to_thread(
            _batch_quantdb_last_close,
            [str(p.get("stock_code") or "").strip() for p in positions_raw],
        )
        # 0 股模板行不是持仓（桥清仓后仍返回）——丢弃，见 normalize_bridge_positions
        positions = normalize_bridge_positions(positions_raw, price_map)
        total_asset = float(asset.get("asset") or 0)
        cash = float(asset.get("cash") or 0)
        # 桥的 market_value 可能为 0，按持仓市值累加
        market_value = float(asset.get("market_value") or 0)
        if market_value <= 0 and positions:
            market_value = round(
                sum(float(x.get("market_value") or 0) for x in positions), 2
            )

        # 零资产守卫：桥返回空账户（开盘前后/桥未就绪）时不落库，
        # 避免 0 资产快照污染日终账本与收益计算（与 qmt 链路 is_inconsistent_zero_total_snapshot 同语义）。
        if total_asset <= 0 and cash <= 0 and market_value <= 0:
            logger.info(
                "[TdxPush] 桥返回空账户 asset=%.2f cash=%.2f mv=%.2f，跳过落库",
                total_asset,
                cash,
                market_value,
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "empty_account",
                "total_asset": total_asset,
                "cash": cash,
                "market_value": market_value,
                "position_count": len(positions),
            }

        from datetime import date, datetime, timezone

        from sqlalchemy import insert

        from backend.services.trade_shared.models.real_account_snapshot import (
            RealAccountSnapshot,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        account_id = f"tdx-{tenant_id}-{user_id}"
        snapshot_date = now.date()
        snapshot_month = snapshot_date.strftime("%Y-%m")
        payload = {
            "positions": positions,
            "source": "tdx_bridge",
            "broker_type": "tdx",
        }
        async with get_session() as db:
            await db.execute(
                insert(RealAccountSnapshot).values(
                    tenant_id=tenant_id,
                    user_id=user_id or "0",
                    account_id=account_id,
                    snapshot_at=now,
                    snapshot_date=snapshot_date,
                    snapshot_month=snapshot_month,
                    total_asset=total_asset,
                    cash=cash,
                    market_value=market_value,
                    today_pnl_raw=0.0,
                    total_pnl_raw=0.0,
                    floating_pnl_raw=0.0,
                    source="tdx_bridge",
                    payload_json=payload,
                )
            )
            # 本笔委托里「首次转 FILLED」的成交事件；commit 成功后才推 QQ
            # （事务没提交就推 = 可能推一笔回滚掉的成交；见 _notify_fill_qq 口径）
            fill_events = await self._sync_orders_to_pg(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                now=now,
            )
            # —— 日终账本同步（与 qmt 链路共用 upsert_real_account_daily_ledger）——
            # tdx 桥不提供当日盈亏，用派生口径：今日盈亏 = 总资产 - 上一交易日收盘权益；
            # 首日无上一交易日则退化到今日首条快照。口径与 internal_strategy_utils 一致。
            from sqlalchemy import select

            from backend.services.trade.services.real_account_ledger_service import (
                normalize_equity,
                upsert_real_account_daily_ledger,
            )

            # 锚值（day_open/month_open/initial_equity）取自快照表的**原始** total 列，
            # 必须先过一致性归一：被读错的总资产当锚会把假跌/假涨带进派生列
            # （2026-09-07 实况：day_open 拷贝 09-04 的坏值 856,948.02 → 假 +7.32%）。
            async def _first_asset(*where) -> float | None:
                stmt = (
                    select(
                        RealAccountSnapshot.total_asset,
                        RealAccountSnapshot.cash,
                        RealAccountSnapshot.market_value,
                    )
                    .where(
                        RealAccountSnapshot.tenant_id == tenant_id,
                        RealAccountSnapshot.user_id == (user_id or "0"),
                        RealAccountSnapshot.account_id == account_id,
                        *where,
                    )
                    .order_by(
                        RealAccountSnapshot.snapshot_at.asc(),
                        RealAccountSnapshot.id.asc(),
                    )
                    .limit(1)
                )
                row = (await db.execute(stmt)).first()
                if not row or not row[0]:
                    return None
                equity, _ = normalize_equity(row[0], row[1], row[2])
                return float(equity) if equity else None

            prev_day_stmt = (
                select(
                    RealAccountSnapshot.total_asset,
                    RealAccountSnapshot.cash,
                    RealAccountSnapshot.market_value,
                )
                .where(
                    RealAccountSnapshot.tenant_id == tenant_id,
                    RealAccountSnapshot.user_id == (user_id or "0"),
                    RealAccountSnapshot.account_id == account_id,
                    RealAccountSnapshot.snapshot_date < snapshot_date,
                )
                .order_by(
                    RealAccountSnapshot.snapshot_at.desc(),
                    RealAccountSnapshot.id.desc(),
                )
                .limit(1)
            )
            prev_row = (await db.execute(prev_day_stmt)).first()
            prev_close_equity = None
            if prev_row and prev_row[0]:
                prev_close_equity, _ = normalize_equity(
                    prev_row[0], prev_row[1], prev_row[2]
                )
                prev_close_equity = prev_close_equity or None
            day_open_equity = prev_close_equity or await _first_asset(
                RealAccountSnapshot.snapshot_date == snapshot_date
            )
            month_open_equity = await _first_asset(
                RealAccountSnapshot.snapshot_month == snapshot_month
            )
            initial_equity = await _first_asset() or total_asset
            today_pnl = total_asset - (day_open_equity or total_asset)
            total_pnl = total_asset - (initial_equity or total_asset)

            await upsert_real_account_daily_ledger(
                db,
                tenant_id=tenant_id,
                user_id=user_id or "0",
                account_id=account_id,
                snapshot_at=now,
                snapshot_date=snapshot_date,
                total_asset=total_asset,
                cash=cash,
                market_value=market_value,
                initial_equity=initial_equity or total_asset,
                day_open_equity=day_open_equity or total_asset,
                month_open_equity=month_open_equity or total_asset,
                today_pnl=today_pnl,
                total_pnl=total_pnl,
                floating_pnl=0.0,
                position_count=len(positions),
                source="tdx_bridge",
                payload_json=payload,
            )
            await db.commit()
        # 落库已提交才推成交回执（真单成交 → QQ 的唯一出口；模拟层不重复推）
        for fill in fill_events:
            _notify_fill_qq(fill)
        logger.info(
            "[TdxPush] 通达信账户已落库 PG: asset=%.2f cash=%.2f positions=%d",
            total_asset,
            cash,
            len(positions),
        )
        return {
            "success": True,
            "account_id": account_id,
            "total_asset": total_asset,
            "cash": cash,
            "market_value": market_value,
            "position_count": len(positions),
        }

    async def _sync_orders_to_pg(
        self,
        *,
        db,
        tenant_id: str,
        user_id: str,
        now,
    ) -> list[dict]:
        """把通达信当日委托同步到 orders 表（REAL 模式交易记录展示）+ 成交/分账。

        幂等 + 增量修正：按 exchange_order_id（桥的委托编号）去重；
        已存在的行用桥的最新状态/成交回报刷新。桥是真实成交的权威来源，
        若只插不更新，订单会永远停在 SUBMITTED，随后被超时扫描器误判为
        EXPIRED（表现为"交易记录全部已过期、成交为 0"）。

        **增量成交（乙2/乙3，2026-10-14 补）**：桥只回报委托级**累计**成交量与
        柜面均价、没有逐笔成交号——累计量的每一档差额补一行 ``trades``（成交 tab /
        TCA / 对账读它，此前这条路一笔都没写过，09-29 之后成交面全空），并按
        ``agent_ledger_fill`` 的三条纪律把同一笔记进 ``orders.agent`` 的分账段。
        接在这里而不是另起一个任务：订单行与成交行必须同事务（「成交在、订单状态
        没跟上」会让对账/持仓两套数各说各话）。

        返回本轮**首次转 FILLED** 的成交事件（供调用方在 commit 后推 QQ；
        事件不在这里推送——事务没提交就推送=可能推一笔没落库的成交）。

        **失败形态（乙4）**：桥那一侧（``pull_orders``）失败只降级——本轮没数据，
        账户快照照落库，桥健康另有 BridgeHealthTracker 在账户通道上判；**DB/账本**
        失败一律往外抛，调用方的事务一起回滚、下一轮 30s 轮询重投（见
        ``agent_ledger_fill`` 纪律 3：宁可晚记，不可「成交在、账本无」）。
        原先的裸 ``except → logger.warning → return []`` 把「成交回报落不进去」
        伪装成「桥今天没成交」——两者在库里长得一模一样，没人能分辨。
        """
        from datetime import datetime

        from sqlalchemy import func, select, text

        from backend.services.trade_shared.models.enums import (
            OrderSide,
            OrderStatus,
            OrderType,
            PositionSide,
            TradeAction,
            TradingMode,
        )
        from backend.services.trade_shared.models.order import Order
        from backend.services.trade_shared.models.trade import Trade
        from backend.services.trade_shared.order_fees import fee_columns

        try:
            orders = await self.pull_orders()
        except Exception as exc:  # noqa: BLE001 桥侧失败=本轮没数据（见 docstring）
            logger.warning("[TdxSync] 通达信当日委托拉取失败: %s", exc)
            return []
        if not orders:
            return []

        fills: list[dict] = []
        # 预载本地订单行：既有状态（判「首次转 FILLED」，轮询刷新不许重复当新成交推）
        # + 成交增量所需的前值（已成交量/均价）+ 分账归属（agent，成交回报里没有）。
        existing: dict[str, _LocalOrderState] = {}
        for row in (
            await db.execute(
                select(
                    Order.exchange_order_id,
                    Order.order_id,
                    Order.status,
                    Order.portfolio_id,
                    Order.filled_quantity,
                    Order.average_price,
                    Order.agent,
                ).where(
                    Order.exchange_order_id.is_not(None),
                    Order.tenant_id == tenant_id,
                    Order.user_id == str(user_id),
                )
            )
        ).fetchall():
            existing[str(row[0])] = _LocalOrderState(
                order_id=str(row[1]),
                status=row[2],
                portfolio_id=row[3],
                filled_quantity=float(row[4] or 0.0),
                average_price=float(row[5]) if row[5] else None,
                agent=str(row[6] or ""),
            )
        # 本轮已落的成交键：同一委托被桥重复返回时只落一次（跨轮幂等靠键本身）
        written_fills: set[str] = set()
        # 乙4：静默跳过必须有出口——「成交在、成交面没有」不许只留在库里没人知道
        skipped_no_price = 0  # 台阶增量取不出价（柜面均价坏且无限价可退）
        skipped_no_key = 0  # 台阶增量派生不出幂等键（如累计量非整数）——无键不落行

        def _map_bridge_status(status: str) -> OrderStatus:
            if status in ("filled",):
                return OrderStatus.FILLED
            if status in ("partial", "partial_fill", "partially_filled"):
                return OrderStatus.PARTIALLY_FILLED
            if status in ("cancel", "cancelled", "partial_cancelled"):
                return OrderStatus.CANCELLED
            if status in ("rejected",):
                return OrderStatus.REJECTED
            return OrderStatus.SUBMITTED

        for o in orders:
            exchange_id = str(o.get("order_id") or "").strip()
            if not exchange_id:
                continue
            symbol = str(o.get("stock_code") or "").strip()
            if not symbol:
                continue
            side = str(o.get("side") or "buy").strip().lower()
            status = str(o.get("status") or "pending").strip().lower()
            # 桥已修复方向解析: buy/sell/cancel(方向未知, 保守默认 buy)
            order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
            order_status = _map_bridge_status(status)

            time_hhmm = str(o.get("time") or "").strip()
            submitted_at = now
            # 桥报 HHMM 的北京墙钟瞬时（解析失败=None）：orders.submitted_at、
            # trades.executed_at 与分账 filled_at 全用它——成交瞬时只取一次
            # （成交日是账本幂等键的一半，两处各取一次 now() 会在跨零点打架）。
            wall_time = None
            if len(time_hhmm) >= 4:
                try:
                    wall_time = now.replace(
                        hour=int(time_hhmm[:2]),
                        minute=int(time_hhmm[2:4]),
                        second=0,
                        microsecond=0,
                    )
                    submitted_at = wall_time
                except (ValueError, TypeError):
                    pass
            raw_price = float(o.get("order_price") or 0)
            is_market = raw_price <= 0 or raw_price == MARKET_ORDER_PRICE_SENTINEL
            order_type = OrderType.MARKET if is_market else OrderType.LIMIT
            total_volume = float(o.get("total_volume") or 0)
            filled_volume = float(o.get("filled_volume") or 0)
            price = 0.0 if is_market else raw_price
            filled_price = float(o.get("filled_price") or price)
            filled_value = round(filled_volume * filled_price, 2)
            fee = estimate_order_fee(filled_value, side)
            filled_at = submitted_at if order_status == OrderStatus.FILLED else None

            # 本轮分账回合行带来的卖出成本口径（成交回执要报盈亏/持有天数/清仓）；
            # 无 agent 归属或买入单保持空，回执就不渲染这一段。
            pnl_fields: dict[str, Any] = {}
            prior = existing.get(exchange_id)
            prior_status = (
                getattr(prior.status, "value", prior.status) if prior else None
            )
            became_filled = _fill_transitioned(
                str(prior_status or ""), order_status.value, filled_volume
            )

            if prior:
                order_pk = prior.order_id
                # 已存在 → 用桥最新状态刷新（成交回报追平，避免被超时扫描器误标过期）
                await db.execute(
                    text(
                        """
                        UPDATE orders SET
                            status = :status,
                            filled_quantity = :filled_quantity,
                            average_price = :average_price,
                            filled_value = :filled_value,
                            filled_at = :filled_at,
                            commission = :commission,
                            remarks = '通达信桥委托'
                        WHERE order_id = :order_id
                        """
                    ),
                    {
                        "status": order_status.value,
                        "filled_quantity": filled_volume,
                        "average_price": filled_price if filled_volume > 0 else None,
                        "filled_value": filled_value,
                        "filled_at": filled_at,
                        "commission": fee,
                        "order_id": prior.order_id,
                    },
                )
            else:
                result = await db.execute(
                    text(
                        """
                        INSERT INTO orders (
                            order_id, tenant_id, user_id, portfolio_id, strategy_id, symbol,
                            side, trade_action, position_side, is_margin_trade,
                            order_type, trading_mode, status,
                            quantity, filled_quantity, price, average_price,
                            order_value, filled_value, commission,
                            submitted_at, filled_at,
                            client_order_id, exchange_order_id, remarks
                        ) VALUES (
                            :order_id, :tenant_id, :user_id, :portfolio_id, NULL, :symbol,
                            :side, :trade_action, :position_side, FALSE,
                            :order_type, :trading_mode, :status,
                            :quantity, :filled_quantity, :price, :average_price,
                            :order_value, :filled_value, :commission,
                            :submitted_at, :filled_at,
                            :client_order_id, :exchange_order_id, :remarks
                        )
                        RETURNING order_id
                        """
                    ),
                    {
                        "order_id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "user_id": user_id or "0",
                        "portfolio_id": 0,
                        "symbol": symbol,
                        "side": order_side.value,
                        # PG tradeaction enum: OPEN/CLOSE（与 Python 命名不同）
                        "trade_action": "OPEN"
                        if order_side == OrderSide.BUY
                        else "CLOSE",
                        # PG positionside enum: LONG/SHORT（大写）
                        "position_side": "LONG",
                        "order_type": order_type.value,
                        "trading_mode": TradingMode.REAL.value,
                        "status": order_status.value,
                        "quantity": total_volume,
                        "filled_quantity": filled_volume,
                        "price": price if price > 0 else None,
                        "average_price": filled_price if filled_volume > 0 else None,
                        "order_value": round(total_volume * price, 2),
                        "filled_value": filled_value,
                        "commission": fee,
                        "submitted_at": submitted_at,
                        "filled_at": filled_at,
                        "client_order_id": f"tdx-{exchange_id}",
                        "exchange_order_id": exchange_id,
                        "remarks": "通达信桥委托",
                    },
                )
                new_id = result.scalar()
                order_pk = str(new_id) if new_id else None
                if order_pk:
                    existing[exchange_id] = _LocalOrderState(
                        order_id=order_pk,
                        status=order_status,
                        portfolio_id=0,
                        filled_quantity=filled_volume,
                        average_price=(filled_price if filled_volume > 0 else None),
                        agent="",
                    )

            # 乙2/乙3：累计量台阶的增量 → 一行 trades + 分账账本。
            # 基线**从 trades 读**（本单全部 cum- 行的量），不是 orders.filled_quantity：
            # 后者只是「最后一次刷新的累计值」，对 09-29 后没写过成交行的历史单
            # （含今日 600338）会永远算出 0 增量，成交面永远补不上。
            if order_pk and filled_volume > 0:
                # 成交瞬时只取一次：trades.executed_at 与分账 filled_at 必须同一个值
                # （成交日是账本幂等键的一半，两处各取一次 now() 会在跨零点打架）。
                executed_at = wall_time or datetime.now()
                baseline_qty = float(
                    (
                        await db.execute(
                            select(func.coalesce(func.sum(Trade.quantity), 0.0)).where(
                                Trade.order_id == order_pk,
                                Trade.exchange_trade_id.startswith(
                                    CUMULATIVE_TRADE_PREFIX
                                ),
                            )
                        )
                    ).scalar()
                    or 0.0
                )
                delta_qty = filled_volume - baseline_qty
                fill_key = (
                    derive_cumulative_fill_key(
                        broker_order_id=exchange_id,
                        cumulative_volume=filled_volume,
                        trade_date=executed_at,
                    )
                    if delta_qty > 0
                    else ""
                )
                if delta_qty > 0 and not fill_key:
                    # 没有唯一号就没有幂等：宁可晚记/不记，不可重投双记
                    skipped_no_key += 1
                elif fill_key:
                    marginal = _marginal_fill_price(
                        delta_qty=delta_qty,
                        prior_qty=baseline_qty,
                        prior_avg=prior.average_price if prior else None,
                        cum_qty=filled_volume,
                        cum_avg=filled_price,
                        fallback_price=price,
                    )
                    if marginal <= 0:
                        # 0 元成交比缺一行更坏（TCA/分账按价算金额，白记一笔还污染成本）
                        skipped_no_price += 1
                    else:
                        duplicate = fill_key in written_fills
                        if not duplicate:
                            dup_rows = await db.execute(
                                select(Trade.trade_id).where(
                                    Trade.tenant_id == tenant_id,
                                    Trade.user_id == str(user_id),
                                    Trade.exchange_trade_id == fill_key,
                                )
                            )
                            duplicate = dup_rows.first() is not None
                        if not duplicate:
                            # commit 不在本方法（sync_account_to_pg）：拿不到
                            # IntegrityError-at-commit 回滚 ⇒ 先查后插 + 本轮集合，
                            # DB 唯一索引（uq_trades_scope_exchange_trade_id）兜底。
                            written_fills.add(fill_key)
                            db.add(
                                Trade(
                                    order_id=order_pk,
                                    tenant_id=tenant_id,
                                    user_id=str(user_id),
                                    portfolio_id=0,
                                    symbol=symbol,
                                    side=order_side,
                                    trade_action=(
                                        TradeAction.BUY_TO_OPEN
                                        if order_side == OrderSide.BUY
                                        else TradeAction.SELL_TO_CLOSE
                                    ),
                                    position_side=PositionSide.LONG,
                                    trading_mode=TradingMode.REAL,
                                    quantity=delta_qty,
                                    price=marginal,
                                    trade_value=round(delta_qty * marginal, 2),
                                    exchange_trade_id=fill_key,
                                    exchange_name="tdx_bridge",
                                    executed_at=executed_at,
                                    remarks="通达信桥成交",
                                    **fee_columns(
                                        symbol, delta_qty, marginal, order_side
                                    ),
                                )
                            )
                            # 分账（乙2）：无 agent 的单静默跳过（返回 None）；
                            # 失败**不 catch**（纪律 3：与成交行同事务，写不进去
                            # 就整轮重投——宁可晚记，不可「成交在、账本无」）。
                            outcome = await post_fill_for_order(
                                db,
                                order=_LedgerOrderRef(
                                    order_id=str(order_pk),
                                    tenant_id=tenant_id,
                                    user_id=str(user_id),
                                    symbol=symbol,
                                    side=order_side.value,
                                    agent=prior.agent if prior else "",
                                ),
                                fill_key=fill_key,
                                quantity=delta_qty,
                                price=marginal,
                                # 分账列是 aware 瞬时（as_utc：naive 一律当
                                # UTC）。executed_at 是桥报北京墙钟（见上
                                # wall_time 注释），naive 直传=13:01Z→显示
                                # 21:01（2026-10-14 实况）；trades.executed_at
                                # 保持墙钟不动。
                                filled_at=(
                                    executed_at
                                    if executed_at.tzinfo
                                    else executed_at.replace(tzinfo=TZ)
                                ),
                                trade_date=executed_at,
                            )
                            if order_side == OrderSide.SELL and outcome:
                                pnl_fields = _sell_roundtrip_fields(outcome)

            if became_filled:
                fills.append(
                    {
                        "side": side,
                        "symbol": symbol,
                        "filled_volume": filled_volume,
                        "filled_price": filled_price,
                        "exchange_order_id": exchange_id,
                        **pnl_fields,
                    }
                )
        if skipped_no_key or skipped_no_price:
            logger.warning(
                "[TdxSync] 台阶增量未落成交面：无幂等键 %d 笔 / 取不到价 %d 笔"
                "（下一轮轮询会自动重投；此警告每轮复发直到能落或桥不再报该档）",
                skipped_no_key,
                skipped_no_price,
            )
        logger.info("[TdxSync] 通达信委托落库 %d 笔 (user=%s)", len(orders), user_id)
        return fills

    async def check_order_success(self, wtbh: str) -> dict:
        """检查下单是否成功.

        返回: {wtbh, status_code, status_text, filled, all_filled}
          status_code: 0无效/1未成交/2部分成交/3全部成交/4部分撤/5全撤
        """
        order = await self.pull_order_status(wtbh)
        if not order:
            return {
                "wtbh": wtbh,
                "status_code": -1,
                "status_text": "未找到",
                "filled": False,
                "all_filled": False,
            }
        # 桥返回字符串状态（pull_orders 协议），旧实现 int(status) 直接 ValueError
        # （2026-09-18 实测）；按字符串映射为旧数字码保持接口兼容。
        code = {
            "rejected": 0,
            "submitted": 1,
            "partial_fill": 2,
            "partially_filled": 2,
            "filled": 3,
            "partial_cancelled": 4,
            "cancelled": 5,
            "cancel": 5,
        }.get(str(order.get("status") or "").strip().lower(), 1)
        status_map = {
            0: "无效单",
            1: "未成交",
            2: "部分成交",
            3: "全部成交",
            4: "部分成交部分撤单",
            5: "全部撤单",
        }
        filled = code in (2, 3)
        return {
            "wtbh": wtbh,
            "status_code": code,
            "status_text": status_map.get(code, "未知"),
            "filled": filled,
            "all_filled": code == 3,
            "filled_price": order.get("filled_price", 0),
            "filled_volume": order.get("filled_volume", 0),
        }


# 全局单例
tdx_pusher = TdxPushService()
