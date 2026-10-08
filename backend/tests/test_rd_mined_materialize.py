"""rd_mined 物化器：清单/续跑、值级相关查重、因子代码执行、分区 schema 对齐。

物化器把 ``rd_agent_factors`` 里带代码的因子执行出全历史因子值，
写进 ``6_ml_datasets/rd_mined``（CUSTOM 市场），供训练直读消费。
"""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

from backend.scripts.rd_mined_materialize import (
    MANIFEST_NAME,
    _align_partition_schemas,
    _as_float,
    _column_owners,
    _disambiguate_column,
    _eligible_row,
    _evaluate_gates,
    _execute_factor_code,
    _load_manifest,
    _max_abs_corr,
    _pick_sample_days,
    _save_manifest,
    _should_materialize,
    _to_canonical,
)
from backend.shared.factor_identity import code_fingerprint


# ── 清单与续跑 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_manifest_roundtrip(tmp_path):
    assert _load_manifest(tmp_path) == {}
    data = {"fid1": {"status": "materialized", "column": "rd_vz5", "rows": 123}}
    _save_manifest(tmp_path, data)
    assert (tmp_path / MANIFEST_NAME).is_file()
    assert _load_manifest(tmp_path) == data


@pytest.mark.unit
def test_manifest_corrupt_file_treated_as_empty(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{ not json", encoding="utf-8")
    assert _load_manifest(tmp_path) == {}


@pytest.mark.unit
def test_should_materialize_lifecycle():
    row = {"factor_id": "fid1", "factor_code": "x = 1"}
    # 新因子
    assert _should_materialize(row, {}, force=False) == (True, "new")
    # 已物化 → 跳过；--force 重做
    m = {"fid1": {"status": "materialized"}}
    ok, reason = _should_materialize(row, m, force=False)
    assert not ok and reason == "already_materialized"
    assert _should_materialize(row, m, force=True) == (True, "force")
    # 值级查重拒入的因子默认不再尝试；--force 才重算
    m2 = {"fid1": {"status": "rejected_duplicate"}}
    ok, reason = _should_materialize(row, m2, force=False)
    assert not ok and reason == "rejected_duplicate"
    # 上次失败 → 重试
    m3 = {"fid1": {"status": "error"}}
    assert _should_materialize(row, m3, force=False) == (True, "retry")


@pytest.mark.unit
def test_should_materialize_code_change_invalidates_verdict():
    """同 factor_id 代码改写（同任务重跑 UPDATE factor_code）→ 旧值/旧判定失效。"""
    row_new = {"factor_id": "fid1", "factor_code": "x = 2"}
    fp_old = code_fingerprint("x = 1  # 旧版")
    row_old = {"factor_id": "fid1", "factor_code": "x = 1  # 旧版"}
    # 代码未变 → 维持跳过
    m = {"fid1": {"status": "materialized", "code_fp": fp_old}}
    assert _should_materialize(row_old, m, force=False) == (
        False,
        "already_materialized",
    )
    # 代码变了 → 重算（物化与拒绝条目一视同仁）
    assert _should_materialize(row_new, m, force=False) == (True, "code_changed")
    m_rej = {"fid1": {"status": "rejected_duplicate", "code_fp": fp_old}}
    assert _should_materialize(row_new, m_rej, force=False) == (True, "code_changed")
    # 升级前写的旧清单没有指纹 → 保守不动（不因缺指纹误重算）
    m_legacy = {"fid1": {"status": "materialized"}}
    assert _should_materialize(row_new, m_legacy, force=False) == (
        False,
        "already_materialized",
    )


@pytest.mark.unit
def test_eligible_row_market_and_code_gate():
    ok, reason = _eligible_row(
        {"factor_id": "a", "market": "a_share", "factor_code": "x = 1"}
    )
    assert ok and reason == "ok"
    ok, reason = _eligible_row(
        {"factor_id": "b", "market": "hong_kong", "factor_code": "x = 1"}
    )
    assert not ok and reason == "market_unsupported"
    ok, reason = _eligible_row(
        {"factor_id": "c", "market": "a_share", "factor_code": "  "}
    )
    assert not ok and reason == "no_code"
    ok, reason = _eligible_row(
        {"factor_id": "d", "market": "a_share", "factor_code": None}
    )
    assert not ok and reason == "no_code"


# ── 采样日 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_pick_sample_days_even_and_bounded():
    days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 5) for d in range(1, 11)]  # 40 天
    picked = _pick_sample_days(days, n=10)
    assert len(picked) == 10
    assert picked[0] == days[0] and picked[-1] == days[-1]
    assert picked == sorted(picked)
    # 天数不足 → 全量
    assert _pick_sample_days(days[:5], n=10) == days[:5]
    assert _pick_sample_days([], n=10) == []


# ── 值级查重（逐日截面秩相关，取日均）────────────────────────────────────


