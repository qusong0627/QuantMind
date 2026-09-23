"""P2.2 IO 适配：QM 的四个数据面 → :class:`RebalanceContext`（渲染在 ``decision/context.py``）。

纯核心只管「拿到快照之后怎么排版」，本模块管「快照从哪来」。分开是为了让排版能拿
隔壁的独立实现做逐字节金样（合成 + 真语料两套，见
``backend/tests/test_decision_context.py`` 与 ``docs/local/diff_decision_prompt_vs_baymax.py``）。

**输入源映射（隔壁 22 项 → QM 落点）**

======================  ==========================================  ==========
隔壁                     QM 落点                                     状态
======================  ==========================================  ==========
``live_ledger.json``    无（``l1.vcash`` **没有生产者**）            缺口
桥 ``account``          ``real_account_snapshots.payload_json``     已接
``lq_klines`` 涨跌       ``market:snapshot:*`` 的 ``Now/PreClose``   已接
``select_from_reports`` ``data/reports/stock_picks/{date}_*.json``   已接
``l2_factors_live.json`` ``market:snapshot:*``（TDX 热集/桥）        已接
``risk_block.json``     无                                          缺口
``industry_risk.json``  无                                          缺口
``news_brief``          无（新闻走 Huntly/RSS，未做分子聚合）        缺口
``decision_scorecard``  无（P2.5 记分卡是**别的**东西：审计表在 PG） 缺口
======================  ==========================================  ==========

缺口不是「以后再补」四个字：**块整段不出现**（渲染器对空串的行为），而不是填假数据。
池侧还缺 ``rank``/``industry``/``fusion`` 三列——QM 的产物里没有，渲染成 ``—``/空。

代码口径：本模块出口一律 **suffix 式**（``600036.SH``）。QM 落库是 prefix 式
（``SH600036``），模型 schema 与 ``decision.gates`` 内部口径都是 suffix——转换只在
这一层做，别的地方不许再手写切片。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from backend.shared.decision.context import (
    QUOTE_MAX_AGE_MIN,
    DirectionBlock,
    HoldingRow,
    PoolQuote,
    PoolRow,
    RebalanceContext,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

#: 行情快照键的 TTL（与 ``tdx_aidata.collector.SNAPSHOT_TTL`` 同值）。键还在 =
#: 5 分钟内有帧；键没了就是**断流**，不是「价格是 0」。
SNAPSHOT_TTL_S = 300

#: 池文件两代命名：``{date}_agent_picks.json``（带事件标注，现行）与
#: ``{date}_picks.json``（旧格式，``market_direction`` 里多 total_score）。
_POOL_SUFFIXES = ("_agent_picks.json", "_picks.json")


def reports_dir() -> Path:
    """报告根目录（``QM_REPORTS_DIR`` 优先，与 ``scripts/decision_ledger`` 同源）。"""
    env = os.getenv("QM_REPORTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "reports"


def pool_path(day: str) -> Path | None:
    """该交易日的池文件（新的优先）；都没有返回 ``None``（当天还没跑出池）。"""
    base = reports_dir() / "stock_picks"
    for suffix in _POOL_SUFFIXES:
        p = base / f"{day}{suffix}"
        if p.exists():
            return p
    return None


def _to_suffix(code: object) -> str:
    from backend.shared.stock_utils import StockCodeUtil

    return StockCodeUtil.to_suffix(str(code or "").strip())


# ── 池 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PoolDoc:
    """池文件解析结果：行 + 方向 + 缺失列清单（缺失要说出来，不静默）。"""

    rows: tuple[PoolRow, ...]
    direction: DirectionBlock
    missing_columns: tuple[str, ...]
    source: str


def load_pool_doc(day: str, *, top: int | None = None) -> PoolDoc | None:
    """读 QM 的池产物 → :class:`PoolDoc`；文件不在返回 ``None``。

    ``top``：只取前 N 只（**排序以文件为准**，文件里没有 ``rank`` 就按出现顺序编号）。
    ``rank``/``industry``/``fusion`` 三列 QM 没有 → 缺失列登记进 ``missing_columns``，
    渲染成 ``—``/空，**绝不**编一个行业名出来。
    """
    path = pool_path(day)
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("池文件读不动 %s：%s", path, exc)
        return None
    picks = doc.get("picks") or []
    if not isinstance(picks, list):
        logger.warning("池文件 %s 的 picks 不是列表（%s）", path, type(picks).__name__)
        return None
    if top is not None:
        picks = picks[: max(0, int(top))]

    missing: set[str] = set()
    rows: list[PoolRow] = []
    for i, p in enumerate(picks):
        if not isinstance(p, Mapping) or not p.get("code"):
            continue
        for col in ("rank", "industry", "fusion"):
            if p.get(col) is None:
                missing.add(col)
        score = p.get("score")
        rows.append(
            PoolRow(
                code=_to_suffix(p["code"]),
                name=str(p.get("name") or ""),
                industry=str(p.get("industry") or ""),
                score=float(score) if isinstance(score, (int, float)) else None,
                fusion=(
                    float(p["fusion"])
                    if isinstance(p.get("fusion"), (int, float))
                    else None
                ),
                rank=p.get("rank", i + 1),
                remark=str(p.get("reason") or p.get("remark") or ""),
            )
        )
    md = doc.get("market_direction") or {}
    direction = DirectionBlock(
        direction=str(md.get("direction") or "—"),
        total_score=md.get("total_score"),
    )
    return PoolDoc(
        rows=tuple(rows),
        direction=direction,
        missing_columns=tuple(sorted(missing)),
        source=str(path),
    )


# ── 行情快照（现价 / 涨跌 / 池内定价依据）────────────────────────────


def snapshot_key(code: str) -> str:
    """任意形态 → ``market:snapshot:{prefix.lower()}``（与 ``tdx_aidata.collector`` 同构）。

    小写前缀是**写侧契约**（``market:snapshot:sh600036``）——别按 PG 键名习惯写成
    大写，那样读到的是空。
    """
    from backend.shared.stock_utils import StockCodeUtil

    return f"market:snapshot:{StockCodeUtil.to_prefix(code).lower()}"


def _f(value: object) -> float | None:
    """快照字段 → float；空/脏一律 ``None``（**不是 0**）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def day_change_pct(snap: Mapping[str, Any] | None) -> float | None:
    """快照 → 今日涨跌%。``Now``/``PreClose`` 缺任一 → ``None``。

    字段名两套并存：标准键契约是 ``Now/PreClose``（``RemoteRedisDataSource`` 口径），
    原始推送字段是 ``pre_close``/``now``——两套都认，取不到就是取不到。
    """
    if not isinstance(snap, Mapping):
        return None
    now_px = _f(snap.get("Now") if snap.get("Now") is not None else snap.get("now"))
    pre = _f(
        snap.get("PreClose")
        if snap.get("PreClose") is not None
        else snap.get("pre_close")
    )
    if now_px is None or pre is None or pre <= 0:
        return None
    return round((now_px / pre - 1) * 100, 2)


