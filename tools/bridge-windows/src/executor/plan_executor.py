import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime

from ..core.trade_plan import TradePlan
from ..core.types import OrderStatus, PriceType, Side
from ..security.trade_limits import TradeRateGuard, verify_hard_limits
from ..tdx import parser
from ..tdx.client import TdxClient, TdxError, TdxTimeoutError

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
        # 幂等去重：check-then-add 必须原子（trade 槽 2 并发 + file_sync 同进程）
        self._executed_plans: OrderedDict[str, bool] = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._account_id = -1          # 未显式指定则用 0
        # 写死的交易硬上限：299/秒、1999/日（见 security/trade_limits.py）
        verify_hard_limits()
        # 日计数持久化：桥重启不能清零 1999/日 额度
        self.order_guard = TradeRateGuard(
            state_path=os.path.join(state_dir, "trade_limits_orders.json")
            if state_dir else "")
        self._plan_deadline_s = 20.0

    def resolve_account_id(self, account: str, account_type: str) -> int:
        """用户指定账号则取句柄, 否则默认 0."""
        if account:
            aid = self.tdx.stock_account(account=account, account_type=account_type)
            if aid >= 0:
                return aid
            log.warning(f"stock_account 返回句柄 {aid}, 回退默认 0")
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
        with self._dedup_lock:
            if plan.plan_id in self._executed_plans:
                return {"plan_id": plan.plan_id, "status": "duplicate",
                        "message": "plan 已执行过"}
            self._executed_plans[plan.plan_id] = True
            if len(self._executed_plans) > 100000:
                self._executed_plans.popitem(last=False)

        self.log_trade({"event": "plan_received", "plan": plan.to_dict()})
        deadline = time.monotonic() + self._plan_deadline_s
        account_id = self.resolve_account_id(plan.account, plan.account_type)

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
            result = self._execute_one(account_id, o, plan)
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

        report = {
            "plan_id": plan.plan_id,
            "status": overall,
            "orders": orders_out,
            "executed_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.log_trade({"event": "plan_done", "plan_id": plan.plan_id, "status": overall})
        return report

    # ---- 单笔下单 ----

    def _execute_one(self, account_id: int, order, plan: TradePlan) -> dict:
        side = Side(order.side)
        stock = order.stock_code

        # 幂等: 查今日委托是否已存在同代码同方向
        # BSFlag=0(买)/1(卖), 缺失时用 WtVol 符号兜底
        try:
            existing = self.tdx.query_stock_orders(account_id, stock_code=stock)
            want_flag = 1 if side == Side.SELL else 0
            for eo in existing:
                flag = eo.get("BSFlag")
                if flag in (None, ""):
                    flag = 1 if float(eo.get("WtVol") or 0) < 0 else 0
                if int(float(flag)) == want_flag and eo.get("Status") in ("3", 3):
                    log.info(f"{stock} 已有成交委托, 跳过重复下单")
                    return self._order_out(order, "duplicate", None,
                                           "当日已有同方向成交, 跳过")
        except TdxError as e:
            log.warning(f"查委托失败(继续): {e}")

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
