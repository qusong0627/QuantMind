"""QuantDB 投影特征服务单元测试（不依赖真实 parquet 数据）。"""

from __future__ import annotations

import asyncio
import math

import pytest

from backend.services.api.routers import research_features_service as svc


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._PROJ_DAY_CACHE.clear()  # noqa: SLF001
    yield
    svc._PROJ_DAY_CACHE.clear()  # noqa: SLF001


class _FakeHub:
    """最小 QuantDBHub 替身：按视图名返回预置行。"""

    def __init__(
        self,
        rows_by_view: dict[str, list[dict]],
        *,
        available: bool = True,
        calendar: list[str] | None = None,
    ):
        self._rows_by_view = rows_by_view
        self.available = available
        self.data_dir = None
        self.queries: list[str] = []
        self._calendar = calendar or []

    def query(self, sql: str):
        import pandas as pd

        self.queries.append(sql)
        for view, rows in self._rows_by_view.items():
            if f"FROM {view}\n" in sql or f"FROM {view} " in sql:
                return pd.DataFrame(rows)
        raise RuntimeError("view not found")

    def _partition_dates(self, rel_path, start=None, end=None):
        if "daily_forward" not in str(rel_path):
            return []
        start_s = start.strftime("%Y%m%d") if start else ""
        end_s = end.strftime("%Y%m%d") if end else "99999999"
        return [d for d in self._calendar if start_s <= d <= end_s]

    def fetch_latest_rows(self, view, symbols, *, dt=None, lookback=None, columns=None):
        """按视图名返回预置行的 DataFrame（新 QuantDBHub 直读接口）。

        视图不存在时优雅降级为空表（与真实 Hub 行为一致）。
        lookback=0 时按精确 dt 过滤，供远期收益现算读取 T / T+N 收盘价。
        """
        import pandas as pd

        self.queries.append(f"SELECT * FROM {view}")
        rows = self._rows_by_view.get(view, [])
        df = pd.DataFrame(rows)
        if "symbol" in df.columns:
            wanted = {str(s) for s in symbols}
            df = df[df["symbol"].isin(wanted)]
        if dt is not None and lookback == 0 and not df.empty and "dt" in df.columns:
            df = df[df["dt"].astype(str) == str(dt)]
        if columns:
            keep = [c for c in columns if c in df.columns]
            if "symbol" in df.columns:
                keep = ["symbol", *keep]
            df = df[list(dict.fromkeys(c for c in keep if c in df.columns))]
        return df.reset_index(drop=True)


def _install_hub(monkeypatch, hub):
    monkeypatch.setattr(svc, "_get_hub", lambda: hub)
    return hub


# --------------------------------------------------------------------------
# normalize_symbols
# --------------------------------------------------------------------------
def test_normalize_symbols_accepts_prefix_suffix_and_bare_digits():
    assert svc.normalize_symbols(["SH600036", "000001.SZ", "300750"]) == [
        "600036.SH",
        "000001.SZ",
        "300750.SZ",
    ]


def test_normalize_symbols_drops_invalid_and_deduplicates_preserving_order():
    assert svc.normalize_symbols(
        ["600036.SH", "AAPL", "", None, "SH600036", "000001.SZ"]
    ) == ["600036.SH", "000001.SZ"]


def test_normalize_symbols_rejects_sql_injection_payload():
    assert svc.normalize_symbols(["600036.SH'; DROP VIEW qdb_valuation; --"]) == []


# --------------------------------------------------------------------------
# _to_jsonable
# --------------------------------------------------------------------------
def test_to_jsonable_converts_non_finite_floats_to_none():
    assert svc._to_jsonable(float("nan")) is None  # noqa: SLF001
    assert svc._to_jsonable(float("inf")) is None  # noqa: SLF001
    assert svc._to_jsonable(1.5) == 1.5  # noqa: SLF001


