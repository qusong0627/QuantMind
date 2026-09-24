"""市场级哨兵告警 → 人读文案（通知**标题 + 正文**的唯一实现）。

为什么要有这个模块
------------------
`sentinel_alert_service._decide_push` 曾把告警信封 `json.dumps` 直接当正文发出去，
用户收到的推送长这样（实测 2026-09-24 15:09）：

    [sentinel] [warn] 600503.SH 大幅下行
    {"alert_type": "anomaly:price_surge", "market": "CN", "targets": ["600503.SH"], ...}

两处毛病：标的是**裸代码**（六个字的股票要用户自己去查是哪家），正文是**机器信封**
（键名、浮点原值 `-0.09863945578231292` 全糊在脸上）。信封本身没错——它该留在
`sentinel_alerts.detail` 里给机器读；错的是把同一份东西端给了人。

口径纪律（与展示面其它组件同一套）
----------------------------------
* **只描述位置，不给方向词与祈使句**：不说「建议减仓」「赶紧卖」。与
  `holding_alert_contract.build_alert_content` 的尾句同一口径——提醒不是指令。
* **不认识的键不进正文**：`metrics` 里没登记标签的键一律略去（否则正文又变回代码汤）。
  要看得见全部原始字段，去交易台 → 实时情报（读的是 `detail`，不是这段文案）。
* **名称查不到就退回代码**，绝不编一个名字，也绝不因此丢掉整条告警。

前端那份标签表在 ``electron/src/features/desk/components/copilotModel.ts``
（`alertTypeLabel` / `severityMeta`）：两处必须同值，
`backend/tests/test_alert_text.py` 有漂移守卫盯着。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.market_labels import market_label

_CST = ZoneInfo("Asia/Shanghai")

#: 级别 → 中文（与前端 `severityMeta` 同值，测试盯漂移）。
SEVERITY_LABELS: dict[str, str] = {
    "critical": "严重",
    "warn": "关注",
    "info": "提示",
}

#: 告警类型 → 中文（与前端 `alertTypeLabel` 同值，测试盯漂移）。
TYPE_LABELS: dict[str, str] = {
    "news:risk_event": "新闻风险",
    "news:negative": "新闻利空",
    "news:positive": "新闻利好",
    "news:sentiment_spike": "情绪突变",
    "anomaly:volume_surge": "异常放量",
    "anomaly:price_limit_up": "涨停异动",
    "anomaly:price_limit_down": "跌停异动",
    "anomaly:price_surge": "大幅波动",
    "anomaly:account_cancel_ratio": "撤单异常",
    "anomaly:account_concentration": "集中度偏高",
    "anomaly:data_jump": "数据跳变",
    "anomaly:data_gap": "数据缺口",
    "anomaly:model_ic_drop": "模型 IC 异常",
    "regime": "市场状态",
}

#: 方向 → 中文。`none` 不是「中性」而是**不可评分**（见 sentinel_alert_contract），
#: 所以不显示——把「判不出来」写成「中性」是一种编造。
DIRECTION_LABELS: dict[str, str] = {"down": "利空", "up": "利多"}

#: `metrics` 键 → (中文标签, 格式化函数)。**只登记会进正文的键**；
#: 其余键（orders/cancelled/date/top_symbol…）留在 detail 里，正文不出现。
METRIC_LABELS: dict[str, tuple[str, Callable[[Any], str]]] = {
    "price": ("现价", lambda v: f"{float(v):.2f}"),
    "limit_up": ("涨停价", lambda v: f"{float(v):.2f}"),
    "limit_down": ("跌停价", lambda v: f"{float(v):.2f}"),
    "pct_chg": ("涨跌幅", lambda v: f"{float(v) * 100:+.2f}%"),
    "volume_ratio": ("量比", lambda v: f"{float(v):.2f}"),
    "cancel_ratio": ("撤单率", lambda v: f"{float(v) * 100:.1f}%"),
    "concentration": ("持仓集中度", lambda v: f"{float(v) * 100:.1f}%"),
    "ic_short": ("短窗 IC", lambda v: f"{float(v):+.4f}"),
    "ic_long": ("长窗 IC", lambda v: f"{float(v):+.4f}"),
}

#: 尾句（说明去哪儿看，不是祈使句）。
_TAIL = "详情见交易台 · 实时情报。"


def _resolve_name(symbol: str) -> str:
    """中文名（``stock_name_mapper``）；查不到/索引坏 → 空串，绝不抛。

    告警文案是**消费端**：名字服务坏了只该少一个名字，不该少一条告警。
    """
    try:
        from backend.shared.stock_name_mapper import resolve_name

        return str(resolve_name(symbol) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _fmt_metric(key: str, value: Any) -> str:
    """一条指标 → ``标签 值``；值不可解析 → 空串（略去该条，不写 ``nan``）。"""
    label, fmt = METRIC_LABELS[key]
    try:
        return f"{label} {fmt(value)}"
    except (TypeError, ValueError):
        return ""


def _covered_labels(description: str) -> set[str]:
    """描述里已经出现过的指标标签（避免同一数字在正文里写两遍）。"""
    return {label for label, _ in METRIC_LABELS.values() if label in description}


def _metric_items(raw: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``metrics`` → ``[(标签, 值文本)]``：只留登记过的键、值可解析的项，标签去重。"""
    out: list[tuple[str, str]] = []
    labels: set[str] = set()
    for key, value in raw.items():
        meta = METRIC_LABELS.get(str(key))
        if meta is None or meta[0] in labels:
            continue
        text = _fmt_metric(str(key), value)
        if text:
            out.append((meta[0], text))
            labels.add(meta[0])
    return out


