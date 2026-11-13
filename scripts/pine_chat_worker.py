#!/usr/bin/env python3
"""Pine 策略对话 worker（宿主侧）。

界面在右栏「对话」发消息时，API 只往 ``data/pine_library/chat/queue/<job_id>.json``
写一个请求——容器里没有 bwrap，也不该执行模型产出的代码。本脚本在宿主上消费队列：

  1. 读上下文：生效源码 + 备注 + 最近一次沙箱回测报告 + 最近若干轮对话
  2. 调模型（复用 ``scripts/pine_to_pyne.py`` 的 ``_chat``，关思考）出回答
  3. 写 ``chat/<id>/thread.jsonl``（界面回放）与 ``chat/jobs/<job_id>.json``（界面轮询）

``mode=ask`` 只出文本。``mode=edit`` 让模型产出**新 Pine**，落到
``chat/<id>/<job_id>/candidate.pine``，再交给 ``scripts/pine_transpile_worker.py``
的同一套链路（转写 → 静态闸 → bwrap 沙箱回测）跑出「改后」指标，与「改前」对比
写进 job——未点「采用」前不碰正式源码。

cron::

    * * * * * cd /home/zbox/projects/quantmind && /usr/bin/python3 scripts/pine_chat_worker.py >> logs/pine_chat_worker.log 2>&1

手动跑一次：``/usr/bin/python3 scripts/pine_chat_worker.py``
"""
from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.agent_arena import pine_library as pl  # noqa: E402
from scripts import pine_transpile_worker as ptw        # noqa: E402
from scripts.pine_to_pyne import _chat                   # noqa: E402

MAX_SOURCE_CHARS = 12000     # 源码进上下文的上限（语料最大 512KB，不能整份塞）
THREAD_TURNS = 12            # 带进上下文的历史轮数
SUPPORTED_MODES = ("ask", "edit")
DEFAULT_SYMBOL = "600309.SH"  # 候选回测的兜底标的（正式报告里没有标的时用）

SYSTEM = """你是 A 股量化策略研究助手，帮用户理解并改进 TradingView Pine 策略。

上下文里会给你：策略元信息、Pine 源码、用户的备注、最近一次回测结果。

规则：
1. 只依据给出的源码与回测数据回答，**不要编造**没给出的数字或行情。
2. 本机没有 Pine 解释器：源码先被转写成 Pyne-Python，再在断网沙箱里回测。
   讨论收益时提醒这一点（转写可能有偏差，回测是历史数据、不含未来函数）。
3. 回答要具体：指到变量名/行号级的逻辑，别讲空泛套话。
4. 该说风险就说风险（过拟合、参数敏感、样本内外差异、A股涨跌停与 T+1 限制）。
5. 用中文，控制在 400 字以内，必要时用小标题或列表。"""

EDIT_SYSTEM = """你是 A 股量化策略研究助手，这一轮要**按用户要求改源码**。

输出格式（严格遵守，别加别的）：
1. 先用 1–3 句中文说清改了什么、为什么（不要贴代码、不要写小标题）；
2. 然后给**一个** ```pine 代码块，里面是完整可编译的修改后 Pine 源码。

规则：
1. 只改用户要求的地方，其余逐字保留（注释、缩进、变量名、顺序都别动）。
2. 保留 `//@version=5` 与 `strategy(...)` 声明，Pine 版本不变。
3. 仓位口径必须保持 `initial_capital=100000`、`default_qty_value=95`、
   `default_qty_type=strategy.percent_of_equity`——跨策略要可比，改它会被静态闸打回。
4. 不要引入外部请求、文件读写、随机数等不可回测的东西。
5. 改法在 Pine 里做不到（语法限制 / 缺数据）就直说做不到，别硬改。
6. 改完的源码会被自动转写 + 断网沙箱回测，结果和原版并列给用户看。"""

# 对比卡展示的指标（其余 28 项在报告里，界面按需取）。
COMPARE_KEYS = (("Net profit", "净收益"), ("Buy & hold return", "买入持有"),
                ("Max equity drawdown", "最大回撤"), ("Profit factor", "盈亏因子"),
                ("Percent profitable", "胜率"), ("Sharpe ratio", "夏普"))
