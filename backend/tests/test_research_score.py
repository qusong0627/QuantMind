"""研究评分（后端侧）——含与前端共用的金样。

金样只有**一份**（``backend/tests/fixtures/researchScoreGolden.json``），前端
``electron/src/features/shared/__tests__/researchScore.test.ts`` 反向读取本文件：
两边各存一份迟早漂移，而同包存放是因为后端测试跑在容器里、只挂了 ``./backend``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.shared.research_score import (
    RESEARCH_SCORE_BANDS,
    RESEARCH_SCORE_HINT,
    SIGNAL_POSITION_LABELS,
    format_rank_pct_as_research_score,
    format_research_score,
    format_research_score_with_band,
    research_score,
    research_score_band,
    signal_position_label,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "researchScoreGolden.json"
#: 信号位置词金样。前端 ``signalVocabulary.test.ts`` 反向读同一份——两侧各存一份
#: 迟早漂移，而「展示面不许出现方向词」这条纪律恰恰是最容易在一侧松掉的那种。
POSITION_GOLDEN_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "signalPositionGolden.json"
)


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN_PATH.exists(), f"金样文件缺失：{GOLDEN_PATH}"
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_file_is_present(golden: dict) -> None:
    assert golden["cases"], "金样用例为空——两侧一致性就没被验证"
    assert golden["bands"], "金样档位为空"


@pytest.mark.parametrize("case", json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"])
def test_matches_frontend_golden(case: dict) -> None:
    assert research_score(case["rankPct"]) == pytest.approx(case["score"])


def test_band_scale_matches_frontend_golden(golden: dict) -> None:
    """档位刻度两侧必须逐条相同（含切线），否则同一分数会给出不同档位。"""
    backend = [(minimum, label) for minimum, label in RESEARCH_SCORE_BANDS]
    frontend = [(float(b["min"]), b["label"]) for b in golden["bands"]]
    assert backend == frontend


def test_边界_0_与_1_是合法分数而非缺失() -> None:
    assert research_score(0.0) == 0.0
    assert research_score(1.0) == 100.0


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf"), True, "0.5"])
def test_缺失与非数值输入返回_None(bad: object) -> None:
    assert research_score(bad) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("out_of_range", [87.5, -0.1])
def test_越界返回_None_不猜不截断(out_of_range: float) -> None:
    """越界通常意味着把百分数当分位传进来了；静默截断会把口径错误伪装成正常分数。"""
    assert research_score(out_of_range) is None


def test_保留一位小数() -> None:
    assert research_score(0.871) == 87.1
    assert research_score(0.879) == 87.9


def test_银行家舍入不会分叉() -> None:
    """``round(0.5) == 0`` 的银行家舍入会与前端分叉，这里锁死四舍五入。"""
    assert research_score(0.0005) == 0.1
    assert research_score(0.8625) == 86.3


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100.0, "头部"),
        (85.0, "头部"),
        (84.9, "居前"),
        (70.0, "居前"),
        (69.9, "居中"),
        (60.0, "居中"),
        (59.9, "居后"),
        (40.0, "居后"),
        (39.9, "尾部"),
        (0.0, "尾部"),
    ],
)
def test_档位切线(score: float, expected: str) -> None:
    assert research_score_band(score) == expected


def test_缺失的档位是破折号而不是尾部() -> None:
    """把「没数据」说成「尾部」，等于替模型说了它没说过的话。"""
    assert research_score_band(None) == "—"


def test_缺失的格式化是破折号而不是_0() -> None:
    assert format_research_score(None) == "—"
    assert format_research_score(0.0) == "0.0"


def test_组合展示() -> None:
    assert format_research_score_with_band(87.2) == "87.2（头部）"
    assert format_research_score_with_band(None) == "—"


def test_由_rank_pct_一步得到展示串() -> None:
    assert format_rank_pct_as_research_score(0.871) == "87.1（头部）"
    assert format_rank_pct_as_research_score(None) == "—"


def test_口径声明不含方向性措辞() -> None:
    for word in ("买入", "卖出", "看多", "看空", "建议"):
        assert word not in RESEARCH_SCORE_HINT


def test_口径声明说明跨市场不可比() -> None:
    assert "截面" in RESEARCH_SCORE_HINT
    assert "跨市场" in RESEARCH_SCORE_HINT
    assert "不可直接比较" in RESEARCH_SCORE_HINT


# ---------------------------------------------------------------------------
# 信号位置词：同一张表的另一侧实现在前端 signalVocabulary.ts，共用金样防漂移。
# 每条都先断言金样非空 —— 空金样会让测试零项通过，那是最坏的一种绿。
# ---------------------------------------------------------------------------


def _position_golden() -> dict:
    return json.loads(POSITION_GOLDEN_PATH.read_text(encoding="utf-8"))


def test_位置词与金样逐条一致() -> None:
    golden = _position_golden()
    assert golden["labels"], "金样 labels 为空 —— 这条测试将零项通过"
    assert SIGNAL_POSITION_LABELS == golden["labels"]


def test_位置词不含方向性措辞() -> None:
    golden = _position_golden()
    banned = golden["bannedWords"]
    assert banned, "金样 bannedWords 为空 —— 禁词校验将零项通过"
    for side, label in golden["labels"].items():
        for word in banned:
            assert word not in label, f"{side} 的译法 {label!r} 含方向词 {word!r}"


def test_未知值与缺失一律破折号不猜() -> None:
    """猜一个方向出来是最坏的结果：既错，又等于替用户下了判断。"""
    golden = _position_golden()
    assert golden["unknownInputs"], "金样 unknownInputs 为空 —— 这条测试将零项通过"
    for raw in golden["unknownInputs"]:
        assert signal_position_label(raw) == "—", f"{raw!r} 不该被译出位置"
    assert signal_position_label(None) == "—"


def test_大小写与空白都归一() -> None:
    assert signal_position_label("buy") == signal_position_label("BUY")
    assert signal_position_label("  sell ") == signal_position_label("SELL")


def test_位置词不是百分比也不是数字档() -> None:
    """不写「前 20% / 后 20%」：该判定有分位与绝对阈值两条来源，写死百分比会在其中一条上变成假话。"""
    assert SIGNAL_POSITION_LABELS, "译法表为空 —— 这条测试将零项通过"
    for label in SIGNAL_POSITION_LABELS.values():
        assert "%" not in label
        assert not any(ch.isdigit() for ch in label)
