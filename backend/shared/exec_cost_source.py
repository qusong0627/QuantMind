"""执行损耗（TCA）的**取数层**：真单成交（``trades`` ⨝ ``orders``）→ TCA 样本行。

与 ``exec_cost``（纯口径）的分工：那边只做算术、不认识数据库；这边只做取数与
字段映射、不做任何统计判断。样本 dict 的键就是两边的接口，改动即改动报告口径。

为什么从**既有台账反推**，而不是新开一份成交流水
--------------------------------------------------

隔壁的实现是"下单链路每成交一笔，往 ``logs/live_trade_YYYYMMDD.jsonl`` 追一行"，
本仓**不复制这条写路径**，理由是它会把一条**观测性**副作用插进真金白银的成交落库
链上，而本仓的成交台账本来就是完整的：

* 成交事实在 ``trades``：一笔一行、带券商成交号、``(tenant,user,exchange_trade_id)``
  唯一（``uq_trades_scope_exchange_trade_id``）——流水该有的幂等性它已经有了；
* 下单意图在 ``orders``：限价（``price``）、委托量（``quantity``）、下单时刻
  （``submitted_at``）、幂等号（``client_order_id``）、归属（``agent``）全在；
* 决策时刻在决策账 ``qm_decision_ledger``（按 ``order_id`` 回填）。

于是"读时拼接"与"写时追流水"给出同一份事实，而**写时追流水**还要多背三个包袱：
合成成交升级（``qmt_exec_poller._upgrade_synth_trade`` 就地改写成交行的键与价，
流水那份会停在旧键旧价）、重复回报去重、以及"账本写不进去要不要拦成交"的取舍。
读时拼接没有这些包袱：``trades`` 行是唯一事实，它被升级/删除，报告跟着变。

**唯一需要补写的是基准价** ``orders.ref_price``（决策时点的参考价，见
``backend/shared/order_contract.py`` 的 ``ORDER_COLUMNS``）。它缺失时样本进
"不可定价"，**不猜**——报告如实把缺口计数印出来。

时间列的两个时区（本模块最容易出错的地方）
--------------------------------------------

本仓同时存在**两种 naive 时刻**，同名不同义，混用会静默错位 8 小时：

* ``sim_trades.executed_at`` = **naive UTC**（dispatcher 写 ``datetime.utcnow()``，
  见 ``dual_book_reconciliation_task.collect_sim_rows`` 的注释）；
* ``trades.executed_at`` / ``orders.submitted_at`` / ``orders.created_at``
  = **naive 北京时间**（写侧是 ``datetime.now()``，容器 TZ=Asia/Shanghai，
  见 ``collect_real_rows`` 的注释）。

**本模块读的是后者**，故窗口按 CST 直接算、``replace(tzinfo=None)`` 后进 SQL。
实测佐证：库内最早一笔成交为 ``2026-09-10 11:18:15``——若按 UTC 解释那是北京
19:18，A 股不可能有成交。

输出时刻一律转成 **aware UTC** 的 ISO 串（``+00:00``，不用 ``Z``：Python 3.10 的
``datetime.fromisoformat`` 不认 ``Z``，而本仓运行在 3.10 上），这样下游做差、排序、
跨日归组都不必再关心来源时区。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from backend.shared.exec_cost import PATH_UNKNOWN, tca_path
from backend.shared.trade_contract import SYNTH_TRADE_PREFIX

logger = logging.getLogger(__name__)

__all__ = [
    "CST",
    "LoadResult",
    "TCA_SAMPLE_FIELDS",
    "cst_day_bounds",
    "load_samples",
    "sample_from_report_row",
]

#: 容器本地时区（北京）。**不是**"把 naive 当 UTC 再平移"——见模块 docstring。
CST = timezone(timedelta(hours=8))

#: 零成交终态：进了成交率的分母，但不进滑点样本（把废单算成"执行得好"是最常见的
#: 假读数）。取值与 ``OrderStatus`` 的枚举标签逐字一致（PG 里是小写字符串）。
ZERO_FILL_STATUSES: tuple[str, ...] = ("rejected", "cancelled", "expired")

#: 真实成交模式。SHADOW/SIMULATION 的单**不是**真钱，不进 TCA（会混进别人的账）。
TRADING_MODE_REAL = "REAL"

#: 样本行的键（下游 ``exec_cost.summarize``/``merge_orders`` 读的就是这些）。
#: 列在这里是为了让"取数层多给一个字段"变成一次有意的改动，而不是随手加键。
TCA_SAMPLE_FIELDS: tuple[str, ...] = (
    "order_id",
    "symbol",
    "side",
    "fill_px",
    "filled",
    "ts",
    "date",
    "ref_px",
    "limit_px",
    "wanted",
    "decided_ts",
    "submit_ts",
    "fill_ts",
    "path",
    "agent",
    "exchange_trade_id",
    "price_source",
    "fees",
)


@dataclass(frozen=True)
class LoadResult:
    """取数结果：样本 + 覆盖计数。

    ``coverage`` 是**报告的另一半**：只有样本数没有覆盖数，读者无法判断
    "这批单是全部还是碰巧被拼上的那部分"。缺基准价、拼不上订单、非真钱单……
    每一项都在这里计数，报告逐项印出。
    """

    samples: tuple[dict[str, Any], ...]
    zero_fill_orders: int
    window: tuple[str, ...]
    coverage: dict[str, int] = field(default_factory=dict)


def cst_day_bounds(days: int, today: date) -> tuple[datetime, datetime]:
    """``(起, 止)`` 的 CST naive 边界（左闭右开）。

    ``days <= 0`` ⇒ 不设下界（全历史），上界仍是 ``today + 1`` 的零点。
    """
    end = datetime.combine(today + timedelta(days=1), time.min)
    if days <= 0:
        return datetime.min, end
    return datetime.combine(today - timedelta(days=days - 1), time.min), end


def _iso_utc(value: Any) -> str:
    """naive（按 CST 解释）/aware → aware UTC 的 ISO 串；无效 → 空串。

    空串而不是 ``None``：``summarize`` 的延迟统计把"解析不出"当缺失处理，
    两种都行，但空串让样本 dict 的键集合恒定（少一个键就少一处能写错的地方）。
    """
    if isinstance(value, datetime):
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=CST)
        return stamp.astimezone(timezone.utc).isoformat()
    return ""


def _num_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def sample_from_report_row(row: dict[str, Any]) -> dict[str, Any]:
    """一行"成交 ⨝ 订单"结果 → TCA 样本 dict（**纯函数**，无 DB、无 IO）。

    字段映射（左 = 样本键，右 = 来源）：

    ==================  ====================================================
    ``fill_px``         ``trades.price``（成交明细价；合成成交时是柜面均价）
    ``filled``          ``trades.quantity``
    ``fill_ts``         ``trades.executed_at``（naive CST → aware UTC）
    ``limit_px``        ``orders.price``（我方限价；市价单为空）
    ``wanted``          ``orders.quantity``（委托量）
    ``submit_ts``       ``orders.submitted_at``
    ``decided_ts``      ``qm_decision_ledger.decided_at``（拼不上则空）
    ``ref_px``          ``orders.ref_price``（**基准价**，决策时点参考价；缺失即不可定价）
    ``path``            ``exec_cost.tca_path``（幂等号 → 备注 → agent）
    ``price_source``    ``"synth"`` / ``"detail"``（合成成交单列，见下）
    ==================  ====================================================

    ``price_source``：``qmt_exec_poller`` 在"委托已成但查不到成交明细"时会先用
    ``qmt-synth-<委托键>`` 落一行合成成交，价取柜面均价；真实明细到达后就地升级
    （改键改价，见 ``_upgrade_synth_trade``）。升级前读到的那一版价是均价、
    升级后是明细价——两者通常相等，故**不剔除**，只标记出来：报告据此能回答
    "这批读数里有多少笔是均价口径"，而不是让读者以为全是逐笔明细。
    """
    order_id = str(row.get("order_id") or "")
    trade_id = str(row.get("exchange_trade_id") or "")
    filled = _num_or_none(row.get("quantity")) or 0.0
    fees = (_num_or_none(row.get("commission")) or 0.0) + (
        _num_or_none(row.get("stamp_duty")) or 0.0
    )
    path = tca_path(row.get("client_order_id"), row.get("remarks"), row.get("agent"))
    stamp = row.get("executed_at")
    return {
        "order_id": order_id,
        "symbol": str(row.get("symbol") or ""),
        "side": str(row.get("side") or "").lower(),
        "fill_px": _num_or_none(row.get("price")),
        "filled": filled,
        "ts": _iso_utc(stamp),
        "date": (
            stamp.astimezone(CST).date().isoformat()
            if isinstance(stamp, datetime)
            else ""
        ),
        "ref_px": _num_or_none(row.get("ref_price")),
        "limit_px": _num_or_none(row.get("limit_price")),
        "wanted": _num_or_none(row.get("wanted")),
        "decided_ts": _iso_utc(row.get("decided_at")),
        "submit_ts": _iso_utc(row.get("submitted_at")),
        "fill_ts": _iso_utc(stamp),
        "path": path or PATH_UNKNOWN,
        "agent": str(row.get("agent") or ""),
        "exchange_trade_id": trade_id,
        "price_source": (
            "synth" if trade_id.startswith(SYNTH_TRADE_PREFIX) else "detail"
        ),
        "fees": round(fees, 4),
    }


#: 成交 ⨝ 订单（+ 决策账）。``LEFT JOIN`` 是刻意的：拼不上订单的成交要能被**数出来**
#: （``n_orphan``），内连接会让它们从报告里彻底消失——"少了一笔"和"这一笔拼不上"
#: 是两种不同的坏消息。
_SQL_SAMPLES = """
SELECT t.order_id, t.symbol, t.side, t.quantity, t.price, t.executed_at,
       t.exchange_trade_id, t.commission, t.stamp_duty,
       o.order_id AS o_order_id, o.client_order_id, o.remarks, o.agent,
       o.ref_price, o.price AS limit_price, o.quantity AS wanted, o.submitted_at,
       o.trading_mode::text AS trading_mode,
       d.decided_at
