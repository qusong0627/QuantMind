# ── 移植注（2026-09-30，arena 原生迁移 A3）─────────────────────────────
# 逐字节副本：quant-Trader `backend/services/pine_library.py`（上游已退役）。
# 改写点两处：① 落点 `parents[2]/data/pine_library` 在 QM 包结构下会指错，
# 改 QM 数据根 `<root>/pine_library`（A1 已落；转写产物随之 `<root>/pine_transpile`，
# 同已落）；env `PINE_LIBRARY_DIR` 覆盖保留。**落点全部惰性化**——上游是模块级
# 常量（LIB_DIR/INDEX/.../VERSIONS_DIR 11 个），import 即固化路径，测试夹具
# 进程内切 QM_ARENA_DATA_ROOT 会失效并打到真实落点（arena_config 同款纪律）；
# 本版一律函数、引用点 call-time 解析，外部行为无差。
# ② `_check_symbol` 的 `from backend.services import market_lab` 改本包。
# 队列类写面（chat/backtest enqueue）落 `<root>/pine_library/chat/queue` 等；
# 宿主 worker 未迁移（B2），路由层 A3 不挂这些端点（队列无人消费=黑洞）。
# ─────────────────────────────────────────────────────────────────────
"""Pine 策略库：读 scripts/pine_library_index.py 产出的索引，提供列表 / 源码 / 编辑保存。

索引与源码副本都在 ``data/pine_library/``（容器内 /app/data 可读）；桌面原语料容器看不到，
所以索引构建时已把生效源码复制到 ``data/pine_library/source/<id>.pine``。

编辑保存在 ``data/pine_library/edited/<id>.pine``，优先级高于重爬与原始语料，不会被索引重建覆盖。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from backend.services.agent_arena import arena_config

# 落点一律走函数而非模块级常量（同 arena_config 的纪律注释）：常量在 import 时
# 固化路径，QM_ARENA_DATA_ROOT 进程内切换（测试夹具最典型）会失效并打到真实落点。
def lib_dir() -> Path:
    """策略库根（env PINE_LIBRARY_DIR 覆盖，默认 QM 数据根/pine_library）。"""
    return Path(os.environ.get("PINE_LIBRARY_DIR")
                or arena_config.data_root() / "pine_library")


def index_file() -> Path:
    return lib_dir() / "index.json"


def source_dir() -> Path:
    return lib_dir() / "source"


def edited_dir() -> Path:
    return lib_dir() / "edited"

MAX_SOURCE_BYTES = 512 * 1024
ID_RE = re.compile(r"^(?:\d{4}|x\d{3})$")


class LibraryError(ValueError):
    """入参不合法（无效 id / 源码过大）。"""


def _check_id(item_id: str) -> str:
    item_id = (item_id or "").strip()
    if not ID_RE.match(item_id):
        raise LibraryError(f"无效的策略 id: {item_id!r}")
    return item_id


def load_index() -> dict:
    idx = index_file()
    if not idx.exists():
        return {"generated": "", "source_dir": "", "total": 0, "usable": 0,
                "categories": [], "items": []}
    return json.loads(idx.read_text(encoding="utf-8"))


def _match(item: dict, q: str) -> bool:
    q = q.lower()
    return any(q in str(item.get(k, "")).lower()
               for k in ("id", "title", "title_en", "file", "category"))


def list_items(category: str = "", q: str = "", limit: int = 200, offset: int = 0) -> dict:
    """分类 + 关键词过滤后的列表（不带源码，界面列表用）。"""
    idx = load_index()
    items = idx.get("items", [])
    if category:
        items = [it for it in items if it.get("category") == category]
    if q:
        items = [it for it in items if _match(it, q)]
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "generated": idx.get("generated", ""),
        "total": idx.get("total", 0),
        "usable": idx.get("usable", 0),
        "filtered": total,
        "categories": idx.get("categories", []),
        "items": page,
    }


def _find(idx: dict, item_id: str) -> dict:
    for it in idx.get("items", []):
        if it.get("id") == item_id:
            return it
    raise LibraryError(f"策略不存在: {item_id}")


def source_path(item_id: str) -> Path:
    """当前生效的源码文件（edited/ 优先，与索引构建的来源优先级一致）。"""
    item_id = _check_id(item_id)
    edited_path = edited_dir() / f"{item_id}.pine"
    return edited_path if edited_path.exists() else source_dir() / f"{item_id}.pine"


def get_source(item_id: str) -> dict:
    """单条策略元数据 + 源码（编辑版优先，与索引构建的来源优先级一致）。"""
    item_id = _check_id(item_id)
    idx = load_index()
    item = _find(idx, item_id)
    path = source_path(item_id)
    if not path.exists():
        raise LibraryError(f"策略源码缺失: {item_id}（重建索引试试）")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {**item, "edited": (edited_dir() / f"{item_id}.pine").exists(), "source": text}


def save_source(item_id: str, text: str) -> dict:
    """保存界面里的编辑（写入 edited/，不回写桌面语料）。"""
    item_id = _check_id(item_id)
    if not isinstance(text, str) or not text.strip():
        raise LibraryError("源码不能为空")
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise LibraryError("源码过大（>512KB）")
    get_source(item_id)  # 校验 id 存在
    edited_dir().mkdir(parents=True, exist_ok=True)
    (edited_dir() / f"{item_id}.pine").write_text(text, encoding="utf-8")
    return {"id": item_id, "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bytes": len(text.encode("utf-8")),
            "lines": len(text.splitlines()),
            "indent_ok": any(ln[:1] in (" ", "\t") for ln in text.splitlines())}


def reset_source(item_id: str) -> dict:
    """丢弃编辑，回到索引构建时的生效版本。"""
    item_id = _check_id(item_id)
    path = edited_dir() / f"{item_id}.pine"
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"id": item_id, "removed": existed}


# ---------- 策略备注（每条一份 JSON） ----------
# 索引由 scripts/pine_library_index.py 整体原子重建，备注写进 index.json 下次就没了，
# 所以单独放 notes/<id>.json（与 edited/<id>.pine 同构，天然避免并发写冲突）。

def notes_dir() -> Path:
    return lib_dir() / "notes"
# 备注要同时覆盖库 id（0001 / x001）与内置模板 id（sma_cross），比源码的 _check_id 宽
NOTE_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
NOTE_STATUSES = ("待研究", "观察中", "已采用", "已弃用")
NOTE_BY = ("user", "agent")
MAX_NOTE_CHARS = 20000
MAX_TAGS = 20
EMPTY_NOTE = {"note": "", "tags": [], "rating": None, "status": "待研究", "by": "user"}


def _check_note_id(item_id: str) -> str:
    item_id = (item_id or "").strip()
    if not NOTE_ID_RE.match(item_id):
        raise LibraryError(f"无效的备注 id: {item_id!r}")
    return item_id


def _clean_note(payload: dict) -> dict:
    """校验并归一化备注字段（越界一律报错，不静默截断成别的意思）。"""
    if not isinstance(payload, dict):
        raise LibraryError("备注必须是 JSON 对象")
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise LibraryError("note 必须是字符串")
    if len(note) > MAX_NOTE_CHARS:
        raise LibraryError(f"备注过长（>{MAX_NOTE_CHARS} 字）")

    tags = payload.get("tags", [])
    if isinstance(tags, str):                 # 界面直接传「趋势 多周期」也认
        tags = re.split(r"[,，\s]+", tags)
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise LibraryError("tags 必须是字符串数组")
    tags = [t.strip() for t in tags if t.strip()][:MAX_TAGS]

    rating = payload.get("rating")
    if rating in ("", None):
        rating = None
    elif isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
        raise LibraryError("rating 必须是 0–5 的整数")

    status = payload.get("status") or EMPTY_NOTE["status"]
    if status not in NOTE_STATUSES:
        raise LibraryError(f"未知状态: {status!r}")
    by = payload.get("by") or "user"
    if by not in NOTE_BY:
        raise LibraryError(f"未知来源: {by!r}")
    return {"note": note, "tags": tags, "rating": rating, "status": status, "by": by}


def get_note(item_id: str) -> dict:
    """读备注；没写过就返回空结构（界面不必判空）。"""
    item_id = _check_note_id(item_id)
    stored = _read_json(notes_dir() / f"{item_id}.json")
    return {"id": item_id, **EMPTY_NOTE, **{k: stored[k] for k in EMPTY_NOTE if k in stored},
            "updated": stored.get("updated", "")}


def save_note(item_id: str, payload: dict) -> dict:
    """写备注（原子替换；只动 notes/，不碰索引与源码）。"""
    item_id = _check_note_id(item_id)
    doc = {"id": item_id, **_clean_note(payload),
           "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    notes_dir().mkdir(parents=True, exist_ok=True)
    path = notes_dir() / f"{item_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return doc


def list_notes() -> dict:
    """有备注的策略 id 集合（左栏 💬 角标用，不读正文）。"""
    if not notes_dir().is_dir():
        return {"ids": [], "count": 0}
    ids = sorted(p.stem for p in notes_dir().glob("*.json") if NOTE_ID_RE.match(p.stem))
    return {"ids": ids, "count": len(ids)}


# ---------- AI 转写产物（scripts/pine_to_pyne.py 写的，页面只读） ----------

def transpile_dir() -> Path:
    return lib_dir().parent / "pine_transpile"


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def list_transpile() -> dict:
    """转写产物一览：{id: {title, problems, has_report, stats, job}}。

    转写在宿主上跑（要执行模型产出的代码，不进 API 进程），页面只读结果。
    """
    out: dict[str, dict] = {}
    if not transpile_dir().is_dir():
        return out
    for d in sorted(transpile_dir().iterdir()):
        if not d.is_dir() or not ID_RE.match(d.name):   # queue/ 之类的工作目录不算条目
            continue
        meta = _read_json(d / "meta.json")
        report = _read_json(d / "report.json")
        if not meta and not report:
            continue
        stats = report.get("stats") or {}
        job = _read_json(d / "job.json")
        out[d.name] = {
            "title": meta.get("title", ""),
            "model": meta.get("model", ""),
            "generated": meta.get("generated", ""),
            "problems": meta.get("problems") or [],
            "has_candidate": (d / "candidate.py").exists(),
            "has_report": bool(report),
            "symbol": report.get("symbol", ""),
            "trades": report.get("trades"),
            "net_pct": (stats.get("Net profit") or {}).get("pct"),
            "dd_pct": (stats.get("Max equity drawdown") or {}).get("pct"),
            "bh_pct": (stats.get("Buy & hold return") or {}).get("pct"),
            "job": {"status": job.get("status", ""), "stage": job.get("stage", ""),
                    "error": job.get("error", "")},
        }
    return out


# ---------- 转写 / 回测队列（宿主 worker 消费：scripts/pine_transpile_worker.py） ----------
# API 只写请求文件（容器里没有 bwrap，也不能执行模型产出的代码），
# worker 在宿主上转写 + 沙箱回测，进度写回 <id>/job.json。

def queue_dir() -> Path:
    return transpile_dir() / "queue"
ADJS = ("backward", "forward", "unadjusted")


def _check_symbol(symbol: str) -> str:
    from backend.services.agent_arena import market_lab as ml

    code = ml.normalize_code(symbol)
    if not code or code not in ml._names():
        raise LibraryError(f"未知标的: {symbol!r}")
    return code


def job_status(item_id: str) -> dict:
    """该策略当前任务状态。队列里有请求但还没开跑 → queued。"""
    item_id = _check_id(item_id)
    job = _read_json(transpile_dir() / item_id / "job.json")
    if not job and (queue_dir() / f"{item_id}.json").exists():
        return {"status": "queued", "stage": "queued", "error": ""}
    return job


def report_path(item_id: str) -> Path:
    """正式回测报告文件（对话 worker 靠它的 mtime 判断「改前」基线有多旧）。"""
    return transpile_dir() / _check_id(item_id) / "report.json"


def read_report(item_id: str) -> dict:
    """回测报告（stats + 逐笔 + 净值）。没跑过 → 空 dict。"""
    return _read_json(report_path(item_id))


def enqueue_backtest(item_id: str, symbol: str, adj: str = "backward",
                     model: str = "", force: bool = False) -> dict:
    """把「转写 + 回测」请求排进队列，由宿主 worker 消费。"""
    item_id = _check_id(item_id)
    get_source(item_id)                      # id 必须存在
    code = _check_symbol(symbol)
    if adj not in ADJS:
        raise LibraryError(f"未知复权口径: {adj}")
    qpath = queue_dir() / f"{item_id}.json"
    if qpath.exists():
        raise LibraryError("该策略已在队列里")
    if job_status(item_id).get("status") == "running":
        raise LibraryError("该策略正在跑，等它结束")

    req = {"id": item_id, "symbol": code, "adj": adj, "model": model,
           "force": bool(force),
           "requested": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    queue_dir().mkdir(parents=True, exist_ok=True)
    tmp = qpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(qpath)                        # 原子落地，worker 不会读到半截
    return {"queued": True, "request": req}


# ---------- 策略对话（队列 + 宿主 worker：scripts/pine_chat_worker.py） ----------
# 与转写队列同构，但**独立目录、独立锁**：问答/改策略不该被转写任务堵住，反之亦然。
# 模型调用与沙箱回测都只在宿主上发生，API 只落请求、读结果。

def chat_dir() -> Path:
    return lib_dir() / "chat"


def chat_queue() -> Path:
    return chat_dir() / "queue"


def chat_jobs() -> Path:
    return chat_dir() / "jobs"
CHAT_MODES = ("ask", "edit")
CHAT_MODEL = "deepseek-v4-flash"     # 与 Pine 转写同源：改策略产出的代码要被同一条链路读懂
MAX_MESSAGE_CHARS = 4000
JOB_ID_RE = re.compile(r"^[a-z0-9_]{1,32}-\d{8}T\d{6}-[0-9a-f]{6}$")


def _check_job_id(job_id: str) -> str:
    job_id = (job_id or "").strip()
    if not JOB_ID_RE.match(job_id):
        raise LibraryError(f"无效的任务 id: {job_id!r}")
    return job_id


def chat_thread(item_id: str, limit: int = 100) -> dict:
    """对话历史（最近的在后）。没聊过 → 空线程。"""
    item_id = _check_id(item_id)
    path = chat_dir() / item_id / "thread.jsonl"
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # worker 正在追加的半截行，跳过而不是炸掉整个线程
    return {"id": item_id, "count": len(rows), "messages": rows[-limit:]}


def chat_job(item_id: str, job_id: str) -> dict:
    """单个对话任务的状态与回复。"""
    item_id = _check_id(item_id)
    job_id = _check_job_id(job_id)
    job = _read_json(chat_jobs() / f"{job_id}.json")
    if not job:
        if _read_json(chat_queue() / f"{job_id}.json"):
            return {"job_id": job_id, "id": item_id, "status": "queued",
                    "stage": "queued", "reply": ""}
        raise LibraryError(f"任务不存在: {job_id}")
    if job.get("id") != item_id:   # 别让 A 策略的 job_id 读到 B 策略的结果
        raise LibraryError(f"任务不属于该策略: {job_id}")
    return job


def _pending_chat(item_id: str) -> bool:
    if not chat_queue().is_dir():
        return False
    return any(p.stem.startswith(f"{item_id}-") for p in chat_queue().glob("*.json"))


def enqueue_chat(item_id: str, message: str, mode: str = "ask") -> dict:
    """投递一条对话消息，由宿主 worker 消费。"""
    item_id = _check_id(item_id)
    get_source(item_id)                        # 对话是围绕源码的，策略必须存在
    message = (message or "").strip()
    if not message:
        raise LibraryError("消息不能为空")
    if len(message) > MAX_MESSAGE_CHARS:
        raise LibraryError(f"消息过长（>{MAX_MESSAGE_CHARS} 字）")
    if mode not in CHAT_MODES:
        raise LibraryError(f"未知模式: {mode!r}")
    if _pending_chat(item_id):
        raise LibraryError("上一条还在处理，等它回答完再发")

    now = datetime.now(timezone.utc)
    job_id = f"{item_id}-{now.strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"
    req = {"job_id": job_id, "id": item_id, "message": message, "mode": mode,
           "model": CHAT_MODEL, "requested": now.isoformat(timespec="seconds")}
    chat_queue().mkdir(parents=True, exist_ok=True)
    tmp = chat_queue() / f"{job_id}.json.tmp"
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(chat_queue() / f"{job_id}.json")
    return {"job_id": job_id, "queued": True, "request": req}


# ---------- 候选采用 / 版本回滚（P3） ----------
# agent 改出来的候选在用户点「采用」之前只活在 chat/<id>/<job_id>/ 里；采用时先把
# 当前生效源码快照到 versions/<id>/<ts>.pine 再写 edited/。回滚就是把这个快照写回去
# ——快照自己就是审计轨迹，不另建索引文件（索引会被 scripts/pine_library_index.py 重建）。

def versions_dir() -> Path:
    return lib_dir() / "versions"
CANDIDATE_PINE = "candidate.pine"
VERSION_RE = re.compile(r"^\d{8}T\d{12}$")


def _candidate_dir(item_id: str, job_id: str) -> Path:
    return chat_dir() / item_id / job_id


def candidate(item_id: str, job_id: str) -> dict:
    """读候选：Pine 源码 + 转写元信息 + 沙箱回测报告。"""
    item_id = _check_id(item_id)
    job_id = _check_job_id(job_id)
    pine = _candidate_dir(item_id, job_id) / CANDIDATE_PINE
    if not pine.exists():
        raise LibraryError(f"没有候选源码: {job_id}")
    d = pine.parent
    return {"id": item_id, "job_id": job_id,
            "pine": pine.read_text(encoding="utf-8", errors="replace"),
            "meta": _read_json(d / "meta.json"), "report": _read_json(d / "report.json")}


def list_versions(item_id: str) -> dict:
    """可回滚的版本快照（新的在后）。"""
    item_id = _check_id(item_id)
    d = versions_dir() / item_id
    rows = []
    if d.is_dir():
        rows = [{"version": p.stem, "bytes": p.stat().st_size}
                for p in sorted(d.glob("*.pine")) if VERSION_RE.match(p.stem)]
    return {"id": item_id, "count": len(rows), "versions": rows}


def _snapshot(item_id: str, text: str) -> str:
    """采用前先把「现在这版」存一份，返回快照名。"""
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    d = versions_dir() / item_id
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{name}.pine.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(d / f"{name}.pine")
    return name


def _same_as_corpus(item_id: str, text: str) -> bool:
    """与原始语料一字不差？（回滚到未编辑状态时用来决定删不删 edited/ 覆盖文件）"""
    try:
        return (source_dir() / f"{item_id}.pine").read_text(
            encoding="utf-8", errors="replace") == text
    except OSError:
        return False


def _write_edited(item_id: str, text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise LibraryError("源码不能为空")
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise LibraryError("源码过大（>512KB）")
    edited_dir().mkdir(parents=True, exist_ok=True)
    (edited_dir() / f"{item_id}.pine").write_text(text, encoding="utf-8")


def _requeue(item_id: str, symbol: str, adj: str) -> tuple[bool, str]:
    """采用/回滚后自动重跑官方产物。入不了队不算失败，但要如实回报原因。"""
    if not symbol:
        return False, "没有标的，未自动重跑"
    try:
        enqueue_backtest(item_id, symbol, adj=adj, force=True)
        return True, ""
    except LibraryError as exc:
        return False, str(exc)


def apply_candidate(item_id: str, job_id: str, symbol: str = "",
                    adj: str = "backward") -> dict:
    """采用候选：快照当前源码 → 写 edited/<id>.pine → 入队重跑转写+回测。"""
    item_id = _check_id(item_id)
    job_id = _check_job_id(job_id)
    job = chat_job(item_id, job_id)
    if job.get("status") != "done":
        raise LibraryError("候选还没跑完，不能采用")
    if job.get("problems"):
        raise LibraryError("候选没通过静态检查，不能采用")
    got = candidate(item_id, job_id)          # 候选文件必须真的在
    report = got.get("report") or {}
    symbol = symbol or report.get("symbol") or read_report(item_id).get("symbol") or ""
    adj = adj or report.get("adj") or "backward"

    snap = _snapshot(item_id, get_source(item_id)["source"])
    _write_edited(item_id, got["pine"])
    queued, queue_error = _requeue(item_id, symbol, adj)
    return {"id": item_id, "job_id": job_id, "applied": True, "snapshot": snap,
            "bytes": len(got["pine"].encode("utf-8")), "queued": queued,
            "queue_error": queue_error}


def revert_source(item_id: str, version: str = "") -> dict:
    """回滚到上一版（默认最新快照）。

    回滚前先把「当前这版」也存一份快照——否则回滚会永久丢掉刚采用的那版，
    再点一次就没法退回来。快照因此是一叠可来回翻的版本，不是一次性撤销。
    """
    item_id = _check_id(item_id)
    d = versions_dir() / item_id
    snaps = [p for p in sorted(d.glob("*.pine")) if VERSION_RE.match(p.stem)] if d.is_dir() else []
    if not snaps:
        raise LibraryError("没有可回滚的版本")
    if version:
        version = version.strip()
        if not VERSION_RE.match(version):
            raise LibraryError(f"无效的版本号: {version!r}")
        path = d / f"{version}.pine"
        if not path.exists():
            raise LibraryError(f"版本不存在: {version}")
    else:
        path, version = snaps[-1], snaps[-1].stem

    text = path.read_text(encoding="utf-8", errors="replace")
    _snapshot(item_id, get_source(item_id)["source"])   # 当前版也留一份，回滚可逆
    if _same_as_corpus(item_id, text):
        # 还原成与语料一字不差时，删掉覆盖文件——否则界面永远挂着「已编辑」，
        # 而内容其实等于原始语料。被删掉的那版仍在快照里，再回滚一次能切回来。
        (edited_dir() / f"{item_id}.pine").unlink(missing_ok=True)
    else:
        _write_edited(item_id, text)
    path.unlink(missing_ok=True)
    report = read_report(item_id)
    queued, queue_error = _requeue(item_id, report.get("symbol") or "",
                                   report.get("adj") or "backward")
    return {"id": item_id, "reverted": version, "bytes": len(text.encode("utf-8")),
            "remaining": list_versions(item_id)["count"],
            "queued": queued, "queue_error": queue_error}
