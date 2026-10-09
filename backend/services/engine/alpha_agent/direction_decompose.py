"""方向拆解器 —— 一段粗方向 → N 张正交子假设卡片（只拆解，不启动任务）。

用户诉求：「我给了 10 个方向、一批一批的去挖掘……自动拆解。效率更高。挖之前拆解」。

拆解产出的每张卡片会各自派发一次独立的因子挖掘任务并行验证，所以这里的
质量口径是 **正交性**：换措辞的卡片是浪费一整次挖掘成本。拆解本身不落任何
任务行——派发是批量端点的事（POST /mining/batch），拆解失败没有半张卡片开跑。

四条纪律（与测试金样 ``tests/fixtures/decomposeCardsGolden.json`` 绑定）：

1. **卡片归一金样化**：``validate_cards`` 是唯一入鞘口——title/hypothesis
   必填，categories 收敛（容忍单值写法/去重/剔除白名单外的 id/每卡至多 3 个），
   可选字段空则省略，超量按上限截断且 ``dropped`` 如实上报（不静默丢）。
   改口径必须同步金样，否则测试红。
2. **坏输出显式失败**：非对象/空 cards/卡片缺必填 → ``DecomposeSchemaError``；
   半截 JSON（输出预算被思考耗尽）→ ``DecomposeTruncatedError`` 并指路
   ``DECOMPOSE_MAX_TOKENS`` 旋钮——绝不静默降级成「没有卡片」。
3. **上下文注入是增益层，不是主链**：L1 类别清单（QuantDB feature catalog）
   与因子池摘要（``pool_service.build_injection_digest``，让新假设避开已挖
   构造）任一加载失败只告警，提示词用显式占位，拆解照跑。
4. **LLM 单通道**：复用 ``llm_client.chat_with_meta``（用户 Profile Key 优先，
   config 由路由注入）；默认关思考（``DECOMPOSE_DISABLE_THINKING=false``
   归一后精确等值才恢复）；所有调用经 ``chat_fn`` 可替身注入（测试不碰网络）。

提示词文本 + ``PROMPT_VERSION`` 是模板版本化的机器闸：任何文本改动都要 bump
版本并同步金样文件名里的契约（``validate_cards`` 口径不变则卡片金样不动）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from string import Template
from typing import Any

import httpx

from backend.services.engine.alpha_agent.doc_organize import (
    OrganizeError,
    extract_json_object,
)
from backend.services.engine.alpha_agent.llm_client import LLMConfig, chat_with_meta
from backend.services.engine.alpha_agent.task_store import (
    MAX_DIRECTION_CHARS as MAX_DECOMPOSE_DIRECTION_CHARS,
)

logger = logging.getLogger(__name__)

#: 模板版本：提示词文本改动必须 bump（历史拆解结果按它追溯「哪版模板拆的」）
PROMPT_VERSION = "decompose_v1"

#: 卡片数：默认值 / 硬上限（上限的读取点收敛在 resolve_max_cards）
MAX_CARDS_DEFAULT = 6
MAX_CARDS_LIMIT = 12
#: 每张卡片关联的 L1 类别数上限
CATEGORIES_PER_CARD_MAX = 3
#: 提示词里渲染的 L1 类别条数上限（类别过百时防提示词爆长）
CATEGORY_RENDER_MAX = 20
#: 因子池摘要注入的字符上限（超出截断并标记）
POOL_DIGEST_MAX_CHARS = 3000

DECOMPOSE_LLM_TEMPERATURE = 0.4
DECOMPOSE_LLM_TIMEOUT_S = 120.0

#: 输出预算（推理模型的思考也计入）：模块导入时读取，与 doc_organize 同口径。
#: 截断报错会指路这个旋钮，所以名字同时出现在文案里。
_MAX_TOKENS = max(1, int(os.getenv("DECOMPOSE_MAX_TOKENS", "4000") or "4000"))

_CLIP_MARKER = "\n…（已截断，完整摘要见「因子池」页）"
_NO_CATEGORIES = "（L1 类别清单不可用——本卡 categories 请留空数组）"
_NO_POOL = "（因子池为空或不可用——按经典因子逻辑展开即可）"

_TRUNCATION_HINT = (
    "请调大 DECOMPOSE_MAX_TOKENS（当前 %d）后重试，或调小拆解卡片数让输出更短。"
)


class DecomposeError(RuntimeError):
    """拆解失败基类（路由层统一映射 400/可操作文案）。"""


class DecomposeSchemaError(DecomposeError):
    """LLM 输出不符合卡片 schema（半成品卡片比没有更害人）。"""


class DecomposeTruncatedError(DecomposeError):
    """输出被截断（预算耗尽/思考占满）——必须带可操作旋钮提示。"""


ChatFn = Callable[..., Awaitable[str]]


# ── 卡片归一 ────────────────────────────────────────────────────────


def _normalize_categories(value: Any, allowed: set[str]) -> list[str]:
    """容忍单值写法；去重；剔除白名单外的 id（白名单为空则不剔）；至多 3 个。"""
    if isinstance(value, str):
        raw_list: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_list = list(value)
    else:
        raw_list = []
    out: list[str] = []
    for item in raw_list:
        cid = str(item or "").strip()
        if not cid or cid in out:
            continue
        if allowed and cid not in allowed:
            continue
        out.append(cid)
        if len(out) >= CATEGORIES_PER_CARD_MAX:
            break
    return out


def validate_cards(
    payload: Any,
    *,
    max_cards: int,
    allowed_categories: Iterable[str] = (),
) -> dict[str, Any]:
    """LLM 输出 → 归一卡片列表。

    Returns: ``{"cards": [...], "dropped": int}``——超量被截断的卡片数如实
    上报，前端可提示「还有 N 张超限未展示」，绝不静默丢。
    """
    if not isinstance(payload, Mapping):
        raise DecomposeSchemaError("拆解输出不是 JSON 对象")
    raw_cards = payload.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        raise DecomposeSchemaError("拆解输出缺少 cards 数组（或为空）")

    allowed = {str(c).strip() for c in allowed_categories if str(c).strip()}
    cards: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_cards):
        if not isinstance(raw, Mapping):
            raise DecomposeSchemaError(f"拆解输出 cards[{i}] 不是对象")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise DecomposeSchemaError(f"拆解输出 cards[{i}] 缺少 title")
        hypothesis = str(raw.get("hypothesis") or "").strip()
        if not hypothesis:
            raise DecomposeSchemaError(f"拆解输出 cards[{i}] 缺少 hypothesis")
        card: dict[str, Any] = {
            "title": title,
            "hypothesis": hypothesis,
        }
        rationale = str(raw.get("rationale") or "").strip()
        if rationale:
            card["rationale"] = rationale
        card["categories"] = _normalize_categories(raw.get("categories"), allowed)
        hint = str(raw.get("evaluation_hint") or "").strip()
        if hint:
            card["evaluation_hint"] = hint
        cards.append(card)

    kept = cards[:max_cards]
    return {"cards": kept, "dropped": len(cards) - len(kept)}


def resolve_max_cards(value: int | None) -> int:
    """None/坏类型 → 默认 6；其余夹到 [1, 上限]（读取点唯一，不 ValueError 炸链）。"""
    if not isinstance(value, int) or isinstance(value, bool):
        return MAX_CARDS_DEFAULT
    return max(1, min(value, MAX_CARDS_LIMIT))


# ── 上下文源（增益层，失败不拦拆解）─────────────────────────────────


def _allowed_category_ids(categories: Mapping | None) -> list[str]:
    items = (categories or {}).get("categories")
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for cat in items:
        if not isinstance(cat, Mapping):
            continue
        cid = str(cat.get("id") or "").strip()
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def render_categories(categories: Mapping | None) -> tuple[str, int]:
    """L1 类别 → 提示词两列表项。Returns: (markdown, 有效条数)。"""
    items = (categories or {}).get("categories")
    if not isinstance(items, list):
        return "", 0
    lines: list[str] = []
    for cat in items:
        if not isinstance(cat, Mapping):
            continue
        cid = str(cat.get("id") or "").strip()
        if not cid:
            continue
        name = str(cat.get("name") or "").strip()
        label = f"{cid}（{name}）" if name else cid
        count = cat.get("feature_count")
        count_txt = (
            f"，{int(count)} 个特征" if isinstance(count, int) and count > 0 else ""
        )
        samples = [
            str(s).strip() for s in (cat.get("sample_features") or []) if str(s).strip()
        ]
        sample_txt = f"，示例：{'、'.join(samples[:4])}" if samples else ""
        lines.append(f"- {label}{count_txt}{sample_txt}")
        if len(lines) >= CATEGORY_RENDER_MAX:
            break
    return "\n".join(lines), len(lines)


def _load_categories() -> Mapping | None:
    """L1 类别（QuantDB feature catalog）；失败返回 None（提示词用占位）。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        return QuantDBDataHub.get_instance().fetch_l1_factor_categories()
    except Exception as exc:  # noqa: BLE001 —— 注入是增益层，绝不拦拆解
        logger.warning("[decompose] L1 类别加载失败（不拦拆解）: %s", exc)
        return None


