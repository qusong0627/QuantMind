"""TDX L2 实时因子采集任务 — TQ 接口 → 13 个回测验证因子 → PG 落库 + Redis。

数据源（经桥 /api/v1/tdx/call 透传 TQ 接口，实测 2026-08-25，午休 12:52 仍返回累积数据）:
- get_exday_data:    L2 扩展日线（成交单数/买卖净挂单/净撤单/委买卖均价/总委买卖/Vol·Amo 4×4分档）
- get_more_info:     88 字段扩展信息（L2TicNum/L2OrderNum/TotalBVol/TotalSVol）
- get_market_snapshot: Now/Open/LastClose/Volume/Amount + 5档 Buy*[5]/Sell*[5] + Inside/Outside

轮询集合 = 当日推理候选池 Top N（默认 20）+ 全部持仓（tdx + paper）。
桥读限流 60 次/分钟/IP 与行情推送（≈40/min）共享 → 持仓优先 + 恒定 ≤16/min 节奏，
全量每 ~6min 刷新一遍；RATE_LIMITED 退避加倍。

13 个因子 = 回测 14 推荐因子（db/feature_snapshots/l2_recommended_factors.csv, 2026-08-21 检验）
去掉 micro_pin（VPIN 家族已有 vol/amount 两席）后的实时近似，ICIR 权重原样保留。
实时数据不足的因子用"60s 采样序列"近似：VPIN 用时间桶、时段占比用本任务自记的
时段基准、RV/量价背离/流动性/冲击衰减用滚动窗口自相关与波动。
"""
import asyncio
import collections
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.trade_shared.redis_client import redis_client as trade_redis

logger = logging.getLogger(__name__)

# ---- 配置默认值 ----
DEFAULT_POOL_SIZE = 20          # 候选池大小（与实时推理共享）
MAX_WATCHLIST = 50              # 轮询上限（含持仓）
DEFAULT_INTERVAL_SEC = 60       # exday 轮询周期（每只）
SAMPLE_WINDOW = 40              # 因子滚动窗口（样本数，~40min）
_CALLS_PER_MIN = 300            # 目标桥调用速率：桥限流 600/min, 行情推送 ~40 + 账户 ~5 ≈ 345 < 600, 留一半余量
_CALLS_PER_CYCLE = 500          # 每周期桥调用预算（= 250 只×2, 全量刷新, 速率由节奏控制）
ZONE_BOUNDARIES = [("T3", (600, 630)), ("T4", (630, 660)), ("T5", (660, 690)), ("T6", (780, 810))]
# 时段（分钟数, 开盘后偏移）: T3=10:00-10:30, T4=10:30-11:00, T5=11:00-11:30, T6=13:00-13:30

_STATUS_KEY = "tdx:l2:capture:status"
_REALTIME_KEY = "tdx:l2:realtime:{symbol}"
_CONFIG_KEY = "tdx:l2:config"

# 14 个回测推荐因子的 ICIR 权重（l2_recommended_factors.csv, 去 micro_pin 后 13 个）
FACTOR_ICIR = {
    "micro_vpin_vol_ratio": 0.562,
    "micro_vpin_amount_ratio": 0.483,
    "micro_zone_distribution": 0.417,
    "micro_zone_vol_ratio_T4": 0.345,
    "micro_zone_vol_ratio_T6": 0.338,
    "vol_price_divergence": 0.332,
    "micro_zone_vol_ratio_T5": 0.316,
    "micro_open_gap": 0.273,
    "micro_impact_decay_half_life": 0.271,
    "micro_liquidity_daily_pattern": 0.237,
    "micro_zone_vol_ratio_T3": 0.198,
    "flow_imbalance_revert_speed": 0.161,
    "micro_zone_rv_ratio_close": 0.156,
}

l2_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_cycle_at": None,
    "last_error": None,
    "rate_limited": False,
    "watchlist_size": 0,
    "symbols": [],
    "snapshots_saved": 0,
    "bridge_ok": True,
}

# 上次成功解析出的候选池；engine 拉分失败时兜底沿用, 不让采集周期空转
_last_watchlist: list[str] = []


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ============ 表结构 ============

