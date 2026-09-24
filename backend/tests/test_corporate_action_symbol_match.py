"""公司行为代码匹配回归（纯函数，无 DB 依赖）。

历史 bug：台账 lots 用后缀式（603810.SH），公司行为表用前缀式（SH603810），
_apply_action 只按前缀查 lots → 永不匹配 → 分红/送股静默不入账。
"""

from __future__ import annotations

from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)


def test_lot_symbol_candidates_from_prefix_action():
    assert SimulationCorporateActionService._lot_symbol_candidates("SH603810") == {
        "603810.SH",
        "SH603810",
    }


def test_lot_symbol_candidates_from_suffix_lot():
    assert SimulationCorporateActionService._lot_symbol_candidates("603810.SH") == {
        "603810.SH",
        "SH603810",
    }


def test_lot_symbol_candidates_covers_sz_bj():
    assert "000001.SZ" in SimulationCorporateActionService._lot_symbol_candidates(
        "SZ000001"
    )
    assert "830001.BJ" in SimulationCorporateActionService._lot_symbol_candidates(
        "BJ830001"
    )
