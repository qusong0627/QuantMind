"""TDX 实盘 + 模拟/实盘全链路状态一键快照（供 AI 实时监控）。

运行方式（宿主机，容器内执行）:
  docker exec -i -w /app quantmind python - < skills/tdx-live-trading/scripts/tdx_live_status.py

输出: 桥健康 / L2 实时推理循环 / 滚动策略 / 今日 REAL 委托统计 / 实盘持仓 / 模拟盘持仓。
每节独立 try/except，单节失败不影响其余输出，适合定时监控与异常巡检。
"""

import asyncio
import json
import os
import sys
from datetime import date

try:
    import urllib.request

    # 模块在交易服务分层重构中从 trade 迁到 trade_shared；旧路径已不存在，
    # 该导入失败会让整个脚本 FATAL —— 监控自己先死了，却什么也不监控。
    from backend.services.trade_shared.redis_client import get_redis
    from backend.shared.database_manager_v2 import get_session  # noqa: F401 — 供 section_orders_today 使用
except Exception as exc:  # 非容器环境兜底
    print(f"[FATAL] 需在容器内运行(backend 导入失败): {exc}")
    sys.exit(1)

USERS = ["00000000", "00000001"]  # 常见 user_id，可 --user 指定


def _get_redis():
    """惰性连接的共享单例（``get_redis`` 会在首次访问时 ``connect()``）。"""
    return get_redis()


def _bridge_base(r) -> str:
    """桥地址：**运行时配置优先**，其次环境变量。

    曾在此硬编码一个字面量桥地址 —— 桥换机后该常量过期，
    ① 桥健康与 ⑤ 实盘持仓两节恒报「桥不可达」，而真实链路是通的：
    一个会把正常系统报成故障的监控，比没有监控更糟。
    现在与后端同源（``trade:tdx_config:runtime``），地址变了不用改脚本。
    """
    cfg = r.get("trade:tdx_config:runtime") or {}
    return str(cfg.get("bridge_url") or os.getenv("TDX_BRIDGE_URL") or "").rstrip("/")


def _http_json(
    url: str,
    token: str,
    timeout: float = 8.0,
    method: str = "GET",
    body: dict | None = None,
):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
        data=json.dumps(body or {}).encode() if method == "POST" else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _bridge_token(r) -> str:
    cfg = r.get("trade:tdx_config:runtime") or {}
    return str(cfg.get("bridge_token") or os.getenv("TDX_BRIDGE_TOKEN", ""))


def section_bridge(r):
    print("== ① 桥健康 (通达信客户端)")
    try:
        token = _bridge_token(r)
        base = _bridge_base(r)
        if not token or not base:
            print("   [WARN] 未找到桥地址/token (trade:tdx_config:runtime)")
            return
        h = _http_json(f"{base}/api/v1/health", token)
        ok = h.get("tdx_connected")
        print(
            f"   tdx_connected: {ok}  server_time: {h.get('server_time')}  "
            f"{'✅' if ok else '❌ 客户端掉线 → 下单/持仓同步/L2 实时行情全停'}"
        )
        # 桥自带 StopLossDaemon 必须恒为空：它是个独立卖出者，与 QuantMind 的
        # sltp_executor 指向同一账户同一持仓 —— 同时触发即超卖。
        st = _http_json(f"{base}/api/v1/sltp/state", token)
        items = st.get("items") or []
        armed = [i for i in items if isinstance(i, dict) and i.get("enabled")]
        if armed:
            codes = ", ".join(str(i.get("stock_code")) for i in armed[:5])
            print(
                f"   ❌ 桥侧止损 daemon 已 arm {len(armed)} 条（{codes}）"
                " → 与 sltp_executor 重复卖出，必须在桥侧停用"
            )
        else:
            print(f"   桥侧止损 daemon: 未 arm（{len(items)} 条历史记录均已停用）✅")
    except Exception as exc:
        print(f"   [ERR] 桥不可达: {exc}")


