"""训练配置契约（方案 B 单源，双端复用）。

api 容器（``_build_config_yaml``）与训练容器（``train.py main()``）import 同一个包，
config.yaml 的 key 天然一致。B1 只做 ``TrainingConfig``（config.yaml 层）类型化；
入参编排层（``pause_others/horizons/...``）保持现状，见 REFACTOR_TRAINING_B §3.3。
"""

from backend.shared.training.request import ContextRequest, TrainingRequest
from backend.shared.training.schemas import TrainingConfig, dump_contract_dict

__all__ = ["ContextRequest", "TrainingConfig", "TrainingRequest", "dump_contract_dict"]
