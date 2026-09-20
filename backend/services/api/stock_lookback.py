"""信号准确率回看（T-N 分数/排名 → 至今涨跌）。

回答一个问题：**模型 T-N 那天给高分/低分的票，到今天的涨跌对不对。**

## 口径（改这里之前先读完整段）

- **回看点按「信号日序列」数**，不按行情日数。`engine_signal_scores.trade_date`
  是**信号生效日 = 数据日 + 1 个交易日**（如 09-18 的数据写成 `trade_date=09-21`），
  所以它可能是**未来日、没有行情分区**——取价一律走
  `latest_partition_on_or_before()`，禁止直取 `close(trade_date)`。
- **一天只取一个 run**。实测同一天多个 run 的 `fusion_score` 量纲互不相同
  （2026-09-09 的 `eccdc1d2` 与另三个 run 相关系数 −0.03~−0.08、平均绝对差 0.16），
  跨 run 拼出来的名次和分档全是假的。规则：`created_at` 最新，且排除
  `source='realtime'` 的 529 只热集批（那是盘中增量，不该当日口径）。
- **口径一致性逐行判**（`scale_comparable`）。实测 2026-09-07 那天**压根没有**
  宽口径族的 run，只有 sd=0.0104 的窄族，与锚点 sd=0.2478 差 24 倍——全局判定
  会把本来可信的 T-3/T-5 一起拖黑，所以按行标。
- **收益 = 现价 / 回看日收盘 − 1**，一律前复权（`daily_forward`）。基准取
  **回看日当天收盘**（「看到信号按当日收盘买入」的可执行口径）。这与
  `engine/inference/data_loader.load_forward_labels` 的 T+1 lag 口径**不同**，
  两者别互相套。
- **绝不用 `features_daily.return_Nd` / `technical_indicators.return_Nd`**：
  那是未来收益（标签），已实测 corr(return_1d[T], pct_change[T+1]) = 0.9991。
- **`score_rank` 不能用**（批量路径恒 NULL），**`rank_pct` 也不用**（per-run 分位，
  同日多 run 时会有多个 1.0；且它由回填脚本写入）。名次与分位一律现算。
- 分档按**分位**不按分数符号：量纲跨日会变，绝对阈值会得到空桶或全市场。
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import date
from typing import Any

import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# 行情视图（前复权）：QuantDB 相对路径与 hub 的 view 名保持同源
REL_DAILY_FORWARD = "1_kline_data/daily_forward"

# 价格有效下限（元）。`daily_forward` 早年存在负价/趋零的损坏记录
# （见 docs：前复权以最新日为锚，早期被压到近 0），相除会炸出 inf/天文数字。
MIN_VALID_PRICE = 1e-4

# 分档默认宽度：高分档 = 分位前 20%，低分档 = 后 20%
DEFAULT_BUCKET_PCT = 0.2

# 口径一致性阈值：该 run 的 sd 与锚点 run 的 sd 之比超出 [1/3, 3] 即判不可比
COMPARABLE_RATIO = 3.0

_CN_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


# ---------------------------------------------------------------------------
# 纯函数（无 IO，单测直接打）
# ---------------------------------------------------------------------------


def canonical_symbol(raw: Any) -> str | None:
    """`engine_signal_scores.symbol` → QuantDB 行情口径（`600036.SH`）。

    非 A 股（港股 4~5 位码、美股 ticker）与垃圾值一律 None——转不出来就宁可缺，
    绝不瞎猜交易所。归一本身复用 `StockCodeUtil.to_suffix`，不另写一套切片。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    suffix = StockCodeUtil.to_suffix(text)
    return suffix if _CN_SUFFIX_RE.match(suffix) else None


def pick_lookback_dates(
    signal_dates_desc: list[str], lookbacks: list[int]
) -> dict[int, str]:
    """信号日阶梯（降序，`[0]` = 锚点）→ `{N: 日期}`。

    阶梯不够长时，缺的档**整个不出现**——不顺延、不补零、不错位。调用方看到
    `N not in result` 就该把那一档渲染成「无证据」。
    """
    out: dict[int, str] = {}
    for n in lookbacks:
        if 0 <= n < len(signal_dates_desc):
            out[int(n)] = signal_dates_desc[int(n)]
    return out


