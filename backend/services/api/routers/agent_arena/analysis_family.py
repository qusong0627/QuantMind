"""arena 数据面 · 分析触发族（2026-10-08 复活）——「立即分析」两枚按钮的宿主链路。

上游两条触发（``POST /live/analyze``、``POST /news/analyze``）随原平台退役
（A6 曾把按钮置灰）。本族按原生契约复活，端点是新的、语义与旧的一致：

- ``POST /analysis/trigger``：写一条任务行（台账
  ``backend/services/agent_arena/analysis_ledger.py``，落 ``/data/logs/analysis_jobs.jsonl``），
  宿主 cron 的 ``scripts/analysis_trigger_worker.py`` 每分钟消费——
  ``news`` → ``scripts/news_brief.py``（QM 原生新闻管线）；
  ``live`` → ``scripts/live_model_analysis.py``（模型对话轮：读桥账户/持仓 + 当日
  新闻分子，各模型出一轮分析。**只出观点，不下单**——下单走决策轮/信号链）。
- ``GET /analysis/jobs``：任务台账（最新在前，按钮回显用）。

响应形状与代理时代逐位相同：``{success, data}`` 信封。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.agent_arena import analysis_ledger as ledger
from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter()

#: 单次触发最多点名的模型数（防构造超长 agents 数组灌台账）
_MAX_AGENTS = 10
_MAX_AGENT_LEN = 64


class TriggerBody(BaseModel):
    """``type``: live（模型对话轮）| news（新闻管线）；``agents``: "all" 或模型签名数组。"""

    type: str = Field(..., description="live | news")
    agents: str | list[str] = Field("all", description="all 或模型签名数组（live 用）")


def _normalize_agents(raw: str | list[str]) -> str | list[str]:
    """归一化 agents：'all' 原样；数组去重定序 + 逐项校验（防脏值进台账）。"""
    if isinstance(raw, str):
        if raw != "all":
            raise HTTPException(status_code=400, detail="agents 只接受 'all' 或数组")
        return raw
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(status_code=400, detail="agents 数组只接受非空字符串")
        name = item.strip()
        if len(name) > _MAX_AGENT_LEN:
            raise HTTPException(status_code=400, detail="agents 数组含超长项")
        if name not in seen:
            seen.append(name)
    if not seen:
        raise HTTPException(status_code=400, detail="agents 数组不能为空")
    if len(seen) > _MAX_AGENTS:
        raise HTTPException(status_code=400, detail=f"agents 最多 {_MAX_AGENTS} 项")
    return seen


@router.post("/analysis/trigger")
def trigger_analysis(
    body: TriggerBody,
    _current_user: dict = Depends(get_current_user),
):
    if body.type not in ledger.VALID_TYPES:
        raise HTTPException(status_code=400, detail="type 只接受 live | news")
    agents = "all" if body.type == "news" else _normalize_agents(body.agents)
    job = ledger.trigger_job(body.type, agents)
    return {"success": True, "data": job}


@router.get("/analysis/jobs")
def list_analysis_jobs(
    limit: int = Query(5, ge=1, le=50),
    _current_user: dict = Depends(get_current_user),
):
    rows = ledger.load_jobs()
    newest_first = list(reversed(rows))[:limit]
    return {"success": True, "data": newest_first}
