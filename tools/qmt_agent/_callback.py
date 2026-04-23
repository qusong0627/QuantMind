"""QMT callback adapter used by the local agent runtime."""
from __future__ import annotations

import logging
from typing import Any

try:
    from .config import _QMT_ORDER_STATUS_MAP
except ImportError:
    import sys
    from pathlib import Path
    _MODULE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    def _load_local_module(module_name: str):
        import importlib.util
        qualified_name = f"qmt_agent_local_{module_name}"
        module = sys.modules.get(qualified_name)
        if module is not None:
            return module
        module_path = _MODULE_DIR / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load local module {module_name} from {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module

    _QMT_ORDER_STATUS_MAP = _load_local_module("config")._QMT_ORDER_STATUS_MAP  # type: ignore[attr-defined]

logger = logging.getLogger("qmt_agent.callback")


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


class _QMTCallbackAdapter:
    def __init__(self, client: Any):
        self.client = client

    def _emit(self, payload: dict[str, Any]) -> None:
        callback = getattr(self.client, "_execution_callback", None)
        if callback:
            callback(payload)

    def _build_payload(
        self,
        data: Any,
        *,
        status: str,
        message: str = "",
        exchange_trade_id: Any = None,
    ) -> dict[str, Any] | None:
        exchange_order_id = _text(_obj_get(data, "order_id", ""))
        if not exchange_order_id:
            exchange_order_id = _text(_obj_get(data, "sysid", ""))
        seq = _obj_get(data, "seq", None)
        client_order_id = _text(
            self.client.resolve_client_order_id(
                client_order_id=self.client._extract_client_order_id(data),
                exchange_order_id=exchange_order_id,
                seq=seq,
            )
        )
        if not client_order_id and not self.client.is_valid_exchange_order_id(exchange_order_id):
            logger.warning(
                "skip qmt callback without resolvable ids: seq=%s exchange_order_id=%s payload=%s",
                seq,
                exchange_order_id,
                data,
            )
            return None
        if client_order_id and exchange_order_id:
            self.client.bind_exchange_order_id(client_order_id, exchange_order_id)

        payload = {
            "client_order_id": client_order_id or "",
            "exchange_order_id": exchange_order_id or None,
            "exchange_trade_id": _text(exchange_trade_id) or None,
            "account_id": getattr(self.client.cfg, "account_id", ""),
            "symbol": _text(_obj_get(data, "stock_code", "")),
            "side": _text(self.client._resolve_side(_obj_get(data, "order_type", 0))),
            "status": status,
            "filled_quantity": float(_obj_get(data, "traded_volume", 0) or _obj_get(data, "trade_volume", 0) or 0),
            "filled_price": float(_obj_get(data, "traded_price", 0) or _obj_get(data, "price", 0) or 0),
            "message": message,
        }
        return payload

    def on_stock_order(self, order: Any) -> None:
        qmt_status = int(_obj_get(order, "order_status", 48) or 48)
        status_msg = _text(_obj_get(order, "status_msg", "") or _obj_get(order, "order_status_msg", "") or "")
        message = f"order_status={qmt_status}" + (f" {status_msg}" if status_msg else "")
        payload = self._build_payload(
            order,
            status=_QMT_ORDER_STATUS_MAP.get(qmt_status, "SUBMITTED"),
            message=message,
        )
        if payload:
            self._emit(payload)

    def on_stock_trade(self, trade: Any) -> None:
        payload = self._build_payload(
            trade,
            status="FILLED",
            exchange_trade_id=_obj_get(trade, "traded_id", ""),
            message="trade callback",
        )
        if payload:
            self._emit(payload)

    def on_order_error(self, error: Any) -> None:
        payload = self._build_payload(
            error,
            status="REJECTED",
            message=_text(_obj_get(error, "error_msg", "") or _obj_get(error, "message", "") or "order error"),
        )
        if payload:
            self._emit(payload)

    def on_cancel_error(self, error: Any) -> None:
        # 撤单失败属于操作反馈，不影响原委托状态，不上报 execution callback。
        # 改为发送专属的 cancel_error 事件供上层感知，避免将原委托误报为 REJECTED。
        exchange_order_id = _text(_obj_get(error, "order_id", "") or _obj_get(error, "sysid", ""))
        message = _text(_obj_get(error, "error_msg", "") or _obj_get(error, "message", "") or "cancel error")
        logger.warning(
            "qmt cancel_error exchange_order_id=%s message=%s",
            exchange_order_id,
            message,
        )
        self._emit(
            {
                "type": "cancel_error",
                "account_id": getattr(self.client.cfg, "account_id", ""),
                "exchange_order_id": exchange_order_id or None,
                "message": message,
            }
        )

    def on_account_status(self, status: Any) -> None:
        self._emit(
            {
                "type": "account_status",
                "account_id": getattr(self.client.cfg, "account_id", ""),
                "status": _text(_obj_get(status, "status", "") or _obj_get(status, "message", "") or "unknown"),
                "message": _text(_obj_get(status, "message", "") or ""),
            }
        )

    def on_order_stock_async_response(self, resp: Any) -> None:
        payload = self._build_payload(
            resp,
            status="SUBMITTED",
            message="async order accepted by qmt",
        )
        if payload:
            self._emit(payload)

    def on_cancel_order_stock_async_response(self, resp: Any) -> None:
        payload = self._build_payload(
            resp,
            status="SUBMITTED",
            message="async cancel accepted by qmt",
        )
        if payload:
            self._emit(payload)


def build_callback(client: Any) -> _QMTCallbackAdapter:
    return _QMTCallbackAdapter(client)
