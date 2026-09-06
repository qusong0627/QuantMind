"""训练诊断包（P1 由 train.py 拆出）。

- drift: 数据漂移双通道检测（水平 PSI + 截面 rank 位移）
- explain: SHAP 解释配置与汇总
- utils: 硬件探测、JSON 清洗等小工具

与 model_trainers 同级，训练容器经挂载/COPY/rsync 三通道同步（见编排器）。
"""
from diagnostics.drift import compute_psi_drift
from diagnostics.explain import _compute_shap_summary, _normalize_explain_cfg
from diagnostics.utils import _sanitize_nan_inf, detect_hardware

__all__ = [
    "compute_psi_drift",
    "_compute_shap_summary",
    "_normalize_explain_cfg",
    "_sanitize_nan_inf",
    "detect_hardware",
]