# asyncpg 不允许一条 prepared statement 多条语句 → 每条独立执行
_DDL_SNAPSHOT_STMTS = [
    """
CREATE TABLE IF NOT EXISTS tdx_l2_snapshot (
    id BIGSERIAL PRIMARY KEY,
    trade_date VARCHAR(8) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    cjbs BIGINT,
    b_order DOUBLE PRECISION, b_cancel DOUBLE PRECISION,
    s_order DOUBLE PRECISION, s_cancel DOUBLE PRECISION,
    buy_avp DOUBLE PRECISION, sell_avp DOUBLE PRECISION,
    total_b_order DOUBLE PRECISION, total_s_order DOUBLE PRECISION,
    vol_4x4 JSONB, amo_4x4 JSONB, vol_num JSONB,
    l2_tic_num BIGINT, l2_order_num BIGINT,
    total_b_vol DOUBLE PRECISION, total_s_vol DOUBLE PRECISION,
    now_price DOUBLE PRECISION, open_price DOUBLE PRECISION,
    pre_close DOUBLE PRECISION, bid5 JSONB, ask5 JSONB,
    factors JSONB,
    signal_score DOUBLE PRECISION,
    UNIQUE (symbol, ts)
)
""",
    "CREATE INDEX IF NOT EXISTS idx_l2_snap_symbol_date ON tdx_l2_snapshot(symbol, trade_date)",
]

_DDL_DAILY_STMTS = [
    """
CREATE TABLE IF NOT EXISTS tdx_l2_daily (
    trade_date VARCHAR(8) NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    cjbs BIGINT,
    b_order DOUBLE PRECISION, b_cancel DOUBLE PRECISION,
    s_order DOUBLE PRECISION, s_cancel DOUBLE PRECISION,
    buy_avp DOUBLE PRECISION, sell_avp DOUBLE PRECISION,
    total_b_order DOUBLE PRECISION, total_s_order DOUBLE PRECISION,
    vol_4x4 JSONB, amo_4x4 JSONB, vol_num JSONB,
    l2_tic_num BIGINT, l2_order_num BIGINT,
    total_b_vol DOUBLE PRECISION, total_s_vol DOUBLE PRECISION,
    now_price DOUBLE PRECISION, open_price DOUBLE PRECISION,
    pre_close DOUBLE PRECISION, bid5 JSONB, ask5 JSONB,
    factors JSONB,
    signal_score DOUBLE PRECISION,
    UNIQUE (trade_date, symbol)
)
""",
    "CREATE INDEX IF NOT EXISTS idx_l2_daily_date ON tdx_l2_daily(trade_date)",
]

# 早期迭代残留的老表不会因 CREATE TABLE IF NOT EXISTS 补列 → 逐列 ALTER 自愈迁移
_L2_DAILY_COLUMNS = {
    "stock_code": "VARCHAR(16)",
    "ts": "TIMESTAMPTZ",
    "cjbs": "BIGINT",
    "b_order": "DOUBLE PRECISION",
    "b_cancel": "DOUBLE PRECISION",
    "s_order": "DOUBLE PRECISION",
    "s_cancel": "DOUBLE PRECISION",
    "buy_avp": "DOUBLE PRECISION",
    "sell_avp": "DOUBLE PRECISION",
    "total_b_order": "DOUBLE PRECISION",
    "total_s_order": "DOUBLE PRECISION",
    "vol_4x4": "JSONB",
    "amo_4x4": "JSONB",
    "vol_num": "JSONB",
    "l2_tic_num": "BIGINT",
    "l2_order_num": "BIGINT",
    "total_b_vol": "DOUBLE PRECISION",
    "total_s_vol": "DOUBLE PRECISION",
    "now_price": "DOUBLE PRECISION",
    "open_price": "DOUBLE PRECISION",
    "pre_close": "DOUBLE PRECISION",
    "bid5": "JSONB",
    "ask5": "JSONB",
    "factors": "JSONB",
    "signal_score": "DOUBLE PRECISION",
}
_L2_DAILY_ALTER_STMTS = [
    f"ALTER TABLE tdx_l2_daily ADD COLUMN IF NOT EXISTS {col} {ctype}"
    for col, ctype in _L2_DAILY_COLUMNS.items()
]


async def ensure_l2_tables() -> None:
    async with get_session() as db:
        for stmt in _DDL_SNAPSHOT_STMTS + _DDL_DAILY_STMTS + _L2_DAILY_ALTER_STMTS:
            await db.execute(text(stmt))
        await db.commit()


# ============ 解析 ============

def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _clip(v: float, lo: float = -math.inf, hi: float = math.inf) -> float:
    return max(lo, min(hi, v))


