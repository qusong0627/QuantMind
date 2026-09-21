"""建议卡规则生成器（T-P6-16 闭环补全）：信号×情报共振 → 观察仓建议卡（人在环）。

机构口径（v1，严格；所有卡片 source="rule_engine"、context_refs.gen 幂等）：
- 候选 = 当日融合信号 **靠前梯队 Top20**（fusion_score 降序，即 signal_side='BUY' 侧）；
- **否决**：近 24h 存在风险类告警（news:negative / news:risk_event / critical）的标的不生成
  ——与新闻 veto 同纪律；同时列出共振项（近 24h news:positive）作为排序与理由增益；
- **去重**：模拟账户已持仓的标的不生成；近 7 自然日（≈5 交易日）已自动生成过同标的的不再生成；
- **regime 门控**：日内 regime position_hint ≤ 0.4（弱市）→ 整体停发；
- 产出上限：每交易日 ≤ 3 张；动作 = 最小观察仓（100 股，市价）；执行仍由用户在面板自行决定。
- rationale 强制纪律声明（自动生成/单一信号透镜/仅观察仓/执行前复核）。

调度：trade 常驻 worker，交易日 ≥16:20 一次（Redis done 键防重跑）；也可 CLI：
``python -m backend.services.trade.services.advice_generator``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_CST = ZoneInfo("Asia/Shanghai")

TOP_N = 20
MAX_CARDS = 3
OBSERVE_LOT = 100
REGIME_MIN_POSITION_HINT = 0.4
DEDUPE_LOOKBACK_DAYS = 7
SOURCE = "rule_engine"
GEN_DONE_PREFIX = "trade:advice-gen:done:"

_RISK_ALERT_MARKERS = ("negative", "risk")


def _norm_symbol(raw: Any) -> str:
    """任意代码形态 → 后缀式大写（失败原样大写）。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        return (StockCodeUtil.to_suffix(text) or text).upper()
    except Exception:  # noqa: BLE001
        return text


def select_candidates(
    *,
    signals: list[dict[str, Any]],
    veto_symbols: set[str],
    held_symbols: set[str],
    positive_symbols: set[str] | None = None,
    k: int = MAX_CARDS,
) -> list[dict[str, Any]]:
    """纯函数：按序挑选生成候选（否决→持仓→上限），带 rank/共振标记。

    signals 元素需含 {symbol, fusion_score, signal_side}，调用方保证已按分降序。
    """
    positives = positive_symbols or set()
    picked: list[dict[str, Any]] = []
    for item in signals:
        sym = _norm_symbol(item.get("symbol"))
        if not sym:
            continue
        if str(item.get("signal_side") or "").upper() != "BUY":
            continue
        if sym in veto_symbols:
            continue
        if sym in held_symbols:
            continue
        picked.append(
            {
                "symbol": sym,
                "fusion_score": float(item.get("fusion_score") or 0.0),
                "rank": len(picked) + 1,
                "resonance": sym in positives,
            }
        )
        if len(picked) >= k:
            break
    return picked


def build_card(*, candidate: dict[str, Any], gen_key: str, trade_date: str, side_counts: dict[str, int] | None, regime_hint: float | None) -> dict[str, Any]:
    """纯函数：候选 → 建议卡 payload（title/rationale/actions/context_refs）。"""
    sym = candidate["symbol"]
    res = "情报共振（近24h 正面情报）" if candidate["resonance"] else "无风险告警（近24h）"
    regime_txt = f"{regime_hint:.2f}" if regime_hint is not None else "未取到"
    counts = side_counts or {}
    return {
        "title": f"观察仓（自动）：{sym} 融合信号截面靠前（{res}）",
        "rationale": (
            f"自动生成（规则 v1 · 信号×情报共振）：{trade_date} 融合信号靠前梯队 Top{TOP_N} 之列"
            f"（序列第 {candidate['rank']}，分 {candidate['fusion_score']:.4f}）；{res}；"
            f"当日信号分布 BUY {counts.get('BUY', '—')} / SELL {counts.get('SELL', '—')} / "
            f"HOLD {counts.get('HOLD', '—')}；日内 regime 仓位提示 {regime_txt}。"
            f"纪律声明：单一信号透镜、仅最小观察仓 {OBSERVE_LOT} 股，执行与否由你在交易台决策；"
            f"规模样本（n≥20）前只看流程不看胜负。"
        ),
        "actions": [
            {"symbol": sym, "side": "buy", "quantity": OBSERVE_LOT, "order_type": "market", "price": None}
        ],
        "context_refs": {
            "gen": gen_key,
            "signal": {"trade_date": trade_date, "symbol": sym, "rank": candidate["rank"], "fusion_score": candidate["fusion_score"]},
            "regime": {"position_hint": regime_hint},
            "policy": "自动生成（rule_engine v1）：信号Top∩无风险告警；已持仓/近5交易日重复不生成；每交易日≤3张",
        },
    }


