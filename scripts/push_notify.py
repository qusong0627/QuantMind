#!/usr/bin/env python3
"""QQ 机器人推送（腾讯官方 QQ 机器人 OpenAPI，直连，不依赖 dsh 容器）。

消息链：交易事件 → 本模块 → bots.qq.com → 用户 QQ（C2C 单聊）。
与 dsh 的 dsh-qq 集成（integrations/dsh-qq，2026-09-13 配置）共用同一官方机器人
（appId 1905604147），owner 即本机器人的所有者 openid。

凭据（不落代码/日志；读取顺序：进程环境 > config/keeper.env > .env）：
  QQ_BOT_APP_ID / QQ_BOT_APP_SECRET / QQ_BOT_OWNER_OPENID
  （2026-09-29 随 keeper 迁入 QuantMind：宿主侧唯一来源 = config/keeper.env，
    chmod 600、不进 git；下方 DSH_QQBOT_* 常量仅作历史兜底）
  （2026-10-08 起分市场通道：QQ_BOT_HK_* / QQ_BOT_US_* 各一台独立机器人，
    三键未配齐回退默认通道 + 标题前缀 [港股]/[美股]；CLI 用 --channel 选择）

接口：
  POST https://bots.qq.com/app/getAppAccessToken  {appId, clientSecret} → access_token（缓存 100min）
  POST https://api.sgroup.qq.com/v2/users/{openid}/messages  {content, msg_type: 0}

纪律（本模块被交易执行路径调用，绝不阻断交易）：
  - 任何失败只写 logs/push_notify.log，不抛异常
  - 网络超时 8s；令牌失败自动重取一次
  - 股票名称：logs/stock_names.json 缓存 → 缺失时直读 QuantDB
    instrument_detail.parquet（duckdb；2026-09-29 替代已停机的 8105 MCP 查询，
    拿不到名就只报代码，不影响推送）

用法：
  python scripts/push_notify.py send <title> <content>   # 主动发一条
  python scripts/push_notify.py test                     # 发一条测试消息
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_REF = "DSH_QQBOT_APP_SECRET_8F253DAF61C7170664F218F8"
OWNER_OPENID = "EBDB9B9BB923B3B891E98EA24A62976D"
APP_ID = "1905604147"
# ── 分市场通道（2026-10-08）：每市场一台独立机器人（用户裁决）────────────
# default = 上述历史机器人；hk/us 凭据只从环境/keeper.env 读（源码不落真实值）。
# 三键未配齐 → 回退默认通道 + 标题 [市场] 前缀（与容器侧 qq_notify 同口径）。
_CHANNEL_KEYS: dict[str, tuple[str, str, str]] = {
    "default": ("QQ_BOT_APP_ID", "QQ_BOT_APP_SECRET", "QQ_BOT_OWNER_OPENID"),
    "hk": ("QQ_BOT_HK_APP_ID", "QQ_BOT_HK_APP_SECRET", "QQ_BOT_HK_OWNER_OPENID"),
    "us": ("QQ_BOT_US_APP_ID", "QQ_BOT_US_APP_SECRET", "QQ_BOT_US_OWNER_OPENID"),
}
_CHANNEL_TAG = {"hk": "[港股]", "us": "[美股]"}
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# C2C 单聊发送走腾讯开放平台 v2 接口（bots.qq.com/v3 会被 stgw 网关 503 拦掉，
# 2026-09-14 实测；dsh-qq 插件同源用的是 api.sgroup.qq.com）
SEND_URL = "https://api.sgroup.qq.com/v2/users/{openid}/messages"
TOKEN_CACHE = ROOT / "logs" / "qqbot_token.json"
NAME_CACHE = ROOT / "logs" / "stock_names.json"
LOG = ROOT / "logs" / "push_notify.log"
CN_TZ = timezone(timedelta(hours=8))
_TIMEOUT = 8


def _load_env() -> None:
    """读 .service.env / config/keeper.env / .env，不覆盖已有环境变量。

    keeper.env 是宿主侧 keeper 凭据的唯一来源（2026-09-29 迁入，chmod 600、不进 git）；
    .env 保留兜底（容器同源键）。"""
    for p in (ROOT / ".service.env", ROOT / "config" / "keeper.env", ROOT / ".env"):
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
        except OSError:
            continue


_load_env()


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(CN_TZ):%F %T} {msg}\n")
    except OSError:
        pass


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


# ---------- 名称解析（缓存 → quantdb MCP → 仅代码） ----------


def _load_name_cache() -> dict:
    try:
        return json.loads(NAME_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_name_cache(cache: dict) -> None:
    try:
        NAME_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _quantdb_instrument_file() -> Path:
    """instrument_detail.parquet 路径（尊重 QM_QUANTDB_DATA_DIR，仅接受绝对路径）。"""
    q = os.environ.get("QM_QUANTDB_DATA_DIR") or ""
    base = Path(q) if q.startswith("/") else ROOT / "data" / "quantdb"
    return base / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"


def _quantdb_lookup_name(symbol: str) -> str:
    """直读 QuantDB instrument_detail（duckdb）；任何失败返回 ""（名拿不到不影响推送）。"""
    import duckdb

    s = str(symbol or "").strip()
    if not s:
        return ""
    cands = [s]
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)
    if m:  # 前缀式 → 后缀式（instrument_detail 键为 000001.SZ 这类格式）
        cands.append(f"{m.group(2)}.{m.group(1)}")
    f = _quantdb_instrument_file()
    if not f.is_file():
        return ""
    con = duckdb.connect()
    try:
        for c in cands:
            row = con.execute(
                "SELECT Name FROM read_parquet(?) WHERE Symbol = ? LIMIT 1",
                [str(f), c]).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    finally:
        con.close()
    return ""


def _repair_name(name: str) -> str:
    """把 latin-1 误解码的中文名修回来（UTF-8 字节被按 latin-1 解成了两段乱码）。

    只在名称全部落在 latin-1 区间时尝试重编码；真正的外文名重解码失败则原样返回。
    """
    try:
        fixed = name.encode("latin-1").decode("utf-8")
        return fixed if fixed != name else name
    except (UnicodeDecodeError, UnicodeEncodeError):
        return name


def stock_name(symbol: str) -> str:
    """股票中文名（缓存 → quantdb → ''）。失败不影响调用方。"""
    if not symbol:
        return ""
    cache = _load_name_cache()
    if symbol in cache:
        return _repair_name(cache[symbol])
    name = ""
    try:
        name = _quantdb_lookup_name(symbol)
    except Exception as exc:  # noqa: BLE001
        _log(f"名称查询失败 {symbol}: {exc}")
    name = _repair_name(name)
    if name and name != "None":
        cache[symbol] = name
        _save_name_cache(cache)
    return name or ""


# ---------- 令牌与发送 ----------


def _token_cache(channel: str) -> Path:
    """通道令牌缓存：默认 logs/qqbot_token.json；市场通道 logs/qqbot_token_{ch}.json。"""
    if channel == "default":
        return TOKEN_CACHE
    return TOKEN_CACHE.with_name(f"{TOKEN_CACHE.stem}_{channel}{TOKEN_CACHE.suffix}")


def _resolve_channel(channel: str) -> str:
    """通道归一：市场通道三键齐备才生效，否则回退 default（notify 层补前缀）。"""
    ch = str(channel or "default").strip().lower() or "default"
    keys = _CHANNEL_KEYS.get(ch)
    if ch != "default" and keys and all(os.environ.get(k) for k in keys):
        return ch
    if ch not in ("", "default"):
        _log(f"{ch} 通道凭据未配齐，回退默认通道")
    return "default"


def _get_token(channel: str = "default") -> str:
    keys = _CHANNEL_KEYS[channel]
    if channel == "default":
        # 默认通道保留历史兜底：环境 > DSH 引用键/常量
        secret = os.environ.get("QQ_BOT_APP_SECRET") or os.environ.get(SECRET_REF) or ""
        app_id = os.environ.get("QQ_BOT_APP_ID") or APP_ID
    else:
        secret = os.environ.get(keys[1]) or ""
        app_id = os.environ.get(keys[0]) or ""
    if not secret:
        raise RuntimeError(f"缺少 {keys[1]}（请写入 config/keeper.env）")
    cache_path = _token_cache(channel)
    cached = None
    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if cached and float(cached.get("expires_at") or 0) > time.time() + 120:
        return str(cached["token"])
    import requests

    r = requests.post(TOKEN_URL, timeout=_TIMEOUT,
                      json={"appId": app_id, "clientSecret": secret})
    r.raise_for_status()
    data = r.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError(f"机器人令牌接口未返回 access_token: {str(data)[:200]}")
    try:
        cache_path.write_text(json.dumps({
            "token": token,
            "expires_at": time.time() + float(data.get("expires_in") or 7200) - 60,
        }), encoding="utf-8")
    except OSError:
        pass
    return token


def _channel_openid(channel: str) -> str:
    """通道收件人 openid（openid 按机器人隔离，不能跨通道复用）。"""
    if channel == "default":
        return os.environ.get("QQ_BOT_OWNER_OPENID") or OWNER_OPENID
    return os.environ.get(_CHANNEL_KEYS[channel][2]) or ""


def send_text(content: str, channel: str = "default") -> dict:
    """发一条 C2C 纯文本给所有者；失败抛异常（调用方自行兜底）。"""
    import requests

    openid = _channel_openid(channel)
    if not openid:
        raise RuntimeError(f"缺少 {_CHANNEL_KEYS[channel][2]}（请写入 config/keeper.env）")
    token = _get_token(channel)
    r = requests.post(
        SEND_URL.format(openid=openid),
        timeout=_TIMEOUT,
        headers={"Authorization": f"QQBot {token}",  # v2 接口要求 QQBot 前缀（Bearer 会 11241）
                 "Content-Type": "application/json"},
        json={"content": content, "msg_type": 0,
              "msg_seq": int(time.time() * 1000) % (2**31)})
    r.raise_for_status()
    return r.json()


def send_markdown(content: str, channel: str = "default") -> dict:
    """发一条 C2C markdown（**加粗**等富文本）；失败抛异常（调用方自行兜底）。"""
    import requests

    openid = _channel_openid(channel)
    if not openid:
        raise RuntimeError(f"缺少 {_CHANNEL_KEYS[channel][2]}（请写入 config/keeper.env）")
    token = _get_token(channel)
    r = requests.post(
        SEND_URL.format(openid=openid),
        timeout=_TIMEOUT,
        headers={"Authorization": f"QQBot {token}",
                 "Content-Type": "application/json"},
        json={"msg_type": 2, "markdown": {"content": content}})
    r.raise_for_status()
    return r.json()


def bold(s) -> str:
    """markdown 加粗（文本降级路径会剥掉 **）"""
    return f"**{s}**"


def _strip_bold(s: str) -> str:
    return str(s).replace("**", "")


def notify(title: str, content: str, channel: str = "default") -> None:
    """最外层安全网：发 QQ 通知，任何失败只写日志，绝不影响主流程。

    排版约定：标题一行说完「动作+标的+量价」（▲买/▼卖），正文每行一个字段
    （原因/止损止盈/委托/余额/T+1/账号）；股票名、金额、价位等关键信息加粗
    （markdown）；markdown 被拒/不可用时自动剥掉 ** 降级纯文本重发。

    ``channel``：default/hk/us；市场通道未配齐时回退默认通道，标题自动加
    ``[港股]/[美股]`` 前缀（与容器侧 qq_notify 同口径）。
    """
    requested = str(channel or "default").strip().lower() or "default"
    effective = _resolve_channel(requested)
    if requested != "default" and effective == "default":
        title = f"{_CHANNEL_TAG.get(requested, f'[{requested}]')} {title}"
    content = "\n".join(line for line in content.splitlines() if line.strip())
    md = f"{title}\n\n{content}"
    if len(md) > 1600:  # markdown 消息上限兜底（正常推送远小于此）
        md = md[:1590] + "…"
    try:
        resp = send_markdown(md, channel=effective)
    except Exception as exc:  # noqa: BLE001
        _log(f"QQ markdown 推送失败，降级纯文本: {title} → {exc}")
        try:
            resp = send_text(_strip_bold(md), channel=effective)
        except Exception as exc2:  # noqa: BLE001
            _log(f"QQ 推送失败: {title} → {exc2}")
            return
    _log(f"已发送 QQ: {title}（{str(resp)[:160]}）")


# ---------- 交易事件格式化 ----------


def _ledger_cash(agent: str) -> float | None:
    try:
        d = json.loads((ROOT / "logs" / "live_ledger.json").read_text(encoding="utf-8"))
        return float((d.get("agents") or {}).get(agent, {}).get("virtual_cash") or 0)
    except (OSError, ValueError, TypeError):
        return None


def _ledger_hold_volume(agent: str, symbol: str) -> int:
    try:
        d = json.loads((ROOT / "logs" / "live_ledger.json").read_text(encoding="utf-8"))
        pos = ((d.get("agents") or {}).get(agent, {}).get("positions") or {}).get(symbol) or {}
        return int(pos.get("volume") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _clamp_reason(text: str, limit: int = 300) -> str:
    """原因截断：优先在分句边界（、，。；！？）收尾，不从半句中间腰斩。

    上限 300 字（2026-09-15 用户口径：原因不能被切在「30min动量…」半句上；
    本系统 agent 证据链普遍 100-250 字 → 基本全量显示）。超限才在 limit 内
    最后一个标点收口；毫无标点的长文做硬截。
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    idx = max(window.rfind(ch) for ch in "、，。；！？,;.")
    if idx >= limit // 2:            # 边界别收得太靠前（至少留半窗内容）
        return window[: idx + 1] + "…"
    return window + "…"


