"""arena 数据面 · 富途港股直连族（2026-10-08 恢复；旧栈 `/api/futu/*` 的 QM 原生实现）。

上游 = 已退役 baymax 后端 `api_server.py` L475-577（7 个端点）。QM 落点
`/api/v1/agent-arena/futu/*`，body/响应形状与上游逐位对齐，前端移植层零改动。
执行端 = 本机容器化 FutuOpenD（docker/futu-opend），经 `futu_live` 子进程直连，
与 trading_engine 的 FutuBroker **共用同一 OpenD 与 rsa.key**（互不干扰：
OpenD 侧串行化连接）。

错误语义（与上游逐个对齐，两处刻意偏差）：

- 读面（account/orders/closed/snapshot）：失败 200 + `{success:false,error}`
  ——面板据此降级为「读取失败」，不炸页面；读面**不挂**实盘闸门
  （与 live_family 读面口径一致，闸门只管写面）。
- `account-both` 是全断例外：单腿失败该腿 null + `errors[env]`，两腿都断
  503——双卡轮询的调用方必须能识别「整体不可达」而不是画两张空卡。
- 写面（place/cancel）：REAL 单 handler 级闸门（403 `real_trading_disabled`
  逐字）+ 解锁凭据 fail-closed（`trade_pwd_md5` 未配 → 409
  `futu_unlock_required`，**不启子进程**）；桥/子进程失败 502
  `futu_bridge_error`。SDK 拒单是业务失败不是 HTTP 失败：200 +
  `{success:false, data:{message}}`（上游口径）。
- `closed` 返回 `{closed:[...]}` 而非上游解包后的裸数组——上游那个解包是
  bug（前端按数组消费，旧实现拿到的永远是空）。前端做了双形状容错
  （移植补丁），两端都认。
- 每次写追加审计 `logs/futu_orders_{北京日期}.jsonl`（best-effort，不阻断
  请求）；请求/响应均不含解锁凭据——`trade_pwd_md5` 由 `futu_live` 在
  子进程 payload 内注入，本层从不见该值。
- 委托回执（含被拒）与桥故障告警经 QQ **分市场通道**推送（HK→港股机器人、
  US→美股机器人；市场通道未配齐由 qq_notify 回退默认机器人 + 标题前缀）。
  通知全 best-effort：发不出去只记日志，绝不阻断/延迟交易请求本身。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.live_trading_gate import ensure_real_trading_allowed

logger = logging.getLogger(__name__)

router = APIRouter()

#: 审计文件名按北京日期切（操作者视角的「今天」；与 live_trade_* 同口径）
_BJ = timezone(timedelta(hours=8))


def _live():
    from backend.services.trade.services import futu_live

    return futu_live


def _futu_env(raw: object) -> str:
    """env 归一：只认 REAL/SIMULATE，其余落 SIMULATE（上游口径，失败方向安全）。"""
    value = str(raw or "").strip().upper()
    return value if value in {"REAL", "SIMULATE"} else "SIMULATE"


def _audit(
    user: dict | None,
    op: str,
    env: str,
    market: str,
    request: dict,
    response: dict,
) -> None:
    """写审计 jsonl（best-effort）。绝不含解锁凭据（本层从不见该值）。"""
    try:
        from backend.services.agent_arena.arena_config import logs_dir
        from backend.shared.utc_datetime import utc_now

        path = logs_dir() / f"futu_orders_{datetime.now(_BJ):%Y%m%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": utc_now().isoformat(),
            "op": op,
            "env": env,
            "market": market,
            "request": request,
            "response": response,
            "user": str((user or {}).get("user_id") or ""),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 审计失败不阻断交易请求
        logger.warning("arena futu 审计写入失败: %s", exc)


# ---------- 委托回执 / 桥告警 → QQ 分市场通道（best-effort，绝不阻断交易） ----------


def _qq_channel(market: str) -> str:
    """market → QQ 通道：HK→``hk``、其余→``us``（未配齐时 qq_notify 回退+前缀）。"""
    return "hk" if str(market).strip().upper() == "HK" else "us"


def _num(value: object) -> float:
    """宽容数值：富途行里可能是 ``'N/A'``/``None``/字符串数字。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _display_identity(code: str) -> str:
    """代码 → ``腾讯控股(0700.HK)``（``stock_display_name``）；失败回原样，绝不抛。"""
    try:
        from backend.shared.stock_display_name import identity

        return identity(code) or code
    except Exception:  # noqa: BLE001
        return code


