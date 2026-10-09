#!/usr/bin/env python3
"""交易日历（节假日感知闸门）：统一入口 → 法定假日休市不分析。

数据源（两层重叠区间逐日核对一致）：
1. configs/trading_days.json —— 新浪沪深交易日清单，**含当年未来法定假日**
   （跨年需 scripts/refresh_trading_cal.py 刷新；quantdb 每日同步只到昨天，
   覆盖不到"今天"，所以盘中/盘前判定必须以本文件为主）
2. quantdb trading_calendar/trading_days.parquet —— 历史口径，每日收盘后自动补全

原则：日历覆盖到当日 → 以日历为准（法定假日休市、调休照交易所口径）；
日历未覆盖未来（如跨年 json 未刷新）→ 退化为周一到周五判断，并由调用方
打印覆盖缺口提示。盘中/复盘/研究/预算/执行哨兵入口统一用它。
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_JSON_PATH = ROOT / "configs" / "trading_days.json"
_QDB_PATHS = [
    Path.home() / "projects/quantmind/data/quantdb/2_base_sector/trading_calendar",
    Path("/data/quantdb/2_base_sector/trading_calendar"),
]
# 进程内缓存（cron 每进程一次；json 优先于 duckdb，避免高频进程都开库）
_json_cache: dict = {"days": None, "max": None}
_qdb_cache: dict = {"days": None, "max": None}


def _load_json() -> tuple[set, str]:
    """仓库静态全年清单（1990-当年年末）。"""
    if _json_cache["days"] is None:
        days: set = set()
        mx = ""
        try:
            j = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
            for x in j.get("days", []):
                s = str(x)
                if s.isdigit() and len(s) == 8:
                    days.add(s)
                    mx = max(mx, s)
        except (OSError, ValueError, TypeError):
            pass
        _json_cache["days"], _json_cache["max"] = days, mx
    return _json_cache["days"], _json_cache["max"]


def _load_qdb() -> tuple[set, str]:
    """quantdb 每日同步文件（昨日及更早）。"""
    if _qdb_cache["days"] is None:
        days: set = set()
        mx = ""
        try:
            import glob

            import duckdb

            for base in _QDB_PATHS:
                fs = sorted(glob.glob(f"{base}/*.parquet"))
                if not fs:
                    continue
                rows = duckdb.connect().execute(
                    "SELECT TradingDate FROM read_parquet(?)", [fs[-1]]).fetchall()
                for r in rows:
                    s = str(r[0])
                    if s.isdigit() and len(s) == 8:
                        days.add(s)
                        mx = max(mx, s)
                break
        except Exception:  # noqa: BLE001  duckdb/文件缺失 → json 已足够
            pass
        _qdb_cache["days"], _qdb_cache["max"] = days, mx
    return _qdb_cache["days"], _qdb_cache["max"]


def coverage_max() -> str | None:
    """日历覆盖到的最大日期（YYYYMMDD）；None=无任何日历数据。"""
    jd, jmx = _load_json()
    qd, qmx = _load_qdb()
    return max(jmx, qmx) or None


def is_trading_day(d: date) -> bool:
    """日历覆盖 d → 以日历为准（法定假日/调休均正确）；否则星期判断兜底。"""
    key = d.strftime("%Y%m%d")
    jd, jmx = _load_json()
    if jd and key <= jmx:
        return key in jd
    qd, qmx = _load_qdb()
    if qd and key <= qmx:
        return key in qd
    return d.weekday() < 5


def why_not(d: date | datetime) -> str:
    """非交易日原因文案（调用方打日志用）；交易日返回空串。"""
    if isinstance(d, datetime):
        d = d.date()
    if is_trading_day(d):
        return ""
    key = d.strftime("%Y%m%d")
    jd, jmx = _load_json()
    qd, qmx = _load_qdb()
    covered = (jd and key <= jmx) or (qd and key <= qmx)
    if d.weekday() >= 5:
        return "周末"
    if covered:
        return "法定节假日（交易日历休市）"
    return "非交易日（日历未覆盖至当日，按星期兜底判定）"


def trading_days_between(start: date, end: date) -> int:
    """(start, end] 内的交易日数——建仓日不计，用于"已持有 N 个交易日"。"""
    if end < start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def next_trading_day(d: date | None = None) -> date:
    """下一个交易日（日历覆盖时优先）。"""
    d = d or date.today()
    for _ in range(15):
        d = d + timedelta(days=1)
        if is_trading_day(d):
            return d
    return d


def prev_trading_day(d: date | None = None, *, inclusive: bool = False) -> date:
    """最近交易日：inclusive=True 且 d 本身是交易日 → 返回 d；否则严格早于 d。

    用于「最近已收盘交易日」判定：批量任务在凌晨/盘前运行时当天的会话尚未收盘，
    必须回看前一天（周末/假期自动跳过）。日历未覆盖时退化为星期判断
    （同 is_trading_day 的口径）。
    """
    d = d or date.today()
    if inclusive and is_trading_day(d):
        return d
    for _ in range(20):          # 覆盖春节/国庆最长休市段
        d = d - timedelta(days=1)
        if is_trading_day(d):
            return d
    return d


# 当日日K 从开盘起才有意义（集合竞价阶段的 bar 尚未生成）；收盘后今天的 bar 已定稿。
_OPEN_MIN = 9 * 60 + 30
_CLOSE_MIN = 15 * 60


def expected_bar_date(now: datetime | None = None) -> str:
    """此刻最新一根**日K** 应该是什么日期（YYYYMMDD）—— 新鲜度判据的锚点。

    锚点不是「今天」：它取决于**日期与时刻两个输入**。休市日、开盘前，今天的 bar
    本就不该存在，锚点得回退到上一个交易日；否则整个假日都在假报「停更」——
    仓里三份新鲜度实现（live_hourly_analysis.market_data_stale、技能脚本
    tdx_realtime、桥侧 client.py）此前都各自拿「今天」当锚点，故收敛到这里。

    保守方向：算出的锚点只会**不晚于**真实应出的 bar 日期，于是「最后一根 <
    锚点」更难成立 —— 这个函数只会消掉假停更，不会新增。调用方仍应保持
    fail-open（取不到行情 → 视为新鲜），别让桥抖动变成阻塞交易。
    """
    n = now or datetime.now()
    if n.tzinfo is not None:                  # 本机时区是 JST：带 tz 的一律换算到北京时间
        from zoneinfo import ZoneInfo

        n = n.astimezone(ZoneInfo("Asia/Shanghai"))
    d = n.date()
    if is_trading_day(d) and n.hour * 60 + n.minute >= _OPEN_MIN:
        return d.strftime("%Y%m%d")
    return prev_trading_day(d).strftime("%Y%m%d")


def days_to_next_trading_day(d: date | None = None) -> int:
    """距下一交易日的自然日数（1=复市就在明天 → 「休市最后一晚」）。

    供晚间研究等面向次日开盘的任务做闸门：交易日晚上照常执行，
    非交易日中只有 gap==1（周末/长假末夜）值得补研究，假期中段跳过。
    """
    return (next_trading_day(d) - (d or date.today())).days


if __name__ == "__main__":
    today = date.today()
    cov = coverage_max()
    print(f"今天 {today}({['周一','周二','周三','周四','周五','周六','周日'][today.weekday()]}) "
          f"交易日: {is_trading_day(today)}  {why_not(today)}")
    print(f"日历覆盖至: {cov or '无'}  下一交易日: {next_trading_day()}")
    for p in ("20260904", "20261001", "20261002", "20261008", "20261231", "20270101"):
        d = date.fromisoformat(f"{p[:4]}-{p[4:6]}-{p[6:]}")
        print(f"  {p}: {is_trading_day(d)}  {why_not(d)}")
