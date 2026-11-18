"""宿主晚间研究轮（``scripts/night_pool_agent.py``）测试。

覆盖 2026-10-14 改接（戊1）：
1. **大脑改接**：``call_brain`` 直连 OpenAI 兼容端点（原 dsh CLI + BayMax MCP
   栈已退役）——glm* 前缀走 ``GLM_*`` 键、``QM_NIGHT_POOL_MODEL`` 可覆写、
   失败重试 1 次、``finish_reason=length`` 截断即 raise（半截 JSON 不可救）；
2. **空壳不再写池文件**：大脑失败 / 输出非 JSON / pool 为空 → **不写**
   ``{target}_agent_picks.json``——该后缀在 ``decision_context_source.pool_path``
   里**优先于** ``_picks.json``，空壳会遮蔽在产池（2026-09-15 事故口径：主跑失败
   落 0 只池 → 当天全系统无候选池）；研究纪要与对话流照写（展示面不静默变空）；
3. **成功路径**：池文件按目标交易日命名 + usage 进对话行（/token-usage 可计）。
4. **``_load_env``**：环境变量优先，repo .env 补缺（与 live_model_analysis 同口径）。

脚本不是包内模块，用 importlib 从 ``scripts/`` 直接装载（import 无副作用：
只插 sys.path、拼路径常量，不读 .env、不连桥、不建目录）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import requests

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "night_pool_agent.py"

SESSION = "20261013"
TARGET = "20261014"

_BRAIN_PAYLOAD = {
    "date": "2026-10-13",
    "market_view": "震荡偏强，主线在半导体",
    "sectors": [{"name": "半导体", "strength": "强", "reason": "资金回流"}],
    "pool": [
        {"code": "600338.SH", "name": "西藏珠峰", "score": 88, "reason": "板块主线"},
        {"code": "688183", "name": "生益电子", "score": 81, "reason": "强势延续"},
    ],
}
_BRAIN_JSON = json.dumps(_BRAIN_PAYLOAD, ensure_ascii=False)
_BRAIN_USAGE = {"prompt_tokens": 1200, "completion_tokens": 800, "total_tokens": 2000}


@pytest.fixture(scope="module")
def npa():
    spec = importlib.util.spec_from_file_location(
        "night_pool_agent_under_test", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _ok_body(content: str, finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": dict(_BRAIN_USAGE),
    }


def _install_stub_modules(monkeypatch, *, market_state_text="盘面状态（测试）"):
    """把 run() 的数据/桥依赖换成桩：market_state / event_radar / risk_list /
    agent_tools.brokers.tdx_bridge（都在 run() 里 import，逐个顶掉 sys.modules 项）。"""
    ms = types.ModuleType("market_state")
    ms.build_market_state = lambda broker=None: market_state_text
    monkeypatch.setitem(sys.modules, "market_state", ms)

    er = types.ModuleType("event_radar")
    er.tag_events = lambda codes: {}
    monkeypatch.setitem(sys.modules, "event_radar", er)

    rl = types.ModuleType("risk_list")
    rl.load_risk = lambda: {}
    monkeypatch.setitem(sys.modules, "risk_list", rl)

    at = types.ModuleType("agent_tools")
    at.__path__ = []
    ab = types.ModuleType("agent_tools.brokers")
    ab.__path__ = []
    at.brokers = ab
    tb = types.ModuleType("agent_tools.brokers.tdx_bridge")
    tb.TdxBridgeBroker = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "agent_tools", at)
    monkeypatch.setitem(sys.modules, "agent_tools.brokers", ab)
    monkeypatch.setitem(sys.modules, "agent_tools.brokers.tdx_bridge", tb)


@pytest.fixture()
def sandbox(npa, monkeypatch, tmp_path):
    """run() 的全隔离环境：ROOT/两个产物目录指向 tmp_path，数据与桥全桩。"""
    root = tmp_path / "repo-harness"
    picks = root / "data" / "reports" / "stock_picks"
    picks.mkdir(parents=True)
    monkeypatch.setattr(npa, "ROOT", root)
    monkeypatch.setattr(npa, "NIGHT_POOL_DIR", root / "logs" / "night_pool")
    monkeypatch.setattr(npa, "PICKS_DIR", picks)
    monkeypatch.setattr(
        npa, "PUSH_STATE", root / "data" / "logs" / ".night_pool_push_state.json"
    )
    monkeypatch.setattr(
        npa,
        "build_candidates",
        lambda session: (
            [
                {
                    "code": "600338.SH",
                    "name": "西藏珠峰",
                    "chg1": 3.2,
                    "amount_yi": 12.5,
                    "score": 88.0,
                },
                {
                    "code": "688183.SH",
                    "name": "生益电子",
                    "chg1": 1.1,
                    "amount_yi": 8.0,
                    "score": 81.0,
                },
            ],
            session,
        ),
    )
    monkeypatch.setattr(npa, "_news_block", lambda d0: "")
    # 研究总控 brief：默认空（不读真实 repo 档案）；注入路径由专项测试覆盖
    monkeypatch.setattr(npa, "_research_block", lambda target: "")
    # 证券主表桩：与候选同口径（真实环境里候选代码本身就来自这张表）
    monkeypatch.setattr(
        npa,
        "load_names_industry",
        lambda: {"600338.SH": "西藏珠峰", "688183.SH": "生益电子"},
    )
    # 板块表桩（写池时逐票补 industry；真实环境读 instrument_detail.parquet）
    monkeypatch.setattr(
        npa,
        "load_industry_map",
        lambda: {"600338.SH": "有色金属", "688183.SH": "电子元件"},
    )
    # QQ 推送桩：run() 产池后会推一条，测试绝不碰真网络
    monkeypatch.setattr(npa, "notify_qq", lambda title, content: True)
    _install_stub_modules(monkeypatch)
    return root


def test_news_block_includes_p0_decisions(npa, tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setattr(npa, "ROOT", tmp_path)
    nb = tmp_path / "data" / "news_brief"
    nb.mkdir(parents=True)
    (nb / "latest.json").write_text(_json.dumps({
        "ts": "2026-11-18T19:05:00+08:00",
        "text": " molecule ",
        "decisions": [{
            "code": "600519.SH", "name": "贵州茅台", "decision": "P0",
            "event_type": "监管处罚", "reason": "硬利空+pre5>5%",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    text = npa._news_block("20261118")
    assert "P0" in text and "600519.SH" in text
    assert "P0 禁买" in text


def test_news_block_merges_fast_p0_decisions(npa, tmp_path, monkeypatch):
    import json as _json

    today = npa.now_cn().strftime("%Y%m%d")
    monkeypatch.setattr(npa, "ROOT", tmp_path)
    nb = tmp_path / "data" / "news_brief"
    nb.mkdir(parents=True)
    (nb / "latest.json").write_text(_json.dumps({
        "ts": "2026-11-18T19:05:00+08:00",
        "text": " molecule ",
        "decisions": [],
    }, ensure_ascii=False), encoding="utf-8")
    (nb / "news_decisions.json").write_text(_json.dumps({
        "as_of": "2026-11-18T21:00:00+08:00",
        "items": [{
            "decision_id": f"{today}-301585.SZ-fast-1",
            "code": "301585.SZ", "name": "某股", "decision": "P0",
            "event_type": "监管处罚", "reason": "硬利空+pre5>5%",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    text = npa._news_block("20261118")
    assert "P0 某股(301585.SZ)" in text


def _dialogue_rows(root: Path) -> list[dict]:
    lf = (
        root
        / "data"
        / "agent_data_astock"
        / "market-research"
        / "log"
        / "2026-10-14"
        / "log.jsonl"
    )
    if not lf.is_file():
        return []
    return [json.loads(x) for x in lf.read_text(encoding="utf-8").splitlines() if x]


def _picks_file(root: Path) -> Path:
    return root / "data" / "reports" / "stock_picks" / f"{TARGET}_agent_picks.json"


# ── 1. call_brain：直连 OpenAI 兼容端点 ─────────────────────────────────


def _env_stub(**over):
    env = {
        "OPENAI_API_BASE": "https://api.example.com/v1",
        "OPENAI_API_KEY": "k-openai",
        "GLM_API_BASE": "https://glm.example.com/v1",
        "GLM_API_KEY": "k-glm",
    }
    env.update(over)
    return env


def test_call_brain_posts_chat_completions_with_resolved_model(npa, monkeypatch):
    monkeypatch.setattr(npa, "_load_env", lambda: _env_stub())
    calls: list[tuple] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json, timeout))
        return _Resp(_ok_body("研究结论"))

    monkeypatch.setattr(requests, "post", fake_post)
    content, usage = npa.call_brain("任务文本")

    assert content == "研究结论"
    assert usage == _BRAIN_USAGE
    url, headers, payload, timeout = calls[0]
    assert url == "https://glm.example.com/v1/chat/completions"  # 默认模型 glm 系
    assert headers["Authorization"] == "Bearer k-glm"
    assert payload["model"] == npa.DEFAULT_BRAIN_MODEL
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"] == "任务文本"
    # glm 族始终思考（thinking 不可关，error 1210）：必须带 low 档 + 16k 预算，
    # 否则 reasoning 吃光 completion 预算 → length 截断（2026-10-15 首跑 exit 3 事故）
    assert payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] == 16000
    assert timeout == npa.BRAIN_TIMEOUT_S


def test_call_brain_model_env_override_switches_key_prefix(npa, monkeypatch):
    monkeypatch.setattr(
        npa, "_load_env", lambda: _env_stub(QM_NIGHT_POOL_MODEL="deepseek-v4-pro")
    )
    calls: list[tuple] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json, timeout))
        return _Resp(_ok_body("ok"))

    monkeypatch.setattr(requests, "post", fake_post)
    npa.call_brain("任务文本")

    url, headers, payload, _ = calls[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k-openai"
    assert payload["model"] == "deepseek-v4-pro"
    # reasoning_effort 是 GLM 端点方言，非 glm 模型不得注入
    assert "reasoning_effort" not in payload


def test_call_brain_missing_keys_raises(npa, monkeypatch):
    monkeypatch.setattr(npa, "_load_env", lambda: {})
    with pytest.raises(RuntimeError, match="缺失"):
        npa.call_brain("任务文本")


def test_call_brain_retries_once_then_raises(npa, monkeypatch):
    monkeypatch.setattr(npa, "_load_env", lambda: _env_stub())
    attempts = {"n": 0}

    def fake_post(*a, **k):
        attempts["n"] += 1
        raise ConnectionError("网络不通")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(ConnectionError):
        npa.call_brain("任务文本")
    assert attempts["n"] == 2  # 重试 1 次


def test_call_brain_truncated_finish_reason_raises(npa, monkeypatch):
    monkeypatch.setattr(npa, "_load_env", lambda: _env_stub())
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: _Resp(_ok_body("半截", finish="length"))
    )
    with pytest.raises(RuntimeError, match="截断"):
        npa.call_brain("任务文本")


# ── 2. _load_env：环境变量优先，repo .env 补缺 ──────────────────────────


def test_load_env_env_wins_file_fills_gaps(npa, monkeypatch, tmp_path):
    envf = tmp_path / ".env"
    envf.write_text(
        "OPENAI_API_KEY=from-file\nOPENAI_API_BASE=https://file.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(npa, "ENV_FILE", envf)
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    env = npa._load_env()

    assert env["OPENAI_API_KEY"] == "from-env"  # env 优先
    assert env["OPENAI_API_BASE"] == "https://file.example/v1"  # 文件补缺


# ── 3. run()：空壳不写池文件（遮蔽在产池的隐患） ─────────────────────────


def test_brain_failure_writes_notes_and_dialogue_but_no_agent_picks(
    npa, sandbox, monkeypatch
):
    def boom(task, *a, **k):
        raise RuntimeError("端点 500")

    monkeypatch.setattr(npa, "call_brain", boom)
    rc = npa.run(SESSION, TARGET)

    assert rc != 0
    assert not _picks_file(sandbox).exists()  # 关键：不落空壳
    notes = sandbox / "logs" / "night_pool" / f"{SESSION}.md"
    assert notes.is_file() and "研究 agent 失败" in notes.read_text(encoding="utf-8")
    rows = _dialogue_rows(sandbox)
    assert rows and "研究 agent 失败" in rows[0]["new_messages"][-1]["content"]


def test_malformed_brain_output_writes_no_agent_picks(npa, sandbox, monkeypatch):
    monkeypatch.setattr(
        npa, "call_brain", lambda task, *a, **k: ("没有 JSON 的散文", None)
    )
    rc = npa.run(SESSION, TARGET)

    assert rc != 0
    assert not _picks_file(sandbox).exists()
    assert _dialogue_rows(sandbox)  # 展示面照写


def test_empty_pool_writes_no_agent_picks(npa, sandbox, monkeypatch):
    empty = json.dumps({"date": "2026-10-13", "market_view": "x", "pool": []})
    monkeypatch.setattr(npa, "call_brain", lambda task, *a, **k: (empty, None))
    rc = npa.run(SESSION, TARGET)

    assert rc != 0
    assert not _picks_file(sandbox).exists()


def test_valid_pool_writes_agent_picks_with_usage_row(npa, sandbox, monkeypatch):
    monkeypatch.setattr(
        npa, "call_brain", lambda task, *a, **k: (_BRAIN_JSON, dict(_BRAIN_USAGE))
    )
    rc = npa.run(SESSION, TARGET)

    assert rc == 0
    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert doc["date"] == TARGET  # 池按**目标交易日**命名（09:35 消费的就是当日池）
    assert doc["session"] == SESSION
    # 裸 6 位码按主表规范化成后缀式（2026-10-19 代码校验：下游行情键是后缀式）
    assert [p["code"] for p in doc["picks"]] == ["600338.SH", "688183.SH"]
    assert doc["picks"][0]["side"] == "HOLD"  # 占位侧，无信息量（读侧不展示）
    assert doc["picks"][0]["reason"] == "板块主线"
    # 板块随池落盘（2026-10-20 用户：「top20 的股票板块都要发我」）
    assert doc["picks"][0]["industry"] == "有色金属"
    assert doc["picks"][1]["industry"] == "电子元件"
    rows = _dialogue_rows(sandbox)
    assert rows[0]["signature"] == "market-research"
    assert rows[0]["kind"] == "night_pool"
    assert rows[0]["usage"] == _BRAIN_USAGE


# ── 3a. 研究总控 brief 注入 + 富化落盘（2026-10-20） ────────────────────


def test_run_injects_research_brief_into_prompt(npa, sandbox, monkeypatch):
    """研究总控夜间综合稿必须进产池提示词（方向/资金/新闻/模型分数同口径）。"""
    monkeypatch.setattr(
        npa,
        "_research_block",
        lambda target: (
            "【研究总控·昨日复盘与今日要点】\n- 明日方向：震荡偏强，半导体主线"
        ),
    )
    seen: dict = {}

    def fake_brain(task, *a, **k):
        seen["task"] = task
        return _BRAIN_JSON, dict(_BRAIN_USAGE)

    monkeypatch.setattr(npa, "call_brain", fake_brain)

    assert npa.run(SESSION, TARGET) == 0

    assert "研究总控结论" in seen["task"]
    assert "震荡偏强，半导体主线" in seen["task"]
    # 池文件标记本轮确实读过研究总控稿（消费面可辨「有无综合结论」）
    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert doc["research_brief"] is True


def test_run_without_brief_marks_it_and_still_produces(npa, sandbox, monkeypatch):
    """研究总控未产出不挡产池：提示词注明「本次无」，池照落、标记 False。"""
    seen: dict = {}

    def fake_brain(task, *a, **k):
        seen["task"] = task
        return _BRAIN_JSON, dict(_BRAIN_USAGE)

    monkeypatch.setattr(npa, "call_brain", fake_brain)

    assert npa.run(SESSION, TARGET) == 0

    assert "本次无" in seen["task"]
    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert doc["research_brief"] is False


def test_run_writes_direction_capital_news_models_from_fenced_json(
    npa, sandbox, monkeypatch
):
    """Markdown 正文 + ```json 围栏：正文里的花括号不干扰机读块抽取。"""
    payload = {
        **_BRAIN_PAYLOAD,
        "direction": "震荡偏强，主线在半导体，注意高位分化",
        "capital": "增量资金有限，偏存量博弈",
        "news": "半导体政策催化，板块有持续性",
        "models": "CN/batch 平均 fusion +0.002，BUY 占比约 20%",
        "risks": ["高位股分化", "量能不足"],
    }
    content = (
        "## 明日方向\n震荡偏强（集合 {测试} 花括号）。\n\n"
        "## 资金判断\n存量博弈。\n\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"
    )
    monkeypatch.setattr(npa, "call_brain", lambda task, *a, **k: (content, None))

    assert npa.run(SESSION, TARGET) == 0

    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    md = doc["market_direction"]
    assert md["direction"] == "震荡偏强，主线在半导体"  # market_view 仍是短句旧契约
    assert md["summary"] == "震荡偏强，主线在半导体，注意高位分化"
    assert md["capital"] == "增量资金有限，偏存量博弈"
    assert md["news"] == "半导体政策催化，板块有持续性"
    assert md["models"] == "CN/batch 平均 fusion +0.002，BUY 占比约 20%"
    assert md["risks"] == ["高位股分化", "量能不足"]
    # 研究纪要保留 Markdown 正文（展示面不再是裸 JSON）
    notes = sandbox / "logs" / "night_pool" / f"{SESSION}.md"
    assert "## 明日方向" in notes.read_text(encoding="utf-8")


# ── 3b. 产池后的 QQ 推送（板块 + top20） ────────────────────────────────


def _pool_card_inputs():
    payload = {
        "date": "2026-10-13",
        "market_view": "震荡偏强，主线在半导体；注意高位股分化",
        "sectors": [
            {"name": "半导体", "strength": "强", "reason": "资金回流"},
            {"name": "有色", "strength": "中", "reason": "商品价格反弹"},
        ],
    }
    picks = [
        {
            "code": "600338.SH",
            "name": "西藏珠峰",
            "industry": "有色金属",
            "score": 88.0,
        },
        {
            "code": "688183.SH",
            "name": "生益电子",
            "industry": "电子元件",
            "score": 81.0,
        },
        {"code": "300911.SZ", "name": "亿田智能", "industry": "", "score": None},
    ]
    return payload, picks


def test_format_pool_card_has_direction_sectors_and_rows(npa):
    payload, picks = _pool_card_inputs()
    title, content = npa.format_pool_card(TARGET, payload, picks)

    assert title == "🌙 晚间股票池 · 10-14"
    assert "🧭 方向：震荡偏强，主线在半导体；注意高位股分化" in content
    assert "🔥 板块" in content
    assert "　半导体（强）：资金回流" in content
    assert "📦 3 只 · 板块 · 分数" in content
    assert "01 西藏珠峰 600338.SH · 有色金属 · 88.0" in content
    assert "02 生益电子 688183.SH · 电子元件 · 81.0" in content
    assert "03 亿田智能 300911.SZ · —" in content  # 缺板块显示 —，不编造
    assert "━" * 14 in content


def test_format_pool_card_shortens_long_direction(npa):
    payload, picks = _pool_card_inputs()
    payload["market_view"] = "震荡偏强（半导体回流、有色反弹、量能温和），" * 4
    _, content = npa.format_pool_card(TARGET, payload, picks)
    line = next(x for x in content.splitlines() if x.startswith("🧭 方向："))
    assert line.endswith("…") and len(line) <= 60


def test_push_pool_once_per_target_and_switch(npa, monkeypatch, tmp_path):
    monkeypatch.setattr(npa, "PUSH_STATE", tmp_path / "push_state.json")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        npa, "notify_qq", lambda title, content: calls.append((title, content)) or True
    )
    payload, picks = _pool_card_inputs()

    assert npa.push_pool(TARGET, payload, picks, env={"QM_NIGHT_POOL_PUSH": "true"})
    assert not npa.push_pool(TARGET, payload, picks, env={"QM_NIGHT_POOL_PUSH": "true"})
    assert len(calls) == 1  # 同一目标交易日只推一次

    # 词表唯一读法：只有 true 判开（1/yes/on 都算关）
    for raw in ("1", "yes", "on", "false", ""):
        assert not npa.push_pool(
            "20261022", payload, picks, env={"QM_NIGHT_POOL_PUSH": raw}
        ), raw


def test_push_pool_retries_when_delivery_fails(npa, monkeypatch, tmp_path):
    """没送达就不记状态——同止损失败告警的纪律，否则这条推送永远没人看到。"""
    monkeypatch.setattr(npa, "PUSH_STATE", tmp_path / "push_state.json")
    monkeypatch.setattr(npa, "notify_qq", lambda title, content: False)
    payload, picks = _pool_card_inputs()

    assert not npa.push_pool(TARGET, payload, picks, env={"QM_NIGHT_POOL_PUSH": "true"})
    # 未记状态 → 下一次还能推
    monkeypatch.setattr(npa, "notify_qq", lambda title, content: True)
    assert npa.push_pool(TARGET, payload, picks, env={"QM_NIGHT_POOL_PUSH": "true"})


def test_run_pushes_pool_after_writing_file(npa, sandbox, monkeypatch):
    monkeypatch.setattr(
        npa, "call_brain", lambda task, *a, **k: (_BRAIN_JSON, dict(_BRAIN_USAGE))
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        npa, "notify_qq", lambda title, content: calls.append((title, content)) or True
    )
    monkeypatch.setattr(npa, "PUSH_STATE", sandbox / "push_state.json")
    monkeypatch.setattr(npa, "_load_env", lambda: {})

    assert npa.run(SESSION, TARGET) == 0

    assert len(calls) == 1
    assert "晚间股票池" in calls[0][0]
    assert "有色金属" in calls[0][1]
    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert [p["industry"] for p in doc["picks"]] == ["有色金属", "电子元件"]


# ── 4. 纯函数 ───────────────────────────────────────────────────────────


def test_filter_risk_candidates_matches_suffix_codes_by_6_digits(npa):
    cands = [
        {"code": "600338.SH", "name": "西藏珠峰"},
        {"code": "688183", "name": "生益电子"},
    ]
    kept, dropped = npa.filter_risk_candidates(cands, {"600338": {"reason": "解禁"}})
    assert [c["code"] for c in kept] == ["688183"]
    assert dropped[0][0]["code"] == "600338.SH"


# ── 5. 池行代码校验（2026-10-19：模型写错 code，旧实现逐字落盘） ────────

_NAMES = {
    "300911.SZ": "亿田智能",
    "600930.SH": "华电新能",
    "600338.SH": "西藏珠峰",
    "688183.SH": "生益电子",
    "600000.SH": "浦发银行",
}


def test_validate_pool_rows_corrects_ghost_code_by_unique_name(npa):
    """当日事故原样复现：code=600930.SZ、name=亿田智能、理由里写着 300911.SZ 更正。

    代码不在主表（600930.SZ 不存在）→ 用名称反查唯一命中 → 回填 300911.SZ。
    """
    rows = [{"code": "600930.SZ", "name": "亿田智能", "score": 26.9, "reason": "—"}]
    kept, blocked, notes = npa.validate_pool_rows(rows, _NAMES)

    assert blocked == []
    assert [r["code"] for r in kept] == ["300911.SZ"]
    assert kept[0]["name"] == "亿田智能"
    assert notes and "600930.SZ→300911.SZ" in notes[0]
    assert rows[0]["code"] == "600930.SZ"  # 纯函数：不改入参


def test_validate_pool_rows_blocks_unknown_code_and_name(npa):
    rows = [{"code": "999999.SZ", "name": "查无此股", "score": 10}]
    kept, blocked, _ = npa.validate_pool_rows(rows, _NAMES)

    assert kept == []
    assert blocked and "不在证券主表" in blocked[0][1]


def test_validate_pool_rows_blocks_ambiguous_name(npa):
    """同名多码（主板/中小板历史并存）无法判定 → 拦下而不是赌一个。"""
    names = {**_NAMES, "000001.SZ": "同名股", "600001.SH": "同名股"}
    rows = [{"code": "888888.SZ", "name": "同名股", "score": 10}]
    kept, blocked, _ = npa.validate_pool_rows(rows, names)

    assert kept == []
    assert blocked and "对应多个代码" in blocked[0][1]


def test_validate_pool_rows_code_wins_over_wrong_name(npa):
    """代码命中主表时以代码为准、名称按主表回填（名称错不毁代码）。"""
    rows = [{"code": "600000.SH", "name": "亿田智能", "score": 10}]
    kept, blocked, notes = npa.validate_pool_rows(rows, _NAMES)

    assert blocked == []
    assert (kept[0]["code"], kept[0]["name"]) == ("600000.SH", "浦发银行")
    assert notes and "浦发银行" in notes[0]


def test_validate_pool_rows_normalizes_bare_and_prefixed_codes(npa):
    rows = [
        {"code": "688183", "name": "生益电子", "score": 10},
        {"code": "SH600338", "name": "西藏珠峰", "score": 9},
    ]
    kept, blocked, _ = npa.validate_pool_rows(rows, _NAMES)

    assert blocked == []
    assert [r["code"] for r in kept] == ["688183.SH", "600338.SH"]


def test_validate_pool_rows_dedupes_after_correction(npa):
    """同一只票被模型写了两遍（一遍错码）→ 校正后去重，保留首次出现。"""
    rows = [
        {"code": "300911.SZ", "name": "亿田智能", "score": 30},
        {"code": "600930.SZ", "name": "亿田智能", "score": 26},
    ]
    kept, blocked, _ = npa.validate_pool_rows(rows, _NAMES)

    assert [r["code"] for r in kept] == ["300911.SZ"]
    assert blocked and "重复代码" in blocked[0][1]


def test_run_corrects_and_blocks_before_writing_picks(npa, sandbox, monkeypatch):
    """写盘前校验：错码行被更正，幽灵行被拦，其余池行照常落盘。"""
    payload = {
        "date": "2026-10-13",
        "market_view": "震荡",
        "pool": [
            {"code": "600338.SH", "name": "西藏珠峰", "score": 88, "reason": "主线"},
            {
                "code": "600930.SZ",
                "name": "亿田智能",
                "score": 26,
                "reason": "小盘动量",
            },
            {"code": "999999.SZ", "name": "查无此股", "score": 5, "reason": "幽灵"},
        ],
    }
    monkeypatch.setattr(
        npa, "call_brain", lambda task, *a, **k: (json.dumps(payload), None)
    )
    monkeypatch.setattr(
        npa,
        "load_names_industry",
        lambda: {
            "300911.SZ": "亿田智能",
            "600930.SH": "华电新能",
            "600338.SH": "西藏珠峰",
            "688183.SH": "生益电子",
        },
    )
    assert npa.run(SESSION, TARGET) == 0

    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert [p["code"] for p in doc["picks"]] == ["600338.SH", "300911.SZ"]
    assert "999999" not in _picks_file(sandbox).read_text(encoding="utf-8")


def test_run_fails_open_when_security_master_is_empty(npa, sandbox, monkeypatch):
    """主表与候选都为空 → 不校验、照写（宁可有未校验码，不可整池丢失）。"""
    monkeypatch.setattr(npa, "load_names_industry", lambda: {})
    # 候选行也提供不了真值（脏行）——否则候选代码本身就会把真值表撑起来
    monkeypatch.setattr(
        npa, "build_candidates", lambda session: ([{"code": "", "name": ""}], session)
    )
    payload = {
        "date": "2026-10-13",
        "market_view": "震荡",
        "pool": [{"code": "600000.SH", "name": "浦发银行", "score": 10, "reason": "—"}],
    }
    monkeypatch.setattr(
        npa, "call_brain", lambda task, *a, **k: (json.dumps(payload), None)
    )

    assert npa.run(SESSION, TARGET) == 0
    doc = json.loads(_picks_file(sandbox).read_text(encoding="utf-8"))
    assert [p["code"] for p in doc["picks"]] == ["600000.SH"]