def _place_detail(order: dict, market: str) -> str:
    """一行说清「方向 标的 量价」（QQ 标题用）；港美补中文名与币种前缀。"""
    side = "买入" if str(order.get("trd_side")) == "BUY" else "卖出"
    price = _num(order.get("price"))
    qty = _num(order.get("quantity"))
    ident = _display_identity(str(order.get("code") or ""))
    cur = "HK$" if str(market or "").strip().upper() == "HK" else "$"
    detail = f"{side} {ident} {qty:g}股"
    return detail + (f" @{cur}{price:g}" if price > 0 else "")


def _notify_order_outcome(
    *, op: str, env: str, market: str, detail: str, out: dict
) -> None:
    """委托回执 → QQ（成功与被拒都要回执；best-effort）。"""
    ok = bool(out.get("success"))
    title = f"{'✅' if ok else '❌'} 富途{op} {detail}（{env}·{market}）"
    lines: list[str] = []
    if out.get("order_id"):
        lines.append(f"委托号：{out['order_id']}")
    if out.get("status"):
        lines.append(f"状态：{out['status']}")
    filled_qty = _num(out.get("filled_quantity"))
    if filled_qty > 0:
        filled_price = _num(out.get("filled_price"))
        lines.append(
            f"成交：{filled_qty:g}股"
            + (f" @{filled_price:g}" if filled_price > 0 else "")
        )
    if not ok and out.get("message"):
        lines.append(f"原因：{out['message']}")
    try:
        from backend.shared.qq_notify import notify_async

        notify_async(title, "\n".join(lines), channel=_qq_channel(market))
    except Exception as exc:  # noqa: BLE001 通知失败不阻断交易
        logger.warning("arena futu QQ 回执失败: %s", exc)


