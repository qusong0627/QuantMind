-- ============================================================
-- QuantMind Database Upgrade Script v1.0.7
-- 模拟成交瞬时列统一为 TIMESTAMPTZ；管理员 user_id 纠正拆成独立语句
-- ============================================================
--
-- 背景：
-- 1. sim_trades.executed_at 在旧升级脚本里是 timestamptz，新 ORM 一度按
--    TIMESTAMP WITHOUT TIME ZONE 写 naive UTC。asyncpg 按真实列类型编码，
--    旧库（timestamptz + naive）和新库（timestamp + aware）会交替报
--    "can't subtract offset-naive and offset-aware datetimes"，成交整笔回滚。
--    本脚本把瞬时列统一成 timestamptz，与 UtcDateTime + aware UTC 写入对齐。
-- 2. v1.0.6 把 users.user_id 纠正和加 FK 放进同一个 DO 块，后面失败会整段
--    回滚，线上仍可能停在 user_id='admin'。此处拆成独立语句，可单独提交。
--
-- 幂等，可重复执行。禁止百分号字符（psycopg2 fallback 会误解析）。

-- 1. 卸指向 users(user_id) 的 FK（与 db_init / v1.0.6 同名）
ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;
ALTER TABLE identity_verifications DROP CONSTRAINT IF EXISTS identity_verifications_user_id_fkey;
ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_user_id_fkey;
ALTER TABLE password_reset_tokens DROP CONSTRAINT IF EXISTS password_reset_tokens_user_id_fkey;

-- 2. 字符型 user_id 列：admin -> 00000001（users 表单独下一步）
DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'user_id'
          AND data_type IN ('character varying', 'character', 'text')
          AND table_name <> 'users'
        ORDER BY table_name
    LOOP
        BEGIN
            EXECUTE 'UPDATE ' || quote_ident(t)
                || ' SET user_id = ' || quote_literal('00000001')
                || ' WHERE user_id = ' || quote_literal('admin');
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'upgrade_v1.0.7 sweep skipped';
        END;
    END LOOP;
END $$;

-- 3. users 主行（独立语句，不被后续 FK 失败回滚）
UPDATE users SET user_id = '00000001' WHERE user_id = 'admin';

-- 4. 原名建回 FK
DO $$
BEGIN
    BEGIN
        ALTER TABLE user_roles
            ADD CONSTRAINT user_roles_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.7 add user_roles FK skipped';
    END;
    BEGIN
        ALTER TABLE identity_verifications
            ADD CONSTRAINT identity_verifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.7 add identity_verifications FK skipped';
    END;
    BEGIN
        ALTER TABLE notifications
            ADD CONSTRAINT notifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.7 add notifications FK skipped';
    END;
    BEGIN
        ALTER TABLE password_reset_tokens
            ADD CONSTRAINT password_reset_tokens_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.7 add password_reset_tokens FK skipped';
    END;
END $$;

-- 5. 模拟盘瞬时列 -> timestamptz（存量 naive 值按 UTC 解释，禁止当上海墙钟）
DO $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.table_name IN ('sim_trades', 'sim_orders')
          AND c.column_name IN (
              'executed_at', 'submitted_at', 'filled_at', 'cancelled_at',
              'created_at', 'updated_at'
          )
          AND c.data_type = 'timestamp without time zone'
    LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(rec.table_name)
            || ' ALTER COLUMN ' || quote_ident(rec.column_name)
            || ' TYPE timestamptz USING '
            || quote_ident(rec.column_name)
            || ' AT TIME ZONE ' || quote_literal('UTC');
        RAISE NOTICE 'upgrade_v1.0.7 converted timestamp column to timestamptz';
    END LOOP;
END $$;
