"""海外/港美股实盘券商 Broker 三件套：老虎(Tiger) / 富途(Futu) / 盈透(IB)。

统一实现 broker_client.BaseBroker 接口，由 live_trade_config 的 broker 字段
路由。三家 SDK 均为同步阻塞，全部经 asyncio.to_thread 包装，不阻塞事件循环。

部署前提（各自独立）：
- TigerBroker:  pip install tigeropen；.env 提供 TIGER_ID / TIGER_RSA_PRIVATE_KEY
  （老虎 OpenAPI 平台生成 RSA 密钥对，公钥绑定账户）。纯云端 REST，无需网关。
  TIGER_ACCOUNT 指定交易账户：实盘 U 开头、模拟 SIM 开头，模拟账户可直接
  用作"实盘链路演练"。
- FutuBroker:   pip install futu-api；本机/内网常驻 FutuOpenD 网关
  （.env: FUTU_OPEND_HOST/PORT，默认 127.0.0.1:11111）。FUTU_TRADE_ENV
  = REAL/SIMULATE；下单前需 unlock_trade（FUTU_TRADE_PWD_MD5）。
  OpenD 登录需人工扫码/设备验证一次，掉线需重登——日志会显式提示。
- IBBroker:     pip install ib_async（原 ib_insync）；常驻 IB Gateway
  容器（.env: IB_GATEWAY_HOST/PORT，paper 4002 / real 4001，IB_CLIENT_ID）。
  账户需在 IB 端开通对应市场行情/交易权限。

QuantMind 符号 → 券商代码映射：
  AAPL    → Tiger: AAPL        / Futu: US.AAPL   / IB: Stock(AAPL, SMART, USD)
  0001.HK → Tiger: 0001        / Futu: HK.0001   / IB: Stock(0700, SEHK, HKD)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any

import httpx
from pathlib import Path
import json

from backend.services.live_trading.services.broker_client import BaseBroker, BrokerResult

logger = logging.getLogger(__name__)

_DEFAULT_MARKET_URL = "http://quantmind-stream:8003"


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _require_env(key: str) -> str:
    value = _env(key)
    if not value:
        raise RuntimeError(
            f"配置 {key} 未提供：请在「模拟交易设置 → 券商接入」填写，或在 .env 中配置"
        )
    return value


def _setting(broker: str, field: str, env_key: str, default: str = "") -> str:
    """券商配置读取：Trade Redis 的 broker:config 优先，回退环境变量。"""
    try:
        from backend.services.trade.routers.broker_config import get_broker_setting

        value = get_broker_setting(broker, field)
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    return _env(env_key) or default


def _futu_code(symbol: str) -> str:
    """QuantMind 符号 → 富途代码（HK.0001 / US.AAPL / SH.600036）。"""
    upper = symbol.upper()
    if upper.endswith(".HK"):
        return f"HK.{upper.split('.')[0]}"
    if upper.endswith(".CN") or upper.endswith(".FUT"):
        return symbol  # 期货走富途期货账户，代码原样（如 CL.FUT 视账户支持）
    if "." in upper:
        code, suffix = upper.split(".", 1)
        return f"{suffix}.{code}"
    if re.fullmatch(r"\d{6}", upper):
        market = "SH" if upper.startswith(("6", "9")) else "SZ"
        return f"{market}.{upper}"
    return f"US.{upper}"


def _tiger_contract(symbol: str) -> tuple[str, str, str]:
    """QuantMind 符号 → (symbol, currency, exchange)：
    AAPL → ('AAPL', 'USD', 'SMART')；0001.HK → ('00001', 'HKD', 'SEHK')"""
    upper = symbol.upper()
    if upper.endswith(".HK"):
        return upper.split(".")[0].zfill(5), "HKD", "SEHK"
    if upper.endswith(".US"):
        return upper.split(".")[0], "USD", "SMART"
    if "." in upper:
        return upper.split(".")[0], "USD", "SMART"
    return upper, "USD", "SMART"


def _ib_contract_params(symbol: str) -> tuple[str, str, str]:
    """QuantMind 符号 → (IB symbol, exchange, currency)。"""
    upper = symbol.upper()
    if upper.endswith(".HK"):
        return upper.split(".")[0], "SEHK", "HKD"
    if upper.endswith(".US"):
        return upper.split(".")[0], "SMART", "USD"
    if "." in upper:
        code, suffix = upper.split(".", 1)
        return code, suffix, "USD"
    return upper, "SMART", "USD"


class _StreamQuoteMixin:
    """行情查询走平台行情网关（与 PaperTradingBroker 同源），不依赖券商行情权限。"""

    market_url: str = _DEFAULT_MARKET_URL
    _http: httpx.AsyncClient | None = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    async def query_quote(self, symbol: str) -> dict[str, Any]:
        try:
            from backend.shared.auth import get_internal_call_secret

            client = await self._http_client()
            resp = await client.get(
                f"{self.market_url.rstrip('/')}/api/v1/quotes/{symbol}",
                headers={"X-Internal-Call": get_internal_call_secret()},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "symbol": symbol,
                    "price": float(data.get("current_price") or data.get("last_price") or 0),
                    "pre_close": float(data.get("pre_close") or 0),
                    "suspended": bool(data.get("suspended") or data.get("is_suspended")),
                }
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] query_quote %s failed: %s", type(self).__name__, symbol, e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 老虎证券
# ─────────────────────────────────────────────────────────────────────────────


class TigerBroker(_StreamQuoteMixin, BaseBroker):
    """老虎证券 OpenAPI（tigeropen SDK，纯云端 REST，无需网关）。

    配置（Redis broker:config:tiger 优先，回退环境变量）：
      tiger_id        — 开放平台用户 ID（如 20161435）
      rsa_private_key — PKCS#1 私钥（平台生成的裸 base64、PEM 文本或 .pem 文件路径）
      account         — 交易账户：实盘数字（如 3667944）/ 模拟 SIM 开头
    """

    def __init__(self) -> None:
        self._client: Any = None
        # _get_client 在 asyncio.to_thread 的 worker 线程中执行，须用线程锁
        # （asyncio.Lock 的同步 with 在 Python 3.10 无 __enter__，且线程内无事件循环）
        self._lock = threading.Lock()

    def _wrap_pem(self, key_text: str) -> str:
        """裸 PKCS#1 base64 → PEM；清洗空白并校验完整性（复制丢失字符会在此报错）。

        兼容三种形态：PEM 文本（含 BEGIN 头）、.pem 文件路径、裸 base64。
        """
        key_text = (key_text or "").strip()
        if not key_text:
            raise RuntimeError("RSA 私钥为空")
        # 文件路径形态
        if "BEGIN" not in key_text and os.path.exists(key_text):
            with open(key_text) as f:
                key_text = f.read().strip()
        if "BEGIN" in key_text:
            return key_text
        # 清除所有空白（平台复制时常见的折行空格/不可见字符）
        compact = "".join(key_text.split())
        data_chars = len(compact.rstrip("="))
        if data_chars % 4 != 0:
            raise RuntimeError(
                f"RSA 私钥格式无效：base64 数据字符数 {data_chars} 不是 4 的倍数，"
                "复制可能不完整（末尾缺字符）。请从老虎 OpenAPI 平台重新完整复制，"
                "或下载 .pem 文件后在私钥字段填写该文件的完整路径"
            )
        return (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + "\n".join(compact[i : i + 64] for i in range(0, len(compact), 64))
            + "\n-----END RSA PRIVATE KEY-----\n"
        )

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from tigeropen.tiger_open_config import TigerOpenClientConfig

                tiger_id = _setting("tiger", "tiger_id", "TIGER_ID")
                private_key = _setting("tiger", "rsa_private_key", "TIGER_RSA_PRIVATE_KEY")
                account = _setting("tiger", "account", "TIGER_ACCOUNT")
                if not tiger_id or not private_key or not account:
                    raise RuntimeError(
                        "老虎证券接入未配置：请在「券商实盘接入」填写 Tiger ID、RSA 私钥、交易账户"
                    )
                config = TigerOpenClientConfig()
                config.tiger_id = tiger_id
                config.private_key = self._wrap_pem(private_key)
                config.account = account
                from tigeropen.trade.trade_client import TradeClient
                self._client = TradeClient(config)
            return self._client

    def _account(self) -> str:
        return _setting("tiger", "account", "TIGER_ACCOUNT")

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        def _place() -> dict[str, Any]:
            from tigeropen.common.util.contract_utils import stock_contract
            from tigeropen.common.util.order_utils import limit_order, market_order

            client = self._get_client()
            sym, currency, exchange = _tiger_contract(symbol)
            contract = stock_contract(symbol=sym, currency=currency, exchange=exchange)
            action = "BUY" if str(side).upper() == "BUY" else "SELL"
            account = client.client_config.account
            if str(order_type).lower() == "market" or not price:
                order = market_order(
                    account=account, contract=contract,
                    action=action, quantity=float(quantity),
                )
            else:
                order = limit_order(
                    account=account, contract=contract,
                    action=action, quantity=float(quantity), limit_price=float(price),
                )
            # place_order 返回全局订单 id；order.id 提交后亦被 SDK 回填，双保险
            order_id = client.place_order(order) or getattr(order, "id", None)
            return {"order_id": str(order_id or ""), "message": "SUBMITTED"}

        try:
            data = await asyncio.to_thread(_place)
            return BrokerResult(
                success=True,
                exchange_order_id=data.get("order_id", ""),
                message=data.get("message", ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[TigerBroker] place_order %s failed: %s", symbol, e)
            return BrokerResult(success=False, message=str(e))

    async def query_account(self, user_id: str, tenant_id: str = "default") -> dict[str, Any]:
        def _query() -> dict[str, Any]:
            client = self._get_client()
            config = client.client_config
            assets_list = client.get_assets(account=config.account) or []
            # get_assets 返回 PortfolioAccount 列表；按 account 匹配，缺省取第一个
            assets = next(
                (a for a in assets_list if str(getattr(a, "account", "")) == str(config.account)),
                assets_list[0] if assets_list else None,
            )
            if assets is None:
                return {}
            summary = getattr(assets, "summary", None)
            total_asset = float(getattr(summary, "net_liquidation", 0) or 0)
            cash = float(getattr(summary, "cash", 0) or 0)
            gross = float(getattr(summary, "gross_position_value", 0) or 0)
            positions: dict[str, Any] = {}
            try:
                pos_list = client.get_positions(account=config.account) or []
                for pos in pos_list:
                    contract = getattr(pos, "contract", None)
                    sym = str(getattr(contract, "symbol", "") or getattr(pos, "symbol", "") or "")
                    if not sym:
                        continue
                    cost = getattr(pos, "average_cost", None)
                    if cost is None:
                        cost = getattr(pos, "avg_cost", None)
                    positions[sym] = {
                        "volume": float(getattr(pos, "quantity", 0) or 0),
                        "available_volume": float(getattr(pos, "salable_qty", 0) or 0),
                        "price": float(getattr(pos, "market_price", 0) or 0),
                        "market_value": float(getattr(pos, "market_value", 0) or 0),
                        "cost": float(cost or 0),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TigerBroker] positions query failed: %s", exc)
            return {
                "total_asset": total_asset,
                "cash": cash,
                "market_value": gross or (total_asset - cash),
                "positions": positions,
            }

        try:
            return await asyncio.to_thread(_query)
        except Exception as e:  # noqa: BLE001
            logger.error("[TigerBroker] query_account failed: %s", e)
            return {}

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        def _cancel() -> bool:
            client = self._get_client()
            client.cancel_order(id=int(exchange_order_id), account=self._account())
            return True

        try:
            return await asyncio.to_thread(_cancel)
        except Exception as e:  # noqa: BLE001
            logger.error("[TigerBroker] cancel_order %s failed: %s", exchange_order_id, e)
            return False


class FutuBroker(_StreamQuoteMixin, BaseBroker):
    """富途 OpenAPI（futu-api，经 FutuOpenD 网关）。

    配置（Redis broker:config:futu 优先，回退环境变量）：
      opend_host/opend_port  — FutuOpenD 网关地址（局域网 IP 即可）
      trade_pwd_md5          — 交易密码 MD5（REAL 环境下单前自动解锁）
      trade_env              — SIMULATE（模拟）/ REAL（实盘）
      rsa_key                — OpenD 端 RSA 私钥文件路径（默认 /data/futu-opend/rsa.key）
    """

    def __init__(self) -> None:
        self.host = _setting("futu", "opend_host", "FUTU_OPEND_HOST", "127.0.0.1")
        try:
            self.port = int(_setting("futu", "opend_port", "FUTU_OPEND_PORT", "11111"))
        except ValueError:
            self.port = 11111
        self.trade_env_real = _setting(
            "futu", "trade_env", "FUTU_TRADE_ENV", "SIMULATE"
        ).upper() == "REAL"
        self._pwd_md5 = _setting("futu", "trade_pwd_md5", "FUTU_TRADE_PWD_MD5")
        self.rsa_key = _setting("futu", "rsa_key", "FUTU_RSA_KEY", "/data/futu-opend/rsa.key")

    @staticmethod
    def _run_subprocess(op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """在独立子进程中执行 futu SDK 调用（脚本 futu_subprocess.py）。

        futu SDK 的连接/等待模型与 asyncio 事件循环混用会死锁（实测
        to_thread 挂起），独立子进程隔离最可靠。
        """
        import subprocess
        import sys as _sys

        host = _setting("futu", "opend_host", "FUTU_OPEND_HOST", "").strip()
        # 0.0.0.0 是监听地址不是连接地址；127.0.0.1 在本容器内连不到 OpenD——
        # 两者一律回退同网络的容器名（quantmind-net 内直接可达）
        if host in {"", "0.0.0.0", "127.0.0.1", "localhost"}:
            host = "futu-opend"
        port = _setting("futu", "opend_port", "FUTU_OPEND_PORT", "11111")
        rsa_key = _setting("futu", "rsa_key", "FUTU_RSA_KEY", "/data/futu-opend/rsa.key")
        script_path = Path(__file__).resolve().parent / "futu_subprocess.py"
        import tempfile

        fd, result_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        cmd = [
            _sys.executable, str(script_path),
            host, str(port), rsa_key, op, json.dumps(payload), result_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            with open(result_path) as f:
                out_text = f.read().strip()
            if proc.returncode != 0 or not out_text:
                detail = (proc.stderr or "futu subprocess failed")[-300:]
                raise RuntimeError(detail)
            return json.loads(out_text)
        finally:
            os.unlink(result_path)

    async def query_account(self, user_id: str, tenant_id: str = "default") -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._run_subprocess, "account",
                {"env": "REAL" if self.trade_env_real else "SIMULATE"},
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[FutuBroker] query_account failed: %s", e)
            return {}

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        is_hk = symbol.upper().endswith(".HK")
        payload = {
            "env": "REAL" if self.trade_env_real else "SIMULATE",
            "order": {
                "code": _futu_code(symbol),
                "price": float(price or 0),
                "quantity": float(quantity),
                "order_type": "MARKET" if str(order_type).lower() == "market" else "NORMAL",
                "trd_side": "BUY" if str(side).upper() == "BUY" else "SELL",
                "is_hk": is_hk,
            },
        }
        try:
            data = await asyncio.to_thread(self._run_subprocess, "place", payload)
            return BrokerResult(
                success=bool(data.get("success")),
                exchange_order_id=str(data.get("order_id", "")),
                message=str(data.get("message", "")),
                # SIMULATE MARKET 单即时成交（dealt_qty>0）；透传给 trading_engine
                # 即时落成交，否则富途模拟成交不会进 ledger。
                filled_quantity=float(data.get("filled_quantity") or 0),
                filled_price=float(data.get("filled_price") or 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[FutuBroker] place_order %s failed: %s", symbol, e)
            return BrokerResult(success=False, message=str(e))

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        try:
            data = await asyncio.to_thread(
                self._run_subprocess, "cancel",
                {"env": "REAL" if self.trade_env_real else "SIMULATE",
                 "order_id": exchange_order_id},
            )
            return bool(data.get("success"))
        except Exception as e:  # noqa: BLE001
            logger.error("[FutuBroker] cancel_order %s failed: %s", exchange_order_id, e)
            return False


class IBBroker(_StreamQuoteMixin, BaseBroker):
    """盈透证券 TWS API（ib_async / ib_insync + IB Gateway）。

    IB_GATEWAY_HOST/PORT 指向常驻 IB Gateway 容器（paper 4002 / real 4001）。
    IB 连接为长连接，懒建立、断线自动重连。
    """

    def __init__(self) -> None:
        self.host = _setting("ib", "gateway_host", "IB_GATEWAY_HOST", "127.0.0.1")
        try:
            self.port = int(_setting("ib", "gateway_port", "IB_GATEWAY_PORT", "4002"))
        except ValueError:
            self.port = 4002
        try:
            self.client_id = int(_setting("ib", "client_id", "IB_CLIENT_ID", "7"))
        except ValueError:
            self.client_id = 7
        self._ib: Any = None
        self._lock = asyncio.Lock()

    async def _get_ib(self) -> Any:
        async with self._lock:
            if self._ib is None or not self._ib.isConnected():
                from ib_async import IB

                ib = IB()
                await ib.connectAsync(self.host, self.port, clientId=self.client_id)
                self._ib = ib
            return self._ib

    async def place_order(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
        tenant_id: str = "default",
    ) -> BrokerResult:
        try:
            from ib_async import LimitOrder, MarketOrder, Stock

            ib = await self._get_ib()
            ib_symbol, exchange, currency = _ib_contract_params(symbol)
            contract = Stock(ib_symbol, exchange, currency)
            action = "BUY" if str(side).upper() == "BUY" else "SELL"
            if str(order_type).lower() == "market" or not price:
                order = MarketOrder(action, float(quantity))
            else:
                order = LimitOrder(action, float(quantity), float(price))
            trade = ib.placeOrder(contract, order)
            return BrokerResult(
                success=True,
                exchange_order_id=str(trade.order.orderId),
                message=str(getattr(trade.orderStatus, "status", "Submitted")),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[IBBroker] place_order %s failed: %s", symbol, e)
            return BrokerResult(success=False, message=str(e))

    async def query_account(self, user_id: str, tenant_id: str = "default") -> dict[str, Any]:
        try:
            ib = await self._get_ib()
            summary = await ib.accountSummaryAsync()
            values = {item.tag: item.value for item in summary}
            positions: dict[str, Any] = {}
            for pos in await ib.reqPositionsAsync():
                contract = pos.contract
                key = getattr(contract, "localSymbol", "") or contract.symbol
                positions[key] = {
                    "volume": float(pos.position),
                    "available_volume": float(pos.position),
                    "price": float(getattr(pos, "marketPrice", 0) or 0),
                    "market_value": float(pos.position) * float(getattr(pos, "marketPrice", 0) or 0),
                    "cost": float(pos.avgCost),
                }
            return {
                "total_asset": float(values.get("NetLiquidation", 0) or 0),
                "cash": float(values.get("AvailableFunds", values.get("TotalCashValue", 0)) or 0),
                "market_value": float(values.get("GrossPositionValue", 0) or 0),
                "positions": positions,
            }
        except Exception as e:  # noqa: BLE001
            logger.error("[IBBroker] query_account failed: %s", e)
            return {}

    async def cancel_order(self, exchange_order_id: str, **kwargs) -> bool:
        try:
            ib = await self._get_ib()
            for trade in ib.openTrades():
                if str(trade.order.orderId) == str(exchange_order_id):
                    ib.cancelOrder(trade.order)
                    return True
            return False
        except Exception as e:  # noqa: BLE001
            logger.error("[IBBroker] cancel_order %s failed: %s", exchange_order_id, e)
            return False


def get_overseas_broker(broker_type: str) -> BaseBroker:
    """按 broker_type 构建海外券商 broker（live_trade_config.broker 路由）。"""
    broker_type = str(broker_type or "").lower().strip()
    if broker_type == "tiger":
        return TigerBroker()
    if broker_type == "futu":
        return FutuBroker()
    if broker_type == "ib":
        return IBBroker()
    raise ValueError(
        f"未知券商类型: {broker_type}（可选 tiger / futu / ib）"
    )