def parse_exday_row(row: dict) -> dict:
    """TQ get_exday_data 单条 → 归一化字段。"""
    return {
        "trade_date": str(row.get("Date") or "").replace("-", "")[:8],
        "cjbs": int(_f(row.get("CJBS"))),
        "b_order": _f(row.get("BOrder")),
        "b_cancel": _f(row.get("BCancel")),
        "s_order": _f(row.get("SOrder")),
        "s_cancel": _f(row.get("SCancel")),
        "buy_avp": _f(row.get("BuyAvp")),
        "sell_avp": _f(row.get("SellAvp")),
        "total_b_order": _f(row.get("TotalBOrder")),
        "total_s_order": _f(row.get("TotalSOrder")),
        "vol_4x4": row.get("Vol") or [],
        "amo_4x4": row.get("Amo") or [],
        "vol_num": row.get("VolNum") or [],
    }


def parse_snapshot(snap_result: dict) -> dict:
    """get_market_snapshot → {now, open, pre_close, volume, amount, bid5, ask5, inside, outside}。"""
    r = snap_result.get("Value") if isinstance(snap_result, dict) else None
    if isinstance(r, list) and r:
        r = r[0] if isinstance(r[0], dict) else r
    if not isinstance(r, dict):
        return {}
    return {
        "now": _f(r.get("Now")),
        "open": _f(r.get("Open")),
        "pre_close": _f(r.get("LastClose")),
        "volume": _f(r.get("Volume")),
        "amount": _f(r.get("Amount")),
        "bid5": [_f(v) for v in (r.get("Buyv") or [])],
        "ask5": [_f(v) for v in (r.get("Sellv") or [])],
        "inside": _f(r.get("Inside")),
        "outside": _f(r.get("Outside")),
    }


def _vol_matrix(data: dict, col: int, key: str = "vol_4x4") -> float:
    """Vol/Amo 4×4 矩阵第 col 列之和（列: 0=买 1=卖 2=主买 3=主卖）。"""
    mat = data.get(key) or []
    return sum(_f(row_[col]) for row_ in mat if isinstance(row_, (list, tuple)) and len(row_) > col)


# ============ 因子计算 ============

class L2SeriesState:
    """单只股票的 60s 采样序列 + 时段基准。"""

    __slots__ = ("samples", "zone_baselines", "prev_price")

    def __init__(self) -> None:
        self.samples: collections.deque = collections.deque(maxlen=SAMPLE_WINDOW)
        self.zone_baselines: dict[str, float] = {}
        self.prev_price = 0.0


def _minute_of_day() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def _r2(series: list[float], lag: int = 1) -> float:
    """相关系数（lag 滞后的自相关或两序列相关），长度不足返回 0。"""
    if len(series) < 6:
        return 0.0
    a, b = series[:-lag], series[lag:]
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    den = math.sqrt(va * vb)
    return cov / den if den > 1e-12 else 0.0


def _autocorr(series: list[float], lag: int = 1) -> float:
    if len(series) < 8:
        return 0.0
    return _r2(series, lag)


