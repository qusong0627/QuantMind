-- upgrade_v1.0.17.sql：回测预算独立池（backtest）种子行。
-- 随 qlib_app/resource_budget.py（POOL="backtest"）及回测准入/对账模块补齐。
-- 与 backend/shared/db_init.sql Q 节同 DDL，幂等可重放（main_oss 启动自动扫描）。
-- 说明：回测/优化 HTTP+Celery 共用独立池，不与 alpha_mining 混用名额；
-- ON CONFLICT 仅覆盖 ready（保留 reconciler 维护的 epoch）。
INSERT INTO resource_lease_pools (pool, epoch, ready)
VALUES ('backtest', 'oss-bootstrap', TRUE)
ON CONFLICT (pool) DO UPDATE SET ready = TRUE, updated_at = NOW();
