"""期初结转的**桥锚定对账**（P0.4 规则 1/2/3/4）：台账只提供归属，仓位以桥为准。

为什么这层必须有：隔壁台账是派生数据——「卖出没归因回去」时它只增不减。实测切换前
台账 2400 股 vs 桥 1900 股，多出的正是 `002518.SZ`(400) + `603678.SH`(100) 两只
**幻影**（桥侧早已清仓）。照搬台账 ⇒ 提示词告诉模型「你还有 400 股」而柜台没有。

三层一起钉（落库闸门与流水 note 钉在 `test_agent_ledger_seed`，那里有假 session）：

1. **纯对账**（`reconcile_seed_with_bridge`）——真语料（2026-09-23 dump 的 3 agent /
   10 只 + 桥 6 只 / 1900 股）、六类判定（match / anchored / phantom / orphan /
   override / drift）、双向断言（规则 4）；
2. **桥读取**（`bridge_positions` / `bridge_rows_from_view`）——重码/负量/坏形态一律拒；
3. **CLI**——`--apply` 必须带 `--record`（规则 2 的留痕不许忘）、`--bridge-json` 复放、
   对账差异要人看一眼（退出码 1）。

真库 E2E 用随机租户跑完即删（``t-cli-*``）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.scripts import import_agent_ledger_seed as cli
from backend.shared.decision.agent_ledger import (
    BRIDGE_ANCHORED,
    BRIDGE_DRIFT,
    BRIDGE_MATCH,
    BRIDGE_ORPHAN,
    BRIDGE_OVERRIDE,
    BRIDGE_PHANTOM,
    BridgePosition,
    SeedReconciliation,
    bridge_positions,
    bridge_rows_from_view,
    parse_legacy_ledger,
    reconcile_seed_with_bridge,
)

#: 隔壁 2026-09-23 实测 dump 的 ``agents`` 段（**逐字**：3 agent / 10 只，含两只幻影）。
REAL_DUMP = {
    "version": 1,
    "agents": {
        "deepseek-v4-flash": {
            "positions": {
                "002074.SZ": {
                    "volume": 200,
                    "cost_price": 25.44,
                    "buy_ts": "2026-09-14T11:07:35.714953+08:00",
                },
                "600276.SH": {
                    "volume": 100,
                    "cost_price": 45.66,
                    "buy_ts": "2026-09-23T09:35:25.265177+08:00",
                },
                "603213.SH": {
                    "volume": 700,
                    "cost_price": 13.6,
                    "buy_ts": "2026-09-11T09:37:26.903941+08:00",
                },
            },
            "virtual_cash": 95044.8,
            "used": 356421.2,
        },
        "deepseek-v4-pro": {
            "positions": {
                "002518.SZ": {
                    "volume": 200,
                    "cost_price": 39.55,
                    "buy_ts": "2026-09-21T10:43:21.683356+08:00",
                },
                "002709.SZ": {
                    "volume": 300,
                    "cost_price": 32.97,
                    "buy_ts": "2026-09-17T11:26:19.513199+08:00",
                },
                "300687.SZ": {
                    "volume": 300,
                    "cost_price": 29.95,
                    "buy_ts": "2026-09-21T10:47:49.337123+08:00",
                },
                "600276.SH": {
                    "volume": 100,
                    "cost_price": 45.64,
                    "buy_ts": "2026-09-23T09:37:04.482882+08:00",
                },
            },
            "virtual_cash": 74011.8,
            "used": 167302.2,
        },
        "glm-5.3-flash": {
            "positions": {
                "002518.SZ": {
                    "volume": 200,
                    "cost_price": 39.38,
                    "buy_ts": "2026-09-21T10:59:26.582701+08:00",
                },
                "600176.SH": {
                    "volume": 200,
                    "cost_price": 45.53,
                    "buy_ts": "2026-09-22T13:56:51.703352+08:00",
                },
                "603678.SH": {
                    "volume": 100,
                    "cost_price": 50.16,
                    "buy_ts": "2026-09-14T11:11:20.585636+08:00",
                },
            },
            "virtual_cash": 76930.34,
            "used": 360931.2,
        },
    },
    "applied_fills": {"25446": {"filled": 200, "ts": "2026-09-23"}},
}

#: 桥侧实测（2026-09-23 16:59 快照，6 只 / 1900 股）——幻影那两只**不在**其中。
REAL_BRIDGE = {
    "002074.SZ": 200,
    "002709.SZ": 300,
    "300687.SZ": 300,
    "600176.SH": 200,
    "600276.SH": 200,
    "603213.SH": 700,
}


# --- 夹具 -------------------------------------------------------------------


def _bridge(items) -> dict[str, BridgePosition]:
    bmap, problems = bridge_positions(items)
    assert not problems, problems
    return bmap


def _bridge_rows(table: dict) -> list[dict]:
    return [{"code": c, "volume": v} for c, v in dict(table or {}).items()]


def _plan(dump: dict = REAL_DUMP, bridge=REAL_BRIDGE, **kw) -> SeedReconciliation:
    """``bridge`` 收行表（``{码: 量}``）或已建好的 bmap（``{码: BridgePosition}``）。"""
    if bridge and isinstance(next(iter(bridge.values())), BridgePosition):
        bmap = dict(bridge)
    else:
        bmap = _bridge(_bridge_rows(bridge))
    return reconcile_seed_with_bridge(parse_legacy_ledger(dump), bmap, **kw)


def _dump(*positions: tuple[str, str, int], cash: float = 1000.0) -> dict:
    """单/多 agent 的最小台账：``(agent, code, volume)`` 三元组逐条给。"""
    agents: dict[str, dict] = {}
    for agent, code, vol in positions:
        rec = agents.setdefault(agent, {"positions": {}, "virtual_cash": cash})
        rec["positions"][code] = {
            "volume": vol,
            "cost_price": 25.0,
            "buy_ts": "2026-09-14T11:07:35+08:00",
        }
    return {"version": 1, "agents": agents}


# --- 真语料：判定与双向断言 --------------------------------------------------


def test_real_dump_reconciles_to_the_bridge():
    """真 dump + 真桥：**两只幻影不迁入**，其余逐只相符，合计 1900 == 桥 1900。"""
    rec = _plan()
    assert rec.ok, rec.problems
    assert rec.assert_balances() == ""
    assert rec.bridge_total == 1900 and rec.carried_total == 1900
    assert rec.phantom_codes == ("002518.SZ", "603678.SH")
    assert rec.orphan_codes == () and rec.orphan_total == 0
    # 幻影两只**不在**计划里；其余 8 条（含 600276.SH 两家各 100）都在
    carried = {
        (a.agent, p.code, p.volume) for a in rec.seed.agents for p in a.positions
    }
    assert ("deepseek-v4-pro", "002518.SZ", 200) not in carried
    assert ("glm-5.3-flash", "603678.SH", 100) not in carried
    assert ("deepseek-v4-flash", "600276.SH", 100) in carried
    assert ("deepseek-v4-pro", "600276.SH", 100) in carried
    assert len(carried) == 7  # 10 − 3 只（pro/glm 的 002518 + glm 的 603678）
    # 成本口径不受对账影响（成本以台账为准：那是该 agent 的真实买入成本）
    flash = rec.seed.agent("deepseek-v4-flash")
    assert flash is not None and flash.used == pytest.approx(19174.0)


def test_real_dump_phantom_records_carry_the_four_required_fields():
    """P0.4 规则 2：幻影不是静默丢弃——原台账量 / 桥实况 / 差额 / 判定逐条在案。"""
    rec = _plan()
    r = rec.record("002518.SZ")
    assert r is not None
    assert r.kind == BRIDGE_PHANTOM
    assert (r.ledger_volume, r.bridge_volume, r.carried_volume) == (400.0, 0.0, 0.0)
    assert r.delta == -400
    assert sorted(r.claims) == [("deepseek-v4-pro", 200.0), ("glm-5.3-flash", 200.0)]
    assert "幻影" in r.verdict and "不迁入" in r.verdict
    # 幻影**没有结转流水行**（它不在计划里）；note_for 仍答得出判定，供人工核对
    assert "对账：" in rec.note_for("002518.SZ")
    assert "002518.SZ" not in {p.code for a in rec.seed.agents for p in a.positions}
    # 幻影剔除后两个 agent 都还有别的仓：现金照迁、used 是现算的
    pro = rec.seed.agent("deepseek-v4-pro")
    glm = rec.seed.agent("glm-5.3-flash")
    assert pro is not None and pro.used == pytest.approx(23440.0)  # 31350 − 39.55×200
    assert glm is not None and glm.used == pytest.approx(9106.0)  # 21998 − 12892
    assert any("未迁入" in n for n in rec.notes)


def test_reconcile_is_deterministic():
    """同一份输入两次对账逐字相同（报告可复现，人工比对才有意义）。"""
    assert _plan() == _plan()


def test_every_carried_code_has_a_record_and_equals_the_bridge():
    """逐标的：迁入的每只在桥里都有、量相等；记录覆盖两边出现过的全部代码。"""
    rec = _plan()
    for code, vol in rec.carried:
        r = rec.record(code)
        assert r is not None and r.kind == BRIDGE_MATCH
        assert r.carried_volume == vol and r.bridge_volume == REAL_BRIDGE[code]
    assert {r.code for r in rec.records} == set(REAL_BRIDGE) | {
        "002518.SZ",
        "603678.SH",
    }
    assert [c for c, _v in rec.bridge_held] == sorted(REAL_BRIDGE)
    assert rec.bridge_held == tuple(
        sorted((c, float(v)) for c, v in REAL_BRIDGE.items())
    )


def test_parse_problems_block_the_reconcile():
    """解析阻断 ⇒ 对账也不放行（原样上抛，不做「猜着搬」的补救）。"""
    bad = parse_legacy_ledger({**REAL_DUMP, "version": 2})
    rec = reconcile_seed_with_bridge(bad, _bridge(_bridge_rows(REAL_BRIDGE)))
    assert not rec.ok
    assert rec.problems == bad.problems
    assert rec.records == ()


# --- 三类非「逐只相符」的判定 -----------------------------------------------


def test_single_claimant_volume_is_anchored_to_the_bridge():
    """单一认领人：台账量不准时**以桥为准**（台账只说「这只归谁」）。"""
    rec = _plan(dump=_dump(("pro", "002074.SZ", 400)), bridge={"002074.SZ": 200})
    assert rec.ok, rec.problems
    r = rec.record("002074.SZ")
    assert r is not None and r.kind == BRIDGE_ANCHORED
    assert (r.ledger_volume, r.bridge_volume, r.carried_volume, r.delta) == (
        400.0,
        200.0,
        200.0,
        -200,
    )
    pro = rec.seed.agent("pro")
    assert pro is not None and [(p.code, p.volume) for p in pro.positions] == [
        ("002074.SZ", 200)
    ]
    assert "以桥为准" in r.verdict
    assert "对账：" in rec.note_for("002074.SZ")  # 判定进得了流水行


def test_bridge_only_code_is_recorded_as_an_orphan_and_not_invented():
    """规则 3：桥有、无任何 agent 认领 ⇒ **孤仓**分支显式覆盖，不入分账。"""
    rec = _plan(
        dump=_dump(("pro", "002074.SZ", 200)),
        bridge={"002074.SZ": 200, "600519.SH": 500},
    )
    assert rec.ok, rec.problems  # 孤仓是**已知情形**（总账户既存仓），不是错误
    assert rec.orphan_codes == ("600519.SH",)
    assert rec.orphan_total == 500
    r = rec.record("600519.SH")
    assert r is not None and r.kind == BRIDGE_ORPHAN
    assert (r.ledger_volume, r.bridge_volume, r.carried_volume) == (0.0, 500.0, 0.0)
    assert "无任何 agent 认领" in r.verdict
    # 断言按「桥合计 − 孤仓」两边扣掉（孤仓按定义不在任何 agent 名下）
    assert rec.assert_balances() == ""
    assert rec.bridge_total == 700 and rec.carried_total == 200
    assert {p.code for a in rec.seed.agents for p in a.positions} == {"002074.SZ"}


def test_zero_volume_bridge_row_with_a_claimant_is_a_phantom():
    """桥侧残留的 0 量行（实测 4 条）：有认领 ⇒ 幻影，并说明「有行无量」。"""
    rec = _plan(
        dump=_dump(("pro", "603267.SH", 100)),
        bridge=_bridge([{"code": "603267.SH", "volume": 0}]),
    )
    assert rec.ok
    r = rec.record("603267.SH")
    assert r is not None and r.kind == BRIDGE_PHANTOM and r.bridge_present is True
    assert "该行为 0 股" in r.verdict


def test_zero_volume_row_without_a_claimant_is_only_a_note():
    """零量行且无人认领：无信息量（记 note 即可，不进对账记录）。"""
    rec = _plan(
        dump={"version": 1, "agents": {"pro": {"positions": {}, "virtual_cash": 1000}}},
        bridge=_bridge([{"code": "603267.SH", "volume": 0}]),
    )
    assert rec.ok and rec.records == ()
    assert any("清仓残留行" in n for n in rec.notes)


def test_multi_claimant_drift_blocks_the_whole_plan():
    """多认领人且合计≠桥：归属不可判定 ⇒ **阻断**（摊派会让某一家静默少仓）。"""
    rec = _plan(
        dump=_dump(("a", "002074.SZ", 300), ("b", "002074.SZ", 300)),
        bridge={"002074.SZ": 400},
    )
    assert not rec.ok
    assert any("无法判定归属" in p and "--resolve" in p for p in rec.problems)
    r = rec.record("002074.SZ")
    assert r is not None and r.kind == BRIDGE_DRIFT and r.carried_volume == 0
    assert "多个 agent 认领" in r.verdict
    # 阻断的标的**不迁入**（计划里不出现），也不会被当成「桥多出来的」孤仓
    assert {p.code for a in rec.seed.agents for p in a.positions} == set()
    assert rec.orphan_codes == ()


# --- 人工判定（阻断的唯一出路）----------------------------------------------


def test_resolution_with_a_reason_clears_the_block():
    """人工判定（带理由）⇒ 计划可落库，判定与理由进记录与流水行 note。"""
    rec = _plan(
        dump=_dump(("a", "002074.SZ", 300), ("b", "002074.SZ", 300)),
        bridge={"002074.SZ": 400},
        overrides={"002074.SZ": {"a": 400}},
        reasons={"002074.SZ": "b 的 300 股已于 09-22 卖出未回写"},
    )
    assert rec.ok, rec.problems
    r = rec.record("002074.SZ")
    assert r is not None and r.kind == BRIDGE_OVERRIDE
    assert r.reason == "b 的 300 股已于 09-22 卖出未回写"
    assert (r.ledger_volume, r.carried_volume) == (600.0, 400.0)
    a = rec.seed.agent("a")
    assert a is not None and [(p.code, p.volume) for p in a.positions] == [
        ("002074.SZ", 400)
    ]
    assert "人工判定" in rec.note_for("002074.SZ")


@pytest.mark.parametrize(
    ("overrides", "reasons", "needle"),
    [
        ({"002074.SZ": {"a": 200}}, {"002074.SZ": ""}, "缺 reason"),
        ({"002074.SZ": {"bogus": 200}}, {"002074.SZ": "理由"}, "不在台账里"),
        ({"002074.SZ": {"a": 0}}, {"002074.SZ": "理由"}, "非正"),
        ({"002074.SZ": {}}, {"002074.SZ": "理由"}, "认领表为空"),
    ],
)
def test_bad_resolutions_block(overrides, reasons, needle):
    """人工判定的四种病：无理由 / 凭空开户 / 数量非正 / 空认领表——一律阻断。"""
    rec = _plan(
        dump=_dump(("a", "002074.SZ", 300)),
        bridge={"002074.SZ": 400},
        overrides=overrides,
        reasons=reasons,
    )
    assert not rec.ok
    assert any(needle in p for p in rec.problems), rec.problems


def test_override_cannot_invent_shares_the_bridge_does_not_have():
    """人工判定也不能凭空造仓：断言（规则 4）把超出桥的部分拦下来。"""
    rec = _plan(
        dump=_dump(("a", "002074.SZ", 100)),
        bridge={"002074.SZ": 200},
        overrides={"002074.SZ": {"a": 300}},
        reasons={"002074.SZ": "手滑"},
    )
    assert not rec.ok
    assert any("对账断言未通过" in p and "002074.SZ" in p for p in rec.problems)


# --- 双向断言本身（规则 4）--------------------------------------------------


def test_assert_balances_is_a_real_guard_not_a_formality():
    """断言能被证伪：手工构造不平衡的计划，它必须给出理由（且 ``ok`` 为假）。"""
    seed = parse_legacy_ledger(
        {"version": 1, "agents": {"a": {"positions": {}, "virtual_cash": 1}}}
    )
    too_much = SeedReconciliation(
        seed=seed,
        bridge_held=(("002074.SZ", 200.0),),
        carried=(("002074.SZ", 300.0),),  # 比桥多 100 股
    )
    assert "002074.SZ" in too_much.assert_balances()
    assert "≠" in too_much.assert_balances()
    assert too_much.ok is False

    invented = SeedReconciliation(
        seed=seed,
        bridge_held=(("002074.SZ", 200.0),),
        carried=(("600519.SH", 100.0),),  # 桥侧压根没有这只（幻影未被剔除）
    )
    assert "幻影" in invented.assert_balances()
    assert invented.ok is False

    missing = SeedReconciliation(
        seed=seed,
        bridge_held=(("002074.SZ", 200.0), ("600519.SH", 500.0)),
        carried=(("002074.SZ", 200.0),),
        orphan_total=0.0,  # 孤仓没被记录 ⇒ 两边对不上
    )
    assert "孤仓" in missing.assert_balances()
    assert missing.ok is False


# --- 桥读取 -----------------------------------------------------------------


def test_bridge_positions_normalizes_prefix_and_keeps_zero_rows():
    bmap, problems = bridge_positions(
        [{"code": "SH600036", "volume": 100}, {"code": "300687.SZ", "volume": 0}]
    )
    assert not problems
    assert sorted(bmap) == ["300687.SZ", "600036.SH"]
    assert bmap["300687.SZ"].held is False and bmap["600036.SH"].held is True


def test_bridge_positions_accepts_hand_written_shapes():
    """手写快照（``{码: 数量}`` / ``{码: {volume: …}}``）也认——运维手里常是这种。"""
    bmap, problems = bridge_positions(
        {"600036.SH": 100, "300687.SZ": {"volume": 200, "cost_price": 9.9}}
    )
    assert not problems
    assert bmap["600036.SH"].volume == 100
    assert bmap["300687.SZ"].cost_price == 9.9


@pytest.mark.parametrize(
    ("items", "needle"),
    [
        (
            [{"code": "600036.SH", "volume": 100}, {"code": "SH600036", "volume": 100}],
            "出现两行",
        ),
        ([{"code": "600036.SH", "volume": -5}], "为负"),
        ([{"code": "600036.SH", "volume": "abc"}], "非有限"),
        ([{"code": "", "volume": 100}], "代码为空"),
        ("not-a-list", "不是列表"),
    ],
)
def test_bridge_positions_refuses_dirty_input(items, needle):
    """桥快照脏数据一律拒（宁可停手，不许按猜的对账）。"""
    _bmap, problems = bridge_positions(items)
    assert any(needle in p for p in problems), problems


def test_bridge_rows_from_view_normalizes_the_prefix_keys():
    """``load_real_positions`` 的视图是 **prefix 键**；出口一律后缀式。"""
    view = {
        "SH600276": {
            "symbol": "600276.SH",
            "volume": 200,
            "cost_price": 45.664,
            "source": "tdx_bridge",
        },
        "SZ002074": {"volume": 200, "source": "tdx_bridge"},  # 缺 symbol → 归一键
        "": {"volume": 1},  # 归一出空码 → 由 bridge_positions 拦
    }
    rows = bridge_rows_from_view(view)
    assert [r["code"] for r in rows] == ["", "002074.SZ", "600276.SH"]
    assert rows[2]["cost_price"] == 45.664 and rows[2]["source"] == "tdx_bridge"
    _bmap, problems = bridge_positions(rows)
    assert any("代码为空" in p for p in problems)


def test_reconcile_against_the_view_shape_end_to_end():
    """视图 → 行 → 桥表 → 对账：一条链跑通（中间少一步归一就全盘皆空）。"""
    view = {
        "SH600276": {"symbol": "600276.SH", "volume": 200, "source": "tdx_bridge"},
        "SZ002074": {"symbol": "002074.SZ", "volume": 200, "source": "tdx_bridge"},
    }
    bmap, problems = bridge_positions(bridge_rows_from_view(view))
    assert not problems
    rec = reconcile_seed_with_bridge(
        parse_legacy_ledger(_dump(("a", "002074.SZ", 200))), bmap
    )
    assert rec.ok
    assert rec.record("002074.SZ").kind == BRIDGE_MATCH
    assert rec.record("600276.SH").kind == BRIDGE_ORPHAN


def test_reconcile_tolerates_plain_bridge_mappings():
    """``bridge`` 也吃 ``{码: 数量}`` / ``{码: {volume}}``（脚本手搓时不设防）。"""
    seed = parse_legacy_ledger(
        {"version": 1, "agents": {"a": {"positions": {}, "virtual_cash": 1}}}
    )
    rec = reconcile_seed_with_bridge(
        seed, {"600036.SH": 100, "300687.SZ": {"volume": 200}}
    )
    assert rec.ok
    assert rec.orphan_total == 300
    assert rec.record("600036.SH").kind == BRIDGE_ORPHAN

    rec2 = reconcile_seed_with_bridge(seed, {"600036.SH": {"volume": "x"}})
    assert not rec2.ok and any("形态不认识" in p for p in rec2.problems)


def test_reconcile_tolerates_a_prefixed_bridge_mapping_key():
    """手写键写成 prefix 式也要归一到后缀（否则同一只票被当成两只）。"""
    seed = parse_legacy_ledger(_dump(("a", "002074.SZ", 200)))
    rec = reconcile_seed_with_bridge(seed, {"SZ002074": 200})
    assert rec.ok and rec.record("002074.SZ").kind == BRIDGE_MATCH


# --- CLI --------------------------------------------------------------------


def _cli_src() -> str:
    import inspect

    return inspect.getsource(cli)


def test_cli_requires_a_record_before_applying():
    """规则 2 的留痕不许忘：--apply 没有 --record 直接退 2、不碰库。"""
    src = _cli_src()
    assert "if args.apply and not args.record:" in src
    assert "_record_doc" in src and "--record" in src


def test_cli_reads_the_bridge_through_the_single_accessor():
    """桥侧只经 `load_real_positions`（唯一口径）读，不许本脚本自己拼 SQL。"""
    src = _cli_src()
    assert "from backend.shared.real_positions import load_real_positions" in src
    assert "rows = bridge_rows_from_view(view)" in src
    assert "bridge_positions(rows)" in src
    assert "reconcile_seed_with_bridge(" in src
    assert "SELECT" not in src.upper(), "脚本自己查桥 = 第二套口径"


def test_cli_exit_codes_are_the_documented_trio():
    """0/1/2 的口径不许悄悄改（runbook 与自动化的判据）。"""
    assert (cli.EXIT_OK, cli.EXIT_ATTENTION, cli.EXIT_USAGE) == (0, 1, 2)


def test_cli_refuses_apply_without_record_before_any_async_work(monkeypatch, tmp_path):
    """行为级：缺 --record 时 `main()` 退 2，且**不进异步体**（那里才会连库）。"""

    def _boom(*_a, **_kw):  # 进了异步体就炸（说明检查顺序错了）
        raise AssertionError("缺 --record 时不该进入异步体（会去连库）")

    ledger = tmp_path / "live_ledger.json"
    ledger.write_text(json.dumps(REAL_DUMP), encoding="utf-8")
    monkeypatch.setattr(cli.asyncio, "run", _boom)
    monkeypatch.setattr(
        "sys.argv", ["cli", "--path", str(ledger), "--apply", "--user", "99"]
    )
    assert cli.main() == cli.EXIT_USAGE


def test_cli_missing_ledger_file_is_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["cli", "--path", str(tmp_path / "nope.json")])
    assert cli.main() == cli.EXIT_USAGE


def test_cli_broken_bridge_file_is_a_usage_error(monkeypatch, tmp_path):
    """桥文件坏形态 ⇒ 退 2（先于任何库操作；不静默当成空桥，否则幻影全留下）。"""
    ledger = tmp_path / "live_ledger.json"
    ledger.write_text(json.dumps(REAL_DUMP), encoding="utf-8")
    bridge = tmp_path / "bridge.json"
    bridge.write_text(json.dumps({"positions": "nope"}), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["cli", "--path", str(ledger), "--bridge-json", str(bridge), "--user", "99"],
    )
    assert cli.main() == cli.EXIT_USAGE


# --- 真库 E2E：CLI 全程（--bridge-json 复放）--------------------------------


def _loop(step):
    """跑一个异步步骤；用完把连接池关掉（池不能跨 ``asyncio.run`` 的 loop 复用）。"""

    async def _wrapped():
        from backend.shared.database_manager_v2 import close_database

        try:
            await close_database()  # 释放别的 loop 留下的引擎
        except Exception:  # noqa: BLE001
            pass
        try:
            return await step()
        finally:
            await close_database()

    return asyncio.run(_wrapped())


def _db_probe() -> bool:
    async def _p() -> bool:
        from sqlalchemy import text as _t

        from backend.shared.database_manager_v2 import get_session

        try:
            async with get_session(read_only=True) as s:
                await s.execute(_t("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    return _loop(_p)


async def _counts(tenant: str) -> dict[str, int]:
    from sqlalchemy import text as _t

    from backend.shared.agent_ledger_store import (
        ACCOUNT_TABLE,
        FILL_TABLE,
        POSITION_TABLE,
    )
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as s:
        return {
            name: (
                await s.execute(
                    _t(f"SELECT COUNT(*) FROM {name} WHERE tenant_id = :t"),
                    {"t": tenant},
                )
            ).scalar()
            for name in (FILL_TABLE, POSITION_TABLE, ACCOUNT_TABLE)
        }


def _ledger_counts(tenant: str) -> dict[str, int]:
    """该租户在三张账本表里的行数（dry-run 后应为 0、拒绝后应不变）。"""
    return _loop(lambda: _counts(tenant))


def _ledger_snapshot(tenant: str, user: str) -> tuple[dict, list[dict]]:
    """账本读回 + 结转流水行（键/量/note）。"""

    async def _r():
        from sqlalchemy import text as _t

        from backend.shared.agent_ledger_store import FILL_TABLE, load_ledger
        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as s:
            led = await load_ledger(s, tenant_id=tenant, user_id=user)
            fills = (
                (
                    await s.execute(
                        _t(
                            f"SELECT fill_key, volume, note FROM {FILL_TABLE} "
                            "WHERE tenant_id = :t AND user_id = :u"
                        ),
                        {"t": tenant, "u": user},
                    )
                )
                .mappings()
                .all()
            )
        return led, [dict(r) for r in fills]

    return _loop(_r)


def _db_cleanup(tenant: str) -> dict[str, int]:
    async def _c() -> dict[str, int]:
        from sqlalchemy import text as _t

        from backend.shared.agent_ledger_store import (
            ACCOUNT_TABLE,
            FILL_TABLE,
            POSITION_TABLE,
            ROUNDTRIP_TABLE,
        )
        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=False) as s:
            for name in (FILL_TABLE, POSITION_TABLE, ACCOUNT_TABLE, ROUNDTRIP_TABLE):
                await s.execute(
                    _t(f"DELETE FROM {name} WHERE tenant_id = :t"), {"t": tenant}
                )
            await s.commit()
        return await _counts(tenant)

    return _loop(_c)


def test_cli_apply_end_to_end_on_live_db(tmp_path, monkeypatch):
    """真库：dry-run → apply → 二次结转，全程走 CLI（桥用捕获文件复放）。

    用随机租户（``t-cli-*``）跑完即删；幻影那两只**不入库**，被锚定的标的 note 带判定。
    """
    if not _db_probe():
        pytest.skip("数据库不可用")

    import uuid as _uuid

    tenant = f"t-cli-{_uuid.uuid4().hex[:8]}"
    user = f"99{_uuid.uuid4().int % 1_000_000:06d}"
    ledger_path = tmp_path / "live_ledger.json"
    ledger_path.write_text(json.dumps(REAL_DUMP), encoding="utf-8")
    # 桥刻意与台账**有一处不同**：002074.SZ 台账 200 / 桥 150（单一认领人 ⇒ 锚定）
    bridge = dict(REAL_BRIDGE, **{"002074.SZ": 150})
    bridge_path = tmp_path / "bridge.json"
    bridge_path.write_text(
        json.dumps(
            {"positions": _bridge_rows(bridge)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record_path = tmp_path / "record.json"
    args = [
        "--path",
        str(ledger_path),
        "--bridge-json",
        str(bridge_path),
        "--record",
        str(record_path),
        "--tenant",
        tenant,
        "--user",
        user,
    ]

    try:
        # ── 1) dry-run：计划 + 对账记录，一行不写 ─────────────────────────
        monkeypatch.setattr("sys.argv", ["cli", *args])
        assert cli.main() == cli.EXIT_ATTENTION, "有幻影/锚定差异 ⇒ 要人看一眼"
        doc = json.loads(record_path.read_text(encoding="utf-8"))
        assert doc["mode"] == "dry-run" and doc["assert"]["ok"] is True
        assert doc["account"] == {"tenant": tenant, "user": user}
        assert doc["bridge"]["positions"], "记录里必须带桥快照（复放凭据）"
        assert doc["result"]["accounts_written"] == 0, "dry-run 不许写"
        assert doc["result"]["positions_written"] == 0, "dry-run 连计数都不该有"
        plan_codes = [p["code"] for a in doc["plan"] for p in a["positions"]]
        assert len(plan_codes) == 7, "10 只 − 3 只幻影行"
        assert "002518.SZ" not in plan_codes and "603678.SH" not in plan_codes
        assert {r["code"] for r in doc["records"]} >= {"002518.SZ", "603678.SH"}
        assert doc["assert"]["carried_total"] == doc["assert"]["bridge_total"] == 1850
        # 记录里的幻影四要素（原台账量 / 桥实况 / 差额 / 判定）
        ph = next(r for r in doc["records"] if r["code"] == "002518.SZ")
        assert (ph["kind"], ph["ledger_volume"], ph["bridge_volume"], ph["delta"]) == (
            "phantom",
            400.0,
            0.0,
            -400,
        )
        assert ph["claims"] == [
            {"agent": "deepseek-v4-pro", "volume": 200.0},
            {"agent": "glm-5.3-flash", "volume": 200.0},
        ]
        assert sum(_ledger_counts(tenant).values()) == 0, "dry-run 之后表里不该有任何行"

        # ── 2) apply：真写，幻影仍不入库 ────────────────────────────────
        monkeypatch.setattr("sys.argv", ["cli", *args, "--apply"])
        assert cli.main() == cli.EXIT_ATTENTION  # 差异仍在，照样要人看一眼
        doc = json.loads(record_path.read_text(encoding="utf-8"))
        assert doc["mode"] == "apply" and doc["assert"]["ok"] is True
        assert doc["result"]["accounts_written"] == 3
        assert doc["result"]["fills_written"] == 7

        led, fills = _ledger_snapshot(tenant, user)
        codes = {c for a in led["agents"].values() for c in a["positions"]}
        assert "002518.SZ" not in codes and "603678.SH" not in codes, "幻影不入库"
        assert len(codes) == 6 and len(fills) == 7
        # 锚定量落库（台账 200 → 桥 150）
        assert (
            led["agents"]["deepseek-v4-flash"]["positions"]["002074.SZ"]["volume"]
            == 150
        )
        assert all(float(r["volume"]) != 400 for r in fills)
        # 键带 agent：**两家同持一只票**（600276.SH）时第二家的流水也落得下来
        assert len({r["fill_key"] for r in fills}) == 7, "键撞了 = 有人静默丢了流水"
        assert {r["fill_key"] for r in fills} >= {
            "legacy-seed:deepseek-v4-flash:600276.SH",
            "legacy-seed:deepseek-v4-pro:600276.SH",
        }
        # 锚定判定进得了流水行 note
        anchored = [r for r in fills if r["fill_key"].endswith("flash:002074.SZ")]
        assert len(anchored) == 1 and "单一认领人" in anchored[0]["note"]
        # 逐只双向：Σ 账本 == Σ 桥（这里孤仓 0）
        total = sum(
            float(p["volume"])
            for a in led["agents"].values()
            for p in a["positions"].values()
        )
        assert total == sum(bridge.values()) == 1850

        # ── 3) 二次结转：空账本纪律依旧生效（一行不写）────────────────────
        before = _ledger_counts(tenant)
        monkeypatch.setattr("sys.argv", ["cli", *args, "--apply"])
        assert cli.main() == cli.EXIT_ATTENTION
        doc = json.loads(record_path.read_text(encoding="utf-8"))
        assert doc["result"]["applied"] is False and doc["result"]["refused"]
        assert _ledger_counts(tenant) == before, "拒绝时不许再写"
    finally:
        left = _db_cleanup(tenant)
        assert sum(left.values()) == 0, left
