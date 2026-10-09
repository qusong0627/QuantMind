#!/usr/bin/env python3
"""新闻 Agent 管线（A股实盘，全 deepseek-v4-flash）：RSS 新闻 → 金融相关筛选
→ 大方向/小方向/持仓情报 → 主编「新闻分子」简报。

数据源：quantmind 后端聚合 API（Huntly RSS + enrichment 标签：tickers/industries/
event_tags/sentiment，正文级规则抽取）。本管线把标签当证据喂各段，不重复读正文。
源分层依据：2026-09-07 一周 19,238 篇实证（详见模块内分层常量注释）。

五段流水线（每段独立 try/超时，失败跳段沿用上一版，绝不空手）：
  ① news-gate     新闻门卫：标题+标签证据 → 金融相关? 粗分 macro/micro/holdings
  ② news-macro    宏观政策研究（大方向）：宏观/政策/监管/外围 → 大盘方向分+主线+风险
  ③ news-micro    板块个股情报（小方向）：行业/概念/公司事件 → 热点主题+事件清单
  ④ news-holdings 持仓情报（重点）：只吃命中持仓/关注池的条目 → 利好/利空/幅度/时效
  ⑤ news-chief    新闻主编：②③④ → 「新闻分子」latest.json（交易/市场研究唯一入口）

窗口语义（用户口径）：不重复历史——每轮只分析「上次成功分析结束 → 现在」的新增；
游标存 data/news_brief/state.json；超过 MAX_WINDOW_HOURS 自动截断。

触发 cron（北京时刻 → JST+1h 写）：
  09:25 盘前(隔夜+早间公告)    25 10 * * 1-5
  09:55/10:55/12:55/13:55 盘中 55 10,11,13,14 * * 1-5   # 整点交易分析前 5 分钟
  19:05 收盘当日全景           5 20 * * 1-5  # 夜池 19:30 消费 fresh 分子
手动：python scripts/news_brief.py [--since 'YYYY-MM-DD HH:MM'] [--stage ...] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from news_digest import (  # noqa: E402
    FIN_KEYWORDS,
    GLOBAL_KW,
    NOISY_SOURCES,
    _internal_headers,
    _norm_key,
    _to_bj,
)
from trading_cal import is_trading_day  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
MODEL = "deepseek-v4-flash"
API = "http://127.0.0.1:8000/api/v1/news/articles"
FETCH_FOLDERS = list(range(1, 10))     # Huntly 订阅桶（源分层在本地做）
MAX_WINDOW_HOURS = 72                  # 增量兜底上限（覆盖周五晚→周一早盘前）
MAX_TITLES = 400                       # 单轮进门卫上限（按新鲜度截断）
MAX_PAGES_PER_FOLDER = 4               # 单桶翻页上限（防深偏移 502）

STATE_FILE = ROOT / "data" / "news_brief" / "state.json"
BRIEF_FILE = ROOT / "data" / "news_brief" / "latest.json"
HISTORY_FILE = ROOT / "data" / "news_brief" / "history.jsonl"
WATCH_FILE = ROOT / "data" / "news_brief" / "watch_codes.json"
LESSONS_FILE = ROOT / "data" / "news_brief" / "lessons.json"  # 晚间复盘经验（news_review 维护）
INTRA_WATCH_FILE = ROOT / "data" / "news_brief" / "intraday_watch.json"  # 盘中异动关注（L2 轮询消费）
INTRA_WATCH_TTL_MIN = 60   # 异动关注冷却：60 分钟后过期
INTRA_WATCH_MAX = 20       # 上限（新优先）

# signature = 落盘目录名（data/agent_data_astock/{sig}/log/... → 前端直接复用）
GATE, MACRO, MICRO, HOLD, CHIEF = ("news-gate", "news-macro", "news-micro",
                                   "news-holdings", "news-chief")
AGENT_CN = {GATE: "新闻门卫", MACRO: "宏观政策研究", MICRO: "板块个股情报",
            HOLD: "持仓情报", CHIEF: "新闻主编"}
ALL_STAGES = (GATE, MACRO, MICRO, HOLD, CHIEF)

# ---- 源分层（2026-09-07 一周 19,238 篇实证） ----
# S=核心快讯源（快讯准/富化率高，全量进门卫） A=高量需门卫去噪 B=外围/未知 X=实证排除
_TIER_S = ("财联社", "格隆汇", "华尔街见闻", "智通财经", "财新", "金十",
           "⚡️ 7x24投资快讯", "同花顺", "万得", "雪球", "界面", "富途", "36氪")
_TIER_A = ("7x24", "实时快讯", "看盘", "电报", "时讯", "Telegram Channel")
_TIER_X = ("World - Latest - Google News", "新浪财经－国内滚动", "澎湃新闻 - 首页头条",
           "中国话题", "国内滚动", "全球新闻", "中央社",
           # Crypto/Web3（与 A股 主线无关；要独立通道再开）
           "Crypto", "CoinDesk", "Cointelegraph", "链捕手", "TechFlow", "登链",
           "Web3", "吴说", "白话区块链", "虚拟货币", "敏感经济信息")
_GOOGLE_ALERT_OK = ("证监会", "汇率", "商务部", "海关总署", "央行", "发改委", "工信部")
_FIN_RE = re.compile(FIN_KEYWORDS)

_TIER_ORDER = {"S": 0, "A": 1, "B": 2, "X": 3}


def source_tier(src: str) -> str:
    if any(k in src for k in _TIER_X):
        return "X"
    if "Google" in src or "Alert" in src:
        return "B" if any(k in src for k in _GOOGLE_ALERT_OK) else "X"
    if any(k in src for k in _TIER_S):
        return "S"
    if any(k in src for k in _TIER_A):
        return "A"
    return "B"


def pick_relevant(art: dict) -> bool:
    """金融相关规则减载（进门卫前挡 ~85% 噪声，各层统一）：
    正文级标签证据（tickers/industries/event_tags/key_terms）或标题财经词
    命中即保留；排除桶（社会/crypto）必须标题财经词佐证（防"路况新闻带公司
    代码"误伤）。判定权仍在门卫 agent——这里只做量级减载。"""
    src = str(art.get("source_name") or "")
    title = str(art.get("title") or "")
    tier = source_tier(src)
    if tier == "X":
        return bool(_FIN_RE.search(title) and not any(n in src for n in NOISY_SOURCES))
    enr = art.get("enrichment") or {}
    if enr.get("tickers") or enr.get("industries") or enr.get("event_tags") or enr.get("key_terms"):
        return True
    return bool(_FIN_RE.search(title))


def dedup_articles(arts: list) -> list:
    """事件聚类去重（news_digest 同口径）：同标的同事件多源转发 → 一条。
    优先保留带 ticker 证据的变体；其余稳定保留首条。"""
    seen, out = set(), []
    for a in sorted(arts, key=lambda x: -int(bool((x.get("enrichment") or {}).get("tickers")))):
        k = _norm_key(a)
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def _hits_watch(a: dict, watch_codes: "set | dict") -> bool:
    """确定性持仓命中：enrichment.tickers 命中关注代码，或标题点名关注公司名。
    2026-09-08 实录：全天原始标题 0 次出现关注代码/名称（万华/福恩等小票上不了
    快讯标题），门卫 LLM 无素材可判 → holdings 分流全天恒 0。代码/名称级命中
    不再依赖门卫（与 enrichment 同级的最强信号），直接进 holdings。"""
    keys = set(watch_codes) if isinstance(watch_codes, dict) else set(watch_codes)
    tk = set((a.get("enrichment") or {}).get("tickers") or [])
    if tk & keys:
        return True
    title = str(a.get("title") or "")
    names = set(watch_codes.values()) if isinstance(watch_codes, dict) else set()
    return any(n and n in title for n in names)


def split_holdings_related(arts: list, watch_codes: "set | dict") -> tuple[list, list]:
    """规则保底分流：命中关注代码或公司名 → holdings；其余 → 其他。
    watch_codes 兼容 set 与 {code: name} dict（dict 按键判定）。"""
    h, other = [], []
    for a in arts:
        (h if _hits_watch(a, watch_codes) else other).append(a)
    return h, other


_GLOBAL_RE = re.compile("|".join(GLOBAL_KW))


def rule_direction(art: dict, watch_codes: "set | dict") -> str:
    """门卫降级时的规则方向：命中关注代码/公司名 → holdings；外围/宏观词 → macro；
    其余 → micro。只用于门卫 LLM 失败时的兜底（宁粗勿丢）。"""
    if _hits_watch(art, watch_codes):
        return "holdings"
    title = str(art.get("title") or "")
    if _GLOBAL_RE.search(title):
        return "macro"
    return "micro"


def route_by_gate(arts: list, rel_idx: dict,
                  watch_codes: "set | dict") -> tuple[list, list, list, int]:
    """门卫例外表 → (macro, micro, holdings, 剔除数)。

    黑名单式语义（2026-09-08）：rel_idx 只含例外项——`macro`/`holdings` 改向、
    `skip` 剔除；未列出的条目一律「保留 + micro」。剔除项真正不进下游（此前
    被兜底塞回 micro，既白烧 token 又推高 micro 段截断率）。
    """
    skip_i = {i for i, d in rel_idx.items() if d == "skip"}
    g_macro = [a for i, a in enumerate(arts) if rel_idx.get(i) == "macro"]
    g_micro = [a for i, a in enumerate(arts)
               if i not in skip_i and rel_idx.get(i, "micro") == "micro"]
    # 确定性持仓命中直达 holdings（标题点名关注公司/代码时不依赖门卫判断）
    h_art = [a for i, a in enumerate(arts)
             if rel_idx.get(i) == "holdings" or _hits_watch(a, watch_codes)]
    return g_macro, g_micro, h_art, len(skip_i)


