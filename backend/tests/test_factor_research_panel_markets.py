"""因子研究 B5 —— 数据层与私有面板构建器的市场分层契约（2026-10-16）。

锁死四组不变量：

1. **口径收口点唯一**：``amount_yi_factor``（CN 万元 / 非 CN 元）与
   ``_to_trade_dates``（YYYYMMDD 串/整数 vs timestamp）——各市场不许各写各的
   除数/格式，两处都是实测口径（见 data.py 模块 docstring）；
2. **``build_universe(instr=None)`` 非 CN 全池近似 + 空池反例**：误传空表
   （而不是 None）会得到全 False 股票池，快照全空且全链零报错 —— 所以
   「None 与空表」必须得到**不同**结果，且 None 得到真池；
3. **目录路由 fail-closed**：未知市场抛错绝不落 CN；``load_instrument`` 非 CN
   诚实抛错；估值快照供给由注册表裁决（HK 盘上有分区也不供给）；
4. **end-to-end（真跑 ``main()``）**：非 CN **不读 classic**（fwd/benchmarks
   毒饵全程不被吃）、基准按市场写（HK=恒生系）、无指数市场落**空表**、meta 携带
   market/source 留痕（请求 kept → 强制 auto 的事实必须可见）。
"""
from __future__ import annotations

import functools
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_research import data as frdata

_ROOT = Path(__file__).resolve().parents[2]

#: 迷你构建的交易日（覆盖 4 个月末；120 日历史过滤在 2020-02 之后才放行）
BDAYS = pd.bdate_range("2019-09-02", "2020-04-30")
MONTH_END_DATES = pd.DatetimeIndex(
    pd.Series(BDAYS, index=BDAYS).groupby([BDAYS.year, BDAYS.month]).last().to_numpy()
)
#: 构建器实际采样的月终日（≥ DEFAULT_START=2020-01-01）
DATES_USED = MONTH_END_DATES[MONTH_END_DATES >= "2020-01-01"]
#: 40 只：IC 口径要求每期 ≥30 只（cnt>=30 才计 IC）
SYMS = [f"{i:04d}.HK" for i in range(1, 41)]
FUT_SYMS = [f"F{i:03d}.FUT" for i in range(1, 41)]


@functools.lru_cache(maxsize=1)
def _load_bfpp():
    spec = importlib.util.spec_from_file_location(
        "_bfpp_markets", _ROOT / "backend" / "scripts" / "build_factor_panel_private.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────── 迷你市场根构造 ───────────────────────

def _write_daily(root: Path, syms: list[str]) -> None:
    """非 CN daily_forward 分区：time 为 timestamp[ns]（实测口径），逐日一文件。"""
    rng = np.random.RandomState(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(BDAYS), len(syms))), 0))
    d = root / "1_kline_data" / "daily_forward"
    for i, day in enumerate(BDAYS):
        p = d / f"dt={day:%Y%m%d}"
        p.mkdir(parents=True, exist_ok=True)
        px = close[i]
        pd.DataFrame(
            {
                "time": np.full(len(syms), day.to_datetime64(), dtype="datetime64[ns]"),
                "symbol": syms,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": np.full(len(syms), 1e6),
                "amount": px * 1e6,  # 非 CN 口径：元
            }
        ).to_parquet(p / "data.parquet", index=False)


def _write_factors(root: Path, dates: pd.DatetimeIndex, syms: list[str]) -> None:
    d = root / "6_ml_datasets" / "l1_factors"
    base = np.arange(len(syms), dtype=float)
    for j, day in enumerate(dates):
        p = d / f"dt={day:%Y%m%d}"
        p.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "symbol": syms,
                **{f"a{k}": np.roll(base, k) + j * 0.1 for k in range(1, 7)},
            }
        ).to_parquet(p / "data.parquet", index=False)


