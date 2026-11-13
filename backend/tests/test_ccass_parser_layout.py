"""CCASS 页面解析布局契约：5 列（含百分比）与 4 列（缺百分比）。

HKEX 对部分股票不返回「占已发行股份百分比」列。旧实现按 len(cols)>4 取列，
缺列时 fallback 到持股量列：带逗号的数 → 0，少于 1000 股 → 数量/100。
本测试锁死：缺列必须是 None，落库层必须是 NaN，绝不能伪造百分比。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("akshare")
pytest.importorskip("yfinance")

from backend.scripts.ccass_crawler import AsyncHKEXFetcher
from backend.scripts.quanthk_ccass_sync import _normalise_fetch


def _html(has_pct: bool, qty: str, pct: str | None = None) -> str:
    pct_th = "<th>佔已發行股份/權證/單位百分比</th>" if has_pct else ""
    pct_td = (
        f"<td>佔已發行股份/權證/單位百分比:{pct}</td>" if has_pct else ""
    )
    return f"""
    <html><body>
    <table class="table">
      <thead>
        <tr>
          <th>參與者編號</th><th>參與者名稱</th><th>地址</th><th>持股量</th>{pct_th}
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>參與者編號:C00019</td>
          <td>參與者名稱:香港上海匯豐銀行有限公司</td>
          <td>地址:HONG KONG</td>
          <td>持股量:{qty}</td>
          {pct_td}
        </tr>
      </tbody>
    </table>
    </body></html>
    """


def test_five_column_layout_parses_percentage():
    df = AsyncHKEXFetcher._parse_html(_html(True, "1,393,732,677", "36.38%"))
    assert df is not None and len(df) == 1
    row = df.iloc[0]
    assert row["参与者编号"] == "C00019"
    assert row["持股数量"] == 1_393_732_677
    assert row["占已发行股份百分比"] == "36.38%"


def test_four_column_layout_returns_none_not_shareholding():
    df = AsyncHKEXFetcher._parse_html(_html(False, "1,393,732,677"))
    assert df is not None and len(df) == 1
    row = df.iloc[0]
    assert row["持股数量"] == 1_393_732_677
    assert row["占已发行股份百分比"] is None


def test_small_quantity_without_percentage_is_not_written_as_pct():
    """旧 bug：990 股（无逗号）会被写成 990/100 = 9.9%。"""
    df = AsyncHKEXFetcher._parse_html(_html(False, "990"))
    assert df is not None
    assert df.iloc[0]["占已发行股份百分比"] is None


def test_normalise_fetch_missing_percentage_becomes_nan():
    raw = pd.DataFrame(
        {
            "参与者编号": ["C00019", "B01955"],
            "参与者名称": ["香港上海匯豐銀行有限公司", "富途證券國際(香港)有限公司"],
            "持股数量": [1_393_732_677, 990],
            "占已发行股份百分比": [None, ""],
        }
    )
    out = _normalise_fetch(raw, "00001", "长和", pd.Timestamp("2026-11-12").date())
    assert out is not None
    assert out["holding_quantity"].tolist() == [1_393_732_677, 990]
    assert np.isnan(out["holding_percentage"]).all()


def test_normalise_fetch_parses_percent_and_rejects_over_100():
    raw = pd.DataFrame(
        {
            "参与者编号": ["C00019", "B01955"],
            "参与者名称": ["A", "B"],
            "持股数量": [1_393_732_677, 990],
            "占已发行股份百分比": ["36.38%", "990%"],
        }
    )
    out = _normalise_fetch(raw, "00001", "长和", pd.Timestamp("2026-11-12").date())
    assert out is not None
    pct = out.sort_values("holding_quantity", ascending=False)["holding_percentage"]
    assert pct.iloc[0] == pytest.approx(0.3638)
    assert np.isnan(pct.iloc[1])
