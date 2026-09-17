"""识别引擎 v1 检测器（T-P6-14）——四类检测的**纯函数**实现（金样可测，无 IO）。

设计（2026-09-17，细案 T-P6-14）：

- **市场异动（量价）**：量比（时间校正后）≥ 阈值 → 放量；价格触涨跌停档 → 涨停/跌停异动；
  未到档但大幅波动 → 大幅波动（price_surge）。
- **账户异常**：撤单率（样本 ≥ N 才判）与持仓集中度（单一标的市场价值占比）。
- **数据异常**：日线跳变（超出涨跌停包络可解释范围）/ 缺口（相邻交易日缺失）/ 零成交。
- **模型异常**：短窗 IC 均值低于绝对阈，或相对长窗骤降（接入 `model_ic_monitor` 输出）。

契约：每个检测器输入为**调用方取好数的 dict/序列**（服务层负责取数与缺失计数），
输出 :class:`Detection` 列表（空列表=无异常）。**宁可漏报不臆造**：输入缺字段/样本不足
一律跳过（服务层计数 `skipped`），绝不用默认值编造判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping, Sequence

# 异动类型（写入 qm_market_anomalies.anomaly_type；CHECK 约束见 shared/anomaly_contract.py）
KIND_VOLUME_SURGE = "volume_surge"
KIND_PRICE_LIMIT_UP = "price_limit_up"
KIND_PRICE_LIMIT_DOWN = "price_limit_down"
KIND_PRICE_SURGE = "price_surge"
KIND_ACCOUNT_CANCEL = "account_cancel_ratio"
KIND_ACCOUNT_CONCENTRATION = "account_concentration"
KIND_DATA_JUMP = "data_jump"
KIND_DATA_GAP = "data_gap"
KIND_DATA_ZERO_VOLUME = "data_zero_volume"
KIND_MODEL_IC_DROP = "model_ic_drop"

# 总线事件级别（intel_events.LEVELS）
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"


@dataclass(frozen=True)
class Detection:
    """单条检测结果（不可变；服务层据此执行 告警/否决/降仓 三类动作）。"""

    kind: str
    subject: str  # symbol（市场/数据类）/ user_id（账户类）/ model_id（模型类）
    severity: str
    title: str
    description: str = ""
    market: str = "CN"
    metrics: dict[str, Any] = field(default_factory=dict)
    targets: tuple[str, ...] = ()
    actions_hint: tuple[str, ...] = ()


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _limit_band_tolerance(limit_band: float) -> float:
    """涨跌停价舍入容差（交易所四舍五入到分；0.5% 覆盖一切档位噪声）。"""
    return max(0.005, abs(limit_band) * 0.005)


def detect_volume_price(
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    volume_ratio_min: float = 3.0,
    price_pct_min: float = 0.05,
    elapsed_fraction: float = 1.0,
) -> list[Detection]:
    """市场异动（量价）。quotes: {symbol: {price, pct_chg, now_volume, avg_daily_volume,
    limit_up?, limit_down?, is_suspended?}}；pct_chg 为小数（0.05=5%）。

    - 量比 = now_volume / (avg_daily_volume × elapsed_fraction)（时间校正，防早盘误报）；
    - 触档（price ≥ limit_up−容差 / ≤ limit_down+容差）单列涨跌停异动；
    - 未触档但 |pct_chg| ≥ price_pct_min → 大幅波动。
    """
    out: list[Detection] = []
    frac = min(max(float(elapsed_fraction or 1.0), 0.05), 1.0)
    for symbol, q in (quotes or {}).items():
        if not isinstance(q, Mapping):
            continue
        if str(q.get("is_suspended") or "").strip().lower() in {"1", "true", "yes", "on"}:
            continue
        price = _f(q.get("price"))
        pct = _f(q.get("pct_chg"))
        if price is None or price <= 0 or pct is None:
            continue
        limit_up = _f(q.get("limit_up"))
        limit_down = _f(q.get("limit_down"))
        now_vol = _f(q.get("now_volume"))
        avg_vol = _f(q.get("avg_daily_volume"))

        if limit_up is not None and limit_up > 0 and price >= limit_up - _limit_band_tolerance(limit_up):
            out.append(
                Detection(
                    kind=KIND_PRICE_LIMIT_UP,
                    subject=str(symbol),
                    severity=LEVEL_INFO,
                    title=f"{symbol} 涨停异动",
                    description=f"现价 {price:.2f} 触涨停 {limit_up:.2f}（{pct:+.2%}）",
                    metrics={"price": price, "limit_up": limit_up, "pct_chg": pct},
                    targets=(str(symbol),),
                    actions_hint=("heat_watch",),
                )
            )
        elif limit_down is not None and limit_down > 0 and price <= limit_down + _limit_band_tolerance(limit_down):
            out.append(
                Detection(
                    kind=KIND_PRICE_LIMIT_DOWN,
                    subject=str(symbol),
                    severity=LEVEL_WARN,
                    title=f"{symbol} 跌停异动",
                    description=f"现价 {price:.2f} 触跌停 {limit_down:.2f}（{pct:+.2%}）",
                    metrics={"price": price, "limit_down": limit_down, "pct_chg": pct},
                    targets=(str(symbol),),
                    actions_hint=("risk_review",),
                )
            )
        elif abs(pct) >= price_pct_min:
            out.append(
                Detection(
                    kind=KIND_PRICE_SURGE,
                    subject=str(symbol),
                    severity=LEVEL_INFO if pct > 0 else LEVEL_WARN,
                    title=f"{symbol} 大幅{'上' if pct > 0 else '下'}行",
                    description=f"涨跌幅 {pct:+.2%}（阈值 {price_pct_min:.1%}）",
                    metrics={"price": price, "pct_chg": pct},
                    targets=(str(symbol),),
                    actions_hint=("risk_review",),
                )
            )

        if now_vol is not None and avg_vol is not None and avg_vol > 0 and now_vol > 0:
            expected = avg_vol * frac
            ratio = now_vol / expected if expected > 0 else None
            if ratio is not None and ratio >= volume_ratio_min:
                sev = LEVEL_CRITICAL if ratio >= volume_ratio_min * 2 else LEVEL_WARN
                out.append(
                    Detection(
                        kind=KIND_VOLUME_SURGE,
                        subject=str(symbol),
                        severity=sev,
                        title=f"{symbol} 异常放量",
                        description=(
                            f"量比 {ratio:.1f}（现量 {now_vol:,.0f} / 预期 {expected:,.0f}，"
                            f"时段进度 {frac:.0%}）"
                        ),
                        metrics={
                            "volume_ratio": round(ratio, 2),
                            "now_volume": now_vol,
                            "expected_volume": round(expected, 1),
                            "elapsed_fraction": frac,
                        },
                        targets=(str(symbol),),
                        actions_hint=("heat_watch",),
                    )
                )
    return out


def detect_account_anomaly(
    orders: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    *,
    cancel_ratio_min: float = 0.6,
    min_orders: int = 10,
    concentration_max: float = 0.5,
    min_position_value: float = 1_000.0,
    subject: str = "",
) -> list[Detection]:
    """账户异常：撤单率 + 持仓集中度。orders: [{status, ...}]；positions: [{symbol, market_value}]。

    样本不足（订单数 < min_orders / 持仓总市值 < min_position_value）→ 该维度不出结果。
    撤单判定：status 归一化后属于 {cancelled, canceled, 已撤, 部撤}。
    """
    out: list[Detection] = []
    cancel_states = {"cancelled", "canceled", "已撤", "部撤", "partially_cancelled"}
    total = len(orders or [])
    if total >= max(1, int(min_orders)):
        cancelled = 0
        for o in orders:
            st = str((o or {}).get("status") or "").strip().lower()
            if st in cancel_states:
                cancelled += 1
        ratio = cancelled / total
        if ratio >= cancel_ratio_min:
            out.append(
                Detection(
                    kind=KIND_ACCOUNT_CANCEL,
                    subject=str(subject),
                    severity=LEVEL_WARN if ratio < 0.8 else LEVEL_CRITICAL,
                    title=f"账户 {subject or '-'} 撤单率异常",
                    description=f"撤单率 {ratio:.0%}（{cancelled}/{total}，阈值 {cancel_ratio_min:.0%}）",
                    metrics={"cancel_ratio": round(ratio, 3), "orders": total, "cancelled": cancelled},
                    targets=(str(subject),) if subject else (),
                    actions_hint=("risk_review",),
                )
            )
    values: list[tuple[str, float]] = []
    for p in positions or []:
        mv = _f((p or {}).get("market_value"))
        if mv is None or mv <= 0:
            continue
        values.append((str((p or {}).get("symbol") or "?"), mv))
    total_mv = sum(v for _, v in values)
    if values and total_mv >= min_position_value:
        top_symbol, top_mv = max(values, key=lambda kv: kv[1])
        share = top_mv / total_mv
        if share >= concentration_max:
            out.append(
                Detection(
                    kind=KIND_ACCOUNT_CONCENTRATION,
                    subject=str(subject),
                    severity=LEVEL_WARN,
                    title=f"账户 {subject or '-'} 持仓集中度偏高",
                    description=(
                        f"{top_symbol} 占持仓市值 {share:.0%}（阈值 {concentration_max:.0%}，"
                        f"总市值 {total_mv:,.0f}）"
                    ),
                    metrics={"concentration": round(share, 3), "top_symbol": top_symbol,
                             "total_market_value": round(total_mv, 1)},
                    targets=tuple([str(subject)] if subject else []) + (top_symbol,),
                    actions_hint=("risk_review",),
                )
            )
    return out


def detect_data_anomaly(
    latest: Mapping[str, Any],
    prev: Mapping[str, Any] | None,
    *,
    jump_pct_max: float = 0.11,
    expected_prev_date: str | None = None,
    subject: str = "",
) -> list[Detection]:
    """数据异常：跳变 / 缺口 / 零成交。latest/prev: {date, close, volume, limit_up?, limit_down?}。

    - 跳变：|latest.close/prev.close − 1| 超出「涨跌停包络 + 容差」（无包络字段则用 jump_pct_max）；
    - 缺口：expected_prev_date 给了且 != prev.date → 相邻交易日缺失；
    - 零成交：close 正常但 volume ≤ 0。
    """
    out: list[Detection] = []
    close = _f((latest or {}).get("close"))
    volume = _f((latest or {}).get("volume"))
    date = str((latest or {}).get("date") or "")
    if close is None or close <= 0:
        return out
    if volume is not None and volume <= 0:
        out.append(
            Detection(
                kind=KIND_DATA_ZERO_VOLUME,
                subject=str(subject),
                severity=LEVEL_WARN,
                title=f"{subject or '-'} {date} 零成交可疑",
                description=f"close={close:.2f} 但 volume={volume}",
                metrics={"date": date, "close": close, "volume": volume},
                targets=(str(subject),) if subject else (),
                actions_hint=("data_quality_review",),
            )
        )
    if expected_prev_date and prev is not None:
        prev_date = str((prev or {}).get("date") or "")
        if prev_date and prev_date != str(expected_prev_date):
            out.append(
                Detection(
                    kind=KIND_DATA_GAP,
                    subject=str(subject),
                    severity=LEVEL_CRITICAL,
                    title=f"{subject or '-'} 日线缺口",
                    description=f"最新 {date} 的前一交易日应为 {expected_prev_date}，实际 {prev_date}",
                    metrics={"date": date, "expected_prev": str(expected_prev_date), "actual_prev": prev_date},
                    targets=(str(subject),) if subject else (),
                    actions_hint=("data_quality_review",),
                )
            )
    prev_close = _f((prev or {}).get("close")) if prev else None
    if prev_close and prev_close > 0:
        jump = close / prev_close - 1.0
        limit_up = _f((latest or {}).get("limit_up"))
        limit_down = _f((latest or {}).get("limit_down"))
        band = jump_pct_max
        if limit_up and limit_down and prev_close > 0:
            band = max(abs(limit_up / prev_close - 1.0), abs(1.0 - limit_down / prev_close), jump_pct_max)
        if abs(jump) > band + 0.02:
            out.append(
                Detection(
                    kind=KIND_DATA_JUMP,
                    subject=str(subject),
                    severity=LEVEL_CRITICAL,
                    title=f"{subject or '-'} 日线跳变",
                    description=f"{prev_close:.2f} → {close:.2f}（{jump:+.2%}，包络 ±{band:.1%}）",
                    metrics={"date": date, "prev_close": prev_close, "close": close,
                             "jump_pct": round(jump, 4), "band": round(band, 4)},
                    targets=(str(subject),) if subject else (),
                    actions_hint=("data_quality_review",),
                )
            )
    return out


def detect_model_anomaly(
    model_id: str,
    ic_stats: Mapping[str, Any],
    *,
    short_window: str = "ic_5",
    long_window: str = "ic_20",
    short_min: float = 0.0,
    drop_ratio_max: float = 0.5,
    min_samples: int = 5,
) -> list[Detection]:
    """模型异常：短窗 IC 低于绝对阈，或相对长窗骤降。

    ic_stats: {ic_5: float, ic_20: float, n_5: int, n_20: int, latest_ic_date?: str}
    （即 `scripts/model_ic_monitor.monitor()` 输出的窗口统计）。
    """
    out: list[Detection] = []
    short = _f((ic_stats or {}).get(short_window))
    long = _f((ic_stats or {}).get(long_window))
    n_short = _f((ic_stats or {}).get(f"n_{short_window.split('_')[-1]}"))
    if short is None or (n_short is not None and n_short < min_samples):
        return out
    drop_triggered = long is not None and long > 0 and short < long * (1.0 - drop_ratio_max)
    abs_triggered = short < short_min
    if not (drop_triggered or abs_triggered):
        return out
    severity = LEVEL_CRITICAL if short < 0 else LEVEL_WARN
    desc = f"短窗 {short_window}={short:+.3f}"
    if long is not None:
        desc += f"，长窗 {long_window}={long:+.3f}"
    if drop_triggered:
        desc += f"（相对骤降 > {drop_ratio_max:.0%}）"
    out.append(
        Detection(
            kind=KIND_MODEL_IC_DROP,
            subject=str(model_id),
            severity=severity,
            title=f"模型 {model_id} IC 异常",
            description=desc,
            metrics={"ic_short": short, "ic_long": long, "short_window": short_window,
                     "latest_ic_date": (ic_stats or {}).get("latest_ic_date")},
            targets=(str(model_id),),
            actions_hint=("model_review",),
        )
    )
    return out
