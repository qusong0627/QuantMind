"""QMT 执行端 RPC 客户端（大 QMT 内置 Python 桥）。

QuantMind → 大 QMT 的真实下单/查询通道。大 QMT 那台 Windows 机器的内置
Python 里常驻 `xtquant-big-convert` 的 RPC 服务端（入口 ``BIGQMT_REDIS_DRYRUN.py``），
本模块作为客户端经 Redis 传输调用其白名单方法（无需 XtQuantServer 权限）。

设计要点
--------
* RPC 调用是**同步阻塞**的（redis brpop 等响应），对外只暴露 async 方法，
  内部一律 :func:`asyncio.to_thread` 包装，避免阻塞事件循环。
  注意：``wait_for`` 超时只会放弃等待，线程仍会跑完 —— 因此超时**绝不重试**，
  必须先用 ``query_orders`` 确认委托是否已进系统。
* 下单/撤单受服务端 ``rpc_allow_order_methods`` 开关保护，未开启时抛
  :class:`QmtExecError` (``code="ORDER_DISABLED"``)。
* 委托备注（``order_remark``）承载 client_order_id 的反查键。QMT 备注长度有限，
  故备注为定长摘要 ``qm<sha1_16>``，明文的 client_order_id ↔ 备注映射写入
  Redis（TTL 7 天），轮询回报时据此还原。
* 无成交推送：成交只能靠 ``query_orders`` / ``query_trades`` 轮询。

环境变量（``QMT_EXEC_*`` 优先，兼容 big-convert 原生 ``BIGQMT_*``）
------------------------------------------------------------------
===========================  ==================================================
``QMT_EXEC_ENABLED``         是否启用（默认 false）
``QMT_EXEC_ACCOUNT_ID``      资金账号（必填，未配置则客户端禁用）
``QMT_EXEC_ACCOUNT_TYPE``    账号类型，默认 ``STOCK``
``QMT_EXEC_REDIS_HOST``      桥的 Redis 地址（留空则回落到 trade Redis 配置）
``QMT_EXEC_REDIS_PORT``      桥的 Redis 端口，默认 6380
``QMT_EXEC_REDIS_DB``        桥的 Redis 库号，默认 0
``QMT_EXEC_REDIS_PASSWORD``  桥的 Redis 密码
``QMT_EXEC_TIMEOUT``         单次 RPC 超时秒数，默认 10
``QMT_EXEC_STRATEGY_NAME``   下单策略名，默认 ``quantmind``
===========================  ==================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from typing import Any, Optional, Protocol
from collections.abc import Callable
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 备注前缀 + 摘要长度：``qm`` + sha1[:16] = 18 字符，远小于 QMT 备注上限
_REMARK_PREFIX = "qm"
_REMARK_DIGEST_LEN = 16
_REMARK_MAP_PREFIX = "quantmind:qmt_exec:remark:"
_CLIENT_ID_MAP_PREFIX = "quantmind:qmt_exec:cid:"
_REMARK_TTL_SECONDS = 7 * 24 * 3600

# QMT 委托状态码 → 本系统 OrderStatus 字符串（口径见 xtconstant）
# 51/52 是「撤单请求已发出但未确认」，委托仍可能成交，不能当终态；
# 55 是部分成交（旧映射误标为废单，会漏记成交）。
QMT_STATUS_MAP: dict[int, str] = {
    48: "PENDING",  # ORDER_UNREPORTED 未申报
    49: "SUBMITTED",  # ORDER_WAIT_REPORTING 等待申报
    50: "SUBMITTED",  # ORDER_REPORTED 已申报
    51: "SUBMITTED",  # ORDER_REPORTED_CANCEL 报撤中（未确认，可能仍成交）
    52: "PARTIALLY_FILLED",  # ORDER_PARTSUCC_CANCEL 部成待撤
    53: "CANCELLED",  # ORDER_PART_CANCEL 部撤
    54: "CANCELLED",  # ORDER_CANCELED 已撤
    55: "PARTIALLY_FILLED",  # ORDER_PART_SUCC 部分成交
    56: "FILLED",  # ORDER_SUCCEEDED 全部成交
    57: "REJECTED",  # ORDER_JUNK 废单
    255: "SUBMITTED",  # ORDER_UNKNOWN 未知（不误判为终态）
}

# 服务端返回的错误串 → 错误码（用于上层决定「可重试 / 需人工复核」）
_ERROR_CODE_HINTS: tuple[tuple[str, str], ...] = (
    ("order_disabled", "ORDER_DISABLED"),
    ("rpc_allow_order_methods", "ORDER_DISABLED"),
    ("timeout", "TIMEOUT"),
    ("超时", "TIMEOUT"),
    ("not connected", "NOT_CONNECTED"),
    ("未连接", "NOT_CONNECTED"),
    ("connection", "NOT_CONNECTED"),
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# 页面配置键（trade Redis，由 trade 服务 broker_config 路由写入）
BROKER_CONFIG_KEY = "broker:config:qmt_exec"
_SETTINGS_FIELDS = (
    "enabled",
    "account_id",
    "account_type",
    "strategy_name",
    "timeout",
    "redis_host",
    "redis_port",
    "redis_db",
    "redis_password",
)

_settings_redis: Any = None


def load_broker_settings() -> dict[str, Any]:
    """读取页面配置 ``broker:config:qmt_exec``（trade Redis）。失败返回空字典。"""
    global _settings_redis
    try:
        import json

        from backend.services.trade_shared.redis_client import RedisClient

        if _settings_redis is None or _settings_redis.client is None:
            client = RedisClient()
            client.connect()
            _settings_redis = client
        raw = _settings_redis.client.get(BROKER_CONFIG_KEY)
        if not raw:
            return {}
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="ignore")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return {k: data.get(k) for k in _SETTINGS_FIELDS if data.get(k) is not None}
    except Exception as exc:  # noqa: BLE001 - 配置读取失败按环境变量走
        logger.debug("[QmtExec] 读取 %s 失败: %s", BROKER_CONFIG_KEY, exc)
        return {}


class QmtExecError(Exception):
    """QMT 执行端调用失败。``code`` 供上层做重试/复核决策。"""

    def __init__(self, message: str, code: str = "RPC_ERROR"):
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - 直通基类
        return str(self.args[0]) if self.args else self.code


def _classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    for needle, code in _ERROR_CODE_HINTS:
        if needle.lower() in text:
            return code
    if isinstance(exc, PermissionError):
        return "ORDER_DISABLED"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "IMPORT_FAIL"
    return "RPC_ERROR"


def build_remark(client_order_id: str) -> str:
    """client_order_id → QMT 委托备注（定长、确定、可反查）。"""
    digest = hashlib.sha1(str(client_order_id).encode("utf-8")).hexdigest()
    return f"{_REMARK_PREFIX}{digest[:_REMARK_DIGEST_LEN]}"


def is_qmt_exec_remark(remark: Any) -> bool:
    """备注是否由本系统写入（``qm`` 前缀）——用于轮询时筛掉账户里的手工单。"""
    return str(remark or "").strip().startswith(_REMARK_PREFIX)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """按顺序取第一个非 None 属性（兼容不同版本的字段命名）。"""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _side_from_order_type(order_type: Any, direction: Any = None) -> str:
    """QMT 委托类型 23=买 24=卖；兜底看 direction 字段。"""
    code = _to_int(order_type, -1)
    if code == 23:
        return "BUY"
    if code == 24:
        return "SELL"
    direction_code = _to_int(direction, -1)
    if direction_code == 48:  # xtconstant.DIRECTION_BUY
        return "BUY"
    if direction_code == 49:  # xtconstant.DIRECTION_SELL
        return "SELL"
    return ""


def _to_qmt_symbol(symbol: str) -> str:
    """内部代码 → QMT 口径（后缀式，如 ``600519.SH``）。"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    try:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = StockCodeUtil.to_suffix(raw)
        if suffix:
            return suffix
    except Exception:  # noqa: BLE001 - 兜底不阻断下单
        pass
    if raw.startswith(("6", "9")):
        return f"{raw}.SH"
    if raw.startswith(("0", "3", "2")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    return raw


def _to_prefix_symbol(qmt_symbol: str) -> str:
    """QMT 口径 → 内部前缀式（``600519.SH`` → ``SH600519``）。"""
    raw = str(qmt_symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        code, _, market = raw.partition(".")
        return f"{market}{code}" if market else code
    return raw


class QmtExecBackend(Protocol):
    """big-convert 桥的最小能力面（便于单测注入假实现）。"""

    def ping(self) -> dict[str, Any]: ...

    def get_asset(self) -> dict[str, Any]: ...

    def get_positions(self) -> list[dict[str, Any]]: ...

    def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]: ...

    def query_trades(self) -> list[dict[str, Any]]: ...

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        price: float,
        remark: str,
        strategy_name: str,
    ) -> dict[str, Any]: ...

    def cancel_order(self, *, order_id: str, symbol: str) -> dict[str, Any]: ...


