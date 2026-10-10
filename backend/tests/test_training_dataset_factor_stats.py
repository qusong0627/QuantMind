"""训练目录页 per-feature 统计聚合（training_stats）契约测试。

链路背景（2026-10-10）：后台「模型训练数据集」页要在每个因子旁展示因子报告的
质量指标（IC/ICIR/换手/单调性/胜率/样本量/有效天数）。数据来自各数据集目录下的
``report/factor_report.json`` 快照；该文件最大 14MB（factor_defs），所以读侧必须
「精简索引 + mtime/TTL 缓存」，且聚合入口承诺**绝不抛**——它挂在 /fields 响应里，
统计层出问题不能把字段列表本身拖垮。

硬口径（用户纪律）：缺失一律 None（前端显示「—」），**绝不伪造 0**；口径混用必须
带来源标记（报告=日频 2359 日 vs 私域快照=82 采样日，IC 不可比）。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from datetime import datetime, timedelta

import pytest

from backend.services.engine.factor_report import training_stats
from backend.services.engine.factor_report.datasets import DATASETS

_VALID_SCALARS = {
    "ic_mean": -0.08345,
    "icir": -0.4879,
    "t_value": -23.68,
    "turnover": 0.5576,
    "monotonicity": -0.699,
    "win_rate": 0.3031,
    "n_valid_mean": 4204.90137,
    "ic_neutral_days": 2355,
}


@pytest.fixture()
def qdb_root(tmp_path, monkeypatch):
    """隔离的 QuantDB 根：env 指向 tmp（mkdir 后目录非空 → _is_usable 命中），
    绝不落回真实 /data 或仓库根data 目录。"""
    root = tmp_path / "quantdb"
    (root / "6_ml_datasets").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    training_stats.clear_cache()
    yield root
    training_stats.clear_cache()


def _write_report(
    root, dataset: str, factors: list[dict], *, generated_at: str | None = None
):
    """按真快照结构（top: meta+factors；meta 含 start/end/n_dates/horizon）落盘。"""
    d = root / "6_ml_datasets" / dataset / "report"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "dataset": dataset,
        "horizon": "fwd_ret_5",
        "n_dates": 2359,
        "start": "20170103",
        "end": "20260917",
        "generated_at": generated_at
        or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = {"meta": meta, "factors": factors, "correlation": {}}
    (d / "factor_report.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return d / "factor_report.json"


def _write_private(root, factors: list[dict]):
    """私域快照：factors.json（研究口径，n_dates=82 采样日）。"""
    d = root / "factor_research_private"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "factors": factors,
        "meta": {
            "dataset": "factor_research_private",
            "window": ["2020-01-23", "2026-10-09"],
            "n_dates": 82,
            "n_factors": len(factors),
            "built_at": "2026-10-10T02:36:39",
        },
    }
    (d / "factors.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.mark.unit
def test_report_registry_membership_is_exact():
    """report_dataset_for 只认报告注册表键，绝不兜底到默认数据集。

    兜底方向是错的：ccass_factors 这类没有报告的源若被兜到 alpha_library，
    页面会把别的库的指标安在这个库的因子上——静默错数。
    """
    for key in DATASETS:
        assert training_stats.report_dataset_for(key) == key
    assert training_stats.report_dataset_for("ccass_factors") is None
    assert training_stats.report_dataset_for("l1_factors ") is None


@pytest.mark.unit
def test_report_snapshot_hit_returns_scalars_and_window(qdb_root):
    _write_report(
        qdb_root,
        "l1_factors",
        [
            {
                "name": "turn_1",
                "library": "L1",
                "display_name": "1日换手率",
                **_VALID_SCALARS,
            },
        ],
    )

    result = training_stats.build_fields_stats(
        "CN", "l1_factors", ["turn_1", "no_such_col"]
    )

    stat = result["stats"]["turn_1"]
    assert stat["source"] == "report"
    assert stat["ic_mean"] == pytest.approx(-0.08345)
    assert stat["icir"] == pytest.approx(-0.4879)
    assert stat["ic_neutral_days"] == 2355
    assert stat["library"] == "L1"
    # 未命中的列是 None（前端渲染「—」），不是 0
    assert result["stats"]["no_such_col"] is None
    meta = result["meta"]
    assert meta["available"] is True
    assert meta["dataset"] == "l1_factors"
    assert meta["matched"] == 1 and meta["total"] == 2
    assert meta["window"]["n_dates"] == 2359
    assert meta["window"]["start"] == "2017-01-03"
    assert meta["window"]["end"] == "2026-09-17"
    assert meta["window"]["horizon"] == "fwd_ret_5"
    assert meta["stale"] is False
    assert meta["fallback_used"] == 0


@pytest.mark.unit
def test_missing_snapshot_no_fake_zero(qdb_root):
    """快照不存在：命中列为 None、available False 且给出原因——绝不落回 0。"""
    (qdb_root / "6_ml_datasets" / "l1_factors").mkdir(parents=True)

    result = training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])

    assert result["stats"]["turn_1"] is None
    assert result["meta"]["available"] is False
    assert result["meta"]["reason"]
    assert result["meta"]["matched"] == 0


@pytest.mark.unit
def test_corrupt_snapshot_fails_soft(qdb_root):
    d = qdb_root / "6_ml_datasets" / "l1_factors" / "report"
    d.mkdir(parents=True)
    (d / "factor_report.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    result = training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])

    assert result["stats"]["turn_1"] is None
    assert result["meta"]["available"] is False


@pytest.mark.unit
def test_nan_inf_bool_string_sanitized(qdb_root):
    """坏值（NaN/Inf/bool/字符串）一律 None；同条目的好值保留。

    同时锁一个序列化事故面：Python 的 json.dump 会把 NaN 原样写出，
    Starlette JSONResponse（allow_nan=False）序列化时直接 500——守卫必须
    在这里把非有限值拦成 None，而不是等响应层炸。
    """
    d = qdb_root / "6_ml_datasets" / "l1_factors" / "report"
    d.mkdir(parents=True)
    raw = (
        '{"meta": {"n_dates": 100, "start": "20200101", "end": "20260101",'
        ' "horizon": "fwd_ret_5"}, "factors": ['
        '{"name": "bad_col", "ic_mean": NaN, "icir": Infinity, "t_value": true,'
        ' "turnover": "0.5", "monotonicity": null, "win_rate": -Infinity,'
        ' "n_valid_mean": 1e400, "ic_neutral_days": 12.5},'
        '{"name": "good_col", "ic_mean": 0.02, "icir": 0.3, "t_value": 2.1,'
        ' "turnover": 0.5, "monotonicity": 0.8, "win_rate": 0.55,'
        ' "n_valid_mean": 4000, "ic_neutral_days": 900}'
        "]}"
    )
    (d / "factor_report.json").write_text(raw, encoding="utf-8")

    result = training_stats.build_fields_stats(
        "CN", "l1_factors", ["bad_col", "good_col"]
    )

    bad = result["stats"]["bad_col"]
    assert bad is not None  # 条目在索引里
    for key in (
        "ic_mean",
        "icir",
        "t_value",
        "turnover",
        "monotonicity",
        "win_rate",
        "n_valid_mean",
    ):
        assert bad[key] is None, f"{key} 未消毒"
    assert bad["ic_neutral_days"] is None  # 非整数天数不收
    good = result["stats"]["good_col"]
    assert good["ic_mean"] == pytest.approx(0.02)
    assert good["ic_neutral_days"] == 900
    # 全量可 JSON 序列化（allow_nan=False 的响应层前提）
    json.dumps(result, allow_nan=False)


@pytest.mark.unit
def test_stale_flag_for_old_snapshot(qdb_root):
    _write_report(
        qdb_root,
        "l1_factors",
        [
            {"name": "turn_1", **_VALID_SCALARS},
        ],
        generated_at="2020-01-01 00:00:00",
    )

    result = training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])

    assert result["meta"]["stale"] is True
    assert result["meta"]["report_date"] == "2020-01-01"


@pytest.mark.unit
def test_non_cn_market_gated_even_with_snapshot_on_disk(qdb_root):
    """非 CN 市场一律收口：即便同名快照在盘上也不许读（报告快照只有 A 股根）。"""
    _write_report(qdb_root, "l1_factors", [{"name": "turn_1", **_VALID_SCALARS}])

    result = training_stats.build_fields_stats("HK", "l1_factors", ["turn_1"])

    assert result["stats"]["turn_1"] is None
    assert result["meta"]["available"] is False
    assert "A" in result["meta"]["reason"] or "股" in result["meta"]["reason"]


@pytest.mark.unit
def test_research_fallback_flagged_and_not_mixed(qdb_root):
    """私域快照兜底必须带 source 徽标与 82 采样日口径；报告命中优先于兜底。"""
    _write_report(qdb_root, "l1_factors", [{"name": "turn_1", **_VALID_SCALARS}])
    _write_private(
        qdb_root,
        [
            {
                "code": "turn_1",
                "ic_mean": 0.99,
                "ic_ir": 9.9,
            },  # 报告里有 → 不许用兜底值
            {"code": "only_private", "ic_mean": 0.03, "ic_ir": 0.309},
        ],
    )

    result = training_stats.build_fields_stats(
        "CN", "l1_factors", ["turn_1", "only_private"]
    )

    assert result["stats"]["turn_1"]["source"] == "report"
    assert result["stats"]["turn_1"]["ic_mean"] == pytest.approx(-0.08345)
    fb = result["stats"]["only_private"]
    assert fb["source"] == "research_snapshot"
    assert fb["ic_mean"] == pytest.approx(0.03)
    assert fb["icir"] == pytest.approx(0.309)
    # 兜底没有的字段是 None，不许拿报告口径的 0 填充
    assert fb["turnover"] is None and fb["n_valid_mean"] is None
    assert result["meta"]["fallback_used"] == 1
    assert result["meta"]["fallback_window"]["n_dates"] == 82


@pytest.mark.unit
def test_mtime_change_invalidates_without_ttl_expiry(qdb_root):
    path = _write_report(qdb_root, "l1_factors", [{"name": "turn_1", **_VALID_SCALARS}])
    assert training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])["stats"][
        "turn_1"
    ]["ic_mean"] == pytest.approx(-0.08345)

    _write_report(
        qdb_root,
        "l1_factors",
        [{"name": "turn_1", **{**_VALID_SCALARS, "ic_mean": 0.123}}],
    )
    future = os.stat(path).st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(future, future))

    result = training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])
    assert result["stats"]["turn_1"]["ic_mean"] == pytest.approx(0.123)


@pytest.mark.unit
def test_build_never_raises_on_internal_error(qdb_root, monkeypatch):
    """聚合入口承诺绝不抛（挂在 /fields 上，炸了会把字段列表一起拖垮）。"""

    def _boom(_dataset):
        raise RuntimeError("boom")

    monkeypatch.setattr(training_stats, "load_report_index", _boom)

    result = training_stats.build_fields_stats("CN", "l1_factors", ["turn_1"])

    assert result["stats"]["turn_1"] is None
    assert result["meta"]["available"] is False


# ---------------------------------------------------------------------------
# 路由级：/fields 响应内嵌 stats / stats_meta（假会话，不碰真库）
# ---------------------------------------------------------------------------

from backend.services.api.routers.admin import quantdb_factor_catalog as qfc  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只认 qm_quantdb_factor_field 查询；DDL（_ensure_schema）等回空。"""

    def __init__(self, field_rows):
        self._field_rows = field_rows

    async def execute(self, statement, params=None) -> _Rows:
        if "qm_quantdb_factor_field" in str(statement):
            return _Rows(self._field_rows)
        return _Rows([])


