"""T-MV-07：L2 微观结构因子全量接入挖掘数据面 + 正名。

验收（框架 §4）：「挖掘可按 L2 方向跑；数据面列清单与目录一致」。
三条纪律钉死：
1. **列清单 = 磁盘事实**：``l2_factor_columns`` 现场读 parquet schema，剔除
   键列与 OHLCV 锚列（锚列与 base h5 的 $open 等重名，混入即 $ 引用错位），
   输出字母序（parquet 物理列序随日漂移，历史教训）。测试一律用 tmp 夹具，
   列数不写死。
2. **分块左连接不改行数**：200+ 列按 ``_L2_FETCH_CHUNK`` 分块，任一块失败
   只跳过该块；l2 重复键（HK daily_forward 双来源同型坑）→ 整块跳过，绝不
   把错行写进 GB 级共享缓存。
3. **正名**：LLM 提示面（``_l2_referenceable_columns``）与数据面同一列清单
   单源；种子模板以 ``MicroSeed_`` 前缀与 L2 实列区分。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.services.engine.rd_agent import rd_loop_wrapper as rw
from backend.services.engine.rd_agent.market_adapters.a_share import AShareAdapter

_ANCHORS = ("open", "high", "low", "close", "volume", "amount")


def _write_l2_partition(
    quantdb_dir: Path,
    dt: str,
    columns: list[str],
    *,
    n: int = 3,
    with_data: bool = True,
) -> Path:
    """伪造一个 l2_factors 分区（列序故意乱序，验证选择器不依赖文件序）。"""
    part = quantdb_dir / "6_ml_datasets" / "l2_factors" / f"dt={dt}"
    part.mkdir(parents=True, exist_ok=True)
    if not with_data:
        return part
    data: dict[str, object] = {
        "date": pd.date_range("2026-01-01", periods=n),
        "symbol": ["600036.SH"] * n,
    }
    for c in columns:
        data[c] = np.arange(n, dtype="float64") + 1.0
    pq.write_table(pa.table(data), part / "data.parquet")
    return part


class _FakeHub:
    """记录分块请求并返回可定制 frame 的 duck-type hub。"""

    def __init__(
        self, frame: pd.DataFrame, *, fail_from_call: int | None = None
    ) -> None:
        self.frame = frame
        self.fail_from_call = fail_from_call
        self.calls: list[list[str]] = []

    def fetch_ml_columns(self, dataset, columns, start, end, symbols=None):  # noqa: ANN001
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise RuntimeError("boom")
        self.calls.append(list(columns))
        cols = [c for c in columns if c in self.frame.columns]
        return self.frame[["symbol", "trade_date", *cols]].copy()


def _bare_wrapper() -> rw.RDLoopWrapper:
    w = rw.RDLoopWrapper.__new__(
        rw.RDLoopWrapper
    )  # 不跑 __init__（避免拉起适配器注册）
    w.market = "a_share"
    return w


# ── 1. 列清单 = 磁盘事实 ──────────────────────────────────────────────


def test_l2_factor_columns_sorted_minus_keys_and_anchors(tmp_path: Path) -> None:
    _write_l2_partition(
        tmp_path,
        "20260102",
        [*_ANCHORS, "micro_vpin_100", "flow_cancel_ratio", "vol_realized_5min"],
    )
    assert rw.l2_factor_columns(str(tmp_path)) == [
        "flow_cancel_ratio",
        "micro_vpin_100",
        "vol_realized_5min",
    ]


def test_l2_factor_columns_empty_when_missing_or_no_partitions(tmp_path: Path) -> None:
    assert rw.l2_factor_columns(str(tmp_path)) == []  # 目录不存在
    assert rw.l2_factor_columns("") == []
    (tmp_path / "6_ml_datasets" / "l2_factors").mkdir(parents=True)  # 有库目录无分区
    assert rw.l2_factor_columns(str(tmp_path)) == []


def test_l2_factor_columns_latest_partition_wins_and_empty_falls_back(
    tmp_path: Path,
) -> None:
    _write_l2_partition(tmp_path, "20260101", ["micro_old"])
    # 最新分区无 parquet（坏分区）→ 回退到早先可读分区
    _write_l2_partition(tmp_path, "20260102", ["micro_new"], with_data=False)
    assert rw.l2_factor_columns(str(tmp_path)) == ["micro_old"]
    # 最新分区可读 → 以最新为准
    _write_l2_partition(tmp_path, "20260102", ["micro_new", "flow_x"])
    assert rw.l2_factor_columns(str(tmp_path)) == ["flow_x", "micro_new"]


# ── 2. 分块左连接 ────────────────────────────────────────────────────


def _l2_frame(dates: list[str], symbols: list[str], cols: list[str]) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for d in dates:
            rows.append({"symbol": s, "trade_date": pd.Timestamp(d)})
    frame = pd.DataFrame(rows)
    for i, c in enumerate(cols):
        frame[c] = np.arange(len(frame), dtype="float64") + i
    return frame


def test_merge_l2_factors_chunked_left_join_float32(
    tmp_path: Path, monkeypatch
) -> None:
    cols = [f"micro_f{i:03d}" for i in range(60)]  # > _L2_FETCH_CHUNK 强制分块
    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: list(cols))

    df = pd.DataFrame(
        {
            "symbol": ["600036.SH", "600036.SH", "000001.SZ", "000001.SZ"],
            "trade_date": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-01", "2026-01-02"]
            ),
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )
    # l2 只覆盖部分 (symbol, date)：未覆盖行必须留 NaN（左连接语义）
    hub = _FakeHub(_l2_frame(["2026-01-01", "2026-01-02"], ["600036.SH"], cols))

    out, merged = _bare_wrapper()._merge_l2_factors(
        df, hub, str(tmp_path), None, None, None
    )

    assert len(out) == len(df), "左连接不得改变行数"
    assert merged == cols
    assert [len(c) for c in hub.calls] == [
        rw._L2_FETCH_CHUNK,
        len(cols) - rw._L2_FETCH_CHUNK,
    ]
    assert [c for call in hub.calls for c in call] == cols, (
        "分块并集 = 全量清单，序稳定"
    )
    for c in cols:
        assert out[c].dtype == np.float32
    assert out.loc[1, "micro_f000"] == 1.0  # 600036.SH 2026-01-02 有数据
    assert np.isnan(out.loc[2, "micro_f000"]), "未覆盖行留 NaN"


def test_merge_l2_factors_chunk_failure_skips_chunk(
    tmp_path: Path, monkeypatch
) -> None:
    cols = [f"micro_f{i:03d}" for i in range(60)]
    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: list(cols))
    df = pd.DataFrame(
        {
            "symbol": ["600036.SH"],
            "trade_date": pd.to_datetime(["2026-01-01"]),
        }
    )
    hub = _FakeHub(_l2_frame(["2026-01-01"], ["600036.SH"], cols), fail_from_call=1)

    out, merged = _bare_wrapper()._merge_l2_factors(
        df, hub, str(tmp_path), None, None, None
    )

    assert len(hub.calls) == 1, "第二块抛异常被吞，仅第一块成功"
    assert merged == cols[: rw._L2_FETCH_CHUNK], "失败块不并入，成功块保留"
    assert "micro_f000" in out.columns
    assert cols[-1] not in out.columns


def test_merge_l2_factors_duplicate_keys_skip_chunk(
    tmp_path: Path, monkeypatch
) -> None:
    cols = ["micro_dup"]
    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: list(cols))
    df = pd.DataFrame(
        {
            "symbol": ["600036.SH", "600036.SH"],
            "trade_date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
        }
    )
    dup = _l2_frame(["2026-01-01", "2026-01-01"], ["600036.SH"], cols)  # 同日两行
    hub = _FakeHub(dup)

    out, merged = _bare_wrapper()._merge_l2_factors(
        df, hub, str(tmp_path), None, None, None
    )

    assert len(out) == len(df), "重复键块整块跳过，行数不变"
    assert "micro_dup" not in out.columns
    assert merged == []


def test_merge_l2_factors_empty_column_list_returns_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: [])
    df = pd.DataFrame({"symbol": ["a"], "trade_date": pd.to_datetime(["2026-01-01"])})
    out, merged = _bare_wrapper()._merge_l2_factors(
        df, _FakeHub(df), str(tmp_path), None, None, None
    )
    assert merged == [] and out is df


# ── 3. 缓存失效随 L2 更新 ────────────────────────────────────────────


def test_h5_cache_fresh_tracks_l2_mtime(tmp_path: Path) -> None:
    t0 = 1_700_000_000.0

    def touch(rel: str, ts: float) -> Path:
        d = tmp_path / rel / "dt=20260101"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "data.parquet"
        f.write_bytes(b"x")
        os.utime(f, (ts, ts))
        return f

    for rel in (
        "1_kline_data/daily_forward",
        "6_ml_datasets/features_daily",
        "6_ml_datasets/l1_factors",
        "6_ml_datasets/l2_factors",
    ):
        touch(rel, t0)
    cache = tmp_path / "cache.h5"
    cache.write_bytes(b"x")
    os.utime(cache, (t0 + 100, t0 + 100))

    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is True
    os.utime(touch("6_ml_datasets/l2_factors", t0 + 200), (t0 + 200, t0 + 200))
    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is False


def test_h5_cache_fresh_requires_l2_column_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    """存量缓存 mtime 新鲜但缺整批 L2 列 → 判定过期；补列后转新鲜。

    没有这条覆盖检查，T-MV-07 之前生成的共享缓存（mtime 比所有输入都新）
    会永远命中，「数据面列清单与目录一致」在存量部署上永不成立。
    """
    t0 = 1_700_000_000.0
    for rel in (
        "1_kline_data/daily_forward",
        "6_ml_datasets/features_daily",
        "6_ml_datasets/l1_factors",
    ):
        d = tmp_path / rel / "dt=20260101"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "data.parquet"
        f.write_bytes(b"x")
        os.utime(f, (t0, t0))

    cache = tmp_path / "cache.h5"
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-01-01"), "sh600036")], names=["datetime", "instrument"]
    )

    def write_cache(columns: dict[str, list[float]]) -> None:
        pd.DataFrame(columns, index=idx).to_hdf(cache, key="data", mode="w")
        os.utime(cache, (t0 + 100, t0 + 100))

    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: ["alpha_x", "beta_y"])
    write_cache({"$close": [1.0]})
    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is False, (
        "缺 L2 列的存量缓存必须判过期"
    )

    write_cache({"$close": [1.0], "$alpha_x": [1.0], "$beta_y": [2.0]})
    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is True

    # 损坏缓存读不出列 → 覆盖检查失败 → 判过期（宁重算不放行）
    cache.write_bytes(b"not-an-h5")
    os.utime(cache, (t0 + 100, t0 + 100))
    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is False

    # 本机无 L2 库（列清单为空）→ 无覆盖义务，mtime 新鲜即新鲜
    write_cache({"$close": [1.0]})
    monkeypatch.setattr(rw, "l2_factor_columns", lambda _d: [])
    assert rw.RDLoopWrapper._h5_cache_fresh(str(tmp_path), str(cache)) is True


# ── 4. 正名（提示面与数据面同源） ────────────────────────────────────


def test_l2_referenceable_columns_uses_disk_list_and_dictionary(
    tmp_path: Path, monkeypatch
) -> None:
    _write_l2_partition(
        tmp_path, "20260102", [*_ANCHORS, "micro_vpin_100", "flow_cancel_ratio"]
    )
    monkeypatch.setattr(
        AShareAdapter, "_get_quantdb_dir", staticmethod(lambda: str(tmp_path))
    )

    out = AShareAdapter._l2_referenceable_columns()

    assert set(out) == {"micro_vpin_100", "flow_cancel_ratio"}, "锚列/键列不得进提示面"
    assert "L2 微观结构列" in out["micro_vpin_100"]
    assert "VPIN" in out["micro_vpin_100"], "释义取自 quantdb_factor_dictionary"
    assert "撤单" in out["flow_cancel_ratio"]


def test_usable_quantdb_columns_includes_l2_real_columns(
    tmp_path: Path, monkeypatch
) -> None:
    _write_l2_partition(tmp_path, "20260102", ["micro_vpin_100"])
    monkeypatch.setattr(
        AShareAdapter, "_get_quantdb_dir", staticmethod(lambda: str(tmp_path))
    )

    cols = AShareAdapter._usable_quantdb_columns()

    assert "rsi_14" in cols, "原 32 列富化清单保留"
    assert "micro_vpin_100" in cols, "L2 真列并入 $ 可引用清单"


def test_l2_referenceable_columns_degrades_to_empty(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        AShareAdapter, "_get_quantdb_dir", staticmethod(lambda: str(tmp_path))
    )
    assert AShareAdapter._l2_referenceable_columns() == {}


def test_seed_templates_are_not_l2_columns() -> None:
    seeds = AShareAdapter._l2_factor_expressions()
    assert seeds, "种子模板非空"
    assert all(k.startswith("MicroSeed_") for k in seeds), (
        "T-MV-07 正名：种子模板不得再以 L2_ 前缀冒充 L2 实列"
    )