def compute_return(base_close: Any, now_close: Any) -> float | None:
    """`现价 / 基准价 − 1`（小数）。

    基准价非正/趋零/NaN 一律 None——不产生 inf，也不兜底成 0（兜 0 等于宣称
    「没涨没跌」，那是事实主张）。现价为 0 是合法的 −100%，照常返回。
    """
    base = _finite(base_close)
    now = _finite(now_close)
    if base is None or now is None or base < MIN_VALID_PRICE:
        return None
    return now / base - 1.0


def apply_scale(now: Any, pre_close: Any, qfq_latest: Any) -> tuple[float | None, bool]:
    """把**未复权**的实时价缩放到**前复权**基准上，返回 `(价格, 是否缩放)`。

    `k = 前复权(最新分区) / PreClose`；无除权时 k ≡ 1。不做这步的话，除权日
    当天实时价与历史前复权收盘相除会凭空多出（或少掉）分红那部分。
    `PreClose` 缺失/非正，或 `前复权(最新分区)` 缺失时，取 k=1 原样返回。
    """
    price = _finite(now)
    if price is None:
        return None, False
    pc = _finite(pre_close)
    qfq = _finite(qfq_latest)
    if pc is None or qfq is None or pc <= 0 or qfq <= 0:
        return price, False
    k = qfq / pc
    if not math.isfinite(k) or k <= 0 or abs(k - 1.0) < 1e-12:
        return price, False
    return price * k, True


def scale_comparable(sd: Any, anchor_sd: Any, ratio: float = COMPARABLE_RATIO) -> bool:
    """该回看点 run 的分数尺度与锚点是否同口径（离散度之比在 ratio 之内）。

    任一侧未知 → True（判不了就别报警，免得把表涂满黄条）。
    已知且非正 → False：分数全同意味着没有区分度，不是「完美一致」。

    ⚠️ **这个判据只查「量纲同档」，不查「是不是同一个模型」——它的能力上限如此。**
    实测反例：`asof=2026-09-14` 时 T-3 选中 `run_20260908_eccdc1d2`（sd 0.199）
    对锚点 `run_20260911_ee65f7c2`（sd 0.205），比值 0.97 → 判 True；而这两个 run
    的分数**相关系数 −0.03**（2026-09-09 那天实测）。离散度相近完全可以建立在
    互不相关之上。

    所以 `comparable=True` 的准确读法是「这行的涨跌幅与命中率算得没错、
    量纲没串」，**不是**「三天量的是同一个模型」。要回答后一个问题得看每行回传的
    `run_id` / `model_version`。宁可把这条写死，也不要让界面替判据吹牛。
    """
    a = _finite(sd)
    b = _finite(anchor_sd)
    if a is None or b is None:
        return True
    if a <= 0 or b <= 0:
        return False
    r = a / b
    return (1.0 / ratio) <= r <= ratio


def bucket_stats(
    rows: list[dict[str, Any]], bucket_pct: float = DEFAULT_BUCKET_PCT
) -> dict[str, Any]:
    """按分位切高分/低分档，另附负分档，算均涨、命中率、价差。

    `rows` 每项：`{"pct": 0..1 | None, "score": float | None, "ret": 小数 | None}`。

    命中率方向**相反**才是对的：高分档看「上涨占比」，低分档看「下跌占比」。
    所有缺失一律 None，**尤其 `spread` 不能兜成 0.0**——0 价差是在宣称
    「模型无区分度」，那是事实主张，缺数据不是事实主张。
    """
    hi = [r for r in rows if _pct_ok(r.get("pct")) and r["pct"] >= 1.0 - bucket_pct]
    lo = [r for r in rows if _pct_ok(r.get("pct")) and r["pct"] <= bucket_pct]
    neg = [r for r in rows if _finite(r.get("score")) is not None and r["score"] < 0]

    hi_avg = _mean_ret(hi)
    lo_avg = _mean_ret(lo)
    return {
        "sample": len(rows),
        "n_hi": len(hi),
        "n_lo": len(lo),
        "n_neg": len(neg),
        "missing_price": sum(1 for r in rows if _finite(r.get("ret")) is None),
        "hi_avg": hi_avg,
        "lo_avg": lo_avg,
        "spread": (hi_avg - lo_avg)
        if (hi_avg is not None and lo_avg is not None)
        else None,
        "hi_hit": _hit_rate(hi, above=True),
        "lo_hit": _hit_rate(lo, above=False),
        "neg_avg": _mean_ret(neg),
        "avg_score_hi": _mean_score(hi),
        "avg_score_lo": _mean_score(lo),
    }


