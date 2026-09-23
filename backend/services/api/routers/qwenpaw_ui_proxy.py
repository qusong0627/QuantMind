"""
QwenPaw Web UI 反向代理 — 将 QwenPaw 容器的完整 Web 界面
通过 /api/v1/qwenpaw-ui/ 路径暴露给前端 iframe 嵌入使用。

同时也代理 QwenPaw 的 API 调用（/api/v1/qwenpaw-api/*），
使 Web 浏览器模式下的 iframe 可以通过网关与 QwenPaw 后端通信。

API 路径重写策略：
  在返回的 HTML 中注入一段 <script>，在页面加载最早阶段拦截
  fetch() 和 XMLHttpRequest，将 /api/xxx 请求自动改写为
  /api/v1/qwenpaw-api/xxx，使 JS bundle 无需修改即可正常通信。

WebSocket 代理：
  /api/v1/qwenpaw-ws/{path} -> ws://qwenpaw:8088/{path}
  HTML 注入脚本同时拦截 WebSocket 构造，将 ws(s):// 路径重写。
"""

import os

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse

from backend.shared.trusted_headers import sanitize_forward_headers

router = APIRouter(tags=["QwenPaw-UI"])

QWENPAW_BASE_URL = os.getenv("QWENPAW_BASE_URL", "http://qwenpaw:8088").rstrip("/")
QWENPAW_WS_URL = QWENPAW_BASE_URL.replace("http://", "ws://").replace("https://", "wss://")

_STATIC_MIME = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".webp": "image/webp",
    ".map": "application/json",
}

# 注入到 HTML <head> 的拦截脚本：
# 1. 重写 fetch/XHR 的 /api/ 路径 -> /api/v1/qwenpaw-api/
# 2. 重写 WebSocket URL -> /api/v1/qwenpaw-ws/
# 3. 拦截动态 import() 以重写 /assets/ 路径
_API_REWRITE_SCRIPT = r"""<script>
(function(){
  var API_P='/api/v1/qwenpaw-api/';
  var WS_P='/api/v1/qwenpaw-ws/';
  var UI_P='/api/v1/qwenpaw-ui/';

  function rwApi(u){
    if(typeof u!=='string') return u;
    if(u.charAt(0)==='/' && u.startsWith('/api/')) return API_P+u.slice(5);
    return u;
  }
  function rwWs(u){
    if(typeof u!=='string') return u;
    var m=u.match(/^(wss?):\/\/[^\/]+(\/.*)$/);
    if(m) return (window.location.protocol==='https:'?'wss:':'ws:')+'//'+window.location.host+WS_P+m[2].slice(1);
    return u;
  }
  function rwAsset(u){
    if(typeof u!=='string') return u;
    if(u.charAt(0)==='/' && u.startsWith('/assets/')) return UI_P+u.slice(1);
    if(u.charAt(0)==='/' && u.startsWith('/online.svg')) return UI_P+'online.svg';
    return u;
  }
  function rwAll(u){ return rwAsset(rwApi(rwWs(u))); }

  // Vite's code-split chunks create <link href="/assets/..."> and
  // <script src="/assets/..."> dynamically.  fetch/XHR interception does
  // not see these browser-managed requests, so rewrite them before the
  // browser starts loading the resource.  This is required when QwenPaw is
  // embedded below the gateway prefix instead of at the site root.
  function patchUrlProperty(proto, property){
    var descriptor=Object.getOwnPropertyDescriptor(proto, property);
    if(!descriptor || !descriptor.set || !descriptor.get) return;
    Object.defineProperty(proto, property, {
      configurable:true,
      enumerable:descriptor.enumerable,
      get:descriptor.get,
      set:function(value){ return descriptor.set.call(this,rwAsset(value)); }
    });
  }
  patchUrlProperty(HTMLLinkElement.prototype,'href');
  patchUrlProperty(HTMLScriptElement.prototype,'src');
  var _setAttr=Element.prototype.setAttribute;
  Element.prototype.setAttribute=function(name,value){
    if((name==='href' || name==='src') &&
       (this instanceof HTMLLinkElement || this instanceof HTMLScriptElement)){
      value=rwAsset(value);
    }
    return _setAttr.call(this,name,value);
  };

  // Intercept fetch
  var _f=window.fetch;
  window.fetch=function(u,o){
    var url=(typeof u==='string')?u:(u instanceof URL)?u.toString():u;
    url=rwAll(url);
    return _f.call(this,url,o);
  };

  // Intercept XMLHttpRequest
  var _o=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    return _o.apply(this,[m,rwAll(u)]);
  };

  // Intercept WebSocket
  var _ws=WebSocket;
  window.WebSocket=function(url,protocols){
    return new _ws(rwWs(url),protocols);
  };
  window.WebSocket.prototype=_ws.prototype;
  window.WebSocket.CONNECTING=_ws.CONNECTING;
  window.WebSocket.OPEN=_ws.OPEN;
  window.WebSocket.CLOSING=_ws.CLOSING;
  window.WebSocket.CLOSED=_ws.CLOSED;
})();
</script>"""


_UNREACHABLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QwenPaw</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: #1a1a1a;
      color: #94a3b8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 24px;
      text-align: center;
    }
    .container {
      max-width: 440px;
      padding: 32px 24px;
      background: #222222;
      border: 1px solid #333333;
      border-radius: 16px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.4);
    }
    .icon {
      width: 44px;
      height: 44px;
      margin: 0 auto 16px;
      border-radius: 50%;
      background: rgba(148, 163, 184, 0.1);
      display: flex;
      align-items: center;
      justify-content: center;
      color: #94a3b8;
    }
    .title {
      font-size: 15px;
      font-weight: 500;
      color: #cbd5e1;
      margin-bottom: 8px;
    }
    .desc {
      font-size: 13px;
      color: #64748b;
      line-height: 1.6;
      margin-bottom: 16px;
    }
    .code {
      display: inline-block;
      padding: 6px 14px;
      background: #18181b;
      border: 1px solid #27272a;
      border-radius: 6px;
      color: #34d399;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="icon">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    </div>
    <div class="title">QwenPaw 未部署或未启动</div>
    <div class="desc">请按文档部署并启动 QwenPaw 智能体服务</div>
    <div class="code">docker-compose up -d qwenpaw</div>
  </div>
</body>
</html>"""


def _unreachable_response(accept: str = "") -> Response:
    if "text/html" in (accept or ""):
        return HTMLResponse(_UNREACHABLE_HTML, status_code=502)
    return PlainTextResponse("QwenPaw 未部署或未启动，请按文档部署", status_code=502)


def _guess_mime(path: str) -> str:
    from pathlib import PurePosixPath

    suffix = PurePosixPath(path).suffix.lower()
    return _STATIC_MIME.get(suffix, "application/octet-stream")


def _forward_headers(request: Request) -> dict[str, str]:
    """客户端头 → QwenPaw UI。

    2026-09-23 收紧：此前只过滤逐跳头，客户端自带的信任头会被透传
    （本文件是 `test_trusted_headers` 的结构性扫描查出来的漏网代理）。
    """
    return sanitize_forward_headers(request.headers.items())


async def _proxy_static(path: str, accept: str) -> Response:
    """代理静态资源（GET only）。"""
    url = f"{QWENPAW_BASE_URL}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": accept})
    except httpx.HTTPError:
        return _unreachable_response(accept)

    content_type = resp.headers.get("content-type", "")
    body = resp.content

    # HTML: 重写资源路径 + 注入 API 拦截脚本
    if "text/html" in content_type and body:
        html = body.decode("utf-8", errors="replace")
        # 静态资源路径重写
        html = html.replace('src="/assets/', 'src="/api/v1/qwenpaw-ui/assets/')
        html = html.replace('href="/assets/', 'href="/api/v1/qwenpaw-ui/assets/')
        html = html.replace('href="/online.svg"', 'href="/api/v1/qwenpaw-ui/online.svg"')
        html = html.replace('src="/online.svg"', 'src="/api/v1/qwenpaw-ui/online.svg"')
        # 在 <head> 后注入 API 路径拦截脚本
        html = html.replace("<head>", "<head>" + _API_REWRITE_SCRIPT, 1)
        return HTMLResponse(content=html, status_code=resp.status_code)

    resp_headers = {}
    if "content-type" not in resp.headers:
        guessed = _guess_mime(path)
        resp_headers["content-type"] = guessed
    else:
        resp_headers["content-type"] = content_type

    if any(
        path.endswith(ext)
        for ext in (".js", ".mjs", ".css", ".woff2", ".woff", ".ttf", ".svg", ".png", ".webp")
    ):
        resp_headers["cache-control"] = "public, max-age=3600"

    return Response(content=body, status_code=resp.status_code, headers=resp_headers)


# ---------- QwenPaw UI 静态资源路由 ----------


@router.get("/api/v1/qwenpaw-ui/{path:path}")
async def proxy_qwenpaw_ui(path: str, request: Request):
    """代理 QwenPaw Web UI 的所有静态资源。"""
    accept = request.headers.get("accept", "*/*")
    return await _proxy_static(path, accept)


@router.get("/api/v1/qwenpaw-ui")
async def proxy_qwenpaw_ui_index(request: Request):
    """代理 QwenPaw Web UI 首页。"""
    accept = request.headers.get("accept", "text/html,*/*")
    return await _proxy_static("", accept)


@router.get("/assets/{path:path}")
async def proxy_qwenpaw_root_assets(path: str, request: Request):
    """兜底代理 QwenPaw Vite 动态 import 产生的根路径 /assets/* 请求。"""
    accept = request.headers.get("accept", "*/*")
    return await _proxy_static(f"assets/{path}", accept)


@router.get("/{filename:path}.svg")
async def proxy_qwenpaw_root_svg(filename: str, request: Request):
    """兜底代理 QwenPaw 页面内引用的根路径 *.svg 图标（如 logo-light.svg）。"""
    accept = request.headers.get("accept", "*/*")
    return await _proxy_static(f"{filename}.svg", accept)


# ---------- QwenPaw API 代理路由 ----------

_QWENPAW_API_PREFIX = "/api/v1/qwenpaw-api/"
_UPSTREAM_API_PREFIX = "/api/"


async def _proxy_api(request: Request) -> Response:
    """通用 API 代理：将 /api/v1/qwenpaw-api/* 转发到 QwenPaw /api/*。"""
    path = request.url.path.removeprefix(_QWENPAW_API_PREFIX)
    upstream_url = f"{QWENPAW_BASE_URL}{_UPSTREAM_API_PREFIX}{path}"

    if request.url.query:
        upstream_url += f"?{request.url.query}"

    method = request.method.upper()
    fwd_headers = _forward_headers(request)
    body = await request.body()

    timeout = httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "POST" and "text/event-stream" in fwd_headers.get("accept", ""):
                # Streaming SSE
                req = client.build_request(method, upstream_url, content=body, headers=fwd_headers)
                resp = await client.send(req, stream=True)

                if resp.status_code >= 400:
                    err_body = await resp.aread()
                    await resp.aclose()
                    await client.aclose()
                    return Response(
                        content=err_body,
                        status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"),
                    )

                async def _cleanup():
                    await resp.aclose()
                    await client.aclose()

                return StreamingResponse(
                    resp.aiter_raw(),
                    status_code=resp.status_code,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    background=_cleanup,
                )
            else:
                req = client.build_request(
                    method,
                    upstream_url,
                    content=body if body else None,
                    headers=fwd_headers,
                )
                resp = await client.send(req)

                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers={"content-type": resp.headers.get("content-type", "application/json")},
                )
    except httpx.HTTPError:
        return PlainTextResponse("QwenPaw 未部署或未启动，请按文档部署", status_code=502)


