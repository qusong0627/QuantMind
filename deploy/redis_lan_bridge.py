#!/usr/bin/env python3
"""Redis LAN 转发桥（源 IP 白名单）——跨机桥接专用，Redis 本体保持回环绑定。

背景（2026-09-18 根因定位）：docker-compose 安全修复（93f5bbe5）把 Redis 端口
映射改为 127.0.0.1，该改动在 2026-09-17 09:52 Redis 容器重建时生效——大 QMT 的
Windows 桥（bigqmt Redis-RPC 传输）自此在局域网内够不到 Redis（请求队列只进不出，
账户同步/委托轮询全超时）。本转发器在宿主机 LAN 地址上开启指定端口，只放行白名单
源 IP，其余连接立即拒绝；Redis 容器保持 127.0.0.1 绑定与免密，不外露。

环境变量:
  REDIS_LAN_BIND_IPS    监听 IP（逗号分隔；默认自动枚举全部 192.168.* 非回环 IPv4）
  REDIS_LAN_BIND_PORTS  监听端口（逗号分隔；默认 6379,6380——覆盖历史两种配置）
  REDIS_LAN_ALLOW       源 IP 白名单（CIDR 或精确 IP，逗号分隔；默认 192.168.31.13）
  REDIS_LAN_TARGET      转发目标（默认 127.0.0.1:6379）

运行（宿主机）:
  docker run -d --name quantmind-redis-lan-bridge --restart unless-stopped \
    --network host -e REDIS_LAN_ALLOW=192.168.31.13 \
    -v <repo>/deploy/redis_lan_bridge.py:/bridge.py:ro \
    python:3.10-slim-bookworm python3 /bridge.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import sys

DEFAULT_ALLOW = "192.168.31.13"
TARGET_HOST = os.getenv("REDIS_LAN_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.getenv("REDIS_LAN_TARGET_PORT", "6379"))
PIPE_BUFSIZE = 65536


def _auto_bind_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("192.168.") and not ip.startswith("192.168.122."):
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    # getaddrinfo 常拿不全，再补枚举所有本地地址
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith("192.168.") and ip not in ips and not ip.startswith("192.168.122."):
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return ips


def _parse_bind_ips() -> list[str]:
    raw = os.getenv("REDIS_LAN_BIND_IPS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return _auto_bind_ips()


def _parse_ports() -> list[int]:
    raw = os.getenv("REDIS_LAN_BIND_PORTS", "6379,6380").strip()
    ports = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            ports.append(int(x))
    return ports


def _parse_allow() -> list[ipaddress.IPv4Network | ipaddress.IPv4Address]:
    raw = os.getenv("REDIS_LAN_ALLOW", DEFAULT_ALLOW).strip() or DEFAULT_ALLOW
    nets = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        nets.append(ipaddress.ip_network(x, strict=False))
    return nets


def _allowed(peer_ip: str, allow) -> bool:
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in allow)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(PIPE_BUFSIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, listen_ip: str
) -> None:
    peer = writer.get_extra_info("peername") or ("?", 0)
    peer_ip = str(peer[0])
    if not _allowed(peer_ip, ALLOW):
        print(f"[redis-lan-bridge] 拒绝非白名单来源 {peer_ip} → {listen_ip}", flush=True)
        writer.close()
        return
    try:
        up_reader, up_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception as exc:  # noqa: BLE001
        print(f"[redis-lan-bridge] 目标不可达 {TARGET_HOST}:{TARGET_PORT}: {exc}", flush=True)
        writer.close()
        return
    print(f"[redis-lan-bridge] 接受 {peer_ip} → {listen_ip} → {TARGET_HOST}:{TARGET_PORT}", flush=True)
    await asyncio.gather(
        _pipe(reader, up_writer),
        _pipe(up_reader, writer),
        return_exceptions=True,
    )
    for w in (writer, up_writer):
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


ALLOW = _parse_allow()


async def main() -> int:
    bind_ips = _parse_bind_ips()
    ports = _parse_ports()
    if not bind_ips:
        print("[redis-lan-bridge] 未找到可监听 LAN IP，退出", flush=True)
        return 1
    servers = []
    for ip in bind_ips:
        for port in ports:
            try:
                srv = await asyncio.start_server(
                    lambda r, w, _ip=ip: handle_client(r, w, _ip),
                    host=ip,
                    port=port,
                    reuse_address=True,
                )
            except OSError as exc:
                print(f"[redis-lan-bridge] 监听 {ip}:{port} 失败: {exc}", flush=True)
                continue
            servers.append(srv)
            print(f"[redis-lan-bridge] 监听 {ip}:{port} → {TARGET_HOST}:{TARGET_PORT}", flush=True)
    if not servers:
        return 1
    print(
        f"[redis-lan-bridge] 白名单: {[str(n) for n in ALLOW]}（其余来源一律拒绝）",
        flush=True,
    )
    await asyncio.gather(*(s.serve_forever() for s in servers))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
