"""新闻情报真机链路测试（T-P6-12，I 类）：造新闻 → enrichment → 总线事件 → veto 拦截留痕。

验收口径：
- 造 enrichment 行（真 PG）→ 真服务增量扫 → **真 intel 总线**事件（schema 有效）；
- 风险事件 → **真 veto 标记**（交易库）；
- 策略配置 ``risk.veto.news_event=true`` → 模拟引擎 ``_apply_news_veto`` **真拦截买单**
  且写 ``risk_events`` 留痕（rule_type=news_event_veto）；
- 去重：同 title_hash 第二轮不再发布。
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration
_CST = timezone(timedelta(hours=8))


def _redis(db: int):
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


class _Order:
    def __init__(self, side: str, symbol: str):
        self.side, self.symbol = side, symbol


def test_news_intel_end_to_end_with_veto_interception(monkeypatch):
    from sqlalchemy import text as sql_text

    import backend.services.engine.news_intel_engine as mod
    from backend.services.engine.news_intel_engine import NewsIntelConfig, NewsIntelEngine
    from backend.services.live_trading.services import news_veto
    from backend.shared.sync_db import sync_session

    tag = uuid.uuid4().hex[:8]
    title_hash = int(tag, 16)
    symbol = "600036.SH"
    page_id = 990000 + int(tag[:4], 16) % 9000
    title = f"[itest-{tag}] 某银行因违规被立案调查"

    # 0) 隔离游标/去重键（不污染生产游标）
    monkeypatch.setattr(mod, "CURSOR_KEY", f"qm:news:intel:cursor:itest-{tag}")
    monkeypatch.setattr(mod, "SEEN_KEY", f"qm:news:intel:seen:itest-{tag}")

    cursor_client = _redis(0)
    cursor_client.delete(f"qm:news:intel:neg:{symbol}", f"qm:news:intel:spike:{symbol}")
    cursor_client.set(
        f"qm:news:intel:cursor:itest-{tag}",
        (datetime.now(_CST) - timedelta(seconds=1)).isoformat(),
        ex=600,
    )
    cursor_client.close()

    with sync_session() as session:
        session.execute(
            sql_text(
                "INSERT INTO news_article_enrichment "
                "(huntly_page_id, tickers, industries, event_tags, sentiment_score, "
                " sentiment_label, title, title_hash, enriched_at, model_version) "
                "VALUES (:p, :tk, :ind, :tags, :sc, :lb, :ti, :th, NOW(), 'itest')"
            ),
            {"p": page_id, "tk": [symbol], "ind": ["银行"], "tags": ["立案调查"],
             "sc": -0.9, "lb": "bearish", "ti": title, "th": title_hash},
        )
        session.execute(
            sql_text(
                "INSERT INTO strategies (user_id, name, config, status) "
                "VALUES (:u, :n, CAST(:c AS JSONB), 'draft')"
            ),
            {"u": 1, "n": f"itest-news-veto-{tag}",
             "c": '{"risk": {"veto": {"news_event": true}}}'},
        )
        session.commit()
        sid = session.execute(
            sql_text("SELECT id FROM strategies WHERE name = :n"), {"n": f"itest-news-veto-{tag}"}
        ).scalar()

    # 1) 真服务增量扫（真 PG 取数 / 真总线 / 真 veto 标记）
    engine = NewsIntelEngine(config_loader=lambda: NewsIntelConfig(enabled=True))
    trade = _redis(2)
    trade_date = datetime.now(_CST).date().isoformat()
    veto_key = f"risk:veto:news:{trade_date}:{symbol}"
    try:
        trade.delete(veto_key)
        result = engine.build_once()
        assert result["enabled"] is True and result["published"] >= 1

        # 2) 总线事件（读真 stream，按 payload.title 定位）
        bus = _redis(0)
        found = None
        for _eid, fields in bus.xrevrange("intel:events", count=200):
            try:
                ev = json.loads(fields.get("data") or "{}")
            except (TypeError, ValueError):
                continue
            payload = ev.get("payload") or {}
            if (ev.get("source") == "news_intel" and payload.get("title") == title
                    and payload.get("kind") == "risk_event"):
                found = ev
                break
        bus.close()
        assert found is not None, "风险新闻未落总线"
        assert found["level"] == "critical" and found["type"] == "news"
        assert symbol in found["targets"]

        # 3) veto 标记（交易库）
        assert trade.get(veto_key) is not None, "风险事件未写 veto 标记"

        # 4) 真拦截：策略开启 veto → 买单被拦 + 留痕
        shim = types.SimpleNamespace(redis=trade)
        from backend.services.simulation.engine import SimulationEngine

        orders = [_Order("BUY", symbol), _Order("SELL", symbol), _Order("BUY", "000001.SZ")]
        kept = asyncio.run(
            SimulationEngine._apply_news_veto(
                shim, orders, tenant="default", user_id="1", strategy_id=str(sid)
            )
        )
        kept_symbols = [(o.side, o.symbol) for o in kept]
        assert ("BUY", symbol) not in kept_symbols, "veto 未拦截买单（fail）"
        assert ("SELL", symbol) in kept_symbols and ("BUY", "000001.SZ") in kept_symbols

        with sync_session() as session:
            rows = session.execute(
                sql_text(
                    "SELECT symbol, action, status FROM risk_events "
                    "WHERE rule_type = 'news_event_veto' AND symbol = :s "
                    "AND created_at > NOW() - INTERVAL '5 minutes'"
                ),
                {"s": symbol},
            ).fetchall()
        assert rows and rows[0][1] == "deny", "拦截未留痕（risk_events）"

        # 5) 去重：游标回拨 2s（模拟重启/重叠读，行会再次进入窗口）→ 同 title_hash 不再发布
        back = _redis(0)
        back.set(f"qm:news:intel:cursor:itest-{tag}",
                 (datetime.now(_CST) - timedelta(seconds=2)).isoformat(), ex=600)
        back.close()
        engine2 = NewsIntelEngine(config_loader=lambda: NewsIntelConfig(enabled=True))
        result2 = engine2.build_once()
        assert result2["published"] == 0 and engine2.counters["skipped_dup"] >= 1
    finally:
        # 清理：enrichment 行 / 策略行 / veto 标记 / 留痕 / 隔离键
        try:
            trade.delete(veto_key)
            trade.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            bus = _redis(0)
            bus.delete(f"qm:news:intel:cursor:itest-{tag}", f"qm:news:intel:seen:itest-{tag}",
                       f"qm:news:intel:spike:{symbol}",
                       f"qm:news:intel:neg:{symbol}")
            # 自清理总线事件（测试事件会经 WS 广播到用户 UI——不留痕）
            victims = []
            for _eid, fields in bus.xrevrange("intel:events", count=500):
                try:
                    ev = json.loads(fields.get("data") or "{}")
                except (TypeError, ValueError):
                    continue
                if str(((ev.get("payload") or {}).get("title")) or "").startswith(f"[itest-{tag}]"):
                    victims.append(_eid)
            if victims:
                bus.xdel("intel:events", *victims)
            bus.close()
        except Exception:  # noqa: BLE001
            pass
        news_veto._flag_cache.pop(str(sid), None)
        with sync_session() as session:
            session.execute(
                sql_text("DELETE FROM news_article_enrichment WHERE huntly_page_id = :p AND title_hash = :th"),
                {"p": page_id, "th": title_hash},
            )
            session.execute(
                sql_text("DELETE FROM risk_events WHERE rule_type='news_event_veto' AND symbol=:s "
                         "AND created_at > NOW() - INTERVAL '5 minutes'"),
                {"s": symbol},
            )
            session.execute(sql_text("DELETE FROM strategies WHERE id = :sid"), {"sid": sid})
            session.commit()
