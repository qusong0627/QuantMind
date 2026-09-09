#!/usr/bin/env python3
"""大 QMT 执行端链路自检（**只读，绝不下单**）。

在 QuantMind 后端环境（容器内）运行，逐层判定「模拟盘 → 大 QMT」这条链断在哪：

  1. 配置层  页面 ``broker:config:qmt_exec``（env 兜底）→ enabled / account_id / Redis 参数
  2. 网络层  用 redis 直连桥的 Redis（PING + 队列堆积探测）
  3. 监听层  RPC ``ping``（big-convert 服务端在不在）
  4. 账号层  ``get_asset`` / ``get_positions``（账号注入与查询）
  5. 委托层  ``query_orders`` / ``query_trades``（只读）

用法（容器内）::

    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \\
        python scripts/qmt_bridge_selftest.py
    docker exec ... python scripts/qmt_bridge_selftest.py --json   # 机器可读

退出码：0=全通；1=有失败；2=配置层就没过（后面无从谈起）。

配套：Windows 侧部署见 ``docs/大QMT真单镜像_部署与上线手册.md`` §三；
开箱包用 ``python scripts/export_qmt_bridge_kit.py`` 生成。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

_RESULTS: list[dict[str, Any]] = []
_CFG: dict[str, Any] = {}  # main() 载入的生效配置（脱敏用）

# 失败码 → 下一步动作（只读链路能自证的都在这）
_CODE_HINTS: dict[str, str] = {
    "DISABLED": "页面「券商接入 → 大 QMT(执行端)」的 enabled 选 true 后保存",
    "NOT_CONFIGURED": "填写 account_id（QMT 资金账号），须与 QMT 端 BIGQMT_ACCOUNT_ID 一致",
    "IMPORT_FAIL": 'Linux 侧缺依赖：pip install "xtquant-big-convert[redis]"',
    "NOT_CONNECTED": (
        "QMT 那台机器上的服务端没跑，或 Redis 地址/密码不对、防火墙未放行；"
        "确认 QMT 策略编辑器里加载运行了 BIGQMT_REDIS_DRYRUN.py"
    ),
    "TIMEOUT": (
        "Redis 通了但没人应答：① 服务端没在 QMT 策略编辑器里运行（面板日志会以 finished "
        "结尾）② Redis 库号/队列前缀不一致 ③ 服务端卡死，重启策略"
    ),
    "ORDER_DISABLED": "服务端 rpc_allow_order_methods=False（只影响下单，不影响查询）",
    "EMPTY_ASSET": "账号已连上但查不到资金：确认 QMT 已登录、账号类型 STOCK/CREDIT 选对",
}


def _record(name: str, ok: bool, detail: str, hint: str = "") -> None:
    _RESULTS.append({"step": name, "ok": ok, "detail": detail, "hint": hint})
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    if hint:  # 通过也可能带提醒（如队列积压），照样打出来
        print(f"       → {hint}")


def _skip(name: str, why: str) -> None:
    """上一层没通 → 本层无从判定（不计入通过，也不重复计失败）。"""
    _RESULTS.append(
        {"step": name, "ok": False, "skipped": True, "detail": why, "hint": ""}
    )
    print(f"[SKIP] {name}: {why}")


def _mask(secret: Any) -> str:
    text = str(secret or "")
    return "***" if text else "(空)"


def _safe(text: Any) -> str:
    """异常文案可能带 Redis URL 里的密码，统一脱敏后再打印。"""
    from backend.services.live_trading.services.qmt_exec_client import redact_secrets

    return redact_secrets(text, str(_CFG.get("redis_password") or ""))


def _redis_params(cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """桥 Redis 连接参数；未配地址时按客户端口径回落到业务 Redis。"""
    host = str(cfg.get("redis_host") or "").strip()
    note = ""
    if not host:
        host = os.getenv("REDIS_HOST", "localhost")
        port = os.getenv("REDIS_PORT", "6379")
        password = os.getenv("REDIS_PASSWORD", "")
        note = "（未配桥地址，按口径回落到业务 Redis）"
        return {
            "host": host,
            "port": int(port or 6379),
            "db": int(str(cfg.get("redis_db") or 0) or 0),
            "password": password or None,
        }, note
    return {
        "host": host,
        "port": int(str(cfg.get("redis_port") or 6379) or 6379),
        "db": int(str(cfg.get("redis_db") or 0) or 0),
        "password": str(cfg.get("redis_password") or "") or None,
    }, note


def _check_config(cfg: dict[str, Any]) -> bool:
    from backend.services.live_trading.services.qmt_exec_client import mask_account_id

    enabled = bool(cfg.get("enabled"))
    account_id = str(cfg.get("account_id") or "").strip()
    params, note = _redis_params(cfg)
    detail = (
        f"enabled={enabled} account={mask_account_id(account_id) or '(空)'} "
        f"type={cfg.get('account_type')} redis={params['host']}:{params['port']}/"
        f"{params['db']} 密码={_mask(cfg.get('redis_password'))}{note}"
    )
    if not enabled:
        _record("1.配置层", False, detail, _CODE_HINTS["DISABLED"])
        return False
    if not account_id:
        _record("1.配置层", False, detail, _CODE_HINTS["NOT_CONFIGURED"])
        return False
    _record("1.配置层", True, detail)
    return True


def _check_redis(cfg: dict[str, Any]) -> bool:
    """网络层：Redis 本身通不通 + 队列是否堆积。返回是否连通。"""
    import redis as redis_lib

    params, _ = _redis_params(cfg)
    account_id = str(cfg.get("account_id") or "").strip()
    timeout = float(cfg.get("timeout") or 10)
    try:
        client = redis_lib.Redis(
            host=params["host"],
            port=params["port"],
            db=params["db"],
            password=params["password"],
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
        )
        client.ping()
        # llen 也可能失败（ACL 只给 PING 权限、或队列键类型被占）→ 一并算网络层失败
        depth = client.llen(f"bigqmt:rpc:queue:{account_id}")
    except Exception as exc:  # noqa: BLE001 - 自检脚本必须报出原始原因
        _record(
            "2.网络层",
            False,
            f"Redis {params['host']}:{params['port']}/{params['db']} 访问失败：{_safe(exc)}",
            "确认地址/端口/密码、防火墙放行 QuantMind 主机 IP；"
            "Windows 上 Redis 服务本身要真的在跑（redis-py 只是客户端库）",
        )
        return False
    detail = f"Redis 连通 {params['host']}:{params['port']}/{params['db']}，待处理队列深度={depth}"
    hint = ""
    if depth > 0:
        hint = "队列有积压 = 服务端没在消费（QMT 侧策略没运行或已卡死）"
    _record("2.网络层", True, detail, hint)
    return True


def _check_rpc(client: Any) -> bool:
    from backend.services.live_trading.services.qmt_exec_client import (
        QmtExecError,
        mask_account_id,
    )

    try:
        result = asyncio.run(client.ping())
    except QmtExecError as exc:
        _record(
            "3.监听层",
            False,
            f"RPC ping 失败[{exc.code}]：{_safe(exc)}",
            _CODE_HINTS.get(exc.code, ""),
        )
        return False
    except Exception as exc:  # noqa: BLE001
        _record(
            "3.监听层",
            False,
            f"RPC ping 异常：{_safe(exc)}",
            _CODE_HINTS.get("TIMEOUT", ""),
        )
        return False

    # 以**服务端上报**为准：客户端回显的 account_id 只是本地配置，证明不了对面是谁
    payload = result.get("result") if isinstance(result, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    server_account = str(payload.get("account_id") or "").strip()
    server_type = str(payload.get("account_type") or "").strip().upper()
    allow_order = payload.get("allow_order_methods")
    local_account = str(_CFG.get("account_id") or "").strip()
    local_type = str(_CFG.get("account_type") or "STOCK").strip().upper()
    detail = (
        f"服务端应答正常（account={mask_account_id(server_account) or '(未上报)'} "
        f"type={server_type or '(未上报)'} 下单开关={allow_order}）"
    )
    if server_account and local_account and server_account != local_account:
        _record(
            "3.监听层",
            False,
            f"{detail}；与页面配置的资金账号不一致",
            "QMT 端 bigqmt_signal_trader_local_config.py 的 BIGQMT_ACCOUNT_ID "
            "必须与页面「资金账号」完全一致",
        )
        return False
    if server_type and server_type != local_type:
        _record(
            "3.监听层",
            False,
            f"{detail}；与页面配置的 account_type={local_type} 不一致",
            "服务端只认 QMT 端 BIGQMT_ACCOUNT_TYPE：普通账户 STOCK、两融/信用 CREDIT；"
            "填错会返回「资产全 0」而不是报错",
        )
        return False
    hint = _CODE_HINTS["ORDER_DISABLED"] if allow_order is False else ""
    _record("3.监听层", True, detail, hint)
    return True


def _check_account(client: Any) -> None:
    from backend.services.live_trading.services.qmt_exec_client import QmtExecError

    try:
        asset = asyncio.run(client.get_asset())
        positions = asyncio.run(client.get_positions())
    except QmtExecError as exc:
        _record(
            "4.账号层",
            False,
            f"资金/持仓查询失败[{exc.code}]：{_safe(exc)}",
            _CODE_HINTS.get(exc.code, ""),
        )
        return
    except Exception as exc:  # noqa: BLE001
        _record("4.账号层", False, f"资金/持仓查询异常：{_safe(exc)}", "")
        return
    _record(
        "4.账号层",
        True,
        f"总资产 {float(asset.get('total_asset') or 0):.2f}，"
        f"可用 {float(asset.get('cash') or 0):.2f}，持仓 {len(positions)} 只",
    )


def _check_orders(client: Any) -> None:
    from backend.services.live_trading.services.qmt_exec_client import QmtExecError

    try:
        orders = asyncio.run(client.query_orders())
        trades = asyncio.run(client.query_trades())
    except QmtExecError as exc:
        _record(
            "5.委托层",
            False,
            f"委托/成交查询失败[{exc.code}]：{_safe(exc)}",
            _CODE_HINTS.get(exc.code, ""),
        )
        return
    except Exception as exc:  # noqa: BLE001
        _record("5.委托层", False, f"委托/成交查询异常：{_safe(exc)}", "")
        return
    _record(
        "5.委托层", True, f"当日委托 {len(orders)} 笔，成交 {len(trades)} 笔（只读）"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="大 QMT 执行端链路自检（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = parser.parse_args()

    _RESULTS.clear()  # 同进程内重复调用（测试/REPL）不残留上一轮结果
    _CFG.clear()

    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    asyncio.run(client.refresh_settings())  # 页面配置刚落库也能立即生效
    cfg = client.effective_config()
    _CFG.update(cfg)

    print("=" * 72)
    print("大 QMT 执行端链路自检（只读；不会下任何单）")
    print("=" * 72)

    if not _check_config(cfg):
        _emit_json(args)
        return 2

    if _check_redis(cfg):
        if _check_rpc(client):
            _check_account(client)
            _check_orders(client)
    else:
        _skip("3.监听层", "Redis 不通，RPC 无从判定（先修上一层）")
        _skip("4.账号层", "同上")
        _skip("5.委托层", "同上")

    failed = [row for row in _RESULTS if not row["ok"] and not row.get("skipped")]
    skipped = [row for row in _RESULTS if row.get("skipped")]
    passed = len(_RESULTS) - len(failed) - len(skipped)
    print("-" * 72)
    summary = f"结论：{passed}/{len(_RESULTS)} 层通过"
    if failed:
        summary += f"，{len(failed)} 层失败"
    if skipped:
        summary += f"，{len(skipped)} 层跳过"
    print(summary + ("，链路可用" if not failed and not skipped else ""))
    _emit_json(args)
    return 0 if not failed and not skipped else 1


def _emit_json(args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(_RESULTS, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # RPC 超时后，工作线程仍卡在桥连接里；解释器正常退出会等 executor 的
    # join（实测挂死 >45s）→ 诊断脚本必须能自己退出，直接跳过收尾。
    os._exit(_code)
