"""AlphaAgent / RD-Agent 因子挖掘 REST API

支持多市场因子挖掘: A股、加密货币、港股、美股
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pandas 在函数内按需 import；这里只为字符串注解提供名字
    import pandas as pd

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.services.engine.alpha_agent import direction_quota, profile_gateway
from backend.services.engine.alpha_agent.algorithm_registry import (
    DEFAULT_ALGORITHM_KEY,
    AlgorithmError,
    list_algorithms,
    resolve_algorithm,
)
from backend.services.engine.alpha_agent.direction_decompose import (
    MAX_CARDS_DEFAULT,
    MAX_CARDS_LIMIT,
)
from backend.services.engine.alpha_agent.direction_sampling import (
    sample_weighted_direction,
    sample_weighted_directions_n,
)
from backend.services.engine.alpha_agent.doc_gate import require_doc_mining
from backend.services.engine.alpha_agent.doc_store import get_doc_store
from backend.services.engine.alpha_agent.hw_lock import HardwareLockError
from backend.services.engine.alpha_agent.launcher import (
    QueueFullError,
    capacity_clamp_reason,
    get_launcher,
    minute_scope,
    normalize_mining_config,
    require_minute_window,
)
from backend.services.engine.alpha_agent.mining_config import (
    FREQ_MINUTE,
    SUPPORTED_FREQS,
    Window,
    normalize_freq,
)
from backend.services.engine.alpha_agent.task_store import get_mining_task_store
from backend.services.engine.auth_context import (
    assert_identity_not_spoofed,
    get_authenticated_identity,
)
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)
from backend.shared.stock_pool.builtins import cn_index_symbols

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alpha-agent", tags=["AlphaAgent"])
persistence = RDAgentFactorPersistence()

# P2：CN 可选股票池由 shared.stock_pool.builtins 派生（唯一事实源），
# 与 quantdb_hub.UNIVERSE_MAP / Strategy Lab 白名单同源，不再各写一份。
_VALID_CN_UNIVERSES: list[str] = list(cn_index_symbols().keys())

#: 提交前 direction 的长度闸（T-FM-10）。task_store 的 20k 是**存储层**兜底
#: （静默截断）；这里 8k 是**提交层**显式拒绝——文档整理草稿可长，但方向文本
#: 塞给 RD-Agent 子进程是有成本的，超限要让用户看到并自己精简，不是被截。
MAX_SUBMIT_DIRECTION_CHARS = 8000

#: 批量派发的单次条数上限。拆解上限 12 张卡，正常批量都在其内；明显超限的
#: 请求是误用（且会瞬间占满排队深度），整包先拒而不是逐条失败。
MAX_BATCH_DISPATCH_ITEMS = 20

#: 挖掘频率目录的中文标签（值域与 mining_config.SUPPORTED_FREQS 同源：加频率只
#: 改那处常量，这里补标签，不会出现"前端能选、后端拒收"）。
_FREQ_LABELS: dict[str, str] = {
    "day": "日线（默认）",
    "1min": "分钟 1min（仅 A 股）",
}


class EvolveRequest(BaseModel):
    """evolve 的 JSON body 变体（T-FM-10）。

    老前端只带 query（payload=None），行为一字不变；新前端走 body 并带
    ``doc_id`` 记录文档血统。合并语义：body 里**非 None** 的字段覆盖 query。
    """

    market: str | None = None
    universe: str | None = None
    loop_n: int | None = Field(None, ge=1, le=20)
    direction: str | None = None
    directions: list[str] | None = None
    direction_mode: str | None = None
    data_source: str | None = None
    #: 并行方向数（T-MV-04）：仅类别路径（directions 非空）且 >1 时一次派 N 条
    #: 任务；自由文本/文档血统路径忽略（单方向是它们的事实）。
    num_directions: int | None = Field(None, ge=1, le=10)
    #: 入库闸门模式（T-MV-05）：off/soft/hard；None=未指定（入池时叠
    #: env ALPHA_GATE_MODE 解析，两层都没指定则回落逐门禁配置）
    quality_gate_mode: str | None = None
    #: 文档血统：来自文档链的挖掘任务带上它（写 rd_agent_mining_tasks.doc_id +
    #: 回写 rd_agent_docs.task_id）；需 ENABLE_DOC_MINING=true
    doc_id: str | None = None
    #: 自定义挖掘数据区间（移植 P1）：ISO 日期；None/空 = 不指定
    #: （默认窗口，行为与迁移前逐位一致）。两端须成对。
    mining_start: str | None = None
    mining_end: str | None = None
    #: 因子检验区间（样本外，可选）：起点须 ≥ 挖掘终点；只填一端即 400
    valid_start: str | None = None
    valid_end: str | None = None
    #: 挖掘数据集选择（登记序 key，见 /mining/config-options）：None/[] = 全选
    datasets: list[str] | None = None
    #: 挖掘频率（移植 P1.5）：None/"" = 日线（默认）；"1min" = 分钟
    #: 原生挖掘（仅 A 股；窗口锚定分钟可用连续块，见 /mining/config-options）
    freq: str | None = None
    #: 挖掘算法（移植 P2）：None/"" = 默认 rdagent（迁移前唯一路径）；
    #: 可选值见 GET /algorithms（如 statistical=统计量价挖掘，不消费
    #: 方向文本/数据集选择/loop_n）
    algorithm: str | None = None


class DecomposeRequest(BaseModel):
    """拆解请求：一段粗方向 → N 张正交子假设卡片（只拆解，不启动挖掘）。"""

    direction: str = Field(
        "", description="待拆解的粗方向（研报摘录/长文指令），上限与存储闸同一口径"
    )
    market: str = Field("a_share", description="目标市场")
    universe: str = Field("csi300", description="股票池")
    max_cards: int | None = Field(
        None,
        description=(
            f"卡片数上限（默认 {MAX_CARDS_DEFAULT}、上限 {MAX_CARDS_LIMIT}，越界自动收敛）"
        ),
    )
    seed_factor_ids: list[str] = Field(
        default_factory=list,
        description=(
            "种子（父本）因子 id（≤3，取自本用户本市场因子池）：拆解围绕父本做"
            "受控变异，卡片带 seed_factor_id 血统；超量整包 400"
        ),
    )


class MiningBatchRequest(BaseModel):
    """批量派发请求：每条 direction 独立成任务；名满自动排队（不 429 背压）。"""

    directions: list[str] = Field(
        ...,
        min_length=1,
        description="按顺序派发的挖掘方向列表（通常是拆解卡片拼接文本）",
    )
    market: str = Field("a_share", description="目标市场")
    universe: str = Field("csi300", description="股票池")
    loop_n: int | None = Field(
        None, ge=1, le=20, description="每任务演化轮数（默认 5）"
    )
    #: 入库闸门模式（T-MV-05）：off/soft/hard；None=未指定（入池时叠 env 解析）
    quality_gate_mode: str | None = Field(
        None, description="入库闸门模式: off/soft/hard；空=未指定（回落默认软闸）"
    )
    #: 自定义挖掘配置（移植 P1），语义同 evolve 的 body 字段：
    #: 卡片批量派发与单条 evolve 走同一套校验/落库/注入，不许两条入口两种口径。
    mining_start: str | None = None
    mining_end: str | None = None
    valid_start: str | None = None
    valid_end: str | None = None
    datasets: list[str] | None = None
    #: 挖掘频率（P1.5）：None/"" = 日线；"1min" = 分钟原生（仅 A 股；不指定
    #: 窗口时自动锚定分钟可用连续块——整批同窗，与 evolve 同一套归一）
    freq: str | None = None
    #: 挖掘算法（P2）：None/"" = 默认 rdagent；整批同一算法（与其它字段同语义）
    algorithm: str | None = None


def _normalize_pool_ref(universe: str) -> str:
    u = (universe or "").strip()
    if not u:
        return "pool:csi300"
    if u.startswith(("pool:", "pool_id:", "list:", "file:")):
        return u
    return f"pool:{u}"


def _universe_is_valid(universe: str) -> bool:
    if universe in _VALID_CN_UNIVERSES:
        return True
    try:
        from backend.shared.stock_pool.resolver import resolve_pool_sync

        snap = resolve_pool_sync(_normalize_pool_ref(universe))
        return bool(snap.unfiltered or snap.symbols)
    except Exception:
        return False


def _resolve_custom_pool_instruments(universe: str) -> list[str] | None:
    """非内置 code 时尝试全局股票池解析；内置池返回 None 走原逻辑。"""
    if universe in _VALID_CN_UNIVERSES:
        return None
    try:
        from backend.shared.stock_pool.resolver import resolve_pool_sync
        from backend.shared.stock_utils import StockCodeUtil

        snap = resolve_pool_sync(_normalize_pool_ref(universe))
        if snap.unfiltered:
            return None
        if not snap.symbols:
            return []
        return sorted({StockCodeUtil.to_prefix(s) for s in snap.symbols})
    except Exception as e:
        logger.warning("custom pool %s resolve failed: %s", universe, e)
        return None


_running_backtests: set[str] = set()
# 回测子进程句柄 + 取消标记：cancel 接口据此真正 kill 子进程
_backtest_processes: dict[str, subprocess.Popen] = {}
_backtest_cancelled: set[str] = set()
# 本进程内「因子 → 当前未完结运行的 run_id」注册表：历史台账按 run_id 精确收口。
# 不用「最新未完结行」启发式——取消会先放行去重闸（允许立即重跑），旧任务延迟
# 收尾时若按「最新」找行，会把**新任务**的行收错（真结果永久丢失）；按 run_id
# 收口后旧任务对自己的行幂等无操作，新任务不受影响。
_running_backtest_runs: dict[str, str | None] = {}


# 向量检索（embedding）通道的 profile 字段。三个通道彼此独立：chat 供应商
# （DeepSeek 等）通常没有 /embeddings 端点，必须能单独指向别的供应商或本地服务。
_EMBEDDING_FIELDS = ("embedding_model", "embedding_base_url", "embedding_api_key")

# 掩码最短长度：短于这个长度就不做「首3末4」——9 位 Key 用「首3末4」会露出 7/9。
# 真实供应商的 Key 远长于此，取 16 是留出安全边际而不是卡住谁。
_MIN_MASKABLE_KEY_LEN = 16


class EmbeddingFieldError(ValueError):
    """embedding 字段值不是字符串（含字段名，供端点拼 400 文案）。"""


def build_embedding_payload(body: dict) -> dict:
    """从请求体挑出**显式传入**的 embedding 字段，组装 Profile 更新 payload。

    未传 = 不动，``""`` = 清除，``null`` = 未传。这条语义是硬要求：如果改成
    「全量提交」，用户只改模型名就会把已存的 Key 一起清空，子进程随即退回
    容器级 ``EMBEDDING_*`` —— 表现为「改了个模型名，检索悄悄换了个供应商」，
    而没有任何一层会报错。

    非字符串一律拒绝，不做 ``str()`` 兜底：``str(["https://x/v1"])`` 得到
    ``"['https://x/v1']"``，会以 200 落库、界面显示「已保存」，直到子进程
    拿着这串垃圾去请求才以不透明错误失败。
    """
    payload: dict = {}
    for key in _EMBEDDING_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if value is None:
            continue  # JSON null 是「没这个字段」，不是「清空」
        if not isinstance(value, str):
            raise EmbeddingFieldError(f"{key} 必须是字符串")
        payload[key] = value.strip()
    return payload


def normalize_embedding_status(profile_data: dict | None) -> dict:
    """Profile → 可回显的 embedding 状态（**绝不带明文 Key**）。

    短 Key 不做「首3末4」掩码：那样等于把整条 Key 原样打印出来。

    model/base_url 一并 strip：运行时取配置时会 strip（``"  "`` 等同未配置），
    这里不 strip 就会出现「配置页显示已填、挖掘实际用容器默认值」的口径差。
    """
    data = profile_data or {}
    key = str(data.get("embedding_api_key") or "").strip()
    return {
        "model": str(data.get("embedding_model") or "").strip(),
        "base_url": str(data.get("embedding_base_url") or "").strip(),
        "has_key": bool(key),
        "key_masked": (
            f"{key[:3]}****{key[-4:]}" if len(key) >= _MIN_MASKABLE_KEY_LEN else ""
        ),
    }


def _profile_gateway() -> str:
    return profile_gateway.profile_gateway_url()


async def _fetch_profile_raw(user_id: str, tenant_id: str) -> dict | None:
    """直接取用户 Profile 原始字段（不做「有没有 chat key」的判断）。

    实现已迁至 ``alpha_agent.profile_gateway``（文档解析后台轮询也要读
    Profile——非路由代码不该 import 路由私有名）。保留本名做兼容层：
    本模块多处调用与既有测试 monkeypatch 的都是这个名字。
    """
    return await profile_gateway.fetch_profile_raw(user_id, tenant_id)


async def _update_profile(user_id: str, tenant_id: str, payload: dict) -> None:
    """把 payload 写回 Profile。失败抛 HTTPException（调用方据此回滚 UI 状态）。"""
    from backend.shared.auth import get_internal_call_secret

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                f"{_profile_gateway()}/api/v1/profiles/{user_id}",
                headers={
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id,
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        # 连接失败/超时：网关没起来或地址错。不转成 502 的话冒泡成 500，
        # 前端拿到的就是「服务器内部错误」，与「写失败」这个可操作信息无关。
        # 不记 exc 详情：URL 里含 user_id，且异常链可能带上请求体（含 Key）。
        logger.error("[alpha-agent] update profile %s: %s", user_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="保存失败，请稍后重试") from exc
    if resp.status_code != 200:
        # 不记 resp.text：FastAPI 422 的 detail[].input 会把明文密钥原样回显，
        # 等于把 Key 写进服务端日志。本请求体含 embedding_api_key。
        logger.error(
            "[alpha-agent] update profile %s: http %s", user_id, resp.status_code
        )
        raise HTTPException(status_code=502, detail="保存失败，请稍后重试")


def llm_config_from_profile(data: dict):
    """Profile 原始字段 → ``LLMConfig``（**不做「chat 是否配全」的判断**）。

    拆出来是为了让 embedding 通道能独立于 chat 取值：一个账号可能 chat 走
    容器级 env（OSS 默认），但 embedding 在个人中心配了自己的供应商。若把
    两者绑在同一个「三件套齐全才算配置」的判断里，用户的 embedding 会静默
    失效——这正是本次改动要消灭的口径。

    chat 三件套（key/base/model）可能为空，由调用方各自决定够不够用。
    """
    from backend.services.engine.alpha_agent.llm_client import (
        LLMConfig,
        normalize_embedding_base_url,
        parse_extra_headers,
    )

    base = (data.get("llm_base_url") or "").strip().rstrip("/")
    model = (data.get("llm_model") or "").strip()
    # DeepSeek 等 Anthropic 兼容端点（.../anthropic）走 Anthropic 协议
    protocol = (
        "anthropic"
        if "/anthropic" in base or model.lower().startswith("astron")
        else "openai"
    )
    # OpenAI 兼容端点统一保留 /v1（实际调用/RD-Agent 子进程都按 {base}/chat/completions 拼接）。
    # base 为空时**不能**补成 "/v1"：那会凭空造出一个看似合法的地址。
    if protocol == "openai" and base and not base.endswith("/v1"):
        base += "/v1"
    return LLMConfig(
        api_key=(data.get("api_key") or "").strip(),
        base_url=base,
        model=model,
        protocol=protocol,
        headers=parse_extra_headers(data.get("llm_extra_headers")),
        # 向量检索（embedding）通道：可指向与 chat 完全不同的供应商/本地服务。
        # 缺失时留空，由容器级 EMBEDDING_* 兜底（见 rd_agent/llm_env.build_llm_env）。
        embedding_model=(data.get("embedding_model") or "").strip(),
        embedding_base_url=normalize_embedding_base_url(data.get("embedding_base_url")),
        embedding_api_key=(data.get("embedding_api_key") or "").strip(),
    )


def profile_embedding_overrides(data: dict | None) -> dict:
    """Profile → 子进程用的 ``EMBEDDING_*`` env（**只含用户显式填了的项**）。

    与 chat 的取值来源无关：无论 chat 来自 Profile 还是容器 env，只要用户在
    个人中心配过 embedding，就必须把这组变量传给子进程。否则用户在配置页
    看到「已保存」，挖掘却按容器默认供应商检索——静默换供应商，没有任何一层
    会报错。空项一律不产出，交由容器级 ``EMBEDDING_*`` 兜底。
    """
    from backend.services.engine.alpha_agent.llm_client import _is_placeholder

    if not data:
        return {}
    try:
        cfg = llm_config_from_profile(data)
    except Exception:
        logger.exception("[alpha-agent] build embedding overrides failed")
        return {}
    if not (
        cfg.embedding_model.strip()
        and cfg.embedding_base_url.strip()
        and cfg.embedding_api_key.strip()
    ):
        return {}
    # 占位符（如 "your-api-key"）不是有效配置，别覆盖容器级兜底
    if _is_placeholder(cfg.embedding_api_key):
        return {}
    return cfg.embedding_env_overrides()


def build_subprocess_overrides(llm_config, embedding_env: dict | None) -> dict:
    """子进程 env = chat 覆盖 ∪ embedding 覆盖（embedding 后写，优先级更高）。

    两组变量来源可以不同：chat 走容器 env（``resolve_llm_config``，其
    ``LLMConfig`` 的 embedding 字段恒为空）而 embedding 来自用户 Profile。
    因此必须**分头取值再合并**，不能只下发 ``llm_config.llm_env_overrides()``
    —— 那正是「用户配了 embedding 却按容器默认供应商检索」的成因。
    """
    return {**llm_config.llm_env_overrides(), **(embedding_env or {})}


async def _fetch_profile_llm_config(
    user_id: str, tenant_id: str, *, data: dict | None = None
):
    """读取用户个人中心「AI 服务配置」（Profile 的 api_key/llm_base_url/llm_model）。

    与 AI-IDE 共享同一份凭证。无有效 Key 返回 None。

    ``data`` 可由调用方预先取好传进来，避免同一个请求里重复打网关——注意
    chat 配置**不可**复用 embedding 状态那条路径，反之亦然：二者判「有没有配」
    的条件不同（chat 要求 key+base+model 三件套齐全）。
    """
    from backend.services.engine.alpha_agent.llm_client import _is_placeholder

    try:
        if data is None:
            data = await _fetch_profile_raw(user_id, tenant_id)
        if data is None:
            return None
        cfg = llm_config_from_profile(data)
        if not cfg.api_key or _is_placeholder(cfg.api_key):
            return None
        # 以用户设置为准：base/model 缺失视为未配置，回退 env 兜底
        if not cfg.base_url or not cfg.model:
            return None
        return cfg
    except Exception:
        logger.exception("[alpha-agent] fetch profile llm config failed")
        return None


async def _resolve_effective_llm_config(user_id: str, tenant_id: str):
    """以用户设置为准：优先当前用户 Profile 的「AI 服务配置」，环境变量仅作兜底。

    Returns: ``(LLMConfig | None, source, embedding_env)``。``embedding_env``
    是**独立于 source** 的：chat 走 env 时用户配的 embedding 同样要生效。
    """
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

    data = await _fetch_profile_raw(user_id, tenant_id)
    embedding_env = profile_embedding_overrides(data)
    cfg = await _fetch_profile_llm_config(user_id, tenant_id, data=data)
    if cfg is not None:
        return cfg, "user_profile", embedding_env
    cfg = resolve_llm_config()
    if cfg is not None:
        return cfg, "env", embedding_env
    return None, "none", embedding_env


async def _launcher_llm_override_resolver(user_id: str, tenant_id: str) -> dict | None:
    """排队任务排空时的 LLM 覆盖重解析（注册给 launcher）。

    排队行**绝不持久化密钥**：排空时按 (user_id, tenant_id) 走与 evolve
    完全同一条取值链（用户 Profile 优先、容器 env 兜底）。返回 None 语义 =
    「两边都没有配置」——launcher 据此把任务显式置失败（与 evolve 的 412
    同一口径），绝不静默换供应商。
    """
    llm_config, llm_source, embedding_env = await _resolve_effective_llm_config(
        user_id, tenant_id
    )
    if llm_config is None:
        return None
    logger.info(
        "[alpha-agent] queue drain llm re-resolve source=%s model=%s",
        llm_source,
        llm_config.model,
    )
    return build_subprocess_overrides(llm_config, embedding_env)


def register_mining_queue_llm_resolver() -> None:
    """engine 启动期把排空重解析器注册到 launcher（见 main_oss lifespan）。"""
    from backend.services.engine.alpha_agent.launcher import set_llm_override_resolver

    set_llm_override_resolver(_launcher_llm_override_resolver)


class FactorBacktestCancelled(RuntimeError):
    """用户主动取消因子回测（子进程被 kill）。"""


async def _run_subprocess_tracked(
    factor_id: str,
    args: list[str],
    timeout: float = 600,
) -> tuple[int, str, str]:
    """运行回测子进程并登记句柄，供 cancel 接口 kill。

    Returns: (returncode, stdout, stderr)。用户取消时抛 FactorBacktestCancelled。
    """
    import subprocess

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    _backtest_processes[factor_id] = proc
    try:
        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = await asyncio.to_thread(proc.communicate)
            raise RuntimeError(
                f"因子计算超时（>{int(timeout)}s），子进程已终止"
            ) from None
        if factor_id in _backtest_cancelled:
            raise FactorBacktestCancelled("回测已被用户取消")
        return proc.returncode, stdout or "", stderr or ""
    finally:
        _backtest_processes.pop(factor_id, None)


async def _require_owned_task(task_id: str, request: Request) -> dict:
    """返回任务状态，若不属于当前认证用户则 404（不泄露任务是否存在）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    launcher = get_launcher()
    status = await launcher.get_task_status(task_id)
    if not status or status.get("user_id") != auth_user_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return status


