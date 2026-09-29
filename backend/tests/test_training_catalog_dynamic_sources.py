"""训练目录必须认「动态因子源」（rd_mined 这类不在静态清单里的数据集）。

链路背景（2026-09-29）：RD-Agent 挖掘因子 → 物化进 CUSTOM `rd_mined` →
`--register` 写源状态表 → 训练页直读。最后一环的回归风险：源列表迭代与
归属校验若改回静态清单 `sources_for_market`，`rd_mined` 会从训练页**静默消失**
（它按设计不在静态清单里），页面不报错，只是链断了——挖掘跑多久都白费。

这里用假会话（不碰真库）锁两条契约：
1. 源列表按源状态表迭代：状态表里有、静态清单里没有的源必须出现，
   并带上发布/可训练状态与启用特征数；
2. 目录归属校验同样按状态表判：动态源返回空目录态而不是 422；
   状态表里没有的源仍然 422（防「把校验整个拿掉」式的假修复）。
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi import HTTPException

from backend.services.api.routers.admin import quantdb_factor_catalog as qfc
from backend.services.engine.data_platform.quantdb_factor_reader import (
    sources_for_market,
)

_DYNAMIC = "rd_mined"


class _Rows:
    """最小结果集替身：与 SQLAlchemy Result 的 mappings()/all()/first() 同形。"""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只认两条查询；DDL（_ensure_schema）等一律返回空结果。"""

    def __init__(self, published, counts):
        self._published = published
        self._counts = counts

    async def execute(self, statement, params=None) -> _Rows:
        sql = str(statement)
        if "qm_training_factor_catalog_version" in sql:
            return _Rows(self._published)
        if "qm_training_factor_mapping" in sql:
            return _Rows(self._counts)
        return _Rows([])


def _status(source: str, *, ready: bool = True, reason: str | None = None) -> dict:
    return {
        "dataset_id": source,
        "path": f"/data/quantcustom/6_ml_datasets/{source}",
        "files": 1635,
        "column_count": 10,
        "columns": [],
        "column_types": {},
        "schema_hash": f"hash-{source}",
        "min_date": "2018-01-02",
        "max_date": "2026-09-24",
        "ready": ready,
        "missing_required": [],
        "reason": reason,
        "refreshed_at": None,
    }


@pytest.fixture()
def patch_env(monkeypatch):
    """把 get_session 与源状态读取换成内存替身，返回布置函数。"""

    def _apply(statuses, published=(), counts=()):
        session = _FakeSession(list(published), list(counts))

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield session

        async def _fake_statuses(_session, _market):
            return statuses

        monkeypatch.setattr(qfc, "get_session", _fake_session)
        monkeypatch.setattr(qfc, "_cached_factor_sources", _fake_statuses)

    return _apply


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dynamic_source_listed_with_publish_state(patch_env):
    """状态表里的动态源必须出现在源列表，且发布/可训练/特征数如实带出。"""
    # 前提校验：rd_mined 确实不在 CUSTOM 的静态清单里——否则本用例在
    # 「改回静态迭代」的实现下也会通过，守不住任何东西（防假通过）。
    assert _DYNAMIC not in sources_for_market("CUSTOM")

    patch_env(
        {
            "l1_factors": _status("l1_factors"),
            _DYNAMIC: _status(_DYNAMIC),
        },
        published=[
            {"version_id": "v-rd", "source_dataset": _DYNAMIC, "published_at": None}
        ],
        counts=[{"source_dataset": _DYNAMIC, "n": 7}],
    )

    data = await qfc.load_quantdb_training_sources(market="CUSTOM")

    assert [s["id"] for s in data["sources"]] == ["l1_factors", _DYNAMIC]
    rd = next(s for s in data["sources"] if s["id"] == _DYNAMIC)
    assert rd["name"] == "RD 挖掘因子"
    assert rd["default"] is False
    assert rd["published"] is True
    assert rd["trainable"] is True
    assert rd["feature_count"] == 7
    assert rd["catalog_version"] == "v-rd"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unpublished_source_is_not_trainable(patch_env):
    """对照：没有发布版本的源列表项 `published/trainable` 为假、给出原因。

    防止上一条的 `trainable is True` 断言在「恒真」的实现下也通过。
    """
    patch_env({"l1_factors": _status("l1_factors")})

    data = await qfc.load_quantdb_training_sources(market="CUSTOM")

    only = data["sources"][0]
    assert only["published"] is False
    assert only["trainable"] is False
    assert only["reason"] == "尚未发布因子目录"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_catalog_accepts_dynamic_source_without_version(patch_env):
    """动态源没发布版本时是「空目录态」，绝不是 422。"""
    patch_env({_DYNAMIC: _status(_DYNAMIC)})

    catalog = await qfc.load_quantdb_training_catalog(_DYNAMIC, market="CUSTOM")

    assert catalog["catalog_status"] == "unpublished"
    assert catalog["source_dataset"] == _DYNAMIC
    assert catalog["feature_count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_catalog_rejects_source_absent_from_statuses(patch_env):
    """状态表里没有的源仍然 422——归属校验是收窄，不是废除。"""
    patch_env({"l1_factors": _status("l1_factors")})

    with pytest.raises(HTTPException) as excinfo:
        await qfc.load_quantdb_training_catalog("l2_factors", market="CUSTOM")

    assert excinfo.value.status_code == 422
