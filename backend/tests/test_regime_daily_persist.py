"""P3 Regime 日表（§6.2）：DDL 三份镜像不漂移 + 冻结写入语义 + 诚实拒绝。

- 单元：阈值指纹稳定/可区分；行校验（状态白名单、日期必填）；表外市场诚实拒绝；
- 契约：``regime_daily_store._DDL_STATEMENTS`` ≡ ``db_init.sql`` ≡ ``data/upgrade_v1.1.5.sql``
  （逐字镜像，漂移即红——新库/老库/自愈三条路必须同一张表）；
- 集成（真库）：冻结写入——重跑 0 新行；迟到修订**不覆盖**已写行（computed_at 留痕）；
  CHECK 约束在 SQL 层真实存在（绕过 Python 校验直插坏行必须被拒）。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from backend.shared.market_regime import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WINDOW,
    thresholds_fingerprint,
)
from backend.shared.regime_daily_store import (
    _DDL_STATEMENTS,
    _validate_row,
    insert_frozen_rows,
)

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent

#: 集成测试用（远离真实数据的日期段，用完即删）
_T_LO, _T_HI = "2090-01-01", "2090-12-31"
_T_LO_D, _T_HI_D = date.fromisoformat(_T_LO), date.fromisoformat(_T_HI)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


# ── 契约：三份 DDL 镜像 ──────────────────────────────────────────────


def test_store_ddl_mirrored_in_db_init():
    """老库自愈用的每条语句都必须能在 db_init.sql 里原样找到（否则新库缺东西）。"""
    haystack = _norm((_BACKEND / "shared" / "db_init.sql").read_text(encoding="utf-8"))
    missing = [s for s in _DDL_STATEMENTS if _norm(s) not in haystack]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）:\n{missing}"


def _find_upgrade_script(name: str) -> Path | None:
    """与 main_oss._upgrade_sql_files 同序定位（容器里 data/ 挂在 /data 而非 /app/data）。"""
    import os

    candidates = [
        os.getenv("QM_UPGRADE_SQL_DIR", ""),
        os.getenv("QM_DATA_DIR", ""),
        "/data",
        "/app/data",
        str(_REPO / "data"),
    ]
    for directory in candidates:
        if directory and (Path(directory) / name).exists():
            return Path(directory) / name
    return None


def test_store_ddl_mirrored_in_upgrade_script():
    """受控升级链的 v1.1.5 脚本必须是同一张表（幂等 CREATE TABLE IF NOT EXISTS）。"""
    upgrade = _find_upgrade_script("upgrade_v1.1.5.sql")
    assert upgrade is not None, "upgrade_v1.1.5.sql 缺失（受控升级链载体，三种布局都不命中）"
    haystack = _norm(upgrade.read_text(encoding="utf-8"))
    missing = [s for s in _DDL_STATEMENTS if _norm(s) not in haystack]
    assert not missing, f"升级脚本缺少以下语句:\n{missing}"


def test_ddl_state_vocabulary_matches_canonical():
    """CHECK 白名单必须与 market_regime.STATES 同集（改词表两侧一起改）。"""
    from backend.shared.market_regime import STATES

    ddl = _norm(_DDL_STATEMENTS[0])
    clause = re.search(r"check \(state in \(([^)]*)\)\)", ddl)
    assert clause, "DDL 里找不到 state CHECK 子句"
    values = {v.strip().strip("'") for v in clause.group(1).split(",")}
    assert values == set(STATES)


# ── 单元：指纹与行校验 ──────────────────────────────────────────────


def test_thresholds_fingerprint_stable_and_sensitive():
    base = thresholds_fingerprint()
    assert base == thresholds_fingerprint(DEFAULT_WINDOW, dict(DEFAULT_THRESHOLDS))
    assert len(base) == 64  # sha256 hex
    changed = dict(DEFAULT_THRESHOLDS)
    changed["ret_up"] = 0.03
    assert thresholds_fingerprint(DEFAULT_WINDOW, changed) != base
    assert thresholds_fingerprint(DEFAULT_WINDOW + 1, dict(DEFAULT_THRESHOLDS)) != base


def test_validate_row_rejects_bad_state_and_missing_date():
    ok = _validate_row("CN", {"effective_date": "2026-01-05", "state": "bull",
                              "ret_window": 0.03, "vol_window": 0.01, "volume_ratio": None})
    assert ok["trade_date"] == date(2026, 1, 5) and ok["state"] == "bull"
    with pytest.raises(ValueError, match="状态非法"):
        _validate_row("CN", {"effective_date": "2026-01-05", "state": "sideways"})
    with pytest.raises(ValueError, match="缺有效日期"):
        _validate_row("CN", {"state": "bull"})


def test_validate_row_normalizes_nonfinite_numbers():
    row = _validate_row("HK", {"effective_date": "2026-01-05", "state": "neutral",
                               "ret_window": float("nan"), "vol_window": float("inf")})
    assert row["ret_window"] is None and row["vol_window"] is None


@pytest.mark.asyncio
async def test_persist_market_regime_rejects_unknown_market_without_io():
    """表外市场（CRYPTO/FUTURES/笔误）诚实拒绝，且不做任何取数。"""
    from backend.services.engine.regime_persist import persist_market_regime

    summary = await persist_market_regime("CRYPTO")
    assert summary["supported"] is False
    assert "no_regime_index_vocabulary" in summary["reason"]
    assert summary["index"] is None

    typo = await persist_market_regime("XSHG")
    assert typo["supported"] is False


# ── 单元：尾行（末根 bar → 下一交易日；日历答不了就不写）────────────────


def _synth_history(n: int = 60) -> dict[str, list]:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(11)
    rets = rng.normal(0.001, 0.012, n)
    closes = list(np.round(4000 * np.cumprod(1 + rets), 2))
    volumes = list(np.round(rng.uniform(0.8, 1.4, n) * 1e8, 0))
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end="2026-09-30", periods=n)]
    return {"dates": dates, "closes": closes, "volumes": volumes}


def _wire_persist(monkeypatch, history: dict, answer):
    """替身：取数/日历/写库全断网——只观察 persist 交给 insert 的行。"""
    from backend.services.engine import regime_persist as rp
    from backend.shared import regime_daily_store, trading_calendar

    monkeypatch.setattr(rp, "load_index_history", lambda m, s: history)
    monkeypatch.setattr(trading_calendar, "next_trading_day_xcal", answer)
    captured: dict = {}

    async def _fake_insert(market, rows, thresholds_hash=None):
        captured.update(market=market, rows=rows)
        return {"inserted": len(rows), "existed": 0, "total": len(rows)}

    monkeypatch.setattr(regime_daily_store, "insert_frozen_rows", _fake_insert)
    return rp, captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_persist_tail_row_labels_last_bar_to_next_trading_day(monkeypatch):
    from backend.shared.market_regime import (
        DEFAULT_WINDOW,
        _roll_ret,
        _roll_std,
        _roll_vratio,
        classify_regime,
    )

    history = _synth_history()
    rp, captured = _wire_persist(monkeypatch, history, lambda m, after: date(2026, 10, 9))

    summary = await rp.persist_market_regime("CN")
    assert summary["tail_effective_date"] == "2026-10-09"
    rows = captured["rows"]
    assert len(rows) == 10  # 尾窗 TAIL_EFFECTIVE_DATES，尾行在内
    assert rows[-1]["effective_date"] == "2026-10-09"
    assert rows[-2]["effective_date"] == history["dates"][-1]  # 前置行仍是自然配对
    i = len(history["closes"]) - 1  # 末根 bar
    assert rows[-1]["state"] == classify_regime(
        _roll_ret(history["closes"], i, DEFAULT_WINDOW),
        _roll_std(history["closes"], i, DEFAULT_WINDOW),
        _roll_vratio(history["volumes"], i, DEFAULT_WINDOW),
    )

    # 回填区间语义：区间覆盖下一交易日 → 尾行在内；区间止于末根 bar → 不含尾行
    captured.clear()
    await rp.persist_market_regime("CN", backfill_from="2026-01-01", backfill_to="2026-10-09")
    assert captured["rows"][-1]["effective_date"] == "2026-10-09"
    captured.clear()
    await rp.persist_market_regime("CN", backfill_from="2026-01-01", backfill_to="2026-09-30")
    assert captured["rows"][-1]["effective_date"] == history["dates"][-1]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("answer_kind", ["none", "raise"])
async def test_persist_skips_tail_row_when_calendar_cannot_answer(
    monkeypatch, caplog, answer_kind
):
    """日历答不了（None/异常）→ 少写一行 + 告警，绝不按自然日/工作日猜。"""
    import logging

    history = _synth_history()

    def _answer(m, after):
        if answer_kind == "raise":
            raise RuntimeError("xcal out of range")
        return None

    rp, captured = _wire_persist(monkeypatch, history, _answer)
    with caplog.at_level(logging.WARNING):
        summary = await rp.persist_market_regime("CN")
    assert summary["tail_effective_date"] is None
    assert captured["rows"][-1]["effective_date"] == history["dates"][-1]  # 仍到自然配对为止
    assert any("尾行" in r.getMessage() for r in caplog.records)


# ── 集成（真库）：冻结语义 ───────────────────────────────────────────


def _rows(dates_states: list[tuple[str, str]]) -> list[dict]:
    return [
        {"effective_date": d, "state": s, "ret_window": 0.01, "vol_window": 0.02,
         "volume_ratio": 1.0}
        for d, s in dates_states
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_frozen_rows_idempotent_and_never_overwrites():
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    first = [("2090-01-03", "bull"), ("2090-01-04", "neutral")]

    async def _cleanup():
        async with get_session() as session:
            await session.execute(
                text(
                    "DELETE FROM qm_regime_daily WHERE market = 'CN' "
                    "AND trade_date BETWEEN :lo AND :hi"
                ),
                {"lo": _T_LO_D, "hi": _T_HI_D},
            )

    try:
        await _cleanup()
        res1 = await insert_frozen_rows("CN", _rows(first), thresholds_hash="fp-1")
        assert res1 == {"inserted": 2, "existed": 0, "total": 2}

        # 重跑同一批：0 新行
        res2 = await insert_frozen_rows("CN", _rows(first), thresholds_hash="fp-1")
        assert res2 == {"inserted": 0, "existed": 2, "total": 2}

        # 迟到修订（同日期不同状态/指纹）：不覆盖，computed_at 也不动
        revised = [("2090-01-03", "bear"), ("2090-01-04", "neutral")]
        res3 = await insert_frozen_rows("CN", _rows(revised), thresholds_hash="fp-2")
        assert res3 == {"inserted": 0, "existed": 2, "total": 2}

        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, thresholds_hash FROM qm_regime_daily "
                        "WHERE market = 'CN' AND trade_date = '2090-01-03'"
                    )
                )
            ).mappings().one()
        assert row["state"] == "bull"  # 原值保留
        assert row["thresholds_hash"] == "fp-1"  # 原始口径留痕
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_check_constraint_rejects_bad_state():
    """CHECK 在 SQL 层真实存在：绕过 Python 校验直插坏行必须被拒。"""
    import sqlalchemy
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session() as session:
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO qm_regime_daily (market, trade_date, state) "
                        "VALUES ('CN', '2090-06-01', 'sideways')"
                    )
                )
    finally:
        await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_states_roundtrip_and_range():
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.regime_daily_store import load_states

    from sqlalchemy import text

    async def _cleanup():
        async with get_session() as session:
            await session.execute(
                text(
                    "DELETE FROM qm_regime_daily WHERE market = 'HK' "
                    "AND trade_date BETWEEN :lo AND :hi"
                ),
                {"lo": _T_LO_D, "hi": _T_HI_D},
            )

    try:
        await _cleanup()
        await insert_frozen_rows(
            "HK",
            _rows([("2090-02-01", "bear"), ("2090-02-02", "neutral"), ("2090-02-03", "bull")]),
            thresholds_hash="fp-1",
        )
        got = await load_states("HK", since="2090-02-02")
        assert got == {"2090-02-02": "neutral", "2090-02-03": "bull"}
        got_all = await load_states("HK", since=_T_LO, until=_T_HI)
        assert list(got_all) == ["2090-02-01", "2090-02-02", "2090-02-03"]
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()
