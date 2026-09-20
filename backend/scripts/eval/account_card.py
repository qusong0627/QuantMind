"""账户评分卡（T-P4-05b-2，设计 §2.5）：模拟账户 → 四维评分（v1 权重 30/20/25/25）。

数据源（实测在盘）：
- 持仓：Redis `simulation:account:{tenant}:{user}[:MARKET]`（经 SimulationAccountManager
  读取，缓存缺失时由 PG 台账自愈重建），positions{symbol:{market_value,price,cost,volume}}；
- 净值序列：PG `simulation_fund_snapshots`（日度 total_asset/market_value，键形探测同影子对照）；
- 风控事件：PG `risk_events`（**真实状态词表**：skipped_no_quote/no_targets/…，
  分类与评分见 `scripts/eval/risk_events.py`）+ `sim_orders` status='rejected'（拒单）；
- 基准：index_daily 沪深300（窗口首尾对齐，同策略卡口径）。

四维（v1 权重 30/20/25/25）：
- 暴露 30：前 5 持仓集中度 + 单票占比（红线 >50%）+ 行业集中度（申万映射；
  未映射占比 >50% → 行业子项缺省）；净敞口入 detail；
- 归因 20：**v1 简化口径**（净值窗口超额 + 盈利贡献集中度），Brinson 三段分解待基准
  行业权重接线（detail 如实标注）；
- 风控事件 25：执行失败罚分（failed/rejected/alert 计数）+ **盲区占比**
  （skipped_no_quote = 有持仓但取不到行情 → 判不了止损），两条红线
  （失败 ≥3 / 盲区 ≥90%）；风控机制从未启用（无事件且无活跃规则）→ 维度缺省
  （不假评"无事件=满分"）；
- 资金效率 25：现金拖累（cash/total_asset 阶梯映射）。

空账户（无持仓且无任何委托记录）→ 不评分（error 说明），避免污染评级分布。

用法：python backend/scripts/eval/account_card.py --user 1 [--market CN] [--save] [--json]
      python backend/scripts/eval/account_card.py --all --save     # 扫描全部在册账户
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.benchmark import BENCHMARK_SYMBOL  # noqa: E402
from backend.scripts.eval.account_series import account_series_payload  # noqa: E402
from backend.scripts.eval.risk_events import (  # noqa: E402
    load_risk_summary,
    score_risk_events,
)
from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
    score_from_thresholds,
)
from backend.shared.eval_series import is_series_id, save_series  # noqa: E402

WEIGHTS = {
    "exposure": 30.0,
    "attribution": 20.0,
    "risk_events": 25.0,
    "capital_efficiency": 25.0,
}
BENCHMARK = BENCHMARK_SYMBOL
DEFAULT_WINDOW_DAYS = 30


# ── 纯函数（可单测）─────────────────────────────────────────────────


def position_stats(positions: dict | None) -> dict[str, Any]:
    """持仓统计：市值>0 的持仓 → 笔数/总市值/前5占比/单票最大占比。"""
    vals: list[tuple[str, float]] = []
    for sym, pos in (positions or {}).items():
        try:
            mv = float((pos or {}).get("market_value") or 0.0)
        except (TypeError, ValueError):
            mv = 0.0
        if mv > 0:
            vals.append((str(sym), mv))
    vals.sort(key=lambda x: -x[1])
    total = sum(v for _, v in vals)
    if not vals or total <= 0:
        return {
            "n": 0,
            "total_market_value": 0.0,
            "top5_share": None,
            "max_share": None,
            "items": [],
        }
    shares = [v / total for _, v in vals]
    return {
        "n": len(vals),
        "total_market_value": round(total, 2),
        "top5_share": round(sum(shares[:5]), 4),
        "max_share": round(shares[0], 4),
        "items": vals,
    }


def industry_breakdown(
    positions: dict | None, industry_map: dict[str, str] | None
) -> dict[str, Any] | None:
    """行业市值占比（申万映射；未命中归 '未映射'）。无持仓/无映射 → None。"""
    if not industry_map:
        return None
    stats = position_stats(positions)
    if stats["n"] == 0:
        return None
    by_industry: dict[str, float] = {}
    unmapped_mv = 0.0
    total = sum(v for _, v in stats["items"])
    for sym, mv in stats["items"]:
        industry = industry_map.get(sym) or industry_map.get(sym.upper())
        if not industry:
            industry = "未映射"
            unmapped_mv += mv
        by_industry[industry] = by_industry.get(industry, 0.0) + mv
    return {
        "max_industry": max(by_industry, key=by_industry.get),
        "max_industry_share": round(max(by_industry.values()) / total, 4),
        "n_industries": len(by_industry),
        "unmapped_ratio": round(unmapped_mv / total, 4),
    }


def score_exposure(
    positions: dict | None, total_asset: float, industry_map: dict[str, str] | None
) -> DimensionScore:
    """风险暴露：集中度（前5）+ 单票上限（红线 >50%）+ 行业集中度。"""
    stats = position_stats(positions)
    if stats["n"] == 0:
        return DimensionScore(
            "exposure",
            "风险暴露",
            WEIGHTS["exposure"],
            None,
            False,
            {"insufficient": True, "note": "无持仓（空仓期不评暴露）"},
        )
    concentration_score = score_from_thresholds(
        stats["top5_share"],
        [(0.2, 100.0), (0.4, 85.0), (0.6, 65.0), (0.8, 40.0), (1.0, 0.0)],
    )
    single_score = score_from_thresholds(
        stats["max_share"],
        [(0.1, 100.0), (0.2, 85.0), (0.3, 60.0), (0.5, 25.0), (1.0, 0.0)],
    )
    industry = industry_breakdown(positions, industry_map)
    industry_score = None
    if industry and industry["unmapped_ratio"] <= 0.5:
        industry_score = score_from_thresholds(
            industry["max_industry_share"],
            [(0.2, 100.0), (0.4, 85.0), (0.6, 60.0), (0.8, 30.0), (1.0, 0.0)],
        )
    parts = [
        (concentration_score, 0.5),
        (single_score, 0.3),
        (industry_score, 0.2 if industry_score is not None else 0.0),
    ]
    available = [(s, w) for s, w in parts if s is not None and w > 0]
    total = (
        round(sum(s * w for s, w in available) / sum(w for _, w in available), 2)
        if available
        else None
    )
    red = bool(stats["max_share"] is not None and stats["max_share"] > 0.5)
    detail: dict[str, Any] = {
        "n_positions": stats["n"],
        "total_market_value": stats["total_market_value"],
        "top5_share": stats["top5_share"],
        "max_share": stats["max_share"],
        "concentration_score": concentration_score,
        "single_score": single_score,
        "industry": industry,
        "industry_score": industry_score,
        "net_exposure": (
            round(stats["total_market_value"] / total_asset, 4)
            if total_asset and total_asset > 0
            else None
        ),
        "top3": [
            {"symbol": s, "share": round(v / stats["total_market_value"], 4)}
            for s, v in stats["items"][:3]
        ],
    }
    if red:
        detail["red_line"] = "单票 >50%"
    return DimensionScore(
        "exposure", "风险暴露", WEIGHTS["exposure"], total, red, detail
    )


def window_return(series: list[tuple[str, float]]) -> float | None:
    """首尾净值窗口收益（跳过非正净值）；<2 个有效点 → None。"""
    vals = [float(v) for _, v in series if v is not None and float(v) > 0]
    if len(vals) < 2 or vals[0] <= 0:
        return None
    return float(vals[-1] / vals[0] - 1.0)


def contribution_share(pnl_items: list[tuple[str, float]] | None) -> float | None:
    """盈利贡献集中度：最大单票盈利 / 全部盈利之和；无盈利 → None。"""
    gains = [float(p) for _, p in (pnl_items or []) if p is not None and float(p) > 0]
    total = sum(gains)
    if not gains or total <= 0:
        return None
    return float(max(gains) / total)


def score_attribution(
    account_series: list[tuple[str, float]],
    bench_series: list[tuple[str, float]],
    pnl_items: list[tuple[str, float]] | None,
) -> DimensionScore:
    """归因（v1 简化）：净值窗口超额 0.6 + 盈利贡献集中度 0.4——Brinson 待接线。"""
    acc_ret = window_return(account_series)
    bench_ret = window_return(bench_series)
    if acc_ret is None or bench_ret is None:
        return DimensionScore(
            "attribution",
            "归因",
            WEIGHTS["attribution"],
            None,
            False,
            {
                "insufficient": True,
                "note": f"净值/Benchmark 样本不足（账户 {len(account_series)} 点，基准 {len(bench_series)} 点）",
            },
        )
    excess = acc_ret - bench_ret
    excess_score = score_from_thresholds(
        excess, [(-0.08, 0.0), (-0.02, 35.0), (0.0, 50.0), (0.03, 80.0), (0.08, 100.0)]
    )
    cshare = contribution_share(pnl_items)
    contribution_score = score_from_thresholds(
        cshare, [(0.15, 100.0), (0.3, 85.0), (0.5, 65.0), (0.7, 35.0), (1.0, 0.0)]
    )
    parts = [
        (excess_score, 0.6),
        (contribution_score, 0.4 if contribution_score is not None else 0.0),
    ]
    available = [(s, w) for s, w in parts if s is not None and w > 0]
    total = round(sum(s * w for s, w in available) / sum(w for _, w in available), 2)
    return DimensionScore(
        "attribution",
        "归因",
        WEIGHTS["attribution"],
        total,
        False,
        {
            "account_return": round(acc_ret, 6),
            "benchmark_return": round(bench_ret, 6),
            "excess": round(excess, 6),
            "excess_score": excess_score,
            "top1_contribution_share": round(cshare, 4) if cshare is not None else None,
            "contribution_score": contribution_score,
            "window_points": len(account_series),
            "note": "v1 简化口径：净值超额 + 盈利贡献集中度（Brinson 三段分解待基准行业权重接线）",
        },
    )


def score_capital_efficiency(
    total_asset: float | None, cash: float | None, market_value: float | None
) -> DimensionScore:
    """资金效率：现金拖累（cash/total_asset 越低越好）。"""
    if not total_asset or float(total_asset) <= 0:
        return DimensionScore(
            "capital_efficiency",
            "资金效率",
            WEIGHTS["capital_efficiency"],
            None,
            False,
            {"insufficient": True, "note": "总资产缺失或 ≤0"},
        )
    total = float(total_asset)
    if cash is None:
        cash_est = total - float(market_value or 0.0)
    else:
        cash_est = float(cash)
    cash_ratio = min(1.0, max(0.0, cash_est / total))
    score = score_from_thresholds(
        cash_ratio, [(0.03, 100.0), (0.15, 90.0), (0.35, 65.0), (0.6, 30.0), (1.0, 0.0)]
    )
    return DimensionScore(
        "capital_efficiency",
        "资金效率",
        WEIGHTS["capital_efficiency"],
        score,
        False,
        {
            "total_asset": round(total, 2),
            "market_value": round(float(market_value or 0.0), 2),
            "cash": round(cash_est, 2),
            "cash_ratio": round(cash_ratio, 4),
            "utilization": round(1.0 - cash_ratio, 4),
        },
    )


# ── IO / 编排 ───────────────────────────────────────────────────────


def uid_forms(raw_user: str | None) -> list[str]:
    """user_id 候选键形（纯函数）：原文优先、补零 8 位兜底（双 ID 存量的既成纪律）。"""
    text = str(raw_user or "").strip()
    if not text:
        return []
    if not text.isdigit():
        return [text]
    forms: list[str] = []
    for form in (text, str(int(text)), text.zfill(8)):
        if form not in forms:
            forms.append(form)
    return forms


def _trade_redis_client():
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

    client = get_trade_redis()
    if getattr(client, "client", None) is None:
        client.connect()
    if getattr(client, "client", None) is None:
        raise RuntimeError("Redis 不可用（trade 库）")
    return client


def list_accounts(client) -> list[dict[str, Any]]:
    """扫描 Redis 在册模拟账户 → [{tenant,user,market,key}]（按 key 排序去重）。"""
    from backend.shared.simulation_account_keys import parse_account_key

    raw = getattr(client, "client", client)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in raw.scan_iter("simulation:account:*", count=200):
        ks = key.decode() if isinstance(key, bytes) else str(key)
        parsed = parse_account_key(ks)
        if not parsed or ks in seen:
            continue
        seen.add(ks)
        tenant, user, market = parsed
        out.append({"tenant": tenant, "user": user, "market": market, "key": ks})
    out.sort(key=lambda a: a["key"])
    return out


async def load_fund_snapshot_rows(
    tenant: str, user_raw: str, *, market: str = "CN", days: int = 90
) -> list[dict[str, Any]]:
    """模拟盘日度快照行（键形探测取第一个有数据的；ISO 日期升序）。

    返回 ``[{date, total_asset, today_pnl}]``——长序列侧车与净值序列共用这一份
    取数，避免两处 SQL 各写一遍、口径悄悄分叉。

    **市场口径**：快照表带 market 列时按市场取行（``FUTURES``/``ALL`` 各自成序列，
    不混）；v1 形态无列 → 只有 CN 有数据，非 CN 直接返回空，绝不拿 CN 行顶替。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    since = date.today() - timedelta(days=max(7, int(days)))
    from backend.shared.fund_snapshot_contract import (
        fund_snapshot_has_market_column_async,
    )

    if await fund_snapshot_has_market_column_async():
        market_clause = "AND market = :m "
        extra_params: dict[str, Any] = {"m": str(market or "CN").upper()}
    elif str(market or "CN").upper() != "CN":
        # 无市场列 = 只有 CN 的账有序列（v1）：非 CN 不误读 CN 行
        return []
    else:
        market_clause = ""
        extra_params = {}
    async with get_session(read_only=True) as session:
        for form in uid_forms(user_raw):
            rows = (
                await session.execute(
                    _text(
                        "SELECT snapshot_date, total_asset, today_pnl "
                        "FROM simulation_fund_snapshots "
                        "WHERE tenant_id = :t AND user_id = :u "
                        f"{market_clause}AND snapshot_date >= :since "
                        "ORDER BY snapshot_date"
                    ),
                    {"t": tenant, "u": form, "since": since, **extra_params},
                )
            ).fetchall()
            if rows:
                return [
                    {
                        "date": r[0].isoformat(),
                        # 净值为空的行退回 0 会画成「账户归零」：留 None 让上层剔除并计数
                        "total_asset": float(r[1]) if r[1] is not None else None,
                        "today_pnl": float(r[2]) if r[2] is not None else None,
                    }
                    for r in rows
                ]
    return []


