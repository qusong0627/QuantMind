"""基本面/长期走势劣化清单（fundamental_flags）：quantdb → 买入硬拦数据底座。

用户口径（2026-09-11）：
  「财务状况不好的、基于kuantdb、也需要、财务状况一直不好的、也需要排除、
   长期下跌趋势，不管牛市，熊市、都不好的」
  「长期横盘，股价没有啥变化的。也排除」
  「长期没有成交量的、需要排除吗？」→ 需要（卖出端执行风险）
  「对比最近几年的走势，是不是不好？然后不要误伤一些」

九类判据（全部基于 quantdb 本地数据，不依赖网络）：
  fin       连亏≥3年报 或 净资产为负 或 扣非连亏≥3年
  delist    财务类退市预警：最近一个完整年度营收低于板块线（主板 3 亿 /
            创业板科创板 1 亿 / 北交所 5000 万）且扣非前后孰低为负——
            比 fin 的"连亏 3 年"早两年预警（2024 退市新规口径）
  shell     保壳特征：最近 2 年扣非为负但净利润为正（靠补贴/卖资产撑账面）
  goodwill  商誉/净资产 > 50%（减值一夜亏光净资产）
  debt      资产负债率 > 85%（金融业豁免——银行保险天然 90%+）
  trend     长期下跌，**多窗口一致**（见下）
  flat      长期横盘：近 1 年涨跌≤10%、振幅≤20%，且近 2 年涨跌≤15%
  illiquid  流动性枯竭：近 60 日日均成交额 < 2000 万（买卖价差大、跌停时出不来）
  new       次新股：上市不足 60 个交易日（无历史规律、上市初无涨跌停限制）

「不要误伤」怎么保证：
  - 趋势判据要求**多窗口一致**：一只票只在近 1 年跌得多不算数，必须 1/2/3 年
    每个窗口都跑输中位数、且幅度够大，才是"不管牛熊都不好"。曾经的牛股回调
    （3 年仍大幅跑赢）会被正确放行。
  - 用**相对全市场中位收益**而非绝对值：绝对口径实测命中 58% 市场——那是大盘
    beta，不是个股差。
  - 收益用**后复权**价（未复权会把送转/分红当跌幅，实测误判 55 只）。
  - 财务只用已披露报表（m_anntime ≤ asof）——防未来函数。
  - 全是**每日重算的动态判据**（涨回来/成交恢复/次新变老 → 自动出列），
    不是永久拉黑。

本模块只负责**算 + 落盘**（data/fundamental_flags.json，每日 08:35 cron）；
消费在 risk_list（kind "weak"，粘性条目）。不让 risk_list 每次 refresh 现算：
全市场 5600 只的一轮扫描 ~15s，而 risk_list 每交易日要重建 6 次。

用法：
  python scripts/fundamental_flags.py build     # 重建缓存
  python scripts/fundamental_flags.py show      # 打印当前清单
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

QUANTDB = Path(os.environ.get("QUANTDB_DIR", "/home/zbox/projects/quantmind/data/quantdb"))
CONF = ROOT / "configs" / "live_symbols.json"
OUT = ROOT / "data" / "fundamental_flags.json"

BJ = timezone(timedelta(hours=8))

# 趋势判据用**后复权**日线：未复权价会把送转/分红当跌幅（10 送 10 → 价格腰斩，
# 假跌 50%），把正常公司误判成"长期下跌"。后复权序列的历史值不随新的除权变化，
# 是收益计算的标准口径。注意：**面值退市判据（risk_list.price_items）必须用未复权**，
# 它看的是实际成交价——两处口径不能混。
KLINE_REL = "1_kline_data/daily_backward"
# 后复权日线停更超过此天数 → 趋势/横盘判据停用（宁缺勿错：拿一个月前的价格判
# "当前是否跌破年线"，比不判更危险）。10 天覆盖春节/国庆长假。
KLINE_STALE_DAYS = 10

LOOKBACK_DAYS = 1400     # 日线回看自然日（≈950 交易日，够 3 年收益 + MA250）
MA_WINDOW = 250          # 年线
MA_SLOPE_LAG = 60        # 年线斜率回看（交易日）

# 阈值默认值（configs/live_symbols.json 的 risk 段可覆盖）
DEFAULTS = {
    "loss_years": 3,            # 净利润连亏年数
    "ded_loss_years": 3,        # 扣非净利润连亏年数
    "net_assets_min_yi": 0.0,   # 净资产低于此值（亿元）→ 资不抵债
    "shell_years": 2,           # 保壳特征回看年数
    "goodwill_ratio_max": 0.5,  # 商誉/净资产上限
    "debt_ratio_max": 0.85,     # 资产负债率上限（金融业豁免）
    "trend_rel250_max": -0.25,  # 急跌口径：近 1 年相对中位收益下限
    "trend_rel500_max": -0.35,  # 急跌口径：近 2 年
    "trend_slow_rel750_max": -0.40,  # 阴跌口径：近 3 年
    "trend_slow_rel500_max": -0.30,  # 阴跌口径：近 2 年
    "flat_ret1y_max": 0.10,     # 横盘：近 1 年涨跌幅绝对值上限
    "flat_amp1y_max": 0.20,     # 横盘：近 1 年区间振幅上限
    "flat_ret2y_max": 0.15,     # 横盘：近 2 年涨跌幅绝对值上限
    "flat_amp2y_max": 0.45,     # 横盘：近 2 年区间振幅上限
    "min_amount_yi": 0.2,       # 日均成交额下限（亿元；0 = 关闭）
    "amount_days": 60,          # 成交额窗口（交易日）
    "new_stock_days": 60,       # 次新股门槛（上市交易日数）
    # 财务类退市预警的营收线（亿元，按板块）。监管口径的营收还要"扣除与主业
    # 无关/不具商业实质的收入"，这里用全口径（不扣）→ 偏保守，只会少拦不会多拦。
    "delist_rev_main_yi": 3.0,  # 主板
    "delist_rev_gem_yi": 1.0,   # 创业板 / 科创板
    "delist_rev_bj_yi": 0.5,    # 北交所
}

# 金融业豁免负债率判据（银行/保险/证券的负债率天然 90%+，不是风险信号）
FIN_SECTOR_KEYS = ("银行", "保险", "证券", "多元金融")


def load_conf(path: Path | None = None) -> dict:
    """读 configs/live_symbols.json 的 risk 段；脏值逐项兜底到 DEFAULTS。"""
    cfg = dict(DEFAULTS)
    try:
        doc = json.loads((path or CONF).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    risk = doc.get("risk") if isinstance(doc, dict) else None
    if not isinstance(risk, dict):
        return cfg
    for k, default in DEFAULTS.items():
        if k in risk:
            try:
                cfg[k] = float(risk[k])
            except (TypeError, ValueError):
                pass
    return cfg


# ---------------------------------------------------------------- 纯函数（可测）

def to_annual(rows: list) -> list:
    """单季度报表 → 年度报表：quantdb 的 income **每一行都是单季度值**（1231 那行
    是 Q4 单季，不是全年——实测茅台 2024 四行相加 862.28 亿 = 真实全年归母净利）。

    直接拿 1231 行当"年度净利润"会把 Q4 单季亏损读成"全年亏损"：南航 2016-2019
    四年 Q4 都是单季亏损，被误判成"连续 10 年亏损"（实际那四年全年都是盈利的）。
    **只有 4 个季度齐全的年份才产出**（缺季不猜），各数值列独立求和
    （某一列缺季 → 该列该年 None，其余列不受影响）。

    rows: [(报告期, 列1, 列2, ...)] → [(f"{年}1231", 年列1, 年列2, ...)]，
    列数任意（income 传 净利润/扣非/营收 三列）。
    """
    by_year: dict = {}
    for row in rows:
        t = str(row[0])
        if len(t) != 8 or not t.isdigit():
            continue
        by_year.setdefault(t[:4], {})[t[4:]] = tuple(row[1:])
    out = []
    for year, qs in sorted(by_year.items()):
        vals = [qs.get(q) for q in ("0331", "0630", "0930", "1231")]
        if any(v is None for v in vals):
            continue  # 缺季（未上市/数据断档）→ 这年不出数
        cols = [_sum_or_none([v[i] for v in vals]) for i in range(len(vals[0]))]
        if all(v is None for v in cols):
            continue
        out.append((f"{year}1231", *cols))
    return out


def _sum_or_none(vals: list):
    return None if any(v is None for v in vals) else sum(vals)


def loss_streak(rows: list) -> int:
    """年报序列 → 最近连续亏损年数。rows: [(报告期 'YYYY1231'|int, 利润)]。

    只看年报（1231），倒序数到第一份盈利为止。缺值当断点（不猜）。
    """
    ann = [(str(t), v) for t, v in rows if str(t).endswith("1231")]
    ann.sort()
    n = 0
    for _, v in ann[::-1]:
        if v is not None and v < 0:
            n += 1
        else:
            break
    return n


def latest_value(rows: list):
    """[(报告期, 值)] → 报告期最新的一条的值（缺值往前找）。空/全缺 → None。"""
    for _, v in sorted(((str(t), v) for t, v in rows), reverse=True):
        if v is not None:
            return v
    return None


def latest_span(rows: list) -> str:
    """**最近这段**连续亏损的年报区间文本，如 '2023-2025'（只用于理由文案）。

    必须与 loss_streak 同步走：取全部亏损年份的首尾会写出「连续4年亏损
    （2016-2025）」这种自相矛盾的文案（2016 那次是十年前的旧账）。
    """
    ann = sorted((str(t), v) for t, v in rows if str(t).endswith("1231"))
    run: list = []
    for t, v in ann[::-1]:
        if v is not None and v < 0:
            run.append(t[:4])
        else:
            break
    if not run:
        return ""
    return f"{run[-1]}-{run[0]}" if len(run) >= 2 else run[0]


def shell_signal(rows: list, years: int = 2) -> bool:
    """保壳特征：最近 years 年**扣非为负但净利润为正**（靠非经常性损益撑账面）。

    rows: [(报告期, 净利润, 扣非净利润, ...)]。这是最经典的爆雷前兆——主业已经
    不赚钱，靠政府补贴/卖房/卖股权把报表做成微利，一旦补贴断档就是大额亏损。
    """
    ann = sorted((str(r[0]), r[1], r[2]) for r in rows if str(r[0]).endswith("1231"))
    ann = [r for r in ann if r[1] is not None and r[2] is not None]
    if len(ann) < years:
        return False
    return all(np_ > 0 and ded < 0 for _, np_, ded in ann[-years:])


def trend_stats(closes: list, highs: list | None = None,
                lows: list | None = None) -> dict | None:
    """后复权日线（升序）→ 趋势统计；不够长 → None。

    返回 close/ma250/ma250_prev/ret250/ret500/ret750/amp250/amp500/n。
    收益类字段在数据不足时为 None（调用方按 None 跳过该口径）。
    """
    n = len(closes)
    if n < MA_WINDOW + MA_SLOPE_LAG:
        return None
    cur = closes[-1]
    st = {
        "n": n, "close": cur,
        "ma250": sum(closes[-MA_WINDOW:]) / MA_WINDOW,
        "ma250_prev": sum(closes[-MA_WINDOW - MA_SLOPE_LAG:-MA_SLOPE_LAG]) / MA_WINDOW,
        "ret250": cur / closes[-MA_WINDOW] - 1,
        "ret500": cur / closes[-500] - 1 if n >= 500 else None,
        "ret750": cur / closes[-750] - 1 if n >= 750 else None,
        "amp250": None, "amp500": None,
    }
    if highs and lows and len(highs) == n and len(lows) == n:
        st["amp250"] = (max(highs[-MA_WINDOW:]) - min(lows[-MA_WINDOW:])) / cur
        if n >= 500:
            st["amp500"] = (max(highs[-500:]) - min(lows[-500:])) / cur
    return st


def liquidity_flag(stats: dict, conf: dict) -> str:
    """流动性枯竭 → 理由文本；充足 → ""。成交额单位：amount 列为万元。"""
    floor_yi = _num(conf, "min_amount_yi")
    days = int(_num(conf, "amount_days"))
    amt = stats.get("amt")
    if floor_yi <= 0 or amt is None or stats.get("n", 0) < days:
        return ""
    if amt >= floor_yi * 1e4:
        return ""
    return f"流动性枯竭：近{days}日日均成交额{amt / 1e4:.2f}亿（低于{floor_yi:.1f}亿）"


def new_stock_flag(n_days: int, conf: dict) -> str:
    """次新股 → 理由文本。n_days = 该票日线交易日数（数据不足 310 日也算不出来，
    这里直接拿原始长度判）。"""
    limit = int(_num(conf, "new_stock_days"))
    if limit <= 0 or n_days >= limit:
        return ""
    return f"次新股：上市仅{n_days}个交易日（无历史规律，上市初无涨跌停限制）"


def trend_flags(stats: dict, conf: dict) -> dict:
    """{symbol: trend_stats} → {code6: 理由}（相对全市场中位，只含命中）。

    两个口径（都要求现价在年线下方）：
      急跌：1 年跑输 > 25% 且 2 年跑输 > 35%；
      阴跌：3 年跑输 > 40% 且 2 年跑输 > 30% 且 1 年也跑输。
    阴跌口径是「对比最近几年走势」的落地：必须**每个窗口都跑输**，
    单窗口跌得多不算——避免把"牛股回调"当"长期差"误伤。
    """
    out: dict = {}
    rows = {s: st for s, st in (stats or {}).items()
            if st and st.get("ret250") is not None and st.get("ret500") is not None}
    if not rows:
        return out
    med250 = _median([st["ret250"] for st in rows.values()])
    med500 = _median([st["ret500"] for st in rows.values()])
    r750 = [st for st in rows.values() if st.get("ret750") is not None]
    med750 = _median([st["ret750"] for st in r750]) if r750 else None
    f250, f500 = _num(conf, "trend_rel250_max"), _num(conf, "trend_rel500_max")
    s750, s500 = _num(conf, "trend_slow_rel750_max"), _num(conf, "trend_slow_rel500_max")
    for sym, st in rows.items():
        code = str(sym).split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        if st["close"] >= st["ma250"]:
            continue  # 已站上年线的（正在修复）不动
        rel250 = st["ret250"] - med250
        rel500 = st["ret500"] - med500
        if rel250 < f250 and rel500 < f500:
            out[code] = (f"长期下跌：近1年跑输大盘{abs(rel250):.0%}、"
                         f"近2年跑输{abs(rel500):.0%}，且现价位于年线下方")
            continue
        if st.get("ret750") is None or med750 is None:
            continue
        rel750 = st["ret750"] - med750
        if rel750 < s750 and rel500 < s500 and rel250 < 0:
            out[code] = (f"多年阴跌：近3年跑输大盘{abs(rel750):.0%}、"
                         f"近2年跑输{abs(rel500):.0%}、近1年仍跑输，现价在年线下方")
    return out


def flat_flags(stats: dict, conf: dict) -> dict:
    """长期横盘 → {code6: 理由}。两个窗口都"没动"才算（一年横盘可能是蓄势）。"""
    out: dict = {}
    r1, a1 = _num(conf, "flat_ret1y_max"), _num(conf, "flat_amp1y_max")
    r2, a2 = _num(conf, "flat_ret2y_max"), _num(conf, "flat_amp2y_max")
    for sym, st in (stats or {}).items():
        code = str(sym).split(".")[0]
        if len(code) != 6 or not code.isdigit() or not st:
            continue
        if st.get("ret250") is None or st.get("ret500") is None:
            continue
        if st.get("amp250") is None or st.get("amp500") is None:
            continue
        if (abs(st["ret250"]) <= r1 and st["amp250"] <= a1
                and abs(st["ret500"]) <= r2 and st["amp500"] <= a2):
            out[code] = (f"长期横盘：近2年涨跌{st['ret500']:+.1%}、近1年{st['ret250']:+.1%}，"
                         f"区间振幅仅{st['amp250']:.0%}（没有交易价值）")
    return out


def fin_flags(fin: dict, conf: dict) -> dict:
    """财务劣化 → {code6: 理由}。

    fin: {symbol: {"loss_years","span","ded_years","net_assets","goodwill",
                   "tot_assets","tot_liab","is_fin"}}
    判据：连亏≥loss_years 或 扣非连亏≥ded_loss_years 或 净资产低于阈值
         或 商誉/净资产 > 上限 或 资产负债率 > 上限（金融业豁免）。
    """
    out: dict = {}
    years = int(_num(conf, "loss_years"))
    ded_years = int(_num(conf, "ded_loss_years"))
    eq_min = _num(conf, "net_assets_min_yi") * 1e8
    gw_max = _num(conf, "goodwill_ratio_max")
    debt_max = _num(conf, "debt_ratio_max")
    for sym, f in (fin or {}).items():
        code = str(sym).split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        parts = []
        if f.get("loss_years", 0) >= years:
            span = f"（{f['span']}）" if f.get("span") else ""
            parts.append(f"连续{f['loss_years']}年亏损{span}")
        if f.get("ded_years", 0) >= ded_years and f.get("loss_years", 0) < years:
            parts.append(f"扣非连续{f['ded_years']}年亏损（主业不赚钱）")
        eq = f.get("net_assets")
        if eq is not None and eq < eq_min:
            parts.append(f"净资产{eq / 1e8:.1f}亿（资不抵债）")
        gw = f.get("goodwill")
        if (gw is not None and eq is not None and eq > 0 and gw / eq > gw_max):
            parts.append(f"商誉{gw / 1e8:.1f}亿占净资产{gw / eq:.0%}（减值风险）")
        ta, tl = f.get("tot_assets"), f.get("tot_liab")
        if (not f.get("is_fin") and ta and tl is not None and ta > 0
                and tl / ta > debt_max):
            parts.append(f"资产负债率{tl / ta:.0%}（高杠杆）")
        if parts:
            out[code] = "；".join(parts)
    return out


def shell_flags(fin: dict, conf: dict) -> dict:
    """保壳特征 → {code6: 理由}。"""
    out: dict = {}
    for sym, f in (fin or {}).items():
        code = str(sym).split(".")[0]
        if len(code) == 6 and code.isdigit() and f.get("is_shell"):
            out[code] = f"保壳特征：扣非连亏{f.get('shell_years', 2)}年，靠非经常性损益维持账面盈利"
    return out


def delist_rev_floor(sym: str, conf: dict) -> float:
    """该票适用的"财务类退市"营收阈值（元）。板块按代码前缀/后缀判。"""
    s = str(sym)
    code = s.split(".")[0]
    if code[:3] in ("300", "301", "688", "689"):
        return _num(conf, "delist_rev_gem_yi") * 1e8
    if s.endswith(".BJ") or code[:2] in ("43", "83", "87", "88", "92"):
        return _num(conf, "delist_rev_bj_yi") * 1e8
    return _num(conf, "delist_rev_main_yi") * 1e8


def delist_flags(fin: dict, conf: dict) -> dict:
    """财务类退市预警 → {code6: 理由}（2024 退市新规口径）。

    最近一个**完整**会计年度：营收低于板块线 且 净利润（扣非前后孰低）为负
    → 明年年报再不改善就是 *ST。比 fin 的"连亏 3 年"早两年预警，实测全市场
    39 只踩线、其中 34 只已被其他判据拦下，增量 4 只是"只亏一年"的早期信号。

    fin: {symbol: {"fy_year","fy_rev","fy_np","fy_ded", ...}}。数据缺任一项
    或营收为 None → 不判（不在数据不全时扣帽子）。
    """
    out: dict = {}
    for sym, f in (fin or {}).items():
        code = str(sym).split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        rev, np_a, ded_a = f.get("fy_rev"), f.get("fy_np"), f.get("fy_ded")
        cands = [v for v in (np_a, ded_a) if v is not None]
        floor = delist_rev_floor(sym, conf)
        if rev is None or not cands or floor <= 0:
            continue
        worse = min(cands)
        if rev < floor and worse < 0:
            out[code] = (f"退市风险：{f.get('fy_year')}年营收{rev / 1e8:.2f}亿"
                         f"（低于{floor / 1e8:g}亿线）且扣非前后孰低为负"
                         f"（{worse / 1e8:.2f}亿）")
    return out


def _num(conf: dict, key: str) -> float:
    return float(conf.get(key, DEFAULTS[key]))


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _code_map(by_sym: dict, fn) -> dict:
    """{symbol: 值} 逐票套判据函数 → {code6: 理由}（空理由/坏代码自动跳过）。"""
    out = {}
    for sym, val in (by_sym or {}).items():
        code = str(sym).split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        reason = fn(val)
        if reason:
            out[code] = reason
    return out


# ---------------------------------------------------------------- 数据读取（quantdb）

def _con():
    import duckdb

    c = duckdb.connect()
    c.execute("PRAGMA threads=4")
    return c


def latest_kline_dt(asof: date, root: Path | None = None) -> date | None:
    """日线里 dt ≤ asof 的最新一天；无分区 → None。"""
    base = Path(root or QUANTDB) / KLINE_REL
    ds = [p.name[3:] for p in base.glob("dt=*")
          if p.is_dir() and p.name[3:].isdigit() and p.name[3:] <= asof.strftime("%Y%m%d")]
    return datetime.strptime(max(ds), "%Y%m%d").date() if ds else None


def read_daily(asof: date) -> dict:
    """{symbol: {"close"/"high"/"low"/"amount": [升序]}}（后复权价 + 原始成交额万元）。

    dt ≤ asof 的 LOOKBACK_DAYS 自然日。日线停更超过 KLINE_STALE_DAYS → {}（fail-open：
    不给陈旧数据背书）。amount 单位万元（quantdb 口径）。
    """
    dmax = latest_kline_dt(asof)
    if dmax is None or dmax < asof - timedelta(days=KLINE_STALE_DAYS):
        print(f"⚠️ 后复权日线停更（最新 {dmax}）→ 本轮跳过趋势/横盘判据", file=sys.stderr)
        return {}
    lo = (asof - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    try:
        df = _con().execute(
            f"SELECT symbol, dt, high, low, close, amount FROM read_parquet("
            f"'{QUANTDB}/{KLINE_REL}/dt=*/data.parquet') "
            f"WHERE dt >= {lo} AND dt <= {asof:%Y%m%d} ORDER BY symbol, dt").df()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 读日线失败：{str(exc)[:120]}", file=sys.stderr)
        return {}
    out = {}
    for s, g in df.groupby("symbol"):
        out[s] = {"close": g["close"].astype(float).tolist(),
                  "high": g["high"].astype(float).tolist(),
                  "low": g["low"].astype(float).tolist(),
                  "amount": g["amount"].astype(float).tolist()}
    return out


def read_income(asof: date) -> dict:
    """{symbol: [(年报期, 年度归母净利润, 年度扣非净利润, 年度营收)]}，只用
    m_anntime ≤ asof。

    两道口径闸门：
      1. **每个报告期只留最新版本**（同报告期多条 = 财报重述）。当前数据里只有
         3 个报告期有重述，不加这道闸的代价现在还看不见；但一旦重述变多，
         `loss_streak` 会按**行数**而不是年数计数，把同一个亏损年度数两遍。
      2. **单季度 → 年度合计**（quantdb 存的是单季值，见 `to_annual`）。漏了这道，
         Q4 单季亏损会被读成全年亏损。
      选版本用公告日决定，这是 PIT 口径的正确做法。
    """
    try:
        df = _con().execute(
            f"SELECT Symbol, m_timetag, net_profit_excl_min_int_inc AS np, "
            f"deducted_net_profit AS ded, revenue FROM ("
            f"  SELECT *, row_number() OVER (PARTITION BY Symbol, m_timetag "
            f"    ORDER BY m_anntime DESC) AS rn "
            f"  FROM read_parquet('{QUANTDB}/3_financial_data/income/*.parquet', "
            f"union_by_name=True) WHERE m_anntime <= '{asof:%Y%m%d}') WHERE rn = 1").df()
    except Exception as exc:  # noqa: BLE001 数据缺失不阻塞（fail-open）
        print(f"⚠️ 读 income 失败：{str(exc)[:120]}", file=sys.stderr)
        return {}
    return {s: to_annual(list(zip(g["m_timetag"], g["np"], g["ded"], g["revenue"])))
            for s, g in df.groupby("Symbol")}


def read_balance(asof: date) -> dict:
    """{symbol: [(报告期, 归母净资产, 商誉, 总资产, 总负债)]}，口径同 read_income
    （含按公告日选版本的重述去重）。"""
    try:
        df = _con().execute(
            f"SELECT Symbol, m_timetag, tot_shrhldr_eqy_excl_min_int AS eq, "
            f"goodwill AS gw, tot_assets AS ta, tot_liab AS tl FROM ("
            f"  SELECT *, row_number() OVER (PARTITION BY Symbol, m_timetag "
            f"    ORDER BY m_anntime DESC) AS rn "
            f"  FROM read_parquet('{QUANTDB}/3_financial_data/balance/*.parquet', "
            f"union_by_name=True) WHERE m_anntime <= '{asof:%Y%m%d}') WHERE rn = 1").df()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 读 balance 失败：{str(exc)[:120]}", file=sys.stderr)
        return {}
    return {s: list(zip(g["m_timetag"], g["eq"], g["gw"], g["ta"], g["tl"]))
            for s, g in df.groupby("Symbol")}


def read_fin_sectors() -> set:
    """金融业代码（银行/保险/证券/多元金融）→ 负债率判据豁免。失败 → 空集。

    quantdb 的申万行业分类里金融在**二级**（'行业板块(二级)'：全国性银行/地方性
    银行/保险/证券/多元金融）；一级是产业分类（食品/钢铁/化工…），没有金融。
    """
    try:
        import duckdb

        p = QUANTDB / "2_base_sector" / "sector_concept" / "sector_members.parquet"
        df = duckdb.connect().execute(
            f"SELECT DISTINCT Symbol, SectorName FROM read_parquet('{p}') "
            f"WHERE SectorType = '行业板块(二级)'").df()
        return {s for s, n in zip(df.Symbol, df.SectorName)
                if any(k in str(n) for k in FIN_SECTOR_KEYS)}
    except Exception:  # noqa: BLE001 行业数据缺失 → 不豁免（宁可多拦，不误放）
        return set()


# ---------------------------------------------------------------- 组装 / 落盘

def _fin_snapshot(inc: dict, bal: dict, conf: dict, fins: set) -> dict:
    """逐票财务画像（供 fin_flags / shell_flags / delist_flags 消费）。"""
    snap = {}
    for sym in set(inc) | set(bal):
        rows = inc.get(sym, [])          # [(年报期, 归母, 扣非, 营收)]（to_annual 升序）
        np_rows = [(r[0], r[1] if len(r) > 1 else None) for r in rows]
        ded_rows = [(r[0], r[2] if len(r) > 2 else None) for r in rows]
        fy = rows[-1] if rows else ()     # 最近一个完整年度
        b = list(bal.get(sym, []))   # [(报告期, 净资产, 商誉, 总资产, 总负债)]
        eq = latest_value([(t, v) for t, v, _, _, _ in b])
        gw = latest_value([(t, v) for t, _, v, _, _ in b])
        ta = latest_value([(t, v) for t, _, _, v, _ in b])
        tl = latest_value([(t, v) for t, _, _, _, v in b])
        snap[sym] = {
            "loss_years": loss_streak(np_rows), "span": latest_span(np_rows),
            "ded_years": loss_streak(ded_rows), "net_assets": eq,
            "goodwill": gw, "tot_assets": ta, "tot_liab": tl,
            "is_fin": sym in fins,
            "is_shell": shell_signal(rows, int(_num(conf, "shell_years"))),
            "shell_years": int(_num(conf, "shell_years")),
            "fy_year": str(fy[0])[:4] if fy else None,
            "fy_np": fy[1] if len(fy) > 1 else None,
            "fy_ded": fy[2] if len(fy) > 2 else None,
            "fy_rev": fy[3] if len(fy) > 3 else None,
        }
    return snap


def build(asof: date | None = None, conf: dict | None = None, out: Path | None = None,
          income: dict | None = None, balance: dict | None = None,
          daily: dict | None = None, fins: set | None = None) -> dict:
    """扫全市场 → 落盘 data/fundamental_flags.json。显式传数据即不读真实源（测试用）。"""
    asof = asof or datetime.now(BJ).date()
    conf = conf if conf is not None else load_conf()
    inc = read_income(asof) if income is None else income
    bal = read_balance(asof) if balance is None else balance
    day = read_daily(asof) if daily is None else daily
    fin_sec = read_fin_sectors() if fins is None else fins

    stats, thin = {}, {}
    amt_days = int(_num(conf, "amount_days"))
    for sym, d in day.items():
        st = trend_stats(d["close"], d.get("high"), d.get("low"))
        if not st:
            thin[sym] = len(d.get("close") or [])
            continue
        amt = d.get("amount") or []
        if len(amt) >= amt_days:
            st["amt"] = sum(amt[-amt_days:]) / amt_days
        stats[sym] = st

    snap = _fin_snapshot(inc, bal, conf, fin_sec)
    items: dict = {}
    sources = [
        ("fin", fin_flags(snap, conf)),
        ("delist", delist_flags(snap, conf)),
        ("shell", shell_flags(snap, conf)),
        ("trend", trend_flags(stats, conf)),
        ("flat", flat_flags(stats, conf)),
        ("illiquid", _code_map(stats, lambda st: liquidity_flag(st, conf))),
        ("new", _code_map(thin, lambda n: new_stock_flag(n, conf))),
    ]
    for tag, flags_map in sources:
        for code, reason in (flags_map or {}).items():
            it = items.setdefault(code, {"flags": [], "reasons": [], "asof": asof.isoformat()})
            it["flags"].append(tag)
            it["reasons"].append(reason)
    for it in items.values():
        it["reason"] = "；".join(it.pop("reasons"))

    doc = {"asof": asof.isoformat(), "generated_at": datetime.now(BJ).isoformat(),
           "items": items}
    p = out or OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    counts = {tag: sum(1 for v in items.values() if tag in v["flags"]) for tag, _ in sources}
    return {**counts, "total": len(items), "scanned": len(day)}


def main() -> int:
    ap = argparse.ArgumentParser(description="基本面/长期走势劣化清单（quantdb）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="重建 data/fundamental_flags.json")
    sub.add_parser("show", help="打印当前清单")
    a = ap.parse_args()

    if a.cmd == "build":
        st = build()
        print("✓ " + " / ".join(f"{k} {v}" for k, v in st.items() if k not in ("total", "scanned"))
              + f" | 合计 {st['total']}（扫 {st['scanned']} 只）→ {OUT}")
        return 0
    try:
        doc = json.loads(OUT.read_text(encoding="utf-8"))
        items = doc.get("items") or {}
    except (OSError, ValueError):
        print("（无缓存文件）")
        return 0
    print(f"asof={doc.get('asof')} 共 {len(items)} 只")
    for code, it in sorted(items.items()):
        print(f"  {code} [{'/'.join(it.get('flags', []))}] {it.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