def _write_index(root: Path, codes: list[str]) -> None:
    d = root / "1_kline_data" / "index_daily"
    rng = np.random.RandomState(3)
    nav = {c: np.cumprod(1 + rng.normal(0, 0.005, len(MONTH_END_DATES))) for c in codes}
    for j, day in enumerate(MONTH_END_DATES):
        p = d / f"dt={day:%Y%m%d}"
        p.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "time": [day.to_datetime64()] * len(codes),
                "symbol": codes,
                "close": [float(100 * nav[c][j]) for c in codes],
            }
        ).to_parquet(p / "data.parquet", index=False)


def _write_master(root: Path, syms: list[str]) -> None:
    """主表只覆盖前 35 只：30 只中英文齐全、5 只中文为空；尾部 5 只不在主表。"""
    p = root / "2_base_sector" / "security_master" / "data.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": s,
            "cn_name": f"名称{i + 1}" if i < 30 else "",
            "en_name": f"EN{i + 1}",
        }
        for i, s in enumerate(syms[:35])
    ]
    pd.DataFrame(rows).to_parquet(p, index=False)


def _write_sector(root: Path, rows: list[dict]) -> None:
    d = root / "2_base_sector" / "sector"
    d.mkdir(parents=True, exist_ok=True)
    for r in rows:
        pd.DataFrame([r]).to_parquet(d / f"{r['symbol']}.parquet", index=False)


def _write_val(root: Path, day: str, rows: list[dict]) -> None:
    p = root / "5_technical_derived" / "valuation" / f"dt={day}"
    p.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(p / "data.parquet", index=False)


def _poison_classic(root: Path, syms: list[str]) -> None:
    """CN 口径毒饵：非 CN 构建器若去碰 classic，产物里会出现这些值。"""
    classic = root / "factor_research"
    classic.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"trade_date": d, "symbol": s, "fwd_ret": 999.0}
            for d in DATES_USED
            for s in syms
        ]
    ).to_parquet(classic / "fwd_returns.parquet", index=False)
    pd.DataFrame(
        {"trade_date": list(DATES_USED), "nav": 1.0, "index_code": "毒饵000300.SH"}
    ).to_parquet(classic / "benchmarks.parquet", index=False)


# ─────────────────────── end-to-end：真跑 main() ───────────────────────

@pytest.fixture(scope="module")
def hk_e2e(tmp_path_factory):
    root = tmp_path_factory.mktemp("quanthk")
    out = tmp_path_factory.mktemp("hk_out")
    bfpp = _load_bfpp()
    _write_daily(root, SYMS)
    _write_factors(root, DATES_USED, SYMS)
    _write_index(root, ["HSI.HK", "HSTECH.HK"])
    _write_master(root, SYMS)
    _write_sector(  # 实测 HK 板块全为空串
        root,
        [
            {"symbol": s, "sector": "", "industry": "", "source": "yahoo_finance"}
            for s in SYMS[:5]
        ],
    )
    _poison_classic(root, SYMS)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("QM_QUANTHK_DATA_DIR", str(root))
        mp.setenv("FACTOR_RESEARCH_OUT", str(out))
        mp.delenv("QM_QUANTDB_DATA_DIR", raising=False)
        mp.setattr(sys, "argv", ["bfpp", "--market", "HK", "--source", "kept"])
        rc = bfpp.main()
    return {"rc": rc, "root": root, "out": out}


def test_e2e_hk_成功且不读经典毒饵(hk_e2e):
    """毒饵双验证：fwd 用了 classic 会出 999，基准用了 classic 会出 CN 代码。"""
    assert hk_e2e["rc"] == 0
    out = hk_e2e["out"]

    fwd = pd.read_parquet(out / "fwd_returns.parquet")
    assert len(fwd) > 0 and not (fwd["fwd_ret"] == 999.0).any()

    bench = pd.read_parquet(out / "benchmarks.parquet")
    assert set(bench["index_code"]) == {"HSI.HK", "HSTECH.HK"}  # 市场注册表，不是 CN 三指数


