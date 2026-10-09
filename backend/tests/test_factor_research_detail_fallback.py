"""factor_detail / 排行榜持仓：断更因子回退到自身最近截面（2026-10-09 空表事故）。

面板按「该因子当月有值」才落行：源库断更（rd_mined 物化停在 08 月底、
gap_mined 停在 09 月中）时，全局末月整列缺行。引擎旧逻辑直接取全局末月 →
个股表只有表头、行业分布「—」、市值分布全 0，stocks_date 却写着面板末月
（假日期）。本文件用内存 store 钉住三条行为线：

1. **回退**：stocks/stocks_date/行业/市值分布全部取该因子自身最近有数据的截面；
2. **标记**：stocks_stale=True 让前端能提示「数据未更新到最新截面」；
3. **不误伤**：数据新鲜的因子不得被标 stale、不得回退。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.factor_research import scorecard, service

_MONTHS = ["2026-08-31", "2026-09-30", "2026-10-08"]  # 末月只属 FRESH
_SYMS = ["000001.SZ", "600000.SH"]


def _panel() -> scorecard.Panel:
    """FRESH 全 3 月有数据；STALE 停在 09-30（模拟 rd_mined 断更）。"""
    rows = []
    for code, months in (("FRESH", 3), ("STALE", 2)):
        for mi in range(months):
            for rk, sym in enumerate(_SYMS, 1):
                rows.append(
                    {
                        "factor_code": code,
                        "trade_date": pd.Timestamp(_MONTHS[mi]),
                        "rank": rk,
                        "symbol": sym,
                        "score": float(10 - rk - mi),
                        "raw": float(rk) * 0.5,
                        "fwd_ret": 0.01 * rk,
                    }
                )
    return scorecard.Panel(pd.DataFrame(rows))


def _factor_meta(code: str) -> dict:
    return {
        "code": code,
        "name_cn": f"因子{code}",
        "display_name": "",
        "l1": "L1",
        "l2": "L2",
        "direction": 1,
        "description": "",
        "formula": "",
        "wind_source": "",
        "available": True,
    }


@pytest.fixture()
def env(monkeypatch):
    """内存 store：面板/快照/元数据全部注入，绕开磁盘产物。"""
    service._ctx_cache.clear()
    service._derived_cache.clear()
    p = _panel()
    monkeypatch.setattr(service.store, "panel", lambda dataset="classic": p)
    monkeypatch.setattr(
        service.store, "panel_stamp", lambda dataset="classic": "snap-1"
    )
    monkeypatch.setattr(service.store, "benchmark_table", lambda *a, **k: None)
    monkeypatch.setattr(service.store, "ic_table", lambda *a, **k: None)
    monkeypatch.setattr(
        service.store,
        "factors_meta",
        lambda dataset="private": {
            "factors": [_factor_meta("FRESH"), _factor_meta("STALE")]
        },
    )
    snap = pd.DataFrame(
        [
            {
                "symbol": sym,
                "name": f"名称{idx}",
                "industry": "银行",
                "total_mv_yi": 500.0 + idx,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "avg_amount_yi": 3.0,
            }
            for idx, sym in enumerate(_SYMS)
        ]
    )
    monkeypatch.setattr(service.store, "stock_snapshot", lambda: snap)
    yield
    service._ctx_cache.clear()
    service._derived_cache.clear()


def test_detail_falls_back_to_own_last_month(env):
    """断更因子：回退截面 + 真日期 + stale 标记，个股/分布全部有数据。"""
    d = service.factor_detail("STALE", ns=[1], stocks_n=30, dataset="private")

    assert d is not None
    assert d["stocks_date"] == "2026-09-30", "日期必须是回退截面的真实日期"
    assert d["stocks_stale"] is True
    assert [s["symbol"] for s in d["stocks"]] == _SYMS, "回退截面必须填出个股表"
    assert d["stocks"][0]["industry"] == "银行"
    assert d["industry_dist"] == [{"name": "银行", "count": 2}]
    assert sum(r["count"] for r in d["cap_dist"]) == 2, "市值分布不许全 0"


def test_detail_fresh_factor_not_marked_stale(env):
    """数据新鲜：取全局末月、stale=False —— 回退不许误伤正常因子。"""
    d = service.factor_detail("FRESH", ns=[1], stocks_n=30, dataset="private")

    assert d is not None
    assert d["stocks_date"] == "2026-10-08"
    assert d["stocks_stale"] is False
    assert len(d["stocks"]) == 2
