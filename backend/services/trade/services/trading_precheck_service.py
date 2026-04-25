import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis as redis_lib
from backend.services.trade.services.k8s_manager import k8s_manager
from backend.services.trade.services.signal_readiness_service import (
    signal_readiness_service,
)
from backend.shared.trade_redis_keys import (
    pick_first_matching_key,
    trade_agent_heartbeat_key_candidates,
)


def _build_check(key: str, label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "detail": detail,
    }


def _resolve_runner_image() -> tuple[str, str]:
    configured = str(os.getenv("STRATEGY_RUNNER_IMAGE", "")).strip()
    if configured:
        return configured, "configured"
    # 与 k8s_manager 的默认行为保持一致，避免“预检失败但实际可启动”的误报
    default_image = (
        "quantmind-ml-runtime:latest"
        if k8s_manager.mode == "docker"
        else "asia-east1-docker.pkg.dev/gen-lang-client-0953736716/quantmind-repo/quantmind-qlib-runner:latest"
    )
    return default_image, "default"


def _get_env_with_root_fallback(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip()

    try:
        root_env = Path(__file__).resolve().parents[4] / ".env"
        if root_env.exists():
            for line in root_env.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                env_key, env_value = raw.split("=", 1)
                if env_key.strip() == key:
                    return env_value.strip().strip("'").strip('"')
    except Exception:
        return default

    return default


def _get_stream_series_redis_client():
    host = _get_env_with_root_fallback("REDIS_HOST", "localhost")
    port = int(_get_env_with_root_fallback("REDIS_PORT", "6379") or "6379")
    password = _get_env_with_root_fallback("REDIS_PASSWORD", "") or None
    db = int(_get_env_with_root_fallback("REDIS_DB_MARKET", "3"))
    client = redis_lib.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
    )
    return client, host, port


def _resolve_probe_symbols() -> list[str]:
    raw = str(os.getenv("PREFLIGHT_STREAM_SYMBOLS", "000001.SZ,600000.SH")).strip()
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    return symbols or ["000001.SZ", "600000.SH"]


def _parse_bridge_report_ts(report: dict[str, Any]) -> float | None:
    candidates = (
        "timestamp",
        "ts",
        "last_seen",
        "updated_at",
        "report_ts",
        "report_time",
    )
    for key in candidates:
        raw = report.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            ts = float(raw)
            return ts / 1000.0 if ts > 1e12 else ts
        if isinstance(raw, str):
            text_raw = raw.strip()
            if not text_raw:
                continue
            try:
                ts = float(text_raw)
                return ts / 1000.0 if ts > 1e12 else ts
            except Exception:
                pass
            try:
                iso = text_raw.replace("Z", "+00:00")
                return datetime.fromisoformat(iso).timestamp()
            except Exception:
                continue
    return None


def _previous_trading_day(today: date) -> date:
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        session = calendar.date_to_session(today, direction="previous")
        prev_session = calendar.previous_session(session)
        return prev_session.date()
    except Exception:
        candidate = today - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate


async def _query_market_data_readiness(
    db: AsyncSession, expected_trade_date: date
) -> dict[str, Any]:
    feature_cols_count_row = (
        (
            await db.execute(
                text("""
                SELECT COUNT(*) AS cnt
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_data_daily'
                  AND column_name ~ '^feature_[0-9]+$'
                """)
            )
        )
        .mappings()
        .first()
    )
    feature_cols_count = int((feature_cols_count_row or {}).get("cnt") or 0)
    has_48_feature_cols = feature_cols_count >= 48

    dim48_condition = (
        "jsonb_typeof(features) = 'array' AND jsonb_array_length(features) = 48"
    )
    if has_48_feature_cols:
        dim48_columns_condition = " AND ".join(
            [f"feature_{i} IS NOT NULL" for i in range(48)]
        )
        dim48_condition = f"(({dim48_condition}) OR ({dim48_columns_condition}))"

    row = (
        (
            await db.execute(
                text(f"""
                SELECT
                    MAX(date) AS latest_trade_date,
                    COUNT(*) FILTER (WHERE date = :expected_trade_date) AS expected_rows,
                    COUNT(*) FILTER (
                        WHERE date = :expected_trade_date
                          AND {dim48_condition}
                    ) AS expected_dim48_rows
                FROM market_data_daily
                """),
                {"expected_trade_date": expected_trade_date},
            )
        )
        .mappings()
        .first()
    )

    data_stats = dict(row or {})
    latest_trade_date = data_stats.get("latest_trade_date")
    expected_rows = int(data_stats.get("expected_rows") or 0)
    expected_dim48_rows = int(data_stats.get("expected_dim48_rows") or 0)
    passed = (
        str(latest_trade_date) >= expected_trade_date.isoformat()
        and expected_rows > 0
        and expected_dim48_rows == expected_rows
    )
    detail = (
        f"latest_trade_date={latest_trade_date}, expected_trade_date={expected_trade_date.isoformat()}, "
        f"rows={expected_rows}, dim48_rows={expected_dim48_rows}, feature_columns={feature_cols_count}"
    )
    return {
        "passed": passed,
        "detail": detail,
    }