async def _load_pool_digest(
    *, user_id: str, market: str, universe: str
) -> tuple[str, tuple[str, ...]]:
    """因子池摘要；失败返回空（提示词用占位）。"""
    try:
        from backend.services.engine.mining_plugins.pool_service import (
            build_injection_digest,
        )

        return await build_injection_digest(
            user_id=user_id, market=market, universe=universe
        )
    except Exception as exc:  # noqa: BLE001 —— 注入是增益层，绝不拦拆解
        logger.warning("[decompose] 因子池摘要加载失败（不拦拆解）: %s", exc)
        return "", ()


# ── 提示词 ──────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = Template(
    """你是 QuantMind 的因子挖掘研究主管，服务专业量化团队。用户给你一段粗粒度挖掘\
方向（研报摘录、策略想法或长文指令），你的任务是把它拆解成不超过 $max_cards 张\
**互相正交**的子假设卡片——每张卡片会各自启动一次独立的因子挖掘任务并行验证，\
拆得越正交、并行收益越大；含糊或重复的卡片会白白烧掉一整次任务成本。

拆解纪律：
1. 正交优先：卡片之间不许只是换措辞。每张卡片落在不同的信号维度（动量/反转、\
波动率、流动性、量价关系、估值、质量、情绪、微观结构等），同一维度最多一张。
2. 可检验：hypothesis 必须是一句话可证伪的因子逻辑（"具备 XX 特征的股票未来\
表现更好/更差"），不写"研究一下 XX"这类无法判定的任务描述。
3. 与已有因子错位：参考「因子池现状」，避开池中已覆盖的构造，优先补缺位维度；\
池为空时按经典因子逻辑展开。
4. 不虚构：只使用给定信息；引用资料时忠实原意，不添数字，不编造数据字段。
5. 宁缺毋滥：能拆几张拆几张，凑数卡片会浪费一次挖掘任务。

输出必须是且仅是一个 JSON 对象（不含任何 JSON 以外的文字、解说或代码围栏）：
{
  "cards": [
    {
      "title": "卡片标题（≤12 字）",
      "hypothesis": "一句话可检验的因子假设",
      "rationale": "为何可能有效（行为/结构/制度逻辑，≤120 字）",
      "categories": ["最相关的 L1 类别 id（取自给定清单，可为空数组，至多 3 个）"],
      "evaluation_hint": "验证建议：评估指标 / 分组方式 / 换手预期（≤80 字）"
    }
  ]
}"""
)

