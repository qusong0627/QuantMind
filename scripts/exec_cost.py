#!/usr/bin/env python3
"""执行损耗（TCA）口径的唯一出处：一段价格，三段读。

    ref_px（决策基准价：下单时模型/行情看到的价）
      → limit_px（报出去的限价：买入 = 基准 +1%，卖出 = 基准 −1%）
      → fill_px（成交价：桥回报，坏 tick 已过护栏）

本仓库此前只度量"选得对不对"（decision_track 明确写着"不衡量执行滑点"），
下单这件事花掉的钱没有任何账。TCA 补的就是这一层。

**符号约定（全局唯一，报告与告警都按它读）**：
  * `slip_bps` **正号 = 比基准差**。买入成交价高于基准 = +；卖出成交价低于
    基准 = +。两侧同号同义，报告不用翻译方向。
  * `cushion_used_bps`：0 = 按基准成交，100 = 成交在限价上（把 ±1% 缓冲吃满），
    负 = 优于基准（买入成交在基准下方 / 卖出成交在基准上方）。

**为什么单独一个模块**：这三个公式此前不存在；一旦存在，就必须只有一份——
"两边各写一遍、涨跌方向各翻一次符号"是本仓库三次漂移教训的标准剧本
（限价口径、虚拟现金口径、涨跌停口径都是这么漂的）。

失效姿态（fail-open，与 observation 层同纪律）：任何缺项/脏值 → 返回 None，
不抛异常、不吐 NaN。None 的语义是"不可定价"，与"0 bps（执行完美）"严格区分；
报告必须把两者分开计数，否则"没数据"会被读成"没问题"。

已知边界（不许在报告里装作它们不存在）：
  * 手续费/印花税**不在账**：桥只回报成交价量，账本无费用字段；
  * `fill_ts` 是**观察到成交**的时刻（wait_fill 轮询粒度 3s），不是交易所
    成交时刻，用途是量级判断（秒 vs 分钟），不能当毫秒级延迟；
  * `decided_ts` 是**轮级**的（同轮各单共用决策产出时刻），不是逐单决策时刻。
"""
from __future__ import annotations

import math

#: 成交行识别：这些 mode 是"真的成交了"（fill_abort=零成交终态不算成交）
FILL_MODES = ("execute", "execute_intraday", "execute_us", "fill_confirm")
#: 这些 mode 的**确认成交**行带 fill 载荷（wait_fill/reconcile 的回报）；没有载荷的
#: 同名行是提交回执或挂队 trace（见 is_submit_trace）
_PAYLOAD_MODES = ("execute", "execute_intraday", "execute_us")
#: tape_fields 写进流水的键（顺序即人读顺序）；值为 None 的键直接不写
FIELDS = ("side", "ref_px", "limit_px", "fill_px", "wanted", "filled",
          "decided_ts", "submit_ts", "fill_ts", "tca_path")
#: 老行没有 tca_path：按 mode 归一到接线后同一套路径词表（跨期同组比较）。
#: execute_us 归 "us"（该路径尚未接线，行会在报告里落 "us/*" 不可定价组）。
_PATH_BY_MODE = {"execute": "llm_trade", "execute_intraday": "hourly",
                 "execute_us": "us", "fill_confirm": "reconcile"}
_SIDES = ("buy", "sell")


