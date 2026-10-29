"""因子概率板 · 精选链单测：闸门 / 族封顶 / 降级 / 汇总口径（纯函数，合成数据）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.shared.factor_board import config as C
from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import selection as SEL


def _cand(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """(lib, name, kept_ic) → 最小候选表。"""
    return pd.DataFrame(
        [
            {
                "name": n,
                "lib": lib,
                "sublibrary": "s",
                "display_name": n,
                "kept_ic": ic,
                "kept_icir": 1.0,
                "turnover": None,
                "src": "kept",
                "group": f"{lib}/s",
            }
            for lib, n, ic in rows
        ]
    )


def _stats(rows: list[tuple[str, str, float, float, float]]) -> pd.DataFrame:
    """(lib, name, ic60, win60, kept_ic) → summarize_ic 形态表。"""
    return pd.DataFrame(
        [
            {
                "lib": lib,
                "name": n,
                "group": f"{lib}/s",
                "display": n,
                "kept_ic": kept,
                "kept_icir": 1.0,
                "turnover": None,
                "src": "kept",
                "ic60": ic60,
                "win60": win,
                "n_days": 60,
                "icir60": ic60 * 10,
            }
            for lib, n, ic60, win, kept in rows
        ]
    )


@pytest.mark.unit
def test_闸门三条件必须同时成立():
    # Arrange：分别只违反一个条件
    stats = _stats(
        [
            ("a", "pass", 0.05, 0.6, 0.04),  # 全过
            ("a", "small_ic", 0.01, 0.9, 0.04),  # |IC60| 不足
            ("a", "flip", -0.05, 0.9, 0.04),  # 方向与联合闸不一致
            ("a", "low_win", 0.05, 0.4, 0.04),  # 胜率不足
        ]
    )

    # Act
    gated = SEL.gate(stats, min_win=0.5)

    # Assert
    assert gated["name"].tolist() == ["pass"]


@pytest.mark.unit
def test_族封顶每族只留_IC60_绝对值前3():
    # Arrange：同一 (lib, group) 5 个 + 另一族 1 个 + 第三族 11 个
    # （第三族是陪跑：把过闸数抬过 MIN_GATED=15，隔离本用例要测的封顶逻辑，
    #  否则会顺带触发 win 降级路径——降级本身另有专测）
    rows = [("a", f"f{i}", 0.2 - i * 0.01, 0.8, 0.05) for i in range(5)]
    rows += [("b", "x", 0.1, 0.8, 0.05)]
    rows += [("c", f"c{i}", 0.09 - i * 0.004, 0.8, 0.05) for i in range(11)]
    stats = _stats(rows)

    # Act
    sel, info = SEL.select_factors(stats)

    # Assert
    fam_a = sel[sel["lib"] == "a"]
    assert len(fam_a) == 3
    assert set(fam_a["name"]) == {"f0", "f1", "f2"}  # 按 |IC60| 降序前3
    assert len(sel[sel["lib"] == "c"]) == 3  # 封顶对每族都生效
    assert info["families"] == 3
    assert info["gated"] == 17
    assert info["degraded"] is False


@pytest.mark.unit
def test_rd_mined_族上限为4():
    rows = [("rd_mined", f"r{i}", 0.2 - i * 0.01, 0.8, 0.05) for i in range(6)]
    stats = _stats(rows)
    sel, _ = SEL.select_factors(stats)
    assert len(sel) == 4


@pytest.mark.unit
def test_过闸不足时降级胜率至_0_45():
    # Arrange：全部胜率 0.47 → 0.5 门槛全灭 → 降级后全过
    stats = _stats([("a", f"f{i}", 0.1, 0.47, 0.05) for i in range(3)])

    # Act
    sel, info = SEL.select_factors(stats)

    # Assert
    assert info["degraded"] is True
    assert len(sel) == 3


@pytest.mark.unit
def test_反向因子保留负号_by_sgn_而权重取正():
    stats = _stats(
        [
            ("a", "rev", -0.3, 0.8, -0.05),  # 反向因子：低值利好
            ("a", "fwd", 0.005, 0.8, 0.05),  # |IC60| 不足，落选
        ]
    )
    sel, _ = SEL.select_factors(stats)
    row = sel.iloc[0]
    assert row["name"] == "rev"
    assert row["sgn"] == -1
    assert row["w"] == pytest.approx(0.15)  # |IC60|=0.3 截断到上限
    assert row["w"] > 0  # 权重恒正，方向由 sgn 携带


@pytest.mark.unit
def test_汇总口径_ic60_与方向胜率():
    # Arrange：一个因子 60 天里 58 天正 IC、2 天负
    ics = [0.1] * 58 + [-0.05, -0.05]
    ic_table = {("a", "f"): ics}
    cand = _cand([("a", "f", 0.08)])

    # Act
    stats = SEL.summarize_ic(ic_table, cand)

    # Assert
    row = stats.iloc[0]
    assert row["ic60"] == pytest.approx(np.mean(ics))
    assert row["win60"] == pytest.approx(58 / 60)
    assert row["n_days"] == 60


@pytest.mark.unit
def test_有效天数不足40天不可评估():
    ic_table = {("a", "f"): [0.1] * 39}
    stats = SEL.summarize_ic(ic_table, _cand([("a", "f", 0.08)]))
    assert stats.empty


# ═══════════════════ P2-b：衰减四态（方向对齐 sgn×IC） ═══════════════════


def _decay_row(tail: list[float], head: float = 0.2):
    """前 40 日 head、尾 20 日 tail 的 IC 序列 → summarize 行。"""
    ics = [head] * 40 + tail
    stats = SEL.summarize_ic({("a", "f"): ics}, _cand([("a", "f", 0.08)]))
    return stats.iloc[0]


@pytest.mark.unit
def test_衰减_ok_尾窗方向对齐均值过阈值():
    row = _decay_row([0.1] * 20)
    assert row["decay"] == "ok"
    assert row["ic20"] == pytest.approx(0.1)
    assert row["win20"] == pytest.approx(1.0)


@pytest.mark.unit
def test_衰减_weak_尾窗均值绝对值过小():
    # 全窗均值仍为正（ic60 不被带反），但尾 20 日均值仅 0.005 < 0.02
    row = _decay_row([0.005] * 20)
    assert row["decay"] == "weak"
    assert row["ic20"] == pytest.approx(0.005)
    assert row["ic60"] > 0  # 方向由全窗定


@pytest.mark.unit
def test_衰减_flipped_尾窗对齐后为负_反转显形():
    # 前 40 日 0.2、尾 20 日 -0.1 → 全窗均值仍 +0.1，但尾窗对齐后 -0.1
    row = _decay_row([-0.1] * 20)
    assert row["decay"] == "flipped"
    assert row["ic20"] == pytest.approx(-0.1)  # 负值原样显形，不截断
    assert row["win20"] == pytest.approx(0.0)


@pytest.mark.unit
def test_衰减_flipped_反向因子_sgn对齐后不误判():
    """反向因子（ic60<0）尾窗持续负 IC = 方向一致 = ok，不是 flipped。"""
    ics = [-0.2] * 60
    stats = SEL.summarize_ic({("a", "rev"): ics}, _cand([("a", "rev", -0.08)]))
    row = stats.iloc[0]
    assert row["decay"] == "ok"
    assert row["ic20"] == pytest.approx(0.2)  # sgn(-1) × (-0.2) 对齐后为正


@pytest.mark.unit
def test_衰减_insufficient_直接打状态机():
    """有效日 <DECAY_MIN_DAYS 的尾窗 → insufficient（不硬判）。

    经 summarize_ic 不可达（MIN_IC_DAYS=40 > DECAY_WINDOW=20，尾窗恒 ≥20 日），
    这里直接打状态机分支，防未来调参把该态改坏。
    """
    assert C.DECAY_MIN_DAYS < C.MIN_IC_DAYS  # 记录不可达的前提
    got = SEL._decay_stat(pd.Series([0.1] * (C.DECAY_MIN_DAYS - 1)), ic60=0.1)
    assert got["decay"] == "insufficient"
    assert got["ic20"] is None and got["win20"] is None


# ═══════════════════ P2-a：净 IC 统计（eroded/三态） ═══════════════════


@pytest.mark.unit
def test_净IC_无净轨时四列全None():
    stats = SEL.summarize_ic({("a", "f"): [0.1] * 60}, _cand([("a", "f", 0.08)]))
    row = stats.iloc[0]
    for key in ("net_ic60", "net_win60", "net_icir60", "eroded"):
        assert row[key] is None or pd.isna(row[key]), key


@pytest.mark.unit
def test_净IC_eroded_成本把符号吃反():
    # 毛 IC 为正，净轨多数为负 → eroded=True
    net = [-0.05] * 55 + [0.02] * 5
    stats = SEL.summarize_ic(
        {("a", "f"): [0.1] * 60}, _cand([("a", "f", 0.08)]), net_table={("a", "f"): net}
    )
    row = stats.iloc[0]
    assert row["net_ic60"] < 0
    assert bool(row["eroded"]) is True  # DataFrame 存储后是 np.bool_
    # 净轨有效日不足 → 四列回 None（不拿 39 天冒充）
    net_short = net[:39] + [np.nan] * 21
    stats2 = SEL.summarize_ic(
        {("a", "f"): [0.1] * 60},
        _cand([("a", "f", 0.08)]),
        net_table={("a", "f"): net_short},
    )
    row2 = stats2.iloc[0]
    assert pd.isna(row2["net_ic60"])  # 列被 pandas 归一成 float → NaN
    assert row2["eroded"] is None or pd.isna(row2["eroded"])


@pytest.mark.unit
def test_成本率解析_env优先_坏值不炸(monkeypatch):
    monkeypatch.setenv("QM_MINING_COST_RATE", "0.003")
    got = SEL.resolve_cost_rate()
    assert got == {"rate": 0.003, "source": "env", "warning": None}
    # 非法值 → 落单源解析（不抛；rate 一定是正浮点）
    monkeypatch.setenv("QM_MINING_COST_RATE", "not-a-number")
    got = SEL.resolve_cost_rate()
    assert isinstance(got["rate"], float) and got["rate"] > 0
    assert got["source"] in {"mining_plugins", "fallback"}


# ═══════════════ P2-a：compute_ic_table 净轨口径（合成 QuantDB 树） ═══════════════


def _write_factor_day(tmp_path, lib: str, dt: str, values: dict[str, float | None]):
    """写一个库分区（symbol + f 列；None → NaN）。"""
    p = tmp_path / "6_ml_datasets" / lib / f"dt={dt}"
    p.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": list(values), "f": [values[s] for s in values]}).to_parquet(
        p / "data.parquet", index=False
    )


def _net_fixture(tmp_path):
    """5 个评估日 × 12 只标的的合成树 + 前向收益。

    12 只时 rank_pct ≥ 0.7 ⟺ 名次 9~12（第 8 名 = 0.667，无边界并列），
    多头名单 = 前 4 名。因子值（按符号序）：d1 名单 {s0,s1,s2,s3}；
    d2/d3 s5 跳进前 4 → 名单 {s0,s1,s2,s5}（**s5 新进**、s3 退出——退出
    不收费，成本只扣新进）；d4 仅 8 只有值（<10 → 名单日样本不足，prev
    不许前进）；d5 恢复满档且与 d3 同名单（若 prev 被错误推进到 d4 的
    名单，这里会因 s5 再次「新进」而扣费——本用例的判别位）。

    前向原始收益 = 因子值本身（毛 RankIC 恒 ≈ 1.0）；成本 1000（巨大）
    确保被扣费的新进名名次翻到榜尾，净 IC 明显下移。
    """
    syms = [f"6000{i:02d}.SH" for i in range(12)]
    base = {s: float(12 - i) for i, s in enumerate(syms)}  # s0=12 … s11=1
    d2 = dict(base)
    d2[syms[5]] = 9.5  # s5（原第 6 名）挤进前 4
    d4 = {s: base[s] for s in syms[:8]}  # 仅 8 只有值
    vals_by = {
        "20260101": base,
        "20260102": d2,
        "20260103": d2,
        "20260104": d4,
        "20260105": d2,
    }
    for dt, values in vals_by.items():
        _write_factor_day(tmp_path, "a", dt, values)
    dates = list(vals_by)
    raw = {dt: pd.Series(v, dtype=float) for dt, v in vals_by.items()}
    ranks = {dt: r.rank() for dt, r in raw.items()}
    return dates, raw, ranks, syms


@pytest.mark.unit
def test_净IC_只扣当日新进_首日不扣_成本为零等于毛(tmp_path, monkeypatch):
    monkeypatch.setattr(SEL.C, "MIN_STOCK_SAMPLE", 5)  # 合成盘 10 只
    dates, raw, ranks, _ = _net_fixture(tmp_path)
    cand = _cand([("a", "f", 0.08)])

    acc, net, days = SEL.compute_ic_table(
        tmp_path, cand, dates, ranks, fwd_ret=raw, cost_rate=1000.0
    )
    gross = acc[("a", "f")]
    netv = net[("a", "f")]

    # 逐日对齐 + 毛 IC 恒为 1（前向收益=因子值）
    assert days["a"] == dates
    assert gross == pytest.approx([1.0] * 5, abs=1e-12)
    # d1 首日不扣费但照常计 IC
    assert netv[0] == pytest.approx(1.0, abs=1e-12)
    # d2 新进 {s5}: 扣费前 = 1.0；s5 扣费后名次从 9 翻到 1——
    # 手算：rank_v=[12,11,10,8,7,9,6,5,4,3,2,1] vs rank_adj=[12,11,10,9,8,1,7,6,5,4,3,2]
    # → Pearson = 107/(2×71.5) = 107/143
    assert netv[1] == pytest.approx(107 / 143, abs=1e-9)
    assert netv[1] < 0.9
    # d3 名单与 d2 相同 → 无新进 → 不扣费
    assert netv[2] == pytest.approx(1.0, abs=1e-12)
    # d4 样本不足（8 只 <10）→ 名单日不成立，不扣费且 prev 不前进
    assert netv[3] == pytest.approx(gross[3], abs=1e-12)
    # d5 若 prev 被错误推进到 d4 名单则 s5 会再次「新进」而扣费；
    # prev 应仍停在 d3 → 名单不变 → 无新进
    assert netv[4] == pytest.approx(1.0, abs=1e-12)

    # 成本 0（显式给出但为零）→ 净轨启用但等于毛（0 是真实值不是「没给」）
    acc0, net0, _ = SEL.compute_ic_table(
        tmp_path, cand, dates, ranks, fwd_ret=raw, cost_rate=0.0
    )
    assert net0[("a", "f")] == pytest.approx(acc0[("a", "f")], abs=1e-12)


@pytest.mark.unit
def test_净IC_未给成本或未给原始收益_净轨不启用(tmp_path, monkeypatch):
    monkeypatch.setattr(SEL.C, "MIN_STOCK_SAMPLE", 5)
    dates, raw, ranks, _ = _net_fixture(tmp_path)
    cand = _cand([("a", "f", 0.08)])
    _, net_a, _ = SEL.compute_ic_table(tmp_path, cand, dates, ranks, fwd_ret=raw)
    assert net_a is None
    _, net_b, _ = SEL.compute_ic_table(tmp_path, cand, dates, ranks, cost_rate=0.002)
    assert net_b is None


@pytest.mark.unit
def test_ICIR_是均值除标准差_t_才乘根号n():
    """ICIR 与 t 不可混用：ICIR=mean/std，t=ICIR×√n（旧实现把 t 写进 ICIR）。"""
    cand = _cand([("a", "f", 0.05)])
    ics = list(np.linspace(0.01, 0.11, 60))
    stats = SEL.summarize_ic({("a", "f"): ics}, cand)
    row = stats.iloc[0]
    s = pd.Series(ics)
    expected_icir = float(s.mean() / s.std())
    assert row["icir60"] == pytest.approx(expected_icir, rel=1e-9)
    assert row["t60"] == pytest.approx(expected_icir * np.sqrt(60), rel=1e-9)
    assert row["t60"] != pytest.approx(row["icir60"])


@pytest.mark.unit
def test_全部因子不过闸时返回空表而不是抛异常():
    stats = _stats([("a", "weak", 0.01, 0.4, 0.05)])
    sel, info = SEL.select_factors(stats)
    assert sel.empty
    assert "sgn" in sel.columns and "w" in sel.columns
    assert info["gated"] == 0


@pytest.mark.unit
def test_共同分区日取全库交集_不按_l1_最新日(tmp_path):
    root = tmp_path / "quantdb"
    for lib, dates in {
        "l1_factors": ["20261027", "20261028"],
        "alpha360": ["20261027"],
        "rd_mined": ["20261024", "20261027"],
    }.items():
        for d in dates:
            (root / "6_ml_datasets" / lib / f"dt={d}").mkdir(parents=True)
    common, latest = CP.common_factor_dates(
        root, ["l1_factors", "alpha360", "rd_mined"]
    )
    assert common == ["20261027"]  # 10-28 只有 l1 有，不进入共同截面
    assert latest == {
        "l1_factors": "20261028",
        "alpha360": "20261027",
        "rd_mined": "20261027",
    }
