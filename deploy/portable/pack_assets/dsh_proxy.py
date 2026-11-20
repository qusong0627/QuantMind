#!/usr/bin/env python3
"""便携版 dsh（DeepSeek Harness）回环前门。

背景
----
dsh 官方 web 端只监听 127.0.0.1，且要求浏览器带签名 cookie
（``dsh-auth-<b64url(sha256(authority))>``，密钥在 ``$DSH_HOME/.credentials.yaml``）。
容器版用 nginx map + mint_cookies.py 注入；便携包不背 nginx，这里用 aiohttp
做同一件事：

* HTTP/WebSocket 反代到 127.0.0.1:<upstream>；
* 每个请求按上游 authority 现场签一个 cookie 注入（浏览器无需登录）；
* Host/Origin/Referer 统一改写成上游地址，dsh 的 trusted-host 围栏只需信任回环；
* 浏览器看到的是 http://<本机>:<listen>，SPA 自身用相对路径，不受影响。

用法：
    python dsh_proxy.py --listen 0.0.0.0:18088 --upstream 127.0.0.1:3081 \
        --home /path/to/dsh-home
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, WSMsgType, web

COOKIE_PREFIX = "dsh-auth-"
PAYLOAD_VERSION = 1
DAY_MS = 86_400_000
COOKIE_MAX_DAYS = 3650
COOKIE_DAYS = 3600
CRED_NAME = ".credentials.yaml"

# 逐跳头：不能透传（RFC 7230 §6.1）
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

log = logging.getLogger("dsh-proxy")


def b64url(raw: bytes) -> str:
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def read_secret(home: Path) -> bytes | None:
    path = home / CRED_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*secret:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        return None
    secret = b64url_decode(match.group(1))
    return secret if len(secret) == 32 else None


def mint(secret: bytes, authority: str) -> tuple[str, str]:
    name = COOKIE_PREFIX + b64url(hashlib.sha256(authority.encode()).digest())
    issued = int(time.time() * 1000)
    expires = issued + COOKIE_DAYS * DAY_MS
    body = b64url(
        json.dumps(
            {
                "version": PAYLOAD_VERSION,
                "authority": authority,
                "issuedAt": issued,
                "expiresAt": expires,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    sig = b64url(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return name, f"v1.{body}.{sig}"


class SecretCache:
    """按 credentials 文件 mtime 缓存密钥；dsh 首启生成后自动可用。"""

    def __init__(self, home: Path):
        self.home = home
        self._mtime = -1.0
        self._secret: bytes | None = None

    def get(self) -> bytes | None:
        path = self.home / CRED_NAME
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if mtime != self._mtime:
            self._secret = read_secret(self.home)
            self._mtime = mtime
        return self._secret


def rewrite_origin(value: str, authority: str) -> str:
    try:
        parts = urlsplit(value)
        host, port = authority.rsplit(":", 1)
        return urlunsplit(
            (parts.scheme or "http", f"{host}:{port}", parts.path or "", parts.query, parts.fragment)
        )
    except Exception:
        return f"http://{authority}"


def request_headers(request: web.Request, authority: str, cookie: str | None) -> dict:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_HEADERS and k.lower() != "host"
    }
    headers["Host"] = authority
    if cookie:
        existing = [
            item.strip()
            for item in headers.get("Cookie", "").split(";")
            if item.strip() and not item.strip().startswith(COOKIE_PREFIX)
        ]
        existing.append(cookie)
        headers["Cookie"] = "; ".join(existing)
    for key in ("Origin", "Referer"):
        if key in headers:
            headers[key] = rewrite_origin(headers[key], authority)
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


def make_app(upstream: str, home: Path) -> web.Application:
    authority = upstream
    ws_url = f"ws://{upstream}"
    http_url = f"http://{upstream}"
    secrets = SecretCache(home)
    client: ClientSession | None = None

    async def wait_secret() -> bytes:
        for _ in range(240):
            secret = secrets.get()
            if secret:
                return secret
            await asyncio.sleep(0.5)
        raise web.HTTPServiceUnavailable(text="dsh credentials not ready")

    async def proxy_http(request: web.Request) -> web.StreamResponse:
        secret = await wait_secret()
        name, value = mint(secret, authority)
        headers = request_headers(request, authority, f"{name}={value}")
        body = await request.read() if request.can_read_body else None
        assert client is not None
        async with client.request(
            request.method,
            http_url + request.rel_url.raw_path_qs,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as upstream_resp:
            out = web.StreamResponse(
                status=upstream_resp.status,
                reason=upstream_resp.reason,
                headers={
                    k: v
                    for k, v in upstream_resp.headers.items()
                    if k.lower()
                    not in HOP_HEADERS
                    | {"content-encoding", "content-length"}
                },
            )
            await out.prepare(request)
            async for chunk in upstream_resp.content.iter_chunked(64 * 1024):
                await out.write(chunk)
            await out.write_eof()
            return out

    async def proxy_ws(request: web.Request) -> web.WebSocketResponse:
        secret = await wait_secret()
        name, value = mint(secret, authority)
        protocols = [
            item.strip()
            for item in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if item.strip()
        ] or None
        ws_server = web.WebSocketResponse(protocols=protocols, max_msg_size=0)
        await ws_server.prepare(request)
        headers = request_headers(
            request, authority, f"{name}={value}"
        )
        assert client is not None
        try:
            async with client.ws_connect(
                ws_url + request.rel_url.raw_path_qs,
                headers=headers,
                protocols=protocols,
                max_msg_size=0,
                heartbeat=30,
            ) as ws_client:
                async def to_upstream() -> None:
                    async for msg in ws_server:
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)

                async def to_browser() -> None:
                    async for msg in ws_client:
                        if msg.type == WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_server.send_bytes(msg.data)
                        elif msg.type == WSMsgType.ERROR:
                            break

                await asyncio.gather(to_upstream(), to_browser())
        except Exception as exc:  # noqa: BLE001 - 前端会按断开重连
            log.warning("websocket proxy 断开: %s", exc)
        finally:
            await ws_server.close()
        return ws_server

    async def on_startup(_: web.Application) -> None:
        nonlocal client
        client = ClientSession(auto_decompress=True, timeout=None)

    async def on_cleanup(_: web.Application) -> None:
        if client is not None:
            await client.close()

    async def proxy(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await proxy_ws(request)
        return await proxy_http(request)

    app = web.Application(client_max_size=1024**3)
    app.router.add_route("*", "/", proxy)
    app.router.add_route("*", "/{path:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="便携版 dsh 回环前门")
    ap.add_argument("--listen", default="0.0.0.0:18088")
    ap.add_argument("--upstream", default="127.0.0.1:3081")
    ap.add_argument("--home", required=True)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[dsh-proxy] %(asctime)s %(levelname)s %(message)s",
    )
    host, port = args.listen.rsplit(":", 1)
    web.run_app(
        make_app(args.upstream, Path(args.home)),
        host=host,
        port=int(port),
        print=None,
        access_log=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