def test_to_jsonable_unwraps_numpy_scalars():
    import numpy as np

    assert svc._to_jsonable(np.float64(2.5)) == 2.5  # noqa: SLF001
    assert svc._to_jsonable(np.int64(7)) == 7  # noqa: SLF001
    assert svc._to_jsonable(np.float64("nan")) is None  # noqa: SLF001


def test_to_jsonable_serializes_timestamps_to_iso_strings():
    import pandas as pd

    assert svc._to_jsonable(pd.Timestamp("2026-07-29")).startswith(  # noqa: SLF001
        "2026-07-29"
    )
    assert svc._to_jsonable(pd.NaT) is None  # noqa: SLF001


# --------------------------------------------------------------------------
# get_batch_full_features（投影模式）
# --------------------------------------------------------------------------
def test_batch_returns_empty_for_no_valid_symbols(monkeypatch):
    _install_hub(monkeypatch, _FakeHub({}))
    data = asyncio.run(svc.get_batch_full_features(["AAPL", ""]))["data"]
    assert data == {"items": [], "total": 0, "missing": []}


def test_batch_rejects_empty_fields(monkeypatch):
    _install_hub(monkeypatch, _FakeHub({}))
    result = asyncio.run(svc.get_batch_full_features(["600036.SH"]))
    assert result["code"] == 400
    assert result["data"] is None


def test_batch_returns_items_and_missing(monkeypatch):
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "pe_ttm": 6.6},
                    {"symbol": "000001.SZ", "dt": 1, "pe_ttm": 5.1},
                ]
            }
        ),
    )

    data = asyncio.run(
        svc.get_batch_full_features(
            ["SH600036", "000001.SZ", "300750.SZ"], fields=["pe"]
        )
    )["data"]

    assert [item["symbol"] for item in data["items"]] == ["600036.SH", "000001.SZ"]
    assert data["total"] == 2
    assert data["missing"] == ["300750.SZ"]
    assert data["truncated"] is False


def test_batch_truncates_oversized_requests(monkeypatch):
    symbols = [f"{600000 + i}.SH" for i in range(svc.MAX_BATCH_SYMBOLS_PROJECTED + 20)]
    _install_hub(
        monkeypatch,
        _FakeHub(
            {"qdb_valuation": [{"symbol": s, "dt": 1, "pe_ttm": 1.0} for s in symbols]}
        ),
    )

    data = asyncio.run(svc.get_batch_full_features(symbols, fields=["pe"]))["data"]
    assert data["truncated"] is True
    assert data["total"] == svc.MAX_BATCH_SYMBOLS_PROJECTED


def test_batch_issues_single_query_per_view(monkeypatch):
    """批量查询应一次覆盖所有 symbol，而非按 symbol 循环。"""
    hub = _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "pe_ttm": 6.6},
                    {"symbol": "000001.SZ", "dt": 1, "pe_ttm": 5.1},
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    asyncio.run(svc.get_batch_full_features(["600036.SH", "000001.SZ"], fields=["pe"]))

    valuation_queries = [q for q in hub.queries if "qdb_valuation" in q]
    assert len(valuation_queries) == 1


def test_batch_output_is_json_serializable(monkeypatch):
    import json

    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "pe_ttm": float("nan"), "pb": 1.2}
                ]
            }
        ),
    )

    result = asyncio.run(svc.get_batch_full_features(["600036.SH"], fields=["pe", "pb"]))
    encoded = json.dumps(result, allow_nan=False)
    assert "NaN" not in encoded
    assert math.isnan(float("nan"))  # sanity: NaN 确实存在于输入


# --------------------------------------------------------------------------
# camelCase 投影（fields 参数）
# --------------------------------------------------------------------------
def test_to_camel_matches_frontend_convention():
    assert svc._to_camel("mom_ret_1d") == "momRet1d"  # noqa: SLF001
    assert svc._to_camel("micro_vpin_8") == "microVpin8"  # noqa: SLF001
    assert svc._to_camel("ma5") == "ma5"  # noqa: SLF001


