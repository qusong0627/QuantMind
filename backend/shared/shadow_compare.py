"""影子对照（T-P2-06）：模拟账本 vs 镜像真单的偏差指标 —— **纯函数唯一实现**。

设计口径（`docs/统一交易栈_设计方案.md` §8.3 模拟接近实盘验收指标）：
- 成交价偏差分布（bps；**正 = 相对模拟成交更差**：买贵/卖便宜均为正成本）；
- 成交率 / 部分成交比例（real 成交量 ÷ sim 成交量）；
- 滑点实现值 vs 配置值（|real-sim| 均值 bps 对照配置滑点）；
- 模拟-实盘跟踪误差（共同交易日日收益差的 mean/std，年化 ×√244）。

配对键：真单 ``orders.client_order_id = 'mir-{base}'``，base ∈ {sim
``client_order_id`` | sim ``order_id`` | remarks 内嵌 ``client_order_id=`` }。
采集/落库见 ``services/trade/services/shadow_compare_service.py``；本模块不碰 IO。

约定（输入行 dict，采集层保证键存在）：
  sim : {order_id, client_order_id, remarks, symbol, side, fill_price, filled_quantity}
  real: {client_order_id, symbol, side, average_price, filled_quantity, status,
         commission, price_source}
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

MIRROR_CID_PREFIX = "mir-"
SIM_REMARK_PREFIX = "client_order_id="

REJECTED_STATUSES = frozenset({"rejected", "cancelled", "canceled", "expired"})
# 仅「全部成交」计入 filled；部分成交单列（口径 = 订单状态精确匹配）
FILLED_STATUSES = frozenset({"filled"})
PARTIAL_STATUSES = frozenset({"partially_filled", "partial_filled", "partial"})

# 跟踪误差年化因子：244 交易日（与训练评估口径一致）
TRADING_DAYS_PER_YEAR = 244
# 跟踪误差最小样本：≥3 个共同交易日（≥2 个日收益）
_MIN_RETURNS = 2


def mirror_base(real_cid: str | None) -> str | None:
    """真单 cid → 镜像 base（非 ``mir-`` 前缀返回 None）。"""
    cid = str(real_cid or "").strip()
    if not cid.startswith(MIRROR_CID_PREFIX):
        return None
    base = cid[len(MIRROR_CID_PREFIX) :].strip()
    return base or None


def sim_base_keys(sim_order: dict[str, Any]) -> set[str]:
    """模拟单可被镜像引用的全部 base 键（三种镜像建单路径的交集口径）。"""
    keys: set[str] = set()
    order_id = str(sim_order.get("order_id") or "").strip()
    if order_id:
        keys.add(order_id)
    cid = str(sim_order.get("client_order_id") or "").strip()
    if cid:
        keys.add(cid)
    remarks = str(sim_order.get("remarks") or "")
    if remarks.startswith(SIM_REMARK_PREFIX):
        token = remarks[len(SIM_REMARK_PREFIX) :].strip().split(" ", 1)[0]
        if token:
            keys.add(token)
    return keys


def _side_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def pair_orders(
    sim_orders: list[dict[str, Any]], real_orders: list[dict[str, Any]]
) -> dict[str, Any]:
    """按镜像 base 键配对（纯函数）。

    返回 ``{pairs, sim_only, real_only, symbol_side_mismatch}``；
    ``pairs`` 元素为两侧合并行（保持原始 dict 字段 + ``base`` 与配对结果键）。
    """
    index: dict[str, dict[str, Any]] = {}
    for sim in sim_orders:
        for key in sim_base_keys(sim):
            index.setdefault(key, sim)

    pairs: list[dict[str, Any]] = []
    real_only: list[dict[str, Any]] = []
    matched_sim_keys: set[int] = set()
    mismatch = 0
    for real in real_orders:
        base = mirror_base(real.get("client_order_id"))
        sim = index.get(base) if base else None
        if sim is None:
            real_only.append(real)
            continue
        matched_sim_keys.add(id(sim))
        sim_symbol = str(sim.get("symbol") or "").strip().upper()
        real_symbol = str(real.get("symbol") or "").strip().upper()
        sim_side = _side_text(sim.get("side"))
        real_side = _side_text(real.get("side"))
        symbol_mismatch = bool(sim_symbol and real_symbol and sim_symbol != real_symbol)
        side_mismatch = bool(sim_side and real_side and sim_side != real_side)
        if symbol_mismatch or side_mismatch:
            mismatch += 1
        pairs.append(
            {
                "base": base,
                "symbol": real_symbol or sim_symbol,
                "side": real_side or sim_side,
                "sim_order_id": str(sim.get("order_id") or ""),
                "sim_cid": str(sim.get("client_order_id") or ""),
                "sim_user_id": str(sim.get("user_id") or ""),
                "sim_price": float(sim.get("fill_price") or 0.0),
                "sim_quantity": float(sim.get("filled_quantity") or 0.0),
                # 透传（端点/报告展示用；缺省安全）
                "sim_fee": float(sim.get("total_fee") or 0.0),
                "sim_status": str(sim.get("status") or ""),
                "real_cid": str(real.get("client_order_id") or ""),
                "real_user_id": str(real.get("user_id") or ""),
                "real_order_id": str(real.get("order_id") or ""),
                "real_exchange_order_id": str(real.get("exchange_order_id") or ""),
                "real_price": float(real.get("average_price") or 0.0),
                "real_limit_price": float(real.get("price") or 0.0),
                "real_quantity": float(real.get("filled_quantity") or 0.0),
                "real_status": _side_text(real.get("status")),
                "real_commission": float(real.get("commission") or 0.0),
                "real_remarks": str(real.get("remarks") or ""),
                "price_source": str(real.get("price_source") or ""),
                "symbol_mismatch": symbol_mismatch or side_mismatch,
            }
        )
    sim_only = [s for s in sim_orders if id(s) not in matched_sim_keys]
    return {
        "pairs": pairs,
        "sim_only": sim_only,
        "real_only": real_only,
        "symbol_side_mismatch": mismatch,
    }


def _percentile(values: list[float], pct: float) -> float:
    """最近秩（nearest-rank）分位；调用方保证 values 非空。"""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct * len(ordered)))
    return float(ordered[rank - 1])


def _median(values: list[float]) -> float:
    """统计中位数（偶数个取中间两值均值）。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean(values: list[float]) -> float:
    return float(sum(values)) / len(values)