async def _require_owned_factor(
    factor_id: str, request: Request, *, for_write: bool = False
) -> dict:
    """返回因子，若不属于当前认证用户则 404。

    历史因子的 user_id 可能为空（该列加入前写入），此类记录允许只读访问，
    但禁止写操作（回测/解释会回填 metrics），避免跨用户篡改。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    factor = await persistence.get_factor(factor_id)
    if not factor:
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    owner = factor.get("user_id")
    if owner != auth_user_id and (owner or for_write):
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    return factor


@router.get("/markets")
async def list_markets():
    """列出所有可用的市场"""
    from backend.services.engine.rd_agent.market_adapters import (
        list_markets as _list_markets,
    )

    markets = _list_markets()
    # Check data readiness for each market
    for m in markets:
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter

            adapter = get_adapter(m["market_id"])
            m["data_ready"] = adapter.is_data_ready()
        except Exception:
            m["data_ready"] = False
    return {"code": 200, "data": {"markets": markets, "total": len(markets)}}


@router.get("/algorithms")
async def list_mining_algorithms():
    """挖掘算法目录（移植 P2）——前端下拉的唯一数据源。

    与提交路径同一份 manifest（``algorithm_registry``）：**能力面字段随目录
    一起下发**（markets/freqs/uses_datasets/uses_llm/requires_gpu/stages），
    前端按它决定显示哪些配置控件（数据集选择/演化轮数/方向输入），不硬编码
    算法名。坏 manifest 如实带 ``valid=false`` + 原因，不拖垮列表。
    """
    return {
        "code": 200,
        "data": {
            "algorithms": list_algorithms(),
            "default": DEFAULT_ALGORITHM_KEY,
        },
    }


async def _resolve_multi_picks(
    clean_dirs: list[str],
    num_directions: int,
    record_mode: str,
    *,
    user_id: str,
    market: str,
) -> list[tuple[str, str | None]]:
    """并行方向数（T-MV-04）：解析一次派发的 N 条方向，返回 ``(direction, meta_json)``。

    纪律与单条路径同型：

    - selected：按传入顺序取前 N——确定性，没抽签就没有抽签凭证（meta=None）。
    - random：空白度加权抽 N 条**互不相同**（T-MV-03/04 同一权重口径），
      逐条带可独立重放的 meta；抽样模块整体异常 → 普通不放回随机 + meta=None
      （last-resort fail-open：抽样证据绝不拦任务创建）。
    """
    if record_mode != "random":
        return [(d, None) for d in clean_dirs[:num_directions]]
    try:
        pairs = await sample_weighted_directions_n(
            clean_dirs, num_directions, user_id=user_id, market=market
        )
        return [
            (d, json.dumps(meta, ensure_ascii=False) if meta else None)
            for d, meta in pairs
        ]
    except Exception as e:  # noqa: BLE001 - 抽样证据不许拦任务创建
        logger.warning("[alpha-agent] weighted sampling failed, plain sample: %s", e)
        import random as _random

        return [
            (d, None)
            for d in _random.sample(clean_dirs, k=min(num_directions, len(clean_dirs)))
        ]


def _sampling_record_mode(direction_mode: str | None) -> str:
    """方向抽样模式归一（T-FE-10 三值）：random/quota 原样，其余（含空/脏值）→ selected。"""
    return direction_mode if direction_mode in ("random", "quota") else "selected"


async def _resolve_quota_picks(
    clean_dirs: list[str],
    num_directions: int,
    *,
    user_id: str,
    market: str,
) -> list[tuple[str, str | None]]:
    """方向配额（T-FE-10）：三档预算抽 N 条，返回 ``(direction, meta_json)``。

    ``direction_quota.allocate`` 内部已有两层兜底（输入读失败 → random 空白度
    加权 + ``quota_fallback`` 标注；N ≥ 候选数 → 全集直派 meta=None）——这里
    再兜一层 last-resort（与 ``_resolve_multi_picks`` 同纪律）：抽样证据绝不
    拦任务创建。
    """
    try:
        pairs = await direction_quota.allocate(
            clean_dirs, num_directions, user_id=user_id, market=market
        )
        return [
            (d, json.dumps(meta, ensure_ascii=False) if meta else None)
            for d, meta in pairs
        ]
    except Exception as e:  # noqa: BLE001 - 抽样证据不许拦任务创建
        logger.warning("[alpha-agent] quota allocation failed, plain sample: %s", e)
        import random as _random

        return [
            (d, None)
            for d in _random.sample(clean_dirs, k=min(num_directions, len(clean_dirs)))
        ]


#: QuantDB 数据覆盖缓存时长（秒）。覆盖只随日更变化，按请求现读分区目录
#: （2000+ 个 dt= 目录）没必要；TTL 内复用同一份 Window。
_COVERAGE_TTL_S = 300.0

#: ``(读取时刻 monotonic, Window | None)``；``(0.0, None)`` = 尚未读过，
#: ``(ts, None)`` = 读过但读不到（读不到也缓存，防每次请求刷日志）。
_coverage_cache: tuple[float, Window | None] = (0.0, None)


def _quantdb_coverage() -> Window | None:
    """QuantDB 日线数据覆盖窗口 → ``mining_config.Window``；读不到回 None。

    覆盖 = ``1_kline_data/daily_forward``（挖掘 h5 实际读取的库）分区目录
    ``dt=YYYYMMDD`` 的 min/max——与数据面同源，不用交易日历冒充数据覆盖。
    ``None`` 的语义是「不做覆盖校验」（``validate_coverage`` 契约：宁可放行
    由引擎侧失败，也不做假校验）。结果（含失败）按 :data:`_COVERAGE_TTL_S`
    缓存：覆盖只随日更变化，按请求现读 2000+ 个分区目录没有必要。
    """
    global _coverage_cache
    now = time.monotonic()
    ts, cached = _coverage_cache
    if ts and now - ts < _COVERAGE_TTL_S:  # ts=0 → 从未读过
        return cached
    coverage = None
    try:
        from backend.services.engine.rd_agent.rd_loop_wrapper import (
            RDLoopWrapper,
            quantdb_coverage_from_dir,
        )

        coverage = quantdb_coverage_from_dir(RDLoopWrapper._resolve_quantdb_dir())
    except Exception as exc:  # noqa: BLE001 - 覆盖读不到不拦请求（只记一笔）
        logger.warning("[alpha-agent] QuantDB 覆盖读取失败，跳过窗口覆盖校验: %s", exc)
    _coverage_cache = (now, coverage)
    return coverage


def _backtest_guard(coverage: Window | None) -> date | None:
    """回测可用末端（qlib 日历倒数第二个交易日 ∩ 覆盖末端）；读不到回 None。

    自定义窗口的终点必须退让到这一天：qlib 回测按 t+1 成交需要次日行情，
    终点压在日历最后一根 bar 上必崩（见 ``rd_loop_wrapper
    .qlib_backtest_data_end`` 的实证记录）。单独抽一层是为了让测试能像
    ``_quantdb_coverage`` 一样整体替换（本函数会读 provider 日历目录）。
    """
    from backend.services.engine.rd_agent.rd_loop_wrapper import (
        qlib_backtest_data_end,
    )

    return qlib_backtest_data_end(coverage)


def _normalize_request_mining_config(
    *,
    market: str,
    mining_start: str | None,
    mining_end: str | None,
    valid_start: str | None,
    valid_end: str | None,
    datasets: Sequence[str] | None,
    freq: str | None = None,
) -> tuple[
    str | None, str | None, str | None, str | None, tuple[str, ...] | None, str | None
]:
    """请求级挖掘配置归一 + 覆盖校验（evolve / mining/batch / config-options 共用）。

    非法配置抛 ``ValueError``（调用方转 400——静默回默认窗会让用户以为自定义
    区间生效了）。返回值可直接展开为 launcher 的挖掘配置 kwargs，未指定/全选
    一律回 ``None``（launcher 契约：None=默认窗口、None=全选数据集、None=日线），
    保证「用户没动过挖掘配置」的任务行不留参数痕迹。

    ``freq`` = ``"1min"`` 时：市场闸（只有 A 股）与覆盖/末端校验都在 launcher 里
    按**分钟可用连续块**重算——日线 QuantDB 覆盖（2016→2026）与 1min 断层不是
    一回事，这里不读 QuantDB；分钟默认窗 = 可用连续块，因此返回的挖掘区间
    一定非空（任务行/子进程 env 拿到的都是显式日期）。
    """
    freq_value = normalize_freq(freq, market=market)
    is_minute = freq_value == FREQ_MINUTE
    has_window = any(
        str(v or "").strip() for v in (mining_start, mining_end, valid_start, valid_end)
    )
    has_datasets = bool(datasets and [d for d in datasets if str(d or "").strip()])
    if (has_window or has_datasets) and market != "a_share":
        raise ValueError(
            "自定义挖掘区间与数据集目前仅支持 A 股（a_share）市场："
            f"{market} 的数据面不走 QuantDB，暂无对应时间窗/数据集口径"
        )
    # 覆盖校验只在真给了窗口时做（默认窗口由 default_windows 锚定覆盖，无需再验）；
    # 末端保护则与覆盖无关——覆盖读不到不等于放弃保护（日历读得到就照退让）。
    coverage = _quantdb_coverage() if (has_window and not is_minute) else None
    data_end = _backtest_guard(coverage) if (has_window and not is_minute) else None
    cfg = normalize_mining_config(
        mining_start,
        mining_end,
        valid_start,
        valid_end,
        datasets,
        freq=freq_value,
        market=market,
        coverage=coverage,
        # 任务行存**退让后**的窗口：任务详情/预览/实跑看到的是同一组日期，
        # 不在存储层留一个"好看但会崩"的端点
        data_end=data_end,
    )
    return (
        cfg.mining_start or None,
        cfg.mining_end or None,
        cfg.valid_start or None,
        cfg.valid_end or None,
        cfg.datasets if has_datasets else None,
        cfg.freq or None,
    )


async def _dispatch_direction_items(
    launcher,
    user_id: str,
    tenant_id: str | None,
    *,
    market: str,
    universe: str,
    loop_n: int,
    picks: list[tuple[str, str | None]],
    direction_mode: str | None,
    data_source: str | None = None,
    quality_gate_mode: str | None = None,
    mining_start: str | None = None,
    mining_end: str | None = None,
    valid_start: str | None = None,
    valid_end: str | None = None,
    datasets: Sequence[str] | None = None,
    freq: str | None = None,
    algorithm: str | None = None,
    overrides: dict | None = None,
) -> tuple[list[dict], int, int, int]:
    """逐条 ``start_or_queue`` 派发的单源派发环（/mining/batch 与 evolve N>1 共用）。

    运行期错误（排队满/硬件锁/意外异常）逐条回传、**绝不拖垮整批**；
    items 顺序 = picks 顺序。返回 ``(items, started, queued, failed)``。

    ``mining_start`` 等 = 已归一的自定义挖掘配置（``_normalize_request_mining_config``
    产物）：校验在请求入口一次做完，派发环只负责原样下发（``freq`` 含在其中，
    分钟任务的窗口是入口物化后的显式日期；``algorithm`` 亦已过 resolve 闸门）。
    """
    items: list[dict] = []
    started = queued = failed = 0
    for idx, (direction, meta_json) in enumerate(picks):
        try:
            receipt = await launcher.start_or_queue(
                user_id,
                market=market,
                universe=universe,
                loop_n=loop_n,
                direction=direction,
                direction_mode=direction_mode,
                direction_meta=meta_json,
                quality_gate_mode=quality_gate_mode,
                data_source=data_source,
                llm_overrides=overrides,
                tenant_id=tenant_id,
                mining_start=mining_start,
                mining_end=mining_end,
                valid_start=valid_start,
                valid_end=valid_end,
                datasets=datasets,
                freq=freq,
                algorithm=algorithm,
            )
        except (QueueFullError, HardwareLockError) as exc:
            items.append(
                {
                    "index": idx,
                    "task_id": None,
                    "status": "failed",
                    "queue_position": None,
                    "direction": direction,
                    "direction_preview": direction[:80],
                    "direction_meta": meta_json,
                    "error": str(exc),
                }
            )
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 —— 单条失败不拖垮整批，错误随条目回传
            logger.warning("[alpha-agent] batch dispatch item %d failed: %s", idx, exc)
            items.append(
                {
                    "index": idx,
                    "task_id": None,
                    "status": "failed",
                    "queue_position": None,
                    "direction": direction,
                    "direction_preview": direction[:80],
                    "direction_meta": meta_json,
                    "error": f"派发失败：{exc}",
                }
            )
            failed += 1
            continue
        if receipt.status == "queued":
            queued += 1
        else:
            started += 1
        items.append(
            {
                "index": idx,
                "task_id": receipt.task_id,
                "status": receipt.status,
                "queue_position": receipt.queue_position,
                # 命中去重（P3）：task_id 是既有任务的——同配置已在跑/已在排，
                # 本次没新建；前端据此提示「复用」而不是「又起了一个」
                "deduplicated": bool(receipt.deduplicated),
                "direction": direction,
                "direction_preview": direction[:80],
                "direction_meta": meta_json,
                "error": None,
            }
        )
    return items, started, queued, failed


@router.post("/evolve")
async def start_evolution(
    request: Request,
    user_id: str | None = Query(
        None, description="已废弃：身份取自 JWT，仅用于防伪校验"
    ),
    market: str = Query(
        "a_share", description="市场: a_share, crypto, hong_kong, us_stock"
    ),
    universe: str = Query(
        "csi300",
        description="股票池: csi300, csi500, csi1000, sse50, gem, star, csi800, all_a",
    ),
    loop_n: int = Query(5, ge=1, le=20, description="演化轮数"),
    direction: str = Query("", description="因子挖掘方向/假设"),
    directions: list[str] = Query(
        default=[], description="L1 因子类别方向列表（多选）"
    ),
    direction_mode: str = Query(
        "selected",
        description=(
            "类别选择模式: selected=取第一条, random=随机一条, "
            "quota=方向配额（探索/利用/变异三档，T-FE-10）"
        ),
    ),
    num_directions: int = Query(
        1, ge=1, le=10, description="并行方向数：类别路径一次派 N 条任务（T-MV-04）"
    ),
    quality_gate_mode: str = Query(
        "",
        description=(
            "入库闸门模式（T-MV-05）: off/soft/hard；留空=未指定"
            "（入池判定叠 ALPHA_GATE_MODE，默认软闸）"
        ),
    ),
    data_source: str = Query(
        "", description="数据源: qlib_bin, parquet, pg (留空使用默认)"
    ),
    mining_start: str = Query(
        "", description="自定义挖掘数据区间起点（YYYY-MM-DD；留空=默认窗口）"
    ),
    mining_end: str = Query("", description="自定义挖掘数据区间终点（YYYY-MM-DD）"),
    valid_start: str = Query(
        "", description="因子检验区间起点（样本外，须 ≥ 挖掘终点；留空=不启用）"
    ),
    valid_end: str = Query("", description="因子检验区间终点"),
    datasets: list[str] | None = Query(
        None, description="挖掘数据集（可重复；留空=全选，见 /mining/config-options）"
    ),
    freq: str = Query(
        "",
        description=(
            "挖掘频率: 留空/day=日线（默认）；"
            "1min=分钟原生挖掘（仅 A 股；窗口自动锚定分钟可用连续块）"
        ),
    ),
    algorithm: str = Query(
        "",
        description=(
            "挖掘算法: 留空=rdagent（默认）；可选值见 GET /algorithms"
            "（statistical=统计量价挖掘，不消费方向文本/数据集/loop_n）"
        ),
    ),
    payload: EvolveRequest | None = Body(
        default=None, description="JSON body 变体（新前端；含 doc_id 血统）"
    ),
):
    """启动因子演化任务"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )

    # JSON body 覆盖 query（老前端 payload=None，行为不变）
    if payload is not None:
        if payload.market:
            market = payload.market
        if payload.universe:
            universe = payload.universe
        if payload.loop_n is not None:
            loop_n = payload.loop_n
        if payload.direction is not None:
            direction = payload.direction
        if payload.directions is not None:
            directions = payload.directions
        if payload.direction_mode:
            direction_mode = payload.direction_mode
        if payload.num_directions is not None:
            num_directions = payload.num_directions
        if payload.quality_gate_mode:
            quality_gate_mode = payload.quality_gate_mode
        if payload.data_source is not None:
            data_source = payload.data_source
        if payload.mining_start:
            mining_start = payload.mining_start
        if payload.mining_end:
            mining_end = payload.mining_end
        if payload.valid_start:
            valid_start = payload.valid_start
        if payload.valid_end:
            valid_end = payload.valid_end
        if payload.datasets is not None:
            datasets = payload.datasets
        if payload.freq is not None:
            freq = payload.freq
        if payload.algorithm:
            algorithm = payload.algorithm
    doc_id = ((payload.doc_id or "").strip() or None) if payload is not None else None

    # 入库闸门模式（T-MV-05）：纯参数校验先做——非法值显式 400（静默当
    # 「未指定」会让用户以为硬闸已开却走了软闸）。''（未指定）原样保留，
    # 生效模式在入池时由 resolve_admission_gate_mode 叠 env 解析——任务行
    # 只存请求意图，不冻结当时的 env。
    try:
        from backend.services.engine.mining_plugins.config import normalize_gate_mode

        gate_mode = normalize_gate_mode(quality_gate_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 自定义挖掘配置（移植 P1）：与闸门模式同一条纪律——纯参数
    # 先算完，非法区间/未登记数据集在烧 token 前显式 400（也绝不静默回默认窗）。
    try:
        (
            mining_start,
            mining_end,
            valid_start,
            valid_end,
            datasets,
            freq,
        ) = _normalize_request_mining_config(
            market=market,
            mining_start=mining_start,
            mining_end=mining_end,
            valid_start=valid_start,
            valid_end=valid_end,
            datasets=datasets,
            freq=freq,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 文档血统闸门 + 归属 + 状态（T-FM-10）：文档链没开的地方不存在
    # 「合法的 doc_id」；他人/未解析完的 doc_id 不许挂任务
    if doc_id:
        require_doc_mining()
        doc_row = await get_doc_store().get_doc(doc_id, user_id=auth_user_id)
        if not doc_row or doc_row.get("status") == "deleted":
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        if doc_row.get("status") not in ("parsed", "organized"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文档尚未解析完成（当前状态 {doc_row.get('status') or '未知'}），"
                    "无法发起挖掘"
                ),
            )

    # Validate market
    try:
        from backend.services.engine.rd_agent.market_adapters import (
            get_adapter,
            list_markets,
        )

        adapter = get_adapter(market)
    except ValueError as e:
        available = [m["market_id"] for m in list_markets()]
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market: {market}. Available: {available}",
        ) from e

    # Validate universe（内置指数 + 全局自定义股票池）
    if market == "a_share" and not _universe_is_valid(universe):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown universe: {universe}. "
                f"Available builtins: {_VALID_CN_UNIVERSES}, "
                "or any active global/custom pool code from /stock-pools/options"
            ),
        )

    # 挖掘算法闸门（移植 P2）：在类别/方向解析与 LLM 解析之前定下算法——
    # 非法组合（未知/停用/不支持该市场或频率/不消费数据集选择）在烧任何成本
    # 之前显式 400；同时决定后续走不走 LLM 链路（统计派不该被 412 拦）。
    try:
        algo = resolve_algorithm(
            algorithm, market=market, freq=freq, datasets=list(datasets or [])
        )
    except AlgorithmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 类别方向下发：前端传多选类别 + 模式，服务端解析成方向
    # （放在长度闸与 LLM 解析之前：纯函数先算完，超长在烧 token 前就被拒）。
    # 方向历史（T-MV-02）：只有类别选择真正参与时才记录生效模式；自由文本/
    # 卡片派发路径保持 NULL——mode 列是「方向怎么来的」的事实，不是参数回声。
    # random 不是均匀随机（T-MV-03）：按空白度（该方向在本用户×本市场的挖掘史
    # 次数）加权抽样，seed/候选/权重/命中落 direction_meta 供复现。
    # 并行方向数（T-MV-04）：N>1 且类别路径且非文档血统 → 一次派 N 条任务
    # （selected=按序取前 N；random=空白度加权抽 N 条互不相同）。自由文本/
    # 文档血统路径 N 不适用——单方向是它们的事实（文档 task_id 反写单列）。
    clean_dirs = [d.strip() for d in directions if isinstance(d, str) and d.strip()]

    # 方向文本闸（P2）：不消费提示词的算法（uses_llm=false）收下方向就是**沉默
    # 丢弃**——用户以为「按我写的研究方向挖」，实际跑的是固定候选目录。显式
    # 400 要求清空输入或换算法（与 datasets 闸同一纪律：收下再忽略比报错更坏）。
    if not algo.uses_llm and ((direction or "").strip() or clean_dirs):
        raise HTTPException(
            status_code=400,
            detail=(
                f"挖掘算法 {algo.key}（{algo.name}）不消费方向文本："
                "它按固定候选目录逐日检验，与研究方向无关。"
                "请清空方向输入后重试，或改用其他算法。"
            ),
        )

    multi_dispatch = bool(clean_dirs) and num_directions > 1 and doc_id is None
    record_mode: str | None = None
    direction_meta_json: str | None = None
    picked_pairs: list[tuple[str, str | None]] = []
    if multi_dispatch:
        record_mode = _sampling_record_mode(direction_mode)
        if record_mode == "quota":
            picked_pairs = await _resolve_quota_picks(
                clean_dirs,
                num_directions,
                user_id=auth_user_id,
                market=market,
            )
        else:
            picked_pairs = await _resolve_multi_picks(
                clean_dirs,
                num_directions,
                record_mode,
                user_id=auth_user_id,
                market=market,
            )
        # 请求级长度闸：一条超长整包 400（与 /mining/batch 同纪律——不半批派）
        for idx, (picked_dir, _meta) in enumerate(picked_pairs, start=1):
            if len(picked_dir) > MAX_SUBMIT_DIRECTION_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"第 {idx} 条方向过长（{len(picked_dir)} 字，上限 "
                        f"{MAX_SUBMIT_DIRECTION_CHARS} 字），请精简后重试"
                    ),
                )
        logger.info(
            "[alpha-agent] evolve multi-dispatch directions=%d mode=%s n=%d -> %s",
            len(clean_dirs),
            record_mode,
            len(picked_pairs),
            [d for d, _ in picked_pairs],
        )
    elif clean_dirs:
        record_mode = _sampling_record_mode(direction_mode)
        if record_mode == "quota":
            _quota_pairs = await _resolve_quota_picks(
                clean_dirs, 1, user_id=auth_user_id, market=market
            )
            direction, direction_meta_json = _quota_pairs[0]
        elif record_mode == "random":
            try:
                direction, _sampling_meta = await sample_weighted_direction(
                    clean_dirs, user_id=auth_user_id, market=market
                )
                direction_meta_json = json.dumps(_sampling_meta, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001 - 抽样证据不许拦任务创建
                logger.warning(
                    "[alpha-agent] weighted sampling failed, plain choice: %s", e
                )
                import random as _random

                direction = _random.choice(clean_dirs)
        else:
            direction = clean_dirs[0]
        logger.info(
            "[alpha-agent] evolve directions=%d mode=%s -> %s",
            len(clean_dirs),
            record_mode,
            direction,
        )
    elif num_directions > 1:
        # 自由文本路径：N 不适用（并行的是「方向」，不是同一条方向的副本）。
        # 显式记一笔，免得用户按设置页 N>1 却只见 1 个任务时无从排查。
        logger.info(
            "[alpha-agent] evolve num_directions=%d ignored (free-text path)",
            num_directions,
        )

    # 提交前长度闸：超长显式拒绝（task_store 的 20k 存储兜底是静默截断，
    # 不该让用户的编辑止步于「怎么少了半段」）
    if not multi_dispatch and len(direction) > MAX_SUBMIT_DIRECTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"挖掘方向过长（{len(direction)} 字，上限 {MAX_SUBMIT_DIRECTION_CHARS} 字），"
                "请精简后重试"
            ),
        )

    # LLM 配置只对消费提示词的算法解析（P2）：统计派不需要 Key——拿 412 拦它
    # 是错的（用户没配 Key 也能跑纯统计挖掘）。overrides 只在这条分支产出，
    # 不消费 LLM 的算法不注入任何用户级密钥。
    llm_config = None
    llm_source = "not_required"
    overrides: dict | None = None
    if algo.uses_llm:
        llm_config, llm_source, embedding_env = await _resolve_effective_llm_config(
            auth_user_id, auth_tenant_id
        )
        if llm_config is None:
            raise HTTPException(
                status_code=412,
                detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
                "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
            )
        logger.info(
            "[alpha-agent] evolve llm source=%s model=%s embedding=%s",
            llm_source,
            llm_config.model,
            "user_profile" if embedding_env else "container",
        )
        overrides = build_subprocess_overrides(llm_config, embedding_env)
    else:
        logger.info("[alpha-agent] evolve algorithm=%s（不消费 LLM）", algo.key)

    launcher = get_launcher()
    if multi_dispatch:
        # 并行方向数（T-MV-04）：批量语义——逐条 start_or_queue，满额排队而
        # 非 429（N 条是一次提交的组成部分，整批背压会在半途留下残缺的方向集）；
        # 运行期错误逐条回传，绝不拖垮整批（与 /mining/batch 同一派发环）。
        items, started, queued, failed = await _dispatch_direction_items(
            launcher,
            auth_user_id,
            auth_tenant_id,
            market=market,
            universe=universe,
            loop_n=loop_n,
            picks=picked_pairs,
            direction_mode=record_mode,
            data_source=data_source or None,
            quality_gate_mode=gate_mode or None,
            mining_start=mining_start,
            mining_end=mining_end,
            valid_start=valid_start,
            valid_end=valid_end,
            datasets=datasets,
            freq=freq,
            algorithm=algo.key,
            overrides=overrides,
        )
        first_task_id = next((it["task_id"] for it in items if it["task_id"]), None)
        logger.info(
            "[alpha-agent] evolve multi-dispatch started=%d queued=%d failed=%d",
            started,
            queued,
            failed,
        )
        return {
            "code": 200,
            "data": {
                "task_id": first_task_id,
                "items": items,
                "started": started,
                "queued": queued,
                "failed": failed,
                "direction_mode": record_mode,
                "algorithm": algo.key,
                "market": market,
                "universe": universe,
                "market_name": adapter.market_name,
                "source": "text",
                "doc_id": None,
                "message": (
                    f"{adapter.market_name} 已派发 {started + queued} 条方向任务"
                    f"（启动 {started} / 排队 {queued} / 失败 {failed}）"
                ),
            },
        }

    # 并发上限：每个任务是 RD-Agent 子进程（烧 LLM token + Qlib 回测），
    # 必须限流防止 fork 风暴。读取点收敛到 launcher（与排队判断同一份口径，
    # 坏值回落默认而非 ValueError 炸路由）。本端点保持 429 背压——满了立即
    # 拒（不排队），批量派发路径走 start_or_queue。
    # 上限可能被可用内存自动收敛（内存吃紧 → 1 路串行）：文案要说清是
    # 「机器内存不够」而不是「你配的上限就这么低」，否则用户会去改配置找答案。
    counts = launcher.count_running()
    max_per_user, max_global = launcher.running_capacity()
    clamp_reason = capacity_clamp_reason()
    clamp_hint = f"（{clamp_reason}）" if clamp_reason else ""
    user_running = counts["by_user"].get(auth_user_id, 0)
    if user_running >= max_per_user:
        raise HTTPException(
            status_code=429,
            detail=(
                f"您已有 {user_running} 个挖掘任务在运行（上限 {max_per_user}）{clamp_hint}，"
                "请等待完成或先取消任务。"
            ),
        )
    if counts["global"] >= max_global:
        raise HTTPException(
            status_code=429,
            detail=(
                f"当前全平台挖掘任务数已达上限（{max_global}）{clamp_hint}，请稍后再试。"
            ),
        )
    try:
        task_id = await launcher.start_evolution(
            auth_user_id,
            market=market,
            universe=universe,
            loop_n=loop_n,
            direction=direction or None,
            direction_mode=record_mode,
            direction_meta=direction_meta_json,
            quality_gate_mode=gate_mode or None,
            data_source=data_source or None,
            mining_start=mining_start,
            mining_end=mining_end,
            valid_start=valid_start,
            valid_end=valid_end,
            datasets=datasets,
            freq=freq,
            algorithm=algo.key,
            # 文档血统：落 rd_agent_mining_tasks.source/doc_id（历史页可见出处）
            source="doc" if doc_id else "text",
            doc_id=doc_id,
            # embedding 独立于 chat 的来源：chat 走容器 env 时，用户在个人中心
            # 配的向量检索同样要下发（否则配置页显示「已保存」，挖掘却按容器
            # 默认供应商检索）。两组变量同源时值相同，覆盖幂等。
            # 不消费 LLM 的算法 → overrides=None（不注入任何用户密钥）。
            llm_overrides=overrides,
        )
    except HardwareLockError as exc:
        raise HTTPException(status_code=412, detail=str(exc)) from exc
    except AlgorithmError as exc:
        # launcher 二次 resolve 与上面的闸门同一份 manifest；此处兜底并发改动
        # （manifest 在两次读取之间被改坏）——仍按请求错误回 400，不 500。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 血统回写（T-FM-10）：doc → task 的反向指针，供文档列表显示「已挖掘」。
    # 回写失败只告警——它是审计面，不是主链；任务已经起来了。
    if doc_id:
        try:
            await get_doc_store().update_doc(doc_id, task_id=task_id)
        except Exception:  # noqa: BLE001
            logger.warning("[alpha-agent] doc %s task_id 回写失败", doc_id)

    return {
        "code": 200,
        "data": {
            "task_id": task_id,
            "algorithm": algo.key,
            "market": market,
            "universe": universe,
            "market_name": adapter.market_name,
            "status": "pending",
            "source": "doc" if doc_id else "text",
            "doc_id": doc_id,
            "message": f"{adapter.market_name} 因子挖掘任务已启动（{algo.name}）",
        },
    }