def _check_inference_model_exists() -> tuple[bool, str]:
    """检查推理模型文件是否存在，不查询数据库。"""
    production_dir = Path(
        os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib")
    )
    if not production_dir.exists() or not production_dir.is_dir():
        return False, f"推理模型目录不存在: {production_dir}"
    try:
        model_files = [f for f in production_dir.iterdir() if f.is_file()]
    except Exception as exc:
        return False, f"推理模型目录读取失败: {exc}"
    if not model_files:
        return False, f"推理模型目录为空: {production_dir}"
    return (
        True,
        f"推理模型已存在 (model_dir={production_dir}, files={len(model_files)})",
    )


def _check_stream_series_freshness(redis_client) -> tuple[bool, str]:
    stream_symbols = _resolve_probe_symbols()
    series_threshold_sec = int(os.getenv("PREFLIGHT_SERIES_STALE_THRESHOLD_SEC", "180"))
    try:
        stream_redis, stream_redis_host, stream_redis_port = (
            _get_stream_series_redis_client()
        )
        stream_redis.ping()
        for symbol in stream_symbols:
            key = f"market:series:{symbol}"
            latest = stream_redis.zrevrange(key, 0, 0, withscores=True)
            if not latest:
                continue
            _, score = latest[0]
            latest_age_sec = max(0, int(time.time() - float(score)))
            ok = latest_age_sec <= series_threshold_sec
            return ok, (
                f"symbol={symbol}, latest_age_sec={latest_age_sec}, threshold={series_threshold_sec}, "
                f"source={stream_redis_host}:{stream_redis_port}"
            )
    except Exception as exc:
        remote_error = str(exc)
        try:
            for symbol in stream_symbols:
                key = f"market:series:{symbol}"
                latest = redis_client.zrevrange(key, 0, 0, withscores=True)
                if not latest:
                    continue
                _, score = latest[0]
                latest_age_sec = max(0, int(time.time() - float(score)))
                ok = latest_age_sec <= series_threshold_sec
                return ok, (
                    f"symbol={symbol}, latest_age_sec={latest_age_sec}, threshold={series_threshold_sec}, "
                    f"source=trade_redis, remote_probe_error={remote_error}"
                )
        except Exception as fallback_exc:
            return (
                False,
                f"series_probe_error={fallback_exc}, remote_probe_error={remote_error}",
            )

    return False, f"未发现可用行情序列 sample_symbols={','.join(stream_symbols)}"