def _corr_frame(n_days: int = 20, n_syms: int = 30, seed: int = 7):
    days = [f"2026-01-{d:02d}" for d in range(1, n_days + 1)]
    syms = [f"SH60{i:04d}" for i in range(n_syms)]
    idx = pd.MultiIndex.from_product([days, syms], names=["trade_date", "symbol"])
    rng = np.random.default_rng(seed)
    base = pd.Series(rng.normal(size=len(idx)), index=idx, name="new")
    controls = pd.DataFrame(
        {
            "dup": base * 3.0 + 1.0,  # 保秩变换 → |ρ|=1
            "neg": -base,  # 反秩 → |ρ|=1
            "noise": pd.Series(rng.normal(size=len(idx)), index=idx),
        },
        index=idx,
    )
    return base, controls


@pytest.mark.unit
def test_max_abs_corr_detects_rank_duplicate():
    base, controls = _corr_frame()
    rho, col = _max_abs_corr(base, controls)
    assert rho == pytest.approx(1.0, abs=1e-9)
    assert col in {"dup", "neg"}


@pytest.mark.unit
def test_max_abs_corr_noise_is_low():
    base, controls = _corr_frame()
    rho_noise, _ = _max_abs_corr(base, controls[["noise"]])
    assert rho_noise < 0.3


@pytest.mark.unit
def test_max_abs_corr_ignores_nan_pairs():
    base, controls = _corr_frame()
    base_missing = base.copy()
    base_missing.iloc[:100] = np.nan
    rho, col = _max_abs_corr(base_missing, controls)
    assert rho == pytest.approx(1.0, abs=1e-9)


@pytest.mark.unit
def test_max_abs_corr_respects_sample_days():
    base, controls = _corr_frame()
    days = sorted({d for d, _ in base.index})
    # 只用一半日期：仍应识别 dup（同秩关系在子集上保持）
    rho, col = _max_abs_corr(base, controls, sample_days=set(days[:10]))
    assert rho == pytest.approx(1.0, abs=1e-9) and col in {"dup", "neg"}
    # 无重叠日期 → 无法计算，返回 0（不误报）
    rho0, col0 = _max_abs_corr(base, controls, sample_days={"1999-01-01"})
    assert rho0 == 0.0 and col0 is None


@pytest.mark.unit
def test_max_abs_corr_excludes_own_old_column():
    """--force 重做时排除自己的旧列：拿旧值比新值会 |ρ|=1 自我拒绝。"""
    base, controls = _corr_frame()
    frame = controls[["noise"]].assign(rd_old=base * 2.0)
    # 不排除：与自己的旧列完全同秩，命中
    rho_plain, col_plain = _max_abs_corr(base, frame)
    assert rho_plain == pytest.approx(1.0, abs=1e-9) and col_plain == "rd_old"
    # 排除后：只剩噪声列，不再自我拒绝
    rho_excl, col_excl = _max_abs_corr(base, frame, exclude={"rd_old"})
    assert col_excl == "noise" and rho_excl < 0.3
    # 排除列不存在于对照帧：静默忽略（不抛错）
    rho_miss, _ = _max_abs_corr(base, frame, exclude={"not_there"})
    assert rho_miss == pytest.approx(1.0, abs=1e-9)


# ── 执行结果规范化（_to_canonical）────────────────────────────────────


@pytest.mark.unit
def test_to_canonical_cleans_inf_like_nan():
    """±inf（除零产物）与 NaN 同罪，落库前统一清掉，绝不进训练列。"""
    idx = pd.MultiIndex.from_arrays(
        [
            pd.to_datetime(["2026-01-05"] * 4),
            ["sh600000", "sh600036", "sz000001", "sz000002"],
        ],
        names=["datetime", "instrument"],
    )
    frame = pd.DataFrame({"f": [1.0, np.inf, -np.inf, np.nan]}, index=idx)
    out = _to_canonical(frame)
    assert out.tolist() == [1.0]
    assert out.index.get_level_values(0)[0] == "2026-01-05"
    assert out.index.get_level_values(1)[0] == "SH600000"  # 前缀式标准化


@pytest.mark.unit
def test_to_canonical_rejects_wrong_index_shape():
    frame = pd.DataFrame({"f": [1.0, 2.0]})  # 单层索引
    with pytest.raises(ValueError):
        _to_canonical(frame)


# ── 因子代码执行（需要 PyTables 读写 h5）────────────────────────────────