@router.post("/directions/decompose")
async def decompose_directions(request: Request, payload: DecomposeRequest):
    """把一个粗挖掘方向拆成多张正交子假设卡片（只拆解，不启动任何任务）。

    挖掘是钱（LLM token + Qlib 回测 + 子进程名额）：先拆明白，再批量派发
    （派发走 POST /mining/batch）。本端点不落任何任务行——拆解失败没有
    半张卡片会开跑。LLM 取值链与 evolve 完全同一条（无配置 412）。
    """
    from backend.services.engine.alpha_agent.direction_decompose import (
        DecomposeError,
        decompose_direction,
    )

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)

    # 市场有效性：拆解本身不吃市场，但因子池摘要按市场取——假市场会静默空注入
    try:
        from backend.services.engine.rd_agent.market_adapters import (
            get_adapter,
            list_markets,
        )

        get_adapter(payload.market)
    except ValueError as e:
        available = [m["market_id"] for m in list_markets()]
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market: {payload.market}. Available: {available}",
        ) from e

    llm_config, llm_source, _embedding_env = await _resolve_effective_llm_config(
        auth_user_id, auth_tenant_id
    )
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
            "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
        )

    try:
        result = await decompose_direction(
            direction=(payload.direction or "").strip(),
            user_id=auth_user_id,
            llm_config=llm_config,
            market=payload.market,
            universe=payload.universe,
            max_cards=payload.max_cards,
            seed_factor_ids=payload.seed_factor_ids,
        )
    except DecomposeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "[alpha-agent] decompose source=%s model=%s cards=%d dropped=%d seeds=%d/%d",
        llm_source,
        llm_config.model,
        len(result["cards"]),
        result["dropped"],
        (result.get("context", {}).get("seeds") or {}).get("used", 0),
        (result.get("context", {}).get("seeds") or {}).get("requested", 0),
    )
    return {"code": 200, "data": result}


@router.post("/mining/batch")
async def dispatch_mining_batch(request: Request, payload: MiningBatchRequest):
    """批量派发挖掘任务：逐条 :meth:`start_or_queue`（有名额即启动，满则排队）。

    与 evolve 的 429 背压不同，这里**不拒正常提交**——只有排队深度上限
    （``ALPHA_AGENT_MAX_QUEUED_*``）才让该条目失败。请求级问题（空列表/
    超条数/空方向/超长/未知市场）整包 400：一条都没派，不存在半批派出去；
    运行期错误（排队满/硬件锁/意外异常）逐条回传，绝不拖垮整批。
    """
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)

    directions = [(d or "").strip() for d in payload.directions]
    if len(directions) > MAX_BATCH_DISPATCH_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多派发 {MAX_BATCH_DISPATCH_ITEMS} 条方向（当前 {len(directions)} 条），请分批提交",
        )
    for idx, direction in enumerate(directions, start=1):
        if not direction:
            raise HTTPException(
                status_code=400, detail=f"第 {idx} 条方向为空，请删除或补全"
            )
        if len(direction) > MAX_SUBMIT_DIRECTION_CHARS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第 {idx} 条方向过长（{len(direction)} 字，上限 {MAX_SUBMIT_DIRECTION_CHARS} 字），"
                    "请精简后重试"
                ),
            )

    # 入库闸门模式（T-MV-05）：与 evolve 同一条校验（非法显式 400，不静默降级）
    try:
        from backend.services.engine.mining_plugins.config import normalize_gate_mode

        gate_mode = normalize_gate_mode(payload.quality_gate_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 自定义挖掘配置（移植 P1）：与 evolve 同一处归一/覆盖校验——
    # 卡片批量派发和单条提交必须是同一份窗口语义（整包 400，不半批派）。
    try:
        (
            mining_start,
            mining_end,
            valid_start,
            valid_end,
            datasets,
            freq,
        ) = _normalize_request_mining_config(
            market=payload.market,
            mining_start=payload.mining_start,
            mining_end=payload.mining_end,
            valid_start=payload.valid_start,
            valid_end=payload.valid_end,
            datasets=payload.datasets,
            freq=payload.freq,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        from backend.services.engine.rd_agent.market_adapters import (
            get_adapter,
            list_markets,
        )

        adapter = get_adapter(payload.market)
    except ValueError as e:
        available = [m["market_id"] for m in list_markets()]
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market: {payload.market}. Available: {available}",
        ) from e
    if payload.market == "a_share" and not _universe_is_valid(payload.universe):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown universe: {payload.universe}. "
                f"Available builtins: {_VALID_CN_UNIVERSES}, "
                "or any active global/custom pool code from /stock-pools/options"
            ),
        )

    # 挖掘算法闸门（P2）：与 evolve 同一处 resolve、同一套 400 口径
    try:
        algo = resolve_algorithm(
            payload.algorithm,
            market=payload.market,
            freq=freq,
            datasets=list(datasets or []),
        )
    except AlgorithmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 方向文本闸（P2）：批量契约里每条方向必填（上面已校验非空）——统计派这种
    # 不消费方向的算法整批 400（不半批派、也不是把 20 条方向都静默丢掉）
    if not algo.uses_llm:
        raise HTTPException(
            status_code=400,
            detail=(
                f"挖掘算法 {algo.key}（{algo.name}）不消费方向文本，"
                "无法用于按方向批量派发；请改用其他算法。"
            ),
        )

    llm_config = None
    llm_source = "not_required"
    overrides: dict | None = None
    if algo.uses_llm:
        llm_config, llm_source, embedding_env = await _resolve_effective_llm_config(
            auth_user_id, auth_tenant_id
        )
        if llm_config is None:
            raise HTTPException(
                status_code=412,
                detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
                "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
            )
        overrides = build_subprocess_overrides(llm_config, embedding_env)

    launcher = get_launcher()
    loop_n = payload.loop_n or 5
    # 派发环与 evolve 并行方向数共用单源实现（direction_mode/direction_meta 仅
    # evolve 的类别路径会传——卡片路径 direction 是唯一载体，模式/证据均为空）
    items, started, queued, failed = await _dispatch_direction_items(
        launcher,
        auth_user_id,
        auth_tenant_id,
        market=payload.market,
        universe=payload.universe,
        loop_n=loop_n,
        picks=[(d, None) for d in directions],
        direction_mode=None,
        quality_gate_mode=gate_mode or None,
        mining_start=mining_start,
        mining_end=mining_end,
        valid_start=valid_start,
        valid_end=valid_end,
        datasets=datasets,
        freq=freq,
        algorithm=algo.key,
        overrides=overrides,
    )

    logger.info(
        "[alpha-agent] batch dispatch source=%s model=%s algorithm=%s n=%d started=%d queued=%d failed=%d",
        llm_source,
        getattr(llm_config, "model", "-"),
        algo.key,
        len(directions),
        started,
        queued,
        failed,
    )
    return {
        "code": 200,
        "data": {
            "items": items,
            "started": started,
            "queued": queued,
            "failed": failed,
            "algorithm": algo.key,
            "market": payload.market,
            "universe": payload.universe,
            "market_name": adapter.market_name,
        },
    }


@router.get("/tasks/history")
async def mining_task_history(
    request: Request,
    user_id: str | None = Query(
        None, description="已废弃：身份取自 JWT，仅用于防伪校验"
    ),
    market: str | None = Query(None, description="按市场过滤"),
    status: str | None = Query(
        None, description="按状态过滤: pending/running/completed/failed/cancelled"
    ),
    limit: int = Query(50, ge=1, le=200, description="分页大小"),
    offset: int = Query(0, ge=0, description="偏移"),
):
    """挖掘历史（PG 权威，重启不失忆）。

    与内存版 ``GET /tasks`` 的分工：``/tasks`` 服务运行中监控（带 timeline /
    token 明细，进程重启即失忆）；这里是历史页数据源——direction / 状态 /
    因子数全部来自 ``rd_agent_mining_tasks``。

    路由顺序纪律：本端点必须注册在 ``/tasks/{task_id}`` **之前**（FastAPI 按
    注册顺序匹配，否则 history 会被当作 task_id 吞掉）。
    """
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    try:
        store = get_mining_task_store()
        tasks = await store.list_history(
            user_id=auth_user_id,
            market=market,
            status=status,
            limit=limit,
            offset=offset,
        )
        # total 是过滤后的全量行数（分页器用），不是本页行数
        total = await store.count_history(
            user_id=auth_user_id, market=market, status=status
        )
    except ValueError as exc:
        # 未知状态是客户端错误，不是 500，更不是静默空列表
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 200,
        "data": {"tasks": tasks, "total": total, "limit": limit, "offset": offset},
    }


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request):
    """查询演化任务状态（附带该任务已落库的结构化因子，供前端实时展示）"""
    status = await _require_owned_task(task_id, request)
    auth_user_id, _ = get_authenticated_identity(request)
    try:
        # 载荷刻意截 20 条（本端点 2s 轮询、要小）；结果区权威清单走
        # GET /factors?task_id=…&limit=500（「挖到多少显示多少」按此为准）。
        status["factors"] = await persistence.list_factors(
            user_id=auth_user_id,
            task_id=task_id,
            limit=20,
        )
    except Exception:
        logger.exception("[alpha-agent] list factors for task %s failed", task_id)
        status["factors"] = []
    return {"code": 200, "data": status}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """取消演化任务"""
    await _require_owned_task(task_id, request)
    launcher = get_launcher()
    ok = await launcher.cancel_task(task_id)
    if not ok:
        raise HTTPException(
            status_code=400, detail="无法取消任务（可能已完成或不存在）"
        )
    return {"code": 200, "data": {"task_id": task_id, "status": "cancelled"}}


@router.get("/tasks/{task_id}/log")
async def get_task_log(
    task_id: str,
    request: Request,
    tail: int = Query(500, ge=1, le=5000, description="返回行数"),
    offset: int = Query(0, ge=0, description="从第N行开始返回（0-based）"),
):
    """获取任务的详细子进程日志（分页读取）"""
    await _require_owned_task(task_id, request)
    launcher = get_launcher()
    log_content = await launcher.get_task_log(task_id, tail=0)
    if log_content is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} log not found")
    all_lines = log_content.splitlines()
    total = len(all_lines)
    # offset-based slicing: return lines[offset:offset+tail]
    end = min(offset + tail, total)
    lines = all_lines[offset:end]
    return {
        "code": 200,
        "data": {
            "task_id": task_id,
            "lines": lines,
            "total": total,
        },
    }


@router.get("/tasks/{task_id}/trajectory")
async def get_task_trajectory(task_id: str, request: Request):
    """任务决策轨迹（T-FM-07，只读归档：五环节节点 + 因子结局 + 低奖励定位）。

    组装于读时（零持久化）。任务行取 PG（重启不失忆；归属收口 = 他人任务
    404，不泄露存在性）——与内存版 ``/tasks/{task_id}`` 的差异即在此：历史
    任务在进程重启后仍可看轨迹。两类 404 detail 可区分：任务不存在 vs 任务
    存在但日志目录缺失（日志被 GC / 宿主机直跑未留档）。运行中任务返回当前
    已落目录（``partial:true``）。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    from backend.services.engine.alpha_agent import trajectory_store
    from backend.services.engine.alpha_agent.task_store import (
        get_mining_task_store,
    )

    task = await get_mining_task_store().get_task(task_id, user_id=auth_user_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    try:
        log_dir = trajectory_store.resolve_task_log_dir(task_id)
    except ValueError as exc:
        # 形状不合法的 task_id 不可能有归属行（上面已 404）——防御纵深，同一文案
        raise HTTPException(
            status_code=404, detail=f"Task {task_id} trajectory not found"
        ) from exc
    if not log_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} trajectory not found (log dir missing)",
        )
    trajectory = await trajectory_store.load_trajectory(task=task, log_dir=log_dir)
    return {"code": 200, "data": trajectory.to_dict()}


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user_id: str | None = Query(
        None, description="已废弃：身份取自 JWT，仅用于防伪校验"
    ),
    market: str | None = Query(None, description="按市场过滤"),
):
    """列出当前用户的演化任务"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    launcher = get_launcher()
    tasks = await launcher.list_tasks(user_id=auth_user_id)
    if market:
        tasks = [t for t in tasks if t.get("market") == market]
    return {"code": 200, "data": {"tasks": tasks, "total": len(tasks)}}


async def _decorate_factor_rows(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """因子行附加六维标签 + 生命周期视图（T-FE-06/07 装配层）。

    - ``tags``：``shared/factor_tags.derive_tags``（成熟度取生命周期派生值，
      单一 source of truth）；
    - ``lifecycle``：``shared/factor_lifecycle.lifecycle_view``——盲窗裁剪
      （observing 不返回 ``last_eval``）在视图函数内，「验证中」展示态由
      批量查询 running 验证行供给。
    附加件是**纯增量**字段（原行字段原样保留），前端未消费也无副作用。

    Fail-closed（评审 2026-10-11，HIGH-1 + security-MEDIUM）：

    - 两个装饰查询**各自**降级（run 台账 / 验证表互不拖累），新装部署表未建
      时列表端点不可 500；
    - run 台账失败 = 证据不足：既不得把无成功回测痕迹的行冒充「实验」
      （``lifecycle=None`` 不作声明），也不得放行任何读数（盲窗终点不可
      证实 → 全行脱下 ``last_eval``）；
    - ``metadata_json`` 与 ``lifecycle`` 视图是同源数据的两个出口——观测态
      或降级时两处同裁（``redact_blind_readings``），只裁视图等于没裁。
    """
    from backend.services.engine.factor_backtest import store as fb_store
    from backend.services.engine.mining_plugins import validation_store
    from backend.shared.factor_lifecycle import (
        MATURITY_UNKNOWN,
        STATE_EXPERIMENT,
        STATE_OBSERVING,
        lifecycle_view,
        redact_blind_readings,
    )
    from backend.shared.factor_tags import derive_tags
    from backend.shared.utc_datetime import utc_now

    ids = [str(r.get("factor_id") or "") for r in rows]
    ids = [i for i in ids if i]
    run_ledger_degraded = False
    first_at: dict[str, Any] = {}
    try:
        first_at = await fb_store.first_completed_at(ids)
    except Exception:  # noqa: BLE001 — 装饰层降级，核心列表不可被拖垮
        run_ledger_degraded = True
        logger.warning(
            "因子库 run 台账查询失败（生命周期按保守口径装饰）", exc_info=True
        )
    running: set[str] = set()
    try:
        running = await validation_store.running_factor_ids(ids)
    except Exception:  # noqa: BLE001 — 仅「验证中」展示态降级，不波及其他装饰
        logger.warning("因子库验证查询失败（「验证中」展示态缺省关闭）", exc_info=True)
    now = utc_now()
    decorated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        fid = str(item.get("factor_id") or "")
        lifecycle: dict[str, Any] | None = lifecycle_view(
            item,
            first_completed_run_at=first_at.get(fid),
            now=now,
            has_active_validation=fid in running,
        )
        if (
            run_ledger_degraded
            and lifecycle["state"] == STATE_EXPERIMENT
            and str(item.get("status") or "") != "completed"
        ):
            # 证据不足 + 无行内成功痕迹（跨市场回测流不写 status）→ 不冒充「实验」
            lifecycle = None
        show_readings = (
            not run_ledger_degraded
            and lifecycle is not None
            and lifecycle["state"] != STATE_OBSERVING
        )
        if not show_readings:
            if lifecycle is not None:
                lifecycle = {**lifecycle, "last_eval": None}
            meta = item.get("metadata")
            if isinstance(meta, Mapping):
                item["metadata"] = redact_blind_readings(meta)
        item["lifecycle"] = lifecycle
        item["tags"] = derive_tags(
            item,
            maturity=(lifecycle or {}).get("maturity") or MATURITY_UNKNOWN,
        )
        decorated.append(item)
    return decorated


@router.get("/factors")
async def list_factors(
    request: Request,
    user_id: str | None = Query(
        None, description="已废弃：身份取自 JWT，仅用于防伪校验"
    ),
    market: str | None = Query(None, description="按市场过滤"),
    universe: str | None = Query(None, description="按股票池过滤"),
    status: str | None = Query(
        None, description="按状态过滤: pending/backtesting/completed/failed"
    ),
    task_id: str | None = Query(None, description="只返回该挖掘任务产出的因子"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, description="最新窗口内的分页偏移（翻更早的因子）"),
):
    """列出当前用户已生成的因子（最新窗口 + 全量统计）。

    ``task_id`` 走 ``metadata_json->>'task_id'``；查询本身已按认证用户收口
    （``user_id = auth_user_id``），他人 task_id 天然查空——不需要也不应该
    对 task_id 另做归属校验（多一次查询只会多一个存在性泄露面）。

    ``factors`` 是 ``created_at DESC`` 的最新窗口（limit/offset 分页），
    ``total`` 与 ``quality_counts`` 则覆盖**同一过滤域的全量**——界面统计
    瓦片必须用全量口径：拿窗口长度当总数会制造「越挖、中等因子越少」的
    假象（窗口滑动把老因子挤出可视范围，不是质量真的下降）。阈值与前端
    ``classifyQuality`` 同一口径（金样双端钉死）。
    """
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    factors = await persistence.list_factors(
        user_id=auth_user_id,
        status=status,
        market=market,
        universe=universe,
        task_id=task_id,
        limit=limit,
        offset=offset,
    )
    scope = await persistence.factor_scope_stats(
        user_id=auth_user_id,
        status=status,
        market=market,
        universe=universe,
        task_id=task_id,
    )
    return {
        "code": 200,
        "data": {
            "factors": await _decorate_factor_rows(factors),
            "total": scope["total"],
            "limit": limit,
            "offset": offset,
            "quality_counts": {
                "high": scope["high"],
                "medium": scope["medium"],
                "low": scope["low"],
                "unknown": scope["unknown"],
            },
        },
    }


@router.get("/variant-ledger")
async def get_variant_ledger(
    request: Request,
    universe: str = Query(
        "", max_length=128, description="按股票池过滤冷却（空=全量原样）"
    ),
):
    """变体残差台账（T-FE-09）：32 骨架 ×（最新观测 + 冷却态），只读投影。

    统计派 runner 收尾把子变体对父本的残差观测并入台账、按非对称否决维护
    冷却名单；本端点只读该文件（口径单源 ``statistical/variant_ledger``）。
    台账文件缺席（从未跑过统计派）返回空骨架 ``available=false``，不 404
    ——「没跑过」不是接口错误；``universe`` 给了就过滤冷却并给出该池生效集。

    共享语义（评审 2026-10-12）：台账是**平台级共享事实池**——因子库本身
    共享，冷却名单是池级安全事实（谁跑统计派都要避让），故不按用户过滤；
    投影层已脱敏 ``run_id``（security-F2），身份校验仍必须（401 面）。
    """
    # 只做鉴权（401 面）：返回值不参与过滤——本投影是全平台共享读面
    get_authenticated_identity(request)

    from backend.services.engine.alpha_agent.statistical import variant_ledger as vl

    path = vl.default_ledger_path()
    data = vl.ledger_view(
        vl.load_ledger(path), universe=(universe or "").strip() or None
    )
    data["available"] = path.is_file()
    return {"code": 200, "data": data}


@router.get("/experience-memory")
async def get_experience_memory(
    request: Request,
    market: str = Query(
        "a_share", max_length=32, description="市场词汇（仓库现行口径，默认 a_share）"
    ),
):
    """经验记忆/禁区（T-FM-04）：拒因聚类 + 饱和方向 + 方向任务史，读时组装。

    与提示词注入**同源同一** ``build_memory``（口径单源
    ``alpha_agent/experience_memory``）：数据源全部既有事实（任务史 /
    rd_mined 物化清单 / T-FM-01 统计派报告 / rd_agent_factors 归因），
    纯派生零持久化。口径 = 本用户 × 本市场；任何源缺席如实降级，
    全空 → ``available=false``（不是接口错误，不 404）。
    """
    user_id, _ = get_authenticated_identity(request)

    from backend.services.engine.alpha_agent import experience_memory

    memory = await experience_memory.load_memory(user_id=str(user_id), market=market)
    return {"code": 200, "data": memory.to_dict()}


@router.get("/direction-quota")
async def get_direction_quota(
    request: Request,
    market: str = Query(
        "a_share", max_length=32, description="市场词汇（仓库现行口径，默认 a_share）"
    ),
    directions: list[str] = Query(
        default=[],
        description="候选方向词表（可重复；留空=只出全史打分表，无档位预演）",
    ),
    n: int = Query(
        direction_quota.DEFAULT_PREVIEW_N,
        ge=1,
        le=10,
        description="预演名额数（与 /evolve 的 num_directions 同口径）",
    ),
):
    """方向配额预览（T-FE-10）：代理模型打分表 + 三档名额预演，只读投影。

    与派发侧**同源同一** ``direction_quota``（口径单源）：成功率 =
    经验贝叶斯平滑（Jeffreys 先验）+ 饱和惩罚（T-FM-04 的 90 天重复命中）。
    零持久化；读取失败 → ``available=false`` + 稳定文案（不是接口错误，
    不 5xx）；``n`` 上限与 /evolve 的 num_directions 同闸。
    """
    user_id, _ = get_authenticated_identity(request)

    data = await direction_quota.preview(
        user_id=str(user_id),
        market=market,
        directions=[d.strip() for d in directions if isinstance(d, str) and d.strip()],
        n=n,
    )
    return {"code": 200, "data": data}


class FactorMaterializeRequest(BaseModel):
    factor_ids: list[str]
    force: bool = False


# 用户自助物化的启动确认参数（模块级常量：测试 monkeypatch 缩短用；
# 与 admin 面同值同纪律——回包前必须确认子进程真拿住 flock）。
_MATERIALIZE_CONFIRM_TIMEOUT_S = 15.0
_MATERIALIZE_CONFIRM_POLL_S = 0.25


@router.post("/factors/materialize")
async def post_factor_materialize(request: Request, body: FactorMaterializeRequest):
    """把选中因子手动送入物化（rd_mined 训练数据集），owner 恒为鉴权身份。

    与 admin 面板共用同一把 flock 与同一启动纪律（shared launcher）；只接受
    当前用户名下、带代码、a_share 且未定终态的因子。越权/无码/非 a_share/
    已物化等一律在明细里如实回报，不静默吞（跳过原因与物化器同词表）。
    """
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.scripts.rd_mined_materialize import (
        _eligible_row,
        _lib_root,
        _load_manifest,
        _query_candidates,
        _should_materialize,
        _web_log_path,
        build_run_command,
        probe_run_lock,
        project_root,
    )
    from backend.shared.rd_mined_materialize_launch import (
        START_GUARD,
        MaterializeSpawnError,
        normalize_factor_ids,
        spawn_materialize,
    )

    try:
        ids = normalize_factor_ids(body.factor_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await _query_candidates(factor_ids=ids, ensure=False)
    by_id = {str(row.get("factor_id")): row for row in rows}
    manifest = _load_manifest(_lib_root())

    rejected: list[dict[str, str]] = []
    skipped: dict[str, str] = {}
    materializable: list[str] = []
    for factor_id in ids:
        row = by_id.get(factor_id)
        owner = row.get("user_id") if row else None
        # 归属过滤先于一切：他人 / 历史无主（user_id IS NULL，只读）一律按
        # 「不存在」处理（与 _require_owned_factor(for_write) 同语义，不泄露存在性）。
        if row is None or owner != auth_user_id:
            rejected.append({"factor_id": factor_id, "reason": "not_found"})
            continue
        ok, reason = _eligible_row(row)
        if not ok:
            skipped[factor_id] = reason
            continue
        ok, reason = _should_materialize(row, manifest, force=body.force)
        if not ok:
            skipped[factor_id] = reason
            continue
        materializable.append(factor_id)

    if len(rejected) == len(ids):
        # 全是他人的/不存在的 id：与单因子端点同一口径 404（不逐个回显原因）
        raise HTTPException(status_code=404, detail="Factor not found")

    if not materializable:
        return {
            "code": 200,
            "data": {
                "started": False,
                "running": probe_run_lock(),
                "requested": len(ids),
                "materializable": [],
                "skipped": skipped,
                "rejected": rejected,
                "message": "没有可物化的因子（原因见明细）",
            },
        }

    if probe_run_lock():
        raise HTTPException(
            status_code=409,
            detail="已有物化进程在运行，本次未启动（物化独占同一座库，避免并发写坏分区）",
        )
    if not START_GUARD.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="上一次启动确认尚未完成，请稍后重试"
        )
    try:
        try:
            result = await spawn_materialize(
                command=build_run_command(materializable),
                log_path=_web_log_path(),
                cwd=project_root(),
                probe=probe_run_lock,
                log_holder=logger,
                confirm_timeout_s=_MATERIALIZE_CONFIRM_TIMEOUT_S,
                confirm_poll_s=_MATERIALIZE_CONFIRM_POLL_S,
            )
        except MaterializeSpawnError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        START_GUARD.release()

    return {
        "code": 200,
        "data": {
            "started": True,
            "running": bool(result["confirmed"]),
            "confirmed": bool(result["confirmed"]),
            "pid": result["pid"],
            "log_path": result["log_path"],
            "requested": len(ids),
            "materializable": materializable,
            "skipped": skipped,
            "rejected": rejected,
            "message": (
                f"物化已确认在后台运行：{len(materializable)} 个因子；"
                "完成后自动刷新字段注册与训练目录"
                if result["confirmed"]
                else "物化进程已启动但暂未确认持锁，请稍后刷新状态"
            ),
        },
    }


@router.post("/factors/materialize-pending")
async def post_factor_materialize_pending(request: Request):
    """把当前用户名下**所有待物化**的挖掘因子一次性送进 rd_mined。

    与 ``POST /factors/materialize`` 同一把 flock / 同一启动纪律；区别只是
    factor_ids 由服务端按「当前用户 + 可物化 + 未定终态」筛出来，省去前端逐条
    勾选。``factor_ids`` 端点仍保留（精确重跑/force 用）。

    这是「每个人不同的物化程序」的入口：每个因子执行的是挖掘时生成的
    ``factor_code``，不是共享脚本；只接受有代码、a_share、未物化的因子。
    """
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.scripts.rd_mined_materialize import (
        _eligible_row,
        _lib_root,
        _load_manifest,
        _query_candidates,
        _should_materialize,
        _web_log_path,
        build_run_command,
        probe_run_lock,
        project_root,
    )
    from backend.shared.rd_mined_materialize_launch import (
        START_GUARD,
        MaterializeSpawnError,
        spawn_materialize,
    )

    rows = await _query_candidates(ensure=False)
    owned = [row for row in rows if str(row.get("user_id") or "") == auth_user_id]
    manifest = _load_manifest(_lib_root())
    skipped: dict[str, str] = {}
    eligible: list[str] = []
    for row in owned:
        factor_id = str(row.get("factor_id") or "")
        if not factor_id:
            continue
        ok, reason = _eligible_row(row)
        if not ok:
            skipped[factor_id] = reason
            continue
        ok, reason = _should_materialize(row, manifest, force=False)
        if not ok:
            skipped[factor_id] = reason
            continue
        eligible.append(factor_id)

    materializable = eligible[:100]
    remaining = max(0, len(eligible) - len(materializable))
    if not materializable:
        return {
            "code": 200,
            "data": {
                "started": False,
                "running": probe_run_lock(),
                "requested": len(owned),
                "materializable": [],
                "skipped": skipped,
                "remaining": 0,
                "message": "没有可物化的因子（未挖掘/无代码/非 a_share/已物化）",
            },
        }
    if probe_run_lock():
        raise HTTPException(
            status_code=409,
            detail="已有物化进程在运行，本次未启动（物化独占同一座库，避免并发写坏分区）",
        )
    if not START_GUARD.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="上一次启动确认尚未完成，请稍后重试"
        )
    try:
        try:
            result = await spawn_materialize(
                command=build_run_command(materializable),
                log_path=_web_log_path(),
                cwd=project_root(),
                probe=probe_run_lock,
                log_holder=logger,
                confirm_timeout_s=_MATERIALIZE_CONFIRM_TIMEOUT_S,
                confirm_poll_s=_MATERIALIZE_CONFIRM_POLL_S,
            )
        except MaterializeSpawnError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        START_GUARD.release()

    return {
        "code": 200,
        "data": {
            "started": True,
            "running": bool(result["confirmed"]),
            "confirmed": bool(result["confirmed"]),
            "pid": result["pid"],
            "log_path": result["log_path"],
            "requested": len(owned),
            "materializable": materializable,
            "skipped": skipped,
            "remaining": remaining,
            "message": (
                f"物化已确认在后台运行：{len(materializable)} 个因子"
                + (f"（另有 {remaining} 个待下一轮）" if remaining else "")
                + "；完成后自动刷新字段注册与训练目录"
                if result["confirmed"]
                else "物化进程已启动但暂未确认持锁，请稍后刷新状态"
            ),
        },
    }


