"""QMT（迅投大 QMT）桥 Broker —— 阶段二：查询 + 下单（下单默认关闭）。

通道拓扑（与 8550 通达信桥**完全独立**，互不拖累）：
    BayMax(Linux) ──Redis RPC──> Windows 交易机 · 大 QMT 内置策略
Windows 侧 = quantmind 的 qmt-bridge-kit（QMT 策略编辑器里加载 BIGQMT_REDIS_DRYRUN.py，
它自己 import 其余模块）；本模块是它的 Linux 侧客户端，走 `xtquant-big-convert`
的 xtquant 兼容层（Linux 不需要真机 xtquant，也不需要 userdata_mini）。

职责边界：只做「账户 / 持仓 / 委托 / 成交」；**行情不在这里**（继续走 TDX 桥与
quantdb）。2026-09-10 实录：行情通道全通而账户通道整日掉线，两条链路必须可分——
把行情也搬过来等于自毁冗余。

阶段二（当前）：buy/sell/cancel_order 已接线下单，但受**两处独立总闸**约束——
本侧 allow_trading（默认 false，见下）+ Windows 侧 rpc_allow_order_methods。
接线到位 ≠ 能下单：上线顺序是「小额验证 → 人工确认 → 才开本侧开关」。

配置（按优先级：构造参数 → config/qmt_bridge.json → 环境变量）：
  account_id / redis_host / redis_port(6380) / redis_db(0) / redis_password
  account_type(STOCK) / timeout(10) / strategy_name(baymax) / allow_trading(false)
  环境变量 QMT_EXEC_* 优先，兼容 big-convert 原生 BIGQMT_*。
  ★ config/qmt_bridge.json 含密码，不入库（.gitignore）。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_tools.brokers.base import Broker, BrokerError

# 与 config/tdx_bridge.json 同惯例：运维/安装写入的运行时配置，优先于环境变量
_OVERRIDE_FILE = Path(__file__).resolve().parents[2] / "config" / "qmt_bridge.json"

DEFAULT_PORT = 6380
DEFAULT_DB = 0
DEFAULT_TIMEOUT = 10.0
DEFAULT_ACCOUNT_TYPE = "STOCK"
DEFAULT_STRATEGY_NAME = "baymax"

# QMT 委托状态码 → 本系统（TDX 口径）小写状态串。
# 口径必须与 scripts/live_fills.py 的终态集合 ("cancelled","withdrawn","rejected",
# "expired","filled") 对齐：51(报撤中)/52(部成待撤) **不是终态**，误判会漏记成交
# 或提前清掉在途单；53(部撤) 是终态（已撤，剩量不再成交，部成由 reconcile 补记）。
STATUS_MAP: Dict[int, str] = {
    48: "submitted",   # 未申报
    49: "submitted",   # 等待申报
    50: "submitted",   # 已申报
    51: "submitted",   # 报撤中（未确认，仍可能成交）
    52: "partial_fill",  # 部成待撤
    53: "cancelled",   # 部撤（终态）
    54: "cancelled",   # 已撤
    55: "partial_fill",  # 部分成交
    56: "filled",      # 全部成交
    57: "rejected",    # 废单
    255: "submitted",  # 未知（不误判为终态）
}

# 本侧下单总闸：默认关闭。接线到位 ≠ 能下单——上线要走「小额验证 → 人工确认」，
# 所以默认值必须是关，靠 config/qmt_bridge.json 的 allow_trading（或
# QMT_EXEC_ALLOW_TRADING=1）显式打开。
DEFAULT_ALLOW_TRADING = False
TRADING_OFF_MSG = ("QMT 下单未开启（本侧总闸关闭）：要下单请把 config/qmt_bridge.json 的 "
                   "allow_trading 设为 true（或 QMT_EXEC_ALLOW_TRADING=1）——"
                   "开启前请确认小额验证已完成")


def qmt_overrides() -> Dict[str, Any]:
    """读取 config/qmt_bridge.json（缺文件/坏 JSON 返回空，不阻断构造）。"""
    try:
        data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _attr(obj: Any, *names: str, default: Any = "") -> Any:
    """取对象首个存在且非 None 的属性（QMT 字段名随版本略有出入）。"""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = -1) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_TRUE = {"1", "true", "yes", "on", "y"}


def _to_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() in _TRUE


def _normalize_order_status(v: Any) -> str:
    """委托状态归一：数值码走 STATUS_MAP，字符串取小写；空值按 submitted。

    对端不同版本/不同 gateway 给的形状不一：真机 gateway 给 "SUBMITTED" 串，
    查单类接口给 48-57 数值码，dry-run gateway 给 "DRY_RUN"。数值原样小写会得到
    "57" 这种谁也认不出的「状态」，落不进 live_fills 的终态集合。
    """
    if v is None or v == "" or isinstance(v, bool):
        return "submitted"
    if isinstance(v, (int, float)):
        return STATUS_MAP.get(int(v), "submitted")
    s = str(v).strip()
    try:
        # 数值码也认字符串与浮点形状（"57" / "57.0"）——只判 isdigit 会把 JSON
        # 里的 57.0 得成字符串 "57.0"，谁也不认识，落不进 live_fills 的终态集合
        return STATUS_MAP.get(int(float(s)), "submitted")
    except (TypeError, ValueError, OverflowError):
        return s.lower() or "submitted"


class QmtBridgeBroker(Broker):
    """大 QMT 桥。下单已接线但默认关闭（allow_trading）；接口形状对齐 TdxBridgeBroker。"""

    name = "qmt"
    markets = "cn"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        ov = qmt_overrides()

        def pick(key: str, envs: tuple, default: Any = "") -> Any:
            for src in (cfg.get(key), ov.get(key)):
                if src not in (None, ""):
                    return src
            for e in envs:
                if os.getenv(e):
                    return os.getenv(e)
            return default

        self.account_id = str(pick("account_id",
                                   ("QMT_EXEC_ACCOUNT_ID", "BIGQMT_ACCOUNT_ID"), ""))
        self.account_type = str(pick("account_type",
                                     ("QMT_EXEC_ACCOUNT_TYPE", "BIGQMT_ACCOUNT_TYPE"),
                                     DEFAULT_ACCOUNT_TYPE))
        self.strategy_name = str(pick("strategy_name",
                                      ("QMT_EXEC_STRATEGY_NAME",), DEFAULT_STRATEGY_NAME))
        self.timeout = float(pick("timeout", ("QMT_EXEC_TIMEOUT",), DEFAULT_TIMEOUT))
        self.allow_trading = _to_bool(
            pick("allow_trading", ("QMT_EXEC_ALLOW_TRADING",), DEFAULT_ALLOW_TRADING),
            DEFAULT_ALLOW_TRADING)
        self.redis: Dict[str, Any] = {
            "host": str(pick("redis_host", ("QMT_EXEC_REDIS_HOST", "BIGQMT_REDIS_HOST"), "")),
            "port": int(pick("redis_port", ("QMT_EXEC_REDIS_PORT",), DEFAULT_PORT)),
            "db": int(pick("redis_db", ("QMT_EXEC_REDIS_DB",), DEFAULT_DB)),
            "password": str(pick("redis_password",
                                 ("QMT_EXEC_REDIS_PASSWORD", "BIGQMT_REDIS_PASSWORD"), "")),
        }
        self._trader: Any = None
        self._account: Any = None
        self._lock = threading.Lock()

        if not self.account_id:
            raise BrokerError("QMT 桥未配置资金账号：config/qmt_bridge.json 的 "
                              "account_id（或 QMT_EXEC_ACCOUNT_ID）")
        if not self.redis["host"]:
            raise BrokerError("QMT 桥未配置 Redis 地址：config/qmt_bridge.json 的 "
                              "redis_host（或 QMT_EXEC_REDIS_HOST）")

    # ---------- 连接 ----------

    def _redact(self, text: Any) -> str:
        """底层库报错常把 Redis URL（含密码）原样带出来 → 出日志前先脱敏。"""
        s = str(text)
        pw = str(self.redis.get("password") or "")
        return s.replace(pw, "***") if pw else s

    def _ensure(self):
        """惰性连接（首次调用时 import + configure），双检锁防并发重复初始化。"""
        if self._trader is not None and self._account is not None:
            return self._trader, self._account
        with self._lock:
            if self._trader is not None and self._account is not None:
                return self._trader, self._account
            try:
                from bigqmt_signal_trader.xtquant_compat import (  # noqa: PLC0415
                    StockAccount,
                    configure,
                    xt_trader,
                )
            except ImportError as exc:
                raise BrokerError(
                    '未安装 QMT 客户端库（pip install "xtquant-big-convert[redis]==0.3.31"）'
                ) from exc
            try:
                configure(account_id=self.account_id,
                          redis_config=dict(self.redis),
                          timeout_seconds=self.timeout)
            except Exception as exc:  # noqa: BLE001 底层库异常类型不稳定
                raise BrokerError(
                    f"QMT 客户端配置失败：{self._redact(exc)}"
                    "（检查资金账号与桥 Redis 地址/密码）") from exc
            self._trader = xt_trader
            self._account = StockAccount(self.account_id, self.account_type)
            return self._trader, self._account

    # ---------- 查询 ----------

    def ping(self) -> Dict[str, Any]:
        """RPC 存活探测（只读，不触发任何交易方法）。"""
        trader, _ = self._ensure()
        try:
            result = trader.client.call("ping", {})
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 桥 ping 失败：{self._redact(exc)}") from exc
        return {"ok": True, "result": result, "account_id": self.account_id}

    def _account_query(self) -> Dict[str, Any]:
        """资产+持仓，返回形状与 TdxBridgeBroker._account_query 完全一致。

        上层 10+ 个脚本读的键固定为 asset.asset / asset.cash /
        positions[].stock_code|total_volume|available_volume|cost_price——
        available_volume 是 T+1 卖出闸门的唯一依据，必须如实映射 can_use_volume。
        """
        trader, account = self._ensure()
        try:
            asset = trader.query_stock_asset(account)
            positions = trader.query_stock_positions(account) or []
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 账户查询失败：{self._redact(exc)}") from exc
        if asset is None:
            raise BrokerError("QMT 账户查询返回空（交易端可能未登录）")

        total_asset = _to_float(_attr(asset, "total_asset"))
        cash = _to_float(_attr(asset, "cash"))
        market_value = _to_float(_attr(asset, "market_value"))
        pos_out: List[Dict[str, Any]] = []
        for p in positions:
            code = str(_attr(p, "stock_code", "instrument_id", default="") or "")
            volume = _to_float(_attr(p, "volume", "total_volume"))
            if not code or volume <= 0:
                continue          # 清仓残留行：不算持仓
            mv = _to_float(_attr(p, "market_value"))
            pos_out.append({
                "stock_code": code,
                "stock_name": str(_attr(p, "stock_name", "instrument_name", default="") or ""),
                "cost_price": _to_float(_attr(p, "avg_price", "open_price")),
                "total_volume": volume,
                "available_volume": _to_float(_attr(p, "can_use_volume")),
                "market_value": mv,
                # 现价兜底：消费方把 0/缺省视为"拿不到"，会回落到行情接口
                "last_price": round(mv / volume, 4) if volume > 0 and mv > 0 else 0,
            })
        return {
            "account_id": self.account_id,
            "channel_used": "qmt",
            "asset": {
                "currency": "CNY",
                "asset": total_asset,
                "balance": total_asset,
                "cash": cash,
                "market_value": market_value,
                "frozen_cash": _to_float(_attr(asset, "frozen_cash")),
            },
            "positions": pos_out,
        }

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        """{symbol: total_volume}（对齐 TdxBridgeBroker）。"""
        data = self._account_query()
        return {p["stock_code"]: float(p["total_volume"])
                for p in data.get("positions") or [] if p.get("stock_code")}

    def get_cash(self, signature: str, today_date: str) -> float:
        """可用资金（对齐 TdxBridgeBroker：asset.cash）。"""
        return float((self._account_query().get("asset") or {}).get("cash") or 0)

    def get_orders(self, stock_code: str = "",
                   cancelable_only: bool = False) -> List[Dict[str, Any]]:
        """当日委托（QMT 无历史接口），字段名对齐 TDX 桥口径。"""
        trader, account = self._ensure()
        try:
            orders = trader.query_stock_orders(account, cancelable_only=cancelable_only) or []
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 委托查询失败：{self._redact(exc)}") from exc
        out: List[Dict[str, Any]] = []
        for o in orders:
            code = str(_attr(o, "stock_code", default="") or "")
            if stock_code and code != stock_code:
                continue
            status_code = _to_int(_attr(o, "order_status", "status"), -1)
            side_code = _to_int(_attr(o, "order_type"), -1)
            out.append({
                "order_id": str(_attr(o, "order_id", default="") or ""),
                "stock_code": code,
                "side": {23: "buy", 24: "sell"}.get(side_code, ""),
                "status": STATUS_MAP.get(status_code, "submitted"),
                "status_code": status_code,
                "order_price": _to_float(_attr(o, "price")),
                "filled_price": _to_float(_attr(o, "traded_price")),
                "filled_volume": _to_float(_attr(o, "traded_volume")),
                "total_volume": _to_float(_attr(o, "order_volume")),
                "time": str(_attr(o, "order_time", "time", default="") or ""),
                "status_msg": str(_attr(o, "status_msg", default="") or ""),
            })
        return out

    def get_trades(self) -> List[Dict[str, Any]]:
        """当日成交明细（对账用；TDX 桥无此接口，QMT 有）。"""
        trader, account = self._ensure()
        try:
            trades = trader.query_stock_trades(account) or []
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 成交查询失败：{self._redact(exc)}") from exc
        return [{
            "trade_id": str(_attr(t, "traded_id", "trade_id", default="") or ""),
            "order_id": str(_attr(t, "order_id", default="") or ""),
            "stock_code": str(_attr(t, "stock_code", default="") or ""),
            "side": {23: "buy", 24: "sell"}.get(_to_int(_attr(t, "order_type"), -1), ""),
            "filled_volume": _to_float(_attr(t, "traded_volume")),
            "filled_price": _to_float(_attr(t, "traded_price")),
            "time": str(_attr(t, "traded_time", "time", default="") or ""),
        } for t in trades]

    # ---------- 交易（阶段二：已接线，默认关闭） ----------

    def bridge_status(self) -> Dict[str, Any]:
        """桥自述（只读 ping）：rpc_allow_order_methods / 版本 / 账号类型。

        与账户查询分开是必要的：账户读得通 ≠ 那边放开了下单。两处独立总闸
        （本侧 allow_trading + Windows 侧 rpc_allow_order_methods），任一处没开
        都不该发单——发出去只会换回一个 PermissionError。
        """
        trader, _ = self._ensure()
        try:
            result = trader.client.call("ping", {})
        except Exception as exc:  # noqa: BLE001 底层库异常类型不稳定
            raise BrokerError(f"QMT 桥状态查询失败：{self._redact(exc)}") from exc
        return dict(result or {})

    def _order_gate(self) -> Dict[str, Any]:
        """下单前两道闸都过一遍，并把桥自述回给调用方（便于记进日志）。"""
        if not self.allow_trading:
            raise BrokerError(TRADING_OFF_MSG)
        status = self.bridge_status()
        if not status.get("allow_order_methods"):
            raise BrokerError(
                "QMT 桥拒绝下单：Windows 侧 rpc_allow_order_methods 未开——"
                "改 bigqmt_signal_trader_local_config.py 后重载策略")
        return status

    def _next_remark(self, signature: str, plan_id: Optional[str] = None) -> str:
        """委托备注 → QMT 的 user_order_id。

        桥的委托号由 passorder 异步分配（不返回值），回填是靠 remark 精确匹配的；
        备注不唯一就认不回自己的单，只能把「已提交但暂无委托号」误判成下单失败。

        plan_id（执行路径的幂等键，如哨兵的 watch-<agent>-<code>-<时间戳>）优先取
        前 16 字符作 tag：对端直连 RPC 无 plan 队列、不做去重，plan_id 只用于让
        委托在柜台可辨识（哨兵路径 signature 为空，原本只能标成 baymax-x-...）。
        总长度与既有格式一致（remark 长度上限未知，不动）；毫秒后缀保证唯一。
        """
        import time as _time  # noqa: PLC0415 只在真正下单时才需要

        tag = str(plan_id or signature or "x")[:16]
        return f"baymax-{tag}-{int(_time.time() * 1000)}"

    def _place_order(self, signature: str, symbol: str, side: str, amount: int,
                     price: Optional[float] = None,
                     plan_id: Optional[str] = None) -> Dict[str, Any]:
        """A 股下单（限价 price 有值 / 市价-LATEST_PRICE 无值），形状对齐 TdxBridgeBroker。

        校验先于 RPC：入参不合法就地报错，不把注定被拒的单子发到桥上。
        """
        code = str(symbol or "").strip().upper()
        if not code:
            raise BrokerError("QMT 下单缺少股票代码")
        volume = _to_int(amount, 0)
        if volume <= 0:
            raise BrokerError(f"QMT 下单数量必须为正整数：{amount!r}")
        if price is not None and _to_float(price, 0.0) <= 0:
            raise BrokerError(f"QMT 下单价格必须为正：{price!r}")

        self._order_gate()
        trader, _ = self._ensure()
        remark = self._next_remark(signature, plan_id)
        params: Dict[str, Any] = {
            "account_id": self.account_id,
            "action": "BUY" if side == "buy" else "SELL",
            "stock_code": code,
            "volume": volume,
            "price": _to_float(price, 0.0) if price is not None else 0.0,
            # 限价 11 / 最新价 5（见对端 PRICE_TYPE_ALIASES）；市价单在 A 股
            # 不是所有券商都支持，无价时走「最新价」而不是「市价」更稳妥
            "price_type": "LIMIT" if price is not None else "LATEST_PRICE",
            "strategy_name": self.strategy_name,
            "signal_id": remark,
            "remark": remark,
        }
        try:
            raw = trader.client.call("submit_order", params)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 下单失败（{side} {code} {volume}）："
                              f"{self._redact(exc)}") from exc
        return self._submit_result(raw, code, side, volume, remark)

    @staticmethod
    def _submit_result(raw: Any, code: str, side: str, volume: int,
                       remark: str) -> Dict[str, Any]:
        """把对端 OrderSubmitResult 归一成稳定形状（status 口径同 STATUS_MAP）。

        诚实性三则（与 TdxBridgeBroker._place_order 的 2026-09-11 口径对齐）：
        - 数值状态码（对端有的版本给码、有的给串）走 STATUS_MAP；
        - status 归一为 rejected → 抛 BrokerError（废单不是成功）；
        - order_sys_id 为空 → message 如实说明「委托号未知、成交跟踪不了」，
          不写「已受理」也不再转述对端那句看着像成功的 "passorder submitted"
          （对端在查单异常时会静默降级成这个形状，order_sys_id 恒 None）。
        """
        info = dict(raw) if isinstance(raw, dict) else {"message": str(raw)}
        status = _normalize_order_status(info.get("status"))
        order_id = str(info.get("order_sys_id") or "")
        message = str(info.get("message") or "")
        if status == "rejected":
            raise BrokerError(
                f"QMT 下单被拒（废单 rejected，{side} {code} {volume}）："
                f"{message or '对端未给原因'}")
        if not order_id:
            # 对端直连 RPC 无 plan 队列、不做去重 → 重试会变成第二笔真委托
            message = (f"QMT 未返回委托号（受理状态未知）——单可能已在柜台，"
                       f"成交无法跟踪，不要重发；请按备注 {remark} 人工核对当日委托。"
                       + (f" 对端说明：{message}" if message else ""))
        return {
            # ok = 可跟踪的受理（有委托号）。原实现写 status != "rejected" 是死条件
            # （rejected 已在上面 raise），无委托号时也 True → 若有调用方按 ok 判成功
            # 就会被误导（2026-09-11 审查 LOW）。当前唯一消费方 qmt_probe 只读只读接口。
            "ok": bool(order_id),
            "order_id": order_id,
            "order_sys_id": order_id,
            "user_order_id": str(info.get("user_order_id") or remark),
            "stock_code": code,
            "side": side,
            "volume": volume,
            "status": status,
            "message": message,
            "raw": info,
        }

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None,
            plan_id: Optional[str] = None) -> Dict[str, Any]:
        # plan_id：执行路径（哨兵/整点轮）的幂等键，接口与 TdxBridgeBroker 对齐；
        # 对端不做去重（直连 RPC 无 plan 队列），仅进 remark 供人工核对
        return self._place_order(signature, symbol, "buy", amount, price, plan_id)

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None,
             plan_id: Optional[str] = None) -> Dict[str, Any]:
        return self._place_order(signature, symbol, "sell", amount, price, plan_id)

    def cancel_order(self, stock_code: str, order_id: str,
                     user_order_id: str = "") -> Dict[str, Any]:
        """撤单（非幂等）：返回不描述「已撤」，只描述「撤单请求已受理」——
        终态以 get_orders() 的状态码为准（53/54 才是撤成）。"""
        if not str(order_id or "").strip():
            raise BrokerError("QMT 撤单缺少委托号（order_sys_id）")
        self._order_gate()
        trader, _ = self._ensure()
        try:
            raw = trader.client.call("cancel_order", {
                "account_id": self.account_id,
                "order_sys_id": str(order_id),
                "user_order_id": str(user_order_id or ""),
            })
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"QMT 撤单失败（{stock_code} {order_id}）："
                              f"{self._redact(exc)}") from exc
        info = dict(raw) if isinstance(raw, dict) else {"message": str(raw)}
        return {
            "ok": bool(info.get("success")),
            "order_sys_id": str(order_id),
            "stock_code": str(stock_code or ""),
            "message": str(info.get("message") or ""),
            "raw": info,
        }

    # ---------- 行情（不在本通道职责内） ----------

    def get_quote(self, symbol: str, date: str, market: str = "cn") -> Optional[Dict[str, Any]]:
        raise BrokerError("QMT 通道不提供行情：行情走 TDX 桥 / quantdb（两条链路分开是刻意设计）")

    def get_klines(self, symbol: str, start: str = "", end: str = "",
                   interval: str = "daily", market: str = "cn") -> List[Dict[str, Any]]:
        raise BrokerError("QMT 通道不提供行情：行情走 TDX 桥 / quantdb（两条链路分开是刻意设计）")


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(QmtBridgeBroker)


register()
