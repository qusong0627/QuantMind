"""通达信桥 IP 自动发现（断线多重保险）。

背景：桥跑在 Windows 交易机（DHCP 地址易变），.env 里固定 IP 失效后，
盘中分析/价格哨兵/杠杆守护等所有 cron 会整段空窗（2026-09-07 上午复盘）。
本模块在桥连接失败时（由 tdx_bridge 的 _post 触发）：
  1) 快探 .env 与运行时覆盖 config/tdx_bridge.json（每 URL ≤1s）；
  2) 都不通 → 同端口局网扫 /24，用免鉴权 GET /api/v1/health 的签名响应
     （{"status":"ok",...}，普通设备不会返回）认领桥主机；
  3) 命中即写 config/tdx_bridge.json（broker 每次构造都读 → 全链路收敛到新 IP；
     UI 设置页也写该文件，保留其 bridge_token 等键不覆盖）；
     扫网有跨进程冷却 + 文件锁（多个 cron 进程同分钟失败只扫一次）。

探针/候选/冷却逻辑集中在纯函数（可注入），便于单测。只依赖标准库 + requests。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_FILE = ROOT / "config" / "tdx_bridge.json"   # 与 UI 设置页同源（优先级高于 .env）
SCAN_STATE_FILE = ROOT / "logs" / "bridge_scan.json"  # 冷却/上次扫描结果
LOCK_FILE = ROOT / "logs" / "bridge_scan.lock"        # 跨进程互斥

HEALTH_PATH = "/api/v1/health"      # 桥免鉴权健康端点（实测 {"status":"ok","tdx_connected":...}）
PROBE_TIMEOUT_S = 1.0               # 候选快探超时
SCAN_TIMEOUT_S = 0.6                # 单 IP 扫网超时
SCAN_THREADS = 64                   # 并发（254 IP ≈ 4 波，全程 ≤2.5s）
SCAN_COOLDOWN_S = 180               # 冷却期内失败不重扫（已知候选仍每进程快探）


def probe_url(url: str, timeout: float = PROBE_TIMEOUT_S) -> dict | None:
    """健康探针：GET /api/v1/health，HTTP 200 且 JSON status=="ok" 才算桥主机。
    连接失败/超时/非桥响应一律 None（扫网误认普通设备会造成 token 外发）。"""
    if not url:
        return None
    import requests

    try:
        resp = requests.get(url.rstrip("/") + HEALTH_PATH, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 探针失败按不可用处理
        return None
    if isinstance(data, dict) and data.get("status") == "ok":
        return data
    return None


def _local_ips() -> set:
    """本机出口 IP（udp connect 不回包，只用于拿路由），扫网时跳过自己。"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 哨兵地址用 RFC 5737 文档网段（TEST-NET-1）：UDP connect 不会真发包，
            # 只为让内核选出口网卡；写死任一部署的内网网关都会泄露内网拓扑。
            s.connect(("192.0.2.1", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return ips


def subnet_hosts(host: str, exclude: set | None = None) -> list:
    """同 /24 待扫主机（.1–.254，排除已知地址自身——它刚失败过），按与已知
    IP 的数值距离升序（DHCP 漂移常见近邻，先探近的命中快）。非 IPv4 返回空。"""
    parts = host.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return []
    base = int(parts[3])
    ex = {x for x in (exclude or set()) if x} | {host}
    prefix = ".".join(parts[:3])
    cands = [f"{prefix}.{i}" for i in range(1, 255)
             if f"{prefix}.{i}" not in ex]
    cands.sort(key=lambda ip: abs(int(ip.rsplit(".", 1)[1]) - base))
    return cands


def scan_bridge(host: str, port: int, timeout: float = SCAN_TIMEOUT_S,
                threads: int = SCAN_THREADS) -> str | None:
    """局网 /24 并发扫健康端点 → 第一个签名为桥的 IP；找不到返回 None。
    全程有界：单 IP ≤timeout，命中即停（后续任务空跑退出）。"""
    if not port or not host:
        return None
    found: list = []
    stop = threading.Event()

    def _try(ip: str) -> None:
        if stop.is_set():
            return
        if probe_url(f"http://{ip}:{port}", timeout):
            stop.set()
            found.append(ip)

    hosts = subnet_hosts(host, exclude=_local_ips())
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(_try, ip): ip for ip in hosts}
        for fut in futs:
            if stop.is_set():
                break
            try:
                fut.result()
            except Exception:  # noqa: BLE001 单 IP 失败不影响整体
                continue
    return found[0] if found else None


def _load_json(path: Path) -> dict:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def override_url(override_file: Path | None = None) -> str:
    return str((_load_json(override_file or OVERRIDE_FILE).get("bridge_url") or "")).rstrip("/")


def persist_override(url: str, source: str, override_file: Path | None = None) -> None:
    """写运行时覆盖：只改 bridge_url，保留 UI/旧发现写入的 bridge_token 等键。"""
    p = override_file or OVERRIDE_FILE
    cur = _load_json(p)
    cur["bridge_url"] = url
    cur["bridge_url_source"] = source
    cur["bridge_found_ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(p, cur)


def _host_port(url: str):
    if not url:
        return None, None
    try:
        u = urlparse(url)
        host = u.hostname or ""
        return host, u.port
    except ValueError:
        return None, None


def _cooldown_ok(state_file: Path, state: dict | None, clock) -> bool:
    ts = (state or {}).get("last_scan_ts")
    if not ts:
        return True
    try:
        return clock() - float(ts) >= SCAN_COOLDOWN_S
    except (TypeError, ValueError):
        return True


def resolve_bridge(env_url: str, current_url: str = "", *,
                   override_file: Path | None = None,
                   scan_state_file: Path | None = None,
                   lock_file: Path | None = None,
                   probe=None, scanner=None, clock=None) -> str | None:
    """桥失联时的地址解析：env → override(上次发现/UI) → 局网扫描（冷却+锁）。
    命中 env/扫描结果且与当前不一致 → 落盘 override（全链路收敛 / 静态 IP 恢复自愈）。
    全部失败返回 None（调用方保留并抛出原始连接错误）。
    probe/scanner/clock 仅测试注入。"""
    probe = probe or probe_url
    clock = clock or time.time
    of = override_file or OVERRIDE_FILE
    sf = scan_state_file or SCAN_STATE_FILE
    lf = lock_file or LOCK_FILE

    # 1) 已知候选快探：env（声明静态真相）→ current（本实例在用）→ override（上次发现/UI）
    cands: list = []
    for u in (env_url, current_url, override_url(of)):
        u = (u or "").rstrip("/")
        if u and u not in cands:
            cands.append(u)
    for u in cands:
        if probe(u):
            if u != current_url:
                # 收敛回写：env 恢复（自愈）/ 扫描结果替换死地址
                persist_override(u, "env" if u == env_url else "override", of)
            return u

    # 2) 全死 → 局网扫描（跨进程冷却 + 锁，避免每分钟多进程全量扫）
    state = _load_json(sf)
    if not _cooldown_ok(sf, state, clock):
        return None
    import fcntl

    try:
        fh = open(lf, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        state = _load_json(sf)  # 拿锁后二次确认（并发进程已扫过则让位）
        if not _cooldown_ok(sf, state, clock):
            return None
        host, port = _host_port(env_url or current_url)
        found_ip = (scanner or scan_bridge)(host, port) if host else None
        _write_json(sf, {"last_scan_ts": clock(), "winner": found_ip})
        if found_ip:
            url = f"http://{found_ip}:{port}"
            persist_override(url, "lan-scan", of)
            return url
        return None
    except OSError:
        return None
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