@router.get("/factors/materialize/status")
async def get_factor_materialize_status(
    request: Request,
    factor_ids: str = Query("", description="逗号分隔的因子 ID（最多 100 个）"),
):
    """选中因子的物化状态（manifest 面）。

    只回当前用户名下的条目（越权/不存在的 id 直接缺席）。刻意**不回日志尾**：
    web 日志可能夹带其他用户或管理员运行的因子名，对普通用户回吐即跨租户
    泄露；probe_run_lock + manifest 足够支撑界面（日志尾保持 admin 专属）。
    """
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.scripts.rd_mined_materialize import (
        _lib_root,
        _load_manifest,
        _query_candidates,
        probe_run_lock,
    )
    from backend.shared.rd_mined_materialize_launch import normalize_factor_ids

    raw = [part for part in factor_ids.split(",") if part.strip()]
    try:
        ids = normalize_factor_ids(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await _query_candidates(factor_ids=ids, ensure=False)
    owned = {
        str(row.get("factor_id")) for row in rows if row.get("user_id") == auth_user_id
    }
    manifest = _load_manifest(_lib_root())

    factors: list[dict[str, object]] = []
    last_at: str | None = None
    for factor_id in ids:
        if factor_id not in owned:
            continue
        entry = manifest.get(factor_id) or {}
        at = entry.get("at")
        factors.append(
            {
                "factor_id": factor_id,
                "status": str(entry.get("status") or "none"),
                "at": at if isinstance(at, str) else None,
            }
        )
        if isinstance(at, str) and (last_at is None or at > last_at):
            last_at = at
    return {
        "code": 200,
        "data": {
            "running": probe_run_lock(),
            "factors": factors,
            "last_at": last_at,
        },
    }


@router.post("/factors/recovery")
async def start_factor_recovery(
    request: Request,
    limit: int = Query(200, ge=1, le=500, description="单批最多处理的因子数"),
):
    """对「待评估」（无 IC）因子批量补码并评估。

    旧批次半成品没有实现代码（factor_code 为空），行内「回测」会直接 400；
    本端点按公式+描述用 LLM 补码（写回 factor_code，metadata 标注
    ``code_recovered``），随后逐个跑标准回测补 IC。后台串行执行，进度用
    ``GET /factors/recovery/status`` 轮询；失败条目保留原因、可重发（选择
    口径 ic 为空即可重入）。无 LLM 配置时 412（与 explain/evolve 同一提示）。
    """
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    if _recovery_state["running"]:
        return {
            "code": 200,
            "data": {
                **_recovery_snapshot(auth_user_id),
                "message": "补码评估批次已在进行中",
            },
        }

    # 候选清单与 LLM 配置都是无副作用的读，先做完再认领——认领那一刻
    # （下方二次检查 → 置位）之间无让出点，两个并发提交只有一个能起批次。
    factors = await persistence.list_factors_needing_recovery(auth_user_id, limit=limit)
    if not factors:
        return {
            "code": 200,
            "data": {
                **_recovery_snapshot(auth_user_id),
                "total": 0,
                "message": "没有待评估的因子",
            },
        }
    llm_config, _, _ = await _resolve_effective_llm_config(auth_user_id, auth_tenant_id)
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置。",
        )

    if _recovery_state["running"]:
        return {
            "code": 200,
            "data": {
                **_recovery_snapshot(auth_user_id),
                "message": "补码评估批次已在进行中",
            },
        }

    from backend.shared.utc_datetime import utc_now

    _recovery_state.update(
        {
            "running": True,
            "user_id": auth_user_id,
            "total": len(factors),
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "current_factor_id": None,
            "current_factor_name": None,
            "message": None,
            "started_at": utc_now().isoformat(),
            "finished_at": None,
        }
    )
    logger.info(
        "[factor-recovery] 批次启动 user=%s 待处理=%s（无码=%s）",
        auth_user_id,
        len(factors),
        sum(1 for f in factors if not (f.get("factor_code") or "").strip()),
    )
    _spawn_recovery_batch(_run_factor_recovery(factors, llm_config, auth_user_id))
    return {"code": 200, "data": _recovery_snapshot(auth_user_id)}


@router.get("/factors/recovery/status")
async def get_factor_recovery_status(request: Request):
    """补码评估批次进度（发起人可见当前因子名；其他登录用户只见计数）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    return {"code": 200, "data": _recovery_snapshot(auth_user_id)}


@router.get("/factors/{factor_id}")
async def get_factor(factor_id: str, request: Request):
    """获取单个因子详情（附六维标签与生命周期视图，T-FE-06/07）"""
    factor = await _require_owned_factor(factor_id, request)
    return {"code": 200, "data": (await _decorate_factor_rows([factor]))[0]}


@router.get("/metrics/registry")
async def get_metrics_registry(request: Request):
    """指标描述符注册表（前端展示契约）。

    静态词汇表（label/unit/better/precision/description），前端
    ``services-v2/metricRegistry.ts`` 拉取后与本地默认表合并；本仓金样
    ``backend/tests/fixtures/miningMetricsGolden.json`` 的 registry 段为准。
    ``gates`` 段为门禁描述符（物化门禁徽标/配置页用），与 metrics 同源枚举。
    """
    get_authenticated_identity(request)  # 复用统一鉴权（与 /factors 一致）

    from backend.services.engine.mining_plugins import (
        list_descriptors,
        list_gate_descriptors,
    )

    return {
        "code": 200,
        "data": {
            "version": 1,
            "metrics": list_descriptors(),
            "gates": list_gate_descriptors(),
        },
    }


def _resolve_factory_manifest() -> Path:
    """因子工厂 MANIFEST.csv 路径（quantcustom 用户自定义数据集）。"""
    root = os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom"
    return Path(root) / "6_ml_datasets" / "l1_factors" / "MANIFEST.csv"


def _to_float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if f == f else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


@router.get("/factory-factors")
async def list_factory_factors(request: Request):
    """列出因子工厂产出的表达式因子（只读，来自 quantcustom MANIFEST.csv）。

    工厂因子是共享的批量产出（不属某个用户），只展示、不提供回测/训练操作。
    """
    import csv

    get_authenticated_identity(request)  # 复用统一鉴权（与 /factors 一致）

    manifest = _resolve_factory_manifest()
    if not manifest.is_file():
        return {"code": 200, "data": {"factors": [], "total": 0, "generated_at": None}}

    factors: list[dict] = []
    with manifest.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("factor_name") or "").strip()
            if not name:
                continue
            expr = (row.get("expression") or "").strip()
            factors.append(
                {
                    "factor_id": f"factory:{name}",
                    "factor_name": name,
                    "factor_expression": expr,
                    "factor_formulation": expr,
                    "factor_code": "",
                    "ic_value": _to_float(row.get("ic")),
                    "icir": _to_float(row.get("icir")),
                    "coverage": _to_float(row.get("coverage")),
                    "rank_ic": None,
                    "status": "completed",
                    "market": "a_share",
                    "universe": "all_a",
                    "source": "factor_factory",
                    "read_only": True,
                    "metadata": {
                        "source": "factor_factory",
                        "read_only": True,
                        "field": row.get("field") or "",
                        "icir": _to_float(row.get("icir")),
                        "coverage": _to_float(row.get("coverage")),
                    },
                }
            )
    factors.sort(key=lambda x: abs(x.get("ic_value") or 0.0), reverse=True)
    return {
        "code": 200,
        "data": {
            "factors": factors,
            "total": len(factors),
            "generated_at": datetime.fromtimestamp(
                manifest.stat().st_mtime
            ).isoformat(),
        },
    }


@router.post("/factors/{factor_id}/explain")
async def explain_factor(factor_id: str, request: Request):
    """用 LLM 中文解释因子含义"""
    factor = await _require_owned_factor(factor_id, request, for_write=True)

    # Check if explanation already exists
    metadata = factor.get("metadata") or {}
    if metadata.get("explanation"):
        return {
            "code": 200,
            "data": {
                "explanation": metadata["explanation"],
                "logic_score": metadata.get("logic_score"),
                "cached": True,
            },
        }

    factor_name = factor.get("factor_name", "unknown")
    factor_code = factor.get("factor_code", "")
    factor_formulation = metadata.get("factor_formulation", "")

    from backend.services.engine.alpha_agent.llm_client import chat as llm_chat

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    # 只用 chat 通道解释因子，不落子进程 —— embedding 覆盖在此无意义
    llm_config, _, _ = await _resolve_effective_llm_config(auth_user_id, auth_tenant_id)
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置。",
        )

    prompt = f"""请用中文简洁地解释以下量化因子。输出格式：
1. **含义**：一句话概括
2. **金融直觉**：为什么这个因子可能有效
3. **适用场景**：在什么市场环境下表现较好
4. **预期方向**：因子值高/低时预示什么

因子名称：{factor_name}
因子公式：{factor_formulation or factor_code[:500]}

请直接输出解释，不要重复因子公式。
解释正文写完后，另起一行输出一行机器可读评分（不要解释这行）：
SCORE: 50 到 100 的整数（50-70 逻辑牵强/易过拟合，70-85 逻辑合理，85-100 经济学依据扎实）"""

    try:
        explanation = await llm_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
            timeout=30,
            config=llm_config,
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "LLM explain failed: status=%s body=%s",
            e.response.status_code,
            e.response.text[:300],
        )
        raise HTTPException(
            status_code=502,
            detail=f"LLM 服务返回错误 ({e.response.status_code})，请检查 API Key 配置",
        ) from e
    except Exception as e:
        logger.error("LLM explain failed: %s", e)
        raise HTTPException(status_code=500, detail="LLM 解释失败，请稍后重试") from e

    # 解析金融逻辑评分（LLM 可能不按格式输出 → 评分缺失但不影响解释文本）
    explanation, logic_score = _parse_logic_score(explanation)

    # Store explanation in metadata
    metadata["explanation"] = explanation
    if logic_score is not None:
        metadata["logic_score"] = logic_score
    await persistence.update_factor_metrics(factor_id, metadata=metadata)

    return {
        "code": 200,
        "data": {
            "explanation": explanation,
            "logic_score": logic_score,
            "cached": False,
        },
    }


async def _record_backtest_start(
    factor_id: str,
    factor: dict,
    *,
    market: str,
    universe: str,
    data_source: str,
) -> str | None:
    """回测发起时登记历史台账（一次运行一行，供后续对比）。

    增益层纪律（照 pool_service 先例）：台账写失败只告警，绝不拦回测。

    Returns: run_id（收口的行身份，登记进 ``_running_backtest_runs``；
    登记失败返回 None，收口时跳过——不按 factor 猜行）。
    """
    try:
        return await persistence.start_backtest_run(
            factor_id,
            factor_name=factor.get("factor_name"),
            user_id=factor.get("user_id"),
            market=market,
            universe=universe,
            data_source=data_source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[alpha-backtest] 历史台账登记失败（不拦回测）%s: %s", factor_id, exc
        )
        return None


async def _record_backtest_finish(run_id: str | None, status: str, **kwargs) -> None:
    """回测到达终态时收口历史台账（增益层：异常只告警）。

    按 run_id 精确收口（不是按 factor 找「最新未完结行」）：取消端点与后台任务
    可能先后收口同一行，第二次因行已非 running 而幂等返回 False。
    run_id 为空（发起时登记失败）则无行可收口，直接跳过。
    """
    if not run_id:
        return
    try:
        await persistence.finish_backtest_run(run_id, status, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[alpha-backtest] 历史台账收口失败（不拦回测）%s: %s", run_id, exc
        )


async def _prepare_factor_backtest(
    factor_id: str,
    factor: dict,
    *,
    market: str,
    universe: str,
    data_source: str,
) -> str | None:
    """回测发起前的公共前置：去重占位 → 状态置 backtesting → 台账登记。

    调用方（单因子端点与补码评估批次）须**先自查** ``factor_id not in
    _running_backtests``；本函数入口即 ``add``，check→add 之间不得出现让出点，
    否则双击「回测」两个请求都通过检查、并发跑两个子进程。

    Returns: 台账 run_id（收口身份，透传给 ``_run_factor_backtest``）。
    """
    _running_backtests.add(factor_id)
    try:
        await persistence.update_factor_metrics(factor_id, status="backtesting")
    except Exception:
        _running_backtests.discard(factor_id)
        raise
    # 历史台账：发起即登记 running 行（收口在 _run_factor_backtest 的终态写入点）。
    # run_id 是本进程内收口该行的唯一身份——不能等到收口时再按 factor「找最新」，
    # 取消→立即重跑后旧任务收尾会收错行。
    run_id = await _record_backtest_start(
        factor_id,
        factor,
        market=market,
        universe=universe,
        data_source=data_source,
    )
    _running_backtest_runs[factor_id] = run_id
    return run_id


# ── 补码评估（2026-10-09）─────────────────────────────────────────────
# 「待评估」因子（ic 从未算出）的存量主体来自 2026-09-13 之前的旧挖掘批次：
# 旧提取器把 coding 未完成的半成品也落了库（factor_code 为空串），工作区已
# 清理、代码不可找回，物化/回测/训练三条路都堵。这里按公式+描述用 LLM 补出
# 实现代码（factor_codegen），再自动跑标准回测补 IC——完成后归入高/中/低档，
# 重新可用。批次**进程内单跑**（_recovery_state["running"] 互斥）：45 次 LLM
# 调用 + 45 次回测是重活，重复提交只会互相抢引擎。
#
#: 连续失败熔断阈值：LLM/回测属环境性问题时（断网、Key 失效、Qlib 数据坏），
#: 逐条硬跑到黑只会空烧配额且每条都写一遍同样的错——连败即止，已完成的保留，
#: 未轮到的下次重发（选择口径 ic IS NULL 天然可重入）。
_MAX_CONSECUTIVE_RECOVERY_FAILURES = 3

_recovery_state: dict = {
    "running": False,
    "user_id": None,
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current_factor_id": None,
    "current_factor_name": None,
    "message": None,
    "started_at": None,
    "finished_at": None,
}


def _recovery_snapshot(viewer_id: str | None = None) -> dict:
    """批次状态快照（JSON 键名与前端 FactorRecoveryStatus 对齐）。

    非发起人查看时隐去当前因子身份（跨租户不回吐因子名/ID；计数是纯进度，
    保留以便用户知道「引擎正忙」而不是自己的批次卡住）。
    """
    state = dict(_recovery_state)
    if viewer_id is not None and state.get("user_id") not in (None, viewer_id):
        state["current_factor_id"] = None
        state["current_factor_name"] = None
    state.pop("user_id", None)
    return state


def _spawn_recovery_batch(coro) -> None:
    """后台启动补码评估批次。

    单独一个函数是为测试留挂桩点：TestClient 每个请求走独立 portal loop，
    ``asyncio.create_task`` 的后台任务在响应结束后会被取消——路由级测试
    monkeypatch 本函数直接 await worker（与单测直调 worker 同路径）。
    """
    asyncio.create_task(coro)


@router.post("/factors/{factor_id}/backtest")
async def backtest_factor(
    factor_id: str,
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = Query(
        "csi300",
        description="回测股票池: csi300, csi500, csi1000, sse50, gem, star, csi800, all_a",
    ),
    data_source: str | None = Query(
        "qlib_bin", description="回测数据源: qlib_bin(默认) | h5"
    ),
):
    """对因子发起轻量验证（多市场 + 数据源可选）

    data_source=qlib_bin (默认): 用 Qlib 二进制 (5 个市场均支持)；
    data_source=h5: 走 RD-Agent daily_pv.h5（A股/港股/美股有预生成，期货从 parquet 自动生成，crypto 预生成）。
    """
    factor = await _require_owned_factor(factor_id, request, for_write=True)

    if not factor.get("factor_code"):
        raise HTTPException(status_code=400, detail="因子代码为空，无法回测")

    if factor_id in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": "backtesting",
                "message": "回测已在进行中",
            },
        }

    market = factor.get("market") or "a_share"
    # 先占位再 await：check（上方）→ add 之间不得出现让出点，否则双击「回测」
    # 两个请求都通过检查、并发跑两个子进程（旧实现 add 在 update 之后）。
    run_id = await _prepare_factor_backtest(
        factor_id,
        factor,
        market=market,
        universe=universe or "csi300",
        data_source=data_source or "qlib_bin",
    )

    asyncio.create_task(
        _run_factor_backtest(
            factor_id,
            factor.get("factor_code") or "",
            market=market,
            data_source=data_source or "qlib_bin",
            start_date=start_date,
            end_date=end_date,
            universe=universe or "csi300",
            run_id=run_id,
        )
    )

    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "status": "backtesting",
            "message": f"快速验证已触发: {factor.get('factor_name')} (market={market}, data_source={data_source})",
        },
    }


@router.post("/factors/{factor_id}/cancel")
async def cancel_backtest(factor_id: str, request: Request):
    """取消一个正在进行的回测：kill 子进程（不再等 600s 超时）并标记 cancelled"""
    factor = await _require_owned_factor(factor_id, request)
    if factor_id not in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": factor.get("status"),
                "message": "回测未在运行",
            },
        }
    _backtest_cancelled.add(factor_id)
    proc = _backtest_processes.get(factor_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            for _ in range(10):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.2)
            if proc.poll() is None:
                proc.kill()
                logger.warning(
                    "[alpha-backtest] force-killed subprocess for %s", factor_id
                )
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning("[alpha-backtest] cancel kill %s failed: %s", factor_id, e)
    try:
        await persistence.update_factor_metrics(
            factor_id,
            status="cancelled",
            metadata={"backtest_error": "cancelled_by_user"},
        )
    except Exception:
        pass
    # 历史台账收口本**次**运行（注册表里的 run_id；若后台任务已先收口，
    # 该行已非 running，这里幂等无操作）。
    await _record_backtest_finish(
        _running_backtest_runs.get(factor_id), "cancelled", error="cancelled_by_user"
    )
    # 去重标记不在这里拆：kill 后任务还要走 DB 收尾，若此刻放行重跑，旧任务
    # finally 的清理判断可能仍指着旧 run_id，会把**新任务**的标记一起拆掉
    # （随后可并发双跑同因子）。标记归任务 finally 以身份守卫独占清理——
    # 收尾期间的重复提交得到「回测已在进行中」，等它真正停稳再放行。
    return {"code": 200, "data": {"factor_id": factor_id, "status": "cancelled"}}


@router.get("/factors/{factor_id}/backtests")
async def list_factor_backtests(
    factor_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100, description="最多返回的回测次数"),
):
    """列出该因子的历次回测记录（新→旧，一次运行一行；含指标与配置）。

    供回测页「回测历史」对比：universe / data_source / 窗口 / 全量指标。
    """
    await _require_owned_factor(factor_id, request)
    runs = await persistence.list_backtest_runs(factor_id, limit=limit)
    return {"code": 200, "data": {"factor_id": factor_id, "runs": runs}}


@router.post("/factors/{factor_id}/export")
async def export_factor_to_ide(
    factor_id: str,
    request: Request,
):
    """将因子代码导出到 AI-IDE 工作空间"""
    factor = await _require_owned_factor(factor_id, request)
    user_id, _ = get_authenticated_identity(request)

    factor_code = factor.get("factor_code") or ""
    if not factor_code.strip():
        raise HTTPException(status_code=400, detail="因子代码为空，无法导出")

    factor_name = factor.get("factor_name", "unnamed_factor")
    meta = factor.get("metadata") or {}

    # 质量闸门（软）：PFS 扰动保真度 / LLM 金融逻辑分低于阈值时给出显式警告。
    # 不阻断导出（研究流程需人工判断），但警告随响应与文件头一并交付。
    quality = meta.get("quality") or {}
    pfs_val = quality.get("pfs")
    logic_score = meta.get("logic_score")
    quality_warnings = _quality_warnings(pfs_val, logic_score)
    if quality_warnings:
        logger.warning(
            "[alpha-export] %s quality warnings: %s",
            factor_id,
            "; ".join(quality_warnings),
        )

    # 生成带头部注释的完整 Python 文件
    header_lines = [
        '"""',
        f"Factor: {factor_name}",
        "Source: RD-Agent Alpha Research",
        f"IC: {factor.get('ic_value', 'N/A')}",
        f"RankIC: {meta.get('rank_ic', 'N/A')}",
        f"Sharpe: {factor.get('sharpe_ratio', 'N/A')}",
        f"Market: {meta.get('market', 'a_share')}",
        f"PFS (perturbation fidelity): {f'{float(pfs_val):.3f}' if pfs_val is not None else 'N/A'}",
        f"LogicScore: {logic_score if logic_score is not None else 'N/A'}",
        f"Description: {meta.get('description', '')[:200]}",
        '"""',
        "",
    ]
    full_code = "\n".join(header_lines) + factor_code

    # 保存到策略库
    from backend.shared.strategy_storage import get_strategy_storage_service

    svc = get_strategy_storage_service()
    file_name = f"factor_{factor_name}"
    res = await svc.save(
        user_id=user_id,
        name=file_name,
        code=full_code,
        metadata={
            "status": "DRAFT",
            "source": "alpha_research",
            "factor_id": factor_id,
            "description": f"Alpha Factor: {factor_name}",
            "tags": ["alpha", meta.get("market", "a_share")],
        },
    )

    return {
        "code": 200,
        "data": {
            "strategy_id": res["id"],
            "name": file_name,
            "message": f"因子 {factor_name} 已导出到 AI-IDE 工作空间",
            "quality": {"pfs": pfs_val, "logic_score": logic_score},
            "quality_warnings": quality_warnings,
        },
    }


@router.get("/stats")
async def get_stats(
    request: Request,
    market: str | None = Query(None, description="按市场过滤统计"),
):
    """当前用户的因子统计信息"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    auth_user_id, _ = get_authenticated_identity(request)

    conditions = ["user_id = :user_id"]
    params: dict = {"user_id": auth_user_id}
    if market:
        conditions.append("market = :market")
        params["market"] = market
    where_clause = "WHERE " + " AND ".join(conditions)

    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'backtesting') AS backtesting,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                AVG(ic_value) FILTER (WHERE ic_value IS NOT NULL) AS avg_ic,
                AVG(sharpe_ratio) FILTER (WHERE sharpe_ratio IS NOT NULL) AS avg_sharpe,
                MAX(ic_value) AS best_ic,
                MAX(sharpe_ratio) AS best_sharpe
            FROM rd_agent_factors
            {where_clause}
        """),
            params,
        )
        row = rows.mappings().first()

    if not row:
        return {"code": 200, "data": {}}

    data = dict(row)
    for key in ("avg_ic", "best_ic", "avg_sharpe", "best_sharpe"):
        if data.get(key) is not None:
            data[key] = round(float(data[key]), 4)
    return {"code": 200, "data": data}


