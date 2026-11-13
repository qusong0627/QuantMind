"""Pine 对话 worker：上下文拼装 + 队列消费（模型调用打桩，不联网）。

关键口径：候选/回答只在宿主上产生，容器只读结果；本轮问题不进历史（否则会重复问两遍）。

运行（容器）：python3 -m pytest backend/tests/test_pine_chat_worker.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.agent_arena import pine_library as pl          # noqa: E402
from scripts import pine_chat_worker as pcw              # noqa: E402


def _fake_source(item_id: str, source: str = "//@version=5\nstrategy('t')") -> dict:
    return {"id": item_id, "title": "斐波那契云", "file": "001.pine",
            "category": "趋势跟踪", "version": 5, "lines": 120,
            "source_kind": "desktop", "source": source}


@pytest.fixture()
def chat_env(tmp_path, monkeypatch):
    """对话目录指到临时库；源码/备注/报告/模型全部打桩。"""
    d = tmp_path / "chat"
    monkeypatch.setattr(pl, "chat_dir", lambda: d)
    monkeypatch.setattr(pl, "chat_queue", lambda: d / "queue")
    monkeypatch.setattr(pl, "chat_jobs", lambda: d / "jobs")
    monkeypatch.setattr(pl, "get_source", lambda item_id: _fake_source(item_id))
    monkeypatch.setattr(pl, "get_note", lambda item_id: {
        "id": item_id, "note": "趋势市有效", "tags": ["趋势"], "rating": 4,
        "status": "观察中", "by": "user"})
    monkeypatch.setattr(pl, "read_report", lambda item_id: {
        "symbol": "600309.SH", "adj": "backward", "trades": 203,
        "stats": {"Net profit": {"pct": 62.3}, "Buy & hold return": {"pct": 555.1},
                  "Max equity drawdown": {"pct": 42.2}}})
    # 基线新鲜度看的是真实文件 mtime → 一律指到临时目录，别去读仓库里的正式报告
    monkeypatch.setattr(pl, "report_path", lambda item_id: tmp_path / "report.json")
    monkeypatch.setattr(pl, "source_path", lambda item_id: tmp_path / "source.pine")
    monkeypatch.setattr(pcw, "_chat",
                        lambda messages, model, think=False: ("这是回答", {"total_tokens": 10}, False))
    return d


class TestBuildMessages:
    def test_carries_source_note_report_and_history(self, chat_env):
        msgs = pcw.build_messages("0001", "问题", [
            {"role": "user", "content": "旧问"}, {"role": "assistant", "content": "旧答"}])
        assert msgs[0]["role"] == "system"
        ctx = msgs[0]["content"]
        assert "斐波那契云" in ctx
        assert "趋势市有效" in ctx
        assert "净收益 62.3%" in ctx and "最大回撤 42.2%" in ctx
        assert "strategy('t')" in ctx
        assert [m["content"] for m in msgs[1:]] == ["旧问", "旧答", "问题"]

    def test_says_no_backtest_when_report_missing(self, chat_env, monkeypatch):
        monkeypatch.setattr(pl, "read_report", lambda item_id: {})
        ctx = pcw.build_messages("0001", "问", [])[0]["content"]
        assert "还没有" in ctx

    def test_truncates_long_source(self, chat_env, monkeypatch):
        monkeypatch.setattr(pl, "get_source",
                            lambda item_id: _fake_source(item_id, "x" * (pcw.MAX_SOURCE_CHARS + 500)))
        ctx = pcw.build_messages("0001", "问", [])[0]["content"]
        assert "已截断" in ctx
        assert len(ctx) < pcw.MAX_SOURCE_CHARS + 2000

    def test_drops_rows_with_unexpected_role(self, chat_env):
        msgs = pcw.build_messages("0001", "问", [{"role": "system", "content": "越权"}])
        assert [m["role"] for m in msgs] == ["system", "user"]


class TestProcess:
    def test_ask_flow_writes_thread_and_done_job(self, chat_env):
        req = pl.enqueue_chat("0001", "适合震荡市吗？")["request"]

        job = pcw.process(req)

        assert job["status"] == "done" and job["reply"] == "这是回答"
        thread = pl.chat_thread("0001")["messages"]
        assert [m["role"] for m in thread] == ["user", "assistant"]
        assert thread[0]["content"] == "适合震荡市吗？" and thread[0]["job_id"] == req["job_id"]
        assert not list(pl.chat_queue().glob("*.json"))     # 摘牌了
        saved = json.loads((pl.chat_jobs() / f"{req['job_id']}.json").read_text(encoding="utf-8"))
        assert saved["status"] == "done"

    def test_current_question_is_not_duplicated_in_history(self, chat_env, monkeypatch):
        seen = {}

        def fake(messages, model, think=False):
            seen["msgs"] = messages
            return ("答", None, False)

        monkeypatch.setattr(pcw, "_chat", fake)
        pcw.process(pl.enqueue_chat("0001", "第一问")["request"])
        pcw.process(pl.enqueue_chat("0001", "第二问")["request"])
        assert [m["content"] for m in seen["msgs"] if m["role"] == "user"] == ["第一问", "第二问"]

    def test_model_called_without_thinking(self, chat_env, monkeypatch):
        seen = {}

        def fake(messages, model, think=False):
            seen.update(model=model, think=think)
            return ("答", None, False)

        monkeypatch.setattr(pcw, "_chat", fake)
        pcw.process(pl.enqueue_chat("0001", "问")["request"])
        assert seen == {"model": pl.CHAT_MODEL, "think": False}

    def test_unknown_mode_rejected(self, chat_env):
        req = pl.enqueue_chat("0001", "问", mode="ask")["request"]
        req["mode"] = "magic"

        job = pcw.process(req)

        assert job["status"] == "failed" and job["stage"] == "mode"
        assert pl.chat_thread("0001")["count"] == 0        # 没被当成问答写进历史

    def test_llm_failure_marks_job_failed(self, chat_env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pcw, "_chat", boom)
        req = pl.enqueue_chat("0001", "问")["request"]

        with pytest.raises(RuntimeError):
            pcw.process(req)

        saved = json.loads((pl.chat_jobs() / f"{req['job_id']}.json").read_text(encoding="utf-8"))
        assert saved["status"] == "failed" and "boom" in saved["error"]

    def test_empty_reply_marks_failed(self, chat_env, monkeypatch):
        monkeypatch.setattr(pcw, "_chat", lambda *a, **k: ("", None, False))
        job = pcw.process(pl.enqueue_chat("0001", "问")["request"])
        assert job["status"] == "failed" and "空内容" in job["error"]

    def test_missing_fields_rejected(self, chat_env):
        with pytest.raises(ValueError):
            pcw.process({"id": "0001", "message": "问"})


PINE_REPLY = """把止损从 ATR 改成固定 3%，其余不动。

