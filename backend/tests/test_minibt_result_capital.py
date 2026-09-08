# -*- coding: utf-8 -*-
"""minibt 初始资金注入单测（backend/shared/minibt_result.py）。

minibt 的账户在 ``bt.run()`` 里按 ``strategy.config.value`` 创建（默认 100 万），
回测中心的"初始资金"字段必须经此注入才生效。
"""
import os
import sys

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.shared.minibt_result import _apply_initial_capital  # noqa: E402


class _Config:
    def __init__(self, value=1_000_000.0):
        self.value = value


class _Strategy:
    def __init__(self, value=1_000_000.0):
        self.config = _Config(value)


class _Bt:
    def __init__(self, *strategies):
        self.strategies = list(strategies)


class TestApplyInitialCapital:
    def test_sets_explicit_capital(self, monkeypatch):
        monkeypatch.delenv("QM_MINIBT_INITIAL_CAPITAL", raising=False)
        bt = _Bt(_Strategy())

        applied = _apply_initial_capital(bt, 500_000.0)

        assert applied == pytest.approx(500_000.0)
        assert bt.strategies[0].config.value == pytest.approx(500_000.0)

    def test_reads_env_when_arg_missing(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INITIAL_CAPITAL", "250000")
        bt = _Bt(_Strategy())

        applied = _apply_initial_capital(bt, None)

        assert applied == pytest.approx(250_000.0)
        assert bt.strategies[0].config.value == pytest.approx(250_000.0)

    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INITIAL_CAPITAL", "250000")
        bt = _Bt(_Strategy())

        applied = _apply_initial_capital(bt, 800_000.0)

        assert applied == pytest.approx(800_000.0)
        assert bt.strategies[0].config.value == pytest.approx(800_000.0)

    def test_no_capital_keeps_minibt_default(self, monkeypatch):
        monkeypatch.delenv("QM_MINIBT_INITIAL_CAPITAL", raising=False)
        bt = _Bt(_Strategy())

        applied = _apply_initial_capital(bt, None)

        assert applied is None
        assert bt.strategies[0].config.value == pytest.approx(1_000_000.0)

    def test_ignores_invalid_capital(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INITIAL_CAPITAL", "not-a-number")
        bt = _Bt(_Strategy())

        applied = _apply_initial_capital(bt, -5.0)

        assert applied is None
        assert bt.strategies[0].config.value == pytest.approx(1_000_000.0)
