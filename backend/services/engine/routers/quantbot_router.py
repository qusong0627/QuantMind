"""QuantBot 智能对话路由 — 替换 qwenpaw proxy"""

import json
import logging
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.services.engine.quantbot.intent_parser import parse_intent
from backend.services.engine.alpha_agent.hw_lock import HardwareLockError
from backend.services.engine.alpha_agent.launcher import get_launcher as get_alpha_agent_launcher
from backend.services.engine.quantbot.task_store import QuantBotTaskStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/quantbot", tags=["QuantBot"])

task_store = QuantBotTaskStore()

# AlphaAgent 回环调用（查询/回测/解读/导出已挖因子）
_ALPHA_BASE = os.getenv("ALPHA_AGENT_BASE_URL", "http://127.0.0.1:8000")


def _alpha_headers() -> dict[str, str]:
    """回环调用 AlphaAgent 的身份头（内部信任链，**不用口令**）。

    这里原是「拿 admin/admin123 去 /auth/login 换 JWT」——把一个管理员口令写进
    源码（公开仓 = 公开），而且那条路一旦口令变了就静默 401。可它本来就是**同进程
    回环**调用（`_ALPHA_BASE` 默认 127.0.0.1:8000），走 `X-Internal-Call` 这条
    内部信任链才是对的口径：密钥读 `shared/auth.get_internal_call_secret()`
    （唯一读取点，启动期自动生成并落盘；空 = fail-closed，绝不退回默认值）。
    """
    from backend.shared.auth import get_internal_call_secret

    secret = get_internal_call_secret()
    if not secret:
        logger.warning("内部调用密钥缺失：AlphaAgent 回环调用将拿不到管理员身份")
        return {}
    return {
        "X-Internal-Call": secret,
        "X-User-Id": os.getenv("ALPHA_INTERNAL_USER_ID", "10000001"),  # admin
        "X-Tenant-Id": os.getenv("ALPHA_INTERNAL_TENANT_ID", "default"),
    }


async def _alpha_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(f"{_ALPHA_BASE}{path}", headers=_alpha_headers())
        r.raise_for_status()
        return r.json()


async def _alpha_post(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            f"{_ALPHA_BASE}{path}", params=params, headers=_alpha_headers()
        )
        r.raise_for_status()
        return r.json()


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] | None = []


class ChatResponse(BaseModel):
    intent: str
    answer: str | None = None
    task_id: str | None = None


@router.post("/chat")
async def chat(request: Request, item: ChatRequest):
    """QuantBot 统一聊天接口

    - 一般对话：SSE 流式返回 LLM 回答
    - 因子挖掘：异步启动 AlphaAgent 演化，返回 task_id
    """
    user_context = getattr(request.state, "user", None)
    user_id = user_context.get("user_id", "anonymous") if user_context else "anonymous"

    # 1. 意图识别
    history = item.history or []
    intent = await parse_intent(item.message, history)

    if intent.get("intent") == "factor_evolution":
        result = await _handle_factor_evolution(item, user_id, intent)
        return JSONResponse(content=result)
    if intent.get("intent") == "factor_inquiry":
        result = await _handle_factor_inquiry(item, user_id, intent)
        return JSONResponse(content=result)
    else:
        return await _handle_chat_stream(item, history)


async def _handle_factor_evolution(
    item: ChatRequest,
    user_id: str,
    intent: dict,
) -> dict:
    """处理因子挖掘请求"""
    # 检查 LLM API key 是否可用
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if not api_key or "mock-api-key" in api_key:
        raise HTTPException(
            status_code=500,
            detail="API Key 未配置。请先在个人中心配置 DeepSeek API Key 后再使用因子演化功能。",
        )

    # 创建任务
    task_request = {
        "message": item.message,
        "intent": intent,
    }
    task_id = await task_store.create_task(user_id, task_request)

    # 异步启动演化 (AlphaAgent)
    alpha_launcher = get_alpha_agent_launcher()
    try:
        alpha_task_id = await alpha_launcher.start_evolution(
            user_id,
            universe=intent.get("constraints", {}).get("universe", "csi300"),
            loop_n=int(intent.get("constraints", {}).get("loop_n", 3)),
            direction=intent.get("description", item.message),
        )
    except HardwareLockError as exc:
        raise HTTPException(status_code=412, detail=str(exc)) from exc

    return {
        "intent": "factor_evolution",
        "task_id": alpha_task_id,
        "answer": f"已启动 AlphaAgent 因子演化任务：{intent.get('description', item.message)}",
    }


