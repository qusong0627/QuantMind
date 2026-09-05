import html as _html
import os
import re
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.services.api.routers import (
    auth,
    inquiry,
    notifications,
    profiles,
    trading_calendar,
    users,
)
from backend.services.api.routers.asset import router as asset_router
from backend.services.api.routers.admin import admin_router
from backend.services.api.routers.ai_ide_proxy import router as ai_ide_proxy_router
from backend.services.api.routers.community.router import router as community_router
from backend.services.api.routers.data_dashboard import router as data_dashboard_router
from backend.services.api.routers.data_gateway_proxy import router as data_gateway_proxy_router
from backend.services.api.routers.hub_proxy import router as hub_proxy_router
from backend.services.api.routers.qwenpaw_proxy import router as qwenpaw_proxy_router
from backend.services.api.routers.qwenpaw_ui_proxy import router as qwenpaw_ui_proxy_router
from backend.services.api.routers.engine_proxy import router as engine_proxy_router
from backend.services.api.routers.files import router as files_router
from backend.services.api.market_analysis.router import router as market_analysis_router
from backend.services.api.routers.market_kline import router as market_kline_router
from backend.services.api.routers.model_training import router as model_training_router
from backend.services.api.routers.training_per_model import build_per_model_router
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.api.routers.news import router as news_router
from backend.services.api.routers.research import router as research_router
from backend.services.api.routers.stocks_search import router as stocks_search_router
from backend.services.api.routers.stock_terminal import router as stock_terminal_router
from backend.services.api.routers.system import router as system_router
from backend.services.api.routers.trade_proxy import router as trade_proxy_router
from backend.services.api.routers.public_sync import router as public_sync_router
from backend.services.api.routers.ws_proxy import router as ws_proxy_router
from backend.services.api.user_app.api.v1.api_keys import router as api_keys_router
from backend.services.api.user_app.api.v1.subscriptions import (
    router as subscriptions_router,
)
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.database_pool import init_default_databases as init_sync_db_pool
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger, setup_logging
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import (
    build_metrics_response,
    set_service_health,
)

