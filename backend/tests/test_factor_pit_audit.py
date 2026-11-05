"""财务因子 PIT 审计（A3）测试。"""

from __future__ import annotations

import pandas as pd

from backend.scripts import audit_factor_pit as audit
from backend.scripts import financial_pit_panel as panel


def _pit_frame(reverse_second: bool = False) -> pd.DataFrame:
    symbols = [f"{600000 + i}.SH" for i in range(10)]
    first = list(range(1, 11))
    second = list(range(10, 0, -1)) if reverse_second else list(range(11, 21))
    rows = []
    for idx, symbol in enumerate(symbols):
        rows.append(
            {
                "symbol": symbol,
                "report_date": "2019-12-31",
                "ann_date": "2020-03-30",
                "roe": first[idx],
                "profit_growth": first[idx],
            }
        )
        rows.append(
            {
                "symbol": symbol,
                "report_date": "2020-03-31",
                "ann_date": "2020-04-30",
                "roe": second[idx],
                "profit_growth": second[idx],
            }
        )
    return pd.DataFrame(rows)


def _v5_frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"{600000 + i}.SH" for i in range(10)],
            "fun_roe": values,
            "fun_np_growth": values,
        }
    )


def test_single_snapshot_is_insufficient():
    pit = _pit_frame(reverse_second=True)
    # V5 与「未公告的第二期」完全一致 → 与 report_asof 相关 1、与 pit_asof 相关 -1
    snapshots = [(pd.Timestamp("2020-04-15"), _v5_frame(list(range(10, 0, -1))))]

    result = audit.audit_snapshots(pit, snapshots, threshold=0.10)

    for item in result:
        assert item["valid_snapshots"] == 1
        assert item["status"] == "insufficient"  # 单快照不足以定论


def test_contaminated_factor_flags_with_two_snapshots():
    pit = _pit_frame(reverse_second=True)
    v5 = _v5_frame(list(range(10, 0, -1)))
    snapshots = [
        (pd.Timestamp("2020-04-15"), v5),
        (pd.Timestamp("2020-04-16"), v5),
    ]

    result = audit.audit_snapshots(pit, snapshots, threshold=0.10)

    for item in result:
        assert item["valid_snapshots"] == 2
        assert item["avg_corr_pit"] == -1.0
        assert item["avg_corr_report"] == 1.0
        assert item["status"] == "pit_pending"
        assert item["suspicious_snapshots"] == ["2020-04-15", "2020-04-16"]


def test_clean_factor_is_pit_ok():
    pit = _pit_frame(reverse_second=True)
    # V5 与已公告的第一期一致
    v5 = _v5_frame(list(range(1, 11)))
    snapshots = [
        (pd.Timestamp("2020-04-15"), v5),
        (pd.Timestamp("2020-04-16"), v5),
    ]

    result = audit.audit_snapshots(pit, snapshots, threshold=0.10)

    for item in result:
        assert item["avg_corr_pit"] == 1.0
        assert item["avg_corr_report"] == -1.0
        assert item["status"] == "pit_ok"


def test_panel_build_quarterly_uses_announcement_date():
    pit = _pit_frame(reverse_second=False)
    asofs = pd.DatetimeIndex([pd.Timestamp("2020-04-15")])

    out = panel.build_quarterly(pit, asofs)

    # 2020-04-15 时第二期尚未公告（ann 04-30）→ 应取第一期（1..10），不是第二期
    assert len(out) == 10
    assert sorted(out["roe"].tolist()) == list(range(1, 11))
    assert set(pd.to_datetime(out["report_date"]).dt.strftime("%Y-%m-%d")) == {
        "2019-12-31"
    }
