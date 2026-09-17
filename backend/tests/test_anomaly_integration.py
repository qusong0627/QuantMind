"""识别引擎真机接线测试（T-P6-14，I 类）：确定性夹具 → 真实动作 → 断言 → 清理。

覆盖验收口径：
- 四类检测（此处用确定性夹具走 账户异常/市场量价 两类 + 契约/动作；检测器正反例见
  test_anomaly_detectors）；
- 动作真实生效：告警落**真 intel 总线**（intel:events）、留痕落**真 PG**
  （qm_market_anomalies + risk_events）、否决写**真 risk lock**（fail-closed 通道）；
- 契约自愈：ensure_anomaly_types 让新 anomaly_type 可写。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

_CST = timezone(timedelta(hours=8))


def _redis(db: int = 0):
    import os

    import redis as _r

    return _r.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def test_anomaly_engine_real_actions_end_to_end():
    from sqlalchemy import text as sql_text

    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine
    from backend.shared.anomaly_contract import ensure_anomaly_types
    from backend.shared.sync_db import sync_session as SessionLocal

    # 契约自愈（新 anomaly_type 可写）
    assert ensure_anomaly_types() is True

    tag = uuid.uuid4().hex[:6]
    user_id = f"9000{tag[:4]}"
    symbol = f"T{tag[:5]}".upper()

    def market_fetcher(cfg):
        return {symbol: {"price": 10.0, "pct_chg": 0.01, "now_volume": 500_000.0,
                         "avg_daily_volume": 1_000.0}}  # 巨量 → critical

    def account_fetcher(cfg):
        return [{"user_id": user_id,
                 "orders": [{"status": "cancelled"}] * 9 + [{"status": "filled"}],
                 "positions": []}]

    def data_fetcher(cfg):
        return [{"symbol": "600036.SH",
                 "latest": {"date": "2026-09-14", "close": 20.0, "volume": 1000,
                            "limit_up": 12.1, "limit_down": 9.9},
                 "prev": {"date": "2026-09-11", "close": 11.0},
                 "expected_prev_date": "2026-09-11"}]

    def model_fetcher(cfg):
        return [{"model_id": f"itest-{tag}",
                 "ic_stats": {"ic_5": -0.05, "ic_20": 0.03, "n_5": 6, "n_20": 20}}]

    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True, volume_ratio_min=3.0,
                                            cancel_ratio_min=0.6, min_orders=5,
                                            reduce_enabled=True),
        market_fetcher=market_fetcher,
        account_fetcher=account_fetcher,
        data_fetcher=data_fetcher,
        model_fetcher=model_fetcher,
    )

    bus = _redis(0)      # intel 总线在通用库
    trade = _redis(2)    # risk lock 在交易库（trade_shared.redis_client db=2）
    trade_date = datetime.now(_CST).date().isoformat()
    account_lock_key = f"risk:lock:account:default:{user_id}:{trade_date}"

    try:
        trade.delete(account_lock_key)  # 防御性清理
        result = engine.build_once()
        assert result["enabled"] is True and result["detections"] >= 2

        # ① 告警 → 真 intel 总线（intel:events）
        events = bus.xrevrange("intel:events", count=60)
        mine = []
        for _eid, fields in events:
            try:
                payload = json.loads(fields.get("data") or "{}")
            except (TypeError, ValueError):
                continue
            ev = payload.get("event") if isinstance(payload.get("event"), dict) else payload
            if str(ev.get("source") or "") == "anomaly_engine":
                mine.append(ev)
        assert mine, "识别引擎事件未落总线"
        kinds = {str((e.get("payload") or {}).get("kind")) for e in mine}
        assert "volume_surge" in kinds

        # ② 留痕 → 真 PG（qm_market_anomalies）
        with SessionLocal() as session:
            rows = session.execute(
                sql_text(
                    "SELECT anomaly_type FROM qm_market_anomalies "
                    "WHERE details->>'source' = 'anomaly_engine' "
                    "AND created_at > NOW() - INTERVAL '5 minutes'"
                )
            ).fetchall()
        types = {r[0] for r in rows}
        # 四类检测（量价/账户/数据/模型）全部真机落表
        assert {"volume_surge", "account_cancel_ratio", "data_jump", "model_ic_drop"} <= types

        # ③ 否决 → 真 risk lock（账户锁；fail-closed 通道）+ risk_events 审计
        assert trade.get(account_lock_key) is not None, "账户锁未写入（fail-closed 通道断裂）"
        with SessionLocal() as session:
            audits = session.execute(
                sql_text(
                    "SELECT action, status FROM risk_events "
                    "WHERE rule_type LIKE 'anomaly_engine:%' "
                    "AND created_at > NOW() - INTERVAL '5 minutes'"
                )
            ).fetchall()
        actions = {r[0] for r in audits}
        assert "deny" in actions and "reduce_suggested" in actions
    finally:
        # 清理：账户锁 + 审计行（总线事件为追加流，按 MAXLEN 自然淘汰，不动）
        try:
            trade.delete(account_lock_key)
            bus.close()
            trade.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            with SessionLocal() as session:
                session.execute(
                    sql_text(
                        "DELETE FROM risk_events WHERE rule_type LIKE 'anomaly_engine:%' "
                        "AND created_at > NOW() - INTERVAL '5 minutes'"
                    )
                )
                session.execute(
                    sql_text(
                        "DELETE FROM qm_market_anomalies "
                        "WHERE details->>'source' = 'anomaly_engine' "
                        "AND created_at > NOW() - INTERVAL '5 minutes'"
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            pass


def test_anomaly_recent_symbols_feed_hot_set_source():
    """异动源接线：引擎标记的近期异动标的可被 hot_set_builder 的异动池读到。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine
    from backend.shared.anomaly_contract import read_recent_anomaly_symbols

    symbol = f"T{uuid.uuid4().hex[:5]}".upper()
    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True),
        market_fetcher=lambda cfg: {symbol: {"price": 10.0, "pct_chg": 0.06}},
        account_fetcher=lambda cfg: [],
        data_fetcher=lambda cfg: [],
        model_fetcher=lambda cfg: [],
        publisher=lambda d: None,
        recorder=lambda d: None,
        denier=lambda d: {},
    )
    bus = _redis(0)
    try:
        engine.build_once()
        assert symbol in read_recent_anomaly_symbols(limit=200)
    finally:
        from backend.shared.anomaly_contract import RECENT_SYMBOLS_KEY

        bus.srem(RECENT_SYMBOLS_KEY, symbol)
        bus.close()