def _metrics_line(payload: Mapping[str, Any], description: str) -> str:
    """未被描述覆盖的指标，拼成一行 ``现价 2.65 · 量比 2.10``。"""
    raw = payload.get("metrics")
    if not isinstance(raw, Mapping):
        return ""
    seen = _covered_labels(description)
    return " · ".join(text for label, text in _metric_items(raw) if label not in seen)


def _description_line(payload: Mapping[str, Any]) -> str:
    """事实行：优先检测器写好的描述，缺了就用已知指标兜（绝不留空）。"""
    description = str(payload.get("description") or "").strip()
    if description:
        return description
    raw = payload.get("metrics")
    if not isinstance(raw, Mapping):
        return ""
    return " · ".join(text for _, text in _metric_items(raw))


def _identity(symbol: str, name: str) -> str:
    """``华仪电气(600503.SH)``；名字缺失或与代码相同 → 只留代码。"""
    if not name or name == symbol:
        return symbol
    return f"{name}({symbol})" if symbol else name


def _title_with_identity(title: str, symbol: str, name: str) -> str:
    """把标题里的裸代码换成 ``名称(代码)``；标题里没有该代码就在前面补一个身份。"""
    ident = _identity(symbol, name)
    if not ident or ident == symbol:
        return title
    if symbol and symbol in title:
        return title.replace(symbol, ident, 1)
    return f"{ident} {title}".strip()


def _context_line(row: Mapping[str, Any]) -> str:
    """``A股 · 大幅波动 · 利空 · 09-24 15:09``（缺失的段略去，不写占位符）。"""
    parts = [
        market_label(row.get("market")),
        TYPE_LABELS.get(
            str(row.get("alert_type") or ""), str(row.get("alert_type") or "")
        ),
        DIRECTION_LABELS.get(str(row.get("direction") or ""), ""),
    ]
    ts = row.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        parts.append(datetime.fromtimestamp(float(ts), tz=_CST).strftime("%m-%d %H:%M"))
    return " · ".join(p for p in parts if p)


def format_sentinel_alert(
    row: Mapping[str, Any],
    *,
    name_resolver: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """告警行 → ``(标题, 正文)``，都是给人读的中文。**纯函数**（名字解析可注入）。

    标题：``[关注] 华仪电气(600503.SH) 大幅下行``
    正文：事实行 / 补充指标行 / 上下文行 / 尾句，逐行；空行不出现。
    """
    resolve = name_resolver or _resolve_name
    symbol = str(row.get("symbol") or "").strip()
    if symbol == "*":  # 无标的的市场级告警：没有可解析的身份
        symbol = ""
    name = resolve(symbol) if symbol else ""

    severity = str(row.get("severity") or "").strip().lower()
    tag = SEVERITY_LABELS.get(severity, severity or "提示")
    title = f"[{tag}] {_title_with_identity(str(row.get('title') or '').strip(), symbol, name)}"

    payload = row.get("detail")
    payload = payload.get("payload") if isinstance(payload, Mapping) else None
    payload = payload if isinstance(payload, Mapping) else {}

    description = _description_line(payload)
    lines = [
        line
        for line in (
            description,
            _metrics_line(payload, description),
            _context_line(row),
        )
        if line
    ]
    lines.append(_TAIL)
    return title.strip(), "\n".join(lines)
