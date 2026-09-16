"""T-P6-09 回放复现验收器测试：摘要/编排/U 与端到端 diff=0/篡改检出。

覆盖：
1. U：matrix/scores 摘要确定性与敏感性（round(6) 噪声吸收）；
2. I：合成一日——服务（live 路径）两周期产账本 → 归档帧写入 L0.5 目录 → 回放器
   逐周期 diff=0；**篡改归档一帧 → 不一致被检出**（负例，防假绿）；
3. D：空账本/空归档 → 明确 reason；
4. G：服务/回放共用装配（compute_cycle 唯一）——导出器与装配的模块边界守卫。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

_CST = timezone(timedelta(hours=8))


def _today_ts(hhmmss: str) -> float:
    now = datetime.now(tz=_CST)
    dt = now.replace(hour=int(hhmmss[:2]), minute=int(hhmmss[2:4]), second=int(hhmmss[4:]), microsecond=0)
    return dt.timestamp()


def _snap(price: float, ts: float, *, open_: float = 11.0, high: float | None = None,
          low: float | None = None, volume: float = 100000.0, amount: float = 1_200_000.0) -> dict:
    return {
        "Now": str(price), "Open": str(open_), "High": str(high if high is not None else price),
        "Low": str(low if low is not None else price - 0.5), "Volume": str(volume),
        "Amount": str(amount), "PreClose": "11.0", "timestamp": str(int(ts)),
    }


def _archive_record(sym: str, price: float, ts: float, *, open_: float = 11.0,
                    high: float | None = None, low: float | None = None,
                    volume: float = 100000.0, amount: float = 1_200_000.0) -> dict:
    return {
        "symbol": sym, "ts": int(ts), "price": price, "pre_close": 11.0, "open": open_,
        "high": high if high is not None else price, "low": low if low is not None else price - 0.5,
        "volume": volume, "amount": amount,
    }


# ── 1. 摘要 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_digests_deterministic_and_sensitive():
    from backend.services.engine.inference.realtime_core import matrix_digest, scores_digest

    x = np.arange(12, dtype=np.float32).reshape(3, 4)
    cols = ["a", "b", "c", "d"]
    d1 = matrix_digest(x, cols, "m1")
    assert d1 == matrix_digest(x.copy(), cols, "m1")
    assert d1 != matrix_digest(x + 1e-6, cols, "m1")     # 数值敏感
    assert d1 != matrix_digest(x, cols, "m2")            # 模型版本敏感
    assert d1 != matrix_digest(x, ["a", "b", "c", "e"], "m1")  # 列序/列名敏感

    s = np.array([0.1, 0.2, 0.3])
    assert scores_digest(s) == scores_digest(s + 1e-9)   # round(6) 吸收浮点尾差
    assert scores_digest(s) != scores_digest(s + 1e-3)


@pytest.mark.unit
def test_effective_override_intersection():
    from backend.services.engine.inference.realtime_core import effective_override

    cols = ["mom_ret_1d", "vol_std_20", "not_in_tier"]
    got = effective_override(("mom_ret_1d", "not_in_tier", "ghost"), cols)
    assert got == {"mom_ret_1d"}  # ∩ TIER ∩ 模型列


# ── 2. 端到端：服务账本 → 归档回放 ──────────────────────────────────


def _build_day(tmp_path):
    """跑两周期服务（注入账本），并把同源帧写入 L0.5 目录。返回 (day, model_dir, ledger, l05_base)。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )
    from backend.shared.l05_store import write_records
    from backend.tests.test_realtime_inference import _fake_engine_inputs, _make_model_dir

    model_dir = _make_model_dir(tmp_path)
    hot, snaps1, bundle = _fake_engine_inputs(price=12.0)
    ts1, ts2 = _today_ts("100000"), _today_ts("100015")
    snaps1 = {k: {**v, "timestamp": str(int(ts1))} for k, v in snaps1.items()}
    snaps2 = {
        "600036.SH": _snap(12.4, ts2, high=12.4),
        "000001.SZ": _snap(10.6, ts2, high=10.6),
    }
    ledger: list[dict] = []
    svc = RealtimeInferenceService(
        config_loader=lambda: RealtimeInferConfig(
            enabled=True, model_dir=str(model_dir), cadence_s=3,
            override_whitelist=("mom_ret_1d",),
        ),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: current[0],
        baseline_loader=lambda syms, day: bundle,
        ledger_sink=ledger.append,
    )
    current = [snaps1]
    svc.build_cycle()
    current = [snaps2]
    svc.build_cycle()
    assert len(ledger) == 2 and ledger[0]["cuts"] and ledger[1]["cuts"]

    l05_base = tmp_path / "l05"
    day = datetime.now(tz=_CST).date()
    records = []
    for sym, price in (("600036.SH", 12.0), ("000001.SZ", 10.5)):
        records.append(_archive_record(sym, price, ts1, high=price))
    for sym, price in (("600036.SH", 12.4), ("000001.SZ", 10.6)):
        records.append(_archive_record(sym, price, ts2, high=price))
    write_records(records, base_dir=str(l05_base))
    return day, model_dir, ledger, l05_base, bundle


