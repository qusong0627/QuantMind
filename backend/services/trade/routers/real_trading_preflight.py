import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from .real_trading_utils import *
from .real_trading_utils import (
    _check_inference_model_exists,
    _fetch_latest_real_account_snapshot,
    _get_stream_series_redis_client,
    _normalize_identity,
    _parse_bridge_report_ts,
    _parse_user_id,
    _resolve_preflight_symbols,
    _resolve_runner_image_for_mode,
    _upsert_preflight_snapshot,
)
from backend.services.trade.services.manual_execution_service import manual_execution_service
from backend.shared.trade_redis_keys import build_trade_agent_heartbeat_key

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_snapshot_timestamp(raw: Any) -> float | None:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc).timestamp()
        return raw.timestamp()
    if isinstance(raw, str):
        text_raw = raw.strip()
        if not text_raw:
            return None
        try:
            parsed = datetime.fromisoformat(text_raw.replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None

@router.get("/preflight")
async def preflight_check(
    trading_mode: str = "REAL",
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    """
    启动前自检：
    - K8s 客户端（REAL/SHADOW 必需）
    - Redis 连通性（必需）
    - DB 连通性（必需）
    - Runner 镜像配置（REAL/SHADOW 必需）
    - INTERNAL_CALL_SECRET 配置（必需）
    """
    resolved_user_id, resolved_tenant_id = _normalize_identity(auth, user_id=user_id, tenant_id=tenant_id)
    mode = str(trading_mode or "REAL").strip().upper()
    if mode not in {"REAL", "SHADOW", "SIMULATION"}:
        raise HTTPException(status_code=400, detail=f"unsupported trading_mode: {mode}")

    checks = []

    def add_check(
        key: str,
        label: str,
        ok: bool,
        required: bool,
        message: str,
        details: Optional[dict] = None,
    ):
        checks.append(
            {
                "key": key,
                "label": label,
                "ok": ok,
                "required": required,
                "message": message,
                "details": details or {},
            }
        )

    # 1) Redis
    try:
        redis_ok = bool(redis.client.ping())
        add_check(
            "redis",
            "Redis",
            redis_ok,
            True,
            "Redis 已连接" if redis_ok else "Redis 不可达",
        )
    except Exception as e:
        add_check("redis", "Redis", False, True, f"Redis 自检失败: {e}")

    # 2) DB
    try:
        await db.execute(text("SELECT 1"))
        add_check("db", "PostgreSQL", True, True, "数据库连接正常")
    except Exception as e:
        add_check("db", "PostgreSQL", False, True, f"数据库自检失败: {e}")
        await db.rollback()

    # 3) Internal Secret
    internal_secret = str(os.getenv("INTERNAL_CALL_SECRET", "")).strip()
    if internal_secret:
        add_check("internal_secret", "内部密钥", True, True, "INTERNAL_CALL_SECRET 已配置")
    else:
        add_check("internal_secret", "内部密钥", False, True, "缺少 INTERNAL_CALL_SECRET 配置")

    # 4) User ID 格式（执行链路要求可转 int）
    try:
        _parse_user_id(resolved_user_id)
        add_check("user_id", "用户标识", True, True, "用户标识格式合法")
    except HTTPException:
        add_check(
            "user_id",
            "用户标识",
            False,
            True,
            "当前用户ID不是数字，实盘执行链路可能失败",
        )

    try:
        hosted_status = await manual_execution_service.get_default_model_hosted_status(
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
        )
    except Exception as exc:
        hosted_status = {
            "available": False,
            "message": f"读取默认模型托管状态失败: {exc}",
        }
        await db.rollback()
    add_check(
        "signal_pipeline_enabled",
        "自动托管默认模型",
        bool(hosted_status.get("available")),
        False,
        str(hosted_status.get("message") or "默认模型托管状态正常"),
    )
    add_check(
        "latest_signal_run",
        "最新推理批次",
        bool(hosted_status.get("latest_run_id")),
        False,
        (
            f"latest_run_id={hosted_status.get('latest_run_id')}"
            if hosted_status.get("latest_run_id")
            else str(
                hosted_status.get("message")
                or "[WARNING] 未检测到当前用户默认模型的最新完成推理"
            )
        ),
    )

    # 5) Runner 镜像
    image, image_source = _resolve_runner_image_for_mode()
    image_required = mode in {"REAL", "SHADOW"}
    image_ok = bool(image)
    if image_required:
        add_check(
            "strategy_runner_image",
            "Runner 镜像",
            image_ok,
            image_required,
            (
                "已配置 STRATEGY_RUNNER_IMAGE"
                if image_source == "configured"
                else f"未配置 STRATEGY_RUNNER_IMAGE，已回退默认镜像: {image}"
            ),
            {"image": image, "image_source": image_source},
        )

    # 6) 容器编排客户端 (Docker/K8s)
    orchestration_required = mode in {"REAL", "SHADOW"}
    orchestration_ok = bool(k8s_manager.api and k8s_manager.core_api)
    orchestration_label = "Docker 引擎" if k8s_manager.mode == "docker" else "K8s 集群"
    if orchestration_required:
        add_check(
            "orchestration",
            orchestration_label,
            orchestration_ok,
            orchestration_required,
            f"{orchestration_label} 客户端已就绪" if orchestration_ok else f"{orchestration_label} 客户端未初始化",
        )

    # 7) QMT Agent 在线状态（PG 账户快照 + Redis 心跳）
    bridge_required = mode == "REAL"
    account_report: dict | None = None
    if bridge_required:
        heartbeat_key = build_trade_agent_heartbeat_key(resolved_tenant_id, resolved_user_id)
        try:
            account_snapshot = await _fetch_latest_real_account_snapshot(
                db,
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
            )
            heartbeat_raw = redis.client.get(heartbeat_key)
            if not account_snapshot:
                add_check(
                    "qmt_agent_online",
                    "QMT Agent 在线状态",
                    False,
                    bridge_required,
                    "未检测到 PostgreSQL 实盘账户快照，请先等待 QMT Agent 上报并落库",
                )
            elif not heartbeat_raw:
                add_check(
                    "qmt_agent_online",
                    "QMT Agent 在线状态",
                    False,
                    bridge_required,
                    f"未检测到 QMT Agent 心跳上报({heartbeat_key})，请确认 QMT Agent 已连接",
                )
            else:
                account_report = account_snapshot.get("payload_json") or {}
                if not isinstance(account_report, dict):
                    account_report = {}
                try:
                    heartbeat_report = json.loads(heartbeat_raw)
                except Exception as e:
                    add_check(
                        "qmt_agent_online",
                        "QMT Agent 在线状态",
                        False,
                        bridge_required,
                        f"检测到 QMT Agent 心跳无法解析为 JSON: {e}",
                    )
                    heartbeat_report = None

                if not isinstance(heartbeat_report, dict):
                    add_check(
                        "qmt_agent_online",
                        "QMT Agent 在线状态",
                        False,
                        bridge_required,
                        "检测到 QMT Agent 心跳格式异常（非 JSON 对象）",
                    )
                else:
                    account_ts = _parse_snapshot_timestamp(account_snapshot.get("snapshot_at"))
                    heartbeat_ts = _parse_bridge_report_ts(heartbeat_report)
                    if account_ts is None or heartbeat_ts is None:
                        add_check(
                            "qmt_agent_online",
                            "QMT Agent 在线状态",
                            False,
                            bridge_required,
                            "QMT Agent 上报缺少有效时间戳（PG 账户快照或心跳）",
                        )
                    else:
                        account_age_sec = max(0, int(time.time() - account_ts))
                        heartbeat_age_sec = max(0, int(time.time() - heartbeat_ts))
                        account_threshold_sec = int(os.getenv("QMT_AGENT_ACCOUNT_STALE_THRESHOLD_SEC", "120"))
                        heartbeat_threshold_sec = int(os.getenv("QMT_AGENT_HEARTBEAT_STALE_THRESHOLD_SEC", "60"))
                        if account_age_sec <= account_threshold_sec and heartbeat_age_sec <= heartbeat_threshold_sec:
                            add_check(
                                "qmt_agent_online",
                                "QMT Agent 在线状态",
                                True,
                                bridge_required,
                                f"账户快照 {account_age_sec} 秒前，心跳 {heartbeat_age_sec} 秒前",
                            )
                        else:
                            add_check(
                                "qmt_agent_online",
                                "QMT Agent 在线状态",
                                False,
                                bridge_required,
                                f"QMT Agent 上报已过期：账户快照 {account_age_sec}/{account_threshold_sec} 秒，心跳 {heartbeat_age_sec}/{heartbeat_threshold_sec} 秒",
                            )
        except Exception as e:
            add_check(
                "qmt_agent_online",
                "QMT Agent 在线状态",
                False,
                bridge_required,
                f"QMT Agent 检测失败: {e}",
            )
            # 回滚事务以清除 aborted 状态，避免后续查询失败
            await db.rollback()

    # 7.1~7.4) 双向交易专属预检
    margin_enabled = bool(getattr(settings, "ENABLE_MARGIN_TRADING", False))
    add_check(
        "margin_trading_feature",
        "双向交易功能开关",
        True,  # 警告：不阻断启动
        False, # 不阻断
        "ENABLE_MARGIN_TRADING 已开启" if margin_enabled else "[WARNING] ENABLE_MARGIN_TRADING 未开启(部分双向策略可能无法下单)",
    )

    if margin_enabled:
        try:
            pool = get_margin_stock_pool_service(settings.MARGIN_STOCK_POOL_PATH)
            snapshot = pool.snapshot()
            add_check(
                "margin_stock_pool_loaded",
                "融资融券股票池",
                snapshot.record_count > 0,
                True,
                (
                    f"融资融券股票池已加载，共 {snapshot.record_count} 只股票"
                    if snapshot.record_count > 0
                    else "融资融券股票池为空"
                ),
                {"source_path": snapshot.source_path, "record_count": snapshot.record_count},
            )
        except Exception as e:
            add_check(
                "margin_stock_pool_loaded",
                "融资融券股票池",
                False,
                True,
                f"融资融券股票池加载失败: {e}",
            )

        real_short_required = mode == "REAL"
        long_short_enabled = bool(getattr(settings, "ENABLE_LONG_SHORT_REAL", False))
        add_check(
            "long_short_real_feature",
            "实盘多空灰度开关",
            long_short_enabled or not real_short_required,
            real_short_required,
            "ENABLE_LONG_SHORT_REAL 已开启" if long_short_enabled else "ENABLE_LONG_SHORT_REAL 未开启",
        )
        whitelist_users = {
            item.strip()
            for item in str(getattr(settings, "LONG_SHORT_WHITELIST_USERS", "")).split(",")
            if item and item.strip()
        }
        in_whitelist = str(resolved_user_id) in whitelist_users
        add_check(
            "long_short_whitelist",
            "实盘多空白名单",
            in_whitelist or not real_short_required,
            real_short_required,
            "当前用户在 LONG_SHORT_WHITELIST_USERS 白名单内"
            if in_whitelist
            else "当前用户不在 LONG_SHORT_WHITELIST_USERS 白名单",
        )

        short_real_ok = bool(getattr(settings, "ENABLE_SHORT_SELLING_REAL", False))
        add_check(
            "broker_margin_trade_support",
            "信用交易动作支持",
            short_real_ok or not real_short_required,
            real_short_required,
            (
                "实盘信用交易动作已开启"
                if short_real_ok
                else "未开启 ENABLE_SHORT_SELLING_REAL，实盘不会发送融券交易动作"
            ),
        )

        account_has_credit_fields = isinstance(account_report, dict) and any(
            key in account_report for key in ("liabilities", "credit_limit", "short_market_value")
        )
        add_check(
            "margin_account_state",
            "信用账户状态",
            account_has_credit_fields or mode != "REAL",
            mode == "REAL",
            (
                "账户快照已包含信用账户字段"
                if account_has_credit_fields
                else "当前账户快照未包含 liabilities/short_market_value 等信用字段"
            ),
            account_report if account_has_credit_fields else {},
        )
        short_admission_ready = isinstance(account_report, dict) and bool(account_report.get("credit_enabled", False)) and (
            int(account_report.get("shortable_symbols_count") or 0) > 0
        )
        add_check(
            "short_admission_capability",
            "做空准入能力可用",
            short_admission_ready or mode != "REAL",
            mode == "REAL",
            (
                f"credit_enabled={bool(account_report.get('credit_enabled'))}, "
                f"shortable_symbols_count={int(account_report.get('shortable_symbols_count') or 0)}, "
                f"last_short_check_at={account_report.get('last_short_check_at')}"
            )
            if isinstance(account_report, dict)
            else "未检测到账户快照",
        )

    # 8~10) Stream 探针：REAL/SHADOW/SIMULATION 均检测行情质量
    if mode in {"REAL", "SHADOW", "SIMULATION"}:
        # 模拟盘也需要行情来计算成交金额，否则会用随机价格导致金额错误
        stream_required = True
        stream_symbols = _resolve_preflight_symbols()
        series_threshold_sec = int(os.getenv("PREFLIGHT_SERIES_STALE_THRESHOLD_SEC", "180"))
        try:
            stream_redis, stream_redis_host, stream_redis_port = _get_stream_series_redis_client()
            # 显式 ping，确保不是惰性连接误判
            stream_redis.ping()
            matched_symbol = None
            matched_key = None
            latest_age_sec = None
            data_source = "remote_quote_redis"
            for symbol in stream_symbols:
                key = f"market:series:{symbol}"
                latest = stream_redis.zrevrange(key, 0, 0, withscores=True)
                if not latest:
                    continue
                _, score = latest[0]
                age = max(0, int(time.time() - float(score)))
                matched_symbol = symbol
                matched_key = key
                latest_age_sec = age
                break
            if latest_age_sec is None:
                # 交易时段（9:15-15:00）严格检查，非交易时段仅警告
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                is_trading_hours = (
                    now.weekday() < 5
                    and now.hour >= 9
                    and (now.hour < 15 or (now.hour == 9 and now.minute >= 15))
                )
                passed = not is_trading_hours  # 交易时段无数据则失败
                add_check(
                    "stream_series_freshness",
                    "Stream时序序列",
                    passed,
                    stream_required,
                    (
                        f"[WARNING] 未发现可用序列: sample_symbols={stream_symbols}"
                        + (f" (交易时段阻断)" if is_trading_hours else " (非交易时段放行)")
                    ),
                    {
                        "sample_symbols": stream_symbols,
                        "stale_threshold_sec": series_threshold_sec,
                        "series_redis": f"{stream_redis_host}:{stream_redis_port}",
                        "source": data_source,
                        "is_trading_hours": is_trading_hours,
                    },
                )
            else:
                ok = latest_age_sec <= series_threshold_sec
                # 交易时段（9:15-15:00）严格检查，非交易时段仅警告
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                is_trading_hours = (
                    now.weekday() < 5
                    and now.hour >= 9
                    and (now.hour < 15 or (now.hour == 9 and now.minute >= 15))
                )
                passed = ok if is_trading_hours else True
                add_check(
                    "stream_series_freshness",
                    "Stream时序序列",
                    passed,
                    stream_required,
                    (
                        f"时序序列新鲜度正常（{latest_age_sec}s）"
                        if ok
                        else f"[WARNING] 时序序列延迟过高（{latest_age_sec}s）"
                        + (f" (交易时段阻断)" if is_trading_hours else " (非交易时段放行)")
                    ),
                    {
                        "symbol": matched_symbol,
                        "series_key": matched_key,
                        "latest_age_sec": latest_age_sec,
                        "stale_threshold_sec": series_threshold_sec,
                        "series_redis": f"{stream_redis_host}:{stream_redis_port}",
                        "source": data_source,
                        "is_trading_hours": is_trading_hours,
                    },
                )
        except Exception as e:
            # 回退：尝试交易 Redis，避免 remote Redis 临时不可用时完全失明
            try:
                matched_symbol = None
                matched_key = None
                latest_age_sec = None
                for symbol in stream_symbols:
                    key = f"market:series:{symbol}"
                    latest = redis.client.zrevrange(key, 0, 0, withscores=True)
                    if not latest:
                        continue
                    _, score = latest[0]
                    age = max(0, int(time.time() - float(score)))
                    matched_symbol = symbol
                    matched_key = key
                    latest_age_sec = age
                    break
                if latest_age_sec is None:
                    add_check(
                        "stream_series_freshness",
                        "Stream时序序列",
                        False,
                        stream_required,
                        "未发现可用的行情时序序列，请检查 Stream quote->series 写入链路",
                        {
                            "sample_symbols": stream_symbols,
                            "stale_threshold_sec": series_threshold_sec,
                            "fallback": "trade_redis",
                            "remote_probe_error": str(e),
                        },
                    )
                else:
                    ok = latest_age_sec <= series_threshold_sec
                    add_check(
                        "stream_series_freshness",
                        "Stream时序序列",
                    True,  # 警告：不阻断启动
                    stream_required,
                    (
                        f"时序序列新鲜度正常（{latest_age_sec}s）"
                        if ok
                        else f"[WARNING] 时序序列延迟过高（{latest_age_sec}s）"
                    ),
                        {
                            "symbol": matched_symbol,
                            "series_key": matched_key,
                            "latest_age_sec": latest_age_sec,
                            "stale_threshold_sec": series_threshold_sec,
                            "source": "trade_redis(fallback)",
                            "remote_probe_error": str(e),
                        },
                    )
            except Exception as fallback_error:
                add_check(
                    "stream_series_freshness",
                    "Stream时序序列",
                    False,
                    stream_required,
                    f"时序序列检测失败: {fallback_error}",
                    {"remote_probe_error": str(e)},
                )

        # 9) Stream quote 落库速率检测（quotes 表）
        quote_window_min = int(os.getenv("PREFLIGHT_QUOTE_WINDOW_MINUTES", "5"))
        quote_min_count = int(os.getenv("PREFLIGHT_QUOTE_MIN_COUNT", "5"))
        try:
            quote_sql = text("""
                SELECT COUNT(1) AS cnt
                FROM quotes
                WHERE timestamp >= NOW() - make_interval(mins => :window)
                """)
            quote_cnt = int((await db.execute(quote_sql, {"window": quote_window_min})).scalar() or 0)
            quote_ok = quote_cnt >= quote_min_count
            add_check(
                "stream_quote_persist_rate",
                "Stream行情落库",
                True,  # 警告：不阻断启动
                stream_required,
                (
                    f"最近{quote_window_min}分钟落库 {quote_cnt} 条"
                    if quote_ok
                    else f"[WARNING] 最近{quote_window_min}分钟落库过少（{quote_cnt} 条）"
                ),
                {
                    "window_minutes": quote_window_min,
                    "recent_quote_count": quote_cnt,
                    "min_required_count": quote_min_count,
                },
            )
        except Exception as e:
            add_check(
                "stream_quote_persist_rate",
                "Stream行情落库",
                False,
                stream_required,
                f"行情落库检测失败: {e}",
            )
            await db.rollback()

        # 10) Stream K线拉取可用性（只做可用性探针，默认非阻断）
        kline_required = False
        kline_symbol = stream_symbols[0]
        stream_base_url = str(settings.MARKET_DATA_SERVICE_URL or "http://quantmind-stream:8003").rstrip("/")
        kline_url = f"{stream_base_url}/api/v1/klines/{kline_symbol}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                resp = await client.get(
                    kline_url,
                    params={"interval": "1d", "limit": 3, "use_cache": False},
                )
            if resp.status_code != 200:
                add_check(
                    "stream_kline_fetch",
                    "Stream K线抓取",
                    False,
                    kline_required,
                    f"K线接口返回异常: HTTP {resp.status_code}",
                    {"endpoint": kline_url, "status_code": resp.status_code},
                )
            else:
                payload = resp.json() if resp.content else {}
                kline_total = int(payload.get("total", 0) or 0)
                kline_ok = kline_total > 0
                add_check(
                    "stream_kline_fetch",
                    "Stream K线抓取",
                    kline_ok,
                    kline_required,
                    "K线接口可用" if kline_ok else "K线接口可达，但未返回有效数据",
                    {
                        "endpoint": kline_url,
                        "symbol": kline_symbol,
                        "kline_total": kline_total,
                    },
                )
        except Exception as e:
            add_check(
                "stream_kline_fetch",
                "Stream K线抓取",
                False,
                kline_required,
                f"K线探针失败: {e}",
                {"endpoint": kline_url, "symbol": kline_symbol},
            )

    # 11) 模拟盘专用：沙箱进程池与关键表可用性
    simulation_required = mode == "SIMULATION"
    if simulation_required:
        # 11.0 推理模型就绪度（检查生产模型目录是否有模型文件）
        try:
            model_ok, model_detail = _check_inference_model_exists()
            add_check(
                "inference_database_ready",
                "推理模型已就绪",
                model_ok,
                True,
                model_detail,
            )
        except Exception as e:
            add_check(
                "inference_database_ready",
                "推理模型已就绪",
                False,
                True,
                f"推理模型检测失败: {e}",
            )

        # 11.1 沙箱进程池
        try:
            from backend.services.trade.sandbox.manager import sandbox_manager

            workers = list(getattr(sandbox_manager, "_workers", {}).values())
            worker_total = len(workers)
            alive_total = sum(1 for proc in workers if bool(proc and proc.is_alive()))
            pool_ok = alive_total > 0
            add_check(
                "simulation_sandbox_pool",
                "模拟盘沙箱池",
                pool_ok,
                True,
                (
                    f"沙箱进程池可用（alive={alive_total}/{worker_total}）"
                    if pool_ok
                    else "沙箱进程池不可用（无存活 worker）"
                ),
                {"worker_total": worker_total, "alive_total": alive_total},
            )
        except Exception as e:
            add_check(
                "simulation_sandbox_pool",
                "模拟盘沙箱池",
                False,
                True,
                f"沙箱进程池检测失败: {e}",
            )

        # 11.2 模拟盘关键表（防止库被清空后启动才报错）
        try:
            table_probe_sql = text("""
                SELECT
                    to_regclass('public.sim_orders') IS NOT NULL AS sim_orders,
                    to_regclass('public.sim_trades') IS NOT NULL AS sim_trades,
                    to_regclass('public.simulation_fund_snapshots') IS NOT NULL AS simulation_fund_snapshots
                """)
            table_probe_row = (await db.execute(table_probe_sql)).mappings().one()
            missing_tables = [
                name
                for name in (
                    "sim_orders",
                    "sim_trades",
                    "simulation_fund_snapshots",
                )
                if not bool(table_probe_row.get(name))
            ]
            tables_ok = len(missing_tables) == 0
            add_check(
                "simulation_tables",
                "模拟盘数据表",
                tables_ok,
                True,
                "模拟盘关键表已就绪" if tables_ok else f"缺少模拟盘关键表: {', '.join(missing_tables)}",
                {
                    "required_tables": [
                        "sim_orders",
                        "sim_trades",
                        "simulation_fund_snapshots",
                    ],
                    "missing_tables": missing_tables,
                },
            )
        except Exception as e:
            add_check(
                "simulation_tables",
                "模拟盘数据表",
                False,
                True,
                f"模拟盘关键表检测失败: {e}",
            )
            # 回滚事务以清除 aborted 状态
            await db.rollback()

        # 11.3 资金快照任务配置（非阻断，便于排障）
        snapshot_enabled = str(os.getenv("SIM_FUND_SNAPSHOT_ENABLED", "true")).strip().lower() != "false"
        interval_raw = str(os.getenv("SIM_FUND_SNAPSHOT_INTERVAL_SECONDS", "300")).strip()
        try:
            interval_seconds = int(interval_raw)
        except Exception:
            interval_seconds = 300
        snapshot_config_ok = (not snapshot_enabled) or interval_seconds > 0
        add_check(
            "simulation_snapshot_worker_config",
            "模拟盘资金快照任务",
            snapshot_config_ok,
            False,
            (
                f"已启用（interval={interval_seconds}s）"
                if snapshot_enabled and snapshot_config_ok
                else (
                    "已关闭（SIM_FUND_SNAPSHOT_ENABLED=false）"
                    if not snapshot_enabled
                    else "配置异常（SIM_FUND_SNAPSHOT_INTERVAL_SECONDS 应大于 0）"
                )
            ),
            {
                "enabled": snapshot_enabled,
                "interval_seconds": interval_seconds,
            },
        )

    ready = all(item["ok"] for item in checks if item["required"])

    try:
        await _upsert_preflight_snapshot(
            db,
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
            trading_mode=mode,
            ready=ready,
            checks=checks,
        )
    except Exception as e:
        logger.warning("Failed to persist preflight snapshot: %s", e)
        await db.rollback()

    return {
        "ready": ready,
        "mode": mode,
        "user_id": resolved_user_id,
        "tenant_id": resolved_tenant_id,
        "checks": checks,
    }


@router.get("/trading-precheck", response_model=TradingPrecheckResponse)
async def trading_precheck(
    trading_mode: str = "REAL",
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    mode = str(trading_mode or "REAL").strip().upper()
    if mode not in {"REAL", "SHADOW", "SIMULATION"}:
        raise HTTPException(status_code=400, detail=f"unsupported trading_mode: {mode}")
    resolved_user_id, resolved_tenant_id = _normalize_identity(auth)
    return await run_trading_readiness_precheck(
        db,
        mode=mode,
        redis_client=redis.client,
        user_id=resolved_user_id,
        tenant_id=resolved_tenant_id,
    )


@router.get("/preflight/snapshots/daily")
async def list_preflight_snapshots_daily(
    days: int = Query(30, ge=1, le=3650),
    trading_mode: Optional[str] = Query(None, description="REAL/SHADOW/SIMULATION"),
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(auth, user_id=user_id, tenant_id=tenant_id)
    query = (
        select(PreflightSnapshot)
        .where(
            PreflightSnapshot.tenant_id == resolved_tenant_id,
            PreflightSnapshot.user_id == resolved_user_id,
        )
        .order_by(desc(PreflightSnapshot.snapshot_date))
        .limit(days)
    )
    mode = str(trading_mode or "").strip().upper()
    if mode:
        query = query.where(PreflightSnapshot.trading_mode == mode)

    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "snapshot_date": r.snapshot_date.isoformat() if r.snapshot_date else None,
            "tenant_id": r.tenant_id,
            "user_id": r.user_id,
            "trading_mode": r.trading_mode,
            "ready": bool(r.ready),
            "run_count": int(r.run_count or 0),
            "total_checks": int(r.total_checks or 0),
            "passed_checks": int(r.passed_checks or 0),
            "required_failed_count": int(r.required_failed_count or 0),
            "failed_required_keys": r.failed_required_keys or [],
            "last_checked_at": r.last_checked_at.isoformat() if r.last_checked_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "checks": r.checks or [],
        }
        for r in rows
    ]


@router.get("/account")
async def get_account(
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    获取账户资金与持仓。

    只读取 PostgreSQL 中最近一次持久化快照，不再用 Redis 参与展示口径。
    """
    try:
        resolved_user_id, resolved_tenant_id = _normalize_identity(auth, user_id=user_id, tenant_id=tenant_id)
        latest_snapshot = await _fetch_latest_real_account_snapshot(
            db,
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
        )
        if latest_snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="账户信息尚未持久化，请先等待柜台侧代理上报 PostgreSQL 快照",
            )

        account_info = dict(latest_snapshot)
        snapshot_ts = _parse_snapshot_timestamp(account_info.get("snapshot_at"))
        stale_threshold_sec = max(30, int(os.getenv("QMT_AGENT_ACCOUNT_STALE_THRESHOLD_SEC", "120") or 120))
        account_age_sec = None if snapshot_ts is None else max(0.0, time.time() - snapshot_ts)
        account_info["is_online"] = bool(account_age_sec is not None and account_age_sec <= stale_threshold_sec)
        if account_age_sec is not None:
            account_info["account_age_seconds"] = int(account_age_sec)
        if account_info["is_online"] is False:
            account_info["stale_reason"] = f"account_snapshot_stale({int(account_age_sec or 0)}s)"

        # ── 字段归一化 ──────────────────────────────────────────────────
        # 对外暴露语义统一来自 PostgreSQL 最新快照视图：
        #   available_cash  = 可用资金（真正可下单的自由资金）
        #   cash            = 现金总额（= available_cash + 当日委托冻结部分）
        #   frozen_cash     = 冻结资金（委托冻结 + 其他冻结）
        # ───────────────────────────────────────────────────────────────

        try:
            raw_cash = float(account_info.get("cash") or 0.0)
            raw_available = float(account_info.get("available_cash") or 0.0)
            total_asset = float(account_info.get("total_asset") or 0.0)
            market_value = float(account_info.get("market_value") or 0.0)
            reported_frozen = float(account_info.get("frozen_cash") or 0.0)

            if raw_available <= 0.0 and raw_cash > 0.0:
                effective_available = raw_cash
            else:
                effective_available = raw_available

            base_frozen = max(0.0, raw_cash - effective_available)
            gap_frozen = max(0.0, total_asset - market_value - raw_cash)
            calc_frozen = max(base_frozen, gap_frozen)
            final_frozen = max(reported_frozen, calc_frozen)

            account_info["available_cash"] = effective_available
            account_info["cash"] = effective_available
            account_info["frozen_cash"] = final_frozen
            account_info["frozen"] = final_frozen
            account_info["market_value"] = market_value
            account_info["total_asset"] = total_asset
            account_info["baseline"] = {
                "initial_equity": float(account_info.get("initial_equity") or 0.0),
                "day_open_equity": float(account_info.get("day_open_equity") or 0.0),
                "month_open_equity": float(account_info.get("month_open_equity") or 0.0),
            }

            return account_info
        except Exception as e:
            logger.warning(
                "Failed to normalize PostgreSQL account snapshot for tenant=%s user=%s: %s",
                resolved_tenant_id,
                resolved_user_id,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="账户快照字段格式异常，请检查 PostgreSQL 视图口径",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get account info: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取账户信息失败",
        )