def test_anomaly_dedup_suppresses_repeat_within_cooldown():
    """去重冷却（真 Redis）：同 (kind,subject,severity) 窗口内第二次不再触发告警。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    symbol = f"T{uuid.uuid4().hex[:5]}".upper()
    fired = []
    cfg = AnomalyConfig(enabled=True, volume_ratio_min=3.0, cooldown_s=600)
    engine = AnomalyEngine(
        config_loader=lambda: cfg,
        market_fetcher=lambda c: {symbol: {"price": 10.0, "pct_chg": 0.01,
                                           "now_volume": 500_000.0, "avg_daily_volume": 1_000.0}},
        account_fetcher=lambda c: [],
        data_fetcher=lambda c: [],
        model_fetcher=lambda c: [],
        publisher=lambda d: fired.append(d),
        recorder=lambda d: None,
        denier=lambda d: {},
    )
    bus = _redis(0)
    try:
        engine.build_once()
        assert len(fired) == 1, "首轮应触发"
        engine.build_once()
        assert len(fired) == 1, "冷却窗内第二次不应再触发"
        assert engine.counters["deduped"] >= 1
    finally:
        bus.delete(f"qm:anomaly:last_fired:volume_surge:{symbol}:critical")
        bus.srem("qm:anomaly:recent_symbols", symbol)
        bus.close()