def compute_l2_factors(
    data: dict, snap: dict, state: L2SeriesState, minute_of_day: int | None = None
) -> dict[str, float | None]:
    """13 个回测推荐因子的实时近似。全部基于累积字段，非交易时间也可算（静态日值）。

    样本不足的因子返回 None（截面标准化时跳过），避免把"无数据"误当"中性值"。
    """
    now = time.monotonic()
    v_buy, v_sell = _vol_matrix(data, 2), _vol_matrix(data, 3)                      # 主买/主卖（列 2/3）
    a_buy, a_sell = _vol_matrix(data, 2, "amo_4x4"), _vol_matrix(data, 3, "amo_4x4")  # Amo 同列
    v_total = v_buy + v_sell
    if v_total <= 0:
        v_buy, v_sell = snap.get("outside", 0), snap.get("inside", 0)   # 快照外盘/内盘兜底
        v_total = v_buy + v_sell
        a_buy, a_sell = v_buy, v_sell

    price = snap.get("now") or state.prev_price or 0
    vol_day = snap.get("volume") or v_total

    # ---- 时段基准（zone_vol_ratio_T* 需要开盘以来各时段累积量）----
    minute = _minute_of_day() if minute_of_day is None else minute_of_day
    for zone, (start, end) in ZONE_BOUNDARIES:
        if start <= minute < end and zone not in state.zone_baselines:
            state.zone_baselines[zone] = v_total          # 进入时段时记录的累积量

    factors: dict[str, float | None] = {}
    # 1/2. VPIN（时间桶近似: 滚动窗口 Σ|Δ买−Δ卖|/ΣΔ） — vol / amount
    state.samples.append((now, v_buy, v_sell, a_buy, a_sell, price))
    samples = list(state.samples)
    if len(samples) >= 3:
        d_v, d_buy, d_sell = [], [], []
        d_amt, d_abuy, d_asell = [], [], []
        for (t0, b0, s0, ab0, as0, _), (t1, b1, s1, ab1, as1, _) in zip(samples, samples[1:]):
            dv = max(b1 + s1 - b0 - s0, 0)
            da = max(ab1 + as1 - ab0 - as0, 0)
            d_buy, d_sell = d_buy + [max(b1 - b0, 0)], d_sell + [max(s1 - s0, 0)]
            d_abuy, d_asell = d_abuy + [max(ab1 - ab0, 0)], d_asell + [max(as1 - as0, 0)]
            d_v, d_amt = d_v + [dv], d_amt + [da]
        sv = sum(d_v) + 1e-9
        sa = sum(d_amt) + 1e-9
        factors["micro_vpin_vol_ratio"] = round(sum(abs(b - s) for b, s in zip(d_buy, d_sell)) / sv, 6)
        factors["micro_vpin_amount_ratio"] = round(sum(abs(b - s) for b, s in zip(d_abuy, d_asell)) / sa, 6)
    else:
        factors["micro_vpin_vol_ratio"] = None
        factors["micro_vpin_amount_ratio"] = None

    # 3. zone_distribution: 5 档深度按档位衰减加权的不平衡（买压正）
    bid5, ask5 = snap.get("bid5") or [], snap.get("ask5") or []
    depth_bal = 0.0
    depth_tot = 0.0
    for k, (b, a) in enumerate(zip(bid5, ask5)):
        w = 1.0 / (k + 1)
        depth_bal += w * (b - a)
        depth_tot += w * (b + a)
    factors["micro_zone_distribution"] = round(depth_bal / (depth_tot + 1e-9), 6)

    # 4-7. zone_vol_ratio_T3/T4/T5/T6: 时段成交量 / 当前总成交
    for zone, _ in ZONE_BOUNDARIES:
        base = state.zone_baselines.get(zone, 0)
        factors[f"micro_zone_vol_ratio_{zone}"] = round((v_total - base) / (v_total + 1e-9), 6)

    # 8. vol_price_divergence: 价格变动与量变动的负相关（背离为正）
    if len(samples) >= 10:
        prices = [s[5] for s in samples]
        vols = [s[1] + s[2] for s in samples]
        dp = [b - a for a, b in zip(prices, prices[1:])]
        dv = [b - a for a, b in zip(vols, vols[1:])]
        factors["vol_price_divergence"] = round(-_corr(dp, dv), 6)
    else:
        factors["vol_price_divergence"] = None

    # 9. open_gap: 开盘缺口（快照 Open/LastClose）
    open_p, pre_c = snap.get("open", 0), snap.get("pre_close", 0)
    factors["micro_open_gap"] = round((open_p - pre_c) / (pre_c + 1e-9), 6) if pre_c else None

    # 10/12. impact_decay / flow_revert_speed: 不平衡序列的自相关（1−ρ1, 快回复=高）
    imbalances = [_clip(s[1] - s[2], -1e12, 1e12) for s in samples]
    rho = _autocorr(imbalances, 1) if len(samples) >= 8 else None
    factors["micro_impact_decay_half_life"] = round(_clip(1 - rho, -1, 1), 6) if rho is not None else None
    factors["flow_imbalance_revert_speed"] = round(_clip(1 - rho, 0, 2), 6) if rho is not None else None

    # 11. liquidity_daily_pattern: 近 30min Amihud / 全天 Amihud
    if len(samples) >= 3 and price > 0:
        rets = []
        for (t0, *_a, p0), (t1, *_b, p1) in zip(samples, samples[1:]):
            if p0 > 0 and p1 > 0:
                rets.append(abs(p1 - p0) / p0)
        amt_day = snap.get("amount") or (a_buy + a_sell)
        cur_ret = sum(rets[-15:]) / max(len(rets[-15:]), 1)
        day_ret = sum(rets) / max(len(rets), 1)
        cur_amihud = cur_ret / max(amt_day * 0.25, 1e-9)   # 近 15 分钟 ≈ 全天 25%
        day_amihud = day_ret / max(amt_day, 1e-9)
        factors["micro_liquidity_daily_pattern"] = round(
            _clip(cur_amihud / (day_amihud + 1e-9), 0, 10), 6
        )
    else:
        factors["micro_liquidity_daily_pattern"] = None

    # 13. zone_rv_ratio_close: 近 30min 波动 / 全天波动
    if len(samples) >= 6:
        prices = [s[5] for s in samples]
        rets = [abs((b - a) / a) for a, b in zip(prices, prices[1:]) if a > 0]
        if rets:
            half = max(len(rets) // 2, 1)
            cur_rv = sum(rets[-half:]) / half
            day_rv = sum(rets) / len(rets)
            factors["micro_zone_rv_ratio_close"] = round(_clip(cur_rv / (day_rv + 1e-9), 0, 10), 6)
        else:
            # 全部价格 ≤ 0（收盘后/停牌快照）→ 无收益序列，置 None 而非除零崩溃
            factors["micro_zone_rv_ratio_close"] = None
    else:
        factors["micro_zone_rv_ratio_close"] = None

    state.prev_price = price or state.prev_price
    return {k: (round(_clip(v, -1e6, 1e6), 6) if v is not None else None) for k, v in factors.items()}


def _corr(a: list[float], b: list[float]) -> float:
    """两序列皮尔逊相关。"""
    n = len(a)
    if n < 6:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    den = math.sqrt(va * vb)
    return cov / den if den > 1e-12 else 0.0


def build_signal_score(factors: dict[str, float | None]) -> float:
    """ICIR 加权原始分（未做截面标准化，仅用于 DB 存档参考）；None 因子跳过。"""
    w_sum = sum(FACTOR_ICIR.values())
    acc = 0.0
    for k, w in FACTOR_ICIR.items():
        v = factors.get(k)
        if isinstance(v, (int, float)):
            acc += v * w
    return round(acc / w_sum * 100, 2)


# ============ 采集 ============

async def _tdx_call(method: str, params: dict) -> dict | None:
    """经桥透传 TQ 方法，失败返回 None 并记录错误。"""
    from backend.services.live_trading.services.tdx_push_service import (
        TdxPushError,
        tdx_pusher,
    )

    try:
        return await tdx_pusher.tdx_call(method, params)
    except TdxPushError as exc:
        if "RATE_LIMITED" in str(exc):
            l2_status["rate_limited"] = True
        else:
            l2_status["last_error"] = str(exc)
        return None
    except Exception as exc:
        l2_status["last_error"] = str(exc)
        return None


async def fetch_l2_data(symbol_suffix: str, with_more: bool = False) -> tuple[dict | None, dict]:
    """拉取单只股票 L2 扩展日线（+ 可选 more_info）+ 快照。返回 (exday_data, snapshot)。

    more_info 仅 L2TicNum/L2OrderNum 等诊断字段，因子计算不依赖 →
    每 8 只取一次，省下 1 次桥调用（60/min 限流与行情推送共享）。
    """
    exday = await _tdx_call("get_exday_data", {"stock_code": symbol_suffix, "count": 1})
    row = None
    if isinstance(exday, list) and exday:
        row = exday[0]
    elif isinstance(exday, dict):
        rows = exday.get("Value")
        if isinstance(rows, list) and rows:
            row = rows[0]

    data = parse_exday_row(row) if row else None
    if data is not None and with_more:
        more = await _tdx_call("get_more_info", {"stock_code": symbol_suffix})
        more_v = more.get("Value") if isinstance(more, dict) else None
        if isinstance(more_v, dict):
            data["l2_tic_num"] = int(_f(more_v.get("L2TicNum")))
            data["l2_order_num"] = int(_f(more_v.get("L2OrderNum")))
            data["total_b_vol"] = _f(more_v.get("TotalBVol"))
            data["total_s_vol"] = _f(more_v.get("TotalSVol"))

    snap = await _tdx_call("get_market_snapshot", {"stock_code": symbol_suffix})
    snap_data = parse_snapshot(snap)
    return data, snap_data


def _resolve_watchlist(
    scores: dict[str, float], tdx_positions: list, paper_positions: list, pool_size: int
) -> list[str]:
    """候选池 Top N + 持仓（prefix 格式），去重保序。"""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:pool_size]
    watch: list[str] = [StockCodeUtil.to_prefix(s) or s for s, _ in ranked]
    for pos in list(tdx_positions) + list(paper_positions):
        code = StockCodeUtil.to_prefix(str(pos.get("symbol") or pos.get("stock_code") or ""))
        if code and code not in watch:
            watch.append(code)
    return watch[:MAX_WATCHLIST]