def compute_price_deviation(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """成交价偏差分布（bps，正 = 成本）。

    方向归一：买入 dev = (real-sim)/sim；卖出 dev = (sim-real)/sim；
    两侧 ``dev>0`` 均表示「实际成交比模拟更差」。仅统计双侧价格 >0 的配对。
    """
    devs: list[float] = []
    for p in pairs:
        sim_price = float(p.get("sim_price") or 0.0)
        real_price = float(p.get("real_price") or 0.0)
        if sim_price <= 0 or real_price <= 0:
            continue
        raw = (real_price - sim_price) / sim_price * 10000.0
        if _side_text(p.get("side")) == "sell":
            raw = -raw
        devs.append(raw)
    if not devs:
        return {
            "n": 0,
            "mean_bps": None,
            "median_bps": None,
            "p95_abs_bps": None,
            "abs_mean_bps": None,
        }
    abs_devs = [abs(d) for d in devs]
    return {
        "n": len(devs),
        "mean_bps": round(_mean(devs), 4),
        "median_bps": round(_median(devs), 4),
        "p95_abs_bps": round(_percentile(abs_devs, 0.95), 4),
        "abs_mean_bps": round(_mean(abs_devs), 4),
    }


def compute_fill_stats(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """成交率 / 部分成交 / 拒单统计（成交量口径）。"""
    sim_quantity = 0.0
    real_quantity = 0.0
    partial = 0
    rejected = 0
    filled = 0
    for p in pairs:
        sim_q = float(p.get("sim_quantity") or 0.0)
        real_q = float(p.get("real_quantity") or 0.0)
        status = str(p.get("real_status") or "").strip().lower()
        sim_quantity += max(0.0, sim_q)
        real_quantity += max(0.0, real_q)
        if status in REJECTED_STATUSES:
            rejected += 1
        elif status in PARTIAL_STATUSES or (
            status in FILLED_STATUSES and real_q < sim_q
        ):
            partial += 1
        elif status in FILLED_STATUSES:
            filled += 1
    return {
        "sim_quantity": round(sim_quantity, 4),
        "real_quantity": round(real_quantity, 4),
        "fill_rate": round(real_quantity / sim_quantity, 6)
        if sim_quantity > 0
        else None,
        "filled_count": filled,
        "partial_count": partial,
        "rejected_count": rejected,
    }


def compute_slippage_realization(
    pairs: list[dict[str, Any]], configured_bps: float
) -> dict[str, Any]:
    """滑点实现值 vs 配置值（|real-sim| 均值 bps）。"""
    abs_devs: list[float] = []
    for p in pairs:
        sim_price = float(p.get("sim_price") or 0.0)
        real_price = float(p.get("real_price") or 0.0)
        if sim_price <= 0 or real_price <= 0:
            continue
        abs_devs.append(abs(real_price - sim_price) / sim_price * 10000.0)
    if not abs_devs:
        return {
            "n": 0,
            "configured_bps": float(configured_bps),
            "realized_abs_mean_bps": None,
            "delta_bps": None,
        }
    realized = _mean(abs_devs)
    return {
        "n": len(abs_devs),
        "configured_bps": float(configured_bps),
        "realized_abs_mean_bps": round(realized, 4),
        "delta_bps": round(realized - float(configured_bps), 4),
    }


def compute_tracking_error(
    sim_series: list[tuple[Any, float]],
    real_series: list[tuple[Any, float]],
    *,
    annualization: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """模拟-实盘跟踪误差：共同交易日日收益差（模拟 − 实盘）的 mean/std。

    输入 ``[(date, equity)]``（任意顺序，内部按日对齐去重——同日多行取最后一条）。
    ``sufficient`` 需 ≥3 个共同交易日（≥2 个日收益）；不足时仍给出可得统计。
    年化：``std × √annualization``（默认 244）。
    """
    sim_map: dict[Any, float] = {}
    for day, equity in sim_series or []:
        sim_map[day] = float(equity or 0.0)
    real_map: dict[Any, float] = {}
    for day, equity in real_series or []:
        real_map[day] = float(equity or 0.0)

    common = sorted(set(sim_map) & set(real_map))
    # 只保留权益 >0 的相邻共同日（防除零/停牌残值）
    diffs_bps: list[float] = []
    for prev_day, day in zip(common, common[1:], strict=False):
        s0, s1 = sim_map[prev_day], sim_map[day]
        r0, r1 = real_map[prev_day], real_map[day]
        if min(s0, s1, r0, r1) <= 0:
            continue
        sim_ret = s1 / s0 - 1.0
        real_ret = r1 / r0 - 1.0
        diffs_bps.append((sim_ret - real_ret) * 10000.0)

    result: dict[str, Any] = {
        "common_days": len(common),
        "n_returns": len(diffs_bps),
        "start": common[0].isoformat()
        if common and hasattr(common[0], "isoformat")
        else (str(common[0]) if common else None),
        "end": common[-1].isoformat()
        if common and hasattr(common[-1], "isoformat")
        else (str(common[-1]) if common else None),
        "annualization": int(annualization),
    }
    if len(diffs_bps) < _MIN_RETURNS:
        result.update(
            {
                "sufficient": False,
                "reason": (
                    f"共同交易日不足（{len(common)} 天/{len(diffs_bps)} 个收益，"
                    f"需 ≥{_MIN_RETURNS + 1} 天）"
                ),
                "mean_diff_bps": None,
                "std_diff_bps": None,
                "te_ann_bps": None,
            }
        )
        return result
    mean_diff = _mean(diffs_bps)
    if len(diffs_bps) > 1:
        var = sum((d - mean_diff) ** 2 for d in diffs_bps) / (len(diffs_bps) - 1)
        std_diff = math.sqrt(var)
    else:  # pragma: no cover - 上方已保证 ≥2
        std_diff = 0.0
    result.update(
        {
            "sufficient": True,
            "reason": "",
            "mean_diff_bps": round(mean_diff, 4),
            "std_diff_bps": round(std_diff, 4),
            "te_ann_bps": round(std_diff * math.sqrt(int(annualization)), 4),
        }
    )
    return result


def build_shadow_report(
    *,
    date_str: str,
    pairing: dict[str, Any],
    configured_bps: float,
    tracking: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """组装影子对照日报（纯函数；``ok`` = 无符号/方向错配）。"""
    pairs = list(pairing.get("pairs") or [])
    mismatch = int(pairing.get("symbol_side_mismatch") or 0)
    return {
        "date": str(date_str),
        "generated_at": generated_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "coverage": {
            "matched": len(pairs),
            "sim_only": len(pairing.get("sim_only") or []),
            "real_only": len(pairing.get("real_only") or []),
            "symbol_side_mismatch": mismatch,
        },
        "price_deviation": compute_price_deviation(pairs),
        "fill": compute_fill_stats(pairs),
        "slippage": compute_slippage_realization(pairs, configured_bps),
        "tracking_error": tracking
        if tracking is not None
        else {"sufficient": False, "reason": "未提供跟踪误差输入（模拟/实盘日度净值）"},
        "ok": mismatch == 0,
        "source": "shared/shadow_compare.py（T-P2-06 影子对照唯一实现）",
    }
