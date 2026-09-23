-- ============================================================
-- QuantMind Database Upgrade Script v1.1.2
-- 对外数据面（/api/ext/v1/data/*）行级游标的索引支撑
-- ============================================================
--
-- 背景：对外数据面用 `(游标列, 兜底键…) > (…, …) ORDER BY 游标列, 兜底键 LIMIT n`
-- 做增量翻页。四条查询在 2026-09-23 实测**都没有可用索引**，计划是
-- `Seq Scan + Sort`——`news_article_enrichment`（689,497 行）单页 76–151 ms，
-- 一次全量镜像 1379 页 ≈ 3 分钟数据库时间，且每页都要重扫全表；
-- `/data/datasets` 里那句 `MAX(enriched_at)` 也是 56 ms 的全表扫描。
--
-- 这几个索引不是「顺手优化」，它们**就是那条查询的访问路径**：列顺序与
-- `ORDER BY` 完全一致，建完后是索引区间扫描（不再排序），MAX 变成索引末端取一。
--
-- 多租户三张表的列顺序是 `(tenant_id, user_id, 游标列, 兜底键)`：
-- 前两列是等值谓词，把它们放前缀后，剩余部分在该租户内已经有序，
-- 于是翻页天然按游标序返回，不需要排序。若把游标列放前缀，
-- 单个租户翻页要沿索引走并丢弃别人的行。
--
-- 幂等（IF NOT EXISTS），可重复执行；不含任何破坏性语句。
-- ============================================================

-- 新闻富化：689k 行且持续增长，唯一真正需要索引的一张
CREATE INDEX IF NOT EXISTS idx_news_enrichment_cursor
    ON news_article_enrichment (enriched_at, huntly_page_id);

-- 推理运行记录（当前 1,337 行；索引随增长始终对齐查询形状）
CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_cursor
    ON qm_model_inference_runs (tenant_id, user_id, updated_at, run_id);

-- 推理批次（当前 14 行）
CREATE INDEX IF NOT EXISTS idx_qm_model_inference_batches_cursor
    ON qm_model_inference_batches (tenant_id, user_id, updated_at, batch_id);

-- 特征快照运行（当前 2,943 行）
CREATE INDEX IF NOT EXISTS idx_engine_feature_runs_cursor
    ON engine_feature_runs (updated_at, run_id);