def _field_row(column: str) -> dict:
    return {
        "column_name": column,
        "data_type": "float64",
        "schema_hash": "h",
        "min_date": "2018-01-02",
        "max_date": "2026-10-09",
        "is_present": True,
        "discovered_at": None,
    }


@pytest.fixture()
def patch_qfc_session(monkeypatch):
    def _apply(rows):
        session = _FakeSession(list(rows))

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield session

        monkeypatch.setattr(qfc, "get_session", _fake_session)

    return _apply


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fields_endpoint_embeds_stats(qdb_root, patch_qfc_session):
    _write_report(qdb_root, "l1_factors", [{"name": "turn_1", **_VALID_SCALARS}])
    patch_qfc_session([_field_row("turn_1"), _field_row("no_such_col")])

    data = await qfc.list_factor_fields(
        market="CN", source_dataset="l1_factors", include_keys=False, current_user={}
    )

    assert data["stats"]["turn_1"]["ic_mean"] == pytest.approx(-0.08345)
    assert data["stats"]["no_such_col"] is None
    assert data["stats_meta"]["available"] is True
    assert data["stats_meta"]["matched"] == 1
    assert data["stats_meta"]["total"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fields_endpoint_survives_stats_explosion(
    qdb_root, patch_qfc_session, monkeypatch
):
    """统计层炸掉时 /fields 必须照常出字段（stats 空 + reason），绝不 500。"""
    patch_qfc_session([_field_row("turn_1")])

    def _boom(_market, _source, _columns):
        raise RuntimeError("boom")

    monkeypatch.setattr(qfc, "build_fields_stats", _boom)

    data = await qfc.list_factor_fields(
        market="CN", source_dataset="l1_factors", include_keys=False, current_user={}
    )

    assert [f["column_name"] for f in data["fields"]] == ["turn_1"]
    assert data["stats"] == {}
    assert data["stats_meta"]["reason"]
