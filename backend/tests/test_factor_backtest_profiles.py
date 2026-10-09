"""T-FB-01 单测：市场档案注册表（单源）。

档案是「回测中心」一切参数的唯一出处：provider 解析、universe 模式、默认
窗口、费率、基准、实验性标注。CN 是**样本内基准列**（用户要求「A股也要别的
市场也要对比」），其余为样本外；BC/FUTURES 结构不同（7×24 / 合约混合），
标实验性但可选。
"""

from pathlib import Path

import pytest

from backend.services.engine.factor_backtest import profiles as P
from backend.services.engine.factor_backtest.compat import (
    BASE_COLUMNS,
    CN_MINING_COLUMNS,
)

pytestmark = pytest.mark.unit


# ── 注册表完整性 ─────────────────────────────────────────────────────


def test_registry_has_five_markets_with_qlib_mapping():
    """五市场档案齐备，应用侧键与 qlib 键映射固定。"""
    profs = {p.market: p for p in P.list_market_profiles()}
    assert set(profs) == {"a_share", "hong_kong", "us_stock", "crypto", "futures"}
    assert profs["a_share"].qlib_market == "CN"
    assert profs["hong_kong"].qlib_market == "HK"
    assert profs["us_stock"].qlib_market == "US"
    assert profs["crypto"].qlib_market == "CRYPTO"
    assert profs["futures"].qlib_market == "FUTURES"


def test_only_a_share_is_in_sample_baseline():
    """CN 列 = 样本内基准（挖掘原始市场）；其余全部样本外。"""
    for p in P.list_market_profiles():
        assert p.in_sample is (p.market == "a_share")


def test_crypto_and_futures_are_experimental():
    """BC/FUTURES 结构差异大（7×24 / 合约混合）——标实验性，可选但注明。"""
    flags = {p.market: p.experimental for p in P.list_market_profiles()}
    assert flags["a_share"] is False
    assert flags["hong_kong"] is False
    assert flags["us_stock"] is False
    assert flags["crypto"] is True
    assert flags["futures"] is True


def test_default_universe_per_market():
    """默认 universe：CN 沪深300；US 全列（本就精选池）；HK 流动性 top-N。"""
    profs = {p.market: p for p in P.list_market_profiles()}
    assert profs["a_share"].default_universe == "csi300"
    assert profs["us_stock"].universe_mode == "all"
    assert profs["hong_kong"].universe_mode == "liquid_top_n"
    assert profs["hong_kong"].universe_top_n >= 100


def test_benchmark_profile_real_index_vs_equal_weight():
    """T-FB-19 基准档案：CN/HK/US 请求真实指数；crypto/futures 声明即等权兜底。

    真实指数能否取到由 ``benchmarks.load_benchmark_returns`` 裁决（读数失败
    回落等权并在载荷如实标注）；此处钉的是**档案声明**这一层。
    """
    expected = {
        "a_share": "csi300",
        "hong_kong": "hsi",
        "us_stock": "spx",
        "crypto": "equal_weight",
        "futures": "equal_weight",
    }
    for p in P.list_market_profiles():
        assert p.benchmark == expected[p.market]


def test_research_cost_profile_audit():
    """T-FB-19 费率审计：CN 20 / HK 25 / US 10 / 加密 20 / 期货 5（双边 bps）。

    口径 = 显性交易成本（佣金/印花税/规费）+ 保守滑点；审计依据见
    ``profiles._PROFILES`` 各档案上方注释。
    """
    expected = {
        "a_share": 20,
        "hong_kong": 25,
        "us_stock": 10,
        "crypto": 20,
        "futures": 5,
    }
    for p in P.list_market_profiles():
        assert p.cost_bps == expected[p.market]


# ── 列集与窗口 ───────────────────────────────────────────────────────


def test_columns_for_market_cn_uses_mining_contract():
    """CN 分类列集 = 39 列挖掘契约；其余市场 = 基础 7 列（bin 实测）。"""
    assert P.columns_for_market("a_share") == CN_MINING_COLUMNS
    for market in ("hong_kong", "us_stock", "crypto", "futures"):
        assert P.columns_for_market(market) == BASE_COLUMNS


def test_default_window_is_last_n_years_from_calendar_end(monkeypatch):
    """默认窗口 = 日历末日往前 N 年（N=档案值，可被 env 覆盖）。"""
    monkeypatch.setattr(
        P, "_calendar_bounds", lambda provider: ("2016-01-04", "2026-10-08")
    )
    p = P.get_market_profile("a_share")
    start, end = P.default_window(p)
    assert end == "2026-10-08"
    assert start == "2023-10-08"


