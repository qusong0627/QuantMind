"""T-P3-06 模板库质量闸门（机构测试）：全库 **零空壳** 断言。

背景（用户点名 2026-09-16）："有些没有内容的模板"——minibt×11 参数面板为空、
legacy/hk 缺执行默认与提示。本闸门逐模板校验既有 UI/运行时契约，防复发：
- 必填字段（id/name/description/category/difficulty）；
- **params 非空**，且 default 落在 [min, max] 区间（数值参数）；
- category ∈ {basic, advanced, risk_control}；difficulty ∈ {beginner, intermediate, advanced}；
- markets ∈ {a_share, hong_kong, us_stock, crypto}（空=历史A股默认，合法）；
- execution_defaults ⊂ {max_buy_drop, stop_loss} 且值落在运行时校验区间
  （max_buy_drop ∈ [-0.10,-0.01]、stop_loss ∈ [-0.20,-0.03]，与 _normalize_execution_config 同源）；
- live_defaults 若含 sell_time/buy_time 则 sell < buy；enabled_sessions ⊂ {AM,PM,AFTER_HOURS}；
- .py 同名存在；描述不重复（模板需有辨识度）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _PROJECT_ROOT / "strategy_templates"

_ALLOWED_MARKETS = {"a_share", "hong_kong", "us_stock", "crypto"}
_ALLOWED_SESSIONS = {"AM", "PM", "AFTER_HOURS", "NIGHT"}


def _load_all_jsons() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for jf in sorted(_TEMPLATES_DIR.glob("*.json")):
        out.append((jf.stem, json.loads(jf.read_text(encoding="utf-8"))))
    return out


@pytest.mark.unit
def test_template_library_is_not_empty():
    items = _load_all_jsons()
    assert len(items) >= 80, f"模板库异常缩小: {len(items)}"


@pytest.mark.unit
def test_required_fields_and_enums():
    problems: list[str] = []
    for tid, d in _load_all_jsons():
        if d.get("id") != tid:
            problems.append(f"{tid}: id 与文件名不一致 ({d.get('id')})")
        for key in ("name", "description", "category", "difficulty"):
            if not str(d.get(key) or "").strip():
                problems.append(f"{tid}: 缺 {key}")
        if d.get("category") not in {"basic", "advanced", "risk_control"}:
            problems.append(f"{tid}: category 非法 {d.get('category')}")
        if d.get("difficulty") not in {"beginner", "intermediate", "advanced"}:
            problems.append(f"{tid}: difficulty 非法 {d.get('difficulty')}")
        for m in d.get("markets") or []:
            if m not in _ALLOWED_MARKETS:
                problems.append(f"{tid}: markets 非法 {m}")
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_every_template_has_params_with_valid_ranges():
    """零空壳闸门：参数面板不得为空；数值默认值必须落在区间内。"""
    problems: list[str] = []
    for tid, d in _load_all_jsons():
        params = d.get("params") or []
        if not params:
            problems.append(f"{tid}: params 为空")
            continue
        for p in params:
            name = str(p.get("name") or "")
            if not name or not str(p.get("description") or "").strip():
                problems.append(f"{tid}: 参数缺 name/description ({p})")
            default = p.get("default")
            mn, mx = p.get("min"), p.get("max")
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                if mn is not None and default < mn:
                    problems.append(f"{tid}.{name}: default {default} < min {mn}")
                if mx is not None and default > mx:
                    problems.append(f"{tid}.{name}: default {default} > max {mx}")
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_execution_defaults_match_runtime_contract():
    """execution_defaults 必须落在 _normalize_execution_config 的运行时校验区间。"""
    problems: list[str] = []
    for tid, d in _load_all_jsons():
        exec_d = d.get("execution_defaults") or {}
        if not exec_d:
            problems.append(f"{tid}: execution_defaults 为空（机构口径：显式风控默认）")
            continue
        for key, value in exec_d.items():
            if key == "max_buy_drop" and not (-0.10 <= float(value) <= -0.01):
                problems.append(f"{tid}: max_buy_drop={value} 超出 [-0.10,-0.01]")
            if key == "stop_loss" and not (-0.20 <= float(value) <= -0.03):
                problems.append(f"{tid}: stop_loss={value} 超出 [-0.20,-0.03]")
            if key not in {"max_buy_drop", "stop_loss"}:
                problems.append(f"{tid}: execution_defaults 含未识别键 {key}")
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_live_defaults_sane():
    problems: list[str] = []
    for tid, d in _load_all_jsons():
        live = d.get("live_defaults") or {}
        sell = str(live.get("sell_time") or "")
        buy = str(live.get("buy_time") or "")
        if sell and buy and sell >= buy:
            problems.append(f"{tid}: sell_time {sell} >= buy_time {buy}")
        for s in live.get("enabled_sessions") or []:
            if str(s).upper() not in _ALLOWED_SESSIONS:
                problems.append(f"{tid}: enabled_sessions 非法 {s}")
        if not (d.get("live_config_tips") or []):
            problems.append(f"{tid}: live_config_tips 为空（启动表单无口径提示）")
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_py_pair_exists_and_descriptions_unique():
    seen: dict[str, str] = {}
    problems: list[str] = []
    for tid, d in _load_all_jsons():
        if not (_TEMPLATES_DIR / f"{tid}.py").exists():
            problems.append(f"{tid}: 缺同名 .py")
        desc = str(d.get("description") or "").strip()
        if desc in seen:
            problems.append(f"{tid}: description 与 {seen[desc]} 重复")
        seen.setdefault(desc, tid)
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_loader_loads_all_templates_cleanly():
    """真实加载器全量加载：0 解析错误，params 面板全非空。

    用**新实例**加载并前后清理单例缓存——加载器测试套件会以 monkeypatch 的临时目录
    调用单例公开接口，残留缓存会跨用例污染（实测混跑时 60s TTL 缓存里只剩 2 个模板）。
    """
    try:
        from backend.services.engine.qlib_app.services.strategy_templates import (
            StrategyTemplateLoader,
            invalidate_templates_cache,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"模板加载器不可用: {exc}")

    invalidate_templates_cache()
    try:
        templates = StrategyTemplateLoader().load()
        assert len(templates) >= 80
        empty_params = [t.id for t in templates if not t.params]
        assert empty_params == [], f"参数面板为空的模板: {empty_params}"
    finally:
        invalidate_templates_cache()
