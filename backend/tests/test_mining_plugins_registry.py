"""mining_plugins 插件注册表测试（TDD 先红后绿）。

契约：
- 内置 evaluator（reliability/turnover_cost）与描述符（IC 族 / PFS 族 / 毛收益族）注册齐全；
- ``list_descriptors()`` 与金样 ``miningMetricsGolden.json`` 的 registry **逐项全等**
  （前端 metricRegistry.ts 的本地默认表也读同一份，三处不许漂移）；
- 重复 key 注册必须报错（防插件撞名静默覆盖）；
- 单个插件抛异常不拖垮整条评估链（其键落 None + 告警，其余照常）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = json.loads(
    (
        Path(__file__).resolve().parent / "fixtures" / "miningMetricsGolden.json"
    ).read_text(encoding="utf-8")
)

try:  # pragma: no cover - 环境相关
    from backend.services.engine.mining_plugins import (
        EvalContext,
        evaluate_paired,
        get_evaluator_names,
        list_descriptors,
    )
    from backend.services.engine.mining_plugins.base import MetricDescriptor
    from backend.services.engine.mining_plugins.registry import PluginRegistry
except Exception as _exc:  # noqa: BLE001
    evaluate_paired = None
    _IMPORT_ERR = _exc

pytestmark = pytest.mark.skipif(evaluate_paired is None, reason="mining_plugins 不可用")

EVALUATOR_KEYS = {
    "rre",
    "turnover_daily",
    "ann_turnover",
    "ann_return_net",
    "sharpe_net",
    "max_drawdown_net",
}


def _tiny_paired(n_days: int = 3, n_symbols: int = 10) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n_days)
    rows = []
    for d in dates:
        for i in range(n_symbols):
            rows.append((d, f"S{i:02d}", float(n_symbols - i), 0.01))
    return pd.DataFrame(rows, columns=["datetime", "symbol", "factor", "ret"])


def test_builtin_evaluators_registered() -> None:
    names = set(get_evaluator_names())
    assert {"reliability", "turnover_cost"} <= names


def test_descriptors_match_golden_exactly() -> None:
    """逐项全等（含顺序、label、unit、better、precision、description）。"""
    assert list_descriptors() == GOLDEN["registry"]


def test_duplicate_metric_key_rejected() -> None:
    reg = PluginRegistry()
    d = MetricDescriptor(
        key="probe.metric",
        label="探针",
        group="prediction",
        unit="ratio",
        better="higher",
    )
    reg.register_descriptor(d)
    with pytest.raises(ValueError):
        reg.register_descriptor(d)


def test_duplicate_evaluator_name_rejected() -> None:
    class _Probe:
        name = "probe"
        descriptors = (
            MetricDescriptor(
                key="probe.x",
                label="X",
                group="prediction",
                unit="ratio",
                better="higher",
            ),
        )

        def evaluate(self, ctx: EvalContext) -> dict:
            return {"probe.x": 1.0}

    reg = PluginRegistry()
    reg.register_evaluator(_Probe())
    with pytest.raises(ValueError):
        reg.register_evaluator(_Probe())


def test_evaluate_paired_returns_full_key_set() -> None:
    got = evaluate_paired(_tiny_paired(), cost_rate=0.002)
    assert set(got) == EVALUATOR_KEYS
    assert got["rre"] is not None
    assert got["turnover_daily"] is not None
    assert got["ann_return_net"] is not None


def test_evaluate_paired_enabled_filter() -> None:
    got = evaluate_paired(_tiny_paired(), cost_rate=0.002, enabled={"reliability"})
    assert set(got) == {"rre"}


def test_evaluate_paired_uses_config_cost_rate_by_default() -> None:
    from backend.services.engine.mining_plugins.config import get_cost_rate

    ctx = EvalContext(paired=_tiny_paired())
    assert ctx.cost_rate == pytest.approx(get_cost_rate())


def test_plugin_exception_is_isolated() -> None:
    class _Boom:
        name = "boom"
        descriptors = (
            MetricDescriptor(
                key="boom.metric",
                label="爆炸",
                group="prediction",
                unit="ratio",
                better="higher",
            ),
        )

        def evaluate(self, ctx: EvalContext) -> dict:
            raise RuntimeError("boom")

    class _Ok:
        name = "ok"
        descriptors = (
            MetricDescriptor(
                key="ok.metric",
                label="正常",
                group="prediction",
                unit="ratio",
                better="higher",
            ),
        )

        def evaluate(self, ctx: EvalContext) -> dict:
            return {"ok.metric": 0.5}

    reg = PluginRegistry()
    reg.register_evaluator(_Boom())
    reg.register_evaluator(_Ok())
    got = reg.run_evaluators(EvalContext(paired=_tiny_paired()))
    assert got["boom.metric"] is None  # 异常 → 键在值 None，不拖垮其他插件
    assert got["ok.metric"] == pytest.approx(0.5)


def test_descriptor_json_fields() -> None:
    for desc in list_descriptors():
        assert set(desc) == {
            "key",
            "label",
            "group",
            "unit",
            "better",
            "precision",
            "description",
        }
        assert desc["unit"] in {"ratio", "pct", "score", "days", "count"}
        assert desc["better"] in {"higher", "lower", "none"}
