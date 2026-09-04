-- ============================================================
-- QuantMind Database Upgrade Script v1.0.3
-- news_article_enrichment 新增 title 列
-- ============================================================

-- 修复 news enrich 流程：enricher.py 的 upsert 引用 title 列，但表/Schema 缺少该列，
-- 导致线上 news_enrich_recent 任务首条写入即报
--   column "title" of relation "news_article_enrichment" does not exist
-- 且同一连接后续语句全部进入 aborted 状态（current transaction is aborted）。
--
-- 此升级为存量环境补齐该列；新建环境由 backend/shared/db_init.sql 建表时直接包含。
-- 幂等（IF NOT EXISTS），可重复执行。

ALTER TABLE news_article_enrichment
    ADD COLUMN IF NOT EXISTS title TEXT;