@router.api_route(
    f"{_QWENPAW_API_PREFIX}{{path:path}}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_qwenpaw_api(path: str, request: Request):
    """代理 QwenPaw API 调用（/api/v1/qwenpaw-api/* -> QwenPaw /api/*）。"""
    return await _proxy_api(request)


# ---------- QwenPaw WebSocket 代理路由 ----------


@router.websocket("/api/v1/qwenpaw-ws/{path:path}")
async def proxy_qwenpaw_ws(websocket: WebSocket, path: str):
    """代理 QwenPaw WebSocket 连接。

    使用 websockets 库进行上游连接。
    如果 websockets 不可用，则优雅降级关闭连接。
    """
    await websocket.accept()

    import asyncio

    upstream_url = f"{QWENPAW_WS_URL}/{path}"
    if websocket.url.query:
        upstream_url += f"?{websocket.url.query}"

    try:
        import websockets
    except ImportError:
        try:
            await websocket.close(code=1011, reason="WebSocket proxy not available (websockets not installed)")
        except Exception:
            pass
        return

    try:
        async with websockets.connect(upstream_url) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        await websocket.send_text(message)
                except Exception:
                    pass

            await asyncio.gather(
                client_to_upstream(),
                upstream_to_client(),
            )
    except Exception:
        try:
            await websocket.close(code=1011, reason="Upstream connection failed")
        except Exception:
            pass