async def _upsert_snapshot(
    data: dict, snap: dict, factors: dict[str, float], signal_score: float
) -> None:
    row = {
        "trade_date": data.get("trade_date") or datetime.now().strftime("%Y%m%d"),
        "ts": datetime.now(timezone.utc),
        "symbol": data["symbol"],
        "stock_code": data["stock_code"],
        "cjbs": data.get("cjbs"),
        "b_order": data.get("b_order"), "b_cancel": data.get("b_cancel"),
        "s_order": data.get("s_order"), "s_cancel": data.get("s_cancel"),
        "buy_avp": data.get("buy_avp"), "sell_avp": data.get("sell_avp"),
        "total_b_order": data.get("total_b_order"), "total_s_order": data.get("total_s_order"),
        "vol_4x4": data.get("vol_4x4"), "amo_4x4": data.get("amo_4x4"), "vol_num": data.get("vol_num"),
        "l2_tic_num": data.get("l2_tic_num"), "l2_order_num": data.get("l2_order_num"),
        "total_b_vol": data.get("total_b_vol"), "total_s_vol": data.get("total_s_vol"),
        "now_price": snap.get("now"), "open_price": snap.get("open"),
        "pre_close": snap.get("pre_close"), "bid5": snap.get("bid5"), "ask5": snap.get("ask5"),
        "factors": factors,
        "signal_score": signal_score,
    }
    # sa.text() 无列类型信息 → JSONB 列需手动 json 序列化，否则 asyncpg 报
    # "'list' object has no attribute 'encode'"（值原样透传驱动）
    for key in ("vol_4x4", "amo_4x4", "vol_num", "bid5", "ask5", "factors"):
        v = row.get(key)
        if v is not None and not isinstance(v, str):
            row[key] = json.dumps(v, ensure_ascii=False)
    cols = ", ".join(row.keys())
    ph = ", ".join(f":{k}" for k in row)
    async with get_session() as db:
        await db.execute(
            text(
                f"INSERT INTO tdx_l2_snapshot ({cols}) VALUES ({ph}) "
                "ON CONFLICT (symbol, ts) DO NOTHING"
            ),
            row,
        )
        await db.execute(
            text(
                f"INSERT INTO tdx_l2_daily ({cols}) VALUES ({ph}) "
                "ON CONFLICT (trade_date, symbol) DO UPDATE SET "
                + ", ".join(f"{k} = EXCLUDED.{k}" for k in row if k not in ("trade_date", "symbol"))
            ),
            row,
        )
        await db.commit()