def test_e2e_hk_meta携带市场与来源留痕(hk_e2e):
    meta = json.loads(
        (hk_e2e["out"] / "factors.json").read_text(encoding="utf-8")
    )["meta"]
    assert meta["market"] == "HK"
    # 请求 kept（A 股口径）→ 强制 auto，且事实必须可见
    assert meta["source"] == "auto"
    assert meta["source_requested"] == "kept"
    assert "A 股口径" in meta["source_skip_reason"]
    assert "无 ST/退市口径" in meta["universe"]
    assert meta["sources"]["l1_factors"] == 6


def test_e2e_hk_个股快照名称链与诚实置空(hk_e2e):
    snap = pd.read_parquet(hk_e2e["out"] / "stock_snapshot.parquet").set_index("symbol")
    assert snap.loc[SYMS[0], "name"] == "名称1"  # cn_name 优先
    assert snap.loc[SYMS[30], "name"] == "EN31"  # cn 空 → en_name
    assert snap.loc[SYMS[35], "name"] == SYMS[35]  # 主表无此标的 → 回退代码
    assert snap["industry"].isna().all()  # 夹具未提供 profile/sector 值 → 置空，不造行业
    assert snap["total_mv_yi"].isna().all()  # 夹具未提供 akshare_financial → 不造市值
    assert np.isfinite(snap["avg_amount_yi"]).all()  # 近 252 日均额（元→亿换算）有值


def test_e2e_hk_面板只收有效采样日(hk_e2e):
    panel = pd.read_parquet(hk_e2e["out"] / "factor_panel.parquet")
    assert set(panel["factor_code"]) == {f"a{k}" for k in range(1, 7)}
    # 2020-01-31 历史不足 120 日 → 股票池空、该期无面板行；其余 3 期 × 40 只
    assert len(panel) == 6 * 3 * 40
    assert panel["trade_date"].min() == pd.Timestamp("2020-02-28")


@pytest.fixture(scope="module")
def fut_e2e(tmp_path_factory):
    root = tmp_path_factory.mktemp("quantfutures")
    out = tmp_path_factory.mktemp("fut_out")
    bfpp = _load_bfpp()
    _write_daily(root, FUT_SYMS)
    _write_factors(root, DATES_USED, FUT_SYMS)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("QM_QUANTFUTURES_DATA_DIR", str(root))
        mp.setenv("FACTOR_RESEARCH_OUT", str(out))
        mp.delenv("QM_QUANTDB_DATA_DIR", raising=False)
        mp.setattr(sys, "argv", ["bfpp", "--market", "FUTURES"])
        rc = bfpp.main()
    return {"rc": rc, "out": out}


def test_e2e_futures_无指数市场基准落空表(fut_e2e):
    """空表 ≠ 缺文件：缺文件是「没建过」（可一键构建），空表是「本市场无基准」。"""
    assert fut_e2e["rc"] == 0
    bench = pd.read_parquet(fut_e2e["out"] / "benchmarks.parquet")
    assert len(bench) == 0
    assert list(bench.columns) == ["trade_date", "nav", "index_code"]


def test_e2e_futures_无主表名称回退代码(fut_e2e):
    snap = pd.read_parquet(fut_e2e["out"] / "stock_snapshot.parquet")
    assert len(snap) == 40
    assert (snap["name"] == snap["symbol"]).all()
    # 无持仓/乘数 → 20 日成交额代理；未识别品种归「其他」，两项都不留空
    assert np.isfinite(snap["total_mv_yi"]).all()
    assert (snap["industry"] == "其他").all()


# ─────────────────────── 口径收口点 ───────────────────────

def test_单位换算按市场():
    assert frdata.amount_yi_factor("CN") == 1e-4  # 万元 → 亿元
    assert frdata.amount_yi_factor("HK") == 1e-8  # 元 → 亿元
    assert frdata.amount_yi_factor("US") == 1e-8
    assert frdata.amount_yi_factor("FUTURES") == 1e-8
    with pytest.raises(ValueError, match="未知市场"):
        frdata.amount_yi_factor("XX")  # fail-closed，绝不落 CN


