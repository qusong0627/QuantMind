"""通达信（TDX）桥 Broker：Quant-Trader 直连 8550 桥（Windows 交易机）实盘下单。

桥协议（brokers/tdx-bridge/src/api/routes.py，实测）：
  POST /api/v1/plans/execute   下单（TradePlan{plan_id,account,account_type,orders[]}）
  POST /api/v1/account/query   资产+持仓（account 可空，桥 resolve 默认账户）
  POST /api/v1/orders/query    当日委托查询（无历史接口）
  POST /api/v1/orders/cancel   撤单
  POST /api/v1/tdx/call        JSON-RPC 透传（get_market_data/get_market_snapshot 等）
  GET  /api/v1/health          健康检查（免鉴权）
  认证：Authorization: Bearer <token>

配置（.env）：
  TDX_BRIDGE_URL=http://<tdx-bridge-ip>:8550
  TDX_BRIDGE_TOKEN=<64-hex>
  TDX_ACCOUNT=       # 可留空，桥 resolve_account_id 解析默认账户
  TDX_ACCOUNT_TYPE=stock

安全：实盘下单必须过风控（见 docs/ARCHITECTURE_UPGRADE.md §4）。
断线保险：桥 IP 变动（DHCP）导致连接失败时，幂等请求自动触发局网 /24
健康扫描（bridge_discovery）换址重试一次；下单/撤单不自动重试（防重复）。
桥状态码：0=REJECTED 1=SUBMITTED 2=PARTIAL_FILL 3=FILLED 4=PARTIAL_CANCELLED 5=CANCELLED
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_tools.brokers.base import Broker, BrokerError

# 设置页（/api/tdx/config POST）保存的运行时覆盖，优先于 .env；
# config/ 目录容器与宿主机共用挂载，cron 侧与本模块同源解析
_OVERRIDE_FILE = Path(__file__).resolve().parents[2] / "config" / "tdx_bridge.json"

# 提交前价格保护带（2026-09-11 移植 quantmind P2-1）：报价必须落在当日涨跌停带内
# （带外 2% 容差）。柜面对超范围价的容忍 = 当市价单执行，程序/模型 bug 打出离谱
# 价格就等于撤掉了价格保护。**取不到行情、或市价本身就在带外（新股首日无涨跌幅等，
# 本模块不追踪上市日）→ 放行**：不能因为自家涨跌停口径过窄把正常单拒掉。
PRICE_BAND_SLACK = 0.02
_PRICE_REF_TTL = 60   # (昨收, 最新价) 进程内缓存秒数；桥侧 get_market_data 另有 300s 缓存


def bridge_overrides() -> Dict[str, str]:
    """读取 config/tdx_bridge.json 的桥连接覆盖（bridge_url/bridge_token）。"""
    try:
        data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
        return {k: str(v).strip() for k, v in (data or {}).items()
                if k in ("bridge_url", "bridge_token") and str(v or "").strip()}
    except (OSError, json.JSONDecodeError):
        return {}


def _ashare_rules():
    """按需导入 scripts/ashare_rules（板块/涨跌停口径的唯一出处）。

    本模块会被 backend/agent 侧 import，那里 sys.path 不一定含 scripts/。
    """
    import sys

    root = Path(__file__).resolve().parents[2]
    for p in (str(root / "scripts"), str(root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import ashare_rules

    return ashare_rules


# 桥真正支持的周期（实测：count=10000 能拉全历史 1248 根日K）。
# 白名单之外一律抛 BrokerError —— 见 get_klines 的 docstring。
SUPPORTED_INTERVALS = {"daily": "1d", "weekly": "1w"}


class TdxBridgeBroker(Broker):
    """通达信桥。"""

    name = "tdx"
    markets = "cn"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        ov = bridge_overrides()
        self.bridge_url = (self.config.get("bridge_url") or ov.get("bridge_url")
                           or os.getenv("TDX_BRIDGE_URL", "")).rstrip("/")
        self.token = (self.config.get("token") or ov.get("bridge_token")
                      or os.getenv("TDX_BRIDGE_TOKEN", ""))
        self.account = self.config.get("account") or os.getenv("TDX_ACCOUNT", "")
        self.account_type = self.config.get("account_type") or os.getenv("TDX_ACCOUNT_TYPE", "tdx")
        self._disc_tried = False  # 断线自动发现每实例至多一次（防同进程重复扫网）
        self._price_refs: Dict[Any, Any] = {}   # 价格保护带的 (昨收,现价) 缓存
        if not self.bridge_url:
            raise BrokerError("TDX 桥未配置：请设置 TDX_BRIDGE_URL（.env）")

    # ---------- 交易（移植自 quantmind broker_client.py） ----------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _post(self, path: str, payload: Dict[str, Any], timeout: float,
              idempotent: bool = True) -> Dict[str, Any]:
        """统一 POST + 断线自动发现重试（多重保险，见 bridge_discovery）。

        幂等读首次传输层失败（连接/超时）→ 触发桥 IP 自动发现（局网 /24 扫
        8550 健康端点）→ 换址重试一次；非幂等（下单/撤单）绝不自动重试——
        超时≠失败，重复下单不可接受。应用层错误（HTTP 4xx/5xx）不触发发现
        （URL 本身是通的，服务端问题另查）。"""
        import requests

        try:
            resp = requests.post(f"{self.bridge_url}{path}", json=payload,
                                 headers=self._headers(), timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            raise
        except requests.RequestException as exc:
            if not idempotent:
                raise
            found = self._discover_or_none()
            if found and found != self.bridge_url:
                self.bridge_url = found
                try:
                    resp = requests.post(f"{self.bridge_url}{path}", json=payload,
                                         headers=self._headers(), timeout=timeout)
                    resp.raise_for_status()
                    return resp.json()
                except requests.RequestException:
                    pass  # 换址仍失败 → 抛原始异常（贴近故障起点）
            raise

    def _discover_or_none(self) -> Optional[str]:
        """桥失联时自动发现：env/覆盖快探 → 局网扫描（有跨进程冷却），
        命中即全链路收敛（写 config/tdx_bridge.json）。失败返回 None。"""
        if self._disc_tried:
            return None
        self._disc_tried = True
        try:
            from agent_tools.brokers.bridge_discovery import resolve_bridge

            env_url = (os.getenv("TDX_BRIDGE_URL") or "").rstrip("/")
            return resolve_bridge(env_url=env_url, current_url=self.bridge_url) or None
        except Exception:  # noqa: BLE001 发现失败不掩盖原始连接错误
            return None

    def tdx_call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """通用透传：POST /api/v1/tdx/call（桥白名单方法，返回 result 解包）。

        用于盘中五档等数据：get_market_snapshot 返回 Buyp/Buyv/Sellp/Sellv 各 5 档。
        """
        data = self._post("/api/v1/tdx/call",
                          {"method": method, "params": params or {}}, 8)
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def _price_ref(self, symbol: str):
        """(昨收, 最新价)：日K 倒数两根（盘中最后一根即当日实时价）。取不到返回 (None, None)。

        进程内缓存 60 秒——一笔委托一次，同一轮里多笔同票不重复拉桥。
        """
        import time as _time

        key = (symbol, _time.strftime("%Y-%m-%d"))
        now = _time.monotonic()
        hit = self._price_refs.get(key)
        if hit and now - hit[0] < _PRICE_REF_TTL:
            return hit[1], hit[2]
        try:
            bars = self.get_klines(symbol, interval="daily", count=2)
        except Exception:  # noqa: BLE001  桥抖动：调用方按"取不到"放行
            return None, None
        if not isinstance(bars, list) or len(bars) < 2:
            return None, None
        try:
            prev = float(bars[-2].get("close") or 0)
            live = float(bars[-1].get("close") or 0)
        except (TypeError, ValueError):
            return None, None
        if prev <= 0:
            return None, None
        self._price_refs[key] = (now, prev, live)
        return prev, live

    def _price_band_reason(self, symbol: str, price: float) -> str:
        """报价是否明显超出当日涨跌停带 → 原因文案；放行返回 ""。"""
        if not price or float(price) <= 0:
            return ""
        prev, live = self._price_ref(symbol)
        if not prev:
            return ""   # 昨收取不到：交易可用性优先（柜台/交易所仍会兜底）
        rules = _ashare_rules()
        pct = rules.price_limit_pct(symbol)
        down = rules.limit_price(prev, pct, "down")
        up = rules.limit_price(prev, pct, "up")
        if not down or not up:
            return ""
        if live and not (down * (1 - PRICE_BAND_SLACK) <= live <= up * (1 + PRICE_BAND_SLACK)):
            # 市价本身在带外：本模块的涨跌停口径对这只票不成立（新股首日/复牌等）→ 放行
            return ""
        px = float(price)
        if down * (1 - PRICE_BAND_SLACK) <= px <= up * (1 + PRICE_BAND_SLACK):
            return ""
        return (f"报价 ¥{px:.2f} 超出当日涨跌停带 [¥{down:.2f}, ¥{up:.2f}]"
                f"（昨收 ¥{prev:.2f}，现价 ¥{live:.2f}）")

    def _place_order(self, symbol: str, side: str, volume: int,
                     price: Optional[float] = None,
                     plan_id: Optional[str] = None) -> Dict[str, Any]:
        """经桥下单（/api/v1/plans/execute，通达信客户端执行）。

        plan_id：调用方可传幂等委托号（同号重试被桥去重，见 live_price_watch
        ``_watch_plan_id``）；缺省生成纳秒时间戳+pid 的一次性号（每次调用唯一）。
        """
        import time

        import requests

        # account 可空：桥 resolve_account_id 会解析默认账户（实测）
        # 审批门：approval_required=true 时拒绝（backend.yaml risk 段）
        try:
            from agent_tools.risk import RiskPolicy

            if RiskPolicy.from_backend_config().approval_required:
                raise BrokerError("风控审批门开启（approval_required=true），实盘下单被拒绝")
        except BrokerError:
            raise
        except Exception:
            pass

        if price:
            why = self._price_band_reason(symbol, float(price))
            if why:
                raise BrokerError(f"本地价格保护带拒单：{symbol} {side} {why}")

        # 缺省委托号必须**每次调用唯一**（与 QMT 侧 remark 的毫秒后缀同口径）：
        # 旧实现只到秒（baymax_{int(time.time())}_{pid}），同一进程同一秒内下的第二笔
        # 单拿到**同一个 plan 号** → 桥的入口去重（plan_executor.execute_plan:49-52）
        # 判「这个 plan 已执行过」→ 第二笔单根本不会送到柜台，调用方只看到
        # status=duplicate、order_id 空（09:35 调仓/整点轮一轮连下多笔，前一笔成交
        # 回报快时两笔就落在同一秒内）。显式传入的 plan_id 不受影响——那是规则级
        # 幂等键（哨兵 _watch_plan_id），同号重试仍须被桥去重。
        plan_id = plan_id or f"qm_{time.time_ns()}_{os.getpid()}"
        payload = {
            "plan_id": plan_id,
            "account": self.account,
            "account_type": self.account_type,
            "source": "qm-keeper",
            "orders": [{
                "stock_code": symbol,
                "side": side,
                "volume": int(volume),
                "order_type": "limit" if price else "market",
                "price_type": 0 if price else 1,
                "price": float(price) if price else None,
            }],
        }
        try:
            data = self._post("/api/v1/plans/execute", payload, 15, idempotent=False)
        except requests.HTTPError as exc:
            # 409 DUPLICATE_PLAN（routes.py:219）=「这个 plan 已执行过」（入口去重，
            # 见 plan_executor.py:49-52）——不是失败，是**非失败状态**：同号重试
            # 必然走到这里。当异常抛会让哨兵每分钟重报失败、条件位永不消费、
            # 已受理的那笔永远补不进账（2026-09-11 审查 HIGH A）。
            resp = getattr(exc, "response", None)
            try:
                body = resp.json() if resp is not None else {}
            except (ValueError, TypeError):
                body = {}
            err = (body or {}).get("error") if isinstance(body, dict) else {}
            err = err if isinstance(err, dict) else {}
            if resp is not None and resp.status_code == 409 \
                    and str(err.get("code") or "") == "DUPLICATE_PLAN":
                return {"order_id": "", "status": "duplicate",
                        "message": str(err.get("message") or "plan 已执行过（桥去重）"),
                        "plan_id": plan_id}
            raise BrokerError(f"TDX 桥下单失败: {exc}") from exc
        except requests.RequestException as exc:
            raise BrokerError(f"TDX 桥下单失败: {exc}") from exc
        orders = data.get("orders") or []
        first = orders[0] if orders else {}
        status = first.get("status", data.get("status", "unknown"))
        if status in ("rejected", "error"):
            raise BrokerError(first.get("message") or data.get("message") or "TDX 下单被拒")
        order_id = str(first.get("order_id") or "")
        # 受理确认以 order_id 为准：桥回 200 但 orders=[] / 缺号时，单可能已在柜台，
        # 但调用方拿不到号就跟踪不了成交（add_pending 挂不上、reconcile 补记不了）。
        # 此时 message 绝不能写「已受理」——2026-09-11 之前就是这么默认的，
        # 哨兵据此打印 ✅ 并消费止损条件位，成交成了账外单。
        return {
            "order_id": order_id,
            "status": status,
            "message": (first.get("message") or data.get("message")
                        or ("TDX 已受理" if order_id else "桥未返回委托号（受理状态未知）")),
            "plan_id": plan_id,
        }

    def _account_query(self) -> Dict[str, Any]:
        """POST /api/v1/account/query → {account_id, asset, positions, channel_used}"""
        import requests

        try:
            data = self._post("/api/v1/account/query",
                              {"account": self.account,
                               "account_type": self.account_type}, 15)
        except requests.RequestException as exc:
            raise BrokerError(f"TDX 桥账户查询失败: {exc}") from exc
        # 快照缓存（2026-09-15）：供桥断期间的降级监控读最近一次真实账户数据。
        # 只写不读——交易/账户真值判定绝不消费（见 scripts/live_account_cache.py 红线）。
        try:
            import datetime as _dt
            import json as _json
            import time as _time
            from pathlib import Path as _Path

            _root = _Path(__file__).resolve().parents[2]
            _cache = _root / "logs" / "broker_account_cache.json"
            _tmp = _cache.with_name(_cache.name + ".tmp")
            _tmp.write_text(_json.dumps({
                "ts": _time.time(),
                # 2026-09-20 修复：strftime 取机器本地时间（桥宿主 JST）却标 +08:00，
                # 与 scripts/live_account_cache.py 同一份数据，同样显式按 UTC+8 构造。
                "ts_cn": _dt.datetime.now(
                    _dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
                "asset": data.get("asset") or {},
                "positions": data.get("positions") or [],
                "channel_used": data.get("channel_used"),
            }, ensure_ascii=False), encoding="utf-8")
            _tmp.replace(_cache)
        except OSError:
            pass
        return data

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        """实盘持仓 {symbol: total_volume}（桥 account/query 返回 Code/Cbj/TotalVol/CanUseVol）"""
        data = self._account_query()
        positions = {}
        for p in data.get("positions") or []:
            code = p.get("stock_code", "")
            if code:
                positions[code] = float(p.get("total_volume") or 0)
        return positions

    def get_cash(self, signature: str, today_date: str) -> float:
        """实盘可用资金（桥 asset.cash）"""
        data = self._account_query()
        return float((data.get("asset") or {}).get("cash") or 0)

    def get_orders(self, stock_code: str = "", cancelable_only: bool = False) -> List[Dict[str, Any]]:
        """当日委托查询（桥只支持当日，无历史接口）"""
        import requests

        try:
            data = self._post("/api/v1/orders/query",
                              {"account": self.account,
                               "account_type": self.account_type,
                               "stock_code": stock_code,
                               "cancelable_only": cancelable_only}, 15)
            return (data or {}).get("orders") or []
        except requests.RequestException as exc:
            raise BrokerError(f"TDX 桥委托查询失败: {exc}") from exc

    def cancel_order(self, stock_code: str, order_id: str) -> Dict[str, Any]:
        """撤单（当日可撤委托）——非幂等：超时≠成功，不自动重试。"""
        import requests

        try:
            return self._post("/api/v1/orders/cancel",
                              {"account": self.account,
                               "account_type": self.account_type,
                               "stock_code": stock_code,
                               "order_id": order_id}, 15, idempotent=False)
        except requests.RequestException as exc:
            raise BrokerError(f"TDX 桥撤单失败: {exc}") from exc

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None,
            plan_id: Optional[str] = None) -> Dict[str, Any]:
        return self._place_order(symbol, "buy", amount, price, plan_id)

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None,
             plan_id: Optional[str] = None) -> Dict[str, Any]:
        return self._place_order(symbol, "sell", amount, price, plan_id)

    # ---------- 行情（桥协议可直接用） ----------

    def get_quote(self, symbol: str, date: str, market: str = "cn") -> Optional[Dict[str, Any]]:
        klines = self.get_klines(symbol, date, date, interval="daily", market=market)
        return klines[-1] if klines else None

    def get_klines(self, symbol: str, start: str = "", end: str = "",
                   interval: str = "daily", market: str = "cn",
                   count: int = 250) -> List[Dict[str, Any]]:
        """经 8550 桥拉 K 线（POST /api/v1/tdx/call get_market_data）。

        周期白名单只有 `daily`(1d) / `weekly`(1w)。**其它周期抛 BrokerError**，
        不再透传给桥 —— 桥对分钟周期返回的是 `ErrorId=0` + `Value` 空数组（成功码
        配空数据），透传出去就与「这只票停牌」不可区分，调用方会把能力缺失读成
        「今天没数据」。分钟因子请走 scripts/minute_feats.py（桥快照自算）。

        空结果按周期分级：日/周K 空 = 合法（停牌/退市/新股）→ 返回 `[]`；
        非白名单周期 = 能力缺失 → 抛错。这是本方法的契约核心。

        Returns: [{"date","open","high","low","close","volume","amount"}]，按日期升序。
        count：拉取根数（最新 N 根）；价格保护带只要最近两根。
        """
        import requests

        try:
            period = SUPPORTED_INTERVALS[interval]
        except KeyError:
            raise BrokerError(
                f"TDX 桥不支持周期 {interval!r}：可用的是 "
                f"{'/'.join(sorted(SUPPORTED_INTERVALS))}"
                f"（{'/'.join(SUPPORTED_INTERVALS[k] for k in sorted(SUPPORTED_INTERVALS))}）。"
                "桥对分钟周期返回 ErrorId=0 + 空数组，与停牌无法区分；"
                "分钟级特征请用 scripts/minute_feats.py（桥快照自算）。"
            ) from None
        # 实测（桥调用日志 18328）：参数名是 stock_list（列表），不是 stock_code
        params: Dict[str, Any] = {
            "stock_list": [symbol],
            "period": period,
            "dividend_type": "front",  # 前复权，与本地价格数据口径一致
            "count": int(count),
        }
        try:
            data = self._post("/api/v1/tdx/call",
                              {"method": "get_market_data", "params": params}, 20)
        except requests.RequestException as exc:
            raise BrokerError(f"TDX 桥请求失败: {exc}") from exc
        if not data.get("success", True):
            err = data.get("error") or {}
            raise BrokerError(f"TDX 桥返回错误: {err.get('message', str(data)[:200])}")
        # tdx/call 返回 {success, result: {ErrorId, Value: {symbol: {...}}}}
        result = data.get("result") or {}
        if str(result.get("ErrorId", "0")) != "0":
            raise BrokerError(f"TDX 行情错误 ErrorId={result.get('ErrorId')}: {str(result)[:200]}")
        value = result.get("Value") or {}
        kline = value.get(symbol) if isinstance(value, dict) and symbol in value else result
        closes = kline.get("Close") or []
        dates = kline.get("Date") or [""] * len(closes)
        bars = []
        for i in range(len(closes)):
            bars.append({
                "date": str(dates[i]) if i < len(dates) else "",
                "open": self._at(kline.get("Open"), i),
                "high": self._at(kline.get("High"), i),
                "low": self._at(kline.get("Low"), i),
                "close": closes[i],
                "volume": self._at(kline.get("Volume"), i),
                "amount": self._at(kline.get("Amount"), i),
            })
        return bars

    @staticmethod
    def _at(lst, i):
        try:
            return lst[i] if lst and i < len(lst) else None
        except Exception:
            return None


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(TdxBridgeBroker)


register()
