"""因子概率板 · 判定层单测：三层真值表（金样）/ 聚合 / 四象限 / 名单 / 合成门槛。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.shared.factor_board import board_io as IO
from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import config as C
from backend.shared.factor_board import sectors as S
from backend.shared.factor_board import selection as SEL

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "factorBoardGolden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN_PATH.exists(), "金样缺失：factorBoardGolden.json"
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert data["verdictTruthTable"], "金样不可为空"
    return data


def _sector_rows() -> pd.DataFrame:
    """一行命中一个判定类别（quantile 由表自身决定，值按构造可预期）。"""
    return pd.DataFrame(
        [
            # 6 个板块：均分 -0.6/-0.4/-0.5 低侧 与 0.4/0.6/0.5 高侧
            {"SectorName": "多A", "fac": 0.6, "fbreadth": 0.9, "mbreadth": 0.7},
            {"SectorName": "多弱B", "fac": 0.5, "fbreadth": 0.8, "mbreadth": 0.2},
            {"SectorName": "中C", "fac": 0.0, "fbreadth": 0.5, "mbreadth": 0.5},
            {"SectorName": "空D", "fac": -0.6, "fbreadth": 0.1, "mbreadth": 0.3},
            {"SectorName": "空抗E", "fac": -0.5, "fbreadth": 0.2, "mbreadth": 0.9},
            {"SectorName": "缺模F", "fac": 0.4, "fbreadth": 0.9, "mbreadth": np.nan},
        ]
    ).set_index("SectorName")


@pytest.mark.unit
@pytest.mark.parametrize(
    "row",
    json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["verdictTruthTable"],
    ids=lambda r: r["case"],
)
def test_三层判定真值表_金样逐条(row: dict):
    # Act
    got = S.classify_sector(
        row["fac"], row["fbreadth"], row["mbreadth"], row["q67"], row["q33"]
    )
    # Assert
    assert got == row["expect"]


@pytest.mark.unit
def test_apply_verdicts_与逐行函数一致(golden):
    # Arrange
    g = _sector_rows()

    # Act
    out, q67, q33 = S.apply_verdicts(g)

    # Assert：逐行复算应与向量化入口一致
    for name, r in out.iterrows():
        expect = S.classify_sector(
            S._f(r["fac"]), S._f(r["fbreadth"]), S._f(r["mbreadth"]), q67, q33
        )
        assert r["verdict"] == expect, name
    verdicts = dict(out["verdict"])
    assert verdicts["多A"] == S.VERDICT_LONG
    assert verdicts["多弱B"] == S.VERDICT_WEAK
    assert verdicts["空D"] == S.VERDICT_SHORT
    assert verdicts["空抗E"] == S.VERDICT_RESILIENT
    assert verdicts["缺模F"] == S.VERDICT_NEUTRAL  # 模型广度缺失 → 不判定


@pytest.mark.unit
def test_板块聚合_广度与最小成分数():
    # Arrange：板块 X 4 只（3 只 composite>0，模型覆盖 2 只其中 1 只≥0.5）
    idx = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"]
    base = pd.DataFrame(
        {
            "composite": [0.2, 0.1, -0.1, 0.3, np.nan],
            "mdl_rank": [0.8, 0.2, np.nan, np.nan, 0.9],
            "SectorName": ["X", "X", "X", "X", "Y"],
        },
        index=idx,
    )

    # Act：min_n=4 → 只剩 X
    g = S.sector_table(base, min_n=4)

    # Assert
    assert list(g.index) == ["X"]
    row = g.loc["X"]
    assert row["n"] == 4
    assert row["fbreadth"] == pytest.approx(0.75)  # 0.2/0.1/0.3 为正
    assert row["mbreadth"] == pytest.approx(0.5)  # 覆盖 2 只，1 只 ≥0.5
    assert row["model_cov"] == pytest.approx(0.5)


@pytest.mark.unit
def test_四象限计数与名单_剔ST_缺失不落0():
    # Arrange
    idx = [f"60{i:04d}.SH" for i in range(10)]
    stock = pd.DataFrame(
        {
            "composite": [0.9, 0.8, 0.7, -0.9, -0.8, -0.7, 0.1, 0.0, 0.05, 0.6],
            "mdl_rank": [0.9, 0.8, 0.75, 0.1, 0.2, 0.25, 0.5, 0.5, 0.5, 0.65],
            "st": [False, True, False, False, False, False, False, False, False, False],
            "name": [f"票{i}" for i in range(10)],
            "sector_l2": [None] * 10,
        },
        index=idx,
    )

    # Act
    q = S.quadrant_frame(stock)
    counts = S.quadrant_counts(q)
    lists = S.stock_lists(q, n=5)

    # Assert：HH = 3 只（其中一只 ST）→ 名单剔 ST 后 2 只
    assert counts["HH"] == 3
    assert counts["LL"] == 3
    assert sum(counts.values()) == 10
    long_syms = [x["symbol"] for x in lists["resonant_long"]]
    assert idx[1] not in long_syms  # ST 剔除
    assert long_syms == [idx[0], idx[2]]  # 按 composite 降序
    short_syms = [x["symbol"] for x in lists["resonant_short"]]
    assert short_syms == [idx[3], idx[4], idx[5]]
    # 缺失语义：sector_l2 是 None 就落 null，不落 0/空串
    assert all(x["sector_l2"] is None for x in lists["resonant_long"])


@pytest.mark.unit
def test_members_index_分组降序_缺失落null_含ST_多对多():
    # Arrange：5 只标的；板A/板B 共享一只（概念多对多），含 ST 与无模型覆盖标的
    syms = [f"60{i:04d}.SH" for i in range(5)]
    stock = pd.DataFrame(
        {
            "composite": [0.5, 0.3, np.nan, -0.2, 0.1],
            "name": ["甲", "乙", "丙", None, "戊"],
            "mdl_rank": [0.9, np.nan, 0.5, 0.2, 0.8],
            "quadrant": ["HH", None, None, "LL", "HH"],  # 双轴缺一即 None
            "st": [False, True, False, False, False],
        },
        index=syms,
    )
    members = pd.DataFrame(
        {
            "SectorType": ["概念板块"] * 6 + ["行业板块(二级)"] * 5,
            "SectorName": ["板A"] * 3 + ["板B"] * 3 + ["行"] * 5,
            "Symbol": syms[:3] + [syms[0], syms[4], syms[3]] + syms,
        }
    )

    # Act
    out = S.members_index(stock, members, "概念板块")
    encoded = json.dumps(out, ensure_ascii=False, allow_nan=False)

    # Assert：板A 按 composite 降序、NaN 垫底；ST 保留；缺失一律 null
    assert set(out) == {"板A", "板B"}
    a = out["板A"]
    assert [r["symbol"] for r in a] == [syms[0], syms[1], syms[2]]
    assert a[0]["quadrant"] == "HH"
    assert a[1]["name"] == "乙" and a[1]["model_rank"] is None  # ST 不剔、模型缺失落 null
    assert a[2]["composite"] is None and a[2]["quadrant"] is None
    # 多对多：板B 与板A 共享 syms[0]，行序同为 composite 降序
    assert [r["symbol"] for r in out["板B"]] == [syms[0], syms[4], syms[3]]
    assert "NaN" not in encoded and "Infinity" not in encoded


@pytest.mark.unit
def test_members_index_空成员与未知类型返回空dict():
    stock = pd.DataFrame(
        {"composite": [0.1], "name": ["甲"], "mdl_rank": [0.6], "quadrant": ["HM"]},
        index=["600001.SH"],
    )
    empty = pd.DataFrame(columns=["SectorType", "SectorName", "Symbol"])
    assert S.members_index(stock, empty, "概念板块") == {}
    assert S.members_index(stock, empty, "行业板块(二级)") == {}


@pytest.mark.unit
def test_合成_反向因子低值加分_覆盖门槛剔除():
    # Arrange：单库两个因子；B 在覆盖门槛内，C 只缺 1 个因子 → 该股被剔除
    selected = pd.DataFrame(
        [
            {"lib": "a", "name": "fwd", "sgn": 1, "w": 0.1},
            {"lib": "a", "name": "rev", "sgn": -1, "w": 0.1},
            {"lib": "a", "name": "aux", "sgn": 1, "w": 0.1},
        ]
    )
    syms = ["S1", "S2", "S3"]
    lib_frames = {
        "a": pd.DataFrame(
            {
                "fwd": [0.9, 0.1, np.nan],  # 高值
                "rev": [0.1, 0.9, 0.5],  # 反向：低值才是利好
                "aux": [0.9, 0.9, 0.5],
            },
            index=syms,
        )
    }

    # Act
    comp = CP.composite_day(selected, lib_frames).dropna()

    # Assert：S1 正向因子高+反向因子低 → 分应高于 S2（仅 aux 平）
    assert comp["S1"] > comp["S2"]
    # S3 缺 fwd（1/3 权重 → 覆盖 2/3 < 70%）→ 被覆盖门槛剔除
    assert "S3" not in comp.index


# ── 1.1 契约金样同步（§八：schema 演进；两侧测试共读防漂移） ─────────────


@pytest.mark.unit
def test_金样_schema版本与代码一致(golden):
    # Assert：config.SCHEMA_VERSION 是唯一事实源，金样锁其对外形态
    assert golden["boardSchemaVersion"] == C.SCHEMA_VERSION


@pytest.mark.unit
def test_金样_顶层键_必需与可选并集等于产板自校验全集(golden):
    # Arrange
    required = set(golden["boardRequiredTop"])
    optional = set(golden["boardOptionalTop"])

    # Assert：两集不交；并集 == board_io 产板自校验的顶层键（1.1 的 regime/dedup
    # 键必须存在、值可为 null —— 前端只把 8 核心键当 REQUIRED，历史 1.0 仍可渲染）
    assert required & optional == set()
    assert required | optional == set(IO._REQUIRED_TOP)


@pytest.mark.unit
def test_金样_衰减枚举与状态机四态一一对应(golden):
    # Arrange：尾窗 20 日；不足 15 日 → insufficient；对齐量 = sgn(ic60)×IC
    cases = {
        "insufficient": (pd.Series([0.1] * 10), 0.1),
        "flipped": (pd.Series([-0.1] * 20), 0.1),
        "weak": (pd.Series([0.01] * 20), 0.1),
        "ok": (pd.Series([0.05] * 20), 0.1),
    }

    # Act
    got = {SEL._decay_stat(s, ic60)["decay"] for s, ic60 in cases.values()}

    # Assert：行为四态 == 金样枚举（前端 DECAY_META 以同一金样锁逐键映射）
    assert got == set(golden["decayEnum"])


@pytest.mark.unit
def test_金样_制度注记枚举与代码字面量一致(golden):
    # Assert：四分支行为断言在 test_factor_board_regime.py 各自成例
    # （weak/ok/insufficient/unavailable）；此处锁金样字面量本身，
    # 前端 REGIME_NOTE_META 以同一金样锁逐键映射。
    assert set(golden["regimeNoteEnum"]) == {
        "ok",
        "weak",
        "insufficient",
        "unavailable",
    }


@pytest.mark.unit
def test_板块广度分母只算有评分成分_且评分样本不足直接剔除():
    idx = [f"6000{i:02d}.SH" for i in range(8)]
    base = pd.DataFrame(
        {
            # 8 只成分里只有 3 只有 composite；旧实现分母=8 → 广度被低估
            "composite": [0.2, 0.1, -0.1, np.nan, np.nan, np.nan, np.nan, np.nan],
            "mdl_rank": [0.8, 0.6, 0.4, np.nan, np.nan, np.nan, np.nan, np.nan],
            "SectorName": ["X"] * 8,
        },
        index=idx,
    )
    g = S.sector_table(base, min_n=3)
    assert list(g.index) == ["X"]
    row = g.loc["X"]
    assert row["n"] == 8 and row["n_scored"] == 3
    assert row["fbreadth"] == pytest.approx(2 / 3)

    # min_n=8：8 只成分只有 3 只有评分 → 不得产出板块判定
    assert S.sector_table(base, min_n=8).empty


@pytest.mark.unit
def test_个股名单_top_包含无模型覆盖标的_象限落空():
    idx = ["600001.SH", "600002.SH", "600003.SH"]
    stock = pd.DataFrame(
        {
            "composite": [0.9, 0.8, -0.9],
            "mdl_rank": [0.9, np.nan, 0.1],
            "quadrant": ["HH", np.nan, "LL"],
            "st": [False, False, False],
            "name": ["甲", "乙", "丙"],
            "sector_l2": [None, None, None],
        },
        index=idx,
    )
    out = S.stock_lists(stock, n=2)
    top = out["top"]
    assert [r["symbol"] for r in top] == [idx[0], idx[1]]
    assert top[1]["model_rank"] is None
    assert top[1]["quadrant"] is None
    assert out["bottom"][0]["symbol"] == idx[2]
