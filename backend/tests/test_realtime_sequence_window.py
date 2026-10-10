"""时序模型（NativeTFT 类）进实时链的三面守卫：裁定 / 窗口装配 / ONNX 形状契约。

背景：生产默认模型（NativeTFT，``is_sequence_model=true``，``dl_params.step_len=20``，
``input_spec.tensor_shape=[null,20,273]``）是**窗口模型**——批量推理按 symbol 取
step_len 天窗口、``feat_norm``（训练集 mean/std）标准化后前向。实时链原是单帧平铺
契约，把窗口模型切进来必须补三段，本文件逐段钉住：

1. ``sequence_len_of``：窗口长度唯一裁定（dl_step_len → step_len → tensor_shape），
   缺全部来源显式报错——窗口猜错 = 拿错形状喂模型；
2. ``compute_cycle`` 时序装配：3D 矩阵、前帧按因子日对齐、末帧=基线+live 覆盖（覆盖
   只作用于末帧）、feat_norm 标准化后 NaN/Inf 归零（镜像批量模板，与模板函数逐值
   对拍）；缺帧记 ``window_missing`` 不装没发生；
3. ``load_window_quantdb``：帧序=日期序、前部补缺、缺行整帧 None——错位一帧整窗移位；
4. 服务与回放器同一装配（verify_day 注入同源窗口 → 摘要 diff=0）；
5. ``_ensure_session`` 形状契约：时序 3D / 平铺 2D 不许错配（错配 = 静默喂错分数）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from backend.services.engine.inference import realtime_core as rc

_CST = timezone(timedelta(hours=8))


# ── 1. 窗口长度唯一裁定 ─────────────────────────────────────────────


class TestSequenceLenOf:
    @pytest.mark.unit
    def test_flat_models_are_one(self):
        assert rc.sequence_len_of({}) == 1
        assert rc.sequence_len_of({"is_sequence_model": False, "dl_params": {"step_len": 20}}) == 1

    @pytest.mark.unit
    def test_priority_dl_step_len_then_step_len_then_tensor_shape(self):
        assert rc.sequence_len_of(
            {"is_sequence_model": True, "dl_params": {"dl_step_len": 10, "step_len": 20}}
        ) == 10
        assert rc.sequence_len_of(
            {"is_sequence_model": True, "dl_params": {"step_len": 20}}
        ) == 20
        assert rc.sequence_len_of(
            {
                "is_sequence_model": True,
                "dl_params": {},
                "input_spec": {"tensor_shape": [None, 5, 273]},
            }
        ) == 5

    @pytest.mark.unit
    def test_missing_every_source_raises_not_defaults(self):
        with pytest.raises(ValueError, match="step_len"):
            rc.sequence_len_of({"is_sequence_model": True})


# ── 2. feat_norm 与批量模板逐值同口径 ───────────────────────────────


@pytest.mark.unit
def test_apply_feat_norm_matches_batch_template():
    """镜像等价对拍：实时链的 apply_feat_norm 必须与批量模板 _apply_feat_norm 同值。

    批量产出的日频分就是模板函数算的——实时镜像若在 NaN 处理、std=0、运算次序上
    有一处漂移，实时分与批次日频分就会系统性偏开（哨兵跨批/实时比较 = 假骤降）。
    """
    from backend.services.engine.inference.templates.inference_parquet import (
        _apply_feat_norm as batch_norm,
    )

    rng = np.random.default_rng(7)
    x = rng.standard_normal((6, 5)).astype(np.float32)
    x[0, 0] = np.nan
    x[1, 2] = np.inf
    x[2, 3] = -np.inf
    feat_norm = {
        "mean": [0.1, -0.2, 0.0, 0.5, 1.0],
        "std": [1.0, 0.5, 0.0, 2.0, 1.0],  # 含 std=0：两侧都必须视作 1
    }

    got = rc.apply_feat_norm(x, feat_norm)
    want = batch_norm(x, {"feat_norm": feat_norm})

    assert np.array_equal(got, want)
    assert np.isfinite(got).all()
    assert got[0, 0] == 0.0  # NaN → 归零
    assert got[1, 2] == 0.0 and got[2, 3] == 0.0  # ±inf → 归零


@pytest.mark.unit
def test_apply_feat_norm_without_norm_still_kills_nan():
    """无 feat_norm 时实时链**有意**与批量分叉：批量原样返回（NaN 留分里），实时归零。"""
    x = np.array([[np.nan, 1.0]], dtype=np.float32)

    got = rc.apply_feat_norm(x, None)

    assert got[0, 0] == 0.0 and got[0, 1] == 1.0


# ── 3. compute_cycle 时序装配 ───────────────────────────────────────


class _SeqSession:
    """伪 3D 会话：收下 [n, seq, d]，按行求和当分数。"""

    def __init__(self) -> None:
        self.seen: np.ndarray | None = None
        self.calls = 0

    def run(self, _outputs, feeds):
        self.calls += 1
        self.seen = feeds["features"]
        return [self.seen.reshape(self.seen.shape[0], -1).sum(axis=1)]


class _Engine:
    """惰性引擎桩：bootstrap/on_snapshot 无操作，compute 返回注入的 live 值。"""

    def __init__(self, live: dict | None = None) -> None:
        self.live = live or {}
        self.snapshots = []

    def bootstrap(self, _sym, _hist):  # noqa: D102
        return None

    def on_snapshot(self, sym, snap):  # noqa: D102
        self.snapshots.append((sym, snap))

    def compute(self, _sym):  # noqa: D102
        return dict(self.live)


def _seq_args(**over):
    args = {
        "session": _SeqSession(),
        "input_name": "features",
        "cols": ["a", "b"],
        "fill": {"a": -9.0, "b": -9.0},
        "model_version": "seq-test",
        "hot": ["600036.SH"],
        "snapshots": {},
        "baseline": {"600036.SH": {"a": 1.0, "b": 2.0}},
        "histories": {},
        "override": set(),
        "engine": _Engine(),
        "bootstrapped": set(),
        "window": {"600036.SH": [{"a": 0.1, "b": 0.2}, {"a": 0.3, "b": 0.4}]},
        "seq_len": 3,
        "feat_norm": {"mean": [1.0, 2.0], "std": [1.0, 2.0]},
    }
    args.update(over)
    return args


@pytest.mark.unit
def test_sequence_cycle_builds_3d_and_normalizes():
    """前帧按日期序入位、末帧=基线、标准化 (x-mean)/std——窗口对齐错一帧这里就红。"""
    # Arrange
    args = _seq_args()

    # Act
    result = rc.compute_cycle(**args)

    # Assert
    assert args["session"].seen.shape == (1, 3, 2)
    expected = (np.array([[0.1, 0.2], [0.3, 0.4], [1.0, 2.0]], dtype=np.float32)
                - np.array([1.0, 2.0], dtype=np.float32)) / np.array([1.0, 2.0], dtype=np.float32)
    assert np.allclose(args["session"].seen[0], expected)
    assert result.seq_len == 3 and result.window_missing == 0
    assert len(result.scores) == 1 and result.ranks.tolist() == [1]
    assert result.x.shape == (1, 3, 2)
    rc.matrix_digest(result.x, result.cols, "seq-test")  # 3D 摘要可用（回放对账依赖）


@pytest.mark.unit
def test_sequence_cycle_missing_frames_counted_and_zeroed():
    """整帧缺席 → NaN → 标准化后归零；window_missing 如实计数。"""
    args = _seq_args(window={}, seq_len=3)
    # window 为空：两帧全缺；末帧 b 改为缺失（走 NaN 而非 fill——时序口径不填 fill_values）
    args["baseline"] = {"600036.SH": {"a": 1.0, "b": np.nan}}

    result = rc.compute_cycle(**args)

    seen = args["session"].seen
    assert result.window_missing == 1
    assert result.missing == 1  # 末帧 b 缺失
    assert np.isfinite(seen).all()
    assert np.array_equal(seen[0, 0], np.zeros(2, dtype=np.float32))  # 缺帧 → 归零
    assert seen[0, 2, 0] == 0.0  # 末帧 a=(1-1)/1
    assert seen[0, 2, 1] == 0.0  # 末帧 b 缺失 → 归零
    # fill_values 没有参与（否则末帧 b 会是 (-9-2)/2=-5.5）
    assert seen[0, 2, 1] != np.float32(-5.5)


@pytest.mark.unit
def test_sequence_cycle_override_only_patches_last_frame():
    """live 覆盖只作用于末帧：历史帧是既成事实，不该被当前快照改写。"""
    engine = _Engine(live={"a": 5.0})
    args = _seq_args(
        engine=engine,
        override={"a"},
        snapshots={"600036.SH": {"Now": "12.0", "timestamp": "1"}},
    )

    result = rc.compute_cycle(**args)

    seen = args["session"].seen
    assert result.overridden == 1
    assert seen[0, 0, 0] == np.float32((0.1 - 1.0) / 1.0)  # 前帧未被改写
    assert seen[0, 2, 0] == np.float32((5.0 - 1.0) / 1.0)  # 末帧 a=live 覆盖
    assert engine.snapshots  # 快照仍喂给引擎（增量特征引导路径不变）


@pytest.mark.unit
def test_flat_cycle_default_is_still_2d_with_fill():
    """默认参数（seq_len=1）走平铺老路径：二维矩阵 + fill_values 兜底，行为不变。"""
    session = _SeqSession()
    result = rc.compute_cycle(
        session=session, input_name="features", cols=["a"], fill={"a": -7.0},
        model_version="flat", hot=["600036.SH"], snapshots={},
        baseline={"600036.SH": {}}, histories={}, override=set(),
        engine=_Engine(), bootstrapped=set(),
    )

    assert session.seen.shape == (1, 1)  # 2D
    assert session.seen[0, 0] == np.float32(-7.0)
    assert result.seq_len == 1 and result.window_missing == 0
    assert result.missing == 1


@pytest.mark.unit
def test_ledger_entry_records_sequence_metadata():
    """窗口信息随账落盘——回放诊断/运维面板要能看见「这周期用了 20 帧、缺了几只」。"""
    result = rc.compute_cycle(**_seq_args())

    entry = rc.ledger_entry(result, ts=1.0, run_id="rt-x", model_version="seq-test")

    assert entry["seq_len"] == 3 and entry["window_missing"] == 0


# ── 4. 窗口加载器（QuantDB 直读镜像）───────────────────────────────


class _FakeReader:
    """最小 QuantDBFactorReader 面：describe/available_dates/read_range。"""

    def __init__(self, dates, df):
        self._dates = dates
        self._df = df
        self.calls: list[dict] = []

    def describe(self, _lib):
        class _D:
            columns = ["symbol", "trade_date", "a", "b"]
        return _D()

    def available_dates(self, _source, end=None):
        return [d for d in self._dates if d <= str(end)]

    def read_range(self, _source, *, features, feature_sources=None, start, end,
                   include_ohlcv=True):
        self.calls.append({"features": list(features), "start": str(start), "end": str(end),
                           "include_ohlcv": include_ohlcv})
        df = self._df
        return df[(df["trade_date"] >= str(start)) & (df["trade_date"] <= str(end))]


def _meta_quantdb():
    return {
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "feature_columns": ["a", "b"],
    }


def _frames_df():
    import pandas as pd

    return pd.DataFrame(
        {
            "symbol": ["600036", "600036", "000001"],
            "trade_date": ["2026-09-28", "2026-09-29", "2026-09-29"],
            "a": [1.0, 2.0, 9.0],
            "b": [10.0, 20.0, 90.0],
        }
    )


@pytest.mark.unit
def test_load_window_quantdb_aligns_frames_by_date():
    """帧序=因子日序（旧→新）；某标的某日缺行 → 该帧 None（整帧缺席，不伪造）。"""
    reader = _FakeReader(["2026-09-28", "2026-09-29", "2026-09-30"], _frames_df())

    got = rc.load_window_quantdb(
        ["600036.SH", "000001.SZ"], date(2026, 10, 1),
        meta=_meta_quantdb(), cols=["a", "b"], step_len=3, reader=reader,
    )

    assert got["dates"] == ["2026-09-28", "2026-09-29"]  # D=09-30 是基线帧，窗口只取前 2 帧
    assert got["frames"]["600036.SH"] == [
        {"a": 1.0, "b": 10.0},
        {"a": 2.0, "b": 20.0},
    ]
    assert got["frames"]["000001.SZ"] == [None, {"a": 9.0, "b": 90.0}]  # 09-28 缺行
    # 区间读下界/上界=前帧日期两端（不含 D），OHLCV 不捎带
    assert reader.calls[0]["start"] == "2026-09-28" and reader.calls[0]["end"] == "2026-09-29"
    assert reader.calls[0]["include_ohlcv"] is False


@pytest.mark.unit
def test_load_window_quantdb_front_pads_when_history_short():
    """因子日不足 step_len-1 天：前部补 None——绝不把缺帧挤到新端（错位=整窗移位）。"""
    reader = _FakeReader(["2026-09-29", "2026-09-30"], _frames_df())

    got = rc.load_window_quantdb(
        ["600036.SH"], date(2026, 10, 1),
        meta=_meta_quantdb(), cols=["a", "b"], step_len=4, reader=reader,
    )

    assert got["dates"] == [None, None, "2026-09-29"]
    assert got["frames"]["600036.SH"] == [None, None, {"a": 2.0, "b": 20.0}]


@pytest.mark.unit
def test_load_window_quantdb_no_dates_all_none():
    reader = _FakeReader([], _frames_df())

    got = rc.load_window_quantdb(
        ["600036.SH"], date(2026, 10, 1),
        meta=_meta_quantdb(), cols=["a", "b"], step_len=3, reader=reader,
    )

    assert got["frames"] == {}  # 无可用因子日：不产帧（调用方记 window_missing）


@pytest.mark.unit
def test_load_window_for_model_rejects_legacy_snapshot_source():
    with pytest.raises(RuntimeError, match="quantdb_factors"):
        rc.load_window_for_model(
            ["600036.SH"], date(2026, 10, 1),
            meta={"data_source": "snapshot", "feature_columns": ["a"]},
            cols=["a"], step_len=3, reader=_FakeReader([], _frames_df()),
        )


# ── 5. 服务接线（build_cycle 时序路径）──────────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self.seen: np.ndarray | None = None

    def get_inputs(self):
        class _I:
            name = "features"
        return [_I()]

    def run(self, _outputs, feeds):
        self.seen = feeds["features"]
        return [self.seen.reshape(self.seen.shape[0], -1).sum(axis=1)]


def _seq_model_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mdl_seq_test"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "model_type": "nativetft",
                "is_sequence_model": True,
                "dl_params": {"step_len": 3},
                "feature_columns": ["a", "b"],
                "fill_values": {"a": -9.0, "b": -9.0},
                "feat_norm": {"mean": [1.0, 2.0], "std": [1.0, 2.0]},
                "model_version": "seq-rt-test",
            }
        ),
        encoding="utf-8",
    )
    return d


def _fresh_snapshot():
    return {
        "Now": "12.0", "Open": "11.0", "High": "12.1", "Low": "10.9",
        "Volume": "100000", "Amount": "1200000", "PreClose": "11.0",
        "timestamp": str(int(datetime.now(tz=_CST).timestamp())),
    }


@pytest.mark.unit
def test_build_cycle_sequence_wiring(tmp_path, monkeypatch):
    """build_cycle 时序路径：窗口 loader 被调、3D 矩阵过会话、载荷/账本如实标注。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _seq_model_dir(tmp_path)
    session = _FakeSession()
    ledger: list[dict] = []
    window_calls: list[dict] = []

    def _window_loader(symbols, day, *, meta, cols, seq_len):
        window_calls.append({"symbols": list(symbols), "seq_len": seq_len})
        return {"dates": ["d1", "d2"],
                "frames": {"600036.SH": [{"a": 0.1, "b": 0.2}, {"a": 0.3, "b": 0.4}]}}

    svc = RealtimeInferenceService(
        config_loader=lambda: RealtimeInferConfig(
            enabled=True, model_dir=str(model_dir), cadence_s=3,
        ),
        hot_set_fetcher=lambda: ["600036.SH"],
        snapshot_fetcher=lambda syms: {"600036.SH": _fresh_snapshot()},
        baseline_loader=lambda syms, day: {
            "rows": {"600036.SH": {"a": 1.0, "b": 2.0}}, "history": {},
        },
        window_loader=_window_loader,
        ledger_sink=ledger.append,
    )
    ensured: list[tuple] = []
    monkeypatch.setattr(
        svc, "_ensure_session",
        lambda model_dir_, n_features, seq_len=1: (ensured.append((n_features, seq_len)), session)[1],
    )

    payload = svc.build_cycle()

    assert payload is not None
    assert window_calls and window_calls[0]["seq_len"] == 3
    assert ensured == [(2, 3)]  # feature 数 + 窗口长度都过会话契约
    assert session.seen.shape == (1, 3, 2)
    assert payload["quality"]["seq_len"] == 3
    assert payload["quality"]["window_missing"] == 0
    assert ledger and ledger[0]["seq_len"] == 3
    # 分数 = 标准化后整窗求和：末帧归零 + 前两帧定值
    # 行符号=后缀身份（审计 M2；账本/落库冲突键以它为身份）
    assert payload["scores"][0]["symbol"] == "600036.SH"
    assert len(payload["scores"]) == 1