def notify_order(*, side: str, symbol: str, volume: int, price: float | None = None,
                 agent: str = "", reason: str = "", order_id: str = "",
                 held_before: int | None = None, source: str = "",
                 stop_loss: float | None = None,
                 take_profit: float | None = None) -> None:
    """下单成功事件 → QQ（▲买/▼卖，正文含原因/止损止盈/委托/余额/T+1/账号）。"""
    side_label = "买入" if side == "buy" else "卖出"
    icon = "▲" if side == "buy" else "▼"
    name = stock_name(symbol)
    sym = f"{name}({symbol})" if name else symbol
    price_txt = f" @¥{price:.2f}" if price else ""
    held = held_before if held_before is not None else _ledger_hold_volume(agent, symbol)
    clearing = side == "sell" and held and volume >= held
    act = f"{icon} {bold('清仓') if clearing else side_label} " \
          f"{bold(sym)} {bold(f'{volume}股{price_txt}')}".strip()
    body = []
    reason = (reason or "").strip()
    body.append(f"原因：{_clamp_reason(reason)}" if reason else "原因：—")
    meta = []
    if stop_loss or take_profit:
        sl = f"止损 {bold(f'¥{stop_loss:.2f}')}" if stop_loss else "止损 —"
        tp = f"止盈 {bold(f'¥{take_profit:.2f}')}" if take_profit else "止盈 —"
        meta.append(f"{sl} ｜ {tp}")
    if order_id:
        meta.append(f"委托 {bold(order_id)}")
    cash = _ledger_cash(agent)
    if cash is not None:
        meta.append(f"余额 {bold(f'¥{cash:,.0f}')}")
    if meta:
        body.append(" ｜ ".join(meta))
    if side == "buy":
        body.append("T+1：今日买入，明日可卖")
    who = f"{agent}" + (f"（{source}）" if source else "")
    if who:
        body.append(f"账号：{who}")
    notify(act, "\n".join(body))


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantMind keeper QQ 通知")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="主动发送一条通知")
    s.add_argument("title")
    s.add_argument("content")
    s.add_argument("--channel", default="default", choices=["default", "hk", "us"])
    t = sub.add_parser("test", help="发送测试消息")
    t.add_argument("--channel", default="default", choices=["default", "hk", "us"])
    args = ap.parse_args()
    if args.cmd == "test":
        notify("通知通道测试 ✅",
               f"QuantMind keeper 已接通 QQ 机器人通知（{_now_cn():%F %T}）\n"
               "keeper 告警（桥离线 / 主机守护 / 风控事件）都会推送到这里。",
               channel=args.channel)
    else:
        notify(args.title, args.content, channel=args.channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())