def test_default_window_clamps_to_short_calendar(monkeypatch):
    """数据长度不足窗口年数时：起点钳制在日历首日（否则空跑）。"""
    monkeypatch.setenv("QM_BACKTEST_WINDOW_YEARS", "3")
    monkeypatch.setattr(
        P, "_calendar_bounds", lambda provider: ("2025-08-10", "2026-08-15")
    )
    p = P.get_market_profile("crypto")
    start, end = P.default_window(p)
    assert start == "2025-08-10"
    assert end == "2026-08-15"


def test_default_window_env_override(monkeypatch):
    """QM_BACKTEST_WINDOW_YEARS 覆盖档案默认窗口年数。"""
    monkeypatch.setenv("QM_BACKTEST_WINDOW_YEARS", "5")
    monkeypatch.setattr(
        P, "_calendar_bounds", lambda provider: ("2016-01-04", "2026-10-08")
    )
    start, end = P.default_window(P.get_market_profile("us_stock"))
    assert start == "2021-10-08"


def test_unknown_market_raises_keyerror():
    """未知市场显式报错（路由层翻 400），绝不静默落 CN。"""
    with pytest.raises(KeyError):
        P.get_market_profile("mars")


# ── profile_status ───────────────────────────────────────────────────


def _make_fake_provider(root, market: str, *, instruments=("a", "b")) -> str:
    """搭一个最小可用的 qlib provider 目录布局。"""
    prov = root / market
    (prov / "calendars").mkdir(parents=True)
    (prov / "calendars" / "day.txt").write_text("2020-01-02\n2026-10-08\n")
    (prov / "instruments").mkdir(parents=True)
    (prov / "instruments" / "all.txt").write_text(
        "".join(f"{i}\t2020-01-02\t2026-10-08\n" for i in instruments)
    )
    feat = prov / "features" / instruments[0]
    feat.mkdir(parents=True)
    for col in ("close", "open", "amount", "factor"):
        (feat / f"{col}.day.bin").write_bytes(b"")
    return str(prov)


def test_profile_status_missing_provider_not_ready(tmp_path, monkeypatch):
    """provider 不存在 → ready=False（unavailable 终态的数据面依据）。"""
    monkeypatch.setattr(
        P, "resolve_qlib_provider_uri", lambda m: str(tmp_path / "nope")
    )
    status = P.profile_status(P.get_market_profile("us_stock"))
    assert status["ready"] is False
    assert status["calendar_start"] is None


def test_profile_status_ready_has_bounds_counts_and_columns(tmp_path, monkeypatch):
    """就绪 provider：日历边界、标的数、bin 列、分类列集一并给出。"""
    prov = _make_fake_provider(tmp_path, "us_data", instruments=("a", "b", "c"))
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    status = P.profile_status(P.get_market_profile("us_stock"))
    assert status["ready"] is True
    assert status["calendar_start"] == "2020-01-02"
    assert status["calendar_end"] == "2026-10-08"
    assert status["instruments"] == 3
    assert "close" in status["bin_columns"]
    assert status["columns"] == sorted(BASE_COLUMNS)
    assert status["in_sample"] is False


def test_profile_status_cn_reports_mining_contract_columns(tmp_path, monkeypatch):
    """CN 的分类列集是 39 列挖掘契约（bin 里只有 8 列，别拿 bin 当契约）。"""
    prov = _make_fake_provider(tmp_path, "cn_data")
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    status = P.profile_status(P.get_market_profile("a_share"))
    assert status["columns"] == sorted(CN_MINING_COLUMNS)
    assert "change" not in status["bin_columns"]  # 假布局里没放 change，仅验证取自 bin


def test_bin_columns_falls_back_to_lowercase_dir(tmp_path, monkeypatch):
    """非 CN provider 的 feature 目录是小写的，而 all.txt 里是原始大小写
    （实测：HK ``hk_0001.HK`` → 目录 ``hk_0001.hk``；US/BC/FUT 同型）。
    取样必须先原样再小写回落，否则 bin 列侦察对四个非 CN 市场全瞎。"""
    prov_path = Path(
        _make_fake_provider(tmp_path, "hk_data", instruments=("hk_0001.HK",))
    )
    (prov_path / "features" / "hk_0001.HK").rename(
        prov_path / "features" / "hk_0001.hk"
    )
    prov = str(prov_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    status = P.profile_status(P.get_market_profile("hong_kong"))
    assert status["bin_columns"] is not None
    assert "close" in status["bin_columns"]
