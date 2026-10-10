"""信号分快照取数口径（**唯一实现**）：最新覆盖充分日 + 每标的最近一条。

为什么单列一个共享模块：自选池统一视图（api 服务）与持仓哨兵（trade 服务）
必须看到**同一张分数表**——否则会出现「列表显示分数 +0.12，哨兵却按 -0.03
报警」这种自相矛盾的现场，用户没法信任任何一边。

口径（与候选信号页 `/stock-terminal/list` 同源）：

1. 取「覆盖充分日」：``COUNT(DISTINCT <标的归一键>) >= MIN_SIGNAL_COVERAGE`` 的最近
   ``trade_date``（计数把后缀身份折叠到裸 6 位——实时行 ``600036.SH`` 与批量行
   ``600036`` 是同一只，同股只算一次；T4-1 审计 M2）；当日覆盖不足（推理刚跑到
   一半/降级日）时回退到最近一天，并把回退事实交给调用方（``meta.fallback=True``）。
2. 该日每标的取**最新一条**（``DISTINCT ON (symbol) ORDER BY created_at DESC, id DESC``）：
   同一天可能既有日频批次行、又有盘中实时行（``source='realtime'``），
   混着取会让分数不确定。
3. 市场口径 ``market IS NULL OR market = 'CN'``——该列可空（老库未回填），
   裸等号会把 CN 行查空。
4. 标的键形归一为 prefix（``SH600036``）：表里是裸 6 位，持仓/自选侧是 prefix。
5. **按桶过滤（P2-0 信号桶隔离）**：表里同日多模型按
   ``feature_version='script_v1_<模型桶>'`` 并存，旧口径混读会把观察期挑战者
   的分数当生产分（2026-10-08 实测默认租户同日 2 桶 6496 行混读）。模式由
   ``signal_buckets.get_scoping_mode`` 决定：``off`` 旧口径 / ``shadow`` 旧口径
   出数 + 影子比对留痕 / ``enforce`` 只读生效模型桶（解析失败 → 空 + 原因，
   绝不退回混读）。

盘中实时分（``source='realtime'``）与日频批次写在**同一张表**，因此开启实时
推理后本快照即盘中分；未开启时这是最近一个批次日，调用方按 ``freq`` 如实标注。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from backend.shared.logging_config import get_logger
from backend.shared.signal_buckets import (
    get_scoping_mode,
    record_shadow_evidence,
    resolve_effective_bucket,
    resolve_feature_version,
    shadow_diff,
)
from backend.shared.stock_utils import StockCodeUtil

logger = get_logger(__name__)

#: 「信号日覆盖充分」判据（全市场 CN 标的数千只，覆盖不足说明推理残缺）
MIN_SIGNAL_COVERAGE = 1000

#: 覆盖计数用的标的归一表达式（T4-1 审计 M2）：实时行 symbol 是**后缀身份**
#: （``600036.SH``，000001.SH≠000001.SZ），批量行是裸 6 位——同一只股票两种形态
#: 在 ``COUNT(DISTINCT symbol)`` 下会算成两只，把覆盖闸门注水抬高（热集 ~500 只
#: 足以让一批残留日假过线）。覆盖日计数一律折叠到裸 6 位；非 A 股形态原样参与。
#: **跨模块共享**（stock_lookback 阶梯 / stock_terminal 默认信号日同判据）。
SYMBOL_COUNT_KEY = (
    "CASE WHEN symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$' THEN left(symbol, 6) ELSE symbol END"
)

SQL_LATEST_COVERED_DATE_TMPL = (
    "SELECT trade_date FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') {bucket}"
    "GROUP BY trade_date HAVING COUNT(DISTINCT {sym_key}) >= :min_cov "
    "ORDER BY trade_date DESC LIMIT 1"
)

_CN_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")

#: 三条 SQL 的模板：``{bucket}`` 占位在 ``_BUCKET_SQL`` 渲染时带上「前导 AND +
#: 尾随空格」。仅覆盖日模板的 DISTINCT 计数带市场段折叠（T4-1 审计 M2，见
#: ``SYMBOL_COUNT_KEY``——该表达式含正则量词 ``{6}``，必须经 ``{sym_key}``
#: 占位传入：str.format 不重扫替换值，内联进模板则会被当替换字段报错）；
#: 其余两条与旧口径逐字节相同。
SQL_LATEST_ANY_DATE_TMPL = (
    "SELECT trade_date FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') {bucket}"
    "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1"
)

SQL_SCORES_BY_DATE_TMPL = (
    "SELECT DISTINCT ON (symbol) symbol, fusion_score, signal_side, source, trade_date "
    "FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND trade_date = :d AND (market IS NULL OR market = 'CN') {bucket}"
    "ORDER BY symbol, created_at DESC, id DESC"
)

#: 桶过滤子句（enforce/shadow 按桶取数用；前导 AND + 尾随空格由这里携带）
_BUCKET_SQL = "AND feature_version = :bucket "

#: 无桶过滤渲染结果（旧口径常量照旧导出——存量引用与单测依赖其字面内容）
SQL_LATEST_COVERED_DATE = SQL_LATEST_COVERED_DATE_TMPL.format(
    bucket="", sym_key=SYMBOL_COUNT_KEY
)
SQL_LATEST_ANY_DATE = SQL_LATEST_ANY_DATE_TMPL.format(bucket="")
SQL_SCORES_BY_DATE = SQL_SCORES_BY_DATE_TMPL.format(bucket="")


def score_freq_of(source: Any) -> str:
    """分数行来源 → 频率标注（``"realtime"`` / ``"daily"``）。

    只有 ``source == 'realtime'``（热集盘中推理落库）才敢说「实时」；``batch``、
    空值、未知来源、大小写不符一律 ``daily``。**欠标优先**：把实时行标成日频只是
    让用户保守看，把日频行标成实时会让用户拿隔夜分当盘中分下单。

    列表页（``stock_terminal``）与快照（本模块 + 自选/哨兵）共用这一处判定，
    否则同一行分数在两个页面会显示成不同频率。
    """
    return "realtime" if source == "realtime" else "daily"


def normalize_a_share_symbol(raw: Any) -> str | None:
    """任意键形 → prefix 规范形（``SH600036``）；非 A 股返回 None。

    表内 symbol 是裸 6 位（``600036``），持仓/自选侧是 prefix。``StockCodeUtil.to_prefix``
    对裸 6 位按市场规则补前缀（6→SH、0/3→SZ、4/8→BJ），带后缀的先转再补。
    """
    base = str(raw or "").strip()
    if not base:
        return None
    prefix = StockCodeUtil.to_prefix(base)
    return prefix if _CN_PREFIX_RE.match(prefix) else None


def normalize_position_symbol(raw: Any) -> str | None:
    """持仓/自选键形 → prefix 规范形；非 A 股返回 None。

    与 :func:`normalize_a_share_symbol` 的差别只有一处：持仓键可能带侧标
    （``SH600036::long``，两融），先切掉再归一。切法放这里是为了让「切侧标」
    也只有一份实现——切漏了同一只票会变成两行。
    """
    return normalize_a_share_symbol(str(raw or "").split("::", 1)[0].strip())


def build_score_map(
    rows: list[Any], trade_date: str
) -> tuple[dict[str, dict[str, Any]], int]:
    """DB 行 → ``{prefix: {value, side, freq, asOf}}``；返回 (map, realtime_count)。

    行序 = ``DISTINCT ON`` 的结果序（每 symbol 仅一行，已是该标的最近一条）。
    分数为 NULL 的行仍进 map：NaN 分数与「没有分数」不同，前端要能区分。
    """
    score_map: dict[str, dict[str, Any]] = {}
    realtime_rows = 0
    for r in rows:
        sym = normalize_a_share_symbol(r[0])
        if not sym:
            continue
        freq = score_freq_of(r[3])
        if freq == "realtime":
            realtime_rows += 1
        score_map[sym] = {
            "value": float(r[1]) if r[1] is not None else None,
            "side": r[2],
            "freq": freq,
            "asOf": str(r[4])[:10] if len(r) > 4 and r[4] is not None else trade_date,
        }
    return score_map, realtime_rows


async def _fetch_snapshot(
    session: Any, tenant_id: str, bucket: str | None
) -> tuple[Any, list[Any], bool]:
    """一次口径取数 → ``(信号日 d0, rows, 覆盖不足回退标志)``。

    ``bucket=None`` 渲染的 SQL 与旧常量逐字节相同；非空则三条查询都带
    ``feature_version = :bucket``（覆盖充分日/回退日也按桶算——否则回退日会
    落到别的桶的日期上）。
    """
    clause = _BUCKET_SQL if bucket else ""
    params: dict[str, Any] = {"tid": tenant_id, "min_cov": MIN_SIGNAL_COVERAGE}
    if bucket:
        params["bucket"] = bucket
    d0 = (
        await session.execute(
            text(
                SQL_LATEST_COVERED_DATE_TMPL.format(bucket=clause, sym_key=SYMBOL_COUNT_KEY)
            ),
            params,
        )
    ).scalar_one_or_none()
    fallback = False
    if d0 is None:
        fallback = True
        d0 = (
            await session.execute(
                text(SQL_LATEST_ANY_DATE_TMPL.format(bucket=clause)), params
            )
        ).scalar_one_or_none()
    if d0 is None:
        return None, [], fallback
    rows = (
        await session.execute(
            text(SQL_SCORES_BY_DATE_TMPL.format(bucket=clause)),
            {"tid": tenant_id, "d": d0, **({"bucket": bucket} if bucket else {})},
        )
    ).fetchall()
    return d0, list(rows), fallback


async def load_score_snapshot(
    tenant_id: str,
    *,
    user_id: str | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读一次分数快照 → ``(score_map, meta)``。

    自选池统一视图与持仓哨兵**必须**经此函数取分：两处各写一份 SQL，迟早在
    「覆盖充分日回退」或「实时行取哪条」上分叉（见模块 docstring）。

    ``meta`` 带 ``signal_date``（实际取到的信号日）、``fallback``（覆盖不足回退到
    最近日）、``realtime_rows``（其中盘中实时行条数）、``ok``/``reason``，以及
    P2-0 桶隔离面 ``scoping``/``bucket``/``model_id``/``model_source``。
    取数失败不抛：返回空 map + ``ok=False``，调用方按「没取到 ≠ 分数是 0」处理。

    桶隔离（``scoping`` 模式见 ``signal_buckets``）：``model_id`` 显式传入时
    直接按该模型桶取；否则按生效模型解析（``user_id`` 缺省时用租户 is_default
    持有者）。``enforce`` 下解析失败 → 空结果 + ``ok=False`` + 原因（混读比
    没数据更危险）；``shadow`` 下照旧口径出数 + 影子比对留痕。
    """
    from backend.shared.database_manager_v2 import get_session

    mode = get_scoping_mode()
    meta: dict[str, Any] = {
        "signal_date": None,
        "fallback": False,
        "realtime_rows": 0,
        "ok": False,
        "reason": None,
        "scoping": mode,
        "bucket": None,
        "model_id": None,
        "model_source": None,
    }
    bucket: str | None = None
    shadow_payload: dict[str, Any] | None = None
    if mode != "off":
        eff: dict[str, Any] = {}
        if str(model_id or "").strip():
            bucket = resolve_feature_version(model_id)
            meta["model_id"] = str(model_id).strip()
            meta["model_source"] = "explicit_param"
        else:
            eff = await resolve_effective_bucket(
                tenant_id=tenant_id, user_id=user_id, market="CN"
            )
            bucket = eff.get("bucket")
            meta["model_id"] = eff.get("model_id")
            meta["model_source"] = eff.get("model_source")
        meta["bucket"] = bucket
        if not bucket and mode == "enforce":
            meta["reason"] = (
                f"信号桶隔离：生效模型桶无法解析（{eff.get('reason')}）；"
                "不混读，返回空"
            )
            logger.warning("[signal_scores] tenant=%s %s", tenant_id, meta["reason"])
            return {}, meta

    try:
        async with get_session(read_only=True) as session:
            read_bucket = bucket if mode == "enforce" else None
            d0, rows, fallback = await _fetch_snapshot(session, tenant_id, read_bucket)
            meta["fallback"] = fallback
            if mode == "shadow":
                if bucket:
                    d0_b, rows_b, _ = await _fetch_snapshot(session, tenant_id, bucket)
                    old_map, _ = build_score_map(
                        rows, str(d0)[:10] if d0 is not None else ""
                    )
                    new_map, _ = build_score_map(
                        rows_b, str(d0_b)[:10] if d0_b is not None else ""
                    )
                    shadow_payload = shadow_diff(
                        {s: e["value"] for s, e in old_map.items()},
                        {s: e["value"] for s, e in new_map.items()},
                        old_date=d0,
                        new_date=d0_b,
                    )
                else:
                    shadow_payload = {
                        "equal": False,
                        "bucket_missing": True,
                        "reason": eff.get("reason") or "no_effective_model",
                    }
                meta["shadow"] = shadow_payload
            if d0 is None:
                meta["ok"] = True  # 查得到，只是（该口径）库里还没有信号日
                meta["reason"] = (
                    f"engine_signal_scores 无数据（生效桶 {bucket}）"
                    if mode == "enforce"
                    else "engine_signal_scores 无数据"
                )
                if shadow_payload is not None:
                    record_shadow_evidence("watchlist", shadow_payload)
                return {}, meta
    except Exception as exc:  # noqa: BLE001 - 取不到分不是致命错（哨兵跳过本轮）
        meta["reason"] = f"分数快照取数失败: {exc}"
        logger.warning("[signal_scores] %s", meta["reason"])
        return {}, meta

    if shadow_payload is not None:
        record_shadow_evidence("watchlist", shadow_payload)
    meta["signal_date"] = str(d0)[:10]
    score_map, realtime_rows = build_score_map(list(rows), meta["signal_date"])
    meta["realtime_rows"] = realtime_rows
    meta["rows"] = len(score_map)
    meta["ok"] = True
    return score_map, meta
