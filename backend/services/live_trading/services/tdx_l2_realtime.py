"""TDX L2 实时推理任务 — 13 因子截面标准化 → ICIR 加权 → 实时胜率分 → 自动买卖。

信号合成（对应当日推理缓存的日频分）:
    signal_score = 100 / (1 + e^(-z_combined))          # 0~100
    z_combined  = Σ(w_i · z_i) / Σw_i                   # 池内截面 z-score × ICIR 权重
    realtime_score = 0.6 × (fusion/3×100) + 0.4 × signal_score

触发（全部 Redis tdx:l2:config 可调）:
    买入: realtime_score > buy_trigger(65) 且 大盘>MA20 且 未持仓 且 冷却已过
    卖出: realtime_score < sell_trigger(45) 且 available_volume>0 (T+1 已解锁)
    冷却: 每只 cooldown_min(30) 分钟（Redis tdx:l2:cooldown:{symbol}）

执行复用 TdxRollingTradeService（三档模式 + 会员门控）:
    tdx  = place_rolling_orders（TQ 收费账号 Value=2 直接提交免确认）
    paper= place_paper_orders（本地模拟盘免确认）
    off  = 仅更新分数不执行
"""
import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any

from backend.services.live_trading.services.tdx_l2_capture_task import (
    _CONFIG_KEY,
    _REALTIME_KEY,
    _REALTIME_MAX_AGE_SEC,
    FACTOR_ICIR,
)
from backend.services.live_trading.services.tdx_exec_alerts import (
    FAMILY_L2,
    alert_exec_leg,
    l2_alerts,
    l2_cycle_is_clean,
    resolve_exec_leg,
)
from backend.services.live_trading.services.tdx_push_service import tdx_pusher
from backend.services.live_trading.services.trading_session import (
    is_trading_time,
    split_session_refusals,
)
from backend.services.trade_shared.redis_client import redis_client as trade_redis
from backend.shared.simulation_account_keys import resolve_db_account_user
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": False,          # 默认关，手动开启（安全）
    "pool_size": 20,
    "buy_trigger": 65.0,
    "sell_trigger": 45.0,
    "interval_sec": 60,
    "cooldown_min": 30,
    "daily_weight": 0.6,       # 日频分权重
    "signal_weight": 0.4,      # 实时信号分权重
    "factor_weights": None,    # None = 用回测 ICIR 权重 FACTOR_ICIR
}

_STATUS_KEY = "tdx:l2:realtime:status"
# 状态键是**活性键**：循环活着就每周期重写，死了就过期消失。
# 「键不存在」与「从未跑过」语义相同（都是"没在跑"），故用短 TTL 而非长留存——
# 长 TTL 会让一份早已停摆的状态看起来还活着。
_STATUS_TTL_CYCLES = 10
_STATUS_TTL_FLOOR = 600
_SCORE_KEY = "tdx:l2:score:{symbol}"
_COOLDOWN_KEY = "tdx:l2:cooldown:{symbol}"
_INFLIGHT_KEY = "tdx:l2:inflight:{symbol}"
_ORDER_QUOTE_KEY = "tdx:l2:order_quote:{order_id}"   # 每笔委托的决策时点行情（7 天）
_QUOTE_KEY = "tdx:l2:quotes:{symbol}"                # 每只标的最近一次委托行情（24h）
_FACTOR_LIST = list(FACTOR_ICIR.keys())

# ---- 在途单重挂参数 ----
_INFLIGHT_TTL = 4 * 3600     # 在途记录 4h 兜底过期
_ORDER_QUOTE_TTL = 7 * 3600  # 委托时点行情 7 天（交易记录回看窗口）
_QUOTE_TTL = 24 * 3600
_RETRY_STALE_SEC = 90        # 挂单超过该时长仍无成交 → 撤旧重挂
_MAX_RETRIES = 10            # 单标的单方向重挂上限（避免死循环）
_FILLED_STATUSES = {"filled"}
_WORKING_STATUSES = {"submitted", "partial_fill"}

realtime_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_cycle_at": None,
    "last_error": None,
    "last_buys": [],
    "last_sells": [],
    "pool_size": 0,
    "scored": 0,
    "bridge_ok": True,
    "capture_stale": False,
    "inflight": {},
    "retry_stats": {},
    # —— 下列字段是**本周期**的事实（每周期起点清空，见主循环）：状态面板与告警面
    # 读同一份，留着上一轮的值会让「这一轮好了」永远看不出来（恢复通知就发不出去）。
    "cycle_error": None,            # 本周期整体异常
    "positions_error": None,        # 持仓读不到（⇒ 触发与重挂整体跳过）
    "trigger_skipped_symbols": 0,   # 因持仓读不到而未做触发判断的标的数
    "session_skipped_symbols": 0,   # 因非交易时段（tdx 模式）而未做触发判断的标的数
    "exec_error": None,             # 执行段没能执行
    "orders_failed": [],            # 本周期下单失败的腿（最多 _MAX_LISTED_FAILURES 条）
    "orders_failed_count": 0,
    "pool_starved_cycles": 0,       # 因子池连续不足的周期数（≥阈值=停摆）
    "in_trading_hours": False,
}

#: 本进程当天推过的告警键（``seen``：见 ``shared.alert_delivery.deliver_alert``）。
#: 循环每 ~60s 跑一次，而 Redis 读不出来时只靠 Redis 去重键会退化成「每周期推一条」。
#: 键里自带交易日，故跨日自然失效，不需要清理。
_ALERTED_KEYS: set[str] = set()

#: 状态镜像里最多带几条失败腿（面板要能一屏读完；总数在 orders_failed_count）。
_MAX_LISTED_FAILURES = 5

#: 因子池不足的下限（与主循环里的判据同源，提到模块级给状态字段用）。
_MIN_POOL_SIZE = 5


