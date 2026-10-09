"""P2-0 信号桶隔离测试（《滚动训练与模型生命周期》§5.3 / §8-P2）。

三层证据：

1. 写读桶名契约金样（含 2026-10-08 生产库实测的两个桶名——写侧折叠/截断规则
   改一位都会让读侧对不上存量行）；
2. 读路径三模式矩阵（``off``=旧并集复现污染 / ``enforce``=只读生效桶 /
   ``shadow``=旧口径出数 + 差异留痕），真库 fixture 租户带两个桶；
3. 租户级持有者解析（单持有者 → 桶；双持有者=歧义 → 不猜，真库）。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest

from backend.shared.signal_buckets import (
    DEFAULT_SCOPING_MODE,
    SCOPING_ENV,
    SCOPING_REDIS_KEY,
    get_scoping_mode,
    normalize_model_bucket,
    record_shadow_evidence,
    reset_scoping_mode_cache,
    resolve_effective_bucket,
    resolve_feature_version,
    shadow_diff,
)

TAG_PREFIX = "t-bscope-"
D1 = date(2099, 1, 5)
TS_PROD = datetime(2099, 1, 5, 9, 0, tzinfo=timezone.utc)
TS_CAND = datetime(2099, 1, 5, 10, 0, tzinfo=timezone.utc)


class _FakeRedis:
    """同步 KV 替身：支持 get/set(key, value, ex)。"""

    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})
        self.writes: dict[str, tuple] = {}

    def get(self, key, use_slave=False):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.writes[key] = (value, ex)
        self.values[key] = value
        return True


class _FakeRegistry:
    """`resolve_effective_model_sync` 替身（记录调用参数）。"""

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def resolve_effective_model_sync(self, *, tenant_id, user_id, market=None, **kw):
        self.calls.append(
            {"tenant_id": tenant_id, "user_id": user_id, "market": market}
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _clean_mode_cache():
    reset_scoping_mode_cache()
    yield
    reset_scoping_mode_cache()


# ---------------------------------------------------------------------------
# 桶名契约（写侧金样）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizeModelBucket:
    def test_empty_or_none_is_inference_script(self):
        assert normalize_model_bucket(None) == "inference_script"
        assert normalize_model_bucket("") == "inference_script"
        assert normalize_model_bucket("   ") == "inference_script"
        # strip("_") 后为空同样回退（历史行为，不是新分支）
        assert normalize_model_bucket("___") == "inference_script"

    def test_folding_and_lowering(self):
        assert normalize_model_bucket("MDL-X Y!Z") == "mdl_x_y_z"

    def test_truncates_at_48_chars(self):
        assert normalize_model_bucket("x" * 60) == "x" * 48

    def test_production_golden_ids(self):
        # 2026-10-08 生产库实测桶名（写侧契约金样，改一位=存量行读不到）
        assert (
            resolve_feature_version("mdl_cust_train_20260917064612_33659b62_754461be")
            == "script_v1_mdl_cust_train_20260917064612_33659b62_754461be"
        )
        assert (
            resolve_feature_version("mdl_cust_train_20260916000342_b701110b_7ed2e231")
            == "script_v1_mdl_cust_train_20260916000342_b701110b_7ed2e231"
        )


# ---------------------------------------------------------------------------
# 模式解析（env 默认 + Redis 覆盖 + 30s 缓存）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScopingMode:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv(SCOPING_ENV, raising=False)
        assert get_scoping_mode(redis_client=_FakeRedis(), use_cache=False) == "off"
        assert DEFAULT_SCOPING_MODE == "off"

    def test_env_value_used_when_redis_empty(self, monkeypatch):
        monkeypatch.setenv(SCOPING_ENV, "shadow")
        assert get_scoping_mode(redis_client=_FakeRedis(), use_cache=False) == "shadow"

    def test_redis_overrides_env(self, monkeypatch):
        monkeypatch.setenv(SCOPING_ENV, "off")
        client = _FakeRedis({SCOPING_REDIS_KEY: b"enforce"})
        assert get_scoping_mode(redis_client=client, use_cache=False) == "enforce"

    def test_redis_str_value_supported(self, monkeypatch):
        monkeypatch.delenv(SCOPING_ENV, raising=False)
        client = _FakeRedis({SCOPING_REDIS_KEY: "shadow"})
        assert get_scoping_mode(redis_client=client, use_cache=False) == "shadow"

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(SCOPING_ENV, "banana")
        assert get_scoping_mode(redis_client=_FakeRedis(), use_cache=False) == "off"

    def test_cache_holds_for_30s_then_refreshes(self, monkeypatch):
        monkeypatch.delenv(SCOPING_ENV, raising=False)
        client = _FakeRedis({SCOPING_REDIS_KEY: b"enforce"})
        assert get_scoping_mode(redis_client=client, now=100.0) == "enforce"
        client.values[SCOPING_REDIS_KEY] = b"off"
        assert get_scoping_mode(redis_client=client, now=110.0) == "enforce"
        assert get_scoping_mode(redis_client=client, now=131.0) == "off"


# ---------------------------------------------------------------------------
# 影子比对（纯函数）+ 留痕
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShadowDiff:
    def test_identical_maps_and_dates_are_equal(self):
        diff = shadow_diff(
            {"A": 1.0, "B": None},
            {"A": 1.0, "B": None},
            old_date="2026-10-08",
            new_date="2026-10-08",
        )
        assert diff["equal"] is True
        assert diff["only_old"] == 0 and diff["only_new"] == 0

    def test_symbol_set_difference(self):
        diff = shadow_diff({"A": 1.0, "B": 2.0}, {"A": 1.0, "C": 3.0})
        assert diff["equal"] is False
        assert diff["sample_only_old"] == ["B"]
        assert diff["sample_only_new"] == ["C"]

    def test_value_difference_beyond_epsilon(self):
        assert shadow_diff({"A": 1.0}, {"A": 1.0 + 1e-12})["equal"] is True
        assert shadow_diff({"A": 1.0}, {"A": 1.01})["equal"] is False

    def test_none_value_switch_counts_as_diff(self):
        # 「没有分数」与「分数是 0」不是一回事，None↔数值必须计差异
        assert shadow_diff({"A": None}, {"A": 0.0})["equal"] is False

    def test_date_mismatch_breaks_equal(self):
        diff = shadow_diff({"A": 1.0}, {"A": 1.0}, old_date="2026-10-07", new_date="2026-10-08")
        assert diff["equal"] is False
        assert diff["old_date"] != diff["new_date"]


@pytest.mark.unit
def test_record_shadow_evidence_counts_and_last_sample():
    client = _FakeRedis()
    record_shadow_evidence("unit", {"equal": False, "only_old": 1}, redis_client=client)
    record_shadow_evidence("unit", {"equal": True}, redis_client=client)
    stats = json.loads(client.values["qm:signal_bucket_shadow:unit:stats"])
    assert stats["total"] == 2 and stats["diff"] == 1
    last = json.loads(client.values["qm:signal_bucket_shadow:unit:last"])
    assert last["equal"] is True
    assert client.writes["qm:signal_bucket_shadow:unit:stats"][1] == 7 * 24 * 3600


# ---------------------------------------------------------------------------
# 生效桶解析（替身注册表，无 DB）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_effective_bucket_with_explicit_user():
    registry = _FakeRegistry(
        {"effective_model_id": "MDL_X", "model_source": "user_default"}
    )
    out = await resolve_effective_bucket(tenant_id="t1", user_id="u1", registry=registry)
    assert out == {
        "bucket": "script_v1_mdl_x",
        "model_id": "MDL_X",
        "model_source": "user_default",
        "fallback_used": False,
        "reason": "",
        "owner_user": "u1",
    }
    assert registry.calls == [{"tenant_id": "t1", "user_id": "u1", "market": "CN"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_effective_bucket_no_model_returns_none():
    registry = _FakeRegistry(
        {"effective_model_id": None, "model_source": "none", "fallback_reason": "no_model"}
    )
    out = await resolve_effective_bucket(tenant_id="t1", user_id="u1", registry=registry)
    assert out["bucket"] is None
    assert out["reason"] == "no_model"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_effective_bucket_registry_error_is_not_guessed():
    registry = _FakeRegistry(error=RuntimeError("db down"))
    out = await resolve_effective_bucket(tenant_id="t1", user_id="u1", registry=registry)
    assert out["bucket"] is None
    assert out["reason"].startswith("resolve_failed")


# ---------------------------------------------------------------------------
# 真库 fixture 工具
# ---------------------------------------------------------------------------


async def _require_db():
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _close_db() -> None:
    """释放共享 asyncpg 连接池（每个真库 async 用例结束必须调用）。

    pytest-asyncio 每个用例一个新事件循环；池内连接绑定创建它的事件循环，
    不释放则下一个用例取到旧连接 → "attached to a different loop"（仓库既有
    真库测试同款纪律，见 test_agent_ledger_store.py）。
    """
    from backend.shared.database_manager_v2 import close_database

    await close_database()


async def _cleanup_tenants(tenants: list[str]) -> None:
    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    score_sql = text(
        "DELETE FROM engine_signal_scores WHERE tenant_id IN :tenants"
    ).bindparams(bindparam("tenants", expanding=True))
    model_sql = text(
        "DELETE FROM qm_user_models WHERE tenant_id IN :tenants"
    ).bindparams(bindparam("tenants", expanding=True))
    async with get_session() as session:
        await session.execute(score_sql, {"tenants": tenants})
        await session.execute(model_sql, {"tenants": tenants})


async def _seed_default_models(tenant: str, owners: list[tuple[str, str]]) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        for user_id, model_id in owners:
            await session.execute(
                text(
                    "INSERT INTO qm_user_models (tenant_id, user_id, model_id, status, "
                    "metadata_json, is_default, activated_at) "
                    "VALUES (:t, :u, :m, 'ready', CAST(:meta AS JSONB), TRUE, NOW())"
                ),
                {"t": tenant, "u": user_id, "m": model_id, "meta": '{"market": "CN"}'},
            )


async def _seed_scores(tenant: str, rows: list[dict]) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        for r in rows:
            await session.execute(
                text(
                    "INSERT INTO engine_signal_scores "
                    "(run_id, tenant_id, user_id, trade_date, symbol, fusion_score, "
                    " model_version, feature_version, market, source, universe_tag, created_at) "
                    "VALUES (:run, :t, :u, :td, :sym, :score, 'inference_script', :fv, "
                    "'CN', 'batch', 'CN', :ts)"
                ),
                {
                    "run": f"run_bscope_{uuid.uuid4().hex[:10]}",
                    "t": tenant,
                    "u": r["user"],
                    "td": r["trade_date"],
                    "sym": r["symbol"],
                    "score": r["score"],
                    "fv": resolve_feature_version(r["model_id"]),
                    "ts": r["created_at"],
                },
            )


async def _seed_two_bucket_tenant(tenant: str) -> dict[str, str]:
    """生产桶 3 行 + 挑战者桶 3 行（600002 在两桶分值不同、挑战者后写）。"""
    tag = tenant.rsplit("-", 1)[-1]
    model_prod = f"mdl_prod_{tag}"
    model_cand = f"mdl_cand_{tag}"
    await _seed_default_models(tenant, [("u1", model_prod)])
    await _seed_scores(
        tenant,
        [
            {"user": "u1", "model_id": model_prod, "symbol": "600001", "score": 0.5, "trade_date": D1, "created_at": TS_PROD},
            {"user": "u1", "model_id": model_prod, "symbol": "600002", "score": 0.4, "trade_date": D1, "created_at": TS_PROD},
            {"user": "u1", "model_id": model_prod, "symbol": "600003", "score": 0.3, "trade_date": D1, "created_at": TS_PROD},
            {"user": "u1", "model_id": model_cand, "symbol": "600002", "score": 0.99, "trade_date": D1, "created_at": TS_CAND},
            {"user": "u1", "model_id": model_cand, "symbol": "600004", "score": 0.8, "trade_date": D1, "created_at": TS_CAND},
            {"user": "u1", "model_id": model_cand, "symbol": "600005", "score": 0.7, "trade_date": D1, "created_at": TS_CAND},
        ],
    )
    return {"model_prod": model_prod, "model_cand": model_cand}


# ---------------------------------------------------------------------------
# 真库：租户级持有者解析
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_owner_resolution_real_db():
    await _require_db()
    tag = uuid.uuid4().hex[:8]
    single = f"{TAG_PREFIX}one-{tag}"
    ambiguous = f"{TAG_PREFIX}two-{tag}"
    await _cleanup_tenants([single, ambiguous])
    try:
        await _seed_default_models(single, [("u1", f"mdl_solo_{tag}")])
        out = await resolve_effective_bucket(tenant_id=single)
        assert out["bucket"] == resolve_feature_version(f"mdl_solo_{tag}")
        assert out["model_id"] == f"mdl_solo_{tag}"
        assert out["model_source"] == "user_default"
        assert out["owner_user"] == "u1"

        # 双用户各有默认 = 歧义 → 不猜（真库路径，不经过 registry）
        await _seed_default_models(
            ambiguous, [("u1", f"mdl_a_{tag}"), ("u2", f"mdl_b_{tag}")]
        )
        out2 = await resolve_effective_bucket(tenant_id=ambiguous)
        assert out2["bucket"] is None
        assert out2["reason"].startswith("ambiguous_default_owners")
    finally:
        await _cleanup_tenants([single, ambiguous])
        await _close_db()


# ---------------------------------------------------------------------------
# 真库：自选池快照三模式矩阵
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchlist_snapshot_bucket_scoping_real_db(monkeypatch):
    await _require_db()
    import backend.shared.signal_scores as ss

    tenant = f"{TAG_PREFIX}matrix-{uuid.uuid4().hex[:8]}"
    await _cleanup_tenants([tenant])
    try:
        ids = await _seed_two_bucket_tenant(tenant)
        fv_prod = resolve_feature_version(ids["model_prod"])
        fv_cand = resolve_feature_version(ids["model_cand"])
        union = {"SH600001", "SH600002", "SH600003", "SH600004", "SH600005"}

        # off：旧口径并集 —— 挑战者行混入（污染复现）
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "off")
        m_off, meta_off = await ss.load_score_snapshot(tenant)
        assert set(m_off) == union
        assert m_off["SH600002"]["value"] == 0.99  # 挑战者后写覆盖生产值
        assert meta_off["scoping"] == "off" and meta_off["bucket"] is None
        assert "shadow" not in meta_off

        # enforce：只读生效模型桶（生产）
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "enforce")
        m_enf, meta_enf = await ss.load_score_snapshot(tenant)
        assert set(m_enf) == {"SH600001", "SH600002", "SH600003"}
        assert m_enf["SH600002"]["value"] == 0.4
        assert meta_enf["bucket"] == fv_prod
        assert meta_enf["model_id"] == ids["model_prod"]
        assert meta_enf["model_source"] == "user_default"

        # enforce + 显式挑战者：只读挑战者桶
        m_cand, meta_cand = await ss.load_score_snapshot(
            tenant, model_id=ids["model_cand"]
        )
        assert set(m_cand) == {"SH600002", "SH600004", "SH600005"}
        assert m_cand["SH600002"]["value"] == 0.99
        assert meta_cand["bucket"] == fv_cand
        assert meta_cand["model_source"] == "explicit_param"

        # shadow：旧口径出数（与 off 相同）+ 差异留痕
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "shadow")
        monkeypatch.setattr(
            ss,
            "record_shadow_evidence",
            lambda kind, diff, **kw: captured.append((kind, dict(diff))),
        )
        m_sh, meta_sh = await ss.load_score_snapshot(tenant)
        assert m_sh == m_off
        shadow = meta_sh["shadow"]
        assert shadow["equal"] is False
        assert shadow["old_rows"] == 5 and shadow["new_rows"] == 3
        assert shadow["only_old"] == 2  # 600004/600005 只在挑战者桶
        assert shadow["value_diff"] == 1  # 600002 两桶分值不同
        assert captured and captured[0][0] == "watchlist"
    finally:
        await _cleanup_tenants([tenant])
        await _close_db()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchlist_enforce_unresolvable_never_mixes(monkeypatch):
    """桶解析失败：off/shadow 出数，enforce 必须空 + 原因（混读比空危险）。"""
    await _require_db()
    import backend.shared.signal_scores as ss

    tenant = f"{TAG_PREFIX}unres-{uuid.uuid4().hex[:8]}"
    await _cleanup_tenants([tenant])
    try:
        ids = await _seed_two_bucket_tenant(tenant)

        async def _unresolved(**_kw):
            return {
                "bucket": None,
                "model_id": None,
                "model_source": None,
                "fallback_used": False,
                "reason": "no_default_model_owner",
                "owner_user": None,
            }

        monkeypatch.setattr(ss, "resolve_effective_bucket", _unresolved)
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "off")
        m_off, _ = await ss.load_score_snapshot(tenant)
        assert set(m_off) == {"SH600001", "SH600002", "SH600003", "SH600004", "SH600005"}

        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "enforce")
        m_enf, meta_enf = await ss.load_score_snapshot(tenant)
        assert m_enf == {}
        assert meta_enf["ok"] is False
        assert "无法解析" in (meta_enf["reason"] or "")
        assert meta_enf["model_id"] is None

        captured: list[dict] = []
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "shadow")
        monkeypatch.setattr(
            ss,
            "record_shadow_evidence",
            lambda kind, diff, **kw: captured.append(dict(diff)),
        )
        m_sh, meta_sh = await ss.load_score_snapshot(tenant)
        assert set(m_sh) == set(m_off)  # shadow 不改变现网行为
        assert meta_sh["shadow"]["bucket_missing"] is True
        assert captured and captured[0]["bucket_missing"] is True
    finally:
        await _cleanup_tenants([tenant])
        await _close_db()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchlist_shadow_single_bucket_diff_zero(monkeypatch):
    """单桶租户影子比对必须 equal（enforce 切换的回归基线：不改变现网结果）。"""
    await _require_db()
    import backend.shared.signal_scores as ss

    tag = uuid.uuid4().hex[:8]
    tenant = f"{TAG_PREFIX}single-{tag}"
    model = f"mdl_single_{tag}"
    await _cleanup_tenants([tenant])
    try:
        await _seed_default_models(tenant, [("u1", model)])
        await _seed_scores(
            tenant,
            [
                {"user": "u1", "model_id": model, "symbol": "600010", "score": 0.6, "trade_date": D1, "created_at": TS_PROD},
                {"user": "u1", "model_id": model, "symbol": "600011", "score": -0.2, "trade_date": D1, "created_at": TS_PROD},
            ],
        )
        captured: list[dict] = []
        monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "shadow")
        monkeypatch.setattr(
            ss,
            "record_shadow_evidence",
            lambda kind, diff, **kw: captured.append(dict(diff)),
        )
        m, meta = await ss.load_score_snapshot(tenant)
        assert set(m) == {"SH600010", "SH600011"}
        assert meta["shadow"]["equal"] is True
        assert captured and captured[0]["equal"] is True
    finally:
        await _cleanup_tenants([tenant])
        await _close_db()


# ---------------------------------------------------------------------------
# 真库：模型信号扫描器（loader）
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scanner_loader_bucket_scoping_real_db(monkeypatch):
    await _require_db()
    import backend.services.engine.scanners.model_signal_loader as msl

    tenant = f"{TAG_PREFIX}loader-{uuid.uuid4().hex[:8]}"
    await _cleanup_tenants([tenant])
    try:
        await _seed_two_bucket_tenant(tenant)

        monkeypatch.setattr(msl, "get_scoping_mode", lambda: "off")
        snap_off = await msl.load_model_signal_snapshot(
            "2099-01-05", tenant_id=tenant, with_price_flags=False
        )
        assert snap_off is not None
        assert set(snap_off.day_scores["symbol"]) == {
            "600001.SH",
            "600002.SH",
            "600003.SH",
            "600004.SH",
            "600005.SH",
        }

        monkeypatch.setattr(msl, "get_scoping_mode", lambda: "enforce")
        snap_enf = await msl.load_model_signal_snapshot(
            "2099-01-05", tenant_id=tenant, with_price_flags=False
        )
        assert snap_enf is not None
        assert set(snap_enf.day_scores["symbol"]) == {
            "600001.SH",
            "600002.SH",
            "600003.SH",
        }
        assert snap_enf.trade_date == "2099-01-05"

        # 显式 model_id → 挑战者桶
        snap_cand = await msl.load_model_signal_snapshot(
            "2099-01-05",
            tenant_id=tenant,
            model_id=f"mdl_cand_{tenant.rsplit('-', 1)[-1]}",
            with_price_flags=False,
        )
        assert snap_cand is not None
        assert set(snap_cand.day_scores["symbol"]) == {
            "600002.SH",
            "600004.SH",
            "600005.SH",
        }
    finally:
        await _cleanup_tenants([tenant])
        await _close_db()
