"""QMT broker client – manages xtquant connection and order lifecycle."""
from __future__ import annotations

import collections
import logging
import math
import importlib.util
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .config import AgentConfig, _QMT_ORDER_STATUS_MAP
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

    _config_mod = _load_local_module("config")
    AgentConfig = _config_mod.AgentConfig  # type: ignore[attr-defined]
    _QMT_ORDER_STATUS_MAP = _config_mod._QMT_ORDER_STATUS_MAP  # type: ignore[attr-defined]

logger = logging.getLogger("qmt_agent")

_INVALID_CLIENT_ORDER_IDS = {"", "-1", "0", "none", "null", "nan"}
_INVALID_EXCHANGE_ORDER_IDS = {"", "-1", "0", "none", "null", "nan"}
_ASYNC_SEQ_MAP_MAX_SIZE = 5000
# QMT order_remark 字段有长度上限，会截断 UUID；用此正则确保提取的 client_order_id 是完整 UUID
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_XTQUANT_SEARCH_PATTERNS = (
    "xtquant",
    "*/xtquant",
    "*/*/xtquant",
    "*/*/*/xtquant",
    "site-packages/xtquant",
    "*/site-packages/xtquant",
    "Lib/site-packages/xtquant",
    "*/Lib/site-packages/xtquant",
)