def summarise_day(
    rows: list[dict[str, Any]], bucket_pct: float = DEFAULT_BUCKET_PCT
) -> dict[str, Any]:
    """一天的分数分布 + 分档统计（`score_std` 供口径一致性判定）。"""
    scores = [s for s in (_finite(r.get("score")) for r in rows) if s is not None]
    stats = bucket_stats(rows, bucket_pct)
    stats["score_std"] = _std(scores)
    stats["score_min"] = min(scores) if scores else None
    stats["score_max"] = max(scores) if scores else None
    return stats


# --- 内部小工具 -------------------------------------------------------------


def _finite(v: Any) -> float | None:
    """→ float，或 None（None / 非数 / NaN / inf）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct_ok(v: Any) -> bool:
    return _finite(v) is not None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _mean_ret(rows: list[dict[str, Any]]) -> float | None:
    return _mean([r for r in (_finite(x.get("ret")) for x in rows) if r is not None])


def _mean_score(rows: list[dict[str, Any]]) -> float | None:
    return _mean([s for s in (_finite(x.get("score")) for x in rows) if s is not None])


def _hit_rate(rows: list[dict[str, Any]], *, above: bool) -> float | None:
    """有价样本中满足方向的占比；无有效样本 → None（不是 0.0）。"""
    rets = [r for r in (_finite(x.get("ret")) for x in rows) if r is not None]
    if not rets:
        return None
    hit = sum(1 for r in rets if (r > 0 if above else r < 0))
    return hit / len(rets)


def _std(values: list[float]) -> float | None:
    """样本标准差（n<2 → None）。"""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


# ---------------------------------------------------------------------------
# 取数层
# ---------------------------------------------------------------------------

# 选 run：每日最新一条（排除 realtime 热集批），再取其全部行并现算名次/分位。
#
# 为什么 `ORDER BY created_at DESC` 而不是「取覆盖最广的 run」——实测 2026-09-14
# 覆盖最广的是 5189 只的**窄口径族**，而锚点与 T-3 都在 3271 只的宽口径族，
# 按覆盖取会让 T-5 掉到另一个族。按 created_at 取，实测 D0/T-3/T-5 恰好同族
# （09-18、09-19 两次回填都把宽口径族写在了最后），T-10 则确实无同族 run 可用。
# 这不是保证，所以调用方必须按行看 `scale_comparable`。
#
# 说明：`tenant_id` 默认 'default' 与 `/stock-terminal/list` 保持一致，
# 保证回看表与左侧列表看到的是同一批数据；`user_id` 不参与过滤（实测批量写在
# '10000001'、实时写在 'system'，按登录用户过滤会静默返回 0 行）。
_SCORES_SQL = """
WITH run_pick AS (
    SELECT DISTINCT ON (trade_date) trade_date, run_id
    FROM engine_signal_scores
    WHERE tenant_id = :tid
      AND trade_date IN :ds
      AND fusion_score IS NOT NULL
      AND COALESCE(source, 'batch') <> 'realtime'
      {mwhere}
    ORDER BY trade_date, created_at DESC, run_id DESC
)
SELECT e.trade_date, e.run_id, e.symbol, e.fusion_score, e.signal_side,
       RANK() OVER (
           PARTITION BY e.trade_date ORDER BY e.fusion_score DESC
       ) AS day_rank,
       PERCENT_RANK() OVER (
           PARTITION BY e.trade_date ORDER BY e.fusion_score ASC
       ) AS day_pct,
       COUNT(*) OVER (PARTITION BY e.trade_date) AS day_n,
       e.model_version
