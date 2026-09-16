"""兼容层：统一导出 Trade 主实现的 SimulationAccountManager。"""

from backend.services.trade_shared.simulation_manager import (
    SimulationAccountManager,
    canonical_sim_uid,
    require_sim_user_id,
)

__all__ = ["SimulationAccountManager", "canonical_sim_uid", "require_sim_user_id"]