@pytest.mark.integration
def test_replay_diff_zero_and_tamper_detection(tmp_path):
    from backend.services.engine.inference.replay_verifier import verify_day
    from backend.shared.l05_store import read_day

    day, model_dir, ledger, l05_base, bundle = _build_day(tmp_path)
    frames = read_day(day, base_dir=str(l05_base))
    assert len(frames) == 4

    report = verify_day(
        day=day, model_dir=model_dir, ledger=ledger, frames=frames,
        baseline_bundle=bundle,
    )
    assert report["diff_zero"] is True, report
    assert report["entries"] == 2 and report["matched"] == 2

    # 负例：篡改归档（第二周期 600036 的价格）→ 必须检出不一致（防假绿）
    import pyarrow.parquet as pq

    files = sorted((l05_base / f"date={day.strftime('%Y%m%d')}").glob("part-*.parquet"))
    assert files
    table = pq.read_table(files[0]).to_pydict()
    rows = []
    for i in range(len(table["symbol"])):
        row = {k: table[k][i] for k in table}
        if row["symbol"] == "600036.SH" and int(row["ts"]) == int(ledger[1]["cuts"][0]):
            row["price"] = float(row["price"]) + 0.5
        rows.append(row)
    from backend.shared.l05_store import write_records as _wr

    for f in files:
        f.unlink()
    _wr(rows, base_dir=str(l05_base))
    frames2 = read_day(day, base_dir=str(l05_base))
    report2 = verify_day(
        day=day, model_dir=model_dir, ledger=ledger, frames=frames2,
        baseline_bundle=bundle,
    )
    assert report2["diff_zero"] is False
    assert report2["mismatched"] >= 1
    assert report2["mismatch_details"], "不一致需带诊断明细"


# ── 3. 数据缺失 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_replay_missing_data_reasons(tmp_path):
    from datetime import date as _date

    from backend.services.engine.inference.replay_verifier import verify_day

    report = verify_day(day=_date(2026, 9, 17), model_dir=tmp_path, ledger=[], frames=None)
    assert report["ok"] is False and report["reason"] == "no_ledger"

    report2 = verify_day(
        day=_date(2026, 9, 17), model_dir=tmp_path, ledger=[{"ts": 1}], frames=None
    )
    assert report2["ok"] is False and report2["reason"] == "no_frames"


# ── 4. 结构守卫：装配单源 ───────────────────────────────────────────


@pytest.mark.unit
def test_single_assembly_source_guard():
    """服务与回放器都不得自建矩阵装配循环——必须经 compute_cycle。"""
    backend = Path(__file__).resolve().parents[1]
    service_src = (backend / "services/engine/inference/realtime_service.py").read_text(encoding="utf-8")
    verifier_src = (backend / "services/engine/inference/replay_verifier.py").read_text(encoding="utf-8")
    assert "compute_cycle(" in service_src and "compute_cycle(" in verifier_src
    for src, name in ((service_src, "realtime_service"), (verifier_src, "replay_verifier")):
        assert "np.empty((len(hot)" not in src, f"{name} 自建矩阵循环（应走 compute_cycle）"