FROM engine_signal_scores e
JOIN run_pick p ON p.trade_date = e.trade_date AND p.run_id = e.run_id
WHERE e.tenant_id = :tid AND e.fusion_score IS NOT NULL
"""

_MODEL_FILTER = (
    "AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :m)"
)


_M_DATE_COVERAGE_SQL = """
SELECT trade_date
FROM engine_signal_scores
WHERE tenant_id = :tid AND trade_date >= CURRENT_DATE - INTERVAL '180 days'
  AND fusion_score IS NOT NULL
  {awhere}
  {mwhere}
GROUP BY trade_date
HAVING COUNT(DISTINCT symbol) >= :min_cov
ORDER BY trade_date DESC
LIMIT :k
"""


async def fetch_signal_ladder(
    lookbacks: list[int],
    model: str | None = None,
    tenant_id: str = "default",
    asof: str | None = None,
) -> list[str]:
    """降序信号日阶梯，取到锚点 + max(lookbacks) 个交易日。

    `HAVING` 在 `LIMIT` **之前**生效，所以阶梯里每一天都单独满足覆盖阈值，
    `ladder[N]` 直接就是 T-N，不需要再校验。`asof` 给定时锚点前移到该日（含）。
    """
    from sqlalchemy import text

    from backend.services.api.routers.stock_terminal import _MIN_SIGNAL_COVERAGE
    from backend.shared.database_manager_v2 import get_session

    want = max(lookbacks) + 1 if lookbacks else 1
    params: dict[str, Any] = {
        "tid": tenant_id,
        "min_cov": _MIN_SIGNAL_COVERAGE,
        "k": want,
    }
    mwhere = ""
    if model:
        mwhere = _MODEL_FILTER
        params["m"] = model
    awhere = ""
    asof_d = parse_asof(asof)
    if asof_d:
        awhere = "AND trade_date <= :asof"
        params["asof"] = asof_d  # 必须是 date：asyncpg 不吃字符串（见 parse_asof）

    sql = _M_DATE_COVERAGE_SQL.format(awhere=awhere, mwhere=mwhere)
    async with get_session() as session:
        rows = (await session.execute(text(sql), params)).fetchall()
    return [_iso(r[0]) for r in rows]


async def fetch_lookback_scores(
    dates: list[str], model: str | None = None, tenant_id: str = "default"
) -> list[dict[str, Any]]:
    """三个回看日的分数面板（每日单 run + 现算名次/分位）。"""
    if not dates:
        return []
    from datetime import date as _date

    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    params: dict[str, Any] = {
        "tid": tenant_id,
        "ds": tuple(_date.fromisoformat(_iso(d)) for d in dates),
    }
    mwhere = ""
    if model:
        mwhere = _MODEL_FILTER
        params["m"] = model

    sql = _SCORES_SQL.format(mwhere=mwhere)
    stmt = text(sql).bindparams(bindparam("ds", expanding=True))
    async with get_session() as session:
        rows = (await session.execute(stmt, params)).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        raw_sym = str(r[2])
        canon = canonical_symbol(raw_sym)
        out.append(
            {
                "signal_date": _iso(r[0]),
                "run_id": str(r[1]),
                "raw_symbol": raw_sym,
                "symbol": canon,
                "score": _finite(r[3]),
                "side": str(r[4] or "HOLD"),
                "rank": int(r[5]) if r[5] is not None else None,
                "pct": _finite(r[6]),
                "day_n": int(r[7]) if r[7] is not None else None,
                "model_version": str(r[8]) if r[8] else None,
            }
        )
    drift = sum(1 for x in out if x["symbol"] is None)
    if drift:
        # 不静默：转不出来的票拿不到行情，会在明细里显示为缺价。占比异常时能第一时间发现。
        logger.info("信号回看：%d/%d 条 symbol 无法归一到 A 股口径", drift, len(out))
    return out


def latest_partition_on_or_before(iso_date: str) -> str | None:
    """不晚于 `iso_date` 的最近行情分区（`YYYYMMDD`）。

    ⚠️ `trading_days_until` 返回**降序**、`[0]` 最新；hub 内部的 `_partition_dates`
    是升序，两者别混用（变量名带 `_desc` 就是为了防这个）。
    """
    from backend.services.api.market_analysis_shared.market_days import (
        trading_days_until,
    )
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    data_dir = QuantDBDataHub.get_instance().data_dir
    days_desc = trading_days_until(REL_DAILY_FORWARD, data_dir, _ymd(iso_date), 1)
    return days_desc[0] if days_desc else None


def fetch_close_panel(ymd_dates: list[str]) -> pd.DataFrame:
    """按精确分区读前复权收盘，返回 `symbol / close / dt`。

    `cols` 里**不要带 `dt`**——`_read_partitioned` 自己会补 `, dt`，重复会多出
    `dt_1` 列。整分区读（不绑定 5000 个 symbol）实测 4 分区 0.12s，比 IN 绑定更快也更简单。
    """
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    wanted = sorted({d for d in ymd_dates if d})
    if not wanted:
        return pd.DataFrame()
    df = QuantDBDataHub.get_instance()._read_partitioned(
        REL_DAILY_FORWARD, wanted, cols='symbol, "close"'
    )
    if df.empty:
        return df
    df = df.drop(columns=[c for c in ("dt_1",) if c in df.columns])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def load_live_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Redis `market:snapshot:*` 实时价：suffix symbol → `{now, pre_close}`。

    契约字段见 `backend/shared/tdx_aidata/collector.py`（`Now` / `PreClose` /
    `timestamp`）。Redis 未配置、超时或该票无快照 → 该票不出现（调用方回退收盘价）。
    新鲜度一律走 `freshness.quote_policy()` 这个唯一谓词，不自己定阈值。

    实测 pipelined HMGET × 3300 ≈ 0.12s，所以不做 scan 预筛。
    """
    if not symbols:
        return {}
    try:
        from backend.shared.freshness import UNAVAILABLE, quote_policy
        from backend.shared.remote_quote_config import make_sync_client

        client = make_sync_client(socket_timeout=2.0, socket_connect_timeout=1.5)
        if client is None:
            return {}

        keys = [
            f"market:snapshot:{StockCodeUtil.to_prefix(s).lower()}" for s in symbols
        ]
        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.hmget(key, "Now", "PreClose", "timestamp")
        results = pipe.execute()

        policy = quote_policy()
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        # strict=True：results 与 keys/symbols 一一对应，长度不等说明 pipeline 用法坏了，
        # 静默截断会让「没取到报价」被误判成「这些票就是没快照」。
        for symbol, (price, pre_close, ts) in zip(symbols, results, strict=True):
            p = _finite(price)
            if p is None or p <= 0:
                continue
            stamp = _finite(ts)
            if stamp is None or stamp <= 0:
                continue
            if stamp > 1e12:  # 毫秒戳（部分写入方）
                stamp /= 1000.0
            if policy.classify_ts(stamp, now) == UNAVAILABLE:
                continue
            out[symbol] = {"now": p, "pre_close": _finite(pre_close)}
        return out
    except Exception as exc:  # noqa: BLE001 — 实时是增强位，失败必须降级而非中断
        logger.warning("实时快照读取失败，回退收盘价：%s", exc)
        return {}


def parse_asof(raw: Any) -> date | None:
    """`'YYYY-MM-DD'` / `'YYYYMMDD'` → `date`；空值 → None；非法格式抛 `ValueError`。

    ⚠️ 这个函数存在的唯一理由：asyncpg 绑 DATE 参数**只认 `date` 对象**，传字符串
    会炸 `'str' object has no attribute 'toordinal'`——是 500，不是查空。

    紧凑式（`20260914`，QuantDB 分区口径）**显式**分支解析：`date.fromisoformat`
    对它的接受度随 Python 版本变（3.11+ 收、3.10 拒），留着会让同一次请求
    在本机与容器里得到不同结果。
    """
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    head = s[:8]
    if "-" not in s[:10] and len(head) == 8 and head.isdigit():
        return date(int(head[:4]), int(head[4:6]), int(head[6:8]))
    return date.fromisoformat(s[:10])


def _iso(v: Any) -> str:
    """date / datetime / 字符串 → `YYYY-MM-DD`。"""
    s = str(v)
    return s[:10]


def _ymd(iso_date: str) -> str:
    return str(iso_date).replace("-", "")[:8]
