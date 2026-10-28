"""QMT 账户/持仓只读客户端 —— 经通达信桥（8550）。

架构约定（2026-10-28）：**所有外部取数都走桥**。本客户端只调
``POST {TDX_BRIDGE_URL}/api/v1/account/query {"source": "qmt"}``，
不直连 QMT、不直连伴生桥 17720、不走 Redis RPC。

返回结构与 ``QmtExecClient`` 的 ``get_asset()`` / ``get_positions()`` 对齐，
因此 ``qmt_account_sync_task`` 可以无差别复用（落库 source 仍是 ``qmt_exec``，
前端 QMT 页签无需改动）。

安全守卫：旧版桥（未加载 ``source=qmt`` 路由）会把**通达信账户**当成功返回。
本客户端强制校验 ``account_source/channel_used == "qmt"``，不满足直接报错——
宁可同步失败，也不能把 TDX 数字写进 QMT 账户快照。
"""

from __future__ import annotations

import os
import time
from typing import Any

from backend.services.live_trading.services.qmt_exec_client import QmtExecError

ACCOUNT_PATH = "/api/v1/account/query"
ORDERS_PATH = "/api/v1/orders/query"
HEALTH_PATH = "/api/v1/qmt/health"
DEFAULT_TIMEOUT_S = 12.0
#: get_asset()+get_positions() 共用一次 HTTP：同步任务总是连续调用两者
CACHE_TTL_S = 2.0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_prefix(stock_code: str) -> str:
    """``600519.SH`` → ``SH600519``（与 qmt_exec 的 symbol 口径一致）。"""
    raw = str(stock_code or "").strip().upper()
    if "." in raw:
        code, market = raw.split(".", 1)
        return f"{market}{code}"
    return raw


