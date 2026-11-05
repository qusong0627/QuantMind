#!/usr/bin/env python3
"""通达信桥数据客户端（Claude Code skill 助手）。

只依赖标准库（urllib），可在本机/容器/Windows 侧运行。

连接参数
--------
- 地址：环境变量 `QM_BRIDGE_URL`，默认 `http://192.168.31.250:8550`
- Token：`QM_BRIDGE_TOKEN` > `TDX_BRIDGE_TOKEN` > 自动 `docker exec quantmind printenv TDX_BRIDGE_TOKEN`
  （自动探测仅在 Linux/容器主机可用；失败会给出明确提示）

用法示例
--------
    python3 bridge.py health
    python3 bridge.py quote 600519.SH            # 统一三源行情（裸码/前缀/后缀都行）
    python3 bridge.py quote 600519.SH --prefer ths,aidata,tdx
    python3 bridge.py raw 002415.SZ --aidata     # 三源原始返回
    python3 bridge.py compare 600519.SH          # 同尺对照（归一化后）
    python3 bridge.py fields                     # 字段字典
    python3 bridge.py sources                    # 源统计
    python3 bridge.py ths depth  --params '{"code":"600519.SH","levels":5}'
    python3 bridge.py ths dxjl
    python3 bridge.py ths boards
    python3 bridge.py tdx get_more_info --params '{"stock_code":"002415.SZ"}'
    python3 bridge.py aidata get_minute_data --params '{"stock_code":"002415.SZ","date":"2026-10-23"}'
    python3 bridge.py gates                      # worker 并发闸门/自愈状态

退出码：0 成功；1 调用失败；2 参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://192.168.31.250:8550"
TIMEOUT = float(os.environ.get("QM_BRIDGE_TIMEOUT", "30"))


def base_url() -> str:
    return (os.environ.get("QM_BRIDGE_URL") or DEFAULT_URL).rstrip("/")


def token() -> str:
    for key in ("QM_BRIDGE_TOKEN", "TDX_BRIDGE_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    docker = shutil.which("docker")
    if docker:
        try:
            out = subprocess.run(
                [docker, "exec", "quantmind", "printenv", "TDX_BRIDGE_TOKEN"],
                capture_output=True, text=True, timeout=8)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(
        "缺少桥访问 token：设置 QM_BRIDGE_TOKEN 或 TDX_BRIDGE_TOKEN（与平台 .env 的 TDX_BRIDGE_TOKEN 一致）")


def request(method: str, path: str, payload: dict | None = None,
            params: dict | None = None, timeout: float = TIMEOUT) -> dict:
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            raise SystemExit(f"HTTP {exc.code}: {body[:300]!r}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"桥不可达（{base_url()}）：{exc.reason}") from None


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=1))


def call_or_die(result: dict) -> dict:
    if isinstance(result, dict) and result.get("success") is False:
        print_json(result)
        raise SystemExit(1)
    return result


def cmd_health(_args) -> None:
    print_json(request("GET", "/api/v1/health", timeout=8))


def cmd_quote(args) -> None:
    params = {"code": args.code}
    if args.prefer:
        params["prefer"] = args.prefer
    if args.aidata:
        params["aidata"] = "1"
    print_json(call_or_die(request("GET", "/api/v1/market/quote", params=params)))


def cmd_raw(args) -> None:
    params = {"code": args.code}
    if args.aidata:
        params["aidata"] = "1"
    print_json(call_or_die(request("GET", "/api/v1/market/raw", params=params, timeout=60)))


def cmd_compare(args) -> None:
    print_json(call_or_die(request("GET", "/api/v1/ths/compare",
                                   params={"code": args.code}, timeout=40)))


def cmd_fields(_args) -> None:
    print_json(call_or_die(request("GET", "/api/v1/market/fields")))


def cmd_sources(_args) -> None:
    print_json(call_or_die(request("GET", "/api/v1/market/sources")))


def cmd_gates(_args) -> None:
    print_json(request("GET", "/api/v1/ths/status", timeout=15))


def _parse_params(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params 不是合法 JSON: {exc}") from None
    if not isinstance(value, dict):
        raise SystemExit("--params 必须是 JSON 对象")
    return value


def cmd_ths(args) -> None:
    result = request("POST", "/api/v1/ths/call",
                     payload={"method": args.method, "params": _parse_params(args.params)},
                     timeout=args.timeout)
    print_json(call_or_die(result))


def cmd_tdx(args) -> None:
    result = request("POST", "/api/v1/tdx/call",
                     payload={"method": args.method, "params": _parse_params(args.params)},
                     timeout=args.timeout)
    print_json(call_or_die(result))


def cmd_aidata(args) -> None:
    result = request("POST", "/api/v1/aidata/call",
                     payload={"method": args.method, "params": _parse_params(args.params)},
                     timeout=args.timeout)
    print_json(call_or_die(result))


def cmd_boards(_args) -> None:
    print_json(call_or_die(request("GET", "/api/v1/ths/boards", timeout=60)))


def cmd_dxjl(args) -> None:
    print_json(call_or_die(request("GET", "/api/v1/ths/dxjl",
                                   params={"limit": args.limit}, timeout=30)))


def cmd_live_codes(args) -> None:
    params = {"limit": args.limit}
    if args.include_orders:
        params["include_orders"] = "1"
    if args.source:
        params["source"] = args.source
    print_json(call_or_die(request("GET", "/api/v1/subscription/live-codes",
                                   params=params, timeout=30)))


def cmd_auto(args) -> None:
    if args.enable or args.disable:
        payload = {"enabled": bool(args.enable and not args.disable)}
        if args.interval:
            payload["interval_s"] = args.interval
        if args.mode:
            payload["mode"] = args.mode
        print_json(call_or_die(request("POST", "/api/v1/subscription/auto",
                                       payload=payload, timeout=40)))
    else:
        print_json(call_or_die(request("GET", "/api/v1/subscription/auto",
                                       timeout=15)))


def cmd_status(_args) -> None:
    print_json(request("GET", "/api/v1/ths/status", timeout=15))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="通达信桥数据客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="桥健康").set_defaults(func=cmd_health)

    q = sub.add_parser("quote", help="统一三源行情")
    q.add_argument("code", help="600519 / 600519.SH / SH600519")
    q.add_argument("--prefer", help="源优先级，如 ths,aidata,tdx（默认 ths,tdx）")
    q.add_argument("--aidata", action="store_true", help="把 aidata 加入该次查询（按次计费）")
    q.set_defaults(func=cmd_quote)

    r = sub.add_parser("raw", help="三源原始返回")
    r.add_argument("code")
    r.add_argument("--aidata", action="store_true")
    r.set_defaults(func=cmd_raw)

    c = sub.add_parser("compare", help="TDX/THS 同尺对照")
    c.add_argument("code")
    c.set_defaults(func=cmd_compare)

    sub.add_parser("fields", help="字段字典").set_defaults(func=cmd_fields)
    sub.add_parser("sources", help="源统计").set_defaults(func=cmd_sources)
    sub.add_parser("gates", help="THS worker 闸门/自愈状态").set_defaults(func=cmd_gates)
    sub.add_parser("status", help="桥侧 THS 状态").set_defaults(func=cmd_status)
    sub.add_parser("boards", help="全部板块").set_defaults(func=cmd_boards)

    lc = sub.add_parser("live-codes", help="实盘持仓/委托代码")
    lc.add_argument("--include-orders", action="store_true")
    lc.add_argument("--source", choices=["positions", "orders"], default="positions")
    lc.add_argument("--limit", type=int, default=100)
    lc.set_defaults(func=cmd_live_codes)

    au = sub.add_parser("auto", help="自动跟随实盘订阅（tq ≤100）")
    au.add_argument("--enable", action="store_true")
    au.add_argument("--disable", action="store_true")
    au.add_argument("--interval", type=int, default=0)
    au.add_argument("--mode", choices=["merge", "replace"], default="")
    au.set_defaults(func=cmd_auto)

    d = sub.add_parser("dxjl", help="短线精灵异动")
    d.add_argument("--limit", type=int, default=20)
    d.set_defaults(func=cmd_dxjl)

    for name, fn, help_text in (
            ("ths", cmd_ths, "同花顺 worker 任一方法"),
            ("tdx", cmd_tdx, "TDX 只读方法（白名单）"),
            ("aidata", cmd_aidata, "TdxAiData 付费方法")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("method")
        s.add_argument("--params", default="{}", help="JSON 对象")
        s.add_argument("--timeout", type=float, default=60)
        s.set_defaults(func=fn)
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
