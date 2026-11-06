"""Pure date-window validation and Qlib split construction for RD-Agent mining."""

from __future__ import annotations

from datetime import date, timedelta


def build_mining_window_env(
    mining_start: str | None,
    mining_end: str | None,
    valid_start: str | None,
    valid_end: str | None,
) -> dict[str, str]:
    """Return task-scoped RD-Agent date variables; empty dates preserve defaults."""
    mining_start = (mining_start or "").strip()
    mining_end = (mining_end or "").strip()
    valid_start = (valid_start or "").strip()
    valid_end = (valid_end or "").strip()
    if not mining_start and not mining_end and not valid_start and not valid_end:
        return {}
    if bool(mining_start) != bool(mining_end):
        raise ValueError("挖掘区间须同时填写起止日期")
    if bool(valid_start) != bool(valid_end):
        raise ValueError("因子检验区间须同时填写起止日期")
    if not mining_start:
        raise ValueError("指定因子检验区间时必须同时指定挖掘区间")

    mining = _parse_window(mining_start, mining_end, "挖掘区间")
    validation = (
        _parse_window(valid_start, valid_end, "因子检验区间") if valid_start else None
    )
    if validation is not None and validation[0] <= mining[1]:
        raise ValueError("因子检验区间必须从挖掘区间结束日之后开始")

    mining_s, mining_e = mining
    span_days = (mining_e - mining_s).days
    train_cut = mining_s + timedelta(days=round(span_days * 0.70))
    if validation is not None:
        valid_cut = mining_e
        test = validation
    else:
        valid_cut = mining_s + timedelta(days=round(span_days * 0.85))
        test = (valid_cut, mining_e)

    train = (mining_s, train_cut)
    valid = (train_cut, valid_cut)
    env = {
        "QM_MINING_WINDOW_START": mining_s.isoformat(),
        "QM_MINING_WINDOW_END": mining_e.isoformat(),
        "QM_QLIB_TRAIN_START": train[0].isoformat(),
        "QM_QLIB_TRAIN_END": train[1].isoformat(),
        "QM_QLIB_VALID_START": valid[0].isoformat(),
        "QM_QLIB_VALID_END": valid[1].isoformat(),
        "QM_QLIB_TEST_START": test[0].isoformat(),
        "QM_QLIB_TEST_END": test[1].isoformat(),
        "QLIB_FACTOR_TRAIN_START": train[0].isoformat(),
        "QLIB_FACTOR_TRAIN_END": train[1].isoformat(),
        "QLIB_FACTOR_VALID_START": valid[0].isoformat(),
        "QLIB_FACTOR_VALID_END": valid[1].isoformat(),
        "QLIB_FACTOR_TEST_START": test[0].isoformat(),
        "QLIB_FACTOR_TEST_END": test[1].isoformat(),
    }
    if validation is not None:
        env["QM_VALIDATION_WINDOW_START"] = validation[0].isoformat()
        env["QM_VALIDATION_WINDOW_END"] = validation[1].isoformat()
    return env


def _parse_window(start: str, end: str, label: str) -> tuple[date, date]:
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError(f"{label}日期须为有效的 YYYY-MM-DD") from exc
    if start_date.isoformat() != start or end_date.isoformat() != end:
        raise ValueError(f"{label}日期须为 YYYY-MM-DD")
    if start_date > end_date:
        raise ValueError(f"{label}开始日期不能晚于结束日期")
    return start_date, end_date


def ic_window_from_env(env: dict[str, str]) -> tuple[str, str, str] | None:
    """Resolve the factor metric window from the same frozen task segments."""
    validation = (
        env.get("QM_VALIDATION_WINDOW_START", "").strip(),
        env.get("QM_VALIDATION_WINDOW_END", "").strip(),
    )
    if bool(validation[0]) != bool(validation[1]):
        raise ValueError("Frozen validation window is incomplete")
    if validation[0]:
        _parse_window(*validation, "因子检验区间")
        return validation[0], validation[1], "validation"

    mining = (
        env.get("QM_MINING_WINDOW_START", "").strip(),
        env.get("QM_MINING_WINDOW_END", "").strip(),
    )
    if not mining[0] and not mining[1]:
        return None
    _parse_window(*mining, "挖掘区间")
    test = (
        env.get("QM_QLIB_TEST_START", "").strip(),
        env.get("QM_QLIB_TEST_END", "").strip(),
    )
    _parse_window(*test, "挖掘区间测试段")
    return test[0], test[1], "mining_test"
