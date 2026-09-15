"""T-P0-08 回归：错误自定位格式（errfmt.locate）+ 三条关键路径接线防误删。"""

from pathlib import Path

from backend.shared.errfmt import locate

_BACKEND = Path(__file__).resolve().parents[1]


def test_locate_full_format():
    msg = locate(
        "CONTRACT:LEDGER",
        "record failed",
        ref="order-123",
        where="execution_engine.py:apply_filled",
    )
    assert msg == "[CONTRACT:LEDGER] record failed (ref=order-123) → execution_engine.py:apply_filled"


def test_locate_optional_parts():
    assert locate("RULE:SIGNAL-GATE", "all hold") == "[RULE:SIGNAL-GATE] all hold"
    assert locate("RULE:X", "m", ref="r1") == "[RULE:X] m (ref=r1)"
    assert locate("RULE:X", " m ", where=" f.py:g ") == "[RULE:X] m → f.py:g"


def test_critical_paths_use_locate():
    """信号闸门 / 模拟执行 / 落账 三条路径必须使用自定位格式（防重构回退）。"""
    paths = {
        "信号闸门": "services/engine/inference/script_runner.py",
        "模拟执行": "services/simulation/engine.py",
        "落账": "services/simulation/services/execution_engine.py",
    }
    for name, rel in paths.items():
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "errfmt import locate" in src, name
        assert "RULE:SIGNAL-GATE" in src or "RULE:SIM-EXEC" in src or "CONTRACT:LEDGER" in src, name