# ── IO 装载 ──────────────────────────────────────────────────────────


async def _load_signals(limit: int = TOP_N) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    """最新预测日 BUY 侧 Top 信号 + 当日分布。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        trade_date = (
            await db.execute(
                text("SELECT max(trade_date) FROM engine_signal_scores")
            )
        ).scalar()
        if trade_date is None:
            return [], "", {}
        rows = (
            await db.execute(
                text(
                    "SELECT symbol, fusion_score, signal_side FROM engine_signal_scores "
                    "WHERE trade_date = :d AND signal_side = 'BUY' "
                    "ORDER BY fusion_score DESC LIMIT :lim"
                ),
                {"d": trade_date, "lim": int(limit)},
            )
        ).fetchall()
        counts = dict(
            (
                await db.execute(
                    text(
                        "SELECT signal_side, count(*) FROM engine_signal_scores "
                        "WHERE trade_date = :d GROUP BY signal_side"
                    ),
                    {"d": trade_date},
                )
            ).fetchall()
        )
    return [{"symbol": r[0], "fusion_score": r[1], "signal_side": r[2]} for r in rows], str(trade_date), {str(k): int(v) for k, v in counts.items()}


async def _load_alert_symbols() -> tuple[set[str], set[str]]:
    """近 24h (风险告警集, 正面情报集)——标的一律归一后缀式。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    veto: set[str] = set()
    positive: set[str] = set()
    async with get_session(read_only=True) as db:
        rows = (
            await db.execute(
                text(
                    "SELECT symbol, alert_type, severity FROM sentinel_alerts "
                    "WHERE ts > now() - interval '24 hours' AND symbol IS NOT NULL"
                )
            )
        ).fetchall()
    for sym_raw, atype, sever in rows:
        sym = _norm_symbol(sym_raw)
        if not sym:
            continue
        atype_l = str(atype or "").lower()
        if "positive" in atype_l:
            positive.add(sym)
        elif any(m in atype_l for m in _RISK_ALERT_MARKERS) or str(sever or "").lower() == "critical":
            veto.add(sym)
    return veto, positive


async def _load_held_symbols() -> set[str]:
    """模拟账户(A股)持仓集（trade redis；读取失败按空集，不阻塞生成）。"""
    try:
        from backend.services.trade_shared.redis_client import redis_client
        from backend.services.trade_shared.simulation_manager import (
            SimulationAccountManager,
            canonical_sim_uid,
        )

        if getattr(redis_client, "client", None) is None:
            redis_client.connect()
        uid = canonical_sim_uid("10000001")
        account = await SimulationAccountManager(redis_client).get_account(uid, "default")
        positions = (account or {}).get("positions") or {}
        return {_norm_symbol(k) for k in positions}
    except Exception as exc:  # noqa: BLE001 - 读取失败按空集（不因持仓读取失败阻塞生成）
        logger.warning("[advice-gen] 持仓读取失败（按空集继续）: %s", exc)
        return set()


def _load_regime_hint() -> float | None:
    """日内 regime 仓位提示（db0 qm:regime:intraday；缺失 None=不门控仅记）。"""
    try:
        import redis as _redis_lib

        client = _redis_lib.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        try:
            raw = client.hget("qm:regime:intraday", "position_hint")
        finally:
            client.close()
        return float(raw) if raw not in (None, "") else None
    except Exception:  # noqa: BLE001
        return None


