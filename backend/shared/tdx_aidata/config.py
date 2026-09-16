"""TdxAiData 配置唯一事实源（安装目录 / 开关 / Token 落点 / IPC 路径）。

优先级（读）：Redis 配置（前端「数据源设置」保存）> 环境变量 > 默认值。
Token 物理落点 = `<安装目录>/TdxAiData.ini` 的 `[Token] token=`（原生 .so 自行读取），
本模块提供外科手术式写入（只动该行，其余字节级保留，原子替换）。

约束：
- 默认目录 `/opt/tdx-aidata`；容器部署需将宿主机同名目录挂载进容器（docker-compose）。
- IPC 套接字路径固定（默认 /tmp/qm-tdx-aidata.sock）——**不随目录漂移**，
  保证同一容器内多服务共享同一个 worker 单实例。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DIR = "/opt/tdx-aidata"
REDIS_CONFIG_KEY = "qm:market:tdx_aidata:config"
DEFAULT_SOCKET = "/tmp/qm-tdx-aidata.sock"
DEFAULT_HOT_SET_KEY = "qm:hot_set:symbols"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


# ── 读取侧 ──────────────────────────────────────────────────────────


def _read_redis_config_sync() -> dict[str, str]:
    """同步读 Redis 配置（短超时；任何失败返回空表——配置读取永不阻断主链路）。"""
    try:
        import redis as _redis

        client = _redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        data = client.hgetall(REDIS_CONFIG_KEY) or {}
        client.close()
        return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001 - 配置读取失败回落 env/默认
        return {}


def resolve_dir() -> str:
    """安装目录：Redis 配置 > 环境变量 > 默认。"""
    cfg = _read_redis_config_sync()
    redis_dir = str(cfg.get("dir") or "").strip()
    if redis_dir:
        return redis_dir
    env_dir = str(os.getenv("TDX_AIDATA_DIR") or "").strip()
    if env_dir:
        return env_dir
    return DEFAULT_DIR


def is_enabled() -> bool:
    """启用开关：Redis 配置 > 环境变量禁用位 > 默认开。"""
    cfg = _read_redis_config_sync()
    raw = str(cfg.get("enabled") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    disabled = str(os.getenv("TDX_AIDATA_DISABLED") or "").strip().lower()
    if disabled in _TRUTHY:
        return False
    return True


def socket_path() -> str:
    """IPC 套接字路径（固定，不随目录漂移——跨服务单 worker）。"""
    env = str(os.getenv("QM_TDX_AIDATA_SOCKET") or "").strip()
    return env or DEFAULT_SOCKET


def subscription_enabled() -> bool:
    """订阅采集开关（T-P6-02）：Redis 配置 > env；默认关（T-P6-06 热集服务接线后再默认开）。"""
    cfg = _read_redis_config_sync()
    raw = str(cfg.get("subscription_enabled") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return str(os.getenv("TDX_AIDATA_SUBSCRIBE_ENABLED") or "").strip().lower() in _TRUTHY


def hot_set_key() -> str:
    """热集符号集 Redis 键（T-P6-06 维护；测试可 env 隔离）。"""
    env = str(os.getenv("QM_HOT_SET_KEY") or "").strip()
    return env or DEFAULT_HOT_SET_KEY


def ini_path(directory: str | None = None) -> str:
    return os.path.join(directory or resolve_dir(), "TdxAiData.ini")


def dir_ready(directory: str) -> bool:
    """安装目录就绪判据：动态库 + tqServer.py 均在。"""
    base = Path(directory)
    return (base / "libTdxAiData.so").is_file() and (base / "tqServer.py").is_file()


# ── ini Token 外科手术 ──────────────────────────────────────────────

_TOKEN_LINE_RE = re.compile(r"^(\s*token\s*=\s*)(.*)$", re.IGNORECASE)


def write_token(ini_file: str, token: str) -> None:
    """把 token 写入 ini 的 [Token] 段（只动该行；无段则追加；原子落盘）。"""
    path = Path(ini_file)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    lines = text.splitlines()

    section = None
    replaced = False
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
        if section == "token" and _TOKEN_LINE_RE.match(line) and not replaced:
            out_lines.append(f"token={token}")
            replaced = True
            continue
        out_lines.append(line)

    if not replaced:
        # 段头存在→插在段头之后；不存在→文件尾补段
        header_idx = None
        for i, line in enumerate(out_lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if s[1:-1].strip().lower() == "token":
                    header_idx = i
                    break
        if header_idx is None:
            out_lines.append("[Token]")
            out_lines.append(f"token={token}")
        else:
            out_lines.insert(header_idx + 1, f"token={token}")

    new_text = "\n".join(out_lines)
    if text.endswith("\n") or not text:
        new_text += "\n"

    # 原子替换（同目录临时文件 + os.replace）
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".ini-token-", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def token_status(ini_file: str) -> dict[str, Any]:
    """Token 状态（脱敏：只回掩码后 4 位，绝不回明文）。"""
    token = ""
    try:
        text = Path(ini_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
        if section == "token":
            m = _TOKEN_LINE_RE.match(line)
            if m:
                token = m.group(2).strip()
                break
    if not token:
        return {"configured": False, "masked": ""}
    tail = token[-4:] if len(token) >= 4 else token
    return {"configured": True, "masked": f"••••{tail}"}


def save_config_sync(client, updates: dict[str, Any]) -> dict[str, str]:
    """保存配置到 Redis（前端「数据源设置」调用；空值删除键）。

    client 为同步 redis 客户端（调用方负责连接与线程卸载）。
    """
    mapping = {}
    for key in ("dir", "enabled"):
        if key in updates and updates[key] is not None:
            value = str(updates[key]).strip()
            if value:
                mapping[key] = value
            else:
                client.hdel(REDIS_CONFIG_KEY, key)
    if mapping:
        client.hset(REDIS_CONFIG_KEY, mapping=mapping)
    return {str(k): str(v) for k, v in (client.hgetall(REDIS_CONFIG_KEY) or {}).items()}


def public_config() -> dict[str, Any]:
    """API/前端可读配置视图（无任何密钥明文）。"""
    directory = resolve_dir()
    ready = dir_ready(directory)
    return {
        "dir": directory,
        "enabled": is_enabled(),
        "dir_ready": ready,
        "socket_path": socket_path(),
        "token": token_status(ini_path(directory)),
    }
