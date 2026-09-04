"""
QuantMind OSS Edition - Unified Service Entry Point
单镜像运行所有后端服务

服务端口分配:
- API Gateway: 8000 (主入口)
- Engine: 8001 (回测引擎)
- Trade: 8002 (交易服务)
- Stream: 8003 (实时行情)
"""

import asyncio
import logging
import multiprocessing as mp
import os
import sys
from typing import Optional

try:
    mp.set_start_method("spawn", force=True)
except RuntimeError:
    pass

from backend.shared.logging_config import setup_logging

setup_logging(service_name="quantmind-oss")
logger = logging.getLogger(__name__)

# 后台管理页面写入的运行时密钥（config/runtime.env），真实环境变量优先
from backend.shared.runtime_secrets import load_runtime_env
_runtime_loaded = load_runtime_env()
if _runtime_loaded:
    logger.info("Loaded %d runtime secrets from runtime.env", _runtime_loaded)

# ── P0-3: 启动时强制 INTERNAL_CALL_SECRET 存在（fail-closed）──
# 训练完成回调依赖此 secret；缺失会导致任意人都能伪造回调。
# 生产环境直接 raise，dev/test 自动生成避免本地启动挂掉。
import secrets as _secrets
if not os.getenv("INTERNAL_CALL_SECRET"):
    _env = os.getenv("QUANTMIND_ENV", "").lower()
    if _env in ("production", "prod"):
        raise RuntimeError(
            "INTERNAL_CALL_SECRET must be set in production. "
            "Set it in .env or generate with: openssl rand -hex 32"
        )
    _auto = _secrets.token_urlsafe(32)
    os.environ["INTERNAL_CALL_SECRET"] = _auto
    if _env == "development":
        logger.warning(
            "INTERNAL_CALL_SECRET auto-generated for development"
        )
    else:
        logger.warning(
            "INTERNAL_CALL_SECRET not set; auto-generated for local. "
            "Set QUANTMIND_ENV=production to require explicit secret."
        )

# ── Qlib 数据目录修复 ──
# features_real 是实际数据目录，Qlib 期望 features/
# 通过 qlib_paths 统一解析，优先 QuantDB 缓存路径
from backend.shared.qlib_paths import resolve_qlib_provider_uri

_qlib_cn = resolve_qlib_provider_uri()
if os.path.isdir(_qlib_cn):
    _features = os.path.join(_qlib_cn, "features")
    _features_real = os.path.join(_qlib_cn, "features_real")
    if os.path.isdir(_features_real) and not os.path.isdir(_features):
        try:
            if sys.platform == "win32":
                # Windows 下 os.symlink 需要管理员/开发者模式，目录联接(junction)免权限
                import subprocess

                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", _features, _features_real],
                    check=True,
                    capture_output=True,
                )
            else:
                os.symlink(_features_real, _features)
            logger.info("Created symlink: %s -> %s", _features_real, _features)
        except Exception:
            logger.warning(
                "Failed to link qlib features dir %s -> %s",
                _features_real,
                _features,
                exc_info=True,
            )


def get_workers_config() -> dict:
    """获取各服务的 worker 数量配置"""
    import os
    # OSS 默认保持 engine 单 worker。
    # 原因：AI-IDE 执行任务状态保存在进程内存中，多 worker 会导致
    # /start 与 /execute/logs/{job_id} 命中不同进程，返回 404 Job not found。
    default_workers = {
        # all 模式已经在独立的 multiprocessing 子进程内运行服务；
        # Uvicorn 再创建多 worker 会形成嵌套 spawn，导致 worker 反复退出。
        "api": 1,
        "engine": 1,
        "trade": 1,
        "stream": 1,
    }
    # 支持环境变量覆盖
    return {
        "api": int(os.getenv("API_WORKERS", default_workers["api"])),
        "engine": int(os.getenv("ENGINE_WORKERS", default_workers["engine"])),
        "trade": int(os.getenv("TRADE_WORKERS", default_workers["trade"])),
        "stream": int(os.getenv("STREAM_WORKERS", default_workers["stream"])),
    }