# ── 6. _ensure_session 形状契约（真 ONNX）───────────────────────────


def _export_onnx_3d(path: Path, step_len: int, dim: int) -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnxruntime")

    class _M(torch.nn.Module):
        def forward(self, x):  # [b, s, d]
            return x[:, -1, :].sum(dim=1)

    torch.onnx.export(
        _M().eval(), torch.zeros(1, step_len, dim), str(path),
        input_names=["input"], output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17, dynamo=False,
    )


def _export_onnx_2d(path: Path, dim: int) -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnxruntime")

    class _M(torch.nn.Module):
        def forward(self, x):  # [b, d]
            return x.sum(dim=1)

    torch.onnx.export(
        _M().eval(), torch.zeros(1, dim), str(path),
        input_names=["input"], output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17, dynamo=False,
    )


@pytest.mark.unit
def test_ensure_session_shape_contract(tmp_path):
    """错配必须报错，不许静默：时序模型接 3D、平铺接 2D，窗口长度也要对得上。"""
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    seq_dir = tmp_path / "seq"
    seq_dir.mkdir()
    _export_onnx_3d(seq_dir / "model.onnx", step_len=20, dim=3)
    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    _export_onnx_2d(flat_dir / "model.onnx", dim=3)

    def _svc():
        # 每断言新风实例：session 缓存按 model_dir 键（与 seq_len 无关），
        # 复用实例会让「该报错的第二次调用」吃缓存静默通过
        return RealtimeInferenceService(config_loader=lambda: None, status_writer=lambda _p: None)

    assert _svc()._ensure_session(seq_dir, 3, 20) is not None
    with pytest.raises(RuntimeError, match="窗口长度"):
        _svc()._ensure_session(seq_dir, 3, 5)
    with pytest.raises(RuntimeError, match="3D"):
        _svc()._ensure_session(seq_dir, 3, 1)

    assert _svc()._ensure_session(flat_dir, 3) is not None
    with pytest.raises(RuntimeError, match="3D ONNX"):
        _svc()._ensure_session(flat_dir, 3, 20)
    with pytest.raises(RuntimeError, match="输入维度"):
        _svc()._ensure_session(flat_dir, 7)