FROM trades t
LEFT JOIN orders o
       ON o.order_id = t.order_id
      AND o.tenant_id = t.tenant_id
      AND o.user_id = t.user_id
LEFT JOIN qm_decision_ledger d
       ON d.order_id = t.order_id::text
WHERE t.tenant_id = :tenant_id
  AND t.user_id = :user_id
  AND t.executed_at >= :start
  AND t.executed_at < :end
ORDER BY t.executed_at ASC
"""

#: 零成交终态（成交率分母）。按**下单日**窗口取，与成交窗口同一口径。
_SQL_ZERO_FILL = """
SELECT count(*)
FROM orders
WHERE tenant_id = :tenant_id
  AND user_id = :user_id
  AND created_at >= :start
  AND created_at < :end
  AND trading_mode::text = 'REAL'
  AND status::text IN :statuses
  AND coalesce(filled_quantity, 0) <= 0
"""


async def load_samples(
    *,
    days: int = 30,
    tenant_id: str,
    user_id: str,
    today: date | None = None,
    session: Any = None,
) -> LoadResult:
    """窗口内的真单成交 → TCA 样本行 + 覆盖计数。

    :param days: 回溯天数（含今天）。``<= 0`` ⇒ 不设下界。
    :param tenant_id/user_id: **必须显式给定**，不从请求上下文猜——报告是给某座
        账户看的，猜错账户会把别人的成交算进自己的执行成本。缺省值留在 CLI 一层。
    :param session: 只读 session（测试注入用）。缺省自建并关闭。
    """
    from sqlalchemy import bindparam
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    day = today or datetime.now(CST).date()
    start, end = cst_day_bounds(days, day)
    params = {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "start": start,
        "end": end,
    }

    # ``statuses`` 是元组绑进 ``IN``：裸 ``text()`` 不会展开它（asyncpg 编成 $5 一个
    # 参数，PG 报语法错），必须显式声明 expanding。
    zero_fill_sql = sa_text(_SQL_ZERO_FILL).bindparams(
        bindparam("statuses", expanding=True)
    )

    async def _collect(active: Any) -> tuple[list[Any], int]:
        rows = (await active.execute(sa_text(_SQL_SAMPLES), params)).mappings().all()
        zero = (
            await active.execute(
                zero_fill_sql, {**params, "statuses": list(ZERO_FILL_STATUSES)}
            )
        ).scalar_one()
        return list(rows), int(zero or 0)

    if session is not None:
        raw_rows, zero_fill = await _collect(session)
    else:
        async with get_session(read_only=True) as active:
            raw_rows, zero_fill = await _collect(active)

    samples: list[dict[str, Any]] = []
    coverage = {
        "n_trades": len(raw_rows),
        "n_orphan": 0,
        "n_not_real": 0,
        "n_synth": 0,
        "n_missing_ref": 0,
        "n_missing_limit": 0,
        "n_unknown_path": 0,
    }
    for row in raw_rows:
        order_id = str(row.get("order_id") or "")
        if row.get("o_order_id") is None:
            # 订单侧连主键都拼不上 ＝ 订单行不在（而不是"这单没写这些字段"）——
            # 这种成交仍是真的（钱花了），但基准/限价/决策时刻一样都取不到，
            # 留在样本里只会是一条全空的假样本。数出来、告警，不进样本。
            coverage["n_orphan"] += 1
            logger.warning(
                "[ExecCost] 成交拼不上订单行（不进样本） order_id=%s trade_id=%s",
                order_id,
                row.get("exchange_trade_id"),
            )
            continue
        mode = str(row.get("trading_mode") or "")
        if mode.upper() != TRADING_MODE_REAL:
            coverage["n_not_real"] += 1
            continue
        sample = sample_from_report_row(dict(row))
        if sample["price_source"] == "synth":
            coverage["n_synth"] += 1
        if sample["ref_px"] is None:
            coverage["n_missing_ref"] += 1
        if sample["limit_px"] is None:
            coverage["n_missing_limit"] += 1
        if sample["path"] == PATH_UNKNOWN:
            coverage["n_unknown_path"] += 1
        samples.append(sample)

    dates = sorted({s["date"] for s in samples if s.get("date")})
    coverage["n_orders"] = len({s["order_id"] for s in samples if s["order_id"]})
    return LoadResult(
        samples=tuple(samples),
        zero_fill_orders=zero_fill,
        window=tuple(dates),
        coverage=coverage,
    )
