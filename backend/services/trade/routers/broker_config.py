"""券商接入配置管理（海外券商 + A 股 TDX 桥 / 大 QMT 执行端）。

配置存 Trade Redis（键 broker:config:{broker}），运行时优先读取该配置，缺失时回退
环境变量。敏感字段（私钥/密码/token）只写不回读，查询接口仅返回 *_configured 布尔状态。
``qmt_exec``（大 QMT 执行端）另有热生效：写入后清空客户端配置缓存，下一次轮询即生效。

安全口径
--------
* 全部端点要求管理员（``require_admin``）——这里存的是券商凭据与下单通道。
* **换连接地址必须重填凭据**：只改 ``redis_host``/``bridge_url``/``opend_host`` 而
  密码留空时，运行时会把库内已存的密钥原样发往新地址（等于交出凭据），故拒绝。
* 地址/端口字段做格式校验；写操作记审计日志（只记字段名，绝不记值）。

供前端「模拟交易设置 → 券商接入」卡片使用。
"""
import json
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.trade_shared.deps import AuthContext, get_redis, require_admin
from backend.services.trade_shared.redis_client import RedisClient

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIG_KEY = "broker:config:{broker}"

# 各券商的可配置字段定义：字段名 → 是否敏感（敏感字段只写不读）
BROKER_FIELDS: dict[str, dict[str, bool]] = {
    "tiger": {
        "tiger_id": False,
        "rsa_private_key": True,
        "account": False,
    },
    "futu": {
        "opend_host": False,
        "opend_port": False,
        "trade_pwd_md5": True,
        "trade_env": False,  # REAL / SIMULATE
    },
    "ib": {
        "gateway_host": False,
        "gateway_port": False,
        "client_id": False,
    },
    # 通达信 Windows 桥（TdxBroker）
    "tdx": {
        "bridge_url": False,
        "bridge_token": True,
        "account": False,
        "account_type": False,
    },
    # 大 QMT 执行端（big-convert RPC，QmtExecBroker）
    "qmt_exec": {
        "enabled": False,  # true/false
        "account_id": False,
        "account_type": False,  # STOCK / CREDIT
        "strategy_name": False,
        "timeout": False,
        "redis_host": False,  # 桥（QMT 那台 Windows）的 Redis 地址
        "redis_port": False,
        "redis_db": False,
        "redis_password": True,
    },
}

BROKER_LABELS = {
    "tiger": "老虎证券",
    "futu": "富途证券",
    "ib": "盈透证券(IB)",
    "tdx": "通达信(TDX 桥)",
    "qmt_exec": "大 QMT(执行端)",
}

# 各市场可选的实盘券商（前端「券商接入」卡片按此渲染）
MARKET_BROKERS: dict[str, list[str]] = {
    "CN": ["qmt_exec", "tdx"],
    "HK": ["futu", "tiger", "ib"],
    "US": ["tiger", "ib", "futu"],
    "FUTURES": ["ib"],
    "CRYPTO": [],
}

# 「已配置」判定所需的最小字段集（缺省=全部字段非空）
BROKER_REQUIRED: dict[str, tuple[str, ...]] = {
    "tdx": ("bridge_url", "bridge_token"),
    "qmt_exec": ("enabled", "account_id"),
}

# 连接地址类字段（决定凭据被发往哪里）：变更时必须同时重填该券商的敏感字段
ENDPOINT_FIELDS: dict[str, tuple[str, ...]] = {
    "futu": ("opend_host", "opend_port"),
    "tdx": ("bridge_url",),
    "qmt_exec": ("redis_host", "redis_port", "redis_db"),
}

# 敏感字段的环境变量来源（页面未存凭据时，运行时仍可能从 env 取用）
SECRET_ENV_VARS: dict[str, dict[str, tuple[str, ...]]] = {
    "tiger": {"rsa_private_key": ("TIGER_RSA_PRIVATE_KEY",)},
    "futu": {"trade_pwd_md5": ("FUTU_TRADE_PWD_MD5",)},
    "tdx": {"bridge_token": ("TDX_BRIDGE_TOKEN",)},
    "qmt_exec": {
        "redis_password": ("QMT_EXEC_REDIS_PASSWORD", "BIGQMT_REDIS_PASSWORD"),
    },
}

# 地址/端口格式（主机名或 IPv4；IPv6 请用主机名，避免解析歧义）
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_PORT_PATTERN = re.compile(r"^\d{1,5}$")
_DB_PATTERN = re.compile(r"^\d{1,3}$")
_URL_PATTERN = re.compile(
    r"^https?://[A-Za-z0-9._\-]+(:\d{1,5})?(/[A-Za-z0-9._\-/]*)?$"
)

