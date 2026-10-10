"""识别引擎真机接线测试（T-P6-14，I 类）：确定性夹具 → 真实动作 → 断言 → 清理。

覆盖验收口径：
- 四类检测（此处用确定性夹具走 账户异常/市场量价 两类 + 契约/动作；检测器正反例见
  test_anomaly_detectors）；
- 动作真实生效：告警走**真 publish_event 路径**落**本次运行私有的隔离流**
  （itest:intel:events:{tag}；生产键 intel:events 会被容器内 sentinel 消费组当
  真告警消费——写 sentinel_alerts + 推管理员，2026-10-10 审计 H5/T7-1）、留痕落
  **真 PG**（qm_market_anomalies + risk_events）、否决写**真 risk lock**（fail-closed 通道）；
- 契约自愈：ensure_anomaly_types 让新 anomaly_type 可写。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

_CST = timezone(timedelta(hours=8))

# 时段闸的夹具时钟：2026-10-08（周四）10:30 —— 固定的交易日，闸必开（真 XSHG 答 True；
# 印发期外答不了时按降级口径也放行，见 anomaly_engine.in_market_session）。
_SESSION_EPOCH = datetime(2026, 10, 8, 10, 30, tzinfo=_CST).timestamp()


def _session_now() -> float:
    """测试时钟钉在**固定的交易日 10:30**（2026-10-08，周四）。

    不能钉「今天 10:30」：时段闸还要求工作日 + 交易日历（anomaly_engine.
    market_session_state），周末/假期跑会因闸关而假红（2026-10-08 评审实测 Sat
    2026-10-10 三条用例红；当时的 autouse 跳过只是把红换成静默不覆盖）。固定日期让
    「闸开」成为夹具前提，与真实今天是星期几无关。

    注意时钟只钉**判定面**（时段闸、节流、冷却）：锁/审计/落表的 trade_date 仍取墙钟
    （引擎 _default_deny/_audit/recorder 用的是 datetime.now），所以下面 trade_date
    断言照旧取墙钟日期。
    """
    return _SESSION_EPOCH


def _no_status_write(payload) -> None:
    """**必须注入**：默认 status_writer 写的是生产状态镜像 `qm:anomaly:status`
    （``_default_status_write`` → hset），测试引擎跑一轮就会把它覆盖成测试计数。

    2026-10-08 实测：跑完本文件后读到 `cycles=2 / skipped_market_closed=0 /
    detections=1`，而真身实例当时是 `cycles=10 / skipped_market_closed=10`——
    运维据此以为引擎重启过或时段闸失效。真身下一轮会盖回来（≤1 分钟自愈），
    但那一分钟里的运维判断是错的，所以测试不许碰这个键。"""
    return None


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
        # ⚠️ 必须用**虚构代码**：data_jump 是 critical，deny 走 _symbol_holders → 会给
        # 持有该代码的**真实模拟账户**写标的锁（TTL 到当日 23:59+4h）。原先写真实代码
        # 600036.SH，实测有 3 个真实账户持仓 ⇒ 每跑一次这个测试就锁掉它们一天的买入。
        return [{"symbol": symbol,
                 "latest": {"date": "2026-09-14", "close": 20.0, "volume": 1000,
                            "limit_up": 12.1, "limit_down": 9.9},  # fidelity: allow-limit-threshold — 非阈值：跌停价夹具（供包络判定）
                 "prev": {"date": "2026-09-11", "close": 11.0},
                 "expected_prev_date": "2026-09-11"}]

    # 模型 id 用**生产长度**（60~68 字符，/app/models 实测最长 68）：12 字符的
    # itest-xxxx 塞得进 instrument varchar(16)，会让下面「模型告警必须落表」的断言
    # 假绿——2026-10-08 实测该表 2829 行全是 price_surge、model_ic_drop 0 行，
    # 缺陷正是被短夹具盖住的。
    model_id = f"mdl_it_train_20261008010203_{tag}abcd_ef{tag}12_catboost_9c8d7e6f"
    assert len(model_id) > 16, "夹具必须长于 instrument 列宽"

    def model_fetcher(cfg):
        return [{"model_id": model_id,
                 "ic_stats": {"ic_5": -0.05, "ic_20": 0.03, "n_5": 6, "n_20": 20}}]

    # 隔离总线键（T7-1，审计 H5）：发布走真 publish_event，但写给**本次运行私有**
    # 的流；生产的 sentinel 消费组只读 intel:events，不会把夹具消费成真告警。
    bus_key = f"itest:intel:events:{tag}"

    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True, volume_ratio_min=3.0,
                                            cancel_ratio_min=0.6, min_orders=5,
                                            reduce_enabled=True),
        market_fetcher=market_fetcher,
        account_fetcher=account_fetcher,
        data_fetcher=data_fetcher,
        model_fetcher=model_fetcher,
        bus_key=bus_key,
        status_writer=_no_status_write,
        now_fn=_session_now,
    )

    bus = _redis(0)      # intel 总线在通用库
    trade = _redis(2)    # risk lock 在交易库（trade_shared.redis_client db=2）
    trade_date = datetime.now(_CST).date().isoformat()
    account_lock_key = f"risk:lock:account:default:{user_id}:{trade_date}"

    try:
        trade.delete(account_lock_key)  # 防御性清理
        result = engine.build_once()
        assert result["enabled"] is True and result["detections"] >= 2

        # ① 告警 → 真发布路径 → 本次运行的隔离流（T7-1：不落生产 intel:events）
        events = bus.xrevrange(bus_key, count=60)
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
        # 模型级告警也必须真上总线：旧实现把 68 字符模型 id 塞进证券级 targets，
        # publish_event 抛 IntelEventError 被兜成 WARNING ⇒ 该告警一条都没上过总线。
        model_evs = [e for e in mine if (e.get("payload") or {}).get("subject") == model_id]
        assert model_evs, "本次模型的 IC 告警未上总线"
        assert model_evs[0].get("targets") == [], "模型 id 不得进证券级 targets"
        assert model_evs[0]["payload"]["title"].endswith("IC 异常")

        # ② 留痕 → 真 PG（qm_market_anomalies）
        with SessionLocal() as session:
            # 按**本次夹具 subject** 读：原先按「近 5 分钟」读，会被同表里别的运行
            # 残留的行污染（2026-10-08 实测：断言拿到上一轮另一 tag 的 symbol）。
            rows = session.execute(
                sql_text(
                    "SELECT anomaly_type, instrument, details->>'subject' FROM qm_market_anomalies "
                    "WHERE details->>'source' = 'anomaly_engine' "
                    "AND details->>'subject' IN (:sym, :uid, :mid)"
                ),
                {"sym": symbol, "uid": user_id, "mid": model_id},
            ).fetchall()
        types = {r[0] for r in rows}
        # 四类检测（量价/账户/数据/模型）全部真机落表
        assert {"volume_surge", "account_cancel_ratio", "data_jump", "model_ic_drop"} <= types
        # instrument 只承载证券代码；模型/账户族的 subject 落 details（机器可读）
        by_type = {r[0]: r for r in rows}
        assert by_type["volume_surge"][1] == symbol, "symbol 族的 instrument 不得被本次改动动摇"
        assert by_type["model_ic_drop"][1] is None, "模型 id 不得写进 instrument varchar(16)"
        assert by_type["model_ic_drop"][2] == model_id, "完整模型 id 必须可取回"
        assert by_type["account_cancel_ratio"][1] is None

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
        # 清理：账户锁 + 去重冷却键 + 审计行 + 隔离流（生产总线是追加流，按 MAXLEN
        # 自然淘汰，不动；隔离流是测试私产，整键删）
        try:
            trade.delete(account_lock_key)
            # 去重冷却键（qm:anomaly:last_fired:*，TTL 30min）按夹具 subject 清掉：
            # 留着会在冷却窗内压掉同 (kind,subject,severity) 的后续告警
            for key in bus.scan_iter(match="qm:anomaly:last_fired:*", count=500):
                if any(s in str(key) for s in (symbol, user_id, model_id)):
                    bus.delete(key)
            # 异动源集合同样按自造 subject 清（TTL 1h，留着会喂给热集构建器的"异动源"）
            bus.srem("qm:anomaly:recent_symbols", symbol)
            bus.delete(bus_key)
            bus.close()
            trade.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            with SessionLocal() as session:
                # 只删**本次自造**的行（按夹具 subject 钉死）：原先按「近 5 分钟」
                # 清场会顺手删掉真实引擎这 5 分钟里落的行——测试不许删生产行。
                session.execute(
                    sql_text(
                        "DELETE FROM risk_events WHERE rule_type LIKE 'anomaly_engine:%' "
                        "AND symbol IN (:sym, :uid, :mid)"
                    ),
                    {"sym": symbol, "uid": user_id, "mid": model_id[:32]},
                )
                session.execute(
                    sql_text(
                        "DELETE FROM qm_market_anomalies "
                        "WHERE details->>'source' = 'anomaly_engine' "
                        "AND details->>'subject' IN (:sym, :uid, :mid)"
                    ),
                    {"sym": symbol, "uid": user_id, "mid": model_id},
                )
                # T7-1：清理段覆盖 sentinel_alerts——隔离后夹具事件不再被生产消费组
                # 消费（不该有新行），这段兜住隔离前的历史窗口与未来回归（按本次夹具
                # subject 钉死，绝不碰生产引擎的真告警行）。
                session.execute(
                    sql_text(
                        "DELETE FROM sentinel_alerts "
                        "WHERE source = 'anomaly_engine' "
                        "AND (detail->'payload'->>'subject' IN (:sym, :uid, :mid) "
                        "     OR symbol = :sym)"
                    ),
                    {"sym": symbol, "uid": user_id, "mid": model_id},
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
        status_writer=_no_status_write,
        now_fn=_session_now,
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
        status_writer=_no_status_write,
        now_fn=_session_now,
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


def test_anomaly_dedup_model_kind_uses_daily_cooldown():
    """模型类异动走专属冷却（真 Redis 钉 TTL）：IC 按日推进，扫描间隔（3600s）
    大于全局冷却（1800s）时旧行为每小时刷屏；模型键的 TTL 必须用 model_cooldown_s。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    model_id = f"mdl_t_{uuid.uuid4().hex[:6]}"
    fired: list = []
    cfg = AnomalyConfig(
        enabled=True, cooldown_s=600, model_cooldown_s=1234, model_every_s=0.0
    )
    engine = AnomalyEngine(
        config_loader=lambda: cfg,
        market_fetcher=lambda c: {},
        account_fetcher=lambda c: [],
        data_fetcher=lambda c: [],
        model_fetcher=lambda c: [
            {
                "model_id": model_id,
                "ic_stats": {"ic_5": -0.05, "ic_20": 0.05, "n_5": 5, "n_20": 20},
            }
        ],
        publisher=lambda d: fired.append(d),
        recorder=lambda d: None,
        denier=lambda d: {},
        status_writer=_no_status_write,
        now_fn=_session_now,
    )
    bus = _redis(0)
    key = f"qm:anomaly:last_fired:model_ic_drop:{model_id}:critical"
    try:
        engine.build_once()
        assert len(fired) == 1, "首轮应触发"
        engine.build_once()
        assert len(fired) == 1, "模型冷却窗内第二次不应再触发"
        assert engine.counters["deduped"] >= 1
        ttl = bus.ttl(key)
        assert 1200 < ttl <= 1234, f"TTL 应用 model_cooldown_s(1234) 而非全局 600：{ttl}"
    finally:
        bus.delete(key)
        bus.close()
