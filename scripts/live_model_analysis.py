#!/usr/bin/env python3
"""实盘模型对话轮（「模型对话」tab「立即分析」的宿主执行端）。

上游 ``live_hourly_analysis.py``（quant-Trader）随平台退役后，「立即分析」按钮
一直置灰（A6）。本脚本是其**对话语义的最小复活**：读 QM 桥的实盘账户 + 持仓
（只读），连同当日的新闻分子摘要，喂给各模型各出一轮分析，按 arena 日志格式
落盘 ``data/agent_data_astock/{sig}/log/{date}/log.jsonl`` → 前端「模型对话」
直接可读（复用 news_brief.append_log 的同一结构）。

安全边界：**只读查询 + LLM 调用，绝不下单**——本脚本不 import 任何交易执行
代码；QM 的下单走决策轮/信号链，与对话轮解耦（上游「时段内可真下单」语义
不在此恢复）。

真话纪律（T3-2，审计 C5/H13）：
* **桥挂 = 无账户模板**：没有账户数据时提示词**禁止要求点评持仓**（也不许模型
  编造持仓/账户数字），并给日志落 ``data_gaps=['account_unreachable']`` 机器
  可读标记 → 前端卡片挂「无实盘账户数据」横幅；
* **截断不落盘**：``finish_reason=='length'`` 的回复整轮作废（重试 1 次后仍截断
  即放弃并如实报错）——半截分析混进「模型对话」与完整分析无法区分。

用法：
    python3 scripts/live_model_analysis.py                 # 全部模型（名册）
    python3 scripts/live_model_analysis.py --agents deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CN_TZ = timezone(timedelta(hours=8))
LOCK = ROOT / "data" / "logs" / ".live_model_analysis.lock"
LOG_DIR = ROOT / "data" / "agent_data_astock"
BRIEF_FILE = ROOT / "data" / "news_brief" / "latest.json"
TIMEOUT_S = 180
MAX_TOKENS = 4000

#: 名册：签名 →（凭据 env 前缀, 实际调用的模型名）。签名即落盘目录名，与前端
#: 模型列表（扫描 agent_data_astock/ 目录）天然一致。
ROSTER: dict[str, tuple[str, str]] = {
    "deepseek-v4-flash": ("OPENAI", "deepseek-v4-flash"),
    "deepseek-v4-pro": ("OPENAI", "deepseek-v4-pro"),
    "glm-5.3-flash": ("GLM", "glm-5.3-flash"),
}


def _load_env() -> dict[str, str]:
    """读仓库根 .env（与 news_brief.call_llm 同口径：不覆盖已有键）。"""
    env: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in env:
                env[k] = v.strip().strip('"')
    except OSError:
        pass
    return env


def _session_label(now: datetime) -> str:
    """北京时刻 → 盘面会话标签（只影响提示词文案）。"""
    if now.weekday() >= 5:
        return "周末休市"
    hm = now.hour * 60 + now.minute
    if 555 <= hm <= 695:  # 09:15–11:35
        return "早盘交易时段"
    if 775 <= hm <= 905:  # 12:55–15:05
        return "午盘交易时段"
    if hm < 555:
        return "盘前"
    if 695 < hm < 775:
        return "午间休市"
    return "盘后"


def _bridge_account(env: dict[str, str]) -> dict | None:
    """只读拉账户 + 持仓；桥不可达返回 None（提示词降级为「无账户数据」）。"""
    import requests

    url = (env.get("TDX_BRIDGE_URL") or "").rstrip("/")
    token = env.get("TDX_BRIDGE_TOKEN") or ""
    if not url:
        return None
    try:
        resp = requests.post(
            f"{url}/api/v1/account/query",
            json={"account_type": "stock"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001 桥不可达 → 降级提示词，不中断本轮
        print(f"⚠️ 桥账户查询失败（降级为无持仓上下文）: {e}")
        return None


def _bridge_price(env: dict[str, str], code: str) -> float:
    """单只现价（桥 get_market_snapshot 直查）；失败返回 0（提示词留空）。"""
    import requests

    from backend.shared.stock_utils import StockCodeUtil

    url = (env.get("TDX_BRIDGE_URL") or "").rstrip("/")
    token = env.get("TDX_BRIDGE_TOKEN") or ""
    if not url:
        return 0.0
    try:
        resp = requests.post(
            f"{url}/api/v1/tdx/call",
            json={
                "method": "get_market_snapshot",
                "params": {"stock_code": StockCodeUtil.to_suffix(code) or code},
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        if not isinstance(result, dict):
            return 0.0
        return float(result.get("Now") or result.get("now") or 0) or 0.0
    except Exception:  # noqa: BLE001 单只失败静默（该行现价留空）
        return 0.0


def _news_block() -> str:
    """当日新闻分子摘要（容错：文件缺失/字段缺失都返回空串，不挡本轮）。"""
    try:
        brief = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(brief, dict):
        return ""
    seg = brief.get("segments") or {}
    macro = seg.get("news-macro") or brief.get("macro") or {}
    micro = seg.get("news-micro") or brief.get("micro") or {}
    hold = seg.get("news-holdings") or brief.get("holdings") or {}
    lines: list[str] = [
        f"（窗口 {brief.get('window', {}).get('start', '?')} ~ "
        f"{brief.get('window', {}).get('end', '?')}）"
    ]
    if isinstance(macro, dict) and macro.get("view"):
        lines.append(f"- 大盘方向：{macro['view']}")
    if isinstance(macro, dict):
        for r in (macro.get("risks") or [])[:3]:
            lines.append(f"- 风险：{r}")
    if isinstance(micro, dict):
        themes = micro.get("themes") or []
        if themes:
            lines.append(f"- 热点主题：{'、'.join(str(t) for t in themes[:8])}")
    if isinstance(hold, dict):
        for h in (hold.get("action_hints") or [])[:5]:
            lines.append(f"- 持仓相关：{h}")
        for c in (hold.get("cross_risks") or [])[:3]:
            lines.append(f"- 交叉风险：{c}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_user_content(
    env: dict[str, str],
    *,
    account_reader=None,
    price_reader=None,
) -> tuple[str, list[str]]:
    """拼账户 + 持仓 + 新闻的提示词正文（供所有模型共用）。

    返回 ``(正文, data_gaps)``——``data_gaps`` 是**机器可读的数据缺口标记**
    （当前唯一取值 ``account_unreachable``），随日志落盘供前端挂横幅；提示词
    同时降级为「无账户模板」（**绝不要求点评持仓**，C5：编出来的持仓点评比
    不分析更坏）。

    ``account_reader``/``price_reader`` 缺省走真实桥；测试注入替身。
    """
    read_account = account_reader or _bridge_account
    read_price = price_reader or _bridge_price
    now = datetime.now(CN_TZ)
    gaps: list[str] = []
    parts = [
        f"【时间】{now.strftime('%Y-%m-%d %H:%M')}（北京时间，{_session_label(now)}）"
    ]

    acct = read_account(env)
    has_positions = False
    if acct:
        asset = acct.get("asset") or {}
        positions = acct.get("positions") or []
        parts.append(
            "【实盘账户】\n"
            f"- 总资产 {asset.get('asset', '?')} ｜ 可用资金 {asset.get('cash', '?')} "
            f"｜ 持仓市值 {asset.get('market_value', '?')}"
        )
        if positions:
            has_positions = True
            rows = [
                "【当前持仓】",
                "| 代码 | 名称 | 持仓 | 可用 | 成本 | 现价 | 浮动盈亏 |",
                "|---|---|---|---|---|---|---|",
            ]
            for p in positions:
                code = str(p.get("stock_code") or "")
                name = str(p.get("stock_name") or p.get("name") or "")
                total = p.get("total_volume")
                avail = p.get("available_volume")
                cost = float(p.get("cost_price") or 0)
                last = read_price(env, code) if code else 0.0
                if last > 0 and cost > 0:
                    pnl = f"{(last / cost - 1) * 100:+.2f}%"
                    last_s = f"{last:.2f}"
                else:
                    pnl, last_s = "—", "—"
                rows.append(
                    f"| {code} | {name} | {total} | {avail} | {cost:.3f} | {last_s} | {pnl} |"
                )
            parts.append("\n".join(rows))
        else:
            parts.append("【当前持仓】空仓")
    else:
        gaps.append("account_unreachable")
        parts.append("【实盘账户】桥不可达，本轮无账户/持仓数据（只按新闻面分析）")

    news = _news_block()
    if news:
        parts.append("【当日新闻分子】\n" + news)

    parts.append(
        _requirement_block(has_account=bool(acct), has_positions=has_positions)
    )
    return "\n\n".join(parts), gaps


def _requirement_block(*, has_account: bool, has_positions: bool) -> str:
    """尾段要求模板——**随数据齐缺三态降级**（C5 核心）。

    无账户数据时逐字禁止「点评持仓/编造持仓」：桥挂的轮次只许按新闻面说话。
    """
    tail = (
        "输出中文 markdown，简洁、先结论后理由；你的输出展示在实盘看板的「模型对话」里，"
        "只输出观点与建议（本系统不会据此自动下单）。"
    )
    if not has_account:
        return (
            "【要求】本轮**没有**实盘账户与持仓数据（桥不可达），给出本轮盘中分析：\n"
            "1) **禁止**点评持仓、禁止编造任何持仓或账户数字——你没有这些数据；\n"
            "2) 只按上述新闻面分析大盘与板块的风险与机会；\n"
            "3) 明确说明本轮结论缺少账户上下文，仅供参考。\n" + tail
        )
    if not has_positions:
        return (
            "【要求】账户当前**空仓**（无持仓），给出本轮盘中分析：\n"
            "1) 不要点评持仓——当前没有持仓，也禁止编造；\n"
            "2) 结合新闻面指出风险与机会；\n"
            "3) 如认为值得关注，给出候选方向与入场触发条件（仅为观察，不是指令）。\n" + tail
        )
    return (
        "【要求】围绕上述实盘账户给出本轮盘中分析：\n"
        "1) 逐一点评持仓（结合成本与现价）；\n"
        "2) 结合新闻面指出风险与机会；\n"
        "3) 给出操作倾向（加仓/减仓/持有）与触发条件。\n" + tail
    )


class TruncatedOutputError(RuntimeError):
    """输出被 max_tokens 截断（与 ``news_brief.TruncatedOutputError`` 同语义）：
    半截分析混进「模型对话」与完整分析无法区分，**整轮作废、绝不落盘**。"""

    def __init__(self, content: str, usage: dict | None):
        super().__init__("输出被 max_tokens 截断，整轮作废")
        self.content = content
        self.usage = usage


def call_model(
    env: dict[str, str], sig: str, model: str, user_content: str
) -> tuple[str, dict | None]:
    """OpenAI 兼容调用（各模型走各自供应商；失败重试 1 次后 raise）。"""
    prefix, _ = ROSTER[sig]
    base = (env.get(f"{prefix}_API_BASE") or "").rstrip("/")
    key = env.get(f"{prefix}_API_KEY") or ""
    if prefix == "OPENAI" and not base:
        # 本仓 .env 的 OpenAI 兼容键有两种写法，兜底 DEEPSEEK_*
        base = (env.get("DEEPSEEK_BASE_URL") or "").rstrip("/")
        key = key or env.get("DEEPSEEK_API_KEY") or ""
    if not base or not key:
        raise RuntimeError(f"{prefix}_API_BASE/KEY 缺失")
    import requests

    system = (
        f"你是 {model} 模型驱动的 A股 实盘交易助手盘中持仓分析师。"
        "分析冷静客观，给可执行的操作建议，注意 A股 T+1 规则与风险。输出中文 markdown。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {}
            content = (
                str(msg.get("content") or "").strip()
                or str(msg.get("reasoning_content") or "").strip()
            )
            finish = str(choice.get("finish_reason") or "")
            usage = data.get("usage") or None
            if usage:
                usage = {
                    k: int(usage.get(k) or 0)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            # max_tokens 截断：半截答案（含 reasoning 兜底捞出的思考草稿）整轮作废
            # ——重试 1 次后仍截断即放弃，绝不落盘（照 news_brief.call_llm 的 TRUNC 语义）
            if finish == "length":
                raise TruncatedOutputError(content, usage)
            if not content:
                raise RuntimeError("空回复")
            return content, usage
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                print(f"  ⚠️ {sig} 调用失败，重试 1 次: {exc}")
    raise last_exc  # type: ignore[misc]


def append_log(
    user: str,
    content: str,
    sig: str,
    usage: dict | None = None,
    data_gaps: list[str] | None = None,
) -> Path:
    """落盘对话日志（与 news_brief.append_log 同结构 → 前端直接可读）。

    ``data_gaps`` 随条目落盘（前端据此在卡片挂「无实盘账户数据」横幅）；
    截断轮**根本不会走到这里**（call_model 直接 raise）。
    """
    now = datetime.now(CN_TZ)
    log_dir = LOG_DIR / sig / "log" / now.strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": now.isoformat(),
        "signature": sig,
        "new_messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": content},
        ],
    }
    if usage:
        entry["usage"] = usage
    if data_gaps:
        entry["data_gaps"] = list(data_gaps)
    path = log_dir / "log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘模型对话轮（只出观点，不下单）")
    ap.add_argument("--agents", default="", help="逗号分隔的模型签名；缺省=名册全部")
    args = ap.parse_args()

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有分析实例在跑，本轮跳过")
        return 1

    try:
        targets = [a.strip() for a in args.agents.split(",") if a.strip()] or list(
            ROSTER
        )
        env = _load_env()
        unknown = [t for t in targets if t not in ROSTER]
        for u in unknown:
            print(f"⚠️ 跳过 {u}：不在模型名册（{', '.join(ROSTER)}）")
        runnable = [t for t in targets if t in ROSTER]
        if not runnable:
            print("没有可运行的模型")
            return 2

        user_content, data_gaps = build_user_content(env)
        if data_gaps:
            print(
                "⚠️ 数据缺口：" + ",".join(data_gaps)
                + "（提示词已降级为无账户模板，日志将带缺口标记）"
            )
        ok, failed = [], []
        for sig in runnable:
            _, model = ROSTER[sig]
            try:
                content, usage = call_model(env, sig, model, user_content)
                append_log(user_content, content, sig, usage, data_gaps)
                ok.append(f"{sig} {len(content)}字")
                print(f"✅ {sig} 完成（{len(content)} 字）")
            except Exception as e:  # noqa: BLE001 单模型失败不挡其余
                failed.append(f"{sig}:{e}")
                print(f"❌ {sig} 失败: {e}")
        if ok:
            print(f"[SUMMARY] {len(ok)}/{len(runnable)} 模型完成：" + " · ".join(ok))
            return 0
        print("[SUMMARY] 全部模型失败：" + " · ".join(failed)[:180])
        return 2
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