def test_camel_name_applies_cross_category_aliases():
    """前端把这些列归入别的分组，命名必须与 featureMapper.ts 一致。"""
    assert svc._camel_name("fun_mv_rank", "qdb_l1_factors") == "styleMvRank"  # noqa: SLF001
    assert svc._camel_name("micro_liquidity_amihud_20", "qdb_l2_factors") == "liqAmihud20"  # noqa: SLF001


def test_camel_name_prefixes_sentiment_columns_only_for_sentiment_view():
    """market_sentiment 的列无前缀，需加 sentiment 前缀避免与基础字段撞名。"""
    assert svc._camel_name("body_ratio", "qdb_market_sentiment") == "sentimentBodyRatio"  # noqa: SLF001
    assert svc._camel_name("body_ratio", "qdb_l1_factors") == "bodyRatio"  # noqa: SLF001


def test_projected_batch_returns_only_requested_fields_flattened(monkeypatch):
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "pb": 0.78, "ps_ttm": 2.2, "total_mv": 1e11}
                ],
                "qdb_technical_indicators": [
                    {"symbol": "600036.SH", "dt": 1, "rsi_14": 66.9, "macd_hist": 0.48}
                ],
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["pb", "rsi14"])
    )

    assert result["data"]["projected"] is True
    item = result["data"]["items"][0]
    # 平铺在 values 下，且严格只含请求字段
    assert item["values"] == {"pb": 0.78, "rsi14": 66.9}
    # 投影模式不返回分类结构
    assert "valuation" not in item


def test_projected_batch_drops_non_numeric_values(monkeypatch):
    """表格与筛选只消费数值，字符串/NaN 不应出现在 values 中。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {
                        "symbol": "600036.SH",
                        "dt": 1,
                        "pb": float("nan"),
                        "industry_name": "银行",
                        "ps_ttm": 2.2,
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    result = asyncio.run(
        svc.get_batch_full_features(
            ["600036.SH"], fields=["pb", "industryName", "psTtm"]
        )
    )
    assert result["data"]["items"][0]["values"] == {"psTtm": 2.2}


def test_projected_batch_scales_flow_fields_to_millions(monkeypatch):
    """资金流字段需换算为百万元，与 /research/universe 的单位一致。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_l2_factors": [
                    {"symbol": "600036.SH", "dt": 1, "flow_net_amount": 25_000_000.0}
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["flowNetAmount"])
    )
    assert result["data"]["items"][0]["values"]["flowNetAmount"] == pytest.approx(25.0)


def test_projected_batch_does_not_scale_market_cap(monkeypatch):
    """fun_mv / liq_amount 是对数值，任何线性缩放都是错的——必须原样返回。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_l1_factors": [
                    {"symbol": "600036.SH", "dt": 1, "fun_mv": 26.07, "liq_amount": 22.24}
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["funMv", "liqAmount"])
    )
    values = result["data"]["items"][0]["values"]
    assert values["funMv"] == pytest.approx(26.07)
    assert values["liqAmount"] == pytest.approx(22.24)


def test_projected_batch_does_not_fill_return_from_momentum(monkeypatch):
    """return_* 全为 NaN 时，不再用 mom_ret_*d ×100 兜底。

    投研平台的 return 系列语义是“推理日后 N 日真实收益”，必须来自 features_daily
    的 return_* 标签；mom_ret_*d 是过去动量，混用会把历史收益冒充未来收益。
    未来交易日未走完时 return_* 为 NaN，应返回空由前端显示“-”。
    """
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_technical_indicators": [
                    {"symbol": "600036.SH", "dt": 1, "return_5d": float("nan")}
                ],
                "qdb_l1_factors": [
                    {"symbol": "600036.SH", "dt": 1, "mom_ret_5d": 0.01742}
                ],
            }
        ),
    )

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["return5d"])
    )
    assert "return5d" not in result["data"]["items"][0]["values"]


def test_projected_batch_prefers_real_return_over_fallback(monkeypatch):
    """真实 return_5d 有值时不应被兜底覆盖。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_technical_indicators": [
                    {"symbol": "600036.SH", "dt": 1, "return_5d": 3.5}
                ],
                "qdb_l1_factors": [
                    {"symbol": "600036.SH", "dt": 1, "mom_ret_5d": 0.01742}
                ],
            }
        ),
    )

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["return5d"])
    )
    assert result["data"]["items"][0]["values"]["return5d"] == pytest.approx(3.5)


