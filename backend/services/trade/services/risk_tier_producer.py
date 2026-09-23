"""风险档位定档（P1.8 生产者）：波动 / 回撤 / 情绪 → 当日档位 → ``qm:risk:tier``。

判定内核在 ``backend/shared/risk/tiers.py``（纯函数）；本模块只做**取数与落盘**：

    gather 三项输入 → decide_level（纯判定） → resolve_level（同日只收紧防抖） → save_tier

为什么必须有生产者：档位读侧（闸门）在"从未定档"时是 ``absent``（不覆盖任何参数），
在"档位过期"时**买入侧回退防守**。没有生产者时这两种姿态永远存在——层布防了但不生效；
有了生产者，档位才真正开始按当日市场状态收紧买入侧（``TARGETS`` 四个键）。

三项输入与隔壁 ``quant-Trader/scripts/risk_budget_agent.py`` 一一对应，取不到的
一律 ``None``（**绝不拿 0 顶替**——``decide_level`` 的 fail-safe 会把缺失翻译成
"至少谨慎 / 降级防守"，正是要这个方向）：

===========  ==================================  ==========================================
输入          隔壁口径                             本仓口径
===========  ==================================  ==========================================
vol20        上证近 20 日日收益标准差（%）          QuantDB ``index_daily`` 000001.SH，回退 000300.SH
drawdown20   分账净值近 20 日最大回撤（%）          真账户**日度台账** ``real_account_ledger_daily_snapshots``
limit_up     同花顺 Fuyao 涨停池家数                全市场涨停统计 ``market_breadth_stats``（最近交易日）
===========  ==================================  ==========================================

时点：交易日北京 09:10（与隔壁 cron 同点）。此刻当日尚未开盘，三项输入都是**上一交易日**的
事实——与隔壁盘前读"最近一次盘中记录"同义。

纪律（三条，都与既有风控同向）：

1. **取数失败 = None，不是 0**：三项里缺 1 项 → 至少谨慎，缺 ≥2 项 → 防守（``decide_level``）；
2. **同一自然日只收紧不放宽**：``resolve_level`` 按当前档位文档的 ``date`` 判防抖，
   状态恢复要等隔日——防的是一天之内在数据抖动上反复换档；
3. **日键只在成功后置位**：worker 失败会**删掉**日键下轮重试（置位在前、失败删除），
   否则一次 Redis/PG 抖动会把当天的档位永久留空。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.services.trade.services.real_account_ledger_service import account_family

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

#: 定档时点（交易日北京 09:10 之后每 300s 轮询；与隔壁 ``risk_budget_agent`` cron 同点）
DECIDE_HHMM: tuple[int, int] = (9, 10)
WORK_INTERVAL_S = 300

#: 日键（兼执行中锁：NX 抢到才干活，与档位同库：DB2 交易——运维一条 redis-cli 能看到全部档位状态）
DONE_KEY_PREFIX = "qm:risk:tier:done:"
DONE_TTL_S = 4 * 86400

#: 指数窗口：21 个收盘 → 20 个日收益（与隔壁 ``closes[-21:]`` 同）
VOL_CLOSES = 21
VOL_MIN_RETURNS = 10  # 少于 10 个日收益算不出波动（隔壁同阈值）
INDEX_SYMBOLS: tuple[str, ...] = ("000001.SH", "000300.SH")
INDEX_LOOKBACK_DAYS = 40

#: 回撤窗口：近 20 个日度权益点；少于 5 个点不算（隔壁同阈值）
DRAWDOWN_WINDOW = 20
DRAWDOWN_MIN_POINTS = 5
LEDGER_LOOKBACK_DAYS = 45


# ── 纯计算（可单测）───────────────────────────────────────────────────


def compute_vol20(closes: Sequence[float | None]) -> float | None:
    """近 20 个日收益率的标准差（%）——与隔壁逐式一致（**总体**标准差，非样本）。

    取最后 ``VOL_CLOSES`` 个收盘；不足 ``VOL_MIN_RETURNS + 1`` 个值 → None。
    """
    vals = [float(c) for c in closes if c is not None and float(c) > 0]
    vals = vals[-VOL_CLOSES:]
    if len(vals) < VOL_MIN_RETURNS + 1:
        return None
    rets = [(vals[i] / vals[i - 1] - 1) * 100 for i in range(1, len(vals))]
    mean = sum(rets) / len(rets)
    sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
    return round(sd, 2)


def max_drawdown_pct(values: Sequence[float | None]) -> float | None:
    """窗口内 ``(峰 − 谷) / 峰 × 100``——与隔壁同式（**窗口极差**口径，非滚动回撤）。

    取最后 ``DRAWDOWN_WINDOW`` 个值；不足 ``DRAWDOWN_MIN_POINTS`` 个 → None；
    峰 ≤ 0 → None（不可能的分母，不猜）。
    """
    vals = [float(v) for v in values if v is not None and float(v) > 0]
    vals = vals[-DRAWDOWN_WINDOW:]
    if len(vals) < DRAWDOWN_MIN_POINTS:
        return None
    peak = max(vals)
    if peak <= 0:
        return None
    return round((peak - min(vals)) / peak * 100, 2)


def merge_ledger_rows(
    rows: Sequence[tuple[str, date, float]],
) -> tuple[str, list[tuple[date, float]]]:
    """日度台账行 → (选中的账本家族, 按日升序的权益序列)。

    ``rows`` 是 ``(account_id, snapshot_date, total_asset)``。为什么不能直接把所有
    账户按日求和：同一 user 名下并存多座账本（实测 ``tdx-default-*`` ≈92 万、
    ``qmt-default-*`` ≈2385 万），某些日只有其中一座有行——求和会把"另一座今天没快照"
    读成 **−96% 回撤**。故：

    1. **选一座家族**：``account_id`` 去掉末段（末段是用户标识，用户 id 规范化时
       ``tdx-default-00000001`` → ``tdx-default-10000001`` 会整体改名）；同家族按日
       合并（同日多行取**较晚快照**——改名前后的重叠日），取"日数最多、并列时最新"的家族。
    2. **剔除 ≤ 0 的权益行**：空账户/未回填在库里是 0（列 non-null default 0），
       纳入会造出 −100% 假回撤。

    返回 ``("", [])`` 表示不可用——调用方据此给 ``decide_level`` 传 None。
    """
    fams: dict[str, dict[date, tuple[date, float]]] = {}
    for account_id, snap_date, total_asset in rows:
        if not account_id or snap_date is None:
            continue
        try:
            value = float(total_asset)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        fam = account_family(account_id) or str(account_id)
        day_map = fams.setdefault(fam, {})
        # 同日多行：留较晚快照（"较晚"用日期本身做不出区分，用插入顺序不可靠——
        # 调用方已按 (snapshot_date, last_snapshot_at) 升序给出，后者覆盖前者）
        day_map[snap_date] = (snap_date, value)
    if not fams:
        return "", []
    fam = max(
        fams,
        key=lambda f: (len(fams[f]), max(fams[f])),
    )
    series = sorted(fams[fam].values(), key=lambda item: item[0])
    return fam, series


# ── 取数（三项）────────────────────────────────────────────────────────


def index_vol20(today: date | None = None) -> tuple[float | None, str]:
    """上证近 20 日波动（%）；上证不可用回退沪深300。取不到 → (None, 原因)。

    用 ``QuantDBDataHub.fetch_index_kline``（A 股 parquet 的统一入口）；**不要**用
    ``shared/benchmark.resolve_benchmark``——它按 ``MIN_ROWS=500`` 校验，21 日窗口
    会直接抛错（那是给基准序列用的，不是给短窗波动用的）。
    """
    day = today or datetime.now(tz=CST).date()
    start = day - timedelta(days=INDEX_LOOKBACK_DAYS)
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        for symbol in INDEX_SYMBOLS:
            try:
                df = hub.fetch_index_kline(symbol, start, day)
            except Exception as exc:  # noqa: BLE001 - 单个指数失败换下一个
                logger.warning("[risk-tier] 指数 %s 取数失败: %s", symbol, exc)
                continue
            if df is None or getattr(df, "empty", True):
                continue
            closes = [c for c in df["close"].tolist() if c is not None]
            vol = compute_vol20(closes)
            if vol is not None:
                return vol, symbol
    except Exception as exc:  # noqa: BLE001 - 整体失败 → None（fail-safe 由 decide_level 承接）
        logger.warning("[risk-tier] 指数波动取数失败: %s", exc)
        return None, ""
    return None, ""


async def account_drawdown20(
    today: date | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """真账户近 20 日日度权益的最大回撤（%）；返回 (值, 证据)。

    取数：``real_account_ledger_daily_snapshots``（真账户**日度**台账，实测 35 个
    不同日期）。用户 id 按 ``ledger_user_id_candidates`` **展开别名族**——用户 id
    规范化（``00000001`` → ``10000001``）会把同一座账本拆到两行用户名下，
    只按当前 uid 查会少一半历史（M2b 的同款教训）。
    """
    day = today or datetime.now(tz=CST).date()
    since = day - timedelta(days=LEDGER_LOOKBACK_DAYS)
    evidence: dict[str, Any] = {"source": "real_account_ledger_daily_snapshots"}
    try:
        from sqlalchemy import select

        from backend.services.trade_shared.models.real_account_ledger import (
            RealAccountLedgerDailySnapshot as Ledger,
        )
        from backend.shared.admin_identity import ADMIN_USER_ID
        from backend.shared.database_manager_v2 import get_session
        from backend.shared.simulation_account_keys import ledger_user_id_candidates

        users = ledger_user_id_candidates(ADMIN_USER_ID)
        async with get_session(read_only=True) as db:
            rows = (
                await db.execute(
                    select(Ledger.account_id, Ledger.snapshot_date, Ledger.total_asset)
                    .where(
                        Ledger.user_id.in_(users),
                        Ledger.snapshot_date >= since,
                    )
                    .order_by(Ledger.snapshot_date, Ledger.last_snapshot_at)
                )
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 取数失败 → None（fail-safe 由 decide_level 承接）
        logger.warning("[risk-tier] 账户回撤取数失败: %s", exc)
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return None, evidence

    fam, series = merge_ledger_rows([(r[0], r[1], r[2]) for r in rows])
    dd = max_drawdown_pct([v for _, v in series])
    evidence.update(
        {
            "account_family": fam,
            "points": len(series),
            "first": series[0][0].isoformat() if series else "",
            "last": series[-1][0].isoformat() if series else "",
        }
    )
    return dd, evidence


def limit_up_count(today: date | None = None) -> tuple[int | None, str]:
    """最近交易日的全市场涨停家数；取不到 → (None, 原因)。

    走 ``market_breadth_stats``（涨停判定的唯一事实源是 ``shared/market_breadth``
    的 ``limit_up_down_counts``，容差按板块、ST 按名单）。``trade_date=None`` 由它自己
    取最新可用交易日——盘前跑时那正是上一交易日。返回空 dict = 数据不可用（**不是 0 家**）。
    """
    try:
        from backend.services.api.market_analysis.quantdb_service import (
            market_breadth_stats,
        )

        stats = market_breadth_stats(None)
        if not stats:
            return None, ""
        value = stats.get("limit_up")
        if value is None:
            return None, ""
        return int(value), str(stats.get("trade_date") or "")
    except Exception as exc:  # noqa: BLE001 - 取数失败 → None
        logger.warning("[risk-tier] 涨停家数取数失败: %s", exc)
        return None, ""


# ── 定档（组装 + 落盘）────────────────────────────────────────────────


def _client(redis: Any):
    """拆 RedisClient 包装（`.client`）→ 原生客户端；原生客户端原样返回。

    `callable` 那半句是必需的：**原生 `redis.Redis` 自己有一个 `client()` 方法**，
    裸 `getattr(redis, "client", redis)` 会把它当包装拆出来（定档首次真跑踩中，
    见 tiers._client 同款注释）。本模块只在日键的 NX/EX 这类原子操作上用它——
    档位读写一律交包装形态给 tiers（那边自己拆）。
    """
    client = getattr(redis, "client", None)
    return client if client is not None and not callable(client) else redis


def _trade_redis_client(redis: Any = None):
    """档位读写用的 Redis（DB2 交易，与闸门读侧同一座）。"""
    if redis is not None:
        return redis
    from backend.services.trade_shared.redis_client import get_redis

    return get_redis()


async def run_tier_decision(
    *, today: date | None = None, redis: Any = None
) -> dict[str, Any]:
    """算一次并写档位。返回结果字典（``ok`` = 是否真的写了档位）。

    非交易日**不写**（隔壁同：沿用上一交易日档位）——闸门读到的仍是上一交易日的
    文档，``tier_stale_reason`` 按"应定档日"判定，不会误报过期。
    """
    day = today or datetime.now(tz=CST).date()
    result: dict[str, Any] = {"date": day.isoformat(), "ok": False}

    if not await _is_trading_day(day):
        result["skipped"] = "non_trading_day"
        return result

    vol, vol_src = index_vol20(day)
    drawdown, dd_evidence = await account_drawdown20(day)
    limit_up, zt_src = limit_up_count(day)

    from backend.shared.risk.tiers import (
        LEVELS,
        decide_level,
        load_tier,
        resolve_level,
        save_tier,
    )

    computed, reasons = decide_level(vol20=vol, drawdown20=drawdown, limit_up=limit_up)

    # 交给 tiers 的句柄**不下钻**到原生客户端：tiers._client 自己会拆一层包装，
    # 而下钻后再被它拆一次，会撞上原生 redis.Redis 自带的 `client()` 方法
    # （2026-09-23 首次真跑：'function' object has no attribute 'hset'）。
    conn = _trade_redis_client(redis)
    prev = load_tier(conn, today=day)
    effective, debounce_note = resolve_level(
        computed=computed,
        prev_level=prev.level or None,
        prev_date=prev.date or None,
        today=day,
    )
    if debounce_note:
        reasons = [*reasons, debounce_note]

    inputs: dict[str, Any] = {
        "vol20": vol,
        "drawdown20": drawdown,
        "limit_up": limit_up,
        "index_source": vol_src or "无可用指数数据",
        "limit_up_source": zt_src or "无可用情绪数据",
        "drawdown_evidence": dd_evidence,
    }
    state = save_tier(
        conn,
        level=effective,
        reasons=reasons,
        inputs=inputs,
        source="producer",
        today=day,
    )
    result.update(
        {
            "ok": True,
            "level": state.level,
            "label": LEVELS.get(state.level, {}).get("label", ""),
            "source": state.source,
            "computed": computed,
            "debounced": bool(debounce_note),
            "reasons": list(state.reasons),
            "inputs": inputs,
        }
    )
    return result


async def _is_trading_day(day: date) -> bool:
    """交易日判定（与 ``eod_service`` 同口径：日历不可用时按工作日近似）。

    近似失败方向：把节假日当交易日 → 多写一份当日档位（无害，隔日覆盖）；
    反向（把交易日当休市）才危险——档位不更新会按过期收紧买入侧。
    """
    try:
        from backend.shared.trading_calendar import calendar_service

        return await calendar_service.is_trading_day(
            market="SSE", trade_date=day, tenant_id="default", user_id="0"
        )
    except Exception:  # noqa: BLE001
        return day.weekday() < 5


# ── worker ───────────────────────────────────────────────────────────


async def run_risk_tier_worker() -> None:
    """常驻：交易日 09:10 后定档一次（日键防重；**失败删键下轮重试**）。"""
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    logger.info(
        "[risk-tier] 定档循环启动：交易日 %02d:%02d 后每 %ds 轮询",
        DECIDE_HHMM[0],
        DECIDE_HHMM[1],
        WORK_INTERVAL_S,
    )
    while True:
        try:
            _sched_heartbeat("risk_tier")  # best-effort：心跳写不进也绝不拖垮循环
        except Exception as exc:  # noqa: BLE001 - 心跳写不进也不拖垮循环
            logger.debug("[risk-tier] 心跳写入失败: %s", exc)
        try:
            now = datetime.now(CST)
            if (now.hour, now.minute) >= DECIDE_HHMM and await _is_trading_day(
                now.date()
            ):
                await _decide_once(now.date())
        except Exception as exc:  # noqa: BLE001 - 下轮重试
            logger.warning("[risk-tier] 定档失败（下轮重试）: %s", exc)
        await asyncio.sleep(WORK_INTERVAL_S)


async def _decide_once(day: date) -> None:
    """取日键（NX）→ 定档 → 成功保留日键；失败**删键**让下轮重试。

    日键同时充当"执行中锁"：多实例/多轮并发时只有一个能进来（NX），
    另一次直接跳过——档位是全局单键，不需要两边都算。
    """
    conn = _trade_redis_client()
    raw = _client(conn)  # 只给日键用：NX/EX 是原生客户端的原子语义
    done_key = f"{DONE_KEY_PREFIX}{day.isoformat()}"
    claimed = raw.set(done_key, "1", nx=True, ex=DONE_TTL_S)
    if not claimed:
        return
    try:
        # 档位写入交包装形态（见 run_tier_decision 注释），与日键同一座连接
        result = await run_tier_decision(today=day, redis=conn)
    except Exception:
        try:
            raw.delete(done_key)
        except Exception as exc:  # noqa: BLE001 - 删不掉也只是当天不再重试，不升级为异常
            logger.warning("[risk-tier] 日键清理失败（当天可能不再重试）: %s", exc)
        raise
    if result.get("skipped"):
        # 非交易日：不留日键（今天没定档这件事本身不需要记）
        try:
            raw.delete(done_key)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻断
            logger.debug("[risk-tier] 非交易日日键清理失败: %s", exc)
        return
    if result.get("ok"):
        logger.info(
            "[risk-tier] 定档 %s（%s）computed=%s debounced=%s reasons=%s",
            result.get("level"),
            result.get("label"),
            result.get("computed"),
            result.get("debounced"),
            result.get("reasons"),
        )


def main() -> int:
    """CLI：算一次并写档位（`schedule_ctl run risk_tier` 的入口）。"""
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_tier_decision())
    if result.get("skipped"):
        print(f"[risk-tier] {result['date']} 非交易日，跳过定档（沿用上一交易日档位）")
        return 0
    if not result.get("ok"):
        print(f"[risk-tier] 定档失败：{result}")
        return 1
    print(
        f"[risk-tier] {result['date']} → {result['level']}（{result['label']}）"
        f" computed={result['computed']} debounced={result['debounced']} "
        f"inputs={result['inputs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