# ── 7. 服务 → 回放 摘要 diff=0（时序路径同源装配）───────────────────


@pytest.mark.integration
def test_sequence_replay_diff_zero(tmp_path):
    """在线（窗口注入）与回放（同窗口注入）逐周期摘要相等——回放漏拼窗口这里必红。"""
    import pandas as pd

    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )
    from backend.services.engine.inference.replay_verifier import verify_day

    model_dir = _seq_model_dir(tmp_path)
    session = _FakeSession()
    ledger: list[dict] = []
    frames = {"600036.SH": [{"a": 0.1, "b": 0.2}, {"a": 0.3, "b": 0.4}]}

    svc = RealtimeInferenceService(
        config_loader=lambda: RealtimeInferConfig(
            enabled=True, model_dir=str(model_dir), cadence_s=3,
        ),
        hot_set_fetcher=lambda: ["600036.SH"],
        snapshot_fetcher=lambda syms: {"600036.SH": _fresh_snapshot()},
        baseline_loader=lambda syms, day: {
            "rows": {"600036.SH": {"a": 1.0, "b": 2.0}}, "history": {},
        },
        window_loader=lambda symbols, day, *, meta, cols, seq_len: {
            "dates": ["d1", "d2"], "frames": frames,
        },
        ledger_sink=ledger.append,
    )
    svc._ensure_session = lambda model_dir_, n_features, seq_len=1: session  # type: ignore[method-assign]
    assert svc.build_cycle() is not None

    day = datetime.now(tz=_CST).date()
    snap = _fresh_snapshot()
    l05 = pd.DataFrame([{"symbol": "600036.SH", "ts": int(snap["timestamp"]), **snap}])
    report = verify_day(
        day=day, model_dir=model_dir, ledger=ledger, frames=l05,
        baseline_bundle={"rows": {"600036.SH": {"a": 1.0, "b": 2.0}}, "history": {}},
        window_frames=frames,
        session_factory=lambda _p, _n: _FakeSession(),
    )

    assert report["diff_zero"] is True, report.get("mismatch_details")
    assert report["entries"] == 1