@router.get("/data-summary")
async def get_data_summary():
    """返回 QuantDB 数据可用性摘要（日期范围、股票池、数据集）"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        summary = hub.get_data_summary()
        return {"code": 200, "data": summary}
    except Exception as e:
        logger.warning("Failed to get data summary: %s", e)
        return {"code": 200, "data": {"available": False, "error": str(e)[:200]}}


@router.get("/mining/config-options")
async def mining_config_options(
    request: Request,
    mining_start: str = Query("", description="预览挖掘区间起点（YYYY-MM-DD，可选）"),
    mining_end: str = Query("", description="预览挖掘区间终点（可选）"),
    valid_start: str = Query("", description="预览检验区间起点（可选）"),
    valid_end: str = Query("", description="预览检验区间终点（可选）"),
    freq: str = Query(
        "",
        description=(
            "预览挖掘频率: 留空/day=日线；"
            "1min=分钟原生（仅 A 股，覆盖/默认窗换成分钟可用连续块）"
        ),
    ),
):
    """挖掘配置目录与默认值（前端挖掘表单初始化 / 分段预览）。

    各段返回，全部与提交路径同源（同一组归一函数）：

    - ``datasets``：可选挖掘数据集（key + 中文标签，登记序）；
    - ``algorithms`` + ``default_algorithm``：可选挖掘算法目录（P2，含能力面
      字段——前端按 uses_datasets/uses_llm 决定数据集选择/演化轮数控件是否生效）；
    - ``freqs``：可选挖掘频率目录；``freq`` 回显本次预览口径；
    - ``coverage``：数据实际覆盖区间（前端据此限位日期选择器）；
    - ``defaults``：默认挖掘/检验窗口（日线锚定覆盖末端 4+1 年）；
    - ``windows`` + ``segments``：**生效窗口**与 train/valid/test 分段——
      给了窗口参数即预览该组合（校验与提交同一口径，非法即 400），
      没给则预览默认窗口。

    ``windows.validation`` 为 ``None`` = 不启用样本外检验（分段退化为
    挖掘区间内的 70/15/15 切分），不是"检验窗为空"。

    ``freq=1min`` 时 ``coverage``/``defaults``/``backtest_end_guard`` 全部换成
    分钟口径（provider 覆盖 + 可用连续块），并额外回 ``minute`` 段（可用区间、
    断层表、就绪态）。分钟只有 ~4 个月数据，默认窗 = 可用连续块且**必然显式
    落到任务行**（子进程要求显式窗口，前端不传也一样——见 launcher
    ``normalize_mining_config``）。
    """
    from backend.services.engine.alpha_agent.mining_config import (
        dataset_catalog,
        default_windows,
        qlib_segments,
        resolve_windows,
    )

    get_authenticated_identity(request)

    try:
        freq_value = normalize_freq(freq)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    is_minute = freq_value == FREQ_MINUTE
    scope = None
    minute_error: str | None = None
    if is_minute:
        try:
            scope = minute_scope()
        except Exception as exc:  # noqa: BLE001 —— 目录接口不炸：坏 provider 如实上屏
            logger.warning("[alpha-agent] 分钟 provider 事实读取失败: %s", exc)
            minute_error = str(exc)

    if is_minute:
        coverage = scope.coverage if scope else None
        guard = scope.data_end if scope else None
        default_mining, default_validation = (
            (scope.usable, None) if scope else (None, None)
        )
    else:
        coverage = _quantdb_coverage()
        guard = _backtest_guard(coverage)
        default_mining, default_validation = default_windows(coverage, data_end=guard)
    try:
        mining, validation = resolve_windows(
            mining_start,
            mining_end,
            valid_start,
            valid_end,
            coverage=coverage,
            data_end=guard,
        )
        if scope is not None:
            require_minute_window(mining, scope, label="挖掘数据区间")
            require_minute_window(validation, scope, label="因子检验区间")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    is_default = mining is None
    eff_mining = default_mining if is_default else mining
    eff_validation = default_validation if is_default else validation
    # 分钟数据面未就绪：eff_mining 为 None（前端置灰提交），不产分段而不是产假分段
    segments = qlib_segments(eff_mining, eff_validation) if eff_mining else {}

    def _window_json(win: Window | None) -> dict[str, str] | None:
        if win is None:
            return None
        return {"start": win.start.isoformat(), "end": win.end.isoformat()}

    minute_block: dict | None = None
    if is_minute:
        from backend.services.engine import minute_qlib_provider as MP

        minute_block = {
            "ready": scope is not None,
            "provider_dir": str(MP.resolve_minute_provider_dir()),
            "error": minute_error,
            # 覆盖 = provider 全区间（含断层之前的碎片）；usable = 真正可挖的
            # 末端连续块。前端在分钟模式下用 usable 限位日期选择器。
            "coverage": _window_json(scope.coverage) if scope else None,
            "usable": _window_json(scope.usable) if scope else None,
            "usable_days": scope.days if scope else None,
            "gaps": [
                {"start": a.isoformat(), "end": b.isoformat()}
                for a, b in (scope.gaps if scope else ())
            ],
        }

    return {
        "code": 200,
        "data": {
            "datasets": dataset_catalog(),
            # 挖掘算法目录（P2）：与 GET /algorithms 同源同一份 manifest 摘要；
            # 面板一次拉取即可渲染「算法下拉 + 该算法支持哪些配置控件」。
            "algorithms": list_algorithms(),
            "default_algorithm": DEFAULT_ALGORITHM_KEY,
            "freqs": [
                {"value": item, "label": _FREQ_LABELS.get(item, item)}
                for item in SUPPORTED_FREQS
            ],
            "freq": freq_value,
            "minute": minute_block,
            "coverage": _window_json(coverage),
            "defaults": {
                "mining": _window_json(default_mining),
                "validation": _window_json(default_validation),
            },
            "windows": {
                "mining": _window_json(eff_mining),
                "validation": _window_json(eff_validation),
                "is_default": is_default,
            },
            # 回测可用末端（数据末端前一个交易日）：填到这一天的窗口会被原地退让
            # 到该日（qlib 回测按 t+1 成交，终点须留次日行情）。前端用它给日期
            # 选择器设上限并解释"为什么不是覆盖末端"。
            "backtest_end_guard": guard.isoformat() if guard else None,
            "segments": {
                name: {"start": span[0], "end": span[1]}
                for name, span in segments.items()
            },
            # 自定义窗口/数据集目前仅 A 股可用（其他市场数据面不走 QuantDB）
            "scopes": ["a_share"],
        },
    }


@router.get("/factor-categories")
async def get_factor_categories():
    """返回 L1 因子类别（feature catalog）+ 因子值库目录（T-MV-06）。

    两段独立降级：任一段失败只空自己那一段（``logger.warning``），不互相
    拖挂——设置页「挖掘方向」两组各有各的兜底展示。``libraries`` 的列数/
    日期范围由目录加载器现场读磁盘（``factor_libraries.library_disk_facts``）。
    """
    categories: list = []
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        categories = hub.fetch_l1_factor_categories().get("categories", [])
    except Exception as e:
        logger.warning("Failed to get factor categories: %s", e)

    libraries: list = []
    libraries_checked_at = ""
    try:
        from backend.services.engine.mining_plugins.factor_libraries import (
            factor_libraries_payload,
        )

        payload = factor_libraries_payload()
        libraries = payload["libraries"]
        libraries_checked_at = payload["checked_at"]
    except Exception as e:
        logger.warning("Failed to get factor libraries: %s", e)

    return {
        "code": 200,
        "data": {
            "categories": categories,
            "libraries": libraries,
            "libraries_checked_at": libraries_checked_at,
        },
    }


@router.get("/universes")
async def get_universes(request: Request):
    """返回可用股票池及股票数（内置指数 + 用户可见的全局自定义池）"""
    universes: dict[str, dict] = {}
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        summary = hub.get_data_summary()
        for code, meta in (summary.get("universes") or {}).items():
            universes[code] = {
                "count": meta.get("count", 0) if isinstance(meta, dict) else 0,
                "indexSymbol": meta.get("indexSymbol")
                if isinstance(meta, dict)
                else None,
                "is_system": True,
            }
    except Exception as e:
        logger.warning("Failed to get builtin universes: %s", e)

    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
        from backend.shared.stock_pool import repository as pool_repo

        user_id, tenant_id = get_authenticated_identity(request)
        async with get_session() as session:
            await pool_repo.ensure_tables(session)
            rows = (
                (
                    await session.execute(
                        text(
                            """
                    SELECT code, name, symbol_count, is_system
                      FROM qm_stock_pool
                     WHERE status <> 'archived'
                       AND market = 'CN'
                       AND (
                            scope = 'global'
                            OR (scope = 'tenant' AND tenant_id = :tenant_id)
                            OR (scope = 'user' AND owner_user_id = :user_id)
                       )
                     ORDER BY is_system DESC, code ASC
                    """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            code = str(row["code"])
            if code in universes and row["is_system"]:
                continue
            universes[code] = {
                "count": int(row["symbol_count"] or 0),
                "indexSymbol": None,
                "is_system": bool(row["is_system"]),
                "name": str(row["name"] or code),
            }
    except Exception as e:
        logger.warning("Failed to merge custom stock pools: %s", e)

    return {"code": 200, "data": {"universes": universes}}


@router.get("/llm-config")
async def get_llm_config(request: Request):
    """返回当前生效的 LLM 配置状态（不回显完整 key）。

    优先级：当前用户个人中心的 AI 服务配置 > 服务器环境变量（以用户设置为准，
    env 仅作兜底）。

    ``embedding`` 段与 chat 段**互相独立**：它只反映 Profile 里用户自己填的值，
    为空时意味着「沿用容器级 EMBEDDING_*」，不跟随 chat 的 env 兜底。所以哪怕
    chat 未配置，embedding 段也照样返回。
    """
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    profile = await _fetch_profile_raw(auth_user_id, auth_tenant_id)
    embedding = normalize_embedding_status(profile)

    cfg = await _fetch_profile_llm_config(auth_user_id, auth_tenant_id, data=profile)
    source = "user_profile"
    if cfg is None:
        cfg = resolve_llm_config()
        source = "env"
    if cfg is None:
        return {
            "code": 200,
            "data": {
                "configured": False,
                "reason": "未配置可用的 API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置",
                "embedding": embedding,
            },
        }
    # 仅回显 key 末 4 位，避免泄露
    key = cfg.api_key
    masked = f"****{key[-4:]}" if len(key) >= 4 else "****"
    return {
        "code": 200,
        "data": {
            "configured": True,
            "source": source,
            "provider": cfg.protocol,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "api_key_masked": masked,
            "embedding": embedding,
        },
    }


@router.put("/llm-config/embedding")
async def update_embedding_config(request: Request):
    """保存向量检索（embedding）配置。

    因子挖掘的记忆检索走 RD-Agent 子进程的 ``EMBEDDING_*`` 环境变量，
    由 ``rd_agent/llm_env.embedding_overrides`` 从用户配置注入。这条通道与
    chat 独立，正是为了让 DeepSeek 这类没有 ``/embeddings`` 端点的 chat 供应商
    也能用上向量检索。

    **只提交显式传入的字段**（``undefined`` = 不动，``""`` = 清除，``null`` = 未传）：
    未提交的项留给容器级 ``EMBEDDING_*`` 兜底。全量提交会让「只改模型名」的
    请求顺手清掉 Key，然后静默退回容器默认端点。
    """
    try:
        body = await request.json()
    except ValueError as exc:
        # 非法 JSON 会由 Starlette 抛 JSONDecodeError（ValueError 子类）；不接的话
        # 变成 500，用户只看到「服务器错误」而不知道是自己发的 body 有问题。
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    try:
        payload = build_embedding_payload(body)
    except EmbeddingFieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload:
        raise HTTPException(
            status_code=400, detail="请至少填写模型、接口地址或 API Key"
        )

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    await _update_profile(auth_user_id, auth_tenant_id, payload)

    profile = await _fetch_profile_raw(auth_user_id, auth_tenant_id)
    return {"code": 200, "data": normalize_embedding_status(profile)}


# ── 因子池（P1）：池总览 / 池列表 / 谱系图 / 刷新（子进程 + 锁）──────────
#
# universe 的 HTTP 约定：**空串或缺省 = 全 universe 汇总**（页面「全部」选项
# 发空串），非空值 = 精确作用域过滤。空 universe 作用域（因子没写 universe）
# 不从 HTTP 单选，刷新走全 scope 发现自然覆盖。
# 刷新是子进程（与管理员面物化同款硬化）：跨 asyncio.run 的 DB 引擎会绑错事件
# 循环（memory: 全局 _db_manager 缓存跨 loop 陷阱），且 pandas 重算会阻塞引擎
# 事件循环——绝不在引擎进程内跑。


def _pool_scope(market: str, universe: str) -> tuple[str, str | None]:
    """校验 market 并把 HTTP 的空串 universe 归一到 None（全量）。"""
    if market not in _MARKET_TO_QLIB:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 market：{market}（可选：{', '.join(_MARKET_TO_QLIB)}）",
        )
    universe = (universe or "").strip()
    return market, (universe or None)


@router.get("/pool/overview")
async def get_pool_overview(
    request: Request,
    market: str = Query("a_share"),
    universe: str = Query(""),
):
    """因子池总览：计数 / 质量均值 / 多样性熵 / 检索计数（user-scoped）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(market, universe)

    from backend.services.engine.mining_plugins import pool_service

    data = await pool_service.pool_overview(
        user_id=auth_user_id, market=market, universe=universe
    )
    return {"code": 200, "data": data}


@router.get("/pool/factors")
async def get_pool_factors(
    request: Request,
    market: str = Query("a_share"),
    universe: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("pool_score"),
    include_archived: bool = Query(False),
    category: str = Query(""),
    direction: str = Query(""),
):
    """池内因子分页列表（含门禁裁决、被检索次数、面板有无标记、因子大类）。

    默认不含已归档；``include_archived=true`` 时行里带 ``archived_at``
    （UI 用它渲染「已归档」徽章与恢复入口）。``category`` 按因子大类过滤
    （总览分类区块点击下钻；未知类名显式 400，不静默空列表）。
    ``direction`` 按方向过滤（pos=正向 IC≥0 / neg=反向 IC<0；口径与因子库
    「方向」列一致，父本选择器「正反各前 N」用；未知取值显式 400）。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(market, universe)

    from backend.services.engine.mining_plugins import pool_service
    from backend.services.engine.mining_plugins.factor_classify import (
        CANONICAL_CLASSES,
    )

    category = (category or "").strip()
    if category and category not in CANONICAL_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"未知因子大类：{category}（可选：{', '.join(CANONICAL_CLASSES)}）",
        )

    try:
        data = await pool_service.list_pool_factors(
            user_id=auth_user_id,
            market=market,
            universe=universe,
            limit=limit,
            offset=offset,
            sort=sort,
            include_archived=include_archived,
            category=(category or None),
            direction=(direction or None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": data}


@router.get("/pool/graph")
async def get_pool_graph(
    request: Request,
    market: str = Query("a_share"),
    universe: str = Query(""),
    max_nodes: int = Query(200, ge=2, le=500),
    include_archived: bool = Query(False),
):
    """谱系图（nodes + edges）；上限与 service 内部钳制一致，超出直接 422。"""
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(market, universe)

    from backend.services.engine.mining_plugins import pool_service

    data = await pool_service.pool_graph(
        user_id=auth_user_id,
        market=market,
        universe=universe,
        max_nodes=max_nodes,
        include_archived=include_archived,
    )
    return {"code": 200, "data": data}


@router.post("/pool/refresh")
async def post_pool_refresh(
    request: Request,
    market: str = Query("a_share"),
    universe: str = Query(""),
    dry_run: bool = Query(True),
):
    """启动后台刷新子进程（默认 dry_run；单飞，409=已有刷新在跑）。

    **owner 恒为鉴权身份**，不接受客户端传 user_id——刷新会重写池行与
    谱系边，跨用户触发等于替别人重算并占全局锁。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(market, universe)

    from backend.scripts.mining_pool_rebuild import (
        RefreshBusyError,
        RefreshStartError,
        spawn_refresh,
    )

    try:
        data = await spawn_refresh(
            user_id=auth_user_id, market=market, universe=universe, dry_run=dry_run
        )
    except RefreshBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # build_run_command 白名单校验失败
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RefreshStartError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"code": 200, "data": data}


@router.get("/pool/refresh/status")
async def get_pool_refresh_status(request: Request):
    """刷新状态（锁探活 + 最近一次落盘状态 + 日志尾）。

    状态文件与日志是全局单份（单锁单日志）：最近一次刷新属主不是调用者时
    只回 ``running`` 位与 ``other_user`` 标记，日志/scope 一律不回显——
    否则任一面板变成他人挖掘面的浏览窗口。
    """
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.scripts.mining_pool_rebuild import refresh_status

    status = refresh_status()
    owner = str((status.get("args") or {}).get("user") or "")
    if owner and owner != auth_user_id:
        return {
            "code": 200,
            "data": {
                "running": bool(status.get("running")),
                "status": "other_user",
                "log": {"exists": False, "lines": []},
            },
        }
    return {"code": 200, "data": status}


# ── 非 SOTA 清理建议与归档（P3）─────────────────────────────────────────
# 只建议不自动删：判据（池冗余/弱 ICIR/零多样性贡献）由 pool_cleanup 计算、
# 带数字证据展示；归档=打 archived_at 时间戳（非删除），默认退出注入与池
# 视图，可随时恢复。owner 恒为鉴权身份——归档是写操作，跨用户 id 只进
# skipped（service 层 user_id 硬过滤）。


class PoolArchiveRequest(BaseModel):
    factor_ids: list[str] = Field(..., min_length=1, max_length=500)