FIELD_PATTERNS: dict[str, re.Pattern] = {
    "redis_host": _HOST_PATTERN,
    "opend_host": _HOST_PATTERN,
    "gateway_host": _HOST_PATTERN,
    "redis_port": _PORT_PATTERN,
    "opend_port": _PORT_PATTERN,
    "gateway_port": _PORT_PATTERN,
    "redis_db": _DB_PATTERN,
    "bridge_url": _URL_PATTERN,
}


class BrokerConfigUpdate(BaseModel):
    values: dict[str, str] = Field(..., description="字段名 → 值（敏感字段原文）")


def _normalize_broker(broker: str) -> str:
    broker = str(broker or "").lower().strip()
    if broker not in BROKER_FIELDS:
        raise HTTPException(status_code=404, detail=f"未知券商: {broker}")
    return broker


def _read_config(redis: RedisClient, broker: str) -> dict[str, str]:
    if not redis.client:
        return {}
    try:
        raw = redis.client.get(_CONFIG_KEY.format(broker=broker))
        return json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取券商配置失败 broker=%s: %s", broker, exc)
        return {}


def _write_config(redis: RedisClient, broker: str, values: dict[str, str]) -> None:
    if not redis.client:
        raise HTTPException(status_code=503, detail="Redis 不可用")
    redis.client.set(_CONFIG_KEY.format(broker=broker), json.dumps(values, ensure_ascii=False))


def _audit(action: str, auth: AuthContext, **detail: Any) -> None:
    """审计日志：只记字段名/布尔状态，绝不记字段值（含凭据）。"""
    logger.info(
        "[BrokerConfigAPI] %s tenant=%s user=%s %s",
        action,
        auth.tenant_id,
        auth.user_id,
        " ".join(f"{k}={v!r}" for k, v in detail.items()),
    )


def _validate_fields(values: dict[str, str]) -> None:
    """地址/端口字段格式校验（挡住空格、换行、超长等可疑值）。"""
    for name, value in values.items():
        pattern = FIELD_PATTERNS.get(name)
        text = str(value or "").strip()
        if pattern is not None and text and not pattern.match(text):
            raise HTTPException(status_code=422, detail=f"字段 {name} 格式非法")


def _guard_endpoint_change(
    broker: str, values: dict[str, str], stored: dict[str, str]
) -> None:
    """换连接地址时必须同时重填敏感字段。

    否则「只改 host」会让运行时把库内已存的密码/token 原样发往新地址——
    等于把凭据交给填写者。env 里的凭据同样在运行时会兜底使用，所以一并计入。
    空值视为「清除该字段」，不算换地址；库内/env 都没有凭据可泄露时不拦。
    """
    changed = [
        name
        for name in ENDPOINT_FIELDS.get(broker, ())
        if str(values.get(name) or "").strip()
        and str(values.get(name) or "").strip() != str(stored.get(name, "") or "").strip()
    ]
    if not changed:
        return
    env_map = SECRET_ENV_VARS.get(broker, {})
    missing: list[str] = []
    for name, is_secret in BROKER_FIELDS[broker].items():
        if not is_secret or str(values.get(name) or "").strip():
            continue
        has_secret = bool(str(stored.get(name, "") or "").strip()) or any(
            str(os.getenv(var) or "").strip() for var in env_map.get(name, ())
        )
        if has_secret:
            missing.append(name)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"更换 {'/'.join(changed)} 时必须同时重新填写 {'/'.join(missing)}"
                "（防止把已存凭据发往新地址）"
            ),
        )


def get_broker_setting(broker: str, field: str, default: str = "") -> str:
    """供 overseas_brokers 运行时读取（Redis 优先，回退环境变量由调用方处理）。"""
    from backend.services.trade_shared.redis_client import RedisClient

    try:
        rc = RedisClient()
        values = _read_config(rc, broker)
        return str(values.get(field, "") or "")
    except Exception:  # noqa: BLE001
        return ""


_SELECTED_KEY = "broker:selected:{market}"


