#!/usr/bin/env python3
"""休市窗口新闻简报：quantmind 后端聚合 API → 金融相关新闻时间线。

用户口径：新闻从数据库/后端拉（huntly SQLite 采集 + postgres LLM 富化已在
quantmind 侧合流），不现场抓 RSS。

数据源：GET http://127.0.0.1:8000/api/v1/news/articles
  - since/until 为 UTC ISO（内部转上海本地过滤 page.connected_at）
  - folder_id 策展分组：1=政策/央行/统计  2=7x24滚动  3=股票实时市场
    4=实时金融讯息  5=当日财经头条  6=股票市场  7=外汇期货原油（crypto=8 默认排除）
  - 每篇带 source_name + enrichment{tickers,industries,sentiment_label,…}
  - 已知坑（后端 news.py）：keyword 与富化标签过滤组合必 500 → 只用 folder 窗口拉，
    相关度在本地判；page_size≤500，真分页

本地加工：窗口(默认从最近收盘 15:00 起)→ 相关度过滤(标签/标题财经关键词/源名)
→ 精确标题去重 → 按日分组、每日限额 → 紧凑行输出（时间升序，总量 ≤ MAX_ITEMS）。

用法（night_pool_agent 复市前夜自动调用）：
    build_digest("2026-09-04 15:00")          # 北京本地起止；到 now
独立跑：python scripts/news_digest.py --since "2026-09-04 15:00" [--days 3]
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "http://127.0.0.1:8000/api/v1/news/articles"
MAX_ITEMS = 160           # 进模型总行数上限
FIN_FOLDERS = (1, 2, 3, 4, 5, 6, 7)   # 政策 + 金融六件套（2-7），crypto(8)/综合(9)排除
# 标题财经关键词兜底（纯宏观/汇率/期货/政策而无公司标签的新闻）
FIN_KEYWORDS = (
    "央行|降准|降息|加息|利率|流动性|美联储|鲍威尔|欧央行|日本央行|缩表|MLF|LPR|逆回购|"
    "汇率|人民币|美元指数|离岸|在岸|外汇|结汇|北向|南向|外资|关税|贸易战|出口管制|"
    "原油|布油|WTI|黄金|白银|铜价|伦铜|COMEX|期货|大宗商品|铁矿石|焦煤|"
    "证监会|交易所|IPO|注册制|并购|重组|定增|减持|回购|分红|退市|量化|两融|融资融券|"
    "财报|业绩|净利润|营收|预增|预减|涨停|跌停|龙虎榜|连板|板块|概念|主线|景气|"
    "半导体|芯片|光刻|算力|CPO|存储|AI应用|机器人|低空|卫星|军工|稀土|"
    "新能源|锂电|光伏|储能|氢能|电力|汽车|智能驾驶|华为|苹果|英伟达|台积电|"
    "医药|创新药|疫苗|地产|基建|消费|白酒|家电|旅游|航空|航运|物流|"
    "CPI|PPI|PMI|社融|M2|GDP|经济数据|就业数据|非农|通胀|通缩|国常会|政治局|"
    # 全球市场/货币政策对 A 股走势的传导（股票相关全球新闻）
    "美债|收益率|美股|标普|纳指|道指|费半|欧股|日经|恒指|港股|AH股|沪指|深成指|创业板指|"
    "发改委|财政部|商务部|工信部|住建部|国务院|国新办|白宫|美联储议息|"
    "俄乌|中东|伊朗|以色列|红海|地缘|制裁|出口限制|科技股|纳指100"
)
# 外围货币政策/资产主线（影响 A 股走势的全球宏观）：命中即显著提权，避免被公司事件淹没
GLOBAL_KW = (
    "美联储", "加息", "降息", "非农", "美债", "收益率", "美股", "标普", "纳指", "道指",
    "欧央行", "日本央行", "CPI", "PPI", "PMI", "关税", "地缘", "俄乌", "中东", "伊朗",
    "原油", "黄金", "汇率", "人民币", "美元指数", "FOMC",
)
# 强信号中文源名（提升排序）
FIN_SOURCES = ("财联社", "华尔街见闻", "格隆汇", "金十", "汇通", "同花顺", "新浪财经",
               "财新", "智通", "路透", "彭博", "界面", "富途", "央行", "证监会", "统计局",
               "万得", "雪球", "集思录", "快讯", "电报", "涨停")


_REPO_ROOT = Path(__file__).resolve().parents[1]
# 与 backend/shared/auth.py 同名单：公开默认值一律视为未配置（fail-closed）
_PUBLIC_INTERNAL_DEFAULTS = {
    "changeme-internal-secret", "dev-internal-call-secret", "quantmind-internal-secret",
}


def _read_env_value(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _internal_headers() -> dict:
    """内部调用请求头（X-Internal-Call + X-User-Id）。

    news 路由 2026-10-07 起业务端点要求登录态；本脚本是 keeper 内部调用方，
    与 backend/shared/auth.get_internal_call_secret 同优先级取密钥：
    runtime.env（权威；宿主上通常 root-only 读不到）→ .env（compose 注入源，
    实测与容器内生效值一致）→ 环境变量 → 空串（下游 401 = fail-closed）。
    两处密钥若轮换漂移，这里会 401，需随之更新。
    """
    import os

    secret = ""
    for p in (_REPO_ROOT / "config" / "runtime.env", _REPO_ROOT / ".env"):
        v = _read_env_value(p, "INTERNAL_CALL_SECRET")
        if v and v not in _PUBLIC_INTERNAL_DEFAULTS:
            secret = v
            break
    if not secret:
        v = os.getenv("INTERNAL_CALL_SECRET", "").strip()
        if v and v not in _PUBLIC_INTERNAL_DEFAULTS:
            secret = v
    return {
        "X-Internal-Call": secret,
        "X-User-Id": "0",
        "X-Internal-Service": "keeper-news",
    }


def _get(url: str, retry: int = 2) -> dict:
    last: Exception | None = None
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=_internal_headers())
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retry:
                import time
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"news API 不可达: {last}")


def _to_utc_iso(local: str) -> str:
    """北京时间 naive 'YYYY-MM-DD HH:MM' → 'YYYY-MM-DDTHH:MM:00Z'。"""
    return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def fetch_window(folder_id: int, since_utc: str, until_utc: str) -> list[dict]:
    """单 folder 全量翻页（time_asc，直到拉齐 total）。"""
    out, page, seen = [], 1, set()
    while True:
        q = urllib.parse.urlencode({
            "since": since_utc, "until": until_utc, "folder_id": folder_id,
            "page": page, "page_size": 500, "sort": "time_asc"})
        d = _get(f"{API}?{q}")
        arts = d.get("articles") or []
        for a in arts:
            if a.get("id") not in seen:
                seen.add(a["id"])
                out.append(a)
        total = int(d.get("total") or 0)
        if not arts or len(seen) >= total or page >= 50:
            break
        page += 1
    return out


# 噪声源（通用社会新闻桶，标签误伤率高）：仅当标题命中财经关键词才保留
NOISY_SOURCES = ("国内滚动", "全球新闻")


def _relevant(art: dict) -> bool:
    import re
    title = art.get("title") or ""
    src = art.get("source_name") or ""
    title_kw = bool(re.search(FIN_KEYWORDS, title))
    enr = art.get("enrichment") or {}
    if enr.get("industries") or enr.get("tickers"):
        if any(n in src for n in NOISY_SOURCES) and not title_kw:
            return False          # 标签误伤（路况/社会新闻带公司代码）→ 需标题佐证
        return True
    return title_kw


def _score(art: dict) -> int:
    enr = art.get("enrichment") or {}
    s = 0
    if enr.get("industries"):
        s += 25
    if enr.get("tickers"):
        s += 20
    src = art.get("source_name") or ""
    for k in FIN_SOURCES:
        if k in src:
            s += 15
            break
    import re
    if re.search(FIN_KEYWORDS, art.get("title") or ""):
        s += 15
    if any(k in (art.get("title") or "") for k in GLOBAL_KW):
        s += 30          # 外围货币政策/资产主线提权
    return s


def _bz(iso: str) -> str:
    """published_at(UTC ISO) → 北京 'MM-DD HH:MM'。"""
    return _to_bj(iso)[5:16] if iso else ""


def _to_bj(iso: str) -> str:
    """published_at(UTC ISO) → 北京本地 'YYYY-MM-DD HH:MM'。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


