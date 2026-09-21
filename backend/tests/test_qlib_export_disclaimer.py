"""回测导出接口（`/export/{backtest_id}/csv`）必须把免责段写进 CSV 末尾。

缺口（2026-09-21 核实）：这条链路此前完全没有免责段 —— 而它导出的正是
「日期/代码/方向/成交价…」的交易流水，是最容易被当成操作依据的那一类文件。
纯函数 `write_csv_disclaimer` 已由 `test_export_disclaimer.py` 覆盖；
**这里证明的是接线本身**：handler 真的调了它，且两种 style 都调了
（`style` 是两个独立分支，只在其中一个分支加免责段是很容易犯的错）。
"""

from __future__ import annotations

import asyncio
import csv
import io

import pytest
from starlette.requests import Request

from backend.services.engine.qlib_app.api.export import export_backtest
from backend.shared.export_disclaimer import DISCLAIMER_LABELS, DISCLAIMER_SENTENCE

RESULT: dict = {
    "start_date": "2026-01-01",
    "end_date": "2026-09-18",
    "initial_capital": 1_000_000.0,
    "trades": [
        {
            "date": "2026-01-05",
            "symbol": "SH600036",
            "action": "buy",
            "price": 10.5,
            "quantity": 100,
            "commission": 5.0,
        },
        # 逗号在 symbol 里：验证走的是 csv 模块的引号逻辑，而不是手拼字符串
        {
            "date": "2026-01-06",
            "symbol": "SH600036,SH601318",
            "action": "sell",
            "price": 11.0,
            "quantity": 100,
            "commission": 5.0,
        },
    ],
    "trade_list": [
        {
            "date": "2026-01-05",
            "action": "buy",
            "price": 10.5,
            "quantity": 100,
            "pnl": 0.0,
        }
    ],
}


class _FakeService:
    """只实现 handler 用到的 `get_result`。"""

    def __init__(self, result: dict | None) -> None:
        self._result = result

    async def get_result(self, backtest_id: str, **_kwargs):  # noqa: ANN201
        return self._result


def _request() -> Request:
    """带认证身份的极简请求（handler 走 `_identity_from_request`）。"""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "state": {"user": {"user_id": "u1", "tenant_id": "t1"}},
        }
    )


def _export_rows(result: dict | None = RESULT, style: str = "quick") -> list[list[str]]:
    """跑一遍 handler，把流式响应的正文解析成行。

    BOM 是响应自带的（Excel 中文显示用），解析前先剥掉，否则首格带 ``\\ufeff``。
    """
    response = asyncio.run(
        export_backtest(
            request=_request(),
            backtest_id="abcdef123456",
            tenant_id=None,
            style=style,
            service=_FakeService(result),
        )
    )

    async def _drain() -> str:
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(c if isinstance(c, str) else c.decode("utf-8") for c in chunks)

    text = asyncio.run(_drain()).lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _tail_lines(rows: list[list[str]]) -> list[str]:
    return [" ".join(r) for r in rows]


# ── 两种 style 都要有免责段 ─────────────────────────────────────────


@pytest.mark.parametrize("style", ["quick", "legacy"])
def test_export_csv_carries_disclaimer(style: str) -> None:
    rows = _export_rows(style=style)
    lines = _tail_lines(rows)

    assert any(DISCLAIMER_SENTENCE in line for line in lines), (
        f"{style}: 缺免责语句\n{lines[-6:]}"
    )

    labels = [r[0] for r in rows if r]
    assert DISCLAIMER_LABELS["exportedAt"] in labels, f"{style}: 缺生成时间行"
    assert DISCLAIMER_LABELS["dataRange"] in labels, f"{style}: 缺数据区间行"

    range_row = next(r for r in rows if r and r[0] == DISCLAIMER_LABELS["dataRange"])
    assert range_row[1] == "2026-01-01 ~ 2026-09-18"


def test_disclaimer_is_a_tail_block_not_a_prefix() -> None:
    """表头仍是第一行 —— 这条是硬约定：按行序消费导出件的下游不吃前缀块。"""
    rows = _export_rows()
    assert rows[0][0] == "日期", f"首行被改写: {rows[0]}"
    # 免责段前有空行分隔，且后面不再有数据行
    disclaimer_idx = next(
        i for i, r in enumerate(rows) if r and r[0] == DISCLAIMER_LABELS["exportedAt"]
    )
    assert rows[disclaimer_idx - 1] == [], "免责段前应有空行"
    assert all(
        not r or r[0] in DISCLAIMER_LABELS.values() for r in rows[disclaimer_idx:]
    )


# ── 数据行不受影响 ─────────────────────────────────────────────────


def test_data_rows_survive_and_commas_stay_in_one_cell() -> None:
    rows = _export_rows()
    header = rows[0]
    data_rows = [r for r in rows[1:] if r and r[0] in ("2026-01-05", "2026-01-06")]
    assert len(data_rows) == 2, f"数据行数不对: {rows}"

    header_idx = {name: i for i, name in enumerate(header)}
    comma_row = next(r for r in data_rows if r[0] == "2026-01-06")
    # 逗号必须仍在同一格里（csv 引号生效），行宽与表头一致
    assert comma_row[header_idx["代码"]] == "SH600036,SH601318"
    assert len(comma_row) == len(header), "带逗号的字段把行撑破列了"


# ── 区间缺失时省略整行 ─────────────────────────────────────────────


def test_export_omits_range_row_when_result_has_no_dates() -> None:
    rows = _export_rows(result={"trades": []})
    labels = [r[0] for r in rows if r]
    assert DISCLAIMER_LABELS["dataRange"] not in labels
    # 生成时间与语句照旧（不是整段没写）
    assert DISCLAIMER_LABELS["exportedAt"] in labels
    assert any(DISCLAIMER_SENTENCE in line for line in _tail_lines(rows))


def test_export_degrades_to_single_ended_range_instead_of_writing_none() -> None:
    """只剩一端时退化成「… 起」，**绝不**把 ``None`` 字面量写进文件。

    这是本模块最容易被写错的一处：`f"{start} ~ {end}"` 会产出
    「2026-01-01 ~ None」—— 看起来像有区间，实际是坏数据。
    """
    rows = _export_rows(result={"start_date": "2026-01-01", "trades": []})
    range_row = next(r for r in rows if r and r[0] == DISCLAIMER_LABELS["dataRange"])
    assert range_row[1] == "2026-01-01 起"

    only_end = _export_rows(result={"end_date": "2026-09-18", "trades": []})
    end_row = next(r for r in only_end if r and r[0] == DISCLAIMER_LABELS["dataRange"])
    assert end_row[1] == "截至 2026-09-18"