@router.get("/broker-config-status")
async def get_broker_config_status(
    market: str = "CN",
    auth: AuthContext = Depends(require_admin),
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    """按市场汇总：可选券商、各自配置状态、当前选中的券商。"""
    market = str(market or "CN").upper()
    brokers = MARKET_BROKERS.get(market, [])
    items: list[dict[str, Any]] = []
    for broker in brokers:
        stored = _read_config(redis, broker)
        required = BROKER_REQUIRED.get(broker) or tuple(BROKER_FIELDS[broker])
        configured = bool(stored) and all(
            str(stored.get(name, "") or "").strip() for name in required
        )
        items.append({
            "broker": broker,
            "label": BROKER_LABELS.get(broker, broker),
            "configured": configured,
        })
    selected_raw = redis.client.get(_SELECTED_KEY.format(market=market)) if redis.client else None
    selected = (selected_raw or b"").decode() if isinstance(selected_raw, (bytes, bytearray)) else (selected_raw or "")
    return {
        "success": True,
        "market": market,
        "brokers": items,
        "selected": selected or None,
    }


class BrokerSelectUpdate(BaseModel):
    broker: str = Field(
        ..., description="该市场使用的券商（tiger/futu/ib/tdx/qmt_exec），空串=取消选择"
    )


@router.put("/broker-config/selected/{market}")
async def select_market_broker(
    market: str,
    payload: BrokerSelectUpdate,
    auth: AuthContext = Depends(require_admin),
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    """设置某市场使用的实盘券商。"""
    market = str(market or "CN").upper()
    broker = str(payload.broker or "").lower().strip()
    if not redis.client:
        raise HTTPException(status_code=503, detail="Redis 不可用")
    allowed = MARKET_BROKERS.get(market, [])
    if broker and broker not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{market} 市场不支持券商 '{broker}'，可选：{', '.join(allowed) or '无'}",
        )
    redis.client.set(_SELECTED_KEY.format(market=market), broker)
    _audit("select", auth, market=market, broker=broker)
    return {"success": True, "market": market, "selected": broker}



class BrokerTestRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict, description="当前表单值（测试前自动保存）")
    trade_env: str | None = Field(None, description="覆盖交易环境（REAL/SIMULATE），不影响已保存配置")