class BigConvertBackend:
    """基于 ``xtquant-big-convert`` 兼容层的真实实现（懒加载 + 单例）。

    连接参数**显式传参**给 ``configure(account_id=..., redis_config=...)``，
    不写 ``BIGQMT_*`` 环境变量 —— 避免污染进程环境，也避免多实例串号。
    """

    def __init__(
        self,
        account_id: str,
        account_type: str = "STOCK",
        redis_env: dict[str, str] | None = None,
        timeout: float | None = None,
    ):
        self.account_id = str(account_id or "").strip()
        self.account_type = str(account_type or "STOCK").strip().upper()
        self.redis_env = dict(redis_env or {})
        self.timeout = float(timeout) if timeout else None
        self._trader: Any = None
        self._account: Any = None
        self._constants: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _redis_config(self) -> dict[str, Any]:
        """页面/环境配置 → big-convert ``redis_config`` 字段名。"""
        mapping = {
            "redis_host": "host",
            "redis_port": "port",
            "redis_db": "db",
            "redis_username": "username",
            "redis_password": "password",
        }
        config: dict[str, Any] = {}
        for key, name in mapping.items():
            value = str(self.redis_env.get(key) or "").strip()
            if not value:
                continue
            config[name] = int(value) if name in ("port", "db") else value
        return config

    # -- 初始化 ---------------------------------------------------------
    def _ensure(self) -> tuple[Any, Any]:
        """线程内初始化（在 to_thread 中调用），加锁避免重复 configure。"""
        if self._trader is not None and self._account is not None:
            return self._trader, self._account
        with self._lock:
            if self._trader is not None and self._account is not None:
                return self._trader, self._account
            try:
                from bigqmt_signal_trader.xtquant_compat import (
                    FIX_PRICE,
                    LATEST_PRICE,
                    STOCK_BUY,
                    STOCK_SELL,
                    StockAccount,
                    configure,
                    xt_trader,
                )
            except ImportError as exc:  # pragma: no cover - 依赖未装
                raise QmtExecError(
                    '未安装 xtquant-big-convert（pip install "xtquant-big-convert[redis]"）',
                    code="IMPORT_FAIL",
                ) from exc
            try:
                configure(
                    account_id=self.account_id,
                    redis_config=self._redis_config(),
                    timeout_seconds=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                raise QmtExecError(
                    f"big-convert 配置失败：{exc}（检查资金账号与桥 Redis 地址/密码）",
                    code="CONFIG_FAIL",
                ) from exc
            self._constants = {
                "STOCK_BUY": STOCK_BUY,
                "STOCK_SELL": STOCK_SELL,
                "FIX_PRICE": FIX_PRICE,
                "LATEST_PRICE": LATEST_PRICE,
            }
            self._trader = xt_trader
            self._account = StockAccount(self.account_id, self.account_type)
            logger.info(
                "[QmtExec] big-convert 客户端就绪 account=%s type=%s",
                self.account_id,
                self.account_type,
            )
            return self._trader, self._account

    # -- 查询 -----------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        trader, _ = self._ensure()
        result = trader.client.call("ping", {})
        return {"ok": True, "result": result, "account_id": self.account_id}

    def get_asset(self) -> dict[str, Any]:
        trader, account = self._ensure()
        asset = trader.query_stock_asset(account)
        if asset is None:
            raise QmtExecError("query_stock_asset 返回空", code="EMPTY_ASSET")
        return {
            "account_id": str(
                _first_attr(asset, "account_id", default=self.account_id)
            ),
            "cash": _to_float(_first_attr(asset, "cash")),
            "frozen_cash": _to_float(_first_attr(asset, "frozen_cash")),
            "market_value": _to_float(_first_attr(asset, "market_value")),
            "total_asset": _to_float(_first_attr(asset, "total_asset")),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        trader, account = self._ensure()
        positions = trader.query_stock_positions(account) or []
        out: list[dict[str, Any]] = []
        for pos in positions:
            stock_code = str(
                _first_attr(pos, "stock_code", "instrument_id", default="")
            )
            volume = _to_float(_first_attr(pos, "volume", "total_volume"))
            if not stock_code or volume <= 0:
                continue
            out.append(
                {
                    "stock_code": stock_code,
                    "symbol": _to_prefix_symbol(stock_code),
                    "instrument_name": str(
                        _first_attr(pos, "stock_name", "instrument_name", default="")
                        or ""
                    ),
                    "volume": volume,
                    "can_use_volume": _to_float(_first_attr(pos, "can_use_volume")),
                    "open_price": _to_float(_first_attr(pos, "open_price")),
                    "avg_price": _to_float(_first_attr(pos, "avg_price", "open_price")),
                    "market_value": _to_float(_first_attr(pos, "market_value")),
                }
            )
        return out

    def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        trader, account = self._ensure()
        orders = (
            trader.query_stock_orders(account, cancelable_only=cancelable_only) or []
        )
        return [self._normalize_order(item) for item in orders]

    def query_trades(self) -> list[dict[str, Any]]:
        trader, account = self._ensure()
        trades = trader.query_stock_trades(account) or []
        return [self._normalize_trade(item) for item in trades]

    @staticmethod
    def _normalize_order(item: Any) -> dict[str, Any]:
        stock_code = str(_first_attr(item, "stock_code", default=""))
        status_code = _to_int(_first_attr(item, "order_status", "status"), -1)
        return {
            "order_id": str(_first_attr(item, "order_id", default="")),
            "order_sysid": str(_first_attr(item, "order_sysid", default="")),
            "stock_code": stock_code,
            "symbol": _to_prefix_symbol(stock_code),
            "instrument_name": str(
                _first_attr(item, "instrument_name", default="") or ""
            ),
            "side": _side_from_order_type(
                _first_attr(item, "order_type"), _first_attr(item, "direction")
            ),
            "order_volume": _to_float(_first_attr(item, "order_volume")),
            "traded_volume": _to_float(_first_attr(item, "traded_volume")),
            "price": _to_float(_first_attr(item, "price")),
            "traded_price": _to_float(_first_attr(item, "traded_price")),
            "status_code": status_code,
            "status": QMT_STATUS_MAP.get(status_code, "SUBMITTED"),
            "order_remark": str(_first_attr(item, "order_remark", default="") or ""),
            "status_msg": str(_first_attr(item, "status_msg", default="") or ""),
            "strategy_name": str(_first_attr(item, "strategy_name", default="") or ""),
        }

    @staticmethod
    def _normalize_trade(item: Any) -> dict[str, Any]:
        stock_code = str(_first_attr(item, "stock_code", default=""))
        volume = _to_float(_first_attr(item, "traded_volume", "volume"))
        price = _to_float(_first_attr(item, "traded_price", "price"))
        amount = _to_float(_first_attr(item, "traded_amount"), volume * price)
        return {
            "trade_id": str(
                _first_attr(item, "trade_id", "traded_id", "order_sysid", default="")
            ),
            "order_sysid": str(_first_attr(item, "order_sysid", default="")),
            "order_id": str(_first_attr(item, "order_id", default="")),
            "stock_code": stock_code,
            "symbol": _to_prefix_symbol(stock_code),
            "instrument_name": str(
                _first_attr(item, "instrument_name", default="") or ""
            ),
            "side": _side_from_order_type(
                _first_attr(item, "order_type"), _first_attr(item, "direction")
            ),
            "traded_volume": volume,
            "traded_price": price,
            "traded_amount": amount,
            "traded_at": str(
                _first_attr(item, "traded_time", "traded_at", default="") or ""
            ),
            "order_remark": str(_first_attr(item, "order_remark", default="") or ""),
            "commission": _to_float(_first_attr(item, "commission")),
        }

    # -- 交易 -----------------------------------------------------------
    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        price: float,
        remark: str,
        strategy_name: str,
    ) -> dict[str, Any]:
        trader, account = self._ensure()
        constants = self._constants
        is_buy = str(side).upper() == "BUY"
        qmt_order_type = constants["STOCK_BUY"] if is_buy else constants["STOCK_SELL"]
        if str(order_type).upper() == "MARKET":
            price_type = constants["LATEST_PRICE"]
            price_value = 0.0
        else:
            price_type = constants["FIX_PRICE"]
            price_value = float(price or 0)
            if price_value <= 0:
                raise QmtExecError("限价单必须提供价格", code="INVALID_PRICE")
        order_id = trader.order_stock(
            account,
            symbol,
            qmt_order_type,
            int(quantity),
            price_type,
            price_value,
            strategy_name,
            remark,
        )
        if order_id is None or _to_int(order_id, -1) == -1:
            raise QmtExecError(
                f"下单被拒（order_stock 返回 {order_id}），检查账户权限/价格范围/QMT 风控",
                code="ORDER_REJECTED",
            )
        return {
            "order_id": str(order_id),
            "symbol": symbol,
            "side": "BUY" if is_buy else "SELL",
            "quantity": int(quantity),
            "price": price_value,
            "remark": remark,
        }

    def cancel_order(self, *, order_id: str, symbol: str) -> dict[str, Any]:
        trader, account = self._ensure()
        market = ""
        if "." in str(symbol or ""):
            market = str(symbol).split(".")[-1].upper()
        try:
            rc = trader.cancel_order_stock_sysid(account, market, order_id)
        except TypeError:
            # 兼容没有 market 参数的老签名
            rc = trader.cancel_order_stock_sysid(account, order_id)
        # MiniQMT 契约：0=成功，其余=失败
        if _to_int(rc, -1) != 0:
            raise QmtExecError(f"撤单被拒（返回 {rc}）", code="CANCEL_REJECTED")
        return {"success": True, "order_id": order_id}


class QmtExecClient:
    """QMT 执行端客户端：async 门面 + 备注映射 + 超时/错误码归一。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        account_id: str = "",
        account_type: str = "STOCK",
        timeout: float = 10.0,
        strategy_name: str = "quantmind",
        backend: QmtExecBackend | None = None,
        redis_url: str = "",
        bridge_redis: dict[str, str] | None = None,
        settings_loader: Callable[[], dict[str, Any]] | None = None,
    ):
        self.enabled = bool(enabled)
        self.account_id = str(account_id or "").strip()
        self.account_type = str(account_type or "STOCK").strip().upper()
        self.timeout = float(timeout or 10.0)
        self.strategy_name = str(strategy_name or "quantmind").strip() or "quantmind"
        self._backend = backend
        self._backend_key = ""
        self._bridge_redis = dict(bridge_redis or {})
        self._redis: Any = None
        self._redis_url = redis_url
        self._redis_lock = asyncio.Lock()
        # 页面配置（trade Redis broker:config:qmt_exec）覆盖环境变量，
        # 由 refresh_settings() 异步刷新（常驻任务每轮调用一次）
        self._settings_loader = (
            settings_loader if settings_loader is not None else load_broker_settings
        )
        self._page_settings: dict[str, Any] | None = None

    # -- 配置 -----------------------------------------------------------
    @classmethod
    def from_env(cls) -> QmtExecClient:
        def _env(*names: str, default: str = "") -> str:
            for name in names:
                value = os.getenv(name)
                if value is not None and str(value).strip() != "":
                    return str(value).strip()
            return default

        account_id = _env("QMT_EXEC_ACCOUNT_ID", "BIGQMT_ACCOUNT_ID")
        enabled_raw = _env("QMT_EXEC_ENABLED", default="false").lower()
        enabled = enabled_raw in _TRUE_VALUES
        host = _env("QMT_EXEC_REDIS_HOST", "BIGQMT_REDIS_HOST")
        port = _env("QMT_EXEC_REDIS_PORT", "BIGQMT_REDIS_PORT", default="6380")
        db = _env("QMT_EXEC_REDIS_DB", "BIGQMT_REDIS_DB", default="0")
        password = _env("QMT_EXEC_REDIS_PASSWORD", "BIGQMT_REDIS_PASSWORD")
        if not host:
            # 未单独部署桥 Redis 时回落 trade Redis（本机联调最省事）
            host = _env("REDIS_HOST", default="localhost")
            port = _env("REDIS_PORT", default="6379")
            password = _env("REDIS_PASSWORD")
        return cls(
            enabled=enabled,
            account_id=account_id,
            account_type=_env("QMT_EXEC_ACCOUNT_TYPE", default="STOCK"),
            timeout=float(_env("QMT_EXEC_TIMEOUT", default="10")),
            strategy_name=_env("QMT_EXEC_STRATEGY_NAME", default="quantmind"),
            bridge_redis={
                "redis_host": host,
                "redis_port": port,
                "redis_db": db,
                "redis_password": password,
            },
        )

    @property
    def configured(self) -> bool:
        cfg = self._effective()
        return bool(cfg["enabled"] and cfg["account_id"])

    @property
    def bridge_redis(self) -> dict[str, str]:
        """桥 Redis 连接参数（环境变量基线；页面配置在 :meth:`_effective` 中覆盖）。"""
        return dict(self._bridge_redis)

    def effective_config(self) -> dict[str, Any]:
        """生效配置（环境变量 + 页面覆盖），供轮询/对账读取 strategy_name 等。"""
        return self._effective()

    def _effective(self) -> dict[str, Any]:
        """环境变量基线 + 页面配置覆盖（页面配置由 :meth:`refresh_settings` 异步刷新）。"""
        base: dict[str, Any] = {
            "enabled": self.enabled,
            "account_id": self.account_id,
            "account_type": self.account_type,
            "timeout": self.timeout,
            "strategy_name": self.strategy_name,
            "redis_host": self._bridge_redis.get("redis_host", ""),
            "redis_port": self._bridge_redis.get("redis_port", ""),
            "redis_db": self._bridge_redis.get("redis_db", ""),
            "redis_password": self._bridge_redis.get("redis_password", ""),
        }
        override = self._page_settings or {}
        for key in (
            "account_id",
            "strategy_name",
            "redis_host",
            "redis_port",
            "redis_db",
        ):
            value = str(override.get(key) or "").strip()
            if value:
                base[key] = value
        account_type = str(override.get("account_type") or "").strip()
        if account_type:
            base["account_type"] = account_type.upper()
        # 密码单独处理：空串视为「未改动」，保留环境变量值
        password = str(override.get("redis_password") or "")
        if password.strip():
            base["redis_password"] = password
        try:
            timeout = float(override.get("timeout") or 0)
            if timeout > 0:
                base["timeout"] = timeout
        except (TypeError, ValueError):
            pass
        raw_enabled = str(override.get("enabled") or "").strip().lower()
        if raw_enabled in _TRUE_VALUES:
            base["enabled"] = True
        elif raw_enabled in _FALSE_VALUES:
            base["enabled"] = False
        return base

    @staticmethod
    def _build_redis_url(cfg: dict[str, Any], fallback: str = "") -> str:
        host = str(cfg.get("redis_host") or "").strip()
        if not host:
            return fallback
        port = str(cfg.get("redis_port") or "6379").strip() or "6379"
        db = str(cfg.get("redis_db") or "0").strip() or "0"
        password = str(cfg.get("redis_password") or "")
        if password:
            # 密码可能含 @ : / # 等 URL 保留字符，不编码会把主机解析错。
            return f"redis://:{quote(password, safe='')}@{host}:{port}/{db}"
        return f"redis://{host}:{port}/{db}"

    async def refresh_settings(self) -> dict[str, Any]:
        """从 trade Redis 重读页面配置（``broker:config:qmt_exec``）。

        常驻任务每轮调用一次即可（异步、带异常兜底）；失败时保留上一份配置。
        """
        if self._settings_loader is None:
            return self._effective()
        try:
            loaded = await asyncio.to_thread(self._settings_loader)
            if isinstance(loaded, dict):
                self._page_settings = loaded
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断交易
            logger.warning("[QmtExec] 读取页面配置失败，沿用上一份: %s", exc)
        return self._effective()

    def invalidate_settings(self) -> None:
        """页面改配置后立即清空缓存（下一次 refresh_settings 生效）。"""
        self._page_settings = None

    # -- 内部 -----------------------------------------------------------
    def _get_backend(self, cfg: dict[str, Any]) -> QmtExecBackend:
        key = "|".join(
            str(cfg.get(name) or "")
            for name in (
                "account_id",
                "account_type",
                "redis_host",
                "redis_port",
                "redis_db",
                "redis_password",
            )
        )
        if self._backend is None or self._backend_key != key:
            self._backend = BigConvertBackend(
                str(cfg.get("account_id") or ""),
                str(cfg.get("account_type") or "STOCK"),
                redis_env={
                    "redis_host": str(cfg.get("redis_host") or ""),
                    "redis_port": str(cfg.get("redis_port") or ""),
                    "redis_db": str(cfg.get("redis_db") or ""),
                    "redis_password": str(cfg.get("redis_password") or ""),
                },
                timeout=float(cfg.get("timeout") or 0) or None,
            )
            self._backend_key = key
        return self._backend

    async def _get_redis(self) -> Any:
        """备注映射用的 Redis（与桥同一个实例，便于运维只开一个口子）。"""
        url = self._build_redis_url(self._effective(), fallback=self._redis_url)
        if not url:
            raise QmtExecError(
                "桥 Redis 未配置（QMT_EXEC_REDIS_HOST 或页面「桥 Redis 地址」）",
                code="NOT_CONFIGURED",
            )
        if self._redis is not None and url == self._redis_url:
            return self._redis
        async with self._redis_lock:
            if self._redis is not None and url == self._redis_url:
                return self._redis
            if self._redis is not None:
                try:
                    await self._redis.aclose()
                except Exception:  # noqa: BLE001
                    pass
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(url, decode_responses=True)
            self._redis_url = url
        return self._redis

    async def _call(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """同步 RPC → 线程 + 超时。超时**不可重试**（线程仍在跑）。"""
        limit = float(timeout or self._effective()["timeout"])
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=limit,
            )
        except asyncio.TimeoutError as exc:
            raise QmtExecError(
                f"QMT RPC 超时（{limit}s）——委托可能已提交，"
                "必须先用 query_orders 确认，禁止盲目重试",
                code="TIMEOUT",
            ) from exc
        except QmtExecError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QmtExecError(str(exc), code=_classify_error(exc)) from exc

    def _require_enabled(self) -> dict[str, Any]:
        cfg = self._effective()
        if not cfg["enabled"]:
            raise QmtExecError(
                "QMT 执行端未启用（QMT_EXEC_ENABLED=false 或页面开关关闭）",
                code="DISABLED",
            )
        if not cfg["account_id"]:
            raise QmtExecError("QMT_EXEC_ACCOUNT_ID 未配置", code="NOT_CONFIGURED")
        return cfg

    async def _remember_remark(self, remark: str, client_order_id: str) -> None:
        if not remark or not client_order_id:
            return
        try:
            redis = await self._get_redis()
            await redis.set(
                f"{_REMARK_MAP_PREFIX}{remark}", client_order_id, ex=_REMARK_TTL_SECONDS
            )
            await redis.set(
                f"{_CLIENT_ID_MAP_PREFIX}{client_order_id}",
                remark,
                ex=_REMARK_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 映射丢失只降级为兜底匹配
            logger.warning("[QmtExec] 备注映射写入失败 remark=%s: %s", remark, exc)

    async def resolve_client_order_id(self, remark: str) -> str:
        """备注 → client_order_id（映射缺失时返回空串，由调用方走兜底匹配）。"""
        key = str(remark or "").strip()
        if not key:
            return ""
        try:
            redis = await self._get_redis()
            value = await redis.get(f"{_REMARK_MAP_PREFIX}{key}")
            if value:
                return str(value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QmtExec] 备注映射读取失败 remark=%s: %s", key, exc)
        return ""

    # -- 只读接口 -------------------------------------------------------
    async def ping(self) -> dict[str, Any]:
        cfg = self._require_enabled()
        return await self._call(
            self._get_backend(cfg).ping, timeout=min(float(cfg["timeout"]), 5.0)
        )

    async def get_asset(self) -> dict[str, Any]:
        cfg = self._require_enabled()
        return await self._call(self._get_backend(cfg).get_asset)

    async def get_positions(self) -> list[dict[str, Any]]:
        cfg = self._require_enabled()
        return await self._call(self._get_backend(cfg).get_positions)

    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        cfg = self._require_enabled()
        return await self._call(
            self._get_backend(cfg).query_orders, cancelable_only=cancelable_only
        )

    async def query_trades(self) -> list[dict[str, Any]]:
        cfg = self._require_enabled()
        return await self._call(self._get_backend(cfg).query_trades)

    # -- 交易接口 -------------------------------------------------------
    async def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        price: float | None = None,
        client_order_id: str = "",
    ) -> dict[str, Any]:
        """真实下单。返回 ``{order_id, order_sysid, remark, ...}``。

        幂等由上层保证：同一 client_order_id 不应重复调用（orders 表唯一约束）。
        """
        cfg = self._require_enabled()
        qmt_symbol = _to_qmt_symbol(symbol)
        if not qmt_symbol:
            raise QmtExecError(f"非法代码：{symbol}", code="INVALID_SYMBOL")
        volume = int(float(quantity or 0))
        if volume <= 0:
            raise QmtExecError("数量必须大于 0", code="INVALID_QUANTITY")
        cid = str(client_order_id or "").strip()
        remark = build_remark(cid) if cid else build_remark(f"qm-{qmt_symbol}-{volume}")
        if cid:
            # 先写映射再下单：TIMEOUT 时委托可能已提交，映射缺失会让回收端
            # 只能靠兜底匹配（且此时还不知道 QMT 委托号）。写失败不影响下单。
            await self._remember_remark(remark, cid)
        result = await self._call(
            self._get_backend(cfg).submit_order,
            symbol=qmt_symbol,
            side=str(side or "").upper(),
            quantity=volume,
            order_type=str(order_type or "LIMIT").upper(),
            price=float(price or 0),
            remark=remark,
            strategy_name=str(cfg["strategy_name"]),
            timeout=float(cfg["timeout"]),
        )
        logger.info(
            "[QmtExec] 下单已受理 symbol=%s side=%s qty=%s price=%s order_id=%s remark=%s cid=%s",
            qmt_symbol,
            side,
            volume,
            price,
            result.get("order_id"),
            remark,
            cid,
        )
        return result

    async def cancel_order(self, *, order_id: str, symbol: str = "") -> dict[str, Any]:
        cfg = self._require_enabled()
        return await self._call(
            self._get_backend(cfg).cancel_order,
            order_id=str(order_id),
            symbol=_to_qmt_symbol(symbol) if symbol else "",
        )


_singleton: QmtExecClient | None = None
_singleton_lock = threading.Lock()


def get_qmt_exec_client() -> QmtExecClient:
    """进程内单例（配置从环境变量读取，测试可自行构造注入）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = QmtExecClient.from_env()
    return _singleton


def set_qmt_exec_client(client: QmtExecClient | None) -> None:
    """测试/热更新用：替换单例。"""
    global _singleton
    with _singleton_lock:
        _singleton = client
