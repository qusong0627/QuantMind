"""AI 服务配置的共享落点（AI-IDE 智能助手 ↔ Pine 转写共用）。

个人中心「AI 服务配置」把 API Key / 模型 / 接口地址写进 `user_profiles`（按用户）。
但 Pine 转写 worker 是**宿主**进程、没有用户会话，读不到 profile；所以在保存成功时
同步镜像一份到 `<数据根>/arena/ai_service.json`，转写脚本优先读它、再回退 .env。

⚠️ 该文件含明文 API Key：权限 0600、不入 git、任何日志都不打印内容。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from backend.services.agent_arena import arena_config


def config_path() -> Path:
    return arena_config.arena_dir() / "ai_service.json"


def read_config() -> dict:
    """读共享配置；缺失/损坏一律返回空 dict（调用方回退 .env）。"""
    p = config_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {
        "api_key": str(d.get("api_key") or "").strip(),
        "base_url": str(d.get("base_url") or "").strip(),
        "model": str(d.get("model") or "").strip(),
        "extra_headers": d.get("extra_headers") if isinstance(d.get("extra_headers"), dict) else {},
    }


def write_config(api_key: str = "", base_url: str = "", model: str = "",
                 extra_headers: dict | None = None) -> dict:
    """原子写共享配置。

    - api_key 显式空串 → **删除**文件（清 Key）
    - 三个核心字段（key/base/model）不齐 → 不写（避免半截配置覆盖旧值）
    """
    key = str(api_key or "").strip()
    if not key:
        config_path().unlink(missing_ok=True)
        return {}

    base = str(base_url or "").strip()
    mdl = str(model or "").strip()
    if not (base and mdl):
        return {}

    data = {
        "api_key": key,
        "base_url": base,
        "model": mdl,
        "extra_headers": extra_headers or {},
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    # 先按 0600 建临时文件再原子替换；避免「先写后 chmod」的窗口
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    # 容器里的 API 以 root 跑、宿主转写 worker 以数据目录属主跑：把文件归属对齐
    # 数据根（通常 UID 1000），否则 0600 的 root 文件宿主读不到（2026-11-15 实测）。
    try:
        owner = arena_config.data_root().stat()
        os.chown(path, owner.st_uid, owner.st_gid)
    except OSError:
        pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data
