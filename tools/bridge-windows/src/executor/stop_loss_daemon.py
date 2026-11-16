import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime

from ..core.types import PriceType, Side
from ..executor.order_tracker import atomic_write
from ..tdx.client import TdxClient, TdxError, TdxTimeoutError
from .exit_result import (
    KIND_ERROR,
    KIND_NO_POSITION,
    KIND_UNKNOWN,
    ExitResult,
    apply_exit_outcome,
    classify_order_result,
)

log = logging.getLogger(__name__)


class StopLossDaemon:
    """止损/止盈/移动止损后台守护, 在 Windows 侧轮询行情, 触发即市价卖出."""

    def __init__(self, tdx: TdxClient, state_file: str, account_id: int = 0,
                 poll_interval: float = 5.0, trade_log=None):
        import threading
        self.tdx = tdx
        self.state_file = state_file
        self.account_id = account_id
        self.poll_interval = poll_interval
        self.trade_log = trade_log
        self._items = {}
        self._lock = threading.Lock()  # 保护 _items 并发访问
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            for it in data.get("items", []):
                self._items[it["stock_code"]] = it
        except (OSError, ValueError) as e:
            log.error(f"加载止损状态失败: {e}")

    def save(self) -> None:
        atomic_write(self.state_file, {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "items": list(self._items.values()),
        })

    def register_items(self, items: list, account_id: int = 0) -> int:
        """配置止损监控. items: [{stock_code, entry_price, volume, stop_loss_pct, take_profit_pct, trailing_stop_pct, enabled}]"""
        if account_id:
            self.account_id = account_id
        n = 0
        with self._lock:
            for it in items:
                code = it.get("stock_code")
                if not code:
                    continue
                rec = {
                    "stock_code": code,
                    "entry_price": it.get("entry_price", 0.0),
                    "volume": it.get("volume", 0),
                    "highest_price_since_entry": it.get("entry_price", 0.0),
                    "stop_loss_pct": it.get("stop_loss_pct"),
                    "take_profit_pct": it.get("take_profit_pct"),
                    "trailing_stop_pct": it.get("trailing_stop_pct"),
                    "enabled": it.get("enabled", True),
                    "order_id": it.get("order_id", ""),
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                # 合并到已有条目, 保留更高点
                if code in self._items and self._items[code]["highest_price_since_entry"] > rec["entry_price"]:
                    rec["highest_price_since_entry"] = self._items[code]["highest_price_since_entry"]
                self._items[code] = rec
                n += 1
        self.save()
        return n

    def state_items(self) -> list:
        with self._lock:
            return list(self._items.values())

    def remove(self, stock_code: str) -> bool:
        with self._lock:
            if stock_code in self._items:
                del self._items[stock_code]
                self.save()
                return True
            return False

    async def run(self):
        log.info(f"止损监控启动: 每 {self.poll_interval}s 轮询 {len(self._items)} 个标的")
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except Exception as e:
                log.error(f"止损监控循环错误: {e}")
            await asyncio.sleep(self.poll_interval)

    def _tick(self) -> None:
        dirty = False
        now = datetime.now().timestamp()
        for code, it in list(self._items.items()):
            if not it["enabled"]:
                continue
            # 失败退避窗内不重复触发（超时/拒单/无仓都保留规则）。
            try:
                retry_at = float(it.get("retry_after_ts") or 0.0)
            except (TypeError, ValueError):
                retry_at = 0.0
            if retry_at > now:
                continue
            try:
                price = self.tdx.get_latest_price(code)
            except TdxError as e:
                log.warning(f"{code} 取价失败: {e}")
                continue

            # 更新移动止损最高价 (只升不降)
            if price > it["highest_price_since_entry"]:
                it["highest_price_since_entry"] = price
                dirty = True

            trigger, reason = self._check(it, price)
            if not trigger:
                continue
            log.warning(f"[{reason}] {code} 现价 {price}")
            result = self._execute_exit(it, code, reason, price)
            self._items[code] = apply_exit_outcome(it, result, now)
            if not result.is_success:
                # 保留 enabled，退避 + 告警；不再"卖失败就永久停用"。
                log.error(
                    f"{code} 止损退出未成功 kind={result.kind}: {result.detail}"
                )
                self._log_exit_failure(code, reason, result, price)
            dirty = True
        if dirty:
            self.save()

    def _log_exit_failure(self, code: str, reason: str, result: ExitResult,
                          price: float) -> None:
        if not self.trade_log:
            return
        try:
            self.trade_log({
                "event": "sltp_exit_failed", "stock": code,
                "reason": reason, "trigger_price": price,
                "result": result.kind, "msg": result.detail,
            })
        except Exception as e:  # noqa: BLE001 - 日志失败不影响规则
            log.warning(f"写止损失败记录失败(忽略): {e}")

    def _check(self, it: dict, price: float):
        entry = it["entry_price"]
        if entry <= 0:
            return None, None
        if it.get("stop_loss_pct"):
            sl = entry * (1 - it["stop_loss_pct"])
            if price <= sl:
                return True, f"固定止损 price={price:.2f} <= {sl:.2f}"
        if it.get("take_profit_pct"):
            tp = entry * (1 + it["take_profit_pct"])
            if price >= tp:
                return True, f"固定止盈 price={price:.2f} >= {tp:.2f}"
        if it.get("trailing_stop_pct"):
            highest = it["highest_price_since_entry"]
            trail = highest * (1 - it["trailing_stop_pct"])
            if price <= trail:
                return True, f"移动止损 price={price:.2f} <= {trail:.2f} (最高 {highest:.2f})"
        return None, None

    def _execute_exit(self, it: dict, code: str, reason: str,
                      price: float) -> ExitResult:
        """触发后市价卖出；返回三态结果，调用方据此决定是否停用规则。"""
        volume = it["volume"]
        try:
            positions = self.tdx.query_stock_positions(self.account_id)
            avail = next((int(p.get("CanUseVol") or 0) for p in positions
                          if p.get("Code") == code), 0)
            if avail <= 0:
                log.warning(f"{code} 无可卖持仓, 跳过止损卖出")
                return ExitResult(KIND_NO_POSITION, "no available position")
            volume = min(volume, avail) if volume else avail
            res = self.tdx.order_stock(self.account_id, code, Side.SELL, volume,
                                       PriceType.MARKET, 0.0)
            result = classify_order_result(
                res.get("Value", "0"), str(res.get("Msg", "") or ""))
            record = {"event": "sltp_exit", "stock": code, "reason": reason,
                      "trigger_price": price, "volume": volume,
                      "order_id": str(res.get("Wtbh", "")),
                      "value": str(res.get("Value", "0")),
                      "result": result.kind,
                      "msg": res.get("Msg", "")}
            log.info(f"止损卖出 {code}: {record}")
            if self.trade_log:
                self.trade_log(record)
            return ExitResult(
                result.kind, result.detail, str(res.get("Wtbh", "") or ""))
        except TdxTimeoutError as e:
            log.error(f"止损卖出 {code} 超时(结果未知): {e}")
            return ExitResult(KIND_UNKNOWN, f"timeout: {e}")
        except TdxError as e:
            log.error(f"止损卖出 {code} 失败: {e}")
            return ExitResult(KIND_ERROR, str(e))
        except Exception as e:  # noqa: BLE001 - 未预期异常也必须保留规则
            log.error(f"止损卖出 {code} 异常: {e}", exc_info=True)
            return ExitResult(KIND_ERROR, f"{type(e).__name__}: {e}")
