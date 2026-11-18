"""pre5 缓存单测（生产新闻决策用）。"""

from __future__ import annotations

import pandas as pd

from backend.shared.news_pre5 import pre5_from_frame, pre5_lookup


def _frame():
    rows = []
    # 600000: +1% every day; 600001: -1% every day
    for sym, step in (("600000.SH", 0.01), ("600001.SH", -0.01)):
        price = 10.0
        for i, dt in enumerate([20261110, 20261111, 20261112, 20261113, 20261114, 20261117, 20261118]):
            if i:
                price *= 1 + step
            rows.append({"symbol": sym, "dt": dt, "close": price})
    return pd.DataFrame(rows)


def test_pre5_uses_previous_five_days():
    out = pre5_from_frame(_frame())
    assert abs(out["600000.SH"] - 0.05) < 1e-9
    assert abs(out["600001.SH"] + 0.05) < 1e-9


def test_pre5_lookup_accepts_prefix_suffix_and_plain():
    m = {"600000.SH": 0.03}
    assert pre5_lookup("600000.SH", m) == 0.03
    assert pre5_lookup("SH600000", m) == 0.03
    assert pre5_lookup("600000", m) == 0.03
    assert pre5_lookup("000001.SZ", m) is None
