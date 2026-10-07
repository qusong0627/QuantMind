"""因子挖掘质量闸门：PFS 打分接入 + LLM 逻辑评分解析 + 导出警告。

alpha_agent 路由依赖 fastapi/DB，本地轻量环境 import 失败时整体跳过
（与 test_training_factor_selection_report.py 的 _load_module_safe 同策略）；
在 OSS 容器内运行 `python -m pytest backend/tests/` 时全量生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALPHA_AGENT_PY = ROOT / "backend" / "services" / "engine" / "routers" / "alpha_agent.py"

try:  # pragma: no cover - 环境相关
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    aa = None
    _IMPORT_ERR = _exc


pytestmark = pytest.mark.skipif(aa is None, reason="alpha_agent 依赖不可用（需容器环境）")


def test_parse_logic_score_extracts_and_strips() -> None:
    text = "**含义**：动量因子。\n\nSCORE: 85"
    body, score = aa._parse_logic_score(text)
    assert score == 85
    assert "SCORE" not in body and "含义" in body


def test_parse_logic_score_chinese_colon_and_default() -> None:
    assert aa._parse_logic_score("解释正文\nSCORE：78")[1] == 78
    body, score = aa._parse_logic_score("没有评分行的解释")
    assert score is None and body == "没有评分行的解释"


def test_parse_logic_score_clamps_and_takes_last() -> None:
    assert aa._parse_logic_score("SCORE: 150")[1] == 100
    assert aa._parse_logic_score("SCORE: 20")[1] == 20
    # 多个匹配取最后一个（正文引用评分说明时以末行为准）
    _, score = aa._parse_logic_score("评分标准 SCORE: 90 只是说明\n正文\nSCORE: 62")
    assert score == 62


def test_quality_warnings_thresholds() -> None:
    assert aa._quality_warnings(0.95, 75) == []
    assert aa._quality_warnings(None, None) == []
    w = aa._quality_warnings(0.85, 55)
    assert len(w) == 2
    assert "扰动保真度" in w[0] and "PFS=0.850" in w[0]
    assert "金融逻辑评分" in w[1] and "logic_score=55" in w[1]
    assert len(aa._quality_warnings(0.9, 60)) == 0  # 等于阈值不算警告


def test_compute_pfs_quality_on_panel() -> None:
    rng = np.random.default_rng(7)
    n_days, n_stocks = 30, 120
    df = pd.DataFrame({
        "trade_date": np.repeat(pd.date_range("2024-01-01", periods=n_days, freq="B"), n_stocks),
        "symbol": np.tile([f"s{i:03d}" for i in range(n_stocks)], n_days),
        "factor": rng.normal(size=n_days * n_stocks),
    })
    q = aa._compute_pfs_quality(df)
    assert q is not None
    assert 0.0 <= q["pfs"] <= 1.0
    assert q["n_days"] > 0
    # 空表 / None → None（不影响回测主流程）
    assert aa._compute_pfs_quality(pd.DataFrame()) is None
    assert aa._compute_pfs_quality(None) is None


def test_backtest_pipelines_emit_quality() -> None:
    """两条回测路径都必须把 PFS 写进 metadata（qlib 主进程算 / h5 子进程打印解析）。"""
    src = ALPHA_AGENT_PY.read_text(encoding="utf-8")
    # qlib 路径：主进程直接算，quality 进 metadata
    assert "pfs_quality = _compute_pfs_quality(pd.DataFrame({" in src
    assert '"quality": pfs_quality' in src
    # h5/函数式路径：子进程内算并打印，父进程解析（注意脚本在外层 f-string 里，字面花括号已转义）
    assert 'print("PFS=%.4f" % _q["pfs"])' in src
    assert 'or {{}}' in src  # 子进程脚本里的 {} 已按 f-string 转义
    assert 'elif line.startswith("PFS="):' in src
    # IC/ICIR/组合指标改走 _metric 助手后，PFS_GAUSS 成为 PFS 解析循环的首分支
    assert 'if line.startswith("PFS_GAUSS="):' in src
    assert 'metadata={"data_source": "h5", **({"quality": pfs_quality} if pfs_quality else {})},' in src


def test_explain_requests_and_persists_logic_score() -> None:
    src = ALPHA_AGENT_PY.read_text(encoding="utf-8")
    assert "SCORE: 50 到 100 的整数" in src
    assert "explanation, logic_score = _parse_logic_score(explanation)" in src
    assert 'metadata["logic_score"] = logic_score' in src


def test_export_carries_quality_gate() -> None:
    src = ALPHA_AGENT_PY.read_text(encoding="utf-8")
    assert "quality_warnings = _quality_warnings(pfs_val, logic_score)" in src
    assert '"quality_warnings": quality_warnings' in src
    assert "PFS (perturbation fidelity):" in src
