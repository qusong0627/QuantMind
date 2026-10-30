"""研究总控的事实包：系统 / 市场 / 交易 / 对话 / 新闻 / 模型 / 经验 / 已有复盘。

设计纪律（与 ``live_model_analysis`` 的真话纪律同源）：

* **每个采集器都容错**：读不到就写进 ``gaps`` 并在正文里显式标「数据缺口」，
  **绝不编数据、绝不把空表写成 0**；
* **不出现账户总资产**：交易面只给各 agent 的分账账本（持仓成本 / 账面现金 /
  当日成交 / 已实现盈亏），账户主人的总资产与可用资金不在可见面；
* **口径带日期**：quantdb 分区、信号批次、新闻窗口都写明是哪一天的，避免
  「今天的复盘引用了昨天的盘面」却看不出来；
* 宿主 cron 与容器内都可跑：PG 连接按 ``POSTGRES_HOST/DB_HOST`` 解析，读不到
  只降级，不抛。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import (
    CN_TZ,
    agent_data_root,
    data_base,
    data_root,
    extract_section,
    read_report,
    repo_root,
)

#: 各段落标题（提示词与报告结构共用）
SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("system", "系统与数据新鲜度"),
    ("market", "市场与资金"),
    ("trading", "持仓与交易复盘"),
    ("dialogue", "对话与决策复盘"),
    ("news", "新闻与事件"),
    ("models", "模型与因子"),
    ("experience", "经验与教训"),
    ("prior", "本日已出复盘"),
)

#: 主看指数（parquet ``index_daily`` 的 symbol）
MAIN_INDEXES: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
)

#: 经验段落里保留的最近条目数
MEMORY_TAIL_ITEMS = 3
#: 单段渲染上限（字符）——防止某一段把提示词吃光
SECTION_LIMIT = 5200


# ── 小工具 ───────────────────────────────────────────────────────────


def _clip(text: object, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _bullet(lines: list[str]) -> str:
    return "\n".join(f"- {ln}" for ln in lines if str(ln).strip())


def _prefix(code: object) -> str:
    c = str(code or "").strip().upper()
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", c):
        return c
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", c)
    if m:
        return m.group(2) + m.group(1)
    m = re.fullmatch(r"(\d{6})", c)
    if m:
        n = m.group(1)
        if n[0] in "69":
            return "SH" + n
        if n[0] in "023":
            return "SZ" + n
        if n[0] in "48":
            return "BJ" + n
    return c


def _suffix(code: object) -> str:
    p = _prefix(code)
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", p):
        return f"{p[2:]}.{p[:2]}"
    return str(code or "")


def _dash(day: str) -> str:
    s = str(day or "").replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else str(day)


def _d8(day: str) -> str:
    return str(day or "").replace("-", "")[:8]


def load_env() -> dict[str, str]:
    """``.env`` → 进程环境 → ``runtime.env``（后者覆盖前者；空值不覆盖）。"""
    src: dict[str, str] = {}
    env_file = repo_root() / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            src[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    src.update({k: v for k, v in os.environ.items() if str(v).strip()})
    try:
        from backend.shared.runtime_secrets import read_runtime_env

        src.update({k: v for k, v in read_runtime_env().items() if str(v).strip()})
    except Exception:  # noqa: BLE001 - 配置面读失败不挡复盘
        pass
    return src


def _connect(env: dict[str, str]):
    """短连接（宿主 cron 每次新进程）。读不到抛，由调用方记缺口。"""
    import psycopg2

    host = env.get("POSTGRES_HOST") or env.get("DB_HOST") or "127.0.0.1"
    port = int(env.get("POSTGRES_PORT") or env.get("DB_PORT") or 5432)
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=env.get("DB_NAME") or env.get("POSTGRES_DB") or "quantmind",
        user=env.get("DB_USER") or env.get("POSTGRES_USER") or "quantmind",
        password=env.get("DB_PASSWORD") or env.get("POSTGRES_PASSWORD") or "",
        connect_timeout=8,
        options="-c statement_timeout=20000",
    )
    conn.set_session(readonly=True)
    return conn


def _names_map(cur, trade_day: date | None) -> dict[str, str]:
    """一行一天的全市场名称表（键：前缀式、后缀式、裸码都放）。"""
    if not trade_day:
        return {}
    try:
        cur.execute(
            "select symbol, stock_name from stock_daily_latest where trade_date=%s",
            (trade_day,),
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for sym, name in cur.fetchall():
        if not name:
            continue
        s = str(sym)
        out[s] = str(name)
        out[_suffix(s)] = str(name)
        if re.fullmatch(r"(SH|SZ|BJ)\d{6}", s):
            out[s[2:]] = str(name)
    return out


def _name_of(code: object, names: dict[str, str]) -> str:
    c = str(code or "")
    return names.get(c) or names.get(_prefix(c)) or names.get(_suffix(c)) or "—"


# ── 数据类 ───────────────────────────────────────────────────────────


@dataclass
class Dossier:
    day: str
    target: str
    slot: str
    now: datetime
    sections: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def section(self, key: str, title: str) -> str:
        body = (self.sections.get(key) or "").strip()
        if not body:
            return f"## {title}\n（数据缺口：本段无可用数据）"
        return f"## {title}\n{_clip(body, SECTION_LIMIT)}"

    def render(self) -> str:
        head = (
            f"【研究总控事实包】复盘日 {_dash(self.day)}｜档位 {self.slot}｜"
            f"北京时间 {self.now.strftime('%Y-%m-%d %H:%M')}｜目标交易日 {_dash(self.target)}"
        )
        parts = [head]
        for key, title in SECTION_ORDER:
            parts.append(self.section(key, title))
        if self.gaps:
            parts.append("## 数据缺口汇总\n" + "、".join(sorted(set(self.gaps))))
        return "\n\n".join(parts)


# ── 采集器 ───────────────────────────────────────────────────────────


def collect_system(d: Dossier, env: dict[str, str], conn) -> None:
    lines: list[str] = []
    # quantdb 最新分区
    try:
        parts = sorted(
            (data_base() / "quantdb" / "1_kline_data" / "daily_forward").glob("dt=*")
        )
        latest = parts[-1].name.split("=")[1] if parts else ""
        lines.append(
            f"quantdb daily_forward 最新分区：{_dash(latest) if latest else '缺失'}"
        )
        if latest and latest < d.day.replace("-", ""):
            d.gaps.append("quantdb_partition_behind_day")
    except OSError:
        d.gaps.append("quantdb_unreadable")
    # 新闻
    try:
        brief = json.loads(
            (data_base() / "news_brief" / "latest.json").read_text(encoding="utf-8")
        )
        lines.append(
            f"新闻分子最新：{brief.get('ts', '?')}（窗口 {brief.get('window', {}).get('end', '?')}）"
        )
    except (OSError, ValueError):
        d.gaps.append("news_brief_missing")
    # 信号
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute("select max(trade_date) from engine_signal_scores")
            row = cur.fetchone()
            lines.append(f"engine_signal_scores 最新交易日：{row[0] if row else '无'}")
        except Exception:  # noqa: BLE001
            d.gaps.append("signals_unreadable")
    else:
        d.gaps.append("pg_unreachable")
    # 池文件
    try:
        pool = data_base() / "reports" / "stock_picks" / f"{d.target}_agent_picks.json"
        if pool.is_file():
            obj = json.loads(pool.read_text(encoding="utf-8"))
            lines.append(
                f"目标日池文件：{pool.name}（{len(obj.get('picks') or [])} 只）"
            )
        else:
            lines.append(f"目标日池文件：{pool.name} 尚未生成")
    except (OSError, ValueError):
        d.gaps.append("picks_unreadable")
    # 上一份 brief
    try:
        prev = data_root() / "brief"
        if prev.is_dir() and any(prev.glob("*.md")):
            lines.append("研究总控 brief：已有历史文件")
        else:
            lines.append("研究总控 brief：尚无历史文件（本档将首建）")
    except OSError:
        pass
    d.sections["system"] = _bullet(lines)


def collect_market(d: Dossier, env: dict[str, str], conn) -> None:
    lines: list[str] = []
    # 1) 指数日线（parquet，最近两个可用分区）
    try:
        import pandas as pd

        root = data_base() / "quantdb" / "1_kline_data" / "index_daily"
        parts = sorted(root.glob("dt=*"))
        if parts:
            cur_day = parts[-1].name.split("=")[1]
            cur = pd.read_parquet(
                parts[-1] / "data.parquet", columns=["symbol", "close"]
            )
            prev_map: dict[str, float] = {}
            if len(parts) > 1:
                prev = pd.read_parquet(
                    parts[-2] / "data.parquet", columns=["symbol", "close"]
                )
                prev_map = dict(zip(prev["symbol"], prev["close"], strict=False))
            idx_lines = []
            for sym, name in MAIN_INDEXES:
                row = cur[cur["symbol"] == sym]
                if row.empty:
                    continue
                c = float(row.iloc[0]["close"])
                p = prev_map.get(sym)
                pct = f"{(c / p - 1) * 100:+.2f}%" if p else "—"
                idx_lines.append(f"{name} {c:.2f}（{pct}）")
            if idx_lines:
                lines.append(f"指数（分区 {_dash(cur_day)}）：" + "；".join(idx_lines))
                if cur_day < d.day.replace("-", ""):
                    lines.append(f"注意：指数分区止于 {_dash(cur_day)}，非复盘日当日。")
    except Exception as exc:  # noqa: BLE001
        d.gaps.append("index_daily_unreadable")
        lines.append(f"指数日线不可用：{type(exc).__name__}")
    # 2) 全市场宽度 + 行业（PG 最新可用交易日）
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(
                "select max(trade_date) from stock_daily_latest where trade_date <= %s",
                (d.day,),
            )
            row = cur.fetchone()
            mday = row[0] if row else None
            if mday:
                cur.execute(
                    """
                    select count(*) filter (where pct_change > 0),
                           count(*) filter (where pct_change < 0),
                           count(*) filter (where pct_change = 0),
                           count(*) filter (where pct_change is null),
                           percentile_cont(0.5) within group (order by pct_change),
                           avg(pct_change),
                           count(*) filter (where pct_change >= 9.5),
                           count(*) filter (where pct_change <= -9.5),
                           sum(amount) / 10000.0
                    from stock_daily_latest where trade_date=%s
                    """,
                    (mday,),
                )
                adv, dec, flat, nodata, med, avg, up95, dn95, amt_yi = cur.fetchone()
                lines.append(
                    f"全市场（{mday}）：上涨 {adv} / 下跌 {dec} / 平 {flat}"
                    f"（无涨跌幅 {nodata}）｜中位涨幅 {(med or 0):+.2f}%"
                    f"｜均值 {(avg or 0):+.2f}%｜涨幅≥9.5% {up95} 家、≤-9.5% {dn95} 家"
                    f"｜成交 {float(amt_yi or 0):,.0f} 亿元"
                )
                if str(mday) < d.day:
                    d.gaps.append("market_day_behind_review_day")
                cur.execute(
                    """
                    select coalesce(nullif(industry, ''), '未分类') ind,
                           count(*) n, avg(pct_change), count(*) filter (where pct_change > 0)
                    from stock_daily_latest
                    where trade_date=%s and pct_change is not null
                    group by 1 having count(*) >= 3
                    order by avg(pct_change) desc
                    """,
                    (mday,),
                )
                rows = cur.fetchall()
                if rows:
                    top = "；".join(
                        f"{r[0]} {float(r[2]):+.2f}%（{r[3]}/{r[1]}）" for r in rows[:6]
                    )
                    bottom = "；".join(
                        f"{r[0]} {float(r[2]):+.2f}%（{r[3]}/{r[1]}）"
                        for r in rows[-5:][::-1]
                    )
                    lines.append(f"行业前列：{top}")
                    lines.append(f"行业后列：{bottom}")
        except Exception:  # noqa: BLE001
            d.gaps.append("breadth_unreadable")
    # 3) 热集盘中快照（覆盖有限，明确写 N 只）
    # quotes.pre_close 实际整列为 NULL（tdx_bridge 只写 current_price），所以昨收
    # 从 stock_daily_latest 取最近一个**早于复盘日**的交易日，按同一前缀代码 join。
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(
                """
                with q as (
                    select distinct on (symbol) symbol, current_price, timestamp
                    from quotes where timestamp >= %s
                    order by symbol, timestamp desc
                ),
                p as (
                    select symbol, close, trade_date
                    from stock_daily_latest
                    where trade_date = (
                        select max(trade_date) from stock_daily_latest where trade_date < %s)
                )
                select count(*),
                       count(*) filter (where q.current_price > p.close),
                       count(*) filter (where q.current_price < p.close),
                       percentile_cont(0.5) within group (
                           order by (q.current_price / p.close - 1) * 100),
                       max(q.timestamp), max(p.trade_date)
                from q join p using (symbol)
                where p.close > 0 and q.current_price > 0
                """,
                (d.day + " 00:00:00+08", d.day),
            )
            n, up, down, med, last_ts, prev_day = cur.fetchone()
            if n:
                lines.append(
                    f"热集盘中快照：覆盖 {n} 只（{str(last_ts)[11:16]} 最新，昨收取 {prev_day}）"
                    f"｜涨 {up} / 跌 {down}｜中位 {(float(med) if med is not None else 0):+.2f}%"
                    "（仅热集，不代表全市场）"
                )
                if last_ts and hasattr(last_ts, "date") and str(last_ts)[:10] != d.day:
                    d.gaps.append("quotes_stale")
        except Exception:  # noqa: BLE001
            pass
    d.sections["market"] = "\n".join(lines)


def collect_trading(d: Dossier, env: dict[str, str], conn) -> None:
    if conn is None:
        d.sections["trading"] = "（数据缺口：PG 不可达，无分账账本数据）"
        d.gaps.append("pg_unreachable")
        return
    lines: list[str] = []
    try:
        cur = conn.cursor()
        cur.execute(
            "select max(trade_date) from stock_daily_latest where trade_date <= %s",
            (d.day,),
        )
        row = cur.fetchone()
        names = _names_map(cur, row[0] if row else None)
        # 持仓
        cur.execute(
            """
            select agent, count(*), sum(volume * cost_price)
            from qm_agent_ledger_position where volume > 0
            group by 1 order by 3 desc
            """
        )
        positions = cur.fetchall()
        # 现金
        cur.execute("select agent, virtual_cash from qm_agent_ledger_account")
        cash = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
        if positions:
            lines.append(
                "各 agent 分账账本（只含授予额度与本人持仓，不含账户总资产）："
            )
            for agent, n, cost in positions:
                lines.append(
                    f"{agent}：持仓 {n} 只，成本 ¥{float(cost or 0):,.0f}，账面现金 ¥{cash.get(agent, 0):,.0f}"
                )
            cur.execute(
                """
                select agent, code, volume, cost_price
                from qm_agent_ledger_position where volume > 0
                order by agent, volume * cost_price desc
                """
            )
            detail: dict[str, list[str]] = {}
            for agent, code, vol, cost in cur.fetchall():
                detail.setdefault(agent, []).append(
                    f"{_name_of(code, names)}({_suffix(code)}) {float(vol):.0f}股"
                    f"@{float(cost or 0):.3f}"
                )
            for agent, items in detail.items():
                lines.append(f"  · {agent} 持仓明细：{'；'.join(items[:8])}")
        else:
            lines.append("分账账本：无持仓")
        # 当日成交
        cur.execute(
            """
            select agent, side, count(*), sum(volume), sum(volume * price)
            from qm_agent_ledger_fill where trade_date=%s
            group by 1,2 order by 1,2
            """,
            (d.day,),
        )
        fills = cur.fetchall()
        if fills:
            lines.append(f"当日成交（{d.day}）：")
            for agent, side, n, vol, amt in fills:
                lines.append(
                    f"{agent} {'买入' if side == 'buy' else '卖出'} {n} 笔，"
                    f"{float(vol or 0):,.0f} 股，金额 ¥{float(amt or 0):,.0f}"
                )
            cur.execute(
                """
                select agent, code, side, volume, price, filled_at
                from qm_agent_ledger_fill where trade_date=%s
                order by filled_at desc limit 12
                """,
                (d.day,),
            )
            recent = [
                f"{r[0]} {_name_of(r[1], names)}({_suffix(r[1])}) "
                f"{'买' if r[2] == 'buy' else '卖'} {float(r[3] or 0):.0f}@{float(r[4] or 0):.3f}"
                f" {str(r[5])[11:16]}"
                for r in cur.fetchall()
            ]
            lines.append("最近成交：" + "；".join(recent))
        else:
            lines.append("当日成交：无")
        # 已实现盈亏
        cur.execute(
            """
            select agent, count(*), sum(realized_pnl), avg(pnl_pct)
            from qm_agent_ledger_roundtrip
            where sell_ts >= %s and sell_ts < %s
            group by 1 order by 3 desc
            """,
            (
                _dash(d.day) + " 00:00:00+08",
                (date.fromisoformat(_dash(d.day)) + timedelta(days=1)).isoformat()
                + " 00:00:00+08",
            ),
        )
        rt = cur.fetchall()
        if rt:
            lines.append(
                "当日已实现（已平仓）："
                + "；".join(
                    f"{a} {n} 笔 ¥{float(p or 0):+,.0f}（{float(pct or 0):+.2f}%）"
                    for a, n, p, pct in rt
                )
            )
    except Exception as exc:  # noqa: BLE001
        d.gaps.append("ledger_unreadable")
        lines.append(f"分账账本不可读：{type(exc).__name__}")
    d.sections["trading"] = "\n".join(lines)


def _agent_log_dirs() -> list[Path]:
    root = agent_data_root()
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def collect_dialogue(d: Dossier, env: dict[str, str], conn) -> None:
    lines: list[str] = []
    day_dir = d.day
    for agent_dir in _agent_log_dirs():
        name = agent_dir.name
        if name in ("market-research", "fake-model"):
            continue
        log = agent_dir / "log" / day_dir / "log.jsonl"
        if not log.is_file():
            continue
        try:
            rows = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError):
            d.gaps.append(f"log_unreadable:{name}")
            continue
        if not rows:
            continue
        reviews = [r for r in rows if str(r.get("mode") or "") == "review"]
        last = rows[-1]
        assistant = ""
        for msg in reversed(last.get("new_messages") or []):
            if str(msg.get("role")) in ("assistant", "ai"):
                assistant = str(msg.get("content") or "")
                break
        if name.startswith("news-"):
            lines.append(f"{name}：{len(rows)} 条（其中复盘 {len(reviews)} 条）")
            continue
        gaps = sorted({g for r in rows for g in (r.get("data_gaps") or [])})
        lines.append(
            f"{name}：{len(rows)} 条（复盘 {len(reviews)} 条）"
            + (f"，数据缺口 {'/'.join(gaps)}" if gaps else "")
        )
        if assistant:
            lines.append(
                f"  最后一轮（{str(last.get('timestamp') or '')[:16]}）：{_clip(assistant, 420)}"
            )
    d.sections["dialogue"] = "\n".join(lines) if lines else "（当日无 agent 对话日志）"


def collect_news(d: Dossier, env: dict[str, str], conn) -> None:
    root = data_base() / "news_brief"
    lines: list[str] = []
    try:
        brief = json.loads((root / "latest.json").read_text(encoding="utf-8"))
        lines.append(f"新闻分子（{brief.get('ts', '?')}）：")
        if brief.get("text"):
            lines.append(_clip(brief["text"], 1500))
        macro = brief.get("macro") or {}
        if isinstance(macro, dict) and macro.get("view"):
            lines.append(f"宏观视角：{macro['view']}")
        themes = brief.get("themes") or []
        if themes:
            lines.append(
                "主题："
                + "；".join(
                    f"{t.get('name')}（{_clip(t.get('logic'), 90)}）"
                    for t in themes[:5]
                )
            )
        if brief.get("market_notes"):
            lines.append(f"盘面注解：{_clip(brief['market_notes'], 300)}")
        risks = brief.get("open_risks") or []
        if risks:
            lines.append("未消风险：" + "；".join(_clip(r, 100) for r in risks[:5]))
    except (OSError, ValueError):
        d.gaps.append("news_brief_missing")
    try:
        review = json.loads(
            (root / "reviews" / f"{d.day}.json").read_text(encoding="utf-8")
        )
        summary = review.get("summary") or {}
        lines.append(
            f"新闻→走势复盘（{d.day}）：命中 {summary.get('hit', 0)} / 反向 {summary.get('reverse', 0)}"
            f" / 平 {summary.get('flat', 0)} / 无数据 {summary.get('nodata', 0)}"
        )
        hits = [s for s in (review.get("signals") or []) if s.get("result") == "hit"][
            :5
        ]
        revs = [
            s for s in (review.get("signals") or []) if s.get("result") == "reverse"
        ][:5]
        if hits:
            lines.append(
                "命中样例："
                + "；".join(
                    f"{s.get('name')}({s.get('code')}) {s.get('verdict')}/{s.get('event')}"
                    f" {s.get('move')}%"
                    for s in hits
                )
            )
        if revs:
            lines.append(
                "反向样例："
                + "；".join(
                    f"{s.get('name')}({s.get('code')}) {s.get('verdict')}/{s.get('event')}"
                    f" {s.get('move')}%"
                    for s in revs
                )
            )
    except (OSError, ValueError):
        pass
    try:
        lessons = json.loads((root / "lessons.json").read_text(encoding="utf-8"))
        rows = []
        for event, stat in (lessons.get("by_event_type") or {}).items():
            n = sum(int(v or 0) for v in stat.values())
            if n < 10:
                continue
            hit = int(stat.get("hit") or 0)
            rows.append((hit / n, n, event, hit, int(stat.get("reverse") or 0)))
        rows.sort(reverse=True)
        if rows:
            lines.append(
                "事件类型命中率（样本≥10）："
                + "；".join(
                    f"{e} {r * 100:.0f}%（n={n}，反向 {rev}）"
                    for r, n, e, _h, rev in rows[:6]
                )
            )
    except (OSError, ValueError):
        pass
    d.sections["news"] = "\n".join(lines)


def collect_models(d: Dossier, env: dict[str, str], conn) -> None:
    if conn is None:
        d.sections["models"] = "（数据缺口：PG 不可达，无模型分数）"
        d.gaps.append("pg_unreachable")
        return
    lines: list[str] = []
    try:
        cur = conn.cursor()
        # 市场状态
        try:
            cur.execute(
                "select state, trade_date from qm_regime_daily where market='CN' order by trade_date desc limit 1"
            )
            r = cur.fetchone()
            if r:
                lines.append(f"市场状态（regime）：CN {r[0]}（{r[1]}）")
        except Exception:  # noqa: BLE001
            pass
        # 最新批次（每个 market/source 一条）。
        # 排序先 trade_date 再 signal_ts：老批次有 signal_ts 为 NULL 的（NULLS FIRST
        # 会把 6 月的批抬到最新），按 trade_date 兜住；同日多批再由 signal_ts 取最新。
        #
        # **不要退回 `distinct on (market, source)` 全表版**（2026-10-30 实测）：
        # 它在 2,200 万行上走 parallel seq scan + sort，20s statement_timeout 直接
        # QueryCanceled——连续四天「模型分数不可读」就是它。改成 VALUES × LATERAL
        # 后每个 (market,source) 用 idx_ess_market_source_date_ts 取首行，0.9ms。
        cur.execute(
            """
            with combos(market, source) as (
              values ('CN', 'batch'), ('CN', 'realtime'),
                     ('CUSTOM', 'batch'), ('CUSTOM', 'realtime')
            )
            select c.market, c.source, x.run_id, x.trade_date, x.signal_ts
            from combos c
            cross join lateral (
              select run_id, trade_date, signal_ts
              from engine_signal_scores
              where market = c.market and source = c.source and trade_date <= %s
              order by trade_date desc, signal_ts desc nulls last
              limit 1
            ) x
            order by c.market, c.source
            """,
            (d.day,),
        )
        runs = cur.fetchall()
        if not runs:
            d.gaps.append("signals_missing")
            lines.append("engine_signal_scores：无可用批次")
        for market, source, run_id, tday, ts in runs:
            cur.execute(
                """
                select count(*),
                       count(*) filter (where signal_side = 'BUY'),
                       count(*) filter (where signal_side = 'HOLD'),
                       count(*) filter (where signal_side = 'SELL'),
                       count(*) filter (where signal_side is null),
                       avg(fusion_score), max(fusion_score),
                       max(model_version), max(feature_version)
                from engine_signal_scores
                where run_id=%s and market=%s and source=%s
                """,
                (run_id, market, source),
            )
            n, buy, hold, sell, unlabeled, avg, mx, mv, fv = cur.fetchone()
            side_txt = f"BUY {buy} / HOLD {hold} / SELL {sell}"
            if unlabeled:
                # 实时批的 signal_side 是 NULL（只出分数、不出方向），不能当成 0 多 0 空。
                side_txt += f" / 未标方向 {unlabeled}"
            lines.append(
                f"{market}/{source} 最新批次 {run_id}（交易日 {tday}，写入 {str(ts or '—')[:16]}）："
                f"{n} 条｜{side_txt}｜"
                f"平均 fusion {float(avg or 0):+.3f}，最高 {float(mx or 0):+.3f}｜"
                f"模型 {mv or '—'} / 特征 {fv or '—'}"
            )
            cur.execute(
                """
                select distinct on (symbol) symbol, fusion_score, rank_pct, signal_side
                from engine_signal_scores
                where run_id=%s and market=%s and source=%s
                order by symbol, fusion_score desc
                """,
                (run_id, market, source),
            )
            rows = cur.fetchall()
            if rows:
                rows = sorted(rows, key=lambda x: float(x[1] or 0), reverse=True)
                cur.execute(
                    "select max(trade_date) from stock_daily_latest where trade_date <= %s",
                    (d.day,),
                )
                nd = cur.fetchone()[0]
                names = _names_map(cur, nd)
                top = "；".join(
                    f"{_name_of(s, names)}({_suffix(s)}) {float(sc or 0):+.2f}"
                    for s, sc, _rp, _side in rows[:6]
                )
                lines.append(f"  头部：{top}")
                bottom = "；".join(
                    f"{_name_of(s, names)}({_suffix(s)}) {float(sc or 0):+.2f}"
                    for s, sc, _rp, _side in rows[-3:][::-1]
                )
                lines.append(f"  尾部：{bottom}")
        # 与上一交易日对比（仅 CN batch）：每天只取当天**最后一批**。
        # 直接 group by trade_date 会把一天 30 多批一起平均，把「模型变化」洗成噪声。
        # `limit 2` 必须在 CTE 内：不加会为**全部历史交易日**做 distinct（2,200 万行
        # 上 20s 超时，2026-10-30 实测）；加了以后只扫最近两个交易日（~90ms）。
        cur.execute(
            """
            with per_day as (
              select distinct on (trade_date) trade_date, run_id
              from engine_signal_scores
              where market='CN' and source='batch' and trade_date <= %s
              order by trade_date desc, signal_ts desc nulls last
              limit 2
            )
            select p.trade_date,
                   avg(s.fusion_score),
                   count(*) filter (where s.signal_side='BUY')::float / count(*)
            from per_day p
            join engine_signal_scores s
              on s.run_id = p.run_id and s.market='CN' and s.source='batch'
            group by 1 order by 1 desc limit 2
            """,
            (d.day,),
        )
        hist = cur.fetchall()
        if len(hist) >= 2:
            d0, avg0, buy0 = hist[0]
            d1, avg1, buy1 = hist[1]
            lines.append(
                f"较上一批（{d0} 最后一批 vs {d1} 最后一批）："
                f"平均 fusion {float(avg0 or 0) - float(avg1 or 0):+.3f}，"
                f"BUY 占比 {(float(buy0 or 0) - float(buy1 or 0)) * 100:+.1f}pp"
            )
    except Exception as exc:  # noqa: BLE001
        d.gaps.append("signals_unreadable")
        lines.append(f"模型分数不可读：{type(exc).__name__}")
    d.sections["models"] = "\n".join(lines)


def collect_experience(d: Dossier, env: dict[str, str], conn) -> None:
    lines: list[str] = []
    mem = agent_data_root() / "market_memory.md"
    try:
        text = mem.read_text(encoding="utf-8")
        for title in ("策略心得", "失败教训", "待改进"):
            body = extract_section(text, (title,))
            if not body:
                continue
            items = [
                ln.strip() for ln in body.splitlines() if ln.strip().startswith("-")
            ]
            if items:
                lines.append(f"【长期记忆·{title}】")
                lines.extend(items[-MEMORY_TAIL_ITEMS:])
    except OSError:
        d.gaps.append("market_memory_missing")
    # 近 3 日总控报告的经验段
    try:
        end = date.fromisoformat(d.day)
        for back in range(1, 4):
            day = (end - timedelta(days=back)).isoformat()
            report = read_report(day, "night") or read_report(day, "news")
            body = extract_section(report, ("经验", "教训"))
            if body:
                lines.append(f"【{day} 总控经验】{_clip(body, 500)}")
    except ValueError:
        pass
    d.sections["experience"] = "\n".join(lines) if lines else "（暂无积累的经验条目）"


def collect_prior(d: Dossier, env: dict[str, str], conn) -> None:
    from .config import SLOTS

    lines: list[str] = []
    idx = SLOTS.index(d.slot) if d.slot in SLOTS else len(SLOTS) - 1
    for slot in SLOTS[:idx]:
        text = read_report(d.day, slot)
        if text:
            title = {
                "midday": "午间复盘",
                "close": "收盘复盘",
                "inference": "推理数据复盘",
                "news": "新闻复盘",
            }.get(slot, slot)
            lines.append(f"【{title}】\n{_clip(text, 1600)}")
    # 昨晚的最终稿（夜间档在凌晨跑时，这里就是「几小时前」的稿）
    try:
        prev_day = (date.fromisoformat(d.day) - timedelta(days=1)).isoformat()
        prev = read_report(prev_day, "night")
        if prev:
            lines.append(f"【上一交易日 {prev_day} 夜间综合】\n{_clip(prev, 2000)}")
    except ValueError:
        pass
    d.sections["prior"] = "\n\n".join(lines) if lines else "（本日尚无更早档位的复盘）"


# ── 入口 ─────────────────────────────────────────────────────────────


def build_dossier(
    *,
    day: str,
    target: str,
    slot: str,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> Dossier:
    """跑全部采集器。**永不抛**：单段失败只进 ``gaps``。"""
    env = env if env is not None else load_env()
    now = now or datetime.now(CN_TZ)
    d = Dossier(day=day, target=target, slot=slot, now=now)
    conn = None
    try:
        conn = _connect(env)
    except Exception as exc:  # noqa: BLE001
        d.gaps.append("pg_unreachable")
        d.sections["system"] = (
            f"PG 不可达（{type(exc).__name__}）：市场/账本/模型三段降级。"
        )
    collectors = (
        ("system", collect_system),
        ("market", collect_market),
        ("trading", collect_trading),
        ("dialogue", collect_dialogue),
        ("news", collect_news),
        ("models", collect_models),
        ("experience", collect_experience),
        ("prior", collect_prior),
    )
    for _key, fn in collectors:
        try:
            fn(d, env, conn)
        except Exception as exc:  # noqa: BLE001 - 单段失败不拖垮整包
            d.gaps.append(f"collector_failed:{fn.__name__}")
            d.sections.setdefault(_key, f"（采集失败：{type(exc).__name__}: {exc}）")
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return d


def next_trading_day(day: date) -> date:
    """下一交易日（周末跳过；节假日缺口由调度与数据分区兜底）。"""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def resolve_days(
    slot: str, now: datetime | None = None, day: str | None = None
) -> tuple[str, str]:
    """``(复盘日, 目标交易日)``。

    - 显式 ``day`` 优先（``YYYY-MM-DD`` 或 ``YYYYMMDD``）。
    - ``night`` 档在北京时间凌晨跑 → 复盘日 = 昨天；其余档 = 今天。
    - 目标交易日 = 复盘日之后的下一个工作日。
    """
    now = now or datetime.now(CN_TZ)
    if day:
        d = date.fromisoformat(_dash(day))
    elif slot == "night" and now.hour < 8:
        d = now.date() - timedelta(days=1)
    else:
        d = now.date()
    return d.isoformat(), next_trading_day(d).strftime("%Y%m%d")


def brief_for_prompt(target: str, *, limit: int = 2800) -> str:
    """给决策/对话模型看的**紧凑版** brief（完整报告留在报告文件里）。"""
    from .store import brief_meta_path, latest_brief

    meta_path = brief_meta_path(target)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict) or not meta:
        key, md = latest_brief(target)
        if not md:
            return ""
        # 不谎称「最终稿」：文件正文自带状态行（最终稿/阶段稿），照实抬出即可。
        return f"【研究总控·最近复盘与今日要点】（目标交易日 {_dash(key)}）\n" + _clip(
            md, limit
        )
    machine = (meta.get("final") or {}).get("machine") or {}
    body = str((meta.get("final") or {}).get("content") or "")
    if not body:
        latest = None
        sections = meta.get("sections") or {}
        for sec in sections.values():
            if isinstance(sec, dict) and (
                latest is None or str(sec.get("ts")) > str(latest.get("ts"))
            ):
                latest = sec
        body = str((latest or {}).get("content") or "")
        machine = (latest or {}).get("machine") or {}
    if not body:
        return ""
    status = (
        "最终稿"
        if str(meta.get("status") or "") == "final"
        else "阶段稿（夜间综合未生成）"
    )
    lines = [f"【研究总控·{_dash(target)} 交易日前瞻】（{status}）"]
    for key, label in (
        ("direction", "明日方向"),
        ("capital", "资金判断"),
        ("news", "新闻"),
        ("models", "模型分数"),
        ("sectors", "板块"),
        ("risks", "风险"),
    ):
        val = machine.get(key)
        if isinstance(val, list):
            val = "；".join(str(x) for x in val if str(x).strip())
        if val:
            lines.append(f"- {label}：{_clip(val, 400)}")
    lines.append("")
    lines.append(_clip(body, limit))
    return "\n".join(lines)
