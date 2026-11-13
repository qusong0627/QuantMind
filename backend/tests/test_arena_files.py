"""arena 文件面写侧回归（``backend/services/agent_arena/arena_files.py``）。

契约来源是**读侧**：arena 前端与 ``agent_data.py`` 的 8 个读侧全部按
``position.jsonl`` / ``log.jsonl`` 的行格式写死。所以本测试**用真读侧读回写入
结果**，而不是断言自己写的字面量——字面量断言在写侧与读侧一起漂移时全绿，
而那正是要防的事。

写侧存在的理由见模块 docstring：``position.jsonl`` 全仓曾**零写入方**，
arena 的成交/持仓/净值三个 tab 因此冻结在 2026-09-29。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.agent_arena import agent_data, arena_files, arena_config

_AGENT = "glm-5.3-flash"
_DAY = "2026-10-14"


@pytest.fixture()
def arena_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 arena 数据根指向临时目录（唯一换根入口 ``QM_ARENA_DATA_ROOT``）。"""
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv(arena_files.ENABLE_FLAG, raising=False)
    return tmp_path


@pytest.fixture()
def config(arena_root: Path) -> dict:
    return arena_config.load_arena_config()


# ── 对话（log.jsonl）───────────────────────────────────────────────────


def test_dialogue_line_is_readable_by_arena_log_reader(arena_root, config):
    """一行对话写下去，读侧 `load_agent_logs` 必须原样读出来。"""
    # Arrange
    prompt = "【时间】2026-10-14 13:00\n【当前持仓】…"
    response = "【总体总结】仓位极轻，持有为主。\n【决策】{}"

    # Act
    ok = arena_files.append_dialogue(
        agent=_AGENT,
        prompt=prompt,
        response=response,
        usage={"prompt_tokens": 1104, "completion_tokens": 3819, "total_tokens": 4923},
        timestamp=datetime(2026, 10, 14, 13, 2, 19, tzinfo=arena_files.CN_TZ),
        day=_DAY,
    )

    # Assert
    assert ok is True
    records = agent_data.load_agent_logs(config, _AGENT, market="cn")
    assert len(records) == 1
    rec = records[0]
    assert rec["signature"] == _AGENT
    assert rec["new_messages"][0] == {"role": "user", "content": prompt}
    assert rec["new_messages"][1] == {"role": "assistant", "content": response}


def test_dialogue_timestamp_is_cst_wall_clock(arena_root):
    """时间戳必须是 CST 墙钟 + ``+08:00``。

    不写 UTC ``Z``：前端 ``fmtDateTime`` 是字符串切片不做时区换算，写 ``Z`` 会让
    界面钟面差 8 小时；``/token-usage`` 又用字符串比大小取最新，混格式会静默选错行。
    """
    arena_files.append_dialogue(
        agent=_AGENT,
        prompt="p",
        response="r",
        timestamp=datetime(2026, 10, 14, 9, 30, 0, tzinfo=arena_files.CN_TZ),
        day=_DAY,
    )

    raw = arena_files.log_file(_AGENT, _DAY).read_text(encoding="utf-8")
    assert json.loads(raw)["timestamp"] == "2026-10-14T09:30:00+08:00"


def test_dialogue_usage_present_for_token_usage_endpoint(arena_root, config):
    """带 usage 的行必须被 `/token-usage` 那条统计路径计入。

    复刻端点口径（``routers/agent_arena/agents_family.py``）：glob
    ``*/log/**/log.jsonl``，对 ``usage`` 为 dict 的行累加。
    """
    arena_files.append_dialogue(
        agent=_AGENT,
        prompt="p",
        response="r",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        day=_DAY,
    )

    files = list((arena_root / "agent_data_astock").glob("*/log/**/log.jsonl"))
    assert len(files) == 1
    rows = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    counted = [r for r in rows if isinstance(r.get("usage"), dict)]
    assert len(counted) == 1
    assert counted[0]["usage"]["total_tokens"] == 15


def test_missing_usage_is_omitted_not_forged_as_zero(arena_root):
    """取不到 usage 就不带这个键——绝不编造 0。

    ``{"total_tokens": 0}`` 会被 ``/token-usage`` 当成一次真实调用累加，token 账虚低。
    「这次没统计到」与「用了 0 个 token」是两件事。
    """
    arena_files.append_dialogue(agent=_AGENT, prompt="p", response="r", day=_DAY)

    row = json.loads(arena_files.log_file(_AGENT, _DAY).read_text(encoding="utf-8"))
    assert "usage" not in row