# 事件动词表（聚类键用）：同公司 + 同事件 = 一条代表，跨事件不误并
_EVENT_VERBS = ("立案", "回购", "增持", "减持", "质押", "解禁", "中标", "签约", "获批",
                "获准", "注册", "重组", "收购", "并购", "定增", "发债", "退市", "处罚",
                "罚没", "问询", "警示", "调查", "诉讼", "预增", "预减", "预亏", "分红",
                "派息", "停牌", "复牌", "增发", "涨停", "跌停", "涨停", "涨价", "降价")


def _norm_key(a: dict) -> str:
    """同源事件聚类键。

    7x24 多源转载同一事件的标题仅尾部/来源措辞不同（代码开头 vs 公司名开头），
    用「首标代码 + 事件动词」聚类：康欣新材各种"被立案"变体 → 一条；
    同公司同日不同事件（立案 vs 回购）动词不同 → 各自保留。
    纯宏观无代码行退回标题前缀 20 字符去重。
    """
    import re
    title = re.sub(r"[^一-鿿A-Za-z0-9]", "", a.get("title") or "")
    tk = (a.get("enrichment") or {}).get("tickers") or []
    tk0 = re.sub(r"[.\-]", "", str(tk[0])) if tk else ""
    if tk0:
        verb = next((v for v in _EVENT_VERBS if v in title), "")
        return f"{tk0}|{verb}" if verb else f"{tk0}|{title[:20]}"
    return f"|{title[:20]}"


