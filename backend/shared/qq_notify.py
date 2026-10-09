"""QQ 机器人推送通道（腾讯官方 OpenAPI，C2C 单聊，直连、不依赖中间容器）。

源头 = 老系统 ``scripts/push_notify.py``（**同一个机器人**：继续用原
appId/owner，无需向腾讯重新绑定）。本仓为公共仓库：appId / secret / openid
一律走配置读取（真实环境变量 > ``config/runtime.env`` > 未配置），源码不落
任何真实值；未配置时如实返回 skipped，绝不假装发送成功。

凭据三键（``runtime_secrets.set_secret`` 写入后热生效，无需重启进程）::

    QQ_BOT_APP_ID / QQ_BOT_APP_SECRET / QQ_BOT_OWNER_OPENID

接口事实（老系统 2026-09-14 实测，原样保留）：
* 取令牌 ``POST https://bots.qq.com/app/getAppAccessToken``（appId+clientSecret）
* 发消息 ``POST https://api.sgroup.qq.com/v2/users/{openid}/messages``，
  Authorization 必须是 ``QQBot <token>``（Bearer 会得到 11241）；bots.qq.com/v3 被网关 503。

纪律（本模块被交易/告警路径调用，绝不阻断主流程）：
* ``notify`` 不抛异常，只写日志；markdown 被拒时自动剥 ``**`` 降级纯文本；
* 告警接线走 :func:`alert_async`（daemon 线程旁路，调用方零阻塞）；
* 令牌缓存在盘上（多进程/重启共享）+ 进程内，过期自动重取。

分市场通道（2026-10-08，用户裁决「每市场一台官方机器人」）
--------------------------------------------------------
默认通道 = 既有机器人（``QQ_BOT_APP_ID/SECRET/OWNER_OPENID``）；``hk``/``us``
为市场通道，各用独立机器人三键（``QQ_BOT_HK_*`` / ``QQ_BOT_US_*``，写入
config/runtime.env，``set_secret`` 热生效）。市场通道三键未配齐 → **回退默认
通道**发送且标题自动带 ``[港股]/[美股]`` 前缀——宁可落到旧聊天窗，不可静默
丢失。令牌缓存按通道独立（``qqbot_token.json`` → ``qqbot_token_hk.json``）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from backend.shared.runtime_secrets import get_secret

logger = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
SEND_URL = "https://api.sgroup.qq.com/v2/users/{openid}/messages"
DEFAULT_TOKEN_CACHE = "/app/logs/qqbot_token.json"
TIMEOUT_SECONDS = 8
MAX_CONTENT_LEN = 1600
# 只把「告警」语义的等级外发，避免 info 噪声淹没手机
ALERT_LEVELS = frozenset({"warning", "error"})

APP_ID_KEY = "QQ_BOT_APP_ID"
APP_SECRET_KEY = "QQ_BOT_APP_SECRET"
OWNER_OPENID_KEY = "QQ_BOT_OWNER_OPENID"

_DEFAULT_KEYS = (APP_ID_KEY, APP_SECRET_KEY, OWNER_OPENID_KEY)

#: 分市场通道 → 凭据三键。三键齐备才走本通道，否则回退默认（见 _resolve_channel）。
#: us 通道先行落地：用户注册好第二台机器人后写入三键即生效，代码零改动。
_MARKET_CHANNELS: dict[str, tuple[str, str, str]] = {
    "hk": ("QQ_BOT_HK_APP_ID", "QQ_BOT_HK_APP_SECRET", "QQ_BOT_HK_OWNER_OPENID"),
    "us": ("QQ_BOT_US_APP_ID", "QQ_BOT_US_APP_SECRET", "QQ_BOT_US_OWNER_OPENID"),
}
_CHANNEL_TAG = {"hk": "[港股]", "us": "[美股]"}


def _cfg(key: str) -> str:
    """读取配置（env > runtime.env），测试可经 monkeypatch 注入。"""
    return get_secret(key, "").strip()


def _resolve_channel(channel: str | None) -> tuple[tuple[str, str, str], str]:
    """通道归一 →（凭据键组, 实际生效通道）。

    请求市场通道但三键未配齐 → 回退默认通道（生效通道 = ``default``），
    调用方据生效通道决定是否加市场前缀。
    """
    ch = str(channel or "default").strip().lower() or "default"
    keys = _MARKET_CHANNELS.get(ch)
    if keys and all(_cfg(k) for k in keys):
        return keys, ch
    if keys:
        logger.warning("[QQNotify] %s 通道凭据未配齐，回退默认通道", ch)
    return _DEFAULT_KEYS, "default"


def is_configured(channel: str = "default") -> bool:
    """通道是否可发（市场通道未配齐但默认通道在 → 也算可发：回退 + 前缀）。"""
    keys, _ = _resolve_channel(channel)
    return all(_cfg(k) for k in keys)


def _token_cache_path(channel: str = "default") -> Path:
    base = Path(os.getenv("QM_QQ_TOKEN_CACHE", DEFAULT_TOKEN_CACHE))
    if channel == "default":
        return base
    # 分市场通道令牌缓存独立：qqbot_token.json → qqbot_token_hk.json
    return base.with_name(f"{base.stem}_{channel}{base.suffix}")


def _read_cached_token(cache: Path) -> str | None:
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if float(data.get("expires_at") or 0) > time.time() + 120:
            token = str(data.get("token") or "")
            return token or None
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cached_token(cache: Path, token: str, expires_in: float) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"token": token, "expires_at": time.time() + expires_in - 60}),
            encoding="utf-8",
        )
    except OSError:
        pass  # 缓存在盘上只是加速项，写失败不影响发送


def _get_token(
    channel: str = "default", keys: tuple[str, str, str] | None = None
) -> str:
    """取通道 access_token（缓存优先）。失败抛异常，由 notify 的统一兜底接住。

    ``keys`` 缺省时按通道自行解析（老调用点 ``_get_token()`` 兼容默认通道）。
    """
    if keys is None:
        keys, channel = _resolve_channel(channel)
    cache = _token_cache_path(channel)
    cached = _read_cached_token(cache)
    if cached:
        return cached
    app_id, secret = _cfg(keys[0]), _cfg(keys[1])
    if not secret or not app_id:
        raise RuntimeError(
            f"未配置 {keys[0]}/{keys[1]}（写入 config/runtime.env）"
        )
    import requests

    resp = requests.post(
        TOKEN_URL,
        timeout=TIMEOUT_SECONDS,
        json={"appId": app_id, "clientSecret": secret},
    )
    resp.raise_for_status()
    data = resp.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError(f"令牌接口未返回 access_token: {str(data)[:200]}")
    _write_cached_token(cache, token, float(data.get("expires_in") or 7200))
    return token


def _api_code(body: object) -> int:
    """接口返回体里的业务码；没有该字段 = 0（成功体只有 ``id``/``timestamp``）。"""
    if not isinstance(body, dict):
        return 0
    for key in ("code", "errcode", "retcode"):
        if key in body:
            try:
                return int(body[key] or 0)
            except (TypeError, ValueError):
                return -1  # 解析不出来的码一律当失败：宁可报错，不许静默当成功
    return 0


def _send_message(payload: dict, channel: str, keys: tuple[str, str, str]) -> dict:
    """发一条 C2C 消息给所有者，并**校验接口的业务返回码**。

    为什么要看 body：腾讯的失败**不一定给非 2xx**——配额/频控这类错误可以是
    HTTP 200 + ``{"code": N, "message": "…"}``。原先只 ``raise_for_status()`` 就把
    body 丢了，于是「已推送」可能是假的：手机上没收到、日志里全绿（实测 2026-09-24
    一小时内推出 26~29 条，全记成功而平台侧主动消息配额约 20 条/小时——不发 body
    就分不清是「配额没管」还是「静默被拒」）。

    报错只带 code 与 message：**绝不回显 token 与请求体**（日志会被贴进 issue）。
    """
    import requests

    openid = _cfg(keys[2])
    if not openid:
        raise RuntimeError(f"未配置 {keys[2]}（写入 config/runtime.env）")
    resp = requests.post(
        SEND_URL.format(openid=openid),
        timeout=TIMEOUT_SECONDS,
        headers={
            # v2 要求 QQBot 前缀（Bearer → 11241）
            "Authorization": f"QQBot {_get_token(channel, keys)}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    resp.raise_for_status()
    try:
        body: object = resp.json()
    except ValueError:
        body = None
    code = _api_code(body)
    if code != 0:
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("msg") or "")[:120]
        raise RuntimeError(
            f"QQ 接口拒绝：code={code}" + (f" message={message}" if message else "")
        )
    return body if isinstance(body, dict) else {}


def send_text(content: str, channel: str = "default") -> dict:
    """发一条 C2C 纯文本；失败抛异常（notify 层兜底）。"""
    keys, effective = _resolve_channel(channel)
    return _send_message(
        {
            "content": content,
            "msg_type": 0,
            "msg_seq": int(time.time() * 1000) % (2**31),
        },
        effective,
        keys,
    )


def send_markdown(content: str, channel: str = "default") -> dict:
    """发一条 C2C markdown；失败抛异常（notify 层会降级纯文本重发）。"""
    keys, effective = _resolve_channel(channel)
    return _send_message(
        {"msg_type": 2, "markdown": {"content": content}}, effective, keys
    )


def _strip_bold(text: str) -> str:
    return text.replace("**", "")


def notify(title: str, content: str = "", channel: str = "default") -> bool:
    """最外层安全网：发 QQ 通知。任何失败只写日志，返回 False，绝不抛异常。

    ``channel``：``default``/``hk``/``us``；市场通道未配齐时回退默认通道并给
    标题加 ``[港股]/[美股]`` 前缀（见模块 docstring）。
    """
    requested = str(channel or "default").strip().lower() or "default"
    keys, effective = _resolve_channel(requested)
    if requested != "default" and effective == "default":
        title = f"{_CHANNEL_TAG.get(requested, f'[{requested}]')} {title}"
    if not all(_cfg(k) for k in keys):
        logger.info("[QQNotify] 未配置凭据，跳过推送: %s", title)
        return False
    body = "\n".join(line for line in (content or "").splitlines() if line.strip())
    message = f"{title}\n\n{body}" if body else title
    if len(message) > MAX_CONTENT_LEN:
        message = message[: MAX_CONTENT_LEN - 10] + "…"
    try:
        send_markdown(message, channel=effective)
    except Exception as exc:  # noqa: BLE001 - 降级重发，不外抛
        logger.warning("[QQNotify] markdown 推送失败，降级纯文本: %s → %s", title, exc)
        try:
            send_text(_strip_bold(message), channel=effective)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("[QQNotify] 推送失败: %s → %s", title, exc2)
            return False
    logger.info("[QQNotify] 已推送: %s", title)
    return True


def notify_async(title: str, content: str = "", channel: str = "default") -> None:
    """普通事件旁路：daemon 线程发送，调用方零阻塞（请求/交易热路径用）。

    与 :func:`alert_async` 分工：本函数**不做等级过滤**，供非告警的常态事件
    （委托回执/分析摘要）使用；告警语义仍走 alert_async。
    """
    threading.Thread(
        target=notify,
        args=(title, content),
        kwargs={"channel": channel},
        name="qq-notify-event",
        daemon=True,
    ).start()


def alert_async(
    *,
    level: str,
    title: str,
    content: str = "",
    alert_type: str = "system",
    force: bool = False,
    channel: str = "default",
) -> bool:
    """告警旁路：warning/error 等级才外发，daemon 线程发送、调用方零阻塞。

    返回「是否已排入发送」；False 表示被过滤（低等级）或未配置。
    线程内走 notify（自带全兜底），因此本函数本身不会抛。

    ``force=True`` 供**告警恢复类**事件显式越过等级过滤：等级过滤的本意是
    不让日常 success（回测完成/成交回执）淹没手机，而「掉线后恢复」是掉线
    告警的闭环，必须送到同一面。普通 success 生产者不得使用。

    ``channel`` 同 :func:`notify`（分市场机器人；未配齐回退默认 + 市场前缀）。
    """
    if not force and str(level or "").strip().lower() not in ALERT_LEVELS:
        return False
    if not is_configured(channel):
        logger.info("[QQNotify] 告警未推送（凭据未配置）: %s", title)
        return False
    prefix = f"[{alert_type}] " if alert_type and alert_type != "system" else ""
    threading.Thread(
        target=notify,
        args=(f"{prefix}{title}", content),
        kwargs={"channel": channel},
        name="qq-notify-alert",
        daemon=True,
    ).start()
    return True