def test_projected_batch_omits_unavailable_long_horizon_returns(monkeypatch):
    """10/20/60 日收益不上游动量兜底——宁缺勿滥。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_l1_factors": [
                    {"symbol": "600036.SH", "dt": 1, "mom_ret_20d": 2.75}
                ]
            }
        ),
    )

    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["return20d"])
    )
    assert "return20d" not in result["data"]["items"][0]["values"]


def test_projected_batch_fills_missing_return10d_from_kline(monkeypatch):
    """宽表 return_10d 为 NaN 时，用 daily_forward 按交易日 LEAD 现算未来 10 日收益。"""
    calendar = [
        "20260901",
        "20260902",
        "20260903",
        "20260904",
        "20260907",
        "20260908",
        "20260909",
        "20260910",
        "20260911",
        "20260914",
        "20260915",
    ]
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_features_daily": [
                    {
                        "symbol": "301589.SZ",
                        "dt": 20260901,
                        "return_1d": -1.1708,
                        "return_10d": float("nan"),
                    }
                ],
                "qdb_daily_forward": [
                    {"symbol": "301589.SZ", "dt": 20260901, "close": 145.2},
                    {"symbol": "301589.SZ", "dt": 20260915, "close": 147.33},
                ],
            },
            calendar=calendar,
        ),
    )

    result = asyncio.run(
        svc.get_batch_full_features(
            ["301589.SZ"],
            fields=["return1d", "return10d", "return20d"],
            trade_date="2026-09-01",
        )
    )
    values = result["data"]["items"][0]["values"]
    assert values["return1d"] == pytest.approx(-1.1708)
    assert values["return10d"] == pytest.approx((147.33 / 145.2 - 1.0) * 100.0)
    assert "return20d" not in values


def test_projected_batch_cap_covers_full_pool():
    """投影响应体小，上限需覆盖整个候选池（全池筛选的前提）。"""
    assert svc.MAX_BATCH_SYMBOLS_PROJECTED > 1015


# --------------------------------------------------------------------------
# PG 缺列的接管：PE / ROE / RSI / ATR / 换手率
# --------------------------------------------------------------------------
def test_pe_and_roe_aliases_cover_pg_gap(monkeypatch):
    """PG 从 2026-06-26 起不再回填 PE/ROE，QuantDB 必须能顶上同名 UI 字段。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [{"symbol": "600036.SH", "dt": 1, "pe_ttm": 6.64}],
                "qdb_l1_factors": [{"symbol": "600036.SH", "dt": 1, "fun_roe": 11.76}],
            }
        ),
    )
    result = asyncio.run(svc.get_batch_full_features(["600036.SH"], fields=["pe", "roe"]))
    values = result["data"]["items"][0]["values"]
    assert values["pe"] == pytest.approx(6.64)
    # fun_roe 已是百分数，不得再乘 100
    assert values["roe"] == pytest.approx(11.76)


