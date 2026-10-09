"""方向拆解器（一段粗方向 → N 张正交子假设卡片）的契约。

用户诉求：「我给了 10 个方向、一批一批的去挖掘……自动拆解。效率更高。挖之前拆解」。

拆解器只做一件事：把一段粗方向（研报摘录/长文指令）交给 LLM 拆成互相正交、
可独立挖掘的子假设卡片。它**不启动任何任务、不落任何任务行**——派发是批量
端点的事；拆解失败绝不该有半张卡片已经开跑。

钉住的边：

1. **卡片归一金样**（decomposeCardsGolden.json，PROMPT_VERSION 与金样同步）：
   title/hypothesis 必填、空白清理、categories 收敛（单值容忍/去重/剔未知 id/
   每卡至多 3 个）、可选字段空则省略、超量按上限截断并把 dropped 如实上报
   （不静默丢）。
2. **坏输出显式失败**：非对象 / cards 非数组 / 空数组 / 卡片缺必填 → SchemaError
   （SchemaError ⊂ DecomposeError，路由一条 400 映射）。
3. **截断可操作**：finish_reason=length 或「只有推理、可见输出为空」升级为指路
   DECOMPOSE_MAX_TOKENS 的报错；半截 JSON 绝不静默变成「没有卡片」。
4. **上下文注入单通道 + 容错**：L1 类别清单 + 因子池摘要（避开已挖因子）进
   user 消息；池摘要超长截断并标记；两个上下文源任一失败只告警不拦拆解。
5. **输入先拒**：方向为空 / 超长（与 task_store 存储闸同一上限）在碰任何 IO 前拒绝。
6. **端点**：无 LLM 配置 412（与 evolve 同文案）；未知市场 400；DecomposeError
   → 400；成功 {code:200, data:{prompt_version, cards, dropped, max_cards, context}}。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import direction_decompose as dd  # noqa: E402
from backend.services.engine.alpha_agent.llm_client import LLMConfig  # noqa: E402
from backend.services.engine.routers import alpha_agent as router_mod  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "decomposeCardsGolden.json").read_text("utf-8"))

CFG = LLMConfig(
    api_key="sk-x", base_url="https://llm.test/v1", model="m1", protocol="openai"
)

CATEGORIES = {
    "categories": [
        {
            "id": "momentum",
            "name": "动量类",
            "feature_count": 30,
            "sample_features": ["mom_5d", "mom_20d"],
        },
        {
            "id": "volatility",
            "name": "波动率类",
            "feature_count": 12,
            "sample_features": ["vol_20d"],
        },
    ]
}


def _chat_returning(cards: list[dict], *, record: list | None = None):
    """替身 chat_fn：返回包着代码围栏+前后解说的 JSON（LLM 常见形态）。"""

    async def _chat(messages, **kwargs):
        if record is not None:
            record.append({"messages": messages, "kwargs": kwargs})
        body = json.dumps({"cards": cards}, ensure_ascii=False)
        return f"先说明一下\n```json\n{body}\n```\n以上。"

    return _chat


async def _boom_chat(messages, **kwargs):
    raise httpx.ConnectError("connection refused")


# ── 卡片归一（金样机器闸）────────────────────────────────────────────


def test_prompt_version_matches_golden() -> None:
    assert dd.PROMPT_VERSION == GOLDEN["prompt_version"]


def test_validate_cards_golden() -> None:
    out = dd.validate_cards(
        GOLDEN["input"],
        max_cards=GOLDEN["max_cards"],
        allowed_categories=GOLDEN["allowed_categories"],
    )
    assert out["cards"] == GOLDEN["expected"]["cards"]
    assert out["dropped"] == GOLDEN["expected"]["dropped"]


@pytest.mark.parametrize(
    "payload",
    [
        "不是对象",
        ["cards"],
        {"cards": "x"},
        {"cards": []},
        {"cards": [{"title": "t"}]},
        {"cards": [{"hypothesis": "h"}]},
        {"cards": [{"title": "   ", "hypothesis": "h"}]},
        {"cards": ["裸字符串"]},
    ],
)
def test_validate_cards_rejects_bad_output(payload) -> None:
    with pytest.raises(dd.DecomposeSchemaError):
        dd.validate_cards(payload, max_cards=6, allowed_categories=())


def test_validate_cards_schema_error_is_decompose_error() -> None:
    """SchemaError ⊂ DecomposeError：路由只需一条 400 映射。"""
    assert issubclass(dd.DecomposeSchemaError, dd.DecomposeError)
    assert issubclass(dd.DecomposeTruncatedError, dd.DecomposeError)


def test_validate_cards_without_whitelist_keeps_categories() -> None:
    """无 L1 白名单（类别源不可用）时不做剔除——只去重 + 每卡至多 3 个。"""
    out = dd.validate_cards(
        {
            "cards": [
                {"title": "t", "hypothesis": "h", "categories": ["a", "a", "b", "c"]}
            ]
        },
        max_cards=6,
        allowed_categories=(),
    )
    assert out["cards"][0]["categories"] == ["a", "b", "c"]


def test_validate_cards_unknown_whitelist_still_filters() -> None:
    out = dd.validate_cards(
        {
            "cards": [
                {"title": "t", "hypothesis": "h", "categories": ["momentum", "zzz"]}
            ]
        },
        max_cards=6,
        allowed_categories=["momentum"],
    )
    assert out["cards"][0]["categories"] == ["momentum"]


# ── 提示词组装 ──────────────────────────────────────────────────────


def test_render_categories_skips_junk_and_reports_count() -> None:
    cats = {
        "categories": [
            None,
            "junk",
            {"id": ""},
            {"id": "c1", "name": "一类", "feature_count": 7, "sample_features": ["f1"]},
        ]
    }
    text, n = dd.render_categories(cats)
    assert n == 1
    assert len(text.splitlines()) == 1
    assert "c1" in text and "一类" in text and "f1" in text


def test_render_categories_caps_line_count() -> None:
    cats = {"categories": [{"id": f"c{i}"} for i in range(50)]}
    _, n = dd.render_categories(cats)
    assert n == dd.CATEGORY_RENDER_MAX


def test_render_categories_tolerates_missing_source() -> None:
    assert dd.render_categories(None) == ("", 0)
    assert dd.render_categories({"categories": "坏形状"}) == ("", 0)


def test_build_messages_injects_all_context() -> None:
    messages = dd._build_messages(
        direction="复现研报 X 的动量方向",
        market="a_share",
        universe="csi300",
        max_cards=6,
        categories=CATEGORIES,
        pool_digest="## 已挖因子\n- mom_5d ICIR 0.4",
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "6" in messages[0]["content"]  # 张数上限进系统提示词
    user = messages[1]["content"]
    assert "复现研报 X 的动量方向" in user
    assert "momentum" in user and "动量类" in user and "mom_5d" in user
    assert "mom_5d ICIR 0.4" in user
    assert "a_share" in user and "csi300" in user


def test_build_messages_places_placeholder_for_missing_sources() -> None:
    messages = dd._build_messages(
        direction="方向",
        market="a_share",
        universe="csi300",
        max_cards=6,
        categories=None,
        pool_digest="",
    )
    user = messages[1]["content"]
    assert "类别清单不可用" in user
    assert "因子池为空" in user


def test_build_messages_clips_pool_digest_with_marker() -> None:
    digest = "A" * (dd.POOL_DIGEST_MAX_CHARS + 500) + "TAIL-MARK"
    messages = dd._build_messages(
        direction="方向",
        market="a_share",
        universe="csi300",
        max_cards=6,
        categories=CATEGORIES,
        pool_digest=digest,
    )
    user = messages[1]["content"]
    assert "TAIL-MARK" not in user
    assert "截断" in user


def test_build_messages_tolerates_template_like_direction() -> None:
    """方向可能含 ``$``/``{}``（研报公式）——模板替换绝不能因此炸。"""
    weird = "用 $alpha 与 {rank} 的方向"
    messages = dd._build_messages(
        direction=weird,
        market="a_share",
        universe="csi300",
        max_cards=6,
        categories=CATEGORIES,
        pool_digest="",
    )
    assert weird in messages[1]["content"]


# ── decompose_direction 主链 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_decompose_happy_path() -> None:
    record: list = []
    chat = _chat_returning(
        [
            {
                "title": "动量",
                "hypothesis": "超跌反弹",
                "categories": ["momentum", "zzz"],
            },
            {"title": "波动", "hypothesis": "低波动占优"},
        ],
        record=record,
    )
    out = await dd.decompose_direction(
        direction="研究动量与波动方向",
        user_id="u-1",
        llm_config=CFG,
        chat_fn=chat,
        categories=CATEGORIES,
        pool_digest="池摘要 mom_5d",
    )

    assert out["prompt_version"] == dd.PROMPT_VERSION
    assert out["cards"] == [
        {"title": "动量", "hypothesis": "超跌反弹", "categories": ["momentum"]},
        {"title": "波动", "hypothesis": "低波动占优", "categories": []},
    ]
    assert out["dropped"] == 0
    assert out["max_cards"] == dd.MAX_CARDS_DEFAULT
    assert out["context"]["categories"] == 2
    assert out["context"]["pool_digest_chars"] == len("池摘要 mom_5d")
    assert out["context"]["model"] == "m1"
    assert record[0]["kwargs"]["temperature"] == dd.DECOMPOSE_LLM_TEMPERATURE
    assert record[0]["kwargs"]["max_tokens"] > 0


@pytest.mark.asyncio
async def test_decompose_truncation_is_actionable_length(monkeypatch) -> None:
    async def _fake_meta(messages, **kwargs):
        return "", {"finish_reason": "length", "has_reasoning": True, "model": "m1"}

    monkeypatch.setattr(dd, "chat_with_meta", _fake_meta)
    with pytest.raises(dd.DecomposeTruncatedError) as ei:
        await dd.decompose_direction(
            direction="方向",
            user_id="u-1",
            llm_config=CFG,
            categories=CATEGORIES,
            pool_digest="",
        )
    assert "DECOMPOSE_MAX_TOKENS" in str(ei.value)


@pytest.mark.asyncio
async def test_decompose_reasoning_only_output_is_actionable(monkeypatch) -> None:
    async def _fake_meta(messages, **kwargs):
        return "", {"finish_reason": "stop", "has_reasoning": True, "model": "m1"}

    monkeypatch.setattr(dd, "chat_with_meta", _fake_meta)
    with pytest.raises(dd.DecomposeTruncatedError) as ei:
        await dd.decompose_direction(
            direction="方向",
            user_id="u-1",
            llm_config=CFG,
            categories=CATEGORIES,
            pool_digest="",
        )
    assert "推理" in str(ei.value)


@pytest.mark.asyncio
async def test_default_chat_factory_disables_thinking_by_default(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_meta(messages, **kwargs):
        captured.update(kwargs)
        return '{"cards": []}', {"finish_reason": "stop", "model": "m1"}

    monkeypatch.setattr(dd, "chat_with_meta", _fake_meta)
    monkeypatch.delenv("DECOMPOSE_DISABLE_THINKING", raising=False)
    factory = dd._default_chat_factory(CFG)
    await factory(
        [{"role": "user", "content": "x"}], max_tokens=10, temperature=0, timeout=1
    )
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}

    captured.clear()
    monkeypatch.setenv("DECOMPOSE_DISABLE_THINKING", "false")
    await factory(
        [{"role": "user", "content": "x"}], max_tokens=10, temperature=0, timeout=1
    )
    assert captured["extra_body"] is None


@pytest.mark.asyncio
async def test_decompose_llm_network_failure_wrapped() -> None:
    with pytest.raises(dd.DecomposeError) as ei:
        await dd.decompose_direction(
            direction="方向",
            user_id="u-1",
            llm_config=CFG,
            chat_fn=_boom_chat,
            categories=CATEGORIES,
            pool_digest="",
        )
    assert type(ei.value) is dd.DecomposeError
    assert "LLM 调用失败" in str(ei.value)


@pytest.mark.asyncio
async def test_decompose_garbage_output_rejected() -> None:
    async def _garbage(messages, **kwargs):
        return "这里没有任何 JSON"

    with pytest.raises(dd.DecomposeError) as ei:
        await dd.decompose_direction(
            direction="方向",
            user_id="u-1",
            llm_config=CFG,
            chat_fn=_garbage,
            categories=CATEGORIES,
            pool_digest="",
        )
    assert "找不到合法 JSON" in str(ei.value)


@pytest.mark.asyncio
async def test_decompose_rejects_empty_direction_before_io() -> None:
    async def _must_not_call(messages, **kwargs):
        raise AssertionError("方向为空时不该碰 LLM")

    with pytest.raises(dd.DecomposeError):
        await dd.decompose_direction(
            direction="   ",
            user_id="u-1",
            llm_config=CFG,
            chat_fn=_must_not_call,
        )


@pytest.mark.asyncio
async def test_decompose_rejects_overlong_direction_before_io() -> None:
    async def _must_not_call(messages, **kwargs):
        raise AssertionError("方向超长时不该碰 LLM")

    with pytest.raises(dd.DecomposeError) as ei:
        await dd.decompose_direction(
            direction="字" * (dd.MAX_DECOMPOSE_DIRECTION_CHARS + 1),
            user_id="u-1",
            llm_config=CFG,
            chat_fn=_must_not_call,
        )
    assert str(dd.MAX_DECOMPOSE_DIRECTION_CHARS) in str(ei.value)


@pytest.mark.asyncio
async def test_decompose_clamps_max_cards() -> None:
    record: list = []
    chat = _chat_returning([{"title": "t", "hypothesis": "h"}], record=record)

    out = await dd.decompose_direction(
        direction="方向",
        user_id="u-1",
        llm_config=CFG,
        chat_fn=chat,
        max_cards=99,
        categories=CATEGORIES,
        pool_digest="",
    )
    assert out["max_cards"] == dd.MAX_CARDS_LIMIT
    assert str(dd.MAX_CARDS_LIMIT) in record[0]["messages"][0]["content"]

    out2 = await dd.decompose_direction(
        direction="方向",
        user_id="u-1",
        llm_config=CFG,
        chat_fn=chat,
        max_cards=None,
        categories=CATEGORIES,
        pool_digest="",
    )
    assert out2["max_cards"] == dd.MAX_CARDS_DEFAULT


def test_resolve_max_cards_falls_back_on_bad_values() -> None:
    assert dd.resolve_max_cards(None) == dd.MAX_CARDS_DEFAULT
    assert dd.resolve_max_cards(0) == 1
    assert dd.resolve_max_cards(-3) == 1
    assert dd.resolve_max_cards("8") == dd.MAX_CARDS_DEFAULT  # 坏类型回默认，不炸
    assert dd.resolve_max_cards(3) == 3


@pytest.mark.asyncio
async def test_decompose_survives_context_source_failures(monkeypatch) -> None:
    """类别/池两个源都挂 → 拆解照跑，prompt 用显式占位，context 计数为 0。"""
    from backend.services.engine.data_platform import quantdb_hub as hub_mod
    from backend.services.engine.mining_plugins import pool_service

    def _hub_down():
        raise RuntimeError("hub down")

    async def _digest_down(**kwargs):
        raise RuntimeError("pool down")

    monkeypatch.setattr(hub_mod.QuantDBDataHub, "get_instance", _hub_down)
    monkeypatch.setattr(pool_service, "build_injection_digest", _digest_down)

    record: list = []
    chat = _chat_returning([{"title": "t", "hypothesis": "h"}], record=record)
    out = await dd.decompose_direction(
        direction="方向",
        user_id="u-1",
        llm_config=CFG,
        chat_fn=chat,
    )

    assert out["context"]["categories"] == 0
    assert out["context"]["pool_digest_chars"] == 0
    user = record[0]["messages"][1]["content"]
    assert "类别清单不可用" in user and "因子池为空" in user


# ── 路由端点 ────────────────────────────────────────────────────────


def _wire_router(monkeypatch, llm_config: LLMConfig | None) -> None:
    monkeypatch.setattr(
        router_mod, "get_authenticated_identity", lambda req: ("u-1", "t-1")
    )

    async def fake_resolve(user_id, tenant_id):
        return llm_config, "user_profile" if llm_config else "none", None

    monkeypatch.setattr(router_mod, "_resolve_effective_llm_config", fake_resolve)


async def _call_decompose(payload=None):
    return await router_mod.decompose_directions(
        request=SimpleNamespace(),
        payload=payload or router_mod.DecomposeRequest(direction="一段粗方向"),
    )


@pytest.mark.asyncio
async def test_decompose_endpoint_412_without_llm(monkeypatch) -> None:
    _wire_router(monkeypatch, None)

    async def _must_not_call(**kwargs):
        raise AssertionError("无 LLM 配置时不该进拆解")

    monkeypatch.setattr(dd, "decompose_direction", _must_not_call)
    with pytest.raises(HTTPException) as ei:
        await _call_decompose()
    assert ei.value.status_code == 412
    assert "未配置 LLM API Key" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_decompose_endpoint_happy(monkeypatch) -> None:
    _wire_router(monkeypatch, CFG)
    seen: dict = {}

    async def fake_decompose(**kwargs):
        seen.update(kwargs)
        return {
            "prompt_version": "decompose_v1",
            "cards": [{"title": "t", "hypothesis": "h", "categories": []}],
            "dropped": 0,
            "max_cards": 6,
            "context": {
                "categories": 0,
                "pool_digest_chars": 0,
                "pool_factors": 0,
                "model": "m1",
            },
        }

    monkeypatch.setattr(dd, "decompose_direction", fake_decompose)
    out = await _call_decompose(
        router_mod.DecomposeRequest(direction="  粗方向  ", max_cards=3)
    )

    assert out["code"] == 200
    assert out["data"]["cards"][0]["title"] == "t"
    assert seen["direction"] == "粗方向"  # strip 后进链
    assert seen["user_id"] == "u-1"
    assert seen["market"] == "a_share" and seen["universe"] == "csi300"
    assert seen["max_cards"] == 3
    assert seen["llm_config"] is CFG


@pytest.mark.asyncio
async def test_decompose_endpoint_error_maps_to_400(monkeypatch) -> None:
    _wire_router(monkeypatch, CFG)

    async def fake_decompose(**kwargs):
        raise dd.DecomposeError(
            "拆解输出被截断：输出预算已耗尽。请调大 DECOMPOSE_MAX_TOKENS"
        )

    monkeypatch.setattr(dd, "decompose_direction", fake_decompose)
    with pytest.raises(HTTPException) as ei:
        await _call_decompose()
    assert ei.value.status_code == 400
    assert "DECOMPOSE_MAX_TOKENS" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_decompose_endpoint_unknown_market_400(monkeypatch) -> None:
    _wire_router(monkeypatch, CFG)

    async def _must_not_call(**kwargs):
        raise AssertionError("未知市场不该进拆解")

    monkeypatch.setattr(dd, "decompose_direction", _must_not_call)
    with pytest.raises(HTTPException) as ei:
        await _call_decompose(
            router_mod.DecomposeRequest(direction="方向", market="no_such_market")
        )
    assert ei.value.status_code == 400
    assert "no_such_market" in str(ei.value.detail)
