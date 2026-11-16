"""盘中结构/量能块（决策提示词）：全市场涨跌停结构 + 板块强度 + 个股量能 + 异动。

数据来源 = 交易服务常驻任务 ``intraday_market_watch`` 写入的 Redis 文档
（``qm:intraday:market:{YYYYMMDD}`` 的 ``doc`` 字段，db2 交易库），本模块只做
**纯渲染**：不取数、不连 Redis、不读文件。

纪律（与 ``shared/decision_blocks`` 同一批）：

1. 缺失一律显式 ``—``，绝不补 0；整块无数据 → 空串（提示词逐字节不变）。
2. 每段带**样本口径**（全市场 / 热集）与**新鲜度**，炸板明确标注是**采样口径**。
3. 盘口档位 < 2 时把「五档失衡」降级成「一档失衡」——行情源只回一档时，
   五档失衡是假口径，不能喂给模型当事实。
4. 展示面只说位置/事实（涨/跌/涨停/炸板/量比/日内位置），不写买卖建议。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

MISSING = "—"

#: 文档超过该秒数未更新 → 头部标「快照 N 分钟前」（只标注，不阻断渲染）。
STALE_WARN_S = 180.0

#: 异动行最多渲染条数（控制提示词长度）。
_MAX_EVENTS = 8


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _pct(value: float | None, digits: int = 2) -> str:
    return MISSING if value is None else f"{value:+.{digits}f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return MISSING if value is None else f"{value:.{digits}f}"


def _money_wan(value: float | None) -> str:
    """资金净流入（万元）→ 亿/万文本；缺失 → ``—``。"""
    if value is None:
        return MISSING
    if abs(value) >= 10000.0:
        return f"{value / 1e4:+.2f} 亿"
    return f"{value:+.0f} 万"


def _age_text(ts: float | None, now: datetime) -> str:
    if ts is None or ts <= 0:
        return ""
    age = max(0.0, now.timestamp() - ts)
    if age < 60:
        return f"{age:.0f} 秒前"
    return f"{age / 60:.0f} 分钟前"


def _code_label(code: str, name: str) -> str:
    return f"{name}({code})" if name else code


def _breadth_lines(breadth: Mapping[str, Any]) -> list[str]:
    if not breadth:
        return []
    n = _f(breadth.get("n"))
    up = _f(breadth.get("up"))
    down = _f(breadth.get("down"))
    flat = _f(breadth.get("flat"))
    lu = _f(breadth.get("limit_up"))
    ld = _f(breadth.get("limit_down"))
    broke = _f(breadth.get("broke"))
    rate = _f(breadth.get("broke_rate"))
    if n is None and up is None and lu is None:
        return []
    sample = str(breadth.get("sample") or "")
    scope = "全市场" if sample == "full" else (sample or "样本")
    head = f"- {scope}"
    if n is not None:
        head += f" {n:,.0f} 只"
    head += "：" + " / ".join(
        part
        for part in (
            f"涨 {up:,.0f}" if up is not None else "",
            f"跌 {down:,.0f}" if down is not None else "",
            f"平 {flat:,.0f}" if flat is not None else "",
        )
        if part
    )
    lines = [head] if head.strip("- ：") else []
    tail = []
    if lu is not None:
        tail.append(f"涨停 {lu:,.0f}")
    if ld is not None:
        tail.append(f"跌停 {ld:,.0f}")
    if broke is not None:
        rate_txt = f"（炸板率 {rate:.1f}%）" if rate is not None else ""
        tail.append(f"炸板 {broke:,.0f}{rate_txt}")
    if tail:
        lines.append("- " + " ｜ ".join(tail))
    return lines


def _seal_text(value: float) -> str:
    if value >= 1e8:
        return f"{value / 1e8:.2f} 亿"
    if value >= 1e4:
        return f"{value / 1e4:.0f} 万"
    return f"{value:.0f} 元"


def _ladder_line(breadth: Mapping[str, Any]) -> str:
    items = breadth.get("top_limit_up")
    if not isinstance(items, Sequence) or not items:
        return ""
    parts: list[str] = []
    for row in list(items)[:8]:
        if not isinstance(row, Mapping):
            continue
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        pct = _f(row.get("pct"))
        boards = _f(row.get("boards"))
        board_txt = f"{boards:.0f}板" if boards is not None else ""
        part = _code_label(code, name)
        if board_txt:
            part += f" {board_txt}"
        if pct is not None:
            part += f" {pct:+.2f}%"
        seal = _f(row.get("seal"))
        if seal is not None and seal > 0:
            part += f" 封单 {_seal_text(seal)}"
        parts.append(part)
    if not parts:
        return ""
    return "- 涨停梯队：" + " ｜ ".join(parts)


def _sector_lines(sectors: Sequence[Any]) -> list[str]:
    if not isinstance(sectors, Sequence) or not sectors:
        return []
    strong: list[str] = []
    weak: list[str] = []
    for row in sectors:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        chg = _f(row.get("chg"))
        lu = _f(row.get("limit_up"))
        if chg is None:
            continue
        part = f"{name} {chg:+.2f}%"
        if lu:
            part += f"（涨停 {lu:.0f}）"
        (strong if chg >= 0 else weak).append(part)
    lines: list[str] = []
    if strong:
        lines.append("- 板块领涨：" + " ｜ ".join(strong[:5]))
    if weak:
        lines.append("- 板块领跌：" + " ｜ ".join(weak[:3]))
    return lines


def _metric_line(role: str, code: str, row: Mapping[str, Any]) -> str:
    name = str(row.get("name") or "")
    head = f"[{role}] {_code_label(code, name)}"
    parts: list[str] = []
    vr = _f(row.get("vr"))
    if vr is not None:
        parts.append(f"量比 {vr:.2f}")
    else:
        parts.append(f"量比 {MISSING}")
    speed = _f(row.get("speed"))
    parts.append(f"涨速 {_pct(speed)}")
    pos = _f(row.get("pos"))
    if pos is not None:
        parts.append(f"日内位置 {pos * 100:.0f}%")
    else:
        parts.append(f"日内位置 {MISSING}")
    zjl = _f(row.get("zjl"))
    if zjl is not None:
        parts.append(f"资金净流入 {_money_wan(zjl)}")
    wtb = _f(row.get("wtb"))
    if wtb is not None:
        parts.append(f"委比 {wtb:+.1f}%")
    open_zaf = _f(row.get("open_zaf"))
    if open_zaf is not None:
        parts.append(f"竞价 {_pct(open_zaf)}")
    levels = _f(row.get("depth_levels"))
    if levels is not None:
        depth_txt = "一档" if levels <= 1 else f"{levels:.0f}档"
        parts.append(f"盘口 {depth_txt}")
    return head + "：" + " ｜ ".join(parts)


def _metrics_lines(
    metrics: Mapping[str, Any], holdings: Sequence[Any], pool: Sequence[Any]
) -> list[str]:
    if not isinstance(metrics, Mapping) or not metrics:
        return []
    from backend.shared.stock_utils import StockCodeUtil

    held: set[str] = set()
    lines: list[str] = []
    missing = 0
    for role, items in (("持仓", holdings), ("池", pool)):
        for item in items:
            code = str(getattr(item, "code", item) or "")
            prefix = StockCodeUtil.to_prefix(code)
            if not prefix or prefix in held:
                continue
            held.add(prefix)
            row = metrics.get(prefix)
            if not isinstance(row, Mapping):
                missing += 1
                continue
            lines.append("- " + _metric_line(role, prefix, row))
    if missing:
        lines.append(f"- （另有 {missing} 只持仓/池本轮无量能数据）")
    return lines


def _event_lines(events: Sequence[Any]) -> list[str]:
    if not isinstance(events, Sequence) or not events:
        return []
    out: list[str] = []
    for row in list(events)[-_MAX_EVENTS:]:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        ts = _f(row.get("ts"))
        stamp = ""
        if ts is not None and ts > 0:
            try:
                stamp = datetime.fromtimestamp(ts).strftime("%H:%M")
            except (OSError, ValueError):
                stamp = ""
        out.append(f"- {stamp + ' ' if stamp else ''}{text}")
    return out


def build_intraday_live_block(
    doc: Mapping[str, Any] | None,
    holdings: Sequence[Any] = (),
    pool: Sequence[Any] = (),
    now: datetime | None = None,
) -> str:
    """Redis 文档 → 【盘中结构/量能/异动】提示词块；无数据 → ``""``。"""
    if not isinstance(doc, Mapping) or not doc:
        return ""
    moment = now or datetime.now()
    ts = _f(doc.get("ts"))
    age_txt = _age_text(ts, moment)
    stale = bool(
        ts is not None and ts > 0 and max(0.0, moment.timestamp() - ts) > STALE_WARN_S
    )
    source = str(doc.get("source") or "tdx_bridge")
    header = f"【盘中结构】（{source} · 全市场快照"
    if age_txt:
        header += f" · {age_txt}"
    header += "）"
    if stale:
        header += "——快照已过期，按旧数据读"

    lines: list[str] = [header]
    if str(doc.get("phase") or "") == "non_continuous":
        lines.append("- 非连续竞价阶段：涨跌停/广度未定型，只作参考")
    breadth = doc.get("breadth")
    if isinstance(breadth, Mapping):
        lines.extend(_breadth_lines(breadth))
        ladder = _ladder_line(breadth)
        if ladder:
            lines.append(ladder)
    sectors = doc.get("sectors")
    if isinstance(sectors, Sequence) and sectors:
        lines.extend(_sector_lines(sectors))
    metrics = doc.get("metrics")
    metric_doc = metrics if isinstance(metrics, Mapping) else {}
    metric_lines = _metrics_lines(metric_doc, holdings, pool)
    if metric_lines:
        lines.append("【盘中量能】（持仓+池；量比=当日量/5日均，涨速=源端口径）")
        lines.extend(metric_lines)
    events = doc.get("events")
    event_lines = _event_lines(events if isinstance(events, Sequence) else [])
    if event_lines:
        lines.append("【盘中异动】（最近事件，按采样时间）")
        lines.extend(event_lines)

    if len(lines) == 1:
        return ""

    # 口径注：只写真实存在的降级，不打印空话。
    notes: list[str] = []
    depth_values = [
        _f(row.get("depth_levels"))
        for row in (metrics or {}).values()
        if isinstance(row, Mapping)
    ]
    if any(v is not None and v <= 1 for v in depth_values):
        notes.append("盘口档位=1：行情源只回一档，五档失衡不可用")
    if isinstance(breadth, Mapping) and breadth.get("broke_sampled") is True:
        notes.append("炸板按轮询采样统计，秒级触板可能漏计")
    if notes:
        lines.append("- 口径：" + "；".join(notes))
    return "\n".join(lines)


__all__ = ["build_intraday_live_block", "STALE_WARN_S"]