# ---------- 时间与状态 ----------

def _iso(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=CN_TZ)
    except (ValueError, TypeError):
        return None


def _bj_fmt(d: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
    return d.astimezone(CN_TZ).strftime(fmt) if d else ""


def load_state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name("state.json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        pass


def state_after_run(st: dict, stages, until_iso: str, *, advance: bool = True,
                    skip_reason: str = "", **extra) -> dict:
    """状态合并：**只有跑满五段、无桶失败、且主编有产出才推进 last_end 游标**。

    `--stage X` 单跑（手动调试/补段）此前也推进游标 → 这段新闻对后续正式轮
    永久跳过。游标是「完整一轮的消费位」，单段跑不该动它。

    advance=False（本窗口有桶失败，或主编截断/失败无产出）：同样不推进——
    失败窗口没取全/没成刊，推进游标会把这批新闻**永久跳过**
    （2026-09-18 实录：主机冻结致新闻 API 不可达、9 桶失败，空窗路径照推
    last_end → 09:38-10:21 窗口新闻静默丢失；同日发现主编失败轮也在推进 →
    该窗口内容同样丢失且 last_end 假装健康、停更告警看不出）。
    下轮从旧游标重取；窗口内已处理过的条目重复进入属可接受代价（少一条新闻
    是不可逆的，多跑一遍 gate 是可逆的）——取舍方向：宁可重复，不可丢失。
    """
    out = {**st, **extra}
    if tuple(stages) == ALL_STAGES and advance:
        out["last_end"] = until_iso
    elif not advance:
        # 原因由调用方给出（桶失败 / 主编无产出）——写死"有桶失败"会让"仅主编失败"
        # 的排查往错误方向找（2026-09-18 评审 L-1）
        print(f"ℹ️ {skip_reason or '本窗口有桶失败'}，游标不推进（保持 "
              f"{_bj_fmt(_iso(st.get('last_end', '')), '%m-%d %H:%M') or '空'}，下轮重取）")
    else:
        print(f"ℹ️ 单段运行（{'/'.join(stages)}）不推进游标，last_end 保持 "
              f"{_bj_fmt(_iso(st.get('last_end', '')), '%m-%d %H:%M') or '空'}")
    return out


def window_for(last_end: str, now_iso: str) -> tuple[str, str]:
    """增量窗口 (start,end)：从上次成功分析结束起（不重复历史），
    超 MAX_WINDOW_HOURS 自动截断（首跑/长假防爆量）。"""
    now = _iso(now_iso) or datetime.now(CN_TZ)
    start = _iso(last_end) if last_end else None
    if start is None or start >= now:
        start = now - timedelta(hours=MAX_WINDOW_HOURS)
    if (now - start).total_seconds() > MAX_WINDOW_HOURS * 3600:
        start = now - timedelta(hours=MAX_WINDOW_HOURS)
    return start.isoformat(), now.isoformat()


# ---------- 拉取 ----------

def _get_json(url: str, timeout: int = 25) -> dict:
    import urllib.request

    req = urllib.request.Request(url, headers=_internal_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _utc(iso_s: str) -> str:
    d = _iso(iso_s) or datetime.now(CN_TZ)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_incr(since_iso: str, until_iso: str) -> tuple[list, list]:
    """逐桶增量拉取（time_desc 分页，翻到早于 since 即停，单桶 ≤4 页）。
    返回 (articles 新→旧, [(folder, err)])；单桶失败跳过不阻塞（缺口记账）。"""
    since_dt = _iso(since_iso)
    until_u, out, fails = _utc(until_iso), [], []
    for fid in FETCH_FOLDERS:
        try:
            page, done = 1, False
            while page <= MAX_PAGES_PER_FOLDER and not done:
                q = f"since=2000-01-01T00:00:00Z&until={until_u}&folder_id={fid}" \
                    f"&page={page}&page_size=500&sort=time_desc"
                d = _get_json(f"{API}?{q}")
                arts = d.get("articles") or []
                for a in arts:
                    ts = _iso(str(a.get("published_at") or "").replace("Z", "+00:00"))
                    if ts and since_dt and ts < since_dt:
                        done = True
                        break
                    out.append(a)
                if len(arts) < 500:
                    done = True
                page += 1
        except Exception as exc:  # noqa: BLE001
            fails.append((fid, str(exc)[:100]))
    seen, uniq = set(), []
    for a in out:
        if a.get("id") in seen:
            continue
        seen.add(a["id"])
        uniq.append(a)
    return uniq, fails


# ---------- LLM 各段 ----------

def _fmt_titles(arts: list) -> str:
    lines = []
    for i, a in enumerate(arts):
        enr = a.get("enrichment") or {}
        ev = (" 标签[" + ",".join((enr.get("event_tags") or [])[:3]) + "]") if enr.get("event_tags") else ""
        tk = (" 代码[" + ",".join((enr.get("tickers") or [])[:4]) + "]") if enr.get("tickers") else ""
        ind = (" 行业[" + ",".join((enr.get("industries") or [])[:2]) + "]") if enr.get("industries") else ""
        se = (enr.get("sentiment_label") or "").lower() or ""
        st = {"bullish": " 情绪[多]", "bearish": " 情绪[空]"}.get(se, "")
        src = str(a.get("source_name") or "?")
        lines.append(
            f"{i}. [{_to_bj(str(a.get('published_at') or ''))[5:]}][{source_tier(src)}]"
            f"{src[:20]} {str(a.get('title') or '')[:104]}{tk}{ind}{ev}{st}")
    return "\n".join(lines)


def _user_block(title: str, arts: list) -> str:
    return f"{title}（{len(arts)} 条，新→旧）\n" + _fmt_titles(arts)


def _system_for(stage: str) -> str:
    if stage == GATE:
        return ("你是 A股实盘『新闻门卫』：从批量标题中挑出对 A股（含港股联动/外围传导）"
                "可能有影响的条目，剔除无关噪音（社会/体育/娱乐/纯海外与他国市场/广告）。"
                "方向定义：macro=宏观/政策/货币/监管/外围(美债/汇率/地缘/大宗)/大盘；"
                "micro=行业板块/概念/公司公告/业绩/盘中异动；holdings=直接命中给定关注代码"
                "或这些标的所属板块的强相关条目。输入带 序号/来源/时间/标签证据(正文级)。"
                "宁全勿漏：不确定就保留（保留 = 不写）。"
                "输出黑名单式：**默认全部条目保留且方向 micro**，你只写例外行，每行一个，"
                "格式 `序号 方向`——方向不是 micro 的写 `12 macro` 或 `7 holdings`；"
                "确定无关的噪音写 `33 skip`。不要复述保留项，不要解释。")
    if stage == MACRO:
        return ("你是『宏观政策研究』（大方向，只分析不下单）：输入为门卫筛出的宏观/政策/"
                "监管/外围标题。输出对 A股 大盘/风格/利率的影响判断。只引用输入事实，"
                "不臆造数据。行式输出（每行一条，字段用 | 分隔，严禁其他文字）：\n"
                "V | 观点一句话 | bias(-1..1)\n"
                "D | 主线 | 对A股传导（可多行）\n"
                "R | 风险 | severity(1..3)（可多行）\n"
                "C | confidence(0..1)\n"
                "示例：V | 中性偏多，政策托底 | 0.2\nD | 3000亿特别国债 | 利好金融权重")
    if stage == MICRO:
        return ("你是『板块个股情报』（小方向，只分析不下单）：输入为门卫筛出的行业/板块/"
                "公司/业绩/异动标题。归纳热点主题与公司事件（精选有影响的，禁止逐条复述）。"
                "股票代码只许来自输入证据，禁止臆造代码。行式输出（每行一条，字段用 | "
                "分隔，严禁其他文字）：\n"
                "T | 板块/主题 | 驱动逻辑 | 强度(1..3) | 催化（可多行）\n"
                "E | 代码(逗号分隔) | 公司 | 事件类型(短词，如 涨停/业绩/减持/解禁/"
                "政策监管；勿照抄表头、勿拿标题当类型) | sentiment(-1..1) | 备注（可多行）\n"
                "O | 轮动观察一句话\nC | confidence(0..1)\n"
                "示例：T | 存储芯片 | 涨价+需求复苏 | 2 | 三星涨价\n"
                "E | 688123.SH | 聚辰股份 | 涨停异动 | 0.8 | 板块带动")
    if stage == HOLD:
        return ("你是『持仓情报』（重点岗，只分析不下单）：输入 = 实盘持仓/关注池代码清单"
                "+ 本轮板块/宏观结论 + 直接命中这些代码的新闻。你的活是**行业→个股传导**："
                "从板块主题/宏观主线推出清单里哪些票受影响、方向与力度。"
                "代码只许来自清单，禁止臆造；没有实质信号就不输出该票。"
                "行式输出（每行一条，字段用 | 分隔，严禁其他文字）：\n"
                "H | 代码 | 名称 | verdict(利好/利空/中性) | impact(-2..2) | "
                "事件类型：一句话传导链 | 来源（最多 12 行）\n"
                "X | 代码 | 跨持仓联动风险（可多行）\n"
                "A | 动作提示(观察/挂条件/警示)（可多行）\nC | confidence(0..1)\n"
                "硬约束：\n"
                "1) verdict 与 impact 必须一致——利好不得配 impact 0（矛盾行会被丢弃）；\n"
                "2) 事件类型以枚举词开头：涨停/跌停/大涨异动/大跌异动/业绩/回购增持/"
                "减持/解禁/并购重组/政策监管/中标订单/涨价/股东变动/问询处罚/其他；"
                "冒号后写传导链一句话（如 涨价：MDI挂牌价上调→聚氨酯龙头成本传导）；\n"
                "3) 来源列：板块/宏观传导推断一律写「板块传导」，直接命中新闻写原来源——"
                "晚间复盘靠这一列区分「推断」与「新闻命中」；\n"
                "4) 输入既无板块/宏观结论、也无直接命中新闻时，只输出 `C | 0`，不要编。\n"
                "示例：H | 600309.SH | 万华化学 | 利好 | 1 | 涨价：MDI挂牌价上调→"
                "聚氨酯龙头成本传导 | 板块传导\n"
                "宁精勿滥：泛泛而谈标中性+impact0。只输出这些行。")
    return ("你是『新闻主编』：汇总本轮宏观/板块/持仓分析，产出面向交易的『新闻分子』。"
            "宏观已在输入给出不用重述；你负责：主题≤4、watch≤5（给代码+触发条件）、"
            "开放风险≤3、持仓逐票保真转写（≤8，只转写输入出现的，不加新内容）、"
            "给市场研究的参考、一句话编辑备注。行式输出（每行一条，字段用 | 分隔，"
            "严禁其他文字；直接写最终答案，不要写思考/推演/自我检查过程）：\n"
            "T | 主题 | 驱动逻辑（可多行）\n"
            "W | 代码 | 名称 | 逻辑 | 触发条件（可多行）\n"
            "R | 开放风险（可多行）\n"
            "H | 代码 | 名称 | verdict(利好/利空/中性) | impact(-2..2) | horizon | "
            "event_type(短词，与上游一致) | headline(截40字) | source | note（≤8行）\n"
            "N | 给市场研究的参考\nE | 编辑备注一句话\n"
            "C | confidence(0..1)（示例：C | 0.75）")


def _fix_leading_zero_ints(t: str) -> str:
    """模型会照抄输入里的补零序号（如 000）——JSON 规范不允许前导零：
    冒号/逗号/数组边界后的 -?0+数字 → 去前导零（保留符号与前导空白）。"""

    def _rep(m: re.Match) -> str:
        body, lead = m.group(0), m.group(1)
        s = body[len(lead):]
        neg = s.startswith("-")
        digits = s.lstrip("-").lstrip("0") or "0"
        return lead + ("-" if neg else "") + digits

    return re.sub(r"(?<=[:,\[{])(\s*)-?0+(?=\d)", _rep, t)


# 门卫例外行（黑名单式，2026-09-08）：`12 macro` / `33 holdings` / `7 skip`；
# 裸序号 `12` = 剔除。行首锚定——裸数字在散文里很常见，非锚定会误判序号。
_GATE_LINE_STRICT = re.compile(
    r"(?m)^\s*(\d{1,3})\s*(macro|micro|holdings|skip)?\s*[.。:：]?\s*$")
# 散文救捞（旧格式内联）：只认带方向的，裸数字不救（避免散文数字被当剔除）
_GATE_LINE = re.compile(r"(\d{1,3})\s+(macro|micro|holdings|skip)")
# 合法"无例外"回答：整段只有这些字/标点（"无" "（无）" "全部保留"）；空串不算
_GATE_EMPTY_RE = re.compile(r"^[\s（）()【】\[\]{}<>《》无没空—\-—。.、,，;；:：例外均全部保留]*$")
_PIPE = re.compile(r"\s*\|\s*")

# ---- 事件类型归一（2026-09-08）----
# 实测噪声：模型照抄字段名（"事件类型"）、把标题当类型（"培育钻石概念异动拉升"）。
# lessons 按 事件类型×来源 聚合，同一现象被拆成 8 个桶（涨停/封板/2连板/连板拉升…）
# → 永远到不了 ≥3 的注入阈值。这里收敛到固定枚举，自由文本一律映射进来。
EVENT_TYPES = ("涨停", "跌停", "大涨异动", "大跌异动", "业绩", "回购增持", "减持",
               "解禁", "并购重组", "政策监管", "中标订单", "涨价", "股东变动",
               "问询处罚", "其他")
# 顺序敏感：先匹配到的胜出（涨停类在前，避免"涨停"被"涨"类规则抢走）
_EVENT_RULES = (
    ("跌停", ("跌停", "闪崩", "暴跌", "重挫")),
    ("涨停", ("涨停", "封板", "连板", "一字板", "首板", "炸板")),
    ("大跌异动", ("大跌", "跳水", "下挫", "领跌", "跌超", "跌逾")),
    ("大涨异动", ("大涨", "拉升", "上涨", "涨超", "涨逾", "高开", "异动")),
    ("业绩", ("业绩", "预增", "预亏", "财报", "净利", "营收", "扭亏", "亏损")),
    ("回购增持", ("回购", "增持", "举牌")),
    ("减持", ("减持", "清仓式")),
    ("解禁", ("解禁", "限售股")),
    ("并购重组", ("并购", "重组", "收购", "合并", "资产注入", "借壳", "分拆")),
    ("政策监管", ("政策", "监管", "补贴", "规划", "意见", "通知", "试点",
                  "关税", "降准", "降息", "央行", "证监会")),
    ("中标订单", ("中标", "订单", "合同", "签约", "供货", "采购")),
    ("涨价", ("涨价", "提价", "价上调", "涨价函")),
    ("股东变动", ("股东", "实控人", "易主", "股权", "要约")),
    ("问询处罚", ("问询", "处罚", "立案", "警示", "违规", "诉讼", "调查", "整改")),
)
# 模型把表头/占位抄成事件类型的几种形态
_EVENT_PLACEHOLDERS = frozenset((
    "", "-", "—", "无", "事件", "类型", "事件类型", "event_type", "eventtype",
    "事件类型：", "事件类型("))


def normalize_event_type(raw: str, *extra: str) -> str:
    """事件类型 → 固定枚举 EVENT_TYPES。

    占位符/字段名/空值 → 退回用 extra（备注、标题）兜底匹配；都匹配不到 → "其他"。
    例："2连板"/"封板"/"触及涨停" → 涨停；"培育钻石概念异动拉升" → 大涨异动；
    "事件类型" → 按备注/标题判。
    """
    t = str(raw or "").strip().strip("：: 　")
    hay = t if t.lower() not in _EVENT_PLACEHOLDERS else ""
    if not hay:
        hay = " ".join(str(x or "") for x in extra)
    if not hay.strip():
        return "其他"
    for name, kws in _EVENT_RULES:
        if any(k in hay for k in kws):
            return name
    return "其他"


def parse_pipe_lines(content: str, kind: str) -> dict | None:
    """行式填表协议解析（| 分隔）。散文/未知行自动忽略（宁缺勿滥）。
    kind: macro|micro|holdings → 规范化 dict（与 JSON 结构同构，供 chief/落盘）。"""
    out: dict = {}
    for ln in str(content or "").splitlines():
        f = [x.strip() for x in _PIPE.split(ln.strip())]
        if len(f) < 2 or not f[0]:
            continue
        if _is_placeholder_row(f):
            continue  # 模型照抄提示词里的示例占位行 → 丢弃
        tag = f[0].upper()
        if kind == "macro":
            if tag == "V" and len(f) >= 3:
                out["view"], out["bias"] = f[1], _fnum(f[2])
            elif tag == "D" and len(f) >= 2:
                out.setdefault("drivers", []).append(
                    {"item": f[1], "transmission": f[2] if len(f) > 2 else ""})
            elif tag == "R" and len(f) >= 2:
                out.setdefault("risks", []).append(
                    {"item": f[1], "severity": _fnum(f[2], 1) if len(f) > 2 else 1})
            elif tag == "C":
                out["confidence"] = _fnum(f[1])
        elif kind == "micro":
            if tag == "T" and len(f) >= 3:
                out.setdefault("themes", []).append({
                    "name": f[1], "logic": f[2],
                    "strength": int(_fnum(f[3], 1)) if len(f) > 3 else 1,
                    "catalyst": f[4] if len(f) > 4 else ""})
            elif tag == "E" and len(f) >= 4:
                out.setdefault("events", []).append({
                    "tickers": [t for t in f[1].replace("，", ",").split(",") if t],
                    "name": f[2],
                    # 事件类型归一：模型常照抄表头/拿标题当类型 → 退回备注兜底
                    "event_type": normalize_event_type(
                        f[3], f[5] if len(f) > 5 else "", f[2]),
                    "sentiment": _fnum(f[4], 0) if len(f) > 4 else 0,
                    "note": f[5] if len(f) > 5 else ""})
            elif tag == "O":
                out["rotation"] = f[1]
            elif tag == "C":
                v = _strict_fnum(f[1])
                if v is not None:
                    out["confidence"] = max(0.0, min(1.0, v))
        elif kind == "chief":
            if tag == "T" and len(f) >= 2:
                out.setdefault("themes", []).append(
                    {"name": f[1], "logic": f[2] if len(f) > 2 else ""})
            elif tag == "W" and len(f) >= 2:
                out.setdefault("watch_list", []).append({
                    "code": f[1], "name": f[2] if len(f) > 2 else "",
                    "logic": f[3] if len(f) > 3 else "",
                    "trigger": f[4] if len(f) > 4 else ""})
            elif tag == "R":
                out.setdefault("open_risks", []).append(f[1])
            elif tag == "H" and len(f) >= 6:
                out.setdefault("holdings", []).append({
                    "code": f[1], "name": f[2], "verdict": f[3] or "中性",
                    "impact": _fnum(f[4], 0), "horizon": f[5] if len(f) > 5 else "",
                    "event_type": normalize_event_type(
                        f[6] if len(f) > 6 else "", f[7] if len(f) > 7 else "", f[2]),
                    "headline": f[7] if len(f) > 7 else "",
                    "source": f[8] if len(f) > 8 else "",
                    "note": f[9] if len(f) > 9 else ""})
            elif tag == "N":
                out["market_notes"] = f[1]
            elif tag == "E":
                out["editor_note"] = f[1]
            elif tag == "C":
                v = _strict_fnum(f[1])
                if v is not None:
                    out["confidence"] = max(0.0, min(1.0, v))
        elif kind == "holdings":
            if tag == "H" and len(f) >= 6:
                info = f[5] if len(f) > 5 else ""
                out.setdefault("per_stock", []).append({
                    "code": f[1], "name": f[2], "verdict": f[3] or "中性",
                    "impact": _fnum(f[4], 0),
                    "horizon": "短期" if "短期" in info else ("日内" if "日内" in info else ""),
                    "event_type": normalize_event_type(
                        info.split("：")[0][:24] if info else "", info, f[2]),
                    "headline": info.split("：", 1)[1] if "：" in info else info,
                    "source": f[6] if len(f) > 6 else "", "note": ""})
            elif tag == "X" and len(f) >= 2:
                out.setdefault("cross_risks", []).append({"code": f[1], "why": f[2] if len(f) > 2 else ""})
            elif tag == "A" and len(f) >= 2:
                out.setdefault("action_hints", []).append(f[1])
            elif tag == "C":
                v = _strict_fnum(f[1])
                if v is not None:
                    out["confidence"] = max(0.0, min(1.0, v))
    return out if out else None


def parse_gate_lines(content: str) -> dict | None:
    """门卫行式输出解析（黑名单式，2026-09-08）：默认全部保留 + micro，
    模型只写例外行——`12 macro`/`33 holdings`（方向例外）、`7 skip` 或裸 `7`（剔除）。
    行首锚定优先（裸序号在散文里会误伤），未命中再走旧的内联救捞。
    合法"无例外"回答（`无`/`全部保留`）返回空 related；空串/疑似乱码返回 None。"""
    t = str(content or "")
    rel: list = []
    seen: set = set()
    for m in _GATE_LINE_STRICT.finditer(t):
        i = int(m.group(1))
        if i in seen:
            continue
        seen.add(i)
        rel.append({"i": i, "dir": m.group(2) or "skip"})
    if not rel:
        for idx, direction in _GATE_LINE.findall(t):
            i = int(idx)
            if i in seen:
                continue
            seen.add(i)
            rel.append({"i": i, "dir": direction})
    if not rel:
        s = t.strip()
        return {"related": []} if s and _GATE_EMPTY_RE.match(s) else None
    return {"related": rel}


def parse_llm_json(text: str) -> dict | None:
    if not text:
        return None
    t = str(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    for cand in (t[i:j + 1], _fix_leading_zero_ints(t[i:j + 1])):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            continue
    return None


class TruncatedOutputError(RuntimeError):
    """输出被 max_tokens 截断。携带原文落盘供审计，但整轮不作解析——
    截断轮的"末尾几行"必然残缺，救捞只会把思考草稿里的半成品行捞出来
    （2026-09-08 实录：名称"(是)"、headline"宏观经济传导..."截尾的 H 行
    进了交易提示词）。"""

    def __init__(self, content: str, usage: dict | None):
        super().__init__("输出被 max_tokens 截断，整轮作废")
        self.content = content
        self.usage = usage


def call_llm(user: str, system: str, stage: str = "",
             max_tokens: int | None = None) -> tuple[str, dict | None]:
    """v4-flash 直连（重试 1 次，120s 超时）。失败 raise 由调用段降级。"""
    env = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in env:
                env[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    base = env.get("OPENAI_API_BASE", "").rstrip("/")
    key = env.get("OPENAI_API_KEY", "")
    if not base or not key:
        raise RuntimeError("OPENAI_API_BASE/KEY 缺失")
    import requests

    payload = {"model": MODEL,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": 0.2,
               "max_tokens": max_tokens or _MAX_TOKENS.get(stage, 8000)}
    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json=payload, timeout=120)
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                print(f"  ⚠️ {MODEL} 调用失败，重试 1 次: {exc}")
    else:
        raise last_exc  # type: ignore[union-attr]
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    content = str(msg.get("content") or "").strip() or str(msg.get("reasoning_content") or "").strip()
    finish = str(((data.get("choices") or [{}])[0].get("finish_reason")) or "")
    usage = data.get("usage") or None
    if usage:
        usage = {k: int(usage.get(k) or 0)
                 for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    # max_tokens 截断：最终答案未写完，行式协议末尾几行必然残缺，救捞只会
    # 把思考草稿里的半成品行捞出来 → 截断轮整体作废（调用侧降级复用上一版）。
    if finish == "length":
        raise TruncatedOutputError(content, usage)
    return content, usage


def append_log(user: str, content: str, sig: str, usage: dict | None = None) -> Path:
    """落盘对话日志（与交易 agent 同结构 → 前端 /api/agents/{sig}/logs 直接可读）。"""
    now = datetime.now(CN_TZ)
    log_dir = ROOT / "data" / "agent_data_astock" / sig / "log" / now.strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": now.isoformat(), "signature": sig,
             "new_messages": [{"role": "user", "content": user},
                              {"role": "assistant", "content": content}]}
    if usage:
        entry["usage"] = usage
    path = log_dir / "log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


# ---------- 主编分子：落盘 / 渲染 / 交易侧注入 ----------

def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def save_brief(brief: dict) -> None:
    _atomic_write(BRIEF_FILE, brief)
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(brief, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _list_of_dicts(v) -> list:
    """LLM 偶发把数组写成字符串/其他形态 → 统一只留 dict 列表，防下游崩。"""
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


def _rows(v) -> list:
    """简报行列表：dict 或 str 均可（首席的 open_risks 常为字符串数组）。"""
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, (dict, str))]


def _fnum(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _strict_fnum(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


_PLACEHOLDER_PAT = ("（可多行）", "示例", "verdict(利好", "impact(-2..2)", "bias(-1..1)",
                    "代码(逗号分隔)", "confidence(0..1)", "severity(1..3)")
_PLACEHOLDER_TOKENS = {"theme", "logic", "name", "code", "主题", "名称", "代码",
                       "驱动逻辑", "trigger condition", "event_type", "板块/主题"}


def _is_placeholder_row(fields: list) -> bool:
    joined = " | ".join(fields)
    if any(p in joined for p in _PLACEHOLDER_PAT):
        return True
    # 模型把提示词字段名照抄成数据（如 T | theme | logic、W | name | code | ...）
    body = [x.strip().lower() for x in fields[1:] if x.strip()]
    if any(x in _PLACEHOLDER_TOKENS for x in body[:2]):
        return True
    # 截断草稿里的半成品行（2026-09-08 chief 实录）：名称列空/纯符号占位
    #（"(是)"、"—"、"无"）；headline 截尾"..."；verdict 利空/利好却 impact=0
    if len(fields) >= 4:
        name = (fields[2] if len(fields) > 2 else "").strip()
        if not name or set(name) <= {"(", ")", "是", "—", "-", "无", "?"}:
            return True
    if len(fields) > 7:
        if str(fields[7]).rstrip().endswith("...") or str(fields[7]).strip() in ("", "…"):
            return True
    if len(fields) > 4:
        verdict = (fields[3] or "").strip() if len(fields) > 3 else ""
        try:
            impact = float(str(fields[4]).strip())
        except ValueError:
            impact = None
        if impact is not None and impact == 0 and verdict in ("利好", "利空"):
            return True
    return False


def _maybe_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _norm_code(code) -> str:
    """代码归一（只用于比对）：去后缀/空格、大写，取前 6 位数字字母。
    "600309.SH" / "600309.sh" / "600309" → "600309"；"9880.HK" → "9880"。"""
    return re.sub(r"[^0-9A-Z]", "", str(code or "").upper())[:6]


def _chief_holdings(rows, hold_seg) -> list:
    """主编 H 行只保留「持仓情报段真出现过的票」，去重保序。

    2026-09-09 实录：HOLD 段只给了 1 条 H 行，主编却输出 21 行——把关注池代码
    全补成「中性/0 未命中」填充行，同一条万华化学还写了两遍。提示词里
    「只转写输入出现的（≤8，不加新内容）」拦不住模型，这里用代码兜底：
    **代码必须来自 HOLD 段**，否则 latest.json.holdings 会被填充行淹没，
    「持仓情报有没有真情报」反而看不出来。

    同时把 source/event_type 换成 HOLD 段的原值——主编转写时常把段名
    「持仓情报」填进 source 列（实录），而晚间复盘靠这一列区分
    「板块传导（推断）」与「财联社（新闻命中）」，不能丢。
    """
    hold_seg = hold_seg if isinstance(hold_seg, dict) else {}
    by_code = {_norm_code(h.get("code")): h
               for h in _list_of_dicts(hold_seg.get("per_stock"))}
    by_code.pop("", None)
    if not by_code:
        return []          # HOLD 没跑/无输出 → 主编无「输入出现过的票」可转写
    out, seen = [], set()
    for r in _list_of_dicts(rows):
        c = _norm_code(r.get("code"))
        if not c or c not in by_code or c in seen:
            continue
        src = by_code[c]
        out.append({**r,
                    "source": src.get("source") or r.get("source"),
                    "event_type": src.get("event_type") or r.get("event_type")})
        seen.add(c)
    return out


def brief_to_text(b: dict) -> str:
    """『新闻分子』markdown（嵌入交易/市场研究提示词）。字段缺失/异形逐段容忍。"""
    m = _maybe_dict(b.get("macro"))
    win = b.get("window") or {}
    ts = _iso(b.get("ts"))
    head = (f"【新闻分子（主编 {_bj_fmt(ts, '%H:%M')} · "
            f"窗口 {str(win.get('start') or '')[:16]}→{str(win.get('end') or '')[:16]}）】")
    lines = [head]
    if m:
        drv = "；".join(d.get("item", "") for d in _list_of_dicts(m.get("drivers"))[:3])
        lines.append(f"- 宏观：{str(m.get('view') or '—')}"
                     f"（偏度 {float(m.get('bias') or 0):+.1f}）"
                     + (f" 主线：{drv}" if drv else ""))
    for t in _list_of_dicts(b.get("themes"))[:3]:
        stars = "★" * int(_fnum(t.get("strength"), 0))
        lines.append(f"- 主题：{t.get('name')}"
                     + (f"（{stars}）" if stars else "") + f"· {str(t.get('logic', ''))[:80]}")
    for r in _rows(b.get("open_risks"))[:3]:
        item = r if isinstance(r, str) else r.get("item", "")
        lines.append(f"- 风险：{str(item)[:90]}")
    for h in _list_of_dicts(b.get("holdings"))[:8]:
        ev = str(h.get("event_type") or h.get("headline") or "")
        lines.append(f"- 持仓：{h.get('name')}({h.get('code')}) {h.get('verdict') or '中性'}"
                     f" impact{int(h.get('impact') or 0):+d} · {ev[:60]}（{h.get('source')}）")
    for w in _list_of_dicts(b.get("watch_list"))[:5]:
        lines.append(f"- 盯：{w.get('name')}({w.get('code')})"
                     f" {w.get('trigger') or w.get('logic') or ''}")
    conf = _fnum(b.get("confidence"))
    lines.append(f"- 置信度 {'—' if conf <= 0 else f'{conf:.2f}'} · 生成 {_bj_fmt(ts, '%H:%M')}"
                 f" · 主编备注：{b.get('editor_note') or '无'}")
    return "\n".join(lines)


def _seg_compact(stage: str, d: dict | None) -> str:
    """段结果 → 给主编的紧凑要点行（不贴大 JSON，防散文模型又写长篇）。"""
    if not d or d.get("skipped"):
        return f"[{AGENT_CN.get(stage, stage)}] 本窗口无输出"
    out = [f"[{AGENT_CN.get(stage, stage)}]"]
    if d.get("degraded"):
        out.append("  ⚠ 本段部分批次失败，以下为已成功批次的要点（数据不完整）")
    if stage == MACRO:
        out.append(f"宏观 {d.get('view')}（bias {_fnum(d.get('bias')):+.1f}）")
        for x in _list_of_dicts(d.get("drivers"))[:4]:
            out.append(f"  · {x.get('item')} → {x.get('transmission')}")
        for x in _list_of_dicts(d.get("risks"))[:4]:
            out.append(f"  ⚠ {x.get('item')}(sev{_fnum(x.get('severity'), 1):.0f})")
    elif stage == MICRO:
        for t in _list_of_dicts(d.get("themes"))[:6]:
            out.append(f"主题 {t.get('name')} 强度{int(_fnum(t.get('strength'), 1))} · "
                       f"{t.get('logic')}（{t.get('catalyst')}）")
        for e in _list_of_dicts(d.get("events"))[:30]:
            out.append(f"事件 {e.get('name')} {','.join(e.get('tickers') or [])} "
                       f"{e.get('event_type')} {_fnum(e.get('sentiment')):+.1f} {e.get('note')}")
        if d.get("rotation"):
            out.append(f"轮动 {d.get('rotation')}")
    elif stage == HOLD:
        for h in _list_of_dicts(d.get("per_stock"))[:10]:
            horizon = (f" {h['horizon']}" if h.get("horizon") else "")
            out.append(f"持仓 {h.get('name')}({h.get('code')}) {h.get('verdict')} "
                       f"impact{_fnum(h.get('impact')):+.0f}{horizon} · "
                       f"{h.get('event_type')}：{str(h.get('headline'))[:44]}（{h.get('source')}）"
                       + (f" note={h.get('note')}" if h.get("note") else ""))
        for x in _list_of_dicts(d.get("cross_risks"))[:3]:
            out.append(f"  ⚠ 联动 {x.get('code')} {x.get('why')}")
    out.append(f"置信度 {_fnum(d.get('confidence')):.2f}")
    return "\n".join(out)


HOLD_NO_OUTPUT = "本窗口无输出"     # _seg_compact 对缺席段的占位文案
HOLD_DIRECT_MAX = 30                # 直接命中新闻最多喂 30 条（热点日可能几十条）


def _hold_has_evidence(results: dict, h_art: list, watch_codes: dict) -> bool:
    """持仓情报证据闸门：关注池非空 **且** 三路素材至少一路有实质内容。

    没有素材时模型只能对着一串代码脑补传导链，H 行无法证伪 → 会污染
    latest.json.holdings 与晚间经验库。这也是该段自 2026-09-07 起长期静默
    之外必须补的防线（旧逻辑是「有命中新闻才跑」，等于永远不跑）。
    """
    if not watch_codes:
        return False
    if h_art:
        return True
    return any(HOLD_NO_OUTPUT not in _seg_compact(s, results.get(s))
               for s in (MICRO, MACRO))


def build_hold_user(watch_codes: dict, results: dict, h_art: list,
                    lessons_block: str = "") -> str:
    """持仓情报输入块：关注池 + 本轮板块/宏观结论 + 直接命中新闻（纯函数，可测）。

    方案 A（2026-09-09）：不再等「新闻标题点名持仓」——关注池是 20 只小盘候选
    + 实盘持仓，快讯标题几乎不点名 → h_art 恒空 → 该段自 09-07 起零输出。
    改为让模型做「板块/主题 → 个股传导」。
    """
    parts = ["当前实盘持仓/关注池 + 本轮板块/宏观结论（请做行业→个股传导）",
             _watch_line(watch_codes, "当前实盘持仓/关注池代码（含实盘持仓与候选池）")]
    if lessons_block:
        parts.append(lessons_block)
    for stage in (MICRO, MACRO):
        parts.append(_seg_compact(stage, results.get(stage)))
    if h_art:
        parts.append(f"直接命中关注代码的新闻（最多 {HOLD_DIRECT_MAX} 条）：\n"
                     + _fmt_titles(h_art[:HOLD_DIRECT_MAX]))
    return "\n".join(parts)


def update_intraday_watch(micro: dict | None, watch_list: list | None,
                          path: Path | None = None) -> dict:
    """盘中异动关注（用户口径：池外异动股也要有 L2/微观结构关注，视野别是闭集）：
    来源 = 板块个股情报段的事件 tickers + 主编 watch_list。冷却 60 分钟、上限 20、
    新优先；live_l2_capture 轮询时合并。"""
    out_path = path or INTRA_WATCH_FILE
    now = datetime.now(CN_TZ)
    items: dict = {}
    try:
        old = _list_of_dicts(json.loads(out_path.read_text(encoding="utf-8")).get("items"))
        for it in old:
            if it.get("code") and it.get("ts"):
                items[str(it["code"])] = it
    except (OSError, json.JSONDecodeError):
        pass

    def _add(code: str, why: str) -> None:
        code = str(code or "").strip()
        if not code:
            return
        items[code] = {"code": code, "ts": now.isoformat(timespec="seconds"),
                       "why": str(why or "")[:60]}

    for e in _list_of_dicts((micro or {}).get("events")):
        for code in (e.get("tickers") or []):
            _add(code, f"{e.get('name')} {e.get('event_type')}")
    for w in _list_of_dicts(watch_list or []):
        _add(w.get("code"), f"主编盯:{w.get('trigger') or w.get('logic')}")

    # 冷却淘汰
    cutoff = now.timestamp() - INTRA_WATCH_TTL_MIN * 60
    kept = []
    for it in items.values():
        ts = _iso(it.get("ts"))
        if ts and ts.timestamp() >= cutoff:
            kept.append(it)
    kept.sort(key=lambda x: x["ts"], reverse=True)
    kept = kept[:INTRA_WATCH_MAX]
    out = {"ts": now.isoformat(timespec="seconds"), "items": kept}
    _atomic_write(out_path, out)
    return out


def load_intraday_watch() -> list:
    """供 L2 采集合并的异动代码列表（过期条目忽略）。"""
    try:
        d = json.loads(INTRA_WATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cutoff = datetime.now(CN_TZ).timestamp() - INTRA_WATCH_TTL_MIN * 60
    out = []
    for it in _list_of_dicts(d.get("items")):
        ts = _iso(it.get("ts"))
        if ts and ts.timestamp() >= cutoff and it.get("code"):
            out.append(str(it["code"]))
    return out[:INTRA_WATCH_MAX]


def load_lessons_text(top: int = 4) -> str:
    """晚间复盘经验（信号有效性统计）→ 各段提示词先验注入。缺失返回空。"""
    try:
        d = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    from news_review import lessons_text  # 延迟导入防环

    t = lessons_text(d, top=top)
    return (f"【历史经验（近30日新闻信号复盘，样本≥3 才列出；反向率高的事件类型要降权）】\n{t}"
            if t else "")


def empty_window_markup(ts: str) -> str:
    d = _iso(ts)
    return (f"窗口（截至 {_bj_fmt(d)}）无新增相关新闻：沿用上一版分子，无新决策依据。"
            if d else "窗口无新增相关新闻：沿用上一版分子。")


# ---------- 关注代码（持仓 + 候选池） ----------

def _watch_line(watch_codes: dict,
                label: str = "关注代码（命中→holdings 方向）") -> str:
    """关注代码行 → 注入 gate/chief/holdings 提示词。带公司名：
    次新股代码模型不认识，不给名它就瞎猜（浪费 token 且易截断）。
    label 供 holdings 段换成「持仓/关注池」口径（门卫看的是「命中→方向」）。"""
    if not watch_codes:
        return f"{label}：无"
    parts = [f"{c} {watch_codes[c]}" if watch_codes.get(c) else c
             for c in sorted(watch_codes)]
    return f"{label}：" + ",".join(parts)


def merge_watch_codes(positions: list | None, pool: list | None,
                      pool_codes: set | None = None) -> set:
    out = set(pool_codes or ())
    for p in positions or []:
        c = p.get("stock_code") if isinstance(p, dict) else ""
        if c and float(p.get("total_volume") or p.get("volume") or 0) > 0:
            out.add(str(c))
    for p in pool or []:
        c = p.get("code") or p.get("symbol") or p.get("stock_code") or ""
        if c:
            out.add(str(c))
    return out


def _quantdb_instrument_file() -> Path:
    """instrument_detail.parquet 路径（尊重 QM_QUANTDB_DATA_DIR，仅接受绝对路径）。"""
    q = os.environ.get("QM_QUANTDB_DATA_DIR") or ""
    base = Path(q) if q.startswith("/") else ROOT / "data" / "quantdb"
    return base / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"


def _name_lookup() -> dict:
    """全市场名称表（quantdb instrument_detail）——桥不返回 stock_name 时的兜底。

    2026-09-08 实测：桥持仓 `stock_name=None`（两只实盘持仓 001312.SZ/600309.SH
    都没有名字）→ `_hits_watch` 的名称级命中对**真正持仓的票**永远不可能，
    holdings 段只剩 enrichment.tickers 一条路（当日 0 命中）。
    2026-09-29 随 keeper 迁入 QuantMind：不再复用隔壁 live_hourly_analysis，
    直读本地 quantdb（与 push_notify 同源）；另给前缀式别名（SH600036）。
    """
    names: dict = {}
    try:
        import duckdb

        f = _quantdb_instrument_file()
        if not f.is_file():
            return names
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT Symbol, Name FROM read_parquet(?)", [str(f)]).fetchall()
        finally:
            con.close()
        for sym, nm in rows:
            s, n = str(sym or "").strip(), str(nm or "").strip()
            if not s or not n:
                continue
            names[s] = n
            m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", s)
            if m:                                   # 后缀式 → 前缀式别名
                names[f"{m.group(2)}{m.group(1)}"] = n
    except Exception:  # noqa: BLE001 名称兜底失败不阻塞管线
        pass
    return names


def _latest_picks_pool(top: int = 20) -> list[dict]:
    """QM 自有候选池 → [{code, name}]（data/reports/stock_picks 最新一期）。

    2026-09-29 迁入替代隔壁 live_llm_trade.load_pool（其子进程 select_from_reports
    本仓不存在）。取日期最大的 *picks.json；同日优先 *_agent_picks.json（晚间研究
    产出，select_from_reports 兼容格式）。两种写入结构都认：night_pool 的
    {picks:[...]} 与 postmarket 的 {candidates:[...]}。读取失败 → []。
    """
    d = ROOT / "data" / "reports" / "stock_picks"
    best, best_key = None, None
    for f in d.glob("*picks.json"):
        m = re.search(r"(\d{8})", f.name)
        if not m:
            continue
        key = (m.group(1), f.name.endswith("_agent_picks.json"))
        if best_key is None or key > best_key:
            best, best_key = f, key
    if best is None:
        return []
    try:
        data = json.loads(best.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for c in (data.get("picks") or data.get("candidates") or []):
        code = str(c.get("code") or c.get("symbol") or "").strip()
        if not code:
            continue
        out.append({"code": code, "name": str(c.get("name") or "")})
        if len(out) >= top:
            break
    return out


def load_watch_codes() -> tuple[dict, list]:
    """现场取持仓（桥）+ 候选池（QM 报告目录）；各自独立降级。
    返回 ({code: name}, warns)——名称一并返回：次新股代码模型不认识，
    不给名称它会花上千 token 瞎猜公司（2026-09-08 chief 截断的诱因）。"""
    codes: dict = {}
    warns = []
    try:
        from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

        acct = TdxBridgeBroker()._account_query()
        positions = [p for p in (acct.get("positions") or [])
                     if float(p.get("total_volume") or 0) > 0]
        for p in positions:
            c = str(p.get("stock_code") or "")
            if c:
                codes.setdefault(c, str(p.get("stock_name") or ""))
    except Exception as exc:  # noqa: BLE001
        warns.append(f"持仓获取失败: {str(exc)[:80]}")
    try:
        pool = _latest_picks_pool(20)
        if not pool:
            warns.append("候选池为空（data/reports/stock_picks 无最新池文件）")
        for p in pool:
            c = str(p.get("code") or "")
            if c:
                codes.setdefault(c, str(p.get("name") or ""))
    except Exception as exc:  # noqa: BLE001
        warns.append(f"候选池获取失败: {str(exc)[:80]}")
    missing = [c for c, n in codes.items() if not n]
    if missing:
        names = _name_lookup()
        for c in missing:
            if names.get(c):
                codes[c] = names[c]
        still = [c for c in missing if not codes.get(c)]
        if still:
            warns.append(f"{len(still)} 只关注码无名称（名称表未覆盖）: "
                         f"{','.join(still[:5])}")
    return codes, warns


# ============================================================
# 编排
# ============================================================

CHUNK = 50           # 单批进 macro/micro 的条数（100 条 × 逐条判决行曾必触 max_tokens 截断，
                     # 2026-09-08 午后两期全灭实录 → 批减半 + 预算放宽双保险）
GATE_CHUNK = 35      # 门卫批单独更小：门卫是「逐条判决」型任务，输入越长越容易长思考
                     # 吃满预算（2026-09-09 实测 323 条/7 批仍有 2 次截断）。
                     # 代价：批数 7→10、调用 +40%，整期更慢；用 state.last_run
                     # 的 truncations/gate_fallback_batches 做前后对比，不达标再回调。
GATE_SKIP_MAX_RATIO = 0.5   # 单批剔除比例上限：过半 → 判门卫失准，该批剔除作废
_JSON_TAIL = ("\n\n严格只输出 JSON：不要任何解释/思考过程/分析草稿/markdown 代码块，"
              "回答的首字符必须是 {。")
_LINE_TAIL = ("\n\n黑名单式输出：**默认所有条目保留、方向 micro**，只写例外行，每行一个：\n"
              "· 方向不是 micro 的保留项 → `序号 macro` 或 `序号 holdings`\n"
              "· 确定与 A股 无关的噪音 → `序号 skip`\n"
              "没列出的条目一律视为「保留 + micro」，不要复述它们，"
              "不要任何解释/标题/代码块。")
_PIPE_TAIL = ("\n\n严格行式输出：每行一个条目，字段以 | 分隔（示例见上），"
              "不要解释/序号/标题/markdown/JSON，散文与多余文字一律不要。")
# 各段输出协议：全部行式填表（散文型模型 JSON 遵从率差，填表最稳；
# chief 2026-09-07 实测 JSON/行式混排指令会引发模型自我矛盾性长思考）
_MODE = {GATE: "gate", MACRO: "macro", MICRO: "micro", HOLD: "holdings", CHIEF: "chief"}
# 输出预算受 120s 超时约束（≈4-6k token 上限）：批量段用行式协议压缩输出。
# 2026-09-08 实录：v4-flash 思考+输出合计吃满预算即截断（GATE 批 100 全灭、
# CHIEF 推演 9943 字未及写分子）→ 常规预算上调一档，截断后另有一次性放宽重试。
_MAX_TOKENS = {GATE: 6000, MICRO: 5000, MACRO: 4000, HOLD: 5000, CHIEF: 8000}
TRUNC_RETRY_MAX_TOKENS = 10000

# 单次运行统计（截断/重试/门卫兜底批数）→ state.last_run，供告警与排查
_RUN_STATS: dict = {}


def _chunked(seq: list, n: int = CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _call_with_trunc_retry(stage: str, full_user: str) -> tuple[str, dict | None]:
    """截断重试：截断多为思考+输出超出预算 → 放宽 max_tokens 再试一次，并在
    system 里压制思考泄漏。重试仍截才让 TruncatedOutputError 上抛作废。"""
    try:
        return call_llm(full_user, _system_for(stage), stage=stage)
    except TruncatedOutputError:
        _RUN_STATS["truncations"] += 1
        print(f"↻ [{AGENT_CN[stage]}] 输出被截断，放宽 max_tokens 重试 1 次")
        content, usage = call_llm(
            full_user,
            _system_for(stage) + "\n再次强调：跳过一切思考/推演/自我检查，"
            "直接按协议逐行输出最终答案。",
            stage=stage, max_tokens=TRUNC_RETRY_MAX_TOKENS)
        _RUN_STATS["trunc_retries_ok"] += 1
        print(f"✓ [{AGENT_CN[stage]}] 截断重试成功")
        return content, usage


def _run_stage(stage: str, user: str) -> tuple[dict | None, bool]:
    """单段单批执行：LLM → 按段协议解析（行式/门卫/JSON）→ 落盘对话。
    失败返回 (None, False) 由上层降级（规则兜底/跳段）。"""
    mode = _MODE.get(stage, "json")
    tail = {"gate": _LINE_TAIL, "json": _JSON_TAIL}.get(mode, _PIPE_TAIL)
    try:
        content, usage = _call_with_trunc_retry(stage, user + tail)
    except TruncatedOutputError as exc:
        # 截断原文照常落盘（前端对话/审计可见），但不做解析救捞 → 上层降级
        path = append_log(user, exc.content, stage, exc.usage)
        print(f"⚠️ [{AGENT_CN[stage]}] 输出被截断（重试仍截），整轮作废防草稿行污染"
              f"（token={exc.usage and exc.usage.get('total_tokens')}）→ {path.relative_to(ROOT)}")
        return None, False
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ [{AGENT_CN[stage]}] LLM 失败: {exc}")
        return None, False
    if mode == "gate":
        d = parse_gate_lines(content)
    elif mode == "json":
        d = parse_llm_json(content)
    else:
        d = parse_pipe_lines(content, mode)
    path = append_log(user, content or "(空输出)", stage, usage)
    tok = f" token={usage and usage.get('total_tokens')}"
    if d is None:
        print(f"⚠️ [{AGENT_CN[stage]}] 结构化解析失败，原文已落盘{tok}")
    else:
        print(f"✓ [{AGENT_CN[stage]}] 完成{tok} → {path.relative_to(ROOT)}")
    return d, d is not None


def _merge_chunks(results: list) -> dict | None:
    """多批输出合并：同名字段 concat 数组 / 均值数值 / 首值其他。全败返回 None。"""
    rs = [r for r in results if r]
    if not rs:
        return None
    if len(rs) == 1:
        return rs[0]
    keys: set = set()
    for r in rs:
        keys |= set(r.keys())
    out: dict = {}
    for k in keys:
        vals = [r.get(k) for r in rs if r.get(k) is not None]
        if not vals:
            continue
        if all(isinstance(v, list) for v in vals):
            out[k] = [x for v in vals for x in v]
        elif all(isinstance(v, (int, float)) for v in vals):
            out[k] = sum(vals) / len(vals)
        else:
            out[k] = vals[0]
    return out


def _run_chunked(stage: str, arts: list, title: str) -> tuple[dict | None, bool]:
    """分批跑一段并合并；空输入短路。"""
    if not arts:
        print(f"⏭️ [{AGENT_CN[stage]}] 无输入，跳过")
        return None, False
    chunks = list(_chunked(arts))
    if len(chunks) > 1:
        print(f"[{AGENT_CN[stage]}] 分批 {len(chunks)}×{len(chunks[0])} 处理")
    d_list = []
    ok_all = True
    for c in chunks:
        d, ok = _run_stage(stage, _user_block(title, c))
        d_list.append(d)
        ok_all = ok_all and ok
    return _merge_chunks(d_list), ok_all and any(d_list)


def _stage_result(d: dict | None, ok: bool) -> dict:
    """段结果归一：全败 → skipped；**部分批次失败 → 保留已解析数据 + degraded 标记**。

    2026-09-08 前：任一批失败即整段丢弃，且标成 degraded=false——8 批里 1 批作废
    = 全段消失（当日 09:25/19:05 两轮 micro 段实录），主编收到"[板块个股情报]
    本窗口无输出"，交易侧整天没有股票级事件。部分成功的数据是有效的，没有理由丢。
    """
    if d is None:
        return {"skipped": True, "degraded": True}
    return {**d, "degraded": True} if not ok else d


def run_pipeline(since_iso: str = "", stages: tuple = ALL_STAGES,
                 window_until: str = "", force: bool = False) -> int:
    """完整管线。0=成功/空窗短路。每段独立降级，不因单段失败中断下游。
    节假日（工作日但非交易日）：白天不跑，晚间 22:00 后统一一次全景；
    --force 手动不受限。"""
    now = datetime.now(CN_TZ)
    _RUN_STATS.clear()
    _RUN_STATS.update({"truncations": 0, "trunc_retries_ok": 0,
                       "gate_fallback_batches": 0})
    if not force and not is_trading_day(now.date()) and now.hour < 21:
        print("⏭️ 节假日：白天不分析，晚间（22:00 后）统一一次全景")
        return 0
    st = load_state()
    if not since_iso:
        since_iso, _ = window_for(st.get("last_end", ""), now.isoformat())
    until_iso = window_until or now.isoformat()

    # 1) 拉取 + 规则减载 + 去重 + 截断（新→旧）
    raw, fails = fetch_incr(since_iso, until_iso)
    arts = dedup_articles([a for a in raw if pick_relevant(a)])
    if len(arts) > MAX_TITLES:
        print(f"ℹ️ 候选 {len(arts)} 超上限，保留最新 {MAX_TITLES}")
        arts = arts[:MAX_TITLES]
    print(f"窗口 {_bj_fmt(_iso(since_iso))} → {_bj_fmt(_iso(until_iso))}："
          f"原始 {len(raw)} / 候选 {len(arts)}"
          + (f" / 桶失败 {len(fails)}" if fails else ""))

    if not arts:
        save_state(state_after_run(st, stages, until_iso, advance=not fails,
                                   last_empty=now.isoformat()))
        msg = empty_window_markup(until_iso)
        print("⏭️ " + msg)
        if CHIEF in stages:
            append_log(msg, "（无新增输入，未调用 LLM）", CHIEF)
        return 0

    # 2) 关注代码（持仓+候选池）→ gate 判定依据 + holdings 段
    lessons_block = ""
    try:
        lessons_block = load_lessons_text()
    except Exception:  # noqa: BLE001 经验缺失不阻塞
        lessons_block = ""
    watch_codes, warns = load_watch_codes()
    _atomic_write(WATCH_FILE, {"ts": now.isoformat(), "codes": sorted(watch_codes),
                               "names": {k: v for k, v in watch_codes.items() if v},
                               "warns": warns})
    watch_line = _watch_line(watch_codes)

    # 3) 门卫（agent 化金融相关 + macro/micro/holdings 方向；分批防超限/防指令丢失）
    g_macro, g_micro, h_art, g_skip = [], [], [], 0
    if GATE in stages:
        rel_idx: dict = {}
        chunks = list(_chunked(arts, GATE_CHUNK))
        print(f"[{AGENT_CN[GATE]}] 并行 {len(chunks)} 批 × {len(chunks[0]) if chunks else 0}")

        def _gate_chunk(chunk: list) -> dict:
            gate_user = _user_block("候选新闻（请剔除与 A股 无关条目并按方向归类）", chunk) \
                + "\n" + watch_line + (f"\n{lessons_block}" if lessons_block else "")
            d, _ok = _run_stage(GATE, gate_user)
            if d:
                base = arts.index(chunk[0])  # 块内序号 → 全局序号
                got = {base + x.get("i"): x.get("dir")
                       for x in (d.get("related") or [])
                       if isinstance(x, dict) and isinstance(x.get("i"), int)}
                # 安全阀：单批剔除过半 → 判模型失准，该批剔除作废（宁全勿漏）
                n_skip = sum(1 for v in got.values() if v == "skip")
                if n_skip > len(chunk) * GATE_SKIP_MAX_RATIO:
                    print(f"⚠️ 门卫该批剔除 {n_skip}/{len(chunk)} 过半，判失准 → 全部保留")
                    got = {k: v for k, v in got.items() if v != "skip"}
                return got
            print("⚠️ 门卫该批失败，本批按规则方向兜底")
            _RUN_STATS["gate_fallback_batches"] += 1
            return {arts.index(a): rule_direction(a, watch_codes) for a in chunk}

        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
            for got in ex.map(_gate_chunk, chunks):
                rel_idx.update(got)
        # 黑名单式（2026-09-08）：rel_idx 只含例外项（macro/holdings 改向、skip 剔除），
        # 其余一律「保留 + micro」。剔除项**真正不进下游**——此前被兜底塞回 micro，
        # 既白烧 token 又推高 micro 段截断率（今日 11 次）。
        g_macro, g_micro, h_art, g_skip = route_by_gate(arts, rel_idx, watch_codes)
        print(f"门卫分流 → macro {len(g_macro)} / micro {len(g_micro)} / "
              f"holdings {len(h_art)} / 剔除 {g_skip}")
    else:
        h_art, others = split_holdings_related(arts, watch_codes)
        g_macro = [a for a in others if _GLOBAL_RE.search(str(a.get("title") or ""))]
        g_micro = [a for a in others if not _GLOBAL_RE.search(str(a.get("title") or ""))]

    # 4) 大/小 并行（各段内分批；空子集短路；--stage 单跑时全量喂该段）
    #    持仓情报（HOLD）**不在这个池子里**——它要吃 macro/micro 的结论做传导，
    #    必须串行排在这两段之后（2026-09-09 方案 A）。
    gate_active = GATE in stages
    results: dict = {}

    def _task(stage: str, arts_sub: list) -> None:
        user = f"新闻输入（{AGENT_CN[stage]}）"
        if lessons_block:
            user += f"\n{lessons_block}"
        d, ok = _run_chunked(stage, arts_sub, user)
        results[stage] = _stage_result(d, ok)

    tasks = []
    if MACRO in stages and (g_macro or not gate_active):
        tasks.append((MACRO, g_macro))
    if MICRO in stages and (g_micro or not gate_active):
        tasks.append((MICRO, g_micro))
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in (ex.submit(_task, s, a) for s, a in tasks):
            f.result()
    for s, _a in tasks:
        results.setdefault(s, {"skipped": True})

    # 4b) 持仓情报：串行在 macro/micro 之后，且必须有素材才跑
    #     （--stage holdings 单跑时 macro/micro 缺席、gate_active=False：
    #       显式单跑=操作员意图，放行，但此时没有素材 → 模型只会输出 C | 0）
    if HOLD in stages:
        if _hold_has_evidence(results, h_art, watch_codes) or not gate_active:
            try:
                hold_user = build_hold_user(watch_codes, results, h_art, lessons_block)
                d, ok = _run_stage(HOLD, hold_user)
                results[HOLD] = _stage_result(d, ok)
            except Exception as exc:  # noqa: BLE001 持仓段失败不能掀翻整期主编产出
                print(f"⚠️ [{AGENT_CN[HOLD]}] 异常：{str(exc)[:120]}")
                results[HOLD] = _stage_result(None, False)
        else:
            print(f"⏭️ [{AGENT_CN[HOLD]}] 无素材（无关注代码 / 无板块宏观结论），跳过")
            results.setdefault(HOLD, {"skipped": True})

    # 5) 主编汇总（含上一版分子连续性 + 各段原始 JSON）
    chief_ok: bool | None = None   # None=未跑（--stage 跳过），True/False=本期产出与否
    if CHIEF in stages:
        seg_parts = [_seg_compact(s, results.get(s)) for s in (MACRO, MICRO, HOLD)]
        prev = {}
        try:
            prev = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        chief_user = (f"本窗口 {_bj_fmt(_iso(since_iso))} → {_bj_fmt(_iso(until_iso))} "
                      f"的三段分析如下；请汇总『新闻分子』。\n"
                      f"{watch_line}\n")
        if prev:
            prev_ref = {k: prev.get(k) for k in ("macro", "themes", "open_risks", "watch_list")}
            chief_user += "\n--- 上一版分子（保持连续口径参考）---\n" \
                + json.dumps(prev_ref, ensure_ascii=False)[:1600] + "\n"
        chief_user += "\n" + ("\n".join(seg_parts) if seg_parts else "（三段均无输出）")
        d, ok = _run_stage(CHIEF, chief_user)
        chief_ok = d is not None
        if d:
            # 主题按名去重（模型偶发同一主题多行）；confidence 缺失回退三段均值
            themes_dedup = list({t.get("name"): t
                                 for t in _list_of_dicts(d.get("themes"))}.values())
            seg_confs = [_fnum((results.get(k) or {}).get("confidence"))
                         for k in (MACRO, MICRO, HOLD)]
            seg_confs = [v for v in seg_confs if v > 0]
            conf = _fnum(d.get("confidence")) or (
                round(sum(seg_confs) / len(seg_confs), 2) if seg_confs else 0.0)
            brief = {
                "ts": now.isoformat(),
                "window": {"start": since_iso, "end": until_iso},
                "counts": {"raw": len(raw), "cand": len(arts), "gate_skip": g_skip},
                "warns": warns, "folder_failures": fails,
                "segments": results,                       # 各段原始 JSON（审计/前端可选读）
                "macro": _maybe_dict(results.get(MACRO)),  # 宏观分子 = ② 段（主编不重述）
                "themes": themes_dedup,
                "watch_list": _list_of_dicts(d.get("watch_list")),
                "open_risks": _list_of_dicts(d.get("open_risks")),
                "holdings": _chief_holdings(d.get("holdings"), results.get(HOLD)),
                "market_notes": d.get("market_notes") or "",
                "confidence": conf,
                "editor_note": d.get("editor_note") or "",
            }
            brief["text"] = brief_to_text(brief)
            save_brief(brief)
            # 盘中异动关注（micro 事件 + 主编 watch）→ L2 轮询合并，视野不闭集
            iw = update_intraday_watch(results.get(MICRO), brief.get("watch_list"))
            print(f"✓ 盘中异动关注 {len(iw['items'])} 只 → {INTRA_WATCH_FILE.name}")
            print(f"✓ 主编分子已落盘 → {BRIEF_FILE}（confidence={brief['confidence']:.2f}）")
            print(brief["text"])

    new_st = state_after_run(
        st, stages, until_iso, advance=(not fails) and chief_ok is not False,
        skip_reason=("本窗口有桶失败" if fails
                     else "主编无产出（截断/解析失败）"),
        last_stages=list(stages), last_ok=now.isoformat(),
        last_run={**_RUN_STATS, "ts": now.isoformat(), "folder_failures": fails})
    if chief_ok:
        new_st["last_chief_ok"] = now.isoformat()
    elif chief_ok is False:
        # 主编本期无产出（截断/解析失败）→ 告警与排查依据（兜底轮 last_ok 会照常刷新，
        # 不能再让 state 看起来"一切正常"，2026-09-08 静默 4 小时实录）
        new_st["last_chief_fail"] = now.isoformat()
    # 事件风险清单重建：本期 micro 负面事件刚落盘，立刻进清单才能影响下一轮买入
    # （解禁侧由 event_radar 18:00 那轮刷新；这里补的是新闻侧时效，2026-09-08）
    try:
        from risk_list import refresh as _risk_refresh

        st_risk = _risk_refresh()
        print(f"✓ 事件风险清单重建：{st_risk['items']} 只禁买 / {st_risk['warns']} 只质押告警"
              f" / {st_risk['watch']} 只监管关注（只提醒）")
    except Exception as exc:  # noqa: BLE001 清单失败不影响新闻产出
        print(f"⚠️ 事件风险清单重建失败：{str(exc)[:100]}")
    save_state(new_st)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="新闻 Agent 管线（全 v4-flash）")
    ap.add_argument("--since", default="", help="北京 'YYYY-MM-DD HH:MM'（默认增量游标）")
    ap.add_argument("--until", default="", help="北京 'YYYY-MM-DD HH:MM'（默认 now）")
    ap.add_argument("--stage", default="",
                    help="只跑单段：gate|macro|micro|holdings|chief")
    ap.add_argument("--force", action="store_true",
                    help="跳过节假日闸门强制运行（run_pipeline 的 force 参数）")
    return ap


def cli() -> int:
    a = build_arg_parser().parse_args()
    stages = ALL_STAGES
    if a.stage:
        stage_map = {}
        for _s in ALL_STAGES:
            stage_map[_s] = (_s,)
            stage_map[_s.removeprefix("news-")] = (_s,)
        stages = stage_map.get(a.stage)
        if not stages:
            print(f"未知 stage: {a.stage}（可选 {','.join(stage_map)}）", file=sys.stderr)
            return 2

    def _to_iso(local: str) -> str:
        return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ).isoformat()

    try:
        return run_pipeline(
            since_iso=_to_iso(a.since) if a.since else "",
            window_until=_to_iso(a.until) if a.until else "",
            stages=stages, force=a.force)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 新闻管线失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