def test_rsi_and_atr_fall_back_to_quantdb_column_names(monkeypatch):
    """UI 的 rsi / atr 对应 QuantDB 的 rsi_6 / vol_atr_14。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_technical_indicators": [
                    {"symbol": "600036.SH", "dt": 1, "rsi_6": 74.64, "vol_atr_14": 0.74}
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})
    result = asyncio.run(svc.get_batch_full_features(["600036.SH"], fields=["rsi", "atr"]))
    values = result["data"]["items"][0]["values"]
    assert values["rsi"] == pytest.approx(74.64)
    assert values["atr"] == pytest.approx(0.74)


def test_market_cap_converted_to_yi(monkeypatch):
    """qdb_valuation.total_mv 是元，UI 期望亿元。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "total_mv": 1.0002e12, "float_mv": 8.1814e11}
                ]
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})
    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["totalMv", "floatMv"])
    )
    values = result["data"]["items"][0]["values"]
    assert values["totalMv"] == pytest.approx(10002.0, rel=1e-3)
    assert values["floatMv"] == pytest.approx(8181.4, rel=1e-3)


def test_turnover_rate_computed_from_volume_and_float_shares(monkeypatch):
    """换手率 = 成交量(股) / 流通股本(股) ×100%。qdb_daily_unadjusted.volume 单位是股。"""
    hub = _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "circulating_capital": 2.062894e10}
                ],
                "qdb_daily_unadjusted": [
                    {"symbol": "600036.SH", "dt": 1, "volume": 105018700.0}
                ],
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})
    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["turnoverRate"])
    )
    # 105018700 股 / 206.29 亿股 × 100 ≈ 0.509%
    assert result["data"]["items"][0]["values"]["turnoverRate"] == pytest.approx(0.509, abs=0.01)
    # 日线视图只在需要换手率时挂载
    assert any("qdb_daily_unadjusted" in q for q in hub.queries)


def test_daily_view_not_queried_when_turnover_not_requested(monkeypatch):
    hub = _install_hub(
        monkeypatch,
        _FakeHub({"qdb_valuation": [{"symbol": "600036.SH", "dt": 1, "pb": 0.78}]}),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})
    asyncio.run(svc.get_batch_full_features(["600036.SH"], fields=["pb"]))
    assert not any("qdb_daily_unadjusted" in q for q in hub.queries)


def test_daily_view_contributes_only_volume(monkeypatch):
    """日线视图的 amount/open/high/low 与 UI 字段同名但量纲不同，必须被丢弃。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_valuation": [
                    {"symbol": "600036.SH", "dt": 1, "circulating_capital": 2.0e10}
                ],
                "qdb_daily_unadjusted": [
                    {
                        "symbol": "600036.SH",
                        "dt": 1,
                        "volume": 1_000_000.0,
                        "amount": 4.136e9,  # 元；UI 的 amount 是亿元
                        "open": 39.17,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})
    result = asyncio.run(
        svc.get_batch_full_features(["600036.SH"], fields=["turnoverRate", "amount", "open"])
    )
    values = result["data"]["items"][0]["values"]
    assert "turnoverRate" in values
    # 未被日线的原始 amount 污染
    assert "amount" not in values
    assert "open" not in values


def test_batch_projected_features_from_features_daily(monkeypatch):
    """测试批量投影模式直接从 qdb_features_daily 提取字段并自动别名与缩放。"""
    _install_hub(
        monkeypatch,
        _FakeHub(
            {
                "qdb_features_daily": [
                    {
                        "symbol": "600036.SH",
                        "time": "2026-07-29",
                        "dt": 20260729,
                        "pe_ttm": 6.6,
                        "pb": 0.85,
                        "total_mv": 9.8e11,
                        "ma5": 39.08,
                        "ma_gap_5": 0.5,
                        "vol_atr_14": 0.88,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(svc, "_latest_l1_from_files", lambda symbols: {})

    result = asyncio.run(
        svc.get_batch_full_features(
            ["600036.SH"],
            fields=["pe", "pb", "totalMv", "ma5", "maGap5", "atr"],
        )
    )

    items = result["data"]["items"]
    assert len(items) == 1
    values = items[0]["values"]
    assert values["pe"] == 6.6
    assert values["pb"] == 0.85
    assert values["totalMv"] == pytest.approx(9800.0)
    assert values["ma5"] == 39.08
    assert values["maGap5"] == 0.5
    assert values["atr"] == 0.88
