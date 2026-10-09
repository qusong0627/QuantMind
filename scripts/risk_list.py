#!/usr/bin/env python3
"""事件风险清单：解禁 / 负面新闻 → data/risk_block.json（买入硬拦 + 持仓告警）。

为什么需要（2026-09-08 用户口径）：「A股很多有风险的股票比如解禁这种就是要告警跑的、
负面新闻股票也要避坑掉。模型有的话需要毙掉」——此前解禁/质押只作为提示词标签
（`event_radar.tag_events` → 夜池标注 / 复盘提示），模型照买不误，没有任何硬拦。

数据源与口径：
  - 解禁：data/events/unlock_*.parquet（event_radar 每交易日 18:00 采集）。
    解禁日往前 `unlock_days` 个自然日内、且解禁量占流通市值 ≥ `unlock_ratio_min`%
    → 拦买。解禁日一过风险即兑现，条目自然失效（expire = 解禁日 + 1）。
  - 负面新闻：data/news_brief/history.jsonl 的 micro 事件（sentiment ≤ 阈值）。
    回溯 `news_days` 天，expire = 事件日 + news_days。涉及标的数 > `news_max_tickers`
    的事件按板块级处理，不拦（否则一条板块负面把整个行业禁掉）。
  - 重大违规（grave，2026-09-11）：立案调查/财务造假/虚增/行政处罚/退市风险等
    关键词命中 → **禁买 `grave_days` 天**（60），不看 sentiment。用户口径
    「垃圾股、财务造假……都黑名单」；立案调查是存续状态，窗口比新闻长得多。
  - 监管关注（watch，2026-09-11）：问询函/监管函/警示函等 → **只提醒不禁买**，
    落 `watch` 段进提示词当因子自评。口径「监管的可以提醒，里面有因子」。
  - 低价/面值退市（penny，2026-09-11）：未复权收盘价 < `min_price`（默认 2.0 元）
    → 禁买。口径「还有一些垃圾股……都黑名单」。A股最硬的垃圾股判据就是面值退市
    （连续 20 个交易日收盘 < 1 元即终止上市），这里设的是**预警带**而非等到 1 元。
    数据源 quantdb 日线分区（未复权；复权价会把面值判据算错），每夜落盘，
    数据停更超过 `penny_days` 自然失效（fail-open）。
  - 基本面/长期走势劣化（weak，2026-09-11）：读 fundamental_flags.py 每日算好的
    缓存 `data/fundamental_flags.json` → 禁买。九类判据：连亏≥3年 / 扣非连亏 /
    净资产为负 / 财务类退市预警（营收低于板块线且扣非孰低为负，比连亏 3 年早两年）/
    保壳特征 / 高商誉 / 高负债（金融豁免）/ 长期下跌（多窗口相对
    全市场跑输且破年线）/ 长期横盘 / 流动性枯竭 / 次新股。用户口径「财务状况
    一直不好的也需要排除、长期下跌趋势不管牛市熊市都不好的、长期横盘也排除、
    不要误伤」。**不在本模块现算**：全市场 5600 只的一轮扫描 ~15s，而本清单
    每交易日要重建 6 次。缓存超过 `flag_days` 自然日即失效。
  - 质押：**只告警不拦买**（慢性状态而非事件；比例高不等于当期风险）。

设计取舍：
  - fail-open：数据缺失/损坏 → 空清单。宁可漏拦一只，也不能因数据故障停掉全天买入
    （同 symbol_policy / live_breaker）。
  - 只拦买入：卖出永远放行（否则被套的仓位出不来）。
  - 产物是**文件**而非内存态：买入闸门在多个进程（整点轮/开盘轮/告警）里跑，
    落盘 + mtime 无关的纯读取是最简单的跨进程共享方式。

用法：
  python scripts/risk_list.py refresh          # 重建 data/risk_block.json
  python scripts/risk_list.py show             # 打印当前清单
  python scripts/risk_list.py check 688795.SH  # 查单只
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

CONF = ROOT / "configs" / "live_symbols.json"
OUT = ROOT / "data" / "risk_block.json"
NEWS_HISTORY = ROOT / "data" / "news_brief" / "history.jsonl"
# 全市场未复权日线（quantdb 分区 dt=YYYYMMDD/data.parquet，每夜 ~01:55 落盘）。
# 用**未复权**口径：面值退市看的是实际成交价，复权价会把判据算错。
KLINE_DIR = Path(os.environ.get(
    "QUANTDB_KLINE_DIR",
    "/home/zbox/projects/quantmind/data/quantdb/1_kline_data/daily_unadjusted"))
# 基本面/长期趋势劣化缓存（scripts/fundamental_flags.py 每日 build，cron 08:35）。
FUND_FLAGS = ROOT / "data" / "fundamental_flags.json"

BJ = timezone(timedelta(hours=8))

DEFAULTS = {
    "enabled": True,
    "unlock_days": 10,          # 解禁前 N 自然日内禁买
    "unlock_after_days": 5,     # 解禁后 N 自然日**仍**禁买（抛压兑现期）
    "unlock_ratio_min": 1.0,    # 解禁占流通市值比例阈值（%）
    "news_days": 3,             # 负面新闻回溯窗口（自然日）
    "news_sentiment_max": -0.5,  # 情绪 ≤ 该值算负面
    "news_max_tickers": 3,      # 事件涉及标的数上限（超过视为板块级，不拦）
    "pledge_warn_ratio": 50.0,  # 质押比例告警阈值（%，只告警）
    "grave_days": 60,           # 重大违规（立案/造假/处罚）存续窗口（自然日）
    "regulatory_days": 30,      # 监管关注（问询/警示）提醒窗口（自然日，不禁买）
    "min_price": 2.0,           # 收盘价低于此值禁买（元；0 = 关闭）
    "penny_days": 5,            # 低价条目的数据新鲜度窗口（自然日）
    # 财务/趋势判据的阈值不在这里——它们在 scripts/fundamental_flags.py 现算，
    # 键名同存于 configs/live_symbols.json 的 risk 段（单一事实来源，避免两处漂移）。
    "flag_days": 7,             # 基本面缓存的过期窗口（自然日）
    # 行业风险榜（scripts/industry_risk.py → data/industry_risk.json，**只提示不禁买**）：
    # 剔除率 = 该行业被长期排除清单剔除数 / 该行业股票总数，行业名取 quantdb rs_hyname。
    "industry_warn_rate_min": 0.45,   # 上榜剔除率下限
    "industry_warn_total_min": 10,    # 行业股票数下限（小样本不上榜）
    "industry_warn_top_n": 15,        # 榜长上限（提示词 token 预算）
}

# 存续状态类风险：与短事件合并时取**更晚**失效日，不被提前解除。
STICKY_KINDS = {"grave", "penny", "weak"}

# 重大违规关键词：命中即按 grave_days 长窗口拉黑（不依赖 sentiment）→ **禁买**。
# 口径（2026-09-11 用户）：「垃圾股、财务造假……这些股票，都黑名单」。
# 只收硬信号："警示函/问询函"是关注级，归下面的 REGULATORY 提醒层，不进这里。
GRAVE_KEYWORDS = ("立案", "造假", "虚增", "违法违规", "行政处罚",
                  "退市风险", "信息披露违法")

# 监管关注关键词 → **只提醒不禁买**（watch 段）。用户口径（2026-09-11）：
# 「监管的可以提醒，里面有因子，不去拿时[再]黑名单」——问询/警示这类事件
# 的信息含量（因子）大于即期风险，一刀切禁买会误伤；进提示词让模型自评。
REGULATORY_KEYWORDS = ("问询", "监管函", "警示", "关注函", "监管关注")


# ---------------------------------------------------------------- 配置

def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_conf(path: Path | None = None) -> dict:
    """读 configs/live_symbols.json 的 risk 段；缺失/脏值 → 默认值逐项兜底。"""
    cfg = dict(DEFAULTS)
    try:
        doc = json.loads((path or CONF).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    risk = doc.get("risk") if isinstance(doc, dict) else None
    if not isinstance(risk, dict):
        return cfg
    for k, default in DEFAULTS.items():
        if k not in risk:
            continue
        cfg[k] = bool(risk[k]) if isinstance(default, bool) else _num(risk[k], default)
    return cfg


# ---------------------------------------------------------------- 解析

def _code6(v) -> str:
    """归一成 6 位代码（数据源后缀口径不统一，风险表按前缀匹配）。"""
    c = str(v or "").strip().split(".")[0]
    return c if len(c) == 6 and c.isdigit() else ""


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _as_dt(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=BJ) if d.tzinfo is None else d


def _ymd(v) -> date | None:
    """20260908 / "20260908" / "2026-09-08" 都归一成 date；其余 None。"""
    s = str(v or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return _as_date(v)


def unlock_items(rows: list, asof: date, conf: dict) -> dict:
    """解禁明细行 → {code6: {reason, kind, expire}}。脏行跳过，绝不抛异常。

    窗口是**解禁日前后双向**的（2026-09-11 用户口径「还有个解禁的也是超级利空」）：
      - 前 unlock_days 天：市场抢跑，解禁盘还没出来价格就先跌；
      - 后 unlock_after_days 天：解禁盘**真的可卖了**，抛压在这几天兑现。
    只拦前不拦后是方向反了——封锁恰好在利空落地那刻解除。上限 unlock_after_days
    天是为了不把已消化完的票永久拉黑。
    """
    out: dict = {}
    horizon = int(_num(conf.get("unlock_days"), DEFAULTS["unlock_days"]))
    after = int(_num(conf.get("unlock_after_days"), DEFAULTS["unlock_after_days"]))
    ratio_min = _num(conf.get("unlock_ratio_min"), DEFAULTS["unlock_ratio_min"])
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = _code6(r.get("股票代码") or r.get("证券代码"))
        dday = _as_date(r.get("解禁时间"))
        if (not code or dday is None
                or not (asof - timedelta(days=after) <= dday
                        <= asof + timedelta(days=horizon))):
            continue
        ratio = _num(r.get("占解禁前流通市值比例"), 0.0) * 100  # 源是小数
        if ratio < ratio_min:
            continue
        kind = str(r.get("限售股类型") or "—")
        when = f"{dday:%m/%d}解禁" if dday >= asof else f"{dday:%m/%d}已解禁"
        out[code] = {
            "reason": f"{when}{ratio:.1f}%流通盘（{kind}）",
            "kind": "unlock",
            "expire": (dday + timedelta(days=after)).isoformat(),
        }
    return out


def news_items(lines: list, asof: date, conf: dict) -> dict:
    """新闻 micro 事件 → {code6: {reason, kind, expire}}。只取负面且个股级的事件。"""
    out: dict = {}
    days = int(_num(conf.get("news_days"), DEFAULTS["news_days"]))
    sent_max = _num(conf.get("news_sentiment_max"), DEFAULTS["news_sentiment_max"])
    max_tk = int(_num(conf.get("news_max_tickers"), DEFAULTS["news_max_tickers"]))
    floor = asof - timedelta(days=days)
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        ts = _as_dt(line.get("ts"))
        if ts is None or ts.date() < floor:
            continue
        micro = (line.get("segments") or {}).get("news-micro") or {}
        for e in micro.get("events") or []:
            if not isinstance(e, dict):
                continue
            sent = e.get("sentiment")
            if not isinstance(sent, (int, float)) or sent > sent_max:
                continue
            tickers = [t for t in (e.get("tickers") or []) if _code6(t)]
            if not tickers or len(tickers) > max_tk:
                continue  # 板块级事件：不按个股负面拦
            reason = (f"{e.get('event_type') or '负面新闻'}：{e.get('name') or ''}"
                      f"（{str(e.get('note') or '')[:60]}）")
            expire = (ts.date() + timedelta(days=days)).isoformat()
            for t in tickers:
                out[_code6(t)] = {"reason": reason, "kind": "news", "expire": expire}
    return out


def grave_items(lines: list, asof: date, conf: dict) -> dict:
    """重大违规事件（立案调查/财务造假/行政处罚…）→ {code6: {reason, kind, expire}}。

    与 news_items 的差别是**窗口**与**判据**：负面新闻按事件处理（news_days 天，
    热度过去就该解除），而立案调查/财务造假是**存续状态**——调查可持续数月，
    期间退市风险一直在（2026-09-11 用户口径「财务造假……都黑名单」）。判据改为
    关键词命中，不看 sentiment：AI 给情绪值可能是 0 或缺省，而"立案"两个字本身
    就是信号（实物：2026-09-10「遭证监会立案」「信披涉嫌重大遗漏」）。
    涉多标的的事件仍按板块级跳过（同 news_max_tickers 口径，不整行业封杀）。
    """
    out: dict = {}
    days = int(_num(conf.get("grave_days"), DEFAULTS["grave_days"]))
    max_tk = int(_num(conf.get("news_max_tickers"), DEFAULTS["news_max_tickers"]))
    floor = asof - timedelta(days=days)
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        ts = _as_dt(line.get("ts"))
        if ts is None or ts.date() < floor:
            continue
        micro = (line.get("segments") or {}).get("news-micro") or {}
        for e in micro.get("events") or []:
            if not isinstance(e, dict):
                continue
            text = " ".join(str(e.get(k) or "") for k in ("event_type", "name", "note"))
            kw = next((k for k in GRAVE_KEYWORDS if k in text), "")
            if not kw:
                continue
            tickers = [t for t in (e.get("tickers") or []) if _code6(t)]
            if not tickers or len(tickers) > max_tk:
                continue
            reason = (f"重大违规（{kw}）：{e.get('name') or ''}"
                      f"（{str(e.get('note') or '')[:60]}）")
            expire = (ts.date() + timedelta(days=days)).isoformat()
            for t in tickers:
                out[_code6(t)] = {"reason": reason, "kind": "grave", "expire": expire}
    return out


def regulatory_watch(lines: list, asof: date, conf: dict) -> dict:
    """监管关注事件（问询函/监管函/警示函）→ {code6: {reason, kind, until}}。

    **只提醒不禁买**（与 items 的硬拦分开）：用户口径（2026-09-11）「监管的可以
    提醒，里面有因子，不去拿时[再]黑名单」——关注级事件进提示词当因子自评，
    不进买入闸门。窗口 regulatory_days 天（关注事项会持续一段时间）。
    判据同 grave：关键词命中即可，不看 sentiment；板块级（涉多标的）跳过。
    """
    out: dict = {}
    days = int(_num(conf.get("regulatory_days"), DEFAULTS["regulatory_days"]))
    max_tk = int(_num(conf.get("news_max_tickers"), DEFAULTS["news_max_tickers"]))
    floor = asof - timedelta(days=days)
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        ts = _as_dt(line.get("ts"))
        if ts is None or ts.date() < floor:
            continue
        micro = (line.get("segments") or {}).get("news-micro") or {}
        for e in micro.get("events") or []:
            if not isinstance(e, dict):
                continue
            text = " ".join(str(e.get(k) or "") for k in ("event_type", "name", "note"))
            kw = next((k for k in REGULATORY_KEYWORDS if k in text), "")
            if not kw:
                continue
            tickers = [t for t in (e.get("tickers") or []) if _code6(t)]
            if not tickers or len(tickers) > max_tk:
                continue
            reason = (f"监管关注（{kw}）：{e.get('name') or ''}"
                      f"（{str(e.get('note') or '')[:60]}）")
            until = (ts.date() + timedelta(days=days)).isoformat()
            for t in tickers:
                out[_code6(t)] = {"reason": reason, "kind": "regulatory", "until": until}
    return out


def price_items(rows: list, asof: date, conf: dict) -> dict:
    """低价股（面值退市预警带）→ {code6: {reason, kind, expire}}。

    用户口径（2026-09-11）「还有一些垃圾股……都黑名单」。A股最硬的垃圾股判据是
    **面值退市**：连续 20 个交易日收盘价 < 1 元即终止上市。这里不等跌到 1 元才拦，
    而是设预警带 `min_price`（默认 2.0 元）——跌到这条线附近的票，退市风险与流动性
    枯竭已经同时出现，而"便宜"恰恰会让模型更想买。
    停牌/脏数据（close 为 0、缺失、'-'）**不算**低价：那是数据缺口不是风险
    （fail-open 同全模块）。数据是未复权日收盘，取 dt ≤ asof 的最新分区。
    """
    out: dict = {}
    floor = _num(conf.get("min_price"), DEFAULTS["min_price"])
    fresh = int(_num(conf.get("penny_days"), DEFAULTS["penny_days"]))
    if floor <= 0:
        return out
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = _code6(r.get("symbol") or r.get("股票代码") or r.get("证券代码"))
        day = _ymd(r.get("dt") or r.get("time"))
        close = _num(r.get("close") if r.get("close") is not None else r.get("最新价"), 0.0)
        if not code or day is None or close <= 0:
            continue
        if day < asof - timedelta(days=fresh):
            continue  # 快照停更 → 条目失效（fail-open）
        if close >= floor:
            continue
        out[code] = {
            "reason": f"股价{close:.2f}元低于{floor:.1f}元预警线（逼近面值退市）",
            "kind": "penny",
            "expire": (day + timedelta(days=fresh)).isoformat(),
        }
    return out


def fundamental_items(doc: dict, asof: date, conf: dict) -> dict:
    """基本面/长期趋势劣化缓存 → {code6: {reason, kind, expire}}。

    用户口径（2026-09-11）「财务状况不好的基于 quantdb 也需要排除、财务状况一直
    不好的也需要排除、长期下跌趋势，不管牛市熊市都不好的」。判据与全市场扫描在
    `scripts/fundamental_flags.py`（cron 每日 build），这里只做**消费**：
      - 缓存 asof 超过 `flag_days` 自然日 → 整份失效（fail-open；行情/财报都会变，
        陈旧结论比没有结论更危险）；
      - 条目自带 asof（与文档同源），过期同样失效。
    存续状态类（kind "weak"）：连亏与年线下方不会一两天就修复，合并时取更晚失效日。
    """
    out: dict = {}
    fresh = int(_num(conf.get("flag_days"), DEFAULTS["flag_days"]))
    src = _as_date(doc.get("asof")) if isinstance(doc, dict) else None
    if src is None or src < asof - timedelta(days=fresh):
        return out  # 缓存缺失/过期 → 空白（fail-open）
    items = doc.get("items") if isinstance(doc, dict) else None
    for code, it in (items or {}).items():
        c6 = _code6(code)
        if not c6 or not isinstance(it, dict):
            continue
        reason = str(it.get("reason") or "").strip()
        if not reason:
            continue
        # reason 自带类型标签（"长期下跌：…"/"流动性枯竭：…"/"商誉…"），不再加
        # 统一前缀——"基本面劣化"框不住次新/流动性这类非财务判据。
        out[c6] = {"reason": reason, "kind": "weak",
                   "expire": (src + timedelta(days=fresh)).isoformat()}
    return out


def pledge_warns(rows: list, conf: dict) -> dict:
    """质押比例 ≥ 阈值 → {code6: 告警文本}。只告警，不进买入闸门。"""
    out: dict = {}
    thr = _num(conf.get("pledge_warn_ratio"), DEFAULTS["pledge_warn_ratio"])
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = _code6(r.get("股票代码") or r.get("证券代码"))
        ratio = _num(r.get("质押比例"), 0.0)
        if code and ratio >= thr:
            out[code] = f"质押比例{ratio:.0f}%"
    return out


# ---------------------------------------------------------------- 组装 / 落盘

def _merge(a: dict, b: dict) -> dict:
    """同一标的命中多类风险 → 理由合并。

    失效日：一般的解禁/新闻取**更早**（先失效的为准）；含存续状态类（grave 立案/
    造假、penny 低价）则取**更晚**——存续期风险不该被一个两天后到期的解禁条目
    提前解除；条目真解除要么等事件窗口走完，要么等数据源不再报（价格回升）。
    """
    out = dict(a)
    for code, it in b.items():
        if code not in out:
            out[code] = it
            continue
        prev = out[code]
        kinds = "+".join(sorted({prev["kind"], it["kind"]}))
        expires = (prev["expire"], it["expire"])
        sticky = bool(set(kinds.split("+")) & STICKY_KINDS)
        out[code] = {"reason": f"{prev['reason']}；{it['reason']}", "kind": kinds,
                     "expire": max(expires) if sticky else min(expires)}
    return out


def _read_unlock_rows() -> list:
    try:
        import event_radar  # 同仓库脚本；akshare 为延迟导入，读缓存不触发网络

        f = event_radar._latest_cache("unlock")
        return event_radar._read_cache(f) if f is not None else []
    except Exception:  # noqa: BLE001 数据缺失不阻塞交易（fail-open）
        return []


def _read_pledge_rows() -> list:
    try:
        import event_radar

        f = event_radar._latest_cache("pledge")
        return event_radar._read_cache(f) if f is not None else []
    except Exception:  # noqa: BLE001
        return []


def read_price_rows(asof: date | None = None, root: Path | None = None) -> list:
    """全市场未复权日收盘（quantdb 分区，取 dt ≤ asof 的最新一天）。失败 → []。

    root 默认 KLINE_DIR（QUANTDB_KLINE_DIR 可覆盖），入参留给测试。只读一天的
    parquet（≈5500 行）——不扫全量历史。
    """
    try:
        import duckdb

        base = Path(root or KLINE_DIR)
        parts = [p for p in base.glob("dt=*") if p.is_dir() and p.name[3:].isdigit()]
        if asof is not None:
            parts = [p for p in parts if p.name[3:] <= asof.strftime("%Y%m%d")]
        if not parts:
            return []
        f = sorted(parts)[-1] / "data.parquet"
        df = duckdb.connect().execute(
            f"SELECT symbol, close, dt FROM read_parquet('{f}')").df()
        return df.to_dict("records")
    except Exception:  # noqa: BLE001 数据源缺失不阻塞交易（fail-open）
        return []


def _read_fundamental_flags(path: Path | None = None) -> dict:
    """读 fundamentals 缓存（fundamental_flags.py 产出）；缺失/损坏 → {}。"""
    try:
        doc = json.loads((path or FUND_FLAGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _read_news_lines() -> list:
    try:
        # errors="replace"：news history 是含中文的追加日志（news_brief.py:680 open("a")），
        # 截断在多字节字符中间会整文件抛 UnicodeDecodeError（不是 OSError）；
        # 逐行 handler 本就容错（2026-09-12 批 10）
        text = NEWS_HISTORY.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build(asof: date | None = None, conf: dict | None = None,
          unlock_rows: list | None = None, news_lines: list | None = None,
          pledge_rows: list | None = None, price_rows: list | None = None,
          fundamental: dict | None = None) -> dict:
    """组装风险清单（items）。显式传入行数据即不读真实数据源（测试用）。"""
    asof = asof or datetime.now(BJ).date()
    conf = conf if conf is not None else load_conf()
    if not conf.get("enabled", True):
        return {}
    rows_u = _read_unlock_rows() if unlock_rows is None else unlock_rows
    rows_p = _read_pledge_rows() if pledge_rows is None else pledge_rows
    lines_n = _read_news_lines() if news_lines is None else news_lines
    rows_pr = read_price_rows(asof) if price_rows is None else price_rows
    fund = _read_fundamental_flags() if fundamental is None else fundamental
    return _merge(_merge(_merge(_merge(unlock_items(rows_u, asof, conf),
                                       news_items(lines_n, asof, conf)),
                                grave_items(lines_n, asof, conf)),
                         price_items(rows_pr, asof, conf)),
                  fundamental_items(fund, asof, conf))


def refresh(path: Path | None = None, asof: date | None = None,
            unlock_rows: list | None = None, news_lines: list | None = None,
            pledge_rows: list | None = None, price_rows: list | None = None,
            fundamental: dict | None = None) -> dict:
    """重建清单并原子落盘。返回 {"items","warns","watch"}（供 cron 日志）。"""
    asof = asof or datetime.now(BJ).date()
    conf = load_conf()
    items = build(asof, conf, unlock_rows, news_lines, pledge_rows, price_rows, fundamental)
    rows_p = _read_pledge_rows() if pledge_rows is None else pledge_rows
    lines_n = _read_news_lines() if news_lines is None else news_lines
    on = bool(conf.get("enabled", True))
    warns = pledge_warns(rows_p, conf) if on else {}
    watch = regulatory_watch(lines_n, asof, conf) if on else {}
    watch = {c: v for c, v in watch.items() if c not in items}  # 已硬拦的不重复提醒
    doc = {"asof": asof.isoformat(),
           "generated_at": datetime.now(BJ).isoformat(),
           "items": items, "warns": warns, "watch": watch}
    p = path or OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return {"items": len(items), "warns": len(warns), "watch": len(watch)}


def load_risk(path: Path | None = None, asof: date | None = None) -> dict:
    """读清单 {code6: {reason, kind, expire}}；缺失/损坏 → {}（fail-open）。

    读取时再按 expire 过滤一次：文件可能是几天前的，解禁日一过就不该继续拦。
    """
    try:
        doc = json.loads((path or OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, dict):
        return {}
    today = (asof or datetime.now(BJ).date()).isoformat()
    return {str(c): v for c, v in items.items()
            if isinstance(v, dict) and str(v.get("expire") or "") >= today}


def load_watch(path: Path | None = None, asof: date | None = None) -> dict:
    """读监管关注段 {code6: {reason, kind, until}}；缺失/损坏 → {}（fail-open）。

    与 load_risk 的 items（禁买）是两回事：这是**提示词提醒**用的软信号。
    """
    try:
        doc = json.loads((path or OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    watch = doc.get("watch") if isinstance(doc, dict) else None
    if not isinstance(watch, dict):
        return {}
    today = (asof or datetime.now(BJ).date()).isoformat()
    return {str(c): v for c, v in watch.items()
            if isinstance(v, dict) and str(v.get("until") or "") >= today}


def annotate(codes, path: Path | None = None) -> dict:
    """对给定代码清单打风险标注：{原样代码: 原因}（只含命中项）。供提示词/告警消费。"""
    risk = load_risk(path)
    out: dict = {}
    for c in codes or []:
        c6 = _code6(c)
        if c6 in risk:
            out[str(c)] = str(risk[c6].get("reason") or "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="事件风险清单：解禁/负面新闻 → 买入硬拦")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="重建 data/risk_block.json")
    sub.add_parser("show", help="打印当前清单")
    c = sub.add_parser("check", help="查指定代码是否在清单内")
    c.add_argument("codes", help="逗号分隔，带不带后缀均可")
    a = ap.parse_args()

    if a.cmd == "refresh":
        stats = refresh()
        print(f"items={stats['items']} warns={stats['warns']}")
        return 0
    if a.cmd == "show":
        risk = load_risk()
        for code, it in sorted(risk.items()):
            print(f"{code} [{it.get('kind')}] {it.get('reason')} (至 {it.get('expire')})")
        if not risk:
            print("（禁买清单为空）")
        watch = load_watch()
        for code, it in sorted(watch.items()):
            print(f"{code} [watch:{it.get('kind')}] {it.get('reason')} "
                  f"(至 {it.get('until')}，只提醒不禁买)")
        return 0
    if a.cmd == "check":
        hit = annotate([x.strip() for x in a.codes.split(",") if x.strip()])
        for c_, r in hit.items():
            print(f"{c_}: {r}")
        if not hit:
            print("（均不在风险清单内）")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
