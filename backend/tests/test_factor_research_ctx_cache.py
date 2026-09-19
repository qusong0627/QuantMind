"""因子研究「区间公共上下文」的缓存、单飞与惰性构建。

背景（2026-09-19）：私人库重建后因子数从 327 涨到 2754，`_range_ctx` 冷构建变成
~65s（面板构造 25s + 全因子 top-30 序列 40s）。原实现有两个问题，都被现场日志坐实：

1. 缓存是 10 分钟 TTL，而快照其实只在重建时变 —— 等于每 10 分钟让第一个请求白卡一次；
2. 没有单飞：因子研究页并发 6 个请求各建一份，内存与 GIL 一起打满，引擎随即被看门狗
   判「健康检查无响应」重启（19:15:06 日志：health check failed 3/3 → restarting）。

顺带把只有排行榜/单因子详情要用的派生量（series30/env_tags）拆出来按需构建，
对比/合成/寻优端点不再陪着等那 40s。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from backend.services.engine.factor_research import scorecard, service

_MONTHS = ["2026-01-31", "2026-02-28", "2026-03-31"]
_SYMS = ["000001.SZ", "600000.SH"]


def _tiny_panel() -> scorecard.Panel:
    rows = []
    for code in ("A", "B", "C"):
        for mi, mon in enumerate(_MONTHS):
            for rk, sym in enumerate(_SYMS, 1):
                rows.append(
                    {
                        "factor_code": code,
                        "trade_date": pd.Timestamp(mon),
                        "rank": rk,
                        "symbol": sym,
                        "score": float(10 - rk - mi),
                        "raw": float(rk),
                        "fwd_ret": 0.01 * rk,
                    }
                )
    return scorecard.Panel(pd.DataFrame(rows))


@pytest.fixture()
def env(monkeypatch):
    """把 store 换成内存迷你面板，并计数「面板构建」与「全因子序列」各发生几次。"""
    service._ctx_cache.clear()
    service._derived_cache.clear()
    real_topn = scorecard.topn_series
    state = {
        "stamp": "snap-1",
        "panel": _tiny_panel(),
        "builds": 0,  # store.panel 调用次数
        "topn": 0,  # topn_series 调用次数
        "delay": 0.0,  # 模拟面板构建耗时（单飞用）
        "topn_delay": 0.0,  # 模拟派生构建耗时（并发用）
        "topn_entered": threading.Event(),
    }

    def fake_panel(dataset="classic"):
        state["builds"] += 1
        if state["delay"]:
            time.sleep(state["delay"])
        return state["panel"]

    def counting_topn(*args, **kwargs):
        state["topn"] += 1
        state["topn_entered"].set()
        if state["topn_delay"] and state["topn"] == 1:
            time.sleep(state["topn_delay"])
        return real_topn(*args, **kwargs)

    monkeypatch.setattr(service.store, "panel", fake_panel)
    monkeypatch.setattr(service.store, "panel_stamp", lambda dataset="classic": state["stamp"])
    monkeypatch.setattr(service.store, "benchmark_table", lambda *a, **k: None)
    monkeypatch.setattr(service.store, "ic_table", lambda *a, **k: None)
    monkeypatch.setattr(scorecard, "topn_series", counting_topn)
    yield state
    service._ctx_cache.clear()
    service._derived_cache.clear()


def test_ctx_cached_without_ttl(env):
    """同一区间重复取：只构建一次（没有 TTL 到期重算）。"""
    first = service._range_ctx(None, None, "private")

    assert env["builds"] == 1, "首次必须真构建，否则下面的缓存断言是空转"

    assert service._range_ctx(None, None, "private") is first
    assert env["builds"] == 1, "命中缓存却重建了面板"


def test_ctx_survives_long_idle(env, monkeypatch):
    """没有 TTL：空闲一天后仍吃缓存 —— 快照没变就不该把 65s 的构建重做一遍。"""
    first = service._range_ctx(None, None, "private")

    assert env["builds"] == 1, "首次必须真构建，否则本用例是空转"

    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 86400)

    assert service._range_ctx(None, None, "private") is first
    assert env["builds"] == 1, "空闲后被重算 —— 说明缓存又按时间过期了"


def test_ctx_invalidated_by_snapshot_stamp(env):
    """快照 mtime 一变（重建落盘）缓存立即作废，不必等 TTL。"""
    first = service._range_ctx(None, None, "private")
    assert first["panel"] is env["panel"]

    env["stamp"] = "snap-2"
    env["panel"] = _tiny_panel()

    second = service._range_ctx(None, None, "private")

    assert env["builds"] == 2, "换了快照却仍吃旧缓存"
    assert second["panel"] is env["panel"], "拿到的是旧快照的面板"
    assert second is not first


def test_ctx_build_is_single_flight(env):
    """并发冷启动只构建一次 —— 否则 6 份 65s/6GB 的构建会把引擎顶死。"""
    env["delay"] = 0.3
    out: list[dict] = []

    threads = [
        threading.Thread(target=lambda: out.append(service._range_ctx(None, None, "private")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(out) == 8, f"有线程没返回（{len(out)}/8）"
    assert env["builds"] == 1, f"单飞失败：并发 8 请求建了 {env['builds']} 份"
    assert len({id(o) for o in out}) == 1, "并发请求拿到的是不同的上下文对象"


def test_derived_built_lazily_and_only_once(env):
    """全因子序列只在需要时算，且只算一次。"""
    light = service._range_ctx(None, None, "private")

    assert env["topn"] == 0, "轻上下文触发了全因子序列（最慢的一段）"
    assert "series30" not in light and "env_tags" not in light

    full = service._range_ctx_full(None, None, "private")
    n_codes = len(full["panel"].codes)

    assert n_codes == 3, "用例规模变了，参与量断言要跟着改"
    assert env["topn"] == n_codes, f"应逐因子各算一次，实得 {env['topn']}/{n_codes}"
    assert set(full) >= {"panel", "mask", "rdates", "benches", "ic", "series30", "env_tags", "time_tags"}
    assert len(full["series30"]) == n_codes

    again = service._range_ctx_full(None, None, "private")

    assert env["topn"] == n_codes, "第二次取派生量又重算了"
    assert again["series30"] is full["series30"]


def test_derived_build_does_not_block_light_path(env):
    """派生量那把锁与轻上下文那把锁分开：一个慢排行榜不该堵住秒级端点。"""
    env["topn_delay"] = 0.6
    worker = threading.Thread(target=lambda: service._range_ctx_full(None, None, "private"))
    worker.start()
    try:
        assert env["topn_entered"].wait(5), "派生构建没开始，本用例没测到并发"

        t0 = time.monotonic()
        service._range_ctx("2026-03", None, "private")  # 另一个区间的轻上下文
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"轻上下文被派生构建堵了 {elapsed:.2f}s"
    finally:
        worker.join(10)


def test_ctx_cache_is_bounded(env):
    """区间探索不设上限会一直攒上下文（每份几十 MB），超过上限淘汰最旧的。"""
    n_distinct = service._MAX_CTX + 2
    for i in range(n_distinct):
        service._range_ctx(f"2026-{i + 1:02d}", None, "private")

    assert env["builds"] == n_distinct, "每个不同区间都应各建一次"
    assert len(service._ctx_cache) == service._MAX_CTX, "缓存条目数未收敛到上限"
