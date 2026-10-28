import logging
from aiohttp import web

from ..api.routes import build_app
from ..tdx.client import TdxClient
from ..executor.plan_executor import PlanExecutor
from ..executor.order_tracker import OrderTracker

log = logging.getLogger(__name__)


async def start_http_server(host: str, port: int, token: str, tdx: TdxClient,
                            executor: PlanExecutor, tracker: OrderTracker, sltp,
                            cache_db=None, extra_tokens=None, rate_limiter=None,
                            memory_cache=None, trade_limits_dir="",
                            aidata=None, subscription=None, ths=None,
                            qmt_orders_enabled: bool = False):
    app = build_app(token, tdx, executor, tracker, sltp, cache_db=cache_db, port=port,
                    extra_tokens=extra_tokens, rate_limiter=rate_limiter,
                    memory_cache=memory_cache, trade_limits_dir=trade_limits_dir,
                    aidata=aidata, subscription=subscription, ths=ths,
                    qmt_orders_enabled=qmt_orders_enabled)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"HTTP 桥已监听 http://{host}:{port}")
    return runner