def get_service_ports() -> dict:
    """获取服务端口配置"""
    return {
        "api": int(os.getenv("API_PORT", "8000")),
        "engine": int(os.getenv("ENGINE_PORT", "8001")),
        "trade": int(os.getenv("TRADE_PORT", "8002")),
        "stream": int(os.getenv("STREAM_PORT", "8003")),
    }


def run_api_service(port: int, workers: int = 1):
    """运行 API 服务"""
    import uvicorn

    logger.info(f"Starting API service on port {port} with {workers} workers")
    uvicorn.run(
        "backend.services.api.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        access_log=False,
    )


def run_engine_service(port: int, workers: int = 4):
    """运行 Engine 服务"""
    import uvicorn

    logger.info(f"Starting Engine service on port {port} with {workers} workers")
    uvicorn.run(
        "backend.services.engine.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        access_log=False,
    )


def run_trade_service(port: int, workers: int = 1):
    """运行 Trade 服务"""
    import uvicorn

    logger.info(f"Starting Trade service on port {port} with {workers} workers")
    uvicorn.run(
        "backend.services.trade.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        access_log=False,
    )


def run_stream_service(port: int, workers: int = 1):
    """运行 Stream 服务"""
    import uvicorn

    logger.info(f"Starting Stream service on port {port} with {workers} workers")
    uvicorn.run(
        "backend.services.stream.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        access_log=False,
    )


def run_single_service(service_name: str, port: int, workers: int = 1):
    """运行单个服务（用于调试或按需启动）"""
    service_runners = {
        "api": run_api_service,
        "engine": run_engine_service,
        "trade": run_trade_service,
        "stream": run_stream_service,
    }

    if service_name not in service_runners:
        raise ValueError(
            f"Unknown service: {service_name}. Available: {list(service_runners.keys())}"
        )

    service_runners[service_name](port, workers)


def run_celery_worker():
    """运行 Celery Worker（处理异步回测任务）"""
    from celery import concurrency
    from backend.services.engine.qlib_app.celery_config import celery_app

    logger.info("Starting Celery Worker for async backtest tasks")
    # 使用 solo 模式单进程执行，避免多进程复杂度
    celery_app.worker_main([
        "worker",
        "--loglevel=info",
        "--concurrency=1",
        "--pool=solo",
    ])