def _num(v) -> float | None:
    """转正数价格；不可用（None/非数字/NaN/inf/≤0）→ None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _side(side) -> str:
    s = str(side or "").strip().lower()
    return s if s in _SIDES else ""


def slip_bps(side: str, ref_px, fill_px) -> float | None:
    """成交价相对决策基准的滑点（bps）。**正 = 比基准差**（见模块 docstring）。

    买入 (fill−ref)/ref；卖出 (ref−fill)/ref；两者 ×1e4。
    """
    s, ref, fill = _side(side), _num(ref_px), _num(fill_px)
    if not (s and ref and fill):
        return None
    raw = (fill - ref) / ref if s == "buy" else (ref - fill) / ref
    return round(raw * 1e4, 2)


def cushion_used_bps(side: str, ref_px, limit_px, fill_px) -> float | None:
    """限价缓冲用尽度（%）：成交价从基准走向限价走了多少。

    0 = 成交在基准上；100 = 成交在限价上（缓冲吃满，再差就废单）；负 = 优于基准。
    零缓冲（限价==基准）不可定义 → None。
    """
    s, ref, limit, fill = _side(side), _num(ref_px), _num(limit_px), _num(fill_px)
    if not (s and ref and limit and fill):
        return None
    window = (limit - ref) if s == "buy" else (ref - limit)
    if not math.isfinite(window) or abs(window) < 1e-12:
        return None
    move = (fill - ref) if s == "buy" else (ref - fill)
    return round(move / window * 100.0, 2)


def tape_fields(side: str, ref_px=None, limit_px=None, fill_px=None, wanted=None,
                filled=None, decided_ts: str = "", submit_ts: str = "",
                fill_ts: str = "", path: str = "") -> dict:
    """构造写进成交流水的 TCA 字段（**键名与缺省语义只此一处**）。

    调用方写法：`log_line({...原有字段..., **exec_cost.tape_fields(...)})`。
    值为 None/脏的键**不写**（不写 null）——报告按"键不存在 = 不可定价"单一语义读。
    未知方向返回 {}（宁可少记一条，不记半条无法归属的行）。本函数不抛异常。
    """
    try:
        if not _side(side):
            return {}
        out: dict = {"side": _side(side)}
        for k, v in (("ref_px", ref_px), ("limit_px", limit_px), ("fill_px", fill_px)):
            n = _num(v)
            if n is not None:
                out[k] = n
        for k, v in (("wanted", wanted), ("filled", filled)):
            if v is None or isinstance(v, bool):
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                out[k] = iv
        for k, v in (("decided_ts", decided_ts), ("submit_ts", submit_ts),
                     ("fill_ts", fill_ts), ("tca_path", path)):
            sv = str(v or "").strip()
            if sv:
                out[k] = sv
        return {k: out[k] for k in FIELDS if k in out}
    except Exception:  # noqa: BLE001 — 观测层绝不反噬交易链路
        return {}


def is_submit_trace(row: dict) -> bool:
    """提交回执/在途 trace：mode 与成交同名，但**不是成交**，必须排除。

    2026-08-31 / 09-01 实录：执行路径在提交时刻写一行
    `{mode: execute, price: 限价, volume: 委托量, result: {status: submitted...}}`。
    它与同委托号的成交行长得几乎一样——被当成交读，就会生成"按我们自己报的限价
    成交"的幽灵样本（滑点恒等于 +1% 缓冲），且没有任何症状。

    判据按行的固有字段（不猜语义）：
      * 带 `result`（桥受理回执）/ `pending` / `untracked`（在途 trace）；
      * execute 系 mode 却**没有 fill 载荷**——确认成交由 wait_fill/reconcile 回报，
        两类行分别带 fill 字典 / 顶层 order_id。
    """
    if row.get("result") or row.get("pending") or row.get("untracked"):
        return True
    mode = str(row.get("mode") or "")
    if mode in _PAYLOAD_MODES and not isinstance(row.get("fill"), dict):
        return True
    return mode == "fill_confirm" and not row.get("order_id")


def normalize_row(row: dict, legacy_limit=None) -> dict | None:
    """成交流水的一行 → 统一样本 dict；不是成交行 → None。

    只认**真的成交**：fill 行（mode ∈ FILL_MODES、带 fill 载荷、有成交价与成交量）
    与 reconcile 的 fill_confirm 行。委托行（提交回执/在途 trace/error/fill_abort）
    一律 None——把"没成交"混进滑点样本，等于把废单算成"执行得好"，把回执算成
    "按限价成交"（is_submit_trace）。

    legacy_limit：历史行没有 limit_px 时由调用方按委托号从日志补齐（买限价可从
    stdout 的受理行恢复；**卖限价历史上没落盘**，补不上就留 None）。补出限价
    **不**据此臆造 ref_px——知道我们报了什么价，不等于知道当时的基准价。
    """
    if not isinstance(row, dict):
        return None
    mode = str(row.get("mode") or "")
    if mode not in FILL_MODES or row.get("error") or is_submit_trace(row):
        return None
    side = _side(row.get("side"))
    fill = _num(row.get("price"))
    filled = row.get("volume")
    try:
        filled_i = int(filled)
    except (TypeError, ValueError):
        filled_i = 0
    if not (side and fill and filled_i > 0):
        return None
    wanted = row.get("wanted")
    if wanted is None and row.get("remaining") is not None:
        try:
            wanted = filled_i + int(row.get("remaining"))
        except (TypeError, ValueError):
            wanted = None
    oid = row.get("order_id") or ((row.get("fill") or {}).get("order_id")
                                  if isinstance(row.get("fill"), dict) else "")
    limit = row.get("limit_px")
    if _num(limit) is None and legacy_limit is not None:
        limit = legacy_limit
    return {
        "ts": str(row.get("ts") or ""),
        "date": str(row.get("ts") or "")[:10],
        "mode": mode,
        "agent": str(row.get("agent") or ""),
        "code": str(row.get("code") or ""),
        "side": side,
        "order_id": str(oid or ""),
        "fill_px": fill,
        "ref_px": _num(row.get("ref_px")),
        "limit_px": _num(limit),
        "filled": filled_i,
        "wanted": int(wanted) if wanted else None,
        "decided_ts": str(row.get("decided_ts") or ""),
        "submit_ts": str(row.get("submit_ts") or ""),
        "fill_ts": str(row.get("fill_ts") or ""),
        "path": str(row.get("tca_path") or _PATH_BY_MODE.get(mode) or mode),
    }


def _pct(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位（n=1 时返回该值）。空列表 → None。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo), 2)


def _minutes(a: str, b: str) -> float | None:
    """b − a 的分钟数；任一解析不出 → None。"""
    from datetime import datetime

    try:
        ta, tb = datetime.fromisoformat(str(a)), datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return None
    return (tb - ta).total_seconds() / 60.0


def summarize(rows: list[dict], zero_fill_orders: int = 0, *, _grouped: bool = False) -> dict:
    """一组成交样本 → 统计读数（**加权口径在这里定死**）。

    * 加权均值 `slip_bps_w` 按**成交额**加权（fill_px×filled，缺成交量时退化为
      计数权重）：一笔 1 万股的单不该和 100 股的单等权——"钱花在哪"问的是钱。
    * 简单均值/中位/p10/p90 一并给出：均值对极端值敏感，中位数说明典型情况。
    * `n` 只数**可定价**的样本（ref+fill 都有）；缺 ref 的进 `n_unpriced`。
    * 成交率 = 成交笔数 / (成交笔数 + 零成交终态笔数)；去重按委托号由调用方
      先行完成（本函数只算术，不做 IO 也不猜）。
    """
    rows = [r for r in rows if isinstance(r, dict)]
    priced, unpriced = [], []
    for r in rows:
        (priced if (r.get("ref_px") and r.get("fill_px")) else unpriced).append(r)

    slips, weights, lat_d2s, lat_s2f = [], [], [], []
    for r in priced:
        got = slip_bps(r["side"], r["ref_px"], r["fill_px"])
        if got is None:
            unpriced.append(r)
            continue
        slips.append(got)
        notional = (r.get("fill_px") or 0) * (r.get("filled") or 0)
        weights.append(notional if notional > 0 else 1.0)
        m = _minutes(r.get("decided_ts"), r.get("submit_ts"))
        if m is not None:
            lat_d2s.append(m)
        m = _minutes(r.get("submit_ts"), r.get("fill_ts"))
        if m is not None:
            lat_s2f.append(m)

    n = len(slips)
    w_sum = sum(weights)
    out = {
        "n": n,
        "n_rows": len(rows),
        "n_unpriced": len(unpriced),
        "slip_bps_w": round(sum(s * w for s, w in zip(slips, weights)) / w_sum, 2) if w_sum else None,
        "slip_bps_mean": round(sum(slips) / n, 2) if n else None,
        "slip_bps_med": _pct(sorted(slips), 0.5),
        "slip_bps_p10": _pct(sorted(slips), 0.10),
        "slip_bps_p90": _pct(sorted(slips), 0.90),
        "notional": round(sum(w for w in weights if w > 1.0), 2),
        "decide_to_submit_min_med": _pct(sorted(lat_d2s), 0.5),
        "submit_to_fill_min_med": _pct(sorted(lat_s2f), 0.5),
        "n_orders": len(rows) + max(int(zero_fill_orders or 0), 0),
        "fill_rate": (round(len(rows) / (len(rows) + zero_fill_orders), 4)
                      if (len(rows) + zero_fill_orders) > 0 else None),
    }
    if not _grouped:
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(f"{r.get('path') or '?'}/{r.get('side') or '?'}", []).append(r)
        out["groups"] = {k: summarize(v, _grouped=True) for k, v in sorted(groups.items())}
    return out
