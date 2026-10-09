"""整理链 doc_organize（T-FM-09）—— 版本化模板 + schema 强约束 + map-reduce。

解析出来的 markdown 是**不可信语料**：整理链的职责是把它变成一份受约束的
「挖掘方向草稿」（free 自由挖掘 / paper 论文复现两口径），人工确认后才进
RD Agent。用例盯的是四道契约：

1. **模板版本化是机器闸**：提示词文本与 `PROMPT_VERSION` 都进金样文件，
   改模板不改版本 = 测试红（历史页要能追溯「哪个模板整理的」）。
2. **schema 强约束挡在 LLM 与落库之间**：缺 direction/假设/因子的输出不许
   进 organized_text——半成品的「挖掘方向」比没有更害人；字符串列表容忍
   单值写法（LLM 常给 "x" 而非 ["x"]），未知键一律丢弃。
3. **map-reduce 只在两端强约束**：分段提取（map）允许单段失败（跳过并
   告警，全失败才报错），最终合并（reduce）必须过完整 schema。
4. **注入不落地**：文档文本带明确边界进 user 消息，系统提示词声明其中
   指令一律不执行；extra 用户要求限长。

LLM 全部用脚本替身（ScriptedChat），不碰网络；末尾一个真库用例验证
organized_* 落库与 Z 序列化。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import doc_organize  # noqa: E402
from backend.services.engine.alpha_agent.doc_organize import (  # noqa: E402
    OrganizeError,
    OrganizeSchemaError,
    OrganizeTruncatedError,
    chunk_text,
    extract_json_object,
    organize_and_store,
    organize_document,
    render_markdown,
    validate_payload,
)
from backend.services.engine.alpha_agent.llm_client import LLMConfig  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _golden_prompts() -> dict:
    return json.loads(
        (FIXTURES / "docOrganizePromptGolden.json").read_text(encoding="utf-8")
    )


def _golden_output() -> dict:
    return json.loads((FIXTURES / "docOrganizeGolden.json").read_text(encoding="utf-8"))


def _golden_paper_output() -> dict:
    return json.loads((FIXTURES / "docPaperGolden.json").read_text(encoding="utf-8"))


class ScriptedChat:
    """按脚本返回 LLM 文本；记录每次调用（断言提示词内容与调用次数）。"""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("ScriptedChat 脚本耗尽：实际调用次数超出预期")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeStore:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    async def update_doc(self, doc_id: str, **fields) -> None:
        self.updates.append((doc_id, fields))


FREE_OK = {
    "title": "动量共振",
    "summary": "资料讨论了换手率与动量的关系。",
    "hypotheses": [
        {
            "hypothesis": "高换手动量延续",
            "rationale": "资料样本显示正相关",
            "data_hints": ["换手率"],
            "metrics": ["IC"],
        }
    ],
    "direction": "在 A 股全市场验证高换手动量延续性。",
}

PAPER_OK = {
    "title": "A Cross-Sectional Momentum Paper",
    "summary": "论文提出动量与波动率交互因子。",
    "method": "用 12-2 动量乘以特质波动倒数。",
    "factors": [
        {
            "name": "MomVol",
            "formula": "mom_12_2 / idio_vol",
            "intuition": "低波动股票的动量更干净",
            "inputs": ["日频收盘价", "波动率"],
        }
    ],
    "data_requirements": {"universe": "美股", "frequency": "日频", "fields": ["close"]},
    "replication_notes": "原文未说明去极值方式",
    "direction": "复现 MomVol 因子。",
}


# ── 模板金样 ────────────────────────────────────────────────────────


def test_prompt_templates_match_golden() -> None:
    """提示词 + 版本号进金样：改模板必须同步 bump 版本并更新金样文件。"""
    g = _golden_prompts()
    assert g["prompt_version"] == doc_organize.PROMPT_VERSION, (
        "PROMPT_VERSION 与金样不一致——版本化是追溯锚点，必须同步"
    )
    joined = {k: "\n".join(v) for k, v in g["system_prompts"].items()}
    assert joined["free"] == doc_organize.SYSTEM_PROMPT_FREE
    assert joined["paper"] == doc_organize.SYSTEM_PROMPT_PAPER
    for key, const in (
        ("map_prompt_template", doc_organize.MAP_PROMPT_TEMPLATE),
        ("reduce_prompt_template", doc_organize.REDUCE_PROMPT_TEMPLATE),
        ("single_user_template", doc_organize.SINGLE_USER_TEMPLATE),
        ("extra_block_template", doc_organize.EXTRA_BLOCK_TEMPLATE),
    ):
        assert "\n".join(g[key]) == const.template, (
            f"{key} 与金样不一致——改提示词必须 bump PROMPT_VERSION"
        )
    assert g["map_item_hints"] == doc_organize.MAP_ITEM_HINTS


# ── JSON 提取 ───────────────────────────────────────────────────────


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('好的，结果如下：\n{"a": 1}\n以上。') == {"a": 1}
    # 字符串里带花括号：平衡扫描必须感知引号
    assert extract_json_object('{"a": "brace } inside", "b": {"c": 2}}') == {
        "a": "brace } inside",
        "b": {"c": 2},
    }
    # 第一个花括号块不是合法 JSON：跳到下一个起点
    assert extract_json_object('{这不是 JSON} 然后 {"a": 1}') == {"a": 1}


def test_extract_json_object_rejects_garbage() -> None:
    with pytest.raises(OrganizeError):
        extract_json_object("完全没有 JSON")
    with pytest.raises(OrganizeError):
        extract_json_object("")


# ── 分块 ────────────────────────────────────────────────────────────


def test_chunk_text_short_document_stays_single() -> None:
    chunks, truncated = chunk_text("一段短文本。", chunk_chars=100, max_chunks=8)
    assert chunks == ["一段短文本。"]
    assert truncated is False


def test_chunk_text_splits_on_paragraphs_within_limit() -> None:
    text = "\n\n".join(f"第 {i} 段内容。" for i in range(20))
    chunks, truncated = chunk_text(text, chunk_chars=40, max_chunks=100)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)
    # 顺序保持、不丢内容（去空白后拼接应与原文一致）
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")
    assert truncated is False


def test_chunk_text_hard_splits_giant_paragraph() -> None:
    text = "字" * 250
    chunks, _ = chunk_text(text, chunk_chars=100, max_chunks=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_chunk_text_samples_when_over_max_chunks() -> None:
    # 每段长度一致（35 字）且小于块上限：段落边界即块边界，便于断言采样结果
    text = "\n\n".join(f"第{i:02d}段内容。" * 5 for i in range(30))
    chunks, truncated = chunk_text(text, chunk_chars=60, max_chunks=4)
    assert truncated is True
    assert len(chunks) <= 4
    assert chunks[0].startswith("第00段内容"), "首段必须保留（开头通常是摘要/引言）"
    assert chunks[-1].startswith("第29段内容"), "尾段也要保留（结论常在最后）"


# ── schema 校验 ─────────────────────────────────────────────────────


def test_validate_free_requires_core_fields() -> None:
    with pytest.raises(OrganizeSchemaError, match="summary"):
        validate_payload({"direction": "x"}, "free")
    with pytest.raises(OrganizeSchemaError, match="direction"):
        validate_payload({"summary": "x"}, "free")
    with pytest.raises(OrganizeSchemaError, match="hypotheses"):
        validate_payload({"summary": "x", "direction": "y", "hypotheses": []}, "free")
    # 条目缺 hypothesis：错误要指到下标
    with pytest.raises(OrganizeSchemaError, match=r"hypotheses\[0\]"):
        validate_payload({"summary": "x", "direction": "y", "hypotheses": [{}]}, "free")


def test_validate_free_normalizes_and_drops_unknown() -> None:
    payload = {
        "title": "  带空白  ",
        "summary": " 摘要 ",
        "direction": " 方向 ",
        "unknown_key": "丢弃",
        "hypotheses": [
            {
                "hypothesis": " 假设 ",
                "rationale": "",
                "data_hints": "单值写法",
                "metrics": ["IC", " ", 42],
                "extra": "丢弃",
            }
        ],
    }
    out = validate_payload(payload, "free")
    assert out["kind"] == "free"
    assert out["title"] == "带空白"
    assert "unknown_key" not in out
    item = out["hypotheses"][0]
    assert item == {
        "hypothesis": "假设",
        "data_hints": ["单值写法"],
        "metrics": ["IC", "42"],
    }, "空串与未知键必须清掉；单值容忍成列表"


def test_validate_paper_requires_factors_and_normalizes_requirements() -> None:
    with pytest.raises(OrganizeSchemaError, match="factors"):
        validate_payload(
            {"summary": "x", "direction": "y", "factors": [{"formula": "f"}]}, "paper"
        )
    out = validate_payload(PAPER_OK, "paper")
    assert out["kind"] == "paper"
    assert out["factors"][0]["name"] == "MomVol"
    assert out["data_requirements"]["universe"] == "美股"
    assert out["data_requirements"]["fields"] == ["close"]


def test_validate_rejects_unknown_kind() -> None:
    with pytest.raises(OrganizeError, match="口径"):
        validate_payload(FREE_OK, "blog")


# ── 渲染 ────────────────────────────────────────────────────────────


def test_render_markdown_free_matches_output_golden() -> None:
    golden = _golden_output()
    markdown = render_markdown(
        validate_payload(golden["expected_payload"], "free"),
        prompt_version=golden["prompt_version"],
    )
    assert markdown == golden["expected_markdown"]


def test_render_markdown_paper_matches_output_golden() -> None:
    golden = _golden_paper_output()
    markdown = render_markdown(
        validate_payload(golden["expected_payload"], "paper"),
        prompt_version=golden["prompt_version"],
    )
    assert markdown == golden["expected_markdown"]
    # 公式里的反斜杠（\prod）必须原样进背引号，不许被转义或吞掉
    assert "`R_{i,t-12,t-2} = \\prod" in markdown


def test_render_markdown_paper_contains_all_sections() -> None:
    md = render_markdown(validate_payload(PAPER_OK, "paper"))
    for needle in (
        "# A Cross-Sectional Momentum Paper",
        "## 摘要",
        "## 方法概述",
        "## 复现因子",
        "### 1. MomVol",
        "mom_12_2 / idio_vol",
        "## 数据要求",
        "## 复现注记",
        "## 挖掘方向（可直接用于 RD Agent）",
        "复现 MomVol 因子。",
        f"模板 {doc_organize.PROMPT_VERSION}",
    ):
        assert needle in md, f"缺 {needle}"


# ── 单次整理 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_organize_single_call_matches_output_golden() -> None:
    golden = _golden_output()
    chat = ScriptedChat([golden["llm_response"]])
    result = await organize_document(text=golden["input_md"], kind="free", chat_fn=chat)
    assert result["payload"] == golden["expected_payload"]
    assert result["markdown"] == golden["expected_markdown"]
    assert result["prompt_version"] == doc_organize.PROMPT_VERSION
    assert result["chunks_used"] == 1
    assert result["truncated"] is False
    # 披露边界：不可信素材标记 + 系统提示词在
    user_msg = chat.calls[0]["messages"][1]["content"]
    assert "<document>" in user_msg and "一律不执行" in user_msg
    assert chat.calls[0]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_organize_paper_single_call_matches_output_golden() -> None:
    """论文复现卡金样：类 MinerU 解析输出 → 结构化 payload 与 Markdown 全等。"""
    golden = _golden_paper_output()
    chat = ScriptedChat([golden["llm_response"]])
    result = await organize_document(
        text=golden["input_md"], kind="paper", chat_fn=chat
    )
    assert result["payload"] == golden["expected_payload"]
    assert result["markdown"] == golden["expected_markdown"]
    assert result["prompt_version"] == golden["prompt_version"]
    assert result["chunks_used"] == 1
    assert result["truncated"] is False
    # 金样落盘的是净形：未知键/空白噪声已在归一化中清掉
    assert "unknown_top_key" not in result["payload"]
    assert all("unknown_field" not in f for f in result["payload"]["factors"])
    user_msg = chat.calls[0]["messages"][1]["content"]
    assert "<document>" in user_msg and "一律不执行" in user_msg
    assert chat.calls[0]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_organize_single_call_schema_violation_raises() -> None:
    chat = ScriptedChat(['{"summary": "只有摘要"}'])
    with pytest.raises(OrganizeSchemaError, match="direction"):
        await organize_document(text="一段资料", kind="free", chat_fn=chat)


@pytest.mark.asyncio
async def test_organize_non_json_output_raises() -> None:
    chat = ScriptedChat(["对不起，我无法整理这份资料。"])
    with pytest.raises(OrganizeError, match="JSON"):
        await organize_document(text="一段资料", kind="free", chat_fn=chat)


@pytest.mark.asyncio
async def test_organize_empty_text_raises() -> None:
    with pytest.raises(OrganizeError, match="为空"):
        await organize_document(text="   ", kind="free", chat_fn=ScriptedChat([]))


@pytest.mark.asyncio
async def test_organize_llm_exception_wrapped_with_context() -> None:
    chat = ScriptedChat([RuntimeError("connection reset")])
    with pytest.raises(OrganizeError, match="LLM 调用失败.*connection reset"):
        await organize_document(text="一段资料", kind="free", chat_fn=chat)


@pytest.mark.asyncio
async def test_organize_extra_instruction_reaches_prompt_and_is_capped() -> None:
    chat = ScriptedChat([json.dumps(FREE_OK, ensure_ascii=False)])
    await organize_document(
        text="资料", kind="free", extra="聚焦银行板块", chat_fn=chat
    )
    assert "聚焦银行板块" in chat.calls[0]["messages"][1]["content"]

    with pytest.raises(OrganizeError, match="过长"):
        await organize_document(
            text="资料", kind="free", extra="x" * 3000, chat_fn=ScriptedChat([])
        )


# ── 输出预算与推理模型（截断可见性） ────────────────────────────────


_FAKE_CONFIG = LLMConfig(
    api_key="k", base_url="https://gw.example/v1", model="m", protocol="openai"
)


def _patch_chat_meta(monkeypatch, responses: list) -> list[dict]:
    """替换默认 chat 工厂下的 chat_with_meta：responses 为 (text, meta) 序列。"""
    calls: list[dict] = []

    async def fake(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        if not responses:
            raise AssertionError("chat_with_meta 脚本耗尽：调用次数超出预期")
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(doc_organize, "chat_with_meta", fake)
    return calls


def test_env_int_reads_positive_ints_with_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("DOC_ORGANIZE_TEST_BUDGET", "1234")
    assert doc_organize._env_int("DOC_ORGANIZE_TEST_BUDGET", 99) == 1234
    for bad in ("垃圾", "-5", "0", " "):
        monkeypatch.setenv("DOC_ORGANIZE_TEST_BUDGET", bad)
        assert doc_organize._env_int("DOC_ORGANIZE_TEST_BUDGET", 99) == 99, bad
    monkeypatch.delenv("DOC_ORGANIZE_TEST_BUDGET")
    assert doc_organize._env_int("DOC_ORGANIZE_TEST_BUDGET", 99) == 99


@pytest.mark.asyncio
async def test_truncated_output_surfaces_actionable_error(monkeypatch) -> None:
    """推理预算耗尽把可见 JSON 截断：必须报「调预算」而不是哑的 JSON 提取失败。"""
    _patch_chat_meta(
        monkeypatch, [("", {"finish_reason": "length", "has_reasoning": True})]
    )
    with pytest.raises(OrganizeTruncatedError, match="输出预算.*MAX_TOKENS"):
        await organize_document(text="资料", kind="free", config=_FAKE_CONFIG)


@pytest.mark.asyncio
async def test_reasoning_only_output_surfaces_actionable_error(monkeypatch) -> None:
    _patch_chat_meta(
        monkeypatch, [("", {"finish_reason": "stop", "has_reasoning": True})]
    )
    with pytest.raises(OrganizeTruncatedError, match="推理"):
        await organize_document(text="资料", kind="free", config=_FAKE_CONFIG)


def test_thinking_off_body_default_and_opt_out(monkeypatch) -> None:
    """默认关思考（JSON 抽取用不着）；只有精确 false 才恢复。"""
    monkeypatch.delenv("DOC_ORGANIZE_DISABLE_THINKING", raising=False)
    assert doc_organize._thinking_off_body() == {"thinking": {"type": "disabled"}}
    monkeypatch.setenv("DOC_ORGANIZE_DISABLE_THINKING", "FALSE")  # 归一大小写
    assert doc_organize._thinking_off_body() is None
    monkeypatch.setenv("DOC_ORGANIZE_DISABLE_THINKING", "true")  # 只有 false 算关
    assert doc_organize._thinking_off_body() == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_normal_output_passes_through_meta_path(monkeypatch) -> None:
    calls = _patch_chat_meta(
        monkeypatch,
        [
            (
                json.dumps(FREE_OK, ensure_ascii=False),
                {"finish_reason": "stop", "has_reasoning": False},
            )
        ],
    )
    result = await organize_document(text="资料", kind="free", config=_FAKE_CONFIG)
    assert result["payload"]["direction"] == FREE_OK["direction"]
    assert calls[0]["kwargs"]["max_tokens"] == doc_organize._FINAL_MAX_TOKENS
    assert calls[0]["kwargs"]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_strict_gateway_400_falls_back_without_thinking_param(
    monkeypatch,
) -> None:
    """严格网关不认 thinking 参数（400）：去掉后重试一次，结果不受影响。"""
    req = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    bad = httpx.HTTPStatusError(
        "400", request=req, response=httpx.Response(400, request=req)
    )
    calls = _patch_chat_meta(
        monkeypatch,
        [
            bad,
            (
                json.dumps(FREE_OK, ensure_ascii=False),
                {"finish_reason": "stop", "has_reasoning": False},
            ),
        ],
    )
    result = await organize_document(text="资料", kind="free", config=_FAKE_CONFIG)
    assert result["payload"]["direction"] == FREE_OK["direction"]
    assert calls[0]["kwargs"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[1]["kwargs"]["extra_body"] is None, "回退重试必须不带 thinking 参数"


@pytest.mark.asyncio
async def test_gateway_500_does_not_retry(monkeypatch) -> None:
    """非 400 的错误不触发回退重试（网络/服务端错误照旧上抛）。"""
    req = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    bad = httpx.HTTPStatusError(
        "502", request=req, response=httpx.Response(502, request=req)
    )
    calls = _patch_chat_meta(monkeypatch, [bad])
    with pytest.raises(OrganizeError, match="LLM 调用失败"):
        await organize_document(text="资料", kind="free", config=_FAKE_CONFIG)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_map_truncated_chunk_is_skipped_like_bad_json(monkeypatch) -> None:
    """单段被推理预算耗尽：按提取失败同级跳过，其余段照常合并。"""
    text = "\n\n".join(["甲" * 40, "乙" * 40])
    calls = _patch_chat_meta(
        monkeypatch,
        [
            ("", {"finish_reason": "length", "has_reasoning": True}),
            (
                json.dumps({"summary": "乙段要点", "items": []}, ensure_ascii=False),
                {"finish_reason": "stop", "has_reasoning": False},
            ),
            (
                json.dumps(FREE_OK, ensure_ascii=False),
                {"finish_reason": "stop", "has_reasoning": False},
            ),
        ],
    )
    result = await organize_document(
        text=text, kind="free", config=_FAKE_CONFIG, chunk_chars=60, max_chunks=8
    )
    assert len(calls) == 3, "1 段截断跳过 + 1 段成功 + reduce"
    reduce_user = calls[-1]["messages"][1]["content"]
    assert "乙段要点" in reduce_user
    assert result["payload"]["direction"] == FREE_OK["direction"]
    assert result["chunks_used"] == 3


@pytest.mark.asyncio
async def test_map_all_truncated_reports_budget_hint(monkeypatch) -> None:
    text = "\n\n".join(["甲" * 40, "乙" * 40])
    _patch_chat_meta(
        monkeypatch,
        [("", {"finish_reason": "length", "has_reasoning": True})] * 2,
    )
    with pytest.raises(OrganizeTruncatedError, match="2/2 段.*预算"):
        await organize_document(
            text=text, kind="free", config=_FAKE_CONFIG, chunk_chars=60, max_chunks=8
        )


# ── map-reduce ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_organize_map_reduce_flow() -> None:
    text = "\n\n".join(["甲" * 40, "乙" * 40, "丙" * 40])
    partials = [
        json.dumps({"summary": "甲段要点", "items": []}, ensure_ascii=False),
        json.dumps({"summary": "乙段要点", "items": []}, ensure_ascii=False),
        json.dumps({"summary": "丙段要点", "items": []}, ensure_ascii=False),
    ]
    final = json.dumps(FREE_OK, ensure_ascii=False)
    chat = ScriptedChat(partials + [final])

    result = await organize_document(
        text=text, kind="free", chat_fn=chat, chunk_chars=60, max_chunks=8
    )

    assert chat.call_count == 4, "3 段 map + 1 次 reduce"
    assert result["chunks_used"] == 4
    first_user = chat.calls[0]["messages"][1]["content"]
    assert "第 1/3 段" in first_user
    reduce_user = chat.calls[-1]["messages"][1]["content"]
    assert "甲段要点" in reduce_user and "丙段要点" in reduce_user
    assert result["payload"]["direction"] == FREE_OK["direction"]


@pytest.mark.asyncio
async def test_organize_paper_map_reduce_merges_factor_fragments() -> None:
    """长论文（超单次上限）走 map-reduce：各段因子碎片必须进 reduce 提示词。"""
    text = "\n\n".join(["甲" * 40, "乙" * 40, "丙" * 40])
    partials = [
        json.dumps(
            {"summary": "引言", "items": [{"name": "MOM_12_2"}]}, ensure_ascii=False
        ),
        json.dumps({"summary": "方法", "items": [{"name": "WML"}]}, ensure_ascii=False),
        json.dumps({"summary": "结论", "items": []}, ensure_ascii=False),
    ]
    final = json.dumps(PAPER_OK, ensure_ascii=False)
    chat = ScriptedChat(partials + [final])

    result = await organize_document(
        text=text, kind="paper", chat_fn=chat, chunk_chars=60, max_chunks=8
    )

    assert chat.call_count == 4, "3 段 map + 1 次 reduce"
    first_user = chat.calls[0]["messages"][1]["content"]
    assert "第 1/3 段" in first_user
    assert doc_organize.MAP_ITEM_HINTS["paper"] in first_user, (
        "map 段要用论文口径的因子卡提示"
    )
    reduce_user = chat.calls[-1]["messages"][1]["content"]
    assert "MOM_12_2" in reduce_user and "WML" in reduce_user
    assert result["payload"]["factors"][0]["name"] == "MomVol"
    assert result["chunks_used"] == 4


@pytest.mark.asyncio
async def test_organize_map_partial_failure_is_skipped() -> None:
    text = "\n\n".join(["甲" * 40, "乙" * 40])
    chat = ScriptedChat(
        [
            "这一段的模型输出坏了",
            json.dumps({"summary": "乙段要点", "items": []}, ensure_ascii=False),
            json.dumps(FREE_OK, ensure_ascii=False),
        ]
    )
    result = await organize_document(
        text=text, kind="free", chat_fn=chat, chunk_chars=60, max_chunks=8
    )
    assert result["chunks_used"] == 3
    reduce_user = chat.calls[-1]["messages"][1]["content"]
    assert "乙段要点" in reduce_user


@pytest.mark.asyncio
async def test_organize_all_map_partials_failed_raises() -> None:
    text = "\n\n".join(["甲" * 40, "乙" * 40])
    chat = ScriptedChat(["坏输出一", "坏输出二"])
    with pytest.raises(OrganizeError, match="分段提取"):
        await organize_document(
            text=text, kind="free", chat_fn=chat, chunk_chars=60, max_chunks=8
        )
    assert chat.call_count == 2, "全失败就不该再调 reduce"


@pytest.mark.asyncio
async def test_organize_truncation_flag_flows_to_result() -> None:
    text = "\n\n".join(f"第 {i} 段。" * 8 for i in range(12))
    partial = json.dumps({"summary": "要点", "items": []}, ensure_ascii=False)
    chat = ScriptedChat([partial, partial, json.dumps(FREE_OK, ensure_ascii=False)])
    result = await organize_document(
        text=text, kind="free", chat_fn=chat, chunk_chars=50, max_chunks=2
    )
    assert result["truncated"] is True
    assert result["chunks_used"] == 3, "截断后 2 段 map + 1 次 reduce"


# ── 落库 ────────────────────────────────────────────────────────────


def _mk_doc(tmp_path: Path, **overrides) -> dict:
    md = tmp_path / "full.md"
    if not md.exists():
        md.write_text("# 资料\n\n内容", encoding="utf-8")
    doc = {"doc_id": "d1", "status": "parsed", "md_path": str(md)}
    doc.update(overrides)
    return doc


@pytest.mark.asyncio
async def test_organize_and_store_persists_all_fields(tmp_path: Path) -> None:
    store = FakeStore()
    chat = ScriptedChat([json.dumps(FREE_OK, ensure_ascii=False)])
    doc = _mk_doc(tmp_path)

    result = await organize_and_store(store, doc, kind="free", chat_fn=chat)

    doc_id, fields = store.updates[-1]
    assert doc_id == "d1"
    assert fields["status"] == "organized"
    assert fields["organize_kind"] == "free"
    assert fields["organize_prompt_version"] == doc_organize.PROMPT_VERSION
    assert fields["organized_text"] == result["markdown"]
    assert fields["organized_at"].tzinfo is not None, "时间必须是 aware UTC"


@pytest.mark.asyncio
async def test_organize_and_store_rejects_unparsed_status(tmp_path: Path) -> None:
    doc = _mk_doc(tmp_path, status="parsing", md_path=None)
    with pytest.raises(OrganizeError, match="parsing"):
        await organize_and_store(
            FakeStore(), doc, kind="free", chat_fn=ScriptedChat([])
        )


@pytest.mark.asyncio
async def test_organize_and_store_rejects_missing_artifact(tmp_path: Path) -> None:
    doc = _mk_doc(tmp_path, md_path=str(tmp_path / "gone.md"))
    with pytest.raises(OrganizeError, match="不在盘上"):
        await organize_and_store(
            FakeStore(), doc, kind="free", chat_fn=ScriptedChat([])
        )


# ── 真库落库 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_db_organize_roundtrip(tmp_path: Path) -> None:
    from sqlalchemy import text

    from backend.services.engine.alpha_agent.doc_store import get_doc_store
    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    store = get_doc_store()
    await store.ensure_tables()
    user = f"t-organize-{uuid.uuid4().hex[:10]}"
    doc_id = f"t-doc-{uuid.uuid4().hex[:12]}"
    md = tmp_path / "full.md"
    md.write_text("# 资料\n\n正文", encoding="utf-8")
    try:
        await store.create_doc(
            doc_id=doc_id,
            user_id=user,
            filename="paper.pdf",
            ext=".pdf",
            size_bytes=1,
            sha256=uuid.uuid4().hex * 2,
            original_path=str(tmp_path / "original.pdf"),
        )
        await store.update_doc(doc_id, status="parsed", md_path=str(md))
        doc = await store.get_doc(doc_id, user_id=user)

        chat = ScriptedChat([json.dumps(FREE_OK, ensure_ascii=False)])
        result = await organize_and_store(store, doc, kind="free", chat_fn=chat)

        row = await store.get_doc(doc_id, user_id=user)
        assert row["status"] == "organized"
        assert row["organize_kind"] == "free"
        assert row["organize_prompt_version"] == doc_organize.PROMPT_VERSION
        assert row["organized_text"] == result["markdown"]
        assert row["organized_at"].endswith("Z"), "组织时间必须是 UTC Z 序列化"
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM rd_agent_docs WHERE user_id = :u"), {"u": user}
            )
        await close_database()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