```pine
//@version=5
strategy("改过的", initial_capital=100000, default_qty_value=95, default_qty_type=strategy.percent_of_equity)
x = 1
```
"""


class TestExtractPine:
    def test_takes_the_pine_block(self):
        assert pcw.extract_pine(PINE_REPLY).startswith("//@version=5")

    def test_skips_examples_without_pine_markers(self):
        text = "看这个\n```\nfoo()\n```\n真源码\n```pine\nstrategy(\"x\")\n```"
        assert 'strategy("x")' in pcw.extract_pine(text)

    def test_no_block_is_empty(self):
        assert pcw.extract_pine("就是不改") == ""

    def test_strip_removes_code_leaves_explanation(self):
        got = pcw.strip_pine_block(PINE_REPLY)
        assert "把止损从 ATR 改成固定 3%" in got and "```" not in got


class TestEditMode:
    def _fake_pipeline(self, monkeypatch, fields: dict, calls: list):
        def fake_process(req, **kw):
            calls.append((req, kw))
            d = Path(kw["item_dir"])
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps(
                {"problems": [], "title": "改过的"}), encoding="utf-8")
            (d / "report.json").write_text(json.dumps({
                "symbol": "600309.SH", "adj": "backward", "trades": 180,
                "stats": {"Net profit": {"pct": 70.0}, "Buy & hold return": {"pct": 500.0},
                          "Max equity drawdown": {"pct": 30.0}}}), encoding="utf-8")
            return fields

        monkeypatch.setattr(pcw.ptw, "process", fake_process)

    def test_edit_runs_candidate_and_writes_compare(self, chat_env, monkeypatch):
        calls: list = []
        self._fake_pipeline(monkeypatch, {"status": "staged", "stage": "done"}, calls)
        monkeypatch.setattr(pcw, "_chat", lambda *a, **k: (PINE_REPLY, {"total_tokens": 9}, False))
        enq = pl.enqueue_chat("0001", "止损改成 3%", mode="edit")["request"]

        job = pcw.process(enq)

        assert job["status"] == "done" and job["stage"] == "done"
        assert job["reply"].startswith("把止损从 ATR 改成固定 3%")
        assert "```" not in job["reply"]                    # 代码块不进对话正文
        req, kw = calls[0]
        assert req["source"].endswith("candidate.pine")
        assert req["symbol"] == "600309.SH"                 # 沿用正式报告的标的
        assert kw["success_status"] == "staged"             # 不能当成终态，还要补对比
        assert kw["job_path"] == pl.chat_jobs() / f"{enq['job_id']}.json"
        cand = job["candidate"]
        assert cand["pine"].startswith("//@version=5")
        assert cand["compare"]["before"]["Net profit"]["pct"] == 62.3
        assert cand["compare"]["after"]["Net profit"]["pct"] == 70.0
        assert cand["compare"]["after"]["trades"]["value"] == 180
        assert cand["compare"]["before_stale"] is False      # 临时目录里没有报告 → 不误报
        assert pl.chat_thread("0001")["messages"][-1]["mode"] == "edit"

    def test_edit_without_pine_block_fails(self, chat_env, monkeypatch):
        monkeypatch.setattr(pcw, "_chat", lambda *a, **k: ("我做不到", None, False))
        monkeypatch.setattr(pcw.ptw, "process",
                            lambda *a, **k: pytest.fail("没有源码不该进候选链路"))

        job = pcw.process(pl.enqueue_chat("0001", "加个比特币过滤", mode="edit")["request"])

        assert job["status"] == "failed" and job["stage"] == "edit"
        assert "Pine" in job["error"]

    def test_edit_candidate_failure_keeps_detail(self, chat_env, monkeypatch):
        calls: list = []
        self._fake_pipeline(monkeypatch, {
            "status": "failed", "stage": "static",
            "error": "静态检查未通过：仓位口径必须与库内统一",
            "problems": ["仓位口径必须与库内统一"]}, calls)
        monkeypatch.setattr(pcw, "_chat", lambda *a, **k: (PINE_REPLY, None, False))

        job = pcw.process(pl.enqueue_chat("0001", "本金改成 20 万", mode="edit")["request"])

        assert job["status"] == "failed" and job["stage"] == "static"
        assert "仓位口径" in job["error"]
        assert job["problems"] == ["仓位口径必须与库内统一"]
        assert job["reply"].startswith("把止损从 ATR")       # 说明仍然保留给用户看

    def test_edit_falls_back_to_default_symbol(self, chat_env, monkeypatch):
        monkeypatch.setattr(pl, "read_report", lambda item_id: {})
        calls: list = []
        self._fake_pipeline(monkeypatch, {"status": "staged", "stage": "done"}, calls)
        monkeypatch.setattr(pcw, "_chat", lambda *a, **k: (PINE_REPLY, None, False))

        pcw.process(pl.enqueue_chat("0001", "改", mode="edit")["request"])

        assert calls[0][0]["symbol"] == pcw.DEFAULT_SYMBOL
        assert calls[0][0]["adj"] == "backward"

    def test_edit_prompt_asks_for_pine(self, chat_env):
        ctx = pcw.build_messages("0001", "改", [], mode="edit")[0]["content"]
        assert "```pine" in ctx and "仓位口径" in ctx
        assert ctx != pcw.build_messages("0001", "改", [])[0]["content"]


class TestBaseline:
    """「改前」那列取自最近一次正式报告，比当前源码旧就必须标出来。"""

    def _pair(self, tmp_path, report_at: float, source_at: float):
        rp = tmp_path / "report.json"
        sp = tmp_path / "source.pine"
        rp.write_text("{}", encoding="utf-8")
        sp.write_text("x", encoding="utf-8")
        os.utime(rp, (report_at, report_at))
        os.utime(sp, (source_at, source_at))
        return rp, sp

    def test_source_newer_than_report_marks_stale(self, chat_env, monkeypatch, tmp_path):
        rp, sp = self._pair(tmp_path, report_at=1_700_000_000, source_at=1_800_000_000)
        monkeypatch.setattr(pl, "report_path", lambda item_id: rp)
        monkeypatch.setattr(pl, "source_path", lambda item_id: sp)

        got = pcw._baseline("0001")

        assert got["before_stale"] is True
        assert got["before_at"].startswith("2023-")            # 报告自己的时间，不是源码的

    def test_report_newer_than_source_is_fresh(self, chat_env, monkeypatch, tmp_path):
        rp, sp = self._pair(tmp_path, report_at=1_800_000_000, source_at=1_700_000_000)
        monkeypatch.setattr(pl, "report_path", lambda item_id: rp)
        monkeypatch.setattr(pl, "source_path", lambda item_id: sp)

        assert pcw._baseline("0001")["before_stale"] is False

    def test_missing_files_are_not_stale(self, chat_env, monkeypatch, tmp_path):
        monkeypatch.setattr(pl, "report_path", lambda item_id: tmp_path / "无.json")
        monkeypatch.setattr(pl, "source_path", lambda item_id: tmp_path / "无.pine")

        assert pcw._baseline("0001") == {"before_at": "", "before_stale": False}
