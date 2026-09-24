"""执行损耗（TCA）口径的**唯一出处**：成交价相对基准/限价的偏差（纯函数）。

本模块回答一个此前没人回答的问题：**"这一单实际花了多少执行成本"**。此前本仓
有的三个近似回答都不是它：

* ``fill_quality.fidelity_metrics``（F2 保真度）——**模拟**撮合得像不像，基准取
  当日收盘，回答"我们的撮合模型可信吗"；
* ``shadow_compare.compute_slippage_realization``——真实成交价 vs 模拟成交价，
  回答"影子盘跟着赚了还是亏了"；
* ``qmt_mirror`` 的 reconcile ——虚拟账本 vs 真单在**镜像那一条腿**上的价差。

三者都不回答"这批真单相对**决策时点的参考价**各付出了多少"。本模块给的就是这一
层，且与 F2 共用同一条公式（见下），避免两块看板各写一份符号约定、各自自测全绿
却给出相反的故事。

符号约定（对外契约，改它就是改报告的含义）
------------------------------------------------

``slip_bps``：**两侧正号同义 = 比基准差（吃亏）**。

===========================================  ==============================
买入成交价 > 基准                              正（付得比基准贵）
卖出成交价 < 基准                              正（卖得比基准便宜）
买入成交价 < 基准 / 卖出成交价 > 基准           负（占了便宜）
===========================================  ==============================

即 ``方向 × (成交/基准 − 1) × 1e4``，方向 +1 买 / −1 卖。报告因此可以直接说
"这批单均价 +12.3 bps"而不必按方向分两句话解释——分方向解释的报表，读的人迟早
会把其中一侧的符号看反。

``cushion_used_bps``：**缓冲用尽度**。0 = 成交在基准上（一点缓冲没用），
100 = 成交在限价上（把基准与限价之间的缓冲吃干净），负 = 优于基准。
零缓冲（限价 == 基准）时该比值不可定义 → ``None``，不是除零崩、更不是 0。

``None`` 不是 ``0``
----------------------

缺项/零价/非数字/未知方向一律返回 ``None``。``0.0`` 在本模块的语义是"与基准分毫不
差"（一个**结论**），``None`` 的语义是"这笔我们不知道"（一个**缺口**）。聚合层据此
把两者分别计入 ``n`` 与 ``n_unpriced``；把缺口算成 0 会让报告在样本最脏的那天显得
最漂亮。

与其他模块的关系
------------------

``backend/shared/fill_quality.py`` 的成交价偏差**反向引用本模块**的 :func:`slip_bps`
（差别只在基准取谁：F2 取当日收盘，TCA 取决策时点参考价）。本模块是纯函数、只依赖
标准库，可以被任何服务导入而不拖进风控引擎或数据库层。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

__all__ = [
    "cushion_used_bps",
    "direction_of",
    "merge_orders",
    "slip_bps",
    "summarize",
    "tca_path",
]

#: 方向 → 符号。买 +1 / 卖 −1。**与 ``fill_quality._DIRECTION`` 同一张表**，故只留这一份。
_DIRECTION: dict[str, float] = {"buy": 1.0, "sell": -1.0}


def _num(value: Any) -> float | None:
    """任意输入 → 有限浮点，否则 ``None``。

    ``bool`` 走 ``float()` 会得到 0.0/1.0——价格位置上出现布尔只可能是上游写错了
    字段，这里按"不是价格"处理，宁可计入不可定价也不要把 ``True`` 当 1 元。
    非有限值（NaN/±inf）一律 ``None``：它们会污染 ``mean`` 与分位数，且无法与
    "没有值"区分。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def direction_of(side: Any) -> float | None:
    """方向字符串 → +1 买 / −1 卖，认不出返回 ``None``（大小写与空白不敏感）。"""
    if not isinstance(side, str):
        return None
    return _DIRECTION.get(side.strip().lower())


def slip_bps(side: Any, ref_px: Any, fill_px: Any) -> float | None:
    """成交价相对基准的偏差（bps），**正 = 比基准差**。不可定价 → ``None``。

    买卖两侧同号同义（见模块 docstring）。基准价 ``ref_px`` 与成交价 ``fill_px``
    都必须为正的有限数；任何一项缺失/为零/非数字，返回 ``None`` 而不是抛。
    """
    direction = direction_of(side)
    ref = _num(ref_px)
    fill = _num(fill_px)
    if direction is None or ref is None or fill is None or ref <= 0 or fill <= 0:
        return None
    return direction * (fill / ref - 1.0) * 1e4


def cushion_used_bps(side: Any, ref_px: Any, limit_px: Any, fill_px: Any) -> float | None:
    """缓冲用尽度（%）：0 = 成交在基准上，100 = 成交在限价上，负 = 优于基准。

    "缓冲"= 基准与限价之间的距离（买入在上方、卖出在下方）。限价 == 基准时缓冲为
    零、比值不可定义 → ``None``（沿街报"用尽 0%"是把"没缓冲可用"说成"一点没用"）。
    """
    direction = direction_of(side)
    ref = _num(ref_px)
    limit = _num(limit_px)
    fill = _num(fill_px)
    if direction is None or ref is None or limit is None or fill is None:
        return None
    if ref <= 0 or limit <= 0 or fill <= 0:
        return None
    buffer_bps = direction * (limit / ref - 1.0) * 1e4
    if buffer_bps == 0.0:
        return None
    used_bps = direction * (fill / ref - 1.0) * 1e4
    return used_bps / buffer_bps * 100.0


# ---------------------------------------------------------------- 路径归属

#: 幂等号前缀 → 执行腿。前缀字面量取自各条腿**真正造键的地方**，不是别处抄的
#: （``orders.client_order_id`` 由这些函数造出来）：``order_contract`` 的
#: ``build_llm_decision_client_order_id``/``build_candidate_client_order_id``/
#: ``build_copilot_client_order_id``、镜像的 ``real_mirror_service._mirror_cid``、
#: 以及强平族的 cid 前缀（登记表见 ``backend/tests/test_forced_exit_prefix_registries.py``）。
#: 长前缀在前：``flatten-`` 必须先于 ``flat-`` 判（当前两者不冲突，但改前缀的人不会
#: 记得这条，顺序写死比注释可靠）。
_CID_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sltp-", "sltp"),
    ("trim-", "trim"),
    ("flatten-", "flatten"),
    ("flat-", "flatten"),
    ("mir-", "mirror"),
    ("lld-", "llm_decision"),
    ("cand-", "candidate_push"),
    ("cop-", "co_pilot"),
    ("manual-", "manual"),
    ("auto-", "hosted"),
)

#: 备注前缀 → 执行腿（**兜底**，见 :func:`tca_path`）。``forced-exit:`` 归入 flatten：
#: 分组问的是"这条腿的成交机制是什么"（保护价/平仓清单 vs 常规限价），不是"哪个字符串
#: 出现在备注里"——两者都是平仓腿，拆成两组只会让每组的样本都不够下结论。
_REMARK_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sltp:", "sltp"),
    ("trim:", "trim"),
    ("flatten:", "flatten"),
    ("forced-exit:", "flatten"),
    ("mirror:", "mirror"),
)

#: 兜底路径：认不出就是认不出，**不猜**（猜错 = 把别的腿的样本拉进自己组里，
#: 而组内样本数正是判读纪律"够了没有"的依据）。
PATH_UNKNOWN = "unknown"

#: 只有 LLM 决策腿会写 ``orders.agent``（非 LLM 腿恒 NULL）。它是**最后一道**兜底：
#: 决策腿的幂等号是 ``lld-``，正常路径上永远轮不到这里。
PATH_LLM_DECISION = "llm_decision"


def tca_path(
    client_order_id: Any = None, remarks: Any = None, agent: Any = None
) -> str:
    """一条订单 → 执行腿名（报告按它分组）。

    判据顺序**不可调换**：``client_order_id`` → ``remarks`` → ``agent``。

    为什么幂等号优先：``remarks`` 在本仓是**活字段**——成交回报到达时
    ``qmt_exec_reconciler.apply_execution_report`` 会拿券商消息**覆盖**它
    （``order.remarks = msg``），派发层也会给它加去重前缀。按备注判路径，一笔
    ``mir-`` 单成交后会从 mirror 组跳到 unknown，同一笔单在两个口径下各出现一次。

    ``remarks`` 因此只能是兜底，且判据限定在**以空白切分后的每个 token 的开头**：
    备注可能是 ``"{去重标记} sltp:保护性止损"``（派发层拼接），前缀匹配会漏、
    子串匹配会把正文里恰好提到 "trim:" 的普通单误判成减仓腿。
    """
    cid = str(client_order_id or "").strip()
    if cid:
        for prefix, path in _CID_PATH_PREFIXES:
            if cid.startswith(prefix):
                return path
    for token in str(remarks or "").split():
        for prefix, path in _REMARK_PATH_PREFIXES:
            if token.startswith(prefix):
                return path
    return PATH_LLM_DECISION if str(agent or "").strip() else PATH_UNKNOWN


# ---------------------------------------------------------------- 合并同单多笔

#: 合并时按**首笔**取的字段（这行单的身份 + 下单时的决定，不随成交笔数变化）。
_IDENTITY_FIELDS: tuple[str, ...] = (
    "ts",
    "date",
    "symbol",
    "side",
    "order_id",
    "agent",
    "path",
    "ref_px",
    "limit_px",
    "wanted",
    "decided_ts",
    "submit_ts",
)


def merge_orders(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一委托号的多笔成交 → 一行（成交价按量加权）。

    为什么要合并：一笔 10000 股的单被拆成 3 笔成交后，若不合并，报告里这**一笔单**
    会变成 3 个样本，按笔数看是"3 笔小单"——分位数、成交笔数、按腿分组的 n 全都被
    分部的粒度带偏，而分部粒度是券商撮合的随机结果，不是我们的执行质量。

    组内取值规则（写死在这里，读数面不再各自决定）：

    * ``filled`` 求和；``fill_px`` 按**成交量**加权（不是简单平均）；
    * 身份字段（方向/标的/基准价/限价/决策时刻…）取**输入顺序里第一个非空值**——
      同一委托号上这些值本就该相同，不同说明上游改了单（改价/改量），取首笔即
      "按当初决定的那一版算"，而不是让最后一次改单改写历史；
    * ``fill_ts`` 取**最后一个**非空值（成交完成时刻，不是第一笔的时刻）。

    ``order_id`` 为空的行**不与任何行合并**（邻座无号行是两笔不同的单，只是都没号）：
    合并它们会把两笔方向相反的单按量加权成一条假样本。空 ``order_id`` 的报告后果是
    "样本数偏多"，合并错方向的后果是"滑点符号是假的"——后者不可接受。

    调用方须按成交时刻升序给行（组内"第一行"即首笔成交）。
    """
    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("order_id") or "").strip()
        if not order_id or order_id not in index:
            copy = dict(row)
            if order_id:
                index[order_id] = len(merged)
            merged.append(copy)
            continue
        target = merged[index[order_id]]
        _merge_into(target, row)
    return merged


def _merge_into(target: dict[str, Any], row: dict[str, Any]) -> None:
    """把 ``row``（同一委托号的又一笔成交）并进 ``target``（就地改 target）。"""
    filled = _num(row.get("filled")) or 0.0
    prev_filled = _num(target.get("filled")) or 0.0
    prev_px = _num(target.get("fill_px"))
    new_px = _num(row.get("fill_px"))
    total = prev_filled + filled
    if total > 0 and prev_px is not None and new_px is not None:
        target["fill_px"] = (prev_px * prev_filled + new_px * filled) / total
    elif new_px is not None and prev_px is None:
        target["fill_px"] = new_px
    target["filled"] = total if total > 0 else prev_filled or filled
    # 手续费与成交额同类：**可加**，不是身份字段。留在 _IDENTITY_FIELDS 之外还不够——
    # 不显式求和就会在合并时把第二笔的费用丢掉，报告里"带费用的笔数"随之少算，
    # 而少算的方向恰好是"看起来更便宜"。
    if target.get("fees") is not None or row.get("fees") is not None:
        target["fees"] = round(
            (_num(target.get("fees")) or 0.0) + (_num(row.get("fees")) or 0.0), 4
        )
    for field in _IDENTITY_FIELDS:
        if target.get(field) in (None, "") and row.get(field) not in (None, ""):
            target[field] = row[field]
    if row.get("fill_ts"):
        target["fill_ts"] = row["fill_ts"]


# ---------------------------------------------------------------- 汇总


def _pct(sorted_vals: Sequence[float], q: float) -> float | None:
    """线性插值分位（n=1 时返回该值）。空 → ``None``。

    与 ``fill_quality._percentile`` 的**取整方式不同**（那是给单日小样本用的
    "最近秩"，这里是给跨日分布用的插值）：同一批数在本报告里要能算 p10/p50/p90
    三档并互相自洽，最近秩在小样本下会把三档全压成同一个数。
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(float(sorted_vals[0]), 2)
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(
        float(sorted_vals[lo])
        + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * (pos - lo),
        2,
    )


def _parse_stamp(value: Any) -> datetime | None:
    """ISO 串 → ``datetime``；解析不出 → ``None``。

    ``Z`` 后缀要手工换成 ``+00:00``：本仓的 JSON 出口（``to_utc_iso``）印的是 ``Z``，
    而运行在 Python 3.10 上的 ``datetime.fromisoformat`` **不认** ``Z``（3.11 才支持）
    ——不换的话延迟统计会静默全空，报告里只是"延迟那一格没数"，没人看得出是解析失败。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _minutes(start: Any, end: Any) -> float | None:
    """``end − start`` 的分钟数；任一解析不出 → ``None``（不猜、不算 0）。"""
    first, second = _parse_stamp(start), _parse_stamp(end)
    if first is None or second is None:
        return None
    try:
        return (second - first).total_seconds() / 60.0
    except TypeError:      # aware 与 naive 混用：不做时区猜测，如实缺失
        return None


def summarize(
    rows: Sequence[dict[str, Any]],
    zero_fill_orders: int = 0,
    *,
    _grouped: bool = False,
) -> dict[str, Any]:
    """一组成交样本 → 统计读数（**加权口径在这里定死**）。

    * 加权均值 ``slip_bps_w`` 按**成交额**加权（``fill_px × filled``，成交额算不出
      时退化为计数权重 1）：一笔 1 万股的单不该和 100 股的单等权——"钱花在哪"问的是钱。
    * 简单均值/中位/p10/p90 一并给出：均值对极端值敏感，中位数说明典型情况；
      两个数分道扬镳时，先去看分部成交和极端单，而不是挑一个报出去。
    * ``n`` 只数**可定价**的样本（基准+成交价都有）；缺基准的进 ``n_unpriced``。
    * ``fill_rate`` 分母含零成交终态（``zero_fill_orders``）：只统计成交笔数会把
      "挂了十单成一单"读成 100%。
    * ``groups`` 按 ``路径/方向`` 分组，**组内同式但不再嵌套**（``_grouped``）：
      两条腿的成交机制不同，混算会把差异洗掉；再往下分只会让每组样本数不够。
    """
    usable = [r for r in rows if isinstance(r, dict)]
    priced: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    for row in usable:
        # 判据与 slip_bps 一致（真值判断只是快速分流；真脏值由 slip_bps 兜底，
        # 它的 None 会把样本移进 unpriced，故下面还要再判一次）。
        if row.get("ref_px") and row.get("fill_px"):
            priced.append(row)
        else:
            unpriced.append(row)

    # 延迟与成交额对**全部**样本算，不只在可定价的那批上：
    # 它们与"有没有基准价"无关。挂在一起算的后果是**静默丢数**——某条腿没记基准价时，
    # 报告里那一组的延迟与成交额会一起变成空白，而空白看不出是"没采到"还是"没算"。
    # （隔壁的实现把延迟写在按 ref 过滤后的循环里；那边每行都带 ref，所以看不出来。）
    lat_decide_to_submit: list[float] = []
    lat_submit_to_fill: list[float] = []
    notional_total = 0.0
    for row in usable:
        fill_px = _num(row.get("fill_px"))
        filled = _num(row.get("filled")) or 0.0
        if fill_px is not None and filled > 0:
            notional_total += fill_px * filled
        for start, end, bucket in (
            (row.get("decided_ts"), row.get("submit_ts"), lat_decide_to_submit),
            (row.get("submit_ts"), row.get("fill_ts"), lat_submit_to_fill),
        ):
            minutes = _minutes(start, end)
            if minutes is not None:
                bucket.append(minutes)

    slips: list[float] = []
    weights: list[float] = []
    cushion_used: list[float] = []
    for row in priced:
        got = slip_bps(row.get("side"), row.get("ref_px"), row.get("fill_px"))
        if got is None:
            unpriced.append(row)
            continue
        slips.append(got)
        row_notional = (_num(row.get("fill_px")) or 0.0) * (
            _num(row.get("filled")) or 0.0
        )
        # 成交额算不出的行退化为计数权重 1——加权均值的分母必须有定义。
        weights.append(row_notional if row_notional > 0 else 1.0)
        got_cushion = cushion_used_bps(
            row.get("side"), row.get("ref_px"), row.get("limit_px"), row.get("fill_px")
        )
        if got_cushion is not None:
            cushion_used.append(got_cushion)

    n = len(slips)
    weight_sum = sum(weights)
    ordered = sorted(slips)
    out: dict[str, Any] = {
        "n": n,
        "n_rows": len(usable),
        "n_unpriced": len(unpriced),
        "slip_bps_w": (
            round(sum(s * w for s, w in zip(slips, weights)) / weight_sum, 2)
            if weight_sum
            else None
        ),
        "slip_bps_mean": round(sum(slips) / n, 2) if n else None,
        "slip_bps_med": _pct(ordered, 0.5),
        "slip_bps_p10": _pct(ordered, 0.10),
        "slip_bps_p90": _pct(ordered, 0.90),
        # 全样本成交额（含不可定价的那些）：回答"这批单一共做了多少钱"，
        # 与"能不能定价"无关。
        "notional": round(notional_total, 2),
        "cushion_used_med": _pct(sorted(cushion_used), 0.5),
        "decide_to_submit_min_med": _pct(sorted(lat_decide_to_submit), 0.5),
        "submit_to_fill_min_med": _pct(sorted(lat_submit_to_fill), 0.5),
        "n_orders": len(usable) + max(int(zero_fill_orders or 0), 0),
        "fill_rate": (
            round(len(usable) / (len(usable) + zero_fill_orders), 4)
            if (len(usable) + zero_fill_orders) > 0
            else None
        ),
    }
    if not _grouped:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in usable:
            key = f"{row.get('path') or '?'}/{row.get('side') or '?'}"
            groups.setdefault(key, []).append(row)
        out["groups"] = {k: summarize(v, _grouped=True) for k, v in sorted(groups.items())}
    return out
