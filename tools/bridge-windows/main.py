import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.channel.file_sync import FileSyncChannel
from src.channel.http_server import start_http_server
from src.executor.order_tracker import OrderTracker
from src.executor.plan_executor import PlanExecutor
from src.executor.stop_loss_daemon import StopLossDaemon
from src.tdx.client import TdxClient
from src.utils.config import Config
from src.security.trade_limits import verify_hard_limits
from src.utils.log_setup import build_handlers

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
# 滚动切割：生产曾出现 1.5GB 的 bridge.log（每个 HTTP 请求一行），见 log_setup。
_log_dir = os.path.join(BASE_DIR, "data", "logs")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=build_handlers(_log_dir),
)
# 访问日志降噪：生产 bridge.log 曾到 1.41GB，绝大多数是 aiohttp.access 的
# 每请求一行（热集轮转每秒数条）。调用审计已由 SQLite tdx_log 承担，
# 这里只保留 WARNING 以上，减少 I/O 与磁盘增长。
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("bridge-windows")


async def main():
    # 交易硬上限（299/秒、1999/日）启动自检：被改动则拒绝启动
    verify_hard_limits()
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
        # ⚠️ 2026-10-23 生产事故：桥在无 BRIDGE_AUTH_TOKEN 的会话里被拉起，
        # token 被静默轮换，平台/采集器立刻全员 401。因此这里必须 CRITICAL，
        # 且**绝不把 token 明文写进日志**。
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
                log.critical(
                    "[安全] auth.token 未配置且未设置 BRIDGE_AUTH_TOKEN——"
                    "已自动生成新 token 并写回 %s。所有既有客户端（平台/采集器）"
                    "将立即鉴权失败；生产环境请注入 BRIDGE_AUTH_TOKEN 或写入固定 token。",
                    args.config)
            else:
                log.critical(
                    "[安全] 配置不存在，已自动生成临时 token（重启即变）；"
                    "部署端请设置 BRIDGE_AUTH_TOKEN")
        except Exception as e:
            log.warning(f"[安全] 写入 token 失败: {e}, 本进程使用临时 token")

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

    executor = PlanExecutor(tdx, report_dir, state_dir=data_dir,
                            default_notify=int(cfg.get("trade.notify", 0) or 0))
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

    # TdxAiData 独立数据接口 + 实时订阅（≤100）
    # aidata: 付费 Key，不走 TdxW；订阅: tq=免费通知 / aidata=付费完整盘口
    aidata = None
    subscription = None
    try:
        from src.tdx.aidata import AidataService
        from src.tdx.subscription import SubscriptionManager
        vendor_dir = cfg.get("aidata.vendor_dir", "./vendor") or ""
        if vendor_dir and not os.path.isabs(vendor_dir):
            vendor_dir = os.path.join(base_dir, vendor_dir)
        aidata = AidataService(
            vendor_dir=vendor_dir,
            ini_path=cfg.get("aidata.ini_path", "") or "",
            enabled=bool(cfg.get("aidata.enabled", True)),
            usage_path=os.path.join(data_dir, "aidata_usage.json"),
            max_rows_per_call=int(cfg.get("aidata.max_rows_per_call", 0) or 0),
            rows_per_token=int(cfg.get("aidata.rows_per_token", 200) or 200),
            min_tokens_per_call=int(cfg.get("aidata.min_tokens_per_call", 1) or 1),
            daily_token_budget=int(cfg.get("aidata.daily_token_budget", 0) or 0))
        sub_state = cfg.get("subscription.state_file", "") or os.path.join(
            data_dir, "subscription.json")
        if not os.path.isabs(sub_state):
            sub_state = os.path.join(base_dir, sub_state)
        tq_sys = cfg.get("subscription.tq_sys_path", "") or os.path.join(
            os.path.dirname(base_dir), "sys")
        subscription = SubscriptionManager(
            aidata=aidata,
            tq_sys_path=tq_sys,
            state_file=sub_state,
            snapshot_fetcher=tdx.get_market_snapshot,
            max_symbols=int(cfg.get("subscription.max_symbols_tq",
                                    cfg.get("aidata.max_symbols", 100)) or 100),
            max_symbols_aidata=int(cfg.get("subscription.max_symbols_aidata", 500) or 500),
            allow_paid_pushes=bool(cfg.get("subscription.allow_paid_pushes", False)))
        a_status = aidata.status()
        log.info("TdxAiData: available=%s token=%s ini=%s",
                 a_status.get("available"), a_status.get("token_configured"),
                 a_status.get("ini_path"))
        # 恢复上次订阅（tq.initialize 阻塞，放后台线程，不挡启动）
        threading.Thread(target=subscription.autostart,
                         name="sub-autostart", daemon=True).start()
        # 自动跟随实盘持仓/委托（默认关；网页订阅面板可开，免费 tq ≤100）
        try:
            from src.tdx.account_codes import fetch_account_codes
            from src.tdx.subscription_sync import SubscriptionAutoSync
            SubscriptionAutoSync(
                subscription,
                lambda source, include: fetch_account_codes(
                    tdx, executor, source=source, include_orders=include)).start()
        except Exception as e:
            log.warning(f"自动订阅同步初始化失败: {e}")
    except Exception as e:
        log.warning(f"TdxAiData/订阅初始化失败: {e}")

    # 同花顺旁路（只读；独立 worker，默认关闭）
    ths = None
    try:
        from src.ths.client import ThsClient
        ths = ThsClient(
            base_url=cfg.get("ths.base_url", "http://127.0.0.1:8765") or
            "http://127.0.0.1:8765",
            enabled=bool(cfg.get("ths.enabled", False)),
            timeout_s=float(cfg.get("ths.timeout_s", 8.0) or 8.0),
            cache_enabled=bool(cfg.get("ths.cache_enabled", True)),
            circuit_failures=int(cfg.get("ths.circuit_failures", 5) or 5),
            circuit_cooldown_s=float(cfg.get("ths.circuit_cooldown_s", 30) or 30),
            max_concurrency=int(cfg.get("ths.max_concurrency", 4) or 4),
            gate_wait_s=float(cfg.get("ths.gate_wait_s", 0.3) or 0.0),
            circuit_timeout_failures=int(
                cfg.get("ths.circuit_timeout_failures", 10) or 10))
        log.info("同花顺旁路: enabled=%s base_url=%s", ths.enabled, ths.base_url)
    except Exception as e:
        log.warning(f"同花顺旁路初始化失败: {e}")

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
            fail_ban_seconds=cfg.get("security.fail_ban_seconds", 30),
            authed_per_minute=int(cfg.get(
                "security.authed_rate_limit_per_minute", 6000) or 6000))
        memory_cache = MemoryCache()
        log.info("安全加固已启用: IP限流 + 内存缓存")
    except Exception as e:
        log.warning(f"安全加固启用失败: {e}")

    async def _close_ths(_app):
        if ths is not None:
            await ths.close()

    tasks = []

    if args.mode in ("auto", "http"):
        host = cfg.get("channels.http.host", "0.0.0.0")
        port = cfg.get("channels.http.port", 8550)
        try:
            runner = await start_http_server(host, port, token, tdx, executor, tracker, sltp,
                                             cache_db=cache_db, rate_limiter=rate_limiter,
                                             memory_cache=memory_cache,
                                             trade_limits_dir=data_dir,
                                             aidata=aidata, subscription=subscription,
                                             ths=ths,
                                             qmt_orders_enabled=bool(
                                                 cfg.get("qmt_orders.enabled", False)))
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