PINE_BLOCK = re.compile(r"```(?:pine|pinescript)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_job(job_id: str, **fields) -> dict:
    """写 jobs/<job_id>.json（原子替换）。界面只读这个文件判断进度。"""
    path = pl.chat_jobs() / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {}
    if path.exists():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            job = {}
    job.update(fields)
    job["job_id"] = job_id
    job["updated"] = _now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return job


def _drop_request(path: Path) -> None:
    """摘牌。目录属主不对时 unlink 会失败——把原因和修法说清楚，别只留个 errno。"""
    try:
        path.unlink(missing_ok=True)
    except PermissionError as exc:
        raise PermissionError(
            f"无权限删除队列文件 {path}：目录属主不是本用户（容器以 root 建的目录）。"
            f"修：docker exec quantmind chown 1000:1000 {path.parent}"
        ) from exc


def _append_thread(item_id: str, row: dict) -> None:
    """追加一行对话（单 worker 串行，直接 append 即可）。"""
    path = pl.chat_dir() / item_id / "thread.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pct(stats: dict, key: str) -> str:
    v = (stats.get(key) or {}).get("pct")
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"


def _system_for(mode: str) -> str:
    return EDIT_SYSTEM if mode == "edit" else SYSTEM


def extract_pine(text: str) -> str:
    """取模型输出里的 Pine 代码块（优先带 //@version / strategy( 的那个）。"""
    blocks = [b.strip() for b in PINE_BLOCK.findall(text or "")]
    for b in blocks:
        if "//@version" in b or "strategy(" in b or "indicator(" in b:
            return b
    return blocks[0] if blocks else ""


def strip_pine_block(text: str) -> str:
    """把代码块从模型输出里去掉，剩下的就是给人看的说明。"""
    return PINE_BLOCK.sub("", text or "").strip()


def metrics(report: dict) -> dict:
    """对比卡用的指标（缺的留 None，界面显示「—」而不是编一个数）。"""
    st = (report or {}).get("stats") or {}
    out = {}
    for key, label in COMPARE_KEYS:
        cell = st.get(key) or {}
        out[key] = {"label": label, "value": cell.get("value"), "pct": cell.get("pct")}
    out["trades"] = {"label": "成交笔数", "value": (report or {}).get("trades")}
    return out


def compare(before: dict, after: dict, symbol: str, adj: str) -> dict:
    return {"symbol": symbol, "adj": adj,
            "before": metrics(before), "after": metrics(after)}


def run_candidate(item_id: str, job_id: str, pine_text: str, symbol: str, adj: str,
                  model: str) -> tuple[dict, Path]:
    """候选落盘 → 走正式链路（转写 → 静态闸 → 沙箱回测），返回 (job 字段, 候选目录)。

    路径改道（``--source``/``--out``）后与正式产物完全隔离；进度写进对话 job
    （合并写，不动已有字段），成功状态是 ``staged``——还没跑完，等本函数返回后补对比。
    """
    cand_dir = pl.chat_dir() / item_id / job_id
    cand_dir.mkdir(parents=True, exist_ok=True)
    pine_path = cand_dir / pl.CANDIDATE_PINE
    tmp = pine_path.with_suffix(".pine.tmp")
    tmp.write_text(pine_text, encoding="utf-8")
    tmp.replace(pine_path)

    return ptw.process({"id": item_id, "symbol": symbol, "adj": adj, "model": model,
                        "source": str(pine_path)},
                       item_dir=cand_dir,
                       job_path=pl.chat_jobs() / f"{job_id}.json",
                       success_status="staged"), cand_dir


def build_messages(item_id: str, question: str, history: list[dict],
                   mode: str = "ask") -> list[dict]:
    """system（策略上下文）+ 历史轮次 + 本轮问题。"""
    src = pl.get_source(item_id)
    note = pl.get_note(item_id)
    report = pl.read_report(item_id)

    ctx = [f"策略 #{item_id}《{src.get('title') or src.get('file')}》",
           f"分类：{src.get('category')} · Pine v{src.get('version')} · "
           f"{src.get('lines')} 行 · 语料来源 {src.get('source_kind')}"]
    if note.get("note") or note.get("tags"):
        line = f"用户备注：{note.get('note') or '（无）'}"
        if note.get("tags"):
            line += f"（标签：{'、'.join(note['tags'])}）"
        ctx.append(line)
    if report:
        st = report.get("stats") or {}
        ctx.append(f"最近一次沙箱回测（{report.get('symbol')} / {report.get('adj')}）："
                   f"净收益 {_pct(st, 'Net profit')} · 买入持有 {_pct(st, 'Buy & hold return')} · "
                   f"最大回撤 {_pct(st, 'Max equity drawdown')} · 成交 {report.get('trades')} 笔")
    else:
        ctx.append("最近一次回测：还没有（讨论收益时请说明无数据）")

    text = (src.get("source") or "")[:MAX_SOURCE_CHARS]
    if len(src.get("source") or "") > MAX_SOURCE_CHARS:
        text += "\n// …（源码过长已截断）"
    ctx.append("当前 Pine 源码：\n```pine\n" + text + "\n```")

    msgs = [{"role": "system", "content": _system_for(mode) + "\n\n" + "\n\n".join(ctx)}]
    msgs += [{"role": m.get("role"), "content": str(m.get("content") or "")}
             for m in history if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": question})
    return msgs


def _finish_ask(job_id: str, item_id: str, reply: str, usage, truncated: bool,
                mode: str) -> dict:
    _append_thread(item_id, {"timestamp": _now(), "role": "assistant", "content": reply,
                             "job_id": job_id, "mode": mode, "usage": usage})
    return _write_job(job_id, id=item_id, status="done", stage="done",
                      reply=reply, usage=usage, truncated=bool(truncated), error="")


def _baseline(item_id: str) -> dict:
    """「改前」那一列的来源与新鲜度。

    基线取的是**最近一次正式回测报告**，它可能比当前源码还旧（用户手改过源码、
    或闸门刚收紧还没重跑）——那样前后对比就不是同一份源码，必须在界面上说清。
    """
    rp = pl.report_path(item_id)
    src = pl.source_path(item_id)
    try:
        at = datetime.fromtimestamp(rp.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        stale = rp.stat().st_mtime < src.stat().st_mtime
    except OSError:
        return {"before_at": "", "before_stale": False}
    return {"before_at": at, "before_stale": stale}


def _finish_edit(job_id: str, item_id: str, raw: str, usage, truncated: bool,
                 model: str, symbol: str, adj: str) -> dict:
    """改策略：模型出 Pine → 同一套链路跑候选 → 前后指标对比。"""
    pine = extract_pine(raw)
    explain = strip_pine_block(raw)
    if not pine:
        _append_thread(item_id, {"timestamp": _now(), "role": "assistant", "content": raw,
                                 "job_id": job_id, "mode": "edit"})
        return _write_job(job_id, id=item_id, status="failed", stage="edit",
                          error="模型没给出 Pine 代码块", reply=raw, usage=usage)

    _write_job(job_id, id=item_id, status="running", stage="transpile", error="")
    try:
        fields, cand_dir = run_candidate(item_id, job_id, pine, symbol, adj, model)
    except Exception as exc:  # noqa: BLE001
        return _write_job(job_id, id=item_id, status="failed", stage="edit",
                          error=f"候选链路失败: {exc}", reply=explain, usage=usage)

    if fields.get("status") != "staged":
        detail = fields.get("error") or "候选没跑通"
        _append_thread(item_id, {"timestamp": _now(), "role": "assistant",
                                 "content": f"{explain}\n\n（候选没跑通：{detail}）",
                                 "job_id": job_id, "mode": "edit"})
        return _write_job(job_id, id=item_id, status="failed",
                          stage=fields.get("stage") or "edit", error=detail,
                          reply=explain, problems=fields.get("problems") or [],
                          usage=usage)

    got = pl.candidate(item_id, job_id)
    before = pl.read_report(item_id)
    cand = {"pine": pine, "problems": got["meta"].get("problems") or [],
            "stats": got["report"].get("stats") or {},
            "trades": got["report"].get("trades"),
            "symbol": symbol, "adj": adj,
            "compare": {**compare(before, got["report"], symbol, adj), **_baseline(item_id)}}
    reply = explain or "已生成候选。"
    _append_thread(item_id, {"timestamp": _now(), "role": "assistant",
                             "content": f"{reply}\n\n（已生成候选，点「采用」才会写入正式源码）",
                             "job_id": job_id, "mode": "edit"})
    return _write_job(job_id, id=item_id, status="done", stage="done", reply=reply,
                      candidate=cand, usage=usage, truncated=bool(truncated), error="")


def process(req: dict) -> dict:
    """处理一条对话请求，返回 job 字段。"""
    job_id = str(req.get("job_id") or "").strip()
    item_id = str(req.get("id") or "").strip()
    message = str(req.get("message") or "").strip()
    mode = str(req.get("mode") or "ask")
    model = str(req.get("model") or pl.CHAT_MODEL)
    if not job_id or not item_id or not message:
        raise ValueError("请求缺 job_id / id / message")

    _write_job(job_id, id=item_id, status="running", stage="context",
               mode=mode, message=message, error="", reply="")
    _drop_request(pl.chat_queue() / f"{job_id}.json")   # 先落 running 再摘牌

    if mode not in SUPPORTED_MODES:
        return _write_job(job_id, id=item_id, status="failed", stage="mode",
                          error=f"未知模式: {mode}")

    try:
        history = pl.chat_thread(item_id, limit=THREAD_TURNS)["messages"]
        messages = build_messages(item_id, message, history, mode)
    except ValueError as exc:
        return _write_job(job_id, id=item_id, status="failed", stage="context",
                          error=str(exc))

    _append_thread(item_id, {"timestamp": _now(), "role": "user", "content": message,
                             "job_id": job_id, "mode": mode})
    _write_job(job_id, id=item_id, status="running", stage="llm", error="")

    try:
        reply, usage, truncated = _chat(messages, model, think=False)
    except Exception as exc:  # noqa: BLE001
        _write_job(job_id, id=item_id, status="failed", stage="llm",
                   error=f"模型调用失败: {exc}")
        raise
    if not reply:
        return _write_job(job_id, id=item_id, status="failed", stage="llm",
                          error="模型返回空内容")

    if mode == "edit":
        report = pl.read_report(item_id) or {}
        symbol = str(req.get("symbol") or report.get("symbol") or DEFAULT_SYMBOL)
        return _finish_edit(job_id, item_id, reply, usage, truncated, model,
                            symbol, str(report.get("adj") or "backward"))
    return _finish_ask(job_id, item_id, reply, usage, truncated, mode)


def _claim_lock():
    """独占锁：cron 每分钟起一次，上一轮没跑完就安静退出（与转写 worker 各自独立）。"""
    pl.chat_dir().mkdir(parents=True, exist_ok=True)
    fh = open(pl.chat_dir() / "worker.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> int:
    lock = _claim_lock()
    if lock is None:
        print(f"{_now()} 上一轮还在跑，跳过")
        return 0
    if not pl.chat_queue().is_dir():
        return 0

    reqs = sorted(pl.chat_queue().glob("*.json"))
    if not reqs:
        return 0
    print(f"{_now()} 待处理 {len(reqs)} 条")
    for path in reqs:
        t0 = time.time()
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{_now()} 请求文件损坏 {path.name}: {exc}")
            _drop_request(path)
            continue
        try:
            job = process(req)
            print(f"{_now()} {req.get('id')} → {job.get('status')}/{job.get('stage')} "
                  f"（{time.time() - t0:.1f}s）{job.get('error') or ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"{_now()} {req.get('id')} 处理异常: {exc}")
            try:
                _write_job(str(req.get("job_id") or "unknown"), id=str(req.get("id") or ""),
                           status="failed", stage="worker", error=f"worker 异常: {exc}")
                _drop_request(path)
            except Exception as exc2:  # noqa: BLE001
                # 连失败都写不下去（例如 jobs/ 目录也被容器建成 root）——至少要留在 cron 日志里
                print(f"{_now()} 写失败状态也失败: {exc2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
