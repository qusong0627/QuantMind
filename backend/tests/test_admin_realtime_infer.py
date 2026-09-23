"""实时推理管理 API 测试（T-P6-08 收口）：配置校验（U）+ 真 Redis 读写（I）。

I 类对真 Redis 的 `qm:realtime:infer:config` 做读写——**先快照现值、结束原样恢复**
（绝不留测试配置污染生产）。
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.unit
def test_validate_model_dir_and_whitelist(tmp_path):
    from backend.services.api.routers.admin.realtime import (
        validate_model_dir,
        validate_override_whitelist,
    )

    with pytest.raises(ValueError):
        validate_model_dir(str(tmp_path / "missing"))
    d = tmp_path / "mdl"
    d.mkdir()
    with pytest.raises(ValueError):
        validate_model_dir(str(d))  # 缺 metadata.json
    (d / "metadata.json").write_text(json.dumps({"feature_columns": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_model_dir(str(d))  # feature_columns 空
    (d / "metadata.json").write_text(
        json.dumps({"feature_columns": ["mom_ret_1d", "f1"]}), encoding="utf-8"
    )
    assert validate_model_dir(str(d)) == ["mom_ret_1d", "f1"]

    validate_override_whitelist(["f1"], ["mom_ret_1d", "f1"])  # 子集通过
    with pytest.raises(ValueError) as exc:
        validate_override_whitelist(["nope"], ["mom_ret_1d", "f1"])
    assert "未定义列" in str(exc.value)


class _RecordingReader:
    """记录构造时收到的 root，并按 root 真假决定「源里有几列」。"""

    seen_roots: list[object] = []
    cols_when_unpinned: tuple[str, ...] = ()

    def __init__(self, root=None, market=None):
        type(self).seen_roots.append(root)
        self._cols = () if root else type(self).cols_when_unpinned

    def describe(self, source):
        import types

        return types.SimpleNamespace(columns=list(self._cols))


@pytest.mark.unit
def test_dead_pin_falls_back_so_coverage_gate_can_pass(monkeypatch, tmp_path):
    """死 pin 不得交给 reader —— 否则覆盖率恒 0%，所有 quantdb 模型都改不了配置。

    实测现场：训练节点把 ``/tmp/quantdb_data`` 写进 metadata.quantdb_dir，
    服务端无此路径。QuantDBFactorReader(死 pin) 不抛异常、describe() 返回 0 列，
    于是 validate_feature_coverage 拿 0% 把**好模型**判成「疑似错误模型」→
    实时推理配置写入恒 400 → 用户改不了模型，旧配置每 15s 刷 metadata.json 不存在。
    """
    from backend.services.api.routers.admin import realtime as rt
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr

    _RecordingReader.seen_roots = []
    _RecordingReader.cols_when_unpinned = ("amt_log", "turn_1")
    monkeypatch.setattr(qfr, "QuantDBFactorReader", _RecordingReader)

    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_l2_factors",
        "quantdb_dir": str(tmp_path / "dead_pin"),  # 不存在
        "context": {"market": "CN"},
    }
    resolved = rt._quantdb_reader_for_meta(meta)

    assert _RecordingReader.seen_roots == [None], "死 pin 必须回退成 None（读本机根）"
    assert resolved is not None, "构造失败会静默回落 parquet 口径，这里必须判得出"
    reader, anchor = resolved
    assert anchor == "l1_l2_factors", "锚源取自 metadata.factor_source"
    assert set(reader.describe(anchor).columns) == {"amt_log", "turn_1"}


@pytest.mark.unit
def test_live_pin_is_still_honoured(monkeypatch, tmp_path):
    """pin 真实存在 → 原样采纳（自定义数据集模型必须读自己的源，不能被回退掉）。"""
    from backend.services.api.routers.admin import realtime as rt
    from backend.services.engine.data_platform import quantdb_factor_reader as qfr

    _RecordingReader.seen_roots = []
    monkeypatch.setattr(qfr, "QuantDBFactorReader", _RecordingReader)

    pin = tmp_path / "quantdb"
    pin.mkdir()
    resolved = rt._quantdb_reader_for_meta(
        {"data_source": "quantdb_factors", "quantdb_dir": str(pin)}
    )

    assert resolved is not None
    assert _RecordingReader.seen_roots == [str(pin)]


@pytest.mark.integration
def test_admin_realtime_config_roundtrip_real_redis(tmp_path):
    import os

    import redis as redis_lib

    from backend.services.api.routers.admin.realtime import (
        CONFIG_KEY,
        InferConfigRequest,
        get_infer_config,
        set_infer_config,
    )

    client = redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
    )
    original = client.hgetall(CONFIG_KEY) or {}
    d = tmp_path / "mdl_ok"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"feature_columns": ["mom_ret_1d", "f1"]}), encoding="utf-8"
    )
    try:
        # ① 读（含状态镜像结构）
        got = asyncio.run(get_infer_config())
        assert got["success"] is True and "config" in got["data"]

        # ② 启用但无 model_dir → 400
        client.delete(CONFIG_KEY)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc0:
            asyncio.run(set_infer_config(InferConfigRequest(enabled=True)))
        assert exc0.value.status_code == 400

        # ③ 正常保存（模型目录 + 白名单子集 + 节拍 + 覆盖率）
        saved = asyncio.run(
            set_infer_config(
                InferConfigRequest(
                    enabled=True, model_dir=str(d), cadence_s=10.0,
                    override_whitelist=["f1"], min_live_coverage=0.6,
                )
            )
        )
        assert saved["success"] is True
        cfg = saved["data"]["config"]
        assert cfg["enabled"] == "true" and cfg["cadence_s"] == "10.0"
        assert cfg["override_whitelist"] == "f1" and cfg["min_live_coverage"] == "0.6"

        # ④ 非法白名单 → 400（防静默错分）
        with pytest.raises(HTTPException) as exc1:
            asyncio.run(
                set_infer_config(
                    InferConfigRequest(override_whitelist=["nope"], model_dir=str(d))
                )
            )
        assert exc1.value.status_code == 400

        # ⑤ 换模型目录未带白名单 → 旧白名单自动清空（防旧列残留）
        saved2 = asyncio.run(
            set_infer_config(InferConfigRequest(model_dir=str(d), enabled=True))
        )
        assert saved2["data"]["config"]["override_whitelist"] == ""
    finally:
        client.delete(CONFIG_KEY)
        if original:
            client.hset(CONFIG_KEY, mapping=original)
        client.close()


@pytest.mark.unit
def test_feature_coverage_guard(tmp_path, monkeypatch):
    """覆盖率防线：模型特征大部分不在基线 parquet → 拒绝（防垃圾分冒充实时）。"""
    import backend.services.api.routers.admin.realtime as rt

    import pandas as pd

    parquet = tmp_path / "model_features_2026.parquet"
    pd.DataFrame({"symbol": ["600036"], "f1": [0.1], "f2": [0.2]}).to_parquet(parquet)
    monkeypatch.setattr(rt, "BASELINE_PARQUET_TMPL", str(tmp_path / "model_features_{year}.parquet"))

    assert rt.feature_coverage("x", ["f1", "f2"]) == (2, 2, 1.0)
    rt.validate_feature_coverage("x", ["f1", "f2"])  # 全覆盖 → 通过
    with pytest.raises(ValueError) as exc:
        rt.validate_feature_coverage("x", ["f1", "nope1", "nope2", "nope3"])
    assert "覆盖率过低" in str(exc.value)


@pytest.mark.unit
def test_feature_coverage_quantdb_binding(tmp_path, monkeypatch):
    """quantdb_factors 绑定模型：覆盖率按**因子源列**判定（不再拿遗留 parquet 误导判定）。"""
    import backend.services.api.routers.admin.realtime as rt

    model_dir = tmp_path / "mdl_qdb"
    model_dir.mkdir()
    (model_dir / "metadata.json").write_text(
        json.dumps(
            {
                "data_source": "quantdb_factors",
                "factor_source": "l1_l2_factors",
                "quantdb_dir": str(tmp_path / "quantdb"),
                "context": {"market": "CN"},
                "feature_columns": ["f1", "f2"],
            }
        ),
        encoding="utf-8",
    )

    class _FakeReader:
        def __init__(self, *args, **kwargs):
            pass

        def describe(self, source):
            from types import SimpleNamespace

            assert source == "l1_l2_factors"
            return SimpleNamespace(columns=["symbol", "date", "f1", "open"])

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.QuantDBFactorReader",
        _FakeReader,
    )
    hit, total, ratio = rt.feature_coverage(str(model_dir), ["f1", "f2"])
    assert (hit, total) == (1, 2) and ratio == pytest.approx(0.5)
    assert "quantdb_factors" in rt.baseline_source_label(str(model_dir))
    with pytest.raises(ValueError) as exc:
        rt.validate_feature_coverage(str(model_dir), ["f2", "f3", "f4"])
    assert "quantdb_factors" in str(exc.value)  # 文案如实标注取数面


@pytest.mark.integration
def test_infer_config_exposes_governor_and_mirror_age(tmp_path):
    """`GET /infer/config` 必须把治理器快照透出给面板（真 Redis，读写后原样恢复）。

    面板要回答「引擎循环还活着吗 / 近窗 p95 有没有压着节拍预算 / 有没有在降级」，
    这三问的数据源只有引擎进程内的治理器 —— 它经状态镜像到 Redis，admin 端点再透出。
    少了这一层，面板只能显示「已发布 N 条」，看起来就不像一个实时推理服务。
    """
    import os

    import redis as redis_lib

    from backend.services.api.routers.admin.realtime import (
        CONFIG_KEY,
        STATUS_KEY,
        get_infer_config,
    )

    client = redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
    )
    original_status = client.hgetall(STATUS_KEY) or {}
    original_config = client.hgetall(CONFIG_KEY) or {}
    d = tmp_path / "mdl_gov"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"feature_columns": ["mom_ret_1d", "f1"]}), encoding="utf-8"
    )
    try:
        client.hset(
            STATUS_KEY,
            mapping={
                "updated_at": "2026-09-20T14:30:00+08:00",
                "counters": json.dumps({"published": 19, "last_ms": 486.6, "degrade_level": 1}),
                "governor": json.dumps(
                    {
                        "level": 1, "base_cadence_s": 15.0, "effective_cadence_s": 15.0,
                        "degraded": True, "p95_ms": 13000.0, "last_ms": 486.6,
                        "cycles": 119, "degradations": 1, "recoveries": 0,
                        "level_since": 9876.5,
                    }
                ),
            },
        )
        client.hset(CONFIG_KEY, mapping={"enabled": "true", "model_dir": str(d), "cadence_s": "15.0"})

        got = asyncio.run(get_infer_config())

        assert got["success"] is True
        status = got["data"]["status"]
        assert status["updated_at"] == "2026-09-20T14:30:00+08:00"
        assert status["counters"]["published"] == 19
        assert status["governor"]["p95_ms"] == 13000.0, "治理器快照必须透出（面板的时延/降级面）"
        assert status["governor"]["degraded"] is True
    finally:
        client.delete(STATUS_KEY)
        if original_status:
            client.hset(STATUS_KEY, mapping=original_status)
        client.delete(CONFIG_KEY)
        if original_config:
            client.hset(CONFIG_KEY, mapping=original_config)
        client.close()


@pytest.mark.unit
def test_infer_config_tolerates_missing_or_corrupt_governor(monkeypatch):
    """镜像里没有/坏掉的 governor 不能让整个端点 500——面板宁可少一格，不能整卡打不开。"""
    import backend.services.api.routers.admin.realtime as rt

    class _FakeRedis:
        def hgetall(self, key):
            if key == rt.CONFIG_KEY:
                return {"enabled": "false", "model_dir": "", "cadence_s": "15"}
            return {"counters": "{bad json", "governor": "not-json"}

        def close(self):
            pass

    monkeypatch.setattr(rt, "_redis", lambda: _FakeRedis())

    got = asyncio.run(rt.get_infer_config())

    assert got["success"] is True
    assert got["data"]["status"]["counters"] == {}
    assert got["data"]["status"]["governor"] is None
