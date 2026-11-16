import json
import logging
import os
import threading
import time
from datetime import datetime

from ..core.trade_plan import TradePlan
from ..core.types import OrderStatus, PriceType, Side
from ..security.trade_limits import TradeRateGuard, verify_hard_limits
from ..tdx import parser
from ..tdx.client import TdxClient, TdxError, TdxTimeoutError
from .plan_state import (
    DECISION_DUPLICATE,
    DECISION_UNKNOWN,
    PlanStateStore,
)

log = logging.getLogger(__name__)


class PlanExecutor:
    """执行 TradePlan: 卖先买后, 资金/持仓校验, 幂等去重, 交易记录同步到共享目录."""

    def __init__(self, tdx: TdxClient, shared_report_dir: str,
                 trade_log_file: str = "trade_log.jsonl", state_dir: str = "",
                 default_notify: int = 0):
        self.tdx = tdx
        # 下单确认：0=客户端手动确认（默认，安全）；1=请求免确认。
        # 实盘免确认需券商开通「支持TQ的版本」，否则仍会返回 Value=1。
        self.default_notify = 1 if int(default_notify or 0) else 0
        self.shared_report_dir = shared_report_dir
        self.trade_log_file = os.path.join(shared_report_dir, trade_log_file)
        self.state_dir = state_dir
        # 持久化幂等：内存只做进程内互斥，跨重启靠按日 JSON。
        self._dedup_lock = threading.Lock()
        self._plan_state = PlanStateStore(state_dir or shared_report_dir)
        self._account_id = -1          # 未显式指定则用 0
        # 写死的交易硬上限：299/秒、1999/日（见 security/trade_limits.py）
        verify_hard_limits()
        # 日计数持久化：桥重启不能清零 1999/日 额度
        self.order_guard = TradeRateGuard(
            state_path=os.path.join(state_dir, "trade_limits_orders.json")
            if state_dir else "")
        self._plan_deadline_s = 20.0

    def resolve_account_id(self, account: str, account_type: str) -> int:
        """用户指定账号则取句柄；显式账号解析失败必须 fail-closed。"""
        if account:
            aid = self.tdx.stock_account(account=account, account_type=account_type)
            if aid >= 0:
                return aid
            raise ValueError(
                f"account={account!r} 解析失败（stock_account 返回 {aid}）；"
                "拒绝回退 0 号账户，避免把单下到别的账户"
            )
        return 0

    # ---- 交易记录同步 ----

    def log_trade(self, record: dict) -> None:
        """把交易记录追加写到共享目录, 供 Linux 侧读取."""
        record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        record.setdefault("bridge", "bridge-windows")
        try:
            os.makedirs(self.shared_report_dir, exist_ok=True)
            with open(self.trade_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error(f"写交易记录失败: {e}")

    # ---- 主流程 ----

    def execute_plan(self, plan: TradePlan) -> dict:
        # 跨重启幂等：started/unknown 绝不自动重放；done/partial/rejected 返回 duplicate。
        with self._dedup_lock:
            decision = self._plan_state.classify(plan.plan_id)
            if decision == DECISION_UNKNOWN:
                return {
                    "plan_id": plan.plan_id, "status": "unknown",
                    "message": "上次执行已开始但未确认完成；请核对柜台后人工处理",
                }
            if decision == DECISION_DUPLICATE:
                return {"plan_id": plan.plan_id, "status": "duplicate",
                        "message": "plan 已执行过"}
            try:
                self._plan_state.mark(plan.plan_id, "started")
            except OSError as e:
                log.error(f"plan 状态落盘失败，拒绝执行 {plan.plan_id}: {e}")
                return {"plan_id": plan.plan_id, "status": "rejected",
                        "message": f"plan 状态无法持久化，拒绝执行: {e}"}

        self.log_trade({"event": "plan_received", "plan": plan.to_dict()})
        deadline = time.monotonic() + self._plan_deadline_s
        try:
            account_id = self.resolve_account_id(plan.account, plan.account_type)
        except (ValueError, TdxError) as e:
            self._mark_done(plan.plan_id, "rejected", error=str(e))
            self.log_trade({"event": "plan_rejected", "plan_id": plan.plan_id,
                            "error": str(e)})
            return {"plan_id": plan.plan_id, "status": "rejected",
                    "message": f"账户解析失败: {e}"}

        orders_out = []
        statuses = []
        # 卖先买后
        sells = [o for o in plan.orders if o.side == Side.SELL.value]
        buys = [o for o in plan.orders if o.side == Side.BUY.value]
        for o in sells + buys:
            if time.monotonic() > deadline:
                orders_out.append(self._order_out(
                    o, "error", None, "计划总超时，未执行（勿重试）"))
                statuses.append("rejected")
                continue
            try:
                result = self._execute_one(account_id, o, plan)
            except Exception as e:  # noqa: BLE001 - 已发单可能已在柜台
                log.error(f"plan {plan.plan_id} 单笔执行异常: {e}", exc_info=True)
                self._mark_done(plan.plan_id, "unknown", error=str(e))
                orders_out.append(self._order_out(
                    o, "error", None, f"执行异常，结果未知（勿重试）: {e}"))
                statuses.append("rejected")
                break
            orders_out.append(result)
            if result["status"] == "filled":
                statuses.append("filled")
            elif result["status"] in ("submitted", "needs_confirm"):
                statuses.append("submitted")
            else:
                statuses.append("rejected")

        overall = "executed" if not any(s in ("rejected", "error") for s in statuses) else "partial"
        if not orders_out:
            overall = "rejected"

        try:
            state = self._plan_state.get(plan.plan_id) or {}
        except Exception:  # noqa: BLE001
            state = {}
        if state.get("status") != "unknown":
            self._mark_done(plan.plan_id, overall)

        report = {
            "plan_id": plan.plan_id,
            "status": overall,
            "orders": orders_out,
            "executed_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.log_trade({"event": "plan_done", "plan_id": plan.plan_id, "status": overall})
        return report

    def _mark_done(self, plan_id: str, status: str, **fields) -> None:
        try:
            self._plan_state.mark(plan_id, status, **fields)
        except OSError as e:
            # 状态写不进去不能改变已发生的下单事实；只能告警。
            log.error(f"plan 状态写盘失败 {plan_id} status={status}: {e}")

    # ---- 单笔下单 ----

    def _execute_one(self, account_id: int, order, plan: TradePlan) -> dict:
        side = Side(order.side)
        stock = order.stock_code

        # 注：旧实现在这里按「当日已有同方向成交」跳过整笔，会挡住合法的
        # 加仓/分批卖。plans 的幂等已由 plan_id + 持久化状态承担，单笔不再
        # 用宽泛的当日方向判据。

        # 卖前校验可用持仓
        if side == Side.SELL:
            try:
                positions = self.tdx.query_stock_positions(account_id)
                avail = next((int(p.get("CanUseVol") or 0) for p in positions
                              if p.get("Code") == stock), 0)
                if avail < order.volume:
                    return self._order_out(order, "rejected", None,
                                           f"可用持仓不足: {avail} < {order.volume}")
            except TdxError as e:
                log.warning(f"查持仓失败: {e}")

        price_type = PriceType(order.price_type) if order.price_type is not None else (
            PriceType.MANUAL if order.order_type == "limit" else PriceType.MARKET)
        price = order.price or 0.0

        # 写死的硬上限：299 笔/秒、1999 笔/日（下单独立计数，撤单不连坐）
        allowed, reason = self.order_guard.allow()
        if not allowed:
            self.log_trade({"event": "order_hard_limit", "stock": stock,
                            "side": side.value, "volume": order.volume,
                            "msg": reason})
            return self._order_out(order, "rejected", None, reason)
        warning = self.order_guard.consume_warning()
        if warning:
            self._send_trade_warning(warning)

        try:
            notify = (order.notify if order.notify is not None
                      else self.default_notify)
            res = self.tdx.order_stock(account_id, stock, side, order.volume,
                                       price_type, price, notify=notify)
            value = str(res.get("Value", "0"))
            wtbh = str(res.get("Wtbh", "") or "")
            msg = res.get("Msg", "")

            if value == "2":
                state = OrderStatus.SUBMITTED
                status = "submitted"
                self.log_trade({"event": "order_placed", "stock": stock, "side": side.value,
                                "volume": order.volume, "price": price, "order_id": wtbh})
            elif value == "1":
                state = OrderStatus.NEEDS_CONFIRM
                status = "needs_confirm"
                self.log_trade({"event": "order_needs_confirm", "stock": stock,
                                "side": side.value, "volume": order.volume,
                                "order_id": wtbh, "msg": msg})
            else:
                state = OrderStatus.REJECTED
                status = "rejected"
                self.log_trade({"event": "order_rejected", "stock": stock,
                                "side": side.value, "volume": order.volume,
                                "msg": msg, "value": value})
            return self._order_out(order, status, wtbh, msg, state)
        except TdxTimeoutError as e:
            # 超时=结果未知，可能已在柜台。按 submitted 报，绝不让上游当失败重试。
            self.log_trade({"event": "order_timeout_unknown", "stock": stock,
                            "side": side.value, "volume": order.volume,
                            "error": str(e)})
            return self._order_out(order, "submitted", None,
                                   f"下单超时，结果未知（勿重试）: {e}",
                                   OrderStatus.SUBMITTED)
        except TdxError as e:
            self.log_trade({"event": "order_error", "stock": stock, "side": side.value,
                            "volume": order.volume, "error": str(e)})
            return self._order_out(order, "error", None, str(e))

    def _send_trade_warning(self, msg: str) -> None:
        """150 笔/日预警：桥日志 + 通达信客户端弹消息（尽力而为）。"""
        log.warning(msg)
        try:
            self.tdx.send_message("MSG," + msg)
        except Exception:
            log.warning("发送交易硬上限预警失败(忽略)", exc_info=True)

    def _order_out(self, order, status: str, order_id, message, state=None):
        out = {
            "stock_code": order.stock_code,
            "side": order.side,
            "volume": order.volume,
            "status": status,
            "order_id": order_id or "",
            "message": message or "",
        }
        if any([order.stop_loss_pct, order.take_profit_pct, order.trailing_stop_pct]):
            out["sltp"] = {
                "stop_loss_pct": order.stop_loss_pct,
                "take_profit_pct": order.take_profit_pct,
                "trailing_stop_pct": order.trailing_stop_pct,
            }
        return out
