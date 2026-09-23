#!/usr/bin/env python3
"""签发 dsh web 的浏览器鉴权 cookie，生成 nginx map include（容器内版）。

背景
----
dsh 0.1.5 起 web 端强制 token/cookie 鉴权；QuantBot 页面是 src 写死、不带 token 的
iframe，唯一进门凭证就是 cookie。本脚本用 dsh 持久化的签名密钥
（$DSH_HOME/.credentials.yaml，卷持久化、容器重启不变）预先签好 cookie，
由容器内 nginx 转发时注入请求头，用户无需手动做 token 交换。

签名格式严格对齐 @deepseek-ai/dsh-client-connection 的 BrowserAuth：
    secret = 解码 credentials 里 payload.secret（base64url → 32 字节）
    name   = "dsh-auth-" + b64url(sha256(authority))
    body   = b64url(json({"version":1,"authority":a,"issuedAt":t,"expiresAt":e}))
    sig    = b64url(hmac_sha256(key=secret_bytes, msg=body))
    value  = f"v1.{body}.{sig}"

硬约束（源码 authorizeIndex 实测，蓝本 baymax scripts/dsh_auth_cookie.py）：
    1. expiresAt - issuedAt 必须 <= cookieMaxAgeDays × 1天
       （cookieMaxAgeDays=3650 配置在 dsh.cordis.yml 的 connection 层；
       本脚本默认 3600 天留余量）→ 改那个值后重启容器即自动重签。
    2. cookie 按 authority(host:port) 精确绑定 → 浏览器从哪个地址访问就签哪个
       （列表 = DSH_TRUSTED_HOSTS + localhost/127.0.0.1）。

用法（entrypoint 调用）：
    python3 mint_cookies.py --days 3600 --port 8088 \
        --authorities "192.0.2.10,quantbot.example.com:8088" \
        --out /etc/nginx/conf.d/dsh-cookies.conf

任何失败都会写「透传兜底」map（保证 nginx 能启动），并在 stderr 告警。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
import time
from pathlib import Path

CRED_PATH = Path("/root/.dsh/.credentials.yaml")
COOKIE_PREFIX = "dsh-auth-"
PAYLOAD_VERSION = 1
DAY_MS = 86_400_000
# 与 docker/dsh/dsh.cordis.yml 的 connection.cookieMaxAgeDays 保持一致（改则同改）
MAX_COOKIE_DAYS = 3650


def b64url(raw: bytes) -> str:
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def read_secret() -> bytes:
    text = CRED_PATH.read_text(encoding="utf-8")
    match = re.search(r"^\s*secret:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise ValueError("凭据文件里没找到 secret 字段（格式变了？）")
    secret = b64url_decode(match.group(1))
    if len(secret) != 32:
        raise ValueError(f"secret 解码后应为 32 字节，实际 {len(secret)}")
    return secret


def mint(secret: bytes, authority: str, days: int) -> tuple[str, str]:
    """返回 (cookie 名, cookie 值)。"""
    name = COOKIE_PREFIX + b64url(hashlib.sha256(authority.encode()).digest())
    issued = int(time.time() * 1000)
    expires = issued + days * DAY_MS
    body = b64url(json.dumps(
        {"version": PAYLOAD_VERSION, "authority": authority,
         "issuedAt": issued, "expiresAt": expires},
        separators=(",", ":"), sort_keys=True,
    ).encode())
    sig = b64url(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return name, f"v1.{body}.{sig}"


def parse_authorities(raw: str, port: str) -> list[str]:
    items = [s.strip().lower() for s in re.split(r"[,\s]+", raw or "") if s.strip()]
    out: list[str] = []
    for item in items:
        out.append(item if ":" in item else f"{item}:{port}")
    for host in (f"localhost:{port}", f"127.0.0.1:{port}"):
        if host not in out:
            out.append(host)
    return list(dict.fromkeys(out))


def write_passthrough(out: Path, reason: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# 由 mint_cookies.py 写入：透传兜底（未能签发 cookie，原因见下行与容器日志）\n"
        f"# 原因：{reason}\n"
        "map $http_cookie $dsh_auth_cookie {\n"
        "    default $http_cookie;\n"
        "}\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="签发 dsh web cookie 并生成 nginx map include")
    ap.add_argument("--days", type=int, default=3600,
                    help=f"cookie 有效期天数（必须 <= dsh cookieMaxAgeDays={MAX_COOKIE_DAYS}）")
    ap.add_argument("--port", default="8088", help="对外端口（用于拼 authority 与默认 localhost/127.0.0.1）")
    ap.add_argument("--authorities", default="",
                    help="authority 列表（host 或 host:port，逗号/空格分隔；不带端口自动补 --port）")
    ap.add_argument("--out", type=Path, default=Path("/etc/nginx/conf.d/dsh-cookies.conf"))
    args = ap.parse_args()

    try:
        if not 0 < args.days <= MAX_COOKIE_DAYS:
            raise ValueError(f"--days 必须落在 (0, {MAX_COOKIE_DAYS}]")
        secret = read_secret()
        authorities = parse_authorities(args.authorities, args.port)
        pairs = [(a, *mint(secret, a, args.days)) for a in authorities]
    except Exception as exc:  # 任何失败都不能拦住 nginx 启动
        write_passthrough(args.out, str(exc))
        print(f"[mint_cookies] 警告：{exc}；已写透传兜底（浏览器需手动 token 交换）", file=sys.stderr)
        return 0

    cookie_header = "; ".join(f"{name}={value}" for _, name, value in pairs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "# 本文件由 mint_cookies.py 自动生成 —— 请勿手改（重启容器自动重签）。\n"
        "# 用途：nginx 转发到 dsh 时注入浏览器鉴权 cookie（dsh 0.1.5 起 web 强制 token/cookie 鉴权）。\n"
        "map $http_cookie $dsh_auth_cookie {\n"
        f'    default       "$http_cookie; {cookie_header}";\n'
        f'    "~*dsh-auth-" "{cookie_header}";\n'
        f'    ""            "{cookie_header}";\n'
        "}\n",
        encoding="utf-8",
    )
    args.out.chmod(0o644)
    print(f"[mint_cookies] 已签发 {len(pairs)} 个 authority 的 cookie（{args.days} 天）：")
    for authority, name, _ in pairs:
        print(f"[mint_cookies]   {authority:32s} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
