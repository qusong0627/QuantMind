"""自定义市场数据集构建脚本的纯函数测试。

覆盖增量重建的守卫部件：筛选指纹（顺序无关、内容敏感）、缺失分区规划、
板别涨跌停阈值。完整 rebuild() 依赖真实 QuantDB 目录，不在单测覆盖。

涨跌停阈值这几条用例钉的是**回归**：旧实现按代码前缀返回一张静态表
（0.098/0.198/0.298）且不看交易日，于是 2020-08-24 前的创业板被套 20% 板线，
涨停日全被当成可成交样本写进训练标签。下面每条都对应那批缺陷的一个面。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.build_factor_custom_dataset import (
    _limit_threshold,
    _limit_thresholds,
    _missing_dates,
    _selection_fingerprint,
)

#: 主板的 10% 扣掉 0.2pp 取整余量。三个期望值提出来是因为同一行里既要有字面量
#: 又要有豁免注释，写在断言里会超行宽。
_MAIN = 0.098  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_GROWTH = 0.198  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_BSE = 0.298  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

#: 改革分界线两侧 —— 两侧用的是**同一个**创业板代码，差异必须全部来自交易日。
_REFORM_BEFORE = "2019-06-03"
_REFORM_AFTER = "2021-06-03"


def test_limit_thresholds_are_fractions_not_percent():
    """阈值必须与 `ret = close/pre_close - 1` **同单位**（比例，不是百分数）。

    这是本文件最重要的一条：向量版一旦返回百分数（9.8），调用点的
    `ret.abs() >= threshold` 恒假，整个可交易性剔除**静默**失效 —— 每一天的
    涨停跌停都会作为「可成交」样本进训练集，而且不报任何错。用 <0.5 兜住
    这个数量级错误（9.8 与 0.098 只差一个「×100」）。
    """
    for t in _limit_thresholds(pd.Index(["600036.SH", "300750.SZ"]), _REFORM_AFTER):
        assert 0.0 < float(t) < 0.5


def test_limit_threshold_main_board():
    assert _limit_threshold("600036.SH", _REFORM_AFTER) == _MAIN
    assert _limit_threshold("000001.SZ", _REFORM_AFTER) == _MAIN


def test_limit_threshold_pre_reform_chinext_uses_main_board_line():
    """2020-08-24 之前的创业板是 10% 板 —— 旧实现在这里套 19.8% 的线。

    后果不是「多剔除」而是**漏剔除**：真实涨停（+10%）落在 19.8% 线之下，
    判定为可买入，于是买不进的票贡献了账面收益，训练标签被系统性美化。
    """
    assert _limit_threshold("300750.SZ", _REFORM_BEFORE) == _MAIN
    assert _limit_threshold("301001.SZ", _REFORM_BEFORE) == _MAIN


def test_limit_threshold_post_reform_chinext_uses_growth_line():
    assert _limit_threshold("300750.SZ", _REFORM_AFTER) == _GROWTH
    assert _limit_threshold("301001.SZ", _REFORM_AFTER) == _GROWTH


def test_limit_threshold_covers_302_prefix():
    """302 前缀不在旧表内，被当成主板套 9.8% —— 反向的错：20% 板上 +12% 的
    普通阳线被误判成涨停，样本被**无谓剔除**（同一处缺陷的两副面孔）。
    """
    assert _limit_threshold("302132.SZ", _REFORM_AFTER) == _GROWTH
    # 与主板必须不同，否则这条用例在任何前缀表下都成立
    assert _limit_threshold("302132.SZ", _REFORM_AFTER) > _MAIN


def test_limit_threshold_star_board():
    assert _limit_threshold("688981.SH", _REFORM_AFTER) == _GROWTH
    assert _limit_threshold("689009.SH", _REFORM_AFTER) == _GROWTH


def test_limit_threshold_bse_board():
    assert _limit_threshold("830799.BJ", _REFORM_AFTER) == _BSE
    assert _limit_threshold("430047.BJ", _REFORM_AFTER) == _BSE


def test_limit_threshold_sh_b_share_is_not_bse():
    """900xxx 是沪市 B 股（10% 板），不能被「9 开头 = 北交所」的写法误判成 30%。

    旧表按前缀 `("4","8","92")` 判北交所时不至于中招，但同源的模板生成器
    （scripts/gen_ashare_strategy_templates.py）用的是 `("4","8","9")` catch-all，
    900xxx 在那里永远判不出涨停。这条用例把权威口径的下界钉死。
    """
    assert _limit_threshold("900901.SH", _REFORM_AFTER) == _MAIN


def test_limit_thresholds_align_with_input_order():
    """批量版必须逐位对应入参顺序 —— 错位会把 A 的阈值安到 B 头上且不报错。"""
    idx = pd.Index(["600036.SH", "300750.SZ", "830799.BJ"])
    arr = _limit_thresholds(idx, _REFORM_AFTER)

    assert arr.shape == (3,)
    assert arr.dtype == np.float64
    assert list(arr) == [_MAIN, _GROWTH, _BSE]
    # 与标量包装逐位一致（同一实现的两个入口不得分叉）
    assert list(arr) == [
        _limit_threshold(s, _REFORM_AFTER)
        for s in ("600036.SH", "300750.SZ", "830799.BJ")
    ]


def test_limit_thresholds_read_the_trade_date():
    """同一批代码换交易日必须换阈值 —— 传给 limit_pct 的日期若被吞掉，上面
    所有「板别」用例仍会通过，改革分界线却被静默忽略。
    """
    before = _limit_thresholds(pd.Index(["300750.SZ"]), _REFORM_BEFORE)
    after = _limit_thresholds(pd.Index(["300750.SZ"]), _REFORM_AFTER)

    assert float(before[0]) < float(after[0])


def test_limit_thresholds_accept_partition_tag_format():
    """真实调用点传的是 ``dt=YYYYMMDD`` 分区标签，不是 ISO —— 接口必须两种都收。

    这条用例来自一次真实的炸机：单测全写 ISO 全绿，接上真数据第一分区就
    `ValueError: Invalid isoformat string: '20190603'`。夹具比调用点「更宽容」
    会掩盖整类缺陷。
    """
    assert _limit_threshold("300750.SZ", "20190603") == _limit_threshold(
        "300750.SZ", "2019-06-03"
    )
    assert _limit_threshold("300750.SZ", "20190603") == _MAIN


def test_limit_thresholds_accepts_date_object_boundary():
    """分界线当日必须**已**切到 20% —— 改革日是 2020-08-24 而非次日。"""
    assert (
        float(
            _limit_thresholds(pd.Index(["300750.SZ"]), date(2020, 8, 24).isoformat())[0]
        )
        == _GROWTH
    )
    assert (
        float(
            _limit_thresholds(pd.Index(["300750.SZ"]), date(2020, 8, 21).isoformat())[0]
        )
        == _MAIN
    )


def test_selection_fingerprint_is_order_insensitive():
    # Arrange：同一筛选集，列顺序不同
    a = {"l1_factors": ["turn_1", "vol_std_5"], "alpha360": ["a360_x"]}
    b = {"alpha360": ["a360_x"], "l1_factors": ["vol_std_5", "turn_1"]}

    # Act / Assert：指纹一致（列清单按内容比较）
    assert _selection_fingerprint(a) == _selection_fingerprint(b)


def test_selection_fingerprint_changes_with_content():
    base = {"l1_factors": ["turn_1"]}

    # Act：增删列或换库都应改变指纹，触发全量重建
    added = {"l1_factors": ["turn_1", "vol_std_5"]}
    moved = {"alpha360": ["turn_1"]}

    # Assert
    assert _selection_fingerprint(base) != _selection_fingerprint(added)
    assert _selection_fingerprint(base) != _selection_fingerprint(moved)


def test_missing_dates_returns_only_unbuilt_partitions(tmp_path: Path):
    # Arrange：两个已建分区（其一仅有目录、缺 data.parquet）
    (tmp_path / "dt=20260910").mkdir()
    (tmp_path / "dt=20260910" / "data.parquet").write_bytes(b"x")
    (tmp_path / "dt=20260911").mkdir()  # 目录在但文件缺失 → 仍需重建
    dates = ["20260910", "20260911", "20260912"]

    # Act
    missing = _missing_dates(dates, tmp_path)

    # Assert
    assert missing == ["20260911", "20260912"]
