"""Scanner SPI（T-P4-01）：机会发现的可插拔扫描器接口 —— **纯函数唯一实现**。

设计（`docs/行情扫描与机会发现_设计方案.md` §二/§三）：
- 扫描器 = 纯函数式快照 → 机会列表（不碰账本、不下单、无副作用，可回放/单测/并行）；
- **注册即生效**：声明式注册表（独立开关/独立打分/独立被评估，仿 scheduler_registry）；
- 机会对象跨扫描器可比：``strength`` 0..1（扫描器内强度）、``score`` 0..100（汇总分）；
- 合并去重：同标的并 ``sources``、**多源共振加分**；同源冷却期内不重复报；过期出池。

铁律：**扫描结果不能直接下单**——买不买由策略（Strategy Spec）与风控决定；
每笔交易可回溯到"哪个扫描器在哪天发现了它、理由是什么"（evidence 证据链）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

MAX_SCORE = 100
# 多源共振加分：每多命中一个扫描器 +8 分（上限 100）
RESONANCE_BONUS = 8
DEFAULT_COOLDOWN_DAYS = 1
DEFAULT_HORIZON = "T+1..T+5"


@dataclass(frozen=True)
class Opportunity:
    """统一机会对象（设计 §三）。"""

    symbol: str
    market: str = "CN"
    sources: tuple[str, ...] = ()
    strength: float = 0.0  # 0..1 扫描器内强度（如 rank 分位）
    score: int = 0  # 0..100 汇总分（跨扫描器可比）
    horizon: str = DEFAULT_HORIZON
    evidence: dict[str, Any] = field(default_factory=dict)
    ts: str = ""  # 发现时刻（ISO）
    expiry: str = ""
    state: str = "watch"  # watch|candidate|chosen|expired


@dataclass(frozen=True)
class ScannerSpec:
    """扫描器声明（注册即生效）。"""

    id: str
    name: str
    market: str  # CN | US | HK ...
    frequency: str  # 盘后 | 盘前 | 盘中-快照 | 盘中-事件
    scope: str  # 全市场 | 热集 | 事件触发
    switch_env: str | None = None
    enabled_default: bool = True
    desc: str = ""


SCANNERS: tuple[ScannerSpec, ...] = (
    ScannerSpec(
        id="model_signal",
        name="模型信号扫描",
        market="CN",
        frequency="盘后",
        scope="全市场",
        switch_env="SCANNER_MODEL_SIGNAL_ENABLED",
        enabled_default=True,
        desc="推理信号 rank 分位（现有主链路迁入，T-P4-01）",
    ),
)


def scanner_spec(scanner_id: str) -> ScannerSpec | None:
    for spec in SCANNERS:
        if spec.id == str(scanner_id or ""):
            return spec
    return None


def scanner_switch_enabled(
    spec: ScannerSpec, env: dict[str, str] | None = None
) -> bool:
    """扫描器开关（仿 scheduler_registry.switch_enabled）：未配置用默认；0/false/no/off 为关。"""
    if not spec.switch_env:
        return spec.enabled_default
    source = env if env is not None else os.environ
    raw = str(source.get(spec.switch_env, "")).strip().lower()
    if raw == "":
        return spec.enabled_default
    return raw not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# 纯函数：过期 / 合并去重 / 共振
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            from zoneinfo import ZoneInfo

            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed
    except Exception:  # noqa: BLE001
        return None


def is_expired(opportunity: Opportunity, now: datetime) -> bool:
    """是否过期（无 expiry 视为不过期，由调用方决定生命周期）。"""
    exp = _parse_ts(opportunity.expiry)
    if exp is None:
        return False
    return now >= exp


def merge_opportunities(
    items: list[Opportunity],
    *,
    prior: list[Opportunity] | None = None,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    as_of: Any = None,
    apply_expiry: bool = True,
) -> list[Opportunity]:
    """合并同标的多源机会：并 sources、共振加分、同源冷却剔除、过期出池（纯函数）。

    - score = min(100, max(各源 score) + RESONANCE_BONUS × (源数-1))；
    - strength = 最强源的 strength；horizon 取首个非默认源；
    - ``prior``：近期已报机会（提供 (symbol, source, ts) 即可）——同 (symbol, source)
      在 ``as_of - cooldown_days`` 内出现过则该源本轮被冷却；全部源被冷却 → 整条不报；
    - evidence 增加 ``resonance``（命中源数 >1 时）与 ``sources_scores`` 审计快照。
    """
    prior_list = list(prior or [])
    as_of_dt = _parse_ts(as_of)
    cooldown_seconds = max(0, int(cooldown_days)) * 86400

    cooled: set[tuple[str, str]] = set()
    if prior_list and as_of_dt is not None and cooldown_seconds > 0:
        for p in prior_list:
            p_ts = _parse_ts(p.ts)
            if p_ts is None:
                continue
            if (as_of_dt - p_ts).total_seconds() <= cooldown_seconds:
                for src in p.sources:
                    cooled.add((str(p.symbol), str(src)))

    groups: dict[str, list[Opportunity]] = {}
    for o in items:
        groups.setdefault(str(o.symbol), []).append(o)

    merged: list[Opportunity] = []
    for symbol, group in groups.items():
        active_sources: dict[str, Opportunity] = {}
        for o in group:
            for src in o.sources or ("unknown",):
                if (symbol, str(src)) in cooled:
                    continue
                # 同源取分数最高的那条
                prev = active_sources.get(str(src))
                if prev is None or o.score > prev.score:
                    active_sources[str(src)] = o
        if not active_sources:
            continue
        best = max(active_sources.values(), key=lambda x: x.score)
        sources = tuple(sorted(active_sources.keys()))
        bonus = RESONANCE_BONUS * max(0, len(sources) - 1)
        score = int(min(MAX_SCORE, best.score + bonus))
        evidence = dict(best.evidence or {})
        evidence["sources_scores"] = {s: active_sources[s].score for s in sources}
        if len(sources) > 1:
            evidence["resonance"] = bonus
        merged.append(
            replace(
                best,
                sources=sources,
                score=score,
                evidence=evidence,
            )
        )

    if apply_expiry and as_of_dt is not None:
        merged = [o for o in merged if not is_expired(o, as_of_dt)]
    merged.sort(key=lambda o: (-o.score, o.symbol))
    return merged


def opportunity_to_dict(opportunity: Opportunity) -> dict[str, Any]:
    """稳定序列化（sources 元组 → 列表；evidence 深拷贝浅层）。"""
    return {
        "symbol": opportunity.symbol,
        "market": opportunity.market,
        "sources": list(opportunity.sources),
        "strength": opportunity.strength,
        "score": opportunity.score,
        "horizon": opportunity.horizon,
        "evidence": dict(opportunity.evidence or {}),
        "ts": opportunity.ts,
        "expiry": opportunity.expiry,
        "state": opportunity.state,
    }