_USER_TEMPLATE = Template(
    """## 挖掘方向（待拆解）
$direction

## 目标市场
市场：$market　股票池：$universe

## L1 因子类别清单（categories 只能取自这里）
$categories

## 因子池现状（本用户已挖出的因子摘要——新假设请避开重复构造）
$pool

## 任务
把「挖掘方向」拆解成不超过 $max_cards 张正交、可检验的子假设卡片，按系统提示词给出的 JSON 结构输出。"""
)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _CLIP_MARKER


def _build_messages(
    *,
    direction: str,
    market: str,
    universe: str,
    max_cards: int,
    categories: Mapping | None,
    pool_digest: str | None,
) -> list[dict[str, str]]:
    """组装 system+user 消息（提示词单通道：上下文只从这里进）。"""
    cats_md, _ = render_categories(categories)
    digest = _clip((pool_digest or "").strip(), POOL_DIGEST_MAX_CHARS)
    system = _SYSTEM_TEMPLATE.substitute(max_cards=max_cards)
    # Template.substitute 只扫描模板本身，值里的 $ / {} 原样落地（方向可能含公式）
    user = _USER_TEMPLATE.substitute(
        direction=direction,
        market=market,
        universe=universe,
        max_cards=max_cards,
        categories=cats_md or _NO_CATEGORIES,
        pool=digest or _NO_POOL,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ── LLM 调用（thinking 默认关 + 截断识别，与 doc_organize 同口径）───


def _thinking_off_body() -> dict | None:
    """默认关思考——拆解是受约束的 JSON 产出，思考只烧预算与延迟。

    ``DECOMPOSE_DISABLE_THINKING=false``（归一后精确等值）恢复默认思考。
    """
    if os.getenv("DECOMPOSE_DISABLE_THINKING", "").strip().lower() == "false":
        return None
    return {"thinking": {"type": "disabled"}}


def _default_chat_factory(config: LLMConfig) -> ChatFn:
    async def _guarded(extra_body: dict | None, messages, **kwargs) -> str:
        text, meta = await chat_with_meta(
            messages, config=config, extra_body=extra_body, **kwargs
        )
        hint = _TRUNCATION_HINT % _MAX_TOKENS
        if meta.get("finish_reason") == "length":
            # 推理模型的 reasoning_content 也计入 max_tokens：预算被思考耗尽时
            # 可见输出在 JSON 中途被截断。半截 JSON 只会变成「找不到合法 JSON」
            # 的哑错误，这里换成可操作的报错。
            raise DecomposeTruncatedError("LLM 输出被截断：输出预算已耗尽。" + hint)
        if not text and meta.get("has_reasoning"):
            raise DecomposeTruncatedError(
                "LLM 只产出了推理内容、可见输出为空（思考占满了输出预算）。" + hint
            )
        return text

    async def _call(messages: list[dict[str, str]], **kwargs) -> str:
        extra_body = _thinking_off_body()
        if extra_body is None:
            return await _guarded(None, messages, **kwargs)
        try:
            return await _guarded(extra_body, messages, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # 严格网关不认 thinking 参数（400）：去掉后重试一次，行为退回默认。
            logger.info("LLM 网关不认 thinking 参数（400），去掉后重试")
            return await _guarded(None, messages, **kwargs)

    return _call


async def _chat_once(chat_call: ChatFn, messages: list[dict[str, str]]) -> str:
    try:
        return await chat_call(
            messages,
            max_tokens=_MAX_TOKENS,
            temperature=DECOMPOSE_LLM_TEMPERATURE,
            timeout=DECOMPOSE_LLM_TIMEOUT_S,
        )
    except DecomposeError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 网络/网关错误统一成可读文案
        raise DecomposeError(f"LLM 调用失败：{exc}") from exc


def _extract_payload(text: str) -> Any:
    if not (text or "").strip():
        raise DecomposeError("拆解结果为空：LLM 没有返回内容")
    try:
        return extract_json_object(text)
    except OrganizeError as exc:
        raise DecomposeError(
            "拆解结果里找不到合法 JSON 对象（请重试；若反复出现可换用更强的模型）"
        ) from exc


# ── 主链 ────────────────────────────────────────────────────────────


async def decompose_direction(
    *,
    direction: str,
    user_id: str,
    llm_config: LLMConfig,
    market: str = "a_share",
    universe: str = "csi300",
    max_cards: int | None = None,
    categories: Mapping | None = None,
    pool_digest: str | None = None,
    chat_fn: ChatFn | None = None,
) -> dict[str, Any]:
    """粗方向 → 正交卡片。只拆解，不落任何任务行。

    ``categories``/``pool_digest`` 为 None 时从真实源加载（失败自动降级为空
    注入）；测试显式传入可完全离线。``chat_fn`` 为 None 时走默认 LLM 通道。
    """
    direction = (direction or "").strip()
    if not direction:
        raise DecomposeError("拆解方向为空：请先输入要拆解的挖掘方向")
    if len(direction) > MAX_DECOMPOSE_DIRECTION_CHARS:
        raise DecomposeError(
            f"拆解方向过长（{len(direction)} 字，上限 {MAX_DECOMPOSE_DIRECTION_CHARS} 字），"
            "请精简后重试"
        )
    n_cards = resolve_max_cards(max_cards)

    ctx_categories = categories if categories is not None else _load_categories()
    if pool_digest is None:
        digest, pool_ids = await _load_pool_digest(
            user_id=user_id, market=market, universe=universe
        )
    else:
        digest, pool_ids = pool_digest, ()
    digest = (digest or "").strip()

    _, cats_n = render_categories(ctx_categories)
    pool_chars = len(_clip(digest, POOL_DIGEST_MAX_CHARS))

    messages = _build_messages(
        direction=direction,
        market=market,
        universe=universe,
        max_cards=n_cards,
        categories=ctx_categories,
        pool_digest=digest,
    )
    chat_call = chat_fn or _default_chat_factory(llm_config)
    raw = await _chat_once(chat_call, messages)
    payload = _extract_payload(raw)
    result = validate_cards(
        payload,
        max_cards=n_cards,
        allowed_categories=_allowed_category_ids(ctx_categories),
    )

    logger.info(
        "[decompose] direction=%d字 -> %d 卡片（超限丢弃 %d）categories=%d pool=%d字",
        len(direction),
        len(result["cards"]),
        result["dropped"],
        cats_n,
        pool_chars,
    )
    return {
        "prompt_version": PROMPT_VERSION,
        "cards": result["cards"],
        "dropped": result["dropped"],
        "max_cards": n_cards,
        "context": {
            "categories": cats_n,
            "pool_digest_chars": pool_chars,
            "pool_factors": len(pool_ids),
            "model": getattr(llm_config, "model", None),
        },
    }
