import argparse
import asyncio
import logging
import os
import signal
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.channel.file_sync import FileSyncChannel
from src.channel.http_server import start_http_server
from src.executor.order_tracker import OrderTracker
from src.executor.plan_executor import PlanExecutor
from src.executor.stop_loss_daemon import StopLossDaemon
from src.tdx.client import TdxClient
from src.utils.config import Config

# Force UTF-8 on stdout/stderr for Chinese text (PowerShell codepage workaround)
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 确定基础目录: exe 用 exe 所在目录, 源码用文件所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志同时输出到文件 (windowed 模式无控制台, 靠日志文件排查)
_log_dir = os.path.join(BASE_DIR, "data", "logs")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler() if sys.stderr else logging.NullHandler(),
        logging.FileHandler(os.path.join(_log_dir, "bridge.log"),
                            encoding="utf-8", mode="a"),
    ])
log = logging.getLogger("bridge-windows")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mode", default="auto", choices=["auto", "http", "file_sync"])
    args = parser.parse_args()

    # PyInstaller exe: 多路径查找 config.yaml (exe同目录 → 当前目录 → 打包资源)
    if getattr(sys, "frozen", False):
        candidates = [
            os.path.join(os.path.dirname(sys.executable), "config.yaml"),
            os.path.join(os.getcwd(), "config.yaml"),
        ]
        for cand in candidates:
            if os.path.exists(cand):
                args.config = cand
                break
        log.info(f"exe 模式, 配置路径: {args.config}")

    cfg = Config(args.config)
    base_dir = os.path.dirname(os.path.abspath(args.config))
    try:
        token = cfg.token()
    except ValueError:
        # token 未配置: 自动生成一个并写回 config.yaml (exe 开箱即用)
        import secrets
        token = secrets.token_hex(32)
        try:
            import yaml as _yaml
            if os.path.exists(args.config):
                with open(args.config, "r", encoding="utf-8") as f:
                    raw = _yaml.safe_load(f) or {}
                raw.setdefault("auth", {})["token"] = token
                with open(args.config, "w", encoding="utf-8") as f:
                    _yaml.safe_dump(raw, f, allow_unicode=True)
                log.info(f"[安全] 已自动生成 token 并写入 {args.config}")
            else:
                log.info(f"[安全] 已自动生成 token: {token}")
        except Exception as e:
            log.warning(f"[安全] 写入 token 失败: {e}, 临时 token: {token}")
        log.info(f"[安全] 桥访问 token: {token}")

    shared_dir = cfg.get("channels.file_sync.shared_dir", "")
    if not shared_dir:
        # 回退: exe 同目录 (本地模式)
        shared_dir = os.path.join(BASE_DIR, "shared")
        os.makedirs(shared_dir, exist_ok=True)
        log.info(f"shared_dir 未配置, 使用本地目录: {shared_dir}")
    # 写回环境变量, 供 stats 网页显示
    os.environ["SHARED_DIR"] = shared_dir

    tdx = TdxClient(cfg.get("tdx.base_url", "http://127.0.0.1:17709/"),
                    timeout=cfg.get("tdx.request_timeout_seconds", 15.0),
                    max_retries=cfg.get("tdx.max_retries", 2))

    log.info("启动步骤1/4: TDX 客户端初始化完成")
    if not tdx.health_check():
        log.warning("启动步骤1: 通达信 17709 不可达, 请确认 TdxW.exe 已运行并登录")
    else:
        log.info("启动步骤1: 通达信 17709 已连通")

    data_dir = cfg.get("order_tracking.state_file", os.path.join(base_dir, "data", "active_orders.json"))
    data_dir = os.path.dirname(data_dir) or os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    log.info("启动步骤2/4: 数据目录就绪")

    report_dir = os.path.join(shared_dir, "execution_reports")
    os.makedirs(report_dir, exist_ok=True)

    executor = PlanExecutor(tdx, report_dir)
    tracker = OrderTracker(tdx, os.path.join(data_dir, "active_orders.json"))
    sltp = StopLossDaemon(tdx, os.path.join(data_dir, "stop_loss_state.json"),
                          poll_interval=cfg.get("sltp_daemon.poll_interval_seconds", 5.0),
                          trade_log=executor.log_trade)
    log.info("启动步骤3/4: 执行器/跟踪器/止损守护初始化完成")

    # SQLite 数据缓存 (K线/股票信息/财务/快照/日志)
    cache_db = None
    if cfg.get("cache.enabled", True):
        try:
            from src.db.cache_db import CacheDb
            cache_db = CacheDb(os.path.join(data_dir, "tdx_cache.db"))
            log.info(f"SQLite 缓存已启用: {os.path.join(data_dir, 'tdx_cache.db')}")
        except Exception as e:
            log.warning(f"SQLite 缓存启用失败: {e}")

    # 安全加固: IP 限流 + 内存缓存
    rate_limiter = None
    memory_cache = None
    try:
        from src.security.rate_limiter import RateLimiter
        from src.tdx.cache import MemoryCache
        rate_limiter = RateLimiter(
            per_minute=cfg.get("security.rate_limit_per_minute", 60),
            write_per_minute=cfg.get("security.write_rate_limit", 10),
            fail_ban_threshold=cfg.get("security.fail_ban_threshold", 5),
            fail_ban_seconds=cfg.get("security.fail_ban_seconds", 30))
        memory_cache = MemoryCache()
        log.info("安全加固已启用: IP限流 + 内存缓存")
    except Exception as e:
        log.warning(f"安全加固启用失败: {e}")

    tasks = []

    if args.mode in ("auto", "http"):
        host = cfg.get("channels.http.host", "0.0.0.0")
        port = cfg.get("channels.http.port", 8550)
        try:
            runner = await start_http_server(host, port, token, tdx, executor, tracker, sltp,
                                             cache_db=cache_db, rate_limiter=rate_limiter,
                                             memory_cache=memory_cache)
            tasks.append(asyncio.create_task(_wait_forever(runner)))
            log.info(f"通达信桥控制台: http://127.0.0.1:{port}/ui")
            # 自动打开网页控制台 (延迟确保服务已监听)
            if cfg.get("ui.auto_open", True):
                tasks.append(asyncio.create_task(_auto_open_ui(port)))
        except OSError as e:
            if "10048" in str(e) or "address already in use" in str(e).lower():
                # 端口已被占用: 桥已在运行, 直接打开现有网页, 不崩溃
                log.warning(f"端口 {port} 已被占用, 桥可能已在运行. 打开现有控制台...")
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{port}/ui")
                log.info("检测到桥已在运行, 本实例退出")
                return
            raise

    if args.mode in ("auto", "file_sync"):
        fs = FileSyncChannel(shared_dir, executor)
        tasks.append(asyncio.create_task(fs.run()))

    # 默认 **关**（原为 True）。这个 `.get` 的字面量才是真正生效的门：
    # `Config.get` 在 config.yaml 存在但缺 `sltp_daemon` 段时返回这里给的默认值，
    # 也就是说 Dockerfile/打包脚本带的 config.yaml 一旦没写这一段，
    # 无论 DEFAULT_CONFIG 改成什么都仍是「开」。
    # 桥自带 daemon 是独立卖出者（触发即市价卖出），守护单已由 QuantMind 的
    # sltp_executor 承担 —— 两者同触发即对同一持仓超卖。
    if cfg.get("sltp_daemon.enabled", False):
        log.warning("⚠️ 桥自带止损守护已启用 —— 确认主系统未同时接管守护单，否则会重复卖出")
        tasks.append(asyncio.create_task(sltp.run()))

    if not tasks:
        log.error("没有启动任何通道")
        return

    log.info(f"启动步骤4/4: 所有任务已启动 ({len(tasks)} 个后台任务), 事件循环运行中")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(tasks)))
        except NotImplementedError:
            pass

    await asyncio.gather(*tasks)


async def _wait_forever(runner):
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()
        raise


async def _shutdown(tasks):
    for t in tasks:
        t.cancel()


async def _auto_open_ui(port: int):
    """延迟 1.5s 后自动用系统浏览器打开控制台."""
    try:
        await asyncio.sleep(1.5)
        url = f"http://127.0.0.1:{port}/ui"
        log.info(f"自动打开控制台: {url}")
        webbrowser.open(url)
    except Exception as e:
        log.warning(f"自动打开控制台失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