async def _auto_open_ui(port: int, min_interval_sec: float = 600.0):
    """延迟 1.5s 后自动用系统浏览器打开控制台（带去重节流）。

    为什么要节流
    ------------
    桥每被看门狗重启一次就会开一个浏览器标签，而**标签自己就是负载**：
    ``ui.html`` 里 ``setInterval(loadStats, 15000)`` 会每 15s 打一次 ``/stats``。
    2026-10-13 实测到自毁循环——桥被重启 378 次 → 桌面上堆了几百个控制台标签 →
    ``/stats`` 把事件循环按住（当时 ``get_stats()`` 的 COUNT(*) 3-52s）→ ``/health``
    一起停摆 → 看门狗 5s 超时判「假活」再杀一次。标签数量与重启次数互为因果。

    （循环的另一半——``/stats`` 不在循环里跑 COUNT(*)——已在 ``src/api/routes.py``
    的 ``_cache_stats_sync`` 修掉。这条是第二道闸：即便将来又有什么原因导致重启，
    也不会再靠开标签把负载越堆越高。）

    节流判据落在 ``data/.ui_opened_at`` 时间戳文件（跨进程存活，重启不清零）。
    超时或读不到就照常打开——节流是为了止血，不是为了挡住正常使用。
    """
    try:
        url = f"http://127.0.0.1:{port}/ui"
        stamp = os.path.join(BASE_DIR, "data", ".ui_opened_at")
        now = time.time()
        try:
            with open(stamp, "r", encoding="utf-8") as f:
                last = float(f.read().strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if last and now - last < min_interval_sec:
            log.info(
                f"控制台 {int(now - last)}s 前刚打开过，跳过自动打开（{url} 可直接访问）"
            )
            return
        try:
            os.makedirs(os.path.dirname(stamp), exist_ok=True)
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(str(now))
        except OSError:
            pass
        await asyncio.sleep(1.5)
        log.info(f"自动打开控制台: {url}")
        webbrowser.open(url)
    except Exception as e:
        log.warning(f"自动打开控制台失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