class QMTClient:
    def __init__(
        self,
        cfg: AgentConfig,
        execution_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.cfg = cfg
        self._work_path = Path(cfg.qmt_path).parent if cfg.qmt_path else None
        self._execution_callback = execution_callback
        self._last_asset_cache: Optional[Any] = None
        self._last_positions_cache: list[Any] = []
        self._cache_lock = threading.RLock()
        self._lock = threading.RLock()

        self._trader: Optional[Any] = None
        self._account: Optional[Any] = None
        self._client_order_to_exchange: dict[str, str] = {}
        self._exchange_order_to_client: dict[str, str] = {}
        self._async_seq_to_client_order: collections.OrderedDict[int, str] = collections.OrderedDict()
        self._short_quota_cache: dict[str, dict[str, Any]] = {}
        self._last_short_check_at: float | None = None
        self._shortable_symbols_count: int = 0
        self._reconnect_event = threading.Event()
        self._query_lock = threading.Lock()  # 新增：专门用于控制 QMT 接口并发查询的锁
        self.xtquant_error = ""
        self.xtdata_error = ""
        self.xtquant_search_paths: list[str] = []
        self._ensure_xtquant_imported()

    def _candidate_xtquant_roots(self) -> list[Path]:
        candidates: list[Path] = []
        raw_paths = [
            str(self.cfg.qmt_bin_path or "").strip(),
            str(Path(str(self.cfg.qmt_path or "").strip()).parent / "bin.x64")
            if str(self.cfg.qmt_path or "").strip()
            else "",
        ]
        for raw in raw_paths:
            if not raw:
                continue
            path = Path(raw)
            candidates.append(path)
            candidates.append(path.parent)
            candidates.append(path / "site-packages")
            candidates.append(path / "Lib" / "site-packages")
            candidates.append(path / "python")
            candidates.append(path / "python" / "Lib" / "site-packages")
        seen: set[str] = set()
        unique: list[Path] = []
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def _locate_xtquant_root(self) -> Path | None:
        candidates = self._candidate_xtquant_roots()
        self.xtquant_search_paths = [str(path) for path in candidates]
        for base in candidates:
            if not base.exists():
                continue
            direct = base / "xtquant"
            if direct.exists():
                return base
            for pattern in _XTQUANT_SEARCH_PATTERNS:
                try:
                    for found in base.glob(pattern):
                        if found.is_dir():
                            return found.parent
                except Exception:
                    continue
        return None

    def _ensure_xtquant_imported(self) -> None:
        xtquant_root = self._locate_xtquant_root()
        if xtquant_root is not None:
            xtquant_root_str = str(xtquant_root)
            if xtquant_root_str not in sys.path:
                sys.path.insert(0, xtquant_root_str)
        try:
            from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback  # type: ignore
            from xtquant.xttype import StockAccount  # type: ignore
            try:
                from xtquant import xtdata  # type: ignore
                self._xtdata = xtdata
                self.xtdata_error = ""
            except Exception as e:
                logger.warning("xtdata unavailable: %s", e)
                self._xtdata = None
                self.xtdata_error = str(e)

            self._XtQuantTrader = XtQuantTrader
            self._XtQuantTraderCallback = XtQuantTraderCallback
            self._StockAccount = StockAccount
            self._xtconstant = self._load_xtconstant()
            self.enabled = True
        except Exception as exc:
            self.enabled = False
            self._XtQuantTrader = None
            self._XtQuantTraderCallback = object
            self._StockAccount = None
            self._xtconstant = None
            self._xtdata = None
            self.xtdata_error = ""
            searched = ", ".join(self.xtquant_search_paths) or str(self.cfg.qmt_bin_path or "").strip()
            self.xtquant_error = f"{exc} (searched: {searched})"
            logger.warning("xtquant unavailable, running in mock mode: %s", self.xtquant_error)

    def runtime_dependency_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.enabled:
            detail = str(self.xtquant_error or "").strip()
            if detail:
                errors.append(f"xtquant 加载失败：{detail}")
            else:
                errors.append("xtquant 加载失败")
            return errors
        if getattr(self, "_xtdata", None) is None:
            detail = str(self.xtdata_error or "").strip()
            if detail:
                errors.append(f"xtdata 加载失败：{detail}")
            else:
                errors.append("xtdata 加载失败")
        return errors

    def _load_xtconstant(self):
        try:
            from xtquant import xtconstant  # type: ignore

            return xtconstant
        except Exception:
            return None

    @staticmethod
    def _to_qmt_symbol(symbol: str) -> str:
        """将平台 symbol 格式（如 SH600519、SZ000001）转换为 QMT 格式（600519.SH、000001.SZ）。
        若已是 QMT 格式（含小数点）则原样返回。"""
        s = str(symbol or "").strip()
        if not s or "." in s:
            return s
        # 平台前缀格式：SH600519 → 600519.SH
        if s.upper().startswith("SH"):
            return f"{s[2:]}.SH"
        if s.upper().startswith("SZ"):
            return f"{s[2:]}.SZ"
        # 无前缀时按首字符推断
        return f"{s}.SH" if s.startswith("6") else f"{s}.SZ"

    def get_level1_price(self, symbol: str, side: str) -> float:
        """根据盘口五档获取最新能够吃单的委买/委卖价"""
        symbol = str(symbol or "").strip()
        side = str(side or "").strip().upper()
        if not self.enabled or getattr(self, "_xtdata", None) is None or not symbol:
            return 0.0
        try:
            qmt_symbol = self._to_qmt_symbol(symbol)
            ticks = self._xtdata.get_full_tick([qmt_symbol])
            tick = ticks.get(qmt_symbol) if ticks else None
            if not tick:
                return 0.0

            if side == "BUY":
                ask_prices = tick.get("askPrice", [])
                return float(ask_prices[0]) if ask_prices else 0.0
            elif side == "SELL":
                bid_prices = tick.get("bidPrice", [])
                return float(bid_prices[0]) if bid_prices else 0.0
        except Exception as exc:
            logger.warning("get_level1_price failed for %s: %s", symbol, exc)
        return 0.0

    @staticmethod
    def _normalize_protect_price_ratio(value: Any, default: float = 0.002) -> float:
        try:
            ratio = float(value)
        except Exception:
            ratio = default
        if not math.isfinite(ratio):
            ratio = default
        return max(0.0, min(0.05, ratio))

    def _resolve_effective_order_price(
        self,
        *,
        payload: dict[str, Any],
        symbol: str,
        side: str,
        order_type: str,
        price: float,
    ) -> tuple[str, float]:
        agent_price_mode = str(payload.get("agent_price_mode") or "").strip().lower()
        if agent_price_mode != "protect_limit":
            return order_type, price

        level1_price = self.get_level1_price(symbol, side)
        if level1_price <= 0:
            raise RuntimeError(f"QMT Agent 无法获取 {symbol} 的盘口价格，无法生成保护限价单")

        ratio = self._normalize_protect_price_ratio(
            payload.get("protect_price_ratio", payload.get("max_price_deviation", 0.002))
        )
        protected_price = level1_price * (1.0 + ratio) if side == "BUY" else level1_price * (1.0 - ratio)
        if protected_price <= 0:
            raise RuntimeError(f"QMT Agent 计算出的保护限价非法: symbol={symbol} side={side} price={protected_price}")

        logger.info(
            "QMT protect-limit pricing client_order_id=%s symbol=%s side=%s level1=%.6f ratio=%.4f protected=%.6f",
            str(payload.get("client_order_id") or "").strip(),
            symbol,
            side,
            level1_price,
            ratio,
            protected_price,
        )
        return "LIMIT", protected_price

    def _resolve_side(self, order_type: Any) -> str:
        side = ""
        if self._xtconstant:
            if order_type == self._xtconstant.STOCK_BUY:
                side = "BUY"
            elif order_type == self._xtconstant.STOCK_SELL:
                side = "SELL"
        else:
            if order_type == 23:
                side = "BUY"
            elif order_type == 24:
                side = "SELL"
        return side

    @staticmethod
    def _normalize_trade_action(value: Any) -> str:
        return str(value or "").strip().lower()

    def _resolve_operation_type(self, side: str, trade_action: str, xtconstant: Any) -> int:
        side = str(side or "").strip().upper()
        trade_action = self._normalize_trade_action(trade_action)
        if xtconstant is None:
            return 23 if side == "BUY" else 24

        def _pick_constant(names: list[str], fallback: int) -> int:
            for name in names:
                if hasattr(xtconstant, name):
                    try:
                        return int(getattr(xtconstant, name))
                    except Exception:
                        continue
            return fallback

        if trade_action == "sell_to_open":
            return _pick_constant(
                ["CREDIT_SLO_SELL", "CREDIT_SHORT_SELL", "SHORT_SELL", "STOCK_SELL"],
                int(getattr(xtconstant, "STOCK_SELL", 24)),
            )
        if trade_action == "buy_to_close":
            return _pick_constant(
                ["CREDIT_SLO_BUY", "CREDIT_BUY_REPAY", "BUY_REPAY", "STOCK_BUY"],
                int(getattr(xtconstant, "STOCK_BUY", 23)),
            )
        return int(getattr(xtconstant, "STOCK_BUY", 23)) if side == "BUY" else int(getattr(xtconstant, "STOCK_SELL", 24))

    def _extract_client_order_id(self, data: Any) -> str:
        # 仅信任 remark/sysid；不要回退到 order_id（常见 -1，会导致回报无法匹配）。
        # QMT 的 order_remark 字段有长度上限，可能截断 UUID，必须校验完整格式。
        for value in (getattr(data, "order_remark", ""), getattr(data, "order_sysid", "")):
            candidate = str(value or "").strip()
            if self.is_valid_client_order_id(candidate) and _UUID_RE.match(candidate):
                return candidate
        return ""

    @staticmethod
    def is_valid_client_order_id(value: Any) -> bool:
        candidate = str(value or "").strip().lower()
        return candidate not in _INVALID_CLIENT_ORDER_IDS

    @staticmethod
    def is_valid_exchange_order_id(value: Any) -> bool:
        candidate = str(value or "").strip().lower()
        return candidate not in _INVALID_EXCHANGE_ORDER_IDS

    def _remember_async_seq(self, seq: Any, client_order_id: str) -> None:
        cid = str(client_order_id or "").strip()
        if not cid:
            return
        try:
            seq_int = int(seq or 0)
        except Exception:
            return
        if seq_int <= 0:
            return
        with self._lock:
            self._async_seq_to_client_order[seq_int] = cid
            # 防止 dict 无上限增长，淘汰最早写入的条目
            while len(self._async_seq_to_client_order) > _ASYNC_SEQ_MAP_MAX_SIZE:
                self._async_seq_to_client_order.popitem(last=False)

    def _resolve_client_order_by_seq(self, seq: Any) -> str:
        try:
            seq_int = int(seq or 0)
        except Exception:
            return ""
        if seq_int <= 0:
            return ""
        with self._lock:
            return str(self._async_seq_to_client_order.get(seq_int) or "").strip()

    @staticmethod
    def _to_epoch_seconds(value: Any) -> float | None:
        try:
            raw = float(value)
        except Exception:
            return None
        if raw <= 0:
            return None
        # 柜台时间戳可能是毫秒，统一转秒
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        return raw

    def _event_ts(self, item: Any, fields: tuple[str, ...]) -> float | None:
        for field in fields:
            ts = self._to_epoch_seconds(getattr(item, field, None))
            if ts is not None:
                return ts
        return None

    def _apply_reconcile_window(
        self,
        items: list[Any],
        *,
        max_items: int,
        lookback_seconds: int,
        ts_fields: tuple[str, ...],
    ) -> list[Any]:
        now = time.time()
        cutoff = now - max(1, int(lookback_seconds or 0))
        retained: list[tuple[float, Any]] = []
        fallback: list[Any] = []
        for item in items:
            ts = self._event_ts(item, ts_fields)
            if ts is None:
                fallback.append(item)
                continue
            if ts >= cutoff:
                retained.append((ts, item))
        retained.sort(key=lambda x: x[0], reverse=True)
        selected = [item for _ts, item in retained[: max(1, int(max_items or 1))]]
        if selected:
            return selected
        # 时间字段缺失时，降级为取最近 N 条原始顺序末尾
        if fallback:
            return fallback[-max(1, int(max_items or 1)) :]
        return []

    @staticmethod
    def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _pick_float(obj: Any, keys: tuple[str, ...], default: float = 0.0) -> float:
        for key in keys:
            try:
                value = QMTClient._obj_get(obj, key, None)
                if value is None:
                    continue
                number = float(value)
                if number == number:
                    return number
            except Exception:
                continue
        return float(default)

    @staticmethod
    def _extract_numeric_fields(obj: Any) -> dict[str, float]:
        fields: dict[str, float] = {}
        if obj is None:
            return fields

        if isinstance(obj, dict):
            items = obj.items()
        else:
            names = [name for name in dir(obj) if name and not name.startswith("_")]
            items = []
            for name in names:
                try:
                    value = getattr(obj, name)
                except Exception:
                    continue
                if callable(value):
                    continue
                items.append((name, value))

        for key, raw in items:
            try:
                value = float(raw)
            except Exception:
                continue
            if not math.isfinite(value):
                continue
            fields[str(key).strip().lower()] = value
        return fields

    @staticmethod
    def _pick_by_patterns(
        fields: dict[str, float],
        *,
        must_include: tuple[str, ...],
        metric_tokens: tuple[str, ...],
    ) -> float:
        if not fields:
            return 0.0
        for key, value in fields.items():
            if value == 0.0:
                continue
            if not any(token in key for token in must_include):
                continue
            if any(token in key for token in metric_tokens):
                return float(value)
        return 0.0

    def _query_credit_snapshot(self, trader: Any, account: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "credit_enabled": False,
            "liabilities": 0.0,
            "short_market_value": 0.0,
            "credit_limit": 0.0,
            "maintenance_margin_ratio": 0.0,
        }
        query_method = getattr(trader, "query_credit_detail", None)
        if query_method is None:
            return result
        try:
            detail = query_method(account)
        except Exception as exc:
            logger.warning("query_credit_detail failed: %s", exc)
            return result
        result["credit_enabled"] = True

        liabilities = self._obj_get(detail, "liabilities")
        if liabilities in (None, ""):
            liabilities = self._obj_get(detail, "total_debt", 0.0)
        result["liabilities"] = float(liabilities or 0.0)

        short_mv = self._obj_get(detail, "short_market_value")
        if short_mv in (None, ""):
            short_mv = self._obj_get(detail, "fina_market_value", 0.0)
        result["short_market_value"] = float(short_mv or 0.0)

        credit_limit = self._obj_get(detail, "credit_limit")
        if credit_limit in (None, ""):
            credit_limit = self._obj_get(detail, "fina_limit", 0.0)
        result["credit_limit"] = float(credit_limit or 0.0)

        ratio = self._obj_get(detail, "maintenance_margin_ratio")
        if ratio in (None, ""):
            ratio = self._obj_get(detail, "assure_ratio", 0.0)
        result["maintenance_margin_ratio"] = float(ratio or 0.0)
        return result

    def _query_short_quota(self, trader: Any, account: Any, symbol: str) -> float | None:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return None
        now_ts = time.time()
        cached = self._short_quota_cache.get(symbol)
        ttl = max(1, int(self.cfg.short_check_cache_ttl_sec or 30))
        if cached and now_ts - float(cached.get("ts", 0)) <= ttl:
            return float(cached.get("quota", 0.0))

        method = getattr(trader, "query_credit_slo_code", None)
        if method is None:
            return None
        try:
            rows = method(account) or []
        except Exception as exc:
            logger.warning("query_credit_slo_code failed: %s", exc)
            return None
        self._last_short_check_at = now_ts
        self._shortable_symbols_count = len(rows) if isinstance(rows, list) else 0
        quota = 0.0
        matched = False
        for item in rows:
            code = str(
                self._obj_get(item, "stock_code")
                or self._obj_get(item, "code")
                or self._obj_get(item, "order_code")
                or ""
            ).strip().upper()
            if code != symbol:
                continue
            matched = True
            values = [
                self._obj_get(item, "available_amount"),
                self._obj_get(item, "enable_amount"),
                self._obj_get(item, "enableSloAmountT0"),
                self._obj_get(item, "enableSloAmountT3"),
                self._obj_get(item, "amount"),
                self._obj_get(item, "volume"),
            ]
            for raw in values:
                try:
                    quota = max(quota, float(raw or 0.0))
                except Exception:
                    continue
            break
        self._short_quota_cache[symbol] = {"ts": now_ts, "quota": quota, "matched": matched}
        return quota

    @staticmethod
    def _is_short_action(trade_action: str) -> bool:
        return trade_action in {"sell_to_open", "buy_to_close"}

    def _derive_trade_action(self, payload: dict[str, Any], side: str) -> str:
        explicit = self._normalize_trade_action(payload.get("trade_action"))
        if explicit:
            return explicit
        is_margin_trade = bool(payload.get("is_margin_trade", False))
        position_side = str(payload.get("position_side") or "").strip().lower()
        is_short = is_margin_trade or position_side == "short"
        if is_short:
            return "sell_to_open" if side == "SELL" else "buy_to_close"
        return "buy_to_open" if side == "BUY" else "sell_to_close"

    def _build_rejected_result(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        error_code: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": None,
            "exchange_trade_id": None,
            "account_id": self.cfg.account_id,
            "symbol": symbol,
            "side": side,
            "status": "REJECTED",
            "filled_quantity": 0.0,
            "filled_price": None,
            "error_code": error_code,
            "message": f"[{error_code}] {reason}",
        }

    def _validate_short_admission(
        self,
        *,
        trader: Any,
        account: Any,
        symbol: str,
        quantity: int,
    ) -> tuple[bool, str, str]:
        if not bool(self.cfg.enable_short_trading):
            return False, "LONG_SHORT_NOT_ENABLED", "enable_short_trading 未开启"
        if str(self.cfg.account_type or "").strip().upper() != "CREDIT":
            return False, "CREDIT_ACCOUNT_UNAVAILABLE", "account_type 非 CREDIT，禁止融券卖空"

        credit_snapshot = self._query_credit_snapshot(trader, account)
        if not credit_snapshot.get("credit_enabled"):
            return False, "CREDIT_ACCOUNT_UNAVAILABLE", "信用账户状态不可用"

        quota = self._query_short_quota(trader, account, symbol)
        if quota is None:
            return False, "CREDIT_ACCOUNT_UNAVAILABLE", "无法查询可融券额度"
        symbol_cache = self._short_quota_cache.get(str(symbol or "").strip().upper()) or {}
        if not bool(symbol_cache.get("matched", False)):
            return False, "SHORT_POOL_FORBIDDEN", f"{symbol} 不在融资融券股票池中"
        if float(quota) < float(quantity):
            return False, "SHORT_QUOTA_INSUFFICIENT", f"{symbol} 可融券额度不足: quota={quota}, required={quantity}"
        return True, "", ""

    def _on_stock_asset_update(self, asset: Any) -> None:
        with self._cache_lock:
            self._last_asset_cache = asset
        logger.debug("QMT stock_asset cache updated")
        if self._execution_callback:
            self._execution_callback({"type": "asset_updated", "account_id": self.cfg.account_id})

    def _on_stock_positions_update(self, positions: list[Any]) -> None:
        with self._cache_lock:
            self._last_positions_cache = positions or []
        logger.debug("QMT stock_positions cache updated (count=%s)", len(self._last_positions_cache))
        if self._execution_callback:
            self._execution_callback({"type": "positions_updated", "account_id": self.cfg.account_id})

    def get_cached_snapshot(self) -> tuple[Optional[Any], list[Any]]:
        with self._cache_lock:
            asset = self._last_asset_cache
            positions = list(self._last_positions_cache)

        # 如果缓存缺失，则尝试同步查询；若另一个线程正在查询，直接返回旧缓存，避免阻塞。
        if asset is None:
            fresh_asset, fresh_positions = self._query_snapshot_from_qmt(blocking=False)
            if fresh_asset is not None or fresh_positions:
                asset, positions = fresh_asset, fresh_positions

        return asset, positions

    def _query_snapshot_from_qmt(self, blocking: bool = False) -> tuple[Optional[Any], list[Any]]:
        """同步向 QMT 采样一次账户快照，并刷新本地缓存。"""
        if not self._query_lock.acquire(blocking=blocking):
            logger.warning("QMT API is busy, returning previous cache/empty to avoid blocking")
            return None, []

        try:
            with self._lock:
                trader = self._trader
                account = self._account
            if trader is None or account is None:
                raise RuntimeError("QMT not connected")

            logger.debug("Performing synchronous QMT snapshot query")
            asset = trader.query_stock_asset(account)
            positions = trader.query_stock_positions(account) or []
            with self._cache_lock:
                self._last_asset_cache = asset
                self._last_positions_cache = positions
            return asset, positions
        except Exception as e:
            logger.error("Synchronous QMT query failed: %s", e)
            return None, []
        finally:
            self._query_lock.release()

    def refresh_snapshot(self) -> tuple[Optional[Any], list[Any]]:
        """强制同步采样一次账户快照；失败时回退到当前缓存。"""
        asset, positions = self._query_snapshot_from_qmt(blocking=False)
        if asset is None and not positions:
            return self.get_cached_snapshot()
        return asset, positions

    def _build_callback(self) -> Any:
        try:
            from . import _callback
        except ImportError:
            import _callback
        return _callback.build_callback(self)
    def _remember_order_mapping(self, client_order_id: str, exchange_order_id: str) -> None:
        client_order_id = str(client_order_id or "").strip()
        exchange_order_id = str(exchange_order_id or "").strip()
        if not self.is_valid_client_order_id(client_order_id):
            return
        if not self.is_valid_exchange_order_id(exchange_order_id):
            return
        with self._lock:
            self._client_order_to_exchange[client_order_id] = exchange_order_id
            self._exchange_order_to_client[exchange_order_id] = client_order_id

    def resolve_client_order_id(
        self,
        client_order_id: Any = "",
        exchange_order_id: Any = "",
        seq: Any = None,
    ) -> str:
        candidate = str(client_order_id or "").strip()
        if self.is_valid_client_order_id(candidate):
            return candidate

        resolved_by_seq = self._resolve_client_order_by_seq(seq)
        if self.is_valid_client_order_id(resolved_by_seq):
            return resolved_by_seq

        ex_oid = str(exchange_order_id or "").strip()
        if self.is_valid_exchange_order_id(ex_oid):
            with self._lock:
                mapped = str(self._exchange_order_to_client.get(ex_oid) or "").strip()
            if self.is_valid_client_order_id(mapped):
                return mapped
        return ""

    def bind_exchange_order_id(self, client_order_id: Any, exchange_order_id: Any) -> None:
        self._remember_order_mapping(str(client_order_id or ""), str(exchange_order_id or ""))

    def connect(self) -> bool:
        if not self.enabled:
            return True
        if not self.cfg.qmt_path:
            logger.error("qmt_path is required when xtquant is enabled")
            return False

        session_id = int(self.cfg.session_id or int(time.time()))
        for attempt in range(3):
            trader = None
            try:
                trader = self._XtQuantTrader(self.cfg.qmt_path, session_id + attempt)
                callback = self._build_callback()
                trader.register_callback(callback)
                trader.start()
                result = trader.connect()
                if result != 0:
                    logger.warning("QMT connect failed, code=%s, attempt=%s", result, attempt + 1)
                    try:
                        trader.stop()
                    except Exception:
                        pass
                    time.sleep(2)
                    continue
                account_type = str(self.cfg.account_type or "STOCK").strip().upper()
                try:
                    account = self._StockAccount(self.cfg.account_id, account_type)
                except TypeError:
                    account = self._StockAccount(self.cfg.account_id)
                trader.subscribe(account)
                with self._lock:
                    self._trader = trader
                    self._account = account
                logger.info("QMT connected, account_id=%s", self.cfg.account_id)
                return True
            except Exception as exc:
                logger.warning("QMT connect exception, attempt=%s, error=%s", attempt + 1, exc)
                if trader is not None:
                    try:
                        trader.stop()
                    except Exception:
                        pass
                time.sleep(2)
        return False

    def reconnect_if_needed(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            triggered = self._reconnect_event.wait(timeout=1)
            if not triggered:
                continue
            self._reconnect_event.clear()
            if stop_event.is_set():
                break
            logger.info("starting QMT reconnect flow")
            self.close()
            while not stop_event.is_set():
                if self.connect():
                    logger.info("QMT reconnect success")
                    break
                time.sleep(max(1, self.cfg.reconnect_interval_seconds))

    def request_reconnect(self) -> None:
        self._reconnect_event.set()

    def is_connected(self) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            return self._trader is not None and self._account is not None

    def _safe_query_stk_compacts(self) -> list[dict[str, Any]]:
        try:
            return self.query_stk_compacts()
        except Exception as e:
            logger.debug(f"query_stk_compacts failed (probably not a credit account): {e}")
            return []

    def _safe_query_credit_subjects(self) -> list[dict[str, Any]]:
        try:
            return self.query_credit_subjects()
        except Exception as e:
            logger.debug(f"query_credit_subjects failed (probably not a credit account): {e}")
            return []

    def snapshot(self, *, prefer_fresh: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {
                "account_id": self.cfg.account_id,
                "broker": "qmt",
                "cash": 100000.0,
                "available_cash": 100000.0,
                "total_asset": 100000.0,
                "market_value": 0.0,
                "liabilities": 0.0,
                "short_market_value": 0.0,
                "credit_limit": 0.0,
                "maintenance_margin_ratio": 0.0,
                "credit_enabled": False,
                "shortable_symbols_count": 0,
                "last_short_check_at": self._last_short_check_at,
                "positions": [],
            }

        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            raise RuntimeError("QMT not connected")

        asset, positions = self.refresh_snapshot() if prefer_fresh else self.get_cached_snapshot()

        payload_positions: list[dict[str, Any]] = []
        market_value = 0.0
        floating_pnl = 0.0
        for pos in positions:
            pos_market_value = float(getattr(pos, "market_value", 0.0) or 0.0)
            market_value += pos_market_value
            volume = int(getattr(pos, "volume", 0) or 0)
            cost_price = self._pick_float(pos, ("cost_price", "avg_price", "open_price"), 0.0)
            last_price = self._pick_float(pos, ("last_price", "price", "current_price", "new_price"), 0.0)
            if last_price <= 0 and volume > 0 and pos_market_value > 0:
                last_price = pos_market_value / max(volume, 1)
            if cost_price <= 0 and last_price > 0:
                # 柜台未返回成本价时，回退为现价，避免前端将整仓市值误算为盈亏。
                cost_price = last_price
            if volume > 0 and cost_price > 0 and last_price > 0:
                floating_pnl += (last_price - cost_price) * volume
            payload_positions.append(
                {
                    "symbol": getattr(pos, "stock_code", ""),
                    "volume": volume,
                    "available_volume": int(getattr(pos, "can_use_volume", getattr(pos, "available_volume", 0)) or 0),
                    "cost_price": cost_price,
                    "last_price": last_price,
                    "market_value": pos_market_value,
                }
            )
        credit_snapshot = self._query_credit_snapshot(trader, account)
        asset_numeric = self._extract_numeric_fields(asset)
        today_pnl = self._pick_float(
            asset,
            (
                "today_pnl",
                "today_profit",
                "day_profit",
                "daily_profit",
                "day_pnl",
                "profit_today",
                "today_income",
                "close_profit",
                "floating_profit",
                "float_profit",
                "flt_profit",
            ),
            0.0,
        ) if asset else 0.0
        total_pnl = self._pick_float(
            asset,
            (
                "total_pnl",
                "total_profit",
                "acc_profit",
                "accumulate_profit",
                "accum_profit",
                "cumulative_profit",
                "cum_profit",
                "all_profit",
                "profit_total",
                "income_total",
            ),
            0.0,
        ) if asset else 0.0
        if today_pnl == 0.0 and asset_numeric:
            today_pnl = self._pick_by_patterns(
                asset_numeric,
                must_include=("today", "day", "daily"),
                metric_tokens=("pnl", "profit", "income", "盈亏"),
            )
        if total_pnl == 0.0 and asset_numeric:
            total_pnl = self._pick_by_patterns(
                asset_numeric,
                must_include=("total", "acc", "cum", "all"),
                metric_tokens=("pnl", "profit", "income", "盈亏"),
            )
        if floating_pnl == 0.0 and asset:
            floating_pnl = self._pick_float(
                asset,
                ("floating_pnl", "float_profit", "floating_profit", "unrealized_pnl"),
                0.0,
            )
        if total_pnl == 0.0 and abs(floating_pnl) > 1e-8:
            total_pnl = float(floating_pnl)
        if today_pnl == 0.0 and asset_numeric:
            logger.info(
                "today_pnl unresolved from QMT asset fields; sampled_keys=%s",
                sorted([k for k in asset_numeric.keys() if ("profit" in k or "pnl" in k or "income" in k)])[:20],
            )
        return {
            "account_id": self.cfg.account_id,
            "broker": "qmt",
            "cash": float(getattr(asset, "cash", 0.0) or 0.0) if asset else 0.0,
            "available_cash": float(
                getattr(asset, "available_cash", None)
                or getattr(asset, "usable_cash", None)
                or getattr(asset, "cash", 0.0)
                or 0.0
            ) if asset else 0.0,
            "total_asset": float(getattr(asset, "total_asset", 0.0) or 0.0) if asset else 0.0,
            "market_value": float(getattr(asset, "market_value", market_value) or market_value) if asset else market_value,
            "short_proceeds": float(getattr(asset, "frozen_margin", 0.0) or 0.0) if asset else 0.0,
            "frozen_cash": float(
                max(
                    float(getattr(asset, "frozen_cash", 0.0) or 0.0) if asset else 0.0,
                    max(
                        0.0,
                        ((getattr(asset, "total_asset", 0.0) or 0.0) if asset else 0.0)
                        - ((getattr(asset, "cash", 0.0) or 0.0) if asset else 0.0)
                        - (float(getattr(asset, "market_value", market_value) or market_value) if asset else market_value)
                    ),
                    max(
                        0.0,
                        ((getattr(asset, "cash", 0.0) or 0.0) if asset else 0.0)
                        - (
                            (
                                getattr(asset, "available_cash", None)
                                or getattr(asset, "usable_cash", None)
                                or getattr(asset, "cash", 0.0)
                                or 0.0
                            ) if asset else 0.0
                        )
                    )
                )
            ),
            "liabilities": float(credit_snapshot.get("liabilities", 0.0) or 0.0),
            "short_market_value": float(credit_snapshot.get("short_market_value", 0.0) or 0.0),
            "credit_limit": float(credit_snapshot.get("credit_limit", 0.0) or 0.0),
            "maintenance_margin_ratio": float(credit_snapshot.get("maintenance_margin_ratio", 0.0) or 0.0),
            "credit_enabled": bool(credit_snapshot.get("credit_enabled", False)),
            "shortable_symbols_count": int(self._shortable_symbols_count or 0),
            "last_short_check_at": self._last_short_check_at,
            "today_pnl": float(today_pnl or 0.0),
            "total_pnl": float(total_pnl or 0.0),
            "floating_pnl": float(floating_pnl or 0.0),
            "positions": payload_positions,
            "compacts": self._safe_query_stk_compacts(),
            "credit_subjects": self._safe_query_credit_subjects(),
            "debug_version": "2.3-pnl-mapping",
        }

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_order_id = str(payload.get("client_order_id") or "").strip()
        if not client_order_id:
            raise ValueError("missing client_order_id")
        symbol = str(payload.get("symbol") or "").strip()
        side = str(payload.get("side") or "").strip().upper()
        trade_action = self._derive_trade_action(payload, side)
        quantity = int(float(payload.get("quantity") or 0))
        price = float(payload.get("price") or 0.0)
        order_type = str(payload.get("order_type") or "LIMIT").strip().upper()

        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError("invalid order payload")

        order_type, price = self._resolve_effective_order_price(
            payload=payload,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
        )

        logger.info(
            "QMT submit_order request client_order_id=%s symbol=%s side=%s quantity=%s order_type=%s trade_action=%s",
            client_order_id,
            symbol,
            side,
            quantity,
            order_type,
            trade_action,
        )

        if not self.enabled:
            result = {
                "client_order_id": client_order_id,
                "exchange_order_id": client_order_id,
                "account_id": self.cfg.account_id,
                "symbol": symbol,
                "side": side,
                "status": "SUBMITTED",
                "filled_quantity": 0.0,
                "filled_price": None,
                "message": "accepted by mock qmt agent",
            }
            logger.info(
                "QMT submit_order mock result client_order_id=%s exchange_order_id=%s",
                client_order_id,
                client_order_id,
            )
            return result

        with self._lock:
            trader = self._trader
            account = self._account
            xtconstant = self._xtconstant
        if trader is None or account is None:
            raise RuntimeError("QMT not connected")

        if trade_action == "sell_to_open":
            passed, code, reason = self._validate_short_admission(
                trader=trader,
                account=account,
                symbol=symbol,
                quantity=quantity,
            )
            if not passed:
                return self._build_rejected_result(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    error_code=code,
                    reason=reason,
                )

        if xtconstant is not None:
            op_type = self._resolve_operation_type(side, trade_action, xtconstant)
            price_type = xtconstant.FIX_PRICE
        else:
            op_type = self._resolve_operation_type(side, trade_action, None)
            price_type = 0
        if order_type == "MARKET" or price <= 0:
            price_type = 1 if xtconstant is None else getattr(xtconstant, "LATEST_PRICE", price_type)
            price = 0.0

        exchange_order_id = trader.order_stock(
            account,
            self._to_qmt_symbol(symbol),
            op_type,
            quantity,
            price_type,
            price,
            "QuantMind_Agent",
            client_order_id,
        )
        if exchange_order_id in (-1, None, ""):
            raise RuntimeError("QMT rejected order")
        self._remember_order_mapping(client_order_id, str(exchange_order_id))

        result = {
            "client_order_id": client_order_id,
            "exchange_order_id": str(exchange_order_id),
            "exchange_trade_id": None,
            "account_id": self.cfg.account_id,
            "symbol": symbol,
            "side": side,
            "status": "SUBMITTED",
            "filled_quantity": 0.0,
            "filled_price": None,
            "message": "accepted by qmt",
        }
        logger.info(
            "QMT submit_order result client_order_id=%s exchange_order_id=%s status=%s",
            client_order_id,
            exchange_order_id,
            result.get("status"),
        )
        return result

    def submit_order_async(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_order_id = str(payload.get("client_order_id") or "").strip()
        if not client_order_id:
            raise ValueError("missing client_order_id")
        symbol = str(payload.get("symbol") or "").strip()
        side = str(payload.get("side") or "").strip().upper()
        trade_action = self._derive_trade_action(payload, side)
        quantity = int(float(payload.get("quantity") or 0))
        price = float(payload.get("price") or 0.0)
        order_type = str(payload.get("order_type") or "LIMIT").strip().upper()

        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError("invalid order payload")

        order_type, price = self._resolve_effective_order_price(
            payload=payload,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
        )

        logger.info(
            "QMT submit_order_async request client_order_id=%s symbol=%s side=%s quantity=%s order_type=%s trade_action=%s",
            client_order_id,
            symbol,
            side,
            quantity,
            order_type,
            trade_action,
        )

        if not self.enabled:
            result = {
                "client_order_id": client_order_id,
                "exchange_order_id": "",
                "exchange_trade_id": None,
                "account_id": self.cfg.account_id,
                "symbol": symbol,
                "side": side,
                "status": "SUBMITTED",
                "filled_quantity": 0.0,
                "filled_price": None,
                "message": "accepted by mock qmt agent (async)",
            }
            logger.info("QMT submit_order_async mock result client_order_id=%s", client_order_id)
            return result

        with self._lock:
            trader = self._trader
            account = self._account
            xtconstant = self._xtconstant
        if trader is None or account is None:
            raise RuntimeError("QMT not connected")

        if trade_action == "sell_to_open":
            passed, code, reason = self._validate_short_admission(
                trader=trader,
                account=account,
                symbol=symbol,
                quantity=quantity,
            )
            if not passed:
                return self._build_rejected_result(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    error_code=code,
                    reason=reason,
                )

        if xtconstant is not None:
            op_type = self._resolve_operation_type(side, trade_action, xtconstant)
            price_type = xtconstant.FIX_PRICE
        else:
            op_type = self._resolve_operation_type(side, trade_action, None)
            price_type = 0
        if order_type == "MARKET" or price <= 0:
            price_type = 1 if xtconstant is None else getattr(xtconstant, "LATEST_PRICE", price_type)
            price = 0.0

        seq = trader.order_stock_async(
            account,
            self._to_qmt_symbol(symbol),
            op_type,
            quantity,
            price_type,
            price,
            "QuantMind_Agent",
            client_order_id,
        )
        if int(seq or 0) <= 0:
            raise RuntimeError(f"QMT async order rejected, seq={seq}")
        self._remember_async_seq(seq, client_order_id)

        result = {
            "client_order_id": client_order_id,
            "exchange_order_id": "",
            "exchange_trade_id": None,
            "account_id": self.cfg.account_id,
            "symbol": symbol,
            "side": side,
            "status": "SUBMITTED",
            "filled_quantity": 0.0,
            "filled_price": None,
            "message": f"async order accepted by qmt, seq={seq}",
        }
        logger.info(
            "QMT submit_order_async result client_order_id=%s seq=%s status=%s",
            client_order_id,
            seq,
            result.get("status"),
        )
        return result

    def resolve_exchange_order_id(
        self,
        exchange_order_id: str,
        client_order_id: str = "",
    ) -> str:
        candidate = str(exchange_order_id or "").strip()
        if candidate:
            return candidate
        key = str(client_order_id or "").strip()
        if not key:
            return ""
        with self._lock:
            return str(self._client_order_to_exchange.get(key) or "").strip()

    def cancel_order(self, exchange_order_id: str) -> dict[str, Any]:
        """向 QMT 发出撤单请求，不等待回执（最终状态由回调异步上报）。"""
        exchange_order_id = str(exchange_order_id or "").strip()
        if not exchange_order_id:
            raise ValueError("missing exchange_order_id")

        if not self.enabled:
            logger.info("mock cancel_order exchange_order_id=%s", exchange_order_id)
            return {
                "accepted": True,
                "code": 0,
                "message": "accepted by mock qmt agent",
                "exchange_order_id": exchange_order_id,
            }

        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            raise RuntimeError("QMT not connected")

        try:
            order_id_int = int(exchange_order_id)
        except ValueError:
            raise ValueError(f"exchange_order_id must be numeric for QMT: {exchange_order_id!r}")

        result = trader.cancel_order_stock(account, order_id_int)
        if result != 0:
            logger.warning("QMT cancel_order returned code=%s for order_id=%s", result, exchange_order_id)
        else:
            logger.info("QMT cancel_order accepted exchange_order_id=%s", exchange_order_id)
        return {
            "accepted": result == 0,
            "code": int(result),
            "message": (
                "cancel request accepted by qmt"
                if result == 0
                else f"qmt cancel request rejected, code={result}"
            ),
            "exchange_order_id": exchange_order_id,
        }

    def cancel_order_async(self, exchange_order_id: str, client_order_id: str = "") -> dict[str, Any]:
        exchange_order_id = str(exchange_order_id or "").strip()
        if not exchange_order_id:
            raise ValueError("missing exchange_order_id")

        if not self.enabled:
            logger.info("mock cancel_order_async exchange_order_id=%s", exchange_order_id)
            return {
                "accepted": True,
                "seq": 1,
                "message": "accepted by mock qmt agent (async cancel)",
                "exchange_order_id": exchange_order_id,
            }

        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            raise RuntimeError("QMT not connected")

        try:
            order_id_int = int(exchange_order_id)
        except ValueError:
            raise ValueError(f"exchange_order_id must be numeric for QMT: {exchange_order_id!r}")

        seq = trader.cancel_order_stock_async(account, order_id_int)
        accepted = int(seq or 0) > 0
        if accepted:
            self._remember_async_seq(seq, client_order_id)
            logger.info(
                "QMT cancel_order_async accepted exchange_order_id=%s seq=%s",
                exchange_order_id,
                seq,
            )
        else:
            logger.warning("QMT cancel_order_async returned seq=%s for order_id=%s", seq, exchange_order_id)
        return {
            "accepted": accepted,
            "seq": int(seq or 0),
            "message": (
                f"async cancel request accepted by qmt, seq={seq}"
                if accepted
                else f"qmt async cancel request rejected, seq={seq}"
            ),
            "exchange_order_id": exchange_order_id,
        }

    def reconcile_recent_activity(self) -> list[dict[str, Any]]:
        """启动补偿：查询当前委托/成交并回写为执行事件。"""
        if not self.enabled:
            return []
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return []

        events: list[dict[str, Any]] = []
        dedup: set[tuple[str, str, str]] = set()

        lookback_seconds = int(self.cfg.reconcile_lookback_seconds or 86400)
        max_orders = int(self.cfg.reconcile_max_orders or 200)
        max_trades = int(self.cfg.reconcile_max_trades or 200)

        try:
            orders = trader.query_stock_orders(account, False) or []
        except TypeError:
            try:
                orders = trader.query_stock_orders(account) or []
            except Exception as exc:
                logger.warning("reconcile query_stock_orders failed: %s", exc)
                orders = []
        except Exception as exc:
            logger.warning("reconcile query_stock_orders failed: %s", exc)
            orders = []
        orders = self._apply_reconcile_window(
            list(orders),
            max_items=max_orders,
            lookback_seconds=lookback_seconds,
            ts_fields=("order_time", "submit_time", "update_time", "timestamp"),
        )

        for order in orders:
            try:
                client_order_id = self._extract_client_order_id(order)
                if not client_order_id:
                    continue
                exchange_order_id = str(getattr(order, "order_id", "") or "")
                self._remember_order_mapping(client_order_id, exchange_order_id)
                qmt_status_code = int(getattr(order, "order_status", 48) or 48)
                status = _QMT_ORDER_STATUS_MAP.get(qmt_status_code, "SUBMITTED")
                
                # [Optimization] 1分钟挂单不成交自动撤单规则 & 自动撤单对齐
                # 方案：如果订单处于活跃状态 (SUBMITTED) 且满足特定超时条件，则触发撤单
                is_zombie = hasattr(self, "_force_cancel_ids") and exchange_order_id in self._force_cancel_ids
                
                # 获取订单时间 (XTQuant order_time 通常是 HHMMSS 格式或时间戳)
                # HHMMSS 格式的最大值为 235959 < 240000，而秒级时间戳远大于此值
                # 若值落在 HHMMSS 范围内，无法可靠计算经过时间，跳过超时判断
                order_time_raw = getattr(order, "order_time", 0)
                is_timeout = False
                cancel_after_seconds = int(getattr(self.cfg, "reconcile_cancel_after_seconds", 300) or 300)
                if status == "SUBMITTED" and order_time_raw > 0:
                    if order_time_raw >= 240000:
                        # 秒级（或毫秒级）时间戳，可安全计算经过时间
                        ts = self._to_epoch_seconds(order_time_raw)
                        if ts is not None and (time.time() - ts) > cancel_after_seconds:
                            is_timeout = True
                    # HHMMSS 格式（order_time_raw < 240000）无法可靠判断超时，跳过

                if (is_zombie or is_timeout) and status == "SUBMITTED":
                    reason = "zombie" if is_zombie else f"timeout(>{cancel_after_seconds}s)"
                    logger.info("Auto-reconcile: cancelling order %s (%s) to release funds/rotate", exchange_order_id, reason)
                    self.cancel_order(exchange_order_id)
                    status = "CANCEL_PENDING"

                event = {
                    "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id or None,
                    "exchange_trade_id": None,
                    "account_id": self.cfg.account_id,
                    "symbol": getattr(order, "stock_code", ""),
                    "side": self._resolve_side(getattr(order, "order_type", 0)),
                    "status": status,
                    "filled_quantity": float(getattr(order, "traded_volume", 0) or 0),
                    "filled_price": float(getattr(order, "price", 0) or 0),
                    "message": f"startup reconcile order_status={qmt_status_code}",
                }
                key = (event["client_order_id"], event["status"], str(event["exchange_trade_id"] or ""))
                if key not in dedup:
                    dedup.add(key)
                    events.append(event)
            except Exception:
                logger.exception("failed to build reconcile order event")

        try:
            trades = trader.query_stock_trades(account) or []
        except Exception as exc:
            logger.warning("reconcile query_stock_trades failed: %s", exc)
            trades = []
        trades = self._apply_reconcile_window(
            list(trades),
            max_items=max_trades,
            lookback_seconds=lookback_seconds,
            ts_fields=("traded_time", "trade_time", "update_time", "timestamp"),
        )

        for trade in trades:
            try:
                client_order_id = self._extract_client_order_id(trade)
                if not client_order_id:
                    continue
                exchange_order_id = str(getattr(trade, "order_id", "") or "")
                exchange_trade_id = str(getattr(trade, "traded_id", "") or "")
                self._remember_order_mapping(client_order_id, exchange_order_id)
                event = {
                    "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id or None,
                    "exchange_trade_id": exchange_trade_id or None,
                    "account_id": self.cfg.account_id,
                    "symbol": getattr(trade, "stock_code", ""),
                    "side": self._resolve_side(getattr(trade, "order_type", 0)),
                    "status": "FILLED",
                    "filled_quantity": float(getattr(trade, "traded_volume", 0) or 0),
                    "filled_price": float(getattr(trade, "traded_price", 0) or 0),
                    "message": "startup reconcile trade",
                }
                key = (event["client_order_id"], event["status"], str(event["exchange_trade_id"] or ""))
                if key not in dedup:
                    dedup.add(key)
                    events.append(event)
            except Exception:
                logger.exception("failed to build reconcile trade event")

        return events

    def query_new_stock_list(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock:
            trader = self._trader
        if trader is None:
            return []
        try:
            stocks = trader.query_new_stock_list() or []
            return [
                {
                    "symbol": getattr(s, "stock_code", ""),
                    "name": getattr(s, "stock_name", ""),
                    "price": float(getattr(s, "price", 0.0) or 0.0),
                    "max_volume": int(getattr(s, "max_volume", 0) or 0),
                }
                for s in stocks
            ]
        except Exception as exc:
            logger.warning("query_new_stock_list failed: %s", exc)
            return []

    def query_ipo_quota(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return []
        try:
            quotas = trader.query_ipo_quota(account) or []
            return [
                {
                    "market": getattr(q, "market", ""),
                    "quota": int(getattr(q, "quota", 0) or 0),
                }
                for q in quotas
            ]
        except Exception as exc:
            logger.warning("query_ipo_quota failed: %s", exc)
            return []

    def credit_buy(self, symbol: str, quantity: int, price: float, order_type: int = 50) -> dict[str, Any]:
        if not self.enabled:
            return {"accepted": False, "message": "xtquant disabled"}
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return {"accepted": False, "message": "QMT not connected"}
        try:
            seq = trader.credit_buy(account, symbol, order_type, quantity, price, "QuantMind-CreditBuy", "")
            return {"accepted": seq > 0, "seq": seq}
        except Exception as exc:
            logger.exception("credit_buy failed")
            return {"accepted": False, "message": str(exc)}

    def credit_sell(self, symbol: str, quantity: int, price: float, order_type: int = 50) -> dict[str, Any]:
        if not self.enabled:
            return {"accepted": False, "message": "xtquant disabled"}
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return {"accepted": False, "message": "QMT not connected"}
        try:
            seq = trader.credit_sell(account, symbol, order_type, quantity, price, "QuantMind-CreditSell", "")
            return {"accepted": seq > 0, "seq": seq}
        except Exception as exc:
            logger.exception("credit_sell failed")
            return {"accepted": False, "message": str(exc)}

    def direct_repayment(self, amount: float) -> dict[str, Any]:
        if not self.enabled:
            return {"accepted": False, "message": "xtquant disabled"}
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return {"accepted": False, "message": "QMT not connected"}
        try:
            seq = trader.direct_repayment(account, amount, "QuantMind-Repay")
            return {"accepted": seq > 0, "seq": seq}
        except Exception as exc:
            logger.exception("direct_repayment failed")
            return {"accepted": False, "message": str(exc)}

    def transfer_fund(self, amount: float, direction: int) -> dict[str, Any]:
        """direction: 1 for bank-to-security, 2 for security-to-bank"""
        if not self.enabled:
            return {"accepted": False, "message": "xtquant disabled"}
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return {"accepted": False, "message": "QMT not connected"}
        try:
            seq = trader.transfer_fund(account, amount, 0, direction, "QuantMind-Transfer")
            return {"accepted": seq > 0, "seq": seq}
        except Exception as exc:
            logger.exception("transfer_fund failed")
            return {"accepted": False, "message": str(exc)}

    def query_stk_compacts(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return []
        try:
            compacts = trader.query_stk_compacts(account) or []
            return [
                {
                    "symbol": getattr(c, "stock_code", ""),
                    "compact_id": getattr(c, "compact_id", ""),
                    "compact_type": int(getattr(c, "compact_type", 0) or 0),
                    "open_date": getattr(c, "open_date", ""),
                    "business_volume": float(getattr(c, "business_volume", 0.0) or 0.0),
                    "business_amount": float(getattr(c, "business_amount", 0.0) or 0.0),
                    "real_compact_amount": float(getattr(c, "real_compact_amount", 0.0) or 0.0),
                    "ret_interest": float(getattr(c, "ret_interest", 0.0) or 0.0),
                    "ret_fee": float(getattr(c, "ret_fee", 0.0) or 0.0),
                }
                for c in compacts
            ]
        except Exception as exc:
            logger.warning("query_stk_compacts failed: %s", exc)
            return []

    def query_credit_subjects(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock:
            trader = self._trader
            account = self._account
        if trader is None or account is None:
            return []
        try:
            subjects = trader.query_credit_subjects(account) or []
            return [
                {
                    "symbol": getattr(s, "stock_code", ""),
                    "subject_type": int(getattr(s, "subject_type", 0) or 0),
                    "margin_rate": float(getattr(s, "margin_rate", 0.0) or 0.0),
                }
                for s in subjects
            ]
        except Exception as exc:
            logger.warning("query_credit_subjects failed: %s", exc)
            return []

    def close(self) -> None:
        with self._lock:
            trader = self._trader
            self._trader = None
            self._account = None
        if trader is not None:
            try:
                trader.stop()
            except Exception:
                pass