def section_l2(r):
    print("== ② L2 实时推理循环 (tdx:l2:config)")
    try:
        cfg = r.get("tdx:l2:config") or {}
        enabled = cfg.get("enabled")
        print(
            f"   enabled={enabled}  buy_trigger={cfg.get('buy_trigger')}  "
            f"sell_trigger={cfg.get('sell_trigger')}  interval={cfg.get('interval_sec')}s  "
            f"cooldown={cfg.get('cooldown_min')}min"
        )
        if enabled:
            status = r.get("tdx:l2:realtime:status") or {}
            scores = list(r.client.scan_iter("tdx:l2:score:*", count=500))
            non_neutral = 0
            for k in scores:
                v = r.get(k)
                # 字段名是 realtime_score —— 曾误写 "score"（该字段不存在）→ 恒取 50
                # → 永远报「全中性」，把一个真的在出信号的循环报成没信号。
                if (
                    isinstance(v, dict)
                    and abs(float(v.get("realtime_score") or 50) - 50) > 0.01
                ):
                    non_neutral += 1
            if not status:
                print("   循环状态: ⚠️ 无状态键 —— 循环未运行（或运行超过 TTL 未刷新）")
            else:
                print(f"   循环状态: {json.dumps(status, ensure_ascii=False)[:220]}")
            print(
                f"   评分标的: {len(scores)}  (非中性 {non_neutral} 只)  "
                f"→ {'✅ 有信号' if non_neutral else '⚠️ 全中性: 无生产推理, 不会触发买卖'}"
            )
        else:
            print("   ⚠️ 循环关闭中 (需要自动买卖时在设置页开启)")
    except Exception as exc:
        print(f"   [ERR] {exc}")


def section_rolling(r):
    print("== ③ 滚动买卖策略 (tdx:rolling_config)")
    for uid in USERS:
        try:
            cfg = r.get(f"tdx:rolling_config:default:{uid}") or {}
            if cfg:
                print(
                    f"   user={uid}: execute_mode={cfg.get('execute_mode', cfg.get('auto_place') and 'tdx' or 'off')}  "
                    f"threshold={cfg.get('score_threshold')}  amount={cfg.get('fixed_buy_amount')}"
                )
        except Exception:
            pass


def section_orders_today():
    print("== ④ 今日 REAL 委托 (PG orders, remarks=通达信桥委托)")
    try:
        from sqlalchemy import text
        from backend.shared.database_manager_v2 import get_session

        async def _q():
            async with get_session() as s:
                rows = (
                    await s.execute(
                        text("""
                    SELECT status, COUNT(*), COALESCE(SUM(filled_value),0),
                           COALESCE(SUM(commission),0)
                    FROM orders
                    WHERE remarks = '通达信桥委托' AND submitted_at >= :d
                    GROUP BY status ORDER BY status"""),
                        {"d": date.today()},
                    )
                ).fetchall()
                pending = (
                    await s.execute(
                        text("""
                    SELECT COUNT(*) FROM orders
                    WHERE remarks = '通达信桥委托'
                      AND status IN ('submitted','partially_filled')
                      AND submitted_at >= :d"""),
                        {"d": date.today()},
                    )
                ).scalar()
                return rows, pending or 0

        rows, pending = asyncio.run(_q())
        total_fee = 0.0
        for r in rows:
            total_fee += float(r[3] or 0)
            print(f"   {r[0]:18s} {int(r[1]):3d} 笔  成交额 ¥{float(r[2]):,.2f}")
        print(
            f"   在途委托: {pending} 笔  累计费用: ¥{total_fee:,.2f}  "
            f"→ {'✅ 正常' if pending == 0 else '⚠️ 有在途委托, 盯成交/撤单'}"
        )
    except Exception as exc:
        print(f"   [ERR] {exc}")


def section_bridge_positions(r):
    print("== ⑤ 实盘持仓 (桥 /api/v1/account/query)")
    try:
        token = _bridge_token(r)
        base = _bridge_base(r)
        if not token or not base:
            print("   [WARN] 无 token/地址, 跳过")
            return
        data = _http_json(f"{base}/api/v1/account/query", token, method="POST")
        positions = data.get("positions") or []
        if isinstance(positions, dict):
            positions = list(positions.values())
        real = [
            p
            for p in positions
            if float(p.get("total_volume") or p.get("volume") or 0) > 0
        ]
        for p in real[:15]:
            print(
                f"   {p.get('stock_code'):10s} 量={p.get('total_volume', p.get('volume')):>8}  "
                f"可用={p.get('available_volume'):>8}  成本={p.get('cost_price')}  "
                f"市值={p.get('market_value')}"
            )
        print(f"   真实持仓 {len(real)} 只  {'✅' if real else '(空仓)'}")
    except Exception as exc:
        print(f"   [ERR] {exc}")


def section_sim():
    print("== ⑥ 模拟盘 (纸面, 见 simulation-trading 技能)")
    print("   默认 100 万模拟金; 查账户: 见 simulation-trading 技能")


def main():
    r = _get_redis()
    print(f"[TDX 链路状态快照 {date.today()}]")
    section_bridge(r)
    print()
    section_l2(r)
    print()
    section_rolling(r)
    print()
    section_orders_today()
    print()
    section_bridge_positions(r)
    print()
    section_sim()
    print()
    print(
        "[异常速判] 桥❌→下单/持仓/实时行情停(行情日线/推理/回测不受影响) | "
        "L2 全中性→无生产推理 | 在途委托滞留→盯成交 | 大批拒绝→查价格/可用(可能 T+1)"
    )


if __name__ == "__main__":
    main()