async def _load_recent_gen_symbols() -> set[str]:
    """近 DEDUPE_LOOKBACK_DAYS 天已建议过的标的（跨来源）：

    - 自动生成（context_refs.gen 反解）；
    - **任何来源**建议卡动作里的标的（防与人工/QuantBot 卡叠发同一标的）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    out: set[str] = set()
    async with get_session(read_only=True) as db:
        rows = (
            await db.execute(
                text(
                    "SELECT context_refs->>'gen' FROM copilot_advice "
                    "WHERE source = :src AND created_at > now() - make_interval(days => :d) "
                    "AND context_refs ? 'gen'"
                ),
                {"src": SOURCE, "d": DEDUPE_LOOKBACK_DAYS},
            )
        ).fetchall()
        for (gen,) in rows:
            parts = str(gen or "").rsplit("-", 1)
            if len(parts) == 2 and parts[1]:
                out.add(_norm_symbol(parts[1]))
        rows2 = (
            await db.execute(
                text(
                    "SELECT DISTINCT a->>'symbol' FROM copilot_advice, "
                    "jsonb_array_elements(actions) a "
                    "WHERE created_at > now() - make_interval(days => :d)"
                ),
                {"d": DEDUPE_LOOKBACK_DAYS},
            )
        ).fetchall()
        for (sym,) in rows2:
            norm = _norm_symbol(sym)
            if norm:
                out.add(norm)
    return out


async def generate_once(*, today: date | None = None) -> dict[str, int]:
    """生成一轮（幂等）：装载 → 选择 → 建卡。返回统计。"""
    from sqlalchemy import text

    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.database_manager_v2 import get_session

    if not ensure_copilot_advice_table():
        return {"created": 0, "failed": 1}
    today = today or datetime.now(_CST).date()
    today_str = today.strftime("%Y-%m-%d")
    stats = {"created": 0, "skipped_regime": 0, "candidates": 0, "failed": 0}

    regime_hint = _load_regime_hint()
    if regime_hint is not None and regime_hint <= REGIME_MIN_POSITION_HINT:
        stats["skipped_regime"] = 1
        logger.info("[advice-gen] regime 弱市门控（position_hint=%.2f）→ 本轮停发", regime_hint)
        return stats

    signals, trade_date, side_counts = await _load_signals()
    if not signals:
        logger.info("[advice-gen] 无可用信号，跳过")
        return stats
    veto, positive = await _load_alert_symbols()
    held = await _load_held_symbols()
    recent_gen = await _load_recent_gen_symbols()

    candidates = select_candidates(
        signals=signals,
        veto_symbols=veto,
        held_symbols=held | recent_gen,
        positive_symbols=positive,
        k=MAX_CARDS,
    )
    stats["candidates"] = len(candidates)

    from types import SimpleNamespace

    from backend.services.api.routers.copilot import validate_actions

    for cand in candidates:
        gen_key = f"adv-gen-{today.strftime('%Y%m%d')}-{cand['symbol'].lower()}"
        card = build_card(
            candidate=cand, gen_key=gen_key, trade_date=trade_date,
            side_counts=side_counts, regime_hint=regime_hint,
        )
        try:
            actions = validate_actions(
                [SimpleNamespace(**a) for a in card["actions"]]
            )
        except Exception as exc:  # noqa: BLE001 - 校验失败=不建卡（资金相关不静默）
            logger.warning("[advice-gen] 动作校验失败 %s: %s", cand["symbol"], exc)
            stats["failed"] += 1
            continue
        try:
            async with get_session(read_only=False) as db:
                await db.execute(
                    text(
                        "INSERT INTO copilot_advice (tenant_id, user_id, source, title, rationale, "
                        "actions, context_refs, status) VALUES "
                        "('default', 0, :src, :ti, :ra, CAST(:ac AS JSONB), CAST(:cr AS JSONB), 'pending')"
                    ),
                    {
                        "src": SOURCE,
                        "ti": card["title"][:256],
                        "ra": card["rationale"][:4000],
                        "ac": json.dumps(actions, ensure_ascii=False),
                        "cr": json.dumps(card["context_refs"], ensure_ascii=False, default=str),
                    },
                )
                await db.commit()
            stats["created"] += 1
            logger.info("[advice-gen] 建卡 %s rank=%d resonance=%s", cand["symbol"], cand["rank"], cand["resonance"])
        except Exception as exc:  # noqa: BLE001 - 单卡失败不拖垮
            logger.warning("[advice-gen] 建卡失败 %s: %s", cand["symbol"], exc)
            stats["failed"] += 1
    return stats


async def run_advice_generator_worker() -> None:
    """常驻：交易日 ≥16:20 生成一次（Redis done 键防重跑；失败不置键下轮重试）。"""
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info("[advice-gen] 生成循环启动")
    while True:
        try:
            _sched_heartbeat("advice_generator")
        except Exception:  # noqa: BLE001
            pass
        now = datetime.now(_CST)
        if now.weekday() < 5 and (now.hour, now.minute) >= (16, 20):
            done_key = f"{GEN_DONE_PREFIX}{now.date().isoformat()}"
            try:
                import redis as _redis_lib

                client = _redis_lib.Redis(
                    host=os.getenv("REDIS_HOST") or "redis",
                    port=int(os.getenv("REDIS_PORT", "6379")),
                    password=os.getenv("REDIS_PASSWORD") or None,
                    db=int(os.getenv("REDIS_DB_GENERAL", "0")),
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=5,
                )
                try:
                    if not client.set(done_key, "1", nx=True, ex=172800):
                        await asyncio.sleep(300)
                        continue
                finally:
                    client.close()
                stats = await generate_once()
                logger.info("[advice-gen] 完成 %s", stats)
            except Exception as exc:  # noqa: BLE001 - 失败不置键，下轮重试
                logger.warning("[advice-gen] 失败（下轮重试）: %s", exc)
        await asyncio.sleep(300)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stats = asyncio.run(generate_once())
    print(f"[advice-gen] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
