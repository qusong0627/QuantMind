"""信号分快照取数口径（**唯一实现**）：最新覆盖充分日 + 每标的最近一条。

为什么单列一个共享模块：自选池统一视图（api 服务）与持仓哨兵（trade 服务）
必须看到**同一张分数表**——否则会出现「列表显示分数 +0.12，哨兵却按 -0.03
报警」这种自相矛盾的现场，用户没法信任任何一边。

口径（与候选信号页 `/stock-terminal/list` 同源）：

1. 取「覆盖充分日」：``COUNT(DISTINCT symbol) >= MIN_SIGNAL_COVERAGE`` 的最近
   ``trade_date``；当日覆盖不足（推理刚跑到一半/降级日）时回退到最近一天，
   并把回退事实交给调用方（``meta.fallback=True``）。
2. 该日每标的取**最新一条**（``DISTINCT ON (symbol) ORDER BY created_at DESC, id DESC``）：
   同一天可能既有日频批次行、又有盘中实时行（``source='realtime'``），
   混着取会让分数不确定。
3. 市场口径 ``market IS NULL OR market = 'CN'``——该列可空（老库未回填），
   裸等号会把 CN 行查空。
4. 标的键形归一为 prefix（``SH600036``）：表里是裸 6 位，持仓/自选侧是 prefix。

盘中实时分（``source='realtime'``）与日频批次写在**同一张表**，因此开启实时
推理后本快照即盘中分；未开启时这是最近一个批次日，调用方按 ``freq`` 如实标注。
"""

from __future__ import annotations

import re
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

#: 「信号日覆盖充分」判据（全市场 CN 标的数千只，覆盖不足说明推理残缺）
MIN_SIGNAL_COVERAGE = 1000

_CN_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")

SQL_LATEST_COVERED_DATE = (
    "SELECT trade_date FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') "
    "GROUP BY trade_date HAVING COUNT(DISTINCT symbol) >= :min_cov "
    "ORDER BY trade_date DESC LIMIT 1"
)

SQL_LATEST_ANY_DATE = (
    "SELECT trade_date FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') "
    "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1"
)

SQL_SCORES_BY_DATE = (
    "SELECT DISTINCT ON (symbol) symbol, fusion_score, signal_side, source, trade_date "
    "FROM engine_signal_scores "
    "WHERE tenant_id = :tid AND trade_date = :d AND (market IS NULL OR market = 'CN') "
    "ORDER BY symbol, created_at DESC, id DESC"
)


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
        source = str(r[3] or "")
        freq = "realtime" if source == "realtime" else "daily"
        if freq == "realtime":
            realtime_rows += 1
        score_map[sym] = {
            "value": float(r[1]) if r[1] is not None else None,
            "side": r[2],
            "freq": freq,
            "asOf": str(r[4])[:10] if len(r) > 4 and r[4] is not None else trade_date,
        }
    return score_map, realtime_rows