def test_usage_partial_keys_are_kept_as_given(arena_root):
    """只给了部分键就只写部分键（缺失的 token 数不许凭空补 0）。"""
    arena_files.append_dialogue(
        agent=_AGENT, prompt="p", response="r", usage={"total_tokens": 7}, day=_DAY
    )

    row = json.loads(arena_files.log_file(_AGENT, _DAY).read_text(encoding="utf-8"))
    assert row["usage"] == {"total_tokens": 7}


# ── 持仓与成交（position.jsonl）─────────────────────────────────────────


def test_position_snapshot_is_readable_as_holdings(arena_root, config):
    """持仓快照行：无 `this_action`，读侧当非交易行（不进成交表）。"""
    ok = arena_files.append_position(
        agent=_AGENT,
        positions={"600338.SH": 800, "601000.SH": 1700, "CASH": 91234.56},
        when=datetime(2026, 10, 14, 13, 5, tzinfo=arena_files.CN_TZ),
    )

    assert ok is True
    records = agent_data.load_position_records(config, _AGENT, market="cn")
    assert len(records) == 1
    assert records[0]["positions"]["600338.SH"] == 800
    assert records[0]["positions"]["CASH"] == 91234.56
    assert agent_data.load_trades(config, _AGENT, market="cn") == []


def test_position_action_row_is_readable_as_trade(arena_root, config):
    """成交行：带 `this_action` 才被 `load_trades` 认成成交。"""
    arena_files.append_position(
        agent=_AGENT,
        positions={"600338.SH": 800, "CASH": 91234.56},
        this_action={"action": "buy", "symbol": "600338.SH", "amount": 800},
        when=datetime(2026, 10, 14, 13, 1, 20, tzinfo=arena_files.CN_TZ),
    )

    trades = agent_data.load_trades(config, _AGENT, market="cn")
    assert len(trades) == 1
    assert trades[0]["this_action"] == {
        "action": "buy",
        "symbol": "600338.SH",
        "amount": 800,
    }
    assert trades[0]["date"] == "2026-10-14"


def test_position_ids_increment_monotonically(arena_root, config):
    """`id` 单调自增——前端按它排序去重，重复 id 会让成交行互相顶掉。"""
    for i in range(3):
        arena_files.append_position(
            agent=_AGENT,
            positions={"CASH": 100 - i},
            when=datetime(2026, 10, 14, 13, i, tzinfo=arena_files.CN_TZ),
        )

    records = agent_data.load_position_records(config, _AGENT, market="cn")
    assert [r["id"] for r in records] == [1, 2, 3]


def test_position_codes_normalize_to_suffix_form(arena_root, config):
    """PG 面的前缀式混进来必须归一到后缀式——否则前端按码查名查不到。"""
    arena_files.append_position(
        agent=_AGENT,
        positions={"SH600338": 800, "000001": 100, "CASH": 1.0},
        this_action={"action": "buy", "symbol": "SH600338", "amount": 800},
        when=datetime(2026, 10, 14, 13, 0, tzinfo=arena_files.CN_TZ),
    )

    rec = agent_data.load_position_records(config, _AGENT, market="cn")[0]
    assert set(rec["positions"]) == {"600338.SH", "000001.SZ", "CASH"}
    assert rec["this_action"]["symbol"] == "600338.SH"


def test_list_agents_reports_cash_and_latest_date(arena_root, config):
    """`list_agents` 用最后一行填 `cash` / `latest_date`——写侧不满足它，列表页就是空的。"""
    arena_files.append_position(
        agent=_AGENT,
        positions={"CASH": 91234.56},
        when=datetime(2026, 10, 14, 13, 0, tzinfo=arena_files.CN_TZ),
    )

    info = {a["name"]: a for a in agent_data.list_agents(config, market="cn")}
    assert info[_AGENT]["has_position"] is True
    assert info[_AGENT]["cash"] == 91234.56
    assert info[_AGENT]["latest_date"] == "2026-10-14"


# ── 分账账本 → positions 表 ─────────────────────────────────────────────


