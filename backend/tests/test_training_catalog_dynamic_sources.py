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
    EXCLUDED_FROM_DISCOVERY,
    EXCLUDED_FROM_TRAINING,
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


# CN 私人因子库（数据集 private）各来源库的因子数，2026-10-07 实测：
#   store.factors_meta('private')['factors'] 的 l2 计数
# 写死而不是运行时读快照：每个来源库都必须被逐一断言，漏一个就是一条静默死路，
# 而快照文件在哪台机器上缺席都不该让这条不变量悄悄变成「跳过」。
CN_PRIVATE_LIBRARIES: dict[str, int] = {
    "factor_defs": 1336,
    "alpha_library": 429,
    "alpha360": 360,
    "l2_factors": 211,
    "l1_factors": 110,
    "jq110": 109,
    "tdxgs": 88,
    "gap_mined": 69,
    "features_daily": 42,
}


@pytest.mark.unit
def test_every_registerable_library_is_reachable_from_the_training_page():
    """**可注册的来源库必须看得到**——写进去的草稿不能落进死胡同。

    这是一条跨模块的不变量，两侧各自看都没问题，只有合起来才出错：

    - 写侧 ``research_factor_registration.resolve_registrable`` 只拦
      ``EXCLUDED_FROM_TRAINING``（标签/泄漏库）。它**不知道**训练页能显示什么。
    - 读侧认哪些源，由 ``MARKET_FACTOR_SOURCES``（静态清单）加上
      「状态表里有、且不在 ``EXCLUDED_FROM_DISCOVERY``」的动态源共同决定。

    一个库只要**两样都占不上**——既不在静态清单里，又被挡在自动发现之外——
    就成了一条只进不出的管道：注册接口照收，草稿写进 ``qm_training_factor_mapping``，
    而 ``load_quantdb_training_catalog`` 对它 422「不属于市场 CN」，
    源列表里也没有它。用户看到的是「注册了但一直没有」。

    ``factor_defs`` 正是这样：1336/2754（49%）的私人因子库因子都在里面，
    2026-10-07 实测撞上（``feat_dstd_va_diff`` / ``feat_ridge_wpx`` 两个因子
    写进了 ``qdb-cn-factor_defs-c3ed2082cb35`` 却无处可看）。
    它的同类 ``alpha_library`` 两个集合都占了，所以一直正常——
    这条测试就是要求两者保持一致。
    """
    static = set(sources_for_market("CN"))

    unreachable = {
        lib
        for lib in CN_PRIVATE_LIBRARIES
        if lib not in EXCLUDED_FROM_TRAINING  # 可注册（写侧放行）
        and lib not in static  # 静态清单里没有
        and lib in EXCLUDED_FROM_DISCOVERY  # 自动发现也被挡住
    }

    assert unreachable == set(), (
        "这些来源库可注册却无法在训练页显示/读取，注册进去的草稿会静默失效："
        f"{sorted(unreachable)}"
    )


@pytest.mark.unit
def test_leaky_libraries_stay_unregisterable():
    """对照：安全边界不能被上一条的修复方向带松。

    上一条要求「可注册 ⇒ 可见」，最省事的假修复是**把库挪出
    ``EXCLUDED_FROM_TRAINING``**（那会让标签库变成训练特征源）或
    把 ``EXCLUDED_FROM_DISCOVERY`` 整个清空。两条都不许：
    ``features_daily`` 含未来收益标签列，任何路径都不得当特征源。
    """
    assert "features_daily" in EXCLUDED_FROM_TRAINING
    assert "alpha_library_labels" in EXCLUDED_FROM_TRAINING