class QmtBridgeClient:
    """经桥的 QMT 只读客户端（账户同步专用）。"""

    def __init__(
        self,
        bridge_url: str | None = None,
        bridge_token: str | None = None,
        timeout: float | None = None,
        http_client: Any = None,
    ) -> None:
        # None = 读环境变量；显式空串 = 明确不配置（测试/多实例用）
        raw_url = (
            bridge_url if bridge_url is not None else os.getenv("TDX_BRIDGE_URL", "")
        )
        raw_token = (
            bridge_token
            if bridge_token is not None
            else os.getenv("TDX_BRIDGE_TOKEN", "")
        )
        self.bridge_url = str(raw_url).rstrip("/")
        self.bridge_token = str(raw_token).strip()
        self.timeout = float(
            timeout or os.getenv("QMT_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT_S)
        )
        self._client = http_client
        self._cache_ts = 0.0
        self._cache: dict[str, Any] | None = None
        self._account_id = str(os.getenv("QMT_EXEC_ACCOUNT_ID", "") or "")
        #: 桥回落的历史快照（休市）：同步任务据此跳过落库
        self.last_stale = False
        self.last_snapshot_at = ""

    # -- 与 QmtExecClient 同形 ------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.bridge_url) and bool(self.bridge_token)

    @property
    def account_id(self) -> str:
        return self._account_id

    async def refresh_settings(self) -> None:
        """桥地址/token 来自环境变量，运行期不热改（与 tdx_push_service 一致）。"""
        return None

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
            self._client = None

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.bridge_token}",
        }

    async def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise QmtExecError(
                "TDX_BRIDGE_URL/TOKEN 未配置（QMT 账户同步无法走桥）",
                code="NOT_CONFIGURED",
            )
        client = await self._http()
        try:
            resp = await client.post(
                f"{self.bridge_url}{path}", json=payload, headers=self._headers()
            )
        except Exception as exc:  # noqa: BLE001 - 网络错误统一转 QmtExecError
            raise QmtExecError(
                f"桥不可达: {type(exc).__name__}: {exc}", code="BRIDGE_UNREACHABLE"
            ) from exc
        if resp.status_code != 200:
            detail = resp.text[:200]
            code = "BRIDGE_HTTP_ERROR"
            # 桥的错误信封里带着上游原因（QMT_SOURCE_ERROR / QMT_SOURCE_UNAVAILABLE），
            # 保下来才能把「管道超时」和「桥没起来」分开报。
            try:
                body = resp.json()
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    detail = str(err.get("message") or detail)
                    code = str(err.get("code") or code)
            except Exception:  # noqa: BLE001 - 非 JSON 错误体按原文上报
                pass
            raise QmtExecError(f"桥返回 HTTP {resp.status_code}: {detail}", code=code)
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise QmtExecError(f"桥返回非 JSON: {exc}", code="BRIDGE_BAD_JSON") from exc
        # 桥的 /account、/orders 沿用 TDX 的**裸对象**契约（没有 success 字段）；
        # 只有显式 `success: false` 才是业务错误，不能拿「缺 success」当失败。
        if not isinstance(body, dict):
            raise QmtExecError("桥返回非对象 JSON", code="BRIDGE_BAD_JSON")
        if body.get("success") is False:
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("code") or "桥返回失败")
                code = str(err.get("code") or "BRIDGE_ERROR")
            else:
                message, code = str(err or "桥返回失败"), "BRIDGE_ERROR"
            raise QmtExecError(message, code=code)
        return body

    async def _account(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cache_ts < CACHE_TTL_S:
            return self._cache
        body = await self._post(ACCOUNT_PATH, {"source": "qmt"})
        marker = str(
            body.get("account_source") or body.get("channel_used") or ""
        ).lower()
        if marker != "qmt":
            raise QmtExecError(
                "桥返回的不是 QMT 账户（8550 未加载 source=qmt 路由？）",
                code="NOT_QMT_SOURCE",
            )
        self._cache_ts = now
        self._cache = body
        self.last_stale = bool(body.get("stale"))
        self.last_snapshot_at = str(body.get("snapshot_at") or "")
        account_id = str(body.get("account_id") or "")
        if account_id:
            self._account_id = account_id
        return body

    # -- 查询 ---------------------------------------------------------------
    async def get_asset(self) -> dict[str, Any]:
        body = await self._account()
        asset = body.get("asset") or {}
        return {
            "account_id": str(body.get("account_id") or self._account_id),
            "cash": _f(asset.get("cash")),
            "frozen_cash": _f(asset.get("frozen_cash")),
            "market_value": _f(asset.get("market_value")),
            "total_asset": _f(asset.get("total_asset", asset.get("asset"))),
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        body = await self._account()
        positions: list[dict[str, Any]] = []
        for row in body.get("positions") or []:
            if not isinstance(row, dict):
                continue
            stock_code = str(row.get("stock_code") or "")
            volume = _f(row.get("total_volume", row.get("volume")))
            if not stock_code or volume <= 0:
                continue
            positions.append(
                {
                    "stock_code": stock_code,
                    "symbol": _to_prefix(stock_code),
                    "instrument_name": str(row.get("stock_name") or ""),
                    "volume": volume,
                    "can_use_volume": _f(row.get("available_volume")),
                    "open_price": _f(row.get("cost_price")),
                    "avg_price": _f(row.get("cost_price")),
                    "market_value": _f(row.get("market_value")),
                }
            )
        return positions

    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        body = await self._post(
            ORDERS_PATH,
            {"source": "qmt", "cancelable_only": bool(cancelable_only)},
        )
        return [
            {
                "order_id": str(row.get("order_id") or ""),
                "stock_code": str(row.get("stock_code") or ""),
                "symbol": _to_prefix(row.get("stock_code") or ""),
                "instrument_name": row.get("stock_name") or "",
                "side": str(row.get("side") or "").upper(),
                "order_volume": _f(row.get("total_volume")),
                "traded_volume": _f(row.get("filled_volume")),
                "price": _f(row.get("order_price")),
                "traded_price": _f(row.get("filled_price")),
                "status": str(row.get("status") or ""),
                "order_remark": row.get("order_remark") or "",
                "status_msg": row.get("status_msg") or "",
                "order_time": row.get("time") or "",
            }
            for row in (body.get("orders") or [])
            if isinstance(row, dict)
        ]

    async def query_trades(self) -> list[dict[str, Any]]:
        body = await self._post(ORDERS_PATH, {"source": "qmt", "kind": "trades"})
        return [
            {
                "trade_id": str(row.get("trade_id") or ""),
                "order_id": str(row.get("order_id") or ""),
                "stock_code": str(row.get("stock_code") or ""),
                "symbol": _to_prefix(row.get("stock_code") or ""),
                "instrument_name": row.get("stock_name") or "",
                "side": str(row.get("side") or "").upper(),
                "traded_volume": _f(row.get("traded_volume")),
                "traded_price": _f(row.get("traded_price")),
                "traded_amount": _f(row.get("traded_amount")),
                "traded_at": row.get("traded_at") or "",
                "order_remark": row.get("order_remark") or "",
            }
            for row in (body.get("trades") or [])
            if isinstance(row, dict)
        ]