def _redis_set_json(key: str, value: dict) -> None:
    """经 RedisClient 包装层写 JSON（内部 json.dumps + 异常吞掉）。"""
    trade_redis.set(key, value)


async def run_tdx_l2_capture_task(interval_sec: int = 0) -> None:
    """L2 因子采集主循环：候选池+持仓轮询 → 13 因子 → PG + Redis。"""
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        TdxRollingTradeService,
    )

    await ensure_l2_tables()
    svc = TdxRollingTradeService()
    base_interval = float(interval_sec or _env_int("TDX_L2_INTERVAL_SEC", DEFAULT_INTERVAL_SEC))
    states: dict[str, L2SeriesState] = {}
    l2_status["running"] = True
    l2_status["started_at"] = datetime.now().isoformat(timespec="seconds")
    logger.info("[TdxL2] L2 因子采集任务启动, base_interval=%.0fs", base_interval)

    while True:
        cycle_start = time.monotonic()
        try:
            await ensure_l2_tables()  # 幂等；失败时循环内重试，不让任务整体退出
            if trade_redis.client is not None:
                cfg = trade_redis.get(_CONFIG_KEY) or {}
            else:
                cfg = {}
            pool_size = int(cfg.get("pool_size") or DEFAULT_POOL_SIZE)
            interval = base_interval
            if pool_size > 20:
                interval = max(base_interval, pool_size * 3)

            # 周期起点三类输入独立容错：engine/桥/模拟盘任一故障只降级, 不拖垮采集
            scores = {}
            try:
                _, scores, _ = await svc.load_latest_scores(
                    tenant_id="default", user_id="00000001"
                )
            except Exception as exc:
                logger.warning("[TdxL2] 拉取推理分数失败(用缓存候选池): %s", exc)
            scores = scores or {}
            tdx_positions: list[dict[str, Any]] = []
            try:
                tdx_positions, _ = await svc.load_positions_from_tdx()
            except Exception as exc:
                l2_status["bridge_ok"] = False
                l2_status["last_error"] = f"桥持仓查询失败(跳过持仓轮询): {exc}"
                logger.warning("[TdxL2] %s", l2_status["last_error"])
            paper_positions: list[dict[str, Any]] = []
            try:
                paper_positions, _ = await svc.load_positions_from_paper("default", "00000001")
            except Exception as exc:
                logger.warning("[TdxL2] 模拟盘持仓查询失败: %s", exc)
            watchlist = _resolve_watchlist(scores, tdx_positions, paper_positions, pool_size)
            if not watchlist and _last_watchlist:
                watchlist = list(_last_watchlist)  # 兜底：沿用上次候选池
                logger.info("[TdxL2] 候选池为空, 沿用上次 %d 只", len(watchlist))
            if watchlist:
                _last_watchlist = list(watchlist)
            l2_status["watchlist_size"] = len(watchlist)
            l2_status["symbols"] = watchlist

            saved = 0
            processed = 0
            calls = 0
            # 卖出侧优先：持仓先扫，再扫候选池；速率由 _CALLS_PER_MIN 节奏保证
            # （全量 ~6min 刷新一遍，桥限流 60/min 与行情推送共享）
            pos_codes: list[str] = []
            for pos in list(tdx_positions) + list(paper_positions):
                code = StockCodeUtil.to_prefix(str(pos.get("symbol") or pos.get("stock_code") or ""))
                if code and code not in pos_codes:
                    pos_codes.append(code)
            pos_set = set(pos_codes)
            cycle_order = pos_codes + [s for s in watchlist if s not in pos_set]
            for prefix in cycle_order:
                if calls >= _CALLS_PER_CYCLE or l2_status.get("rate_limited"):
                    break
                # 限流礼让：每次迭代先按 _CALLS_PER_MIN 摊开（失败也保持节奏，
                # 避免 429 时全速重试打爆桥窗口）；全量 ~6min 刷新一遍
                await asyncio.sleep(120.0 / _CALLS_PER_MIN)
                suffix = StockCodeUtil.to_suffix(prefix) or prefix
                with_more = processed % 8 == 0
                data, snap = await fetch_l2_data(suffix, with_more=with_more)
                calls += 3 if with_more else 2
                if not data:
                    continue
                data["symbol"] = prefix
                data["stock_code"] = suffix
                state = states.setdefault(prefix, L2SeriesState())
                factors = compute_l2_factors(data, snap, state)
                signal = build_signal_score(factors)
                await _upsert_snapshot(data, snap, factors, signal)
                redis_payload = {
                    "symbol": prefix,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "factors": factors,
                    "signal_score": signal,
                    "now": snap.get("now"),
                    "volume": snap.get("volume"),
                    "l2_tic_num": data.get("l2_tic_num"),
                    "l2_order_num": data.get("l2_order_num"),
                }
                _redis_set_json(_REALTIME_KEY.format(symbol=prefix), redis_payload)
                saved += 1
                processed += 1

            l2_status["snapshots_saved"] += saved
            l2_status["processed"] = processed
            l2_status["last_cycle_at"] = datetime.now().isoformat(timespec="seconds")
            if saved == 0:
                l2_status["last_error"] = (
                    "桥限流(HTTP 429)，周期提前中断"
                    if l2_status.get("rate_limited")
                    else "本周期无有效 L2 数据"
                )
        except Exception as exc:
            l2_status["last_error"] = str(exc)
            logger.warning("[TdxL2] 采集循环异常: %s", exc)

        # 限流退避：rate_limited 保留到退避判定之后，下一周期再重新评估
        sleep_sec = interval * 2 if l2_status.get("rate_limited") else interval
        elapsed = time.monotonic() - cycle_start
        await asyncio.sleep(max(5.0, sleep_sec - elapsed))
        l2_status["rate_limited"] = False