def snapshot_age_min(snap: Mapping[str, Any] | None, now: datetime) -> float | None:
    """快照水印（epoch 秒）→ 距今分钟；无可用水印返回 ``None``。"""
    if not isinstance(snap, Mapping):
        return None
    for key in ("timestamp", "ts"):
        ts = _f(snap.get(key))
        if ts is not None and ts > 0:
            return max(0.0, now.timestamp() - ts) / 60
    return None


def read_snapshots(client: Any, codes: Iterable[str]) -> dict[str, Mapping[str, Any]]:
    """批量读快照 Hash（pipeline，逐只缺键就是缺键）。``code`` 用 suffix 形态做键。

    行数上限由调用方控制——热集是全市场的子集，持仓 + 池加起来通常两位数。
    """
    keys: dict[str, str] = {}
    for code in codes:
        k = snapshot_key(code)
        if k:
            keys[k] = _to_suffix(code)
    if not keys:
        return {}
    pipe = client.pipeline()
    for k in keys:
        pipe.hgetall(k)
    out: dict[str, Mapping[str, Any]] = {}
    for symbol, data in zip(keys.values(), pipe.execute(), strict=True):
        if data:
            out[symbol] = data
    return out


def quotes_from_snapshots(
    snaps: Mapping[str, Mapping[str, Any]],
    pool: Sequence[PoolRow],
    now: datetime,
    *,
    max_age_min: int = QUOTE_MAX_AGE_MIN,
) -> tuple[tuple[PoolQuote, ...], int]:
    """池内行情 → (可用行, 过期条数)。

    **只为池内标的出行**：持仓的涨跌走 ``day_change_pct`` 进持仓表，不进行情块
    （否则行情块会长成第二个持仓表）。过期项整行剔除并计数，缺价/缺时间戳的
    静默跳过**不计数**——「没采到」与「采到了但馊了」是两回事，混在一个数字里
    就没法判断是采集断了还是标的被剔出热集。
    """
    quotes: list[PoolQuote] = []
    stale = 0
    for row in pool:
        snap = snaps.get(row.code)
        if not snap:
            continue
        price = _f(snap.get("Now") if snap.get("Now") is not None else snap.get("now"))
        if price is None or price <= 0:
            continue
        age = snapshot_age_min(snap, now)
        if age is None:
            continue
        if age > max_age_min:
            stale += 1
            continue
        pre = _f(
            snap.get("PreClose")
            if snap.get("PreClose") is not None
            else snap.get("pre_close")
        )
        quotes.append(
            PoolQuote(
                code=row.code,
                name=row.name,
                price=price,
                age_min=age,
                pre_close=pre if (pre is not None and pre > 0) else None,
                signal_score=None,  # 快照键里没有信号分；留给后续接推理分数
            )
        )
    return tuple(quotes), stale


