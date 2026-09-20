"""账户卡「风控事件」维：状态词表、user 键形与评分（设计 §2.5）。

**为什么单独一个模块**：这维在实盘数据上踩了两个坑，都需要独立可测的纯函数——

1. **状态词表**：库里的真实取值是 ``skipped_no_quote`` / ``no_targets``（全库 8028 条），
   而评分曾按 ``failed``/``skipped``/``alert`` **精确取键**，一个都命中不了 →
   罚分恒 0 → 风控维静默满分。这里改成**分类**（按前缀/子串），并把没见过的状态
   单列 ``unknown_statuses``，新增状态不会再静默归零。
2. **``skipped_no_quote`` 是盲区不是普通跳过**：`risk_trigger_eval.py` 写这条的含义是
   「**有持仓**但取不到行情 → 止损/止盈判不了」。风控在跑，但看不见价格 →
   风控形同虚设。所以除「执行失败」计数罚分外，另按 ``blind_ratio`` 计一条覆盖率罚分，
   并在 ≥90% 时判红线。

user 键形：管理员族（``10000001``/``00000001``/``1``/``0``/``admin``）在 PG 的 int 列上
是**同一账户的不同写法**（``00000001`` 入库成 1、非数字落 0），只按规范名查会读成
「零事件」。键形收口复用 ``simulation_account_keys`` 的公开判定，不自己造规则。

本节以上的纯函数不碰库；下面的 :func:`load_risk_summary` 是唯一的 DB 取数口。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from backend.shared.eval_scoring import DimensionScore, score_from_thresholds
from backend.shared.simulation_account_keys import (
    CANONICAL_ADMIN_SIM_USER,
    is_admin_sim_user,
    normalize_runtime_user,
)

WEIGHT = 25.0
# 风控事件窗口默认天数（与 account_card.DEFAULT_WINDOW_DAYS 同值）
DEFAULT_WINDOW_DAYS = 30
# 无行情跳过占比红线：风控九成以上的判定看不见价格 = 风控没在工作
BLIND_RED_LINE = 0.9
# 执行失败红线（原设计：failed ≥ 3）
FAILED_RED_LINE = 3

_CLASS_BLIND = "blind"
_CLASS_FAILED = "failed"
_CLASS_ALERT = "alert"
_CLASS_FILLED = "filled"
_CLASS_NO_TARGETS = "no_targets"
_CLASS_OTHER = "other"

_FILLED_TOKENS = frozenset({"filled", "applied", "executed", "success", "triggered"})


def classify_status(status: Any) -> str:
    """``risk_events.status`` → 语义类（按前缀/子串，不穷举字面量）。"""
    s = str(status or "").strip().lower()
    if not s:
        return _CLASS_OTHER
    if s.startswith("skipped"):
        return _CLASS_BLIND
    if s.startswith("no_targets") or s.startswith("no_target"):
        return _CLASS_NO_TARGETS
    if s.startswith("failed") or "error" in s or "exception" in s:
        return _CLASS_FAILED
    if s.startswith("alert") or s.startswith("warn"):
        return _CLASS_ALERT
    if s in _FILLED_TOKENS:
        return _CLASS_FILLED
    return _CLASS_OTHER


def summarize_statuses(rows: Any) -> dict[str, Any]:
    """``[(status, count)]`` → 分类计数 + 原始直方图 + 未知状态 + 盲区占比（纯函数）。"""
    counts: dict[str, int] = {}
    for item in rows or []:
        try:
            status, cnt = item[0], int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        counts[str(status)] = counts.get(str(status), 0) + cnt

    classes: dict[str, int] = {
        _CLASS_BLIND: 0,
        _CLASS_FAILED: 0,
        _CLASS_ALERT: 0,
        _CLASS_FILLED: 0,
        _CLASS_NO_TARGETS: 0,
        _CLASS_OTHER: 0,
    }
    unknown: dict[str, int] = {}
    for status, cnt in counts.items():
        cls = classify_status(status)
        classes[cls] += cnt
        if cls == _CLASS_OTHER:
            unknown[status] = cnt
    total = sum(counts.values())
    return {
        "n_events": total,
        "raw_statuses": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        **classes,
        "unknown_statuses": unknown,
        "blind_ratio": round(classes[_CLASS_BLIND] / total, 6) if total else 0.0,
    }


def db_user_id_keys(user_raw: Any) -> list[int]:
    """账户 user 在 PG int 列上可能的全部键（管理员族一次读全，其它用户各读各的）。

    与 Redis 侧 ``_user_id_aliases`` 同源口径：``00000001`` 入库成 1、非数字落 0。
    """
    raw = str(user_raw or "").strip()
    canonical = normalize_runtime_user(raw) or CANONICAL_ADMIN_SIM_USER
    if is_admin_sim_user(raw) or is_admin_sim_user(canonical):
        return [int(CANONICAL_ADMIN_SIM_USER), 1, 0]
    try:
        return [int(canonical)]
    except ValueError:
        return [0]


def _exec_penalty(summary: dict[str, Any]) -> float:
    return (
        2.0 * int(summary.get("failed") or 0)
        + 1.5 * int(summary.get("rejected_orders") or 0)
        + 0.5 * int(summary.get("alert") or 0)
    )


def score_risk_events(summary: dict[str, Any] | None) -> DimensionScore:
    """风控事件维：执行失败罚分（计数）+ 盲区占比（覆盖率），两条红线。

    ``available=False``（机制从未启用）→ 如实缺省——「无记录」不等于「无事件」。
    """
    if not summary or not summary.get("available"):
        return DimensionScore(
            "risk_events",
            "风控事件",
            WEIGHT,
            None,
            False,
            {
                "insufficient": True,
                "note": "风控机制未启用或历史为零（无法把「无记录」当「无事件」）",
                "summary": summary,
            },
        )

    if not int(summary.get("n_events") or 0):
        # 租户有历史（机制在跑）但**本账户**窗口内一条事件都没有：分不清是
        # 「规则评估过、无事可做」还是「规则根本没看到这个账户」——按缺省处理，
        # 否则静默给满分（实测 user=42 就是这种：0 事件 → 100 分）。
        return DimensionScore(
            "risk_events",
            "风控事件",
            WEIGHT,
            None,
            False,
            {
                "insufficient": True,
                "note": (
                    "本账户窗口内无风控事件"
                    f"（租户历史 {summary.get('tenant_events_all_time')} 条）——"
                    "无记录不等于无事件，不按满分计"
                ),
                "summary": summary,
            },
        )

    failed = int(summary.get("failed") or 0)
    rejected = int(summary.get("rejected_orders") or 0)
    alert = int(summary.get("alert") or 0)
    # `skipped` 是分类前的旧键名（同 `skipped*` 一类）——老调用方传它时按盲区计，
    # 不再原样忽略（忽略 = 罚分悄悄少一项）。
    blind = int(summary.get("blind") or summary.get("skipped") or 0)
    blind_ratio = float(summary.get("blind_ratio") or 0.0)
    penalty = _exec_penalty(summary)
    exec_score = score_from_thresholds(
        penalty, [(0.0, 100.0), (2.0, 75.0), (5.0, 50.0), (10.0, 20.0), (20.0, 0.0)]
    )
    coverage_score = score_from_thresholds(
        blind_ratio, [(0.0, 100.0), (0.05, 90.0), (0.2, 70.0), (0.5, 40.0), (1.0, 0.0)]
    )
    # 短板口径（取 min 而非加权平均）：执行与可见性缺一不可——看见价格却老失败，
    # 与从不失败却看不见价格，都是风控不成立；加权平均会让 100 分的执行把
    # 「99.6% 判定瞎」抬到 60 分，正好掩盖这类系统性失效。
    score = round(
        min(
            exec_score if exec_score is not None else 50.0,
            coverage_score if coverage_score is not None else 50.0,
        ),
        2,
    )

    detail: dict[str, Any] = {
        "window_days": summary.get("window_days"),
        "n_events": summary.get("n_events"),
        "raw_statuses": summary.get("raw_statuses"),
        "failed": failed,
        "rejected_orders": rejected,
        "alert": alert,
        "filled": int(summary.get("filled") or 0),
        "no_targets": int(summary.get("no_targets") or 0),
        "blind": blind,
        "blind_ratio": blind_ratio,
        "penalty": round(penalty, 2),
        "exec_score": exec_score,
        "coverage_score": coverage_score,
        "unknown_statuses": summary.get("unknown_statuses") or {},
    }
    if summary.get("note"):
        detail["note"] = summary["note"]

    red_failed = failed >= FAILED_RED_LINE
    red_blind = blind > 0 and blind_ratio >= BLIND_RED_LINE
    if red_failed:
        detail["red_line"] = f"风控执行失败 {failed} 次 ≥ {FAILED_RED_LINE} 次"
    if red_blind:
        red_text = (
            f"风控盲区 {blind_ratio:.1%} ≥ {BLIND_RED_LINE:.0%}："
            f"{blind} 次判定因取不到行情（无行情跳过）没能执行止损/止盈——"
            "风控在跑但看不见价格，规则形同虚设"
        )
        detail["red_line"] = (
            f"{detail['red_line']}；{red_text}" if red_failed else red_text
        )
    if detail["unknown_statuses"]:
        detail["note"] = (
            f"{detail.get('note', '')} 出现未归类的风控状态 "
            f"{detail['unknown_statuses']}（未计入罚分，需补词表）"
        ).strip()
    return DimensionScore(
        "risk_events", "风控事件", WEIGHT, score, bool(red_failed or red_blind), detail
    )


# ── DB 取数 ──────────────────────────────────────────────────────────


async def load_risk_summary(
    tenant: str, user_raw: str, *, days: int = DEFAULT_WINDOW_DAYS
) -> dict[str, Any]:
    """风控事件窗口汇总（risk_events + 拒单）；机制可用性按「租户历史/活跃规则」判定。

    两处实盘踩过的坑：
    - **`risk_rules` 没有 `tenant_id` 列**（实测列：is_active/applies_to_all/user_ids/…）：
      原查询抛 ``UndefinedColumnError``，而 asyncpg 里失败的语句会**废掉整个事务**，
      后续查询全报 ``InFailedSQLTransactionError`` → 外层 except 吞掉 →
      `available=False` → 风控维在**有 8028 条事件**的库上照样静默缺省。
      现在先读 ``information_schema`` 确认列存在，且把可失败的探测放在
      ``begin_nested()``（SAVEPOINT）里——失败只回滚子事务，主事务继续可用；
    - **user 键形**：管理员族 ``00000001`` 入库成 1、非数字落 0，只按规范名查会读成
      「零事件」；现在按 ``db_user_id_keys`` 一次读全该账户的所有键形。
    """
    from sqlalchemy import text as _text

    from backend.scripts.eval.risk_events import db_user_id_keys, summarize_statuses
    from backend.shared.database_manager_v2 import get_session

    summary: dict[str, Any] = {"available": False, "window_days": int(days)}
    uid_keys = db_user_id_keys(user_raw)
    summary["user_id_keys"] = uid_keys
    since = date.today() - timedelta(days=max(1, int(days)))
    try:
        async with get_session(read_only=True) as session:
            tenant_total = (
                await session.execute(
                    _text("SELECT count(*) FROM risk_events WHERE tenant_id = :t"),
                    {"t": tenant},
                )
            ).scalar() or 0
            rules_total = await _count_active_rules(session, uid_keys)
            summary["available"] = bool(int(tenant_total) > 0 or int(rules_total) > 0)
            summary["tenant_events_all_time"] = int(tenant_total)
            summary["rules_configured"] = int(rules_total)
            if not summary["available"]:
                return summary
            if uid_keys:
                rows = (
                    await session.execute(
                        _text(
                            "SELECT status, count(*) FROM risk_events "
                            "WHERE tenant_id = :t AND user_id = ANY(:u) "
                            "AND trade_date >= :since GROUP BY status"
                        ),
                        {"t": tenant, "u": uid_keys, "since": since},
                    )
                ).fetchall()
                summary.update(summarize_statuses(rows))
                rejected = (
                    await session.execute(
                        _text(
                            "SELECT count(*) FROM sim_orders WHERE tenant_id = :t "
                            "AND user_id = ANY(:u) AND status = 'rejected' "
                            "AND created_at >= :since"
                        ),
                        {"t": tenant, "u": uid_keys, "since": since},
                    )
                ).scalar() or 0
                summary["rejected_orders"] = int(rejected)
    except Exception as exc:  # noqa: BLE001 - 风控表缺失不拖垮评分
        summary["available"] = False
        summary["note"] = f"风控事件取数失败：{type(exc).__name__}: {exc}"
    return summary


async def _count_active_rules(session: Any, uid_keys: list[int]) -> int:
    """对该账户生效的启用规则数（`risk_rules` 无 tenant_id，按 applies_to_all/user_ids 判）。

    表或列缺失时返回 0，**但不许拖着主事务一起死**：探测走 SAVEPOINT，失败即回滚子事务。
    """
    from sqlalchemy import text as _text

    cols = {
        str(r[0])
        for r in (
            await session.execute(
                _text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'risk_rules'"
                )
            )
        ).fetchall()
    }
    if not {"is_active", "applies_to_all", "user_ids"} <= cols:
        return 0
    scope = "applies_to_all IS TRUE"
    params: dict[str, Any] = {}
    if uid_keys:
        # user_ids 是 jsonb 数组（元素可能是 int 或 str）：摊平后按文本比，
        # 别用 `@>` 猜元素类型（[[1]] / ["1"] 两种写法会静默不匹配）。
        scope = (
            "(applies_to_all IS TRUE OR EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text(user_ids) AS e "
            "WHERE e.value = ANY(:ids)))"
        )
        params["ids"] = [str(k) for k in uid_keys]
    try:
        async with session.begin_nested():
            return (
                await session.execute(
                    _text(
                        f"SELECT count(*) FROM risk_rules WHERE is_active AND {scope}"
                    ),
                    params,
                )
            ).scalar() or 0
    except Exception:  # noqa: BLE001 - 规则表异常只影响「机制是否启用」的判定
        return 0