def test_positions_from_ledger_maps_volume_and_cash():
    """账本段（`{后缀码: {volume, …}}`）→ positions 表 + CASH。"""
    out = arena_files.positions_from_ledger(
        {
            "600338.SH": {"volume": 800, "cost_price": 11.5},
            "601000.SH": {"volume": 1700},
        },
        virtual_cash=91234.567,
    )

    assert out == {"600338.SH": 800, "601000.SH": 1700, "CASH": 91234.57}


def test_positions_from_ledger_keeps_zero_volume_rows():
    """volume=0 的票照写 0 不删键：「已清仓」与「从未持有」是两回事。"""
    out = arena_files.positions_from_ledger(
        {"600338.SH": {"volume": 0}}, virtual_cash=1.0
    )

    assert out["600338.SH"] == 0


def test_positions_from_ledger_unparsable_cash_degrades_to_zero():
    """现金取不出 → 写 0 而不是崩（展示面不许打断真钱回路）。"""
    out = arena_files.positions_from_ledger({}, virtual_cash=None)  # type: ignore[arg-type]

    assert out == {"CASH": 0.0}


# ── 边界与失败路径 ─────────────────────────────────────────────────────


def test_empty_agent_is_rejected_and_logged(arena_root, caplog):
    """空 agent 名必须拒写——否则会落进 ``agent_data_astock/`` 根目录本身。"""
    import logging

    with caplog.at_level(logging.ERROR, logger=arena_files.__name__):
        assert arena_files.append_dialogue(agent="", prompt="p", response="r") is False
        assert (
            arena_files.append_position(agent="   ", positions={"CASH": 1.0}) is False
        )

    assert not list((arena_root / "agent_data_astock").glob("*/log/**/log.jsonl"))
    assert len([r for r in caplog.records if "agent 名为空" in r.getMessage()]) == 2


def test_switch_off_writes_nothing(arena_root, monkeypatch):
    """``QM_ARENA_FILES=0`` 时一行都不落（排障退路）。"""
    monkeypatch.setenv(arena_files.ENABLE_FLAG, "0")

    assert arena_files.append_dialogue(agent=_AGENT, prompt="p", response="r") is False
    assert arena_files.append_position(agent=_AGENT, positions={"CASH": 1.0}) is False
    assert not arena_files.log_file(_AGENT, _DAY).exists()
    assert not arena_files.position_file(_AGENT).exists()


def test_switch_requires_true_token(arena_root, monkeypatch):
    """开关词表同 ``env_flags``：只有 ``true``（strip+小写）算开。"""
    for raw in ("1", "yes", "on", "TRUE ", " true "):
        monkeypatch.setenv(arena_files.ENABLE_FLAG, raw)
        expected = raw.strip().lower() == "true"
        assert arena_files.arena_files_enabled() is expected, raw


def test_oversize_line_is_rejected_not_truncated(arena_root, caplog):
    """超长整行拒写并报错——半截 JSON 读侧解析不了，而文件里看起来「有一行」。"""
    import logging

    huge = "x" * (arena_files._LINE_LIMIT + 1)

    with caplog.at_level(logging.ERROR, logger=arena_files.__name__):
        ok = arena_files.append_dialogue(agent=_AGENT, prompt=huge, response="r")

    assert ok is False
    assert any("超长" in r.getMessage() for r in caplog.records)
    assert not arena_files.log_file(_AGENT).exists()


def test_write_failure_returns_false_and_logs(arena_root, monkeypatch, caplog):
    """磁盘写失败：返回 False + error 日志，**不抛**（展示面不许打断真钱回路）。"""
    import logging

    def _boom(self, *a, **kw):  # noqa: ANN001
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "open", _boom)

    with caplog.at_level(logging.ERROR, logger=arena_files.__name__):
        ok = arena_files.append_dialogue(agent=_AGENT, prompt="p", response="r")

    assert ok is False
    assert any("写入失败" in r.getMessage() for r in caplog.records)


def test_empty_response_still_writes_row(arena_root, config):
    """空回复也落行——「模型这次什么都没说」是要被看见的事实，不是省略。"""
    assert (
        arena_files.append_dialogue(agent=_AGENT, prompt="p", response="", day=_DAY)
        is True
    )

    rec = agent_data.load_agent_logs(config, _AGENT, market="cn")[0]
    assert rec["new_messages"][1]["content"] == ""