# ── 持仓 ─────────────────────────────────────────────────────────────


def resolve_name(code: str, fallback: str = "") -> str:
    """中文名解析（``stock_name_mapper`` 索引缺了就退回 fallback，不抛）。"""
    if fallback:
        return fallback
    try:
        from backend.shared.stock_name_mapper import resolve_name as _resolve

        return _resolve(code) or code
    except Exception:  # noqa: BLE001 名字缺失不该挡住一轮调仓
        return code


def positions_to_holding_rows(
    positions: Sequence[Mapping[str, Any]],
    *,
    snaps: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[HoldingRow, ...]:
    """``real_account_snapshots.payload_json.positions[]`` → 持仓行（**桥口径**）。

    字段：``symbol/name/volume/available_volume/cost_price/price``。桥给的 ``name``
    可能是空串（实测 2026-09-23 有一个账户 50 只全是空名）→ 走名字索引兜底。

    ``day_chg`` 只从快照来；没有快照就是 ``None`` → 渲染 ``—``（隔壁在这里会
    直接 ``TypeError`` 炸掉整轮提示词）。
    """
    snaps = snaps or {}
    out: list[HoldingRow] = []
    for p in positions:
        if not isinstance(p, Mapping):
            continue
        raw = str(p.get("symbol") or "").strip()
        if not raw:
            continue
        code = _to_suffix(raw)
        cost = _f(p.get("cost_price"))
        price = _f(p.get("price"))
        volume = _f(p.get("volume"))
        avail = _f(p.get("available_volume"))
        cost_v = cost if cost is not None else 0.0
        price_v = price if price is not None else 0.0
        out.append(
            HoldingRow(
                code=code,
                name=resolve_name(raw, str(p.get("name") or "")),
                volume=int(volume or 0),
                cost=round(cost_v, 2),
                price=round(price_v, 2),
                pnl_pct=(
                    round((price_v - cost_v) / cost_v * 100, 2) if cost_v > 0 else 0.0
                ),
                day_chg=day_change_pct(snaps.get(code)),
                avail=int(avail or 0),
            )
        )
    return tuple(out)


# ── 组装 ─────────────────────────────────────────────────────────────


def build_context(
    *,
    agent: str,
    holdings: Sequence[HoldingRow],
    pool: Sequence[PoolRow],
    direction: DirectionBlock,
    now: datetime,
    quotes: Sequence[PoolQuote] = (),
    quotes_stale_count: int = 0,
    ledger_positions: Mapping[str, Any] | None = None,
    risk_block: str = "",
    industry_caution_block: str = "",
    quota_used: float = 0.0,
    per_stock_pct: float | None = None,
    max_new_buys: int | None = None,
    extra_context: str = "",
) -> RebalanceContext:
    """把读到的四块拼成一轮上下文。

    风控档位（``per_stock_pct``/``max_new_buys``）不传就取 ``decision/context``
    的默认值——由调用方从 ``shared/risk/tiers`` 按当前档位取，**别在本模块里读
    Redis**：取数与渲染分层是本模块存在的理由。
    """
    kwargs: dict[str, Any] = {}
    if per_stock_pct is not None:
        kwargs["per_stock_pct"] = per_stock_pct
    if max_new_buys is not None:
        kwargs["max_new_buys"] = max_new_buys
    return RebalanceContext(
        agent=agent,
        now=now,
        holdings=tuple(holdings),
        ledger_positions=dict(ledger_positions or {}),
        direction=direction,
        pool=tuple(pool),
        quotes=tuple(quotes),
        quotes_stale_count=quotes_stale_count,
        extra_context=extra_context,
        risk_block=risk_block,
        industry_caution_block=industry_caution_block,
        quota_used=quota_used,
        **kwargs,
    )


def now_cn() -> datetime:
    """当前北京时间（aware）——提示词里的「现在是北京时间 …」。"""
    return datetime.now(timezone.utc).astimezone(CST)
