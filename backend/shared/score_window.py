"""T+N 模型分数的交易窗口口径（唯一实现）。

背景：模型在数据日 ``D`` 收盘后产出分数；训练标签按 ``execution_lag_days=1``
在 ``D+1`` 开盘执行、持有 ``horizon`` 个交易日，到 ``D+1+horizon`` 收盘结算。
因此：

- 执行日 entry = D + lag（交易日）
- 目标日 target = D + lag + horizon（交易日）
- 预测窗口 = [entry, target]，共 horizon 个交易日收益区间

历史实现用 ``timedelta(days=horizon)`` 按自然日推算目标日，T+20 会落到周末/
节假日，且与训练标签差一天以上。本模块是唯一允许计算该窗口的地方；任何
"目标日" 都必须走这里。

日历纪律：只认 ``exchange_calendars``（与 ``trading_calendar`` 同一把尺子）。
日历缺失/越界时抛 :class:`CalendarUnavailable`，调用方必须显性降级（例如不画
目标价），**不允许**退回自然日或"只看周末"的近似。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from backend.shared.trading_calendar import (
    _get_xcal_calendar,
    trading_days_xcal,
)

__all__ = [
    "CalendarUnavailable",
    "LabelWindow",
    "label_window",
    "label_window_dates",
    "label_targets_bulk",
    "next_trading_days",
    "normalize_market",
    "trading_days_between",
]

#: 市场别名 → ``trading_calendar`` 能识别的交易所码。
#: 与 ``backend/shared/trading_calendar.py::_MARKET_ALIASES`` 保持同一口径；
#: 这里只做"全仓市场维度 → 交易所"的收敛，避免调用方各写一份 if/else。
_MARKET_ALIASES = {
    "CN": "CN",
    "A": "CN",
    "ASHARE": "CN",
    "AS": "CN",
    "SSE": "SSE",
    "SH": "SSE",
    "SZ": "SZSE",
    "SZSE": "SZSE",
    "HK": "HK",
    "HKEX": "HK",
    "US": "US",
    "NYSE": "US",
    "NASDAQ": "US",
    "XNYS": "US",
    "XNAS": "US",
    "FUTURES": "FUTURES",
    "CME": "FUTURES",
}

#: 预取交易日列表时，首次窗口按 count 放大的自然日跨度。
#: 2*count+30 覆盖春节/国庆等连续休市；不够时翻倍重试。
_SPAN_PER_COUNT = 2
_SPAN_BASE_DAYS = 30
_MAX_EXPAND_ROUNDS = 5


class CalendarUnavailable(RuntimeError):
    """交易日历答不了（库缺失/越界/求值异常）。

    调用方必须把它当成"不能算"，而不是"按工作日近似"。展示面可以少画一个
    目标日；绝不能画一个错的。
    """


def normalize_market(market: str | None) -> str:
    """把 API 里的市场口径归一成 ``trading_calendar`` 可识别值。

    缺省（None/空串）按 CN：平台绝大多数模型是 A 股，且旧接口在缺市场时
    也是 A 股语义。未知市场抛 ValueError，由调用方决定是否降级。
    """
    raw = str(market or "").strip().upper()
    if not raw:
        return "CN"
    mapped = _MARKET_ALIASES.get(raw)
    if mapped:
        return mapped
    # 允许调用方直接传 exchange_calendars 的交易所码（如 XSHG）。
    if raw.startswith("X") and len(raw) == 4:
        return raw
    raise ValueError(f"未知市场代码: {market!r}")


#: 日历覆盖末日缓存：一个进程内日历印发年限不会变，只取一次。
_CAL_LAST_CACHE: dict[str, date | None] = {}


def _calendar_last_session(market: str) -> date | None:
    """交易日历的覆盖末日；取不到返回 None。"""
    key = normalize_market(market)
    if key in _CAL_LAST_CACHE:
        return _CAL_LAST_CACHE[key]
    last: date | None = None
    try:
        cal = _get_xcal_calendar(key)
        if cal is not None:
            last_session = cal.last_session
            last = (
                last_session.date() if hasattr(last_session, "date") else last_session
            )
    except Exception:  # noqa: BLE001 - 取不到覆盖边界就按「答不了」处理
        last = None
    _CAL_LAST_CACHE[key] = last
    return last


def _trading_days_clamped(market: str, start: date, end: date) -> list[date] | None:
    """``trading_days_xcal`` + 覆盖末日裁剪。

    ``trading_days_xcal`` 在 ``end`` 越过日历年尾时整体拒绝作答（它会拒绝
    部分覆盖的窗口）。图表的批量目标日通常只需要窗口尾部，因此把 ``end``
    裁到日历末日再取；真正越界的点由调用方按索引判空。
    """
    last = _calendar_last_session(market)
    if last is not None:
        if start > last:
            return None
        if end > last:
            end = last
    return trading_days_xcal(normalize_market(market), start, end)


def trading_days_between(market: str, start: date, end: date) -> list[date]:
    """闭区间 ``[start, end]`` 的交易日列表；日历答不了时抛异常。"""
    days = trading_days_xcal(normalize_market(market), start, end)
    if days is None:
        raise CalendarUnavailable(
            f"交易日历不可用：market={market} range={start}..{end}"
        )
    return list(days)


def next_trading_days(market: str, after: date, count: int) -> list[date]:
    """``after`` **之后**的连续 ``count`` 个交易日（升序）。

    只做交易日推进，不跨自然日猜。日历覆盖不足时抛 :class:`CalendarUnavailable`。
    """
    n = int(count)
    if n <= 0:
        return []
    span = max(_SPAN_BASE_DAYS, n * _SPAN_PER_COUNT)
    last_error: Exception | None = None
    for _ in range(_MAX_EXPAND_ROUNDS):
        end = after + timedelta(days=span)
        try:
            days = _trading_days_clamped(market, after + timedelta(days=1), end)
        except Exception as exc:  # noqa: BLE001 - 统一转成可识别的日历异常
            last_error = exc
            days = None
        if days is not None and len(days) >= n:
            return list(days[:n])
        if days is None:
            # 越界/库缺失：扩大窗口也不会变好，直接失败。
            raise CalendarUnavailable(
                f"交易日历不可用：market={market} after={after} count={n}"
            ) from last_error
        span *= 2
    raise CalendarUnavailable(
        f"交易日历覆盖不足：market={market} after={after} count={n}"
    )


@dataclass(frozen=True)
class LabelWindow:
    """一个分数日对应的执行/目标窗口（全部为交易日）。"""

    data_date: date
    entry_date: date
    target_date: date
    horizon: int
    execution_lag_days: int
    window_dates: tuple[date, ...]

    def dates(self) -> list[date]:
        """窗口内全部日期（含执行日与目标日，共 horizon+1 个）。"""
        return list(self.window_dates)


def label_window(
    market: str,
    data_date: date,
    horizon: int,
    execution_lag_days: int = 1,
) -> LabelWindow:
    """计算 ``data_date`` 的 T+N 执行/目标窗口。

    与训练标签 ``future_close.shift(-(lag + horizon)) / entry.shift(-lag) - 1``
    严格一致：``lag=1, horizon=5`` → entry=D+1、target=D+6。
    """
    h = int(horizon)
    lag = int(execution_lag_days)
    if h <= 0:
        raise ValueError(f"horizon 必须为正整数，收到 {horizon!r}")
    if lag < 0:
        raise ValueError(f"execution_lag_days 不能为负，收到 {execution_lag_days!r}")

    days = next_trading_days(market, data_date, lag + h)
    entry = days[lag - 1] if lag > 0 else data_date
    target = days[lag + h - 1]
    window = tuple(days[lag - 1 : lag + h]) if lag > 0 else (data_date, *days[:h])
    return LabelWindow(
        data_date=data_date,
        entry_date=entry,
        target_date=target,
        horizon=h,
        execution_lag_days=lag,
        window_dates=window,
    )


def label_window_dates(
    market: str,
    data_date: date,
    horizon: int,
    execution_lag_days: int = 1,
) -> list[date]:
    """窗口内全部交易日（执行日 → 目标日，共 horizon+1 个）。"""
    return label_window(market, data_date, horizon, execution_lag_days).dates()


def label_targets_bulk(
    market: str,
    data_dates: Iterable[date],
    horizon: int,
    execution_lag_days: int = 1,
) -> dict[date, date | None]:
    """批量计算每个数据日对应的目标日（图表逐点 target_date 用）。

    一次取够覆盖区间，再按索引偏移，避免 N 个点调 N 次日历服务。
    日历覆盖不足/某日不是交易日时，该点返回 ``None``（前端不落点），
    不猜日期。
    """
    h = int(horizon)
    lag = int(execution_lag_days)
    if h <= 0:
        raise ValueError(f"horizon 必须为正整数，收到 {horizon!r}")
    if lag < 0:
        raise ValueError(f"execution_lag_days 不能为负，收到 {execution_lag_days!r}")

    dates = sorted({d for d in data_dates if isinstance(d, date)})
    if not dates:
        return {}
    offset = lag + h
    start = dates[0]
    # 先按最大跨度拉一次；如果最后几个点的目标日越界，再按需扩大。
    span = max(_SPAN_BASE_DAYS, (offset + 5) * _SPAN_PER_COUNT)
    end = dates[-1] + timedelta(days=span)
    try:
        calendar_days = _trading_days_clamped(normalize_market(market), start, end)
    except Exception:  # noqa: BLE001
        calendar_days = None
    if calendar_days is None:
        raise CalendarUnavailable(f"交易日历不可用：market={market}")
    index = {d: i for i, d in enumerate(calendar_days)}
    out: dict[date, date | None] = {}
    for d in dates:
        i = index.get(d)
        if i is None:
            out[d] = None
            continue
        j = i + offset
        out[d] = calendar_days[j] if 0 <= j < len(calendar_days) else None
    return out
