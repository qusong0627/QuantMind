"""阶段化课程与评估口径提示块（Phase B）——纯函数，读时计算轮次。

设计来源：开源证券研究所阶段表（图14）——S1 种子（1-3）→ S2 因子族探索
（4-8）→ S3 聚焦精炼（9-12）→ S4 正交组合（13-16）→ S5 非线性（17+）。

本文件钉死的行为：
- 轮次判定：RD-Agent 当前日志 tag（``Loop_{li}.{step}``，ContextVar）**优先**
  ——精确到当前 loop，无「目录还没建出来」的 ±1 竞态；目录数只做兜底；
  最终下限 1（第一轮之前也要有阶段可注入）。
- 阶段边界：1-3/4-8/9-12/13-16/17+；0 与负数钳到 S1（不抛）。
- 渲染块必含五阶段课程表、当前进度、当前阶段纪律；最后一轮补收敛提示。
- 注入是增益层：任何异常输入返回空串或钳位值，绝不向外抛。
"""

from __future__ import annotations

import pytest

from backend.services.engine.rd_agent.stage_prompt import (
    STAGE_BLOCK_MARKER,
    STAGES,
    count_loops,
    current_round,
    prompt_round_from_tag,
    render_eval_criteria_block,
    render_stage_block,
    stage_for_round,
)


class TestStageForRound:
    @pytest.mark.parametrize(
        ("rnd", "key"),
        [
            (1, "S1"),
            (3, "S1"),
            (4, "S2"),
            (8, "S2"),
            (9, "S3"),
            (12, "S3"),
            (13, "S4"),
            (16, "S4"),
            (17, "S5"),
            (40, "S5"),
        ],
    )
    def test_boundaries(self, rnd, key):
        assert stage_for_round(rnd).key == key

    def test_zero_and_negative_clamp_to_s1(self):
        assert stage_for_round(0).key == "S1"
        assert stage_for_round(-5).key == "S1"

    def test_stage_carries_goal_and_disciplines(self):
        stage = stage_for_round(5)
        assert stage.name
        assert stage.goal
        assert len(stage.disciplines) >= 2
        assert stage.range_label  # 「第 4-8 轮」类文案

    def test_five_stages_contiguous(self):
        assert [s.key for s in STAGES] == ["S1", "S2", "S3", "S4", "S5"]
        for prev, nxt in zip(STAGES, STAGES[1:], strict=False):
            assert prev.end is not None
            assert nxt.start == prev.end + 1
        assert STAGES[-1].end is None  # 末阶段无上限


class TestPromptRoundFromTag:
    def test_loop_0_is_round_1(self):
        assert prompt_round_from_tag("Loop_0.direct_exp_gen") == 1

    def test_nested_tag(self):
        assert prompt_round_from_tag("Loop_3.feedback.coding") == 4

    def test_missing_or_unrelated_tag_returns_none(self):
        assert prompt_round_from_tag("") is None
        assert prompt_round_from_tag("scenario") is None


class TestCountLoops:
    def test_counts_only_loop_int_dirs(self, tmp_path):
        for name in ("Loop_0", "Loop_1", "Loop_10", "Loop_x", "notes"):
            (tmp_path / name).mkdir()
        (tmp_path / "Loop_2.txt").write_text("x", encoding="utf-8")
        assert count_loops(tmp_path) == 3

    def test_missing_dir_is_zero(self, tmp_path):
        assert count_loops(tmp_path / "nope") == 0
        assert count_loops(None) == 0


class TestCurrentRound:
    def test_tag_wins_over_dirs(self, tmp_path):
        for i in range(5):
            (tmp_path / f"Loop_{i}").mkdir()
        assert current_round(tag="Loop_0.feedback", log_dir=tmp_path) == 1

    def test_dir_fallback_when_tag_missing(self, tmp_path):
        for i in range(4):
            (tmp_path / f"Loop_{i}").mkdir()
        assert current_round(tag="", log_dir=tmp_path) == 4

    def test_nothing_yields_first_round(self):
        assert current_round(tag="", log_dir=None) == 1


class TestRenderStageBlock:
    def test_block_has_curriculum_and_current_stage(self):
        text = render_stage_block(total_loops=8, current_loop=5)
        assert "阶段化挖掘课程" in text
        assert "第 5/8 轮" in text
        assert "S2" in text and "因子族探索" in text
        for key in ("S1", "S2", "S3", "S4", "S5"):
            assert key in text  # 课程表五阶段全见

    def test_last_round_gets_convergence_hint(self):
        assert "最后一轮" in render_stage_block(total_loops=3, current_loop=3)
        assert "最后一轮" not in render_stage_block(total_loops=8, current_loop=5)

    def test_total_below_current_is_clamped_up(self):
        text = render_stage_block(total_loops=2, current_loop=7)
        assert "第 7/7 轮" in text

    def test_total_none_means_unknown_and_shows_current(self):
        text = render_stage_block(total_loops=None, current_loop=4)
        assert "第 4" in text

    def test_garbage_inputs_never_raise(self):
        assert render_stage_block(total_loops="x", current_loop=None) != ""
        assert render_stage_block(total_loops=-1, current_loop=0) != ""


class TestEvalCriteriaBlock:
    def test_carries_real_evaluation_semantics(self):
        text = render_eval_criteria_block()
        # 目标口径必须与评估器同源（rankIC/ICIR/PFS/RRE/换手/扣费）
        for token in ("rankIC", "ICIR", "PFS", "RRE", "换手"):
            assert token in text
        # 成本口径：0.2% 双边（factor_research.analysis.COST_RATE 单一出处）
        assert "0.2%" in text and "双边" in text
        # 硬规则：|ρ|≥0.9 值级查重硬拒
        assert "0.9" in text
        # 多头口径与评估器一致：截面 rank 前 30%
        assert "30%" in text