def test_交易日索引兼容三形状():
    want = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    ts = pd.DatetimeIndex(["2024-01-02 09:30", "2024-01-03 15:00"])  # 非 CN timestamp
    assert (frdata._to_trade_dates(ts) == want).all()
    strs = pd.Index(["20240102", "20240103"], dtype=object)  # CN 串
    assert (frdata._to_trade_dates(strs) == want).all()
    ints = pd.Index([20240102, 20240103])  # CN 整数
    assert (frdata._to_trade_dates(ints) == want).all()


def test_跨分区日期列混档归一(tmp_path: Path):
    """HK daily_forward 实测两个 dtype 档：20260508 前 ``date32[day]``（读出来是
    ``datetime.date``），20260511 起 ``timestamp[ns]``（``Timestamp``）。拼成一列是
    object，下游 ``pivot_table`` 的 groupby("last") 要对键排序比较 —— Timestamp 与
    date 比较直接 TypeError（2026-10-16 实机构建所踩，构建在写盘前一步炸掉）。

    锁两件事：归一发生在 ``_read_files``（唯一三处读取的公共入口），且归一之后
    pivot 真能跑通 —— 只断言 dtype 不断言 pivot 的话，「归一成别的畸形 dtype」
    这类改法能骗过测试。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    old = tmp_path / "dt=20260508"  # date32 档（早期分区实测型）
    new = tmp_path / "dt=20260511"  # timestamp 档（20260511 起实测型）
    old.mkdir()
    new.mkdir()
    pq.write_table(
        pa.table(
            {
                "time": pa.array([date(2026, 5, 8)], type=pa.date32()),
                "symbol": ["0001.HK"],
                "close": [1.0],
            }
        ),
        old / "data.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "time": pa.array([pd.Timestamp("2026-05-11")], type=pa.timestamp("ns")),
                "symbol": ["0001.HK"],
                "close": [1.1],
            }
        ),
        new / "data.parquet",
    )

    df = frdata._read_files(
        [old / "data.parquet", new / "data.parquet"], columns=["time", "symbol", "close"]
    )

    assert pd.api.types.is_datetime64_any_dtype(df["time"]), "混档列未归一到 datetime64"
    out = df.pivot_table(index="time", columns="symbol", values="close", aggfunc="last")
    assert out.shape == (2, 1)


# ─────────────────────── 股票池 ───────────────────────

def _close_frame(n_days: int = 140, n_syms: int = 4) -> pd.DataFrame:
    days = pd.bdate_range("2024-01-01", periods=n_days)
    return pd.DataFrame(
        np.ones((n_days, n_syms)),
        index=days,
        columns=[f"S{i}" for i in range(n_syms)],
    )


def test_universe_非cn_none走全池近似():
    close = _close_frame()
    mask = frdata.build_universe(None, close)
    assert mask.iloc[-1].all()  # 历史足够 → 全进池
    assert not mask.iloc[0].any()  # 120 日窗口未满 → 不进池（滤新股/长停仍生效）


def test_universe_空池反例_空表必须全拒():
    """反例锁死：把 instr 误传成**空表**（而不是 None）会得到全 False 股票池，
    快照全空且全链零报错。None 与空表必须得到不同结果。"""
    close = _close_frame()
    empty_instr = pd.DataFrame(columns=["symbol", "is_st", "is_quit", "industry"])
    mask = frdata.build_universe(empty_instr, close)
    assert not mask.to_numpy().any()


def test_universe_cn_st退市过滤不回归():
    close = _close_frame(n_syms=3)
    instr = pd.DataFrame(
        {
            "symbol": ["S0", "S1", "S2"],
            "is_st": ["0", "1", "0"],
            "is_quit": ["0", "0", "1"],
        }
    )
    mask = frdata.build_universe(instr, close)
    assert mask.iloc[-1].tolist() == [True, False, False]


# ─────────────────────── 加载器路由与诚实面 ───────────────────────

def test_load_instrument非cn诚实抛错(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(tmp_path / "quanthk"))
    with pytest.raises(FileNotFoundError, match="A 股专属"):
        frdata.load_instrument("HK")


def test_分区扫描未知市场绝不落cn(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path / "cnroot"))
    with pytest.raises(ValueError, match="未知市场"):
        frdata.load_daily_panel("20240101", "20240131", market="XX")


def test_主表缺失返回none_缺列补空(tmp_path, monkeypatch):
    root = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))
    assert frdata.load_security_master("HK") is None

    p = root / "2_base_sector" / "security_master" / "data.parquet"
    p.parent.mkdir(parents=True)
    pd.DataFrame({"symbol": ["0001.HK"], "cn_name": ["长和"]}).to_parquet(
        p, index=False
    )
    out = frdata.load_security_master("HK")
    assert out is not None
    assert list(out.columns) == ["symbol", "cn_name", "en_name"]
    assert pd.isna(out.loc[0, "en_name"])  # 缺列补空，不炸


def test_板块目录缺失返回none(tmp_path, monkeypatch):
    root = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))
    assert frdata.load_sector_map("HK") is None

    d = root / "2_base_sector" / "sector"
    d.mkdir(parents=True)
    pd.DataFrame(
        [{"symbol": "0001.HK", "sector": "", "industry": ""}]
    ).to_parquet(d / "0001.HK.parquet", index=False)
    out = frdata.load_sector_map("HK")
    assert out is not None
    assert set(out.columns) <= {"symbol", "industry", "sector"}
    assert list(out["symbol"]) == ["0001.HK"]


def test_估值快照_us分区与hk财务表(tmp_path, monkeypatch):
    us, hk = tmp_path / "quantus", tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTUS_DATA_DIR", str(us))
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))
    _write_val(
        us, "20260101", [{"symbol": "AAPL", "total_mv": 1.0, "pe_ttm": 2.0, "pb": 3.0}]
    )
    _write_val(
        us,
        "20260202",
        [{"symbol": "AAPL", "total_mv": 10.0, "pe_ttm": 20.0, "pb": 30.0}],
    )
    # HK：5_technical_derived/valuation 为空，估值走 akshare_financial
    fin = hk / "2_base_sector" / "akshare_financial"
    fin.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0001.HK",
                "总市值(港元)": 2.69e11,
                "已发行股本(股)": 3.83e9,
                "市盈率": 7.1,
                "市净率": 0.47,
            }
        ]
    ).to_parquet(fin / "0001.HK.parquet", index=False)

    us_out = frdata.load_valuation_snapshot("US")
    assert us_out is not None and float(us_out.loc[0, "total_mv"]) == 10.0  # 取最新分区

    hk_out = frdata.load_valuation_snapshot("HK")
    assert hk_out is not None
    row = hk_out.set_index("symbol").loc["0001.HK"]
    assert float(row["total_mv"]) == pytest.approx(2.69e11)
    assert float(row["shares"]) == pytest.approx(3.83e9)
    assert float(row["pe_ttm"]) == pytest.approx(7.1)
    assert frdata.load_valuation_snapshot("FUTURES") is None


def test_hk行业回退到profile_不拿空串或英文sector凑数(tmp_path, monkeypatch):
    root = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))
    d = root / "2_base_sector" / "sector"
    d.mkdir(parents=True)
    pd.DataFrame(
        [{"symbol": "0001.HK", "sector": "Conglomerates", "industry": ""}]
    ).to_parquet(d / "0001.HK.parquet", index=False)
    prof = root / "2_base_sector" / "akshare_profile"
    prof.mkdir(parents=True)
    pd.DataFrame(
        [{"symbol": "0001.HK", "所属行业": "综合企业"}]
    ).to_parquet(prof / "0001.HK.parquet", index=False)

    out = frdata.load_sector_map("HK")
    assert out is not None
    row = out.set_index("symbol").loc["0001.HK"]
    assert row["industry"] == "综合企业"  # 恒生中文行业优先于 Yahoo sector


def test_期货品种板块与乘数映射(tmp_path, monkeypatch):
    root = tmp_path / "quantfutures"
    monkeypatch.setenv("QM_QUANTFUTURES_DATA_DIR", str(root))
    rule = root / "2_base_sector" / "trade_rule" / "dt=20260101"
    rule.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "variety": "CU",
                "variety_name": "铜",
                "exchange": "上期所",
                "multiplier": 5,
            },
            {
                "variety": "T",
                "variety_name": "10年期国债",
                "exchange": "中金所",
                "multiplier": 10000,
            },
            {
                "variety": "M",
                "variety_name": "豆粕",
                "exchange": "大商所",
                "multiplier": 10,
            },
        ]
    ).to_parquet(rule / "data.parquet", index=False)

    assert frdata.futures_variety_code("CU0.CN") == "CU"
    assert frdata.futures_variety_code("CU2610.CN") == "CU"
    assert frdata.futures_variety_code("CL.FUT") == "CL"
    smap = frdata.futures_variety_sector_map()
    assert smap["CU"] == "有色"
    assert smap["T"] == "金融"
    assert smap["M"] == "农产品"
    assert smap["CL"] == "能化"  # 外盘显式表兜底
    mult = frdata.futures_multiplier_map()
    assert mult["CU"] == 5
    assert mult["T"] == 10000


# ─────────────────────── 非 CN 个股快照组装 ───────────────────────

def test_个股快照_us组装_回退链与单位(tmp_path, monkeypatch):
    root = tmp_path / "quantus"
    monkeypatch.setenv("QM_QUANTUS_DATA_DIR", str(root))
    p = root / "2_base_sector" / "security_master" / "data.parquet"
    p.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": ["AAPL", "AAL", "ZZZ"],
            "cn_name": ["苹果", "", "泽兹"],
            "en_name": ["APPLE", "AMERICAN", "ZZZ INC"],
        }
    ).to_parquet(p, index=False)
    _write_sector(
        root,
        [{"symbol": "AAPL", "sector": "Technology", "industry": "Consumer Electronics"}],
    )
    _write_sector(
        root, [{"symbol": "AAL", "sector": "Industrials", "industry": ""}]
    )
    _write_val(
        root,
        "20260202",
        [
            {"symbol": "AAPL", "total_mv": 5e12, "pe_ttm": 39.0, "pb": 46.0},
            {"symbol": "AAL", "total_mv": 7.8e9, "pe_ttm": None, "pb": -1.9},
        ],
    )
    idx = pd.bdate_range("2024-01-01", periods=3)
    cols = ["AAPL", "AAL", "ZZZ"]
    close = pd.DataFrame(np.full((3, 3), 10.0), index=idx, columns=cols)
    amount = pd.DataFrame(np.full((3, 3), 10.0), index=idx, columns=cols)  # 元

    snap = _load_bfpp()._stock_snapshot_non_cn(
        "US", {"close": close, "amount": amount}
    ).set_index("symbol")

    assert snap.loc["AAPL", "name"] == "苹果"
    assert snap.loc["AAL", "name"] == "AMERICAN"  # cn 空 → en
    assert snap.loc["ZZZ", "name"] == "泽兹"
    assert snap.loc["AAPL", "industry"] == "Consumer Electronics"
    assert snap.loc["AAL", "industry"] == "Industrials"  # industry 空 → 回退 sector
    assert pd.isna(snap.loc["ZZZ", "industry"])  # 无板块文件 → 置空
    assert snap.loc["AAPL", "total_mv_yi"] == pytest.approx(5e12 / 1e8)
    assert pd.isna(snap.loc["AAL", "pe_ttm"])  # None → NaN，不造数
    assert snap.loc["AAL", "pb"] == pytest.approx(-1.9)
    assert snap.loc["AAPL", "avg_amount_yi"] == pytest.approx(10.0 * 1e-8)  # 元 → 亿


def test_输出目录按市场根(tmp_path, monkeypatch):
    root = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))
    monkeypatch.delenv("FACTOR_RESEARCH_OUT", raising=False)
    out = _load_bfpp()._out_dir("HK")
    assert out == root / "factor_research_private"
    assert out.is_dir()
