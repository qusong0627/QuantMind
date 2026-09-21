"""导出件免责段（后端侧）：单源、必随文件走、不编造区间。

金样只有**一份**（``backend/tests/fixtures/exportDisclaimerGolden.json``），
前端 ``electron/src/utils/exportDisclaimer.ts`` 的测试读同一个文件
（``electron/src/utils/__tests__/exportDisclaimer.test.ts``）。理由与
``researchScoreGolden.json`` 相同：前后端各持一份实现（不同语言、不同产物，
无法共享代码），措辞靠金样钉住 —— 任一侧改了而另一侧没跟，两侧测试都红。

本文件测的是**后端那份**；前端那份由前端测试覆盖，两侧断言的是同一个字符串。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.shared.export_disclaimer import (
    DISCLAIMER_LABELS,
    DISCLAIMER_SENTENCE,
    data_range_text,
    disclaimer_rows,
    format_export_time,
    write_csv_disclaimer,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "exportDisclaimerGolden.json"
)
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

FIXED = datetime(2026, 9, 21, 14, 32, 5)


# ── 1. 金样对拍：后端措辞必须等于金样 ─────────────────────────────────


def test_sentence_matches_golden() -> None:
    """后端那句必须与前端共读的金样逐字相同（含标点）。"""
    assert DISCLAIMER_SENTENCE == GOLDEN["sentence"], (
        "后端免责语句与金样不一致。改措辞要同时改：\n"
        f"  {GOLDEN_PATH}\n"
        "  electron/src/utils/exportDisclaimer.ts\n"
        "  backend/shared/export_disclaimer.py"
    )


def test_labels_match_golden() -> None:
    assert dict(DISCLAIMER_LABELS) == GOLDEN["labels"]


@pytest.mark.parametrize("phrase", GOLDEN["requiredPhrases"])
def test_sentence_keeps_required_phrases(phrase: str) -> None:
    """合规底线：无论怎么润色，「学习研究 / 技术演示 / 不构成投资建议」都得在。

    这条不是重复上面的对拍 —— 上面钉「与金样一致」，这条钉「金样本身没被改成
    一句不再免责的话」。有人把金样与两侧实现一起改成「仅供参考，据此操作
    风险自负」时，对拍会绿而这组会红。
    """
    assert phrase in DISCLAIMER_SENTENCE


# ── 2. 行构成：生成时间恒在，数据区间只在拿得到时出现 ────────────────


def test_rows_always_carry_export_time() -> None:
    rows = dict(disclaimer_rows(now=FIXED))
    assert rows[DISCLAIMER_LABELS["exportedAt"]] == "2026-09-21 14:32:05"
    assert rows[DISCLAIMER_LABELS["sentence"]] == DISCLAIMER_SENTENCE


def test_rows_omit_range_when_absent() -> None:
    """拿不到区间就整行不出现 —— 绝不写一个空值或假区间充数。"""
    labels = [k for k, _ in disclaimer_rows(now=FIXED)]
    assert DISCLAIMER_LABELS["dataRange"] not in labels


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_rows_omit_range_when_blank(blank: object) -> None:
    """空串/空白/None 一律视为「没有」—— 空值不得伪装成「有区间」。"""
    labels = [k for k, _ in disclaimer_rows(data_range=blank, now=FIXED)]  # type: ignore[arg-type]
    assert DISCLAIMER_LABELS["dataRange"] not in labels


def test_rows_order_is_time_then_range_then_sentence() -> None:
    """语句压尾，且区间排在时间之后 —— 顺序固定，两侧渲染器不必各判一次。"""
    labels = [
        k for k, _ in disclaimer_rows(data_range="2026-01-01 ~ 2026-09-18", now=FIXED)
    ]
    assert labels == [
        DISCLAIMER_LABELS["exportedAt"],
        DISCLAIMER_LABELS["dataRange"],
        DISCLAIMER_LABELS["sentence"],
    ]


def test_range_text_degrades_to_single_ended() -> None:
    """只给一端就写单端，不产出「~ 2026-09-18」这种半截区间。"""
    assert data_range_text("2026-01-01", "2026-09-18") == "2026-01-01 ~ 2026-09-18"
    assert data_range_text("2026-01-01", None) == "2026-01-01 起"
    assert data_range_text(None, "2026-09-18") == "截至 2026-09-18"
    assert data_range_text(None, None) is None
    # 两端同日不必写成 "x ~ x"
    assert data_range_text("2026-09-18", "2026-09-18") == "2026-09-18"


def test_format_export_time_pads() -> None:
    assert format_export_time(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"


# ── 3. CSV 渲染：追加在尾部、成列、不破列 ────────────────────────────


def _csv_lines(rows: list[list[object]], **kw: object) -> list[list[str]]:
    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    write_csv_disclaimer(writer, **kw)  # type: ignore[arg-type]
    output.seek(0)
    return list(csv.reader(output))


def test_csv_disclaimer_comes_last() -> None:
    """数据在前、免责段在后，中间空行隔开。"""
    parsed = _csv_lines([["日期", "代码"], ["2026-09-18", "SH600036"]], now=FIXED)
    assert parsed[0] == ["日期", "代码"]
    assert parsed[1] == ["2026-09-18", "SH600036"]
    assert parsed[2] == []  # 空行分隔
    assert parsed[3][0] == DISCLAIMER_LABELS["exportedAt"]
    assert parsed[-1][0] == DISCLAIMER_LABELS["sentence"]


def test_csv_disclaimer_columns_survive_commas() -> None:
    """值里带逗号也必须留在同一格 —— 靠 csv.writer 的引号规则，不靠人肉保证。"""
    parsed = _csv_lines([["x"]], data_range="2026-01-01, 2026-09-18", now=FIXED)
    range_row = next(r for r in parsed if r and r[0] == DISCLAIMER_LABELS["dataRange"])
    assert range_row == [DISCLAIMER_LABELS["dataRange"], "2026-01-01, 2026-09-18"]