def _tiny_pv(path):
    idx = pd.MultiIndex.from_product(
        [
            pd.to_datetime(["2026-01-05", "2026-01-06"]),
            ["sh600000", "sh600036", "sz000001"],
        ],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({"$close": np.arange(6, dtype="float64") + 1.0}, index=idx)
    df.to_hdf(path, key="data", mode="w")


@pytest.mark.unit
def test_execute_factor_code_main_guard(tmp_path):
    pytest.importorskip("tables")
    h5 = tmp_path / "daily_pv.h5"
    _tiny_pv(h5)
    code = (
        "import pandas as pd\n"
        "def calculate_demo():\n"
        "    df = pd.read_hdf('daily_pv.h5')\n"
        "    df['demo'] = df['$close'] * 2.0\n"
        "    result = df[['demo']].copy()\n"
        "    result.to_hdf('result.h5', key='data', mode='w')\n"
        "    return result\n"
        "if __name__ == '__main__':\n"
        "    calculate_demo()\n"
    )
    out = tmp_path / "out.parquet"
    rows = _execute_factor_code(code, h5, out)
    assert rows == 6
    got = pd.read_parquet(out)
    assert got.iloc[:, 0].tolist() == [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]


@pytest.mark.unit
def test_execute_factor_code_no_guard_fallback(tmp_path):
    """无 __main__ 守卫、只写返回值：脚本显式调用 calculate_*() 并落盘。"""
    pytest.importorskip("tables")
    h5 = tmp_path / "daily_pv.h5"
    _tiny_pv(h5)
    code = (
        "import pandas as pd\n"
        "def calculate_demo():\n"
        "    df = pd.read_hdf('daily_pv.h5')\n"
        "    return (df[['$close']] + 1.0).rename(columns={'$close': 'demo'})\n"
    )
    out = tmp_path / "out.parquet"
    rows = _execute_factor_code(code, h5, out)
    assert rows == 6
    got = pd.read_parquet(out)
    assert got.iloc[0, 0] == pytest.approx(2.0)


@pytest.mark.unit
def test_execute_factor_code_failure_raises(tmp_path):
    pytest.importorskip("tables")
    h5 = tmp_path / "daily_pv.h5"
    _tiny_pv(h5)
    with pytest.raises(RuntimeError):
        _execute_factor_code("raise ValueError('boom')", h5, tmp_path / "o.parquet")
    with pytest.raises(RuntimeError):
        _execute_factor_code("x = 1", h5, tmp_path / "o2.parquet")  # 不产 result.h5


# ── h5 缓存魔数校验（L5：防因子脚本写坏共享缓存）──────────────────────


@pytest.mark.unit
def test_h5_magic_validation(tmp_path):
    from backend.scripts.rd_mined_materialize import _h5_is_valid

    good = tmp_path / "good.h5"
    good.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 16)
    bad = tmp_path / "bad.h5"
    bad.write_bytes(b"not an hdf5 file")
    assert _h5_is_valid(good)
    assert not _h5_is_valid(bad)
    assert not _h5_is_valid(tmp_path / "missing.h5")


# ── 分区 schema 对齐 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_align_partition_schemas_unifies_columns(tmp_path):
    def _write(dt, cols):
        d = tmp_path / f"dt={dt}"
        d.mkdir(parents=True)
        pd.DataFrame(
            {
                "symbol": ["600000.SH", "600036.SH"],
                "date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
                **cols,
            }
        ).to_parquet(d / "data.parquet", index=False)

    _write("20260105", {"rd_a": np.array([1.0, 2.0])})
    _write("20260106", {"rd_b": np.array([3.0, 4.0])})
    report = _align_partition_schemas(tmp_path)
    assert report["files"] == 2 and report["aligned"] == 2
    a = pd.read_parquet(tmp_path / "dt=20260105" / "data.parquet")
    b = pd.read_parquet(tmp_path / "dt=20260106" / "data.parquet")
    assert list(a.columns) == list(b.columns)
    assert {"rd_a", "rd_b"} <= set(a.columns)
    assert np.isnan(a["rd_b"]).all() and a["rd_a"].tolist() == [1.0, 2.0]
    # 幂等：再跑一遍全部已对齐
    report2 = _align_partition_schemas(tmp_path)
    assert report2["aligned"] == 0


@pytest.mark.unit
def test_align_partition_schemas_single_or_empty(tmp_path):
    assert _align_partition_schemas(tmp_path)["files"] == 0


@pytest.mark.unit
def test_align_partition_schemas_normalizes_mixed_numeric_dtypes(tmp_path):
    """int64 与 float64 混读时 DuckDB 按第一个文件的类型静默取整（1.5→2）。"""

    def _write(dt, dtype):
        d = tmp_path / f"dt={dt}"
        d.mkdir(parents=True)
        pd.DataFrame(
            {
                "symbol": ["600000.SH", "600036.SH"],
                "date": pd.to_datetime(["2026-01-05"] * 2),
                "rd_rank": np.array([1, 3], dtype=dtype),
            }
        ).to_parquet(d / "data.parquet", index=False)

    _write("20260105", "int64")
    _write("20260106", "float64")
    report = _align_partition_schemas(tmp_path)
    assert report["aligned"] == 1  # 只有 int64 那半边需要转型
    a = pd.read_parquet(tmp_path / "dt=20260105" / "data.parquet")
    b = pd.read_parquet(tmp_path / "dt=20260106" / "data.parquet")
    assert a["rd_rank"].dtype == "float64" and b["rd_rank"].dtype == "float64"
    assert a["rd_rank"].tolist() == [1.0, 3.0]
    # 幂等：第二次全部已对齐
    assert _align_partition_schemas(tmp_path)["aligned"] == 0


