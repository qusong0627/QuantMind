"""今日交易台 API（T-P1-05）。

一屏闭环（设计见 docs/前端界面设计_设计方案.md §二、主文档 §12）：
  数据✓ → 推理✓ → 信号 → 执行 → 盈亏 + 系统健康卡。

设计要点：
- **与体检脚本同源**：`pipeline` 四步状态与 `health` 卡直接由
  ``backend/scripts/diagnose/health.py::CHECKS`` 的判定结果映射（不重复造判定逻辑）；
- 健康检查是同步 IO（psycopg2 + sync redis），统一 `asyncio.to_thread` 执行，
  不阻塞事件循环；`?health=false` 可跳过（纯数据快速加载）；
- **每个数字带 `source` 下钻字段**（前端下钻链路的契约，主文档 §十）。

数据口径：
- 身份：`get_current_user` → 快照/订单用归一 uid（`require_sim_user_id`），
  REAL 订单用原始 sub（orders.user_id 为字符串口径）；
- 信号：`engine_signal_scores` 最新 trade_date（rank_pct 分位口径）。

GET /api/v1/desk/today
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_name_mapper import resolve_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/desk", tags=["Desk"])

# pipeline 步骤 ← 体检断言映射（同源；键=步骤，值=体检项 ID）
_PIPELINE_FROM_CHECKS: list[tuple[str, str, str]] = [
    ("data_sync", "数据同步", "C08"),
    ("inference", "推理就绪", "C02"),
    ("signals", "信号分布", "C01"),
    ("settlement", "结算/台账", "C05"),
]
_LEVEL_TO_STATUS = {"ok": "ok", "warn": "warn", "fail": "fail"}


def build_pipeline(health_items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """体检结果 → 管线步骤（纯函数，可单测）。"""
    out: list[dict[str, Any]] = []
    for key, label, check_id in _PIPELINE_FROM_CHECKS:
        item = health_items.get(check_id)
        if item is None:
            out.append(
                {
                    "key": key,
                    "label": label,
                    "status": "unknown",
                    "detail": "健康检查未运行（?health=false）",
                    "source": f"health:{check_id}",
                }
            )
            continue
        out.append(
            {
                "key": key,
                "label": label,
                "status": _LEVEL_TO_STATUS.get(str(item.get("level")), "unknown"),
                "detail": str(item.get("detail") or ""),
                "source": f"health:{check_id}",
            }
        )
    return out


async def _run_health(market: str = "CN") -> dict[str, dict[str, Any]]:
    """在线程池跑同步体检（不阻塞事件循环）；异常降级为空表。

    ``market`` 透传给 ``HealthContext``：C01/C02/C08 按市场取数（信号分布/就绪/同步），
    其余断言是账户/系统级、与市场无关。不传市场等于按 CN 体检。
    """

    def _sync() -> dict[str, dict[str, Any]]:
        try:
            import asyncio as _aio

            from backend.scripts.diagnose import health as health_mod

            ctx = health_mod._build_context(market)
            items: dict[str, dict[str, Any]] = {}
            # health.py 的检查是 async（内部只做同步 IO）——在专属事件循环里统一执行
            loop = _aio.new_event_loop()
            try:
                for cid, name, fn in health_mod.CHECKS:
                    try:
                        r = loop.run_until_complete(fn(ctx))
                        items[cid] = {
                            "id": cid,
                            "name": name,
                            "level": r.level,
                            "detail": r.detail,
                        }
                    except Exception as exc:  # noqa: BLE001
                        items[cid] = {
                            "id": cid,
                            "name": name,
                            "level": "fail",
                            "detail": f"检查执行异常: {exc}",
                        }
            finally:
                loop.close()
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Desk] health 运行失败: %s", exc)
            return {}

    return await asyncio.to_thread(_sync)


async def _collect_signals(tenant_id: str, market: str = "CN") -> dict[str, Any]:
    """当日信号分布（**按市场**取最新 trade_date 与分布）。

    市场必须进 SQL：`engine_signal_scores` 带 market 列，不滤等于把各市场信号
    混在一张分布里（港股/美股页签翻出 A 股信号的直接原因）。

    谓词一律 `COALESCE(market,'CN')`——该列可空且**契约只加列不回填**
    （``signal_contract.py`` 无 UPDATE，回填脚本无启动期调用点），裸 `market = :m`
    在未回填的实例上会把 CN 查成空。仓库读侧同口径见 copilot.py / hot_set_builder.py。
    """
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        date_row = (
            await session.execute(
                sa_text(
                    "SELECT max(trade_date) AS d FROM engine_signal_scores "
                    "WHERE COALESCE(market, 'CN') = :m"
                ),
                {"m": market},
            )
        ).one()
        trade_date = date_row.d
        if trade_date is None:
            return {
                "trade_date": None,
                "market": market,
                "buy": 0,
                "sell": 0,
                "hold": 0,
                "top_buy": [],
                "source": "db:engine_signal_scores",
            }
        counts = (
            await session.execute(
                sa_text(
                    "SELECT signal_side, count(*) AS n FROM engine_signal_scores "
                    "WHERE trade_date = :d AND COALESCE(market, 'CN') = :m "
                    "GROUP BY signal_side"
                ),
                {"d": trade_date, "m": market},
            )
        ).all()
        top = (
            await session.execute(
                sa_text(
                    "SELECT symbol, signal_side, round(rank_pct::numeric, 4) AS rank_pct, "
                    "round(fusion_score::numeric, 4) AS score "
                    "FROM engine_signal_scores "
                    "WHERE trade_date = :d AND COALESCE(market, 'CN') = :m "
                    "AND signal_side = 'BUY' "
                    "ORDER BY rank_pct DESC NULLS LAST LIMIT 10"
                ),
                {"d": trade_date, "m": market},
            )
        ).all()
    by_side = {str(r.signal_side): int(r.n) for r in counts}
    return {
        "trade_date": str(trade_date),
        "market": market,
        "buy": by_side.get("BUY", 0),
        "sell": by_side.get("SELL", 0),
        "hold": by_side.get("HOLD", 0),
        "top_buy": [
            {
                "symbol": str(r.symbol),
                # 中文名（stocks_index.json 归一查表；未收录为空串，前端回退显示代码）
                "name": resolve_name(str(r.symbol)),
                "side": str(r.signal_side),
                "rank_pct": float(r.rank_pct) if r.rank_pct is not None else None,
                "score": float(r.score) if r.score is not None else None,
            }
            for r in top
        ],
        "source": "db:engine_signal_scores",
    }


def _orders_market_unsupported(market: str) -> dict[str, Any] | None:
    """委托表**无 market 列** → 非 CN 市场无法按市场归集委托，返回不可用块（CN 返回 None）。

    市场维度落在账户键（``simulation:account:{t}:{u}:{MARKET}``）与台账
    （``simulation_accounts.market`` / ``simulation_position_lots.market``）上，
    委托行（``sim_orders`` / ``orders``）本身不带市场。要让港股/美股委托可归集，
    需给委托表加 market 列并在下单链路写入——在那之前非 CN 一律如实返回不可用，
    绝不把 A 股委托数当成港股/美股的。
    """
    if market == "CN":
        return None
    return {
        "available": False,
        "market": market,
        "reason": (
            f"{market} 市场委托暂不可按市场归集：委托表 sim_orders 无 market 列"
            "（市场维度只记在账户键/台账上）"
        ),
        "source": "desk:market-scope",
    }


async def _collect_execution(
    tenant_id: str, sim_uid: int, raw_user: str, market: str = "CN"
) -> dict[str, Any]:
    unsupported = _orders_market_unsupported(market)
    if unsupported is not None:
        return unsupported
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        sim_rows = (
            await session.execute(
                sa_text(
                    "SELECT symbol, side::text AS side, quantity, status::text AS status, "
                    "price_source, client_order_id, source, remarks, created_at "
                    "FROM sim_orders WHERE tenant_id = :t AND user_id = :u "
                    "AND created_at >= date_trunc('day', now()) "
                    "ORDER BY created_at DESC LIMIT 10"
                ),
                {"t": tenant_id, "u": sim_uid},
            )
        ).all()
        real_rows = (
            await session.execute(
                sa_text(
                    "SELECT symbol, side::text AS side, quantity, status::text AS status, "
                    "price_source, client_order_id, source, remarks, created_at "
                    "FROM orders WHERE tenant_id = :t AND user_id = :u "
                    "AND created_at >= date_trunc('day', now()) "
                    "ORDER BY created_at DESC LIMIT 10"
                ),
                {"t": tenant_id, "u": raw_user},
            )
        ).all()

    def _item(mode: str, r: Any) -> dict[str, Any]:
        return {
            "mode": mode,
            "symbol": str(r.symbol),
            # 中文名（stocks_index.json 归一查表；未收录为空串，前端回退显示代码）
            "name": resolve_name(str(r.symbol)),
            "side": str(r.side),
            "quantity": float(r.quantity or 0),
            "status": str(r.status),
            "price_source": r.price_source,
            "client_order_id": r.client_order_id,
            "origin": r.source,
            "reason": r.remarks,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    items = [_item("SIM", r) for r in sim_rows] + [_item("REAL", r) for r in real_rows]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {
        "available": True,
        "market": market,
        "sim_count": len(sim_rows),
        "real_count": len(real_rows),
        "filled": sum(1 for i in items if i["status"] == "filled"),
        "rejected": sum(1 for i in items if i["status"] == "rejected"),
        "items": items[:10],
        "source": "db:sim_orders+orders",
    }


async def _collect_fidelity(tenant_id: str, sim_uid: int, market: str = "CN") -> dict[str, Any]:
    """F2 保真度（T-P6-18）：当日模拟委托 → 成交率/部分成交/成交价偏差/滑点实现/执行核分布。

    参考价 = 当日真实收盘（QuantDB 前复权，与影子对照同源口径）；口径随块返回（caliber）。
    """
    import asyncio

    from sqlalchemy import text as sa_text

    from backend.shared.fill_quality import fidelity_metrics

    source = "db:sim_orders ≈ QuantDB(qdb_daily_forward.close) + shared/fill_quality.py"
    unsupported = _orders_market_unsupported(market)
    if unsupported is not None:
        return unsupported
    try:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    sa_text(
                        "SELECT symbol, side::text AS side, quantity, filled_quantity, status::text AS status, "
                        "       average_price, execution_model, price_source "
                        "FROM sim_orders WHERE tenant_id = :t AND user_id = :u "
                        "AND created_at >= date_trunc('day', now()) "
                        "ORDER BY created_at DESC LIMIT 500"
                    ),
                    {"t": tenant_id, "u": sim_uid},
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"委托读取失败: {exc}"[:200], "source": source}

    symbols = sorted({str(r.symbol) for r in rows if r.symbol})
    refs: dict[str, float] = {}
    if symbols:
        def _load_closes() -> dict[str, float]:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            from backend.shared.stock_utils import StockCodeUtil

            out: dict[str, float] = {}
            df = QuantDBDataHub.get_instance().fetch_latest_rows(
                "qdb_daily_forward", [StockCodeUtil.to_suffix(s) or s for s in symbols][:400],
                columns=["close"],
            )
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    out[str(row.get("symbol"))] = float(row.get("close") or 0)
            return out

        try:
            refs = await asyncio.to_thread(_load_closes)
        except Exception:  # noqa: BLE001 - 参考价缺失如实计 missing_ref
            refs = {}

    try:
        from backend.services.trade_shared.trade_config import settings as _settings

        configured_bps = float(_settings.SIMULATION_SLIPPAGE_BPS)
    except Exception:  # noqa: BLE001
        configured_bps = None

    def _norm_symbol(raw: str) -> str:
        from backend.shared.stock_utils import StockCodeUtil

        return StockCodeUtil.to_suffix(str(raw or "").strip()) or str(raw or "")

    metrics = fidelity_metrics(
        [
            {
                "symbol": _norm_symbol(str(r.symbol)),
                "side": str(r.side),
                "quantity": float(r.quantity or 0),
                "filled_quantity": float(r.filled_quantity or 0),
                "fill_price": float(r.average_price or 0) or None,
                "status": str(r.status),
                "execution_model": str(r.execution_model or "unknown"),
                "price_source": str(r.price_source or ""),
            }
            for r in rows
        ],
        reference_prices={_norm_symbol(k): v for k, v in refs.items()},
        configured_slippage_bps=configured_bps,
    )
    # 执行核切换现值 + 快照核运行计数（降级可见）
    try:
        from backend.services.simulation.services.exec_core import (
            resolve_exec_core,
            snapshot_core_stats,
        )

        metrics["exec_core_mode"] = resolve_exec_core()
        metrics["snapshot_core_stats"] = snapshot_core_stats()
    except Exception:  # noqa: BLE001
        metrics["exec_core_mode"] = None
    metrics.update({"available": True, "market": market, "source": source})
    return metrics


async def _collect_pnl(tenant_id: str, sim_user_id: str, market: str = "CN") -> dict[str, Any]:
    """账户盈亏（**按市场**取快照行）。

    T-P1-07：快照带市场维度后同日各市场行与合并行混排，LIMIT 1 会取到不确定的一行，
    因此必须显式限定市场：页面市场 = 哪个市场就取哪个市场行；
    ``market=ALL`` 才取跨市场合并行（不滤 market 的裸查询一律视为口径错误）。
    """
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        from backend.shared.fund_snapshot_contract import (
            fund_snapshot_has_market_column_async,
        )

        has_market = await fund_snapshot_has_market_column_async()
        market_clause = "AND market = :m " if has_market else ""
        params: dict[str, Any] = {"t": tenant_id, "u": sim_user_id}
        if has_market:
            params["m"] = market
        row = (
            await session.execute(
                sa_text(
                    "SELECT snapshot_date, total_asset, initial_capital, total_pnl, today_pnl, "
                    "market_value, updated_at FROM simulation_fund_snapshots "
                    "WHERE tenant_id = :t AND user_id = :u "
                    f"{market_clause}"
                    "ORDER BY snapshot_date DESC LIMIT 1"
                ),
                params,
            )
        ).first()
    if row is None:
        return {
            "available": False,
            "market": market,
            "detail": (
                f"无资金快照（账户未初始化或权益结算未运行）——市场 {market}"
                if has_market
                else "无资金快照（账户未初始化或权益结算未运行）"
            ),
            "source": "db:simulation_fund_snapshots",
        }
    return {
        "available": True,
        "market": market,
        "snapshot_date": str(row.snapshot_date),
        "total_asset": float(row.total_asset or 0),
        "initial_capital": float(row.initial_capital or 0),
        "total_pnl": float(row.total_pnl or 0),
        "today_pnl": float(row.today_pnl or 0),
        "market_value": float(row.market_value or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "source": "db:simulation_fund_snapshots",
    }


async def _collect_shadow(market: str = "CN") -> dict[str, Any]:
    """影子对照块（T-P2-06）：读最近一份日报（Redis），不重复计算。

    指标口径见 ``shared/shadow_compare.py``（成交价偏差 bps/成交率/滑点实现/
    跟踪误差）；无日报时如实返回不可用（不伪造数字）。

    日报**全局一份、键无市场段**，基准是跨市场合并行（``simulation_fund_snapshots``
    的 `market='ALL'`）与 REAL 成交。因此非 CN 页签拿它当"本市场的模拟对照"结论
    属于张冠李戴——与执行环同口径处理：如实返回不可用，让证据环落到 no_evidence。
    """
    if market != "CN":
        return {
            "available": False,
            "market": market,
            "reason": (
                f"{market} 市场无独立的影子对照日报：日报全局一份"
                "（redis:mirror:shadow:{date} 键无市场段，基准为跨市场合并行）"
            ),
            "suggestion": "给日报键加市场段（或按市场生成）后可对照",
            "source": "desk:market-scope",
        }

    def _sync() -> dict | None:
        try:
            from backend.services.trade.services.shadow_compare_service import (
                load_latest_report,
            )
            from backend.services.trade_shared.deps import get_redis

            return load_latest_report(get_redis())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Desk] shadow 日报读取失败: %s", exc)
            return None

    report = await asyncio.to_thread(_sync)
    if not report:
        return {
            "available": False,
            "detail": "尚无影子对照日报（每日 15:15 生成）",
            "source": "redis:mirror:shadow:{date}",
        }
    return {
        "available": True,
        "date": report.get("date"),
        "stale": bool(report.get("stale")),
        "ok": bool(report.get("ok")),
        "coverage": report.get("coverage"),
        "price_deviation": report.get("price_deviation"),
        "fill": report.get("fill"),
        "slippage": report.get("slippage"),
        "tracking_error": report.get("tracking_error"),
        "generated_at": report.get("generated_at"),
        "source": "redis:mirror:shadow:{date}（T-P2-06 影子对照日报）",
    }


async def _async_unavailable(reason: str) -> dict[str, Any]:
    """统一"不可用"块（跳过/降级时不伪造数据）。"""
    return {"available": False, "reason": reason, "source": "desk:toggle"}


# ── T-FE-16 全链证据矩阵（设计《评估与打分体系》§七：每环有数、每数可验、每验留档）──

EVIDENCE_RINGS: tuple[tuple[str, str, str, str], ...] = (
    # (key, 环节, 证据产物, 频率)
    ("data", "数据", "数据质检报告", "每日"),
    ("feature", "特征", "特征体检表", "每日/训练前"),
    ("model", "模型", "模型卡", "训练后+每日滚动"),
    ("signal", "信号", "信号日报", "每日"),
    ("backtest", "回测", "体检报告", "每次回测"),
    ("simulation", "模拟", "模拟对照报告", "每日"),
    ("execution", "执行", "执行报告", "每日"),
    ("ledger", "账本", "对账报告", "每日"),
    ("strategy", "策略", "策略卡", "每周+事件触发"),
    ("system", "系统", "健康卡", "每日"),
)

# 环节 ← 体检断言映射（除以下外，其余环节证据来自评估留档/日报/文件新鲜度）
_RING_HEALTH_IDS: dict[str, tuple[str, ...]] = {
    "data": ("C08",),
    "signal": ("C01", "C02"),
    "ledger": ("C04", "C05", "C06"),
    "system": ("C03", "C07", "C09", "C10"),
}


def _previous_trading_ymd(ymd: str, market: str = "CN") -> str | None:
    """ymd(YYYYMMDD) 在给定市场的**前一交易日**（严格早于当日）；解析失败返回 None。

    必须按市场取日历：拿 XSHG 的上一交易日当港股/美股的基准，跨市场节假日会
    把「同类交易日」判成滞后（或反之），属口径错误。FUTURES/CRYPTO 无日历
    （``market_calendar`` 返回 None）→ 回落自然日前一天，并在失败时返回 None。
    """
    try:
        import pandas as pd
        from exchange_calendars import get_calendar

        from backend.shared.market_sessions import market_calendar

        ts = pd.Timestamp(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}") - pd.Timedelta(days=1)
        cal_name = market_calendar(market)
        if not cal_name:
            return str(ts.date()).replace("-", "")
        cal = get_calendar(cal_name)
        session = cal.date_to_session(ts, direction="previous")
        return str(session.date()).replace("-", "")
    except Exception:  # noqa: BLE001
        return None


def build_evidence_rings(sources: dict[str, Any]) -> list[dict[str, Any]]:
    """十环证据矩阵（纯函数）→ 每环 {key, label, artifact, frequency, level, summary, items}。

    纪律：**无证据 ≠ 绿**——证据源缺失/为空一律 `no_evidence`（验收：任一环节"无证据"可见）；
    环级状态 = 各证据项最差（fail > warn > ok > no_evidence 仅当无任何项）。
    """
    health_items: dict[str, Any] = sources.get("health_items") or {}
    eval_summary: dict[str, Any] = sources.get("eval_summary") or {}
    features_latest: str | None = sources.get("features_latest")
    signals: dict[str, Any] = sources.get("signals") or {}
    execution: dict[str, Any] = sources.get("execution") or {}
    shadow: dict[str, Any] = sources.get("shadow") or {}

    severity = {"fail": 3, "warn": 2, "ok": 1, "no_evidence": 0}
    rings: list[dict[str, Any]] = []
    for key, label, artifact, frequency in EVIDENCE_RINGS:
        items: list[dict[str, Any]] = []

        for cid in _RING_HEALTH_IDS.get(key, ()):
            item = health_items.get(cid)
            if item is not None:
                items.append(
                    {
                        "id": cid,
                        "name": str(item.get("name") or cid),
                        "level": str(item.get("level") or "warn"),
                        "detail": str(item.get("detail") or ""),
                        "suggestion": str(item.get("suggestion") or ""),
                        "source": f"scripts/diagnose/health.py:{cid}",
                    }
                )

        if key == "feature":
            trade_date = str(signals.get("trade_date") or "").replace("-", "")
            if features_latest:
                # 信号日 = T+1 预测日：特征分区只可能到「上一交易日」（当日特征收盘后才产出），
                # 拿 trade_date 本身当基准会恒判滞后（2026-09-18 实测误报）；基准 = 前一交易日。
                # 日历按**页面市场**取（signals.market），跨市场节假日口径不同。
                expected = (
                    _previous_trading_ymd(trade_date, str(signals.get("market") or "CN"))
                    if trade_date
                    else None
                )
                fresh = features_latest in (trade_date, expected)
                lag = (
                    "同类交易日口径"
                    if fresh
                    else (f"滞后（期望 {expected}）" if expected else "滞后")
                )
                items.append(
                    {
                        "id": "feature_freshness",
                        "name": "特征分区新鲜度",
                        "level": "ok" if fresh else "warn",
                        "detail": f"最新特征分区 {features_latest}，信号日 {trade_date or '—'}（{lag}）",
                        "suggestion": ""
                        if fresh
                        else "检查特征计算/同步任务（滞后会断推理）",
                        "source": "fs:6_ml_datasets/features_daily/dt=*",
                    }
                )
        elif key == "model":
            row = eval_summary.get("model")
            items.append(
                {
                    "id": "model_scores",
                    "name": "模型评分留档",
                    "level": "ok" if row else "no_evidence",
                    "detail": (
                        f"模型卡 {row['count']} 个，最新 {row['latest_date']}"
                        + (f"（最差 {row['worst_grade']}）" if row.get("worst_grade") else "")
                    )
                    if row
                    else "eval_scores 无模型评分（EOD 评分任务未运行或尚无模型）",
                    "suggestion": "" if row else "运行 EOD 评分任务或先训练模型",
                    "source": "db:eval_scores(object_type=model)",
                }
            )
        elif key == "backtest":
            row = eval_summary.get("strategy_health")
            items.append(
                {
                    "id": "backtest_health",
                    "name": "回测体检留档",
                    "level": "ok" if row else "no_evidence",
                    "detail": (
                        f"体检留档 {row['count']} 份，最新 {row['latest_date']}"
                        + (f"（最新结论 {row['latest_grade']}）" if row.get("latest_grade") else "")
                    )
                    if row
                    else "尚无体检留档（回测完成后自动体检写入）",
                    "suggestion": "" if row else "跑一次策略回测即自动生成体检报告",
                    "source": "db:eval_scores(object_type=strategy_health)",
                }
            )
        elif key == "simulation":
            items.append(
                {
                    "id": "shadow_report",
                    "name": "模拟对照日报",
                    "level": "ok" if shadow.get("available") else "no_evidence",
                    "detail": (
                        f"{shadow.get('date')} 日报：成交率 {(shadow.get('fill') or {}).get('fill_rate')}"
                        if shadow.get("available")
                        else str(shadow.get("reason") or "无影子对照日报")
                    ),
                    "suggestion": ""
                    if shadow.get("available")
                    else str(
                        shadow.get("suggestion")
                        or "无真单镜像时该环天然无证据（影子需 REAL 轨迹）"
                    ),
                    "source": "redis:mirror:shadow:{date}（T-P2-06）",
                }
            )
        elif key == "execution":
            if execution.get("available") is False:
                # 委托表无市场列 → 非 CN 市场无证据可挂。**不可**退化成"0 笔委托"：
                # 那会把"归集不了"说成"今天没交易"，是另一种形式的假证据。
                items.append(
                    {
                        "id": "execution_today",
                        "name": "今日执行",
                        "level": "no_evidence",
                        "detail": str(execution.get("reason") or "该市场委托不可归集"),
                        "suggestion": "给 sim_orders 加 market 列并按下单链路写入后可归集",
                        "source": str(execution.get("source") or "desk:market-scope"),
                    }
                )
            else:
                rejected = int(execution.get("rejected") or 0)
                filled = int(execution.get("filled") or 0)
                total = int(execution.get("sim_count") or 0) + int(
                    execution.get("real_count") or 0
                )
                items.append(
                    {
                        "id": "execution_today",
                        "name": "今日执行",
                        "level": "warn" if rejected > 0 else "ok",
                        "detail": f"委托 {total} 笔（成交 {filled} / 拒单 {rejected}）"
                        + ("" if total else "——今日无委托（含盘前，正常空态）"),
                        "suggestion": "核查拒单原因（资金/涨跌停/风控）" if rejected > 0 else "",
                        "source": "db:sim_orders+orders（今日）",
                    }
                )
        elif key == "strategy":
            row = eval_summary.get("strategy")
            items.append(
                {
                    "id": "strategy_scores",
                    "name": "策略评分留档",
                    "level": "ok" if row else "no_evidence",
                    "detail": f"策略卡 {row['count']} 份，最新 {row['latest_date']}" if row else "eval_scores 无策略评分",
                    "suggestion": "" if row else "运行 EOD 评分任务（策略卡按回测曲线评分）",
                    "source": "db:eval_scores(object_type=strategy)",
                }
            )

        if not items:
            level = "no_evidence"
            summary = "该环节暂无证据源接入"
        else:
            level = max((str(i["level"]) for i in items), key=lambda lv: severity.get(lv, 0))
            summary = "；".join(str(i["detail"])[:60] for i in items if i["detail"])[:160]
        rings.append(
            {
                "key": key,
                "label": label,
                "artifact": artifact,
                "frequency": frequency,
                "level": level,
                "summary": summary,
                "items": items,
            }
        )
    return rings


async def _collect_evidence(
    *,
    health_items: dict[str, Any],
    signals: dict[str, Any],
    execution: dict[str, Any],
    shadow: dict[str, Any],
    market: str = "CN",
) -> dict[str, Any]:
    """十环证据采集（只做轻量补充查询：评估留档摘要 + 特征分区新鲜度）。"""
    eval_summary: dict[str, Any] = {}
    try:
        from sqlalchemy import text as _t2

        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    _t2(
                        "SELECT object_type, count(*) AS n, max(snapshot_date) AS latest FROM eval_scores "
                        "WHERE object_type IN ('model','strategy','strategy_health') "
                        "GROUP BY object_type"
                    )
                )
            ).mappings().all()
            latest_rows = (
                await session.execute(
                    _t2(
                        "SELECT DISTINCT ON (object_type) object_type, grade FROM eval_scores "
                        "WHERE object_type IN ('strategy_health') "
                        "ORDER BY object_type, snapshot_date DESC, created_at DESC"
                    )
                )
            ).mappings().all()
        # 模型卡"最差评级"：各模型最新一条的快照评级取最差（A<B<C<D 字典序即档位序）
        grades = [str(g) for g in await _model_grades() if g]
        summary_map: dict[str, Any] = {}
        for r in rows:
            summary_map[str(r["object_type"])] = {
                "count": int(r["n"] or 0),
                "latest_date": str(r["latest"]) if r["latest"] else None,
                "worst_grade": min(grades) if r["object_type"] == "model" and grades else None,
                "latest_grade": None,
            }
        for r in latest_rows:
            item = summary_map.setdefault(str(r["object_type"]), {"count": 0, "latest_date": None})
            item["latest_grade"] = str(r["grade"]) if r["grade"] else None
        eval_summary = summary_map
    except Exception as exc:  # noqa: BLE001 - 证据采集失败按"无证据"呈现
        logger.warning("desk evidence eval query failed: %s", exc)

    features_latest: str | None = None
    try:
        import glob as _glob
        import os as _os

        # 特征分区根按**页面市场**取（CN=quantdb，HK=quanthk，US=quantus…）：
        # 写死 QM_QUANTDB_DATA_DIR 会让港股/美股页签拿 A 股分区日期当自己的新鲜度。
        from backend.shared.training_runtime import local_market_data_root

        root_path = local_market_data_root(market)
        root = str(root_path) if root_path else (
            _os.getenv("QM_QUANTDB_DATA_DIR") or "/data/quantdb"
        )
        parts = sorted(_glob.glob(_os.path.join(root, "6_ml_datasets", "features_daily", "dt=*")))
        if parts:
            features_latest = _os.path.basename(parts[-1]).replace("dt=", "")
    except Exception:  # noqa: BLE001
        features_latest = None

    rings = build_evidence_rings(
        {
            "health_items": health_items,
            "eval_summary": eval_summary,
            "features_latest": features_latest,
            "signals": signals,
            "execution": execution,
            "shadow": shadow,
        }
    )
    no_evidence = [r["label"] for r in rings if r["level"] == "no_evidence"]
    return {
        "rings": rings,
        "no_evidence": no_evidence,
        "source": "体检断言+eval_scores 留档+对照日报+特征分区（每格可下钻原文）",
    }


async def _model_grades() -> list[Any]:
    """模型卡评级列表（判断最差档用；失败返回空）。"""
    try:
        from sqlalchemy import text as _t3

        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    _t3(
                        "SELECT DISTINCT ON (object_id) grade FROM eval_scores "
                        "WHERE object_type='model' AND grade IS NOT NULL "
                        "ORDER BY object_id, snapshot_date DESC, created_at DESC"
                    )
                )
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        return []


def parse_exclude_symbols(raw: str | None, max_items: int = 50) -> set[str]:
    """逗号分隔排除集解析（纯函数）：去空/去重/上限截断。"""
    if not raw:
        return set()
    items = [x.strip() for x in str(raw).split(",") if x.strip()]
    return set(items[:max_items])


def parse_quantity_overrides(raw: Any, max_items: int = 50) -> dict[tuple[str, str], int]:
    """人工改量载荷解析（纯函数，T-FE-05 v2）——**fail-fast，不静默丢改量**。

    接受 [{symbol, side, quantity}, ...]；任何一条不合法（缺字段/方向非法/数量非正整数/
    重复键/超上限）都抛 ValueError 由端点转 400——资金相关的人工调整不做静默降级。
    """
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("quantity_overrides 必须为数组")
    if len(raw) > max_items:
        raise ValueError(f"quantity_overrides 超过上限 {max_items} 条")
    result: dict[tuple[str, str], int] = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"quantity_overrides[{i}] 必须为对象")
        symbol = str(item.get("symbol") or "").strip()
        side = str(item.get("side") or "").strip().upper()
        if not symbol:
            raise ValueError(f"quantity_overrides[{i}] 缺少 symbol")
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"quantity_overrides[{i}] side 必须为 BUY/SELL")
        raw_qty = item.get("quantity")
        if isinstance(raw_qty, bool):  # bool 是 int 子类，显式拒绝
            raise ValueError(f"quantity_overrides[{i}] quantity 必须为整数")
        if isinstance(raw_qty, float) and not raw_qty.is_integer():
            raise ValueError(f"quantity_overrides[{i}] quantity 必须为整数（不接受小数）")
        try:
            quantity = int(raw_qty)
        except (TypeError, ValueError):
            raise ValueError(f"quantity_overrides[{i}] quantity 必须为整数") from None
        if quantity <= 0:
            raise ValueError(f"quantity_overrides[{i}] quantity 必须为正整数")
        key = (symbol, side)
        if key in result:
            raise ValueError(f"quantity_overrides 重复条目: {symbol} {side}")
        result[key] = quantity
    return result


def active_strategy_market(payload: dict[str, Any] | None) -> str:
    """活跃策略所属市场（纯函数）——委托 ``shared.active_strategy_market``。

    实现已上提为共享模块（``/real-trading/status`` 需同一口径判页签闸门），
    此处保留同名函数以兼容既有调用点，**不再保留第二份实现**。
    """
    from backend.shared.active_strategy_market import active_strategy_market as _impl

    return _impl(payload)


def _resolve_active_strategy(tenant_id: str, raw_user: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """活跃策略解析（预演/执行唯一共用）→ (payload, error_block)。"""
    source = "redis:trade:active_strategy"
    try:
        from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
        from backend.shared.simulation_account_keys import active_strategy_key

        client = get_trade_redis()
        if getattr(client, "client", None) is None:
            client.connect()
        raw = client.client.get(active_strategy_key(tenant_id, raw_user))
        payload = json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 - 读取失败不拖垮调用方
        return None, {"available": False, "reason": f"活跃策略读取失败: {exc}", "source": source}
    if not isinstance(payload, dict) or not payload:
        return None, {
            "available": False,
            "reason": "当前无活跃策略——在实盘/模拟页启动策略后，此处显示当日调仓计划预演",
            "source": source,
        }
    return payload, None


async def _collect_plan(
    tenant_id: str,
    raw_user: str,
    exclude_symbols: set[str] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    """调仓计划预演卡（T-FE-05）：活跃策略（Redis）→ 引擎 dry-run（唯一调仓实现）。

    纪律：只读预演——RebalanceCalculator 单一实现复用（退出规则+池过滤+风控买锁同源），
    但绝不撮合/落单/写快照；无活跃策略时如实返回不可用原因。

    市场闸门：活跃策略键无市场维度，策略市场从载荷读（``active_strategy_market``）。
    页面市场 ≠ 策略市场时如实说明，不拿别市场的计划充数。

    ``market=None`` = 调用方**未声明**页签市场（「手动任务」页签等旧调用点）→ 不设闸门，
    保持改造前行为。闸门必须由"声明"触发而不是靠默认值：缺省成 CN 会把港股活跃策略
    在港股页签上误判成"与页签（CN）不一致"，并连带隐藏一键执行按钮（回退）。
    """
    source = "redis:trade:active_strategy → simulation engine dry-run"
    payload, error_block = _resolve_active_strategy(tenant_id, raw_user)
    strategy_market = active_strategy_market(payload) if payload is not None else None
    # 未声明市场 → 回落策略自身市场，保证返回值里的 market 描述的是这份计划
    scope_market = market if market is not None else strategy_market
    if error_block is not None:
        error_block["market"] = scope_market
        return error_block
    if market is not None and strategy_market != market:
        return {
            "available": False,
            "market": market,
            "strategy_market": strategy_market,
            "strategy_id": str(payload.get("strategy_id") or "").strip() or None,
            "reason": (
                f"当前活跃策略属于 {strategy_market} 市场，与当前页签（{market}）不一致"
                "——活跃策略按账户记一份，切市场后需在目标市场重新启动策略"
            ),
            "source": source,
        }
    strategy_id = str(payload.get("strategy_id") or "").strip()
    if not strategy_id:
        return {
            "available": False,
            "market": scope_market,
            "reason": "活跃策略未记录 strategy_id",
            "source": source,
        }
    live_cfg = payload.get("live_trade_config")
    if not isinstance(live_cfg, dict):
        live_cfg = {}

    try:
        from backend.services.simulation.services.simulation_hosted_scheduler import (
            preview_simulation_plan_for_active,
        )

        result = await preview_simulation_plan_for_active(
            tenant_id=tenant_id,
            user_id=raw_user,
            strategy_id=strategy_id,
            live_trade_config=live_cfg,
            exclude_symbols=exclude_symbols,
        )
    except Exception as exc:  # noqa: BLE001 - 预演失败降级为不可用（如实原因）
        logger.warning("desk plan preview failed: %s", exc)
        return {
            "available": False,
            "market": scope_market,
            "reason": f"计划预演失败: {exc}",
            "strategy_id": strategy_id,
            "source": source,
        }

    result.update(
        {
            "strategy_id": strategy_id,
            "strategy_name": payload.get("strategy_name"),
            "mode": str(payload.get("mode") or "SIMULATION"),
            "market": scope_market,
            "source": "simulation engine dry-run（RebalanceCalculator 单一实现，未执行）",
        }
    )
    # 中文名 enrichment（在 desk 边界做，不污染引擎预演服务）；未收录为空串
    orders = result.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if isinstance(order, dict) and order.get("symbol"):
                order["name"] = resolve_name(str(order["symbol"]))
    return result


@router.post("/plan/execute")
async def execute_plan(
    payload: dict[str, Any] | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """一键执行一轮调仓（T-FE-05，人工触发）——机构级三重闸门：

    ① 动作可用性：须有活跃策略且为 SIMULATION（REAL 走另有确认链，此处拒绝）；
    ② 防重：同一策略 60s 内只允许触发一次（Redis NX 锁，重复 → 429 附剩余秒数）；
    ③ 风控不可绕过：退出规则单不接受 exclude_symbols 排除，也**不接受人工改量**
       （quantity_overrides 对退出单一律拒改并如实记录裁定）。

    执行与托管调度共用唯一入口 run_simulation_cycle_for_active（RebalanceCalculator +
    ashare_matcher）——计划预演与真实执行天然同源。

    人工改量（T-FE-05 v2）：body.quantity_overrides = [{symbol, side, quantity}, ...]；
    载荷不合法一律 400（fail-fast，资金相关调整不静默降级）；未命中当前计划的条目
    在 report.quantity_adjustments 中如实裁定（不猜测、不补单）。
    """
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

    tenant_id = str(current_user.get("tenant_id") or "default")
    raw_user = str(current_user.get("user_id") or "")

    body = payload if isinstance(payload, dict) else {}
    exclude_raw = body.get("exclude_symbols")
    if isinstance(exclude_raw, str):
        exclude_symbols = parse_exclude_symbols(exclude_raw)
    elif isinstance(exclude_raw, list):
        exclude_symbols = parse_exclude_symbols(",".join(str(x) for x in exclude_raw))
    else:
        exclude_symbols = set()

    try:
        quantity_overrides = parse_quantity_overrides(body.get("quantity_overrides"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"人工改量载荷不合法: {exc}") from exc

    active, error_block = _resolve_active_strategy(tenant_id, raw_user)
    if error_block is not None:
        raise HTTPException(status_code=409, detail=error_block["reason"])
    strategy_id = str(active.get("strategy_id") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=409, detail="活跃策略未记录 strategy_id")
    mode = str(active.get("mode") or "SIMULATION").upper()
    if mode != "SIMULATION":
        raise HTTPException(
            status_code=409,
            detail=f"当前活跃策略为 {mode} 模式——交易台一键执行仅限模拟盘（实盘请走实盘确认链）",
        )

    # 市场闸门（仅当调用方**显式声明**市场）：页面在港股而活跃策略是 A 股时，
    # 一键执行会把 A 股计划当成港股计划执行——声明了就必须拒。
    #
    # 现状（2026-09-20）：**尚无调用方声明**——`PlanCard` 只传 exclude/quantity 两个参数，
    # 因此本闸门目前不会触发，它保护的是「将来/第三方按声明市场调用」的路径。
    # 别把它当成已经生效的防线；反过来，也正因为不声明即不触发，改造前那些不传市场的
    # 调用点行为完全不变（不会因为缺省 CN 而被误拒）。
    declared_market = str(body.get("market") or "").strip()
    if declared_market:
        from backend.shared.simulation_account_keys import normalize_market

        want = normalize_market(declared_market)
        have = active_strategy_market(active)
        if want != have:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"当前活跃策略属于 {have} 市场，与请求市场（{want}）不一致"
                    "——请先切回对应市场或在目标市场重新启动策略"
                ),
            )

    live_cfg = active.get("live_trade_config")
    if not isinstance(live_cfg, dict):
        live_cfg = {}

    # 防重锁（60s）：double-click / 并发触发一律拒绝并给出剩余时间
    try:
        client = get_trade_redis()
        if getattr(client, "client", None) is None:
            client.connect()
        lock_key = f"qm:desk:plan:execute:{tenant_id}:{raw_user}:{strategy_id}"
        acquired = client.client.set(lock_key, "1", nx=True, ex=60)
        if not acquired:
            ttl = client.client.ttl(lock_key)
            raise HTTPException(
                status_code=429,
                detail=f"刚已触发过执行（同一策略 60 秒内防重），请 {max(0, int(ttl))} 秒后再试",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Redis 不可用时拒绝执行（fail-closed）
        raise HTTPException(
            status_code=503, detail=f"防重锁不可用，为安全起见拒绝执行: {exc}"
        ) from exc

    from backend.services.simulation.services.simulation_hosted_scheduler import (
        execute_simulation_plan_for_active,
    )

    try:
        report = await execute_simulation_plan_for_active(
            tenant_id=tenant_id,
            user_id=raw_user,
            strategy_id=strategy_id,
            live_trade_config=live_cfg,
            exclude_symbols=exclude_symbols or None,
            quantity_overrides=quantity_overrides or None,
        )
    except Exception as exc:  # noqa: BLE001 - 执行失败如实返回（不吞）
        logger.error("desk plan execute failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"执行失败: {exc}") from exc

    return {
        "success": True,
        "data": {
            "strategy_id": strategy_id,
            "mode": mode,
            "excluded": sorted(exclude_symbols),
            "quantity_overrides": [
                {"symbol": symbol, "side": side, "quantity": qty}
                for (symbol, side), qty in sorted(quantity_overrides.items())
            ],
            "report": report,
            "source": "run_simulation_cycle_for_active（与托管调度同一执行入口）",
        },
    }


@router.get("/today")
async def desk_today(
    health: bool = Query(True, description="是否运行体检（10 项断言，约 1-2s）"),
    plan: bool = Query(True, description="是否运行调仓计划预演（dry-run 引擎，约 1-3s）"),
    exclude: str | None = Query(None, description="人工排除标的（逗号分隔；退出规则单不受影响）"),
    market: str | None = Query(
        None,
        description=(
            "页签市场（CN/HK/US/FUTURES/CRYPTO）。**不传 = 旧行为按 CN 取数且不设计划闸门**；"
            "传了才启用「页面市场 vs 活跃策略市场」闸门"
        ),
    ),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """今日交易台聚合：管线/信号/计划预演/执行/盈亏/影子对照/健康——每个数字带 source 下钻字段。

    **市场维度**：页签市场 = 前端顶栏市场，随 `?market=` 传入。信号/盈亏按市场列过滤，
    计划按活跃策略市场闸门，证据矩阵特征分区按市场根目录、交易日历按市场取；
    委托类块（执行/保真度）因 `sim_orders` 无 market 列，非 CN 市场如实返回不可用
    （见 ``_orders_market_unsupported``）——绝不把 A 股数字当成别的市场的。

    **不传 `market` 与传 `market=CN` 不等价**：前者是「调用方未声明页签市场」（旧调用点），
    数据仍按 CN 取（保持改造前行为），但**不设**计划闸门；后者是显式声明，闸门生效。
    若把缺省当 CN 用，港股活跃策略在港股页签上会被误判为「与页签（CN）不一致」。
    """
    from backend.services.trade_shared.simulation_manager import require_sim_user_id
    from backend.shared.simulation_account_keys import normalize_market

    tenant_id = str(current_user.get("tenant_id") or "default")
    raw_user = str(current_user.get("user_id") or "")
    sim_uid = require_sim_user_id(raw_user, tenant_id=tenant_id)
    declared_market = normalize_market(market) if market else None
    # 数据面按市场取数：未声明 → CN（改造前行为）；声明了 → 按声明
    market_norm = declared_market or "CN"

    health_items = await _run_health(market_norm) if health else {}
    signals, execution, pnl, shadow, plan_block, fidelity = await asyncio.gather(
        _collect_signals(tenant_id, market_norm),
        _collect_execution(tenant_id, int(sim_uid), raw_user, market_norm),
        _collect_pnl(tenant_id, str(sim_uid), market_norm),
        _collect_shadow(market_norm),
        _collect_plan(
            tenant_id, raw_user, parse_exclude_symbols(exclude), declared_market
        )
        if plan
        else _async_unavailable("调仓计划预演已跳过（?plan=false）"),
        _collect_fidelity(tenant_id, int(sim_uid), market_norm),
    )
    health_summary = {
        "ok": sum(1 for i in health_items.values() if i.get("level") == "ok"),
        "warn": sum(1 for i in health_items.values() if i.get("level") == "warn"),
        "fail": sum(1 for i in health_items.values() if i.get("level") == "fail"),
        "items": list(health_items.values()),
        "source": "scripts/diagnose/health.py（与体检命令行同源）",
    }
    return {
        "success": True,
        "data": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "user_id": raw_user,
            "sim_user_id": str(sim_uid),
            "market": market_norm,
            "pipeline": build_pipeline(health_items),
            "signals": signals,
            "plan": plan_block,
            "execution": execution,
            "pnl": pnl,
            "shadow": shadow,
            "fidelity": fidelity,
            "health": health_summary,
            "evidence": await _collect_evidence(
                health_items=health_items,
                signals=signals,
                execution=execution,
                shadow=shadow,
                market=market_norm,
            ),
        },
    }
