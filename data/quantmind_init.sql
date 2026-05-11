--
-- PostgreSQL database dump
--

\restrict A6yow9GXpjaPdE4nqy3fX6gPKaCKxmHsAfrMoxT5lcuFT3BvFYav1gflstrULrw

-- Dumped from database version 15.17
-- Dumped by pg_dump version 15.17

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: orderside; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.orderside AS ENUM (
    'buy',
    'sell'
);


ALTER TYPE public.orderside OWNER TO quantmind;

--
-- Name: orderstatus; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.orderstatus AS ENUM (
    'pending',
    'submitted',
    'partially_filled',
    'filled',
    'cancelled',
    'rejected',
    'expired'
);


ALTER TYPE public.orderstatus OWNER TO quantmind;

--
-- Name: ordertype; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.ordertype AS ENUM (
    'market',
    'limit',
    'stop',
    'stop_limit'
);


ALTER TYPE public.ordertype OWNER TO quantmind;

--
-- Name: positionside; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.positionside AS ENUM (
    'long',
    'short'
);


ALTER TYPE public.positionside OWNER TO quantmind;

--
-- Name: simulationstatus; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.simulationstatus AS ENUM (
    'RUNNING',
    'PAUSED',
    'STOPPED',
    'ERROR'
);


ALTER TYPE public.simulationstatus OWNER TO quantmind;

--
-- Name: strategystatus; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.strategystatus AS ENUM (
    'DRAFT',
    'REPOSITORY',
    'LIVE_TRADING',
    'ACTIVE',
    'PAUSED',
    'ARCHIVED'
);


ALTER TYPE public.strategystatus OWNER TO quantmind;

--
-- Name: strategytype; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.strategytype AS ENUM (
    'CUSTOM',
    'TECHNICAL',
    'FUNDAMENTAL',
    'QUANTITATIVE',
    'MIXED'
);


ALTER TYPE public.strategytype OWNER TO quantmind;

--
-- Name: tradeaction; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.tradeaction AS ENUM (
    'buy',
    'sell'
);


ALTER TYPE public.tradeaction OWNER TO quantmind;

--
-- Name: tradingmode; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.tradingmode AS ENUM (
    'BACKTEST',
    'SIMULATION',
    'LIVE',
    'REAL'
);


ALTER TYPE public.tradingmode OWNER TO quantmind;

--
-- Name: auto_populate_id(); Type: FUNCTION; Schema: public; Owner: quantmind
--

CREATE FUNCTION public.auto_populate_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.id IS NULL THEN
    NEW.id := NEW.backtest_id;
  END IF;
  RETURN NEW;
END;
$$;


ALTER FUNCTION public.auto_populate_id() OWNER TO quantmind;

--
-- Name: cleanup_old_qmt_data(); Type: FUNCTION; Schema: public; Owner: quantmind
--

CREATE FUNCTION public.cleanup_old_qmt_data() RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    DELETE FROM qmt_account_assets WHERE timestamp < NOW() - INTERVAL '7 days';
    DELETE FROM qmt_positions WHERE timestamp < NOW() - INTERVAL '7 days';
    DELETE FROM qmt_orders WHERE timestamp < NOW() - INTERVAL '7 days';
    DELETE FROM qmt_trades WHERE timestamp < NOW() - INTERVAL '7 days';
    DELETE FROM qmt_sync_logs WHERE timestamp < NOW() - INTERVAL '30 days';
END;
$$;


ALTER FUNCTION public.cleanup_old_qmt_data() OWNER TO quantmind;

--
-- Name: maintain_stock_daily_window(); Type: FUNCTION; Schema: public; Owner: quantmind
--

CREATE FUNCTION public.maintain_stock_daily_window() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- 删除该股票超过 30 天的旧数据
    -- 性能优化：仅当插入数据日期较新时触发删除，且不频繁全表扫描
    -- 这里采用简单策略：每次插入后，删除该股票 30 天前的数据
    DELETE FROM public.stock_daily 
    WHERE code = NEW.code AND trade_date < (CURRENT_DATE - INTERVAL '30 days');
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.maintain_stock_daily_window() OWNER TO quantmind;

--
-- Name: qm_import_research_candidate_snapshot(date, text, text, boolean); Type: FUNCTION; Schema: public; Owner: quantmind
--

CREATE FUNCTION public.qm_import_research_candidate_snapshot(p_prediction_trade_date date DEFAULT NULL::date, p_tenant_id text DEFAULT NULL::text, p_user_id text DEFAULT NULL::text, p_force_rebuild boolean DEFAULT false) RETURNS TABLE(imported_rows bigint, imported_prediction_trade_date date, source_mode text, max_source_updated_at timestamp with time zone)
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_last_source_updated_at TIMESTAMPTZ;
  v_has_stock_screener_snapshot BOOLEAN := to_regclass('public.stock_screener_snapshot') IS NOT NULL;
  v_has_stock_selection BOOLEAN := to_regclass('public.stock_selection') IS NOT NULL;
  v_imported_rows BIGINT := 0;
  v_imported_prediction_trade_date DATE := p_prediction_trade_date;
  v_max_source_updated_at TIMESTAMPTZ;
  v_last_run_id TEXT;
  v_source_mode TEXT;
BEGIN
  SELECT last_source_updated_at
    INTO v_last_source_updated_at
    FROM qm_research_import_state
   WHERE job_name = 'research_candidate_snapshot';

  IF p_prediction_trade_date IS NULL THEN
    SELECT MAX(prediction_trade_date)
      INTO v_imported_prediction_trade_date
      FROM qm_model_inference_runs
     WHERE status = 'completed'
       AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id)
       AND (p_user_id IS NULL OR user_id = p_user_id)
       AND (
         p_force_rebuild
         OR updated_at > COALESCE(v_last_source_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')
       );
  END IF;

  v_source_mode := CASE
    WHEN v_has_stock_screener_snapshot THEN 'stock_screener_snapshot'
    WHEN v_has_stock_selection THEN 'stock_selection'
    ELSE 'stock_daily_latest'
  END;

  IF v_imported_prediction_trade_date IS NULL THEN
    RETURN QUERY SELECT 0::BIGINT, NULL::DATE, v_source_mode, NULL::TIMESTAMPTZ;
    RETURN;
  END IF;

  IF p_force_rebuild THEN
    DELETE FROM qm_research_candidate_snapshot
     WHERE prediction_trade_date = v_imported_prediction_trade_date
       AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id)
       AND (p_user_id IS NULL OR user_id = p_user_id);
  END IF;

  IF v_has_stock_screener_snapshot THEN
    WITH candidate_runs AS (
      SELECT
        r.run_id,
        r.tenant_id,
        r.user_id,
        r.model_id,
        r.data_trade_date,
        r.prediction_trade_date,
        r.updated_at AS run_updated_at
      FROM qm_model_inference_runs r
      WHERE r.status = 'completed'
        AND r.prediction_trade_date = v_imported_prediction_trade_date
        AND (p_tenant_id IS NULL OR r.tenant_id = p_tenant_id)
        AND (p_user_id IS NULL OR r.user_id = p_user_id)
        AND (
          p_force_rebuild
          OR r.updated_at > COALESCE(v_last_source_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')
        )
    ),
    signal_rows AS (
      SELECT
        ranked.run_id,
        ranked.tenant_id,
        ranked.user_id,
        ranked.trade_date,
        ranked.symbol_norm,
        ranked.light_score,
        ranked.tft_score,
        ranked.fusion_score,
        ranked.score_rank,
        ranked.universe_tag,
        ranked.signal_side,
        ranked.expected_price,
        ranked.quality,
        ranked.created_at
      FROM (
        SELECT
          s.run_id,
          s.tenant_id,
          s.user_id,
          s.trade_date,
          LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0') AS symbol_norm,
          s.light_score,
          s.tft_score,
          s.fusion_score,
          s.score_rank,
          s.universe_tag,
          s.signal_side,
          s.expected_price,
          COALESCE(s.quality, '{}'::jsonb) AS quality,
          s.created_at,
          ROW_NUMBER() OVER (
            PARTITION BY s.run_id, s.tenant_id, s.user_id, s.trade_date, LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0')
            ORDER BY s.created_at DESC NULLS LAST, s.fusion_score DESC NULLS LAST
          ) AS row_num
        FROM engine_signal_scores s
        JOIN candidate_runs r
          ON r.run_id = s.run_id
         AND r.tenant_id = s.tenant_id
         AND r.user_id = s.user_id
         AND r.prediction_trade_date = s.trade_date
      ) ranked
      WHERE ranked.row_num = 1
    ),
    source_rows AS (
      SELECT
        snapshot_date AS trade_date,
        LPAD(REGEXP_REPLACE(symbol, '[^0-9]', '', 'g'), 6, '0') AS symbol_norm,
        short_name AS stock_name,
        industry,
        '[]'::jsonb AS concept_tags,
        close_price,
        change_ratio AS latest_change_pct,
        turnover_rate1 AS turnover_rate,
        amount,
        market_value AS total_mv,
        markettype AS market_type,
        province,
        city,
        NULL::INTEGER AS listed_days,
        COALESCE(continued_rise_days, 0) AS consecutive_limit_up_days,
        LEAST(COALESCE(continued_rise_days, 0), 5) AS recent_limit_up_count_5d,
        COALESCE(vol_3d_up, FALSE) AS volume_trend_3d,
        COALESCE(vol_5d_up, FALSE) AS volume_trend_5d,
        vol_3d_trend AS volume_trend_3d_score,
        vol_3d_sum / 3.0 AS volume_ma_3,
        vol_5d_avg AS volume_ma_5,
        ma5,
        ma10,
        ma20,
        return_5d,
        return_10d,
        close_above_ma5,
        close_above_ma10,
        continued_rise_days,
        continued_fall_days,
        amount_rank,
        amount_3d_sum / 3.0 AS amount_ma_3,
        amount_5d_sum / 5.0 AS amount_ma_5,
        high_5d,
        low_5d,
        life_high_week,
        life_high_month,
        life_high_3month,
        life_high_6month,
        life_high_one_year,
        FALSE AS is_st,
        FALSE AS is_suspended,
        (change_ratio >= 9.80) AS is_limit_up,
        (change_ratio <= -9.80) AS is_limit_down,
        FALSE AS is_hs300,
        FALSE AS is_csi1000
      FROM stock_screener_snapshot
      WHERE snapshot_date = v_imported_prediction_trade_date
    ),
    upserted AS (
      INSERT INTO qm_research_candidate_snapshot (
        tenant_id, user_id, run_id, model_id, data_trade_date, prediction_trade_date, market_snapshot_trade_date,
        symbol, stock_name, industry, concept_tags, close_price, latest_change_pct, turnover_rate, amount, total_mv,
        market_type, province, city, fusion_score, light_score, tft_score, score_rank, signal_side, expected_price,
        universe_tag, quality, confidence_level, confidence_score, listed_days, consecutive_limit_up_days,
        recent_limit_up_count_5d, volume_trend_3d, volume_trend_5d, volume_trend_3d_score, volume_ma_3, volume_ma_5,
        ma5, ma10, ma20, return_5d, return_10d, close_above_ma5, close_above_ma10, continued_rise_days,
        continued_fall_days, amount_rank, amount_ma_3, amount_ma_5, high_5d, low_5d, life_high_week,
        life_high_month, life_high_3month, life_high_6month, life_high_one_year, is_st, is_suspended,
        is_limit_up, is_limit_down, tradable_flag, is_hs300, is_csi1000, hit_reasons, risk_flags,
        thesis_summary, source_updated_at, updated_at
      )
      SELECT
        r.tenant_id,
        r.user_id,
        r.run_id,
        r.model_id,
        r.data_trade_date,
        r.prediction_trade_date,
        sr.trade_date,
        s.symbol_norm,
        sr.stock_name,
        sr.industry,
        COALESCE(sr.concept_tags, '[]'::jsonb),
        sr.close_price,
        sr.latest_change_pct,
        sr.turnover_rate,
        sr.amount,
        sr.total_mv,
        sr.market_type,
        sr.province,
        sr.city,
        s.fusion_score,
        s.light_score,
        s.tft_score,
        s.score_rank,
        s.signal_side,
        s.expected_price,
        s.universe_tag,
        s.quality,
        CASE WHEN s.fusion_score >= 0.80 THEN 'high' WHEN s.fusion_score >= 0.60 THEN 'medium' ELSE 'watch' END,
        s.fusion_score,
        sr.listed_days,
        COALESCE(sr.consecutive_limit_up_days, 0),
        COALESCE(sr.recent_limit_up_count_5d, 0),
        COALESCE(sr.volume_trend_3d, FALSE),
        COALESCE(sr.volume_trend_5d, FALSE),
        sr.volume_trend_3d_score,
        sr.volume_ma_3,
        sr.volume_ma_5,
        sr.ma5,
        sr.ma10,
        sr.ma20,
        sr.return_5d,
        sr.return_10d,
        sr.close_above_ma5,
        sr.close_above_ma10,
        sr.continued_rise_days,
        sr.continued_fall_days,
        sr.amount_rank,
        sr.amount_ma_3,
        sr.amount_ma_5,
        sr.high_5d,
        sr.low_5d,
        sr.life_high_week,
        sr.life_high_month,
        sr.life_high_3month,
        sr.life_high_6month,
        sr.life_high_one_year,
        sr.is_st,
        sr.is_suspended,
        sr.is_limit_up,
        sr.is_limit_down,
        NOT COALESCE(sr.is_limit_up, FALSE) AND NOT COALESCE(sr.is_limit_down, FALSE) AND NOT COALESCE(sr.is_suspended, FALSE),
        sr.is_hs300,
        sr.is_csi1000,
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN s.fusion_score >= 0.80 THEN '模型高分' END,
          CASE WHEN sr.volume_trend_3d THEN '3日量能递增' END,
          CASE WHEN sr.close_above_ma5 THEN '站上MA5' END,
          CASE WHEN COALESCE(sr.consecutive_limit_up_days, 0) >= 2 THEN '连涨' || sr.consecutive_limit_up_days::TEXT || '日' END,
          CASE WHEN sr.industry IS NOT NULL THEN '行业:' || sr.industry END
        ]::TEXT[], NULL)),
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN COALESCE(sr.is_limit_up, FALSE) THEN '涨停不可追' END,
          CASE WHEN COALESCE(sr.is_limit_down, FALSE) THEN '跌停流动性受限' END,
          CASE WHEN sr.return_10d IS NOT NULL AND sr.return_10d < -0.12 THEN '近10日回撤较大' END,
          CASE WHEN sr.close_above_ma10 = FALSE THEN '弱于MA10' END
        ]::TEXT[], NULL)),
        CASE WHEN sr.industry IS NOT NULL THEN '模型高分，' || sr.industry || '方向，结合趋势与量能具备进一步研究价值' ELSE '模型高分候选，结合趋势与量能具备进一步研究价值' END,
        GREATEST(COALESCE(s.created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00'), COALESCE(r.run_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')),
        NOW()
      FROM candidate_runs r
      JOIN signal_rows s
        ON s.run_id = r.run_id AND s.tenant_id = r.tenant_id AND s.user_id = r.user_id
      LEFT JOIN source_rows sr
        ON sr.trade_date = r.prediction_trade_date AND sr.symbol_norm = s.symbol_norm
      ON CONFLICT (tenant_id, user_id, run_id, symbol)
      DO UPDATE SET
        model_id = EXCLUDED.model_id,
        data_trade_date = EXCLUDED.data_trade_date,
        prediction_trade_date = EXCLUDED.prediction_trade_date,
        market_snapshot_trade_date = EXCLUDED.market_snapshot_trade_date,
        stock_name = EXCLUDED.stock_name,
        industry = EXCLUDED.industry,
        concept_tags = EXCLUDED.concept_tags,
        close_price = EXCLUDED.close_price,
        latest_change_pct = EXCLUDED.latest_change_pct,
        turnover_rate = EXCLUDED.turnover_rate,
        amount = EXCLUDED.amount,
        total_mv = EXCLUDED.total_mv,
        market_type = EXCLUDED.market_type,
        province = EXCLUDED.province,
        city = EXCLUDED.city,
        fusion_score = EXCLUDED.fusion_score,
        light_score = EXCLUDED.light_score,
        tft_score = EXCLUDED.tft_score,
        score_rank = EXCLUDED.score_rank,
        signal_side = EXCLUDED.signal_side,
        expected_price = EXCLUDED.expected_price,
        universe_tag = EXCLUDED.universe_tag,
        quality = EXCLUDED.quality,
        confidence_level = EXCLUDED.confidence_level,
        confidence_score = EXCLUDED.confidence_score,
        listed_days = EXCLUDED.listed_days,
        consecutive_limit_up_days = EXCLUDED.consecutive_limit_up_days,
        recent_limit_up_count_5d = EXCLUDED.recent_limit_up_count_5d,
        volume_trend_3d = EXCLUDED.volume_trend_3d,
        volume_trend_5d = EXCLUDED.volume_trend_5d,
        volume_trend_3d_score = EXCLUDED.volume_trend_3d_score,
        volume_ma_3 = EXCLUDED.volume_ma_3,
        volume_ma_5 = EXCLUDED.volume_ma_5,
        ma5 = EXCLUDED.ma5,
        ma10 = EXCLUDED.ma10,
        ma20 = EXCLUDED.ma20,
        return_5d = EXCLUDED.return_5d,
        return_10d = EXCLUDED.return_10d,
        close_above_ma5 = EXCLUDED.close_above_ma5,
        close_above_ma10 = EXCLUDED.close_above_ma10,
        continued_rise_days = EXCLUDED.continued_rise_days,
        continued_fall_days = EXCLUDED.continued_fall_days,
        amount_rank = EXCLUDED.amount_rank,
        amount_ma_3 = EXCLUDED.amount_ma_3,
        amount_ma_5 = EXCLUDED.amount_ma_5,
        high_5d = EXCLUDED.high_5d,
        low_5d = EXCLUDED.low_5d,
        life_high_week = EXCLUDED.life_high_week,
        life_high_month = EXCLUDED.life_high_month,
        life_high_3month = EXCLUDED.life_high_3month,
        life_high_6month = EXCLUDED.life_high_6month,
        life_high_one_year = EXCLUDED.life_high_one_year,
        is_st = EXCLUDED.is_st,
        is_suspended = EXCLUDED.is_suspended,
        is_limit_up = EXCLUDED.is_limit_up,
        is_limit_down = EXCLUDED.is_limit_down,
        tradable_flag = EXCLUDED.tradable_flag,
        is_hs300 = EXCLUDED.is_hs300,
        is_csi1000 = EXCLUDED.is_csi1000,
        hit_reasons = EXCLUDED.hit_reasons,
        risk_flags = EXCLUDED.risk_flags,
        thesis_summary = EXCLUDED.thesis_summary,
        source_updated_at = EXCLUDED.source_updated_at,
        updated_at = NOW()
      RETURNING source_updated_at
    )
    SELECT COUNT(*), MAX(source_updated_at)
      INTO v_imported_rows, v_max_source_updated_at
      FROM upserted;
  ELSIF v_has_stock_selection THEN
    WITH candidate_runs AS (
      SELECT
        r.run_id,
        r.tenant_id,
        r.user_id,
        r.model_id,
        r.data_trade_date,
        r.prediction_trade_date,
        r.updated_at AS run_updated_at
      FROM qm_model_inference_runs r
      WHERE r.status = 'completed'
        AND r.prediction_trade_date = v_imported_prediction_trade_date
        AND (p_tenant_id IS NULL OR r.tenant_id = p_tenant_id)
        AND (p_user_id IS NULL OR r.user_id = p_user_id)
        AND (
          p_force_rebuild
          OR r.updated_at > COALESCE(v_last_source_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')
        )
    ),
    signal_rows AS (
      SELECT
        ranked.run_id,
        ranked.tenant_id,
        ranked.user_id,
        ranked.trade_date,
        ranked.symbol_norm,
        ranked.light_score,
        ranked.tft_score,
        ranked.fusion_score,
        ranked.score_rank,
        ranked.universe_tag,
        ranked.signal_side,
        ranked.expected_price,
        ranked.quality,
        ranked.created_at
      FROM (
        SELECT
          s.run_id,
          s.tenant_id,
          s.user_id,
          s.trade_date,
          LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0') AS symbol_norm,
          s.light_score,
          s.tft_score,
          s.fusion_score,
          s.score_rank,
          s.universe_tag,
          s.signal_side,
          s.expected_price,
          COALESCE(s.quality, '{}'::jsonb) AS quality,
          s.created_at,
          ROW_NUMBER() OVER (
            PARTITION BY s.run_id, s.tenant_id, s.user_id, s.trade_date, LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0')
            ORDER BY s.created_at DESC NULLS LAST, s.fusion_score DESC NULLS LAST
          ) AS row_num
        FROM engine_signal_scores s
        JOIN candidate_runs r
          ON r.run_id = s.run_id
         AND r.tenant_id = s.tenant_id
         AND r.user_id = s.user_id
         AND r.prediction_trade_date = s.trade_date
      ) ranked
      WHERE ranked.row_num = 1
    ),
    source_rows AS (
      SELECT
        trade_date,
        LPAD(REGEXP_REPLACE(symbol, '[^0-9]', '', 'g'), 6, '0') AS symbol_norm,
        name AS stock_name,
        industry,
        '[]'::jsonb AS concept_tags,
        close AS close_price,
        pct_chg AS latest_change_pct,
        turnover_rate,
        amount,
        market_cap AS total_mv,
        NULL::TEXT AS market_type,
        NULL::TEXT AS province,
        NULL::TEXT AS city,
        CASE WHEN is_listed_over_1y THEN 366 ELSE NULL END AS listed_days,
        0 AS consecutive_limit_up_days,
        0 AS recent_limit_up_count_5d,
        COALESCE(vol_3d_up, FALSE) AS volume_trend_3d,
        COALESCE(vol_5d_up, FALSE) AS volume_trend_5d,
        vol_3d_trend AS volume_trend_3d_score,
        vol_3d_sum / 3.0 AS volume_ma_3,
        vol_5d_avg AS volume_ma_5,
        ma5,
        ma10,
        ma20,
        return_5d,
        return_10d,
        close_above_ma5,
        close_above_ma10,
        NULL::INTEGER AS continued_rise_days,
        NULL::INTEGER AS continued_fall_days,
        amount_rank,
        amount_3d_sum / 3.0 AS amount_ma_3,
        amount_5d_sum / 5.0 AS amount_ma_5,
        high_5d,
        low_5d,
        NULL::DOUBLE PRECISION AS life_high_week,
        NULL::DOUBLE PRECISION AS life_high_month,
        NULL::DOUBLE PRECISION AS life_high_3month,
        NULL::DOUBLE PRECISION AS life_high_6month,
        NULL::DOUBLE PRECISION AS life_high_one_year,
        COALESCE(is_st, FALSE) AS is_st,
        COALESCE(is_suspended, FALSE) AS is_suspended,
        FALSE AS is_limit_up,
        FALSE AS is_limit_down,
        FALSE AS is_hs300,
        FALSE AS is_csi1000
      FROM stock_selection
      WHERE trade_date = v_imported_prediction_trade_date
    ),
    upserted AS (
      INSERT INTO qm_research_candidate_snapshot (
        tenant_id, user_id, run_id, model_id, data_trade_date, prediction_trade_date, market_snapshot_trade_date,
        symbol, stock_name, industry, concept_tags, close_price, latest_change_pct, turnover_rate, amount, total_mv,
        market_type, province, city, fusion_score, light_score, tft_score, score_rank, signal_side, expected_price,
        universe_tag, quality, confidence_level, confidence_score, listed_days, consecutive_limit_up_days,
        recent_limit_up_count_5d, volume_trend_3d, volume_trend_5d, volume_trend_3d_score, volume_ma_3, volume_ma_5,
        ma5, ma10, ma20, return_5d, return_10d, close_above_ma5, close_above_ma10, continued_rise_days,
        continued_fall_days, amount_rank, amount_ma_3, amount_ma_5, high_5d, low_5d, life_high_week,
        life_high_month, life_high_3month, life_high_6month, life_high_one_year, is_st, is_suspended,
        is_limit_up, is_limit_down, tradable_flag, is_hs300, is_csi1000, hit_reasons, risk_flags,
        thesis_summary, source_updated_at, updated_at
      )
      SELECT
        r.tenant_id, r.user_id, r.run_id, r.model_id, r.data_trade_date, r.prediction_trade_date, sr.trade_date,
        s.symbol_norm, sr.stock_name, sr.industry, COALESCE(sr.concept_tags, '[]'::jsonb), sr.close_price, sr.latest_change_pct, sr.turnover_rate,
        sr.amount, sr.total_mv, sr.market_type, sr.province, sr.city, s.fusion_score, s.light_score, s.tft_score,
        s.score_rank, s.signal_side, s.expected_price, s.universe_tag, s.quality,
        CASE WHEN s.fusion_score >= 0.80 THEN 'high' WHEN s.fusion_score >= 0.60 THEN 'medium' ELSE 'watch' END,
        s.fusion_score, sr.listed_days, COALESCE(sr.consecutive_limit_up_days, 0), COALESCE(sr.recent_limit_up_count_5d, 0), COALESCE(sr.volume_trend_3d, FALSE),
        COALESCE(sr.volume_trend_5d, FALSE), sr.volume_trend_3d_score, sr.volume_ma_3, sr.volume_ma_5, sr.ma5, sr.ma10, sr.ma20,
        sr.return_5d, sr.return_10d, sr.close_above_ma5, sr.close_above_ma10, sr.continued_rise_days,
        sr.continued_fall_days, sr.amount_rank, sr.amount_ma_3, sr.amount_ma_5, sr.high_5d, sr.low_5d,
        sr.life_high_week, sr.life_high_month, sr.life_high_3month, sr.life_high_6month, sr.life_high_one_year,
        sr.is_st, sr.is_suspended, sr.is_limit_up, sr.is_limit_down,
        NOT COALESCE(sr.is_st, FALSE) AND NOT COALESCE(sr.is_suspended, FALSE),
        sr.is_hs300, sr.is_csi1000,
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN s.fusion_score >= 0.80 THEN '模型高分' END,
          CASE WHEN sr.volume_trend_3d THEN '3日量能递增' END,
          CASE WHEN sr.close_above_ma5 THEN '站上MA5' END,
          CASE WHEN sr.industry IS NOT NULL THEN '行业:' || sr.industry END
        ]::TEXT[], NULL)),
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN COALESCE(sr.is_st, FALSE) THEN 'ST风险' END,
          CASE WHEN COALESCE(sr.is_suspended, FALSE) THEN '停牌' END,
          CASE WHEN sr.close_above_ma10 = FALSE THEN '弱于MA10' END
        ]::TEXT[], NULL)),
        CASE WHEN sr.industry IS NOT NULL THEN '模型高分，' || sr.industry || '方向，结合量能与均线进一步研究' ELSE '模型高分候选，结合量能与均线进一步研究' END,
        GREATEST(COALESCE(s.created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00'), COALESCE(r.run_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')),
        NOW()
      FROM candidate_runs r
      JOIN signal_rows s
        ON s.run_id = r.run_id AND s.tenant_id = r.tenant_id AND s.user_id = r.user_id
      LEFT JOIN source_rows sr
        ON sr.trade_date = r.prediction_trade_date AND sr.symbol_norm = s.symbol_norm
      ON CONFLICT (tenant_id, user_id, run_id, symbol)
      DO UPDATE SET
        model_id = EXCLUDED.model_id,
        data_trade_date = EXCLUDED.data_trade_date,
        prediction_trade_date = EXCLUDED.prediction_trade_date,
        market_snapshot_trade_date = EXCLUDED.market_snapshot_trade_date,
        stock_name = EXCLUDED.stock_name,
        industry = EXCLUDED.industry,
        concept_tags = EXCLUDED.concept_tags,
        close_price = EXCLUDED.close_price,
        latest_change_pct = EXCLUDED.latest_change_pct,
        turnover_rate = EXCLUDED.turnover_rate,
        amount = EXCLUDED.amount,
        total_mv = EXCLUDED.total_mv,
        market_type = EXCLUDED.market_type,
        province = EXCLUDED.province,
        city = EXCLUDED.city,
        fusion_score = EXCLUDED.fusion_score,
        light_score = EXCLUDED.light_score,
        tft_score = EXCLUDED.tft_score,
        score_rank = EXCLUDED.score_rank,
        signal_side = EXCLUDED.signal_side,
        expected_price = EXCLUDED.expected_price,
        universe_tag = EXCLUDED.universe_tag,
        quality = EXCLUDED.quality,
        confidence_level = EXCLUDED.confidence_level,
        confidence_score = EXCLUDED.confidence_score,
        listed_days = EXCLUDED.listed_days,
        consecutive_limit_up_days = EXCLUDED.consecutive_limit_up_days,
        recent_limit_up_count_5d = EXCLUDED.recent_limit_up_count_5d,
        volume_trend_3d = EXCLUDED.volume_trend_3d,
        volume_trend_5d = EXCLUDED.volume_trend_5d,
        volume_trend_3d_score = EXCLUDED.volume_trend_3d_score,
        volume_ma_3 = EXCLUDED.volume_ma_3,
        volume_ma_5 = EXCLUDED.volume_ma_5,
        ma5 = EXCLUDED.ma5,
        ma10 = EXCLUDED.ma10,
        ma20 = EXCLUDED.ma20,
        return_5d = EXCLUDED.return_5d,
        return_10d = EXCLUDED.return_10d,
        close_above_ma5 = EXCLUDED.close_above_ma5,
        close_above_ma10 = EXCLUDED.close_above_ma10,
        continued_rise_days = EXCLUDED.continued_rise_days,
        continued_fall_days = EXCLUDED.continued_fall_days,
        amount_rank = EXCLUDED.amount_rank,
        amount_ma_3 = EXCLUDED.amount_ma_3,
        amount_ma_5 = EXCLUDED.amount_ma_5,
        high_5d = EXCLUDED.high_5d,
        low_5d = EXCLUDED.low_5d,
        life_high_week = EXCLUDED.life_high_week,
        life_high_month = EXCLUDED.life_high_month,
        life_high_3month = EXCLUDED.life_high_3month,
        life_high_6month = EXCLUDED.life_high_6month,
        life_high_one_year = EXCLUDED.life_high_one_year,
        is_st = EXCLUDED.is_st,
        is_suspended = EXCLUDED.is_suspended,
        is_limit_up = EXCLUDED.is_limit_up,
        is_limit_down = EXCLUDED.is_limit_down,
        tradable_flag = EXCLUDED.tradable_flag,
        is_hs300 = EXCLUDED.is_hs300,
        is_csi1000 = EXCLUDED.is_csi1000,
        hit_reasons = EXCLUDED.hit_reasons,
        risk_flags = EXCLUDED.risk_flags,
        thesis_summary = EXCLUDED.thesis_summary,
        source_updated_at = EXCLUDED.source_updated_at,
        updated_at = NOW()
      RETURNING source_updated_at
    )
    SELECT COUNT(*), MAX(source_updated_at)
      INTO v_imported_rows, v_max_source_updated_at
      FROM upserted;
  ELSE
    WITH candidate_runs AS (
      SELECT
        r.run_id,
        r.tenant_id,
        r.user_id,
        r.model_id,
        r.data_trade_date,
        r.prediction_trade_date,
        r.updated_at AS run_updated_at
      FROM qm_model_inference_runs r
      WHERE r.status = 'completed'
        AND r.prediction_trade_date = v_imported_prediction_trade_date
        AND (p_tenant_id IS NULL OR r.tenant_id = p_tenant_id)
        AND (p_user_id IS NULL OR r.user_id = p_user_id)
        AND (
          p_force_rebuild
          OR r.updated_at > COALESCE(v_last_source_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')
        )
    ),
    signal_rows AS (
      SELECT
        ranked.run_id,
        ranked.tenant_id,
        ranked.user_id,
        ranked.trade_date,
        ranked.symbol_norm,
        ranked.light_score,
        ranked.tft_score,
        ranked.fusion_score,
        ranked.score_rank,
        ranked.universe_tag,
        ranked.signal_side,
        ranked.expected_price,
        ranked.quality,
        ranked.created_at
      FROM (
        SELECT
          s.run_id,
          s.tenant_id,
          s.user_id,
          s.trade_date,
          LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0') AS symbol_norm,
          s.light_score,
          s.tft_score,
          s.fusion_score,
          s.score_rank,
          s.universe_tag,
          s.signal_side,
          s.expected_price,
          COALESCE(s.quality, '{}'::jsonb) AS quality,
          s.created_at,
          ROW_NUMBER() OVER (
            PARTITION BY s.run_id, s.tenant_id, s.user_id, s.trade_date, LPAD(REGEXP_REPLACE(s.symbol, '[^0-9]', '', 'g'), 6, '0')
            ORDER BY s.created_at DESC NULLS LAST, s.fusion_score DESC NULLS LAST
          ) AS row_num
        FROM engine_signal_scores s
        JOIN candidate_runs r
          ON r.run_id = s.run_id AND r.tenant_id = s.tenant_id AND r.user_id = s.user_id AND r.prediction_trade_date = s.trade_date
      ) ranked
      WHERE ranked.row_num = 1
    ),
    source_rows AS (
      SELECT
        mdd.trade_date,
        mdd.symbol AS symbol_norm,
        NULL::TEXT AS stock_name,
        NULL::TEXT AS industry,
        '[]'::jsonb AS concept_tags,
        mdd.raw_close AS close_price,
        NULL::DOUBLE PRECISION AS latest_change_pct,
        NULL::DOUBLE PRECISION AS turnover_rate,
        NULL::DOUBLE PRECISION AS amount,
        NULL::DOUBLE PRECISION AS total_mv,
        NULL::TEXT AS market_type,
        NULL::TEXT AS province,
        NULL::TEXT AS city,
        NULL::INTEGER AS listed_days,
        0 AS consecutive_limit_up_days,
        0 AS recent_limit_up_count_5d,
        FALSE AS volume_trend_3d,
        FALSE AS volume_trend_5d,
        NULL::INTEGER AS volume_trend_3d_score,
        NULL::DOUBLE PRECISION AS volume_ma_3,
        NULL::DOUBLE PRECISION AS volume_ma_5,
        NULL::DOUBLE PRECISION AS ma5,
        NULL::DOUBLE PRECISION AS ma10,
        NULL::DOUBLE PRECISION AS ma20,
        NULL::DOUBLE PRECISION AS return_5d,
        NULL::DOUBLE PRECISION AS return_10d,
        NULL::BOOLEAN AS close_above_ma5,
        NULL::BOOLEAN AS close_above_ma10,
        NULL::INTEGER AS continued_rise_days,
        NULL::INTEGER AS continued_fall_days,
        NULL::INTEGER AS amount_rank,
        NULL::DOUBLE PRECISION AS amount_ma_3,
        NULL::DOUBLE PRECISION AS amount_ma_5,
        NULL::DOUBLE PRECISION AS high_5d,
        NULL::DOUBLE PRECISION AS low_5d,
        NULL::DOUBLE PRECISION AS life_high_week,
        NULL::DOUBLE PRECISION AS life_high_month,
        NULL::DOUBLE PRECISION AS life_high_3month,
        NULL::DOUBLE PRECISION AS life_high_6month,
        NULL::DOUBLE PRECISION AS life_high_one_year,
        FALSE AS is_st,
        FALSE AS is_suspended,
        FALSE AS is_limit_up,
        FALSE AS is_limit_down,
        FALSE AS is_hs300,
        FALSE AS is_csi1000
      FROM market_data_daily mdd
      WHERE mdd.trade_date = v_imported_prediction_trade_date
        AND mdd.raw_close IS NOT NULL
    ),
    upserted AS (
      INSERT INTO qm_research_candidate_snapshot (
        tenant_id, user_id, run_id, model_id, data_trade_date, prediction_trade_date, market_snapshot_trade_date,
        symbol, stock_name, industry, concept_tags, close_price, latest_change_pct, turnover_rate, amount, total_mv,
        market_type, province, city, fusion_score, light_score, tft_score, score_rank, signal_side, expected_price,
        universe_tag, quality, confidence_level, confidence_score, listed_days, consecutive_limit_up_days,
        recent_limit_up_count_5d, volume_trend_3d, volume_trend_5d, volume_trend_3d_score, volume_ma_3, volume_ma_5,
        ma5, ma10, ma20, return_5d, return_10d, close_above_ma5, close_above_ma10, continued_rise_days,
        continued_fall_days, amount_rank, amount_ma_3, amount_ma_5, high_5d, low_5d, life_high_week,
        life_high_month, life_high_3month, life_high_6month, life_high_one_year, is_st, is_suspended,
        is_limit_up, is_limit_down, tradable_flag, is_hs300, is_csi1000, hit_reasons, risk_flags,
        thesis_summary, source_updated_at, updated_at
      )
      SELECT
        r.tenant_id, r.user_id, r.run_id, r.model_id, r.data_trade_date, r.prediction_trade_date, sr.trade_date,
        s.symbol_norm, sr.stock_name, sr.industry, COALESCE(sr.concept_tags, '[]'::jsonb), sr.close_price, sr.latest_change_pct, sr.turnover_rate,
        sr.amount, sr.total_mv, sr.market_type, sr.province, sr.city, s.fusion_score, s.light_score, s.tft_score,
        s.score_rank, s.signal_side, s.expected_price, s.universe_tag, s.quality,
        CASE WHEN s.fusion_score >= 0.80 THEN 'high' WHEN s.fusion_score >= 0.60 THEN 'medium' ELSE 'watch' END,
        s.fusion_score, sr.listed_days, COALESCE(sr.consecutive_limit_up_days, 0), COALESCE(sr.recent_limit_up_count_5d, 0), COALESCE(sr.volume_trend_3d, FALSE),
        COALESCE(sr.volume_trend_5d, FALSE), sr.volume_trend_3d_score, sr.volume_ma_3, sr.volume_ma_5, sr.ma5, sr.ma10, sr.ma20,
        sr.return_5d, sr.return_10d, sr.close_above_ma5, sr.close_above_ma10, sr.continued_rise_days,
        sr.continued_fall_days, sr.amount_rank, sr.amount_ma_3, sr.amount_ma_5, sr.high_5d, sr.low_5d,
        sr.life_high_week, sr.life_high_month, sr.life_high_3month, sr.life_high_6month, sr.life_high_one_year,
        sr.is_st, sr.is_suspended, sr.is_limit_up, sr.is_limit_down,
        NOT COALESCE(sr.is_st, FALSE),
        sr.is_hs300, sr.is_csi1000,
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN s.fusion_score >= 0.80 THEN '模型高分' END,
          CASE WHEN sr.industry IS NOT NULL THEN '行业:' || sr.industry END
        ]::TEXT[], NULL)),
        TO_JSONB(ARRAY_REMOVE(ARRAY[
          CASE WHEN COALESCE(sr.is_st, FALSE) THEN 'ST风险' END
        ]::TEXT[], NULL)),
        CASE WHEN sr.industry IS NOT NULL THEN '模型高分，' || sr.industry || '方向，待补充更多技术字段' ELSE '模型高分候选，待补充更多技术字段' END,
        GREATEST(COALESCE(s.created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00'), COALESCE(r.run_updated_at, TIMESTAMPTZ '1970-01-01 00:00:00+00')),
        NOW()
      FROM candidate_runs r
      JOIN signal_rows s
        ON s.run_id = r.run_id AND s.tenant_id = r.tenant_id AND s.user_id = r.user_id
      LEFT JOIN source_rows sr
        ON sr.trade_date = r.prediction_trade_date AND sr.symbol_norm = s.symbol_norm
      ON CONFLICT (tenant_id, user_id, run_id, symbol)
      DO UPDATE SET
        model_id = EXCLUDED.model_id,
        data_trade_date = EXCLUDED.data_trade_date,
        prediction_trade_date = EXCLUDED.prediction_trade_date,
        market_snapshot_trade_date = EXCLUDED.market_snapshot_trade_date,
        stock_name = EXCLUDED.stock_name,
        industry = EXCLUDED.industry,
        concept_tags = EXCLUDED.concept_tags,
        close_price = EXCLUDED.close_price,
        latest_change_pct = EXCLUDED.latest_change_pct,
        turnover_rate = EXCLUDED.turnover_rate,
        amount = EXCLUDED.amount,
        total_mv = EXCLUDED.total_mv,
        market_type = EXCLUDED.market_type,
        province = EXCLUDED.province,
        city = EXCLUDED.city,
        fusion_score = EXCLUDED.fusion_score,
        light_score = EXCLUDED.light_score,
        tft_score = EXCLUDED.tft_score,
        score_rank = EXCLUDED.score_rank,
        signal_side = EXCLUDED.signal_side,
        expected_price = EXCLUDED.expected_price,
        universe_tag = EXCLUDED.universe_tag,
        quality = EXCLUDED.quality,
        confidence_level = EXCLUDED.confidence_level,
        confidence_score = EXCLUDED.confidence_score,
        listed_days = EXCLUDED.listed_days,
        consecutive_limit_up_days = EXCLUDED.consecutive_limit_up_days,
        recent_limit_up_count_5d = EXCLUDED.recent_limit_up_count_5d,
        volume_trend_3d = EXCLUDED.volume_trend_3d,
        volume_trend_5d = EXCLUDED.volume_trend_5d,
        volume_trend_3d_score = EXCLUDED.volume_trend_3d_score,
        volume_ma_3 = EXCLUDED.volume_ma_3,
        volume_ma_5 = EXCLUDED.volume_ma_5,
        ma5 = EXCLUDED.ma5,
        ma10 = EXCLUDED.ma10,
        ma20 = EXCLUDED.ma20,
        return_5d = EXCLUDED.return_5d,
        return_10d = EXCLUDED.return_10d,
        close_above_ma5 = EXCLUDED.close_above_ma5,
        close_above_ma10 = EXCLUDED.close_above_ma10,
        continued_rise_days = EXCLUDED.continued_rise_days,
        continued_fall_days = EXCLUDED.continued_fall_days,
        amount_rank = EXCLUDED.amount_rank,
        amount_ma_3 = EXCLUDED.amount_ma_3,
        amount_ma_5 = EXCLUDED.amount_ma_5,
        high_5d = EXCLUDED.high_5d,
        low_5d = EXCLUDED.low_5d,
        life_high_week = EXCLUDED.life_high_week,
        life_high_month = EXCLUDED.life_high_month,
        life_high_3month = EXCLUDED.life_high_3month,
        life_high_6month = EXCLUDED.life_high_6month,
        life_high_one_year = EXCLUDED.life_high_one_year,
        is_st = EXCLUDED.is_st,
        is_suspended = EXCLUDED.is_suspended,
        is_limit_up = EXCLUDED.is_limit_up,
        is_limit_down = EXCLUDED.is_limit_down,
        tradable_flag = EXCLUDED.tradable_flag,
        is_hs300 = EXCLUDED.is_hs300,
        is_csi1000 = EXCLUDED.is_csi1000,
        hit_reasons = EXCLUDED.hit_reasons,
        risk_flags = EXCLUDED.risk_flags,
        thesis_summary = EXCLUDED.thesis_summary,
        source_updated_at = EXCLUDED.source_updated_at,
        updated_at = NOW()
      RETURNING source_updated_at
    )
    SELECT COUNT(*), MAX(source_updated_at)
      INTO v_imported_rows, v_max_source_updated_at
      FROM upserted;
  END IF;

  -- 概念标签回填（导入源无 concept 时兜底）：
  -- 1) 优先使用 industry_classification.stock_codes 映射的行业标签；
  -- 2) 若仍为空且 industry 非空，则使用 industry 作为单标签。
  WITH concept_map AS (
    SELECT
      TRIM(SPLIT_PART(code_item, '.', 1)) AS symbol_norm,
      JSONB_AGG(ic.industry_name ORDER BY ic.industry_name) AS concept_tags
    FROM industry_classification ic
    CROSS JOIN LATERAL regexp_split_to_table(COALESCE(ic.stock_codes, ''), ',') AS code_item
    WHERE COALESCE(ic.industry_name, '') <> ''
      AND COALESCE(TRIM(code_item), '') <> ''
    GROUP BY TRIM(SPLIT_PART(code_item, '.', 1))
  )
  UPDATE qm_research_candidate_snapshot s
     SET concept_tags = cm.concept_tags,
         updated_at = NOW()
    FROM concept_map cm
   WHERE s.prediction_trade_date = v_imported_prediction_trade_date
     AND (p_tenant_id IS NULL OR s.tenant_id = p_tenant_id)
     AND (p_user_id IS NULL OR s.user_id = p_user_id)
     AND (s.concept_tags IS NULL OR s.concept_tags = '[]'::jsonb)
     AND s.symbol = cm.symbol_norm;

  UPDATE qm_research_candidate_snapshot s
     SET concept_tags = jsonb_build_array(s.industry),
         updated_at = NOW()
   WHERE s.prediction_trade_date = v_imported_prediction_trade_date
     AND (p_tenant_id IS NULL OR s.tenant_id = p_tenant_id)
     AND (p_user_id IS NULL OR s.user_id = p_user_id)
     AND (s.concept_tags IS NULL OR s.concept_tags = '[]'::jsonb)
     AND COALESCE(s.industry, '') <> '';

  SELECT run_id
    INTO v_last_run_id
    FROM qm_research_candidate_snapshot
   WHERE prediction_trade_date = v_imported_prediction_trade_date
     AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id)
     AND (p_user_id IS NULL OR user_id = p_user_id)
   ORDER BY source_updated_at DESC NULLS LAST, run_id DESC
   LIMIT 1;

  UPDATE qm_research_import_state
     SET last_source_updated_at = COALESCE(v_max_source_updated_at, last_source_updated_at),
         last_prediction_trade_date = COALESCE(v_imported_prediction_trade_date, last_prediction_trade_date),
         last_run_id = COALESCE(v_last_run_id, last_run_id),
         updated_at = NOW(),
         extra_json = jsonb_build_object('source_mode', v_source_mode, 'force_rebuild', p_force_rebuild)
   WHERE job_name = 'research_candidate_snapshot';

  RETURN QUERY
  SELECT COALESCE(v_imported_rows, 0), v_imported_prediction_trade_date, v_source_mode, v_max_source_updated_at;