class TestWrapperStageInjection:
    """接线：静态后缀→背景键（冻结）；阶段块→假设规范键（每轮实时加载）。

    防的是老病复发——本项目 prompt 注入曾整段从未生效（import 错模块），
    日志上什么都看不出来。这里用真实的 ``rdagent.utils.agent.tpl`` 打补丁
    后调 ``load_content`` 实读，确保两条通道真接上、且互不串门。
    """

    @pytest.fixture()
    def tpl(self):
        _tpl = pytest.importorskip("rdagent.utils.agent.tpl")
        names = ("load_content", "_qm_patched", "_qm_suffix", "_qm_stage_fn")
        saved = {name: getattr(_tpl, name, None) for name in names}
        yield _tpl
        _tpl.load_content = saved["load_content"]
        for name in names[1:]:
            if saved[name] is None:
                if hasattr(_tpl, name):
                    delattr(_tpl, name)
            else:
                setattr(_tpl, name, saved[name])

    def _patched_wrapper(self, **attrs):
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        wrapper = RDLoopWrapper(market="a_share")
        for key, value in attrs.items():
            setattr(wrapper, key, value)
        wrapper._patch_prompts_for_chinese()
        return wrapper

    def test_background_key_gets_static_suffix_with_eval_criteria(self, tpl):
        self._patched_wrapper(_direction="动量反转与量价背离")
        out = tpl.load_content(
            "scenarios.qlib.experiment.prompts:qlib_factor_background"
        )
        assert "语言要求" in out
        assert "动量反转与量价背离" in out
        assert "评估口径" in out and "0.2%" in out
        # 阶段块不许进冻结的背景（否则永远停在构造时那一轮）
        assert STAGE_BLOCK_MARKER not in out

    def test_stage_key_recomputed_per_round(self, tpl, monkeypatch, tmp_path):
        import backend.services.engine.rd_agent.stage_prompt as sp

        self._patched_wrapper(_loop_n=12, _task_log_dir=str(tmp_path))
        monkeypatch.setattr(sp, "rdagent_tag", lambda: "Loop_4.direct_exp_gen")
        out = tpl.load_content("scenarios.qlib.prompts:factor_hypothesis_specification")
        assert STAGE_BLOCK_MARKER in out
        assert "第 5/12 轮" in out and "因子族探索" in out
        assert "Factors per Generation" in out  # 追加，不替换原文

        # 同一键下一轮再读 → 阶段随轮次前进（读时计算，不是冻结值）
        monkeypatch.setattr(sp, "rdagent_tag", lambda: "Loop_8.direct_exp_gen")
        out2 = tpl.load_content(
            "scenarios.qlib.prompts:factor_hypothesis_specification"
        )
        assert "第 9/12 轮" in out2 and "聚焦精炼" in out2
        assert "第 5/12 轮" not in out2

    def test_dir_count_fallback_when_tag_missing(self, tpl, monkeypatch, tmp_path):
        import backend.services.engine.rd_agent.stage_prompt as sp

        for i in range(2):
            (tmp_path / f"Loop_{i}").mkdir()
        self._patched_wrapper(_loop_n=6, _task_log_dir=str(tmp_path))
        monkeypatch.setattr(sp, "rdagent_tag", lambda: "")
        out = tpl.load_content("scenarios.qlib.prompts:factor_hypothesis_specification")
        assert "第 2/6 轮" in out

    def test_unrelated_key_untouched(self, tpl):
        self._patched_wrapper(_direction="x")
        out = tpl.load_content(
            "scenarios.qlib.experiment.prompts:qlib_factor_interface"
        )
        assert "语言要求" not in out
        assert STAGE_BLOCK_MARKER not in out

    def test_stage_fn_failure_degrades_to_plain_content(self, tpl, monkeypatch):
        self._patched_wrapper()

        def boom():
            raise RuntimeError("tag 不可用")

        monkeypatch.setattr(tpl, "_qm_stage_fn", boom)
        out = tpl.load_content("scenarios.qlib.prompts:factor_hypothesis_specification")
        assert STAGE_BLOCK_MARKER not in out
        assert "Factors per Generation" in out  # 原文仍在，不抛

    def test_guards_prevent_double_append(self, tpl, monkeypatch):
        marked = f"spec\n\n====== {STAGE_BLOCK_MARKER} ..."
        monkeypatch.setattr(tpl, "load_content", lambda uri, *a, **k: marked)
        self._patched_wrapper(_direction="y")
        out = tpl.load_content("scenarios.qlib.prompts:factor_hypothesis_specification")
        assert out == marked

        lang = "bg\n====== 语言要求 / Language Requirement ======"
        monkeypatch.setattr(tpl, "load_content", lambda uri, *a, **k: lang)
        self._patched_wrapper(_direction="y")
        out = tpl.load_content(
            "scenarios.qlib.experiment.prompts:qlib_factor_background"
        )
        assert out == lang

    def test_build_stage_block_without_run_yields_block(self, monkeypatch):
        import backend.services.engine.rd_agent.stage_prompt as sp
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        monkeypatch.setattr(sp, "rdagent_tag", lambda: "")
        text = RDLoopWrapper(market="a_share")._build_stage_block()
        assert STAGE_BLOCK_MARKER in text
        assert "第 1" in text
