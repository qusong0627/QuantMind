# ADR-0003 瞬时时间统一 TIMESTAMPTZ + aware UTC

- 状态：已采纳（事故驱动，2026-09）
- 背景：`sim_trades.executed_at` 等瞬时列曾出现 naive-UTC 与上海墙钟混用；timestamptz 列
  与 naive 参数比较按**会话时区**解释 → 影子对照 16:00 后窗口假阴性（commit 9fe742b2）。
- 决定：瞬时列一律 TIMESTAMPTZ；写入走 `shared/utc_datetime.utc_now()`/`UtcDateTime`；
  JSON 输出带 `Z`；任何比较 aware 对 aware。存量库由 `data/upgrade_v1.0.7.sql` 对齐。
- 后果：跨时区语义唯一；新表/新列必须遵循，评审红线。