# ── 列名冲突消歧 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_column_owners_from_manifest():
    manifest = {
        "a": {"status": "materialized", "column": "rd_momentum_5d"},
        # error 也占名：失败重试要落回原列，避免与后来者交错抢名
        "b": {"status": "error", "column": "rd_x"},
        "c": {"status": "materialized"},  # 无列名 → 不占
    }
    owners = _column_owners(manifest)
    assert owners == {"rd_momentum_5d": "a", "rd_x": "b"}


@pytest.mark.unit
def test_disambiguate_column_collision_and_identity():
    owners = {"rd_momentum_5d": "other-id"}
    col = _disambiguate_column("rd_momentum_5d", "9636aa50xxxx", owners)
    assert col == "rd_momentum_5d_9636aa"
    assert len(col) <= 80
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col)
    # 已归自己 / 无占用 → 原样
    assert (
        _disambiguate_column("rd_momentum_5d", "other-id", owners) == "rd_momentum_5d"
    )
    assert _disambiguate_column("rd_new", "fid", owners) == "rd_new"
    # 超长名：截断时优先保后缀，长度仍 ≤80
    long_base = "rd_" + "x" * 77
    col2 = _disambiguate_column(long_base, "abcdef123456", {long_base: "other"})
    assert col2.endswith("_abcdef") and len(col2) == 80
    # 前 6 位也已占用 → 加长后缀直到唯一
    owners2 = {long_base: "other", f"{long_base[:73]}_abcdef": "another"}
    col3 = _disambiguate_column(long_base, "abcdef123456", owners2)
    assert col3 != col2 and col3.endswith("_abcdef12") and len(col3) <= 80


@pytest.mark.unit
def test_disambiguate_column_empty_factor_id_numeric_fallback():
    """factor_id 为空（历史脏数据）→ 数字后缀，循环恒有界。"""
    owners = {"rd_x": "other"}
    assert _disambiguate_column("rd_x", "", owners) == "rd_x_2"
    owners2 = {"rd_x": "other", "rd_x_2": "another", "rd_x_3": "third"}
    assert _disambiguate_column("rd_x", "", owners2) == "rd_x_4"
    # factor_id 用尽（整个 id 已含进后缀）也不死循环：退数字后缀
    full = "abcdef"
    owners3 = {"rd_x": "other", f"rd_x_{full}": "another", "rd_x_2": "third"}
    assert _disambiguate_column("rd_x", full, owners3) == "rd_x_3"


# ── 运行锁（并发保护）──────────────────────────────────────────────────


@pytest.mark.unit
def test_run_lock_is_exclusive_and_releasable(tmp_path, monkeypatch):
    """flock 独占：第二个进程（同进程第二个 fd 同语义）让位，释放后可再取。

    锁路径指到 tmp_path：默认全局锁路径上可能正跑着真实回填，测试若去
    抢同一把锁会随环境红/绿（拿到锁的一方行为相反）。
    """
    from backend.scripts.rd_mined_materialize import _acquire_run_lock

    monkeypatch.setenv("RD_MINED_MATERIALIZE_LOCK", str(tmp_path / "run.lock"))
    first = _acquire_run_lock()
    assert first is not None
    try:
        assert _acquire_run_lock() is None, "持锁期间第二方必须拿不到（退出 0 让位）"
    finally:
        first.close()
    second = _acquire_run_lock()
    assert second is not None
    second.close()


@pytest.mark.unit
def test_lock_path_env_override(tmp_path, monkeypatch):
    """覆盖口本身要有效：设了 env 走覆盖路径、不设走全局默认。

    没有这条，隔离缝被改坏时独占测试在「恰好没有回填在跑」的机器上照样
    通过——缝坏了也是绿的。
    """
    from backend.scripts.rd_mined_materialize import _lock_path

    monkeypatch.setenv("RD_MINED_MATERIALIZE_LOCK", str(tmp_path / "x.lock"))
    assert _lock_path() == tmp_path / "x.lock"
    monkeypatch.delenv("RD_MINED_MATERIALIZE_LOCK")
    assert _lock_path().name == "_rd_mined_materialize.lock"


# ── 物化门禁（P1）──────────────────────────────────────────────────────