def _parse_snapshot_at(snapshot: dict[str, Any]) -> float | None:
    raw = snapshot.get("snapshot_at") or snapshot.get("updated_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc).timestamp()
        return raw.timestamp()
    if isinstance(raw, (int, float)):
        ts = float(raw)
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(raw, str):
        text_raw = raw.strip()
        if not text_raw:
            return None
        try:
            ts = float(text_raw)
            return ts / 1000.0 if ts > 1e12 else ts
        except Exception:
            pass
        try:
            parsed = datetime.fromisoformat(text_raw.replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


async def _check_qmt_agent_online(
    db: AsyncSession, redis_client, tenant_id: str, user_id: str
) -> tuple[bool, str]:
    from backend.services.trade.routers.real_trading_utils import (
        _fetch_latest_real_account_snapshot,
    )

    account_snapshot = await _fetch_latest_real_account_snapshot(
        db, tenant_id=tenant_id, user_id=user_id
    )
    heartbeat_key, heartbeat_raw = pick_first_matching_key(
        redis_client.get,
        trade_agent_heartbeat_key_candidates(tenant_id, user_id),
    )
    if not account_snapshot:
        return (
            False,
            "未检测到 PostgreSQL 实盘账户快照，请先启动 QMT Agent 并等待上报落库",
        )
    if not heartbeat_raw:
        return False, (
            f"未检测到 QMT Agent 心跳上报({trade_agent_heartbeat_key_candidates(tenant_id, user_id)[0]})"
        )

    try:
        heartbeat_report = json.loads(heartbeat_raw)
    except Exception:
        return False, "检测到 QMT Agent 心跳格式异常（非 JSON）"

    if not isinstance(heartbeat_report, dict):
        return False, "检测到 QMT Agent 心跳格式异常（非 JSON 对象）"

    account_ts = _parse_snapshot_at(account_snapshot)
    heartbeat_ts = _parse_bridge_report_ts(heartbeat_report)
    if account_ts is None or heartbeat_ts is None:
        return False, "QMT Agent 上报缺少有效时间戳（PG 账户快照或心跳）"

    account_age_sec = max(0, int(time.time() - account_ts))
    heartbeat_age_sec = max(0, int(time.time() - heartbeat_ts))
    account_threshold_sec = int(
        os.getenv("QMT_AGENT_ACCOUNT_STALE_THRESHOLD_SEC", "120")
    )
    heartbeat_threshold_sec = int(
        os.getenv("QMT_AGENT_HEARTBEAT_STALE_THRESHOLD_SEC", "60")
    )
    passed = (
        account_age_sec <= account_threshold_sec
        and heartbeat_age_sec <= heartbeat_threshold_sec
    )
    detail = (
        f"account_age_sec={account_age_sec}/{account_threshold_sec}, "
        f"heartbeat_age_sec={heartbeat_age_sec}/{heartbeat_threshold_sec}"
    )
    return passed, detail


async def run_trading_readiness_precheck(
    db: AsyncSession,
    *,
    mode: str,
    redis_client,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    normalized_mode = str(mode or "REAL").strip().upper()
    if normalized_mode not in {"REAL", "SHADOW", "SIMULATION"}:
        raise ValueError(f"unsupported trading mode: {mode}")

    checks: list[dict[str, Any]] = []

    expected_trade_date = _previous_trading_day(date.today())

    try:
        redis_ok = bool(redis_client.ping())
        checks.append(
            _build_check(
                "redis",
                "Redis",
                redis_ok,
                "Redis 已连接" if redis_ok else "Redis 不可达",
            )
        )
    except Exception as exc:
        checks.append(_build_check("redis", "Redis", False, f"Redis 自检失败: {exc}"))

    try:
        await db.execute(text("SELECT 1"))
        checks.append(_build_check("db", "PostgreSQL", True, "数据库连接正常"))
    except Exception as exc:
        checks.append(_build_check("db", "PostgreSQL", False, f"数据库自检失败: {exc}"))

    internal_secret = str(os.getenv("INTERNAL_CALL_SECRET", "")).strip()
    checks.append(
        _build_check(
            "internal_secret",
            "内部密钥",
            bool(internal_secret),
            "INTERNAL_CALL_SECRET 已配置"
            if internal_secret
            else "缺少 INTERNAL_CALL_SECRET 配置",
        )
    )

    try:
        int(str(user_id).strip())
        checks.append(_build_check("user_id", "用户标识", True, "用户标识格式合法"))
    except Exception:
        checks.append(
            _build_check(
                "user_id", "用户标识", False, "当前用户ID不是数字，实盘执行链路可能失败"
            )
        )

    signal_stream_publish_enabled = str(
        os.getenv("ENABLE_SIGNAL_STREAM_PUBLISH", "false")
    ).strip().lower() in {"1", "true", "yes", "on"}
    vectorized_matcher_enabled = str(
        os.getenv("ENABLE_VECTORIZED_MATCHER", "false")
    ).strip().lower() in {"1", "true", "yes", "on"}
    signal_pipeline_ok = signal_stream_publish_enabled or vectorized_matcher_enabled
    # SIMULATION 模式仅警告，REAL/SHADOW 阻断
    signal_pipeline_passed = signal_pipeline_ok if normalized_mode != "SIMULATION" else True
    checks.append(
        _build_check(
            "signal_pipeline_enabled",
            "自动托管信号链路已启用",
            signal_pipeline_passed,
            (
                "ENABLE_SIGNAL_STREAM_PUBLISH=true"
                if signal_stream_publish_enabled
                else (
                    "ENABLE_VECTORIZED_MATCHER=true (fallback)"
                    if vectorized_matcher_enabled
                    else (
                        "[阻断] 未启用 signal stream publisher 或 vectorized matcher"
                        if normalized_mode != "SIMULATION"
                        else "[WARNING] 未启用 signal stream publisher 或 vectorized matcher"
                    )
                )
            ),
        )
    )
    try:
        signal_readiness = await signal_readiness_service.evaluate(
            db,
            redis_client=redis_client,
            tenant_id=tenant_id,
            user_id=user_id,
            mode=normalized_mode,
        )
    except Exception as exc:
        signal_readiness = {
            "available": False,
            "status": "check_error",
            "message": f"读取默认模型信号就绪状态失败: {exc}",
            "trading_permission": "blocked"
            if normalized_mode == "REAL"
            else "observe_only",
            "blocking": normalized_mode == "REAL",
        }
        await db.rollback()
    signal_passed = not bool(signal_readiness.get("blocking"))
    checks.append(
        _build_check(
            "signal_readiness",
            "默认模型信号可交易",
            signal_passed,
            (
                str(signal_readiness.get("message") or "默认模型信号状态正常")
                if signal_readiness.get("available")
                else (
                    f"[阻断] {signal_readiness.get('message')}"
                    if signal_readiness.get("blocking")
                    else f"[观察态] {signal_readiness.get('message')}"
                )
            ),
        )
    )

    if normalized_mode == "SIMULATION":
        try:
            model_ok, model_detail = _check_inference_model_exists()
            # SIMULATION 模式推理模型仅警告，允许用户先配置系统
            checks.append(
                _build_check(
                    "inference_database_ready",
                    "推理模型已就绪",
                    True,  # 仅警告，不阻断
                    model_detail if model_ok else f"[WARNING] {model_detail}",
                )
            )
        except Exception as exc:
            checks.append(
                _build_check(
                    "inference_database_ready",
                    "推理模型已就绪",
                    True,  # 仅警告，不阻断
                    f"[WARNING] model_check_error={exc}",
                )
            )

        try:
            from backend.services.trade.sandbox.manager import sandbox_manager

            workers = list(getattr(sandbox_manager, "_workers", {}).values())
            worker_total = len(workers)
            alive_total = sum(1 for proc in workers if bool(proc and proc.is_alive()))
            pool_ok = alive_total > 0
            checks.append(
                _build_check(
                    "simulation_sandbox_pool",
                    "模拟盘进程池",
                    pool_ok,
                    (
                        f"进程池可用（alive={alive_total}/{worker_total}）"
                        if pool_ok
                        else "进程池不可用（无存活 worker）"
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                _build_check(
                    "simulation_sandbox_pool",
                    "模拟盘进程池",
                    False,
                    f"process_pool_error={exc}",
                )
            )

        try:
            stream_ok, stream_detail = _check_stream_series_freshness(redis_client)
            # 交易时段（9:15-15:00 工作日）严格检查，非交易时段仅警告
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            is_trading_hours = (
                now.weekday() < 5  # 周一到周五
                and ((now.hour == 9 and now.minute >= 15) or (now.hour >= 10 and now.hour < 15))
            )
            stream_passed = stream_ok if is_trading_hours else True
            checks.append(
                _build_check(
                    "stream_series_freshness",
                    "实时行情服务已就绪",
                    stream_passed,
                    (
                        f"已就绪: {stream_detail}"
                        if stream_ok
                        else (
                            f"[阻断] 行情不新鲜: {stream_detail} (交易时段)"
                            if is_trading_hours
                            else f"[WARNING] 行情不新鲜: {stream_detail} (非交易时段)"
                        )
                    ),
                )
            )
        except Exception as exc:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            is_trading_hours = (
                now.weekday() < 5
                and ((now.hour == 9 and now.minute >= 15) or (now.hour >= 10 and now.hour < 15))
            )
            checks.append(
                _build_check(
                    "stream_series_freshness",
                    "实时行情服务已就绪",
                    not is_trading_hours,  # 交易时段异常则阻断，非交易时段放行
                    f"[阻断] stream_probe_error={exc}" if is_trading_hours else f"[WARNING] stream_probe_error={exc}",
                )
            )
        return {
            "passed": all(bool(item.get("passed")) for item in checks),
            "checked_at": datetime.now().isoformat(),
            "items": checks,
            "signal_readiness": signal_readiness,
            "trading_permission": signal_readiness.get("trading_permission"),
        }

    production_dir = Path(
        os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib")
    )
    model_exists = production_dir.exists() and production_dir.is_dir()
    model_candidates = [
        str(production_dir / "model.txt"),
        str(production_dir / "metadata.json"),
    ]
    checks.append(
        _build_check(
            "production_model",
            "生产模型存在",
            model_exists,
            f"model_dir={production_dir}; candidates={', '.join(model_candidates)}",
        )
    )

    try:
        model_ok, model_detail = _check_inference_model_exists()
        checks.append(
            _build_check(
                "inference_database_ready",
                "推理模型已就绪",
                model_ok,
                model_detail,
            )
        )
    except Exception as exc:
        checks.append(
            _build_check(
                "inference_database_ready",
                "推理模型已就绪",
                False,
                f"model_check_error={exc}",
            )
        )

    resolved_image, image_source = _resolve_runner_image()
    # 容器编排就绪度检测 (支持 Docker 或 K8s)
    orchestration_ready = bool(k8s_manager.api and k8s_manager.core_api)
    orchestration_label = (
        "容器编排服务 (Docker) 与执行镜像已就绪"
        if k8s_manager.mode == "docker"
        else "Kubernetes 服务与执行镜像已就绪"
    )

    checks.append(
        _build_check(
            "k8s_and_runner_ready",
            orchestration_label,
            orchestration_ready and bool(resolved_image),
            (
                f"orchestration_mode={k8s_manager.mode}, "
                f"orchestration_ready={orchestration_ready}, "
                f"runner_image={resolved_image}, image_source={image_source}"
            ),
        )
    )

    stream_ok, stream_detail = _check_stream_series_freshness(redis_client)
    # 交易时段（9:15-15:00 工作日）严格检查，非交易时段仅警告
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_trading_hours = (
        now.weekday() < 5  # 周一到周五
        and ((now.hour == 9 and now.minute >= 15) or (now.hour >= 10 and now.hour < 15))
    )
    stream_passed = stream_ok if is_trading_hours else True
    checks.append(
        _build_check(
            "stream_series_freshness",
            "实时行情服务已就绪",
            stream_passed,
            (
                f"已就绪: {stream_detail}"
                if stream_ok
                else (
                    f"[阻断] 行情不新鲜: {stream_detail} (交易时段)"
                    if is_trading_hours
                    else f"[WARNING] 行情不新鲜: {stream_detail} (非交易时段)"
                )
            ),
        )
    )

    if normalized_mode == "REAL":
        qmt_ok, qmt_detail = await _check_qmt_agent_online(
            db, redis_client, tenant_id, user_id
        )
        checks.append(
            _build_check(
                "qmt_agent_online",
                "QMT Agent 在线且数据已上报",
                qmt_ok,
                qmt_detail,
            )
        )

    return {
        "passed": all(bool(item.get("passed")) for item in checks),
        "checked_at": datetime.now().isoformat(),
        "items": checks,
        "signal_readiness": signal_readiness,
        "trading_permission": signal_readiness.get("trading_permission"),
    }
