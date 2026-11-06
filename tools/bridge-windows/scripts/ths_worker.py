"""TDX 桥 · 同花顺 worker 启动器（含扫码登录端点）。

设计约束
--------
- 只绑 127.0.0.1（绝不对局域网暴露）；
- 凭据来源优先级：环境变量/`data/ths.env` > 扫码缓存 `data/ths_qr.json`；
- 上游代码在 vendor/ths（git clone，可 git pull 升级）；
  依赖在 vendor/ths_deps（离线 wheel，与桥/TQ 的全局 Python 隔离）；
- 只做行情（只读），不含任何交易能力。

扫码端点（供桥代理，桥侧再套一层 token 鉴权）
--------------------------------------------
- POST /api/qr/start   → 生成二维码，后台线程轮询扫码状态
- GET  /api/qr/status  → {state: idle|waiting|scanned|connecting|ok|error, qrid, url, account, error}
- GET  /api/qr.png     → 二维码 PNG（服务端出图，避免本地 Pillow 依赖）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "ths" / "src"))
sys.path.insert(0, str(ROOT / "vendor" / "ths_deps"))


def _tee_log() -> None:
    """stdout/stderr 同时落 data/logs/ths_worker.log。

    计划任务起的进程没有控制台、也没有重定向：启动失败或挂死时零证据
    （2026-11-06 现场：worker 卡在 1 线程/2.2MB，日志目录里什么都没有）。
    日志写不了就静默跳过，绝不影响 worker 本身。
    """
    path = ROOT / "data" / "logs" / "ths_worker.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mirror = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    mirror.write(f"\n===== {stamp} start pid={os.getpid()} =====\n")
    mirror.flush()

    class _Tee:
        def __init__(self, original, other) -> None:
            self._original = original
            self._other = other

        def write(self, data):
            if self._original is not None:
                self._original.write(data)
            self._other.write(data)
            return len(data)

        def flush(self):
            if self._original is not None:
                self._original.flush()
            self._other.flush()

        def isatty(self):
            return bool(self._original is not None and self._original.isatty())

    sys.stdout = _Tee(sys.stdout, mirror)
    sys.stderr = _Tee(sys.stderr, mirror)


def main() -> None:
    _tee_log()
    parser = argparse.ArgumentParser(description="同花顺行情 worker（只读）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env", default=str(ROOT / "data" / "ths.env"))
    args = parser.parse_args()

    import uvicorn  # noqa: PLC0415
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.responses import JSONResponse, Response  # noqa: PLC0415
    from thspypc import qr_login  # noqa: PLC0415
    from thspypc.server.app import create_app  # noqa: PLC0415
    from thspypc.server.runtime import ThsRuntime, load_env  # noqa: PLC0415

    env_path = Path(args.env)
    qr_cache = Path(ROOT / "data" / "ths_qr.json")

    # 启动前：没有 env 账号时，尝试复用扫码缓存（上游自适应：先试再说）
    env_vals = load_env(str(env_path))
    if not (env_vals.get("THS_USERNAME") or os.environ.get("THS_USERNAME")):
        cached = qr_login.load_credentials(str(qr_cache))
        if cached:
            result, saved_at = cached
            if qr_login.is_credentials_expired(result, saved_at):
                print("[ths-worker] 扫码缓存已确定过期，忽略", flush=True)
            else:
                os.environ["THS_USERNAME"] = result.account
                os.environ["THS_PASSWORD"] = result.password
                print(f"[ths-worker] 复用扫码缓存凭证 account={result.account}", flush=True)

    runtime = ThsRuntime(env_path=str(env_path))
    app: FastAPI = create_app(runtime)

    # ---- 并发闸门（2026-10-23 压测教训）--------------------------------------
    # 上游 MAIN 连接由一把 `_sock_lock` 串行化（send+全部响应读取），32 路并发
    # 只会把请求堆在 FastAPI 线程池里：重接口（K 线/超级盘口，30s+）队头阻塞，
    # 轻请求全部超时，最终把 THS 会话拖断且不自愈。这里按端点分级限流：
    # - 重接口并发 1：它们本来就吃满 MAIN 管道；
    # - 轻接口并发 6：留一点流水线，同时不让线程池无限堆积；
    # - 闸门满 → 立刻 503 worker_busy（桥侧有 TDX 兜底，比静默超时好）。
    GATE_LIGHT = 3
    GATE_HEAVY = 1
    HEAVY_PREFIXES = (
        "/api/kline/", "/api/superorder", "/api/order-details/",
        "/api/order-queues/", "/api/board/", "/api/market_snapshot",
        "/api/stocks", "/api/stock_list_ranked", "/api/hot",
        "/api/dde_rank", "/api/history_timeline/", "/api/auction/",
        "/api/closing_auction/", "/api/intraday_auctions/",
        "/api/hot_boards", "/api/dynamic_plate_refresh",
    )
    GATE_SKIP = ("/api/status", "/api/qr", "/api/bridge", "/openapi", "/docs")
    # 单请求硬超时：上游协议偶尔会无限挂（实测 history_timeline >45s 不返回，
    # 持有 MAIN socket 锁 → 之后所有请求排队、连接被拖死）。超时后主动
    # shutdown MAIN socket 让挂住的读线程立即返回，再由重连看门狗重建会话。
    HEAVY_TIMEOUT_S = 30.0
    LIGHT_TIMEOUT_S = 8.0
    gates = {"light": asyncio.Semaphore(GATE_LIGHT),
             "heavy": asyncio.Semaphore(GATE_HEAVY)}
    gate_stats = {"light_inflight": 0, "heavy_inflight": 0, "rejected": 0,
                  "light_timeouts": 0, "heavy_timeouts": 0}

    def _kick_connection(kind: str) -> None:
        """shutdown 上游 MAIN socket：不拿 `_sock_lock`，专治挂死的读线程。"""
        client = getattr(runtime, "_client", None)
        sock = getattr(client, "_sock", None) if client is not None else None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
            print(f"[ths-worker] {kind} 超时：已 shutdown MAIN socket 触发自愈",
                  flush=True)
        except OSError as exc:
            print(f"[ths-worker] socket shutdown 失败: {exc}", flush=True)

    @app.middleware("http")
    async def concurrency_gate(request, call_next):
        path = request.url.path
        if path.startswith(GATE_SKIP):
            return await call_next(request)
        kind = "heavy" if path.startswith(HEAVY_PREFIXES) else "light"
        # 重接口在跑时 MAIN 管道被占满：轻请求立刻 503，让桥毫秒级回落 TDX，
        # 而不是排队 8s 后超时（实测：一次 24s 的 auction 会拖垮全部盘口请求）。
        if kind == "light" and gate_stats["heavy_inflight"] > 0:
            gate_stats["rejected"] += 1
            return JSONResponse(
                {"error": "worker_busy", "detail": "heavy task in flight"},
                status_code=503)
        sem = gates[kind]
        bus = JSONResponse(
            {"error": "worker_busy", "detail": f"{kind} 闸门已满，请稍后重试"},
            status_code=503)
        # 重接口 30s+ 占满管道，满了就立刻拒；轻接口允许排队 2s 吸收突发。
        if sem.locked():
            if kind == "heavy":
                gate_stats["rejected"] += 1
                return bus
            try:
                await asyncio.wait_for(sem.acquire(), timeout=0.5)
            except asyncio.TimeoutError:
                gate_stats["rejected"] += 1
                return bus
        else:
            await sem.acquire()
        gate_stats[f"{kind}_inflight"] += 1
        try:
            return await asyncio.wait_for(
                call_next(request),
                timeout=(HEAVY_TIMEOUT_S if kind == "heavy" else LIGHT_TIMEOUT_S))
        except asyncio.TimeoutError:
            gate_stats[f"{kind}_timeouts"] += 1
            threading.Thread(target=_kick_connection, args=(kind,),
                             name="ths-kick", daemon=True).start()
            return JSONResponse(
                {"error": "worker_timeout",
                 "detail": f"{kind} 上游超时，已触发连接自愈"},
                status_code=504)
        finally:
            gate_stats[f"{kind}_inflight"] -= 1
            sem.release()

    @app.get("/api/bridge/gates")
    def bridge_gates():
        return {**gate_stats,
                "limits": {"light": GATE_LIGHT, "heavy": GATE_HEAVY},
                "connected": bool(runtime.status().get("connected")),
                "ts": int(time.time())}

    # ---- 断线自愈：心跳是 observe_only，不会自己重连 ------------------------
    def _reconnect_loop() -> None:
        delay = 20.0
        while True:
            time.sleep(delay)
            try:
                st = runtime.status()
                if st.get("connected"):
                    delay = 20.0
                    continue
                has_creds = bool(os.environ.get("THS_USERNAME")
                                 or st.get("credentials"))
                if not has_creds:
                    print("[ths-worker] 断线且无账号凭据，跳过重连", flush=True)
                    delay = 60.0
                    continue
                print("[ths-worker] 检测到断线，尝试重连…", flush=True)
                box: dict = {}

                def _do() -> None:
                    try:
                        box["r"] = runtime.connect()
                    except Exception as exc:  # noqa: BLE001
                        box["e"] = exc

                th = threading.Thread(target=_do, name="ths-reconnect", daemon=True)
                th.start()
                th.join(30)
                if th.is_alive():
                    print("[ths-worker] 重连超时（30s），稍后再试", flush=True)
                    delay = 60.0
                    continue
                if "e" in box or not (box.get("r") or {}).get("success"):
                    err = box.get("e") or (box.get("r") or {}).get("error")
                    delay = min(delay * 2, 120.0)
                    print(f"[ths-worker] 重连失败: {err}（{delay:.0f}s 后重试）",
                          flush=True)
                else:
                    delay = 20.0
                    print("[ths-worker] 重连成功", flush=True)
            except Exception as exc:  # noqa: BLE001
                delay = min(delay * 2, 120.0)
                print(f"[ths-worker] 重连异常: {type(exc).__name__}: {exc}",
                      flush=True)

    threading.Thread(target=_reconnect_loop, name="ths-reconnect-watchdog",
                     daemon=True).start()

    state = {"state": "idle", "qrid": "", "url": "", "error": "",
             "account": "", "started_at": 0}
    state_lock = threading.Lock()

    def _set(**kw) -> None:
        with state_lock:
            state.update(kw)

    def _apply_and_connect(result) -> dict:
        """把扫码拿到的 account/password 注入 runtime 并重连。

        `_env` / `_client` / `_lifecycle_lock` 是上游 ThsRuntime 的内部字段；
        这里集中适配，上游若改名只需改这一个函数。
        """
        env = getattr(runtime, "_env", None)
        if isinstance(env, dict):
            env["THS_USERNAME"] = result.account
            env["THS_PASSWORD"] = result.password
        os.environ["THS_USERNAME"] = result.account
        os.environ["THS_PASSWORD"] = result.password
        lock = getattr(runtime, "_lifecycle_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            old = getattr(runtime, "_client", None)
            runtime._client = None
            runtime._connected_once = False
        finally:
            if lock is not None:
                lock.release()
        if old is not None:
            try:
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return runtime.connect()

    def _poll(qrid: str, session, url: str) -> None:
        def on_status(data: dict) -> None:
            if data.get("status") == 2:
                _set(state="scanned")

        try:
            result = qr_login.poll_scan_status(qrid, session, timeout=180,
                                               interval=3, on_status=on_status)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return
        _set(state="connecting", account=result.account)
        try:
            qr_login.save_credentials(result, str(qr_cache))
            print(f"[ths-worker] 扫码凭证已缓存: {qr_cache}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[ths-worker] 扫码凭证落盘失败: {exc}", flush=True)
        try:
            res = _apply_and_connect(result)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return
        if res.get("success"):
            _set(state="ok", error="")
            print(f"[ths-worker] 扫码登录并连接成功 account={result.account}", flush=True)
        else:
            _set(state="error", error=str(res.get("error") or "登录失败"))

    @app.post("/api/qr/start")
    def qr_start():
        with state_lock:
            # 原子占位：旧实现先查后置，两个并发请求会各建一个 session/qrid，
            # 后到的覆盖先到的，前端拿到的二维码可能永远扫不上（审计 MEDIUM）。
            if state["state"] in ("starting", "waiting", "scanned", "connecting"):
                return dict(state)
            state["state"] = "starting"
            state["error"] = ""
        try:
            session = qr_login._session()  # noqa: SLF001 上游私有构造，集中适配
            qrid = qr_login.create_qrcode(session)
            url = qr_login.get_scan_url(qrid)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return JSONResponse({"state": "error", "error": state["error"]},
                                status_code=502)
        _set(state="waiting", qrid=qrid, url=url, error="", account="",
             started_at=int(time.time()))
        threading.Thread(target=_poll, args=(qrid, session, url),
                         name="ths-qr-poll", daemon=True).start()
        return {"state": "waiting", "qrid": qrid, "url": url}

    @app.get("/api/qr/status")
    def qr_status():
        with state_lock:
            return dict(state)

    @app.get("/api/qr.png")
    def qr_png(qrid: str = ""):
        with state_lock:
            q = qrid or state.get("qrid") or ""
        if not q:
            return JSONResponse({"error": "no qrid"}, status_code=404)
        try:
            png = qr_login.fetch_qr_png(q)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"},
                                status_code=502)
        return Response(content=png, media_type="image/png")

    print(f"[ths-worker] root={ROOT}", flush=True)
    print(f"[ths-worker] env={env_path} qr_cache={qr_cache} "
          f"listen={args.host}:{args.port}", flush=True)
    if not env_path.exists():
        print("[ths-worker] 提示：无 data/ths.env，可用网页扫码登录", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