setup_logging(service_name="quantmind-api")
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = datetime.now(timezone.utc)
    app.state.startup_healthy = True
    try:
        await init_unified_config(service_name="quantmind-api")
        init_sync_db_pool(pool_size=20)

        from backend.shared.database_manager_v2 import init_database

        await init_database()

        from backend.services.api.routers.admin.model_management import (
            ensure_admin_tables,
        )

        await ensure_admin_tables()
        from backend.shared.model_registry import model_registry_service

        await model_registry_service.ensure_tables()
        from backend.services.engine.services.model_inference_persistence import (
            model_inference_persistence,
        )

        await model_inference_persistence.ensure_tables()
        from backend.services.engine.services.model_inference_batch_persistence import (
            model_inference_batch_persistence,
        )

        await model_inference_batch_persistence.ensure_tables()
        # from backend.services.api.routers.research import ensure_research_tables
        # await ensure_research_tables()

        logger.info("✅ QuantMind API initialized")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ API initialization failed: {e}", exc_info=True)

    # 节点性能历史采样器（1min 滚动写 Redis；独立于启动健康，采样失败不致命）
    try:
        from backend.services.api.routers.admin.node_history import (
            start_node_history_sampler,
            stop_node_history_sampler,
        )

        start_node_history_sampler()
    except Exception as e:
        logger.error(f"❌ node-history sampler start failed: {e}", exc_info=True)

    try:
        from backend.shared.system_events import record_system_event_async

        ok = bool(app.state.startup_healthy)
        await record_system_event_async(
            event_type="service_lifecycle",
            level="info" if ok else "error",
            source="quantmind-api",
            title="API 服务启动完成" if ok else "API 服务启动异常",
            message="QuantMind API 启动完成" if ok else "API 启动存在初始化失败，请检查日志",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass

    set_service_health("quantmind-api", bool(app.state.startup_healthy))
    yield
    try:
        await stop_node_history_sampler()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="service_lifecycle",
            level="info",
            source="quantmind-api",
            title="API 服务关闭",
            message="QuantMind API 正常关闭",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass
    logger.info("🔚 QuantMind API shutdown complete")


app = FastAPI(
    title="QuantMind Consolidated API",
    version="2.0.0",
    description="用户、认证、交易、引擎统一网关服务",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

# 1. 中间件
install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-api")

# 2. 注册具体业务路由 (高优先级)
# 使用环境变量或默认路径，Docker 容器中 /data/uploads，本地开发 data/uploads
uploads_dir = os.environ.get("UPLOADS_DIR", "/data/uploads" if os.path.exists("/data/uploads") else "data/uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

# 文档静态资源（供管理后台"打开部署指南"等链接使用）
# 容器内 /app/docs（由 docker bind mount 挂入），本地开发用仓库根 docs/
_docs_dir = "/app/docs" if os.path.exists("/app/docs") else os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs")
)
if os.path.isdir(_docs_dir):
    app.mount("/static/docs", StaticFiles(directory=_docs_dir), name="docs")

    # Markdown 文档渲染接口（美化版 HTML 预览）— 零依赖（仅用 stdlib html + 正则），
    # 覆盖标题/段落/列表/代码块/链接/表格等常用语法，足以让"打开部署指南"看到样式化页面
    def _resolve_doc(name: str) -> Path:
        # 安全：禁止 .. 与绝对路径
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="invalid doc name")
        p = (Path(_docs_dir) / name).resolve()
        root = Path(_docs_dir).resolve()
        if root not in p.parents and p != root and root not in p.parents:
            raise HTTPException(status_code=400, detail="invalid doc name")
        if not p.is_file():
            raise HTTPException(status_code=404, detail="doc not found")
        return p

    def _md_to_html(text: str) -> str:
        out = []
        in_code = False
        in_table = False
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.strip().startswith("```"):
                if in_code:
                    out.append("</code></pre>")
                    in_code = False
                else:
                    lang = line.strip().lstrip("`").strip()
                    cls = f' class="language-{_html.escape(lang)}"' if lang else ""
                    out.append(f"<pre{cls}><code>")
                    in_code = True
                continue
            if in_code:
                out.append(_html.escape(line))
                continue
            if line.startswith("|") and "|" in line[1:]:
                # 简易表格
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(re.match(r"^[-:]+$", c) for c in cells):
                    continue
                tag = "th" if not in_table else "td"
                out.append("<tr>" + "".join(f"<{tag}>{_md_to_inline(c)}</{tag}>" for c in cells) + "</tr>")
                in_table = True
                continue
            else:
                if in_table:
                    out.append("</table>")
                    in_table = False
            m = re.match(r"^(#{1,6})\s+(.+)$", line)
            if m:
                level = len(m.group(1))
                out.append(f"<h{level}>{_md_to_inline(m.group(2))}</h{level}>")
                continue
            if re.match(r"^\s*[-*+]\s+", line):
                content = re.sub(r"^\s*[-*+]\s+", "", line)
                if not out or not out[-1].startswith("<ul"):
                    out.append("<ul>")
                out.append(f"<li>{_md_to_inline(content)}</li>")
                continue
            m = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
            if m:
                if not out or not out[-1].startswith("<ol"):
                    out.append("<ol>")
                out.append(f"<li>{_md_to_inline(m.group(2))}</li>")
                continue
            if re.match(r"^\s*>\s*", line):
                content = re.sub(r"^\s*>\s*", "", line)
                out.append(f"<blockquote>{_md_to_inline(content)}</blockquote>")
                continue
            if not line.strip():
                if out and out[-1] not in ("</ul>", "</ol>", "</table>", "</blockquote>"):
                    out.append("")
                continue
            out.append(f"<p>{_md_to_inline(line)}</p>")
        if in_table:
            out.append("</table>")
        # 合并连续 <ul>/<ol> 块
        html_out = "\n".join(out)
        html_out = re.sub(r"(<ul>\s*</ul>)", "", html_out)
        # 包裹表格
        html_out = re.sub(r"(<tr>.*?</tr>(?:\s*<tr>.*?</tr>)*)", r"<table>\1</table>", html_out, flags=re.S)
        return html_out

    def _md_to_inline(s: str) -> str:
        s = _html.escape(s)
        # 反引号代码
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # 粗体 / 斜体
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        # 链接 [txt](url)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return s

    @app.get("/api/v1/docs/render", response_class=HTMLResponse)
    def render_doc(name: str = Query(..., description="docs 下的 markdown 文件名"), title: str | None = None):
        """渲染 docs/*.md 为带样式的 HTML 页面，供管理后台"打开部署指南"使用。"""
        p = _resolve_doc(name)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="gbk", errors="ignore")
        body = _md_to_html(text)
        display_title = title or p.stem
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>{_html.escape(display_title)} · QuantMind 文档</title>
<style>
  :root {{
    --bg: #f8fafc; --fg: #0f172a; --muted: #64748b; --accent: #6366f1;
    --code-bg: #f1f5f9; --border: #e2e8f0; --blockquote: #6366f1;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0; background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", sans-serif;
    line-height: 1.7;
  }}
  .container {{
    max-width: 920px; margin: 0 auto; padding: 40px 32px 80px;
    background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  h1 {{ font-size: 28px; padding-bottom: 12px; border-bottom: 2px solid var(--accent); margin-top: 0; }}
  h2 {{ font-size: 22px; margin-top: 36px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }}
  h3 {{ font-size: 18px; margin-top: 28px; color: #1e293b; }}
  h4 {{ font-size: 16px; margin-top: 24px; color: #334155; }}
  p {{ margin: 12px 0; }}
  a {{ color: var(--accent); text-decoration: none; border-bottom: 1px dashed var(--accent); }}
  a:hover {{ background: rgba(99, 102, 241, 0.08); }}
  code {{
    font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.9em; background: var(--code-bg); padding: 2px 6px; border-radius: 4px;
    color: #be185d;
  }}
  pre {{
    background: #0f172a; color: #e2e8f0; padding: 16px 20px;
    border-radius: 8px; overflow-x: auto; line-height: 1.55; margin: 16px 0;
  }}
  pre code {{ background: transparent; color: inherit; padding: 0; }}
  blockquote {{
    margin: 16px 0; padding: 8px 16px;
    border-left: 4px solid var(--blockquote); background: rgba(99, 102, 241, 0.06);
    color: #475569; border-radius: 0 6px 6px 0;
  }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }}
  th, td {{ padding: 8px 12px; border: 1px solid var(--border); text-align: left; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #f8fafc; }}
  ul, ol {{ padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  hr {{ border: 0; border-top: 1px dashed var(--border); margin: 24px 0; }}
  .doc-meta {{
    color: var(--muted); font-size: 12px; margin-bottom: 20px;
    padding-bottom: 8px; border-bottom: 1px solid var(--border);
  }}
  .doc-meta a {{ color: var(--accent); border-bottom: 1px solid var(--accent); }}
  @media print {{
    body {{ background: #fff; }}
    .container {{ box-shadow: none; max-width: 100%; padding: 20px; }}
  }}
</style>
</head><body>
<div class="container">
  <div class="doc-meta">
    QuantMind 内置文档 ·
    <a href="/static/docs/{_html.escape(p.name)}" target="_blank" rel="noopener">下载原始 Markdown</a>
  </div>
{body}
</div>
</body></html>"""
        )
app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])
app.include_router(community_router)
app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
app.include_router(profiles.router, prefix="/api/v1/profiles", tags=["Profiles"])
app.include_router(notifications.router, prefix="/api/v1", tags=["Notifications"])
app.include_router(inquiry.router, prefix="/api/v1", tags=["Inquiry"])
app.include_router(files_router, prefix="/api/v1")
app.include_router(public_sync_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1/admin")
app.include_router(
    model_training_router, prefix="/api/v1/models", tags=["ModelTraining"]
)
app.include_router(
    build_per_model_router(get_current_user),
    prefix="/api/v1/models",
    tags=["ModelTraining"],
)
app.include_router(research_router)
app.include_router(stocks_search_router)
app.include_router(stock_terminal_router)
app.include_router(trading_calendar.router)
app.include_router(system_router)
app.include_router(api_keys_router, prefix="/api/v1")
app.include_router(asset_router, prefix="/api/v1/asset", tags=["Asset"])
app.include_router(market_kline_router)
app.include_router(market_analysis_router)
app.include_router(
    subscriptions_router, prefix="/api/v1/subscription", tags=["Subscriptions"]
)

# 3. 注册代理路由 (低优先级，兜底捕获)
app.include_router(ws_proxy_router)  # WebSocket 代理，优先级最高
app.include_router(qwenpaw_ui_proxy_router)  # QwenPaw UI 代理（必须优先于 engine_proxy）
app.include_router(engine_proxy_router)
app.include_router(trade_proxy_router)
app.include_router(ai_ide_proxy_router)
app.include_router(qwenpaw_proxy_router)
app.include_router(news_router)
app.include_router(data_gateway_proxy_router)
app.include_router(hub_proxy_router)
app.include_router(data_dashboard_router)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    startup_healthy = bool(getattr(app.state, "startup_healthy", True))
    set_service_health("quantmind-api", startup_healthy)
    return {
        "status": "healthy" if startup_healthy else "degraded",
        "service": "quantmind-api",
    }


@app.get("/")
async def root():
    return {"message": "QuantMind API Service V2 is running"}


@app.get("/metrics")
async def metrics():
    return build_metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
