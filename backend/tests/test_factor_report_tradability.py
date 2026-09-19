"""可交易性口径：定向断言 + 已知答案。

这套掩码最容易出的错不是崩溃而是**方向反了**：把涨停判成可买、或把剔除写成了保留，
结果「可交易轨比理想轨还好」——一个看起来像 alpha 的现象。故本文件的重点是
**方向性**与**零项守卫**（空集合不能静默报通过）。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.factor_report import tradability as TB


# ═══════════════════ 1. 盘面 → 掩码 ═══════════════════


class _Bar:
    """DailyBar 的最小替身：只带本模块读取的字段。"""

    def __init__(self, close, limit_up=np.inf, limit_down=0.0, volume=1e6, is_st=False):
        self.close = close
        self.limit_up = limit_up
        self.limit_down = limit_down
        self.volume = volume
        self.suspended = volume <= 0
        self.is_st = is_st


def test_涨停挡多头_跌停挡空头_停牌两腿都挡():
    bars = {
        "600000.SH": _Bar(11.0, limit_up=11.0, limit_down=9.0),      # 涨停
        "600001.SH": _Bar(9.0, limit_up=11.0, limit_down=9.0),       # 跌停
        "600002.SH": _Bar(10.0, limit_up=11.0, limit_down=9.0),      # 正常
        "600003.SH": _Bar(10.0, limit_up=11.0, limit_down=9.0, volume=0),  # 停牌
    }
    rows = {r[1]: (r[2], r[3]) for r in TB.blocked_rows_for_date(bars, 20260101)}
    assert rows["600000.SH"] == (True, False), "涨停：买不进，但可以卖"
    assert rows["600001.SH"] == (False, True), "跌停：卖不出，但可以买"
    assert "600002.SH" not in rows, "正常标的**不该**产生行（稀疏存储）"
    assert rows["600003.SH"] == (True, True), "停牌两腿都挡"


def test_ST_标的整票不入掩码():
    """ST 名单是静态快照（前视偏差），且其历史限价被按 ±5% 折减会误判涨停。"""
    bars = {
        "600000.SH": _Bar(11.0, limit_up=11.0, limit_down=9.0, is_st=True),
        "600001.SH": _Bar(11.0, limit_up=11.0, limit_down=9.0, is_st=False),
    }
    rows = TB.blocked_rows_for_date(bars, 20260101)
    assert [r[1] for r in rows] == ["600001.SH"], "ST 标的必须缺席（既不当阻挡也不假装可交易）"


def test_新股首日无昨收_不是涨跌停():
    """``compute_limits`` 对无昨收返回 (inf, 0.0) —— 不能被判成「涨停+跌停」。"""
    bars = {"600000.SH": _Bar(10.0, limit_up=np.inf, limit_down=0.0)}
    assert TB.blocked_rows_for_date(bars, 20260101) == []


def test_空盘面返回空而不是抛():
    assert TB.blocked_rows_for_date({}, 20260101) == []


# ═══════════════════ 2. 对齐 ═══════════════════


def test_对齐按行序_而不是集合顺序():
    """错位会把 A 的涨跌停状态扣到 B 头上，且结果「看起来也像有拦截」。"""
    syms = ["600003.SH", "600001.SH", "600002.SH"]
    got = TB.align_blocked(syms, (frozenset({"600001.SH"}), frozenset({"600003.SH"})))
    blk_long, blk_short = got
    assert blk_long.tolist() == [False, True, False]
    assert blk_short.tolist() == [True, False, False]


def test_掩码为空时返回_None_由调用方降级():
    assert TB.align_blocked(["600000.SH"], None) is None
    assert TB.align_blocked(["600000.SH"], (frozenset(), frozenset())) is None


def test_掩码读写往返(tmp_path):
    bars = {
        "600000.SH": _Bar(11.0, limit_up=11.0, limit_down=9.0),
        "600001.SH": _Bar(9.0, limit_up=11.0, limit_down=9.0),
    }
    df = __import__("pandas").DataFrame(TB.blocked_rows_for_date(bars, 20260101), columns=list(TB.MASK_COLUMNS))
    path = tmp_path / "trade_mask.parquet"
    stat = TB.write_mask(df, path)
    assert stat["rows"] == 2 and stat["blocked_long"] == 1 and stat["blocked_short"] == 1
    back = TB.load_mask(path)
    assert back[20260101][0] == frozenset({"600000.SH"})
    assert back[20260101][1] == frozenset({"600001.SH"})


def test_掩码路径不存在返回空字典():
    assert TB.load_mask("/nonexistent/trade_mask.parquet") == {}


# ═══════════════════ 3. 可交易轨：从理想轨的计数里减掉被挡成员 ═══════════════════


def _ideal(idx, yv, nq):
    """理想轨的 (组计数, 组收益和) —— 与被测函数复用同一对数组，故口径必然一致。"""
    cnt = np.bincount(idx, minlength=nq).astype(np.float64)
    ssum = np.bincount(idx, weights=yv, minlength=nq)
    return cnt, ssum


def test_剔掉被挡成员后组均值按剩余成员重算():
    """组内等值时剔除不改变均值 —— 「计数对但算法错」也能通过那种用例。

    故把 G2（idx=1）的成员改成**递增**收益，再断言均值按剩余成员重算到已知值。
    """
    idx = np.array([0, 0, 1, 1, 1, 1])
    yv = np.array([0.01, 0.01, 0.10, 0.20, 0.30, 0.50])
    blk = np.array([False, False, False, False, False, True])   # 挡掉最高的 0.50
    cnt, ssum = _ideal(idx, yv, 2)
    q, n = TB.exclude_blocked(idx, yv, blk, cnt, ssum, n_quantiles=2)
    assert q[0] == pytest.approx(0.01), "未被挡的组分毫不动"
    assert q[1] == pytest.approx(np.mean([0.10, 0.20, 0.30]))    # 理想轨是 0.275
    assert n.tolist() == [0.0, 1.0]


def test_未标记时逐位等于理想轨():
    idx = np.array([0, 0, 1, 1])
    yv = np.array([0.01, 0.03, 0.10, 0.20])
    cnt, ssum = _ideal(idx, yv, 2)
    q, n = TB.exclude_blocked(idx, yv, np.zeros(4, bool), cnt, ssum, n_quantiles=2)
    assert q[0] == pytest.approx(0.02) and q[1] == pytest.approx(0.15)
    assert (n == 0).all()


def test_全涨停时多头腿当日空仓_而不是照常成交():
    """方向性：全市场涨停 → 多头组没有任何成员可买 → 组均值为 NaN（不是原值）。"""
    idx = np.array([0, 1, 1])
    yv = np.array([0.01, 0.02, 0.03])
    cnt, ssum = _ideal(idx, yv, 2)
    q, n = TB.exclude_blocked(idx, yv, np.ones(3, bool), cnt, ssum, n_quantiles=2)
    assert np.isnan(q).all(), "全员被挡时组收益必须无定义"
    assert n.tolist() == [1.0, 2.0]


def test_可交易轨不得优于理想轨():
    """掩码写反（该挡的放过、或干脆没生效）时，可交易轨会『凭空变好』。

    构造：理想轨 G2 的均值被一只**极端高收益**成员抬起来；该成员涨停买不进时，
    可交易轨必须**更低**。实现把 blk 用反了这里就会红。
    """
    idx = np.array([0, 0, 1, 1])
    yv = np.array([0.01, 0.01, 0.01, 5.0])
    cnt, ssum = _ideal(idx, yv, 2)
    q_all, _ = TB.exclude_blocked(idx, yv, np.zeros(4, bool), cnt, ssum, n_quantiles=2)
    q_tr, _ = TB.exclude_blocked(idx, yv, np.array([False, False, False, True]), cnt, ssum, n_quantiles=2)
    assert q_tr[1] < q_all[1], "剔掉被挡的高收益成员后，多头组均值只能更低"
    # 只挡了 G1 的成员时，G2 必须原封不动
    q_g1, _ = TB.exclude_blocked(idx, yv, np.array([True, False, False, False]), cnt, ssum, n_quantiles=2)
    assert q_g1[1] == q_all[1]


def test_空截面返回全_NaN_而不是_0():
    """零项即失败：没有任何成员参与时不能静默返回 0（0 是个『真实观测』）。"""
    q, n = TB.exclude_blocked(np.array([], dtype=int), np.array([]), np.array([], bool),
                              np.zeros(3), np.zeros(3), n_quantiles=3)
    assert np.isnan(q).all() and (n == 0).all()


def test_形状不符即报错():
    cnt, ssum = np.zeros(2), np.zeros(2)
    with pytest.raises(ValueError):
        TB.exclude_blocked(np.zeros(3, int), np.zeros(2), np.zeros(3, bool), cnt, ssum, n_quantiles=2)
    with pytest.raises(ValueError):
        TB.exclude_blocked(np.zeros(3, int), np.zeros(3), np.zeros(2, bool), cnt, ssum, n_quantiles=2)


def test_组编号越界不会污染其他组():
    """bincount 返回的长度可能大于 nq（脏索引）—— 截断必须发生，否则会串组。"""
    idx = np.array([0, 1, 9])          # 9 是非法组号
    yv = np.array([0.01, 0.02, 100.0])
    cnt = np.bincount(idx, minlength=2).astype(float)
    ssum = np.bincount(idx, weights=yv, minlength=2)
    q, _ = TB.exclude_blocked(idx, yv, np.zeros(3, bool), cnt, ssum, n_quantiles=2)
    assert q.tolist() == [pytest.approx(0.01), pytest.approx(0.02)], "越界项不得回流到任何组"


# ═══════════════════ 4. 诊断计数 ═══════════════════


def _fixture():
    """4 组 × 2 因子、20 只股票；组内收益有已知排序，便于断言剔除效果。"""
    n, k = 20, 2
    group = np.full((n, k), -1, dtype=np.int16)
    y = np.zeros(n)
    for i in range(n):
        g = i // 5                      # 0..3
        group[i, 0] = g
        group[i, 1] = 3 - g
        y[i] = 0.01 * (g + 1)           # G1=1% … G4=4%
    return group, y


def test_诊断计数的分母是逐因子有效样本而不是_行数乘因子数():
    """本函数第一版把二维掩码整个 sum —— 20 只 × 2 因子得出「40 只参与」这种量纲错误的数。"""
    group, y = _fixture()
    blk = np.zeros(20, dtype=bool)
    blk[:2] = True
    n_valid, n_blocked = TB.blocked_summary(group, y, blk, n_quantiles=4)
    assert n_valid.tolist() == [20.0, 20.0]
    assert n_blocked.tolist() == [2.0, 2.0]
    # 因子 0 的秩无效（-1）时，它当日不参与 → 分母为 0，而不是全市场行数
    g2 = group.copy()
    g2[:, 0] = -1
    nv2, _ = TB.blocked_summary(g2, y, blk, n_quantiles=4)
    assert nv2.tolist() == [0.0, 20.0]


def test_诊断计数在零项时给_0_而不是抛():
    n_valid, n_blocked = TB.blocked_summary(np.full((3, 1), -1), np.full(3, np.nan), np.zeros(3, bool))
    assert n_valid.tolist() == [0.0] and n_blocked.tolist() == [0.0]


def test_诊断计数形状不符即报错():
    with pytest.raises(ValueError):
        TB.blocked_summary(np.zeros(5), np.zeros(5), np.zeros(5, bool))
    with pytest.raises(ValueError):
        TB.blocked_summary(np.zeros((5, 1)), np.zeros(4), np.zeros(5, bool))