def _alert_bridge_error(
    *, op: str, env: str, market: str, detail: str, error: str
) -> None:
    """桥/子进程故障 → QQ 告警（warning；best-effort）。"""
    try:
        from backend.shared.qq_notify import alert_async

        alert_async(
            level="warning",
            title=f"富途{op}失败：{detail}",
            content=f"环境 {env}·{market}\n桥错误：{error}",
            alert_type="futu-bridge",
            channel=_qq_channel(market),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu QQ 告警失败: %s", exc)


# ---------- 读面（失败 200 + {success:false, error}，不挂实盘闸门） ----------


@router.get("/futu/account")
async def arena_futu_account(
    env: str = "SIMULATE", _user: dict = Depends(get_current_user)
):
    """富途账户（资产/持仓）。env=REAL 实盘 / SIMULATE 模拟（默认）。"""
    env_key = _futu_env(env)
    try:
        out = await _live().query_account(env_key)
    except Exception as exc:  # noqa: BLE001 上游口径：降级返回错误，不炸接口
        logger.warning("arena futu account 查询失败: %s", exc)
        return {"success": False, "error": f"富途查询失败: {exc}"}
    return {"success": True, "data": {**out, "env": env_key}}


@router.get("/futu/account-both")
async def arena_futu_account_both(_user: dict = Depends(get_current_user)):
    """一次握手查 REAL+SIMULATE 两套账户（Live 双卡）。单腿断该腿 null；全断 503。"""
    try:
        out = await _live().query_account_both()
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu account-both 查询失败: %s", exc)
        raise HTTPException(status_code=503, detail=f"futu_unreachable: {exc}") from exc
    errors = out.get("errors") or {}
    real, simulate = out.get("real"), out.get("simulate")
    if real is None and simulate is None:
        raise HTTPException(
            status_code=503,
            detail="futu_unreachable: "
            + "; ".join(f"{k}: {v}" for k, v in errors.items()),
        )
    return {
        "success": True,
        "data": {"real": real, "simulate": simulate, "errors": errors},
    }


@router.get("/futu/orders")
async def arena_futu_orders(
    env: str = "SIMULATE", _user: dict = Depends(get_current_user)
):
    """当日订单历史（order_list_query）→ Live 成交/委托页。"""
    try:
        out = await _live().query_orders(_futu_env(env))
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu orders 查询失败: %s", exc)
        return {"success": False, "error": f"富途查询失败: {exc}"}
    return {"success": True, "data": out}


@router.get("/futu/closed")
async def arena_futu_closed(
    env: str = "SIMULATE", _user: dict = Depends(get_current_user)
):
    """已平仓行（qty==0 且 realized_pl!=0）→ Live「已完成」港股。

    返回 `{closed:[...]}`（**不是**上游解包后的裸数组——上游解包是 bug，
    前端永远空；本层给出完整形状，前端做双形状容错）。
    """
    try:
        out = await _live().query_closed(_futu_env(env))
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu closed 查询失败: %s", exc)
        return {"success": False, "error": f"富途查询失败: {exc}"}
    return {"success": True, "data": {"closed": out.get("closed") or []}}


@router.get("/futu/snapshot")
async def arena_futu_snapshot(
    codes: str = Query("", description="逗号分隔代码，如 00700.HK,HK.0700"),
    _user: dict = Depends(get_current_user),
):
    """实时快照（现价/昨收/当日涨跌）→ 港股盘中分析循环取价。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(status_code=400, detail="codes 参数为空")
    try:
        out = await _live().query_snapshot(code_list)
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu snapshot 查询失败: %s", exc)
        return {"success": False, "error": f"富途快照失败: {exc}"}
    return {"success": True, "data": out}


# ---------- 写面（REAL 闸门 + 解锁 fail-closed；SDK 拒单 = 业务失败非 HTTP 失败） ----------


def _write_gate(env: str) -> None:
    """写面前置判定：REAL 的闸门与解锁凭据（都在起子进程之前）。"""
    if env != "REAL":
        return
    ensure_real_trading_allowed("REAL")
    if not _live().trade_pwd_md5_configured():
        raise HTTPException(status_code=409, detail="futu_unlock_required")


@router.post("/futu/place")
async def arena_futu_place(
    payload: dict = Body(...), user: dict = Depends(get_current_user)
):
    """富途下单。body: {env, market, order:{code,price,quantity,order_type,trd_side}}。"""
    env = _futu_env(payload.get("env"))
    market = str(payload.get("market") or "HK").strip().upper() or "HK"
    order = payload.get("order") or {}

    code = str(order.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="order.code 必填")
    try:
        quantity = float(order.get("quantity") or 0)
        price = float(order.get("price") or 0)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="order.price / order.quantity 必须是数字"
        ) from None
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="order.quantity 必须大于 0")
    order_type = str(order.get("order_type") or "NORMAL").strip().upper()
    if order_type not in {"NORMAL", "MARKET"}:
        raise HTTPException(
            status_code=400, detail="order.order_type 必须是 NORMAL 或 MARKET"
        )
    if order_type == "NORMAL" and price <= 0:
        raise HTTPException(status_code=400, detail="限价单 order.price 必须大于 0")
    trd_side = str(order.get("trd_side") or "").strip().upper()
    if trd_side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="order.trd_side 必须是 BUY 或 SELL")

    _write_gate(env)

    clean_order = {
        "code": code,
        "price": price,
        "quantity": quantity,
        "order_type": order_type,
        "trd_side": trd_side,
    }
    try:
        out = await _live().place_order(clean_order, env, market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu place 失败: %s", exc)
        _audit(user, "place", env, market, clean_order, {"error": str(exc)})
        _alert_bridge_error(
            op="下单",
            env=env,
            market=market,
            detail=_place_detail(clean_order, market),
            error=str(exc),
        )
        raise HTTPException(
            status_code=502, detail=f"futu_bridge_error: {exc}"
        ) from exc
    _audit(user, "place", env, market, clean_order, out)
    _notify_order_outcome(
        op="下单",
        env=env,
        market=market,
        detail=_place_detail(clean_order, market),
        out=out,
    )
    return {"success": bool(out.get("success")), "data": out}


@router.post("/futu/cancel")
async def arena_futu_cancel(
    payload: dict = Body(...), user: dict = Depends(get_current_user)
):
    """富途撤单。body: {env, market, order_id}。"""
    env = _futu_env(payload.get("env"))
    market = str(payload.get("market") or "HK").strip().upper() or "HK"
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id 必填")

    _write_gate(env)

    try:
        out = await _live().cancel_order(order_id, env, market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("arena futu cancel 失败: %s", exc)
        _audit(user, "cancel", env, market, {"order_id": order_id}, {"error": str(exc)})
        _alert_bridge_error(
            op="撤单", env=env, market=market, detail=order_id, error=str(exc)
        )
        raise HTTPException(
            status_code=502, detail=f"futu_bridge_error: {exc}"
        ) from exc
    _audit(user, "cancel", env, market, {"order_id": order_id}, out)
    _notify_order_outcome(op="撤单", env=env, market=market, detail=order_id, out=out)
    return {"success": bool(out.get("success")), "data": out}
