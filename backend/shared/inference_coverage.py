"""推理覆盖日期计算的轻量工具。"""

from datetime import date


def trading_dates_between(start_date: str | date, end_date: str | date) -> list[str]:
    """返回闭区间内的上交所交易日，日历不可用时返回空列表。"""
    try:
        import exchange_calendars as xcals
        import pandas as pd

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        if start > end:
            return []
        cal = xcals.get_calendar("XSHG")
        return [
            session.strftime("%Y-%m-%d")
            for session in cal.sessions_in_range(start, end)
        ]
    except Exception:
        return []


def find_inference_gap_dates(
    covered_dates: list[str], data_cutoff_date: str | date
) -> list[str]:
    """找出模型首个真实推理日之后的全部交易日缺口。

    不能只比较最大日期：补全任务或每日推理偶发失败时，pred.parquet 可能
    在已有最大日期之前留下空洞。以首个真实推理日作为左边界，既能找出
    中间缺口，也不会把训练前的历史数据误加入补全队列。
    """
    normalized = {
        str(value)[:10]
        for value in covered_dates
        if value is not None and str(value)[:10]
    }
    if not normalized:
        return []
    return [
        trade_date
        for trade_date in trading_dates_between(min(normalized), data_cutoff_date)
        if trade_date not in normalized
    ]
