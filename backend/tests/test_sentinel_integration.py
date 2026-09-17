"""哨兵告警真机链路测试（T-P6-15，I 类）：告警→留痕/推送 → T+1 回填 → 报表 → 标注闭环。

验收口径：
- 真总线事件 → 真消费（sentinel 组）→ sentinel_alerts 留痕 + 管理员**真通知**落库；
- T+1 自动回填（真 QuantDB 前复权行情 + 000300.SH 基准）；无数据样本如实 no_data（不假填）；
- 误报率报表口径可查 + 人工标注 → 报表闭环（标注优先于自动判定）；
- 全程自清理（表行/通知/总线条目）。
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


def test_sentinel_alert_backfill_report_annotation_cycle(monkeypatch):
    import asyncio

    from sqlalchemy import text as sql_text

    import backend.services.trade.services.sentinel_alert_service as svc_mod
    from backend.services.api.routers.sentinel import (
        AnnotateRequest,
        annotate_alert,
        sentinel_report,
    )
    from backend.services.trade.services.sentinel_alert_service import (
        SentinelAlertService,
        SentinelConfig,
    )
    from backend.services.trade.services.sentinel_backfill import backfill_pending
    from backend.shared.sentinel_alert_contract import ensure_sentinel_alerts_table
    from backend.shared.sync_db import sync_session

    assert ensure_sentinel_alerts_table() is True

    tag = uuid.uuid4().hex[:8]
    title = f"[itest-{tag}] 某公司因信息披露违规被立案调查"
    # 隔离消费组名（避免与生产 sentinel 组争抢消息）
    monkeypatch.setattr(svc_mod, "CONSUMER_GROUP", f"sentinel-itest-{tag}")
    monkeypatch.setattr(svc_mod, "CONSUMER_NAME", f"c-{tag}")

    bus = _redis(0)
    created_alert_ids: list[str] = []
    event_id = None
    try:
        # ⓪ 清理推送闸门残留（上轮测试的冷却/配额）+ 预热建组（组从 "$" 起——只消费之后的新事件）
        from datetime import datetime as _dt
        bus.delete("qm:sentinel:pushcd:news:risk_event:600036.SH")
        bus.delete(f"qm:sentinel:pushh:{_dt.now(_CST).strftime('%Y%m%d%H')}")
        service = SentinelAlertService(config_loader=lambda: SentinelConfig(enabled=True))
        service.run_once()

        # ① 真总线事件（fresh ts）
        from backend.shared.intel_events import publish_event

        event_id = publish_event(bus, {
            "ts": datetime.now(timezone.utc).timestamp(),
            "type": "news", "market": "CN", "targets": ["600036.SH"], "level": "critical",
            "payload": {"kind": "risk_event", "title": title, "event_tags": ["立案调查"]},
            "actions_hint": ["risk_review"], "source": "news_intel",
        })

        # ② 真消费（真留痕 + 真通知）
        result = service.run_once()
        assert result["enabled"] is True and result["scanned"] >= 1

        with sync_session() as session:
            row = session.execute(
                sql_text(
                    "SELECT alert_id::text, pushed, push_reason, direction, symbol "
                    "FROM sentinel_alerts WHERE title = :t ORDER BY id DESC LIMIT 1"
                ),
                {"t": title},
            ).fetchone()
            assert row is not None, "总线事件未留痕"
            created_alert_ids.append(row[0])
            assert row[3] == "down" and row[4] == "600036.SH"
            assert row[1] is True and row[2] == "pushed", f"推送未达成: {row}"
            notified = session.execute(
                sql_text(
                    "SELECT count(*) FROM notifications WHERE notification_type = 'sentinel' "
                    "AND title LIKE :t AND created_at > NOW() - INTERVAL '5 minutes'"
                ),
                {"t": f"%{title}%"},
            ).scalar()
        assert int(notified or 0) >= 1, "管理员通知未落库"

        # ③ T+1 回填（真行情）：造两条历史告警（一条可兑现、一条无数据超宽限）
        with sync_session() as session:
            for sym, tdate, note_key in (("600036.SH", "2026-09-10", f"fill-{tag}"),
                                         ("999999.SZ", "2026-09-03", f"nodata-{tag}")):
                session.execute(
                    sql_text(
                        "INSERT INTO sentinel_alerts (dedupe_key, ts, trade_date, market, symbol, "
                        "targets, alert_type, severity, source, title, direction) "
                        "VALUES (:dk, to_timestamp(:ts), :d, 'CN', :s, CAST(:tg AS JSONB), "
                        "        'news:risk_event', 'critical', 'news_intel', :ti, 'down') "
                        "ON CONFLICT (dedupe_key) DO NOTHING"
                    ),
                    {"dk": note_key, "ts": datetime.now(timezone.utc).timestamp(),
                     "d": tdate, "s": sym, "tg": json.dumps([sym]), "ti": f"[itest-{tag}] {sym}"},
                )
            session.commit()
        stats = backfill_pending(limit=100, today=datetime.now(_CST).date())
        assert stats["filled"] >= 1 and stats["no_data"] >= 1
        with sync_session() as session:
            filled = session.execute(
                sql_text(
                    "SELECT realized_return, benchmark_return, excess_return, hit, outcome_status "
                    "FROM sentinel_alerts WHERE dedupe_key = :dk"
                ),
                {"dk": f"fill-{tag}"},
            ).fetchone()
        assert filled is not None and filled[4] == "filled"
        assert filled[0] is not None and filled[2] is not None and filled[3] in (True, False)
        with sync_session() as session:
            nodata = session.execute(
                sql_text("SELECT outcome_status FROM sentinel_alerts WHERE dedupe_key = :dk"),
                {"dk": f"nodata-{tag}"},
            ).scalar()
        assert nodata == "no_data"  # 不假填

        # ④ 报表口径 + ⑤ 人工标注 → 报表闭环（同一事件循环内跑，避免 asyncpg 跨 loop 复用）
        with sync_session() as session:
            target_id = session.execute(
                sql_text("SELECT alert_id::text FROM sentinel_alerts WHERE dedupe_key = :dk"),
                {"dk": f"fill-{tag}"},
            ).scalar()
        created_alert_ids.append(str(target_id))

        async def _report_flow():
            r1 = await sentinel_report(days=30, current_user={"user_id": "10000001"})
            a = await annotate_alert(
                alert_id=str(target_id),
                payload=AnnotateRequest(annotation="false_positive", note=f"itest-{tag}"),
                current_user={"user_id": "10000001"},
            )
            r2 = await sentinel_report(days=30, current_user={"user_id": "10000001"})
            return r1, a, r2

        report, annotated, report2 = asyncio.run(_report_flow())
        overall = report["data"]["overall"]
        assert overall["total"] >= 2 and overall["filled"] >= 1
        assert overall["miss_rate"] is not None
        assert "news:risk_event" in report["data"]["by_type"]
        assert annotated["success"] is True
        assert report2["data"]["overall"]["annotated_false_positive"] >= 1
    finally:
        # 清理：表行 / 通知 / 总线条目
        try:
            with sync_session() as session:
                session.execute(
                    sql_text("DELETE FROM sentinel_alerts WHERE title LIKE :t OR annotation_note = :n"),
                    {"t": f"%[itest-{tag}]%", "n": f"itest-{tag}"},
                )
                session.execute(
                    sql_text(
                        "DELETE FROM sentinel_alerts WHERE dedupe_key IN (:a, :b)"
                    ),
                    {"a": f"fill-{tag}", "b": f"nodata-{tag}"},
                )
                session.execute(
                    sql_text(
                        "DELETE FROM notifications WHERE notification_type='sentinel' "
                        "AND title LIKE :t AND created_at > NOW() - INTERVAL '10 minutes'"
                    ),
                    {"t": f"%{title}%"},
                )
                session.commit()
        except Exception:  # noqa: BLE001
            pass
        if event_id:
            try:
                bus.xdel("intel:events", event_id)
            except Exception:  # noqa: BLE001
                pass
        bus.close()
