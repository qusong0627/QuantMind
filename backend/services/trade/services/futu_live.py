"""富途 OpenD 直连薄服务 —— arena 港股面板读/写路径（2026-10-08 自旧栈恢复）。

角色与 `overseas_brokers.FutuBroker` 的分工
--------------------------------------------
FutuBroker 面向 trading_engine（下单/撤单/账户同步），本模块面向
`/api/v1/agent-arena/futu/*`（Live 页港股双卡、委托、已平仓、快照、
最小下单面板）。两者共用同一 OpenD 容器、同一 rsa.key、同一子进程执行器
（`futu_subprocess.py`）——只是调用方与返回契约不同，故不合并。

futu SDK 的连接/等待模型与 asyncio 事件循环混用会死锁，故每次调用起
独立子进程、结果经临时文件回传（单次 ~4s，RSA 握手 + OpenSecTradeContext
主导，OpenD 侧串行化）。

解锁凭据（REAL 下单）
---------------------
`trade_pwd_md5` 从券商配置读取（Redis broker:config:futu → env），仅在
env==REAL 时注入子进程 payload 的 `unlock_pwd_md5`。HTTP 路由层用
`trade_pwd_md5()` 做 fail-closed 判定（缺失 → 409），但**永远不接触该值本身**，
审计日志因此天然无密钥。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

#: RSA 握手 + OpenSecTradeContext ~4s；account_both 双段查询翻倍，留足余量
_SUBPROC_TIMEOUT = 45.0

_SCRIPT = Path(__file__).resolve().parent / "futu_subprocess.py"


def _trade_pwd_md5() -> str:
    """交易密码 MD5（REAL 解锁用）；未配置返回 ''。"""
    from backend.services.trade.services.overseas_brokers import _setting

    return _setting("futu", "trade_pwd_md5", "FUTU_TRADE_PWD_MD5").strip()


def trade_pwd_md5_configured() -> bool:
    """是否已配置解锁凭据（路由层 fail-closed 判定用，不暴露值本身）。"""
    return bool(_trade_pwd_md5())


def _run_subprocess(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """同步执行一个 futu op（在 to_thread 里跑）；失败抛 RuntimeError。"""
    from backend.services.trade.services.overseas_brokers import opend_connection

    host, port, rsa_key = opend_connection()
    fd, result_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = [
        sys.executable,
        str(_SCRIPT),
        host,
        str(port),
        rsa_key,
        op,
        json.dumps(payload),
        result_path,
    ]
    try:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "futu 子进程超时（OpenD 无响应或 RSA 配置错误）"
            ) from None
        with open(result_path, encoding="utf-8") as f:
            out_text = f.read().strip()
        if proc.returncode != 0 or not out_text:
            detail = (proc.stderr or "futu subprocess failed")[-300:]
            raise RuntimeError(detail)
        return json.loads(out_text)
    finally:
        os.unlink(result_path)


async def run_op(op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """跑一个 futu op，返回其 JSON 输出；失败抛 RuntimeError（路由层映射 502/503）。"""
    return await asyncio.to_thread(_run_subprocess, op, payload or {})


def _env_key(env: str) -> str:
    return "REAL" if str(env).upper() == "REAL" else "SIMULATE"


async def query_account(env: str = "SIMULATE") -> dict[str, Any]:
    """单 env 账户（资产/持仓，positions 键为 '00700.HK'）。"""
    return await run_op("account", {"env": _env_key(env)})


async def query_account_both() -> dict[str, Any]:
    """一次握手查 REAL+SIMULATE 两套账户（arena 双卡）。"""
    return await run_op("account_both", {})


async def query_orders(env: str = "SIMULATE") -> dict[str, Any]:
    """当日订单历史（order_list_query）→ {'orders': [...]}。"""
    return await run_op("orders", {"env": _env_key(env)})


async def query_closed(env: str = "SIMULATE") -> dict[str, Any]:
    """已平仓行（qty==0 且 realized_pl!=0）→ {'closed': [...]}。"""
    return await run_op("closed", {"env": _env_key(env)})


async def query_snapshot(codes: list[str]) -> dict[str, Any]:
    """实时快照：{'snapshot': {code: {name,last_price,prev_close,day_chg,...}}}。"""
    return await run_op("snapshot", {"codes": [str(c) for c in codes]})


async def place_order(
    order: dict[str, Any], env: str = "SIMULATE", market: str = "HK"
) -> dict[str, Any]:
    """下单（HK/US，REAL/SIMULATE）；order: {code,price,quantity,order_type,trd_side}。

    REAL 且已配置 trade_pwd_md5 时注入解锁凭据（子进程先 unlock_trade）；
    未配置时应由路由层提前 409，这里不再兜底放行。
    """
    payload: dict[str, Any] = {
        "order": order,
        "env": _env_key(env),
        "market": str(market).upper(),
    }
    md5 = _trade_pwd_md5()
    if payload["env"] == "REAL" and md5:
        payload["unlock_pwd_md5"] = md5
    return await run_op("place", payload)


async def cancel_order(
    order_id: str, env: str = "SIMULATE", market: str = "HK"
) -> dict[str, Any]:
    """撤单（HK/US，REAL/SIMULATE）；解锁口径同 place_order。"""
    payload: dict[str, Any] = {
        "order_id": order_id,
        "env": _env_key(env),
        "market": str(market).upper(),
    }
    md5 = _trade_pwd_md5()
    if payload["env"] == "REAL" and md5:
        payload["unlock_pwd_md5"] = md5
    return await run_op("cancel", payload)
