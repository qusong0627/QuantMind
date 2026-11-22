"""Pine 语料批量回测：策略筛选 / 行 schema / 排行名称映射。

不真跑 PyneCore：`_run_chunk` 里的 load_klines / run_script_on_bars 都打桩，
只锁批量编排契约（进程池外沿由小池真机冒烟覆盖）。

运行（容器）：python3 -m pytest backend/tests/test_lab_batch_pyne.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import lab_batch_pyne as bp  # noqa: E402


def _items():
    return [
        {
            "id": "9001",
            "title": "A股多头",
            "market_hint": "cn",
            "source_kind": "imported",
        },
        {
            "id": "9002",
            "title": "美股多空",
            "market_hint": "us",
            "source_kind": "imported",
        },
        {
            "id": "9003",
            "title": "另一个多头",
            "market_hint": "cn",
            "source_kind": "imported",
        },
    ]


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    for sid in ("9001", "9002", "9003"):
        d = tmp_path / sid
        d.mkdir()
        (d / "candidate.py").write_text(
            "@script.strategy('x')\ndef main():\n    pass\n", encoding="utf-8"
        )
    monkeypatch.setattr(bp.pl, "transpile_dir", lambda: tmp_path)
    return tmp_path


class TestSelectStrategies:
    def test_cn_hint_only_long(self, candidates):
        got = bp.select_strategies(_items(), "cn", "all", "hint")
        assert [s["id"] for s in got] == ["9001", "9003"]

    def test_us_hint_only_short_capable(self, candidates):
        got = bp.select_strategies(_items(), "us", "all", "hint")
        assert [s["id"] for s in got] == ["9002"]

    def test_hk_hint_includes_all(self, candidates):
        got = bp.select_strategies(_items(), "hk", "all", "hint")
        assert [s["id"] for s in got] == ["9001", "9002", "9003"]

    def test_explicit_ids_bypass_scope(self, candidates):
        got = bp.select_strategies(_items(), "cn", "9002", "hint")
        assert [s["id"] for s in got] == ["9002"]

    def test_scope_all_overrides_hint(self, candidates):
        got = bp.select_strategies(_items(), "cn", "all", "all")
        assert len(got) == 3

    def test_missing_candidate_excluded(self, candidates):
        (candidates / "9003/candidate.py").unlink()
        got = bp.select_strategies(_items(), "cn", "all", "hint")
        assert [s["id"] for s in got] == ["9001"]


class TestResolveSymbols:
    def test_cn_uses_lab_pool(self, monkeypatch):
        monkeypatch.setattr(
            bp.lb,
            "resolve_pool",
            lambda pool, limit: [{"code": "600036.SH", "name": "招商银行"}],
        )
        got = bp.resolve_symbols("cn", "hs300", "backward", 0)
        assert got == [{"code": "600036.SH", "name": "招商银行"}]


class TestPlanJobs:
    def test_many_symbols_chunks_by_symbol(self):
        todo = [{"code": f"S{i}"} for i in range(20)]
        strategies = [{"id": "9001", "name": "x", "path": "p"}]
        jobs = bp._plan_jobs(todo, strategies, "cn", "backward", 60, 4, 180)
        assert len(jobs) == 3  # CHUNK=8 → 8/8/4
        assert all(len(j[0]) <= bp.CHUNK for j in jobs)
        assert all(j[1] is strategies for j in jobs)

    def test_few_symbols_chunks_by_strategy(self):
        todo = [{"code": "S0"}]
        strategies = [
            {"id": str(9000 + i), "name": "x", "path": "p"} for i in range(20)
        ]
        jobs = bp._plan_jobs(todo, strategies, "cn", "backward", 60, 4, 180)
        assert len(jobs) == 4
        assert all(len(j[0]) == 1 for j in jobs)
        assert sum(len(j[1]) for j in jobs) == 20


class TestRunChunk:
    def test_rows_and_errors_isolated(self, candidates, monkeypatch):
        def fake_bars(code, **kwargs):
            return [
                {
                    "date": "2024-01-02",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ] * 300

        def fake_run(path, bars, code, **kwargs):
            if code == "BAD":
                raise RuntimeError("boom")
            return {
                "stats": {
                    "Net profit": {"pct": 1.5, "value": 1500.0},
                    "Buy & hold return": {"pct": 0.5, "value": 500.0},
                    "Max equity drawdown": {"pct": 10.0},
                    "Sharpe ratio": {"value": 0.8},
                    "Sortino ratio": {"value": 1.1},
                    "Profit factor": {"value": 1.3},
                    "Percent profitable": {"pct": 55.0},
                    "Total trades": {"value": 12},
                    "Avg # bars in trades": {"value": 20},
                },
                "meta": {"start": "2024-01-02", "end": "2025-01-02"},
            }

        monkeypatch.setattr(bp.ml, "load_klines", fake_bars)
        monkeypatch.setattr(bp.ml, "run_script_on_bars", fake_run)
        strategies = [
            {
                "id": "9001",
                "name": "A股多头",
                "path": str(candidates / "9001/candidate.py"),
            },
        ]
        got = bp._run_chunk(
            (
                [
                    {"code": "600036.SH", "name": "招商银行"},
                    {"code": "BAD", "name": "坏"},
                ],
                strategies,
                "cn",
                "backward",
                60,
                180,
            )
        )
        assert len(got["rows"]) == 1
        assert got["rows"][0]["strategy"] == "9001"
        assert got["rows"][0]["net_pct"] == 1.5
        assert len(got["errors"]) == 1

    def test_strategy_timeout_is_isolated(self, candidates, monkeypatch):
        import time

        monkeypatch.setattr(bp.ml, "load_klines", lambda *a, **k: [{"date": "x"}] * 300)

        def slow(*args, **kwargs):
            time.sleep(5)
            return {}

        monkeypatch.setattr(bp.ml, "run_script_on_bars", slow)
        got = bp._run_chunk(
            (
                [{"code": "600036.SH", "name": "x"}],
                [
                    {
                        "id": "9001",
                        "name": "x",
                        "path": str(candidates / "9001/candidate.py"),
                    }
                ],
                "cn",
                "backward",
                60,
                1,
            )
        )
        assert got["rows"] == []
        assert "超时" in got["errors"][0]["error"]

    def test_short_history_skipped(self, candidates, monkeypatch):
        monkeypatch.setattr(bp.ml, "load_klines", lambda *a, **k: [{"date": "x"}] * 10)
        got = bp._run_chunk(
            (
                [{"code": "600036.SH", "name": "招商银行"}],
                [
                    {
                        "id": "9001",
                        "name": "x",
                        "path": str(candidates / "9001/candidate.py"),
                    }
                ],
                "cn",
                "backward",
                60,
                180,
            )
        )
        assert got["rows"] == []
        assert got["skipped"][0]["reason"].startswith("K线不足")


class TestSave:
    def test_ranking_names_mapped_and_market_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bp.lb, "batch_dir", lambda: tmp_path)
        strategies = [
            {"id": "9001", "name": "A股多头"},
            {"id": "9002", "name": "美股多空"},
        ]
        rows = [
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "strategy": "9001",
                "net_pct": 5.0,
                "buy_hold_pct": 1.0,
                "dd_pct": 10.0,
                "sharpe": 1.0,
                "sortino": 1.2,
                "profit_factor": 1.5,
                "win_rate_pct": 60.0,
                "trades": 10,
                "avg_bars": 20,
                "bars": 300,
                "start": "2024-01-01",
                "end": "2025-01-01",
            },
        ]
        path = bp._save(
            "r1",
            "hs300",
            "cn",
            "backward",
            strategies,
            rows,
            [],
            [],
            len(rows),
            1.2,
            tmp_path,
        )
        d = json.loads(path.read_text(encoding="utf-8"))
        assert d["market"] == "cn"
        assert d["ranking"][0]["name"] == "A股多头"
        assert d["strategies"] == strategies