@router.post("/broker-config/{broker}/test")
async def test_broker_connection(
    broker: str,
    payload: BrokerTestRequest | None = None,
    auth: AuthContext = Depends(require_admin),
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    """测试券商连通性（真实调用 SDK；OpenD/Gateway 未启动会明确报错）。

    测试前自动保存表单值；trade_env 可临时覆盖（测试 REAL 环境无需先改配置）。
    """
    broker = _normalize_broker(broker)
    _audit("test", auth, broker=broker)
    if payload and payload.values:
        allowed = set(BROKER_FIELDS[broker])
        clean = {k: str(v).strip() for k, v in payload.values.items() if k in allowed and str(v).strip()}
        _validate_fields(clean)
        stored = _read_config(redis, broker)
        _guard_endpoint_change(broker, clean, stored)
        stored.update(clean)
        _write_config(redis, broker, stored)
        _audit("test_save", auth, broker=broker, fields=sorted(clean))
    try:
        if broker == "tiger":
            from backend.services.trade.services.overseas_brokers import TigerBroker

            broker_obj = TigerBroker()
            account = await broker_obj.query_account("test")
            if account.get("total_asset"):
                return {"success": True, "message": f"连接成功，账户总资产 {account['total_asset']:.2f}"}
            return {"success": False, "message": "连接失败：请检查 Tiger ID / RSA 私钥 / 账户号，或券商侧 OpenAPI 权限"}
        if broker == "futu":
            from backend.services.trade.services.overseas_brokers import FutuBroker

            broker_obj = FutuBroker()
            env_override = str((payload.trade_env if payload else "") or "").upper()
            if env_override in {"REAL", "SIMULATE"}:
                broker_obj.trade_env_real = env_override == "REAL"
            account = await broker_obj.query_account("test")
            if account.get("total_asset"):
                env = "实盘" if broker_obj.trade_env_real else "模拟"
                return {"success": True, "message": f"FutuOpenD 已连接（{env}环境），账户总资产 {account['total_asset']:.2f}"}
            return {"success": False, "message": "FutuOpenD 未连接：请确认 OpenD 已启动并登录（扫码/设备验证），地址端口正确"}
        if broker == "ib":
            from backend.services.trade.services.overseas_brokers import IBBroker

            broker_obj = IBBroker()
            ib = await broker_obj._get_ib()
            accounts = ib.managedAccounts()
            broker_obj._ib.disconnect()  # 同步方法，不可 await
            return {"success": True, "message": f"IB Gateway 已连接，账户: {', '.join(accounts) or '未知'}"}
        if broker == "tdx":
            from backend.services.live_trading.services.broker_client import TdxBroker

            stored = _read_config(redis, broker)
            broker_obj = TdxBroker(
                bridge_url=stored.get("bridge_url", ""),
                bridge_token=stored.get("bridge_token", ""),
                account=stored.get("account", ""),
                account_type=stored.get("account_type", "") or "stock",
            )
            if not broker_obj.bridge_url:
                return {"success": False, "message": "未填写桥地址（如 http://192.168.31.13:8550）"}
            account = await broker_obj.query_account("test")
            if account.get("total_asset") or account.get("available_cash"):
                return {
                    "success": True,
                    "message": (
                        f"TDX 桥已连接，总资产 {float(account.get('total_asset') or 0):.2f}"
                        f"，可用 {float(account.get('available_cash') or 0):.2f}"
                    ),
                }
            return {"success": False, "message": "桥无响应或返回空账户：确认 Windows 桥已启动、token 一致、通达信已登录"}
        if broker == "qmt_exec":
            from backend.services.live_trading.services.qmt_exec_client import (
                QmtExecError,
                get_qmt_exec_client,
            )

            client = get_qmt_exec_client()
            await client.refresh_settings()  # 表单值刚落库，立即生效
            try:
                await client.ping()
                asset = await client.get_asset()
                positions = await client.get_positions()
            except QmtExecError as exc:
                return {
                    "success": False,
                    "message": f"QMT 执行端调用失败[{exc.code}]：{exc}",
                    "code": exc.code,
                }
            return {
                "success": True,
                "message": (
                    f"QMT 执行端已连接（{client.account_id}），"
                    f"总资产 {float(asset.get('total_asset') or 0):.2f}"
                    f"，可用 {float(asset.get('cash') or 0):.2f}"
                    f"，持仓 {len(positions)} 只"
                ),
            }
        return {"success": False, "message": "该券商暂不支持连接测试"}
    except Exception as exc:
        hint = {
            "futu": "FutuOpenD 未运行或未登录（需在 OpenD 客户端扫码/设备验证），并检查局域网 IP 与端口",
            "ib": "IB Gateway 未运行（4002=模拟 / 4001=实盘），并检查局域网 IP 与端口",
            "tiger": "检查 Tiger ID / RSA 私钥 / 账户号是否正确",
            "tdx": "检查 Windows 桥是否启动、桥地址/token 是否与桥端一致、防火墙是否放行 8550",
            "qmt_exec": "检查 QMT 是否开机登录、big-convert RPC 服务端是否启动、桥 Redis 地址/密码是否正确、防火墙是否放行",
        }.get(broker, "")
        return {"success": False, "message": f"连接失败：{exc}{('；' + hint) if hint else ''}"}


@router.get("/broker-config/{broker}")
async def get_broker_config(
    broker: str,
    auth: AuthContext = Depends(require_admin),
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    """读取券商接入配置（敏感字段脱敏为 *_configured）。"""
    broker = _normalize_broker(broker)
    stored = _read_config(redis, broker)
    fields: dict[str, Any] = {}
    for name, sensitive in BROKER_FIELDS[broker].items():
        value = str(stored.get(name, "") or "")
        if sensitive:
            fields[f"{name}_configured"] = bool(value)
        else:
            fields[name] = value
    return {
        "success": True,
        "broker": broker,
        "label": BROKER_LABELS[broker],
        "fields": fields,
    }


@router.put("/broker-config/{broker}")
async def update_broker_config(
    broker: str,
    payload: BrokerConfigUpdate,
    auth: AuthContext = Depends(require_admin),
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    """更新券商接入配置。未提供的敏感字段保持原值。"""
    broker = _normalize_broker(broker)
    allowed = set(BROKER_FIELDS[broker])
    unknown = set(payload.values) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"无效字段: {', '.join(sorted(unknown))}")

    _validate_fields(payload.values)
    stored = _read_config(redis, broker)
    _guard_endpoint_change(broker, payload.values, stored)
    for name, value in payload.values.items():
        text = str(value or "").strip()
        if text:
            stored[name] = text
        else:
            stored.pop(name, None)  # 空值清除
    _write_config(redis, broker, stored)
    # 审计只记字段名，不记值（值里可能有凭据）
    _audit("update", auth, broker=broker, fields=sorted(payload.values))
    if broker == "qmt_exec":
        # 页面改配置立即生效（否则要等常驻任务下一轮刷新）
        try:
            from backend.services.live_trading.services.qmt_exec_client import (
                get_qmt_exec_client,
            )

            get_qmt_exec_client().invalidate_settings()
        except Exception as exc:  # noqa: BLE001 - 热更新失败不影响配置落库
            logger.warning("清空 qmt_exec 配置缓存失败: %s", exc)

    return await get_broker_config(broker, auth, redis)