END;
$$;


ALTER FUNCTION public.qm_import_research_candidate_snapshot(p_prediction_trade_date date, p_tenant_id text, p_user_id text, p_force_rebuild boolean) OWNER TO quantmind;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_data_files; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.admin_data_files (
    id integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    data_source_id integer,
    filename character varying(255) NOT NULL,
    file_size integer,
    status character varying(32),
    meta json,
    created_at timestamp without time zone
);


ALTER TABLE public.admin_data_files OWNER TO quantmind;

--
-- Name: COLUMN admin_data_files.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_data_files.status IS 'uploaded, processing, ready, error';


--
-- Name: admin_data_files_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.admin_data_files_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.admin_data_files_id_seq OWNER TO quantmind;

--
-- Name: admin_data_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.admin_data_files_id_seq OWNED BY public.admin_data_files.id;


--
-- Name: admin_models; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.admin_models (
    id integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    description text,
    source_type character varying(32) NOT NULL,
    start_date timestamp without time zone,
    end_date timestamp without time zone,
    config json,
    is_active boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE public.admin_models OWNER TO quantmind;

--
-- Name: COLUMN admin_models.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_models.user_id IS '归属用户ID';


--
-- Name: COLUMN admin_models.source_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_models.source_type IS 'ai_model, hybrid, external';


--
-- Name: COLUMN admin_models.start_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_models.start_date IS '模型数据开始日期';


--
-- Name: COLUMN admin_models.end_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_models.end_date IS '模型数据结束日期';


--
-- Name: COLUMN admin_models.config; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_models.config IS '配置参数';


--
-- Name: admin_models_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.admin_models_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.admin_models_id_seq OWNER TO quantmind;

--
-- Name: admin_models_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.admin_models_id_seq OWNED BY public.admin_models.id;


--
-- Name: admin_training_jobs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.admin_training_jobs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    status character varying(32),
    instance_id character varying(64),
    request_payload json,
    logs text,
    result json,
    progress integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE public.admin_training_jobs OWNER TO quantmind;

--
-- Name: COLUMN admin_training_jobs.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.status IS 'pending, provisioning, running, waiting_callback, completed, failed';


--
-- Name: COLUMN admin_training_jobs.instance_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.instance_id IS '云服务器ID';


--
-- Name: COLUMN admin_training_jobs.request_payload; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.request_payload IS '前端请求参数';


--
-- Name: COLUMN admin_training_jobs.logs; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.logs IS '任务日志(或COS链接)';


--
-- Name: COLUMN admin_training_jobs.result; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.result IS '训练结果与指标';


--
-- Name: COLUMN admin_training_jobs.progress; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.admin_training_jobs.progress IS '进度百分比 0-100';


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO quantmind;

--
-- Name: TABLE alembic_version; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.alembic_version IS 'Alembic主库迁移版本：记录当前数据库Schema的迁移版本号';


--
-- Name: alembic_version_community; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.alembic_version_community (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version_community OWNER TO quantmind;

--
-- Name: TABLE alembic_version_community; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.alembic_version_community IS 'Alembic社区模块迁移版本：社区功能独立Schema的迁移版本号';


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.api_keys (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    name character varying(128),
    permissions jsonb DEFAULT '[]'::jsonb,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    access_key character varying(64),
    secret_hash character varying(255)
);


ALTER TABLE public.api_keys OWNER TO quantmind;

--
-- Name: api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.api_keys_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.api_keys_id_seq OWNER TO quantmind;

--
-- Name: api_keys_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.api_keys_id_seq OWNED BY public.api_keys.id;


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.audit_logs (
    id integer NOT NULL,
    user_id character varying(64),
    tenant_id character varying(64) DEFAULT 'default'::character varying,
    action character varying(100) NOT NULL,
    resource_type character varying(50),
    resource_id character varying(100),
    old_value jsonb,
    new_value jsonb,
    ip_address character varying(64),
    user_agent character varying(255),
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.audit_logs OWNER TO quantmind;

--
-- Name: audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.audit_logs_id_seq OWNER TO quantmind;

--
-- Name: audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.audit_logs_id_seq OWNED BY public.audit_logs.id;


--
-- Name: backtests; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.backtests (
    id integer NOT NULL,
    strategy_id integer NOT NULL,
    user_id integer NOT NULL,
    start_date timestamp(6) without time zone NOT NULL,
    end_date timestamp(6) without time zone NOT NULL,
    initial_capital double precision NOT NULL,
    final_value double precision,
    total_return double precision,
    annual_return double precision,
    max_drawdown double precision,
    sharpe_ratio double precision,
    win_rate double precision,
    trades json NOT NULL,
    metrics json NOT NULL,
    status character varying(20) NOT NULL,
    error_message text,
    created_at timestamp(6) without time zone NOT NULL,
    completed_at timestamp(6) without time zone,
    code_snapshot text,
    code_hash_snapshot character varying(64)
);


ALTER TABLE public.backtests OWNER TO quantmind;

--
-- Name: TABLE backtests; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.backtests IS '策略回测记录表：存储 Qlib 引擎生成的历史回测数据与绩效指标';


--
-- Name: COLUMN backtests.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.id IS '回测唯一ID';


--
-- Name: COLUMN backtests.strategy_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.strategy_id IS '关联的策略ID';


--
-- Name: COLUMN backtests.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.user_id IS '执行回测的用户ID';


--
-- Name: COLUMN backtests.start_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.start_date IS '回测开始日期';


--
-- Name: COLUMN backtests.end_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.end_date IS '回测结束日期';


--
-- Name: COLUMN backtests.initial_capital; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.initial_capital IS '初始资金';


--
-- Name: COLUMN backtests.final_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.final_value IS '回测结束时的资产净值';


--
-- Name: COLUMN backtests.total_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.total_return IS '总收益率 (%)';


--
-- Name: COLUMN backtests.annual_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.annual_return IS '年化收益率 (%)';


--
-- Name: COLUMN backtests.max_drawdown; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.max_drawdown IS '最大回撤 (%)';


--
-- Name: COLUMN backtests.sharpe_ratio; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.sharpe_ratio IS '夏普比率';


--
-- Name: COLUMN backtests.win_rate; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.win_rate IS '胜率 (%)';


--
-- Name: COLUMN backtests.metrics; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.metrics IS '详细绩效指标 (JSONB 格式)';


--
-- Name: COLUMN backtests.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.backtests.status IS '状态 (RUNNING/COMPLETED/FAILED)';


--
-- Name: backtests_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.backtests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1;


ALTER TABLE public.backtests_id_seq OWNER TO quantmind;

--
-- Name: backtests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.backtests_id_seq OWNED BY public.backtests.id;


--
-- Name: community_audit_logs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.community_audit_logs (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    action character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    entity_id character varying(64),
    ip character varying(64),
    user_agent character varying(256),
    meta json,
    created_at timestamp without time zone
);


ALTER TABLE public.community_audit_logs OWNER TO quantmind;

--
-- Name: community_audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.community_audit_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.community_audit_logs_id_seq OWNER TO quantmind;

--
-- Name: community_audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.community_audit_logs_id_seq OWNED BY public.community_audit_logs.id;


--
-- Name: community_author_follows; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.community_author_follows (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    follower_user_id character varying(64) NOT NULL,
    author_user_id character varying(64) NOT NULL,
    created_at timestamp without time zone
);


ALTER TABLE public.community_author_follows OWNER TO quantmind;

--
-- Name: community_author_follows_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.community_author_follows_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.community_author_follows_id_seq OWNER TO quantmind;

--
-- Name: community_author_follows_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.community_author_follows_id_seq OWNED BY public.community_author_follows.id;


--
-- Name: community_comments; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.community_comments (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    post_id bigint NOT NULL,
    author_id character varying(64) NOT NULL,
    content text NOT NULL,
    parent_id bigint,
    reply_to_id bigint,
    likes integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE public.community_comments OWNER TO quantmind;

--
-- Name: community_comments_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.community_comments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.community_comments_id_seq OWNER TO quantmind;

--
-- Name: community_comments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.community_comments_id_seq OWNED BY public.community_comments.id;


--
-- Name: community_interactions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.community_interactions (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    post_id bigint,
    comment_id bigint,
    type character varying(32) NOT NULL,
    created_at timestamp without time zone
);


ALTER TABLE public.community_interactions OWNER TO quantmind;

--
-- Name: community_interactions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.community_interactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.community_interactions_id_seq OWNER TO quantmind;

--
-- Name: community_interactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.community_interactions_id_seq OWNED BY public.community_interactions.id;


--
-- Name: community_posts; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.community_posts (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    author_id character varying(64) NOT NULL,
    title character varying(256) NOT NULL,
    content text NOT NULL,
    category character varying(64),
    tags json,
    media json,
    excerpt text,
    views integer,
    likes integer,
    comments integer,
    collections integer,
    pinned boolean,
    featured boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    last_comment_at timestamp without time zone
);


ALTER TABLE public.community_posts OWNER TO quantmind;

--
-- Name: TABLE community_posts; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.community_posts IS '社区帖子：用户发布的策略分享、观点讨论、问答内容';


--
-- Name: community_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.community_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.community_posts_id_seq OWNER TO quantmind;

--
-- Name: community_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.community_posts_id_seq OWNED BY public.community_posts.id;


--
-- Name: data_download_orders; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.data_download_orders (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    order_no character varying(64) NOT NULL,
    amount double precision NOT NULL,
    currency character varying(10),
    status character varying(20),
    download_type character varying(20),
    description character varying(255),
    metadata_info json,
    created_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone,
    expires_at timestamp with time zone,
    download_count integer DEFAULT 0
);


ALTER TABLE public.data_download_orders OWNER TO quantmind;

--
-- Name: COLUMN data_download_orders.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.id IS '自增ID';


--
-- Name: COLUMN data_download_orders.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.user_id IS '用户ID';


--
-- Name: COLUMN data_download_orders.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.tenant_id IS '租户ID';


--
-- Name: COLUMN data_download_orders.order_no; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.order_no IS '订单号/交易号';


--
-- Name: COLUMN data_download_orders.amount; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.amount IS '支付金额';


--
-- Name: COLUMN data_download_orders.currency; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.currency IS '币种';


--
-- Name: COLUMN data_download_orders.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.status IS '状态: pending/paid/expired/failed';


--
-- Name: COLUMN data_download_orders.download_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.download_type IS '下载类型: cdn/baidu/backup';


--
-- Name: COLUMN data_download_orders.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.description IS '描述';


--
-- Name: COLUMN data_download_orders.metadata_info; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.metadata_info IS '元数据 (如 alipay_trade_no 等)';


--
-- Name: COLUMN data_download_orders.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.created_at IS '创建时间';


--
-- Name: COLUMN data_download_orders.completed_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.completed_at IS '完成时间';


--
-- Name: COLUMN data_download_orders.expires_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.data_download_orders.expires_at IS '过期时间';


--
-- Name: data_download_orders_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.data_download_orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.data_download_orders_id_seq OWNER TO quantmind;

--
-- Name: data_download_orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.data_download_orders_id_seq OWNED BY public.data_download_orders.id;


--
-- Name: email_verifications; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.email_verifications (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    email character varying(255) NOT NULL,
    verification_code character varying(128) NOT NULL,
    code_type character varying(32) NOT NULL,
    is_used boolean,
    is_expired boolean,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    attempts integer,
    ip_address character varying(64)
);


ALTER TABLE public.email_verifications OWNER TO quantmind;

--
-- Name: COLUMN email_verifications.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.email_verifications.user_id IS '用户ID或注册标识';


--
-- Name: COLUMN email_verifications.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.email_verifications.tenant_id IS '租户ID';


--
-- Name: COLUMN email_verifications.code_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.email_verifications.code_type IS '类型: register/reset_password/change_email';


--
-- Name: COLUMN email_verifications.attempts; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.email_verifications.attempts IS '验证尝试次数';


--
-- Name: email_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.email_verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.email_verifications_id_seq OWNER TO quantmind;

--
-- Name: email_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.email_verifications_id_seq OWNED BY public.email_verifications.id;


--
-- Name: engine_dispatch_batches; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.engine_dispatch_batches (
    batch_id character varying(64) NOT NULL,
    run_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) NOT NULL,
    trade_date date NOT NULL,
    strategy_id character varying(64),
    trading_mode character varying(16) DEFAULT 'REAL'::character varying NOT NULL,
    stage character varying(24) DEFAULT 'signal_ready'::character varying NOT NULL,
    stage_updated_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    total_signals integer DEFAULT 0 NOT NULL,
    dispatched_signals integer DEFAULT 0 NOT NULL,
    acked_signals integer DEFAULT 0 NOT NULL,
    order_submitted_count integer DEFAULT 0 NOT NULL,
    order_filled_count integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    trace_id character varying(128),
    last_error text,
    created_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    updated_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_engine_dispatch_batches_mode CHECK (((trading_mode)::text = ANY (ARRAY[('REAL'::character varying)::text, ('SHADOW'::character varying)::text, ('SIMULATION'::character varying)::text]))),
    CONSTRAINT ck_engine_dispatch_batches_stage CHECK (((stage)::text = ANY (ARRAY[('signal_ready'::character varying)::text, ('dispatched'::character varying)::text, ('runner_applied'::character varying)::text, ('order_sent'::character varying)::text, ('fill_confirmed'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.engine_dispatch_batches OWNER TO quantmind;

--
-- Name: TABLE engine_dispatch_batches; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.engine_dispatch_batches IS '推理任务批次：按租户/时间聚合的一批推理任务，控制并发调度';


--
-- Name: engine_dispatch_items; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.engine_dispatch_items (
    id bigint NOT NULL,
    batch_id character varying(64) NOT NULL,
    run_id character varying(64) NOT NULL,
    signal_id character varying(128),
    client_order_id character varying(128),
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(20) NOT NULL,
    action character varying(8) NOT NULL,
    quantity double precision NOT NULL,
    price double precision,
    score double precision,
    dispatch_status character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    order_id uuid,
    exchange_order_id character varying(100),
    exchange_trade_id character varying(100),
    exec_message text,
    created_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    updated_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_engine_dispatch_items_action CHECK (((action)::text = ANY (ARRAY[('BUY'::character varying)::text, ('SELL'::character varying)::text, ('HOLD'::character varying)::text]))),
    CONSTRAINT ck_engine_dispatch_items_status CHECK (((dispatch_status)::text = ANY (ARRAY[('pending'::character varying)::text, ('dispatched'::character varying)::text, ('acked'::character varying)::text, ('order_submitted'::character varying)::text, ('order_filled'::character varying)::text, ('rejected'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.engine_dispatch_items OWNER TO quantmind;

--
-- Name: TABLE engine_dispatch_items; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.engine_dispatch_items IS '推理任务明细：批次中每个策略/用户的推理请求状态与结果摘要';


--
-- Name: engine_dispatch_items_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.engine_dispatch_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.engine_dispatch_items_id_seq OWNER TO quantmind;

--
-- Name: engine_dispatch_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.engine_dispatch_items_id_seq OWNED BY public.engine_dispatch_items.id;


--
-- Name: engine_feature_runs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.engine_feature_runs (
    run_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) NOT NULL,
    trade_date date NOT NULL,
    model_name character varying(64) DEFAULT 'model_qlib'::character varying NOT NULL,
    model_version character varying(64) NOT NULL,
    feature_version character varying(64) NOT NULL,
    feature_dim integer NOT NULL,
    window_start timestamp(6) with time zone,
    window_end timestamp(6) with time zone,
    status character varying(24) DEFAULT 'feature_ready'::character varying NOT NULL,
    expected_symbols integer DEFAULT 0 NOT NULL,
    ready_symbols integer DEFAULT 0 NOT NULL,
    missing_symbols integer DEFAULT 0 NOT NULL,
    source character varying(32) DEFAULT 'l2_batch'::character varying NOT NULL,
    checksum character varying(128),
    quality jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_message text,
    created_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    updated_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    factor double precision,
    style_sp_ttm double precision,
    style_cfp_ttm double precision,
    style_ev_ebitda_ttm double precision,
    style_tobin_q double precision,
    mom_ret_1d double precision,
    mom_ret_3d double precision,
    mom_ret_5d double precision,
    mom_ret_10d double precision,
    mom_ret_20d double precision,
    mom_ret_60d double precision,
    mom_ret_120d double precision,
    mom_ma_gap_5 double precision,
    mom_ma_gap_10 double precision,
    mom_ma_gap_20 double precision,
    mom_ma_gap_60 double precision,
    mom_ma_gap_120 double precision,
    mom_ema_gap_12 double precision,
    mom_ema_gap_26 double precision,
    mom_macd_dif double precision,
    mom_macd_dea double precision,
    mom_macd_hist double precision,
    mom_rsi_6 double precision,
    mom_rsi_14 double precision,
    mom_kdj_k double precision,
    mom_kdj_d double precision,
    mom_kdj_j double precision,
    mom_roc_12 double precision,
    mom_breakout_20d double precision,
    vol_std_5 double precision,
    vol_std_10 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    vol_atr_20 double precision,
    vol_true_range double precision,
    vol_parkinson_10 double precision,
    vol_parkinson_20 double precision,
    vol_gk_10 double precision,
    vol_gk_20 double precision,
    vol_rs_10 double precision,
    vol_rs_20 double precision,
    vol_downside_20 double precision,
    vol_upside_20 double precision,
    vol_realized_rv double precision,
    vol_realized_rrv double precision,
    vol_realized_rskew double precision,
    vol_realized_rkurt double precision,
    vol_jump_zadj double precision,
    vol_jump_rjv_ratio double precision,
    vol_jump_sjv_ratio double precision,
    liq_turnover_os double precision,
    liq_turnover_tl double precision,
    liq_volume double precision,
    liq_volume_ma_5 double precision,
    liq_volume_ma_10 double precision,
    liq_volume_ma_20 double precision,
    liq_volume_ratio_5 double precision,
    liq_volume_ratio_20 double precision,
    liq_amount double precision,
    liq_amount_ma_5 double precision,
    liq_amount_ma_10 double precision,
    liq_amount_ma_20 double precision,
    liq_amount_ratio_5 double precision,
    liq_amount_ratio_20 double precision,
    liq_trade_count double precision,
    liq_avg_trade_size double precision,
    liq_obv_20 double precision,
    liq_obv_60 double precision,
    liq_mfi_14 double precision,
    liq_accdist_20 double precision,
    liq_amihud_20 double precision,
    liq_amihud_60 double precision,
    flow_net_amount double precision,
    flow_net_amount_ratio double precision,
    flow_large_net_amount double precision,
    flow_large_net_ratio double precision,
    flow_medium_net_amount double precision,
    flow_medium_net_ratio double precision,
    flow_small_net_amount double precision,
    flow_small_net_ratio double precision,
    flow_net_order_count double precision,
    flow_net_order_ratio double precision,
    flow_large_net_order double precision,
    flow_large_order_ratio double precision,
    flow_vpin double precision,
    flow_vpin_ma_5 double precision,
    flow_vpin_ma_20 double precision,
    flow_vpin_delta_5 double precision,
    flow_qsp double precision,
    flow_esp double precision,
    flow_aqsp double precision,
    flow_qsp_time double precision,
    flow_esp_time double precision,
    flow_pressure_index double precision,
    style_ln_mv_total double precision,
    style_ln_mv_float double precision,
    style_bp double precision,
    style_ep_ttm double precision,
    style_smb double precision,
    style_hml double precision,
    style_mkt_premium double precision,
    style_beta_20 double precision,
    style_beta_60 double precision,
    style_beta_120 double precision,
    style_idio_vol_20 double precision,
    style_idio_vol_60 double precision,
    style_residual_ret_20 double precision,
    style_valuation_composite double precision,
    style_size_percentile double precision,
    style_value_percentile double precision,
    ind_ret_1d double precision,
    ind_ret_5d double precision,
    ind_ret_10d double precision,
    ind_ret_20d double precision,
    ind_vol_20 double precision,
    ind_turnover_20 double precision,
    ind_amount_20 double precision,
    ind_strength_20 double precision,
    ind_strength_60 double precision,
    ind_dispersion_20 double precision,
    ind_up_breadth_20 double precision,
    ind_down_breadth_20 double precision,
    ind_relative_volume_20 double precision,
    ind_relative_volatility_20 double precision,
    ind_relative_flow_20 double precision,
    ind_momentum_rank_20 double precision,
    ind_value_rank double precision,
    ind_size_rank double precision,
    ind_code_l1 double precision,
    ind_code_l2 double precision,
    micro_qsp_equal double precision,
    micro_esp_equal double precision,
    micro_aqsp_equal double precision,
    micro_qsp_time double precision,
    micro_esp_time double precision,
    micro_qsp_volume double precision,
    micro_esp_volume double precision,
    micro_qsp_amount double precision,
    micro_esp_amount double precision,
    micro_effective_spread double precision,
    micro_quoted_spread double precision,
    micro_spread_vol_20 double precision,
    micro_imbalance_volume double precision,
    micro_imbalance_amount double precision,
    micro_imbalance_count double precision,
    micro_imbalance_large double precision,
    micro_imbalance_medium double precision,
    micro_imbalance_small double precision,
    micro_jump_flag double precision,
    micro_pressure_score double precision,
    feature_1 double precision,
    feature_2 double precision,
    feature_3 double precision,
    feature_4 double precision,
    feature_5 double precision,
    feature_6 double precision,
    feature_7 double precision,
    feature_8 double precision,
    feature_9 double precision,
    feature_10 double precision,
    feature_11 double precision,
    feature_12 double precision,
    feature_13 double precision,
    feature_14 double precision,
    feature_15 double precision,
    feature_16 double precision,
    feature_17 double precision,
    feature_18 double precision,
    feature_19 double precision,
    feature_20 double precision,
    feature_21 double precision,
    feature_22 double precision,
    feature_23 double precision,
    feature_24 double precision,
    feature_25 double precision,
    feature_26 double precision,
    feature_27 double precision,
    feature_28 double precision,
    feature_29 double precision,
    feature_30 double precision,
    feature_31 double precision,
    feature_32 double precision,
    feature_33 double precision,
    feature_34 double precision,
    feature_35 double precision,
    feature_36 double precision,
    feature_37 double precision,
    feature_38 double precision,
    feature_39 double precision,
    feature_40 double precision,
    feature_41 double precision,
    feature_42 double precision,
    feature_43 double precision,
    feature_44 double precision,
    feature_45 double precision,
    feature_46 double precision,
    feature_47 double precision,
    feature_48 double precision,
    feature_49 double precision,
    feature_50 double precision,
    feature_51 double precision,
    feature_52 double precision,
    feature_53 double precision,
    feature_54 double precision,
    feature_55 double precision,
    feature_56 double precision,
    feature_57 double precision,
    feature_58 double precision,
    feature_59 double precision,
    feature_60 double precision,
    feature_61 double precision,
    feature_62 double precision,
    feature_63 double precision,
    feature_64 double precision,
    feature_65 double precision,
    feature_66 double precision,
    feature_67 double precision,
    feature_68 double precision,
    feature_69 double precision,
    feature_70 double precision,
    feature_71 double precision,
    feature_72 double precision,
    feature_73 double precision,
    feature_74 double precision,
    feature_75 double precision,
    feature_76 double precision,
    feature_77 double precision,
    feature_78 double precision,
    feature_79 double precision,
    feature_80 double precision,
    feature_81 double precision,
    feature_82 double precision,
    feature_83 double precision,
    feature_84 double precision,
    feature_85 double precision,
    feature_86 double precision,
    feature_87 double precision,
    feature_88 double precision,
    feature_89 double precision,
    feature_90 double precision,
    feature_91 double precision,
    feature_92 double precision,
    feature_93 double precision,
    feature_94 double precision,
    feature_95 double precision,
    feature_96 double precision,
    feature_97 double precision,
    feature_98 double precision,
    feature_99 double precision,
    feature_100 double precision,
    feature_101 double precision,
    feature_102 double precision,
    feature_103 double precision,
    feature_104 double precision,
    feature_105 double precision,
    feature_106 double precision,
    feature_107 double precision,
    feature_108 double precision,
    feature_109 double precision,
    feature_110 double precision,
    feature_111 double precision,
    feature_112 double precision,
    feature_113 double precision,
    feature_114 double precision,
    feature_115 double precision,
    feature_116 double precision,
    feature_117 double precision,
    feature_118 double precision,
    feature_119 double precision,
    feature_120 double precision,
    feature_121 double precision,
    feature_122 double precision,
    feature_123 double precision,
    feature_124 double precision,
    feature_125 double precision,
    feature_126 double precision,
    feature_127 double precision,
    feature_128 double precision,
    feature_129 double precision,
    feature_130 double precision,
    feature_131 double precision,
    feature_132 double precision,
    feature_133 double precision,
    feature_134 double precision,
    feature_135 double precision,
    feature_136 double precision,
    feature_137 double precision,
    feature_138 double precision,
    feature_139 double precision,
    feature_140 double precision,
    feature_141 double precision,
    feature_142 double precision,
    feature_143 double precision,
    feature_144 double precision,
    feature_145 double precision,
    feature_146 double precision,
    feature_147 double precision,
    feature_148 double precision,
    feature_149 double precision,
    feature_150 double precision,
    feature_151 double precision,
    CONSTRAINT ck_engine_feature_runs_dim CHECK ((feature_dim > 0)),
    CONSTRAINT ck_engine_feature_runs_status CHECK (((status)::text = ANY (ARRAY[('feature_ready'::character varying)::text, ('signal_ready'::character varying)::text, ('dispatched'::character varying)::text, ('runner_applied'::character varying)::text, ('order_sent'::character varying)::text, ('fill_confirmed'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.engine_feature_runs OWNER TO quantmind;

--
-- Name: TABLE engine_feature_runs; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.engine_feature_runs IS '特征工程运行记录：ETL特征提取任务的触发时间、状态与覆盖标的数';


--
-- Name: engine_feature_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.engine_feature_snapshots (
    id bigint NOT NULL,
    run_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(20) NOT NULL,
    model_version character varying(64) NOT NULL,
    feature_version character varying(64) NOT NULL,
    feature_dim integer NOT NULL,
    features jsonb NOT NULL,
    data_source character varying(32) DEFAULT 'l2'::character varying NOT NULL,
    is_valid boolean DEFAULT true NOT NULL,
    missing_ratio numeric(6,4),
    quality jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_engine_feature_snapshots_dim CHECK ((feature_dim > 0))
);


ALTER TABLE public.engine_feature_snapshots OWNER TO quantmind;

--
-- Name: TABLE engine_feature_snapshots; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.engine_feature_snapshots IS '特征快照存储：推理时刻的原始特征向量快照，供复现与审计';


--
-- Name: engine_feature_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.engine_feature_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.engine_feature_snapshots_id_seq OWNER TO quantmind;

--
-- Name: engine_feature_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.engine_feature_snapshots_id_seq OWNED BY public.engine_feature_snapshots.id;


--
-- Name: engine_signal_scores; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.engine_signal_scores (
    id bigint NOT NULL,
    run_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(20) NOT NULL,
    model_version character varying(64) NOT NULL,
    feature_version character varying(64) NOT NULL,
    light_score double precision,
    tft_score double precision,
    fusion_score double precision NOT NULL,
    score_rank integer,
    universe_tag character varying(32),
    signal_side character varying(8),
    expected_price double precision,
    quality jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp(6) with time zone DEFAULT now() NOT NULL,
    risk_weight double precision DEFAULT 1.0,
    regime character varying(16) DEFAULT 'normal'::character varying,
    CONSTRAINT ck_engine_signal_scores_side CHECK (((signal_side IS NULL) OR ((signal_side)::text = ANY (ARRAY[('BUY'::character varying)::text, ('SELL'::character varying)::text, ('HOLD'::character varying)::text]))))
);


ALTER TABLE public.engine_signal_scores OWNER TO quantmind;

--
-- Name: TABLE engine_signal_scores; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.engine_signal_scores IS 'AI推理信号评分：每日全市场的模型预测分数(fusion_score)，供策略调仓';


--
-- Name: COLUMN engine_signal_scores.risk_weight; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.engine_signal_scores.risk_weight IS '风控权重 [0.0, 1.5]：0=强制规避, 1=中性, 1.5=增强。由Layer3风控模型计算（新闻/宏观/政策）';


--
-- Name: COLUMN engine_signal_scores.regime; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.engine_signal_scores.regime IS '当日市场状态：normal | trending | volatile | crash。影响 LightGBM/TFT 融合权重比例';


--
-- Name: engine_signal_scores_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.engine_signal_scores_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.engine_signal_scores_id_seq OWNER TO quantmind;

--
-- Name: engine_signal_scores_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.engine_signal_scores_id_seq OWNED BY public.engine_signal_scores.id;


--
-- Name: identity_verifications; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.identity_verifications (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    real_name character varying(128) NOT NULL,
    id_number character varying(128) NOT NULL,
    document_type character varying(32),
    front_image_url character varying(512),
    back_image_url character varying(512),
    handheld_image_url character varying(512),
    status character varying(32),
    rejection_reason text,
    submitted_at timestamp(6) with time zone DEFAULT now(),
    verified_at timestamp(6) with time zone,
    verified_by character varying(64)
);


ALTER TABLE public.identity_verifications OWNER TO quantmind;

--
-- Name: TABLE identity_verifications; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.identity_verifications IS '实名认证记录：姓名、证件号、认证状态及审核结果';


--
-- Name: COLUMN identity_verifications.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.id IS 'ID';


--
-- Name: COLUMN identity_verifications.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.user_id IS '用户ID';


--
-- Name: COLUMN identity_verifications.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.tenant_id IS '租户ID';


--
-- Name: COLUMN identity_verifications.real_name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.real_name IS '真实姓名';


--
-- Name: COLUMN identity_verifications.id_number; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.id_number IS '证件号码';


--
-- Name: COLUMN identity_verifications.document_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.document_type IS '证件类型: id_card/passport';


--
-- Name: COLUMN identity_verifications.front_image_url; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.front_image_url IS '证件正面URL';


--
-- Name: COLUMN identity_verifications.back_image_url; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.back_image_url IS '证件背面URL';


--
-- Name: COLUMN identity_verifications.handheld_image_url; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.handheld_image_url IS '手持证件URL';


--
-- Name: COLUMN identity_verifications.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.status IS '状态: pending/verified/rejected';


--
-- Name: COLUMN identity_verifications.rejection_reason; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.rejection_reason IS '拒绝原因';


--
-- Name: COLUMN identity_verifications.submitted_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.submitted_at IS '提交时间';


--
-- Name: COLUMN identity_verifications.verified_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.verified_at IS '审核时间';


--
-- Name: COLUMN identity_verifications.verified_by; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.identity_verifications.verified_by IS '审核人ID';


--
-- Name: identity_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.identity_verifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1;


ALTER TABLE public.identity_verifications_id_seq OWNER TO quantmind;

--
-- Name: identity_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.identity_verifications_id_seq OWNED BY public.identity_verifications.id;


--
-- Name: index_daily; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.index_daily (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    adj_factor double precision DEFAULT 1.0,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.index_daily OWNER TO quantmind;

--
-- Name: klines; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.klines (
    id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    "interval" character varying(10) NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    open_price double precision NOT NULL,
    high_price double precision NOT NULL,
    low_price double precision NOT NULL,
    close_price double precision NOT NULL,
    volume integer NOT NULL,
    amount double precision,
    change double precision,
    change_percent double precision,
    turnover_rate double precision,
    data_source character varying(20),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.klines OWNER TO quantmind;

--
-- Name: klines_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

ALTER TABLE public.klines ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.klines_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: login_devices; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.login_devices (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    device_id character varying(128) NOT NULL,
    device_name character varying(128),
    device_type character varying(32),
    os character varying(64),
    browser character varying(64),
    ip_address character varying(64),
    location character varying(128),
    is_trusted boolean,
    is_active boolean,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone,
    last_location_change timestamp with time zone
);


ALTER TABLE public.login_devices OWNER TO quantmind;

--
-- Name: COLUMN login_devices.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.user_id IS '用户ID';


--
-- Name: COLUMN login_devices.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.tenant_id IS '租户ID';


--
-- Name: COLUMN login_devices.device_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.device_id IS '设备唯一ID';


--
-- Name: COLUMN login_devices.device_name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.device_name IS '设备名称';


--
-- Name: COLUMN login_devices.device_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.device_type IS '设备类型：mobile/desktop/tablet';


--
-- Name: COLUMN login_devices.os; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.os IS '操作系统';


--
-- Name: COLUMN login_devices.browser; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.browser IS '浏览器';


--
-- Name: COLUMN login_devices.ip_address; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.ip_address IS 'IP地址';


--
-- Name: COLUMN login_devices.location; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.location IS '地理位置';


--
-- Name: COLUMN login_devices.is_trusted; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.is_trusted IS '是否信任设备';


--
-- Name: COLUMN login_devices.is_active; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.is_active IS '是否活跃';


--
-- Name: COLUMN login_devices.last_seen_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.last_seen_at IS '最后活跃时间';


--
-- Name: COLUMN login_devices.last_location_change; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.login_devices.last_location_change IS '最后位置变化时间';


--
-- Name: login_devices_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.login_devices_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.login_devices_id_seq OWNER TO quantmind;

--
-- Name: login_devices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.login_devices_id_seq OWNED BY public.login_devices.id;


--
-- Name: market_daily_stats; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.market_daily_stats (
    trade_date date NOT NULL,
    sh_amount double precision,
    sz_amount double precision,
    total_amount double precision,
    created_at timestamp(6) without time zone DEFAULT now()
);


ALTER TABLE public.market_daily_stats OWNER TO quantmind;

--
-- Name: TABLE market_daily_stats; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.market_daily_stats IS '日行情统计汇总：涨跌停板数、成交量/额分布、市场情绪指标';


--
-- Name: market_data_daily; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.market_data_daily (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    factor double precision,
    mom_ret_1d double precision,
    mom_ret_3d double precision,
    mom_ret_5d double precision,
    mom_ret_10d double precision,
    mom_ret_20d double precision,
    mom_ret_60d double precision,
    mom_ret_120d double precision,
    mom_ma_gap_5 double precision,
    mom_ma_gap_10 double precision,
    mom_ma_gap_20 double precision,
    mom_ma_gap_60 double precision,
    mom_ma_gap_120 double precision,
    mom_ema_gap_12 double precision,
    mom_ema_gap_26 double precision,
    mom_macd_dif double precision,
    mom_macd_dea double precision,
    mom_macd_hist double precision,
    mom_rsi_6 double precision,
    mom_rsi_14 double precision,
    mom_kdj_k double precision,
    mom_kdj_d double precision,
    mom_kdj_j double precision,
    mom_roc_12 double precision,
    mom_breakout_20d double precision,
    vol_std_5 double precision,
    vol_std_10 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    vol_atr_20 double precision,
    vol_true_range double precision,
    vol_parkinson_10 double precision,
    vol_parkinson_20 double precision,
    vol_gk_10 double precision,
    vol_gk_20 double precision,
    vol_rs_10 double precision,
    vol_rs_20 double precision,
    vol_downside_20 double precision,
    vol_upside_20 double precision,
    vol_realized_rv double precision,
    vol_realized_rrv double precision,
    vol_realized_rskew double precision,
    vol_realized_rkurt double precision,
    vol_jump_zadj double precision,
    vol_jump_rjv_ratio double precision,
    vol_jump_sjv_ratio double precision,
    liq_turnover_os double precision,
    liq_turnover_tl double precision,
    liq_volume double precision,
    liq_volume_ma_5 double precision,
    liq_volume_ma_10 double precision,
    liq_volume_ma_20 double precision,
    liq_volume_ratio_5 double precision,
    liq_volume_ratio_20 double precision,
    liq_amount double precision,
    liq_amount_ma_5 double precision,
    liq_amount_ma_10 double precision,
    liq_amount_ma_20 double precision,
    liq_amount_ratio_5 double precision,
    liq_amount_ratio_20 double precision,
    liq_trade_count double precision,
    liq_avg_trade_size double precision,
    liq_obv_20 double precision,
    liq_obv_60 double precision,
    liq_mfi_14 double precision,
    liq_accdist_20 double precision,
    liq_amihud_20 double precision,
    liq_amihud_60 double precision,
    flow_net_amount double precision,
    flow_net_amount_ratio double precision,
    flow_large_net_amount double precision,
    flow_large_net_ratio double precision,
    flow_medium_net_amount double precision,
    flow_medium_net_ratio double precision,
    flow_small_net_amount double precision,
    flow_small_net_ratio double precision,
    flow_net_order_count double precision,
    flow_net_order_ratio double precision,
    flow_large_net_order double precision,
    flow_large_order_ratio double precision,
    flow_vpin double precision,
    flow_vpin_ma_5 double precision,
    flow_vpin_ma_20 double precision,
    flow_vpin_delta_5 double precision,
    flow_qsp double precision,
    flow_esp double precision,
    flow_aqsp double precision,
    flow_qsp_time double precision,
    flow_esp_time double precision,
    flow_pressure_index double precision,
    style_ln_mv_total double precision,
    style_ln_mv_float double precision,
    style_bp double precision,
    style_ep_ttm double precision,
    style_smb double precision,
    style_hml double precision,
    style_mkt_premium double precision,
    style_beta_20 double precision,
    style_beta_60 double precision,
    style_beta_120 double precision,
    style_idio_vol_20 double precision,
    style_idio_vol_60 double precision,
    style_residual_ret_20 double precision,
    style_valuation_composite double precision,
    style_size_percentile double precision,
    style_value_percentile double precision,
    ind_ret_1d double precision,
    ind_ret_5d double precision,
    ind_ret_10d double precision,
    ind_ret_20d double precision,
    ind_vol_20 double precision,
    ind_turnover_20 double precision,
    ind_amount_20 double precision,
    ind_strength_20 double precision,
    ind_strength_60 double precision,
    ind_dispersion_20 double precision,
    ind_up_breadth_20 double precision,
    ind_down_breadth_20 double precision,
    ind_relative_volume_20 double precision,
    ind_relative_volatility_20 double precision,
    ind_relative_flow_20 double precision,
    ind_momentum_rank_20 double precision,
    ind_value_rank double precision,
    ind_size_rank double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_qsp_equal double precision,
    micro_esp_equal double precision,
    micro_aqsp_equal double precision,
    micro_qsp_time double precision,
    micro_esp_time double precision,
    micro_qsp_volume double precision,
    micro_esp_volume double precision,
    micro_qsp_amount double precision,
    micro_esp_amount double precision,
    micro_effective_spread double precision,
    micro_quoted_spread double precision,
    micro_spread_vol_20 double precision,
    micro_imbalance_volume double precision,
    micro_imbalance_amount double precision,
    micro_imbalance_count double precision,
    micro_imbalance_large double precision,
    micro_imbalance_medium double precision,
    micro_imbalance_small double precision,
    micro_jump_flag double precision,
    micro_pressure_score double precision,
    label double precision,
    volume_trend_3d boolean
)
PARTITION BY RANGE (trade_date);


ALTER TABLE public.market_data_daily OWNER TO quantmind;

--
-- Name: notifications; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.notifications (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    notification_type character varying(50) NOT NULL,
    title character varying(200) NOT NULL,
    content text,
    data jsonb DEFAULT '{}'::jsonb,
    level character varying(16) DEFAULT 'info'::character varying,
    action_url character varying(512),
    is_read boolean DEFAULT false,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone
);


ALTER TABLE public.notifications OWNER TO quantmind;

--
-- Name: notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.notifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.notifications_id_seq OWNER TO quantmind;

--
-- Name: notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.notifications_id_seq OWNED BY public.notifications.id;


--
-- Name: orders; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.orders (
    id integer NOT NULL,
    order_id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    portfolio_id integer NOT NULL,
    strategy_id integer,
    symbol character varying(20) NOT NULL,
    symbol_name character varying(50),
    side public.orderside NOT NULL,
    trade_action public.tradeaction,
    position_side public.positionside NOT NULL,
    is_margin_trade boolean NOT NULL,
    order_type public.ordertype NOT NULL,
    trading_mode public.tradingmode NOT NULL,
    status public.orderstatus NOT NULL,
    quantity double precision NOT NULL,
    filled_quantity double precision NOT NULL,
    price double precision,
    stop_price double precision,
    average_price double precision,
    order_value double precision NOT NULL,
    filled_value double precision NOT NULL,
    commission double precision NOT NULL,
    submitted_at timestamp without time zone,
    filled_at timestamp without time zone,
    cancelled_at timestamp without time zone,
    expired_at timestamp without time zone,
    client_order_id character varying(100),
    exchange_order_id character varying(100),
    remarks character varying(500),
    version integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.orders OWNER TO quantmind;

--
-- Name: orders_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.orders_id_seq OWNER TO quantmind;

--
-- Name: orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.password_reset_tokens (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    email character varying(255) NOT NULL,
    token character varying(128) NOT NULL,
    is_used boolean,
    is_expired boolean,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    ip_address character varying(64),
    attempts integer
);


ALTER TABLE public.password_reset_tokens OWNER TO quantmind;

--
-- Name: COLUMN password_reset_tokens.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.password_reset_tokens.tenant_id IS '租户ID';


--
-- Name: COLUMN password_reset_tokens.attempts; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.password_reset_tokens.attempts IS '使用尝试次数';


--
-- Name: password_reset_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.password_reset_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.password_reset_tokens_id_seq OWNER TO quantmind;

--
-- Name: password_reset_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.password_reset_tokens_id_seq OWNED BY public.password_reset_tokens.id;


--
-- Name: payment_methods; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.payment_methods (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    provider character varying(32) NOT NULL,
    provider_token character varying(255),
    last4 character varying(4),
    card_type character varying(32),
    expiry_month integer,
    expiry_year integer,
    is_default integer,
    created_at timestamp(6) with time zone DEFAULT now()
);


ALTER TABLE public.payment_methods OWNER TO quantmind;

--
-- Name: TABLE payment_methods; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.payment_methods IS '支付方式：用户绑定的银行卡/支付宝/微信等支付渠道';


--
-- Name: COLUMN payment_methods.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.id IS 'ID';


--
-- Name: COLUMN payment_methods.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.user_id IS '用户ID';


--
-- Name: COLUMN payment_methods.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.tenant_id IS '租户ID';


--
-- Name: COLUMN payment_methods.provider; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.provider IS '提供商: stripe/alipay/wechat';


--
-- Name: COLUMN payment_methods.provider_token; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.provider_token IS '支付令牌/AgreementID';


--
-- Name: COLUMN payment_methods.last4; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.last4 IS '卡号后四位';


--
-- Name: COLUMN payment_methods.card_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.card_type IS '卡类型: visa/mastercard';


--
-- Name: COLUMN payment_methods.expiry_month; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.expiry_month IS '过期月';


--
-- Name: COLUMN payment_methods.expiry_year; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.expiry_year IS '过期年';


--
-- Name: COLUMN payment_methods.is_default; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.is_default IS '是否默认';


--
-- Name: COLUMN payment_methods.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_methods.created_at IS '创建时间';


--
-- Name: payment_methods_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.payment_methods_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1;


ALTER TABLE public.payment_methods_id_seq OWNER TO quantmind;

--
-- Name: payment_methods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.payment_methods_id_seq OWNED BY public.payment_methods.id;


--
-- Name: payment_transactions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.payment_transactions (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    amount numeric(10,2) NOT NULL,
    currency character varying(3),
    status character varying(32),
    subscription_id integer,
    payment_method_id integer,
    provider character varying(32) NOT NULL,
    transaction_id character varying(128),
    description character varying(255),
    metadata_info json,
    created_at timestamp(6) with time zone DEFAULT now(),
    updated_at timestamp(6) with time zone,
    completed_at timestamp(6) with time zone
);


ALTER TABLE public.payment_transactions OWNER TO quantmind;

--
-- Name: TABLE payment_transactions; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.payment_transactions IS '支付交易流水：充值/扣费记录、第三方订单号、状态';


--
-- Name: COLUMN payment_transactions.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.id IS 'ID';


--
-- Name: COLUMN payment_transactions.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.user_id IS '用户ID';


--
-- Name: COLUMN payment_transactions.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.tenant_id IS '租户ID';


--
-- Name: COLUMN payment_transactions.amount; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.amount IS '金额';


--
-- Name: COLUMN payment_transactions.currency; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.currency IS '货币';


--
-- Name: COLUMN payment_transactions.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.status IS '状态: pending/succeeded/failed/refunded';


--
-- Name: COLUMN payment_transactions.subscription_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.subscription_id IS '订阅ID';


--
-- Name: COLUMN payment_transactions.payment_method_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.payment_method_id IS '支付方式ID';


--
-- Name: COLUMN payment_transactions.provider; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.provider IS '支付提供商';


--
-- Name: COLUMN payment_transactions.transaction_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.transaction_id IS '外部交易号';


--
-- Name: COLUMN payment_transactions.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.description IS '交易描述';


--
-- Name: COLUMN payment_transactions.metadata_info; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.metadata_info IS '元数据';


--
-- Name: COLUMN payment_transactions.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.created_at IS '创建时间';


--
-- Name: COLUMN payment_transactions.updated_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.updated_at IS '更新时间';


--
-- Name: COLUMN payment_transactions.completed_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.payment_transactions.completed_at IS '完成时间';


--
-- Name: payment_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.payment_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1;


ALTER TABLE public.payment_transactions_id_seq OWNER TO quantmind;

--
-- Name: payment_transactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.payment_transactions_id_seq OWNED BY public.payment_transactions.id;


--
-- Name: permissions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.permissions (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    code character varying(128) NOT NULL,
    resource character varying(64) NOT NULL,
    action character varying(32) NOT NULL,
    description text,
    is_active boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone
);


ALTER TABLE public.permissions OWNER TO quantmind;

--
-- Name: COLUMN permissions.name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.name IS '权限名称';


--
-- Name: COLUMN permissions.code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.code IS '权限代码';


--
-- Name: COLUMN permissions.resource; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.resource IS '资源类型';


--
-- Name: COLUMN permissions.action; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.action IS '操作类型';


--
-- Name: COLUMN permissions.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.description IS '权限描述';


--
-- Name: COLUMN permissions.is_active; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.permissions.is_active IS '是否激活';


--
-- Name: permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.permissions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.permissions_id_seq OWNER TO quantmind;

--
-- Name: permissions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.permissions_id_seq OWNED BY public.permissions.id;


--
-- Name: phone_verifications; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.phone_verifications (
    id integer NOT NULL,
    phone_number character varying(32) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    verification_code character varying(32) NOT NULL,
    code_type character varying(32) NOT NULL,
    is_used boolean,
    is_expired boolean,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    attempts integer,
    ip_address character varying(64)
);


ALTER TABLE public.phone_verifications OWNER TO quantmind;

--
-- Name: TABLE phone_verifications; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.phone_verifications IS '手机验证码：注册/改绑手机时的OTP令牌与有效期';


--
-- Name: COLUMN phone_verifications.phone_number; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.phone_number IS '手机号';


--
-- Name: COLUMN phone_verifications.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.tenant_id IS '租户ID';


--
-- Name: COLUMN phone_verifications.verification_code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.verification_code IS '验证码';


--
-- Name: COLUMN phone_verifications.code_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.code_type IS '类型: register/login/reset_password/bind_phone/change_phone_old/change_phone_new';


--
-- Name: COLUMN phone_verifications.is_used; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.is_used IS '是否已使用';


--
-- Name: COLUMN phone_verifications.is_expired; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.is_expired IS '是否过期';


--
-- Name: COLUMN phone_verifications.attempts; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.phone_verifications.attempts IS '验证尝试次数';


--
-- Name: phone_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.phone_verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.phone_verifications_id_seq OWNER TO quantmind;

--
-- Name: phone_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.phone_verifications_id_seq OWNED BY public.phone_verifications.id;


--
-- Name: pipeline_runs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.pipeline_runs (
    run_id text NOT NULL,
    user_id text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    status text NOT NULL,
    stage text NOT NULL,
    error_message text,
    created_at timestamp(6) with time zone NOT NULL,
    updated_at timestamp(6) with time zone NOT NULL,
    request_json jsonb,
    result_json jsonb
);


ALTER TABLE public.pipeline_runs OWNER TO quantmind;

--
-- Name: TABLE pipeline_runs; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.pipeline_runs IS 'AI策略Pipeline运行记录：从特征工程到信号生成的全流程追踪';


--
-- Name: portfolio_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.portfolio_snapshots (
    id integer NOT NULL,
    portfolio_id integer NOT NULL,
    snapshot_date timestamp without time zone NOT NULL,
    total_value numeric(20,2) NOT NULL,
    available_cash numeric(20,2) NOT NULL,
    market_value numeric(20,2) NOT NULL,
    total_pnl numeric(20,2) NOT NULL,
    total_return numeric(10,4) NOT NULL,
    daily_pnl numeric(20,2) NOT NULL,
    daily_return numeric(10,4) NOT NULL,
    max_drawdown numeric(10,4) NOT NULL,
    sharpe_ratio numeric(10,4),
    volatility numeric(10,4),
    position_count integer NOT NULL,
    is_settlement boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.portfolio_snapshots OWNER TO quantmind;

--
-- Name: COLUMN portfolio_snapshots.snapshot_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.snapshot_date IS '快照日期';


--
-- Name: COLUMN portfolio_snapshots.total_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.total_value IS '总市值';


--
-- Name: COLUMN portfolio_snapshots.available_cash; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.available_cash IS '可用现金';


--
-- Name: COLUMN portfolio_snapshots.market_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.market_value IS '持仓市值';


--
-- Name: COLUMN portfolio_snapshots.total_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.total_pnl IS '总盈亏';


--
-- Name: COLUMN portfolio_snapshots.total_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.total_return IS '总收益率';


--
-- Name: COLUMN portfolio_snapshots.daily_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.daily_pnl IS '日盈亏';


--
-- Name: COLUMN portfolio_snapshots.daily_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.daily_return IS '日收益率';


--
-- Name: COLUMN portfolio_snapshots.max_drawdown; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.max_drawdown IS '最大回撤';


--
-- Name: COLUMN portfolio_snapshots.sharpe_ratio; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.sharpe_ratio IS '夏普比率';


--
-- Name: COLUMN portfolio_snapshots.volatility; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.volatility IS '波动率';


--
-- Name: COLUMN portfolio_snapshots.position_count; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.position_count IS '持仓数量';


--
-- Name: COLUMN portfolio_snapshots.is_settlement; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolio_snapshots.is_settlement IS '是否为结算快照';


--
-- Name: portfolio_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.portfolio_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.portfolio_snapshots_id_seq OWNER TO quantmind;

--
-- Name: portfolio_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.portfolio_snapshots_id_seq OWNED BY public.portfolio_snapshots.id;


--
-- Name: portfolios; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.portfolios (
    id integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    initial_capital numeric(20,2) NOT NULL,
    current_capital numeric(20,2) NOT NULL,
    available_cash numeric(20,2) NOT NULL,
    frozen_cash numeric(20,2) NOT NULL,
    total_value numeric(20,2) NOT NULL,
    total_pnl numeric(20,2) NOT NULL,
    total_return numeric(10,4) NOT NULL,
    daily_pnl numeric(20,2) NOT NULL,
    daily_return numeric(10,4) NOT NULL,
    yesterday_total_value numeric(20,2) NOT NULL,
    max_drawdown numeric(10,4) NOT NULL,
    sharpe_ratio numeric(10,4),
    volatility numeric(10,4),
    status character varying(20) NOT NULL,
    trading_mode public.tradingmode NOT NULL,
    broker_type character varying(32),
    broker_account_id character varying(64),
    broker_params json,
    strategy_id integer,
    real_trading_id character varying(50),
    run_status character varying(20) NOT NULL,
    is_deleted boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT check_available_cash_positive CHECK ((available_cash >= (0)::numeric)),
    CONSTRAINT check_initial_capital_positive CHECK ((initial_capital >= (0)::numeric))
);


ALTER TABLE public.portfolios OWNER TO quantmind;

--
-- Name: COLUMN portfolios.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.tenant_id IS '租户ID';


--
-- Name: COLUMN portfolios.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.user_id IS '用户ID';


--
-- Name: COLUMN portfolios.name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.name IS '组合名称';


--
-- Name: COLUMN portfolios.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.description IS '组合描述';


--
-- Name: COLUMN portfolios.initial_capital; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.initial_capital IS '初始资金';


--
-- Name: COLUMN portfolios.current_capital; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.current_capital IS '当前资金';


--
-- Name: COLUMN portfolios.available_cash; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.available_cash IS '可用现金';


--
-- Name: COLUMN portfolios.frozen_cash; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.frozen_cash IS '冻结资金';


--
-- Name: COLUMN portfolios.total_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.total_value IS '总市值';


--
-- Name: COLUMN portfolios.total_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.total_pnl IS '总盈亏';


--
-- Name: COLUMN portfolios.total_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.total_return IS '总收益率';


--
-- Name: COLUMN portfolios.daily_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.daily_pnl IS '日盈亏';


--
-- Name: COLUMN portfolios.daily_return; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.daily_return IS '日收益率';


--
-- Name: COLUMN portfolios.yesterday_total_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.yesterday_total_value IS '昨日结算总资产';


--
-- Name: COLUMN portfolios.max_drawdown; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.max_drawdown IS '最大回撤';


--
-- Name: COLUMN portfolios.sharpe_ratio; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.sharpe_ratio IS '夏普比率';


--
-- Name: COLUMN portfolios.volatility; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.volatility IS '波动率';


--
-- Name: COLUMN portfolios.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.status IS '状态';


--
-- Name: COLUMN portfolios.trading_mode; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.trading_mode IS '交易模式：实盘 / 模拟盘';


--
-- Name: COLUMN portfolios.broker_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.broker_type IS '券商类型 (如 QMT/Paper)';


--
-- Name: COLUMN portfolios.broker_account_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.broker_account_id IS '券商资金账号';


--
-- Name: COLUMN portfolios.broker_params; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.broker_params IS '券商配置参数';


--
-- Name: COLUMN portfolios.strategy_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.strategy_id IS '关联策略ID';


--
-- Name: COLUMN portfolios.real_trading_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.real_trading_id IS '实盘引擎部署ID';


--
-- Name: COLUMN portfolios.run_status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.run_status IS '运行状态';


--
-- Name: COLUMN portfolios.is_deleted; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.portfolios.is_deleted IS '是否删除';


--
-- Name: portfolios_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.portfolios_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.portfolios_id_seq OWNER TO quantmind;

--
-- Name: portfolios_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.portfolios_id_seq OWNED BY public.portfolios.id;


--
-- Name: position_history; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.position_history (
    id integer NOT NULL,
    position_id integer NOT NULL,
    action character varying(20) NOT NULL,
    quantity_change integer NOT NULL,
    price numeric(20,4) NOT NULL,
    amount numeric(20,2) NOT NULL,
    quantity_after integer NOT NULL,
    avg_cost_after numeric(20,4) NOT NULL,
    note text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.position_history OWNER TO quantmind;

--
-- Name: COLUMN position_history.action; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.action IS '操作';


--
-- Name: COLUMN position_history.quantity_change; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.quantity_change IS '数量变化';


--
-- Name: COLUMN position_history.price; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.price IS '价格';


--
-- Name: COLUMN position_history.amount; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.amount IS '金额';


--
-- Name: COLUMN position_history.quantity_after; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.quantity_after IS '变更后数量';


--
-- Name: COLUMN position_history.avg_cost_after; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.avg_cost_after IS '变更后均价';


--
-- Name: COLUMN position_history.note; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.position_history.note IS '备注';


--
-- Name: position_history_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.position_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.position_history_id_seq OWNER TO quantmind;

--
-- Name: position_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.position_history_id_seq OWNED BY public.position_history.id;


--
-- Name: positions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.positions (
    id integer NOT NULL,
    portfolio_id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    symbol_name character varying(100),
    exchange character varying(20),
    side character varying(20) NOT NULL,
    quantity integer NOT NULL,
    available_quantity integer NOT NULL,
    frozen_quantity integer NOT NULL,
    avg_cost numeric(20,4) NOT NULL,
    total_cost numeric(20,2) NOT NULL,
    current_price numeric(20,4) NOT NULL,
    market_value numeric(20,2) NOT NULL,
    unrealized_pnl numeric(20,2) NOT NULL,
    unrealized_pnl_rate numeric(10,4) NOT NULL,
    realized_pnl numeric(20,2) NOT NULL,
    weight numeric(10,4) NOT NULL,
    status character varying(20) NOT NULL,
    opened_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    closed_at timestamp without time zone,
    CONSTRAINT check_available_quantity_positive CHECK ((available_quantity >= 0)),
    CONSTRAINT check_quantity_positive CHECK ((quantity >= 0))
);


ALTER TABLE public.positions OWNER TO quantmind;

--
-- Name: COLUMN positions.symbol; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.symbol IS '证券代码';


--
-- Name: COLUMN positions.symbol_name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.symbol_name IS '证券名称';


--
-- Name: COLUMN positions.exchange; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.exchange IS '交易所';


--
-- Name: COLUMN positions.side; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.side IS '持仓方向';


--
-- Name: COLUMN positions.quantity; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.quantity IS '持仓数量';


--
-- Name: COLUMN positions.available_quantity; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.available_quantity IS '可用数量';


--
-- Name: COLUMN positions.frozen_quantity; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.frozen_quantity IS '冻结数量';


--
-- Name: COLUMN positions.avg_cost; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.avg_cost IS '平均成本';


--
-- Name: COLUMN positions.total_cost; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.total_cost IS '总成本';


--
-- Name: COLUMN positions.current_price; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.current_price IS '当前价格';


--
-- Name: COLUMN positions.market_value; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.market_value IS '市值';


--
-- Name: COLUMN positions.unrealized_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.unrealized_pnl IS '浮动盈亏';


--
-- Name: COLUMN positions.unrealized_pnl_rate; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.unrealized_pnl_rate IS '浮动盈亏率';


--
-- Name: COLUMN positions.realized_pnl; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.realized_pnl IS '已实现盈亏';


--
-- Name: COLUMN positions.weight; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.weight IS '仓位权重';


--
-- Name: COLUMN positions.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.status IS '状态';


--
-- Name: COLUMN positions.opened_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.opened_at IS '开仓时间';


--
-- Name: COLUMN positions.closed_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.positions.closed_at IS '平仓时间';


--
-- Name: positions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.positions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.positions_id_seq OWNER TO quantmind;

--
-- Name: positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.positions_id_seq OWNED BY public.positions.id;


--
-- Name: qlib_backtest_runs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qlib_backtest_runs (
    id character varying(64),
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    strategy_id character varying(64),
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    result jsonb,
    error_message text,
    task_id character varying(64),
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    execution_time_seconds double precision,
    created_at timestamp with time zone DEFAULT now(),
    result_file_path text,
    result_cos_key text,
    result_cos_url text,
    result_backup_status text DEFAULT 'none'::text NOT NULL,
    result_backup_at timestamp with time zone,
    config_json jsonb,
    result_json jsonb,
    backtest_id text
);


ALTER TABLE public.qlib_backtest_runs OWNER TO quantmind;

--
-- Name: qlib_backtest_runs_cleanup_backup; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qlib_backtest_runs_cleanup_backup (
    backtest_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL,
    created_at timestamp(6) with time zone NOT NULL,
    completed_at timestamp(6) with time zone,
    config_json jsonb,
    result_json jsonb,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    task_id text,
    result_file_path text,
    result_cos_key text,
    result_cos_url text,
    result_backup_status text DEFAULT 'none'::text NOT NULL,
    result_backup_at timestamp(6) with time zone,
    backup_batch text,
    backup_reason text,
    backup_at timestamp(6) with time zone
);


ALTER TABLE public.qlib_backtest_runs_cleanup_backup OWNER TO quantmind;

--
-- Name: qlib_optimization_runs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qlib_optimization_runs (
    optimization_id text NOT NULL,
    task_id text,
    mode text NOT NULL,
    user_id text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    status text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    base_request_json jsonb,
    config_snapshot_json jsonb,
    optimization_target text,
    param_ranges_json jsonb,
    total_tasks integer DEFAULT 0 NOT NULL,
    completed_count integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    current_params_json jsonb,
    best_params_json jsonb,
    best_metric_value double precision,
    result_summary_json jsonb,
    all_results_json jsonb,
    error_message text
);


ALTER TABLE public.qlib_optimization_runs OWNER TO quantmind;

--
-- Name: qm_market_calendar_day; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_market_calendar_day (
    market character varying(32) NOT NULL,
    trade_date date NOT NULL,
    is_trading_day boolean NOT NULL,
    timezone character varying(64) DEFAULT 'Asia/Shanghai'::character varying NOT NULL,
    source character varying(64) DEFAULT 'manual'::character varying NOT NULL,
    version character varying(64),
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    user_id character varying(64) DEFAULT '*'::character varying NOT NULL,
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.qm_market_calendar_day OWNER TO quantmind;

--
-- Name: qm_model_inference_runs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_model_inference_runs (
    run_id text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    model_id text NOT NULL,
    data_trade_date date NOT NULL,
    prediction_trade_date date NOT NULL,
    status text NOT NULL,
    signals_count integer DEFAULT 0 NOT NULL,
    duration_ms integer,
    fallback_used boolean DEFAULT false NOT NULL,
    fallback_reason text,
    failure_stage text,
    error_message text,
    stdout text,
    stderr text,
    active_model_id text,
    effective_model_id text,
    model_source text,
    active_data_source text,
    request_json jsonb,
    result_json jsonb,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.qm_model_inference_runs OWNER TO quantmind;

--
-- Name: qm_model_inference_settings; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_model_inference_settings (
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    model_id text NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    schedule_time text DEFAULT '09:30'::text NOT NULL,
    last_run_id text,
    last_run_json jsonb,
    next_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.qm_model_inference_settings OWNER TO quantmind;

--
-- Name: qm_research_candidate_snapshot; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_research_candidate_snapshot (
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    run_id text NOT NULL,
    model_id text NOT NULL,
    data_trade_date date,
    prediction_trade_date date NOT NULL,
    symbol text NOT NULL,
    fusion_score double precision,
    light_score double precision,
    tft_score double precision,
    score_rank integer,
    signal_side text,
    expected_price double precision,
    universe_tag text,
    quality jsonb DEFAULT '{}'::jsonb NOT NULL,
    confidence_level text,
    confidence_score double precision,
    hit_reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    risk_flags jsonb DEFAULT '[]'::jsonb NOT NULL,
    thesis_summary text,
    source_updated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.qm_research_candidate_snapshot OWNER TO quantmind;

--
-- Name: qm_research_import_state; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_research_import_state (
    job_name text NOT NULL,
    last_source_updated_at timestamp with time zone DEFAULT '1970-01-01 00:00:00+00'::timestamp with time zone NOT NULL,
    last_prediction_trade_date date,
    last_run_id text,
    extra_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.qm_research_import_state OWNER TO quantmind;

--
-- Name: qm_strategy_model_bindings; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_strategy_model_bindings (
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    strategy_id character varying(128) NOT NULL,
    model_id character varying(128) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.qm_strategy_model_bindings OWNER TO quantmind;

--
-- Name: qm_user_models; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_user_models (
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    model_id character varying(128) NOT NULL,
    source_run_id character varying(64),
    status character varying(32) DEFAULT 'candidate'::character varying NOT NULL,
    storage_path text,
    model_file character varying(255),
    metadata_json jsonb,
    metrics_json jsonb,
    is_default boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone
);


ALTER TABLE public.qm_user_models OWNER TO quantmind;

--
-- Name: qm_user_research_pool; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_user_research_pool (
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    symbol text NOT NULL,
    stock_name text,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    source_run_id text,
    model_id text,
    fusion_score double precision,
    thesis_summary text,
    status text DEFAULT 'pending'::text NOT NULL,
    notes text,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    features_snapshot jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT chk_pool_status_valid CHECK ((status = ANY (ARRAY['pending'::text, 'confirmed'::text, 'rejected'::text])))
);


ALTER TABLE public.qm_user_research_pool OWNER TO quantmind;

--
-- Name: qm_user_watchlist; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qm_user_watchlist (
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    symbol text NOT NULL,
    stock_name text,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    source_run_id text,
    notes text,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    features_snapshot jsonb DEFAULT '{}'::jsonb
);


ALTER TABLE public.qm_user_watchlist OWNER TO quantmind;

--
-- Name: qmt_agent_bindings; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qmt_agent_bindings (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    api_key_id integer NOT NULL,
    agent_type character varying(32) NOT NULL,
    account_id character varying(64) NOT NULL,
    client_fingerprint character varying(255) NOT NULL,
    hostname character varying(255),
    client_version character varying(64),
    status character varying(32) NOT NULL,
    last_ip character varying(64),
    last_seen_at timestamp with time zone,
    bound_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.qmt_agent_bindings OWNER TO quantmind;

--
-- Name: qmt_agent_sessions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.qmt_agent_sessions (
    id character varying(64) NOT NULL,
    binding_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.qmt_agent_sessions OWNER TO quantmind;

--
-- Name: quote_daily_summaries; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.quote_daily_summaries (
    id integer NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(20) NOT NULL,
    data_source character varying(20) DEFAULT 'remote_redis'::character varying NOT NULL,
    open_price double precision,
    high_price double precision,
    low_price double precision,
    close_price double precision,
    avg_price double precision,
    volume_sum bigint DEFAULT 0,
    amount_sum double precision DEFAULT 0,
    quote_count integer DEFAULT 0,
    first_quote_at timestamp with time zone,
    last_quote_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.quote_daily_summaries OWNER TO quantmind;

--
-- Name: quote_daily_summaries_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

ALTER TABLE public.quote_daily_summaries ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.quote_daily_summaries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: quotes; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.quotes (
    id integer NOT NULL,
    symbol text NOT NULL,
    trade_time timestamp with time zone,
    price numeric(12,4),
    volume bigint,
    bid_price numeric(12,4),
    ask_price numeric(12,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now(),
    current_price double precision,
    open_price double precision,
    high_price double precision,
    low_price double precision,
    close_price double precision,
    amount double precision,
    change double precision,
    change_percent double precision,
    data_source character varying(20)
);


ALTER TABLE public.quotes OWNER TO quantmind;

--
-- Name: quotes_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

ALTER TABLE public.quotes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.quotes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: real_account_baselines; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.real_account_baselines (
    id bigint NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    account_id character varying(64) NOT NULL,
    initial_equity double precision NOT NULL,
    first_snapshot_at timestamp without time zone,
    source character varying(32) DEFAULT 'qmt_bridge_first_report'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.real_account_baselines OWNER TO quantmind;

--
-- Name: real_account_baselines_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.real_account_baselines_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.real_account_baselines_id_seq OWNER TO quantmind;

--
-- Name: real_account_baselines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.real_account_baselines_id_seq OWNED BY public.real_account_baselines.id;


--
-- Name: real_account_ledger_daily_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.real_account_ledger_daily_snapshots (
    id integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    snapshot_date date NOT NULL,
    last_snapshot_at timestamp without time zone NOT NULL,
    initial_equity double precision NOT NULL,
    day_open_equity double precision NOT NULL,
    month_open_equity double precision NOT NULL,
    total_asset double precision NOT NULL,
    cash double precision NOT NULL,
    market_value double precision NOT NULL,
    today_pnl_raw double precision NOT NULL,
    monthly_pnl_raw double precision NOT NULL,
    total_pnl_raw double precision NOT NULL,
    floating_pnl_raw double precision NOT NULL,
    daily_return_pct double precision NOT NULL,
    total_return_pct double precision NOT NULL,
    position_count integer NOT NULL,
    source character varying(32) NOT NULL,
    payload_json json NOT NULL
);


ALTER TABLE public.real_account_ledger_daily_snapshots OWNER TO quantmind;

--
-- Name: real_account_ledger_daily_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.real_account_ledger_daily_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.real_account_ledger_daily_snapshots_id_seq OWNER TO quantmind;

--
-- Name: real_account_ledger_daily_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.real_account_ledger_daily_snapshots_id_seq OWNED BY public.real_account_ledger_daily_snapshots.id;


--
-- Name: real_account_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.real_account_snapshots (
    id integer NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    account_id character varying(64) NOT NULL,
    snapshot_at timestamp without time zone NOT NULL,
    snapshot_date date NOT NULL,
    snapshot_month character varying(7) NOT NULL,
    total_asset double precision NOT NULL,
    cash double precision NOT NULL,
    market_value double precision NOT NULL,
    today_pnl_raw double precision NOT NULL,
    total_pnl_raw double precision NOT NULL,
    floating_pnl_raw double precision NOT NULL,
    source character varying(32) NOT NULL,
    payload_json json NOT NULL
);


ALTER TABLE public.real_account_snapshots OWNER TO quantmind;

--
-- Name: real_account_snapshot_overview_v; Type: VIEW; Schema: public; Owner: quantmind
--

CREATE VIEW public.real_account_snapshot_overview_v AS
 SELECT real_account_snapshots.id,
    real_account_snapshots.tenant_id,
    real_account_snapshots.user_id,
    real_account_snapshots.account_id,
    real_account_snapshots.snapshot_at,
    real_account_snapshots.snapshot_date,
    real_account_snapshots.snapshot_month,
    real_account_snapshots.total_asset,
    real_account_snapshots.cash,
    real_account_snapshots.market_value,
    real_account_snapshots.today_pnl_raw,
    real_account_snapshots.total_pnl_raw,
    real_account_snapshots.floating_pnl_raw,
    real_account_snapshots.source,
    real_account_snapshots.payload_json,
    COALESCE(( SELECT ras.total_asset
           FROM public.real_account_snapshots ras
          WHERE (((ras.tenant_id)::text = (real_account_snapshots.tenant_id)::text) AND ((ras.user_id)::text = (real_account_snapshots.user_id)::text) AND ((ras.account_id)::text = (real_account_snapshots.account_id)::text))
          ORDER BY ras.snapshot_at
         LIMIT 1), real_account_snapshots.total_asset) AS initial_equity,
    COALESCE(( SELECT ras.total_asset
           FROM public.real_account_snapshots ras
          WHERE (((ras.tenant_id)::text = (real_account_snapshots.tenant_id)::text) AND ((ras.user_id)::text = (real_account_snapshots.user_id)::text) AND ((ras.account_id)::text = (real_account_snapshots.account_id)::text) AND (ras.snapshot_date = real_account_snapshots.snapshot_date))
          ORDER BY ras.snapshot_at
         LIMIT 1), real_account_snapshots.total_asset) AS day_open_equity,
    COALESCE(( SELECT ras.total_asset
           FROM public.real_account_snapshots ras
          WHERE (((ras.tenant_id)::text = (real_account_snapshots.tenant_id)::text) AND ((ras.user_id)::text = (real_account_snapshots.user_id)::text) AND ((ras.account_id)::text = (real_account_snapshots.account_id)::text) AND ((ras.snapshot_month)::text = (real_account_snapshots.snapshot_month)::text))
          ORDER BY ras.snapshot_at
         LIMIT 1), real_account_snapshots.total_asset) AS month_open_equity
   FROM public.real_account_snapshots;


ALTER TABLE public.real_account_snapshot_overview_v OWNER TO quantmind;

--
-- Name: real_account_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.real_account_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.real_account_snapshots_id_seq OWNER TO quantmind;

--
-- Name: real_account_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.real_account_snapshots_id_seq OWNED BY public.real_account_snapshots.id;


--
-- Name: real_trading_preflight_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.real_trading_preflight_snapshots (
    id integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    trading_mode character varying(16) NOT NULL,
    snapshot_date date NOT NULL,
    ready boolean NOT NULL,
    total_checks integer NOT NULL,
    passed_checks integer NOT NULL,
    required_failed_count integer NOT NULL,
    run_count integer NOT NULL,
    failed_required_keys json NOT NULL,
    checks json NOT NULL,
    source character varying(32) NOT NULL,
    last_checked_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.real_trading_preflight_snapshots OWNER TO quantmind;

--
-- Name: real_trading_preflight_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.real_trading_preflight_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.real_trading_preflight_snapshots_id_seq OWNER TO quantmind;

--
-- Name: real_trading_preflight_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.real_trading_preflight_snapshots_id_seq OWNED BY public.real_trading_preflight_snapshots.id;


--
-- Name: risk_rules; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.risk_rules (
    id integer NOT NULL,
    rule_name character varying(100) NOT NULL,
    rule_type character varying(50) NOT NULL,
    description character varying(500),
    is_active boolean NOT NULL,
    parameters json NOT NULL,
    applies_to_all boolean NOT NULL,
    user_ids json,
    priority integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.risk_rules OWNER TO quantmind;

--
-- Name: risk_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.risk_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.risk_rules_id_seq OWNER TO quantmind;

--
-- Name: risk_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.risk_rules_id_seq OWNED BY public.risk_rules.id;


--
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.role_permissions (
    role_id integer NOT NULL,
    permission_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.role_permissions OWNER TO quantmind;

--
-- Name: roles; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.roles (
    id integer NOT NULL,
    name character varying(64) NOT NULL,
    code character varying(64) NOT NULL,
    description text,
    is_active boolean,
    is_system boolean,
    priority integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone
);


ALTER TABLE public.roles OWNER TO quantmind;

--
-- Name: COLUMN roles.name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.name IS '角色名称';


--
-- Name: COLUMN roles.code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.code IS '角色代码';


--
-- Name: COLUMN roles.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.description IS '角色描述';


--
-- Name: COLUMN roles.is_active; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.is_active IS '是否激活';


--
-- Name: COLUMN roles.is_system; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.is_system IS '是否系统角色';


--
-- Name: COLUMN roles.priority; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.roles.priority IS '优先级';


--
-- Name: roles_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.roles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.roles_id_seq OWNER TO quantmind;

--
-- Name: roles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.roles_id_seq OWNED BY public.roles.id;


--
-- Name: sim_orders; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.sim_orders (
    id character varying(64) NOT NULL,
    job_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    symbol character varying(20) NOT NULL,
    side public.orderside NOT NULL,
    order_type public.ordertype DEFAULT 'market'::public.ordertype NOT NULL,
    quantity numeric(18,4) NOT NULL,
    price numeric(18,4),
    status public.orderstatus DEFAULT 'pending'::public.orderstatus NOT NULL,
    filled_quantity numeric(18,4) DEFAULT 0,
    filled_price numeric(18,4),
    commission numeric(18,4) DEFAULT 0,
    signal_time timestamp with time zone,
    submit_time timestamp with time zone,
    fill_time timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    order_id uuid DEFAULT gen_random_uuid(),
    portfolio_id integer DEFAULT 0,
    strategy_id integer,
    trading_mode character varying(32) DEFAULT 'simulation'::character varying,
    average_price numeric(18,4),
    order_value numeric(18,4) DEFAULT 0,
    filled_value numeric(18,4) DEFAULT 0,
    submitted_at timestamp with time zone,
    filled_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    execution_model character varying(32) DEFAULT 'synthetic_price'::character varying,
    price_source character varying(64),
    remarks character varying(500),
    version integer DEFAULT 1,
    total_fee numeric(18,4) DEFAULT 0,
    updated_at timestamp with time zone
);


ALTER TABLE public.sim_orders OWNER TO quantmind;

--
-- Name: sim_trades; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.sim_trades (
    id character varying(64) NOT NULL,
    job_id character varying(64) NOT NULL,
    order_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    symbol character varying(20) NOT NULL,
    side public.orderside NOT NULL,
    quantity numeric(18,4) NOT NULL,
    price numeric(18,4) NOT NULL,
    commission numeric(18,4) DEFAULT 0,
    trade_time timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.sim_trades OWNER TO quantmind;

--
-- Name: simulation_daily_reports; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.simulation_daily_reports (
    id integer NOT NULL,
    job_id uuid NOT NULL,
    date date NOT NULL,
    total_asset double precision NOT NULL,
    return_rate double precision NOT NULL,
    holdings_snapshot json,
    created_at timestamp(6) without time zone
);


ALTER TABLE public.simulation_daily_reports OWNER TO quantmind;

--
-- Name: TABLE simulation_daily_reports; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.simulation_daily_reports IS '模拟盘每日报告：日收益率、累计净值、夏普等绩效指标';


--
-- Name: simulation_daily_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.simulation_daily_reports_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 2147483647
    CACHE 1;


ALTER TABLE public.simulation_daily_reports_id_seq OWNER TO quantmind;

--
-- Name: simulation_daily_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.simulation_daily_reports_id_seq OWNED BY public.simulation_daily_reports.id;


--
-- Name: simulation_fund_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.simulation_fund_snapshots (
    id integer NOT NULL,
    tenant_id character varying(50) NOT NULL,
    user_id character varying(50) NOT NULL,
    snapshot_date date NOT NULL,
    total_asset double precision NOT NULL DEFAULT 0.0,
    available_balance double precision NOT NULL DEFAULT 0.0,
    frozen_balance double precision NOT NULL DEFAULT 0.0,
    market_value double precision NOT NULL DEFAULT 0.0,
    initial_capital double precision NOT NULL DEFAULT 0.0,
    total_pnl double precision NOT NULL DEFAULT 0.0,
    today_pnl double precision NOT NULL DEFAULT 0.0,
    source character varying(64) NOT NULL DEFAULT 'redis_simulation_account'::character varying,
    updated_at timestamp without time zone NOT NULL DEFAULT now()
);


ALTER TABLE public.simulation_fund_snapshots OWNER TO quantmind;

--
-- Name: simulation_fund_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.simulation_fund_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.simulation_fund_snapshots_id_seq OWNER TO quantmind;

--
-- Name: simulation_fund_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.simulation_fund_snapshots_id_seq OWNED BY public.simulation_fund_snapshots.id;


--
-- Name: simulation_jobs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.simulation_jobs (
    id uuid NOT NULL,
    tenant_id character varying NOT NULL,
    user_id integer NOT NULL,
    model_id character varying NOT NULL,
    strategy_config json,
    initial_capital double precision NOT NULL,
    current_cash double precision NOT NULL,
    start_date date NOT NULL,
    last_run_date date,
    status public.simulationstatus,
    execution_logs json,
    created_at timestamp(6) without time zone,
    updated_at timestamp(6) without time zone
);


ALTER TABLE public.simulation_jobs OWNER TO quantmind;

--
-- Name: TABLE simulation_jobs; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.simulation_jobs IS '模拟盘任务：模拟账户的策略绑定、初始资金、运行周期配置';


--
-- Name: simulation_positions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.simulation_positions (
    id integer NOT NULL,
    job_id character varying(64) NOT NULL,
    symbol character varying(20) NOT NULL,
    side public.positionside NOT NULL,
    quantity numeric(18,4) DEFAULT 0 NOT NULL,
    avg_cost numeric(18,4),
    market_value numeric(18,4),
    unrealized_pnl numeric(18,4),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.simulation_positions OWNER TO quantmind;

--
-- Name: simulation_positions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.simulation_positions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.simulation_positions_id_seq OWNER TO quantmind;

--
-- Name: simulation_positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.simulation_positions_id_seq OWNED BY public.simulation_positions.id;


--
-- Name: stock_daily_latest; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_daily_latest (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
)
PARTITION BY RANGE (trade_date);


ALTER TABLE public.stock_daily_latest OWNER TO quantmind;

--
-- Name: TABLE stock_daily_latest; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.stock_daily_latest IS '股票日线行情最新数据（分区表），包含行情、技术指标、基本面、概念标签等综合字段';


--
-- Name: COLUMN stock_daily_latest.trade_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.trade_date IS '交易日期';


--
-- Name: COLUMN stock_daily_latest.symbol; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.symbol IS '股票代码（前缀格式，如 SH600000）';


--
-- Name: COLUMN stock_daily_latest.stock_name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.stock_name IS '股票名称';


--
-- Name: COLUMN stock_daily_latest.open; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.open IS '开盘价（后复权）';


--
-- Name: COLUMN stock_daily_latest.high; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.high IS '最高价（后复权）';


--
-- Name: COLUMN stock_daily_latest.low; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.low IS '最低价（后复权）';


--
-- Name: COLUMN stock_daily_latest.close; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.close IS '收盘价（后复权）';


--
-- Name: COLUMN stock_daily_latest.volume; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume IS '成交量（手）';


--
-- Name: COLUMN stock_daily_latest.amount; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.amount IS '成交额（元）';


--
-- Name: COLUMN stock_daily_latest.pct_change; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.pct_change IS '涨跌幅（百分比数值，如 -0.65 表示 -0.65%）';


--
-- Name: COLUMN stock_daily_latest.turnover_rate; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.turnover_rate IS '换手率（%）';


--
-- Name: COLUMN stock_daily_latest.pe_ttm; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.pe_ttm IS '市盈率 TTM';


--
-- Name: COLUMN stock_daily_latest.pb; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.pb IS '市净率';


--
-- Name: COLUMN stock_daily_latest.total_mv; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.total_mv IS '总市值（元）';


--
-- Name: COLUMN stock_daily_latest.float_mv; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.float_mv IS '流通市值（元）';


--
-- Name: COLUMN stock_daily_latest.listed_days; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.listed_days IS '上市天数';


--
-- Name: COLUMN stock_daily_latest.is_st; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.is_st IS '是否 ST 股（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.listing_market; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.listing_market IS '上市市场（SH/SZ/BJ）';


--
-- Name: COLUMN stock_daily_latest.industry; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.industry IS '所属行业（申万一级）';


--
-- Name: COLUMN stock_daily_latest.province; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.province IS '所属省份';


--
-- Name: COLUMN stock_daily_latest.consecutive_limit_up_days; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.consecutive_limit_up_days IS '连续涨停天数';


--
-- Name: COLUMN stock_daily_latest.limit_up_today; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.limit_up_today IS '今日是否涨停（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.limit_down_today; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.limit_down_today IS '今日是否跌停（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.return_1d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_1d IS '次日收益率（小数比例，如 0.05 表示 5%）';


--
-- Name: COLUMN stock_daily_latest.return_3d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_3d IS '3日收益率（小数比例）';


--
-- Name: COLUMN stock_daily_latest.return_5d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_5d IS '5日收益率（小数比例）';


--
-- Name: COLUMN stock_daily_latest.return_10d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_10d IS '10日收益率（小数比例）';


--
-- Name: COLUMN stock_daily_latest.return_20d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_20d IS '20日收益率（小数比例）';


--
-- Name: COLUMN stock_daily_latest.return_60d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.return_60d IS '60日收益率（小数比例）';


--
-- Name: COLUMN stock_daily_latest.ma5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma5 IS '5日均线';


--
-- Name: COLUMN stock_daily_latest.ma10; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma10 IS '10日均线';


--
-- Name: COLUMN stock_daily_latest.ma20; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma20 IS '20日均线';


--
-- Name: COLUMN stock_daily_latest.ma60; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma60 IS '60日均线';


--
-- Name: COLUMN stock_daily_latest.ma_gap_5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma_gap_5 IS '5日乖离率（%）';


--
-- Name: COLUMN stock_daily_latest.ma_gap_10; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma_gap_10 IS '10日乖离率（%）';


--
-- Name: COLUMN stock_daily_latest.ma_gap_20; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ma_gap_20 IS '20日乖离率（%）';


--
-- Name: COLUMN stock_daily_latest.rsi_6; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.rsi_6 IS 'RSI 6日指标';


--
-- Name: COLUMN stock_daily_latest.rsi_14; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.rsi_14 IS 'RSI 14日指标';


--
-- Name: COLUMN stock_daily_latest.kdj_k; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.kdj_k IS 'KDJ K值';


--
-- Name: COLUMN stock_daily_latest.kdj_d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.kdj_d IS 'KDJ D值';


--
-- Name: COLUMN stock_daily_latest.kdj_j; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.kdj_j IS 'KDJ J值';


--
-- Name: COLUMN stock_daily_latest.macd_dif; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.macd_dif IS 'MACD DIF值';


--
-- Name: COLUMN stock_daily_latest.macd_dea; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.macd_dea IS 'MACD DEA值';


--
-- Name: COLUMN stock_daily_latest.macd_hist; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.macd_hist IS 'MACD 柱状值';


--
-- Name: COLUMN stock_daily_latest.vol_std_5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.vol_std_5 IS '5日波动率';


--
-- Name: COLUMN stock_daily_latest.vol_std_20; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.vol_std_20 IS '20日波动率';


--
-- Name: COLUMN stock_daily_latest.vol_std_60; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.vol_std_60 IS '60日波动率';


--
-- Name: COLUMN stock_daily_latest.vol_atr_14; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.vol_atr_14 IS '14日 ATR（平均真实波幅）';


--
-- Name: COLUMN stock_daily_latest.volume_ratio_5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume_ratio_5 IS '5日量比';


--
-- Name: COLUMN stock_daily_latest.volume_ratio_20; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume_ratio_20 IS '20日量比';


--
-- Name: COLUMN stock_daily_latest.volume_ma_5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume_ma_5 IS '5日成交量均线';


--
-- Name: COLUMN stock_daily_latest.amount_ma_5; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.amount_ma_5 IS '5日成交额均线';


--
-- Name: COLUMN stock_daily_latest.bp; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.bp IS '账面市值比';


--
-- Name: COLUMN stock_daily_latest.ep_ttm; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ep_ttm IS '盈利收益率 TTM（1/PE）';


--
-- Name: COLUMN stock_daily_latest.ln_mv_total; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ln_mv_total IS '总市值对数';


--
-- Name: COLUMN stock_daily_latest.beta_20; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.beta_20 IS '20日 Beta 值';


--
-- Name: COLUMN stock_daily_latest.label; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.label IS '标签（用于机器学习）';


--
-- Name: COLUMN stock_daily_latest.ind_code_l1; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ind_code_l1 IS '一级行业代码';


--
-- Name: COLUMN stock_daily_latest.ind_code_l2; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.ind_code_l2 IS '二级行业代码';


--
-- Name: COLUMN stock_daily_latest.micro_effective_spread; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.micro_effective_spread IS '微观结构：有效价差';


--
-- Name: COLUMN stock_daily_latest.micro_imbalance_volume; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.micro_imbalance_volume IS '微观结构：成交量不平衡';


--
-- Name: COLUMN stock_daily_latest.micro_jump_flag; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.micro_jump_flag IS '微观结构：跳跃标志（0/1）';


--
-- Name: COLUMN stock_daily_latest.roe; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.roe IS '净资产收益率 ROE（小数比例）';


--
-- Name: COLUMN stock_daily_latest.volume_trend_3d; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume_trend_3d IS '3日成交量趋势分值';


--
-- Name: COLUMN stock_daily_latest.adj_factor; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.adj_factor IS '复权因子';


--
-- Name: COLUMN stock_daily_latest.volume_ma_3; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.volume_ma_3 IS '3日成交量均线';


--
-- Name: COLUMN stock_daily_latest.idx_all; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.idx_all IS '全市场指数成分（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.idx_hs300; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.idx_hs300 IS '沪深300成分（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.idx_zz1000; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.idx_zz1000 IS '中证1000成分（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.idx_margin; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.idx_margin IS '融资融券标的（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_ai; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_ai IS 'AI 概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_chip; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_chip IS '芯片概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_new_energy; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_new_energy IS '新能源概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_pv; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_pv IS '光伏概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_military; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_military IS '军工概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_medical; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_medical IS '医药概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_fintech; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_fintech IS '金融科技概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_consumption; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_consumption IS '消费概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.concept_state_owned; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.concept_state_owned IS '国企改革概念（0=否，1=是）';


--
-- Name: COLUMN stock_daily_latest.main_flow; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.main_flow IS '主力净流入（元）';


--
-- Name: COLUMN stock_daily_latest.inst_ownership; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.inst_ownership IS '机构持股比例（%）';


--
-- Name: COLUMN stock_daily_latest.profit_growth; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.profit_growth IS '利润增长率（%）';


--
-- Name: COLUMN stock_daily_latest.idx_chinext; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.stock_daily_latest.idx_chinext IS '创业板指数成分（0=否，1=是）';


--
-- Name: stock_daily_new_2026_01; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_daily_new_2026_01 (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
);


ALTER TABLE public.stock_daily_new_2026_01 OWNER TO quantmind;

--
-- Name: TABLE stock_daily_new_2026_01; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.stock_daily_new_2026_01 IS 'stock_daily_latest 分区：2026年1月数据';


--
-- Name: stock_daily_new_2026_02; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_daily_new_2026_02 (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
);


ALTER TABLE public.stock_daily_new_2026_02 OWNER TO quantmind;

--
-- Name: TABLE stock_daily_new_2026_02; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.stock_daily_new_2026_02 IS 'stock_daily_latest 分区：2026年2月数据';


--
-- Name: stock_daily_new_2026_03; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_daily_new_2026_03 (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
);


ALTER TABLE public.stock_daily_new_2026_03 OWNER TO quantmind;

--
-- Name: TABLE stock_daily_new_2026_03; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.stock_daily_new_2026_03 IS 'stock_daily_latest 分区：2026年3月数据';


--
-- Name: stock_daily_new_2026_04; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_daily_new_2026_04 (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
);


ALTER TABLE public.stock_daily_new_2026_04 OWNER TO quantmind;

--
-- Name: TABLE stock_daily_new_2026_04; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.stock_daily_new_2026_04 IS 'stock_daily_latest 分区：2026年4月数据';


--
-- Name: stock_pool_files; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stock_pool_files (
    id integer NOT NULL,
    tenant_id character varying(50) DEFAULT 'default'::character varying,
    user_id character varying(50) NOT NULL,
    pool_name character varying(200),
    session_id character varying(100),
    file_key character varying(500) NOT NULL,
    file_url character varying(1000),
    relative_path character varying(500),
    format character varying(10) DEFAULT 'csv'::character varying,
    file_size integer,
    code_hash character varying(64),
    stock_count integer,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.stock_pool_files OWNER TO quantmind;

--
-- Name: stock_pool_files_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.stock_pool_files_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.stock_pool_files_id_seq OWNER TO quantmind;

--
-- Name: stock_pool_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.stock_pool_files_id_seq OWNED BY public.stock_pool_files.id;


--
-- Name: stocks; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.stocks (
    id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    name character varying(100),
    exchange character varying(20),
    industry character varying(50),
    sector character varying(50),
    list_date date,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.stocks OWNER TO quantmind;

--
-- Name: stocks_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.stocks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.stocks_id_seq OWNER TO quantmind;

--
-- Name: stocks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.stocks_id_seq OWNED BY public.stocks.id;


--
-- Name: strategies; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.strategies (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    strategy_type public.strategytype NOT NULL,
    status public.strategystatus NOT NULL,
    config json NOT NULL,
    parameters json NOT NULL,
    code text,
    cos_url character varying(500),
    code_hash character varying(64),
    file_size integer,
    validated_backtest_id integer,
    promoted_at timestamp(6) without time zone,
    live_trading_started_at timestamp(6) without time zone,
    is_public boolean NOT NULL,
    shared_users json NOT NULL,
    backtest_count integer NOT NULL,
    view_count integer NOT NULL,
    like_count integer NOT NULL,
    created_at timestamp(6) without time zone NOT NULL,
    updated_at timestamp(6) without time zone NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    parent_id integer,
    change_log text,
    cos_key text,
    is_verified boolean DEFAULT false NOT NULL,
    execution_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    tags text[]
);


ALTER TABLE public.strategies OWNER TO quantmind;

--
-- Name: TABLE strategies; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.strategies IS '策略定义表：存储用户编写或生成的量化交易策略元数据与代码快照';


--
-- Name: COLUMN strategies.name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.name IS '策略名称';


--
-- Name: COLUMN strategies.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.description IS '策略逻辑描述';


--
-- Name: COLUMN strategies.code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.code IS '策略源代码 (Python/DSL)';


--
-- Name: COLUMN strategies.cos_url; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.cos_url IS '预签名 COS URL（私读，有效期由 COS_STRATEGY_URL_TTL 控制，默认 3600s）';


--
-- Name: COLUMN strategies.validated_backtest_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.validated_backtest_id IS '该策略最近一次通过验证的回测ID';


--
-- Name: COLUMN strategies.parent_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.parent_id IS '父策略ID (用于版本派生/克隆追踪)';


--
-- Name: COLUMN strategies.cos_key; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.strategies.cos_key IS '策略文件在腾讯云 COS 的对象键';


--
-- Name: strategies_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.strategies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.strategies_id_seq OWNER TO quantmind;

--
-- Name: strategies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.strategies_id_seq OWNED BY public.strategies.id;


--
-- Name: strategy_loop_tasks; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.strategy_loop_tasks (
    task_id text NOT NULL,
    user_id text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    status text NOT NULL,
    error_message text,
    created_at timestamp(6) with time zone NOT NULL,
    updated_at timestamp(6) with time zone NOT NULL,
    request_json jsonb,
    result_json jsonb
);


ALTER TABLE public.strategy_loop_tasks OWNER TO quantmind;

--
-- Name: TABLE strategy_loop_tasks; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.strategy_loop_tasks IS '策略循环任务调度：定时轮询/心跳任务的执行计划与上次运行状态';


--
-- Name: subscription_plans; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.subscription_plans (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    code character varying(50) NOT NULL,
    description character varying(255),
    price numeric(10,2) NOT NULL,
    currency character varying(3),
    "interval" character varying(20),
    features json,
    is_active boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone
);


ALTER TABLE public.subscription_plans OWNER TO quantmind;

--
-- Name: COLUMN subscription_plans.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.id IS 'Plan ID';


--
-- Name: COLUMN subscription_plans.name; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.name IS 'Plan Name';


--
-- Name: COLUMN subscription_plans.code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.code IS 'Plan Code (e.g., pro_monthly)';


--
-- Name: COLUMN subscription_plans.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.description IS 'Description';


--
-- Name: COLUMN subscription_plans.price; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.price IS 'Price';


--
-- Name: COLUMN subscription_plans.currency; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.currency IS 'Currency';


--
-- Name: COLUMN subscription_plans."interval"; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans."interval" IS 'Billing Interval (month/year)';


--
-- Name: COLUMN subscription_plans.features; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.features IS 'List of feature codes enabled by this plan';


--
-- Name: COLUMN subscription_plans.is_active; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.is_active IS 'Is Plan Active';


--
-- Name: COLUMN subscription_plans.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.created_at IS 'Created At';


--
-- Name: COLUMN subscription_plans.updated_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.subscription_plans.updated_at IS 'Updated At';


--
-- Name: subscription_plans_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.subscription_plans_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.subscription_plans_id_seq OWNER TO quantmind;

--
-- Name: subscription_plans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.subscription_plans_id_seq OWNED BY public.subscription_plans.id;


--
-- Name: system_settings; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.system_settings (
    key character varying(100) NOT NULL,
    value jsonb NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.system_settings OWNER TO quantmind;

--
-- Name: system_tasks; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.system_tasks (
    task_id character varying(64) NOT NULL,
    task_type character varying(32) NOT NULL,
    status character varying(20) DEFAULT 'PENDING'::character varying,
    progress integer DEFAULT 0,
    logs text,
    result_path text,
    error_message text,
    created_at timestamp(6) with time zone DEFAULT now(),
    finished_at timestamp(6) with time zone
);


ALTER TABLE public.system_tasks OWNER TO quantmind;

--
-- Name: TABLE system_tasks; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.system_tasks IS '系统后台任务表：用于 Celery 或 APScheduler 的任务调度追踪';


--
-- Name: COLUMN system_tasks.task_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.system_tasks.task_id IS '任务唯一标识符';


--
-- Name: COLUMN system_tasks.task_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.system_tasks.task_type IS '任务分类 (如 BACKTEST/DATASYNC)';


--
-- Name: COLUMN system_tasks.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.system_tasks.status IS '任务状态 (PENDING/SUCCESS/FAILURE)';


--
-- Name: COLUMN system_tasks.progress; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.system_tasks.progress IS '任务执行进度 (%)';


--
-- Name: COLUMN system_tasks.finished_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.system_tasks.finished_at IS '任务完成时间';


--
-- Name: tmp_feature_update; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.tmp_feature_update (
    symbol text,
    trade_date date,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    return_120d double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    beta_20 double precision,
    bp double precision,
    ep_ttm double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag double precision,
    adj_factor double precision
);


ALTER TABLE public.tmp_feature_update OWNER TO quantmind;

--
-- Name: trade_manual_execution_tasks; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.trade_manual_execution_tasks (
    task_id text NOT NULL,
    tenant_id text DEFAULT 'default'::text NOT NULL,
    user_id text NOT NULL,
    strategy_id text NOT NULL,
    strategy_name text NOT NULL,
    run_id text NOT NULL,
    model_id text NOT NULL,
    prediction_trade_date date NOT NULL,
    trading_mode text NOT NULL,
    status text NOT NULL,
    stage text DEFAULT 'queued'::text NOT NULL,
    error_stage text,
    error_message text,
    signal_count integer DEFAULT 0 NOT NULL,
    order_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    request_json jsonb,
    result_json jsonb,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    progress integer DEFAULT 0 NOT NULL,
    task_type text DEFAULT 'manual'::text NOT NULL,
    task_source text DEFAULT 'manual_page'::text NOT NULL,
    trigger_mode text DEFAULT 'manual'::text NOT NULL,
    trigger_context_json jsonb,
    strategy_snapshot_json jsonb,
    parent_runtime_id text
);


ALTER TABLE public.trade_manual_execution_tasks OWNER TO quantmind;

--
-- Name: trades; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.trades (
    id integer NOT NULL,
    trade_id uuid NOT NULL,
    order_id uuid NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    portfolio_id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    symbol_name character varying(50),
    side public.orderside NOT NULL,
    trade_action public.tradeaction,
    position_side public.positionside NOT NULL,
    is_margin_trade boolean NOT NULL,
    trading_mode public.tradingmode NOT NULL,
    quantity double precision NOT NULL,
    price double precision NOT NULL,
    trade_value double precision NOT NULL,
    commission double precision NOT NULL,
    stamp_duty double precision NOT NULL,
    transfer_fee double precision NOT NULL,
    total_fee double precision NOT NULL,
    executed_at timestamp without time zone NOT NULL,
    exchange_trade_id character varying(100),
    exchange_name character varying(50),
    remarks character varying(500),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.trades OWNER TO quantmind;

--
-- Name: trades_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.trades_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.trades_id_seq OWNER TO quantmind;

--
-- Name: trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.trades_id_seq OWNED BY public.trades.id;


--
-- Name: user_audit_logs; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_audit_logs (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    action character varying(64) NOT NULL,
    resource character varying(128),
    resource_id character varying(128),
    description text,
    request_data text,
    response_data text,
    ip_address character varying(64),
    user_agent text,
    request_method character varying(16),
    request_path character varying(255),
    status_code integer,
    success boolean,
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    duration_ms integer
);


ALTER TABLE public.user_audit_logs OWNER TO quantmind;

--
-- Name: COLUMN user_audit_logs.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.tenant_id IS '租户ID';


--
-- Name: COLUMN user_audit_logs.action; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.action IS '操作类型';


--
-- Name: COLUMN user_audit_logs.resource; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.resource IS '操作资源';


--
-- Name: COLUMN user_audit_logs.resource_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.resource_id IS '资源ID';


--
-- Name: COLUMN user_audit_logs.description; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.description IS '操作描述';


--
-- Name: COLUMN user_audit_logs.request_data; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.request_data IS '请求数据(JSON)';


--
-- Name: COLUMN user_audit_logs.response_data; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.response_data IS '响应数据(JSON)';


--
-- Name: COLUMN user_audit_logs.ip_address; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.ip_address IS 'IP地址';


--
-- Name: COLUMN user_audit_logs.user_agent; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.user_agent IS 'User Agent';


--
-- Name: COLUMN user_audit_logs.request_method; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.request_method IS '请求方法';


--
-- Name: COLUMN user_audit_logs.request_path; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.request_path IS '请求路径';


--
-- Name: COLUMN user_audit_logs.status_code; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.status_code IS '状态码';


--
-- Name: COLUMN user_audit_logs.success; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.success IS '是否成功';


--
-- Name: COLUMN user_audit_logs.error_message; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.error_message IS '错误信息';


--
-- Name: COLUMN user_audit_logs.duration_ms; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_audit_logs.duration_ms IS '处理时长(毫秒)';


--
-- Name: user_audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.user_audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.user_audit_logs_id_seq OWNER TO quantmind;

--
-- Name: user_audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.user_audit_logs_id_seq OWNED BY public.user_audit_logs.id;


--
-- Name: user_profiles; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_profiles (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    nickname character varying(128),
    avatar_url character varying(500),
    bio text,
    preferences jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    display_name character varying(128),
    location character varying(128),
    website character varying(256),
    phone character varying(32),
    trading_experience character varying(32),
    risk_tolerance character varying(32),
    investment_goal character varying(64),
    github_url character varying(256),
    twitter_handle character varying(64),
    linkedin_url character varying(256),
    notification_settings jsonb DEFAULT '{}'::jsonb,
    ai_ide_api_key character varying(128),
    api_key text
);


ALTER TABLE public.user_profiles OWNER TO quantmind;

--
-- Name: user_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.user_profiles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.user_profiles_id_seq OWNER TO quantmind;

--
-- Name: user_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.user_profiles_id_seq OWNED BY public.user_profiles.id;


--
-- Name: user_roles; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_roles (
    user_id character varying(64) NOT NULL,
    role_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.user_roles OWNER TO quantmind;

--
-- Name: user_sessions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_sessions (
    id character varying(64) DEFAULT (gen_random_uuid())::character varying(64),
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    token_hash character varying(255),
    device_info character varying(255),
    ip_address character varying(64),
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    session_id character varying(64) NOT NULL,
    token_jti character varying(64),
    user_agent character varying(255),
    last_activity_at timestamp with time zone,
    refresh_token character varying(1024),
    refresh_token_expires_at timestamp with time zone,
    last_active_at timestamp with time zone,
    is_active boolean DEFAULT true,
    is_revoked boolean DEFAULT false
);


ALTER TABLE public.user_sessions OWNER TO quantmind;

--
-- Name: user_strategies; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_strategies (
    id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    strategy_name character varying(255) NOT NULL,
    description text,
    conditions jsonb DEFAULT '{}'::jsonb,
    stock_pool jsonb DEFAULT '{}'::jsonb,
    position_config jsonb DEFAULT '{}'::jsonb,
    style character varying(64),
    risk_config jsonb DEFAULT '{}'::jsonb,
    cos_url text,
    file_size integer,
    code_hash character varying(128),
    qlib_validated boolean DEFAULT false,
    validation_result jsonb DEFAULT '{}'::jsonb,
    tags text[] DEFAULT ARRAY[]::text[],
    is_public boolean DEFAULT false,
    downloads integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    is_verified boolean DEFAULT false NOT NULL,
    shared_users jsonb DEFAULT '[]'::jsonb NOT NULL
);


ALTER TABLE public.user_strategies OWNER TO quantmind;

--
-- Name: user_subscriptions; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_subscriptions (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    plan_id integer NOT NULL,
    status character varying(20),
    start_date timestamp with time zone NOT NULL,
    end_date timestamp with time zone NOT NULL,
    auto_renew boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone,
    alipay_agreement_id character varying(64),
    alipay_agreement_status character varying(20)
);


ALTER TABLE public.user_subscriptions OWNER TO quantmind;

--
-- Name: TABLE user_subscriptions; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.user_subscriptions IS '用户订阅记录：当前套餐、到期时间、自动续费状态';


--
-- Name: COLUMN user_subscriptions.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.id IS 'Subscription ID';


--
-- Name: COLUMN user_subscriptions.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.user_id IS 'User ID';


--
-- Name: COLUMN user_subscriptions.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.tenant_id IS 'Tenant ID';


--
-- Name: COLUMN user_subscriptions.plan_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.plan_id IS 'Plan ID';


--
-- Name: COLUMN user_subscriptions.status; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.status IS 'Status (active, expired, cancelled)';


--
-- Name: COLUMN user_subscriptions.start_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.start_date IS 'Start Date';


--
-- Name: COLUMN user_subscriptions.end_date; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.end_date IS 'End Date';


--
-- Name: COLUMN user_subscriptions.auto_renew; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.auto_renew IS 'Auto Renew';


--
-- Name: COLUMN user_subscriptions.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.created_at IS 'Created At';


--
-- Name: COLUMN user_subscriptions.updated_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_subscriptions.updated_at IS 'Updated At';


--
-- Name: user_subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.user_subscriptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.user_subscriptions_id_seq OWNER TO quantmind;

--
-- Name: user_subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.user_subscriptions_id_seq OWNED BY public.user_subscriptions.id;


--
-- Name: user_usages; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.user_usages (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    usage_type character varying(20) NOT NULL,
    count integer NOT NULL,
    period character varying(7) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone
);


ALTER TABLE public.user_usages OWNER TO quantmind;

--
-- Name: COLUMN user_usages.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.id IS 'ID';


--
-- Name: COLUMN user_usages.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.user_id IS 'User ID';


--
-- Name: COLUMN user_usages.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.tenant_id IS 'Tenant ID';


--
-- Name: COLUMN user_usages.usage_type; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.usage_type IS 'Usage Type (train, inference)';


--
-- Name: COLUMN user_usages.count; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.count IS 'Usage Count';


--
-- Name: COLUMN user_usages.period; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.period IS 'Period (YYYY-MM)';


--
-- Name: COLUMN user_usages.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.created_at IS 'Created At';


--
-- Name: COLUMN user_usages.updated_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.user_usages.updated_at IS 'Updated At';


--
-- Name: user_usages_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.user_usages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.user_usages_id_seq OWNER TO quantmind;

--
-- Name: user_usages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.user_usages_id_seq OWNED BY public.user_usages.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.users (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    username character varying(128) NOT NULL,
    email character varying(255),
    phone_number character varying(32),
    password_hash character varying(255) NOT NULL,
    is_active boolean,
    is_verified boolean,
    is_admin boolean,
    is_locked boolean,
    last_login_at timestamp with time zone,
    last_login_ip character varying(64),
    login_count integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone,
    is_deleted boolean,
    deleted_at timestamp with time zone
);


ALTER TABLE public.users OWNER TO quantmind;

--
-- Name: TABLE users; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON TABLE public.users IS '用户主表：存储核心账号信息、密码哈希及账户状态';


--
-- Name: COLUMN users.id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.id IS '自增ID';


--
-- Name: COLUMN users.user_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.user_id IS '用户唯一标识';


--
-- Name: COLUMN users.tenant_id; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.tenant_id IS '租户ID';


--
-- Name: COLUMN users.username; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.username IS '用户名';


--
-- Name: COLUMN users.email; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.email IS '邮箱';


--
-- Name: COLUMN users.phone_number; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.phone_number IS '手机号';


--
-- Name: COLUMN users.password_hash; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.password_hash IS '密码哈希';


--
-- Name: COLUMN users.is_active; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.is_active IS '是否激活';


--
-- Name: COLUMN users.is_verified; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.is_verified IS '是否验证邮箱';


--
-- Name: COLUMN users.is_admin; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.is_admin IS '是否为管理员';


--
-- Name: COLUMN users.is_locked; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.is_locked IS '是否锁定';


--
-- Name: COLUMN users.last_login_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.last_login_at IS '最后登录时间';


--
-- Name: COLUMN users.last_login_ip; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.last_login_ip IS '最后登录IP';


--
-- Name: COLUMN users.login_count; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.login_count IS '登录次数';


--
-- Name: COLUMN users.created_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.created_at IS '创建时间';


--
-- Name: COLUMN users.updated_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.updated_at IS '更新时间';


--
-- Name: COLUMN users.is_deleted; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.is_deleted IS '是否删除';


--
-- Name: COLUMN users.deleted_at; Type: COMMENT; Schema: public; Owner: quantmind
--

COMMENT ON COLUMN public.users.deleted_at IS '删除时间';


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.users_id_seq OWNER TO quantmind;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: stock_daily_new_2026_01; Type: TABLE ATTACH; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest ATTACH PARTITION public.stock_daily_new_2026_01 FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');


--
-- Name: stock_daily_new_2026_02; Type: TABLE ATTACH; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest ATTACH PARTITION public.stock_daily_new_2026_02 FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');


--
-- Name: stock_daily_new_2026_03; Type: TABLE ATTACH; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest ATTACH PARTITION public.stock_daily_new_2026_03 FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');


--
-- Name: stock_daily_new_2026_04; Type: TABLE ATTACH; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest ATTACH PARTITION public.stock_daily_new_2026_04 FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');


--
-- Name: admin_data_files id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_data_files ALTER COLUMN id SET DEFAULT nextval('public.admin_data_files_id_seq'::regclass);


--
-- Name: admin_models id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_models ALTER COLUMN id SET DEFAULT nextval('public.admin_models_id_seq'::regclass);


--
-- Name: api_keys id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.api_keys ALTER COLUMN id SET DEFAULT nextval('public.api_keys_id_seq'::regclass);


--
-- Name: audit_logs id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.audit_logs ALTER COLUMN id SET DEFAULT nextval('public.audit_logs_id_seq'::regclass);


--
-- Name: backtests id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.backtests ALTER COLUMN id SET DEFAULT nextval('public.backtests_id_seq'::regclass);


--
-- Name: community_audit_logs id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_audit_logs ALTER COLUMN id SET DEFAULT nextval('public.community_audit_logs_id_seq'::regclass);


--
-- Name: community_author_follows id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_author_follows ALTER COLUMN id SET DEFAULT nextval('public.community_author_follows_id_seq'::regclass);


--
-- Name: community_comments id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_comments ALTER COLUMN id SET DEFAULT nextval('public.community_comments_id_seq'::regclass);


--
-- Name: community_interactions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_interactions ALTER COLUMN id SET DEFAULT nextval('public.community_interactions_id_seq'::regclass);


--
-- Name: community_posts id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_posts ALTER COLUMN id SET DEFAULT nextval('public.community_posts_id_seq'::regclass);


--
-- Name: data_download_orders id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.data_download_orders ALTER COLUMN id SET DEFAULT nextval('public.data_download_orders_id_seq'::regclass);


--
-- Name: email_verifications id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.email_verifications ALTER COLUMN id SET DEFAULT nextval('public.email_verifications_id_seq'::regclass);


--
-- Name: engine_dispatch_items id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_items ALTER COLUMN id SET DEFAULT nextval('public.engine_dispatch_items_id_seq'::regclass);


--
-- Name: engine_feature_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_feature_snapshots ALTER COLUMN id SET DEFAULT nextval('public.engine_feature_snapshots_id_seq'::regclass);


--
-- Name: engine_signal_scores id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_signal_scores ALTER COLUMN id SET DEFAULT nextval('public.engine_signal_scores_id_seq'::regclass);


--
-- Name: identity_verifications id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.identity_verifications ALTER COLUMN id SET DEFAULT nextval('public.identity_verifications_id_seq'::regclass);


--
-- Name: login_devices id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.login_devices ALTER COLUMN id SET DEFAULT nextval('public.login_devices_id_seq'::regclass);


--
-- Name: notifications id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.notifications ALTER COLUMN id SET DEFAULT nextval('public.notifications_id_seq'::regclass);


--
-- Name: orders id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.orders ALTER COLUMN id SET DEFAULT nextval('public.orders_id_seq'::regclass);


--
-- Name: password_reset_tokens id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.password_reset_tokens ALTER COLUMN id SET DEFAULT nextval('public.password_reset_tokens_id_seq'::regclass);


--
-- Name: payment_methods id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_methods ALTER COLUMN id SET DEFAULT nextval('public.payment_methods_id_seq'::regclass);


--
-- Name: payment_transactions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_transactions ALTER COLUMN id SET DEFAULT nextval('public.payment_transactions_id_seq'::regclass);


--
-- Name: permissions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);


--
-- Name: phone_verifications id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.phone_verifications ALTER COLUMN id SET DEFAULT nextval('public.phone_verifications_id_seq'::regclass);


--
-- Name: portfolio_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.portfolio_snapshots ALTER COLUMN id SET DEFAULT nextval('public.portfolio_snapshots_id_seq'::regclass);


--
-- Name: portfolios id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.portfolios ALTER COLUMN id SET DEFAULT nextval('public.portfolios_id_seq'::regclass);


--
-- Name: position_history id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.position_history ALTER COLUMN id SET DEFAULT nextval('public.position_history_id_seq'::regclass);


--
-- Name: positions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.positions ALTER COLUMN id SET DEFAULT nextval('public.positions_id_seq'::regclass);


--
-- Name: real_account_baselines id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_baselines ALTER COLUMN id SET DEFAULT nextval('public.real_account_baselines_id_seq'::regclass);


--
-- Name: real_account_ledger_daily_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_ledger_daily_snapshots ALTER COLUMN id SET DEFAULT nextval('public.real_account_ledger_daily_snapshots_id_seq'::regclass);


--
-- Name: real_account_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_snapshots ALTER COLUMN id SET DEFAULT nextval('public.real_account_snapshots_id_seq'::regclass);


--
-- Name: real_trading_preflight_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_trading_preflight_snapshots ALTER COLUMN id SET DEFAULT nextval('public.real_trading_preflight_snapshots_id_seq'::regclass);


--
-- Name: risk_rules id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.risk_rules ALTER COLUMN id SET DEFAULT nextval('public.risk_rules_id_seq'::regclass);


--
-- Name: roles id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.roles ALTER COLUMN id SET DEFAULT nextval('public.roles_id_seq'::regclass);


--
-- Name: simulation_daily_reports id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_daily_reports ALTER COLUMN id SET DEFAULT nextval('public.simulation_daily_reports_id_seq'::regclass);


--
-- Name: simulation_fund_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_fund_snapshots ALTER COLUMN id SET DEFAULT nextval('public.simulation_fund_snapshots_id_seq'::regclass);


--
-- Name: simulation_positions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_positions ALTER COLUMN id SET DEFAULT nextval('public.simulation_positions_id_seq'::regclass);


--
-- Name: stock_pool_files id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_pool_files ALTER COLUMN id SET DEFAULT nextval('public.stock_pool_files_id_seq'::regclass);


--
-- Name: stocks id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stocks ALTER COLUMN id SET DEFAULT nextval('public.stocks_id_seq'::regclass);


--
-- Name: strategies id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.strategies ALTER COLUMN id SET DEFAULT nextval('public.strategies_id_seq'::regclass);


--
-- Name: subscription_plans id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.subscription_plans ALTER COLUMN id SET DEFAULT nextval('public.subscription_plans_id_seq'::regclass);


--
-- Name: trades id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.trades ALTER COLUMN id SET DEFAULT nextval('public.trades_id_seq'::regclass);


--
-- Name: user_audit_logs id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_audit_logs ALTER COLUMN id SET DEFAULT nextval('public.user_audit_logs_id_seq'::regclass);


--
-- Name: user_profiles id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_profiles ALTER COLUMN id SET DEFAULT nextval('public.user_profiles_id_seq'::regclass);


--
-- Name: user_subscriptions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_subscriptions ALTER COLUMN id SET DEFAULT nextval('public.user_subscriptions_id_seq'::regclass);


--
-- Name: user_usages id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_usages ALTER COLUMN id SET DEFAULT nextval('public.user_usages_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: admin_data_files; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.admin_data_files (id, tenant_id, data_source_id, filename, file_size, status, meta, created_at) FROM stdin;
\.


--
-- Data for Name: admin_models; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.admin_models (id, tenant_id, user_id, name, description, source_type, start_date, end_date, config, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: admin_training_jobs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.admin_training_jobs (id, tenant_id, user_id, status, instance_id, request_payload, logs, result, progress, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.alembic_version (version_num) FROM stdin;
\.


--
-- Data for Name: alembic_version_community; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.alembic_version_community (version_num) FROM stdin;
\.


--
-- Data for Name: api_keys; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.api_keys (id, user_id, tenant_id, name, permissions, last_used_at, expires_at, is_active, created_at, access_key, secret_hash) FROM stdin;
\.


--
-- Data for Name: audit_logs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.audit_logs (id, user_id, tenant_id, action, resource_type, resource_id, old_value, new_value, ip_address, user_agent, created_at) FROM stdin;
\.


--
-- Data for Name: backtests; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.backtests (id, strategy_id, user_id, start_date, end_date, initial_capital, final_value, total_return, annual_return, max_drawdown, sharpe_ratio, win_rate, trades, metrics, status, error_message, created_at, completed_at, code_snapshot, code_hash_snapshot) FROM stdin;
\.


--
-- Data for Name: community_audit_logs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.community_audit_logs (id, tenant_id, user_id, action, entity_type, entity_id, ip, user_agent, meta, created_at) FROM stdin;
\.


--
-- Data for Name: community_author_follows; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.community_author_follows (id, tenant_id, follower_user_id, author_user_id, created_at) FROM stdin;
\.


--
-- Data for Name: community_comments; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.community_comments (id, tenant_id, post_id, author_id, content, parent_id, reply_to_id, likes, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: community_interactions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.community_interactions (id, tenant_id, user_id, post_id, comment_id, type, created_at) FROM stdin;
\.


--
-- Data for Name: community_posts; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.community_posts (id, tenant_id, author_id, title, content, category, tags, media, excerpt, views, likes, comments, collections, pinned, featured, created_at, updated_at, last_comment_at) FROM stdin;
\.


--
-- Data for Name: data_download_orders; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.data_download_orders (id, user_id, tenant_id, order_no, amount, currency, status, download_type, description, metadata_info, created_at, completed_at, expires_at, download_count) FROM stdin;
\.


--
-- Data for Name: email_verifications; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.email_verifications (id, user_id, tenant_id, email, verification_code, code_type, is_used, is_expired, created_at, expires_at, used_at, attempts, ip_address) FROM stdin;
\.


--
-- Data for Name: engine_dispatch_batches; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.engine_dispatch_batches (batch_id, run_id, tenant_id, user_id, trade_date, strategy_id, trading_mode, stage, stage_updated_at, total_signals, dispatched_signals, acked_signals, order_submitted_count, order_filled_count, failed_count, trace_id, last_error, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: engine_dispatch_items; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.engine_dispatch_items (id, batch_id, run_id, signal_id, client_order_id, tenant_id, user_id, trade_date, symbol, action, quantity, price, score, dispatch_status, order_id, exchange_order_id, exchange_trade_id, exec_message, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: engine_feature_runs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.engine_feature_runs (run_id, tenant_id, user_id, trade_date, model_name, model_version, feature_version, feature_dim, window_start, window_end, status, expected_symbols, ready_symbols, missing_symbols, source, checksum, quality, error_message, created_at, updated_at, open, high, low, close, volume, factor, style_sp_ttm, style_cfp_ttm, style_ev_ebitda_ttm, style_tobin_q, mom_ret_1d, mom_ret_3d, mom_ret_5d, mom_ret_10d, mom_ret_20d, mom_ret_60d, mom_ret_120d, mom_ma_gap_5, mom_ma_gap_10, mom_ma_gap_20, mom_ma_gap_60, mom_ma_gap_120, mom_ema_gap_12, mom_ema_gap_26, mom_macd_dif, mom_macd_dea, mom_macd_hist, mom_rsi_6, mom_rsi_14, mom_kdj_k, mom_kdj_d, mom_kdj_j, mom_roc_12, mom_breakout_20d, vol_std_5, vol_std_10, vol_std_20, vol_std_60, vol_atr_14, vol_atr_20, vol_true_range, vol_parkinson_10, vol_parkinson_20, vol_gk_10, vol_gk_20, vol_rs_10, vol_rs_20, vol_downside_20, vol_upside_20, vol_realized_rv, vol_realized_rrv, vol_realized_rskew, vol_realized_rkurt, vol_jump_zadj, vol_jump_rjv_ratio, vol_jump_sjv_ratio, liq_turnover_os, liq_turnover_tl, liq_volume, liq_volume_ma_5, liq_volume_ma_10, liq_volume_ma_20, liq_volume_ratio_5, liq_volume_ratio_20, liq_amount, liq_amount_ma_5, liq_amount_ma_10, liq_amount_ma_20, liq_amount_ratio_5, liq_amount_ratio_20, liq_trade_count, liq_avg_trade_size, liq_obv_20, liq_obv_60, liq_mfi_14, liq_accdist_20, liq_amihud_20, liq_amihud_60, flow_net_amount, flow_net_amount_ratio, flow_large_net_amount, flow_large_net_ratio, flow_medium_net_amount, flow_medium_net_ratio, flow_small_net_amount, flow_small_net_ratio, flow_net_order_count, flow_net_order_ratio, flow_large_net_order, flow_large_order_ratio, flow_vpin, flow_vpin_ma_5, flow_vpin_ma_20, flow_vpin_delta_5, flow_qsp, flow_esp, flow_aqsp, flow_qsp_time, flow_esp_time, flow_pressure_index, style_ln_mv_total, style_ln_mv_float, style_bp, style_ep_ttm, style_smb, style_hml, style_mkt_premium, style_beta_20, style_beta_60, style_beta_120, style_idio_vol_20, style_idio_vol_60, style_residual_ret_20, style_valuation_composite, style_size_percentile, style_value_percentile, ind_ret_1d, ind_ret_5d, ind_ret_10d, ind_ret_20d, ind_vol_20, ind_turnover_20, ind_amount_20, ind_strength_20, ind_strength_60, ind_dispersion_20, ind_up_breadth_20, ind_down_breadth_20, ind_relative_volume_20, ind_relative_volatility_20, ind_relative_flow_20, ind_momentum_rank_20, ind_value_rank, ind_size_rank, ind_code_l1, ind_code_l2, micro_qsp_equal, micro_esp_equal, micro_aqsp_equal, micro_qsp_time, micro_esp_time, micro_qsp_volume, micro_esp_volume, micro_qsp_amount, micro_esp_amount, micro_effective_spread, micro_quoted_spread, micro_spread_vol_20, micro_imbalance_volume, micro_imbalance_amount, micro_imbalance_count, micro_imbalance_large, micro_imbalance_medium, micro_imbalance_small, micro_jump_flag, micro_pressure_score, feature_1, feature_2, feature_3, feature_4, feature_5, feature_6, feature_7, feature_8, feature_9, feature_10, feature_11, feature_12, feature_13, feature_14, feature_15, feature_16, feature_17, feature_18, feature_19, feature_20, feature_21, feature_22, feature_23, feature_24, feature_25, feature_26, feature_27, feature_28, feature_29, feature_30, feature_31, feature_32, feature_33, feature_34, feature_35, feature_36, feature_37, feature_38, feature_39, feature_40, feature_41, feature_42, feature_43, feature_44, feature_45, feature_46, feature_47, feature_48, feature_49, feature_50, feature_51, feature_52, feature_53, feature_54, feature_55, feature_56, feature_57, feature_58, feature_59, feature_60, feature_61, feature_62, feature_63, feature_64, feature_65, feature_66, feature_67, feature_68, feature_69, feature_70, feature_71, feature_72, feature_73, feature_74, feature_75, feature_76, feature_77, feature_78, feature_79, feature_80, feature_81, feature_82, feature_83, feature_84, feature_85, feature_86, feature_87, feature_88, feature_89, feature_90, feature_91, feature_92, feature_93, feature_94, feature_95, feature_96, feature_97, feature_98, feature_99, feature_100, feature_101, feature_102, feature_103, feature_104, feature_105, feature_106, feature_107, feature_108, feature_109, feature_110, feature_111, feature_112, feature_113, feature_114, feature_115, feature_116, feature_117, feature_118, feature_119, feature_120, feature_121, feature_122, feature_123, feature_124, feature_125, feature_126, feature_127, feature_128, feature_129, feature_130, feature_131, feature_132, feature_133, feature_134, feature_135, feature_136, feature_137, feature_138, feature_139, feature_140, feature_141, feature_142, feature_143, feature_144, feature_145, feature_146, feature_147, feature_148, feature_149, feature_150, feature_151) FROM stdin;
\.


--
-- Data for Name: engine_feature_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.engine_feature_snapshots (id, run_id, tenant_id, user_id, trade_date, symbol, model_version, feature_version, feature_dim, features, data_source, is_valid, missing_ratio, quality, created_at) FROM stdin;
\.


--
-- Data for Name: engine_signal_scores; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.engine_signal_scores (id, run_id, tenant_id, user_id, trade_date, symbol, model_version, feature_version, light_score, tft_score, fusion_score, score_rank, universe_tag, signal_side, expected_price, quality, created_at, risk_weight, regime) FROM stdin;
\.


--
-- Data for Name: identity_verifications; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.identity_verifications (id, user_id, tenant_id, real_name, id_number, document_type, front_image_url, back_image_url, handheld_image_url, status, rejection_reason, submitted_at, verified_at, verified_by) FROM stdin;
\.


--
-- Data for Name: index_daily; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.index_daily (trade_date, symbol, open, high, low, close, volume, amount, adj_factor, created_at) FROM stdin;
\.


--
-- Data for Name: klines; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.klines (id, symbol, "interval", "timestamp", open_price, high_price, low_price, close_price, volume, amount, change, change_percent, turnover_rate, data_source, created_at) FROM stdin;
\.


--
-- Data for Name: login_devices; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.login_devices (id, user_id, tenant_id, device_id, device_name, device_type, os, browser, ip_address, location, is_trusted, is_active, first_seen_at, last_seen_at, last_location_change) FROM stdin;
\.


--
-- Data for Name: market_daily_stats; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.market_daily_stats (trade_date, sh_amount, sz_amount, total_amount, created_at) FROM stdin;
\.


--
-- Data for Name: notifications; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.notifications (id, user_id, tenant_id, notification_type, title, content, data, level, action_url, is_read, read_at, created_at, expires_at) FROM stdin;
\.


--
-- Data for Name: orders; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.orders (id, order_id, tenant_id, user_id, portfolio_id, strategy_id, symbol, symbol_name, side, trade_action, position_side, is_margin_trade, order_type, trading_mode, status, quantity, filled_quantity, price, stop_price, average_price, order_value, filled_value, commission, submitted_at, filled_at, cancelled_at, expired_at, client_order_id, exchange_order_id, remarks, version, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: password_reset_tokens; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.password_reset_tokens (id, user_id, tenant_id, email, token, is_used, is_expired, created_at, expires_at, used_at, ip_address, attempts) FROM stdin;
\.


--
-- Data for Name: payment_methods; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.payment_methods (id, user_id, tenant_id, provider, provider_token, last4, card_type, expiry_month, expiry_year, is_default, created_at) FROM stdin;
\.


--
-- Data for Name: payment_transactions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.payment_transactions (id, user_id, tenant_id, amount, currency, status, subscription_id, payment_method_id, provider, transaction_id, description, metadata_info, created_at, updated_at, completed_at) FROM stdin;
\.


--
-- Data for Name: permissions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.permissions (id, name, code, resource, action, description, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: phone_verifications; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.phone_verifications (id, phone_number, tenant_id, verification_code, code_type, is_used, is_expired, created_at, expires_at, used_at, attempts, ip_address) FROM stdin;
\.


--
-- Data for Name: pipeline_runs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.pipeline_runs (run_id, user_id, tenant_id, status, stage, error_message, created_at, updated_at, request_json, result_json) FROM stdin;
\.


--
-- Data for Name: portfolio_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.portfolio_snapshots (id, portfolio_id, snapshot_date, total_value, available_cash, market_value, total_pnl, total_return, daily_pnl, daily_return, max_drawdown, sharpe_ratio, volatility, position_count, is_settlement, created_at) FROM stdin;
\.


--
-- Data for Name: portfolios; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.portfolios (id, tenant_id, user_id, name, description, initial_capital, current_capital, available_cash, frozen_cash, total_value, total_pnl, total_return, daily_pnl, daily_return, yesterday_total_value, max_drawdown, sharpe_ratio, volatility, status, trading_mode, broker_type, broker_account_id, broker_params, strategy_id, real_trading_id, run_status, is_deleted, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: position_history; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.position_history (id, position_id, action, quantity_change, price, amount, quantity_after, avg_cost_after, note, created_at) FROM stdin;
\.


--
-- Data for Name: positions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.positions (id, portfolio_id, symbol, symbol_name, exchange, side, quantity, available_quantity, frozen_quantity, avg_cost, total_cost, current_price, market_value, unrealized_pnl, unrealized_pnl_rate, realized_pnl, weight, status, opened_at, updated_at, closed_at) FROM stdin;
\.


--
-- Data for Name: qlib_backtest_runs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qlib_backtest_runs (id, user_id, tenant_id, strategy_id, status, config, result, error_message, task_id, started_at, completed_at, execution_time_seconds, created_at, result_file_path, result_cos_key, result_cos_url, result_backup_status, result_backup_at, config_json, result_json, backtest_id) FROM stdin;
\.


--
-- Data for Name: qlib_backtest_runs_cleanup_backup; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qlib_backtest_runs_cleanup_backup (backtest_id, user_id, status, created_at, completed_at, config_json, result_json, tenant_id, task_id, result_file_path, result_cos_key, result_cos_url, result_backup_status, result_backup_at, backup_batch, backup_reason, backup_at) FROM stdin;
\.


--
-- Data for Name: qlib_optimization_runs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qlib_optimization_runs (optimization_id, task_id, mode, user_id, tenant_id, status, created_at, updated_at, completed_at, base_request_json, config_snapshot_json, optimization_target, param_ranges_json, total_tasks, completed_count, failed_count, current_params_json, best_params_json, best_metric_value, result_summary_json, all_results_json, error_message) FROM stdin;
\.


--
-- Data for Name: qm_market_calendar_day; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_market_calendar_day (market, trade_date, is_trading_day, timezone, source, version, tenant_id, user_id, metadata_json, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: qm_model_inference_runs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_model_inference_runs (run_id, tenant_id, user_id, model_id, data_trade_date, prediction_trade_date, status, signals_count, duration_ms, fallback_used, fallback_reason, failure_stage, error_message, stdout, stderr, active_model_id, effective_model_id, model_source, active_data_source, request_json, result_json, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: qm_model_inference_settings; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_model_inference_settings (tenant_id, user_id, model_id, enabled, schedule_time, last_run_id, last_run_json, next_run_at, created_at, updated_at) FROM stdin;
default	10000001	model_qlib	f		run_20260401_b31d4458	{"run_id": "run_20260401_b31d4458", "status": "completed", "stderr": "2026-05-05 11:18:22,815 [INFO] === parquet 推理脚本 ===\\n2026-05-05 11:18:22,815 [INFO]   model_dir : /app/models/production/model_qlib\\n2026-05-05 11:18:22,815 [INFO]   data_dir  : /app/db/feature_snapshots\\n2026-05-05 11:18:22,815 [INFO]   date      : 2026-04-01\\n2026-05-05 11:18:22,815 [INFO]   output    : /app/models/production/model_qlib/main_run_20260401_b31d4458.json\\n2026-05-05 11:18:22,816 [INFO]   run_id    : train_20260412145721_9273d147\\n2026-05-05 11:18:22,817 [INFO]   features  : 51\\n2026-05-05 11:18:23,756 [INFO] 过滤不可交易记录: 5273 -> 5260 (剔除 13 条)\\n2026-05-05 11:18:23,756 [INFO] 找到 5260 条可交易记录，日期=2026-04-01\\n2026-05-05 11:18:23,760 [INFO] 加载模型: model.lgb\\n2026-05-05 11:18:23,813 [INFO] 推理完成，生成 5260 条信号\\n2026-05-05 11:18:23,834 [INFO] 已写入信号文件: /app/models/production/model_qlib/main_run_20260401_b31d4458.json  (5260 条)\\n", "stdout": "", "success": true, "model_id": "model_qlib", "precheck": {"items": [{"key": "calendar_trade_date", "label": "交易日历校验", "detail": "2026-04-01 为交易日", "passed": true, "severity": "soft"}, {"key": "model_dir", "label": "模型目录存在", "detail": "/app/models/production/model_qlib", "passed": true, "severity": "hard"}, {"key": "model_file", "label": "模型文件存在", "detail": "/app/models/production/model_qlib/model.lgb", "passed": true, "severity": "hard"}, {"key": "metadata", "label": "模型元数据存在", "detail": "/app/models/production/model_qlib/metadata.json", "passed": true, "severity": "hard"}, {"key": "data_dir", "label": "推理数据目录存在", "detail": "/app/db/feature_snapshots", "passed": true, "severity": "hard"}, {"key": "inference_script", "label": "推理脚本存在", "detail": "/app/models/production/model_qlib/inference.py", "passed": true, "severity": "hard"}, {"key": "expected_feature_dim", "label": "期望特征维度", "detail": "51", "passed": true, "severity": "soft"}, {"key": "market_data_ready", "label": "历史 Parquet 数据就绪", "detail": "parquet=model_features_2026.parquet, date=2026-04-01, rows=5273", "passed": true, "severity": "hard"}, {"key": "prediction_trade_date", "label": "预测生效交易日", "detail": "2026-04-02", "passed": true, "severity": "soft"}, {"key": "model_id", "label": "当前模型", "detail": "model_qlib", "passed": true, "severity": "soft"}], "passed": true, "model_id": "model_qlib", "checked_at": "2026-05-05T11:18:20.557687+08:00", "model_file": "model.lgb", "model_source": "explicit_system_model", "storage_path": "/app/models/production/model_qlib", "data_trade_date": "2026-04-01", "calendar_adjusted": false, "effective_model_id": "model_qlib", "prediction_trade_date": "2026-04-02", "requested_inference_date": "2026-04-01"}, "duration_ms": 5135, "model_source": "explicit_system_model", "error_message": "", "failure_stage": "", "fallback_used": false, "signals_count": 5260, "execution_mode": "independent_model", "active_model_id": "model_qlib", "data_trade_date": "2026-04-01", "fallback_reason": "", "calendar_adjusted": false, "model_switch_used": false, "active_data_source": "/app/db/feature_snapshots", "effective_model_id": "model_qlib", "model_switch_reason": "", "prediction_trade_date": "2026-04-02", "requested_inference_date": "2026-04-01"}	\N	2026-05-02 13:05:49.506147+00	2026-05-05 03:18:25.712647+00
default	10000001	alpha158	f		run_20260330_d834338c	{"run_id": "run_20260330_d834338c", "status": "completed", "stderr": "2026-05-05 11:37:01,728 [INFO] Alpha158 V2: Initializing Targeted Inference Pipeline\\n2026-05-05 11:37:01,730 [INFO] Using qlib data path: /app/db/qlib_data\\n2026-05-05 11:37:01,734 [INFO] Loading binary market data for 5519 symbols (skipped BJ stocks)...\\n2026-05-05 11:37:05,903 [INFO] Calculating 158 factors for 5519 symbols across 57 days...\\n2026-05-05 11:37:08,115 [INFO] Loading model and predicting for 2629 symbols...\\n2026-05-05 11:37:08,144 [INFO] Inference successful. Generated 2629 signals.\\n", "stdout": "", "success": true, "model_id": "alpha158", "precheck": {"items": [{"key": "calendar_trade_date", "label": "交易日历校验", "detail": "2026-03-30 为交易日", "passed": true, "severity": "soft"}, {"key": "model_dir", "label": "模型目录存在", "detail": "/app/models/production/alpha158", "passed": true, "severity": "hard"}, {"key": "model_file", "label": "模型文件存在", "detail": "/app/models/production/alpha158/alpha158.bin", "passed": true, "severity": "hard"}, {"key": "metadata", "label": "模型元数据存在", "detail": "/app/models/production/alpha158/metadata.json", "passed": true, "severity": "hard"}, {"key": "data_dir", "label": "推理数据目录存在", "detail": "/app/db/qlib_data", "passed": true, "severity": "hard"}, {"key": "inference_script", "label": "推理脚本存在", "detail": "/app/models/production/alpha158/inference.py", "passed": true, "severity": "hard"}, {"key": "expected_feature_dim", "label": "期望特征维度", "detail": "158", "passed": true, "severity": "soft"}, {"key": "market_data_ready", "label": "Qlib 二进制数据就绪", "detail": "qlib_data=/app/db/qlib_data, date=2026-03-30 (已在日历中找到)", "passed": true, "severity": "hard"}, {"key": "prediction_trade_date", "label": "预测生效交易日", "detail": "2026-03-31", "passed": true, "severity": "soft"}, {"key": "model_id", "label": "当前模型", "detail": "alpha158", "passed": true, "severity": "soft"}], "passed": true, "model_id": "alpha158", "checked_at": "2026-05-05T11:37:00.422035+08:00", "model_file": "alpha158.bin", "model_source": "explicit_system_model", "storage_path": "/app/models/production/alpha158", "data_trade_date": "2026-03-30", "calendar_adjusted": false, "effective_model_id": "alpha158", "prediction_trade_date": "2026-03-31", "requested_inference_date": "2026-03-30"}, "duration_ms": 8805, "model_source": "explicit_system_model", "error_message": "", "failure_stage": "", "fallback_used": false, "signals_count": 2629, "execution_mode": "independent_model", "active_model_id": "alpha158", "data_trade_date": "2026-03-30", "fallback_reason": "", "calendar_adjusted": false, "model_switch_used": false, "active_data_source": "db/qlib_data", "effective_model_id": "alpha158", "model_switch_reason": "", "prediction_trade_date": "2026-03-31", "requested_inference_date": "2026-03-30"}	\N	2026-05-02 13:32:16.020188+00	2026-05-05 03:37:09.235514+00
\.


--
-- Data for Name: qm_research_candidate_snapshot; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_research_candidate_snapshot (tenant_id, user_id, run_id, model_id, data_trade_date, prediction_trade_date, symbol, fusion_score, light_score, tft_score, score_rank, signal_side, expected_price, universe_tag, quality, confidence_level, confidence_score, hit_reasons, risk_flags, thesis_summary, source_updated_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: qm_research_import_state; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_research_import_state (job_name, last_source_updated_at, last_prediction_trade_date, last_run_id, extra_json, updated_at) FROM stdin;
\.


--
-- Data for Name: qm_strategy_model_bindings; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_strategy_model_bindings (tenant_id, user_id, strategy_id, model_id, updated_at) FROM stdin;
\.


--
-- Data for Name: qm_user_models; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_user_models (tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file, metadata_json, metrics_json, is_default, created_at, updated_at, activated_at) FROM stdin;
default	10000001	model_qlib	\N	active	/app/models/production/model_qlib	model.lgb	{"run_id": "train_20260412145721_9273d147", "metrics": {"val_ic": 0.14148698422733702, "test_ic": 0.14928395870339195, "train_ic": 0.1723114985294621, "val_rank_ic": 0.11502790019933246, "test_rank_ic": 0.1341340862655188, "train_rank_ic": 0.15669129191672335, "val_rank_icir": 0.698590954061355, "test_rank_icir": 1.0017719337244009, "train_rank_icir": 1.3502038523286755}, "val_end": "2024-12-31", "features": ["mom_ret_1d", "mom_ret_5d", "mom_ret_20d", "liq_volume", "liq_amount", "liq_turnover_os", "open", "high", "low", "close", "volume", "factor", "mom_ret_10d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist", "mom_rsi_14", "mom_kdj_k", "mom_breakout_20d", "vol_std_20", "vol_atr_14", "vol_parkinson_20", "vol_gk_20", "vol_rs_20", "vol_downside_20", "vol_realized_rv", "vol_jump_zadj", "liq_volume_ma_20", "liq_volume_ratio_5", "liq_amount_ma_20", "liq_amount_ratio_5", "liq_mfi_14", "liq_amihud_20", "liq_amihud_60", "liq_accdist_20", "flow_net_amount", "flow_net_amount_ratio", "flow_large_net_amount", "flow_vpin", "flow_vpin_ma_5", "flow_vpin_ma_20", "style_ln_mv_total", "style_ln_mv_float", "style_beta_20", "style_beta_60", "style_idio_vol_20", "style_residual_ret_20", "ind_ret_1d", "ind_ret_20d", "ind_strength_20", "ind_momentum_rank_20"], "job_name": "model_train_t5_20260412225720", "test_end": "2025-12-31", "framework": "lightgbm", "pred_rows": 6273902, "train_end": "2023-12-31", "val_start": "2024-01-05", "model_file": "model.lgb", "model_type": "lightgbm", "test_start": "2025-01-05", "data_source": "parquet", "fill_values": {"low": 37.46468141729011, "high": 38.926549322557975, "open": 38.153599036605655, "close": 38.181337, "factor": 1.0, "volume": 5877945.0, "flow_vpin": 0.239175, "mom_kdj_k": 45.71811386283329, "vol_gk_20": 0.02348863268345031, "vol_rs_20": 0.023676759398189615, "ind_ret_1d": 0.0013050626459143943, "liq_amount": 69780318.0, "liq_mfi_14": 53.205229041131794, "liq_volume": 5877945.0, "mom_ret_1d": 0.0, "mom_ret_5d": -0.001993986431022887, "mom_rsi_14": 49.78125661803226, "vol_atr_14": 1.4168723879577396, "vol_std_20": 0.022971949058090392, "ind_ret_20d": 0.0010404000000000001, "mom_ret_10d": -0.0034965853806264713, "mom_ret_20d": -0.006292341045822769, "mom_ma_gap_5": -0.0010702861392963836, "liq_amihud_20": 0.0000000002372257693021674, "liq_amihud_60": 0.000000000256570554315033, "mom_ma_gap_20": -0.003914903798397984, "mom_macd_hist": 0.0, "style_beta_20": 0.9512923498280812, "style_beta_60": 0.9487860925720613, "vol_jump_zadj": 0.991653, "flow_vpin_ma_5": 0.24133519999999997, "liq_accdist_20": -10309920.129025847, "flow_net_amount": -2065902.3250000002, "flow_vpin_ma_20": 0.24305990000000002, "ind_strength_20": -0.006588720604658897, "liq_turnover_os": 1.436336, "vol_downside_20": 0.012499583475509272, "vol_realized_rv": 4.524839, "liq_amount_ma_20": 80058743.28099999, "liq_volume_ma_20": 6598366.15, "mom_breakout_20d": -0.07575757661342464, "vol_parkinson_20": 0.0233668676822037, "style_idio_vol_20": 0.019232321786548684, "style_ln_mv_float": 15.269809741277802, "style_ln_mv_total": 15.594220126431612, "liq_amount_ratio_5": 0.9288749012100398, "liq_volume_ratio_5": 0.9333121110314186, "ind_momentum_rank_20": 0.5, "flow_large_net_amount": 0.0, "flow_net_amount_ratio": -0.04653972121854505, "style_residual_ret_20": -0.003394683828465365}, "target_mode": "return", "train_start": "2016-01-01", "generated_at": "2026-04-12T15:02:47.522968", "feature_count": 51, "label_formula": "label = future_return(T, T+5) = close(T+5) / close(T) - 1", "best_iteration": 256, "regenerated_at": "2026-04-26T10:08:38.049767", "elapsed_seconds": 168.35593175888062, "feature_columns": ["mom_ret_1d", "mom_ret_5d", "mom_ret_20d", "liq_volume", "liq_amount", "liq_turnover_os", "open", "high", "low", "close", "volume", "factor", "mom_ret_10d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist", "mom_rsi_14", "mom_kdj_k", "mom_breakout_20d", "vol_std_20", "vol_atr_14", "vol_parkinson_20", "vol_gk_20", "vol_rs_20", "vol_downside_20", "vol_realized_rv", "vol_jump_zadj", "liq_volume_ma_20", "liq_volume_ratio_5", "liq_amount_ma_20", "liq_amount_ratio_5", "liq_mfi_14", "liq_amihud_20", "liq_amihud_60", "liq_accdist_20", "flow_net_amount", "flow_net_amount_ratio", "flow_large_net_amount", "flow_vpin", "flow_vpin_ma_5", "flow_vpin_ma_20", "style_ln_mv_total", "style_ln_mv_float", "style_beta_20", "style_beta_60", "style_idio_vol_20", "style_residual_ret_20", "ind_ret_1d", "ind_ret_20d", "ind_strength_20", "ind_momentum_rank_20"], "training_window": "2016-01-01 → 2023-12-31 | 2024-01-01 → 2024-12-31 | 2025-01-01 → 2025-12-31", "pred_coverage_end": "2026-04-24", "requested_features": ["open", "high", "low", "close", "volume", "factor", "mom_ret_1d", "mom_ret_5d", "mom_ret_10d", "mom_ret_20d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist", "mom_rsi_14", "mom_kdj_k", "mom_breakout_20d", "vol_std_20", "vol_atr_14", "vol_parkinson_20", "vol_gk_20", "vol_rs_20", "vol_downside_20", "vol_realized_rv", "vol_jump_zadj", "liq_volume_ma_20", "liq_volume_ratio_5", "liq_amount_ma_20", "liq_amount_ratio_5", "liq_mfi_14", "liq_amihud_20", "liq_amihud_60", "liq_accdist_20", "flow_net_amount", "flow_net_amount_ratio", "flow_large_net_amount", "flow_vpin", "flow_vpin_ma_5", "flow_vpin_ma_20", "style_ln_mv_total", "style_ln_mv_float", "style_beta_20", "style_beta_60", "style_idio_vol_20", "style_residual_ret_20", "ind_ret_1d", "ind_ret_20d", "ind_strength_20", "ind_momentum_rank_20"], "pred_coverage_start": "2021-01-04", "regeneration_config": {"end_date": "2026-04-27", "cpu_cores": 72, "start_date": "2021-01-01", "parallel_processes": 6}, "target_horizon_days": 5, "effective_trade_date": "2025-01-06", "auto_appended_features": ["liq_volume", "liq_amount", "liq_turnover_os"], "requested_feature_count": 48, "auto_appended_feature_count": 3}	{"val_ic": 0.14148698422733702, "test_ic": 0.14928395870339195, "train_ic": 0.1723114985294621, "val_rank_ic": 0.11502790019933246, "test_rank_ic": 0.1341340862655188, "train_rank_ic": 0.15669129191672335, "val_rank_icir": 0.698590954061355, "test_rank_icir": 1.0017719337244009, "train_rank_icir": 1.3502038523286755}	t	2026-05-02 13:05:47.454406+00	2026-05-05 07:24:12.278193+00	2026-05-05 07:24:12.278193+00
default	10000001	alpha158	\N	active	/app/models/production/alpha158	alpha158.bin	{"files": {"config": "config.yaml", "train_script": "train.py", "prepare_script": "prepare_data.py", "prediction_full": "pred.pkl", "inference_script": "inference.py", "model_checkpoint": "alpha158.bin"}, "notes": "模板文件：config.yaml, prepare_data.py, train.py。训练产物应保存 model_checkpoint.bin。", "val_end": "2025-06-30", "test_end": "2026-03-31", "framework": "lightgbm", "inference": {"script": "inference.py", "pred_file": "pred.pkl", "default_topk": 50, "output_format": "json", "supports_batch": true}, "pred_rows": 4374045, "train_end": "2024-12-31", "val_start": "2025-01-01", "valid_end": "2025-06-30", "model_info": {"name": "Alpha158_Base", "author": "quantmind-ai", "version": "v19", "algorithm": "LightGBM_Vectorized", "created_at": "2026-04-17", "description": "T+3 基线模型 (V2)，使用纯 Pandas 特征工程；数据范围：2020-2026，涵盖 6017 标的"}, "model_path": "/root/qlib-main/model/19_T3_Alpha158_Base/model_checkpoint_v19.bin", "model_type": "LGBModel", "test_start": "2025-07-01", "trained_at": "2026-04-17 17:01:33", "data_source": "qlib", "train_start": "2019-01-01", "valid_start": "2025-01-01", "feature_count": 158, "is_neutralized": false, "qlib_data_path": "/app/db/qlib_data", "regenerated_at": "2026-04-26T10:44:29.599132", "resolved_class": "qlib.contrib.model.gbdt.LGBModel", "prediction_path": "/app/models/production/alpha158/pred.pkl", "training_config": {"label": "Ref($close, -3)/Ref($close, -1) - 1", "end_time": "2026-03-31", "label_name": "LABEL0", "start_time": "2020-01-01", "instruments": "all", "fit_end_time": "2024-12-31", "fit_start_time": "2020-01-01"}, "pred_coverage_end": "2026-04-24", "performance_metrics": {"test": {"icir": 0.4028, "mean_ic": 0.0542}, "train": {"icir": 0.7607, "mean_ic": 0.0793}, "valid": {"icir": 0.4997, "mean_ic": 0.0711}}, "pred_coverage_start": "2021-01-04", "regeneration_config": {"end_date": "2026-04-27", "cpu_cores": 72, "start_date": "2021-01-01"}}	{"test": {"icir": 0.4028, "mean_ic": 0.0542}, "train": {"icir": 0.7607, "mean_ic": 0.0793}, "valid": {"icir": 0.4997, "mean_ic": 0.0711}}	f	2026-05-02 13:05:47.482974+00	2026-05-05 07:24:12.278193+00	2026-05-05 03:34:47.155585+00
\.


--
-- Data for Name: qm_user_research_pool; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_user_research_pool (tenant_id, user_id, symbol, stock_name, added_at, source_run_id, model_id, fusion_score, thesis_summary, status, notes, tags, created_at, updated_at, features_snapshot) FROM stdin;
\.


--
-- Data for Name: qm_user_watchlist; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qm_user_watchlist (tenant_id, user_id, symbol, stock_name, added_at, source_run_id, notes, tags, created_at, updated_at, features_snapshot) FROM stdin;
\.


--
-- Data for Name: qmt_agent_bindings; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qmt_agent_bindings (id, tenant_id, user_id, api_key_id, agent_type, account_id, client_fingerprint, hostname, client_version, status, last_ip, last_seen_at, bound_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: qmt_agent_sessions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.qmt_agent_sessions (id, binding_id, tenant_id, user_id, token_hash, expires_at, revoked_at, last_used_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: quote_daily_summaries; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.quote_daily_summaries (id, trade_date, symbol, data_source, open_price, high_price, low_price, close_price, avg_price, volume_sum, amount_sum, quote_count, first_quote_at, last_quote_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: quotes; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.quotes (id, symbol, trade_time, price, volume, bid_price, ask_price, created_at, "timestamp", current_price, open_price, high_price, low_price, close_price, amount, change, change_percent, data_source) FROM stdin;
\.


--
-- Data for Name: real_account_baselines; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.real_account_baselines (id, tenant_id, user_id, account_id, initial_equity, first_snapshot_at, source, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: real_account_ledger_daily_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.real_account_ledger_daily_snapshots (id, tenant_id, user_id, account_id, snapshot_date, last_snapshot_at, initial_equity, day_open_equity, month_open_equity, total_asset, cash, market_value, today_pnl_raw, monthly_pnl_raw, total_pnl_raw, floating_pnl_raw, daily_return_pct, total_return_pct, position_count, source, payload_json) FROM stdin;
\.


--
-- Data for Name: real_account_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.real_account_snapshots (id, tenant_id, user_id, account_id, snapshot_at, snapshot_date, snapshot_month, total_asset, cash, market_value, today_pnl_raw, total_pnl_raw, floating_pnl_raw, source, payload_json) FROM stdin;
\.


--
-- Data for Name: real_trading_preflight_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.real_trading_preflight_snapshots (id, tenant_id, user_id, trading_mode, snapshot_date, ready, total_checks, passed_checks, required_failed_count, run_count, failed_required_keys, checks, source, last_checked_at, created_at, updated_at) FROM stdin;
\.
\.


--
-- Data for Name: risk_rules; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.risk_rules (id, rule_name, rule_type, description, is_active, parameters, applies_to_all, user_ids, priority, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: role_permissions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.role_permissions (role_id, permission_id, created_at) FROM stdin;
\.


--
-- Data for Name: roles; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.roles (id, name, code, description, is_active, is_system, priority, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: sim_orders; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.sim_orders (id, job_id, user_id, tenant_id, symbol, side, order_type, quantity, price, status, filled_quantity, filled_price, commission, signal_time, submit_time, fill_time, created_at, order_id, portfolio_id, strategy_id, trading_mode, average_price, order_value, filled_value, submitted_at, filled_at, cancelled_at, execution_model, price_source, remarks, version, total_fee, updated_at) FROM stdin;
\.


--
-- Data for Name: sim_trades; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.sim_trades (id, job_id, order_id, user_id, tenant_id, symbol, side, quantity, price, commission, trade_time, created_at) FROM stdin;
\.


--
-- Data for Name: simulation_daily_reports; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.simulation_daily_reports (id, job_id, date, total_asset, return_rate, holdings_snapshot, created_at) FROM stdin;
\.


--
-- Data for Name: simulation_fund_snapshots; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.simulation_fund_snapshots (id, tenant_id, user_id, snapshot_date, total_asset, available_balance, frozen_balance, market_value, initial_capital, total_pnl, today_pnl, source, updated_at) FROM stdin;
\.


--
-- Data for Name: simulation_jobs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.simulation_jobs (id, tenant_id, user_id, model_id, strategy_config, initial_capital, current_cash, start_date, last_run_date, status, execution_logs, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: simulation_positions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.simulation_positions (id, job_id, symbol, side, quantity, avg_cost, market_value, unrealized_pnl, updated_at) FROM stdin;
\.


--
-- Data for Name: stock_pool_files; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.stock_pool_files (id, tenant_id, user_id, pool_name, session_id, file_key, file_url, relative_path, format, file_size, code_hash, stock_count, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: stocks; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.stocks (id, symbol, name, exchange, industry, sector, list_date, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: strategies; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.strategies (id, user_id, name, description, strategy_type, status, config, parameters, code, cos_url, code_hash, file_size, validated_backtest_id, promoted_at, live_trading_started_at, is_public, shared_users, backtest_count, view_count, like_count, created_at, updated_at, version, parent_id, change_log, cos_key, is_verified, execution_config, tags) FROM stdin;
22	10000001	趋势动量策略 (Momentum)	基于"强者恒强"逻辑，自动筛选过去一段时间涨幅最高且波动稳健的行业或个股。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "momentum"}	"""\n趋势动量策略 (Momentum Strategy)\n[Native] 核心逻辑：基于过去 20-60 天的累计收益率进行排名。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisTopkStrategy",\n    "kwargs": {\n        "topk": 30,\n        "n_drop": 6,\n        "momentum_period": 20\n    }\n}\n	\N	db7220d74508b2bafd7a400641c7e9310174ccb384f3c3fdd25f8c46e561cb32	276	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.823613	2026-05-05 10:10:57.823613	1	\N	\N	user_strategies/10000001/2026/05/aa8f652e-4a3c-44cb-a864-52d6b022b3fd.py	t	{"max_buy_drop": -0.03}	{basic,beginner,SystemSync}
23	10000001	默认 Top-K 选股策略	最经典的量化选股逻辑。每日截面排名，精选最具潜力的 Top-K 标的，等权持仓。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "standard_topk"}	"""\n默认 Top-K 选股策略 (Standard Top-K Strategy)\n[Native] 核心逻辑：Top-K 选股 + 零换手强制约束\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisTopkStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "n_drop": 10,\n    }\n}\n	\N	9d7ad9cc2dcad257a9593fbfbeebf8dc676f928fabb654cc94c83f4c78d07ca7	269	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.832732	2026-05-05 10:10:57.832732	1	\N	\N	user_strategies/10000001/2026/05/075432d9-1cfe-4f45-9ed5-b47966351e6c.py	t	{"max_buy_drop": -0.03}	{basic,beginner,SystemSync}
24	10000001	止损止盈策略	在标准 TopK 选股基础上叠加硬性止损/止盈规则，一旦触发立即强制平仓，保护资金安全。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "StopLoss"}	"""\n止损止盈策略 (Stop-Loss / Take-Profit Strategy)\n[Native] 核心逻辑：每日持仓浮亏超过 stop_loss 或浮盈超过 take_profit 时，强制平仓并从选股池剔除。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisStopLossStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 30,\n        "n_drop": 6,\n        "stop_loss": -0.08,\n        "take_profit": 0.15,\n    }\n}\n	\N	6bebc7047b2fe4b48c6ba4753983579f7e2fbf6c58d098f148e86eb13dd05759	400	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.835866	2026-05-05 10:10:57.835866	1	\N	\N	user_strategies/10000001/2026/05/0841619f-239c-4012-88ca-293880e3c079.py	t	{"max_buy_drop": -0.03}	{basic,beginner,SystemSync}
25	10000001	自适应动态调仓策略 (Concept Drift)	集成环境建模，自动识别牛熊阶段。动态调整选股宽度与仓位，应对风格漂移。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "adaptive_drift"}	"""\n自适应动态调仓策略 (Adaptive Concept Drift)\n[Native] 核心逻辑：集成 MarketStateService，自动触发动态仓位开关。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisRecordingStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "n_drop": 10,\n        "dynamic_position": True\n    }\n}\n	\N	172cb56ff825043aa5cffc09eb000c1bb29960dcde36562b6d83c8092b433740	333	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.841459	2026-05-05 10:10:57.841459	1	\N	\N	user_strategies/10000001/2026/05/fee87bba-3543-4aab-9dfd-5e805c5dcdd9.py	t	{"max_buy_drop": -0.03}	{basic,intermediate,SystemSync}
26	10000001	截面 Alpha 预测策略	旗舰级机器学习选股策略。根据预测分自动分配资金权重，分高者重仓。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "alpha_cross_section"}	"""\n截面 Alpha 预测策略 (Cross-sectional Alpha)\n[Native] 核心逻辑：按模型预测分比例进行权重分配。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisWeightStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "min_score": 0.0,\n        "max_weight": 0.05,\n    }\n}\n	\N	3566089fad1c8cc54e45fea2b60a8763f636d2a52d598338c539b1f0d89dd6bf	310	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.845594	2026-05-05 10:10:57.845594	1	\N	\N	user_strategies/10000001/2026/05/249cfaa5-3667-4ccd-8cc4-f4ec3fda1eb0.py	t	{"max_buy_drop": -0.03}	{basic,intermediate,SystemSync}
27	10000001	全量截面 Alpha 预测策略	基于截面 Alpha 评分每日全量重构持仓：跌出 TopK 全卖，候选涨停/停牌时自动顺延补位，目标维持 TopK 持仓。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "full_alpha_cross_section"}	"""\n全量截面 Alpha 预测策略 (Full Cross-sectional Alpha)\n[Native] 核心逻辑：每日全量重构 TopK，跌出即卖，涨停/停牌自动顺延补位。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisFullAlphaStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "max_weight": 0.05,\n        "rebalance_days": 1,\n    },\n}\n\n	\N	bdd414a118617b7f2872b0c80b02869b60bd0a54f1dee8a3599c6276561a7c52	359	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.847977	2026-05-05 10:10:57.847977	1	\N	\N	user_strategies/10000001/2026/05/627de91d-e840-41a7-8f17-8d836c9913af.py	t	{"max_buy_drop": -0.03}	{basic,intermediate,SystemSync}
28	10000001	得分加权组合策略	根据模型预测分自动分配资金权重，分高者重仓，支持单票上限与最低分过滤。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "score_weighted"}	"""\n得分加权组合策略 (Score-Weighted)\n[Native] 核心逻辑：权重 = Score / Sum(Scores)，且 Weight <= Max_Weight。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisWeightStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "min_score": 0.0,\n        "max_weight": 0.05,\n    }\n}\n	\N	a4431ebb33945428c8b67c929fdfea20a2d2bf46aa660998d9e4d5df3aa664c8	315	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.849432	2026-05-05 10:10:57.849432	1	\N	\N	user_strategies/10000001/2026/05/84d7d174-50b2-4923-8eea-04fe5f9683a4.py	t	{"max_buy_drop": -0.03}	{basic,intermediate,SystemSync}
29	10000001	波动率加权 TopK 策略	在 Top-K 选股基础上，以近期实现波动率的倒数分配仓位权重。低波动标的获得更高权重，降低组合整体波动率与尾部风险。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "VolatilityWeighted"}	"""\n波动率加权 TopK 策略 (Volatility-Weighted Top-K)\n[Native] 核心逻辑：Top-K 选股 + 以近期实现波动率的倒数为权重分配仓位。\n低波动标的获得更高权重，高波动标的自动降权，降低组合整体风险。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisVolatilityWeightedStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "vol_lookback": 20,\n        "max_weight": 0.10,\n        "min_score": 0.0,\n    }\n}\n	\N	3e380cd06856f8c787770cf19f92a7f7f34255799b1e8f8050786d01116447e5	477	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.85096	2026-05-05 10:10:57.85096	1	\N	\N	user_strategies/10000001/2026/05/cc29da51-a1d5-44a4-9475-99d726b41497.py	t	{"max_buy_drop": -0.03}	{basic,intermediate,SystemSync}
30	10000001	深度学习时序策略 (GRU/LSTM)	利用深度学习模型捕捉市场的长短期记忆效应，原生支持 3D 时序信号加载。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "deep_time_series"}	"""\n深度学习时序预测策略 (Time-Series GRU/LSTM)\n[Native] 核心逻辑：原生加载 .pkl 时序信号，支持 TS 格式特征。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisRecordingStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 30,\n        "n_drop": 6,\n    }\n}\n	\N	93a318a4c08b3a482bec300d67b14de710a66450e3a1459d81b7d5b3438259ba	297	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.852422	2026-05-05 10:10:57.852422	1	\N	\N	user_strategies/10000001/2026/05/25633912-dc1f-4fec-ba14-39fee0732928.py	t	{"max_buy_drop": -0.03}	{basic,advanced,SystemSync}
31	10000001	多空 TopK 策略	同时做多最高分 TopK 与做空最低分 TopK，支持固定调仓周期、双向敞口和单票权重上限。	CUSTOM	ACTIVE	{}	{"topk": 50, "signal": "<PRED>", "strategy_type": "long_short_topk"}	"""\n多空 TopK 策略 (Long-Short TopK)\n[Native] 核心逻辑：做多最高分 TopK + 做空最低分 TopK。\n"""\nSTRATEGY_CONFIG = {\n    "class": "RedisLongShortTopkStrategy",\n    "kwargs": {\n        "signal": "<PRED>",\n        "topk": 50,\n        "short_topk": 50,\n        "min_score": 0.0,\n        "max_weight": 0.05,\n        "long_exposure": 1.0,\n        "short_exposure": 1.0,\n        "rebalance_days": 5,\n        "enable_short_selling": True\n    }\n}\n	\N	ca328c22a2c6aa9160b2c24ee9167c93f66591aa0632069fc7061f4bcedb1b08	458	\N	\N	\N	f	[]	0	0	0	2026-05-05 10:10:57.853708	2026-05-05 10:10:57.853708	1	\N	\N	user_strategies/10000001/2026/05/e95fda4c-64d2-417f-a754-6ebbce1cb78b.py	t	{"max_buy_drop": -0.03}	{basic,advanced,SystemSync}
\.


--
-- Data for Name: strategy_loop_tasks; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.strategy_loop_tasks (task_id, user_id, tenant_id, status, error_message, created_at, updated_at, request_json, result_json) FROM stdin;
\.


--
-- Data for Name: subscription_plans; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.subscription_plans (id, name, code, description, price, currency, "interval", features, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: system_settings; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.system_settings (key, value, description, updated_at) FROM stdin;
\.


--
-- Data for Name: system_tasks; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.system_tasks (task_id, task_type, status, progress, logs, result_path, error_message, created_at, finished_at) FROM stdin;
\.


--
-- Data for Name: tmp_feature_update; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.tmp_feature_update (symbol, trade_date, return_1d, return_3d, return_5d, return_10d, return_20d, return_60d, return_120d, ma_gap_5, ma_gap_10, ma_gap_20, rsi_6, rsi_14, kdj_k, kdj_d, kdj_j, macd_dif, macd_dea, macd_hist, vol_std_5, vol_std_20, vol_std_60, vol_atr_14, volume_ratio_5, volume_ratio_20, volume_ma_5, amount_ma_5, beta_20, bp, ep_ttm, ind_code_l1, ind_code_l2, micro_effective_spread, micro_imbalance_volume, micro_jump_flag, adj_factor) FROM stdin;
\.


--
-- Data for Name: trade_manual_execution_tasks; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.trade_manual_execution_tasks (task_id, tenant_id, user_id, strategy_id, strategy_name, run_id, model_id, prediction_trade_date, trading_mode, status, stage, error_stage, error_message, signal_count, order_count, success_count, failed_count, request_json, result_json, created_at, updated_at, progress, task_type, task_source, trigger_mode, trigger_context_json, strategy_snapshot_json, parent_runtime_id) FROM stdin;
\.


--
-- Data for Name: trades; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.trades (id, trade_id, order_id, tenant_id, user_id, portfolio_id, symbol, symbol_name, side, trade_action, position_side, is_margin_trade, trading_mode, quantity, price, trade_value, commission, stamp_duty, transfer_fee, total_fee, executed_at, exchange_trade_id, exchange_name, remarks, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: user_audit_logs; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_audit_logs (id, user_id, tenant_id, action, resource, resource_id, description, request_data, response_data, ip_address, user_agent, request_method, request_path, status_code, success, error_message, created_at, duration_ms) FROM stdin;
60	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 03:03:31.77751+00	\N
61	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 04:27:57.647964+00	\N
62	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 05:43:08.615475+00	\N
63	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 07:24:08.200269+00	\N
64	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 08:39:01.402622+00	\N
65	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 09:41:32.854988+00	\N
66	10000001	default	login	auth	\N	用户成功登录	\N	\N	192.168.65.1	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) QuantMind/1.0.0 Chrome/144.0.7559.236 Electron/40.8.0 Safari/537.36	\N	\N	\N	t	\N	2026-05-05 09:48:04.816283+00	\N
\.


--
-- Data for Name: user_profiles; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_profiles (id, user_id, tenant_id, nickname, avatar_url, bio, preferences, created_at, updated_at, display_name, location, website, phone, trading_experience, risk_tolerance, investment_goal, github_url, twitter_handle, linkedin_url, notification_settings, ai_ide_api_key, api_key) FROM stdin;
34	10000001	default	\N	data/uploads/default_avatar.png	\N	{}	2026-05-02 13:03:44.253291+00	2026-05-02 15:25:50.749815+00	\N	\N	\N	\N	intermediate	medium	\N	\N	\N	\N	{}	\N	\N
\.


--
-- Data for Name: user_roles; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_roles (user_id, role_id, created_at) FROM stdin;
\.


--
-- Data for Name: user_sessions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_sessions (id, user_id, tenant_id, token_hash, device_info, ip_address, expires_at, revoked_at, created_at, session_id, token_jti, user_agent, last_activity_at, refresh_token, refresh_token_expires_at, last_active_at, is_active, is_revoked) FROM stdin;
\.


--
-- Data for Name: user_strategies; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_strategies (id, user_id, tenant_id, strategy_name, description, conditions, stock_pool, position_config, style, risk_config, cos_url, file_size, code_hash, qlib_validated, validation_result, tags, is_public, downloads, created_at, updated_at, is_verified, shared_users) FROM stdin;
\.


--
-- Data for Name: user_subscriptions; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_subscriptions (id, user_id, tenant_id, plan_id, status, start_date, end_date, auto_renew, created_at, updated_at, alipay_agreement_id, alipay_agreement_status) FROM stdin;
\.


--
-- Data for Name: user_usages; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.user_usages (id, user_id, tenant_id, usage_type, count, period, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: quantmind
--

COPY public.users (id, user_id, tenant_id, username, email, phone_number, password_hash, is_active, is_verified, is_admin, is_locked, last_login_at, last_login_ip, login_count, created_at, updated_at, is_deleted, deleted_at) FROM stdin;
10000001	10000001	default	admin	admin@quantmind.local	\N	$2b$12$B/yjK9cT.wx4BlB9j.r/t.dADjCbmutIXoDM7PdKZmV6ypuYiiUvW	t	t	t	f	2026-04-19 07:40:27.957017+00	\N	44	2026-04-16 12:57:19.018279+00	2026-04-19 07:40:27.95638+00	f	\N
\.


--
-- Name: admin_data_files_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.admin_data_files_id_seq', 1, false);


--
-- Name: admin_models_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.admin_models_id_seq', 1, false);


--
-- Name: api_keys_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.api_keys_id_seq', 1, false);


--
-- Name: audit_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.audit_logs_id_seq', 1, false);


--
-- Name: backtests_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.backtests_id_seq', 1, false);


--
-- Name: community_audit_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.community_audit_logs_id_seq', 1, false);


--
-- Name: community_author_follows_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.community_author_follows_id_seq', 1, false);


--
-- Name: community_comments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.community_comments_id_seq', 1, false);


--
-- Name: community_interactions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.community_interactions_id_seq', 1, false);


--
-- Name: community_posts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.community_posts_id_seq', 1, false);


--
-- Name: data_download_orders_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.data_download_orders_id_seq', 1, false);


--
-- Name: email_verifications_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.email_verifications_id_seq', 1, false);


--
-- Name: engine_dispatch_items_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.engine_dispatch_items_id_seq', 1, false);


--
-- Name: engine_feature_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.engine_feature_snapshots_id_seq', 1, false);


--
-- Name: engine_signal_scores_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.engine_signal_scores_id_seq', 1, false);


--
-- Name: identity_verifications_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.identity_verifications_id_seq', 1, false);


--
-- Name: klines_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.klines_id_seq', 1, false);


--
-- Name: login_devices_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.login_devices_id_seq', 1, false);


--
-- Name: notifications_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.notifications_id_seq', 1, false);


--
-- Name: orders_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.orders_id_seq', 1, false);


--
-- Name: password_reset_tokens_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.password_reset_tokens_id_seq', 1, false);


--
-- Name: payment_methods_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.payment_methods_id_seq', 1, false);


--
-- Name: payment_transactions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.payment_transactions_id_seq', 1, false);


--
-- Name: permissions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.permissions_id_seq', 1, false);


--
-- Name: phone_verifications_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.phone_verifications_id_seq', 1, false);


--
-- Name: portfolio_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.portfolio_snapshots_id_seq', 1, false);


--
-- Name: portfolios_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.portfolios_id_seq', 1, false);


--
-- Name: position_history_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.position_history_id_seq', 1, false);


--
-- Name: positions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.positions_id_seq', 1, false);


--
-- Name: quote_daily_summaries_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.quote_daily_summaries_id_seq', 1, false);


--
-- Name: quotes_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.quotes_id_seq', 1, false);


--
-- Name: real_account_baselines_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.real_account_baselines_id_seq', 1, false);


--
-- Name: real_account_ledger_daily_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.real_account_ledger_daily_snapshots_id_seq', 1, false);


--
-- Name: real_account_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.real_account_snapshots_id_seq', 1, false);


--
-- Name: real_trading_preflight_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.real_trading_preflight_snapshots_id_seq', 1, false);


--
-- Name: risk_rules_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.risk_rules_id_seq', 1, false);


--
-- Name: roles_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.roles_id_seq', 1, false);


--
-- Name: simulation_daily_reports_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.simulation_daily_reports_id_seq', 1, false);


--
-- Name: simulation_fund_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.simulation_fund_snapshots_id_seq', 1, false);


--
-- Name: simulation_positions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.simulation_positions_id_seq', 1, false);


--
-- Name: stock_pool_files_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.stock_pool_files_id_seq', 2, true);


--
-- Name: stocks_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.stocks_id_seq', 1, false);


--
-- Name: strategies_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.strategies_id_seq', 31, true);


--
-- Name: subscription_plans_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.subscription_plans_id_seq', 1, false);


--
-- Name: trades_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.trades_id_seq', 1, false);


--
-- Name: user_audit_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.user_audit_logs_id_seq', 66, true);


--
-- Name: user_profiles_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.user_profiles_id_seq', 34, true);


--
-- Name: user_subscriptions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.user_subscriptions_id_seq', 1, false);


--
-- Name: user_usages_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.user_usages_id_seq', 1, false);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: quantmind
--

SELECT pg_catalog.setval('public.users_id_seq', 33, true);


--
-- Name: admin_data_files admin_data_files_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_data_files
    ADD CONSTRAINT admin_data_files_pkey PRIMARY KEY (id);


--
-- Name: admin_models admin_models_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_models
    ADD CONSTRAINT admin_models_pkey PRIMARY KEY (id);


--
-- Name: admin_training_jobs admin_training_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_training_jobs
    ADD CONSTRAINT admin_training_jobs_pkey PRIMARY KEY (id);


--
-- Name: alembic_version_community alembic_version_community_pkc; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.alembic_version_community
    ADD CONSTRAINT alembic_version_community_pkc PRIMARY KEY (version_num);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: backtests backtests_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.backtests
    ADD CONSTRAINT backtests_pkey PRIMARY KEY (id);


--
-- Name: community_audit_logs community_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_audit_logs
    ADD CONSTRAINT community_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: community_author_follows community_author_follows_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_author_follows
    ADD CONSTRAINT community_author_follows_pkey PRIMARY KEY (id);


--
-- Name: community_comments community_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_comments
    ADD CONSTRAINT community_comments_pkey PRIMARY KEY (id);


--
-- Name: community_interactions community_interactions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_interactions
    ADD CONSTRAINT community_interactions_pkey PRIMARY KEY (id);


--
-- Name: community_posts community_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_posts
    ADD CONSTRAINT community_posts_pkey PRIMARY KEY (id);


--
-- Name: data_download_orders data_download_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.data_download_orders
    ADD CONSTRAINT data_download_orders_pkey PRIMARY KEY (id);


--
-- Name: email_verifications email_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT email_verifications_pkey PRIMARY KEY (id);


--
-- Name: engine_dispatch_batches engine_dispatch_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_batches
    ADD CONSTRAINT engine_dispatch_batches_pkey PRIMARY KEY (batch_id);


--
-- Name: engine_dispatch_items engine_dispatch_items_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_items
    ADD CONSTRAINT engine_dispatch_items_pkey PRIMARY KEY (id);


--
-- Name: engine_feature_runs engine_feature_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_feature_runs
    ADD CONSTRAINT engine_feature_runs_pkey PRIMARY KEY (run_id);


--
-- Name: engine_feature_snapshots engine_feature_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_feature_snapshots
    ADD CONSTRAINT engine_feature_snapshots_pkey PRIMARY KEY (id);


--
-- Name: engine_signal_scores engine_signal_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_signal_scores
    ADD CONSTRAINT engine_signal_scores_pkey PRIMARY KEY (id);


--
-- Name: identity_verifications identity_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.identity_verifications
    ADD CONSTRAINT identity_verifications_pkey PRIMARY KEY (id);


--
-- Name: index_daily index_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.index_daily
    ADD CONSTRAINT index_daily_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: klines klines_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.klines
    ADD CONSTRAINT klines_pkey PRIMARY KEY (id);


--
-- Name: login_devices login_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.login_devices
    ADD CONSTRAINT login_devices_pkey PRIMARY KEY (id);


--
-- Name: market_daily_stats market_daily_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.market_daily_stats
    ADD CONSTRAINT market_daily_stats_pkey PRIMARY KEY (trade_date);


--
-- Name: market_data_daily market_data_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.market_data_daily
    ADD CONSTRAINT market_data_daily_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: orders orders_client_order_id_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_client_order_id_key UNIQUE (client_order_id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);


--
-- Name: payment_methods payment_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_methods
    ADD CONSTRAINT payment_methods_pkey PRIMARY KEY (id);


--
-- Name: payment_transactions payment_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_transactions
    ADD CONSTRAINT payment_transactions_pkey PRIMARY KEY (id);


--
-- Name: permissions permissions_name_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_name_key UNIQUE (name);


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- Name: phone_verifications phone_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.phone_verifications
    ADD CONSTRAINT phone_verifications_pkey PRIMARY KEY (id);


--
-- Name: pipeline_runs pipeline_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.pipeline_runs
    ADD CONSTRAINT pipeline_runs_pkey PRIMARY KEY (run_id);


--
-- Name: portfolio_snapshots portfolio_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.portfolio_snapshots
    ADD CONSTRAINT portfolio_snapshots_pkey PRIMARY KEY (id);


--
-- Name: portfolios portfolios_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.portfolios
    ADD CONSTRAINT portfolios_pkey PRIMARY KEY (id);


--
-- Name: position_history position_history_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.position_history
    ADD CONSTRAINT position_history_pkey PRIMARY KEY (id);


--
-- Name: positions positions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_pkey PRIMARY KEY (id);


--
-- Name: qlib_backtest_runs_cleanup_backup qlib_backtest_runs_cleanup_backup_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qlib_backtest_runs_cleanup_backup
    ADD CONSTRAINT qlib_backtest_runs_cleanup_backup_pkey PRIMARY KEY (backtest_id);


--
-- Name: qlib_optimization_runs qlib_optimization_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qlib_optimization_runs
    ADD CONSTRAINT qlib_optimization_runs_pkey PRIMARY KEY (optimization_id);


--
-- Name: qm_market_calendar_day qm_market_calendar_day_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_market_calendar_day
    ADD CONSTRAINT qm_market_calendar_day_pkey PRIMARY KEY (market, trade_date, tenant_id, user_id);


--
-- Name: qm_model_inference_runs qm_model_inference_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_model_inference_runs
    ADD CONSTRAINT qm_model_inference_runs_pkey PRIMARY KEY (run_id);


--
-- Name: qm_model_inference_settings qm_model_inference_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_model_inference_settings
    ADD CONSTRAINT qm_model_inference_settings_pkey PRIMARY KEY (tenant_id, user_id, model_id);


--
-- Name: qm_research_candidate_snapshot qm_research_candidate_snapshot_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_research_candidate_snapshot
    ADD CONSTRAINT qm_research_candidate_snapshot_pkey PRIMARY KEY (tenant_id, user_id, run_id, symbol);


--
-- Name: qm_research_import_state qm_research_import_state_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_research_import_state
    ADD CONSTRAINT qm_research_import_state_pkey PRIMARY KEY (job_name);


--
-- Name: qm_strategy_model_bindings qm_strategy_model_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_strategy_model_bindings
    ADD CONSTRAINT qm_strategy_model_bindings_pkey PRIMARY KEY (tenant_id, user_id, strategy_id);


--
-- Name: qm_user_models qm_user_models_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_user_models
    ADD CONSTRAINT qm_user_models_pkey PRIMARY KEY (tenant_id, user_id, model_id);


--
-- Name: qm_user_research_pool qm_user_research_pool_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_user_research_pool
    ADD CONSTRAINT qm_user_research_pool_pkey PRIMARY KEY (tenant_id, user_id, symbol);


--
-- Name: qm_user_watchlist qm_user_watchlist_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_user_watchlist
    ADD CONSTRAINT qm_user_watchlist_pkey PRIMARY KEY (tenant_id, user_id, symbol);


--
-- Name: qmt_agent_bindings qmt_agent_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qmt_agent_bindings
    ADD CONSTRAINT qmt_agent_bindings_pkey PRIMARY KEY (id);


--
-- Name: qmt_agent_sessions qmt_agent_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qmt_agent_sessions
    ADD CONSTRAINT qmt_agent_sessions_pkey PRIMARY KEY (id);


--
-- Name: quote_daily_summaries quote_daily_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.quote_daily_summaries
    ADD CONSTRAINT quote_daily_summaries_pkey PRIMARY KEY (id);


--
-- Name: quotes quotes_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.quotes
    ADD CONSTRAINT quotes_pkey PRIMARY KEY (id);


--
-- Name: real_account_baselines real_account_baselines_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_baselines
    ADD CONSTRAINT real_account_baselines_pkey PRIMARY KEY (id);


--
-- Name: real_account_ledger_daily_snapshots real_account_ledger_daily_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_ledger_daily_snapshots
    ADD CONSTRAINT real_account_ledger_daily_snapshots_pkey PRIMARY KEY (id);


--
-- Name: real_account_snapshots real_account_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_snapshots
    ADD CONSTRAINT real_account_snapshots_pkey PRIMARY KEY (id);


--
-- Name: real_trading_preflight_snapshots real_trading_preflight_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_trading_preflight_snapshots
    ADD CONSTRAINT real_trading_preflight_snapshots_pkey PRIMARY KEY (id);


--
-- Name: risk_rules risk_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.risk_rules
    ADD CONSTRAINT risk_rules_pkey PRIMARY KEY (id);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: roles roles_name_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_name_key UNIQUE (name);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: sim_orders sim_orders_order_id_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.sim_orders
    ADD CONSTRAINT sim_orders_order_id_key UNIQUE (order_id);


--
-- Name: sim_orders sim_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.sim_orders
    ADD CONSTRAINT sim_orders_pkey PRIMARY KEY (id);


--
-- Name: sim_trades sim_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.sim_trades
    ADD CONSTRAINT sim_trades_pkey PRIMARY KEY (id);


--
-- Name: simulation_daily_reports simulation_daily_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_daily_reports
    ADD CONSTRAINT simulation_daily_reports_pkey PRIMARY KEY (id);


--
-- Name: simulation_fund_snapshots simulation_fund_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_fund_snapshots
    ADD CONSTRAINT simulation_fund_snapshots_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.simulation_fund_snapshots
    ADD CONSTRAINT uq_simulation_fund_snapshots_scope_date UNIQUE (tenant_id, user_id, snapshot_date);

CREATE INDEX idx_simulation_fund_snapshots_tenant_id ON public.simulation_fund_snapshots(tenant_id);
CREATE INDEX idx_simulation_fund_snapshots_user_id ON public.simulation_fund_snapshots(user_id);
CREATE INDEX idx_simulation_fund_snapshots_snapshot_date ON public.simulation_fund_snapshots(snapshot_date);


--
-- Name: simulation_jobs simulation_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_jobs
    ADD CONSTRAINT simulation_jobs_pkey PRIMARY KEY (id);


--
-- Name: simulation_positions simulation_positions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_positions
    ADD CONSTRAINT simulation_positions_pkey PRIMARY KEY (id);


--
-- Name: stock_daily_latest stock_daily_new_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest
    ADD CONSTRAINT stock_daily_new_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: stock_daily_new_2026_01 stock_daily_new_2026_01_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_new_2026_01
    ADD CONSTRAINT stock_daily_new_2026_01_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: stock_daily_new_2026_02 stock_daily_new_2026_02_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_new_2026_02
    ADD CONSTRAINT stock_daily_new_2026_02_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: stock_daily_new_2026_03 stock_daily_new_2026_03_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_new_2026_03
    ADD CONSTRAINT stock_daily_new_2026_03_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: stock_daily_new_2026_04 stock_daily_new_2026_04_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_new_2026_04
    ADD CONSTRAINT stock_daily_new_2026_04_pkey PRIMARY KEY (trade_date, symbol);


--
-- Name: stock_pool_files stock_pool_files_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_pool_files
    ADD CONSTRAINT stock_pool_files_pkey PRIMARY KEY (id);


--
-- Name: stocks stocks_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_pkey PRIMARY KEY (id);


--
-- Name: stocks stocks_symbol_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_symbol_key UNIQUE (symbol);


--
-- Name: strategies strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_pkey PRIMARY KEY (id);


--
-- Name: strategy_loop_tasks strategy_loop_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.strategy_loop_tasks
    ADD CONSTRAINT strategy_loop_tasks_pkey PRIMARY KEY (task_id);


--
-- Name: subscription_plans subscription_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.subscription_plans
    ADD CONSTRAINT subscription_plans_pkey PRIMARY KEY (id);


--
-- Name: system_settings system_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (key);


--
-- Name: system_tasks system_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.system_tasks
    ADD CONSTRAINT system_tasks_pkey PRIMARY KEY (task_id);


--
-- Name: trade_manual_execution_tasks trade_manual_execution_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.trade_manual_execution_tasks
    ADD CONSTRAINT trade_manual_execution_tasks_pkey PRIMARY KEY (task_id);


--
-- Name: trades trades_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_pkey PRIMARY KEY (id);


--
-- Name: community_author_follows uq_community_author_follows_model; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_author_follows
    ADD CONSTRAINT uq_community_author_follows_model UNIQUE (tenant_id, follower_user_id, author_user_id);


--
-- Name: community_interactions uq_community_interactions; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.community_interactions
    ADD CONSTRAINT uq_community_interactions UNIQUE (tenant_id, user_id, post_id, comment_id, type);


--
-- Name: engine_dispatch_batches uq_engine_dispatch_batches_run_strategy; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_batches
    ADD CONSTRAINT uq_engine_dispatch_batches_run_strategy UNIQUE (tenant_id, user_id, trade_date, run_id, strategy_id, trading_mode);


--
-- Name: engine_feature_snapshots uq_engine_feature_snapshots; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_feature_snapshots
    ADD CONSTRAINT uq_engine_feature_snapshots UNIQUE (tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id);


--
-- Name: engine_signal_scores uq_engine_signal_scores; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_signal_scores
    ADD CONSTRAINT uq_engine_signal_scores UNIQUE (tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id);


--
-- Name: real_trading_preflight_snapshots uq_preflight_snapshot_daily; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_trading_preflight_snapshots
    ADD CONSTRAINT uq_preflight_snapshot_daily UNIQUE (tenant_id, user_id, trading_mode, snapshot_date);


--
-- Name: qlib_backtest_runs uq_qlib_backtest_runs_backtest_id; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qlib_backtest_runs
    ADD CONSTRAINT uq_qlib_backtest_runs_backtest_id UNIQUE (backtest_id);


--
-- Name: quote_daily_summaries uq_quote_daily_summary; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.quote_daily_summaries
    ADD CONSTRAINT uq_quote_daily_summary UNIQUE (trade_date, symbol, data_source);


--
-- Name: real_account_baselines uq_real_account_baseline; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_baselines
    ADD CONSTRAINT uq_real_account_baseline UNIQUE (tenant_id, user_id, account_id);


--
-- Name: real_account_baselines uq_real_account_baselines_scope; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_baselines
    ADD CONSTRAINT uq_real_account_baselines_scope UNIQUE (tenant_id, user_id, account_id);


--
-- Name: real_account_ledger_daily_snapshots uq_real_account_ledger_daily; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_ledger_daily_snapshots
    ADD CONSTRAINT uq_real_account_ledger_daily UNIQUE (tenant_id, user_id, account_id, snapshot_date);


--
-- Name: klines uq_symbol_interval_timestamp; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.klines
    ADD CONSTRAINT uq_symbol_interval_timestamp UNIQUE (symbol, "interval", "timestamp");


--
-- Name: user_usages uq_user_usage_period; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_usages
    ADD CONSTRAINT uq_user_usage_period UNIQUE (user_id, tenant_id, usage_type, period);


--
-- Name: users uq_users_tenant_email; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_tenant_email UNIQUE (tenant_id, email);


--
-- Name: users uq_users_tenant_phone; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_tenant_phone UNIQUE (tenant_id, phone_number);


--
-- Name: users uq_users_tenant_username; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_tenant_username UNIQUE (tenant_id, username);


--
-- Name: user_audit_logs user_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_audit_logs
    ADD CONSTRAINT user_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: user_profiles user_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_profiles
    ADD CONSTRAINT user_profiles_pkey PRIMARY KEY (id);


--
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (user_id, role_id);


--
-- Name: user_sessions user_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: user_strategies user_strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_strategies
    ADD CONSTRAINT user_strategies_pkey PRIMARY KEY (id);


--
-- Name: user_subscriptions user_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_subscriptions
    ADD CONSTRAINT user_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: user_usages user_usages_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_usages
    ADD CONSTRAINT user_usages_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_user_id_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_user_id_key UNIQUE (user_id);


--
-- Name: idx_api_keys_access_key; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_api_keys_access_key ON public.api_keys USING btree (access_key);


--
-- Name: idx_api_keys_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_api_keys_user_id ON public.api_keys USING btree (user_id);


--
-- Name: idx_audit_logs_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_audit_logs_created_at ON public.audit_logs USING btree (created_at);


--
-- Name: idx_audit_logs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_audit_logs_user_id ON public.audit_logs USING btree (user_id);


--
-- Name: idx_engine_dispatch_batches_stage; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_dispatch_batches_stage ON public.engine_dispatch_batches USING btree (tenant_id, user_id, trade_date DESC, stage);


--
-- Name: idx_engine_dispatch_items_batch_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_dispatch_items_batch_status ON public.engine_dispatch_items USING btree (batch_id, dispatch_status);


--
-- Name: idx_engine_dispatch_items_symbol_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_dispatch_items_symbol_date ON public.engine_dispatch_items USING btree (symbol, trade_date DESC);


--
-- Name: idx_engine_feature_runs_model; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_feature_runs_model ON public.engine_feature_runs USING btree (model_name, model_version, feature_version, trade_date DESC);


--
-- Name: idx_engine_feature_runs_tenant_user_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_feature_runs_tenant_user_date ON public.engine_feature_runs USING btree (tenant_id, user_id, trade_date DESC);


--
-- Name: idx_engine_feature_snapshots_date_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_feature_snapshots_date_symbol ON public.engine_feature_snapshots USING btree (trade_date, symbol);


--
-- Name: idx_engine_feature_snapshots_run; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_feature_snapshots_run ON public.engine_feature_snapshots USING btree (run_id, symbol);


--
-- Name: idx_engine_signal_scores_date_rank; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_signal_scores_date_rank ON public.engine_signal_scores USING btree (trade_date DESC, score_rank);


--
-- Name: idx_engine_signal_scores_run_rank; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_engine_signal_scores_run_rank ON public.engine_signal_scores USING btree (run_id, score_rank);


--
-- Name: idx_feature_runs_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_feature_runs_user ON public.engine_feature_runs USING btree (tenant_id, user_id);


--
-- Name: idx_index_daily_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_index_daily_date ON public.index_daily USING btree (trade_date);


--
-- Name: idx_index_daily_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_index_daily_symbol ON public.index_daily USING btree (symbol);


--
-- Name: idx_mds_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_mds_date ON public.market_daily_stats USING btree (trade_date DESC);


--
-- Name: idx_notifications_tenant_user_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_notifications_tenant_user_created_at ON public.notifications USING btree (tenant_id, user_id, created_at DESC);


--
-- Name: idx_notifications_tenant_user_read_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_notifications_tenant_user_read_created_at ON public.notifications USING btree (tenant_id, user_id, is_read, created_at DESC);


--
-- Name: idx_notifications_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_notifications_user_id ON public.notifications USING btree (user_id);


--
-- Name: idx_order_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_order_created ON public.orders USING btree (created_at);


--
-- Name: idx_order_portfolio_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_order_portfolio_symbol ON public.orders USING btree (portfolio_id, symbol);


--
-- Name: idx_order_tenant_user_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_order_tenant_user_status ON public.orders USING btree (tenant_id, user_id, status);


--
-- Name: idx_order_user_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_order_user_status ON public.orders USING btree (user_id, status);


--
-- Name: idx_pipeline_runs_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_pipeline_runs_status ON public.pipeline_runs USING btree (status);


--
-- Name: idx_pipeline_runs_tenant_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_pipeline_runs_tenant_created ON public.pipeline_runs USING btree (tenant_id, created_at DESC);


--
-- Name: idx_portfolio_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_portfolio_created_at ON public.portfolios USING btree (created_at);


--
-- Name: idx_portfolio_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_portfolio_date ON public.portfolio_snapshots USING btree (portfolio_id, snapshot_date);


--
-- Name: idx_portfolio_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_portfolio_symbol ON public.positions USING btree (portfolio_id, symbol);


--
-- Name: idx_portfolio_tenant_user_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_portfolio_tenant_user_status ON public.portfolios USING btree (tenant_id, user_id, status);


--
-- Name: idx_portfolio_user_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_portfolio_user_status ON public.portfolios USING btree (user_id, status);


--
-- Name: idx_pos_history_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_pos_history_created_at ON public.position_history USING btree (created_at);


--
-- Name: idx_post_tenant_category; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_post_tenant_category ON public.community_posts USING btree (tenant_id, category);


--
-- Name: idx_qlib_backtest_runs_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_backtest_runs_status ON public.qlib_backtest_runs USING btree (status);


--
-- Name: idx_qlib_backtest_runs_tenant_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_backtest_runs_tenant_created ON public.qlib_backtest_runs USING btree (tenant_id, created_at DESC);


--
-- Name: idx_qlib_backtest_runs_user_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_backtest_runs_user_created ON public.qlib_backtest_runs USING btree (user_id, created_at DESC);


--
-- Name: idx_qlib_backtest_runs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_backtest_runs_user_id ON public.qlib_backtest_runs USING btree (user_id);


--
-- Name: idx_qlib_optimization_runs_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_optimization_runs_status ON public.qlib_optimization_runs USING btree (status);


--
-- Name: idx_qlib_optimization_runs_tenant_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_optimization_runs_tenant_created ON public.qlib_optimization_runs USING btree (tenant_id, created_at DESC);


--
-- Name: idx_qlib_optimization_runs_user_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qlib_optimization_runs_user_created ON public.qlib_optimization_runs USING btree (user_id, created_at DESC);


--
-- Name: idx_qm_calendar_day_query; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_calendar_day_query ON public.qm_market_calendar_day USING btree (market, tenant_id, user_id, trade_date);


--
-- Name: idx_qm_model_inference_runs_model_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_model_inference_runs_model_status ON public.qm_model_inference_runs USING btree (tenant_id, user_id, model_id, status, created_at DESC);


--
-- Name: idx_qm_model_inference_runs_owner_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_model_inference_runs_owner_created ON public.qm_model_inference_runs USING btree (tenant_id, user_id, created_at DESC);


--
-- Name: idx_qm_model_inference_runs_target_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_model_inference_runs_target_date ON public.qm_model_inference_runs USING btree (tenant_id, user_id, prediction_trade_date DESC);


--
-- Name: idx_qm_model_inference_settings_owner; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_model_inference_settings_owner ON public.qm_model_inference_settings USING btree (tenant_id, user_id, model_id, updated_at DESC);


--
-- Name: idx_qm_research_candidate_snapshot_model_run; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_research_candidate_snapshot_model_run ON public.qm_research_candidate_snapshot USING btree (tenant_id, user_id, model_id, run_id);


--
-- Name: idx_qm_research_candidate_snapshot_score; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_research_candidate_snapshot_score ON public.qm_research_candidate_snapshot USING btree (tenant_id, user_id, prediction_trade_date DESC, fusion_score DESC);


--
-- Name: idx_qm_research_candidate_snapshot_trade_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_research_candidate_snapshot_trade_date ON public.qm_research_candidate_snapshot USING btree (prediction_trade_date DESC, tenant_id, user_id);


--
-- Name: idx_qm_strategy_model_bindings_model; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_strategy_model_bindings_model ON public.qm_strategy_model_bindings USING btree (tenant_id, user_id, model_id);


--
-- Name: idx_qm_user_models_user_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qm_user_models_user_status ON public.qm_user_models USING btree (tenant_id, user_id, status, updated_at DESC);


--
-- Name: idx_qmt_binding_api_key; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qmt_binding_api_key ON public.qmt_agent_bindings USING btree (api_key_id);


--
-- Name: idx_qmt_binding_tenant_account_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qmt_binding_tenant_account_status ON public.qmt_agent_bindings USING btree (tenant_id, account_id, status);


--
-- Name: idx_qmt_session_binding; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qmt_session_binding ON public.qmt_agent_sessions USING btree (binding_id);


--
-- Name: idx_qmt_session_tenant_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_qmt_session_tenant_user ON public.qmt_agent_sessions USING btree (tenant_id, user_id);


--
-- Name: idx_quote_daily_summaries_symbol_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_quote_daily_summaries_symbol_date ON public.quote_daily_summaries USING btree (symbol, trade_date);


--
-- Name: idx_quote_timestamp; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_quote_timestamp ON public.quotes USING btree ("timestamp");


--
-- Name: idx_sdl_symbol_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_sdl_symbol_date ON ONLY public.stock_daily_latest USING btree (symbol, trade_date);


--
-- Name: idx_signal_scores_run_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_signal_scores_run_id ON public.engine_signal_scores USING btree (run_id);


--
-- Name: idx_signal_scores_trade_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_signal_scores_trade_date ON public.engine_signal_scores USING btree (trade_date);


--
-- Name: idx_signal_scores_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_signal_scores_user ON public.engine_signal_scores USING btree (tenant_id, user_id);


--
-- Name: idx_sim_orders_job_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_sim_orders_job_id ON public.sim_orders USING btree (job_id);


--
-- Name: idx_sim_trades_job_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_sim_trades_job_id ON public.sim_trades USING btree (job_id);


--
-- Name: idx_simulation_jobs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_simulation_jobs_user_id ON public.simulation_jobs USING btree (user_id);


--
-- Name: idx_snapshot_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_snapshot_date ON public.portfolio_snapshots USING btree (snapshot_date);


--
-- Name: idx_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_status ON public.positions USING btree (status);


--
-- Name: idx_strategies_cos_key; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategies_cos_key ON public.strategies USING btree (cos_key) WHERE (cos_key IS NOT NULL);


--
-- Name: idx_strategies_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategies_status ON public.strategies USING btree (status);


--
-- Name: idx_strategies_updated_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategies_updated_at ON public.strategies USING btree (updated_at DESC);


--
-- Name: idx_strategies_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategies_user_id ON public.strategies USING btree (user_id);


--
-- Name: idx_strategy_loop_tasks_user_tenant_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategy_loop_tasks_user_tenant_created ON public.strategy_loop_tasks USING btree (user_id, tenant_id, created_at DESC);


--
-- Name: idx_symbol_interval_timestamp; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_symbol_interval_timestamp ON public.klines USING btree (symbol, "interval", "timestamp");


--
-- Name: idx_symbol_timestamp; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_symbol_timestamp ON public.quotes USING btree (symbol, "timestamp");


--
-- Name: idx_timestamp; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_timestamp ON public.klines USING btree ("timestamp");


--
-- Name: idx_trade_executed; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_executed ON public.trades USING btree (executed_at);


--
-- Name: idx_trade_manual_execution_tasks_owner_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_manual_execution_tasks_owner_created ON public.trade_manual_execution_tasks USING btree (tenant_id, user_id, created_at DESC);


--
-- Name: idx_trade_manual_execution_tasks_status_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_manual_execution_tasks_status_created ON public.trade_manual_execution_tasks USING btree (status, created_at DESC);


--
-- Name: idx_trade_manual_execution_tasks_type_created; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_manual_execution_tasks_type_created ON public.trade_manual_execution_tasks USING btree (task_type, created_at DESC);


--
-- Name: idx_trade_order; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_order ON public.trades USING btree (order_id);


--
-- Name: idx_trade_portfolio; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_portfolio ON public.trades USING btree (portfolio_id, executed_at);


--
-- Name: idx_trade_tenant_user_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_tenant_user_symbol ON public.trades USING btree (tenant_id, user_id, symbol);


--
-- Name: idx_trade_user_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_trade_user_symbol ON public.trades USING btree (user_id, symbol);


--
-- Name: idx_user_research_pool_added_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_research_pool_added_at ON public.qm_user_research_pool USING btree (added_at DESC);


--
-- Name: idx_user_research_pool_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_research_pool_status ON public.qm_user_research_pool USING btree (status);


--
-- Name: idx_user_research_pool_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_research_pool_user_id ON public.qm_user_research_pool USING btree (user_id);


--
-- Name: idx_user_sessions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_sessions_user_id ON public.user_sessions USING btree (user_id);


--
-- Name: idx_user_strategies_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_strategies_user_id ON public.user_strategies USING btree (user_id);


--
-- Name: idx_user_watchlist_added_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_watchlist_added_at ON public.qm_user_watchlist USING btree (added_at DESC);


--
-- Name: idx_user_watchlist_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_watchlist_user_id ON public.qm_user_watchlist USING btree (user_id);


--
-- Name: idx_users_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_users_tenant_id ON public.users USING btree (tenant_id);


--
-- Name: idx_users_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_users_user_id ON public.users USING btree (user_id);


--
-- Name: ix_admin_data_files_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_data_files_id ON public.admin_data_files USING btree (id);


--
-- Name: ix_admin_data_files_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_data_files_tenant_id ON public.admin_data_files USING btree (tenant_id);


--
-- Name: ix_admin_models_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_models_id ON public.admin_models USING btree (id);


--
-- Name: ix_admin_models_name; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_models_name ON public.admin_models USING btree (name);


--
-- Name: ix_admin_models_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_models_tenant_id ON public.admin_models USING btree (tenant_id);


--
-- Name: ix_admin_models_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_models_user_id ON public.admin_models USING btree (user_id);


--
-- Name: ix_admin_training_jobs_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_training_jobs_id ON public.admin_training_jobs USING btree (id);


--
-- Name: ix_admin_training_jobs_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_training_jobs_tenant_id ON public.admin_training_jobs USING btree (tenant_id);


--
-- Name: ix_admin_training_jobs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_admin_training_jobs_user_id ON public.admin_training_jobs USING btree (user_id);


--
-- Name: ix_backtests_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_backtests_id ON public.backtests USING btree (id);


--
-- Name: ix_backtests_strategy_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_backtests_strategy_id ON public.backtests USING btree (strategy_id);


--
-- Name: ix_backtests_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_backtests_user_id ON public.backtests USING btree (user_id);


--
-- Name: ix_community_audit_logs_action; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_audit_logs_action ON public.community_audit_logs USING btree (action);


--
-- Name: ix_community_audit_logs_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_audit_logs_tenant_id ON public.community_audit_logs USING btree (tenant_id);


--
-- Name: ix_community_audit_logs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_audit_logs_user_id ON public.community_audit_logs USING btree (user_id);


--
-- Name: ix_community_author_follows_author_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_author_follows_author_user_id ON public.community_author_follows USING btree (author_user_id);


--
-- Name: ix_community_author_follows_follower_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_author_follows_follower_user_id ON public.community_author_follows USING btree (follower_user_id);


--
-- Name: ix_community_author_follows_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_author_follows_tenant_id ON public.community_author_follows USING btree (tenant_id);


--
-- Name: ix_community_comments_author_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_comments_author_id ON public.community_comments USING btree (author_id);


--
-- Name: ix_community_comments_parent_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_comments_parent_id ON public.community_comments USING btree (parent_id);


--
-- Name: ix_community_comments_post_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_comments_post_id ON public.community_comments USING btree (post_id);


--
-- Name: ix_community_comments_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_comments_tenant_id ON public.community_comments USING btree (tenant_id);


--
-- Name: ix_community_interactions_comment_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_interactions_comment_id ON public.community_interactions USING btree (comment_id);


--
-- Name: ix_community_interactions_post_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_interactions_post_id ON public.community_interactions USING btree (post_id);


--
-- Name: ix_community_interactions_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_interactions_tenant_id ON public.community_interactions USING btree (tenant_id);


--
-- Name: ix_community_interactions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_interactions_user_id ON public.community_interactions USING btree (user_id);


--
-- Name: ix_community_posts_author_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_posts_author_id ON public.community_posts USING btree (author_id);


--
-- Name: ix_community_posts_category; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_posts_category ON public.community_posts USING btree (category);


--
-- Name: ix_community_posts_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_posts_id ON public.community_posts USING btree (id);


--
-- Name: ix_community_posts_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_posts_tenant_id ON public.community_posts USING btree (tenant_id);


--
-- Name: ix_data_download_orders_order_no; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_data_download_orders_order_no ON public.data_download_orders USING btree (order_no);


--
-- Name: ix_data_download_orders_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_data_download_orders_status ON public.data_download_orders USING btree (status);


--
-- Name: ix_data_download_orders_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_data_download_orders_tenant_id ON public.data_download_orders USING btree (tenant_id);


--
-- Name: ix_data_download_orders_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_data_download_orders_user_id ON public.data_download_orders USING btree (user_id);


--
-- Name: ix_email_verifications_email; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_email_verifications_email ON public.email_verifications USING btree (email);


--
-- Name: ix_email_verifications_expires_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_email_verifications_expires_at ON public.email_verifications USING btree (expires_at);


--
-- Name: ix_email_verifications_is_used; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_email_verifications_is_used ON public.email_verifications USING btree (is_used);


--
-- Name: ix_email_verifications_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_email_verifications_tenant_id ON public.email_verifications USING btree (tenant_id);


--
-- Name: ix_email_verifications_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_email_verifications_user_id ON public.email_verifications USING btree (user_id);


--
-- Name: ix_email_verifications_verification_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_email_verifications_verification_code ON public.email_verifications USING btree (verification_code);


--
-- Name: ix_identity_verifications_id_number; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_identity_verifications_id_number ON public.identity_verifications USING btree (id_number);


--
-- Name: ix_identity_verifications_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_identity_verifications_status ON public.identity_verifications USING btree (status);


--
-- Name: ix_identity_verifications_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_identity_verifications_tenant_id ON public.identity_verifications USING btree (tenant_id);


--
-- Name: ix_identity_verifications_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_identity_verifications_user_id ON public.identity_verifications USING btree (user_id);


--
-- Name: ix_klines_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_klines_symbol ON public.klines USING btree (symbol);


--
-- Name: ix_login_devices_device_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_login_devices_device_id ON public.login_devices USING btree (device_id);


--
-- Name: ix_login_devices_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_login_devices_tenant_id ON public.login_devices USING btree (tenant_id);


--
-- Name: ix_login_devices_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_login_devices_user_id ON public.login_devices USING btree (user_id);


--
-- Name: ix_orders_order_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_orders_order_id ON public.orders USING btree (order_id);


--
-- Name: ix_orders_portfolio_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_portfolio_id ON public.orders USING btree (portfolio_id);


--
-- Name: ix_orders_position_side; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_position_side ON public.orders USING btree (position_side);


--
-- Name: ix_orders_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_status ON public.orders USING btree (status);


--
-- Name: ix_orders_strategy_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_strategy_id ON public.orders USING btree (strategy_id);


--
-- Name: ix_orders_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_symbol ON public.orders USING btree (symbol);


--
-- Name: ix_orders_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_tenant_id ON public.orders USING btree (tenant_id);


--
-- Name: ix_orders_trade_action; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_trade_action ON public.orders USING btree (trade_action);


--
-- Name: ix_orders_trading_mode; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_trading_mode ON public.orders USING btree (trading_mode);


--
-- Name: ix_orders_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_orders_user_id ON public.orders USING btree (user_id);


--
-- Name: ix_password_reset_tokens_email; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_password_reset_tokens_email ON public.password_reset_tokens USING btree (email);


--
-- Name: ix_password_reset_tokens_expires_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_password_reset_tokens_expires_at ON public.password_reset_tokens USING btree (expires_at);


--
-- Name: ix_password_reset_tokens_is_used; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_password_reset_tokens_is_used ON public.password_reset_tokens USING btree (is_used);


--
-- Name: ix_password_reset_tokens_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_password_reset_tokens_tenant_id ON public.password_reset_tokens USING btree (tenant_id);


--
-- Name: ix_password_reset_tokens_token; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_password_reset_tokens_token ON public.password_reset_tokens USING btree (token);


--
-- Name: ix_password_reset_tokens_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);


--
-- Name: ix_payment_methods_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_payment_methods_tenant_id ON public.payment_methods USING btree (tenant_id);


--
-- Name: ix_payment_methods_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_payment_methods_user_id ON public.payment_methods USING btree (user_id);


--
-- Name: ix_payment_transactions_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_payment_transactions_status ON public.payment_transactions USING btree (status);


--
-- Name: ix_payment_transactions_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_payment_transactions_tenant_id ON public.payment_transactions USING btree (tenant_id);


--
-- Name: ix_payment_transactions_transaction_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_payment_transactions_transaction_id ON public.payment_transactions USING btree (transaction_id);


--
-- Name: ix_payment_transactions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_payment_transactions_user_id ON public.payment_transactions USING btree (user_id);


--
-- Name: ix_permissions_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_permissions_code ON public.permissions USING btree (code);


--
-- Name: ix_permissions_resource; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_permissions_resource ON public.permissions USING btree (resource);


--
-- Name: ix_phone_verifications_expires_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_phone_verifications_expires_at ON public.phone_verifications USING btree (expires_at);


--
-- Name: ix_phone_verifications_is_used; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_phone_verifications_is_used ON public.phone_verifications USING btree (is_used);


--
-- Name: ix_phone_verifications_lookup; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_phone_verifications_lookup ON public.phone_verifications USING btree (tenant_id, phone_number, code_type, verification_code);


--
-- Name: ix_phone_verifications_phone_number; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_phone_verifications_phone_number ON public.phone_verifications USING btree (phone_number);


--
-- Name: ix_phone_verifications_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_phone_verifications_tenant_id ON public.phone_verifications USING btree (tenant_id);


--
-- Name: ix_portfolio_snapshots_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolio_snapshots_id ON public.portfolio_snapshots USING btree (id);


--
-- Name: ix_portfolio_snapshots_portfolio_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolio_snapshots_portfolio_id ON public.portfolio_snapshots USING btree (portfolio_id);


--
-- Name: ix_portfolios_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolios_id ON public.portfolios USING btree (id);


--
-- Name: ix_portfolios_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolios_tenant_id ON public.portfolios USING btree (tenant_id);


--
-- Name: ix_portfolios_trading_mode; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolios_trading_mode ON public.portfolios USING btree (trading_mode);


--
-- Name: ix_portfolios_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_portfolios_user_id ON public.portfolios USING btree (user_id);


--
-- Name: ix_position_history_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_position_history_id ON public.position_history USING btree (id);


--
-- Name: ix_position_history_position_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_position_history_position_id ON public.position_history USING btree (position_id);


--
-- Name: ix_positions_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_positions_id ON public.positions USING btree (id);


--
-- Name: ix_positions_portfolio_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_positions_portfolio_id ON public.positions USING btree (portfolio_id);


--
-- Name: ix_positions_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_positions_symbol ON public.positions USING btree (symbol);


--
-- Name: ix_qmt_agent_bindings_account_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_bindings_account_id ON public.qmt_agent_bindings USING btree (account_id);


--
-- Name: ix_qmt_agent_bindings_api_key_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_bindings_api_key_id ON public.qmt_agent_bindings USING btree (api_key_id);


--
-- Name: ix_qmt_agent_bindings_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_bindings_status ON public.qmt_agent_bindings USING btree (status);


--
-- Name: ix_qmt_agent_bindings_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_bindings_tenant_id ON public.qmt_agent_bindings USING btree (tenant_id);


--
-- Name: ix_qmt_agent_bindings_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_bindings_user_id ON public.qmt_agent_bindings USING btree (user_id);


--
-- Name: ix_qmt_agent_sessions_binding_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_sessions_binding_id ON public.qmt_agent_sessions USING btree (binding_id);


--
-- Name: ix_qmt_agent_sessions_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_sessions_tenant_id ON public.qmt_agent_sessions USING btree (tenant_id);


--
-- Name: ix_qmt_agent_sessions_token_hash; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_qmt_agent_sessions_token_hash ON public.qmt_agent_sessions USING btree (token_hash);


--
-- Name: ix_qmt_agent_sessions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_qmt_agent_sessions_user_id ON public.qmt_agent_sessions USING btree (user_id);


--
-- Name: ix_real_account_baselines_scope_time; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_baselines_scope_time ON public.real_account_baselines USING btree (tenant_id, user_id, account_id, first_snapshot_at);


--
-- Name: ix_real_account_ledger_daily_scope_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_scope_date ON public.real_account_ledger_daily_snapshots USING btree (tenant_id, user_id, account_id, snapshot_date);


--
-- Name: ix_real_account_ledger_daily_snapshots_account_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_snapshots_account_id ON public.real_account_ledger_daily_snapshots USING btree (account_id);


--
-- Name: ix_real_account_ledger_daily_snapshots_last_snapshot_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_snapshots_last_snapshot_at ON public.real_account_ledger_daily_snapshots USING btree (last_snapshot_at);


--
-- Name: ix_real_account_ledger_daily_snapshots_snapshot_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_snapshots_snapshot_date ON public.real_account_ledger_daily_snapshots USING btree (snapshot_date);


--
-- Name: ix_real_account_ledger_daily_snapshots_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_snapshots_tenant_id ON public.real_account_ledger_daily_snapshots USING btree (tenant_id);


--
-- Name: ix_real_account_ledger_daily_snapshots_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_ledger_daily_snapshots_user_id ON public.real_account_ledger_daily_snapshots USING btree (user_id);


--
-- Name: ix_real_account_snapshots_account; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_account ON public.real_account_snapshots USING btree (account_id);


--
-- Name: ix_real_account_snapshots_account_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_account_id ON public.real_account_snapshots USING btree (account_id);


--
-- Name: ix_real_account_snapshots_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_date ON public.real_account_snapshots USING btree (snapshot_date);


--
-- Name: ix_real_account_snapshots_scope_date_time; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_scope_date_time ON public.real_account_snapshots USING btree (tenant_id, user_id, account_id, snapshot_date, snapshot_at);


--
-- Name: ix_real_account_snapshots_scope_month_time; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_scope_month_time ON public.real_account_snapshots USING btree (tenant_id, user_id, account_id, snapshot_month, snapshot_at);


--
-- Name: ix_real_account_snapshots_scope_time; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_scope_time ON public.real_account_snapshots USING btree (tenant_id, user_id, account_id, snapshot_at);


--
-- Name: ix_real_account_snapshots_snapshot_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_snapshot_at ON public.real_account_snapshots USING btree (snapshot_at);


--
-- Name: ix_real_account_snapshots_snapshot_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_snapshot_date ON public.real_account_snapshots USING btree (snapshot_date);


--
-- Name: ix_real_account_snapshots_snapshot_month; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_snapshot_month ON public.real_account_snapshots USING btree (snapshot_month);


--
-- Name: ix_real_account_snapshots_tenant; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_tenant ON public.real_account_snapshots USING btree (tenant_id);


--
-- Name: ix_real_account_snapshots_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_tenant_id ON public.real_account_snapshots USING btree (tenant_id);


--
-- Name: ix_real_account_snapshots_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_user ON public.real_account_snapshots USING btree (user_id);


--
-- Name: ix_real_account_snapshots_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_account_snapshots_user_id ON public.real_account_snapshots USING btree (user_id);


--
-- Name: ix_real_trading_preflight_snapshots_snapshot_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_trading_preflight_snapshots_snapshot_date ON public.real_trading_preflight_snapshots USING btree (snapshot_date);


--
-- Name: ix_real_trading_preflight_snapshots_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_trading_preflight_snapshots_tenant_id ON public.real_trading_preflight_snapshots USING btree (tenant_id);


--
-- Name: ix_real_trading_preflight_snapshots_trading_mode; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_trading_preflight_snapshots_trading_mode ON public.real_trading_preflight_snapshots USING btree (trading_mode);


--
-- Name: ix_real_trading_preflight_snapshots_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_real_trading_preflight_snapshots_user_id ON public.real_trading_preflight_snapshots USING btree (user_id);


--
-- Name: ix_risk_rules_is_active; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_risk_rules_is_active ON public.risk_rules USING btree (is_active);


--
-- Name: ix_risk_rules_rule_name; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_risk_rules_rule_name ON public.risk_rules USING btree (rule_name);


--
-- Name: ix_risk_rules_rule_type; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_risk_rules_rule_type ON public.risk_rules USING btree (rule_type);


--
-- Name: ix_roles_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_roles_code ON public.roles USING btree (code);


--
-- Name: ix_simulation_daily_reports_job_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_simulation_daily_reports_job_id ON public.simulation_daily_reports USING btree (job_id);


--
-- Name: ix_simulation_jobs_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_simulation_jobs_tenant_id ON public.simulation_jobs USING btree (tenant_id);


--
-- Name: ix_simulation_jobs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_simulation_jobs_user_id ON public.simulation_jobs USING btree (user_id);


--
-- Name: ix_strategies_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_strategies_id ON public.strategies USING btree (id);


--
-- Name: ix_strategies_name; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_strategies_name ON public.strategies USING btree (name);


--
-- Name: ix_strategies_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_strategies_status ON public.strategies USING btree (status);


--
-- Name: ix_strategies_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_strategies_user_id ON public.strategies USING btree (user_id);


--
-- Name: ix_subscription_plans_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_subscription_plans_code ON public.subscription_plans USING btree (code);


--
-- Name: ix_system_tasks_status; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_system_tasks_status ON public.system_tasks USING btree (status);


--
-- Name: ix_system_tasks_task_type; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_system_tasks_task_type ON public.system_tasks USING btree (task_type);


--
-- Name: ix_trades_order_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_order_id ON public.trades USING btree (order_id);


--
-- Name: ix_trades_portfolio_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_portfolio_id ON public.trades USING btree (portfolio_id);


--
-- Name: ix_trades_position_side; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_position_side ON public.trades USING btree (position_side);


--
-- Name: ix_trades_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_symbol ON public.trades USING btree (symbol);


--
-- Name: ix_trades_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_tenant_id ON public.trades USING btree (tenant_id);


--
-- Name: ix_trades_trade_action; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_trade_action ON public.trades USING btree (trade_action);


--
-- Name: ix_trades_trade_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_trades_trade_id ON public.trades USING btree (trade_id);


--
-- Name: ix_trades_trading_mode; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_trading_mode ON public.trades USING btree (trading_mode);


--
-- Name: ix_trades_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_trades_user_id ON public.trades USING btree (user_id);


--
-- Name: ix_user_audit_logs_action; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_audit_logs_action ON public.user_audit_logs USING btree (action);


--
-- Name: ix_user_audit_logs_created_at; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_audit_logs_created_at ON public.user_audit_logs USING btree (created_at);


--
-- Name: ix_user_audit_logs_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_audit_logs_tenant_id ON public.user_audit_logs USING btree (tenant_id);


--
-- Name: ix_user_audit_logs_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_audit_logs_user_id ON public.user_audit_logs USING btree (user_id);


--
-- Name: ix_user_subscriptions_alipay_agreement_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_subscriptions_alipay_agreement_id ON public.user_subscriptions USING btree (alipay_agreement_id);


--
-- Name: ix_user_subscriptions_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_subscriptions_tenant_id ON public.user_subscriptions USING btree (tenant_id);


--
-- Name: ix_user_subscriptions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_subscriptions_user_id ON public.user_subscriptions USING btree (user_id);


--
-- Name: ix_user_usages_period; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_usages_period ON public.user_usages USING btree (period);


--
-- Name: ix_user_usages_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_usages_tenant_id ON public.user_usages USING btree (tenant_id);


--
-- Name: ix_user_usages_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_usages_user_id ON public.user_usages USING btree (user_id);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_users_is_deleted; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_users_is_deleted ON public.users USING btree (is_deleted);


--
-- Name: ix_users_phone_number; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_users_phone_number ON public.users USING btree (phone_number);


--
-- Name: ix_users_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_users_tenant_id ON public.users USING btree (tenant_id);


--
-- Name: ix_users_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_users_user_id ON public.users USING btree (user_id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: market_data_daily_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX market_data_daily_symbol_idx ON ONLY public.market_data_daily USING btree (symbol);


--
-- Name: qlib_backtest_runs_cleanup_backup_status_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX qlib_backtest_runs_cleanup_backup_status_idx ON public.qlib_backtest_runs_cleanup_backup USING btree (status);


--
-- Name: qlib_backtest_runs_cleanup_backup_tenant_id_created_at_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX qlib_backtest_runs_cleanup_backup_tenant_id_created_at_idx ON public.qlib_backtest_runs_cleanup_backup USING btree (tenant_id, created_at DESC);


--
-- Name: qlib_backtest_runs_cleanup_backup_user_id_created_at_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX qlib_backtest_runs_cleanup_backup_user_id_created_at_idx ON public.qlib_backtest_runs_cleanup_backup USING btree (user_id, created_at DESC);


--
-- Name: stock_daily_new_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_symbol_idx ON ONLY public.stock_daily_latest USING btree (symbol);


--
-- Name: stock_daily_new_2026_01_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_01_symbol_idx ON public.stock_daily_new_2026_01 USING btree (symbol);


--
-- Name: stock_daily_new_2026_01_symbol_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_01_symbol_trade_date_idx ON public.stock_daily_new_2026_01 USING btree (symbol, trade_date);


--
-- Name: stock_daily_new_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_trade_date_idx ON ONLY public.stock_daily_latest USING btree (trade_date);


--
-- Name: stock_daily_new_2026_01_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_01_trade_date_idx ON public.stock_daily_new_2026_01 USING btree (trade_date);


--
-- Name: stock_daily_new_2026_02_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_02_symbol_idx ON public.stock_daily_new_2026_02 USING btree (symbol);


--
-- Name: stock_daily_new_2026_02_symbol_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_02_symbol_trade_date_idx ON public.stock_daily_new_2026_02 USING btree (symbol, trade_date);


--
-- Name: stock_daily_new_2026_02_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_02_trade_date_idx ON public.stock_daily_new_2026_02 USING btree (trade_date);


--
-- Name: stock_daily_new_2026_03_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_03_symbol_idx ON public.stock_daily_new_2026_03 USING btree (symbol);


--
-- Name: stock_daily_new_2026_03_symbol_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_03_symbol_trade_date_idx ON public.stock_daily_new_2026_03 USING btree (symbol, trade_date);


--
-- Name: stock_daily_new_2026_03_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_03_trade_date_idx ON public.stock_daily_new_2026_03 USING btree (trade_date);


--
-- Name: stock_daily_new_2026_04_symbol_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_04_symbol_idx ON public.stock_daily_new_2026_04 USING btree (symbol);


--
-- Name: stock_daily_new_2026_04_symbol_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_04_symbol_trade_date_idx ON public.stock_daily_new_2026_04 USING btree (symbol, trade_date);


--
-- Name: stock_daily_new_2026_04_trade_date_idx; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX stock_daily_new_2026_04_trade_date_idx ON public.stock_daily_new_2026_04 USING btree (trade_date);


--
-- Name: uq_api_keys_access_key; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX uq_api_keys_access_key ON public.api_keys USING btree (access_key);


--
-- Name: uq_engine_dispatch_items_client_order_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX uq_engine_dispatch_items_client_order_id ON public.engine_dispatch_items USING btree (client_order_id) WHERE ((client_order_id IS NOT NULL) AND ((client_order_id)::text <> ''::text));


--
-- Name: uq_qm_user_models_default_per_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX uq_qm_user_models_default_per_user ON public.qm_user_models USING btree (tenant_id, user_id) WHERE (is_default = true);


--
-- Name: stock_daily_new_2026_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_pkey ATTACH PARTITION public.stock_daily_new_2026_01_pkey;


--
-- Name: stock_daily_new_2026_01_symbol_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_symbol_idx ATTACH PARTITION public.stock_daily_new_2026_01_symbol_idx;


--
-- Name: stock_daily_new_2026_01_symbol_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.idx_sdl_symbol_date ATTACH PARTITION public.stock_daily_new_2026_01_symbol_trade_date_idx;


--
-- Name: stock_daily_new_2026_01_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_trade_date_idx ATTACH PARTITION public.stock_daily_new_2026_01_trade_date_idx;


--
-- Name: stock_daily_new_2026_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_pkey ATTACH PARTITION public.stock_daily_new_2026_02_pkey;


--
-- Name: stock_daily_new_2026_02_symbol_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_symbol_idx ATTACH PARTITION public.stock_daily_new_2026_02_symbol_idx;


--
-- Name: stock_daily_new_2026_02_symbol_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.idx_sdl_symbol_date ATTACH PARTITION public.stock_daily_new_2026_02_symbol_trade_date_idx;


--
-- Name: stock_daily_new_2026_02_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_trade_date_idx ATTACH PARTITION public.stock_daily_new_2026_02_trade_date_idx;


--
-- Name: stock_daily_new_2026_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_pkey ATTACH PARTITION public.stock_daily_new_2026_03_pkey;


--
-- Name: stock_daily_new_2026_03_symbol_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_symbol_idx ATTACH PARTITION public.stock_daily_new_2026_03_symbol_idx;


--
-- Name: stock_daily_new_2026_03_symbol_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.idx_sdl_symbol_date ATTACH PARTITION public.stock_daily_new_2026_03_symbol_trade_date_idx;


--
-- Name: stock_daily_new_2026_03_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_trade_date_idx ATTACH PARTITION public.stock_daily_new_2026_03_trade_date_idx;


--
-- Name: stock_daily_new_2026_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_pkey ATTACH PARTITION public.stock_daily_new_2026_04_pkey;


--
-- Name: stock_daily_new_2026_04_symbol_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_symbol_idx ATTACH PARTITION public.stock_daily_new_2026_04_symbol_idx;


--
-- Name: stock_daily_new_2026_04_symbol_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.idx_sdl_symbol_date ATTACH PARTITION public.stock_daily_new_2026_04_symbol_trade_date_idx;


--
-- Name: stock_daily_new_2026_04_trade_date_idx; Type: INDEX ATTACH; Schema: public; Owner: quantmind
--

ALTER INDEX public.stock_daily_new_trade_date_idx ATTACH PARTITION public.stock_daily_new_2026_04_trade_date_idx;


--
-- Name: qlib_backtest_runs trg_auto_populate_id; Type: TRIGGER; Schema: public; Owner: quantmind
--

CREATE TRIGGER trg_auto_populate_id BEFORE INSERT ON public.qlib_backtest_runs FOR EACH ROW EXECUTE FUNCTION public.auto_populate_id();


--
-- Name: admin_data_files admin_data_files_data_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.admin_data_files
    ADD CONSTRAINT admin_data_files_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.admin_models(id) ON DELETE CASCADE;


--
-- Name: backtests backtests_strategy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.backtests
    ADD CONSTRAINT backtests_strategy_id_fkey FOREIGN KEY (strategy_id) REFERENCES public.strategies(id);


--
-- Name: engine_dispatch_batches engine_dispatch_batches_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_batches
    ADD CONSTRAINT engine_dispatch_batches_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.engine_feature_runs(run_id) ON DELETE CASCADE;


--
-- Name: engine_dispatch_items engine_dispatch_items_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_items
    ADD CONSTRAINT engine_dispatch_items_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.engine_dispatch_batches(batch_id) ON DELETE CASCADE;


--
-- Name: engine_dispatch_items engine_dispatch_items_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_dispatch_items
    ADD CONSTRAINT engine_dispatch_items_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.engine_feature_runs(run_id) ON DELETE CASCADE;


--
-- Name: engine_feature_snapshots engine_feature_snapshots_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_feature_snapshots
    ADD CONSTRAINT engine_feature_snapshots_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.engine_feature_runs(run_id) ON DELETE CASCADE;


--
-- Name: engine_signal_scores engine_signal_scores_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.engine_signal_scores
    ADD CONSTRAINT engine_signal_scores_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.engine_feature_runs(run_id) ON DELETE CASCADE;


--
-- Name: identity_verifications identity_verifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.identity_verifications
    ADD CONSTRAINT identity_verifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: payment_methods payment_methods_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_methods
    ADD CONSTRAINT payment_methods_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: payment_transactions payment_transactions_payment_method_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_transactions
    ADD CONSTRAINT payment_transactions_payment_method_id_fkey FOREIGN KEY (payment_method_id) REFERENCES public.payment_methods(id);


--
-- Name: payment_transactions payment_transactions_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_transactions
    ADD CONSTRAINT payment_transactions_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.user_subscriptions(id);


--
-- Name: payment_transactions payment_transactions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.payment_transactions
    ADD CONSTRAINT payment_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: portfolio_snapshots portfolio_snapshots_portfolio_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.portfolio_snapshots
    ADD CONSTRAINT portfolio_snapshots_portfolio_id_fkey FOREIGN KEY (portfolio_id) REFERENCES public.portfolios(id);


--
-- Name: position_history position_history_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.position_history
    ADD CONSTRAINT position_history_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.positions(id);


--
-- Name: positions positions_portfolio_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_portfolio_id_fkey FOREIGN KEY (portfolio_id) REFERENCES public.portfolios(id);


--
-- Name: role_permissions role_permissions_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permissions(id);


--
-- Name: role_permissions role_permissions_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id);


--
-- Name: simulation_daily_reports simulation_daily_reports_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_daily_reports
    ADD CONSTRAINT simulation_daily_reports_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.simulation_jobs(id);


--
-- Name: strategies strategies_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.strategies(id);


--
-- Name: strategies strategies_validated_backtest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_validated_backtest_id_fkey FOREIGN KEY (validated_backtest_id) REFERENCES public.backtests(id);


--
-- Name: trades trades_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.trades
    ADD CONSTRAINT trades_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: user_roles user_roles_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id);


--
-- Name: user_roles user_roles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: user_subscriptions user_subscriptions_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.user_subscriptions
    ADD CONSTRAINT user_subscriptions_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.subscription_plans(id);


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT USAGE ON SCHEMA public TO quantmind;


--
-- PostgreSQL database dump complete
--

\unrestrict A6yow9GXpjaPdE4nqy3fX6gPKaCKxmHsAfrMoxT5lcuFT3BvFYav1gflstrULrw
