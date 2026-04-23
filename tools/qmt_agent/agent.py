"""Top-level QMT agent – WebSocket bridge and background loop orchestration."""
from __future__ import annotations

import concurrent.futures
import math
import json
import logging
import importlib.util
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import websocket

try:
    from .auth import AuthManager
    from .client import QMTClient
    from .config import AgentConfig
    from .reporter import BridgeReporter
except ImportError:
    _MODULE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    def _load_local_module(module_name: str):
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

    AuthManager = _load_local_module("auth").AuthManager  # type: ignore[attr-defined]
    QMTClient = _load_local_module("client").QMTClient  # type: ignore[attr-defined]
    AgentConfig = _load_local_module("config").AgentConfig  # type: ignore[attr-defined]
    BridgeReporter = _load_local_module("reporter").BridgeReporter  # type: ignore[attr-defined]

logger = logging.getLogger("qmt_agent")

class QMTAgent:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.auth = AuthManager(cfg)
        self.reporter = BridgeReporter(cfg, self.auth)
        self.qmt = QMTClient(cfg, execution_callback=self._handle_qmt_event)
        self.stop_event = threading.Event()
        self.ws: Optional[websocket.WebSocketApp] = None
        self._ws_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self.runtime_state = "idle"
        self.last_error: Optional[str] = None
        self.last_heartbeat_at: Optional[float] = None
        self.last_account_report_at: Optional[float] = None
        self.last_bridge_connect_at: Optional[float] = None
        self.last_execution_at: Optional[float] = None
        self.last_start_at: Optional[float] = None
        self._dirty_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._pending_orders: dict[str, dict[str, Any]] = {}
        self._pending_orders_lock = threading.RLock()
        self._smart_orders: dict[str, dict[str, Any]] = {}
        self._smart_orders_lock = threading.RLock()
        self._dispatch_metrics_lock = threading.RLock()
        self._dispatch_seq = 0
        self._dispatch_queue_maxsize = max(10, int(getattr(self.cfg, "order_dispatch_queue_size", 500) or 500))
        self._dispatch_submit_interval_ms = max(0, int(getattr(self.cfg, "order_submit_interval_ms", 50) or 50))
        self._dispatch_queue: queue.PriorityQueue[tuple[int, int, dict[str, Any]]] = queue.PriorityQueue(
            maxsize=self._dispatch_queue_maxsize
        )
        self._dispatch_enqueued = 0
        self._dispatch_dropped = 0
        self._dispatch_processed = 0
        self._dispatch_max_queue_depth = 0
        self._dispatch_last_queue_wait_ms = 0
        self._dispatch_last_submit_at: Optional[float] = None
        self._dispatch_last_submit_kind = ""
        self._watchdog_interval_seconds = max(3, min(10, int(self.cfg.reconnect_interval_seconds or 5)))
        self._startup_grace_seconds = max(
            20,
            int(self.cfg.heartbeat_interval_seconds or 15) * 3,
            int(getattr(self.cfg, "account_report_interval_seconds", 30) or 30) * 2,
        )

    def _next_dispatch_seq(self) -> int:
        with self._dispatch_metrics_lock:
            self._dispatch_seq += 1
            return self._dispatch_seq

    def _dispatch_metrics_snapshot(self) -> dict[str, Any]:
        dispatch_queue = getattr(self, "_dispatch_queue", None)
        queue_size = dispatch_queue.qsize() if dispatch_queue is not None else 0
        queue_maxsize = getattr(self, "_dispatch_queue_maxsize", 0)
        with getattr(self, "_dispatch_metrics_lock", self._state_lock):
            return {
                "queue_size": queue_size,
                "queue_maxsize": queue_maxsize,
                "enqueued": int(getattr(self, "_dispatch_enqueued", 0) or 0),
                "dropped": int(getattr(self, "_dispatch_dropped", 0) or 0),
                "processed": int(getattr(self, "_dispatch_processed", 0) or 0),
                "max_queue_depth": int(getattr(self, "_dispatch_max_queue_depth", 0) or 0),
                "last_queue_wait_ms": int(getattr(self, "_dispatch_last_queue_wait_ms", 0) or 0),
                "last_submit_at": getattr(self, "_dispatch_last_submit_at", None),
                "last_submit_kind": str(getattr(self, "_dispatch_last_submit_kind", "") or ""),
                "submit_interval_ms": int(getattr(self, "_dispatch_submit_interval_ms", 0) or 0),
            }

    def _enqueue_dispatch(self, kind: str, payload: dict[str, Any], priority: int) -> bool:
        item = {
            "kind": kind,
            "payload": dict(payload or {}),
            "enqueued_at": time.time(),
        }
        dispatch_queue = self._dispatch_queue
        try:
            dispatch_queue.put_nowait((priority, self._next_dispatch_seq(), item))
        except queue.Full:
            with self._dispatch_metrics_lock:
                self._dispatch_dropped += 1
            message = f"QMT Agent 派单队列已满({self._dispatch_queue_maxsize})，请稍后重试"
            logger.warning("%s kind=%s client_order_id=%s", message, kind, payload.get("client_order_id"))
            if kind == "order":
                try:
                    self.reporter.report_execution(
                        {
                            "client_order_id": payload.get("client_order_id"),
                            "exchange_order_id": None,
                            "exchange_trade_id": None,
                            "account_id": self.cfg.account_id,
                            "symbol": payload.get("symbol"),
                            "side": payload.get("side"),
                            "status": "REJECTED",
                            "filled_quantity": 0.0,
                            "filled_price": None,
                            "message": message,
                        }
                    )
                except Exception:
                    logger.exception("report queue-full rejection failed")
            return False

        with self._dispatch_metrics_lock:
            self._dispatch_enqueued += 1
            self._dispatch_max_queue_depth = max(self._dispatch_max_queue_depth, dispatch_queue.qsize())
        return True

    def _recover_pending_client_order_id(self, payload: dict[str, Any]) -> str:
        symbol = str(payload.get("symbol") or "").strip()
        side = str(payload.get("side") or "").strip().upper()
        if not symbol or side not in {"BUY", "SELL"}:
            return ""
        now = time.time()
        recent_candidates: list[tuple[str, dict[str, Any]]] = []
        max_age = max(30, int(getattr(self.cfg, "reconcile_cancel_after_seconds", 60) or 60) * 3)
        with self._pending_orders_lock:
            pending_snapshot = list(self._pending_orders.items())
        for client_order_id, order in pending_snapshot:
            submitted_at = float(order.get("submitted_at") or 0.0)
            if submitted_at <= 0 or now - submitted_at > max_age:
                continue
            if str(order.get("symbol") or "").strip() != symbol:
                continue
            if str(order.get("side") or "").strip().upper() != side:
                continue
            recent_candidates.append((client_order_id, order))

        if len(recent_candidates) != 1:
            return ""
        return str(recent_candidates[0][0] or "").strip()

    def _run_startup_reconcile(self) -> None:
        reconcile_timeout = max(5, min(30, int(getattr(self.cfg, "reconcile_timeout_seconds", 10) or 10)))
        events = None
        try:
            import concurrent.futures
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reconcile")
            future = executor.submit(self.qmt.reconcile_recent_activity)
            try:
                events = future.result(timeout=reconcile_timeout)
            except concurrent.futures.TimeoutError:
                logger.warning("startup reconcile timed out after %ss, skipping", reconcile_timeout)
                executor.shutdown(wait=False)
                return
            finally:
                executor.shutdown(wait=False)
        except Exception as exc:
            logger.warning("startup reconcile failed: %s", exc)
            return

        if not events:
            logger.info("startup reconcile: no historical order/trade events")
            return

        success = 0
        for event in events:
            try:
                self.reporter.report_execution(event)
                success += 1
            except Exception:
                logger.exception("startup reconcile report failed: %s", event.get("client_order_id"))
        with self._state_lock:
            if success > 0:
                self.last_execution_at = time.time()
        logger.info("startup reconcile reported %s/%s events", success, len(events))

    def _handle_qmt_event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type", "execution")
        if event_type in {"asset_updated", "positions_updated"}:
            logger.debug("QMT state change detected: %s, triggering snapshot report", event_type)
            self._dirty_event.set()
            return

        if event_type == "cancel_error":
            logger.warning(
                "QMT cancel_error received: exchange_order_id=%s message=%s",
                payload.get("exchange_order_id", ""),
                payload.get("message", ""),
            )
            return

        exchange_order_id = str(payload.get("exchange_order_id") or "").strip()
        client_order_id = self.qmt.resolve_client_order_id(
            payload.get("client_order_id"),
            exchange_order_id,
            payload.get("seq"),
        )
        if not self.qmt.is_valid_client_order_id(client_order_id):
            client_order_id = self._recover_pending_client_order_id(payload)
            if self.qmt.is_valid_client_order_id(client_order_id):
                logger.info(
                    "recovered client_order_id from pending order: client_order_id=%s exchange_order_id=%s symbol=%s side=%s",
                    client_order_id,
                    exchange_order_id,
                    str(payload.get("symbol") or "").strip(),
                    str(payload.get("side") or "").strip(),
                )
        if not self.qmt.is_valid_client_order_id(client_order_id):
            if not self.qmt.is_valid_exchange_order_id(exchange_order_id):
                logger.warning("skip execution callback without resolvable ids: %s", payload)
                return
            payload["client_order_id"] = ""
        else:
            payload["client_order_id"] = client_order_id
            if self.qmt.is_valid_exchange_order_id(exchange_order_id):
                self.qmt.bind_exchange_order_id(client_order_id, exchange_order_id)
                with self._pending_orders_lock:
                    pending = self._pending_orders.get(client_order_id)
                    if pending is not None:
                        pending["exchange_order_id"] = exchange_order_id
        status = str(payload.get("status") or "").strip().upper()

        # Handle Sub-orders for Smart Execution
        main_client_order_id = client_order_id
        is_sub_order = False
        if client_order_id and "_" in client_order_id:
            potential_main = client_order_id.split("_")[0]
            with self._smart_orders_lock:
                if potential_main in self._smart_orders:
                    main_client_order_id = potential_main
                    is_sub_order = True

        if is_sub_order:
            with self._smart_orders_lock:
                so = self._smart_orders.get(main_client_order_id)
                if so and so.get("current_sub_client_order_id") == client_order_id:
                    if status in {"CANCELLED", "PARTIALLY_CANCELLED", "REJECTED"}:
                        filled = float(payload.get("filled_quantity") or 0.0)
                        so["remaining_quantity"] -= filled
                        so["remaining_quantity"] = max(0.0, so["remaining_quantity"])
                        if so["remaining_quantity"] <= 0:
                            self._smart_orders.pop(main_client_order_id, None)
                        else:
                            so["state"] = "pending"
                            so["last_action_ts"] = time.time()
                    elif status == "PARTIALLY_FILLED":
                        filled = float(payload.get("filled_quantity") or 0.0)
                        original_qty = float(so.get("original_payload", {}).get("quantity") or 0)
                        so["remaining_quantity"] = max(0.0, original_qty - filled)
                    elif status == "FILLED":
                        so["remaining_quantity"] = 0
                        self._smart_orders.pop(main_client_order_id, None)

            # Map the response back to parent id for reporter
            payload["client_order_id"] = main_client_order_id

        if self.qmt.is_valid_client_order_id(client_order_id) and status in {
            "FILLED",
            "REJECTED",
            "CANCELLED",
            "PARTIALLY_CANCELLED",
            "EXPIRED",
        }:
            with self._pending_orders_lock:
                self._pending_orders.pop(client_order_id, None)
                if is_sub_order:
                    self._pending_orders.pop(main_client_order_id, None)

        try:
            self.reporter.report_execution(payload)
            with self._state_lock:
                self.last_execution_at = time.time()
        except Exception:
            with self._state_lock:
                self.last_error = "execution callback report failed"
            logger.exception("execution callback report failed")

    def _wait_for_snapshot_settle(self, settle_seconds: int, max_wait_seconds: int | None = None) -> bool:
        """
        等待资产/持仓更新进入短暂稳定期。

        QMT 的 asset_updated 与 positions_updated 往往会在很短时间内连续触发。
        如果每次 dirty 事件都立刻上报，会把中间态连续写入 PostgreSQL，前端就会看到
        两组接近的快照来回跳动。这里做一个小窗口合并，只有在没有新 dirty 事件
        持续出现一小段时间后才真正上报。
        """
        settle_seconds = max(1, int(settle_seconds or 1))
        settle_deadline = time.time() + settle_seconds
        hard_deadline = None
        if max_wait_seconds is not None:
            hard_deadline = time.time() + max(1, int(max_wait_seconds))
        while not self.stop_event.is_set():
            now = time.time()
            if hard_deadline is not None and now >= hard_deadline:
                return True
            remaining = settle_deadline - now
            if remaining <= 0:
                return True
            if self._dirty_event.wait(timeout=remaining):
                self._dirty_event.clear()
                settle_deadline = time.time() + settle_seconds
        return False

    def on_open(self, _ws) -> None:
        logger.info("bridge websocket connected")
        with self._state_lock:
            self.runtime_state = "running"
            self.last_bridge_connect_at = time.time()
            self.last_error = None

    def _process_order_message(self, payload: dict[str, Any]) -> None:
        """在独立线程中处理下单消息，避免阻塞 WebSocket 接收线程。"""
        logger.debug("on_message received complete payload: %s", json.dumps(payload, ensure_ascii=False))
        logger.info("received order client_order_id=%s", payload.get("client_order_id"))

        dispatch_mode = str(payload.get("dispatch_mode") or "").strip().lower()
        order_type_str = str(payload.get("order_type") or "").strip().upper()
        is_smart = dispatch_mode == "smart" or (
            order_type_str == "MARKET" and bool(self.cfg.enable_smart_for_market)
        )
        if is_smart and bool(self.cfg.enable_smart_execution):
            client_order_id = str(payload.get("client_order_id") or "").strip()
            if client_order_id:
                with self._smart_orders_lock:
                    self._smart_orders[client_order_id] = {
                        "original_payload": payload,
                        "remaining_quantity": float(int(payload.get("quantity") or 0)),
                        "current_exchange_order_id": None,
                        "current_sub_client_order_id": None,
                        "state": "pending",
                        "retries": 0,
                        "last_action_ts": time.time(),
                    }
                logger.info("smart order accepted: %s", client_order_id)
                try:
                    self.reporter.report_execution({
                        "client_order_id": client_order_id,
                        "exchange_order_id": None,
                        "account_id": self.cfg.account_id,
                        "symbol": payload.get("symbol"),
                        "side": payload.get("side"),
                        "status": "ACCEPTED",
                        "filled_quantity": 0.0,
                        "message": "smart execution tracking engaged",
                    })
                except Exception:
                    pass
                return

        try:
            use_async = bool(payload.get("async")) or dispatch_mode == "async"
            result = self.qmt.submit_order_async(payload) if use_async else self.qmt.submit_order(payload)
            logger.debug("submit_order/submit_order_async returned: %s", json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            logger.exception("order execution failed: %s", exc)
            self.reporter.report_execution(
                {
                    "client_order_id": payload.get("client_order_id"),
                    "exchange_order_id": None,
                    "exchange_trade_id": None,
                    "account_id": self.cfg.account_id,
                    "symbol": payload.get("symbol"),
                    "side": payload.get("side"),
                    "status": "REJECTED",
                    "filled_quantity": 0.0,
                    "filled_price": None,
                    "message": str(exc),
                }
            )
            return
        try:
            logger.debug("about to report_execution with payload: %s", json.dumps(result, ensure_ascii=False))
            self.reporter.report_execution(result)
            logger.info("report_execution succeeded for client_order_id=%s", result.get("client_order_id"))
        except Exception as exc:
            logger.exception("report_execution failed: %s", exc)
        if str(result.get("status") or "").strip().upper() in {"SUBMITTED", "PARTIALLY_FILLED"}:
            client_order_id = str(result.get("client_order_id") or "").strip()
            exchange_order_id = str(result.get("exchange_order_id") or "").strip()
            if client_order_id:
                with self._pending_orders_lock:
                    self._pending_orders[client_order_id] = {
                        "client_order_id": client_order_id,
                        "exchange_order_id": exchange_order_id or "",
                        "symbol": str(result.get("symbol") or "").strip(),
                        "side": str(result.get("side") or "").strip(),
                        "submitted_at": time.time(),
                        "last_status": str(result.get("status") or "").strip().upper(),
                    }

    def _process_cancel_message(self, payload: dict[str, Any]) -> None:
        """在独立线程中处理撤单消息，避免阻塞 WebSocket 接收线程。"""
        client_order_id = str(payload.get("client_order_id") or "").strip()
        provided_exchange_order_id = str(payload.get("exchange_order_id") or "").strip()
        exchange_order_id = self.qmt.resolve_exchange_order_id(
            provided_exchange_order_id,
            client_order_id=client_order_id,
        )
        logger.info("received cancel client_order_id=%s exchange_order_id=%s", client_order_id, exchange_order_id)
        try:
            if not exchange_order_id:
                raise ValueError("missing resolvable exchange_order_id for cancel request")
            use_async = bool(payload.get("async")) or str(payload.get("dispatch_mode") or "").strip().lower() == "async"
            cancel_result = (
                self.qmt.cancel_order_async(exchange_order_id, client_order_id=client_order_id)
                if use_async
                else self.qmt.cancel_order(exchange_order_id)
            )
            if cancel_result.get("accepted"):
                logger.info(
                    "cancel request accepted by qmt: client_order_id=%s exchange_order_id=%s",
                    client_order_id,
                    exchange_order_id,
                )
            else:
                logger.warning(
                    "cancel request rejected by qmt: client_order_id=%s exchange_order_id=%s code=%s",
                    client_order_id,
                    exchange_order_id,
                    cancel_result.get("code") or cancel_result.get("seq"),
                )
        except Exception as exc:
            logger.exception("cancel order failed: %s", exc)

    def on_message(self, _ws, message: str) -> None:
        data = json.loads(message)
        msg_type = data.get("type")
        if msg_type == "order":
            payload = data.get("payload", {}) or {}
            self._enqueue_dispatch("order", payload, priority=1)
        elif msg_type == "cancel":
            payload = data.get("payload", {}) or {}
            self._enqueue_dispatch("cancel", payload, priority=0)

    def on_error(self, _ws, error: Any) -> None:
        with self._state_lock:
            self.last_error = str(error)
        logger.warning("bridge websocket error: %s", error)

    def on_close(self, _ws, code: Any, msg: Any) -> None:
        with self._state_lock:
            if not self.stop_event.is_set():
                self.runtime_state = "reconnecting"
        logger.warning("bridge websocket closed: code=%s msg=%s", code, msg)

    def _refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                refreshed = self.auth.refresh_if_needed()
                if refreshed:
                    self._close_ws()
            except Exception as exc:
                with self._state_lock:
                    self.last_error = f"session refresh failed: {exc}"
                logger.warning("session refresh failed: %s, attempting re-bootstrap", exc)
                try:
                    self.auth.bootstrap()
                    logger.info("session re-bootstrap succeeded")
                except Exception as bootstrap_exc:
                    with self._state_lock:
                        self.last_error = f"session bootstrap failed: {bootstrap_exc}"
                    logger.warning("session re-bootstrap failed: %s", bootstrap_exc)
            self.stop_event.wait(1)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.reporter.report_heartbeat(
                    {
                        "account_id": self.cfg.account_id,
                        "client_version": self.cfg.client_version,
                        "hostname": self.cfg.hostname,
                        "status": "running",
                        "qmt_connected": self.qmt.is_connected(),
                        "latency_ms": 0,
                    }
                )
                with self._state_lock:
                    self.last_heartbeat_at = time.time()
            except Exception as exc:
                with self._state_lock:
                    self.last_error = f"heartbeat report failed: {exc}"
                logger.warning("heartbeat report failed: %s", exc)
            self.stop_event.wait(max(1, self.cfg.heartbeat_interval_seconds))

    def _ws_app_ping_loop(self) -> None:
        """
        向 /ws/bridge 发送应用层 ping，匹配 stream 的连接活跃判定逻辑。
        仅依赖 websocket 协议层 ping 不会刷新 ws_core 的 heartbeat。
        """
        interval = max(10, int(self.cfg.ws_ping_interval_seconds or 20))
        while not self.stop_event.is_set():
            try:
                with self._ws_lock:
                    ws = self.ws
                if ws is not None:
                    ws.send(json.dumps({"type": "ping", "ts": time.time()}))
            except Exception:
                # 连接切换期出现发送失败属于正常现象，交由 ws 重连流程处理
                pass
            self.stop_event.wait(interval)

    def _account_loop(self) -> None:
        interval = max(1, int(getattr(self.cfg, "account_report_interval_seconds", 30) or 30))
        settle_window = max(3, min(10, interval // 2))
        while not self.stop_event.is_set():
            triggered = self._dirty_event.wait(timeout=interval)
            self._dirty_event.clear()

            max_settle_window = max(interval, settle_window * 3)
            if triggered and not self._wait_for_snapshot_settle(settle_window, max_wait_seconds=max_settle_window):
                continue

            try:
                snapshot = self.qmt.snapshot(prefer_fresh=True)
                self.reporter.report_account(snapshot)
                with self._state_lock:
                    self.last_account_report_at = time.time()
            except Exception as exc:
                with self._state_lock:
                    self.last_error = f"account report failed: {exc}"
                logger.warning("account report failed: %s", exc)
                if "QMT not connected" in str(exc):
                    self.qmt.request_reconnect()

    def _order_timeout_loop(self) -> None:
        cancel_after_seconds = max(1, int(getattr(self.cfg, "reconcile_cancel_after_seconds", 60) or 60))
        check_interval = min(10, max(3, cancel_after_seconds // 4))
        while not self.stop_event.is_set():
            now = time.time()
            stale_orders: list[dict[str, Any]] = []
            with self._pending_orders_lock:
                pending_snapshot = list(self._pending_orders.values())
            for order in pending_snapshot:
                client_order_id = str(order.get("client_order_id") or "").strip()
                exchange_order_id = str(order.get("exchange_order_id") or "").strip()
                submitted_at = float(order.get("submitted_at") or 0.0)
                if not client_order_id or not exchange_order_id or submitted_at <= 0:
                    continue
                if now - submitted_at < cancel_after_seconds:
                    continue
                # Skip orders that already have a cancel in flight
                if order.get("cancel_requested_at"):
                    continue
                stale_orders.append(order)

            for order in stale_orders:
                client_order_id = str(order.get("client_order_id") or "").strip()
                exchange_order_id = str(order.get("exchange_order_id") or "").strip()
                if not client_order_id or not exchange_order_id:
                    continue
                try:
                    logger.info(
                        "pending order timeout reached, issuing cancel client_order_id=%s exchange_order_id=%s age=%ss",
                        client_order_id,
                        exchange_order_id,
                        int(now - float(order.get("submitted_at") or now)),
                    )
                    cancel_result = self.qmt.cancel_order(exchange_order_id)
                    if cancel_result.get("accepted"):
                        with self._pending_orders_lock:
                            if client_order_id in self._pending_orders:
                                self._pending_orders[client_order_id]["cancel_requested_at"] = now
                                self._pending_orders[client_order_id]["last_status"] = "CANCEL_PENDING"
                    else:
                        logger.warning(
                            "pending order cancel rejected client_order_id=%s exchange_order_id=%s code=%s",
                            client_order_id,
                            exchange_order_id,
                            cancel_result.get("code"),
                        )
                except Exception as exc:
                    logger.exception(
                        "pending order cancel failed client_order_id=%s exchange_order_id=%s: %s",
                        client_order_id,
                        exchange_order_id,
                        exc,
                    )
            self.stop_event.wait(check_interval)

    def _smart_execution_loop(self) -> None:
        """周期性轮询 _smart_orders 进行发单、撤单、追单操作。
        
        设计原则：在锁外完成所有 I/O（get_level1_price、submit_order_async、cancel_order_async），
        仅在读写 _smart_orders 状态时加锁，避免持锁阻塞其他线程。
        """
        while not self.stop_event.is_set():
            now = time.time()

            # 1. 在锁内快照当前所有 smart order 的状态，锁外执行 I/O
            with self._smart_orders_lock:
                snapshot = {k: dict(v) for k, v in self._smart_orders.items()}

            for client_order_id, so in snapshot.items():
                if so["remaining_quantity"] <= 0:
                    with self._smart_orders_lock:
                        self._smart_orders.pop(client_order_id, None)
                    continue

                if so["state"] == "pending":
                    if so["retries"] >= int(self.cfg.smart_max_retries):
                        logger.warning("smart execution max retries reached for %s", client_order_id)
                        original_qty = float(so.get("original_payload", {}).get("quantity") or 0)
                        filled_qty = max(0.0, original_qty - so["remaining_quantity"])
                        with self._smart_orders_lock:
                            self._smart_orders.pop(client_order_id, None)
                        try:
                            self.reporter.report_execution({
                                "client_order_id": client_order_id,
                                "account_id": self.cfg.account_id,
                                "symbol": so.get("original_payload", {}).get("symbol"),
                                "side": so.get("original_payload", {}).get("side"),
                                "status": "EXPIRED",
                                "message": "max smart retries reached",
                                "filled_quantity": filled_qty,
                            })
                        except Exception:
                            pass
                        continue

                    payload = so["original_payload"].copy()
                    symbol = payload.get("symbol")
                    side = payload.get("side")
                    # I/O 在锁外执行
                    price = self.qmt.get_level1_price(symbol, side)
                    if price <= 0:
                        logger.warning("smart order got 0.0 price for %s, will retry next tick", client_order_id)
                        continue

                    new_retry = so["retries"] + 1
                    sub_client_id = f"{client_order_id}_{new_retry}"
                    payload["client_order_id"] = sub_client_id
                    payload["price"] = price
                    payload["order_type"] = "LIMIT"
                    payload["quantity"] = so["remaining_quantity"]

                    try:
                        logger.info("smart order %s submitting chunk %s at price %s", client_order_id, new_retry, price)
                        # I/O 在锁外执行
                        self.qmt.submit_order_async(payload)
                        with self._smart_orders_lock:
                            if client_order_id in self._smart_orders:
                                self._smart_orders[client_order_id]["retries"] = new_retry
                                self._smart_orders[client_order_id]["current_sub_client_order_id"] = sub_client_id
                                self._smart_orders[client_order_id]["state"] = "submitted"
                                self._smart_orders[client_order_id]["last_action_ts"] = now
                    except Exception as e:
                        logger.error("smart order submit failed for %s: %s", client_order_id, e)

                elif so["state"] == "submitted":
                    timeout = int(self.cfg.smart_timeout_seconds)
                    if now - so["last_action_ts"] > timeout:
                        # I/O 在锁外执行
                        exchange_order_id = self.qmt.resolve_exchange_order_id("", so["current_sub_client_order_id"])
                        if exchange_order_id:
                            logger.info("smart order %s timeout, cancelling sub order: %s", client_order_id, exchange_order_id)
                            self.qmt.cancel_order_async(exchange_order_id, so["current_sub_client_order_id"])
                            with self._smart_orders_lock:
                                if client_order_id in self._smart_orders:
                                    self._smart_orders[client_order_id]["state"] = "cancelling"
                                    self._smart_orders[client_order_id]["last_action_ts"] = now
                        else:
                            if now - so["last_action_ts"] > timeout + 5:
                                logger.warning("smart order %s sub order not found in QMT after timeout, resetting", client_order_id)
                                with self._smart_orders_lock:
                                    if client_order_id in self._smart_orders:
                                        self._smart_orders[client_order_id]["state"] = "pending"

                elif so["state"] == "cancelling":
                    if now - so["last_action_ts"] > 15:
                        logger.warning("smart order %s stuck cancelling, forcing pending", client_order_id)
                        with self._smart_orders_lock:
                            if client_order_id in self._smart_orders:
                                self._smart_orders[client_order_id]["state"] = "pending"

            self.stop_event.wait(1.0)

    def _close_ws(self) -> None:
        with self._ws_lock:
            ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run_ws_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                headers = self.auth.authorization_header()
                ws = websocket.WebSocketApp(
                    self.cfg.server_url,
                    header=[f"{k}: {v}" for k, v in headers.items()],
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                with self._ws_lock:
                    self.ws = ws
                websocket.setdefaulttimeout(30)
                ws.run_forever()
            except Exception as exc:
                logger.warning("websocket loop failed: %s", exc)

            if self.stop_event.is_set():
                break
            time.sleep(max(1, self.cfg.reconnect_interval_seconds))

    def _thread_target_guard(self, name: str, target: Any) -> Any:
        def _runner(*args: Any, **kwargs: Any) -> Any:
            logger.info("background thread started: %s", name)
            try:
                return target(*args, **kwargs)
            except BaseException as exc:  # pragma: no cover - thread boundary guard
                with self._state_lock:
                    self.last_error = f"{name} crashed: {exc}"
                logger.exception("background thread crashed: %s", name)
                raise
            finally:
                logger.info("background thread exited: %s", name)

        return _runner

    def _spawn_background_thread(self, name: str, target: Any, *args: Any) -> threading.Thread:
        thread = threading.Thread(
            target=self._thread_target_guard(name, target),
            name=name,
            args=args,
            daemon=True,
        )
        with self._state_lock:
            self._threads[name] = thread
        thread.start()
        return thread

    @staticmethod
    def _age_seconds(value: Optional[float]) -> float:
        if not value:
            return float("inf")
        return max(0.0, time.time() - float(value))

    @staticmethod
    def _format_age_reason(label: str, age_seconds: float) -> str:
        if not math.isfinite(age_seconds):
            return f"{label}(未上报)"
        return f"{label}({int(age_seconds)}s)"

    def _runtime_health_snapshot(self) -> dict[str, Any]:
        heartbeat_interval = max(1, int(self.cfg.heartbeat_interval_seconds or 15))
        account_interval = max(1, int(getattr(self.cfg, "account_report_interval_seconds", 30) or 30))
        heartbeat_age = self._age_seconds(self.last_heartbeat_at)
        account_age = self._age_seconds(self.last_account_report_at)
        startup_age = self._age_seconds(self.last_start_at)
        in_startup_grace = startup_age <= float(self._startup_grace_seconds)
        dispatch_metrics = self._dispatch_metrics_snapshot()
        with self._state_lock:
            thread_items = list(self._threads.items())
        thread_states = {name: thread.is_alive() for name, thread in thread_items}
        critical_threads = [
            "bridge-websocket",
            "bridge-refresh",
            "bridge-heartbeat",
            "bridge-account",
            "bridge-order-timeout",
            "bridge-order-dispatch",
            "bridge-smart-execution",
            "bridge-app-ping",
            "bridge-watchdog",
        ]
        dead_threads = [name for name in critical_threads if not thread_states.get(name, False)]
        reasons: list[str] = []
        if (not in_startup_grace) and heartbeat_age > heartbeat_interval * 2:
            reasons.append(self._format_age_reason("heartbeat_stale", heartbeat_age))
        if (not in_startup_grace) and account_age > account_interval * 2:
            reasons.append(self._format_age_reason("account_stale", account_age))
        if dead_threads:
            reasons.append(f"thread_dead({','.join(dead_threads)})")
        queue_size = int(dispatch_metrics.get("queue_size") or 0)
        queue_maxsize = max(1, int(dispatch_metrics.get("queue_maxsize") or 1))
        if queue_size >= max(10, math.ceil(queue_maxsize * 0.8)):
            reasons.append(f"dispatch_queue_busy({queue_size}/{queue_maxsize})")
        if int(dispatch_metrics.get("dropped") or 0) > 0:
            reasons.append(f"dispatch_queue_dropped({int(dispatch_metrics.get('dropped') or 0)})")
        if not reasons:
            health = "healthy"
        elif dead_threads:
            health = "degraded"
        elif any(reason.startswith("dispatch_queue_") for reason in reasons):
            health = "degraded"
        else:
            health = "stale"
        return {
            "health": health,
            "health_reason": ";".join(reasons) if reasons else "",
            "heartbeat_interval_seconds": heartbeat_interval,
            "account_report_interval_seconds": account_interval,
            "heartbeat_age_seconds": None if not math.isfinite(heartbeat_age) else int(heartbeat_age),
            "account_age_seconds": None if not math.isfinite(account_age) else int(account_age),
            "startup_age_seconds": None if not math.isfinite(startup_age) else int(startup_age),
            "startup_grace_seconds": int(self._startup_grace_seconds),
            "in_startup_grace": bool(in_startup_grace),
            "worker_threads": thread_states,
            "dispatch_metrics": dispatch_metrics,
        }

    def _order_dispatch_loop(self) -> None:
        dispatch_queue = self._dispatch_queue
        submit_interval_seconds = float(self._dispatch_submit_interval_ms) / 1000.0
        while not self.stop_event.is_set():
            try:
                _priority, _seq, item = dispatch_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            started_at = time.time()
            wait_ms = max(0, int((started_at - float(item.get("enqueued_at") or started_at)) * 1000))
            kind = str(item.get("kind") or "")
            payload = dict(item.get("payload") or {})

            try:
                if kind == "cancel":
                    self._process_cancel_message(payload)
                else:
                    self._process_order_message(payload)
            finally:
                with self._dispatch_metrics_lock:
                    self._dispatch_processed += 1
                    self._dispatch_last_queue_wait_ms = wait_ms
                    self._dispatch_last_submit_at = started_at
                    self._dispatch_last_submit_kind = kind
                dispatch_queue.task_done()

            if submit_interval_seconds > 0 and not self.stop_event.is_set():
                self.stop_event.wait(submit_interval_seconds)

    def _ensure_background_threads(self) -> None:
        desired = {
            "bridge-refresh": self._refresh_loop,
            "bridge-heartbeat": self._heartbeat_loop,
            "bridge-account": self._account_loop,
            "bridge-order-timeout": self._order_timeout_loop,
            "bridge-order-dispatch": self._order_dispatch_loop,
            "bridge-smart-execution": self._smart_execution_loop,
            "bridge-app-ping": self._ws_app_ping_loop,
            "qmt-reconnect": self.qmt.reconnect_if_needed,
            "bridge-websocket": self._run_ws_forever,
            "bridge-watchdog": self._watchdog_loop,
        }
        with self._state_lock:
            existing_threads = dict(self._threads)
        for name, target in desired.items():
            existing = existing_threads.get(name)
            if existing is not None and existing.is_alive():
                continue
            if name == "qmt-reconnect":
                self._spawn_background_thread(name, target, self.stop_event)
            else:
                self._spawn_background_thread(name, target)

    def _watchdog_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._ensure_background_threads()
                health = self._runtime_health_snapshot()
                reason = str(health.get("health_reason") or "")
                if reason:
                    with self._state_lock:
                        self.runtime_state = "degraded" if health.get("health") != "healthy" else self.runtime_state
                        self.last_error = reason
                    logger.warning("runtime watchdog detected issue: %s", reason)
                    if ("heartbeat_stale" in reason or "account_stale" in reason) and not bool(health.get("in_startup_grace")):
                        self.qmt.request_reconnect()
            except Exception as exc:
                with self._state_lock:
                    self.last_error = f"watchdog failed: {exc}"
                logger.exception("runtime watchdog failed")
            self.stop_event.wait(self._watchdog_interval_seconds)

    def start(self) -> None:
        self.stop_event.clear()
        with self._state_lock:
            self._threads = {}
        with self._state_lock:
            self.last_error = None
            self.last_start_at = time.time()
        self.auth.bootstrap()
        if not self.qmt.connect():
            logger.warning("QMT initial connection failed, heartbeat will report disconnected state")
            with self._state_lock:
                self.runtime_state = "degraded"
                self.last_error = "QMT initial connection failed"
        else:
            with self._state_lock:
                self.runtime_state = "starting"

        bootstrap_snapshot = None
        try:
            bootstrap_snapshot = self.qmt.snapshot(prefer_fresh=True)
        except Exception as exc:
            logger.warning("initial account snapshot unavailable: %s", exc)
        if bootstrap_snapshot is not None:
            try:
                self.reporter.report_account(bootstrap_snapshot)
                with self._state_lock:
                    self.last_account_report_at = time.time()
            except Exception as exc:
                logger.warning("initial account report failed: %s", exc)
        try:
            self.reporter.report_heartbeat(
                {
                    "account_id": self.cfg.account_id,
                    "client_version": self.cfg.client_version,
                    "hostname": self.cfg.hostname,
                    "status": "running",
                    "qmt_connected": self.qmt.is_connected(),
                    "latency_ms": 0,
                }
            )
            with self._state_lock:
                self.last_heartbeat_at = time.time()
        except Exception as exc:
            logger.warning("initial heartbeat report failed: %s", exc)
        self._run_startup_reconcile()

        self._spawn_background_thread("bridge-refresh", self._refresh_loop)
        self._spawn_background_thread("bridge-heartbeat", self._heartbeat_loop)
        self._spawn_background_thread("bridge-account", self._account_loop)
        self._spawn_background_thread("bridge-order-timeout", self._order_timeout_loop)
        self._spawn_background_thread("bridge-order-dispatch", self._order_dispatch_loop)
        self._spawn_background_thread("bridge-smart-execution", self._smart_execution_loop)
        self._spawn_background_thread("bridge-app-ping", self._ws_app_ping_loop)
        self._spawn_background_thread("qmt-reconnect", self.qmt.reconnect_if_needed, self.stop_event)
        self._spawn_background_thread("bridge-websocket", self._run_ws_forever)
        self._spawn_background_thread("bridge-watchdog", self._watchdog_loop)

        while not self.stop_event.is_set():
            time.sleep(1)

    def stop(self) -> None:
        self.stop_event.set()
        self._close_ws()
        self.qmt.close()
        with self._state_lock:
            self.runtime_state = "stopped"

    def get_runtime_status(self) -> dict[str, Any]:
        thread_states = {name: thread.is_alive() for name, thread in self._threads.items()}
        health = self._runtime_health_snapshot()
        with self._state_lock:
            return {
                "runtime_state": self.runtime_state,
                "runtime_health": health.get("health"),
                "runtime_health_reason": health.get("health_reason"),
                "qmt_connected": self.qmt.is_connected(),
                "last_error": self.last_error,
                "last_start_at": self.last_start_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_account_report_at": self.last_account_report_at,
                "heartbeat_age_seconds": health.get("heartbeat_age_seconds"),
                "account_age_seconds": health.get("account_age_seconds"),
                "last_bridge_connect_at": self.last_bridge_connect_at,
                "last_execution_at": self.last_execution_at,
                "hostname": self.cfg.hostname,
                "client_fingerprint": self.cfg.client_fingerprint,
                "client_version": self.cfg.client_version,
                "account_id": self.cfg.account_id,
                "worker_threads": thread_states,
                "dispatch_metrics": health.get("dispatch_metrics"),
            }
