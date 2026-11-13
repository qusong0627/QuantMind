"""arena 数据面 · 配置族：比赛配置（comp-config）+ 市场→券商映射（broker-market）。

移植自隔壁 api_server.py 同族 handler（L2043 GET/PUT comp-config、L598 GET/PUT
broker-market）。两处**落点改写**：
- comp-config 读写本包 `analysis_modes.CONFIG_FILE` = `<数据根>/arena/comp-config.json`
  （上游是隔壁 `configs/comp-config.json`；A1 已按同语义把这文件落在新址）；
- broker-market 从隔壁私有 `config/broker_market.json`（`config/` 永不过迁移闸）
  改到 `<数据根>/arena/broker_market.json`；初值中「cn→tdx / hk→tiger / us→ibkr」
  与上游文件内容一致，已随 A2 落盘（见计划文档）。

broker_market 写路径与上游有一处**加固偏离**：临时文件 + `os.replace` 原子替换
（上游直接 write_text，中断会留半截 JSON 导致映射读回默认值）。
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from backend.services.agent_arena.analysis_modes import MODES, load_selection, save_selection
from backend.services.agent_arena.arena_config import broker_market_file
from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter()


# ---------- 比赛配置（Arena「比赛配置」tab ↔ 分析引擎 的读写口） ----------

@router.get("/comp-config")
def get_comp_config(
    market: str = "cn",
    _current_user: dict = Depends(get_current_user),
):
    """比赛配置：目录（4 种配置的中文名/要求）+ 每模型当前多选（按市场分区）。

    market=cn/hk/us；不同市场各自维护模型多选，注入的系统提示词也按市场交易规则调整。
    """
    mk = (market or "cn").lower()
    return {
        "success": True,
        "data": {"market": mk, "catalog": MODES, "selection": load_selection(mk)},
    }


@router.put("/comp-config")
async def put_comp_config(
    request: Request,
    market: str = "cn",
    _current_user: dict = Depends(get_current_user),
):
    """保存每模型多选（按市场分区）：body = {"selection": {模型名: [配置id, ...]}}。"""
    mk = (market or "cn").lower()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    sel = body.get("selection")
    if not isinstance(sel, dict):
        raise HTTPException(400, "selection 必须是对象 {模型名: [配置id]}")

    valid = {m["id"] for m in MODES}
    cleaned: dict = {}
    for model, ids in sel.items():
        if not isinstance(ids, list):
            raise HTTPException(400, f"{model} 的配置必须是数组")
        cleaned[model] = [i for i in ids if i in valid]
    save_selection(mk, cleaned)
    return {"success": True, "data": {"market": mk, "selection": cleaned}}


# ---------- 市场→交易所映射（总控可配：哪个市场用哪个券商） ----------

_BROKER_MARKET_DEFAULT = {"cn": "tdx", "hk": "tiger", "us": "ibkr"}
# 通道现状：tdx=QM 桥（活着）；tiger/ibkr/futu 随上游退役、面板按用户裁决
# 显示「通道已下线」。choices 保留全集让总控 UI 形态不变（A6 决定措辞）；
# 真正用到券商的地方（执行面）在 QM 侧另有开关，不以此映射为准。
_BROKER_CHOICES = {
    "cn": ["tdx"],
    "hk": ["tiger", "ibkr", "futu"],
    "us": ["ibkr", "tiger", "futu"],
}


def _load_broker_market() -> dict:
    try:
        data = json.loads(broker_market_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                k: (v if v in _BROKER_CHOICES.get(k, []) else _BROKER_MARKET_DEFAULT[k])
                for k, v in data.items()
                if k in _BROKER_MARKET_DEFAULT
            }
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_BROKER_MARKET_DEFAULT)


@router.get("/broker-market")
def get_broker_market(_current_user: dict = Depends(get_current_user)):
    """市场→交易所映射（前端总控选择器读取）。"""
    return {
        "success": True,
        "data": {"mapping": _load_broker_market(), "choices": _BROKER_CHOICES},
    }


@router.put("/broker-market")
def put_broker_market(
    payload: dict = Body(...),
    _current_user: dict = Depends(get_current_user),
):
    """保存市场→交易所映射。values: {market: broker}"""
    values = payload.get("values") or {}
    current = _load_broker_market()
    for mkt, brk in values.items():
        if mkt in _BROKER_CHOICES and brk in _BROKER_CHOICES[mkt]:
            current[mkt] = brk
    target = broker_market_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return {"success": True, "data": {"mapping": current}}