@router.get("/pool/cleanup/suggestions")
async def get_pool_cleanup_suggestions(
    request: Request,
    market: str = Query("a_share"),
    universe: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    """清理建议清单：items（判据逐条带数字）+ summary + criteria + sota。"""
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(market, universe)

    from backend.services.engine.mining_plugins import pool_service

    data = await pool_service.cleanup_suggestions(
        user_id=auth_user_id, market=market, universe=universe, limit=limit
    )
    return {"code": 200, "data": data}


@router.post("/pool/cleanup/archive")
async def post_pool_cleanup_archive(request: Request, body: PoolArchiveRequest):
    """批量归档（非删除）。返回 archived/skipped；跨用户因子只会进 skipped。"""
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.services.engine.mining_plugins import pool_service

    data = await pool_service.archive_factors(
        user_id=auth_user_id, factor_ids=body.factor_ids
    )
    return {"code": 200, "data": data}


@router.post("/pool/cleanup/unarchive")
async def post_pool_cleanup_unarchive(request: Request, body: PoolArchiveRequest):
    """恢复归档（清 archived_at），重新参与注入与池视图。"""
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.services.engine.mining_plugins import pool_service

    data = await pool_service.unarchive_factors(
        user_id=auth_user_id, factor_ids=body.factor_ids
    )
    return {"code": 200, "data": data}


# ── 组合实验室（P2）：POST /combos/optimize + 列表 / 详情 ────────────────
# 作业是**子进程**（差分进化是 CPU 大户 + 跨 loop DB 引擎陷阱，与池刷新同
# 架构）；组合行就是作业的请求与结果载体：探活（忙 409，不建行）→ 建行
# （校验失败 400）→ spawn（竞态忙 409 / 起不来 500，失败路径当场把行标
# failed——不许静默 pending）→ 前端轮询 GET /combos/{id} 看 running/done/
# failed 与两窗指标；死亡作业（OOM/重启）由详情路径惰性收敛。


class ComboOptimizeRequest(BaseModel):
    market: str = "a_share"
    # universe 最长 64（安全 M-1：此前原样入库无上限，一行可塞任意大字符串）
    universe: str = Field("", max_length=64)
    factor_ids: list[str]
    name: str = ""
    seed: int | None = None


async def _combo_fail_best_effort(combo_id: str, error: str) -> None:
    """启动失败的行不许静默 pending：尽力标 failed（标记失败不掩盖 409/500 原因）。"""
    try:
        from backend.scripts.mining_combo_optimize import mark_failed_row

        await mark_failed_row(combo_id, error)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[combos] 标记组合行失败状态失败 %s: %s", combo_id, exc)


@router.post("/combos/optimize")
async def post_combo_optimize(request: Request, body: ComboOptimizeRequest):
    """建组合行 + 起后台优化子进程；owner 恒为鉴权身份（不接受客户端 user_id）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    market, universe = _pool_scope(body.market, body.universe)

    from backend.scripts.mining_combo_optimize import (
        ComboBusyError,
        ComboStartError,
        create_combo_row,
        probe_combo_lock,
        spawn_combo,
    )

    # 单飞前置探活：锁被占时 409 直接返回、不做校验也不建行——一次注定被拒
    # 的请求不许留 failed 行（安全 M-1）。竞态窗口由 spawn 取锁兜底。
    if probe_combo_lock():
        raise HTTPException(
            status_code=409, detail="已有组合优化作业在运行，请稍后重试"
        )

    try:
        combo_id = await create_combo_row(
            user_id=auth_user_id,
            market=market,
            universe=universe or "",
            factor_ids=body.factor_ids,
            name=body.name,
            seed=body.seed,
        )
    except ValueError as exc:  # 因子集 / scope 校验失败
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        started = await spawn_combo(combo_id)
    except ComboBusyError as exc:
        await _combo_fail_best_effort(combo_id, str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ComboStartError as exc:
        await _combo_fail_best_effort(combo_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 兜底：托管异常之外也不许静默留 pending
        logger.exception("[combos] 启动组合优化子进程异常 combo=%s", combo_id)
        await _combo_fail_best_effort(combo_id, f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"组合优化启动失败：{exc}") from exc
    return {
        "code": 200,
        "data": {"combo_id": combo_id, "status": "pending", **started},
    }


@router.get("/combos")
async def get_combos(
    request: Request,
    market: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """组合列表（user-scoped，新→旧）；market 空串 = 全部市场。"""
    auth_user_id, _ = get_authenticated_identity(request)
    if market:
        market, _ = _pool_scope(market, "")

    from backend.scripts.mining_combo_optimize import list_combos

    data = await list_combos(
        user_id=auth_user_id, market=market or None, limit=limit, offset=offset
    )
    return {"code": 200, "data": data}


@router.get("/combos/{combo_id}")
async def get_combo_detail(request: Request, combo_id: str):
    """组合详情（含权重/两窗指标/净值曲线）；非属主 404（不泄露存在性）。

    pending/running 的行先做惰性收敛：作业进程死亡（OOM/容器重启）时行里
    没有心跳，只有锁位能证明活着——锁空闲且状态陈旧就收尸标 failed，
    否则前端会永久轮询一个不可能推进的状态（评审发现 2）。
    """
    auth_user_id, _ = get_authenticated_identity(request)

    from backend.scripts.mining_combo_optimize import get_combo, reconcile_stale_row

    data = await get_combo(combo_id, user_id=auth_user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="组合不存在")
    if str(data.get("status") or "") in ("pending", "running"):
        if await reconcile_stale_row(combo_id, user_id=auth_user_id):
            data = await get_combo(combo_id, user_id=auth_user_id) or data
    return {"code": 200, "data": data}


_MARKET_TO_QLIB: dict[str, str] = {
    "a_share": "CN",
    "hong_kong": "HK",
    "us_stock": "US",
    "crypto": "CRYPTO",
    "futures": "FUTURES",
}

_QLIB_NATIVE_UNIVERSES = ("csi300", "csi500", "csi1000", "csi800")


def _compute_pfs_quality(df: "pd.DataFrame") -> dict | None:
    """扰动保真度（PFS）：从 (trade_date, symbol, factor) 面板算因子的排名稳健性。

    实现来自 docker/training/data/factor_quality.py（经 backend.shared.factor_quality
    按路径加载，与训练侧筛选同一公式，口径不漂移）；模块不可用/样本不足返回 None。
    低于 0.9 = 截面 z 分加噪后排名明显塌陷（数据误差/离散化敏感），不宜实盘。
    """
    try:
        from backend.shared.factor_quality import load_factor_quality

        fq = load_factor_quality()
        if fq is None or df is None or df.empty:
            return None
        out = fq.compute_pfs(df, ["factor"]).get("factor") or {}
        if out.get("pfs") is None:
            return None
        return {
            "pfs": round(float(out["pfs"]), 4),
            "pfs_gauss": round(float(out["pfs_gauss"]), 4)
            if out.get("pfs_gauss") is not None
            else None,
            "pfs_t": round(float(out["pfs_t"]), 4)
            if out.get("pfs_t") is not None
            else None,
            "n_days": int(out.get("n_days") or 0),
        }
    except Exception as exc:  # noqa: BLE001 — 质量度量失败不影响回测结果
        logger.warning("[alpha-backtest] PFS computation failed: %s", exc)
        return None


def _quality_warnings(pfs: float | None, logic_score: int | None) -> list[str]:
    """导出前的质量闸门（软）：PFS / 金融逻辑分低于阈值时返回警告文案。"""
    warnings: list[str] = []
    if pfs is not None and float(pfs) < 0.9:
        warnings.append(
            f"扰动保真度偏低（PFS={float(pfs):.3f} < 0.9）：截面加噪后排名易塌，实盘换手不稳"
        )
    if logic_score is not None and int(logic_score) < 60:
        warnings.append(
            f"金融逻辑评分偏低（logic_score={int(logic_score)} < 60）：建议人工复核经济含义"
        )
    return warnings


def _run_mining_evaluators(
    f_clean: "pd.Series",
    r_clean: "pd.Series",
    *,
    market: str,
    universe: str,
    factor_id: str,
) -> dict[str, float]:
    """机构级评估器链（mining_plugins）：RRE / 换手 / 扣成本。

    与 H5 子进程路径共用同一入口（evaluate_paired）；成功值并入回测 metadata
    （缺失键不写——前端一律以「—」呈现缺失）。评估器故障降级为空 dict：
    指标是可加层，绝不拖垮回测本身（与 _compute_pfs_quality 同策略）。
    """
    try:
        import numpy as np
        import pandas as pd

        from backend.services.engine.mining_plugins import evaluate_paired

        paired = pd.DataFrame(
            {
                "datetime": f_clean.index.get_level_values("datetime"),
                "symbol": f_clean.index.get_level_values("instrument"),
                "factor": np.asarray(f_clean, dtype=float),
                "ret": np.asarray(r_clean, dtype=float),
            }
        )
        metrics = evaluate_paired(
            paired, market=market, universe=universe, factor_id=factor_id
        )
        return {k: v for k, v in metrics.items() if v is not None}
    except Exception as exc:  # noqa: BLE001 — 评估器故障不影响回测结果
        logger.warning("[alpha-backtest] mining evaluators failed: %s", exc)
        return {}


def _parse_eval_metrics(out: str) -> dict[str, float]:
    """从子进程输出解析 ``EVAL_<key>=<float>`` 行（评估器插件统一协议）。

    缺失/非法值跳过（与 _metric 的缺失语义一致）；过滤 NaN。返回 {metric_key: value}。
    """
    metrics: dict[str, float] = {}
    for line in out.splitlines():
        if not line.startswith("EVAL_"):
            continue
        key, sep, raw = line[len("EVAL_") :].partition("=")
        if not sep or not key:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value == value:  # 过滤 NaN
            metrics[key] = value
    return metrics


def _parse_logic_score(text: str) -> tuple[str, int | None]:
    """从 LLM 解释文本中抽出 ``SCORE: <50-100>`` 评分行。

    返回 (去掉评分行的解释正文, 评分|None)。LLM 可能不按格式输出或给出越界值：
    没有匹配 → 原文 + None；越界一律钳制到 [0, 100]。取最后一个匹配（正文里
    若引用了评分说明，以最后的行为准）。
    """
    score: int | None = None
    match = None
    for match in re.finditer(r"SCORE\s*[:：]\s*(\d{1,3})", text):  # noqa: B007 -- 取最后一个匹配
        pass
    if match is not None:
        score = max(0, min(100, int(match.group(1))))
        text = (text[: match.start()] + text[match.end() :]).strip()
    return text, score


#: qlib 表达式字符白名单：字段引用（$）、字面量、算子名、四则/比较运算符、
#: 括号与逗号。这是**保守路由**的判据（见 ``_looks_like_qlib_expression``），
#: 不是安全校验——真正的合法性由子进程里的 qlib 求值裁决。
_QLIB_EXPRESSION_CHARS = re.compile(r"^[\w\s$.,()+\-*/%<>=!&|]+$")


def _looks_like_qlib_expression(factor_code: str) -> bool:
    """统计派因子的落库形态：单行 qlib 表达式（``$字段`` + 算子调用）。

    判据从严：出现换行、引号、方括号、注释等一律**不**算表达式，保持
    「Python 代码走 AST 判型」的原行为（报语法错误），把可疑文本拦在求值
    路径之外；``$`` 字段引用必须存在——没有它就只是坏 Python，不是表达式。
    判真也只管**路由**：求不动的表达式会按回测失败落库（带 qlib 原始报错），
    这里不假装验证过。
    """
    if not factor_code or "\n" in factor_code or "\r" in factor_code:
        return False
    if "$" not in factor_code:
        return False
    return bool(_QLIB_EXPRESSION_CHARS.fullmatch(factor_code))


def _looks_like_fm_expression(factor_code: str) -> bool:
    """FM-DSL 因子（论文 110 因子落库形态 ``fm:…``）的保守路由判据。

    双判据：``fm:`` 前缀 + 白名单解析器（``factorminer.dsl.parse``，禁 eval）
    能完整解析——解析器本身就是安全边界，解析不过的文本不路由，维持
    「坏代码报语法错误」的原行为。判真也只管路由，求不动的因子按回测失败
    落库，与 qlib 表达式路径同一纪律。
    """
    from backend.services.engine.alpha_agent.factorminer.dsl import (  # noqa: PLC0415
        FM_CODE_PREFIX,
        parse as fm_parse,
    )

    if not factor_code.startswith(FM_CODE_PREFIX):
        return False
    try:
        fm_parse(factor_code[len(FM_CODE_PREFIX) :])
    except ValueError:
        return False
    return True


def _detect_factor_kind(factor_code: str) -> str:
    """AST 预检：判断 Qlib Factor 类 / RD-Agent 函数式 / qlib 表达式。

    不执行因子代码，只解析语法树。函数式含**两种入口样式**（与
    ``_run_functional_factor_subprocess`` 的优先级注释一一对应）：
    ``calculate_*()`` 函数，或自执行式 ``main()`` + ``__main__`` 守卫。
    判定顺序 calculate_ → class → main 守卫：类因子顺带写个自测守卫不能被
    误判成函数式（进错执行器），而只有守卫没有类/calculate_ 的代码此前直接
    「unknown」拒跑——补码评估实测踩中（2026-10-09，LLM 按自执行契约产出的
    Amihud ILLIQ 因检测缺口报「未找到可调用的 Factor 类或 calculate_*」）。

    ``expression``（统计派）不是 Python——``$close``/``Ref(...)`` 按 Python
    解析必然 SyntaxError，故放在 SyntaxError 分支里保守识别；求值走 qlib
    原生路径（子进程，理由见 ``_run_expression_factor_subprocess``）。
    """
    import ast

    from backend.services.engine.alpha_agent.factor_codegen import is_main_guard

    try:
        tree = ast.parse(factor_code)
    except SyntaxError as e:
        # FM-DSL 因子（`fm:` 前缀，论文 110 因子落库形态）：FM 原文必含
        # `$字段`，按 Python 解析必 SyntaxError（`:` 本身不保证非法——
        # `fm:Mean` 是合法注解语法；真防线是前缀 + 白名单解析双判据）；
        # 求值走 factorminer 自有求值器（见 _run_expression_factor_subprocess）。
        if _looks_like_fm_expression(factor_code):
            return "expression"
        # 统计派因子是 qlib 表达式：只有通过文件头的字符白名单才路由到
        # expression，其余 SyntaxError 文本维持原行为（报错，不送进求值路径）。
        if _looks_like_qlib_expression(factor_code):
            return "expression"
        raise RuntimeError(f"因子代码语法错误: {e.msg} (line {e.lineno})") from e

    has_class = False
    has_calculate = False
    has_main_guard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            name_lower = node.name.lower()
            if "factor" in name_lower or any(
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "name" for t in n.targets)
                for n in node.body
            ):
                has_class = True
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("calculate_"):
            has_calculate = True
        elif isinstance(node, ast.If) and is_main_guard(node.test):
            has_main_guard = True

    if has_calculate:
        return "functional"
    if has_class:
        return "factor_class"
    if has_main_guard:
        return "functional"
    return "unknown"


def _vectorized_daily_spearman_ic(
    f: "pd.Series", r: "pd.Series"
) -> tuple[float, float, float, float, int]:
    """向量化计算日度 Spearman IC（秩相关 = 秩的 Pearson）。

    不用每日 spearmanr() 调用，全表 groupby 一次算完。
    Returns: (ic_mean, rank_ic_median, icir, rank_icir, observations)

    口径与挖掘阶段 scripts/alpha_agent/run_rd_agent.py:compute_factor_ic 一致：
    ic = 日度秩相关均值，rank_ic = 其中位数，std = 该序列标准差(ddof=1)，
    icir = ic/std，rank_icir = rank_ic/std。
    """
    import numpy as np
    import pandas as pd

    if len(f) < 100 or len(r) < 100:
        return 0.0, 0.0, 0.0, 0.0, 0

    df = pd.DataFrame({"f": f.values, "r": r.values})
    # 按**名字**取日期层：层序归位到 (datetime, instrument) 之后按下标取会取到标的。
    df["date"] = f.index.get_level_values("datetime")
    df = df[np.isfinite(df["f"]) & np.isfinite(df["r"])]
    if len(df) < 100:
        return 0.0, 0.0, 0.0, 0.0, 0

    # 每日 rank
    df["f_rank"] = df.groupby("date")["f"].rank(method="average")
    df["r_rank"] = df.groupby("date")["r"].rank(method="average")

    # 每日去均值（per group, transform 一次算完）
    g = df.groupby("date")[["f_rank", "r_rank"]]
    means = g.transform("mean")
    df["fc"] = df["f_rank"] - means["f_rank"]
    df["rc"] = df["r_rank"] - means["r_rank"]

    # 每日 sum / (n - 1) = 协方差 / 方差
    df["fcr"] = df["fc"] * df["rc"]
    df["fc2"] = df["fc"] ** 2
    df["rc2"] = df["rc"] ** 2
    sums = df.groupby("date")[["fcr", "fc2", "rc2"]].transform("sum")
    counts = df.groupby("date")["fcr"].transform("count")
    n = (counts - 1).clip(lower=1)
    cov = sums["fcr"] / n
    var_f = sums["fc2"] / n
    var_r = sums["rc2"] / n
    denom = np.sqrt(var_f * var_r)
    # 避免除零
    df["corr"] = np.where(
        denom > 1e-12, cov / np.where(denom > 1e-12, denom, 1.0), np.nan
    )

    # 每日的 IC
    ic_by_date = df.groupby("date")["corr"].first().dropna()
    ic_by_date = ic_by_date[np.isfinite(ic_by_date)]
    if len(ic_by_date) == 0:
        return 0.0, 0.0, 0.0, 0.0, 0

    ic_mean = float(ic_by_date.mean())
    rank_ic_median = float(ic_by_date.median())
    std = float(ic_by_date.std(ddof=1)) if len(ic_by_date) > 1 else 0.0
    icir = ic_mean / (std + 1e-8)
    rank_icir = rank_ic_median / (std + 1e-8)
    return ic_mean, rank_ic_median, icir, rank_icir, int(len(df))


def _resolve_instruments_for_universe(
    market_upper: str, universe: str
) -> list[str] | str:
    """返回 Qlib instruments 选择。

    A 股: Qlib 原生 (csi300/500/1000/800) 直接用 D.instruments(market=...)
         或 QuantDB 取 (sse50/gem/star/all_a)
    其他市场: Qlib 原生 (csi300/500/...) 不存在 → 用 D.instruments(market="all")
    """
    from qlib.data import D

    if market_upper == "CN":
        custom = _resolve_custom_pool_instruments(universe)
        if custom is not None:
            return custom
        if universe in _QLIB_NATIVE_UNIVERSES:
            return D.instruments(market=universe)
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            from backend.shared.stock_utils import StockCodeUtil

            hub = QuantDBDataHub.get_instance()
            universe_df = hub.fetch_universe_stocks(universe or "csi300")
            if universe_df is None or universe_df.empty:
                raise RuntimeError(f"QuantDB returned no constituents for {universe}")
            return sorted(
                {
                    StockCodeUtil.to_prefix(s)
                    for s in universe_df["symbol"].tolist()[:500]
                }
            )
        except Exception as e:
            logger.warning(
                "QuantDB universe %s unavailable, falling back to csi300: %s",
                universe,
                e,
            )
            return D.instruments(market="csi300")
    # 非 CN 市场：Qlib cache 的 instruments/all.txt 是全集，universe 仅作过滤
    # 这里简化: 直接 D.instruments(market="all")
    return D.instruments(market="all")


def _default_backtest_window(market: str = "a_share") -> tuple[str, str]:
    """默认回测窗口：近一年（end=数据最新交易日，start=end 往前一年）。"""
    import pandas as pd

    end_ts = None
    try:
        if market == "a_share":
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            cal = QuantDBDataHub.get_instance().fetch_calendar()
            if cal is not None and not cal.empty:
                for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                    if col in cal.columns:
                        end_ts = pd.to_datetime(cal[col]).max()
                        break
    except Exception as exc:
        logger.warning("[alpha-backtest] resolve default window failed: %s", exc)
    if end_ts is None or pd.isna(end_ts):
        end_ts = pd.Timestamp.today().normalize()
    return (end_ts - pd.DateOffset(years=1)).strftime("%Y-%m-%d"), end_ts.strftime(
        "%Y-%m-%d"
    )


def _format_backtest_error(exc: BaseException) -> str:
    """失败原文（含 traceback 尾段，封顶 1500 字符）——因子行 metadata 与历史
    台账共用同一份（尾段保留异常发生点，头段多是无关的调用栈顶）。"""
    tb = getattr(exc, "__traceback__", None)
    tb_text = ""
    if tb:
        import traceback as _tb

        tb_text = "".join(_tb.format_tb(tb))[-1500:]
    return f"{type(exc).__name__}: {exc}" + (f"\n{tb_text}" if tb_text else "")


async def _run_factor_backtest(
    factor_id: str,
    factor_code: str,
    market: str = "a_share",
    data_source: str = "qlib_bin",
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = "csi300",
    run_id: str | None = None,
) -> None:
    """统一回测入口（多市场 + 数据源可选）。

    Args:
        market: 'a_share' | 'hong_kong' | 'us_stock' | 'crypto' | 'futures'
        data_source: 'qlib_bin' (默认) | 'h5'
        run_id: 历史台账里本次运行的行身份（发起端点登记并透传）
    """
    market_upper = _MARKET_TO_QLIB.get(market, "CN")
    _default_start, _default_end = _default_backtest_window(market)
    end = end_date or _default_end
    start = start_date or _default_start

    try:
        kind = _detect_factor_kind(factor_code)
        if kind == "unknown":
            raise RuntimeError("因子代码中未找到可调用的 Factor 类或 calculate_* 函数")

        # 表达式因子是 qlib 原生形态（统计派产出），h5 执行器只认 Python 代码
        # 契约——直接切回二进制数据面，不是能力缺失而是数据面归属
        if kind == "expression" and data_source != "qlib_bin":
            logger.warning(
                "[alpha-backtest] 表达式因子走 Qlib 原生求值，data_source=%s 不适用，自动切到 qlib_bin",
                data_source,
            )
            data_source = "qlib_bin"

        # H5 路径: 多市场 H5 不全，自动回退
        if data_source == "h5":
            h5_path = _resolve_factor_h5_path_for_market(market)
            if not h5_path:
                logger.warning(
                    "[alpha-backtest] market=%s H5 不可用，自动回退到 Qlib 二进制",
                    market,
                )
                data_source = "qlib_bin"

        if data_source == "qlib_bin":
            await _backtest_via_qlib(
                factor_id,
                factor_code,
                kind,
                market,
                market_upper,
                universe,
                start,
                end,
                run_id=run_id,
            )
        else:
            await _backtest_via_h5(
                factor_id, factor_code, kind, universe, start, end, run_id=run_id
            )
    except FactorBacktestCancelled:
        logger.info("[alpha-backtest] %s cancelled by user", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="cancelled",
                metadata={"backtest_error": "cancelled_by_user"},
            )
        except Exception:
            pass
        await _record_backtest_finish(
            run_id,
            "cancelled",
            error="cancelled_by_user",
            universe=universe,
            data_source=data_source,
            date_range=f"{start}~{end}",
        )
    except Exception as exc:
        logger.exception("[alpha-backtest] %s failed", factor_id)
        err_msg = _format_backtest_error(exc)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": err_msg[-1500:]},
            )
        except Exception:
            pass
        await _record_backtest_finish(
            run_id,
            "failed",
            error=err_msg[-1500:],
            universe=universe,
            data_source=data_source,
            date_range=f"{start}~{end}",
        )
    finally:
        # 只有注册在册的仍是本次运行才清理：取消→立即重跑后，旧任务延迟收尾
        # 不得拆新任务的台（清掉它的去重键/取消标记）。无注册条目 = 直接调用
        # （测试/遗留路径），维持旧的无条件清理语义。
        if (
            factor_id not in _running_backtest_runs
            or _running_backtest_runs[factor_id] == run_id
        ):
            _running_backtest_runs.pop(factor_id, None)
            _running_backtests.discard(factor_id)
            _backtest_cancelled.discard(factor_id)


async def _run_factor_recovery(
    factors: list[dict],
    llm_config,
    user_id: str | None,
) -> None:
    """补码评估批次 worker（进程内串行；状态写模块级 ``_recovery_state``）。

    逐条：无码 → LLM 补码并写回（metadata 标注 ``code_recovered``）→ 调标准
    回测补 IC。**成败按行终态判**（回测把失败写进 status/metadata 而不抛异常，
    见 ``_run_factor_backtest`` 的 except 分支）：回测后回读 status，completed
    计成功，其余（failed/cancelled/pending）计失败——与用户点「回测」看到的
    口径一致，不按「有没有异常」猜。

    连败 ``_MAX_CONSECUTIVE_RECOVERY_FAILURES`` 条即中止（剩余条目保留待重发）：
    Key 失效/数据坏这类环境性问题逐条硬跑只会空烧配额，且每条都写同样的错。
    """
    from backend.services.engine.alpha_agent.factor_codegen import (
        generate_factor_code,
    )
    from backend.shared.utc_datetime import utc_now

    state = _recovery_state
    consecutive = 0
    try:
        for factor in factors:
            factor_id = str(factor.get("factor_id") or "")
            factor_name = factor.get("factor_name") or factor_id
            state["current_factor_id"] = factor_id
            state["current_factor_name"] = factor_name
            try:
                if factor_id in _running_backtests:
                    # 该因子正被用户的手动回测占用：跳过而不是排队等（进度
                    # 语义会含混）；本轮跳过计 skipped，ic 仍空，下次可重发。
                    state["skipped"] += 1
                    continue

                code = (factor.get("factor_code") or "").strip()
                market = factor.get("market") or "a_share"
                universe = factor.get("universe") or "csi300"
                if not code:
                    code = await generate_factor_code(factor, config=llm_config)
                    await persistence.save_factor(
                        factor_id,
                        factor_name=factor_name,
                        factor_code=code,
                        user_id=factor.get("user_id") or user_id,
                    )
                    metadata = dict(factor.get("metadata") or {})
                    metadata["code_recovered"] = "llm_codegen"
                    metadata["code_recovered_at"] = utc_now().isoformat()
                    await persistence.update_factor_metrics(
                        factor_id, metadata=metadata
                    )
                    factor = {**factor, "factor_code": code}

                run_id = await _prepare_factor_backtest(
                    factor_id,
                    factor,
                    market=market,
                    universe=universe,
                    data_source="qlib_bin",
                )
                await _run_factor_backtest(
                    factor_id,
                    code,
                    market=market,
                    data_source="qlib_bin",
                    universe=universe,
                    run_id=run_id,
                )

                row = await persistence.get_factor(factor_id) or {}
                if row.get("status") == "completed":
                    state["done"] += 1
                    consecutive = 0
                else:
                    state["failed"] += 1
                    consecutive += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不掀批次
                logger.warning(
                    "[factor-recovery] %s(%s) 失败: %s",
                    factor_name,
                    factor_id,
                    exc,
                )
                try:
                    metadata = dict(factor.get("metadata") or {})
                    metadata["recover_error"] = _format_backtest_error(exc)[-500:]
                    await persistence.update_factor_metrics(
                        factor_id, metadata=metadata
                    )
                except Exception:
                    pass
                state["failed"] += 1
                consecutive += 1

            if consecutive >= _MAX_CONSECUTIVE_RECOVERY_FAILURES:
                state["message"] = (
                    f"连续 {consecutive} 条失败，批次提前中止"
                    "（未处理条目保留，修复原因后可重新发起）"
                )
                logger.warning(
                    "[factor-recovery] 连败熔断：已完成 done=%s failed=%s skipped=%s",
                    state["done"],
                    state["failed"],
                    state["skipped"],
                )
                break
        else:
            state["message"] = (
                f"补码评估完成：成功 {state['done']} · 失败 {state['failed']}"
                f" · 跳过 {state['skipped']}"
            )
    finally:
        state["running"] = False
        state["current_factor_id"] = None
        state["current_factor_name"] = None
        state["finished_at"] = utc_now().isoformat()


# ── 层序归位（2026-10-07）─────────────────────────────────────────────
# 因子产出与价格数据的 MultiIndex **层序相反**：
#   · 挖掘侧（RD-Agent 因子代码）：(datetime, instrument)——因子代码里自己断言
#     MultiIndex 并要求 set_index(['datetime','instrument'])；
#   · Qlib D.features()：(instrument, datetime)。
# 旧实现在拿到因子结果后写 `s.index.names = ["instrument", "datetime"]`：**只改名、
# 不换层**，名字与值自此不符；而 Index.intersection 比对的是**值**（元组），
# (日期, 标的) 去撞 (标的, 日期) 交集恒为 0，最终报「因子与价格对齐后数据不足
# (共 0 行)」——层序问题却伪装成了数据不足。
#
# 此后一律**按值**判定哪一层是日期（不看 names，因为 names 本身可能就是被上游
# 改错的），统一归位到挖掘侧层序 (datetime, instrument) 再对齐。
CANONICAL_INDEX_NAMES = ["datetime", "instrument"]


def _detect_datetime_level(index) -> int:
    """按**值**判定 MultiIndex 中哪一层是日期；判定不了就抛错，不猜。"""
    import pandas as _pd

    if not isinstance(index, _pd.MultiIndex) or index.nlevels < 2:
        raise ValueError("因子产出索引必须是 MultiIndex(datetime, instrument)")

    # 先看 dtype（快路径）
    dtype_hits = [
        level
        for level in range(index.nlevels)
        if _pd.api.types.is_datetime64_any_dtype(index.get_level_values(level))
    ]
    if len(dtype_hits) == 1:
        return dtype_hits[0]

    # dtype 判定不出来（object 里装 Timestamp）时取样本看实际类型
    for level in range(index.nlevels):
        sample = index.get_level_values(level)[:8]
        if len(sample) and all(isinstance(v, _pd.Timestamp) for v in sample):
            return level

    raise ValueError(f"无法从索引层 {list(index.names)} 判定哪一层是日期")


def _canonicalize_multiindex(series):
    """把 (datetime, instrument) 两层索引的 Series 归位成挖掘侧层序并排序。

    只重排索引层级与行序，**不动值**——值必须跟着自己那行走。
    """
    import pandas as _pd

    if not isinstance(series.index, _pd.MultiIndex) or series.index.nlevels != 2:
        raise ValueError(
            f"需要 2 层 MultiIndex(datetime, instrument)，实际 "
            f"{getattr(series.index, 'nlevels', 1)} 层"
        )

    dt_level = _detect_datetime_level(series.index)
    out = series
    if dt_level != 0:
        out = out.reorder_levels([dt_level, 1 - dt_level])
    out = out.copy()
    out.index = out.index.set_names(CANONICAL_INDEX_NAMES)
    return out.sort_index()


def _canonicalize_factor_series(result):
    """因子计算结果 → 索引恒为 (datetime, instrument) 的 Series。"""
    import pandas as _pd

    if isinstance(result, _pd.Series):
        s = result
    elif isinstance(result.index, _pd.MultiIndex) and result.index.nlevels >= 2:
        s = result.iloc[:, 0]
    else:
        s = result.stack()
    return _canonicalize_multiindex(s)


def _upper_instrument_level(series):
    """把 instrument 层统一成大写。

    **这是挖掘侧的既有契约，不是回测新发明的**：``scripts/alpha_agent/run_rd_agent.py``
    在算 IC 前对因子产出与收益**两侧**做同一件事，原文注释——
    「因子代码可能假设大写（SH600036），而 daily_pv.h5 用小写（sh600036），不统一
    会导致对齐交集为空、IC 无法计算」。所以「因子把代码大写」在挖掘侧是被容许的、
    有据可依的写法，不是因子作者的笔误。

    回测若不做这一步，同一个因子就会在挖掘阶段算出 IC、在回测阶段撞空——这正是
    「回测要喂挖掘同源数据」要补齐的那条缝。两边都转，且是幂等的，不会把原本
    对齐的数据弄丢（值不动，只动标签）。
    """
    names = list(series.index.names)
    if "instrument" not in names:
        return series
    level = names.index("instrument")
    levels = series.index.levels[level]
    if levels.dtype != object:
        return series
    out = series.copy()
    out.index = out.index.set_levels(levels.str.upper(), level=level)
    return out


def _index_fingerprint(series, canonicalize) -> str:
    """索引指纹：层名 + 行数 + 样本，用来一眼看出两边差在哪。

    对齐结果行数极少时，成因不止层序一种——因子代码自己把标的代码大写
    （``.str.upper()``，注释还写着「以匹配 Qlib」）同样会撞空。只报「数据不足」
    的话这两种成因长得一模一样，所以样本值必须抬出来。
    """
    try:
        index = canonicalize(series).index
    except Exception:  # noqa: BLE001 - 指纹只是诊断信息，取不到就退回原始索引
        index = series.index
    return (
        f"{list(index.names)} 共 {len(index)} 行，样例 {[tuple(v) for v in index[:3]]}"
    )


def _alignment_failure_message(factor_series, close, aligned_rows: int) -> str:
    """对齐行数极少时的报错正文：两边的索引指纹一起给出。"""
    return (
        f"因子与价格对齐后数据不足 (共 {aligned_rows} 行)；"
        f"因子侧 {_index_fingerprint(factor_series, _canonicalize_factor_for_alignment)}；"
        f"价格侧 {_index_fingerprint(close, _canonicalize_price_for_alignment)}"
    )


def _forward_return(close, periods: int = 1):
    """前瞻收益：按 **instrument 组内**前移 ``periods`` 个交易日（默认次日）。

    两个坑都在这一行里：
    · 分组键必须**按名字**取。层序归位后 level 0 是 datetime，`groupby(level=0)`
      会变成按日期分组（每天每只票一组，pct_change 恒为 NaN）；
    · shift 必须落在 **groupby 之内**。`groupby(level=0).pct_change().shift(-1)`
      的 shift 在 groupby 之外，是整表位移，上一只股票的末日会拿到下一只的首日收益。
    ``periods>1`` 供 T-MV-09 多视界衰减复用同一实现（同规整、同分组，h 日收益）；
    h=1 的既有调用方行为逐位不变。
    """
    import pandas as _pd

    if "instrument" not in close.index.names:
        raise ValueError(f"价格索引缺少 instrument 层：{list(close.index.names)}")
    ordered = _canonicalize_multiindex(close)
    grouped = ordered.groupby(level="instrument", sort=False)
    return grouped.shift(-int(periods)) / ordered - 1.0


def _canonicalize_factor_for_alignment(result):
    """因子产出 → 对齐口径：层序 (datetime, instrument) + instrument 大写。"""
    return _upper_instrument_level(_canonicalize_factor_series(result))


def _canonicalize_price_for_alignment(close):
    """价格 → 对齐口径（与因子侧同一套规整，缺一不可）。"""
    return _upper_instrument_level(_canonicalize_multiindex(close))


def _align_factor_returns(factor, close):
    """把因子产出与价格对齐到同一个 (datetime, instrument) 索引。

    两边都要走完**同一套**规整：先归位层序，再统一 instrument 大小写。
    任何一步只做一边，交集都是 0——层序错位与大小写不一致都会伪装成「数据不足」。
    """
    f_all = _canonicalize_factor_for_alignment(factor)
    r_all = _upper_instrument_level(_forward_return(close))
    common = f_all.index.intersection(r_all.index)
    return f_all.loc[common], r_all.loc[common]


async def _backtest_via_qlib(
    factor_id: str,
    factor_code: str,
    kind: str,
    market: str,
    market_upper: str,
    universe: str,
    start: str,
    end: str,
    run_id: str | None = None,
) -> None:
    """Qlib 二进制回测（默认路径，所有 5 个市场支持）。"""
    import numpy as np
    import pandas as pd
    import qlib
    from qlib.data import D
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    provider_uri = resolve_qlib_provider_uri(market_upper)
    region = "cn" if market_upper in ("CN", "HK", "FUTURES", "CRYPTO") else "us"
    # 幂等 init：qlib.init 多次调用是安全的，第二次会快速返回
    try:
        qlib.init(provider_uri=provider_uri, region=region)
    except Exception as e:
        logger.warning("qlib.init(%s) raised: %s", provider_uri, e)

    instruments = _resolve_instruments_for_universe(market_upper, universe)
    fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
    # D.features 是同步阻塞调用（内部 joblib 多进程读 bin，冷启动数十秒）：
    # 直接压在事件循环上会把 /health 拖过看门狗阈值（30s×3 连败）强杀 engine——
    # 批量回测连续执行时必现（2026-10-09 补码评估批次实测：跑到第 4 条被重启）。
    # 放线程池执行，事件循环期间可继续响应健康检查与其他请求。
    df = await asyncio.to_thread(
        D.features, instruments, fields, start_time=start, end_time=end, freq="day"
    )
    if df.empty:
        raise RuntimeError(
            f"Qlib 数据为空: market={market}, instruments={instruments}, provider_uri={provider_uri}"
        )

    logger.info(
        "[alpha-backtest] %s market=%s universe=%s rows=%d cols=%s",
        factor_id,
        market,
        universe,
        len(df),
        list(df.columns),
    )

    # 计算因子值（LLM 生成/用户提交的代码一律 subprocess 隔离执行，
    # 严禁在 engine 主进程 exec——主进程持有 DB 凭证与全部服务状态）
    if kind == "expression":
        # 统计派 qlib 表达式：子进程里 qlib 原生求值（必须子进程的理由见被调函数
        # docstring——qlib 求值实现是主进程内 eval，同样受「严禁主进程 exec」约束）
        factor_series = await _run_expression_factor_subprocess(
            factor_id, factor_code, provider_uri, region, instruments, start, end
        )
    elif kind == "functional":
        # RD-Agent calculate_* 函数式：用 subprocess 跑（隔离 + 捕获 traceback）。
        # 优先喂**挖掘侧同源**的富化数据（39 列）：因子代码是照着挖掘时的列写的，
        # 只给它 6 列量价，读到 $netflow_5 这类富化列就是 KeyError。
        # 回测窗口从 Qlib 特征索引取（与因子产出对齐时的同一份窗口），
        # 用于挑选覆盖该窗口的挖掘数据面缓存
        _dt_level = _detect_datetime_level(df.index)
        _dts = df.index.get_level_values(_dt_level)
        mining_source = _resolve_mining_source_h5(market, (_dts.min(), _dts.max()))
        if mining_source:
            logger.info(
                "[alpha-backtest] %s 使用挖掘同源富化数据: %s", factor_id, mining_source
            )
        else:
            logger.info(
                "[alpha-backtest] %s 无挖掘同源富化数据，回退 Qlib 现拼 6 列（因子若用富化列会缺列）",
                factor_id,
            )
        factor_series = await _run_functional_factor_subprocess(
            factor_id, factor_code, df, source_h5=mining_source
        )
    else:
        # Qlib Factor 类：subprocess 逐股计算，主进程读结果 H5
        factor_series = await _run_factor_class_subprocess(factor_id, factor_code, df)

    if factor_series is None or len(factor_series) == 0:
        raise RuntimeError("因子计算无输出，请检查 calculate_* 函数或 Factor 类")

    # 对齐：因子产出是挖掘侧层序 (datetime, instrument)，Qlib 价格是
    # (instrument, datetime)。**两边都**归位层序后再取交集——只归位一边，
    # 交集照样是 0（正是 2026-10-07 那次「数据不足 (共 0 行)」的成因）。
    close = df["$close"]
    f, r = _align_factor_returns(factor_series, close)
    if len(f) < 100:
        # 这个错误以前只说「数据不足」，而**层序错位**和**标的代码大小写不一致**
        # 都会表现成对齐后行数极少——同一句话盖住了两种完全不同的成因，
        # 真因因此被排查漏过（2026-10-07）。所以两边的层名、行数、样本值一起抬出来。
        raise RuntimeError(_alignment_failure_message(factor_series, close, len(f)))
    mask = np.isfinite(f.values) & np.isfinite(r.values)
    if mask.sum() < 100:
        raise RuntimeError("清洗后有效数据 < 100 行")

    f_clean = pd.Series(f.values[mask], index=f.index[mask])
    r_clean = pd.Series(r.values[mask], index=r.index[mask])

    # 向量化 IC
    ic_mean, rank_ic_median, icir, rank_icir, n_obs = _vectorized_daily_spearman_ic(
        f_clean, r_clean
    )
    if n_obs == 0:
        raise RuntimeError("日度 IC 全部为 NaN，因子可能与价格列不匹配")

    # Sharpe / Annual Return / Max Drawdown: 简单 long-top30% 组合
    try:
        df_pair = pd.DataFrame({"f": f_clean, "r": r_clean})
        df_pair["date"] = df_pair.index.get_level_values("datetime")
        df_pair["f_rank"] = df_pair.groupby("date")["f"].rank(pct=True)
        # long top 30% 每日收益均值
        longs = df_pair[df_pair["f_rank"] >= 0.7].groupby("date")["r"].mean().dropna()
        if len(longs) > 1:
            daily_ret = longs
            ann_ret = float(daily_ret.mean() * 252)
            sharpe = float(
                daily_ret.mean() / (daily_ret.std(ddof=1) + 1e-8) * np.sqrt(252)
            )
            cum = (1 + daily_ret).cumprod()
            peak = cum.cummax()
            dd = (peak - cum) / peak
            max_dd = float(dd.max()) if len(dd) else None
        else:
            ann_ret = sharpe = max_dd = None
    except Exception:
        ann_ret = sharpe = max_dd = None

    # 质量闸门：扰动保真度（PFS）——同一批因子值上直接算，标注进 metadata，
    # 因子列表/详情原样返回（前端可据此过滤；低于阈值时导出会带质量警告）
    pfs_quality = _compute_pfs_quality(
        pd.DataFrame(
            {
                "trade_date": f_clean.index.get_level_values("datetime"),
                "symbol": f_clean.index.get_level_values("instrument"),
                "factor": np.asarray(f_clean, dtype=float),
            }
        )
    )

    # 机构级评估器链（mining_plugins）：RRE / 换手 / 扣成本，新增键不改毛指标
    eval_metrics = _run_mining_evaluators(
        f_clean, r_clean, market=market, universe=universe, factor_id=factor_id
    )

    # 因子行 metadata 与历史台账 metrics_json 同一份（手抄两份必漂移：评估器加键漏一边）
    metrics_payload = {
        "data_source": "qlib_bin",
        "market": market,
        "icir": icir,
        "rank_icir": rank_icir,
        "n_obs": n_obs,
        **({"quality": pfs_quality} if pfs_quality else {}),
        **eval_metrics,
    }

    # 历史台账先收口、再翻因子行终态：前端轮询到终态后立刻拉历史，必然能看到
    # 刚收口的这一行（反序则存在「因子行已终态、台账尚未收口」的粘性窗口）。
    await _record_backtest_finish(
        run_id,
        "completed",
        ic_value=ic_mean,
        rank_ic=rank_ic_median,
        icir=icir,
        rank_icir=rank_icir,
        sharpe_ratio=sharpe,
        annual_return=ann_ret,
        max_drawdown=max_dd,
        universe=universe,
        data_source="qlib_bin",
        date_range=f"{start}~{end}",
        metrics=metrics_payload,
    )

    await persistence.update_factor_metrics(
        factor_id,
        status="completed",
        ic_value=ic_mean,
        rank_ic=rank_ic_median,
        sharpe_ratio=sharpe,
        annual_return=ann_ret,
        max_drawdown=max_dd,
        universe=universe,
        date_range=f"{start}~{end}",
        metadata=metrics_payload,
    )

    # 因子池登记（P1）：面板落盘（进程内有 f_clean，恰好是最全的一份）+ 池行 +
    # 公式/task 边。record_backtested_factor 自身吞异常，这里再兜一层 import 失败——
    # 池是增益层，任何情况下不拖挂回测。
    try:
        from backend.services.engine.mining_plugins import pool_service

        await pool_service.record_backtested_factor(
            factor_id,
            market=market,
            universe=universe,
            values=f_clean,
            forward_return=r_clean,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[alpha-backtest] 因子池登记失败（不拦回测）%s: %s", factor_id, exc
        )

    logger.info(
        "[alpha-backtest] %s done market=%s ic=%.4f rank_ic=%.4f icir=%.4f sharpe=%s ann_ret=%s max_dd=%s pfs=%s rre=%s ann_to=%s net_ret=%s n=%d",
        factor_id,
        market,
        ic_mean,
        rank_ic_median,
        icir,
        f"{sharpe:.3f}" if sharpe is not None else "N/A",
        f"{ann_ret:.3f}" if ann_ret is not None else "N/A",
        f"{max_dd:.3f}" if max_dd is not None else "N/A",
        pfs_quality["pfs"] if pfs_quality else "N/A",
        f"{eval_metrics['rre']:.4f}" if "rre" in eval_metrics else "N/A",
        f"{eval_metrics['ann_turnover']:.2f}"
        if "ann_turnover" in eval_metrics
        else "N/A",
        f"{eval_metrics['ann_return_net']:.3f}"
        if "ann_return_net" in eval_metrics
        else "N/A",
        n_obs,
    )


_MINING_H5_KEY = "data"


_MINING_H5_KEYED_RE = re.compile(
    r"^daily_pv_all__(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})__[a-z0-9+]+\.h5$"
)


def _mining_h5_candidates(cache_dir: str) -> list[tuple[str, str, str]]:
    """已存在的挖掘数据面缓存 → [(path, 覆盖起, 覆盖止)]。

    两类：默认缓存（``daily_pv_all.h5``，生成器默认跨度）与自定义窗口/数据集
    的任务缓存（``daily_pv_all__<起>_<止>__<数据集短码>.h5``，跨度即文件名）。
    文件名不可解析的一律忽略——宁可回退旧路径，不拿认不出跨度的文件赌。
    """
    out: list[tuple[str, str, str]] = []
    try:
        from backend.services.engine.rd_agent.rd_loop_wrapper import (
            _H5_DEFAULT_END,
            _H5_DEFAULT_START,
        )

        default = os.path.join(cache_dir, "daily_pv_all.h5")
        if os.path.exists(default):
            out.append(
                (default, _H5_DEFAULT_START.isoformat(), _H5_DEFAULT_END.isoformat())
            )
    except Exception:  # noqa: BLE001 - 常量导入失败即放弃默认缓存候选
        pass
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return out
    for name in names:
        m = _MINING_H5_KEYED_RE.match(name)
        if not m:
            continue
        path = os.path.join(cache_dir, name)
        if os.path.exists(path):
            out.append((path, m.group(1), m.group(2)))
    return out


def _resolve_mining_source_h5(
    market: str, window: tuple[Any, Any] | None = None
) -> str | None:
    """挖掘侧喂给 RD-Agent 的那份富化 h5；没有就返回 None。

    挖掘走 ``RDLoopWrapper._generate_h5_from_parquet`` →
    ``<quantdb_dir>/.h5_cache/``（契约：MultiIndex[datetime, instrument] +
    $open/$high/$low/$close/$volume/$amount/$factor 再拼 QuantDB 富化列）。回测
    此前从 Qlib 二进制现拼 6 列，富化列全缺，因子一读到 ``$netflow_5`` 就是
    KeyError。

    ``window`` = 本次回测的 (起, 止)（可选）：给定时只挑**能覆盖该窗口**的缓存，
    否则 ``_write_mining_input`` 会筛出 0 行而报「筛不出任何行」。默认缓存优先
    （列最全），其次窗口化缓存按 mtime 新→旧。

    只认**已存在**的缓存：生成一份要读全市场日线（分钟级到 40 分钟），回测绝不
    自己触发；没有就返回 None，由调用方退回旧路径（HK/US 等市场本就没有这份缓存）。
    """
    if _MARKET_TO_QLIB.get(market) != "CN":
        return None
    try:
        # 懒 import：rd_loop_wrapper 会拉进 rdagent，别压在模块导入期
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        quantdb_dir = RDLoopWrapper._resolve_quantdb_dir()
    except Exception as exc:  # noqa: BLE001 - 定位缓存失败不该让回测挂掉
        logger.warning(
            "[alpha-backtest] 解析 QuantDB 目录失败，回退 Qlib 现拼: %s", exc
        )
        return None
    if not quantdb_dir:
        return None
    candidates = _mining_h5_candidates(os.path.join(quantdb_dir, ".h5_cache"))
    if not candidates:
        return None
    if window is not None:
        import pandas as _pd

        lo = _pd.Timestamp(window[0]).normalize()
        hi = _pd.Timestamp(window[1]).normalize()
        covered = [
            c
            for c in candidates
            if _pd.Timestamp(c[1]) <= lo and _pd.Timestamp(c[2]) >= hi
        ]
        if not covered:
            logger.warning(
                "[alpha-backtest] %d 份挖掘数据面缓存均不覆盖回测窗口 %s~%s",
                len(candidates),
                lo.date(),
                hi.date(),
            )
            return None
        candidates = covered
    default_name = "daily_pv_all.h5"
    candidates.sort(
        key=lambda c: (
            os.path.basename(c[0]) != default_name,
            -os.path.getmtime(c[0]),
        )
    )
    return candidates[0][0]


def _write_mining_input(src_h5: str, dest_h5: str, df) -> None:
    """把挖掘侧富化 h5 切成回测窗口/股票池，写成子进程能相对读的 daily_pv.h5。

    ``df`` 是 Qlib ``D.features()`` 取的那份（只用它拿股票池与日期窗口）。

    不把 1.5GB 全量丢给因子代码：那份要读全市场全历史，单次回测既慢又吃内存。
    **列的契约与挖掘逐字一致**（39 列、$ 前缀、(datetime, instrument)），只是范围
    收窄——因子代码看到的仍是它挖掘时看到的那套列。
    """
    import pandas as _pd

    dt_level = _detect_datetime_level(df.index)
    inst_level = 1 - dt_level
    wanted = set(df.index.get_level_values(inst_level).unique())
    lo = _pd.Timestamp(df.index.get_level_values(dt_level).min())
    hi = _pd.Timestamp(df.index.get_level_values(dt_level).max())

    # HDF 层切片只是省 I/O 的优化，**不是**过滤的保证：索引非单调或未建
    # data_columns 时会抛错走全量读，那时若不再自己过滤，窗口就静默失效了。
    # 所以下面无条件再按日期/股票池裁一次，切片成不成功结果都一样。
    try:
        raw = _pd.read_hdf(src_h5, key=_MINING_H5_KEY, start=lo, stop=hi)
    except (TypeError, ValueError):
        raw = _pd.read_hdf(src_h5, key=_MINING_H5_KEY)

    raw_dt_level = _detect_datetime_level(raw.index)
    raw_dates = raw.index.get_level_values(raw_dt_level)
    raw = raw[(raw_dates >= lo) & (raw_dates <= hi)]
    if "instrument" in raw.index.names:
        raw = raw[raw.index.get_level_values("instrument").isin(wanted)]
    elif raw.index.nlevels == 2:
        raw = raw[raw.index.get_level_values(1 - raw_dt_level).isin(wanted)]

    if raw.empty:
        raise RuntimeError(
            f"挖掘同源数据在 {lo.date()}~{hi.date()} / {len(wanted)} 只标的上筛不出任何行"
        )
    raw.to_hdf(dest_h5, key=_MINING_H5_KEY, mode="w")


async def _run_functional_factor_subprocess(
    factor_id: str, factor_code: str, df: "pd.DataFrame", source_h5: str | None = None
) -> "pd.Series | None":
    """对 RD-Agent 函数式因子：用 subprocess 跑（隔离错误），主进程读 result.h5。

    **因子有两种入口样式，与挖掘侧同一套契约、同一优先级**（见
    ``scripts/alpha_agent/run_rd_agent.py``）：

    1. **自执行式**——带 ``main()`` 与 ``__main__`` 守卫，自己读 ``daily_pv.h5``、
       写 ``result.h5``。**优先**：能自执行就自执行，不碰 ``calculate_*``。
    2. **零参函数式**——无守卫，靠显式调 ``calculate_*()`` 并用返回值。
       仅在样式 1 没产出任何结果文件时才走这条。

    顺序不可颠倒：样式 1 的 ``calculate_*(data)`` 需要一个由 ``main()`` 注入的实参，
    无条件先调它必然 ``TypeError``，而结果其实已经写好了。

    ``source_h5`` 给了就写**挖掘侧同源**的富化数据（39 列，层序 (datetime,
    instrument)）；没给才退回「从 Qlib 二进制现拼 6 列」的老路。

    输入文件放在**每次运行独立的临时目录**里（不再是全局 /tmp/daily_pv.h5）：
    并发回测此前会互相覆盖这个文件，而因子代码是按相对路径读它的。
    """
    import sys as _sys
    import shutil
    import tempfile
    from pathlib import Path

    import pandas as _pd  # 只在回退分支的层序归位用（模块级刻意不引 pandas）

    run_dir = tempfile.mkdtemp(prefix=f"bt_{factor_id[:8]}_")
    h5_path = os.path.join(run_dir, "daily_pv.h5")
    try:
        if source_h5:
            _write_mining_input(source_h5, h5_path, df)
        else:
            # 回退路径：把 Qlib 拉的 df 写成 H5 给 subprocess 读。
            # Qlib D.features() 的列名带 "$" 前缀 (如 $close, $volume)，
            # 但 RD-Agent 因子代码通常用不带前缀的列名 (如 close, volume)。
            # 同时写入两组列名，兼容两种命名约定。
            df_out = df.copy()
            for col in list(df_out.columns):
                if col.startswith("$"):
                    plain = col[1:]
                    if plain not in df_out.columns:
                        df_out[plain] = df_out[col]
            # 层序归位：D.features 给的是 (instrument, datetime)，挖掘契约是
            # (datetime, instrument)。挖掘出来的因子按**位置**取 level1 当
            # instrument（pv_sync_10 的 ``get_level_values(1).str.upper()``），
            # 层序不归位时拿到 datetime64，``.str`` 直接在它上面炸——非 CN 市场
            # 全走本分支，2026-10-09 美股首跑实测 AttributeError（CN 走挖掘 h5，
            # 层序本来就是对的不受影响）。层序按**值**判（_detect_datetime_level），
            # instrument 层统一转 str（长度相等，不引入缺列）。
            if isinstance(df_out.index, _pd.MultiIndex) and df_out.index.nlevels == 2:
                dt_i = _detect_datetime_level(df_out.index)
                inst_i = 1 - dt_i
                df_out.index = _pd.MultiIndex.from_arrays(
                    [
                        df_out.index.get_level_values(dt_i),
                        df_out.index.get_level_values(inst_i).astype(str),
                    ],
                    names=["datetime", "instrument"],
                )
            df_out.to_hdf(h5_path, key="data", mode="w")
    except Exception as e:
        raise RuntimeError(f"准备 daily_pv.h5 失败: {e}") from e

    # 三个中间文件也收进 per-run 目录：写死在 /tmp 时，并发回测会互相覆盖，
    # 表现为「A 的回测读到了 B 的因子结果」这种极难复现的错。
    tb_path = os.path.join(run_dir, "_bt_tb.txt")
    Path(tb_path).unlink(missing_ok=True)
    out_path = os.path.join(run_dir, "_bt_result.h5")
    Path(out_path).unlink(missing_ok=True)
    Path(os.path.join(run_dir, "result.h5")).unlink(missing_ok=True)

    script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback, shutil

os.chdir({run_dir!r})
TB = {tb_path!r}
OUT = {out_path!r}
try:
    # __name__ 必须显式给成 '__main__'。挖掘侧对因子有**两种**入口样式，且**优先
    # 自执行**（run_rd_agent.py 原注释：「若因子代码未自执行（无 __main__ 守卫）或
    # 未产出 result.h5，则显式调用 calculate_*()」）。子进程 exec 的命名空间里没有
    # __name__，守卫取到的是 'builtins' 而不是 '__main__'，main() 根本不触发，
    # 于是自执行因子统统掉进下面的零参调用——线上 6 个因子卡在这（2026-10-07）。
    _factor_ns = {{"__name__": "__main__"}}
    exec({repr(factor_code)}, _factor_ns)

    def _produced_h5():
        # 因子自己写出来的结果；daily_pv.h5 是输入，_bt* 是本脚本的中间文件。
        return [f for f in os.listdir('.')
                if f.endswith('.h5') and f != 'daily_pv.h5' and not f.startswith('_bt')]

    # 先看自执行有没有产出；**没产出才**退回显式调用 calculate_*()。
    # 顺序反过来就是错的：自执行因子的 calculate_*(data) 需要一个由 main() 注入的
    # 实参，无条件先调它必然 TypeError，而结果其实已经躺在 result.h5 里了。
    if not _produced_h5():
        _calc_fns = [v for k, v in _factor_ns.items() if k.startswith("calculate_") and callable(v)]
        if _calc_fns:
            _result = _calc_fns[0]()
            if _result is not None and hasattr(_result, 'to_hdf'):
                _result.to_hdf(OUT, key='data', mode='w')

    if not os.path.exists(OUT):
        # 兜底：因子自己写的那个 h5（无论来自自执行还是函数返回）
        _cands = _produced_h5()
        if _cands:
            shutil.move(_cands[0], OUT)
        else:
            print("NO_RESULT_FILE"); sys.exit(1)
    print("FACTOR_DONE")
except Exception as e:
    with open(TB, "w") as f:
        traceback.print_exc(file=f)
    print(f"ERROR: {{e}}")
    sys.exit(1)
"""
    returncode, stdout, stderr = await _run_subprocess_tracked(
        factor_id, [_sys.executable, "-c", script], timeout=600
    )
    if returncode != 0:
        tb = Path(tb_path).read_text() if Path(tb_path).exists() else ""
        out = stdout + "\n" + stderr
        raise RuntimeError(
            f"因子执行失败 (exit={returncode}): {out[-300:]}\n{tb[-1000:]}"
        )

    # 读 result.h5
    try:
        import pandas as _pd

        result_df = _pd.read_hdf(out_path)
    except Exception as e:
        raise RuntimeError(f"读取 result.h5 失败: {e}") from e

    # 归位成 (datetime, instrument)：因子代码自己 set_index(['datetime','instrument'])，
    # 旧实现却按下标改名成 ["instrument","datetime"]——名字与值不符，交集恒 0 行。
    return _canonicalize_factor_series(result_df)


async def _run_factor_class_subprocess(
    factor_id: str, factor_code: str, df: "pd.DataFrame"
) -> "pd.Series | None":
    """Qlib Factor 类因子：subprocess 隔离执行（逐股调用），主进程只读结果 H5。

    因子代码由 LLM 生成或用户提交，绝不能在 engine 主进程 exec。
    每次运行用独立的临时文件，避免并发回测相互覆盖。
    """
    import uuid

    run_id = uuid.uuid4().hex[:8]
    input_path = f"/tmp/_factor_input_{run_id}.h5"
    out_path = f"/tmp/_factor_result_{run_id}.h5"
    tb_path = f"/tmp/_factor_tb_{run_id}.txt"
    Path(tb_path).unlink(missing_ok=True)
    Path(out_path).unlink(missing_ok=True)

    try:
        import pandas as _pd

        df.to_hdf(input_path, key="data", mode="w")
    except Exception as e:
        raise RuntimeError(f"准备因子输入数据失败: {e}") from e

    script = f"""
import pandas as pd
import numpy as np
import sys, os, traceback

try:
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _factor_cls = None
    for _v in _factor_ns.values():
        if isinstance(_v, type) and _v.__module__ == "builtins":
            if getattr(_v, "name", None) or _v.__name__.lower().endswith("factor"):
                _factor_cls = _v
                break
    if _factor_cls is None:
        print("NO_FACTOR_CLASS"); sys.exit(1)
    _df = pd.read_hdf({input_path!r})
    _factor_inst = _factor_cls()
    _pieces = []
    for _code, _sub in _df.groupby(level=0):
        if len(_sub) < 30:
            continue
        try:
            _fv = _factor_inst(_sub.copy())
            _fv_col = _fv.iloc[:, 0] if hasattr(_fv, "iloc") else pd.Series(_fv)
            _pieces.append(pd.Series(_fv_col.values, index=_sub.index, name="f"))
        except Exception:
            continue
    if not _pieces:
        print("NO_PIECES"); sys.exit(1)
    pd.concat(_pieces).to_hdf({out_path!r}, key="data", mode="w")
    print("FACTOR_DONE")
except Exception:
    with open({tb_path!r}, "w") as _f:
        traceback.print_exc(file=_f)
    sys.exit(1)
"""
    returncode, stdout, stderr = await _run_subprocess_tracked(
        factor_id, [sys.executable, "-c", script], timeout=600
    )
    if returncode != 0:
        tb = Path(tb_path).read_text() if Path(tb_path).exists() else ""
        raise RuntimeError(
            f"因子执行失败 (exit={returncode}): {((stdout or '') + stderr)[-300:]}\n{tb[-1000:]}"
        )

    try:
        import pandas as _pd

        result_df = _pd.read_hdf(out_path)
    except Exception as e:
        raise RuntimeError(f"读取因子结果失败: {e}") from e

    if isinstance(result_df, _pd.Series):
        s = result_df
    elif isinstance(result_df.index, _pd.MultiIndex) and result_df.index.nlevels >= 2:
        s = result_df.iloc[:, 0]
    else:
        s = result_df.stack()
    # 同上：层序按值判定并归位，绝不按下标改名。
    return _canonicalize_multiindex(s)


async def _run_expression_factor_subprocess(
    factor_id: str,
    expression: str,
    provider_uri: str,
    region: str,
    instruments,
    start: str,
    end: str,
) -> "pd.Series | None":
    """qlib 表达式因子（统计派）：subprocess 内 qlib 原生求值，主进程只读结果。

    **为什么必须子进程**：qlib 对表达式的求值实现是 ``eval(parse_field(field))``
    （容器内实测 ``qlib/data/data.py``：直接 eval，无 ``__builtins__`` 限制）。
    在主进程调用 ``D.features(fields=[表达式])`` 等于把因子文本当 Python 执行，
    与「因子代码一律 subprocess 隔离，严禁在 engine 主进程 exec」是同一条纪律
    ——表达式只是换了种语言，不是豁免理由。

    求值走 qlib 原生路径，与落库表达式/等价性金样（test_statistical_candidates）
    同一实现：算子窗口/暖机语义由 qlib 自己保证，回测与挖掘不会两说。窗口头部
    由 qlib 自动扩展历史（``get_extended_window_size``）补齐暖机，不依赖调用方
    多取数据。

    ``instruments`` 原样透传主进程解析出的股票池（list 或 qlib config dict），
    两边取数口径必须一致——在子进程里重解析会出现「回测的池子与价格面不同」。

    **两类 dialect**（按 ``fm:`` 前缀分派，求值器不同、隔离纪律相同）：

    - 裸 qlib 表达式（统计派）：qlib 原生求值（上面一段）；
    - ``fm:`` FM-DSL 表达式（FactorMiner 论文落库形态）：qlib 表达式引擎
      没有截面算子（CsRank 类占 74/110），走 ``factorminer.runtime`` 的
      白名单求值器；取数/暖机/全网格语义与挖掘链路同一实现。
    """
    import uuid

    from backend.services.engine.alpha_agent.factorminer.dsl import (  # noqa: PLC0415
        FM_CODE_PREFIX,
    )

    run_id = uuid.uuid4().hex[:8]
    out_path = f"/tmp/_expr_result_{run_id}.h5"
    tb_path = f"/tmp/_expr_tb_{run_id}.txt"
    Path(tb_path).unlink(missing_ok=True)
    Path(out_path).unlink(missing_ok=True)

    if expression.startswith(FM_CODE_PREFIX):
        # FM-DSL：子进程里把仓库根塞进 sys.path（容器 /app == 宿主仓库根，
        # parents[4] 两边同解）；因子文本只进白名单解析器，不进 eval。
        repo_root = str(Path(__file__).resolve().parents[4])
        script = f"""
import sys, traceback
sys.path.insert(0, {repo_root!r})
try:
    from backend.services.engine.alpha_agent.factorminer.runtime import evaluate_window

    _series = evaluate_window(
        {expression!r},
        instruments={instruments!r},
        start={start!r},
        end={end!r},
        qlib_dir={provider_uri!r},
    )
    if _series is None or _series.empty:
        print("EMPTY_EXPRESSION_RESULT"); sys.exit(1)
    _series.to_frame("value").to_hdf({out_path!r}, key="data", mode="w")
    print("FACTOR_DONE")
except Exception as _e:
    with open({tb_path!r}, "w") as _f:
        traceback.print_exc(file=_f)
    print(f"ERROR: {{_e}}")
    sys.exit(1)
"""
    else:
        script = f"""
import sys, traceback
try:
    import qlib
    from qlib.data import D

    qlib.init(provider_uri={provider_uri!r}, region={region!r})
    _df = D.features(
        {instruments!r}, [{expression!r}],
        start_time={start!r}, end_time={end!r}, freq="day",
    )
    if _df is None or _df.empty:
        print("EMPTY_EXPRESSION_RESULT"); sys.exit(1)
    _df.to_hdf({out_path!r}, key="data", mode="w")
    print("FACTOR_DONE")
except Exception as _e:
    with open({tb_path!r}, "w") as _f:
        traceback.print_exc(file=_f)
    print(f"ERROR: {{_e}}")
    sys.exit(1)
"""
    returncode, stdout, stderr = await _run_subprocess_tracked(
        factor_id, [sys.executable, "-c", script], timeout=600
    )
    if returncode != 0:
        tb = Path(tb_path).read_text() if Path(tb_path).exists() else ""
        raise RuntimeError(
            f"表达式因子执行失败 (exit={returncode}): {((stdout or '') + stderr)[-300:]}\n{tb[-1000:]}"
        )

    try:
        import pandas as _pd

        result_df = _pd.read_hdf(out_path)
    except Exception as e:
        raise RuntimeError(f"读取表达式结果失败: {e}") from e

    # 层序归位与其他 kind 同一入口：D.features 给 (instrument, datetime)，
    # _canonicalize_factor_series 按**值**判日期层并归位成挖掘侧层序。
    return _canonicalize_factor_series(result_df)


async def _backtest_via_h5(
    factor_id: str,
    factor_code: str,
    kind: str,
    universe: str,
    start: str,
    end: str,
    run_id: str | None = None,
) -> None:
    """H5 路径（仅 A 股 / 美股 / 港股支持；其他市场回退）。"""
    h5_path = _resolve_factor_h5_path(universe)
    if not Path(h5_path).exists():
        raise RuntimeError(f"H5 数据文件不存在: {h5_path}，请改用 data_source=qlib_bin")
    # 复制到 /tmp 让因子代码能相对路径读
    import shutil

    tmp_h5 = "/tmp/daily_pv.h5"
    if (
        not Path(tmp_h5).exists()
        or Path(tmp_h5).stat().st_mtime < Path(h5_path).stat().st_mtime
    ):
        shutil.copy2(h5_path, tmp_h5)
    await _backtest_functional_factor(
        factor_id, factor_code, start, end, universe, run_id=run_id
    )


def _resolve_factor_h5_path_for_market(market: str) -> str | None:
    """按市场找 H5 文件；不存在返回 None。"""
    candidates = {
        "a_share": [
            "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
            "/app/db/cn_data/daily_pv.h5",
        ],
        "us_stock": ["/app/db/us_data/daily_pv.h5"],
        "hong_kong": ["/app/db/hk_data/daily_pv.h5"],
        "crypto": ["/app/db/crypto_data/5min_pv.h5"],
        "futures": [],  # H5 未生成
    }
    for p in candidates.get(market, []):
        if Path(p).exists():
            return p
    return None


async def _backtest_functional_factor(
    factor_id: str,
    factor_code: str,
    start_date: str | None,
    end_date: str | None,
    universe: str | None = "csi300",
    run_id: str | None = None,
) -> None:
    """回测 RD-Agent 函数式因子（calculate_* 返回 DataFrame，读 daily_pv.h5）。

    与 run_rd_agent.py.compute_factor_ic 同一套逻辑：subprocess 执行因子代码
    写 result.h5，再与价格数据对齐算 IC/RankIC/ICIR。
    """
    try:
        import tempfile
        import sys as _sys
        from pathlib import Path

        _default_start, _default_end = _default_backtest_window("a_share")
        start = start_date or _default_start
        end = end_date or _default_end

        # 市场 → H5 数据文件（因子代码读 daily_pv.h5，subprocess chdir 到 /tmp）
        data_path = _resolve_factor_h5_path(universe)
        # 复制到 /tmp/daily_pv.h5，因子代码用相对路径 daily_pv.h5 能读到
        import shutil

        tmp_h5 = "/tmp/daily_pv.h5"
        try:
            if Path(data_path).exists() and (
                not Path(tmp_h5).exists()
                or Path(tmp_h5).stat().st_mtime < Path(data_path).stat().st_mtime
            ):
                shutil.copy2(data_path, tmp_h5)
        except Exception:
            pass

        script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback

os.chdir(tempfile.gettempdir())
try:
    # 用 exec 定义因子函数（__name__ != __main__，不触发 main 块），再调用 calculate_* 执行
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _calc_fns = [v for k, v in _factor_ns.items() if k.startswith("calculate_") and callable(v)]
    if not _calc_fns:
        print("NO_CALC_FN"); sys.exit(1)
    _calc_fns[0]()
    result_files = [f for f in os.listdir('.') if f.endswith('.h5') and 'result' in f.lower()]
    if not result_files:
        result_files = [f for f in os.listdir('.') if f.endswith('.h5') and f != 'daily_pv.h5']
    if not result_files:
        print("NO_RESULT_FILE"); sys.exit(1)
    factor_df = pd.read_hdf(result_files[0])
    if factor_df.empty:
        print("EMPTY_FACTOR"); sys.exit(1)
    price_df = pd.read_hdf({repr(str(data_path))})
    # 切片到回测窗口（默认近一年，由调用方解析）
    _start = {start!r}
    _end = {end!r}
    if _start or _end:
        _di = price_df.index.get_level_values(0)
        if _start:
            price_df = price_df[_di >= pd.Timestamp(_start)]
            _di = price_df.index.get_level_values(0)
        if _end:
            price_df = price_df[_di <= pd.Timestamp(_end)]
        if price_df.empty:
            print("EMPTY_WINDOW"); sys.exit(1)
    if 'close' in price_df.columns.get_level_values(0):
        close = price_df['close']
    elif '$close' in price_df.columns.get_level_values(0):
        close = price_df['$close']
    else:
        close = price_df.iloc[:, 0]
    returns = close.groupby(level=1).pct_change().shift(-1)
    # 因子结果 → 对齐 (datetime, instrument)
    # factor_df 已是 MultiIndex(datetime,instrument) 单列 → 直接用；
    # 若单 index 则 stack 成 MultiIndex
    if isinstance(factor_df.index, pd.MultiIndex) and factor_df.index.nlevels >= 2:
        factor_values = factor_df.iloc[:, 0]
    else:
        factor_values = factor_df.stack()
    # 统一 index 名（若已有正确的 MultiIndex 则跳过，避免 "Length of names" 报错）
    try:
        factor_values.index.names = ['datetime', 'instrument']
    except Exception:
        pass
    returns.index.names = ['datetime', 'instrument']
    # 统一 instrument 大小写：因子代码可能输出大写（SH600036），而 daily_pv.h5 用小写
    # （sh600036）；不统一会导致交集为空。与挖掘阶段 compute_factor_ic 同口径。
    def _upper_instrument(_s):
        _names = list(_s.index.names)
        if 'instrument' in _names:
            _lvl = _names.index('instrument')
            _lvs = _s.index.levels[_lvl]
            if _lvs.dtype == object:
                _s.index = _s.index.set_levels(_lvs.str.upper(), level=_lvl)
        return _s

    factor_values = _upper_instrument(factor_values)
    returns = _upper_instrument(returns)
    common_idx = factor_values.index.intersection(returns.index)
    if len(common_idx) < 100:
        print("INSUFFICIENT_DATA"); sys.exit(1)
    f = factor_values.loc[common_idx]; r = returns.loc[common_idx]
    mask = np.isfinite(f) & np.isfinite(r)
    f = f[mask]; r = r[mask]
    if len(f) < 100:
        print("INSUFFICIENT_CLEAN_DATA"); sys.exit(1)
    # 日度 Spearman（秩的 Pearson，向量化；口径与挖掘阶段 compute_factor_ic 完全一致）
    df_ic = pd.DataFrame({{'f': f.values, 'r': r.values}})
    df_ic['dt'] = f.index.get_level_values(0)
    df_ic = df_ic[np.isfinite(df_ic['f']) & np.isfinite(df_ic['r'])]
    g = df_ic.groupby('dt')
    df_ic['fr'] = g['f'].rank(method='average')
    df_ic['rr'] = g['r'].rank(method='average')
    g = df_ic.groupby('dt')
    _means = g[['fr', 'rr']].transform('mean')
    df_ic['fc'] = df_ic['fr'] - _means['fr']
    df_ic['rc'] = df_ic['rr'] - _means['rr']
    df_ic['fcr'] = df_ic['fc'] * df_ic['rc']
    df_ic['fc2'] = df_ic['fc'] ** 2
    df_ic['rc2'] = df_ic['rc'] ** 2
    _sums = g[['fcr', 'fc2', 'rc2']].transform('sum')
    _counts = g['fcr'].transform('count')
    _n = (_counts - 1).clip(lower=1)
    _cov = _sums['fcr'] / _n
    _var_f = _sums['fc2'] / _n
    _var_r = _sums['rc2'] / _n
    _denom = np.sqrt(_var_f * _var_r)
    df_ic['corr'] = np.where(_denom > 1e-12, _cov / np.where(_denom > 1e-12, _denom, 1.0), np.nan)
    ic_by_day = g['corr'].first().dropna()
    ic_by_day = ic_by_day[np.isfinite(ic_by_day)]
    if len(ic_by_day) == 0:
        print("NO_IC_VALUES"); sys.exit(1)
    ic = float(ic_by_day.mean())
    rank_ic = float(ic_by_day.median())
    _std = float(ic_by_day.std(ddof=1)) if len(ic_by_day) > 1 else 0.0
    icir = ic / (_std + 1e-8)
    rank_icir = rank_ic / (_std + 1e-8)
    print("IC=%s" % ic); print("RANK_IC=%s" % rank_ic)
    print("ICIR=%s" % icir); print("RANK_ICIR=%s" % rank_icir)
    print("OBSERVATIONS=%s" % len(f))
    # 组合指标（与 Qlib 路径同口径：做多因子前 30% 的等权日收益）
    _pair = pd.DataFrame({{'f': f.values, 'r': r.values}})
    _pair['dt'] = f.index.get_level_values(0)
    _pair['symbol'] = f.index.get_level_values(1)
    _pair = _pair[np.isfinite(_pair['f']) & np.isfinite(_pair['r'])]
    _pair['fr'] = _pair.groupby('dt')['f'].rank(pct=True)
    _longs = _pair[_pair['fr'] >= 0.7].groupby('dt')['r'].mean().dropna()
    if len(_longs) > 1:
        _ann = float(_longs.mean() * 252)
        _sharpe = float(_longs.mean() / (_longs.std(ddof=1) + 1e-8) * np.sqrt(252))
        _cum = (1 + _longs).cumprod()
        _peak = _cum.cummax()
        _dd = (_peak - _cum) / _peak
        _mdd = float(_dd.max()) if len(_dd) else None
        print("ANN_RET=%s" % _ann); print("SHARPE=%s" % _sharpe); print("MAX_DD=%s" % _mdd)
    # 质量闸门：扰动保真度 PFS（与训练侧 data/factor_quality 同一实现；
    # 用 % 格式化避免与外层 f-string 的花括号冲突）
    try:
        from backend.shared.factor_quality import load_factor_quality
        _fq = load_factor_quality()
        if _fq is not None:
            _pfs_df = pd.DataFrame(
                list(zip(f.index.get_level_values(0), f.index.get_level_values(1),
                         np.asarray(f, dtype=float))),
                columns=["trade_date", "symbol", "factor"],
            )
            _q = _fq.compute_pfs(_pfs_df, ["factor"]).get("factor") or {{}}
            if _q.get("pfs") is not None:
                print("PFS=%.4f" % _q["pfs"])
                if _q.get("pfs_gauss") is not None:
                    print("PFS_GAUSS=%.4f" % _q["pfs_gauss"])
                if _q.get("pfs_t") is not None:
                    print("PFS_T=%.4f" % _q["pfs_t"])
                print("PFS_DAYS=%d" % int(_q.get("n_days") or 0))
    except Exception:
        pass
    # 机构级评估器链（mining_plugins）：RRE / 换手 / 扣成本 → EVAL_<key>= 协议
    # （与 Qlib 路径共用 evaluate_paired；本函数仅跑 A 股 h5，市场固定 a_share）
    try:
        from backend.services.engine.mining_plugins import evaluate_paired
        _eval_paired = pd.DataFrame({{"datetime": _pair["dt"], "symbol": _pair["symbol"],
                                      "factor": _pair["f"], "ret": _pair["r"]}})
        for _ek, _ev in sorted(evaluate_paired(
                _eval_paired, market="a_share", universe={universe!r},
                factor_id={factor_id!r}).items()):
            if _ev is not None:
                print("EVAL_%s=%s" % (_ek, _ev))
    except Exception as _ee:
        print("MiningEvalError: %s" % _ee, file=sys.stderr)
except Exception as e:
    print(f"ERROR: {{e}}")
    traceback.print_exc()
    sys.exit(1)
"""
        # subprocess 执行（登记句柄，支持 cancel kill；to_thread 避免阻塞事件循环）
        returncode, stdout, stderr = await _run_subprocess_tracked(
            factor_id, [_sys.executable, "-c", script], timeout=600
        )
        # 合并 stdout + stderr（因子脚本异常用 stderr 输出 traceback）
        out = stdout + "\n" + stderr

        def _metric(name: str) -> float | None:
            """从子进程输出解析 NAME=<float> 指标（缺失或非法返回 None）。"""
            prefix = f"{name}="
            for line in out.splitlines():
                if line.startswith(prefix):
                    try:
                        value = float(line.split("=", 1)[1])
                    except Exception:
                        return None
                    return value if value == value else None  # 过滤 NaN
            return None

        ic_mean = _metric("IC")
        rank_ic_mean = _metric("RANK_IC")
        icir = _metric("ICIR")
        rank_icir = _metric("RANK_ICIR")
        ann_ret = _metric("ANN_RET")
        sharpe = _metric("SHARPE")
        max_dd = _metric("MAX_DD")

        pfs_quality: dict = {}
        for line in out.splitlines():
            if line.startswith("PFS_GAUSS="):
                try:
                    pfs_quality["pfs_gauss"] = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS_T="):
                try:
                    pfs_quality["pfs_t"] = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS_DAYS="):
                try:
                    pfs_quality["n_days"] = int(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS="):
                try:
                    pfs_quality["pfs"] = float(line.split("=")[1])
                except Exception:
                    pass

        eval_metrics = _parse_eval_metrics(out)

        if ic_mean is None:
            raise RuntimeError(f"因子回测失败: {out[-500:]}")

        # 因子行 metadata 与历史台账 metrics_json 同一份（手抄两份必漂移）
        metrics_payload = {
            "data_source": "h5",
            "icir": icir,
            "rank_icir": rank_icir,
            **({"quality": pfs_quality} if pfs_quality else {}),
            **eval_metrics,
        }

        # 历史台账先收口、再翻因子行终态（与 qlib 路径同序：终态可见 ⇒ 台账已收口）
        await _record_backtest_finish(
            run_id,
            "completed",
            ic_value=ic_mean,
            rank_ic=rank_ic_mean,
            icir=icir,
            rank_icir=rank_icir,
            sharpe_ratio=sharpe,
            annual_return=ann_ret,
            max_drawdown=max_dd,
            universe=universe,
            data_source="h5",
            date_range=f"{start}~{end}",
            metrics=metrics_payload,
        )

        await persistence.update_factor_metrics(
            factor_id,
            status="completed",
            ic_value=ic_mean,
            rank_ic=rank_ic_mean,
            icir=icir,
            rank_icir=rank_icir,
            sharpe_ratio=sharpe,
            annual_return=ann_ret,
            max_drawdown=max_dd,
            universe=universe,
            date_range=f"{start}~{end}",
            metadata=metrics_payload,
        )
        # 因子池登记（P1）：h5 路径进程内无因子序列（值在子进程 result.h5 里），
        # values=None → 只建池行 + 公式/task 边，面板留待 mining_pool_rebuild --panels。
        # 池是增益层：record_backtested_factor 自身吞异常，这里再兜 import 失败。
        try:
            from backend.services.engine.mining_plugins import pool_service

            await pool_service.record_backtested_factor(
                factor_id, market="a_share", universe=universe, values=None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[alpha-backtest-fn] 因子池登记失败（不拦回测）%s: %s", factor_id, exc
            )
        logger.info(
            "[alpha-backtest-fn] %s done ic=%.4f rank_ic=%s icir=%s sharpe=%s ann_ret=%s max_dd=%s pfs=%s rre=%s ann_to=%s net_ret=%s",
            factor_id,
            ic_mean,
            f"{rank_ic_mean:.4f}" if rank_ic_mean is not None else "N/A",
            f"{icir:.4f}" if icir is not None else "N/A",
            f"{sharpe:.4f}" if sharpe is not None else "N/A",
            f"{ann_ret:.4f}" if ann_ret is not None else "N/A",
            f"{max_dd:.4f}" if max_dd is not None else "N/A",
            f"{pfs_quality['pfs']:.4f}"
            if pfs_quality.get("pfs") is not None
            else "N/A",
            f"{eval_metrics['rre']:.4f}" if "rre" in eval_metrics else "N/A",
            f"{eval_metrics['ann_turnover']:.2f}"
            if "ann_turnover" in eval_metrics
            else "N/A",
            f"{eval_metrics['ann_return_net']:.3f}"
            if "ann_return_net" in eval_metrics
            else "N/A",
        )
    except FactorBacktestCancelled:
        logger.info("[alpha-backtest-fn] %s cancelled by user", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="cancelled",
                metadata={"backtest_error": "cancelled_by_user"},
            )
        except Exception:
            pass
        await _record_backtest_finish(
            run_id,
            "cancelled",
            error="cancelled_by_user",
            universe=universe,
            data_source="h5",
            date_range=f"{start}~{end}",
        )
    except Exception as exc:
        logger.exception("[alpha-backtest-fn] %s failed", factor_id)
        err_msg = _format_backtest_error(exc)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": err_msg[-1500:]},
            )
        except Exception:
            pass
        await _record_backtest_finish(
            run_id,
            "failed",
            error=err_msg[-1500:],
            universe=universe,
            data_source="h5",
            date_range=f"{start}~{end}",
        )
    finally:
        # 同 _run_factor_backtest：旧任务收尾不得清掉新任务的登记
        if (
            factor_id not in _running_backtest_runs
            or _running_backtest_runs[factor_id] == run_id
        ):
            _running_backtest_runs.pop(factor_id, None)
            _running_backtests.discard(factor_id)
            _backtest_cancelled.discard(factor_id)


def _resolve_factor_h5_path(universe: str = "csi300") -> str:
    """解析因子回测用 H5 数据文件路径（RD-Agent daily_pv.h5）。"""
    base = (
        "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5"
    )
    if Path(base).exists():
        return base
    return "/tmp/daily_pv.h5"


async def _startup_recover_stuck_factors() -> None:
    """模块加载时启动一次性恢复任务：把超时的 backtesting 状态清理为 failed。

    在事件循环里 schedule 一个后台协程，等 3s DB 就绪后执行一次。
    因子行与历史台账同口径收口（引擎崩溃后两边都不留 running）。
    """
    try:
        await asyncio.sleep(3)
        # 先确保表存在再对账：本钩子与 lifespan 的建表并发，刚升级/刚重建时
        # recover 的 UPDATE 可能先撞上「表不存在」被吞掉，等于这次启动没恢复。
        await persistence.ensure_tables()
        count = await persistence.recover_stuck_factors(max_age_min=15)
        if count:
            logger.info("[alpha-agent startup] recovered %d stuck backtests", count)
        runs = await persistence.recover_stuck_backtest_runs(max_age_min=15)
        if runs:
            logger.info("[alpha-agent startup] recovered %d stuck backtest runs", runs)
    except Exception as e:
        # 恢复没跑成必须可见（静默吞掉 = 孤儿 running 行一直挂到下次重启）
        logger.warning("[alpha-agent startup] recovery skipped: %s", e)


# 模块加载时自动注册启动恢复任务（如果事件循环已运行）
try:
    _loop = asyncio.get_running_loop()
    _loop.create_task(_startup_recover_stuck_factors())
except RuntimeError:
    pass  # 事件循环未运行（导入阶段），跳过；下次请求时会懒触发（如果有的话）