class TestMaterializeGates:
    @pytest.mark.unit
    def test_rejected_gate_is_terminal_like_duplicate(self):
        row = {"factor_id": "fid1", "factor_code": "x = 1"}
        m = {"fid1": {"status": "rejected_gate"}}
        ok, reason = _should_materialize(row, m, force=False)
        assert not ok and reason == "rejected_gate"
        assert _should_materialize(row, m, force=True) == (True, "force")
        # 代码改写 → 旧判定失效，重算
        m2 = {"fid1": {"status": "rejected_gate", "code_fp": code_fingerprint("x = 1")}}
        row2 = {"factor_id": "fid1", "factor_code": "x = 2"}
        assert _should_materialize(row2, m2, force=False) == (True, "code_changed")

    @pytest.mark.unit
    def test_as_float_missing_is_none(self):
        assert _as_float("0.5") == 0.5
        assert _as_float(None) is None
        assert _as_float("") is None
        assert _as_float("abc") is None

    @pytest.mark.asyncio
    async def test_evaluate_gates_soft_default_then_strict_env(self, monkeypatch):
        """软默认：指标差只 fail 不拒；env 全局 strict → 同输入硬拒。

        user_id 留空 → 跳过池分位查询，本测试不碰 DB。
        """
        monkeypatch.delenv("QM_MINING_GATES_MODE", raising=False)
        monkeypatch.delenv("QM_MINING_GATES_DISABLED", raising=False)
        row = {
            "factor_id": "f1",
            "market": "a_share",
            "universe": "csi300",
            "user_id": "",
            "pfs": "0.2",  # < 0.9 → fail
            "rre": "0.9",  # ≥ 0.5 → pass
            "ann_turnover": "10",  # ≤ 60 → pass
            "ann_return_net": "0.5",  # ≥ 0 → pass
        }
        soft = await _evaluate_gates(row)
        by_key = {o.key: o for o in soft.outcomes}
        assert soft.rejected is False
        assert by_key["pfs_floor"].status == "fail"
        assert by_key["pfs_floor"].mode == "soft"
        assert by_key["rre_floor"].status == "pass"
        assert by_key["ic_pool_pct"].status == "skipped", "无池分位 → 判 skipped 不判 0"
        assert by_key["ic_pool_pct"].message.startswith("IC 池内分位")

        monkeypatch.setenv("QM_MINING_GATES_MODE", "strict")
        strict = await _evaluate_gates(row)
        assert strict.rejected is True, "strict 下软门槛全升硬，fail 即拒"

    @pytest.mark.asyncio
    async def test_query_candidates_exposes_gate_inputs_and_real_percentile(self):
        """真库链路：_query_candidates 装配门禁输入 → 池内分位落进 ic_pool_pct。"""
        import json as _json
        import uuid as _uuid

        from sqlalchemy import text as _sql

        from backend.scripts.rd_mined_materialize import _query_candidates
        from backend.shared.database_manager_v2 import close_database, get_session
        from backend.shared.factor_pool_contract import POOL_TABLE

        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(_sql("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 不可用: {exc}")

        run = f"t-mat-{_uuid.uuid4().hex[:10]}"
        uid = f"{run}-u"
        f_low, f_high = f"{run}_l", f"{run}_h"
        ids = [f_low, f_high]
        try:
            async with get_session() as session:
                for fid, ic in ((f_low, 0.01), (f_high, 0.03)):
                    await session.execute(
                        _sql("""
                            INSERT INTO rd_agent_factors
                                (factor_id, factor_name, factor_code, status, user_id,
                                 metadata_json, market, universe, factor_formulation,
                                 ic_value)
                            VALUES
                                (:fid, :name, '-', 'completed', :uid,
                                 CAST(:meta AS JSONB), 'a_share', 'csi300', 'close',
                                 :ic)
                        """),
                        {
                            "fid": fid,
                            "name": f"name-{fid}",
                            "uid": uid,
                            "ic": ic,
                            "meta": _json.dumps(
                                {
                                    "quality": {"pfs": 0.95},
                                    "rre": 0.6,
                                    "ann_turnover": 12.5,
                                    "ann_return_net": 0.08,
                                }
                            ),
                        },
                    )
                    await session.execute(
                        _sql(
                            f"INSERT INTO {POOL_TABLE} "
                            "(factor_id, user_id, market, universe) "
                            "VALUES (:fid, :uid, 'a_share', 'csi300') "
                            "ON CONFLICT (factor_id) DO NOTHING"
                        ),
                        {"fid": fid, "uid": uid},
                    )

            rows = await _query_candidates(factor_ids=[f_low])
            assert len(rows) == 1
            row = rows[0]
            assert row["pfs"] == "0.95" and row["rre"] == "0.6"
            assert row["ann_turnover"] == "12.5"
            assert row["ann_return_net"] == "0.08"
            assert row["universe"] == "csi300" and row["user_id"] == uid

            decision = await _evaluate_gates(row)
            by_key = {o.key: o for o in decision.outcomes}
            assert decision.rejected is False
            assert by_key["pfs_floor"].status == "pass"
            assert by_key["rre_floor"].status == "pass"
            assert by_key["turnover_cap"].status == "pass"
            assert by_key["net_return_floor"].status == "pass"
            assert by_key["ic_pool_pct"].status == "fail"
            assert by_key["ic_pool_pct"].observed == pytest.approx(0.0), (
                "池内两因子、自身 IC 更低 → 分位 0（真库算出来的，不是缺省）"
            )
        finally:
            async with get_session() as session:
                if ids:
                    await session.execute(
                        _sql(f"DELETE FROM {POOL_TABLE} WHERE factor_id = ANY(:ids)"),
                        {"ids": ids},
                    )
                    await session.execute(
                        _sql(
                            "DELETE FROM rd_agent_factors WHERE factor_id = ANY(:ids)"
                        ),
                        {"ids": ids},
                    )
            await close_database()


# ── 门禁复评模式（--gates-only）──────────────────────────────────────


class TestGatesRefreshSelection:
    @pytest.mark.unit
    def test_terminal_statuses_participate(self):
        from backend.scripts.rd_mined_materialize import _gates_refresh_needed

        for status in ("materialized", "rejected_duplicate", "rejected_gate"):
            ok, reason = _gates_refresh_needed({"status": status}, force=False)
            assert ok and reason == "refresh", status
        # error 未定终局 → 留给常规物化重做，不在这里出裁决
        ok, reason = _gates_refresh_needed({"status": "error"}, force=False)
        assert not ok and reason == "status_error"

    @pytest.mark.unit
    def test_complete_gates_skip_but_partial_reevaluate(self):
        """含 skipped 的裁决非终态：补指标后默认轮次自动重评，无需 --force。"""
        from backend.scripts.rd_mined_materialize import _gates_refresh_needed

        complete = {
            "status": "materialized",
            "gates": {
                "rejected": False,
                "gates": [{"key": "pfs_floor", "status": "pass"}],
            },
        }
        ok, reason = _gates_refresh_needed(complete, force=False)
        assert not ok and reason == "has_gates"
        assert _gates_refresh_needed(complete, force=True) == (True, "force")

        partial = {
            "status": "materialized",
            "gates": {
                "rejected": False,
                "gates": [
                    {"key": "pfs_floor", "status": "pass"},
                    {"key": "rre_floor", "status": "skipped"},
                ],
            },
        }
        assert _gates_refresh_needed(partial, force=False) == (True, "gates_partial")

    @pytest.mark.unit
    def test_cli_flag_parses_and_conflicts_rejected(self):
        from backend.scripts.rd_mined_materialize import _parse_args

        args = _parse_args(["--gates-only", "--force", "--factor-ids", "a,b"])
        assert args.gates_only is True
        assert args.force is True
        assert args.factor_ids == ["a", "b"]
        # 范围/动作冲突的旗标组合必须响亮拒绝，不能静默扩大写范围
        for extra in (["--task-id", "t1"], ["--align-only"], ["--register"]):
            with pytest.raises(SystemExit):
                _parse_args(["--gates-only", *extra])


class TestGatesOnlyRun:
    """``_run_gates_only``：只碰 manifest + metadata，绝不执行因子代码。"""

    @staticmethod
    def _decision(rejected: bool, status: str = "pass"):
        from backend.services.engine.mining_plugins import GateDecision, GateOutcome

        return GateDecision(
            rejected=rejected,
            outcomes=(
                GateOutcome(
                    key="pfs_floor",
                    label="PFS",
                    mode="soft",
                    status=status,
                    message="ok",
                    observed=0.95,
                    threshold=0.9,
                ),
            ),
        )

    def _fake_env(
        self,
        monkeypatch,
        tmp_path,
        manifest,
        rows,
        decision,
        captured,
        *,
        evaluated=None,
        meta_ok=True,
    ):
        import argparse

        import backend.scripts.rd_mined_materialize as mod

        _save_manifest(tmp_path, manifest)
        monkeypatch.setattr(mod, "_lib_root", lambda: tmp_path)

        async def fake_query(**kwargs):
            return list(rows)

        monkeypatch.setattr(mod, "_query_candidates", fake_query)

        async def fake_eval(row):
            if evaluated is not None:
                evaluated.append(dict(row))
            return decision

        monkeypatch.setattr(mod, "_evaluate_gates", fake_eval)

        async def fake_meta(factor_id, entry):
            captured.append((factor_id, dict(entry)))
            return meta_ok

        monkeypatch.setattr(mod, "_update_factor_meta", fake_meta)

        def boom(*a, **k):
            raise AssertionError("gates-only 不得执行因子代码/写数据集")

        monkeypatch.setattr(mod, "_compute_factor_values", boom)
        monkeypatch.setattr(mod, "_write_factor", boom)
        monkeypatch.setattr(mod, "_align_partition_schemas", boom)

        return mod, argparse.Namespace(
            factor_ids=[],
            market="a_share",
            limit=0,
            force=False,
            dry_run=False,
            verbose=False,
        )

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_gates_only(self, monkeypatch, tmp_path):
        """``_run`` 必须把 --gates-only 路由到复评，绝不能落进物化主路径。"""
        import argparse

        import backend.scripts.rd_mined_materialize as mod

        called: list = []
        monkeypatch.setattr(mod, "_lib_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_acquire_run_lock", lambda: object())

        async def fake_gates_only(args):
            called.append(args)
            return 0

        monkeypatch.setattr(mod, "_run_gates_only", fake_gates_only)

        def boom_candidates(*a, **k):
            raise AssertionError("--gates-only 不得进物化主路径（_load_candidates）")

        monkeypatch.setattr(mod, "_load_candidates", boom_candidates)
        rc = await mod._run(argparse.Namespace(gates_only=True))
        assert rc == 0 and len(called) == 1

    @pytest.mark.asyncio
    async def test_merge_preserves_materialized_fields_and_persists(
        self, monkeypatch, tmp_path
    ):
        manifest = {
            "fid1": {
                "status": "materialized",
                "column": "rd_vz5",
                "name": "动量反转",
                "values": 12345,
                "corr": 0.42,
                "code_fp": "abc",
                "at": "2026-10-07T15:19:03Z",
            },
            "fid2": {
                "status": "rejected_duplicate",
                "column": "rd_zz1",
                "name": "旧重复",
                "corr": 0.97,
                "at": "2026-09-29T03:00:00Z",
            },
        }
        rows = [
            {"factor_id": fid, "market": "a_share", "universe": "csi300"}
            for fid in ("fid1", "fid2")
        ]
        captured: list = []
        evaluated: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            evaluated=evaluated,
        )
        rc = await mod._run_gates_only(args)
        assert rc == 0
        assert [r["factor_id"] for r in evaluated] == ["fid1", "fid2"]
        on_disk = _load_manifest(tmp_path)
        e1 = on_disk["fid1"]
        assert e1["status"] == "materialized" and e1["column"] == "rd_vz5"
        assert e1["values"] == 12345 and e1["corr"] == 0.42
        assert e1["at"] == "2026-10-07T15:19:03Z", "物化时间不因复评而改写"
        assert e1["gates"]["rejected"] is False
        assert e1["gates"]["gates"][0]["key"] == "pfs_floor"
        assert e1["gates_at"], "复评自带时间戳，与物化时间可分"
        assert on_disk["fid2"]["status"] == "rejected_duplicate"
        assert on_disk["fid2"]["gates"]["rejected"] is False
        assert [fid for fid, _ in captured] == ["fid1", "fid2"]
        assert captured[0][1]["status"] == "materialized"
        assert captured[0][1]["gates"]["rejected"] is False

    @pytest.mark.asyncio
    async def test_hard_fail_records_but_keeps_status(self, monkeypatch, tmp_path):
        manifest = {
            "fid1": {
                "status": "materialized",
                "column": "c1",
                "name": "n",
                "at": "t0",
            }
        }
        rows = [{"factor_id": "fid1", "market": "a_share", "universe": ""}]
        captured: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(True, status="fail"),
            captured,
        )
        rc = await mod._run_gates_only(args)
        assert rc == 0
        entry = _load_manifest(tmp_path)["fid1"]
        assert entry["status"] == "materialized", "复评不回溯改已有终态"
        assert entry["gates"]["rejected"] is True, "硬性不过如实落库"

    @pytest.mark.asyncio
    async def test_metadata_written_before_manifest(self, monkeypatch, tmp_path):
        """写序：先 metadata（消费方读的那份）后 manifest（跳过判据读的那份）。"""
        import backend.scripts.rd_mined_materialize as mod

        manifest = {
            "fid1": {"status": "materialized", "column": "c1", "name": "n", "at": "t0"}
        }
        rows = [{"factor_id": "fid1", "market": "a_share", "universe": ""}]
        captured: list = []
        states: list = []
        mod2, args = self._fake_env(
            monkeypatch, tmp_path, manifest, rows, self._decision(False), captured
        )

        async def recording_meta(factor_id, entry):
            states.append(_load_manifest(tmp_path).get(factor_id, {}).get("gates"))
            captured.append((factor_id, dict(entry)))
            return True

        monkeypatch.setattr(mod2, "_update_factor_meta", recording_meta)
        rc = await mod2._run_gates_only(args)
        assert rc == 0
        assert states == [None], (
            "metadata 回写时 manifest 还未落 gates（先写消费方那份）"
        )
        assert _load_manifest(tmp_path)["fid1"]["gates"]["rejected"] is False

    @pytest.mark.asyncio
    async def test_metadata_write_failure_is_error_and_retryable(
        self, monkeypatch, tmp_path
    ):
        """metadata 回写失败 ⇒ rc=1 且 manifest 不落 gates（下一轮自动重试）。"""
        manifest = {
            "fid1": {"status": "materialized", "column": "c1", "name": "n", "at": "t0"}
        }
        rows = [{"factor_id": "fid1", "market": "a_share", "universe": ""}]
        captured: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            meta_ok=False,
        )
        rc = await mod._run_gates_only(args)
        assert rc == 1
        assert len(captured) == 1, "回写被尝试过"
        assert "gates" not in _load_manifest(tmp_path)["fid1"], (
            "写失败绝不能把裁决当既成事实落 manifest（否则永久跳过）"
        )

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, monkeypatch, tmp_path):
        manifest = {
            "fid1": {"status": "materialized", "column": "c1", "name": "n", "at": "t0"}
        }
        rows = [{"factor_id": "fid1", "market": "a_share", "universe": ""}]
        captured: list = []
        evaluated: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            evaluated=evaluated,
        )
        args.dry_run = True
        rc = await mod._run_gates_only(args)
        assert rc == 0
        assert captured == []
        assert [r["factor_id"] for r in evaluated] == ["fid1"], (
            "dry-run 也要真评（预演的就是判定本身），只是不落库"
        )
        assert "gates" not in _load_manifest(tmp_path)["fid1"]

    @pytest.mark.asyncio
    async def test_skip_complete_reeval_partial_missing_row(
        self, monkeypatch, tmp_path
    ):
        """三分支各自取证：完整跳过 / 部分重评 / 行缺失不伪造裁决。"""
        manifest = {
            "fid1": {
                "status": "materialized",
                "gates": {
                    "rejected": False,
                    "gates": [{"key": "pfs_floor", "status": "pass"}],
                },
                "at": "t0",
            },
            "fid2": {
                "status": "materialized",
                "column": "c2",
                "at": "t0",
                "gates": {
                    "rejected": False,
                    "gates": [
                        {"key": "pfs_floor", "status": "pass"},
                        {"key": "rre_floor", "status": "skipped"},
                    ],
                },
            },
            "fid3": {"status": "materialized", "column": "c3", "at": "t0"},
        }
        # fid2 有行（部分重评且会写）；fid3 无行（missing_row）
        rows = [{"factor_id": "fid2", "market": "a_share", "universe": ""}]
        captured: list = []
        evaluated: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            evaluated=evaluated,
        )
        rc = await mod._run_gates_only(args)
        assert rc == 0
        assert [r["factor_id"] for r in evaluated] == ["fid2"], (
            "完整 gates 的 fid1 不评；含 skipped 的 fid2 默认重评；fid3 行缺失不评"
        )
        assert [fid for fid, _ in captured] == ["fid2"]
        on_disk = _load_manifest(tmp_path)
        assert on_disk["fid3"].get("gates") is None, "missing_row 不得伪造裁决"
        fresh = on_disk["fid2"]["gates"]["gates"]
        assert [o["status"] for o in fresh] == ["pass"], (
            "旧的部分裁决被整份替换（不再残留 skipped）"
        )

    @pytest.mark.asyncio
    async def test_limit_and_factor_ids_filter(self, monkeypatch, tmp_path):
        manifest = {
            fid: {"status": "materialized", "column": f"c{i}", "at": "t0"}
            for i, fid in enumerate(("fid1", "fid2", "fid3"))
        }
        rows = [
            {"factor_id": fid, "market": "a_share", "universe": ""} for fid in manifest
        ]
        captured: list = []
        evaluated: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            evaluated=evaluated,
        )
        args.factor_ids = ["fid2", "fid3", "nope"]
        args.limit = 1
        rc = await mod._run_gates_only(args)
        assert rc == 0
        assert [r["factor_id"] for r in evaluated] == ["fid2"], (
            "factor_ids 过滤 + limit 截断都在选择层生效"
        )

    @pytest.mark.asyncio
    async def test_code_changed_entry_is_skipped(self, monkeypatch, tmp_path):
        """代码改写后复评会得出「描述旧列」的裁决 → 让常规物化按 code_changed 重做。"""
        manifest = {
            "fid1": {
                "status": "materialized",
                "column": "c1",
                "at": "t0",
                "code_fp": code_fingerprint("x = 1  # 旧版"),
            }
        }
        rows = [
            {
                "factor_id": "fid1",
                "market": "a_share",
                "universe": "",
                "factor_code": "x = 2  # 新版",
            }
        ]
        captured: list = []
        evaluated: list = []
        mod, args = self._fake_env(
            monkeypatch,
            tmp_path,
            manifest,
            rows,
            self._decision(False),
            captured,
            evaluated=evaluated,
        )
        rc = await mod._run_gates_only(args)
        assert rc == 0
        assert evaluated == [] and captured == []
        assert "gates" not in _load_manifest(tmp_path)["fid1"]

    @pytest.mark.asyncio
    async def test_evaluate_failure_keeps_entry_and_rc_nonzero(
        self, monkeypatch, tmp_path
    ):
        import backend.scripts.rd_mined_materialize as mod

        manifest = {"fid1": {"status": "materialized", "column": "c1", "at": "t0"}}
        rows = [{"factor_id": "fid1", "market": "a_share", "universe": ""}]
        captured: list = []
        env_mod, args = self._fake_env(
            monkeypatch, tmp_path, manifest, rows, self._decision(False), captured
        )

        async def raising_eval(row):
            raise RuntimeError("boom")

        monkeypatch.setattr(env_mod, "_evaluate_gates", raising_eval)
        rc = await env_mod._run_gates_only(args)
        assert rc == 1
        assert captured == []
        assert "gates" not in _load_manifest(tmp_path)["fid1"]
