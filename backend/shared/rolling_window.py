"""滚动重训窗口计算（纯函数，无重型依赖，可单测）。

语义：把原模型的三段窗口（train/valid/test）整体按月平移，
以数据探针的最新日期为基准确定 auto 步长，其余训练参数原样复用。
"""

import bisect
import calendar
from datetime import date

WINDOW_KEYS = (
    "train_start",
    "train_end",
    "valid_start",
    "valid_end",
    "test_start",
    "test_end",
)
_START_KEYS = ("train_start", "valid_start", "test_start")


class RollingWindowError(ValueError):
    """窗口计算失败，router 按 http_status 转 HTTP 异常。"""

    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.http_status = http_status


def parse_ymd(value: object, field: str) -> date:
    """解析 YYYY-MM-DD，失败抛 RollingWindowError。"""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise RollingWindowError(f"原窗口日期非法：{field}={value!r}") from None


def add_months(d: date, n: int) -> date:
    """整月平移（保日，遇月末截断，如 1-31 +1月 → 2-28）。"""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def months_between(a: date, b: date) -> int:
    """b 相对 a 的整月差（只看年月，如 2026-08 → 2026-09 = 1）。"""
    return (b.year - a.year) * 12 + (b.month - a.month)


def snap_up(trading_dates: list[str], target: str) -> str | None:
    """落在交易日上：起点取 ≥ target 的首个交易日。"""
    i = bisect.bisect_left(trading_dates, target)
    return trading_dates[i] if i < len(trading_dates) else None


def snap_down(trading_dates: list[str], target: str) -> str | None:
    """落在交易日上：终点取 ≤ target 的末个交易日。"""
    i = bisect.bisect_right(trading_dates, target) - 1
    return trading_dates[i] if i >= 0 else None


def compute_rolled_split(
    original: dict[str, str],
    trading_dates: list[str],
    latest_date: str,
    shift_months: int | None = None,
) -> tuple[dict[str, str], int, list[str]]:
    """计算滚动后的三段窗口。

    - shift_months 为空时 auto：latest 相对原 test_end 的整月差；
    - 六个边界同值平移（段内时长与段间 gap 保持不变），再吸附到交易日；
    - 返回 (新窗口, 实际步长(月), 提示列表)。
    """
    if not trading_dates:
        raise RollingWindowError("数据探针无交易日序列，无法确定新窗口")
    orig = {k: parse_ymd(original.get(k), k) for k in WINDOW_KEYS}
    latest = parse_ymd(latest_date, "latest_date")

    if shift_months is None:
        shift = months_between(orig["test_end"], latest)
        if shift <= 0:
            raise RollingWindowError(
                f"数据已是最新（原 test_end={orig['test_end']} ≥ 数据末端"
                f"{latest}），无需滚动",
                http_status=409,
            )
    else:
        shift = int(shift_months)
        if shift <= 0:
            raise RollingWindowError("shift_months 须为正整数")

    first_day, last_day = trading_dates[0], trading_dates[-1]
    new_window: dict[str, str] = {}
    warnings: list[str] = []
    for key in WINDOW_KEYS:
        moved = add_months(orig[key], shift).isoformat()
        if key in _START_KEYS:
            if moved < first_day or moved > last_day:
                raise RollingWindowError(
                    f"新 {key}={moved} 超出数据覆盖 [{first_day}, {last_day}]"
                )
            snapped = snap_up(trading_dates, moved)
            assert snapped is not None  # 范围内必命中
            new_window[key] = snapped
        else:
            if moved < first_day:
                raise RollingWindowError(
                    f"新 {key}={moved} 早于数据起点 {first_day}"
                )
            snapped = snap_down(trading_dates, moved)
            assert snapped is not None
            new_window[key] = snapped
        if snapped != moved:
            warnings.append(f"{key} {moved} 非交易日，吸附为 {snapped}")

    order = [new_window[k] for k in WINDOW_KEYS]
    if not (
        order[0] <= order[1] < order[2]
        <= order[3] < order[4] <= order[5]
    ):
        raise RollingWindowError(f"平移后窗口顺序非法：{new_window}")
    if new_window["test_end"] <= orig["test_end"].isoformat():
        raise RollingWindowError(
            f"新窗口未推进（新 test_end={new_window['test_end']}），"
            "数据末端可能无更新",
            http_status=409,
        )
    return new_window, shift, warnings