def run_all_services():
    """运行所有服务（多进程模式 + 子进程死亡自动重启 + 健康检查看门狗）"""
    import time
    import urllib.request
    import urllib.error

    ports = get_service_ports()
    workers_config = get_workers_config()

    services = [
        ("api", run_api_service, (ports["api"], workers_config["api"])),
        ("engine", run_engine_service, (ports["engine"], workers_config["engine"])),
        ("trade", run_trade_service, (ports["trade"], workers_config["trade"])),
        ("stream", run_stream_service, (ports["stream"], workers_config["stream"])),
        ("celery", run_celery_worker, ()),
    ]

    # name -> (runner, args, process, restart_count, last_restart_ts, health_failures)
    state: dict = {}

    def _spawn(name: str, runner, args: tuple):
        p = mp.Process(target=runner, args=args, name=f"quantmind-{name}")
        p.start()
        state[name] = {
            "runner": runner,
            "args": args,
            "process": p,
            "restarts": state.get(name, {}).get("restarts", 0),
            "last_restart": time.time(),
            "health_failures": 0,
        }
        return p

    for name, runner, args in services:
        p = _spawn(name, runner, args)
        if name == "celery":
            logger.info(f"Started celery worker (PID: {p.pid})")
        else:
            port, workers = args
            logger.info(f"Started {name} service (PID: {p.pid}) on port {port} with {workers} workers")

    logger.info("=" * 60)
    logger.info("QuantMind OSS Edition - All services started")
    logger.info(f"  API Gateway:  http://localhost:{ports['api']}")
    logger.info(f"  Engine:       http://localhost:{ports['engine']}")
    logger.info(f"  Trade:        http://localhost:{ports['trade']}")
    logger.info(f"  Stream:       http://localhost:{ports['stream']}")
    logger.info("=" * 60)

    # Supervision loop: detect dead/zombie children and respawn with exponential-backoff cap
    MAX_RESTARTS_PER_WINDOW = 5
    RESTART_WINDOW_SEC = 300  # 5 min sliding window
    HEALTH_CHECK_INTERVAL = 30  # seconds between health checks
    HEALTH_TIMEOUT = 10  # seconds to wait for health response
    MAX_HEALTH_FAILURES = 3  # consecutive failures before restart
    SHUTTING_DOWN = False
    last_health_check = time.time()
    startup_grace_sec = 180  # skip health checks during initial startup (allow DuckDB/Qlib initialization)

    def _check_service_health(name: str, port: int) -> bool:
        """Check if a service responds to /health. Returns True if healthy."""
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT)
            return resp.status == 200
        except Exception:
            return False

    def _restart_service(name: str, info: dict, reason: str):
        """Kill and restart a service."""
        p = info["process"]
        logger.error(f"🔴 {name} service {reason}, restarting...")
        try:
            p.terminate()
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
                p.join(timeout=2)
        except Exception:
            pass

        now = time.time()
        if now - info["last_restart"] > RESTART_WINDOW_SEC:
            info["restarts"] = 0

        if info["restarts"] >= MAX_RESTARTS_PER_WINDOW:
            logger.error(
                f"⛔ {name} crashed too many times "
                f"({info['restarts']}/{MAX_RESTARTS_PER_WINDOW} in {RESTART_WINDOW_SEC}s), "
                f"not restarting. Manual intervention required."
            )
            return

        info["restarts"] += 1
        new_p = _spawn(name, info["runner"], info["args"])
        state[name]["restarts"] = info["restarts"]
        logger.info(f"♻️  Restarted {name} service (new PID: {new_p.pid})")

    try:
        while not SHUTTING_DOWN:
            time.sleep(3)
            now = time.time()

            for name, info in list(state.items()):
                p = info["process"]
                # exitcode is None while alive; set when child exits (even zombie reaped here)
                if not p.is_alive() or p.exitcode is not None:
                    exit_code = p.exitcode
                    try:
                        p.join(timeout=1)
                    except Exception:
                        pass

                    if now - info["last_restart"] > RESTART_WINDOW_SEC:
                        info["restarts"] = 0

                    if info["restarts"] >= MAX_RESTARTS_PER_WINDOW:
                        logger.error(
                            f"⛔ {name} crashed too many times "
                            f"({info['restarts']}/{MAX_RESTARTS_PER_WINDOW} in {RESTART_WINDOW_SEC}s), "
                            f"not restarting. Manual intervention required."
                        )
                        continue

                    info["restarts"] += 1
                    logger.error(
                        f"⚠️  {name} service died (exitcode={exit_code}), "
                        f"respawning [attempt {info['restarts']}/{MAX_RESTARTS_PER_WINDOW}]..."
                    )
                    new_p = _spawn(name, info["runner"], info["args"])
                    state[name]["restarts"] = info["restarts"]
                    logger.info(f"♻️  Restarted {name} service (new PID: {new_p.pid})")

            # Health check watchdog (runs every HEALTH_CHECK_INTERVAL seconds, after startup grace)
            if now - last_health_check >= HEALTH_CHECK_INTERVAL and (now - state[list(state.keys())[0]]["last_restart"]) > startup_grace_sec:
                last_health_check = now
                for name, info in list(state.items()):
                    if name == "celery":
                        continue  # celery has no HTTP health endpoint
                    p = info["process"]
                    if not p.is_alive():
                        continue  # already handled by dead-process logic above

                    port_key = name
                    port = ports.get(port_key)
                    if not port:
                        continue

                    if _check_service_health(name, port):
                        if info.get("health_failures", 0) > 0:
                            logger.info(f"✅ {name} service recovered (port {port})")
                        info["health_failures"] = 0
                    else:
                        info["health_failures"] = info.get("health_failures", 0) + 1
                        logger.warning(
                            f"⚠️  {name} health check failed "
                            f"({info['health_failures']}/{MAX_HEALTH_FAILURES})"
                        )
                        if info["health_failures"] >= MAX_HEALTH_FAILURES:
                            _restart_service(name, info, "unresponsive to health checks")
    except KeyboardInterrupt:
        SHUTTING_DOWN = True
        logger.info("Shutting down all services...")
        for name, info in state.items():
            p = info["process"]
            if p.is_alive():
                p.terminate()
                logger.info(f"Terminated {name} service")

        for name, info in state.items():
            p = info["process"]
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
                logger.warning(f"Force killed {name} service")


