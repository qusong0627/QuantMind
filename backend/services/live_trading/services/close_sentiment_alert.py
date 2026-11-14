"""收盘情绪 QQ 提示：给「复盘前」的一个手机提醒。

用户口径（2026-11-14）：收盘后把情绪这块推一次 QQ，作为打开复盘页之前的提示；
不要在盘后反复刷。数据分两层：

* **今日实时**：``qm:regime:intraday`` 的状态/仓位系数/热集广度（15:09 收盘快照）；
* **上一交易日阶段**：``market_stage_snapshot`` 的六块/事件/情绪/Q1–Q5（官方日频口径）。

官方情绪快照（``sentiment.json``）与市场快照在次日 04:00–05:50 才产出，所以
15:10 这条提醒**不冒充今日官方情绪**：今日只给实时广度，六块/压力/赚钱效应用
上一交易日并明确标注。真正的今日复盘数据在次日凌晨补齐。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.market_stage_snapshot import load_latest_snapshot

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
_STATE_ZH = {"bull": "偏强", "neutral": "中性", "bear": "偏弱"}


def _num(value: Any, digits: int = 1, sign: bool = False) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if f != f or f in (float("inf"), float("-inf")):
        return "—"
    return f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"


def _index_line(now: datetime) -> str:
    """今日指数涨跌（market:snapshot；取不到就省略）。"""
    try:
        from backend.shared.decision_context_source import (
            day_change_pct,
            read_snapshots,
        )
        from backend.shared.remote_quote_config import make_sync_client
        from backend.shared.stock_utils import StockCodeUtil

        codes = ["000001.SH", "399001.SZ", "399006.SZ"]
        client = make_sync_client()
        snaps = read_snapshots(client, codes)
        parts = []
        names = {"000001.SH": "上证", "399001.SZ": "深成", "399006.SZ": "创业板"}
        for code in codes:
            snap = snaps.get(StockCodeUtil.to_suffix(code))
            if not snap:
                continue
            chg = day_change_pct(snap)
            if chg is None:
                continue
            parts.append(f"{names[code]} {chg:+.2f}%")
        return "｜".join(parts)
    except Exception as exc:  # noqa: BLE001 - 指数行取不到不拦整条提醒
        logger.info("[CloseSentiment] 指数快照取数跳过: %s", exc)
        return ""


def _live_block(now: datetime) -> list[str]:
    from backend.services.trade.services.decision_round_io import load_intraday_regime

    regime = load_intraday_regime() or {}
    if not regime:
        return ["今日实时：盘中 regime 不可得"]
    state_raw = str(regime.get("state") or "").strip()
    state = _STATE_ZH.get(state_raw, state_raw or "—")
    hint = _num(regime.get("position_hint"), 1)
    lines = [f"今日实时：状态 {state}｜建议仓位系数 {hint}"]
    try:
        breadth = json.loads(regime.get("breadth") or "{}")
    except (TypeError, ValueError):
        breadth = {}
    if breadth:
        up = breadth.get("up")
        down = breadth.get("down")
        flat = breadth.get("flat")
        parts = [f"涨 {up}", f"跌 {down}", f"平 {flat}"]
        sample = breadth.get("sample")
        if sample is not None:
            parts.append(f"热集 {sample} 只")
        total = sum(int(x) for x in (up, down, flat) if isinstance(x, (int, float)))
        if total > 0:
            parts.append(f"上涨占比 {float(up or 0) / total * 100:.1f}%")
        limit_up = breadth.get("limit_up")
        limit_down = breadth.get("limit_down")
        if limit_up is not None or limit_down is not None:
            parts.append(f"涨停 {limit_up if limit_up is not None else '—'}")
            parts.append(f"跌停 {limit_down if limit_down is not None else '—'}")
        lines.append("｜".join(parts))
    generated = str(regime.get("generated_at") or "")
    if generated:
        try:
            moment = datetime.fromisoformat(generated)
            age = (now - moment).total_seconds() / 60
            if age > 30:
                lines.append(f"（实时快照生成于 {moment:%H:%M}，{age:.0f} 分钟前）")
        except ValueError:
            pass
    return lines


def _stage_block(now: datetime) -> list[str]:
    snap = load_latest_snapshot()
    if not snap:
        return ["上一交易日阶段：数据缺口（收盘快照未生成）"]
    day = str(snap.get("date") or "—")
    state = snap.get("state") or "—"
    score = snap.get("score")
    layers = {str(item.get("key")): item for item in snap.get("layers") or []}
    layer_txt = "｜".join(
        f"{layers[k].get('label')} {layers[k].get('stage')}"
        for k in ("long", "medium", "short", "intraday")
        if k in layers
    )
    score_txt = f"{int(score):+d}" if isinstance(score, int) else "—"
    stage_label = "今日阶段" if day == now.strftime("%Y-%m-%d") else "上一交易日阶段"
    lines = [
        f"{stage_label}（截至 {day} 收盘）：六块 {score_txt}/±12（{state}）"
        + (f"｜{layer_txt}" if layer_txt else "")
    ]
    events = snap.get("events") or {}
    lines.append(
        "事件："
        f"TRIN_量 {_num(events.get('trin_vol'), 2)}"
        f"（{'投降' if events.get('trin_surrender') else '不投降'}）"
        f"｜额价差 {_num(events.get('amount_gap_pp'), 1, sign=True)}pp"
        f"（{'风险' if events.get('amount_gap_risk') else '未到20pp'}）"
        f"｜ADL 背离 {'有' if events.get('adl_divergence') else '无'}"
    )
    sentiment = snap.get("sentiment") or {}
    breadth = snap.get("breadth") or {}
    main_net = sentiment.get("main_net_yi")
    main_txt = "主力净额 —"
    if main_net is not None:
        main_txt = (
            f"主力{'净流入' if float(main_net) >= 0 else '净流出'} "
            f"{_num(abs(float(main_net)), 1)} 亿"
        )
    lines.append(
        f"情绪（{sentiment.get('trade_date') or day}）："
        f"压力 {_num(sentiment.get('stress_score'), 1)}"
        f"（{sentiment.get('zone_label') or '—'}）"
        f"｜赚钱效应 {_num(breadth.get('profit_effect'), 1)}"
        f"｜{main_txt}"
    )
    sb = snap.get("stock_breadth") or {}
    if sb.get("available") and sb.get("quintiles"):
        q = " / ".join(
            f"{row.get('q')} {_num(row.get('ret_after'), 2, sign=True)}"
            for row in sb["quintiles"]
        )
        sample = "热集" if sb.get("sample") == "hot_set" else "全市场"
        lines.append(f"Q1–Q5 收盘中位：{q}（{sample} {sb.get('n', '—')} 只）")
    return lines


def build_message(now: datetime | None = None) -> tuple[str, str] | None:
    """构建收盘情绪提醒；没有任何数据时返回 ``None``。"""
    moment = now or datetime.now(CN_TZ)
    live = _live_block(moment)
    stage = _stage_block(moment)
    index_line = _index_line(moment)
    if not live and not stage:
        return None
    lines = [f"【收盘情绪提示｜{moment:%Y-%m-%d %H:%M}】"]
    if index_line:
        lines.append(f"指数：{index_line}")
    lines.extend(live)
    lines.extend(stage)
    lines.append(
        "复盘前先看：涨跌停/炸板率与持仓板块，再决定明天；情绪只做风险闸，"
        "不做方向预测。今日官方情绪/市场快照次日凌晨补齐。"
    )
    title = f"收盘情绪提示｜{moment:%m-%d}"
    return title, "\n".join(lines)


def _already_sent(day: str) -> bool:
    try:
        import redis as _redis

        client = _redis.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
        return bool(client.exists(f"qm:alert:close_sentiment:{day}"))
    except Exception:  # noqa: BLE001 - 去重失败不拦发送
        return False


def _mark_sent(day: str) -> None:
    try:
        import redis as _redis

        client = _redis.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
        client.set(f"qm:alert:close_sentiment:{day}", "1", ex=2 * 86400)
    except Exception:  # noqa: BLE001
        pass


def send_close_sentiment(now: datetime | None = None) -> dict[str, Any]:
    """发送一次；按日去重（同一天最多一条）。"""
    moment = now or datetime.now(CN_TZ)
    day = moment.strftime("%Y%m%d")
    if _already_sent(day):
        return {"status": "skipped", "reason": "already_sent", "day": day}
    built = build_message(moment)
    if built is None:
        return {"status": "skipped", "reason": "no_data", "day": day}
    title, content = built
    try:
        from backend.shared.qq_notify import alert_async

        queued = alert_async(
            level="info",
            title=title,
            content=content,
            alert_type="market_sentiment",
            force=True,
        )
    except Exception as exc:  # noqa: BLE001 - 提醒失败不升级为异常
        logger.warning("[CloseSentiment] QQ 推送异常: %s", exc)
        return {"status": "failed", "reason": type(exc).__name__, "day": day}
    if queued:
        _mark_sent(day)
    return {
        "status": "sent" if queued else "skipped",
        "reason": "" if queued else "qq_not_configured",
        "day": day,
        "title": title,
    }