def build_digest(since_local: str, until_dt: datetime | None = None,
                 max_items: int = MAX_ITEMS, verbose: bool = False) -> str:
    """返回按日分组新闻简报 markdown（时间升序）；失败抛异常由调用方降级。"""
    since_dt = datetime.strptime(since_local, "%Y-%m-%d %H:%M")
    until_dt = until_dt or datetime.now()
    if until_dt <= since_dt:
        return "（窗口为空）"
    days = max(1, (until_dt.date() - since_dt.date()).days + 1)
    day_cap = max(20, max_items // days)   # 均分额度，保证整个窗口（含周末）都有覆盖
    since_utc = _to_utc_iso(since_local)
    until_utc = until_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    picked: list[tuple[int, dict]] = []
    raw_n = 0
    for fid in FIN_FOLDERS:
        arts = fetch_window(fid, since_utc, until_utc)
        raw_n += len(arts)
        for a in arts:
            if _relevant(a):
                picked.append((_score(a), a))
    # 语义去重：同标代码/标题主干只保留分最高的一条（7x24 多源转载极多）
    seen_key, dedup = set(), []
    for s, a in sorted(picked, key=lambda x: (-x[0], x[1].get("published_at") or "")):
        k = _norm_key(a)
        if k in seen_key:
            continue
        seen_key.add(k)
        dedup.append((s, a))
    per_day: dict[str, list] = {}
    for s, a in dedup:
        day = _to_bj(a.get("published_at") or "")[:10]   # 按北京时间分组
        per_day.setdefault(day, []).append((s, a))

    lines = [f"## 休市窗口新闻简报（{since_dt:%m-%d %H:%M} 北京 ~ 今，"
             f"金融相关 {len(dedup)}/{raw_n} 条，每日限额 {day_cap}）"]
    n_used = 0
    for day in sorted(per_day):
        lines.append(f"\n### {day}")
        shown = 0
        for s, a in per_day[day]:
            if shown >= day_cap or n_used >= max_items:
                break
            enr = a.get("enrichment") or {}
            sent = {"bullish": "多", "bearish": "空"}.get(enr.get("sentiment_label") or "")
            tk = enr.get("tickers") or []
            tag = (f"｜情绪:{sent}" if sent else "") + (f"｜{','.join(str(x) for x in tk[:4])}" if tk else "")
            src = a.get("source_name") or ""
            lines.append(f"- {_bz(a.get('published_at') or '')} [{src}]{tag} {(a.get('title') or '')[:90]}")
            shown += 1
            n_used += 1
        rest = len(per_day[day]) - shown
        if rest > 0:
            lines.append(f"  …当日另有 {rest} 条同类未列出")
    if verbose:
        print("\n".join(lines))
    return "\n".join(lines)


def cli() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="",
                    help="北京本地 'YYYY-MM-DD HH:MM'（默认 N 天前 15:00）")
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    since = a.since or (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d 15:00")
    try:
        print(build_digest(since, verbose=True))
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
