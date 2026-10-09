#!/usr/bin/env python3
"""从新闻库（Huntly RSS 聚合）扫「黑名单候选」：立案/造假/处罚/解禁等 → 代码清单。

背景（2026-09-11 用户口径）：「从今年的 rss 新闻里面找代码，那些黑名单公司、
不好的公司，都给列出来」。已有的 risk_list 只吃 **近期** 新闻（news_days=3）与
解禁数据——今年以来的历史负面事件没有沉淀，除非正在窗口内，否则不会进买入闸门。

为什么要直读 SQLite 而不是走 quantmind 的 /api/v1/news/articles：
  实测该接口在带 keyword 时走的是**富化（enrichment）索引**，而富化只覆盖
  2026-09-07 之后的文章；`since` 参数又只用于裁剪候选 ID（上限 5 万），
  最终 matched_total 上限 5000。结果：『立案』全年真实命中 4071 篇，
  接口只返回 106 篇（2.6%）。做全年扫描必须直读库。

为什么先做快照再扫：
  Huntly 边写边读，直接用 `immutable=1` 读会读到撕裂的页（实测报
  `database disk image is malformed`）。先 `sqlite3.backup()` 出**一致性快照**
  （3 秒）再读，既不与写锁抢，也不会读到半截文件。
  单遍流式扫全年 57 万行 + 关键词/名称匹配 ≈ 6 秒（按关键词逐个 LIKE 要 230 秒/词）。

做法：
  1. 快照 → 单遍流式读 (title, description, connected_at, connector_id)；
  2. 文章 → 代码两条路：富化 tickers（新文章有）+ 标题/摘要里匹配全市场名称表
     （quantdb instrument_detail，含 ST 标记）；
  3. **就近判据**降噪：公司名与关键词要挨得近（NEAR_CHARS 内）才算数——
     否则「证监会处罚 5 家券商……中金公司也在列」这类行业综述会把无关票拉进来；
  4. 按代码聚合：命中类别、条数、时间跨度、样例标题 → JSON + 可读清单。

辅助工具，不接交易路径：产物是**人工复核清单**（是否拉黑由人定，或由
`configs/live_symbols.json` 的 block_buy / risk 段消费）。**自动**进黑名单的是
risk_list 那条链路（news_brief 每次跑都重建，见 risk_list.refresh）。

用法：
  python scripts/news_blacklist_scan.py --since 2026-01-01
  python scripts/news_blacklist_scan.py --since 2026-01-01 --out data/news_blacklist_2026.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Huntly 的 page.connected_at 存的是**上海本地**时间串 'YYYY-MM-DD HH:MM:SS.fff'
HUNTLY_DB = Path(os.environ.get("HUNTLY_SQLITE_PATH",
                                "/home/zbox/projects/quantmind/data/huntly/db.sqlite"))
INSTRUMENT = ("/home/zbox/projects/quantmind/data/quantdb/2_base_sector/"
              "instrument_detail/instrument_detail.parquet")

# 类别 → 关键词。顺序即**榜单排序**（严重度）：
# 立案/造假 > 交易违规 > 退市 > 处罚 > 违规 > 解禁。
# 解禁排最后不是因为它不空——用户口径「还有个解禁的也是超级利空」，它在
# risk_list 里是买入硬拦（解禁前后双向窗口）；这里排尾是因为它**定时事件**属性
# 重：健康的公司到点也会解禁，而这份榜单要的是"公司本身有问题"。
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("立案", ("立案",)),
    ("造假", ("造假", "虚增", "财务舞弊")),
    ("交易违规", ("内幕交易", "操纵市场", "操纵股价")),
    ("退市", ("退市",)),
    ("处罚", ("处罚", "被罚", "警示函", "监管函", "问询函")),
    ("违规", ("违规", "失信", "冻结", "被执行")),
    ("解禁", ("解禁", "限售股上市流通")),
]
NEAR_CHARS = 40        # 公司名与关键词的距离阈值（超出不算"当事方"，多半是行业综述）
BJ = timezone(timedelta(hours=8))


def snapshot(db: Path = HUNTLY_DB) -> str:
    """一致性快照路径（调方负责删除）。Huntly 在写，直读会读到撕裂页。"""
    fd, tmp = tempfile.mkstemp(prefix="huntly_snap_", suffix=".sqlite")
    os.close(fd)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return tmp


def load_name_index(path: Path | str = INSTRUMENT) -> tuple[dict, dict]:
    """全市场（名称 → [代码]）+（代码 → 名称，含 ST/*ST 原样，报告展示用）。

    名称去掉 ST/*ST 前缀再入索引：新闻里可能写「康佳」也可能写「*ST康佳」。
    """
    import duckdb

    con = duckdb.connect()
    df = con.execute(
        f"SELECT Symbol, Name FROM read_parquet('{path}') "
        f"WHERE Symbol LIKE '%.SH' OR Symbol LIKE '%.SZ' OR Symbol LIKE '%.BJ'").df()
    idx: dict[str, list[str]] = defaultdict(list)
    by_code: dict[str, str] = {}
    for sym, name in zip(df["Symbol"], df["Name"]):
        code, n = str(sym), str(name or "").strip()
        by_code[code] = n
        key = re.sub(r"^\*?ST|^S\*?ST|退$", "", n).strip()   # 去风险警示前缀/后缀
        if len(key) >= 2:
            idx[key].append(code)
    return dict(idx), by_code


def load_source_tokens(db: str) -> set:
    """媒体/数据源名集合（connector.name + source.site_name 切词）。

    为什么必须剔除：上市券商/资讯商本身就是 RSS 源，其名号出现在**每篇**稿子里
    （标题后缀「- 东方财富」、正文「同花顺财经讯」）。不清掉，东方财富/同花顺/
    新华网这些会被推成榜首（实测 2684/1045 条），而它们绝大多数是"新闻出处"
    而非"当事方"。代价：真的写「东方财富遭处罚」这类稿子会被漏掉——对一份
    **人工复核清单**可以接受。
    """
    con = sqlite3.connect(db)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM connector WHERE name IS NOT NULL")]
        names += [r[0] for r in con.execute("SELECT site_name FROM source WHERE site_name IS NOT NULL")]
    finally:
        con.close()
    toks: set = set()
    for n in names:
        for part in re.split(r"[-_|·，,、\s]+", str(n)):
            p = part.strip("《》\"'（）()【】[]")
            if len(p) >= 2:
                toks.add(p)
    # 单字/泛词剔除，避免把正文里正常用词一起洗掉
    return {t for t in toks if t not in ("新闻", "财经", "快讯", "最新", "数据", "频道",
                                         "首页", "文章", "资讯", "头条", "栏目")}


def sanitize(text: str, src_tokens: set) -> str:
    """抹掉出处痕迹：来源名 + 标题尾部「 - 出处」后缀。"""
    text = re.sub(r"\s*[-–—_|]\s*[^-–—_|]{1,24}$", "", text.strip())
    for t in src_tokens:
        if t in text:
            text = text.replace(t, "　")
    return text


def build_name_re(idx: dict):
    """全市场名称 → 单个 alternation 正则（长名在前 = 每处取最长匹配）。

    对 57 万篇逐篇跑 5000 次 `in` 是 29 亿次子串比较，跑不动。合并成一个
    正则交给 C 层自动机；再配合"先筛关键词"（全年只有 1.2 万篇命中，
    占 2%），名称匹配只在这 2% 上跑。
    """
    names = sorted(idx, key=len, reverse=True)
    return re.compile("|".join(re.escape(n) for n in names))


def match_names(text: str, pattern, idx: dict) -> list[str]:
    """文本里命中的代码（正则长名优先，天然不重叠）。"""
    return [c for m in pattern.finditer(text) for c in idx.get(m.group(0), ())]


def _near(text: str, name: str, kw: str, window: int = NEAR_CHARS) -> bool:
    """公司名与关键词是否在 window 字符内共现过（判定"当事方"而非"被提及"）。

    行业综述（"证监会处罚 5 家券商，中信、中金均在列"）里，公司名与关键词可能
    隔了几百字；当事方公告（"XX 因涉嫌信披违规被立案"）通常紧挨着。
    """
    npos = [m.start() for m in re.finditer(re.escape(name), text)]
    kpos = [m.start() for m in re.finditer(re.escape(kw), text)]
    if not npos or not kpos:
        return False
    return any(abs(a - b) <= window for a in npos for b in kpos)


def iter_articles(db: str, since: str, until: str):
    """单遍流式产出年内文章（只读三列，不碰 content 大字段——省几个数量级 IO）。

    不要按 content_type 过滤：实测年内 578337 行里 content_type 全为 NULL
    （只有 1 行是 0），加了过滤等于扫空气。
    """
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "SELECT title, description, connected_at FROM page "
            "WHERE connected_at >= ? AND connected_at < ?",
            (f"{since} 00:00:00.000", f"{until} 23:59:59.999"))
        for title, desc, ts in cur:
            yield title or "", desc or "", str(ts or "")[:10]
    finally:
        con.close()


def scan(since: str, until: str, idx: dict, db: str | None = None) -> dict:
    """扫全量 → {code: {name, cats, near, n, near_n, first, last, samples}}。"""
    per_code: dict = defaultdict(lambda: {"cats": set(), "titles": [], "near_cats": set(),
                                          "first": "", "last": "", "n": 0, "near_n": 0})
    name_of = {c: n for n, codes in idx.items() for c in codes}   # 代码 → 短名
    pattern = build_name_re(idx)
    tmp = db or snapshot()
    seen_titles: set = set()        # 同一头条被多家源重复转载，只计一次
    try:
        src_tokens = load_source_tokens(tmp)
        for raw_title, desc, day in iter_articles(tmp, since, until):
            raw = f"{raw_title} {desc}"
            # 先做便宜的关键词筛（全年只有 ~2% 命中）；洗出处（上千个源名逐个替换）
            # 和名称匹配都放在这道闸之后，否则 57 万 × 上千次子串比较跑不动。
            if not any(k in raw for _, kws in CATEGORIES for k in kws):
                continue
            # 名称只在**标题**上匹配，不看 description：Google News 的 description
            # 是「相关报道合集」，里面每条都带着自己的出处（… 东方财富 … 新浪网 …
            # 同花顺财经 … Sohu），拿它匹配会把出处的东家全捞进来（实测东方财富
            # 2684 条、同花顺 1045 条，绝大多数是"新闻出处"不是"当事方"）。
            # 当事公司几乎总在标题里，标题够用。
            title = sanitize(raw_title, src_tokens)
            hits = [(cat, kw) for cat, kws in CATEGORIES for kw in kws if kw in raw]
            if not hits:
                continue
            codes = match_names(title, pattern, idx)
            if not codes:
                continue
            key = title[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            for cat, hit_kw in hits:
                for c in codes:
                    e = per_code[c]
                    e["cats"].add(cat)
                    e["n"] += 1
                    e["first"] = min(e["first"] or day, day)
                    e["last"] = max(e["last"] or day, day)
                    # 就近判定用**去掉 ST 前缀的短名**（新闻里一般不带 *ST）
                    name = name_of.get(c, "")
                    if name and _near(title, name, hit_kw, NEAR_CHARS):
                        e["near_cats"].add(cat)
                        e["near_n"] += 1
                    if len(e["titles"]) < 3:
                        e["titles"].append(f"{day} {raw_title[:90]}")
    finally:
        if db is None:
            Path(tmp).unlink(missing_ok=True)
    return per_code


def collect_evidence(since: str, until: str, idx: dict, want: set,
                     db: str | None = None) -> dict:
    """对指定代码收集**全部**命中标题 → {code: [(日, 标题, 类别, 是否就近)]}。

    为什么要这个：榜单只给条数，看不到"是哪几篇稿子把它拉进来的"。实测
    光看条数会误伤——短名撞词（*ST动力 去掉前缀只剩"动力"，撞上"蛋白质动力学"）、
    正面稿被别的关键词带进来（业绩预增早报）。进 block_buy 是**永久**禁买，
    必须逐只核证据。

    类别与就近**都取标题里的关键词**：标题才是当事方陈述。曾经按 CATEGORIES
    顺序取 raw（标题+描述）里第一个命中的词，再拿它在标题里判就近——描述里
    先蹦出「退市」时，标题里明明白白的「行政处罚事先告知书」会被标成非就近
    （*ST数源 实测踩到，真阳性被误当疑点）。
    """
    name_of = {c: n for n, codes in idx.items() for c in codes}
    pattern = build_name_re(idx)
    out: dict = defaultdict(list)
    tmp = db or snapshot()
    try:
        src_tokens = load_source_tokens(tmp)
        for raw_title, desc, day in iter_articles(tmp, since, until):
            raw = f"{raw_title} {desc}"
            if not any(k in raw for _, kws in CATEGORIES for k in kws):
                continue
            title = sanitize(raw_title, src_tokens)
            for c in match_names(title, pattern, idx):
                if c not in want:
                    continue
                name = name_of.get(c, "")
                hit = next(((cat, k) for cat, kws in CATEGORIES
                            for k in kws if k in title), None)
                if hit:
                    cat, kw = hit
                    near = bool(name) and _near(title, name, kw, NEAR_CHARS)
                else:
                    # 关键词只在 description（相关报道合集）里：仍记录，但类别
                    # 不可信，且一律不算就近——这份清单宁可漏，不可误。
                    cat = next((cat for cat, kws in CATEGORIES
                                if any(k in raw for k in kws)), "?")
                    near = False
                out[c].append((day, raw_title, cat, near))
    finally:
        if db is None:
            Path(tmp).unlink(missing_ok=True)
    return out


def _rank(cats: set) -> int:
    order = [c for c, _ in CATEGORIES]
    return min((order.index(c) for c in cats if c in order), default=len(order))


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻库黑名单候选扫描（直读 Huntly SQLite）")
    ap.add_argument("--since", default="2026-01-01", help="起始日 YYYY-MM-DD")
    ap.add_argument("--until", default=datetime.now(BJ).strftime("%Y-%m-%d"),
                    help="截止日 YYYY-MM-DD")
    ap.add_argument("--out", default="", help="JSON 输出路径（默认只打印）")
    ap.add_argument("--top", type=int, default=0, help="只打印前 N 只")
    ap.add_argument("--db", default="", help="已备好的快照路径（省去重复备份）")
    ap.add_argument("--evidence", default="",
                    help="逗号分隔代码：逐条打印命中标题（进 block_buy 前核证据用）")
    args = ap.parse_args()

    print(f"📰 扫描 {args.since} → {args.until}（Huntly 快照）…", file=sys.stderr)
    idx, by_code = load_name_index()
    print(f"   名称表 {len(idx)} 条", file=sys.stderr)

    if args.evidence:
        want = {c.strip().upper() for c in args.evidence.split(",") if c.strip()}
        ev = collect_evidence(args.since, args.until, idx, want, args.db or None)
        for c in sorted(want):
            rows = ev.get(c, [])
            print(f"\n== {c} {by_code.get(c, '')}：{len(rows)} 条")
            for day, title, cat, near in sorted(rows)[:25]:
                print(f"   {day} [{cat}]{'★' if near else ' '} {title[:88]}")
        return 0

    per_code = scan(args.since, args.until, idx, args.db or None)

    rows = sorted(per_code.items(),
                  key=lambda kv: (_rank(kv[1]["cats"]), -kv[1]["near_n"], -kv[1]["n"]))
    out = [{"code": c, "name": by_code.get(c, ""), "cats": sorted(e["cats"]),
            "near_cats": sorted(e["near_cats"]), "n": e["n"], "near_n": e["near_n"],
            "first": e["first"], "last": e["last"], "samples": e["titles"]}
           for c, e in rows]
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"since": args.since, "until": args.until,
             "generated_at": datetime.now(BJ).isoformat(), "items": out},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"💾 {args.out}", file=sys.stderr)
    show = out[: args.top] if args.top else out
    print(f"\n共 {len(out)} 只候选（近=N 公司名与关键词就近共现的条数，越高质量越高）：")
    for r in show:
        print(f"{r['code']:<11}{r['name']:<10}{'/'.join(r['cats']):<12}"
              f"{r['n']:>4} 条(近 {r['near_n']:>3})  {r['first']}~{r['last']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
