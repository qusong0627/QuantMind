"""港股日线同步的**卡死防护**（2026-11-06 事故）。

那天 HK 跨日补跑挂了 110 分钟、CPU 0.14%、进程 S 态：akshare 的
``stock_hk_daily`` 内部 ``requests.get`` **不传 timeout**，一条死连接就让
``as_completed`` 永远等下，直到任务软超时（``MARKET_SYNC_SOFT_TIME_LIMIT`` =
115 分钟）被杀——当晚本可落盘的 11-05 数据一行没写。单只实测只有 0.3 秒、
6 并发本该 2.4 分钟跑完，所以这不是「慢」，是「卡住」。

两条纪律各自锁一组用例：

* 默认 HTTP 超时**只补调用方没传的**（不许改写别人的超时语义），且幂等；
* 整轮抓取有预算兜底：到点后不再等剩余标的，**已经拿到的不许丢**
  （``timed_out`` 单列计数，已完成的照常落盘）。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from backend.scripts import quanthk_akshare_kline as m


@pytest.fixture()
def fresh_http(monkeypatch):
    """把可能已被打过补丁的 ``Session.request`` 还原，保证用例之间互不污染。"""
    import requests

    patched = requests.Session.request
    monkeypatch.setattr(
        requests.Session, "request", getattr(patched, "_qm_original", patched)
    )
    yield


def test_default_timeout_only_fills_missing(fresh_http, monkeypatch) -> None:
    """显式传了 timeout 的调用方一律不动——别人的超时语义不该被我们改写。"""
    import requests

    seen: dict[str, object] = {}

    def fake_request(self, method, url, **kwargs):  # noqa: ANN001, ARG001
        seen["timeout"] = kwargs.get("timeout")
        return None

    monkeypatch.setattr(requests.Session, "request", fake_request)
    m._install_default_http_timeout(7.0)

    requests.Session().request("GET", "http://example.invalid/a")
    assert seen["timeout"] == 7.0

    requests.Session().request("GET", "http://example.invalid/b", timeout=99)
    assert seen["timeout"] == 99


def test_install_is_idempotent(fresh_http, monkeypatch) -> None:
    """重复安装会把超时层层包裹，最后按最内层生效——必须只装一次。"""
    import requests

    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **k: None)
    m._install_default_http_timeout(5.0)
    first = requests.Session.request

    m._install_default_http_timeout(9.0)

    assert requests.Session.request is first


def _bar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["0700.HK"],
            "time": [pd.Timestamp("2026-11-05")],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100],
            "amount": [100.0],
            "release_id": ["akshare"],
            "published_at": ["2026-11-06T00:00:00"],
        }
    )


def test_budget_stops_waiting_and_keeps_finished(
    fresh_http, monkeypatch, tmp_path
) -> None:
    """一只卡住不许拖垮整轮：到预算就收手，已拿到的照常落盘并如实计数。"""
    monkeypatch.setattr(m, "_existing_partitions", lambda: set())
    monkeypatch.setattr(m, "_quanthk_root", lambda: tmp_path)
    monkeypatch.setattr(m, "DEFAULT_FETCH_BUDGET", 0.3)

    def fake_fetch(symbol: str, min_date: str):  # noqa: ARG001
        if symbol == "09999":
            time.sleep(2)  # 模拟死连接：远超预算
            return None
        return _bar_frame()

    monkeypatch.setattr(m, "_fetch_stock", fake_fetch)

    out = m.sync(symbols=["00001", "00002", "09999"], days=5, concurrent=3)

    assert out["ok"] == 2
    assert out["timed_out"] == 1, "卡住的那只必须单列，不能混进 err"
    part = tmp_path / "1_kline_data" / "daily_forward" / "dt=20261105" / "data.parquet"
    assert part.exists(), "已拿到的数据不许因为一只卡住就整轮白跑"


def test_all_hung_still_returns_instead_of_blocking(
    fresh_http, monkeypatch, tmp_path
) -> None:
    """全部卡住是最危险的形态：没有 as_completed(timeout=) 时一个 future 都不会完成，
    循环体里的 deadline 判断永远轮不到，整轮挂到任务软超时。"""
    monkeypatch.setattr(m, "_existing_partitions", lambda: set())
    monkeypatch.setattr(m, "_quanthk_root", lambda: tmp_path)
    monkeypatch.setattr(m, "DEFAULT_FETCH_BUDGET", 0.3)

    def fake_fetch(symbol: str, min_date: str):  # noqa: ARG001
        time.sleep(2)
        return None

    monkeypatch.setattr(m, "_fetch_stock", fake_fetch)

    started = time.monotonic()
    out = m.sync(symbols=["00001", "00002"], days=5, concurrent=2)

    assert out["timed_out"] == 2
    assert out["rows"] == 0
    # 预算 0.3s：函数体必须在预算量级返回，不能等两个 2s 的线程
    assert time.monotonic() - started < 3.0
