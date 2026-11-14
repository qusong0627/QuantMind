"""收盘「市场阶段」快照：一次算好，研究总控与交易 agent 共用。

为什么单独落一份 JSON
----------------------
``/api/v1/market-analysis/regime`` 是**按需**端点（带鉴权、每次重算 560 日面板），
不适合让 11 个盘中槽位每轮去调；而研究总控事实包需要一份**可复现、带 asof**的
收盘快照。这里把同一份 ``market_regime`` 结果压缩成 JSON：

* ``{SNAPSHOT_DIR}/{YYYY-MM-DD}.json`` —— 当日快照（ALL 口径）；
* ``{SNAPSHOT_DIR}/latest.json`` —— 最近一份（便于人工查看）。

消费方
------
1. 研究总控 ``research_control.dossier.collect_market``：收盘后确保快照存在，
   把 ``render_stage_text`` 拼进「市场与资金」事实包；
2. 交易 agent 决策轮：``decision_round_io.load_market_stage`` 读最近一份，
   ``decision_blocks.build_market_state_block`` 渲染成【上一交易日市场阶段·参考】
   ——**必须标“上一交易日收盘，不是盘中实时”**，盘中实时状态仍以
   ``qm:regime:intraday`` 为准。

纪律：缺失一律如实写“数据缺口”，绝不拿最新快照冒充历史日期。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[2] / "data" / "quantdb"
SENTIMENT_PATH = ROOT / "5_technical_derived" / "market_breadth" / "sentiment.json"

MARKET_SNAPSHOT_DIR = Path(
    os.environ.get("QM_MARKET_SNAPSHOT_DIR", "/data/market-analysis")
)
if not MARKET_SNAPSHOT_DIR.exists():
    MARKET_SNAPSHOT_DIR = (
        Path(__file__).resolve().parents[2] / "data" / "market-analysis"
    )
SNAPSHOT_DIR = MARKET_SNAPSHOT_DIR / "regime"
LATEST_PATH = SNAPSHOT_DIR / "latest.json"

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _snapshot_path(day: str, universe: str = "ALL") -> Path:
    suffix = "" if universe == "ALL" else f"_{universe}"
    return SNAPSHOT_DIR / f"{day}{suffix}.json"


def _compact_layers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for layer in payload.get("layers") or []:
        out.append(
            {
                "key": layer.get("key"),
                "label": layer.get("label"),
                "stage": layer.get("stage"),
                "score": layer.get("score"),
                "metrics": layer.get("metrics") or [],
                "action": layer.get("action"),
            }
        )
    return out


def _compact_sector(payload: Mapping[str, Any]) -> dict[str, Any]:
    sector = payload.get("sector") or {}
    rows = list(sector.get("list") or [])
    return {
        "up_share": sector.get("up_share"),
        "dispersion": sector.get("dispersion"),
        "top": rows[:5],
        "bottom": rows[-5:][::-1],
    }


def _sentiment_summary(day: str) -> dict[str, Any] | None:
    doc = _read_json(SENTIMENT_PATH)
    if not doc or str(doc.get("trade_date") or "") != day:
        return None
    stress = doc.get("stress") or {}
    volume = (doc.get("volume") or {}).get("latest") or {}
    main_force = doc.get("main_force") or {}
    retail = doc.get("retail") or {}
    return {
        "trade_date": doc.get("trade_date"),
        "stress_score": stress.get("score"),
        "stress_zone": stress.get("zone"),
        "zone_label": stress.get("zone_label"),
        "phase": stress.get("phase"),
        "phase_detail": stress.get("phase_detail"),
        "main_net_yi": volume.get("main_net_yi"),
        "main_net_ratio": volume.get("main_net_ratio"),
        "main_breadth": volume.get("main_breadth"),
        "turnover_yi": volume.get("turnover_yi"),
        "main_force_score": main_force.get("score"),
        "retail_score": retail.get("score"),
    }


def _breadth_summary(day: str) -> dict[str, Any] | None:
    doc = _read_json(MARKET_SNAPSHOT_DIR / f"{day}.json")
    breadth = (doc or {}).get("breadth") or {}
    if not breadth:
        return None
    keys = (
        "advance_count",
        "decline_count",
        "flat_count",
        "limit_up_count",
        "limit_down_count",
        "profit_effect",
        "limit_up_broken_ratio",
        "total_turnover_yi",
    )
    return {k: breadth.get(k) for k in keys}


def build_snapshot(date: str | None = None, universe: str = "ALL") -> dict[str, Any]:
    """调用 ``market_regime`` 生成压缩快照（失败抛异常，不静默造数）。"""
    from backend.services.api.market_analysis.regime import market_regime

    payload = market_regime(date=date, universe=universe, current_user={})
    day = str(payload["date"])
    gaps: list[str] = []
    sentiment = _sentiment_summary(day)
    if sentiment is None:
        gaps.append("sentiment_snapshot_missing_for_day")
    breadth = _breadth_summary(day)
    if breadth is None:
        gaps.append("market_breadth_snapshot_missing_for_day")
    return {
        "date": day,
        "universe": payload.get("universe"),
        "universe_label": payload.get("universe_label"),
        "state": (payload.get("summary") or {}).get("state"),
        "score": (payload.get("summary") or {}).get("score"),
        "summary": (payload.get("summary") or {}).get("one_liner"),
        "exposure": payload.get("exposure") or {},
        "top_risk": payload.get("top_risk") or {},
        "bottom": payload.get("bottom") or {},
        "events": payload.get("events") or {},
        "stock_breadth": payload.get("stock_breadth") or {},
        "layers": _compact_layers(payload),
        "blocks": payload.get("blocks") or [],
        "sector": _compact_sector(payload),
        "breadth": breadth,
        "sentiment": sentiment,
        "gaps": gaps,
        "generated_at": datetime.now(CN_TZ).isoformat(),
    }


def write_snapshot(snap: Mapping[str, Any]) -> Path:
    day = str(snap["date"])
    universe = str(snap.get("universe") or "ALL")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(day, universe)
    text = json.dumps(snap, ensure_ascii=False, indent=1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    # latest 只给人工看；日期文件才是机器读入口
    if universe == "ALL":
        try:
            latest_tmp = LATEST_PATH.with_suffix(".tmp")
            latest_tmp.write_text(text, encoding="utf-8")
            latest_tmp.replace(LATEST_PATH)
        except OSError:
            pass
    with _LOCK:
        _CACHE.pop(str(path), None)
    return path


def load_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
    doc = _read_json(path)
    if doc is None:
        return None
    with _LOCK:
        _CACHE[key] = (stat.st_mtime, stat.st_size, doc)
    return doc


def latest_snapshot_path(
    on_or_before: str | None = None, universe: str = "ALL"
) -> Path | None:
    if not SNAPSHOT_DIR.is_dir():
        return None
    suffix = "" if universe == "ALL" else f"_{universe}"
    paths = sorted(SNAPSHOT_DIR.glob(f"????-??-??{suffix}.json"), reverse=True)
    if on_or_before:
        paths = [p for p in paths if p.stem[:10] <= on_or_before]
    return paths[0] if paths else None


def load_latest_snapshot(
    on_or_before: str | None = None, universe: str = "ALL"
) -> dict[str, Any] | None:
    path = latest_snapshot_path(on_or_before, universe)
    return load_snapshot(path) if path else None


def ensure_snapshot(date: str | None, universe: str = "ALL") -> dict[str, Any]:
    """有则读、无则算并落盘（研究总控收盘档用；交易轮只读，不现算）。"""
    if date:
        path = _snapshot_path(date, universe)
        doc = load_snapshot(path)
        if doc is not None:
            return doc
    snap = build_snapshot(date, universe)
    write_snapshot(snap)
    return snap


def _dash(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)


def _num(value: Any, digits: int = 1, sign: bool = False) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if f != f or f in (float("inf"), float("-inf")):
        return "—"
    text = f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"
    return text


def _stage_lines(snap: Mapping[str, Any]) -> list[str]:
    score = snap.get("score")
    state = snap.get("state") or "—"
    layers = {str(layer.get("key")): layer for layer in snap.get("layers") or []}
    layer_txt = "｜".join(
        f"{layers[k].get('label')} {layers[k].get('stage')}"
        for k in ("long", "medium", "short", "intraday")
        if k in layers
    )
    score_txt = f"{int(score):+d}" if isinstance(score, int) else _dash(score)
    exposure = snap.get("exposure") or {}
    actual = exposure.get("actual")
    ceiling = exposure.get("ceiling")
    top = snap.get("top_risk") or {}
    bottom = snap.get("bottom") or {}
    events = snap.get("events") or {}
    ev = (
        f"TRIN_量 {_num(events.get('trin_vol'), 2)}"
        f"（{'投降' if events.get('trin_surrender') else '不投降'}）"
        f"｜额价差 {_num(events.get('amount_gap_pp'), 1, sign=True)}pp"
        f"（{'风险' if events.get('amount_gap_risk') else '未到20pp'}）"
        f"｜ADL 背离 {'有' if events.get('adl_divergence') else '无'}"
    )
    sent = snap.get("sentiment") or {}
    breadth = snap.get("breadth") or {}
    main_net = sent.get("main_net_yi")
    main_txt = (
        f"主力{'净流入' if float(main_net) >= 0 else '净流出'} {_num(abs(float(main_net)), 1)} 亿"
        if main_net is not None
        else "主力净额 —"
    )
    profit = breadth.get("profit_effect")
    turnover = breadth.get("total_turnover_yi")
    sentiment_line = (
        f"情绪压力 {_num(sent.get('stress_score'), 1)}（{_dash(sent.get('zone_label'))}）"
        f"｜赚钱效应 {_num(profit, 1)}"
        f"｜{main_txt}"
        f"｜成交 {_num(turnover, 1)} 亿"
    )
    sb = snap.get("stock_breadth") or {}
    if sb.get("available") and sb.get("quintiles"):
        q = " / ".join(
            f"{row.get('q')} {_num(row.get('ret_after'), 2, sign=True)}"
            for row in sb["quintiles"]
        )
        sample = "热集" if sb.get("sample") == "hot_set" else "全市场"
        stock_line = f"个股：10:10 Q1–Q5 收盘中位 {q}（{sample} {sb.get('n', '—')} 只）"
    else:
        stock_line = "个股：该日无 10:10 快照，Q1–Q5 不可得"
    sector = snap.get("sector") or {}
    top_rows = sector.get("top") or []
    bottom_rows = sector.get("bottom") or []
    sector_txt = ""
    if top_rows or bottom_rows:
        top_txt = "、".join(
            f"{r.get('name')} {_num(r.get('ret'), 2, sign=True)}%" for r in top_rows[:3]
        )
        bottom_txt = "、".join(
            f"{r.get('name')} {_num(r.get('ret'), 2, sign=True)}%"
            for r in bottom_rows[:3]
        )
        sector_txt = (
            f"板块：上涨占比 {_num(sector.get('up_share'), 1)}%"
            f"｜离散度 {_num(sector.get('dispersion'), 2)}"
            + (f"｜前列 {top_txt}" if top_txt else "")
            + (f"｜后列 {bottom_txt}" if bottom_txt else "")
        )
    lines = [
        f"- 六块 {score_txt}/±12（{state}）" + (f"｜{layer_txt}" if layer_txt else ""),
        f"- 基础暴露 {_num(actual, 1)} / {_num(ceiling, 1)}"
        f"｜顶风险 {'有' if top.get('flag') else '无'}"
        f"｜底部 {'成立' if bottom.get('flag') else '无'}",
        f"- 事件：{ev}",
        f"- {sentiment_line}",
        f"- {stock_line}",
    ]
    if sector_txt:
        lines.append(f"- {sector_txt}")
    gaps = [str(g) for g in snap.get("gaps") or [] if str(g).strip()]
    if gaps:
        lines.append("- 数据缺口：" + "；".join(gaps))
    return lines


def render_stage_text(snap: Mapping[str, Any]) -> str:
    """研究总控事实包用的完整段（带日期/口径标题）。"""
    day = _dash(snap.get("date"))
    label = snap.get("universe_label") or "全A等权"
    head = f"【市场阶段｜{label}｜截至 {day} 收盘】"
    return "\n".join([head, *_stage_lines(snap)])


def render_market_stage_prompt(
    snap: Mapping[str, Any] | None, now: datetime | None = None
) -> str:
    """交易 agent 提示词用的盘前参考块；无快照返回空串（整段不出现）。"""
    if not snap:
        return ""
    day = _dash(snap.get("date"))
    head = f"【上一交易日市场阶段·参考（截至 {day} 收盘）】"
    tail = "- 口径：上一交易日收盘，不是盘中实时；盘中实时状态见下方【盘中市场状态】。"
    return "\n".join([head, *_stage_lines(snap), tail])
