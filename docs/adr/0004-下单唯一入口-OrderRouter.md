# ADR-0004 下单唯一入口 OrderRouter（五路径）

- 状态：已采纳（T-P2-01，commit 23a581c6）
- 背景：托管/手动/沙箱/镜像/实时事件五条下单路径曾各写各的校验与落账（拒因丢失、
  撤销单被 worker 重放等数个真缺陷）。
- 决定：全部下单经 `simulation/services/order_router.py`（组合式：即时链委托
  SubmissionService，补 from_bar 托管/镜像收口/strict 分级）；source 域含
  sandbox/tdx_rolling/hosted/forced_liquidation；strict_market 参数贯穿。
- 后果：新下单场景=新 source 或新 ctx，禁止绕过 Router 直调底层链；风控接线（T-RC-02）
  也挂在此单点。
