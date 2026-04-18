--
-- PostgreSQL database dump
--

\restrict ZBQLIsU1ot4kDmoZvQCDByPms1dxfdwQUlFoa7eVfbpIp62x32FrjRYLmMaSJ6G

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
    'STOPPED',
    'ARCHIVED'
);


ALTER TYPE public.strategystatus OWNER TO quantmind;

--
-- Name: strategytype; Type: TYPE; Schema: public; Owner: quantmind
--

CREATE TYPE public.strategytype AS ENUM (
    'TOPK_DROPOUT',
    'WEIGHT_STRATEGY',
    'CUSTOM',
    'LONG_SHORT_TOPK',
    'QUANTITATIVE'
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
-- Name: market_data_daily; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.market_data_daily (
    id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    trade_date date NOT NULL,
    open numeric(10,3),
    high numeric(10,3),
    low numeric(10,3),
    close numeric(10,3),
    volume bigint,
    amount numeric(18,2),
    turnover_rate numeric(10,6),
    feat_01 numeric(10,6),
    feat_02 numeric(10,6),
    feat_03 numeric(10,6),
    feat_04 numeric(10,6),
    feat_05 numeric(10,6),
    feat_06 numeric(10,6),
    feat_07 numeric(10,6),
    feat_08 numeric(10,6),
    feat_09 numeric(10,6),
    feat_10 numeric(10,6),
    feat_11 numeric(10,6),
    feat_12 numeric(10,6),
    feat_13 numeric(10,6),
    feat_14 numeric(10,6),
    feat_15 numeric(10,6),
    feat_16 numeric(10,6),
    feat_17 numeric(10,6),
    feat_18 numeric(10,6),
    feat_19 numeric(10,6),
    feat_20 numeric(10,6),
    feat_21 numeric(10,6),
    feat_22 numeric(10,6),
    feat_23 numeric(10,6),
    feat_24 numeric(10,6),
    feat_25 numeric(10,6),
    feat_26 numeric(10,6),
    feat_27 numeric(10,6),
    feat_28 numeric(10,6),
    feat_29 numeric(10,6),
    feat_30 numeric(10,6),
    feat_31 numeric(10,6),
    feat_32 numeric(10,6),
    feat_33 numeric(10,6),
    feat_34 numeric(10,6),
    feat_35 numeric(10,6),
    feat_36 numeric(10,6),
    feat_37 numeric(10,6),
    feat_38 numeric(10,6),
    feat_39 numeric(10,6),
    feat_40 numeric(10,6),
    feat_41 numeric(10,6),
    feat_42 numeric(10,6),
    feat_43 numeric(10,6),
    feat_44 numeric(10,6),
    feat_45 numeric(10,6),
    feat_46 numeric(10,6),
    feat_47 numeric(10,6),
    feat_48 numeric(10,6),
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.market_data_daily OWNER TO quantmind;

--
-- Name: market_data_daily_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.market_data_daily_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.market_data_daily_id_seq OWNER TO quantmind;

--
-- Name: market_data_daily_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.market_data_daily_id_seq OWNED BY public.market_data_daily.id;


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
    is_read boolean DEFAULT false,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
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
    id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    model_id character varying(64),
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_path character varying(500),
    metrics jsonb,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    prediction_trade_date date
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
-- Name: quotes; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.quotes (
    symbol text NOT NULL,
    trade_time timestamp with time zone NOT NULL,
    price numeric(12,4) NOT NULL,
    volume bigint,
    bid_price numeric(12,4),
    ask_price numeric(12,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.quotes OWNER TO quantmind;

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
    created_at timestamp with time zone DEFAULT now()
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
-- Name: simulation_fund_snapshots; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.simulation_fund_snapshots (
    id integer NOT NULL,
    job_id character varying(64) NOT NULL,
    snapshot_date date NOT NULL,
    total_capital numeric(18,2) NOT NULL,
    cash numeric(18,2) NOT NULL,
    position_value numeric(18,2) NOT NULL,
    daily_return numeric(10,6),
    cumulative_return numeric(10,6),
    created_at timestamp with time zone DEFAULT now()
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
    id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    strategy_id character varying(64),
    backtest_id character varying(64),
    status public.simulationstatus DEFAULT 'RUNNING'::public.simulationstatus NOT NULL,
    initial_capital numeric(18,2) NOT NULL,
    current_capital numeric(18,2),
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_at timestamp with time zone,
    stopped_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.simulation_jobs OWNER TO quantmind;

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
    id integer NOT NULL,
    symbol character varying(20) NOT NULL,
    trade_date date NOT NULL,
    open numeric(10,3),
    high numeric(10,3),
    low numeric(10,3),
    close numeric(10,3),
    volume bigint,
    amount numeric(18,2),
    change_pct numeric(10,6),
    turnover_rate numeric(10,6),
    updated_at timestamp with time zone DEFAULT now(),
    code character varying(20),
    stock_name character varying(64),
    total_mv numeric(18,2) DEFAULT 0,
    pe_ttm numeric(10,4) DEFAULT 0,
    pb numeric(10,4) DEFAULT 0,
    pct_change numeric(10,6) DEFAULT 0,
    turnover numeric(18,2) DEFAULT 0,
    is_st integer DEFAULT 0,
    is_hs300 integer DEFAULT 0,
    is_csi1000 integer DEFAULT 0,
    industry character varying(64) DEFAULT ''::character varying
);


ALTER TABLE public.stock_daily_latest OWNER TO quantmind;

--
-- Name: stock_daily_latest_id_seq; Type: SEQUENCE; Schema: public; Owner: quantmind
--

CREATE SEQUENCE public.stock_daily_latest_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.stock_daily_latest_id_seq OWNER TO quantmind;

--
-- Name: stock_daily_latest_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: quantmind
--

ALTER SEQUENCE public.stock_daily_latest_id_seq OWNED BY public.stock_daily_latest.id;


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
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    strategy_type public.strategytype DEFAULT 'TOPK_DROPOUT'::public.strategytype NOT NULL,
    status public.strategystatus DEFAULT 'DRAFT'::public.strategystatus NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    code text,
    cos_url character varying(500),
    code_hash character varying(64),
    file_size integer,
    tags text[] DEFAULT ARRAY[]::text[],
    is_public boolean DEFAULT false,
    backtest_count integer DEFAULT 0,
    view_count integer DEFAULT 0,
    like_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    version integer DEFAULT 1,
    is_verified boolean DEFAULT false NOT NULL,
    execution_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    shared_users jsonb DEFAULT '[]'::jsonb NOT NULL
);


ALTER TABLE public.strategies OWNER TO quantmind;

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
    ai_ide_api_key character varying(128)
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
    updated_at timestamp with time zone
);


ALTER TABLE public.user_subscriptions OWNER TO quantmind;

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
-- Name: users; Type: TABLE; Schema: public; Owner: quantmind
--

CREATE TABLE public.users (
    id integer NOT NULL,
    user_id character varying(64) NOT NULL,
    tenant_id character varying(64) DEFAULT 'default'::character varying NOT NULL,
    username character varying(128) NOT NULL,
    email character varying(255),
    phone_number character varying(32),
    password_hash character varying(255) NOT NULL,
    is_active boolean DEFAULT true,
    is_verified boolean DEFAULT false,
    is_admin boolean DEFAULT false,
    is_locked boolean DEFAULT false,
    last_login_at timestamp with time zone,
    last_login_ip character varying(64),
    login_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    is_deleted boolean DEFAULT false,
    deleted_at timestamp with time zone
);


ALTER TABLE public.users OWNER TO quantmind;

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
-- Name: email_verifications id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.email_verifications ALTER COLUMN id SET DEFAULT nextval('public.email_verifications_id_seq'::regclass);


--
-- Name: login_devices id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.login_devices ALTER COLUMN id SET DEFAULT nextval('public.login_devices_id_seq'::regclass);


--
-- Name: market_data_daily id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.market_data_daily ALTER COLUMN id SET DEFAULT nextval('public.market_data_daily_id_seq'::regclass);


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
-- Name: permissions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);


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
-- Name: simulation_fund_snapshots id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_fund_snapshots ALTER COLUMN id SET DEFAULT nextval('public.simulation_fund_snapshots_id_seq'::regclass);


--
-- Name: simulation_positions id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_positions ALTER COLUMN id SET DEFAULT nextval('public.simulation_positions_id_seq'::regclass);


--
-- Name: stock_daily_latest id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest ALTER COLUMN id SET DEFAULT nextval('public.stock_daily_latest_id_seq'::regclass);


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
-- Name: users id; Type: DEFAULT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


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
-- Name: email_verifications email_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT email_verifications_pkey PRIMARY KEY (id);


--
-- Name: login_devices login_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.login_devices
    ADD CONSTRAINT login_devices_pkey PRIMARY KEY (id);


--
-- Name: market_data_daily market_data_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.market_data_daily
    ADD CONSTRAINT market_data_daily_pkey PRIMARY KEY (id);


--
-- Name: market_data_daily market_data_daily_symbol_trade_date_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.market_data_daily
    ADD CONSTRAINT market_data_daily_symbol_trade_date_key UNIQUE (symbol, trade_date);


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
    ADD CONSTRAINT qm_model_inference_runs_pkey PRIMARY KEY (id);


--
-- Name: qm_model_inference_settings qm_model_inference_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qm_model_inference_settings
    ADD CONSTRAINT qm_model_inference_settings_pkey PRIMARY KEY (tenant_id, user_id, model_id);


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
-- Name: quotes quotes_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.quotes
    ADD CONSTRAINT quotes_pkey PRIMARY KEY (symbol, trade_time);


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
-- Name: simulation_fund_snapshots simulation_fund_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.simulation_fund_snapshots
    ADD CONSTRAINT simulation_fund_snapshots_pkey PRIMARY KEY (id);


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
-- Name: stock_daily_latest stock_daily_latest_pkey; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest
    ADD CONSTRAINT stock_daily_latest_pkey PRIMARY KEY (id);


--
-- Name: stock_daily_latest stock_daily_latest_symbol_key; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.stock_daily_latest
    ADD CONSTRAINT stock_daily_latest_symbol_key UNIQUE (symbol);


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
-- Name: qlib_backtest_runs uq_qlib_backtest_runs_backtest_id; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.qlib_backtest_runs
    ADD CONSTRAINT uq_qlib_backtest_runs_backtest_id UNIQUE (backtest_id);


--
-- Name: real_account_ledger_daily_snapshots uq_real_account_ledger_daily; Type: CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.real_account_ledger_daily_snapshots
    ADD CONSTRAINT uq_real_account_ledger_daily UNIQUE (tenant_id, user_id, account_id, snapshot_date);


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
-- Name: idx_market_data_daily_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_market_data_daily_date ON public.market_data_daily USING btree (trade_date);


--
-- Name: idx_market_data_daily_symbol; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_market_data_daily_symbol ON public.market_data_daily USING btree (symbol);


--
-- Name: idx_market_data_daily_symbol_date; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_market_data_daily_symbol_date ON public.market_data_daily USING btree (symbol, trade_date);


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
-- Name: idx_strategies_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_strategies_user_id ON public.strategies USING btree (user_id);


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
-- Name: idx_user_sessions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_sessions_user_id ON public.user_sessions USING btree (user_id);


--
-- Name: idx_user_strategies_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX idx_user_strategies_user_id ON public.user_strategies USING btree (user_id);


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
-- Name: ix_community_posts_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_community_posts_tenant_id ON public.community_posts USING btree (tenant_id);


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
-- Name: ix_permissions_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_permissions_code ON public.permissions USING btree (code);


--
-- Name: ix_permissions_resource; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_permissions_resource ON public.permissions USING btree (resource);


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
-- Name: ix_subscription_plans_code; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX ix_subscription_plans_code ON public.subscription_plans USING btree (code);


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
-- Name: ix_user_subscriptions_tenant_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_subscriptions_tenant_id ON public.user_subscriptions USING btree (tenant_id);


--
-- Name: ix_user_subscriptions_user_id; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE INDEX ix_user_subscriptions_user_id ON public.user_subscriptions USING btree (user_id);


--
-- Name: uq_api_keys_access_key; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX uq_api_keys_access_key ON public.api_keys USING btree (access_key);


--
-- Name: uq_qm_user_models_default_per_user; Type: INDEX; Schema: public; Owner: quantmind
--

CREATE UNIQUE INDEX uq_qm_user_models_default_per_user ON public.qm_user_models USING btree (tenant_id, user_id) WHERE (is_default = true);


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
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: quantmind
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


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

\unrestrict ZBQLIsU1ot4kDmoZvQCDByPms1dxfdwQUlFoa7eVfbpIp62x32FrjRYLmMaSJ6G

