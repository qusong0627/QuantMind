"""物化门禁打包：导入即注册。

纪律（用户裁决 + 历史教训）：
- 五个内置门禁**默认全 soft**（失败只记录不拦）；既有 |ρ|≥0.9 值级查重是
  **另一个机制**（``rd_mined_materialize._max_abs_corr``），保持硬拒原语义。
- 指标缺失 = skipped，不判不拦（缺失按 0 判会不可逆地误杀存量因子）。
- ICIR 族阈值默认不设硬拦（memory: model-ic-is-size-regime-bet——ICIR 分母
  是族常数，硬拦会批量拒绝）。
"""

from . import builtin  # noqa: F401 — 导入触发内置门禁注册
