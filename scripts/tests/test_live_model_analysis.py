"""实盘模型对话轮的提示词降级与截断纪律——审计 C5/H13 的回归闸。

跑法（scripts/ 是 bind mount，宿主/容器都可；容器：``docker exec -w /app``）::

    python3 -m pytest scripts/tests/ -q

盯三件事，错了都不会报错、只会让「模型对话」里的结论变假：

* **桥挂 → 无账户模板**：提示词不许要求「逐一点评持仓」，且要逐字禁止编造
  持仓/账户数字；``data_gaps=['account_unreachable']`` 必须随正文一起返回
  （前端横幅与日志标记全靠它，靠文本刮擦不算数）。
* **空仓 ≠ 桥挂**：账户在、仓位空——不是数据缺口（无横幅），但也不许点评持仓。
* **截断轮绝不落盘**：``finish_reason=='length'`` 重试 1 次后仍截断即抛
  ``TruncatedOutputError``，``main`` 不会调 ``append_log``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根

import live_model_analysis as lma  # noqa: E402


def _acct(*positions: dict) -> dict:
    return {
        "asset": {"asset": 100000.0, "cash": 20000.0, "market_value": 80000.0},
        "positions": list(positions),
    }


def _pos(code: str, cost: float = 10.0) -> dict:
    return {
        "stock_code": code,
        "stock_name": "测试",
        "total_volume": 100,
        "available_volume": 100,
        "cost_price": cost,
    }


def _build(account, *, price: float = 12.0):
    return lma.build_user_content(
        {},
        account_reader=lambda env: account,
        price_reader=lambda env, code: price,
    )


class TestDataGapTemplate:
    def test_bridge_down_forbids_position_commentary_and_flags_gap(self):
        """验收（C5）：无账户数据 → 模板无「逐一点评持仓」+ 逐字禁止编造 + 缺口标记。"""
        content, gaps = _build(None)

        assert gaps == ["account_unreachable"]
        assert "逐一点评持仓" not in content  # 要求短语必须消失
        assert "**禁止**点评持仓" in content  # 只有禁止式表达允许存在
        assert "禁止编造" in content
        assert "桥不可达" in content

    def test_account_with_positions_keeps_the_normal_template(self):
        content, gaps = _build(_acct(_pos("600036.SH", cost=10.0)), price=12.0)

        assert gaps == []
        assert "逐一点评持仓" in content
        assert "| 600036.SH | 测试 | 100 | 100 | 10.000 | 12.00 | +20.00% |" in content

    def test_empty_positions_is_not_a_gap_but_still_no_commentary_ask(self):
        """空仓 = 账户数据在（无横幅），但空仓点评无从谈起——模板要换掉。"""
        content, gaps = _build(_acct())

        assert gaps == []
        assert "逐一点评持仓" not in content
        assert "空仓" in content
        assert "禁止编造" in content

    def test_news_block_absence_does_not_change_the_gap_list(self, monkeypatch):
        monkeypatch.setattr(lma, "_news_block", lambda: "")  # 不读真实 latest.json

        content, gaps = _build(None)

        assert gaps == ["account_unreachable"]  # 新闻缺失不是账户缺口（本项范围外）
        assert "【当日新闻分子】" not in content


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _resp(finish: str, content: str = "分析正文") -> _FakeResp:
    return _FakeResp(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


ENV = {"OPENAI_API_BASE": "http://fake", "OPENAI_API_KEY": "k"}
SIG = "deepseek-v4-flash"


def _patch_post(monkeypatch, responses: list[_FakeResp]) -> list[str]:
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr("requests.post", fake_post)
    return calls


class TestCallModelTruncation:
    def test_length_finish_retries_once_then_raises_with_content(self, monkeypatch):
        """验收（H13）：截断 → 重试 1 次；仍截断 → 抛错（调用方据此拒落盘）。"""
        calls = _patch_post(monkeypatch, [_resp("length"), _resp("length")])

        with pytest.raises(lma.TruncatedOutputError) as ei:
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

        assert len(calls) == 2  # 恰好重试一次，不是三次五次
        assert ei.value.content == "分析正文"  # 原文随异常携带，供人工审计
        assert ei.value.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }

    def test_truncated_then_complete_retry_recovers(self, monkeypatch):
        calls = _patch_post(monkeypatch, [_resp("length"), _resp("stop", "完整分析")])

        content, usage = lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

        assert content == "完整分析"
        assert len(calls) == 2
        assert usage["total_tokens"] == 15

    def test_truncated_reasoning_fallback_is_not_salvaged(self, monkeypatch):
        """reasoning 兜底捞出的思考草稿若被截断，必须同样作废（2026-09-08 实录教训）。"""
        truncated = _FakeResp(
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "思考到一半…"},
                        "finish_reason": "length",
                    }
                ]
            }
        )
        _patch_post(monkeypatch, [truncated, truncated])

        with pytest.raises(lma.TruncatedOutputError):
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

    def test_empty_content_with_stop_finish_is_empty_reply(self, monkeypatch):
        _patch_post(monkeypatch, [_resp("stop", ""), _resp("stop", "")])

        with pytest.raises(RuntimeError, match="空回复"):
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")


class TestAppendLogGaps:
    def test_gaps_field_lands_in_the_jsonl_entry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lma, "LOG_DIR", tmp_path)

        path = lma.append_log("提示词", "正文", SIG, None, ["account_unreachable"])

        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["data_gaps"] == ["account_unreachable"]
        assert [m["role"] for m in entry["new_messages"]] == ["user", "assistant"]

    def test_no_gaps_means_no_field(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lma, "LOG_DIR", tmp_path)

        path = lma.append_log("提示词", "正文", SIG, None, [])

        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert "data_gaps" not in entry  # 无缺口不落字段（前端横幅判据=字段存在）


class TestMainRejectsTruncatedRound:
    def test_truncated_round_is_never_written(self, monkeypatch, tmp_path):
        """验收：截断 → main 报失败且 append_log 一次都不被调用（拒落盘）。"""
        monkeypatch.setattr(lma, "LOCK", tmp_path / "lock")
        monkeypatch.setattr(lma, "_load_env", lambda: {})
        monkeypatch.setattr(lma, "build_user_content", lambda env, **kw: ("提示词", []))
        written: list = []
        monkeypatch.setattr(lma, "append_log", lambda *a, **kw: written.append(a))

        def boom(env, sig, model, user_content):
            raise lma.TruncatedOutputError("半截", None)

        monkeypatch.setattr(lma, "call_model", boom)
        monkeypatch.setattr(sys, "argv", ["live_model_analysis.py", "--agents", SIG])

        assert lma.main() == 2  # 全部失败
        assert written == []