async def _handle_factor_inquiry(
    item: ChatRequest,
    user_id: str,
    intent: dict,
) -> dict:
    """处理因子后续操作：报告 / 回测 / 解读 / 导出"""
    action = intent.get("action", "report")
    constraints = intent.get("constraints", {}) or {}
    universe = constraints.get("universe", "csi300")
    target = (intent.get("target") or "").strip().lower()
    fid = (intent.get("factor_id") or "").strip()

    try:
        payload = await _alpha_get("/api/v1/alpha-agent/factors?limit=200")
    except Exception as e:
        return {"intent": "factor_inquiry", "answer": f"获取因子列表失败：{e}"}

    factors = payload.get("data", {}).get("factors", []) or []
    if not factors:
        return {
            "intent": "factor_inquiry",
            "answer": "还没有因子记录。先让我挖一批：例如「帮我挖掘连板情绪方向的因子」。",
        }

    # 定位目标因子：factor_id 优先，其次名称模糊匹配，否则取最高 |IC|
    hit = next((f for f in factors if f.get("factor_id") == fid), None)
    if hit is None and target:
        hit = next(
            (f for f in factors if target in f.get("factor_name", "").lower()), None
        )
    scored = sorted(
        [f for f in factors if f.get("ic_value") is not None],
        key=lambda f: -abs(f["ic_value"]),
    )
    if hit is None:
        hit = scored[0] if scored else factors[0]

    if action == "backtest":
        try:
            r = await _alpha_post(
                f"/api/v1/alpha-agent/factors/{hit['factor_id']}/backtest",
                params={
                    "start_date": "2025-01-01",
                    "end_date": datetime.now().strftime("%Y-%m-%d"),
                    "universe": universe,
                    "data_source": "qlib_bin",
                },
            )
            msg = r.get("data", {}).get("message", "已触发回测")
        except Exception as e:
            msg = f"触发回测失败：{e}"
        return {"intent": "factor_inquiry", "answer": f"因子「{hit.get('factor_name')}」回测：{msg}。稍后可用「看因子结果」查排行榜。"}

    if action == "explain":
        try:
            r = await _alpha_post(f"/api/v1/alpha-agent/factors/{hit['factor_id']}/explain")
            txt = r.get("data") or r.get("explanation") or r.get("message") or ""
        except Exception as e:
            txt = f"解读失败：{e}"
        return {
            "intent": "factor_inquiry",
            "answer": f"### {hit.get('factor_name')}（IC={hit.get('ic_value')}）\n\n{txt}",
        }

    if action == "export":
        if not scored:
            return {"intent": "factor_inquiry", "answer": "还没有完成回测的因子，先跑一次回测再导出。"}
        best = hit if hit.get("ic_value") is not None else scored[0]
        try:
            r = await _alpha_post(f"/api/v1/alpha-agent/factors/{best['factor_id']}/export")
            msg = r.get("data", {}).get("message", "已导出")
        except Exception as e:
            msg = f"导出失败：{e}"
        return {
            "intent": "factor_inquiry",
            "answer": f"已导出最高分因子「{best.get('factor_name')}」：{msg}。可在 AI-IDE 工作空间查看。",
        }

    # report（默认）
    lines = []
    if scored:
        for f in scored[:10]:
            lines.append(
                f"| {f.get('factor_name','?')} | {f.get('ic_value', float('nan')):+.4f} | "
                f"{f.get('rank_ic')} | {f.get('sharpe_ratio') if f.get('sharpe_ratio') is not None else ''} | {f.get('status')} |"
            )
        answer = (
            "当前因子按 |IC| 排序 Top10：\n\n"
            "| 因子 | IC | ICIR | Sharpe | 状态 |\n|---|---|---|---|---|\n" + "\n".join(lines) +
            "\n\n可以对因子做进一步操作：\n"
            "- 「回测这个因子」触发批量回测\n"
            "- 「解释一下因子 xxx」看逻辑\n"
            "- 「导出高分因子」提交进生产特征库"
        )
    else:
        answer = "已有因子记录，但都还没回测（IC 为空）。可以「回测这个因子」或「帮我挖掘 XX 因子」生成新因子。"
    return {"intent": "factor_inquiry", "answer": answer}


