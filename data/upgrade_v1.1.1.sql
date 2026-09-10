-- ============================================================
-- QuantMind Database Upgrade Script v1.1.1
-- 修复 tradeaction / positionside 枚举口径漂移
-- ============================================================
-- 背景：db_init.sql 早期建库用的是 names/小写口径：
--     tradeaction = buy_to_open, sell_to_close, sell_to_open, buy_to_close
--     positionside = long, short
-- 而模型层（trade_shared/models/enums.py + values_callable）与全部写库代码
-- （含 tdx_push_service 的裸 INSERT）用 values 口径：
--     tradeaction = OPEN, CLOSE, OPEN_REVERSE, CLOSE_REVERSE
--     positionside = LONG, SHORT
-- 老库上任何写这两列的 INSERT 都报
--   invalid input value for enum tradeaction: "OPEN"
-- → 内部策略真单 / 模拟盘镜像真单一律 500（docker 库当年被手工 ALTER 过才没暴露）。
--
-- 做法：把老库的枚举**就地转成 values 口径**（新建同义类型 → 逐列 USING 转换 →
-- 换名），存量行的 names 值一并改写为 values 值——只加标签不改正旧行会让
-- SQLAlchemy 读旧行抛 LookupError（委托列表整页 500）。
--
-- 幂等：仅当检测到老标签时才动手；新库 / 已升级库直接跳过。
-- 原子：全部包在一个 DO 块里（单语句 = 单事务），失败整块回滚，不会留半成品。
-- 安全：转换前校验列内无未知标签值，有则抛错中断、留给人工核查，绝不静默写 NULL；
--       列默认值（如 default 'long'）在转换期间摘除、按同一映射改写后装回
--       （枚举间无隐式转换，带默认值的列直接 ALTER TYPE 会报
--        "default for column cannot be cast automatically"）。
-- ============================================================

DO $upgrade_v111$
DECLARE
    col_rec record;
    bad_rows integer;
    def_expr text;
    def_expr_new text;
    def_restores text[] := '{}';
    stmt text;
    converted integer := 0;