def _sql_file(rel_under_backend: str) -> str:
    """定位启动期 SQL 文件：优先容器 /app 布局，回退代码相对布局（便携版/裸机）。"""
    p = os.path.join("/app/backend", rel_under_backend)
    if os.path.isfile(p):
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_under_backend)


def _ensure_database_schema():
    """启动前自动检测并创建缺失的数据库表。

    对于新部署（空库），确保所有业务表存在，避免 'relation does not exist' 错误。
    使用 CREATE TABLE IF NOT EXISTS 保证幂等，不会影响已有数据。
    """
    import subprocess

    init_sql = _sql_file("shared/db_init.sql")
    if not os.path.isfile(init_sql):
        logger.warning("数据库初始化 SQL 未找到: %s，跳过自动建表", init_sql)
        return

    db_host = os.getenv("DB_HOST", os.getenv("POSTGRES_HOST", "db"))
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "quantmind"))
    db_user = os.getenv("DB_USER", os.getenv("POSTGRES_USER", "quantmind"))
    db_password = os.getenv("DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "quantmind2026"))

    env = os.environ.copy()
    env["PGPASSWORD"] = db_password

    try:
        result = subprocess.run(
            ["psql", "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name,
             "-f", init_sql, "--quiet", "-v", "ON_ERROR_STOP=0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("数据库表结构自检完成")
        else:
            # 部分表可能已存在，返回非零但无碍
            logger.warning("数据库初始化有警告（可忽略，表可能已存在）: %s",
                           result.stderr[:200] if result.stderr else "")
        # 执行市场分析模块建表（qm_market_sectors 等，不在 db_init.sql 内）
        _ensure_market_analysis_tables(env)
    except FileNotFoundError:
        # psql 客户端可能未安装在镜像中，回退到 Python 方式
        logger.info("psql 未安装，使用 Python 执行数据库初始化")
        _ensure_database_schema_python()
    except Exception as e:
        logger.warning("数据库自动建表失败（不影响启动，后续按需建表）: %s", e)


def _ensure_market_analysis_tables(env: dict) -> None:
    """执行市场分析模块的建表 SQL（qm_market_sectors / qm_sector_constituents 等）。

    db_init.sql 不含这些表，须额外执行 market_analysis/migrations 下的 SQL。
    """
    import subprocess

    migration_sql = _sql_file(
        "services/api/market_analysis/migrations/001_create_market_analysis.sql"
    )
    if not os.path.isfile(migration_sql):
        logger.debug("市场分析建表 SQL 未找到: %s", migration_sql)
        return
    db_host = os.getenv("DB_HOST", os.getenv("POSTGRES_HOST", "db"))
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "quantmind"))
    db_user = os.getenv("DB_USER", os.getenv("POSTGRES_USER", "quantmind"))
    try:
        result = subprocess.run(
            ["psql", "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name,
             "-f", migration_sql, "--quiet", "-v", "ON_ERROR_STOP=0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("市场分析表结构自检完成")
        else:
            logger.warning("市场分析建表有警告（可忽略）: %s",
                           result.stderr[:200] if result.stderr else "")
    except Exception as e:  # noqa: BLE001
        logger.warning("市场分析建表失败（不影响启动）: %s", e)


def _ensure_database_schema_python():
    """psql 不可用时的回退方案：用 Python psycopg2 执行初始化 SQL。"""
    init_sql = _sql_file("shared/db_init.sql")
    if not os.path.isfile(init_sql):
        return

    db_host = os.getenv("DB_HOST", os.getenv("POSTGRES_HOST", "db"))
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "quantmind"))
    db_user = os.getenv("DB_USER", os.getenv("POSTGRES_USER", "quantmind"))
    db_password = os.getenv("DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "quantmind2026"))

    try:
        import psycopg2
        conn = psycopg2.connect(host=db_host, port=db_port, dbname=db_name,
                                user=db_user, password=db_password)
        conn.autocommit = True
        with open(init_sql) as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
            # 市场分析建表（qm_market_sectors 等，不在 db_init.sql 内）
            market_sql = _sql_file(
                "services/api/market_analysis/migrations/001_create_market_analysis.sql"
            )
            if os.path.isfile(market_sql):
                with open(market_sql) as f:
                    cur.execute(f.read())
                logger.info("市场分析表结构自检完成 (Python psycopg2)")
        conn.close()
        logger.info("数据库表结构自检完成 (Python psycopg2)")
    except Exception as e:
        logger.warning("数据库自动建表失败（不影响启动）: %s", e)


def _ensure_seed_admin():
    """启动期确保默认管理员账号存在（自愈）。

    离线部署的 postgres 转储不含 admin 用户，且历史上应用启动不会自动 seed，
    导致首次登录报 'user not found' (401)。这里在启动期幂等地创建默认 admin
    （admin / admin123，user_id=00000001，is_admin=true，显示名 QuantMind、头像 /logo.png），
    并补全 RBAC 角色。
    失败仅告警，不影响主流程启动。
    """
    try:
        import asyncio

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        # 触发 user_app 模型（users / roles / permissions 等）注册到 Base.metadata
        from backend.services.api.user_app.models.user import Base, User  # noqa: F401
        from backend.services.api.user_app.models import rbac  # noqa: F401
        from backend.services.api.user_app.services.seed_data import init_admin_data

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.warning("DATABASE_URL 未设置，跳过默认管理员 seed")
            return

        engine = create_async_engine(db_url)
        async_session = async_sessionmaker(engine, expire_on_commit=False)

        async def _run():
            # 幂等建表：确保 users / roles / permissions / user_roles 等存在
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_session() as session:
                await init_admin_data(session)
            await engine.dispose()

        asyncio.run(_run())
        logger.info("默认管理员账号 seed 完成")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "默认管理员 seed 失败（不影响启动，可手动执行 seed_data.py）: %s", e
        )


def main():
    """主入口"""
    # 启动前确保数据库表结构完整
    _ensure_database_schema()
    # 启动期确保默认管理员账号存在（幂等，失败不影响启动）
    _ensure_seed_admin()

    service_mode = os.getenv("SERVICE_MODE", "all").lower().strip()
    ports = get_service_ports()
    workers_config = get_workers_config()

    logger.info(f"QuantMind OSS Edition - Service Mode: {service_mode}")

    if service_mode == "all":
        run_all_services()
    elif service_mode in ("api", "engine", "trade", "stream"):
        run_single_service(service_mode, ports[service_mode], workers_config[service_mode])
    else:
        logger.error(f"Unknown SERVICE_MODE: {service_mode}")
        logger.info("Valid modes: all, api, engine, trade, stream")
        sys.exit(1)


if __name__ == "__main__":
    main()