async def _handle_chat_stream(
    item: ChatRequest,
    history: list[dict],
) -> StreamingResponse:
    """一般对话 — SSE 流式"""
    base_url = (
        os.getenv("AI_IDE_LLM_BASE_URL")
        or os.getenv("AI_IDE_BASE_URL")
        or "https://api.deepseek.com"
    )
    model = os.getenv("AI_IDE_LLM_MODEL") or os.getenv("AI_IDE_MODEL") or "deepseek-v4-pro"
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )

    if not api_key or "mock-api-key" in api_key:
        raise HTTPException(
            status_code=500,
            detail="API Key 未配置。请在个人中心配置您的 API Key。",
        )

    system_prompt = (
        "你是 QuantMind 的智能量化助手 QuantBot。用简洁友好的中文回答。\n"
        "\n"
        "## 你的能力\n"
        "1. **因子挖掘 (AlphaAgent)** — 你内置了 AlphaAgent 因子演化引擎，"
        "可基于用户需求自动生成、回测、迭代量化因子。完整流程：\n"
        "   - 演化：按方向自动生成因子（连板情绪/筹码分布/隔夜收益/资金流持续性/"
        "动量质量/低波动/流动性/行业相对强度等方向均支持，也可自由描述）。\n"
        "   - 回测：每个因子自动轻量回测，产出 IC/ICIR/Sharpe/回撤。\n"
        "   - 解读/导出：对高分因子可解释逻辑、提交进生产特征库。\n"
        "   - 触发方式：用户表达「挖因子 / 进化因子 / 生成一批XX因子 / evolve factors」"
        "即自动启动后台演化（约 30–90 分钟/方向，返回 task_id，前端展示卡片轮询）。\n"
        "   - 股票池从用户话中自动识别（沪深300/中证500/创业板/科创板/全A，默认沪深300）。\n"
        "2. **因子后续操作** — 用户说「看因子结果/因子排行榜」（expert: 报告 Top10）、"
        "「回测这个因子」（触发批量回测）、「解释一下因子 xxx」（LLM 解读）、"
        "「导出/提交高分因子」（进生产特征库）时，交给意图引擎处理，不要自己编造因子数据。\n"
        "3. **回答量化研究问题** — 因子构造原理、回测指标解读、Qlib 用法、策略思路等。\n"
        "4. **不主动写代码** — 除非用户明确说「写代码」。\n"
        "\n"
        "## 挖好的因子去哪看 / 怎么测试\n"
        "- 演化完成的因子保存在数据库 `rd_agent_factors` 表，可通过 "
        "`GET /api/v1/alpha-agent/factors` 列出（自动按登录用户过滤），"
        "或通过 `GET /api/v1/alpha-agent/factors/{factor_id}` 看详情（含 IC、IR、夏普、因子代码）。\n"
        "- 一键回测：`POST /api/v1/alpha-agent/factors/{factor_id}/backtest`，"
        "回测结果会更新到同一条记录的 metrics 字段，可继续轮询。\n"
        "- 演化任务状态：`GET /api/v1/alpha-agent/tasks/{task_id}`\n"
        "- Alpha研究页面正在开发中，可直接访问 `/alpha-research` 查看因子列表和演化控制。\n"
        "\n"
        "## 行为约束\n"
        "- 收到「挖因子」请求时，**不要自己写因子代码**，直接交给意图引擎触发 AlphaAgent，"
        "回复用户「已启动演化任务」即可。\n"
        "- 用户问能做什么时，主动提示因子挖掘能力 + 上面的示例触发语。\n"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-10:]:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    messages.append({"role": "user", "content": item.message})

    async def event_generator():
        try:
            from backend.services.engine.alpha_agent.llm_client import (
                env_extra_headers,
                openai_chat_url,
            )

            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    openai_chat_url(base_url),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        **env_extra_headers(),
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": True,
                        "temperature": 0.7,
                    },
                ) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        logger.error(
                            f"QuantBot LLM Error: {response.status_code} {err_body.decode('utf-8', 'ignore')}"
                        )
                        yield f"data: {json.dumps({'error': f'AI 服务返回 {response.status_code}'})}\n\n"
                        return

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        if line.startswith("data: [DONE]"):
                            break
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                delta = data["choices"][0]["delta"]
                                if "content" in delta:
                                    yield f"data: {json.dumps({'delta': delta['content']}, ensure_ascii=False)}\n\n"
                            except Exception:
                                continue
        except Exception as e:
            logger.error(f"QuantBot chat stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/task/{task_id}")
async def get_task(task_id: str, request: Request):
    """查询任务状态和结果"""
    user_context = getattr(request.state, "user", None)
    user_id = user_context.get("user_id") if user_context else None

    task = await task_store.get_task(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": task.get("progress"),
        "result": task.get("result"),
        "error_message": task.get("error_message"),
        "factor_ids": task.get("factor_ids"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
    }


@router.get("/tasks")
async def list_tasks(request: Request):
    """列出当前用户的所有任务"""
    user_context = getattr(request.state, "user", None)
    if not user_context:
        raise HTTPException(status_code=401, detail="需要登录")

    user_id = user_context.get("user_id")
    tasks = await task_store.list_tasks(user_id)
    return {"tasks": tasks}