def _capture_is_stale(interval: float) -> bool:
    """采集链路新鲜度：capture 任务 last_cycle_at 距今超过 3 个周期视为陈旧。

    与 capture 任务同进程, 直接读模块状态, 零额外调用。陈旧时只评分不触发,
    避免拿断裂链路残留的陈旧 L2 因子下单。
    """
    try:
        from backend.services.live_trading.services.tdx_l2_capture_task import l2_status

        last = l2_status.get("last_cycle_at")
        if not last:
            return True
        age = (datetime.now() - datetime.fromisoformat(str(last))).total_seconds()
        return age > max(interval * 3, 300)
    except Exception:
        # fail-closed：本函数的存在意义就是「避免拿断裂链路残留的陈旧 L2 因子下单」，
        # 读不到状态时返回 False（=不陈旧）恰好放行了下单，与自身目的相反。
        # 返回 True 只阻断**触发执行**，评分照常（见调用点注释）。
        return True


def _payload_age_sec(payload: dict[str, Any]) -> float | None:
    """单条实时因子 payload 的年龄（秒）；``ts`` 缺失/不可解析 → None。

    ``ts`` 由采集任务以 ``datetime.now().isoformat(timespec="seconds")`` 写入
    （naive 上海墙钟），读写同进程同钟，故直接用 naive ``datetime.now()`` 作差。
    出现 aware 时间戳时归一到本地钟再比（防御性，正常路径不会走到）。
    """
    raw = str(payload.get("ts") or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return (datetime.now() - ts).total_seconds()  # noqa: DTZ005 — naive 上海墙钟正是契约本身（写侧写它、读侧同钟比较）


# ============ 配置 ============

def load_l2_config() -> dict[str, Any]:
    cfg = dict(_CONFIG_DEFAULTS)
    if trade_redis.client is not None:
        saved = trade_redis.get(_CONFIG_KEY) or {}
        if isinstance(saved, dict):
            cfg.update(saved)
    # 清理遗留因子
    if cfg.get("factor_weights") is not None and not isinstance(cfg["factor_weights"], dict):
        cfg["factor_weights"] = None
    return cfg


def save_l2_config(updates: dict[str, Any]) -> dict[str, Any]:
    """更新 L2 实时配置（Redis），返回保存后的完整配置。"""
    cfg = load_l2_config()
    allowed = set(_CONFIG_DEFAULTS.keys())
    for k, v in updates.items():
        if k not in allowed:
            continue
        if k in ("buy_trigger", "sell_trigger", "interval_sec", "cooldown_min"):
            cfg[k] = float(v)
        elif k == "pool_size":
            cfg[k] = max(5, min(int(v), 50))
        elif k in ("daily_weight", "signal_weight"):
            cfg[k] = float(v)
        elif k == "enabled":
            cfg[k] = bool(v)
        elif k == "factor_weights" and isinstance(v, dict):
            cfg[k] = {kk: float(vv) for kk, vv in v.items() if kk in FACTOR_ICIR}
    if trade_redis.client is not None:
        trade_redis.set(_CONFIG_KEY, cfg)
    return cfg


# ============ 分数合成 ============

def _z_score(values: dict[str, float]) -> dict[str, float]:
    """截面 z-score（clip ±3），标准差为 0 时归零。"""
    vs = list(values.values())
    n = len(vs)
    if n < 3:
        return dict.fromkeys(values, 0.0)
    mean = sum(vs) / n
    var = sum((v - mean) ** 2 for v in vs) / (n - 1)
    std = math.sqrt(var)
    if std < 1e-12:
        return dict.fromkeys(values, 0.0)
    return {k: max(-3.0, min(3.0, (v - mean) / std)) for k, v in values.items()}


def compute_signal_scores(
    pool_factors: dict[str, dict[str, float | None]], factor_weights: dict[str, float] | None
) -> dict[str, float]:
    """池内截面标准化 + ICIR 加权 + sigmoid → 0~100 实时信号分。

    pool_factors: {symbol: {factor: value}}（13 因子原始值，None=样本不足跳过）
    factor_weights: None = 用回测 ICIR 权重；否则用自定义 {factor: weight}
    """
    weights = factor_weights or FACTOR_ICIR
    w_sum = sum(weights.values()) + 1e-9
    symbols = list(pool_factors.keys())
    signal: dict[str, float] = {}
    for sym in symbols:
        z_total = 0.0
        for factor, w in weights.items():
            # 该因子在池内所有有效样本上的截面 z（缺失/None 的股票按 0 处理）
            vals = {
                s: pf[factor]
                for s, pf in pool_factors.items()
                if isinstance(pf.get(factor), (int, float))
            }
            if len(vals) < 3:
                continue
            z = _z_score(vals)
            z_total += w * z.get(sym, 0.0)
        z = max(-3.0, min(3.0, z_total / w_sum))
        signal[sym] = round(100.0 / (1.0 + math.exp(-z)), 2)
    return signal


def compute_realtime_score(fusion_score: float, signal_score: float, daily_weight: float = 0.6, signal_weight: float = 0.4) -> float:
    """实时胜率分 = 日频分(0~100) × daily_weight + 信号分 × signal_weight。"""
    daily = (fusion_score / 3.0 * 100.0) if fusion_score else 50.0
    return round(daily_weight * daily + signal_weight * signal_score, 2)


# ============ 冷却 ============

def _cooldown_key(symbol: str) -> str:
    return _COOLDOWN_KEY.format(symbol=symbol.lower())


def is_cooldown(symbol: str, cooldown_min: float) -> bool:
    if trade_redis.client is None or cooldown_min <= 0:
        return False
    ts = trade_redis.get(_cooldown_key(symbol))
    if not ts:
        return False
    return (time.time() - float(ts)) < cooldown_min * 60


def set_cooldown(symbol: str, cooldown_min: float) -> None:
    if trade_redis.client is None:
        return
    trade_redis.set(_cooldown_key(symbol), time.time())


# ============ 在途单（未成交重挂/撤单） ============

def _inflight_key(symbol: str) -> str:
    return _INFLIGHT_KEY.format(symbol=symbol.lower())


def load_inflight(symbol: str) -> dict | None:
    """读取在途记录；无 Redis 或无记录返回 None。"""
    if trade_redis.client is None:
        return None
    v = trade_redis.get(_inflight_key(symbol))
    return v if isinstance(v, dict) else None


def save_inflight(symbol: str, payload: dict) -> None:
    """写入在途记录（含 TTL 兜底，防止孤儿记录）。"""
    if trade_redis.client is None:
        return
    trade_redis.set(_inflight_key(symbol), payload)
    expire = getattr(trade_redis.client, "expire", None)
    if callable(expire):
        expire(_inflight_key(symbol), _INFLIGHT_TTL)


def clear_inflight(symbol: str) -> None:
    if trade_redis.client is None:
        return
    delete = getattr(trade_redis.client, "delete", None)
    if callable(delete):
        delete(_inflight_key(symbol))


def list_inflight() -> dict[str, dict]:
    """扫描全部在途记录 {symbol: payload}。

    key 中的 symbol 是小写存储, 必须以 payload 里的原始前缀为准,
    否则重挂逻辑拿小写 key 去比大写信号池会误判"信号消失"而撤单。
    """
    if trade_redis.client is None:
        return {}
    out: dict[str, dict] = {}
    for key in trade_redis.client.scan_iter(match=_INFLIGHT_KEY.format(symbol="*")):
        v = trade_redis.get(key)
        if not isinstance(v, dict):
            continue
        sym = str(v.get("symbol") or "").strip() or str(key).rsplit(":", 1)[-1].upper()
        if sym:
            out[sym] = v
    return out


# ============ 委托时点行情（"什么点买的"数据源） ============

def save_order_quote(
    *,
    symbol: str,
    order_id: str,
    plan_id: str,
    side: str,
    volume: int,
    amount: float,
    quote_price: float,
    name: str = "",
    market_detail: str | None = None,
    index_above: bool | None = None,
) -> None:
    """保存下单/重挂时刻的实时行情：成交价参考、决策时点、大盘点位。

    每笔委托一条 `order_quote:{order_id}`（7 天，交易记录按委托编号回查）
    + 每只标的最近一条 `quotes:{symbol}`（24h，待成交面板速览）。
    纯 Redis 无桥依赖；Redis 不可用时静默跳过。
    """
    if trade_redis.client is None:
        return
    payload = {
        "symbol": symbol,
        "order_id": str(order_id or ""),
        "plan_id": str(plan_id or ""),
        "side": str(side or "buy"),
        "name": str(name or ""),
        "volume": int(volume or 0),
        "amount": round(float(amount or 0), 2),
        "quote_price": round(float(quote_price or 0), 3),
        "index_above": bool(index_above),
        "market_detail": str(market_detail or ""),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    expire = getattr(trade_redis.client, "expire", None)
    if order_id:
        key = _ORDER_QUOTE_KEY.format(order_id=order_id)
        trade_redis.set(key, payload)
        if callable(expire):
            expire(key, _ORDER_QUOTE_TTL)
    key = _QUOTE_KEY.format(symbol=symbol.lower())
    trade_redis.set(key, payload)
    if callable(expire):
        expire(key, _QUOTE_TTL)


def load_order_quotes() -> dict[str, dict]:
    """读取全部委托时点行情 {order_id: payload}。"""
    if trade_redis.client is None:
        return {}
    out: dict[str, dict] = {}
    for key in trade_redis.client.scan_iter(match=_ORDER_QUOTE_KEY.format(order_id="*")):
        v = trade_redis.get(key)
        if isinstance(v, dict) and v.get("order_id"):
            out[str(v["order_id"])] = v
    return out


def load_symbol_quotes() -> dict[str, dict]:
    """读取每只标的最近一次委托行情 {symbol: payload}。"""
    if trade_redis.client is None:
        return {}
    out: dict[str, dict] = {}
    for key in trade_redis.client.scan_iter(match=_QUOTE_KEY.format(symbol="*")):
        v = trade_redis.get(key)
        if not isinstance(v, dict):
            continue
        sym = str(v.get("symbol") or str(key).rsplit(":", 1)[-1])
        if sym:
            out[sym] = v
    return out


def merge_order_states(today_orders: list[dict]) -> None:
    """把桥当日委托的最新状态（成交均价/成交量/状态）合并进时点行情记录。

    成交后 order_quote:{order_id} 同时保留决策时行情 quote_price 和实际
    成交均价 filled_price —— "什么点买的 + 什么价成交"双口径，供交易记录展示。
    """
    if trade_redis.client is None or not today_orders:
        return
    quotes = load_order_quotes()
    if not quotes:
        return
    expire = getattr(trade_redis.client, "expire", None)
    for o in today_orders:
        oid = str(o.get("order_id") or "")
        rec = quotes.get(oid)
        if not rec:
            continue
        dirty = False
        for field in ("status", "filled_volume", "filled_price", "order_price"):
            v = o.get(field)
            if v is not None and rec.get(field) != v:
                rec[field] = v
                dirty = True
        if not dirty:
            continue
        key = _ORDER_QUOTE_KEY.format(order_id=oid)
        trade_redis.set(key, rec)
        if callable(expire):
            expire(key, _ORDER_QUOTE_TTL)
        sym_key = _QUOTE_KEY.format(symbol=str(rec.get("symbol") or "").lower())
        trade_redis.set(sym_key, rec)
        if callable(expire):
            expire(sym_key, _QUOTE_TTL)


async def _retry_inflight_orders(
    svc,
    *,
    signal_scores: dict[str, float],
    pool_data: dict[str, dict],
    fixed_buy_amount: float,
    cooldown_min: float,
    today_orders: list[dict],
    market_detail: str | None = None,
    index_above: bool | None = None,
) -> dict:
    """未成交在途单重挂：已成→清档; 挂单中→等待; 废单/已撤/超时→撤旧重挂。

    铁律（不能多）：同标的同方向同一时刻最多一张活单 —— 只有确认旧单已
    废/已撤/超时未成交才撤旧并换新 plan_id 重挂; 网络不确定时先查当日委托
    认领新单, 不盲目重发。
    铁律（不能漏）：确认未成且信号仍在 → 持续重挂直到成交/撤单/放弃。
    返回统计: {"waiting","cleared","cancelled","resubmitted","given_up"}。
    """
    stats = {"waiting": 0, "cleared": [], "cancelled": [], "resubmitted": [], "given_up": []}
    inflight = list_inflight()
    if not inflight:
        return stats
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        LOT_SIZE,
    )

    by_code: dict[str, list[dict]] = {}
    for o in today_orders:
        code = StockCodeUtil.to_prefix(str(o.get("stock_code") or ""))
        by_code.setdefault(code, []).append(o)

    for sym, rec in inflight.items():
        side = str(rec.get("side") or "buy")
        volume = int(rec.get("volume") or 0)
        order_id = str(rec.get("order_id") or "")
        retries = int(rec.get("retries") or 0)
        age = time.time() - float(rec.get("ts") or 0)
        mine = [o for o in by_code.get(sym, []) if o.get("side") == side]
        target = next((o for o in mine if str(o.get("order_id")) == order_id), None) or (mine[0] if mine else None)
        status = str(target.get("status") or "") if target else ""
        filled = float(target.get("filled_volume") or 0) if target else 0.0

        # 已成 → 清档 + 冷却（冷却从实际成交起算, 防同一信号反复触发）
        if target and (status in _FILLED_STATUSES or filled >= volume):
            clear_inflight(sym)
            set_cooldown(sym, cooldown_min)
            stats["cleared"].append(sym)
            continue
        # 信号消失 → 撤单清档（仍在挂的单收掉, 不追涨杀跌）
        if sym not in signal_scores:
            if target and status in _WORKING_STATUSES:
                try:
                    await svc.cancel_order(sym, order_id or str(target.get("order_id") or ""))
                except Exception as exc:
                    logger.warning("[TdxL2] %s 信号消失撤单失败: %s", sym, exc)
            clear_inflight(sym)
            stats["cancelled"].append(sym)
            continue
        # 挂单中且未超时 → 等待撮合
        if target and status in _WORKING_STATUSES and age < _RETRY_STALE_SEC:
            stats["waiting"] += 1
            continue
        # 重挂次数用尽 → 放弃（避免无限重挂）
        if retries >= _MAX_RETRIES:
            clear_inflight(sym)
            stats["given_up"].append(sym)
            continue
        # 到这里: 废单/已撤/查无此单/超时未成交 → 撤旧（若仍挂）→ 新价重挂
        target_id = order_id or (str(target.get("order_id") or "") if target else "")
        if target and status in _WORKING_STATUSES:
            try:
                await svc.cancel_order(sym, target_id)
            except Exception as exc:
                logger.warning("[TdxL2] %s 超时撤单失败(下一轮再试): %s", sym, exc)
                continue
            # 撤单后复核：撤销前可能已成交（撤单失败/刚好撮合）
            try:
                after = await svc.pull_today_orders(sym)
            except Exception:
                after = []
            still = [o for o in after if str(o.get("order_id")) == target_id]
            if still:
                s2 = str(still[0].get("status") or "")
                if s2 in _FILLED_STATUSES or float(still[0].get("filled_volume") or 0) >= volume:
                    clear_inflight(sym)
                    set_cooldown(sym, cooldown_min)
                    stats["cleared"].append(sym)
                    continue
        # 用最新实时价重挂
        price = float(pool_data.get(sym, {}).get("now") or 0)
        if side == "buy":
            if price <= 0:
                logger.warning("[TdxL2] %s 重挂缺实时价, 本轮跳过", sym)
                continue
            new_volume = int((float(fixed_buy_amount or 0) / price) // LOT_SIZE) * LOT_SIZE
            if new_volume < LOT_SIZE:
                clear_inflight(sym)  # 金额已不够一手 → 放弃
                stats["given_up"].append(sym)
                continue
        else:
            new_volume = volume
        if new_volume <= 0:
            clear_inflight(sym)
            stats["given_up"].append(sym)
            continue
        new_plan_id = f"rolling_l2_{sym}_{side}_{int(time.time() * 1000)}"
        # 风控闸（T-RC-02b 覆盖扩展）：重挂同样过闸（影子期不拦；fail-closed 拒单）
        try:
            from backend.services.trade.services.risk_gate_service import (
                check_direct_order as _risk_check,
            )
            from backend.shared.simulation_account_keys import resolve_db_account_user

            risk = await _risk_check(
                tenant_id="default",
                user_id=resolve_db_account_user("TDX_ACCOUNT_USER_ID"),
                symbol=sym,
                side=side,
                quantity=new_volume,
                price=price or None,
                order_type="market",
                source="tdx_l2",
                remarks=new_plan_id,
            )
        except Exception as exc:  # noqa: BLE001 - 闸不可用=fail-closed
            logger.warning("[TdxL2] %s 风控闸不可用（fail-closed，本轮跳过）: %s", sym, exc)
            continue
        if not risk.passed:
            logger.warning(
                "[TdxL2] %s 风控拒单[%s]: %s", sym, risk.rule_id or "risk", risk.reason
            )
            continue
        try:
            resp = await tdx_pusher.place_order(
                stock_code=sym, side=side, volume=new_volume,
                price=price or None, plan_id=new_plan_id,
            )
        except Exception as exc:
            # 网络不确定: 查当日委托认领新单, 没有则下轮再试
            logger.warning("[TdxL2] %s 重挂下单异常(待认领): %s", sym, exc)
            try:
                after = await svc.pull_today_orders(sym)
            except Exception:
                after = []
            fresh = [o for o in after if str(o.get("plan_id")) == new_plan_id]
            if not fresh:
                continue
            first = fresh[0]
            save_inflight(sym, {
                "symbol": sym, "side": side, "volume": int(first.get("total_volume") or new_volume),
                "order_id": str(first.get("order_id") or ""), "plan_id": new_plan_id,
                "ts": time.time(), "retries": retries + 1,
            })
            save_order_quote(
                symbol=sym, order_id=str(first.get("order_id") or ""),
                plan_id=new_plan_id, side=side,
                volume=int(first.get("total_volume") or new_volume),
                amount=float(price or 0) * int(first.get("total_volume") or new_volume),
                quote_price=price, market_detail=market_detail,
                index_above=index_above,
            )
            stats["resubmitted"].append(sym)
            continue
        first = (resp.get("orders") or [{}])[0] if isinstance(resp, dict) else {}
        order_status = str(first.get("status") or resp.get("status") or "submitted")
        new_order_id = str(first.get("order_id") or "")
        if order_status in ("rejected", "error") or (isinstance(resp, dict) and resp.get("status") == "duplicate"):
            # 被拒/重复 → 保留在途, 下轮再试
            save_inflight(sym, {
                "symbol": sym, "side": side, "volume": volume, "order_id": new_order_id,
                "plan_id": new_plan_id, "ts": time.time(), "retries": retries + 1,
            })
            continue
        save_inflight(sym, {
            "symbol": sym, "side": side, "volume": new_volume, "order_id": new_order_id,
            "plan_id": new_plan_id, "ts": time.time(), "retries": retries + 1,
        })
        save_order_quote(
            symbol=sym, order_id=new_order_id, plan_id=new_plan_id, side=side,
            volume=new_volume, amount=float(price or 0) * new_volume,
            quote_price=price, market_detail=market_detail, index_above=index_above,
        )
        stats["resubmitted"].append(sym)
    return stats


# ============ 执行 ============

async def _execute_signals(
    *,
    tenant_id: str,
    user_id: str,
    run_id: str,
    buys: list[dict[str, Any]],
    sells: list[dict[str, Any]],
    execute_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """按三档模式执行下单，返回 (placed, failed, error)。"""
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        TdxRollingTradeService,
    )

    svc = TdxRollingTradeService()
    if execute_mode == "paper":
        placed, failed = await svc.place_paper_orders(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, buys=buys, sells=sells
        )
        return placed, failed, None
    if execute_mode == "tdx":
        placed, failed = await svc.place_rolling_orders(
            run_id=run_id, buys=buys, sells=sells,
            tenant_id=tenant_id, user_id=user_id, source="tdx_l2",
        )
        return placed, failed, None
    return [], [], f"execute_mode={execute_mode} 仅预警不执行"


async def run_tdx_l2_realtime_task(interval_sec: int = 0) -> None:
    """L2 实时推理主循环：读采集因子 → 截面标准化 → 合成 → 触发买卖。"""
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        TdxRollingTradeService,
        load_rolling_config,
    )

    svc = TdxRollingTradeService()
    # 账户名唯一口径：规范名 10000001（前端配置/分数/模拟盘均按规范名读写）
    tenant_id, user_id = "default", resolve_db_account_user("TDX_ACCOUNT_USER_ID")
    base_interval = float(interval_sec or 0)
    # 先给兜底值：首周期若在读到配置前就异常，状态镜像算 TTL 也要有 interval 可用
    # （此前它在 try 内赋值，首周期异常时镜像那行会 NameError——被自己的 except 吞掉，
    # 表现为「刚启动出错时面板空白」）。
    interval = base_interval or 60.0
    realtime_status["running"] = True
    realtime_status["started_at"] = datetime.now().isoformat(timespec="seconds")
    logger.info("[TdxL2] 实时推理任务启动")

    async def _publish_cycle_status() -> None:
        """状态镜像 + 失败可见性。**每个周期结束都要走**（含提前 continue 的分支）。

        镜像在先：通知里说的那份状态落地时，面板上已经是同一份。
        去重设施不可用（``trade_redis.client is None``）时**不推**——循环每 ~60s
        一次，没有进程内 ``seen`` 的去重等于每周期推一条（那会把通知刷成噪音）；
        状态键同样写不了，故障仍在日志里。宁可这条不推，也不刷屏。
        """
        if trade_redis.client is None:
            return
        try:
            trade_redis.set(
                _STATUS_KEY,
                dict(realtime_status),
                ttl=max(_STATUS_TTL_CYCLES * int(interval), _STATUS_TTL_FLOOR),
            )
        except Exception as exc:  # noqa: BLE001 — 镜像失败绝不影响主循环
            logger.warning("[TdxL2] 状态镜像写入失败: %s", exc)
        try:
            alerts = l2_alerts(realtime_status)
            if alerts:
                await alert_exec_leg(
                    FAMILY_L2,
                    alerts,
                    user_id=user_id,
                    redis=trade_redis,
                    seen=_ALERTED_KEYS,
                )
            elif l2_cycle_is_clean(realtime_status):
                # 「alerts 为空」不等于「这一周期是好的」：盘外的每个周期都没做
                # 触发判断（那件事被时段门压住、本来就不报），盘外的 tdx 周期更
                # 是结构上不可能成交。恢复要等一个真能执行的周期来宣布。
                await resolve_exec_leg(
                    FAMILY_L2,
                    user_id=user_id,
                    redis=trade_redis,
                    seen=_ALERTED_KEYS,
                )
        except Exception as exc:  # noqa: BLE001 — 告警面故障不许带走循环
            logger.warning("[TdxL2] 失败可见性推送异常: %s", exc, exc_info=True)

    while True:
        cycle_start = time.monotonic()
        buys_all: list[dict[str, Any]] = []
        sells_all: list[dict[str, Any]] = []
        error = None
        failed: list[dict[str, Any]] = []
        exec_error: str | None = None
        positions_error: str | None = None
        trigger_skipped = 0
        session_skipped = 0
        in_trading_hours = is_trading_time()
        # 本周期字段清零：告警/恢复通知靠「这一轮干不干净」判定，留着上一轮的值
        # 会让干净的一轮看起来还在坏（恢复通知永远发不出去）。
        realtime_status.update(
            {
                "cycle_error": None,
                "positions_error": None,
                "trigger_skipped_symbols": 0,
                "session_skipped_symbols": 0,
                "exec_error": None,
                "orders_failed": [],
                "orders_failed_count": 0,
                "in_trading_hours": in_trading_hours,
            }
        )
        try:
            cfg = load_l2_config()
            interval = float(cfg.get("interval_sec") or base_interval or 60)
            if not cfg.get("enabled"):
                await asyncio.sleep(min(interval, 30))
                continue

            # 1. 采集链路新鲜度（同进程读 capture 状态；只影响触发, 不阻断评分）
            capture_stale = _capture_is_stale(interval)

            # 2. 读采集任务写的全部实时因子（纯 Redis, 不依赖桥）
            if trade_redis.client is None:
                await asyncio.sleep(interval)
                continue
            keys = trade_redis.client.scan_iter(match=_REALTIME_KEY.format(symbol="*"))
            pool_data: dict[str, dict[str, Any]] = {}
            stale_skipped = 0
            for key in keys:
                # 本模块每周期写的状态键 `tdx:l2:realtime:status` **就在池的扫描范围内**
                # （前缀相同，见文件头 _STATUS_KEY）。排除掉，否则它会被当成一只标的
                # 参与截面标准化——目前只是靠「状态字典里没有 symbol 字段」侥幸幸免，
                # 真钱键空间上不留这种巧合。
                if key == _STATUS_KEY:
                    continue
                payload = trade_redis.get(key)
                if not isinstance(payload, dict):
                    continue
                factors = payload.get("factors") or {}
                if not isinstance(factors, dict):
                    continue
                sym = str(payload.get("symbol") or "")
                if not sym:
                    continue
                # 新鲜度闸门：截面标准化的分母是**同一时刻的**那批标的，
                # 混进陈料会让今日每只标的的 z 都建立在跨会话的分布上。
                # 取不到 ts 的按陈旧处理（fail-closed）——宁可少一只，不可偏一池。
                age = _payload_age_sec(payload)
                if age is None or age > _REALTIME_MAX_AGE_SEC:
                    stale_skipped += 1
                    continue
                pool_data[sym] = payload
            if stale_skipped and realtime_status.get("pool_stale_skipped") != stale_skipped:
                logger.warning(
                    "[TdxL2] 因子池丢弃 %d 个陈旧键（>%ds）——采集链路可能已部分停摆",
                    stale_skipped,
                    _REALTIME_MAX_AGE_SEC,
                )
            realtime_status["pool_stale_skipped"] = stale_skipped
            pool_factors = {s: p.get("factors") or {} for s, p in pool_data.items()}
            realtime_status["pool_size"] = len(pool_factors)
            if len(pool_factors) < _MIN_POOL_SIZE:
                # 因子池不足：采集链路可能中断。评分无法进行, 状态如实标记。
                # 连续计数是给告警用的：开盘头几分钟池子还在构建，只有"连续 N 个
                # 周期都不足"才叫停摆（单周期不足报警是每天一次的假警）。
                starved = int(realtime_status.get("pool_starved_cycles") or 0) + 1
                realtime_status.update(
                    {
                        "capture_stale": capture_stale,
                        "bridge_ok": not capture_stale,
                        "pool_starved_cycles": starved,
                        "last_error": (
                            f"L2 因子池不足 {_MIN_POOL_SIZE} 只（采集链路可能中断）"
                            if capture_stale
                            else None
                        ),
                        "last_cycle_at": realtime_status.get("last_cycle_at"),
                    }
                )
                await _publish_cycle_status()
                await asyncio.sleep(interval)
                continue
            realtime_status["pool_starved_cycles"] = 0

            # 3. 信号分 + 日频融合分（engine 失败降级中性分, 不阻断评分）
            signal_scores = compute_signal_scores(pool_factors, cfg.get("factor_weights"))
            try:
                _, fusion_scores, _ = await svc.load_latest_scores(
                    tenant_id=tenant_id, user_id=user_id
                )
            except Exception as exc:
                logger.warning("[TdxL2] 拉取日频融合分失败(降级中性50): %s", exc)
                fusion_scores = {}
            fusion_scores = fusion_scores or {}

            # 4. 评分写入 Redis（纯 Redis 操作, 桥断/engine 断推理照常更新）
            buy_trigger = float(cfg.get("buy_trigger") or 65)
            sell_trigger = float(cfg.get("sell_trigger") or 45)
            cooldown_min = float(cfg.get("cooldown_min") or 30)
            daily_w = float(cfg.get("daily_weight") or 0.6)
            signal_w = float(cfg.get("signal_weight") or 0.4)
            scored = 0
            for sym, signal in signal_scores.items():
                fusion = fusion_scores.get(sym)
                realtime_score = compute_realtime_score(
                    fusion or 0.0, signal, daily_w, signal_w
                )
                redis_payload = {
                    "symbol": sym,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "realtime_score": realtime_score,
                    "signal_score": signal,
                    "fusion_score": fusion,
                    "trigger": "buy" if realtime_score > buy_trigger else "sell" if realtime_score < sell_trigger else "hold",
                }
                if trade_redis.client is not None:
                    trade_redis.set(_SCORE_KEY.format(symbol=sym), redis_payload)
                scored += 1
            realtime_status["scored"] = scored

            # 5. 三档模式（会员门控已移除，全部登录用户可执行）
            _, fixed_buy_amount, execute_mode = load_rolling_config(tenant_id, user_id)
            # 真单闸门：本循环 24/7 常驻，而 A 股委托只在交易时段有效
            # （09:15–11:35 / 12:55–15:05）。非交易时段的真单要么被柜台拒、要么
            # 被客户端挂成次日单——"现在的分"变成"明天的无主委托"。paper 是本地
            # 撮合（盘后演练正需要它），不受限。物理出口另有一道闸
            # （tdx_push_service.place_order），这里这道负责不动 + 如实登记。
            real_orders_allowed = in_trading_hours or execute_mode != "tdx"

            # 6. 持仓（tdx/off 读桥, paper 读模拟盘）。
            #    桥断只跳过触发执行——评分已在上一步落盘, 推理不停
            bridge_ok = True
            try:
                if execute_mode == "paper":
                    positions, pos_err = await svc.load_positions_from_paper(tenant_id, user_id)
                else:
                    positions, pos_err = await svc.load_positions_from_tdx()
            except Exception as exc:
                positions, pos_err = [], f"持仓查询失败: {exc}"
            if pos_err:
                # 两个 loader 都是「出错返回 ([], 原因)」而**不抛**：此前用 `_` 把原因
                # 丢掉，于是这个 except 分支永不触发、bridge_ok 保持 True，held={} 仍然
                # 算出「每只达标标的都是新仓」的买单。持仓未知 ⇒ 不触发（fail-closed）：
                # 少做一轮远好过按错的持仓做一轮。
                bridge_ok = False
                positions_error = str(pos_err)
                error = f"持仓查询失败(跳过触发执行): {pos_err}"
                logger.warning("[TdxL2] %s", error)
            held = {p.get("symbol"): p for p in positions if isinstance(p, dict)}

            # 6.75 大盘点位（本地 QuantDB 读, 桥断不影响；重挂记录与触发共用）
            index_above, market_detail = await svc.is_index_above_ma20()

            # 6.5 在途单重挂（仅 tdx 实盘; 桥断跳过下轮恢复）+ 当日活单抑制集。
            #    抑制集保证"不能多": 标的已有活单在撮合时不重复触发新单。
            suppressed: set[str] = set()
            retry_stats: dict[str, Any] = {}
            if bridge_ok and execute_mode == "tdx" and real_orders_allowed:
                try:
                    today_orders = await svc.pull_today_orders()
                    # 成交后把实际成交均价并进委托时点记录（"什么价成交"）
                    merge_order_states(today_orders)
                    retry_stats = await _retry_inflight_orders(
                        svc,
                        signal_scores=signal_scores,
                        pool_data=pool_data,
                        fixed_buy_amount=fixed_buy_amount,
                        cooldown_min=cooldown_min,
                        today_orders=today_orders,
                        market_detail=market_detail,
                        index_above=index_above,
                    )
                    for o in today_orders:
                        if str(o.get("status")) in _WORKING_STATUSES:
                            suppressed.add(
                                StockCodeUtil.to_prefix(str(o.get("stock_code") or ""))
                            )
                except Exception as exc:
                    logger.warning("[TdxL2] 在途重挂异常(本轮跳过): %s", exc)
                if retry_stats:
                    realtime_status["retry_stats"] = {
                        k: v for k, v in retry_stats.items()
                    }

            # 7. 触发判断（桥断/采集陈旧/持仓读不到/非交易时段/有活单 → 只评分不触发）
            sell_items: list[dict[str, Any]] = []
            buy_items: list[dict[str, Any]] = []
            if positions_error:
                # 触发整段没做：池里这些标的**本可以**产生买卖信号而全部落空。
                # 这是告警面要的数字（"少了一轮判断"），不是普通日志。
                trigger_skipped = len(pool_data)
            elif not real_orders_allowed:
                # 非交易时段的 tdx 模式：本轮不做触发判断（池里这些标的没有判断，
                # 不是"判断了没通过"）。不是失败，故不进告警面。
                session_skipped = len(pool_data)
            if bridge_ok and not capture_stale and real_orders_allowed:
                for sym, signal in signal_scores.items():
                    fusion = fusion_scores.get(sym)
                    realtime_score = compute_realtime_score(
                        fusion or 0.0, signal, daily_w, signal_w
                    )

                    # 卖出: 持仓 + T+1 可卖 + 分数跌破阈值 + 冷却已过 + 无活单
                    pos = held.get(sym)
                    if (
                        pos
                        and sym not in suppressed
                        and float(pos.get("available_volume") or 0) > 0
                        and realtime_score < sell_trigger
                        and not is_cooldown(sym, cooldown_min)
                    ):
                        sell_items.append(
                            {
                                "symbol": sym,
                                "name": pos.get("name") or sym,
                                "score": realtime_score,
                                "volume": int(pos.get("volume") or 0),
                                "available_volume": int(pos.get("available_volume") or 0),
                                "reason": f"L2 实时分 {realtime_score} < {sell_trigger}",
                            }
                        )
                    # 买入: 池内 + 未持仓 + 无活单 + 大盘 MA20 + 分数突破 + 冷却已过
                    elif (
                        fusion is not None
                        and sym not in held
                        and sym not in suppressed
                        and index_above
                        and realtime_score > buy_trigger
                        and not is_cooldown(sym, cooldown_min)
                    ):
                        price = float(pool_data.get(sym, {}).get("now") or 0)
                        if price <= 0:
                            continue
                        from backend.services.live_trading.services.tdx_rolling_trade_service import (
                            LOT_SIZE,
                        )

                        volume = int((float(fixed_buy_amount or 0) / price) // LOT_SIZE) * LOT_SIZE
                        if volume < LOT_SIZE:
                            continue
                        buy_items.append(
                            {
                                "symbol": sym,
                                "score": realtime_score,
                                "volume": volume,
                                "close": round(price, 2),
                                "amount": round(volume * price, 2),
                                "reason": f"L2 实时分 {realtime_score} > {buy_trigger}",
                            }
                        )

            # 8. 执行（先卖后买，同 rolling；桥断时上面已无买无卖, 天然跳过）
            if execute_mode != "off" and (buy_items or sell_items):
                _, run_id, _ = await svc.load_latest_scores(tenant_id=tenant_id, user_id=user_id)
                # _execute_signals 返回**三元组** (placed, failed, error)。
                # 曾按两元组解包 → 每轮 ValueError → 执行段整段跳过、L2 实时自动
                # 交易从未下过单（分数照写, 界面看着是活的）。见回归用例
                # test_execution_path_actually_reaches_broker。
                placed, failed, exec_error = await _execute_signals(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id or "l2",
                    buys=buy_items,
                    sells=sell_items,
                    execute_mode=execute_mode,
                )
                if exec_error:
                    logger.warning("[TdxL2] 执行段返回：%s", exec_error)
                # 跨 11:35/15:05 的周期：提交时被时段闸挡在门内的腿**没有提交过**，
                # 不是下单失败——混进 orders_failed 会推「N 条腿下单失败，请核对
                # orders 补单」，而 orders 里根本没有这些单。并进 session_skipped，
                # 它们与上面那条「本周期开始时就不在时段内」是同一件事。
                failed, refused = split_session_refusals(failed)
                session_skipped += len(refused)
                if failed:
                    # 失败腿进状态（带条数上限）：告警面读它，面板也读它。
                    failed = list(failed)
                    realtime_status["orders_failed"] = failed[:_MAX_LISTED_FAILURES]
                    realtime_status["orders_failed_count"] = len(failed)
                buys_all = [p for p in placed if p.get("side") == "buy"]
                sells_all = [p for p in placed if p.get("side") == "sell"]
                for item in placed:
                    set_cooldown(item["symbol"], cooldown_min)
                    # 下单时刻即保存实时行情（价格/量/大盘点位）——
                    # 交易记录"什么点买的"数据源；成交后由 merge_order_states 补充成交均价
                    try:
                        save_order_quote(
                            symbol=item["symbol"],
                            order_id=str(item.get("order_id") or ""),
                            plan_id=str(item.get("plan_id") or ""),
                            side=str(item.get("side") or "buy"),
                            volume=int(
                                item.get("volume")
                                or item.get("available_volume")
                                or 0
                            ),
                            amount=float(
                                item.get("amount") or item.get("order_value") or 0
                            ),
                            quote_price=float(
                                pool_data.get(item["symbol"], {}).get("now")
                                or item.get("close")
                                or 0
                            ),
                            name=str(item.get("name") or ""),
                            market_detail=market_detail,
                            index_above=index_above,
                        )
                    except Exception:
                        pass
                    if execute_mode == "tdx" and str(item.get("status")) in _WORKING_STATUSES:
                        # 挂单成功未成交 → 登记在途, 由重挂逻辑接管直到成交/撤单
                        save_inflight(
                            item["symbol"],
                            {
                                "symbol": str(item.get("symbol") or ""),
                                "side": str(item.get("side") or "buy"),
                                "volume": int(item.get("volume") or item.get("available_volume") or 0),
                                "order_id": str(item.get("order_id") or ""),
                                "plan_id": str(item.get("plan_id") or ""),
                                "ts": time.time(),
                                "retries": 0,
                            },
                        )
                error = " | ".join(str(f.get("error")) for f in failed[:3]) or None
            elif buy_items or sell_items:
                buys_all, sells_all = buy_items, sell_items

            realtime_status.update(
                {
                    "bridge_ok": bridge_ok,
                    "capture_stale": capture_stale,
                    "last_buys": buys_all[:10],
                    "last_sells": sells_all[:10],
                    "last_cycle_at": datetime.now().isoformat(timespec="seconds"),
                    "index_above_ma20": index_above,
                    "market_detail": market_detail,
                    "execute_mode": execute_mode,
                    "positions_error": positions_error,
                    "trigger_skipped_symbols": trigger_skipped,
                    "session_skipped_symbols": session_skipped,
                    "exec_error": exec_error,
                    "last_error": error,
                }
            )
        except Exception as exc:
            realtime_status["cycle_error"] = str(exc)
            realtime_status["last_error"] = str(exc)
            # exc_info 必须带上：本行曾只打 str(exc)，于是 "too many values to unpack"
            # 这类缺陷在日志里没有文件行号，**潜伏了一整次重构周期**未被定位。
            logger.warning("[TdxL2] 实时推理异常: %s", exc, exc_info=True)

        # 状态镜像 + 失败可见性。运维脚本 `tdx_live_status.py` 与「设置→实盘」页读的是
        # `_STATUS_KEY`，而它**此前从未被写过**（定义在那儿，零引用）→ 监控面板
        # 恒为空 {}，看不出循环是死是活。放在 try 之外：出错的周期也要如实上报。
        await _publish_cycle_status()

        elapsed = time.monotonic() - cycle_start
        await asyncio.sleep(max(5.0, interval - elapsed))