async def load_fund_series(
    tenant: str, user_raw: str, *, days: int = 90
) -> list[tuple[str, float]]:
    """模拟盘日度**净值**序列（CN 口径，ISO 日期升序）；净值为空的行跳过。

    CN-only 是评分侧的口径（归因维的基准是沪深300）：其它市场各有自己的基准，
    要按市场取序列请直接用 :func:`load_fund_snapshot_rows`。
    """
    rows = await load_fund_snapshot_rows(tenant, user_raw, market="CN", days=days)
    return [
        (str(r["date"]), float(r["total_asset"]))
        for r in rows
        if r.get("total_asset") is not None
    ]


def benchmark_probe_start(start_iso: str, *, lookback_days: int = 12) -> int:
    """基准取数起点（YYYYMMDD int）：按**日期运算**回看（覆盖长假）。

    反例（曾经的真 bug）：整数 YYYYMMDD 直接减 12 → 20260911-12=20260899（日 99），
    fetch_series 内 ``date()`` 直接抛 "day is out of range for month"。
    """
    probe = date.fromisoformat(str(start_iso)[:10]) - timedelta(
        days=max(0, int(lookback_days))
    )
    return int(probe.strftime("%Y%m%d"))


def load_benchmark_closes(start_iso: str, end_iso: str) -> list[tuple[str, float]]:
    """沪深300 收盘（含 start 前最近一个交易日作基准起点）→ [(ISO, close)] 升序。"""

    def _to_dt(iso: str) -> int:
        return int(str(iso).replace("-", "")[:8])

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    df = hub.fetch_series(
        "qdb_index_daily",
        BENCHMARK,
        benchmark_probe_start(start_iso),
        _to_dt(end_iso),
        columns=["close"],
    )
    if df is None or len(df) == 0:
        return []
    df = df.sort_values("dt")
    out = [
        (
            f"{int(d) // 10000:04d}-{int(d) // 100 % 100:02d}-{int(d) % 100:02d}",
            float(c),
        )
        for d, c in zip(df["dt"], df["close"], strict=False)
        if c is not None and float(c) > 0
    ]
    start_dt, end_dt = _to_dt(start_iso), _to_dt(end_iso)
    base = [x for x in out if _to_dt(x[0]) <= start_dt]
    window = [x for x in out if start_dt < _to_dt(x[0]) <= end_dt]
    if not base:
        return window
    return [base[-1], *window]


