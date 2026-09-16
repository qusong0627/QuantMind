-- ============================================================
-- QuantMind Database Upgrade Script v1.0.8
-- 管理员 user_id 收口为 10000001（避免 int('00000001')=1）
-- ============================================================
--
-- 背景：
-- 1. 规范 ID 曾是 00000001，Python int() 会丢掉前导零变成 1。
--    JWT sub=00000001 的模拟账户键写成 :1，admin 时代写在 :0，
--    仪表盘读 :1/:00000001 全空，显示总资产 0。
-- 2. 新规范 ID 10000001：8 位且不以 0 开头，int 后仍是 10000001。
--
-- 本脚本只改字符型 user_id（含 users）以及模拟盘整数 user_id
-- （sim_orders / sim_trades）。strategies.user_id 存的是 users.id
-- 主键，不要改。
--
-- 幂等，可重复执行。禁止百分号字符（psycopg2 fallback 会误解析）。

-- 1. 卸指向 users(user_id) 的 FK
ALTER TABLE user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;
ALTER TABLE identity_verifications DROP CONSTRAINT IF EXISTS identity_verifications_user_id_fkey;
ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_user_id_fkey;
ALTER TABLE password_reset_tokens DROP CONSTRAINT IF EXISTS password_reset_tokens_user_id_fkey;

-- 2. 字符型 user_id 列：admin / 00000001 -> 10000001（users 表单独下一步）
DO $$
DECLARE
    t TEXT;
    old_id TEXT;
BEGIN
    FOREACH old_id IN ARRAY ARRAY['admin', '00000001']
    LOOP
        FOR t IN
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name = 'user_id'
              AND data_type IN ('character varying', 'character', 'text')
              AND table_name <> 'users'
            ORDER BY table_name
        LOOP
            BEGIN
                EXECUTE 'UPDATE ' || quote_ident(t)
                    || ' SET user_id = ' || quote_literal('10000001')
                    || ' WHERE user_id = ' || quote_literal(old_id);
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'upgrade_v1.0.8 sweep skipped';
            END;
        END LOOP;
    END LOOP;
END $$;

-- 3. users 主行（独立语句，不被后续 FK 失败回滚）
UPDATE users SET user_id = '10000001'
 WHERE user_id IN ('admin', '00000001')
   AND NOT EXISTS (SELECT 1 FROM users WHERE user_id = '10000001');

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
        RAISE NOTICE 'upgrade_v1.0.8 add user_roles FK skipped';
    END;
    BEGIN
        ALTER TABLE identity_verifications
            ADD CONSTRAINT identity_verifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.8 add identity_verifications FK skipped';
    END;
    BEGIN
        ALTER TABLE notifications
            ADD CONSTRAINT notifications_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.8 add notifications FK skipped';
    END;
    BEGIN
        ALTER TABLE password_reset_tokens
            ADD CONSTRAINT password_reset_tokens_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(user_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN OTHERS THEN
        RAISE NOTICE 'upgrade_v1.0.8 add password_reset_tokens FK skipped';
    END;
END $$;

-- 5. 模拟资金快照：历史 0 / 1 / 00000001 / admin -> 10000001
--    同一天已有规范行则跳过，避免 UNIQUE (tenant_id, user_id, snapshot_date)
DO $$
BEGIN
    IF to_regclass('public.simulation_fund_snapshots') IS NULL THEN
        RETURN;
    END IF;
    UPDATE simulation_fund_snapshots s
       SET user_id = '10000001'
     WHERE s.user_id IN ('0', '1', '00000001', 'admin')
       AND NOT EXISTS (
           SELECT 1 FROM simulation_fund_snapshots x
            WHERE x.tenant_id = s.tenant_id
              AND x.user_id = '10000001'
              AND x.snapshot_date = s.snapshot_date
       );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'upgrade_v1.0.8 snapshots skipped';
END $$;

-- 6. 模拟委托/成交整数 user_id：0 / 1 -> 10000001
DO $$
BEGIN
    IF to_regclass('public.sim_orders') IS NOT NULL THEN
        UPDATE sim_orders SET user_id = 10000001 WHERE user_id IN (0, 1);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'upgrade_v1.0.8 sim_orders skipped';
END $$;

DO $$
BEGIN
    IF to_regclass('public.sim_trades') IS NOT NULL THEN
        UPDATE sim_trades SET user_id = 10000001 WHERE user_id IN (0, 1);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'upgrade_v1.0.8 sim_trades skipped';
END $$;