BEGIN
    -- ---------------- tradeaction: names -> values ----------------
    IF EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'tradeaction' AND e.enumlabel = 'buy_to_open'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = '_tradeaction_v111') THEN
            RAISE EXCEPTION '残留临时类型 _tradeaction_v111，请人工确认后再升级';
        END IF;
        CREATE TYPE _tradeaction_v111 AS ENUM ('OPEN', 'CLOSE', 'OPEN_REVERSE', 'CLOSE_REVERSE');
        FOR col_rec IN
            SELECT n.nspname AS sch, c.relname AS tbl, c.oid AS relid, a.attname AS col
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE t.typname = 'tradeaction' AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relkind = 'r'
        LOOP
            EXECUTE format(
                'SELECT count(*) FROM %I.%I WHERE %I::text NOT IN '
                '(''buy_to_open'', ''sell_to_close'', ''sell_to_open'', ''buy_to_close'')',
                col_rec.sch, col_rec.tbl, col_rec.col
            ) INTO bad_rows;
            IF bad_rows > 0 THEN
                RAISE EXCEPTION '表 %.% 列 % 存在 % 行未知 tradeaction 标签，需人工核查',
                    col_rec.sch, col_rec.tbl, col_rec.col, bad_rows;
            END IF;
            SELECT pg_get_expr(d.adbin, d.adrelid) INTO def_expr
            FROM pg_attrdef d
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE d.adrelid = col_rec.relid AND a.attname = col_rec.col;
            IF def_expr IS NOT NULL THEN
                EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I DROP DEFAULT',
                               col_rec.sch, col_rec.tbl, col_rec.col);
            END IF;
            EXECUTE format($fmt$
                ALTER TABLE %I.%I ALTER COLUMN %I TYPE _tradeaction_v111
                USING (CASE %I::text
                    WHEN 'buy_to_open'   THEN 'OPEN'
                    WHEN 'sell_to_close' THEN 'CLOSE'
                    WHEN 'sell_to_open'  THEN 'OPEN_REVERSE'
                    WHEN 'buy_to_close'  THEN 'CLOSE_REVERSE'
                END)::_tradeaction_v111
            $fmt$, col_rec.sch, col_rec.tbl, col_rec.col, col_rec.col);
            IF def_expr IS NOT NULL THEN
                def_expr_new := replace(replace(replace(replace(def_expr,
                    '''buy_to_open''', '''OPEN'''),
                    '''sell_to_close''', '''CLOSE'''),
                    '''sell_to_open''', '''OPEN_REVERSE'''),
                    '''buy_to_close''', '''CLOSE_REVERSE''');
                -- 默认值必须等类型改名之后再装回：循环期间 tradeaction 还是老类型，
                -- 此刻 SET DEFAULT 'OPEN'::tradeaction 会报 invalid input value。
                def_restores := def_restores || format(
                    'ALTER TABLE %I.%I ALTER COLUMN %I SET DEFAULT %s',
                    col_rec.sch, col_rec.tbl, col_rec.col, def_expr_new);
                def_expr := NULL;
            END IF;
            converted := converted + 1;
        END LOOP;
        DROP TYPE tradeaction;
        ALTER TYPE _tradeaction_v111 RENAME TO tradeaction;
        FOREACH stmt IN ARRAY def_restores LOOP
            EXECUTE stmt;
        END LOOP;
        def_restores := '{}';
        RAISE NOTICE 'tradeaction 已升级为 values 口径（% 列）', converted;
    END IF;

    -- ---------------- positionside: long/short -> LONG/SHORT ----------------
    converted := 0;
    IF EXISTS (
        SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'positionside' AND e.enumlabel = 'long'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = '_positionside_v111') THEN
            RAISE EXCEPTION '残留临时类型 _positionside_v111，请人工确认后再升级';
        END IF;
        CREATE TYPE _positionside_v111 AS ENUM ('LONG', 'SHORT');
        FOR col_rec IN
            SELECT n.nspname AS sch, c.relname AS tbl, c.oid AS relid, a.attname AS col
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE t.typname = 'positionside' AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relkind = 'r'
        LOOP
            EXECUTE format(
                'SELECT count(*) FROM %I.%I WHERE %I::text NOT IN (''long'', ''short'')',
                col_rec.sch, col_rec.tbl, col_rec.col
            ) INTO bad_rows;
            IF bad_rows > 0 THEN
                RAISE EXCEPTION '表 %.% 列 % 存在 % 行未知 positionside 标签，需人工核查',
                    col_rec.sch, col_rec.tbl, col_rec.col, bad_rows;
            END IF;
            SELECT pg_get_expr(d.adbin, d.adrelid) INTO def_expr
            FROM pg_attrdef d
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE d.adrelid = col_rec.relid AND a.attname = col_rec.col;
            IF def_expr IS NOT NULL THEN
                EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I DROP DEFAULT',
                               col_rec.sch, col_rec.tbl, col_rec.col);
            END IF;
            EXECUTE format($fmt$
                ALTER TABLE %I.%I ALTER COLUMN %I TYPE _positionside_v111
                USING (CASE %I::text WHEN 'long' THEN 'LONG' WHEN 'short' THEN 'SHORT'
                END)::_positionside_v111
            $fmt$, col_rec.sch, col_rec.tbl, col_rec.col, col_rec.col);
            IF def_expr IS NOT NULL THEN
                def_expr_new := replace(replace(def_expr, '''long''', '''LONG'''),
                                        '''short''', '''SHORT''');
                def_restores := def_restores || format(
                    'ALTER TABLE %I.%I ALTER COLUMN %I SET DEFAULT %s',
                    col_rec.sch, col_rec.tbl, col_rec.col, def_expr_new);
                def_expr := NULL;
            END IF;
            converted := converted + 1;
        END LOOP;
        DROP TYPE positionside;
        ALTER TYPE _positionside_v111 RENAME TO positionside;
        FOREACH stmt IN ARRAY def_restores LOOP
            EXECUTE stmt;
        END LOOP;
        def_restores := '{}';
        RAISE NOTICE 'positionside 已升级为 LONG/SHORT 口径（% 列）', converted;
    END IF;
END
$upgrade_v111$;