async def _has_trade_history(tenant: str, user_raw: str) -> bool:
    """台账委托历史（PG user_id 为 int）：**仅规范键形**可判定。

    非规范键形（如 00000001）在台账侧的身份有歧义（int 归一会借位到 1 的委托），
    宁可不判（返回 False）也不串号。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    uid = str(user_raw or "").strip()
    if not uid.isdigit() or str(int(uid)) != uid:
        return False
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                _text(
                    "SELECT count(*) FROM sim_orders WHERE tenant_id = :t AND user_id = :u"
                ),
                {"t": tenant, "u": int(uid)},
            )
        ).scalar() or 0
    return int(row) > 0


def _pnl_items(positions: dict | None) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for sym, pos in (positions or {}).items():
        try:
            price = float((pos or {}).get("price") or 0.0)
            cost = float((pos or {}).get("cost") or 0.0)
            volume = float((pos or {}).get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if volume > 0 and price > 0 and cost > 0:
            items.append((str(sym), (price - cost) * volume))
    return items


def _read_raw_account(
    client, tenant: str, user: str, market: str
) -> dict[str, Any] | None:
    """按**原始键后缀**直读账户（不做 int 归一——双 ID 存量键各自成账户）。"""
    from backend.shared.simulation_account_keys import account_key
    from backend.shared.trade_account_cache import read_json_cache

    return read_json_cache(client, account_key(tenant, user, market))


def save_account_sidecar(
    rows: list[dict[str, Any]],
    *,
    market: str,
    combined: dict[str, Any],
    object_id: str,
    live_total_asset: float | None = None,
    n_positions: int | None = None,
) -> dict[str, Any]:
    """模拟盘快照行 → 长序列侧车（``data/eval_series/account/<id>.json``，设计 §1.6）。

    **账户序列天生薄**（实测 5~10 个交易日），所以样本量说明由载荷写进 ``notes``，
    详情页要照着显示——5 个点不是趋势。写失败如实留痕，不静默丢图。
    """
    if not is_series_id(object_id):
        return {
            "object_id": object_id,
            "written": False,
            "bytes": 0,
            "note": f"object_id 不能作为侧车文件名（{object_id!r}），未写长序列",
        }
    payload = account_series_payload(
        rows,
        market=market,
        scalars={
            "score": combined.get("score"),
            "grade": combined.get("grade"),
            # 账户卡自己的数（图上标量与卡上分同源）；live 是 Redis 实时总资产，
            # 与快照末值不同属正常（快照每日一次），分开命名免混淆
            "n_positions": n_positions,
            "live_total_asset": live_total_asset,
        },
    )
    sidecar = save_series("account", object_id, payload)
    return {
        "object_id": object_id,
        "written": sidecar["written"],
        "bytes": sidecar["bytes"],
        "note": sidecar["note"],
    }


async def score_account(
    user: str,
    market: str = "CN",
    *,
    tenant: str = "default",
    window_days: int = DEFAULT_WINDOW_DAYS,
    save: bool = False,
) -> dict[str, Any]:
    """单账户四维评分：Redis 持仓 + PG 净值序列 + 风控事件 + 基准。"""
    object_id = f"{user}:{market}"
    client = _trade_redis_client()
    uid = str(user or "").strip()
    account = _read_raw_account(client, tenant, user, market)
    if account is None and uid.isdigit() and str(int(uid)) == uid:
        # 缓存缺失 → 台账自愈重建（仅规范键形：00000001 经 int 归一读的是 1 的键，会串号）
        from backend.services.trade_shared.simulation_manager import (
            SimulationAccountManager,
        )

        account = await SimulationAccountManager(client).get_account(
            int(uid), tenant_id=tenant, market=market
        )
    if not account:
        return {
            "object_type": "account",
            "object_id": object_id,
            "skipped": True,
            "error": f"模拟账户不存在或未创建（{tenant}:{user}:{market}）",
        }

    positions = dict(account.get("positions") or {})
    total_asset = float(account.get("total_asset") or 0.0)
    market_value = float(account.get("market_value") or 0.0)
    cash = account.get("available_cash")
    if cash is None:
        cash = account.get("cash")

    if not positions and not await _has_trade_history(tenant, user):
        return {
            "object_type": "account",
            "object_id": object_id,
            "skipped": True,
            "error": "空账户（无持仓、无委托历史），跳过评分",
        }

    # 归因维的口径仍是 CN-only（基准=沪深300）；非 CN 市场不误用 CN 序列
    series: list[tuple[str, float]] = []
    bench: list[tuple[str, float]] = []
    if str(market).upper() == "CN":
        series = await load_fund_series(tenant, user, days=max(90, window_days * 3))
        if len(series) >= 3:
            bench = load_benchmark_closes(series[0][0], series[-1][0])
    # 长序列侧车按**本市场**取行（快照表带 market 列时 FUTURES/ALL 各有自己的序列）
    snapshot_rows = await load_fund_snapshot_rows(
        tenant, user, market=market, days=max(90, window_days * 3)
    )

    industry_map: dict[str, str] = {}
    try:
        from backend.services.engine.inference.shenwan_industry import (
            load_shenwan_industry_map,
        )

        industry_map = load_shenwan_industry_map()
    except Exception:  # noqa: BLE001 - 行业映射缺失只降级行业子项
        industry_map = {}

    risk_summary = await load_risk_summary(tenant, user, days=window_days)

    if str(market).upper() == "CN":
        attribution = score_attribution(series, bench, _pnl_items(positions))
    else:
        attribution = DimensionScore(
            "attribution",
            "归因",
            WEIGHTS["attribution"],
            None,
            False,
            {
                "insufficient": True,
                "note": f"非 CN 市场（{market}）净值序列未按市场分表（v1 缺省，不误用 CN 序列）",
            },
        )

    dims = [
        score_exposure(positions, total_asset, industry_map or None),
        attribution,
        score_risk_events(risk_summary),
        score_capital_efficiency(
            total_asset, float(cash) if cash is not None else None, market_value
        ),
    ]
    combined = combine_dimension_scores(dims)
    sidecar = save_account_sidecar(
        snapshot_rows,
        market=market,
        combined=combined,
        object_id=object_id,
        live_total_asset=round(total_asset, 2),
        n_positions=len(positions),
    )
    result: dict[str, Any] = {
        "object_type": "account",
        "object_id": object_id,
        "tenant": tenant,
        "user": user,
        "market": market,
        "total_asset": round(total_asset, 2),
        "n_positions": len(positions),
        "window": [series[0][0], series[-1][0]] if series else None,
        "snapshot_window_days": window_days,
        "inputs_version": {
            "weights": WEIGHTS,
            "benchmark": BENCHMARK,
            "industry_map": bool(industry_map),
            "position_source": "simulation_account",
            # 长序列走 `data/eval_series/account/<用户>%3A<市场>.json`（§1.6）：
            # 快照只有个位数天，写失败/样本少都由 note 如实带出
            "series_sidecar": sidecar,
        },
        **combined,
    }
    if save and combined.get("score") is not None:
        from backend.shared.eval_contract import save_eval_score

        await save_eval_score(
            object_type="account",
            object_id=object_id,
            snapshot_date=date.today(),
            score=combined.get("score"),
            grade=combined.get("grade"),
            low_confidence=bool(combined.get("low_confidence")),
            red_line_failed=combined.get("red_line_failed") or [],
            dimensions=combined.get("dimensions") or {},
            inputs_version=result["inputs_version"],
            tenant_id=tenant,
        )
        result["saved"] = True
    return result


async def score_all_accounts(
    *,
    tenant: str = "default",
    window_days: int = DEFAULT_WINDOW_DAYS,
    save: bool = False,
) -> list[dict[str, Any]]:
    """扫描全部在册账户逐一评分（单账户异常隔离，不拖垮其余）。"""
    try:
        client = _trade_redis_client()
        accounts = [a for a in list_accounts(client) if a["tenant"] == tenant]
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "object_type": "account",
                "object_id": "*",
                "error": f"Redis 不可用: {exc}",
            }
        ]
    results: list[dict[str, Any]] = []
    for acc in accounts:
        try:
            results.append(
                await score_account(
                    acc["user"],
                    acc["market"],
                    tenant=acc["tenant"],
                    window_days=window_days,
                    save=save,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单账户隔离
            results.append(
                {
                    "object_type": "account",
                    "object_id": f"{acc['user']}:{acc['market']}",
                    "error": f"评分失败: {exc}",
                }
            )
    return results


def render_card(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"账户卡 {result.get('object_id')}：{result['error']}"
    lines = [
        f"账户评分卡 {result['object_id']}：{result.get('score')} 分 ｜评级 {result.get('grade')}"
        f"（总资产 {result.get('total_asset')}，持仓 {result.get('n_positions')} 只）"
        + ("（低置信 †）" if result.get("low_confidence") else ""),
        "─" * 46,
    ]
    for key, dim in (result.get("dimensions") or {}).items():
        score = dim.get("score")
        lines.append(
            f"{dim.get('label')}({key}): {score if score is not None else '缺省'} × {dim.get('weight')}"
            + ("  ⚠红线" if dim.get("red_line_failed") else "")
        )
    if result.get("missing_dims"):
        lines.append(f"缺省维度（权重归一）: {result['missing_dims']}")
    return "\n".join(lines)


async def _main_async(args) -> list[dict[str, Any]]:
    if args.all:
        return await score_all_accounts(
            tenant=args.tenant, window_days=args.window, save=args.save
        )
    return [
        await score_account(
            args.user,
            args.market,
            tenant=args.tenant,
            window_days=args.window,
            save=args.save,
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="账户评分卡（T-P4-05b）")
    parser.add_argument("--user", default="1", help="模拟账户 user 后缀")
    parser.add_argument("--market", default="CN")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--all", action="store_true", help="扫描全部在册账户")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS, help="风控事件窗口天数"
    )
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    async def _run():
        from backend.shared.database_manager_v2 import close_database

        try:
            return await _main_async(args)
        finally:
            await close_database()

    results = asyncio.run(_run())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(render_card(r))
    return 0 if any(not r.get("error") for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
