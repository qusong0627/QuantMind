"""P2.1a 差分验收：真实 LLM 输出语料重放（金样）。

金样怎么来的（不是手写的，也不是「跑一遍现在代码存下来」）
----------------------------------------------------------
`backend/tests/fixtures/decision_corpus_real.jsonl` 的 7 条文本，取自隔壁 BayMax
2026-08-25 → 2026-09-23 三只交易 agent（`deepseek-v4-flash` / `deepseek-v4-pro` /
`glm-5.3-flash`）的真实 `log.jsonl` 对话流——**退役后这批数据就不存在了**，所以把
代表性的那一小撮连同期望值一起固化成金样。

关键点：期望值不是本仓实现的自拍照，而是**两个独立实现差分对齐过**的结果——每一条
入样时都跑过隔壁的 `live_prompt_context.parse_intraday_decision`，逐行比对
（action / code / pct / pct 三态 / 价位 / 理由）一致才允许写进 `expected`
（生成脚本见 `docs/local/quant-trader-migration-plan.md` 的 P2 验证记录）。
语料全量差分结论：**612 条真实输出，状态判定 612/612 一致，行级 0 处语义差异**
（唯一差异是一条 `reason` 的结尾空格，本仓 `strip` 掉）。

为什么这组样本值得钉死（每一条对应一类真实形态）
------------------------------------------------
===============  =========================================================
`fence_prose`    散文 + ```json 围栏——**实测 612/612 条真实输出都带前导散文**，
                 442 条带围栏；「整段即 JSON」在真实数据里**一次都没出现**。
`watch_ratchet`  守护单 + 棘轮，`watch` 是盘中链最主流的动作（1136 行 vs 卖出 54 行）。
`multi_row`      多行多动作：`watch` 与 `hold` 混排，`skipped_rows` 必须为 0。
`pct_missing`    **三态对照样本**：同一份输出里 3 条 `watch` 是 `given`、
                 3 条 `hold` 是 `missing`——「没表达比例」与「表达为 0」分开。
`pct_given`      最小可解析样本（541/554 字符，卖出 50%）。
`price_levels`   同码两行（先卖后挂守护）带止损/止盈绝对价——P1.3b 的字段就来自这里。
`parse_failed`   散文式复盘报告（408 字符）——两实现一致判负。钉住它是为了防止
                 「抽不出来就算了」退化：这类输出必须留下 `parse_failed` 痕迹。
===============  =========================================================

公开仓库约束
------------
语料来自私有生产环境，入库前逐条过了禁用模式闸（内网 IP / 端口 / 本机路径 / 密钥
形状），且本文件**每次都重新断言**——将来重新生成语料时混进这些字样会当场红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.shared.decision.contract import (
    STATUS_OK,
    STATUS_PARSE_FAILED,
    parse_decisions,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "decision_corpus_real.jsonl"
)

#: 入库禁用模式（与生成脚本同一套）。这不是「风格检查」：仓库是公开的，
#: 内网拓扑一旦进去就是永久泄漏（历史里删不掉）。
FORBIDDEN_PATTERNS = {
    "内网 IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "内网主机名": re.compile(
        r"\b(?:localhost|[\w.-]+\.(?:internal|local|lan))\b", re.I
    ),
    "本机路径": re.compile(r"/home/|/data/|/opt/|/mnt/|C:\\\\", re.I),
    "疑似密钥": re.compile(r"\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{8,}"),
    "凭据字面量": re.compile(
        r"(?:password|passwd|secret|api[_-]?key|token)\s*[:=]", re.I
    ),
    "端口": re.compile(r":\d{4,5}\b"),
}

#: 行级字段的比对口径（fixture 的 expected 与解析结果逐字段相等）。
ROW_FIELDS = (
    "action",
    "code",
    "name",
    "pct",
    "pct_state",
    "stop_loss",
    "take_profit",
    "move_stop",
    "invalidation",
    "confidence",
    "risk_amount",
)


def _load() -> list[dict]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


CORPUS = _load()


def _row_state(decision) -> dict:
    return {
        "action": decision.action,
        "code": decision.code,
        "name": decision.name,
        "pct": decision.pct.value,
        "pct_state": decision.pct.state,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
        "move_stop": decision.move_stop,
        "invalidation": decision.invalidation,
        "confidence": decision.confidence,
        "risk_amount": decision.risk_amount,
    }


class TestCorpusIsSafeToPublish:
    """语料必须是可公开的：这一组红了说明**不要提交这份 fixture**。"""

    def test_no_internal_marker_in_any_text(self) -> None:
        leaks: list[str] = []
        for item in CORPUS:
            for label, pattern in FORBIDDEN_PATTERNS.items():
                for hit in pattern.findall(item["text"]):
                    leaks.append(f"{item['id']} 命中{label}: {hit!r}")
        assert not leaks, "语料含不可公开内容：\n" + "\n".join(leaks)

    def test_expected_values_contain_no_internal_marker(self) -> None:
        leaks: list[str] = []
        for item in CORPUS:
            blob = json.dumps(item["expected"], ensure_ascii=False)
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(blob):
                    leaks.append(f"{item['id']} 命中{label}")
        assert not leaks, "期望值含不可公开内容：\n" + "\n".join(leaks)


class TestCorpusReplay:
    """逐条重放：解析结果必须与差分验证过的期望值逐字段相等。"""

    @pytest.mark.parametrize("item", CORPUS, ids=[i["id"] for i in CORPUS])
    def test_replay_matches_pinned_expectation(self, item: dict) -> None:
        expected = item["expected"]
        batch = parse_decisions(item["text"], schema=expected.get("schema", "intraday"))

        # 失败样本：必须仍然是失败，且失败态是 parse_failed（不是 api/空输出）
        if expected["status"] == STATUS_PARSE_FAILED:
            assert batch.status == STATUS_PARSE_FAILED, batch.status
            return

        assert batch.status == STATUS_OK
        assert len(batch.decisions) == len(expected["rows"]), (
            f"{item['id']}：行数 {len(batch.decisions)} != {len(expected['rows'])}"
        )
        for got, want in zip(batch.decisions, expected["rows"], strict=True):
            state = _row_state(got)
            for field_name in ROW_FIELDS:
                assert state[field_name] == want[field_name], (
                    f"{item['id']} 行 {want['code']} 字段 {field_name}："
                    f"{state[field_name]!r} != {want[field_name]!r}"
                )
        assert batch.skipped_rows == expected["skipped_rows"], item["id"]
        assert list(batch.ignored_actions) == expected["ignored_actions"], item["id"]

    def test_replay_is_deterministic(self) -> None:
        """同输入同输出：解析器是纯函数，不许有隐藏状态（重放是审计的手段）。"""
        for item in CORPUS:
            schema = item["expected"].get("schema", "intraday")
            first = parse_decisions(item["text"], schema=schema)
            second = parse_decisions(item["text"], schema=schema)
            assert first.status == second.status, item["id"]
            assert [_row_state(d) for d in first.decisions] == [
                _row_state(d) for d in second.decisions
            ], item["id"]


class TestCorpusCoverage:
    """语料退化守卫：样本被删光或全变成同一种形态时，重放测试会「绿得毫无信息」。"""

    def test_every_shape_is_present(self) -> None:
        whys = {item["why"] for item in CORPUS}
        assert whys == {
            "fence_prose",
            "watch_ratchet",
            "multi_row",
            "pct_missing",
            "pct_given",
            "price_levels",
            "parse_failed",
        }

    def test_corpus_carries_the_risky_shapes(self) -> None:
        """三态、棘轮、多行——这三类是历史事故的现场，缺一不可。"""
        rows = [row for item in CORPUS for row in item["expected"].get("rows", [])]
        assert any(r["pct_state"] == "missing" for r in rows), "缺 pct 缺失样本"
        assert any(r["pct_state"] == "given" and r["pct"] for r in rows), (
            "缺 pct 有效样本"
        )
        assert any(r["move_stop"] for r in rows), "缺棘轮样本"
        assert any(r["stop_loss"] and r["take_profit"] for r in rows), "缺绝对价位样本"
        assert any(r["action"] == "watch" for r in rows), "缺守护单样本"
        multi = max(len(item["expected"].get("rows", [])) for item in CORPUS)
        assert multi >= 3, "多行样本丢了——`decisions` 数组解析没被真实验证过"

    def test_real_outputs_are_never_bare_json(self) -> None:
        """真实输出的形态事实：**没有一条**是「整段即 JSON」。

        抽取器三条路径里，「整段」在真实数据上一次都没命中——所以围栏/括号回退
        才是主路。若将来某份语料出现裸 JSON，多半是混进了非真实样本。
        """
        for item in CORPUS:
            assert not item["text"].strip().startswith("{"), item["id"]
