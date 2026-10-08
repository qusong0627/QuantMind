"""排除名单维护（个人中心「交易黑名单」表格）。

候选列表读的是**合并视图**（机器基线 + 用户层），这里维护的是**用户层**那一半：
手工加入（``block``）与例外放行（``allow``）。机器基线 ``data/exclusions/cn.json``
由导入器整份覆盖写，界面上**只读**——可以直接放行其中某一只，但不能删它，
因为下一次导入会把它带回来，而「删了又回来」比「根本删不掉」更让人困惑。

端点:

- ``GET  /api/v1/exclusion/entries``  合并视图分页（含来源、理由、到期、是否本人改动）
- ``POST /api/v1/exclusion/entries``  新增/改判一条手工条目
- ``DELETE /api/v1/exclusion/entries/{symbol}``  撤销一条本人改动
- ``GET  /api/v1/exclusion/meta``     名单基准日 / 陈旧度 / 两层条数
- ``POST /api/v1/exclusion/refresh``  重导机器基线（子进程跑导入器，含审计）

**删除的语义是「撤销本人改动」而不是「把这只票移出名单」**：删掉一条 ``allow``
会让那只票回到「按机器名单被排除」，删掉一条 ``block`` 会让它回到「不在名单里」。
接口文档与界面文案都必须说清这一点——用户按「删除=可以买了」理解会做出反向操作。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.exclusion_list import (
    DEFAULT_EXCLUSION_DIR,
    EXCLUSION_DIR_ENV,
    clear_cache,
    load_exclusion_list,
    source_label,
)
from backend.shared.exclusion_overlay import (
    ACTION_ALLOW,
    ACTION_BLOCK,
    ACTIONS,
    OverlayError,
    delete_entry,
    load_overlay,
    upsert_entry,
)
from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/exclusion", tags=["ExclusionAdmin"])

#: 导入脚本路径。重导**复用 CLI 子进程**而不是在请求里重写导入逻辑：名单值钱的一半
#: 在审计（逐源条数对账、后缀与交易所自洽），CLI 是它的唯一实现，绕开它写第二遍
#: 就等于把两条会漂移的实现放上生产。
_IMPORT_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "import_exclusion_list.py"
)

#: 容器内源目录：``/data`` 是挂载点；``configs/`` 不在 data 下，以只读方式另挂到
#: ``/app/configs``（compose 里的一条 volume）。env 覆盖点与 exclusion_list.py 同风格。
SRC_DATA_DIR_ENV = "QM_EXCLUSION_SRC_DATA_DIR"
SRC_CONFIGS_DIR_ENV = "QM_EXCLUSION_SRC_CONFIGS_DIR"
DEFAULT_SRC_DATA_DIR = "/data"
DEFAULT_SRC_CONFIGS_DIR = "/app/configs"

#: 导入是读四份 JSON + 写一份产物，正常秒级；跑满这个数说明源文件出了异常（如被写坏）
_REFRESH_TIMEOUT_S = 120

#: 双击防重：两个并发导入写同一个 ``.json.tmp``，后完成者的原子替换会踩空。
_refresh_lock = threading.Lock()

#: 本模块只服务 A 股（港股/美股各有自己的名单产物，尚未导入）
SUPPORTED_MARKETS = ("CN",)

#: 分页上限。名单是给人看的表，一页最多 200 行；全量 1800+ 条要靠检索缩。
MAX_PAGE_SIZE = 200


class EntryIn(BaseModel):
    """新增/改判载荷。``action`` 用字面量枚举——拼错时 FastAPI 直接 422，不落到业务层。"""

    symbol: str = Field(..., min_length=4, max_length=16, description="任意层口径代码")
    action: Literal["block", "allow"] = Field(
        ..., description="block=手工排除 / allow=例外放行"
    )
    reason: str = Field(
        "", max_length=200, description="为什么加（会显示在列表行与推送预检里）"
    )
    note: str = Field("", max_length=500, description="备注（可留空）")
    expire: str | None = Field(None, description="到期日 YYYY-MM-DD；留空=永久")
    market: str = Field("CN", description="市场（当前仅 CN）")


def _check_market(market: str) -> str:
    upper = str(market or "CN").upper()
    if upper not in SUPPORTED_MARKETS:
        raise HTTPException(status_code=400, detail=f"暂不支持的市场：{upper}")
    return upper


def _operator(current_user: dict) -> str:
    """操作者标识（落盘留痕用）。取不到就留空——**不编造**，宁可来源显示空白。"""
    for key in ("user_id", "username", "id"):
        value = current_user.get(key)
        if value:
            return str(value)
    return ""


@router.get("/entries")
async def list_entries(
    market: str = Query("CN"),
    q: str | None = Query(None, description="代码或名称模糊检索"),
    action: str | None = Query(
        None,
        description="筛选：manual(仅手工排除) / allow(仅放行) / machine(仅机器来源)",
    ),
    include_expired: bool = Query(True, description="是否包含已过期条目"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    current_user: dict = Depends(get_current_user),
):
    """合并视图（机器基线 + 本人改动），按代码升序分页。"""
    _ = current_user
    mk = _check_market(market)
    today = date.today().isoformat()

    lst = load_exclusion_list(mk)
    if lst is None:
        # 「名单没导入」与「名单是空的」必须分开说：前者是配置事故，得让用户去修
        return {
            "success": True,
            "data": {
                "imported": False,
                "reason": "名单文件未导入（data/exclusions/cn.json）",
                "total": 0,
                "page": page,
                "page_size": page_size,
                "items": [],
                "meta": None,
            },
        }

    overlay = load_overlay(mk)
    rows: list[dict[str, Any]] = []
    for symbol, hit in lst.items.items():
        if not include_expired and hit.expire and hit.expire < today:
            continue
        manual = overlay.entries.get(symbol)
        rows.append(_row(symbol, hit, manual))

    if action:
        rows = [r for r in rows if _matches(action, r)]
    if q:
        rows = [r for r in rows if _hit_query(r, q)]

    rows.sort(key=lambda r: r["symbol"])
    start = (page - 1) * page_size
    return {
        "success": True,
        "data": {
            "imported": True,
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "items": rows[start : start + page_size],
            "meta": lst.meta(today=today),
            "overlay": overlay.as_dict(),
        },
    }


def _matches(action: str, row: dict[str, Any]) -> bool:
    """``action`` 过滤。未知取值**不静默当全部**——返回空集并让前端看到 0 条，
    否则拼错一个词会得到「看起来正常但没过滤」的结果。"""
    key = str(action).strip().lower()
    manual = row.get("manual") or {}
    if key == "manual":
        return manual.get("action") == ACTION_BLOCK
    if key == "allow":
        return manual.get("action") == ACTION_ALLOW
    if key == "machine":
        return not manual
    return False


def _hit_query(row: dict[str, Any], q: str) -> bool:
    needle = str(q).strip().upper()
    if not needle:
        return True
    return (
        needle in str(row.get("symbol") or "").upper()
        or needle in str(row.get("name") or "").upper()
    )


def _row(symbol: str, hit: Any, manual: Any) -> dict[str, Any]:
    """单行载荷：机器命中 + 本人改动（``manual`` 为空即「没动过」）。"""
    from backend.shared.stock_name_mapper import resolve_name

    payload = hit.as_dict()
    return {
        "symbol": symbol,
        "name": resolve_name(symbol) or "",
        "sources": payload["sources"],
        "source_labels": payload["source_labels"],
        "flags": payload["flags"],
        "reason": payload["reason"],
        "expire": payload["expire"],
        "blocking": payload["blocking"],
        "expired": payload["expired"],
        "by_source": payload["by_source"],
        "manual": manual.as_dict() if manual is not None else None,
    }


@router.get("/meta")
async def exclusion_meta(
    market: str = Query("CN"),
    current_user: dict = Depends(get_current_user),
):
    """两层条数 + 基准日 + 陈旧度（个人中心表格顶部与候选页风险条共用）。"""
    _ = current_user
    mk = _check_market(market)
    lst = load_exclusion_list(mk)
    overlay = load_overlay(mk)
    data: dict[str, Any] = {
        "market": mk,
        "overlay": overlay.as_dict(),
        "sources": {
            k: {"label": source_label(k), **dict(v)}
            for k, v in (lst.sources if lst else {}).items()
        },
        "imported": lst is not None,
    }
    if lst is None:
        data["reason"] = "名单文件未导入（data/exclusions/cn.json）"
        data["meta"] = None
    else:
        data["meta"] = lst.meta()
    return {"success": True, "data": data}


@router.post("/entries")
async def create_or_update_entry(
    body: EntryIn,
    current_user: dict = Depends(get_current_user),
):
    """新增或改判一条手工条目（同一代码只保留一条，重复提交即改判）。"""
    mk = _check_market(body.market)
    try:
        entry = upsert_entry(
            body.symbol,
            action=body.action,
            reason=body.reason,
            note=body.note,
            expire=body.expire,
            operator=_operator(current_user),
            market=mk,
        )
    except OverlayError as exc:
        # OverlayError 全是输入问题（代码不认、日期不合法、动作非法、超上限），
        # 原样透给用户——吞成「保存失败」会让他反复重试同一个错误输入
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "[ExclusionAdmin] %s %s（%s）",
        entry.action,
        entry.symbol,
        entry.operator or "unknown",
    )
    return {"success": True, "data": {"entry": entry.as_dict()}}


@router.delete("/entries/{symbol}")
async def remove_entry(
    symbol: str,
    market: str = Query("CN"),
    current_user: dict = Depends(get_current_user),
):
    """撤销一条**本人**改动（不是把这只票移出名单，见模块头）。"""
    _ = current_user
    mk = _check_market(market)
    try:
        removed = delete_entry(symbol, market=mk)
    except OverlayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": {"symbol": symbol, "removed": removed}}


def _read_symbols(path: Path) -> set[str] | None:
    """盘上现产物的代码集合；文件缺失/损坏返回 ``None``。

    ``None`` 与空集是两回事：前者（没有可比基线）不该报「新增 N 只」那样的差异数，
    后者（空产物但可读）是合法状态，差异照常算。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return set(payload.get("items") or {})


def _tail_lines(text: str, limit: int = 8) -> str:
    """取最后若干非空行——导入器的逐条原因落在报告尾部（错误列表与拒绝行都在那里）。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "导入器退出码非零（无输出）"
    return "\n".join(lines[-limit:])


@router.post("/refresh")
def refresh_exclusion_list(current_user: dict = Depends(get_current_user)):
    """重导机器基线：跑 ``backend/scripts/import_exclusion_list.py``（同一份审计逻辑）。

    机器基线是「keeper 产物 → 导入器 → 查表产物」三步里的第三步：源每日刷新，但导入
    此前只有宿主手动一步（2026-10-08 基准日曾停在 09-18 二十天）。审计不过则**拒绝
    写盘**，逐条原因原样抬到响应里——吞成「刷新失败」会让用户无从下手。

    ``def``（非 ``async``）：子进程调用是阻塞的，交给 FastAPI 线程池，不占事件循环。
    """
    _ = current_user
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="上一次重导还在进行中，请稍候再试")

    try:
        data_dir = os.environ.get(SRC_DATA_DIR_ENV) or DEFAULT_SRC_DATA_DIR
        configs_dir = os.environ.get(SRC_CONFIGS_DIR_ENV) or DEFAULT_SRC_CONFIGS_DIR
        out_dir = os.environ.get(EXCLUSION_DIR_ENV) or DEFAULT_EXCLUSION_DIR
        out_path = Path(out_dir) / "cn.json"
        before = _read_symbols(out_path)

        cmd = [
            sys.executable,
            str(_IMPORT_SCRIPT),
            "--data-dir",
            data_dir,
            "--configs-dir",
            configs_dir,
            "--out",
            out_dir,
            "--market",
            "CN",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_REFRESH_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail=f"导入超时（>{_REFRESH_TIMEOUT_S}s）——检查源文件是否正被写坏",
            ) from None
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法启动导入脚本：{exc}") from exc

        if proc.returncode != 0:
            # stdout 尾是逐条审计原因、stderr 尾是拒绝行/缺源说明——两段都要（只有
            # stderr 的话，用户看到「确认无误可加 --force」却看不到到底哪条没过）
            detail = _tail_lines((proc.stdout or "") + "\n" + (proc.stderr or ""))
            logger.warning("[ExclusionAdmin] 重导被拒：%s", detail)
            raise HTTPException(status_code=409, detail=detail)

        # 产物的 mtime 变了缓存本会自动失效，这里显式清一次，让本次响应就带上新 meta
        clear_cache()
        lst = load_exclusion_list("CN")
        after = set(lst.items) if lst else set()
        added = sorted(after - before) if before is not None else []
        removed = sorted(before - after) if before is not None else []
        logger.info(
            "[ExclusionAdmin] 名单已重导 asof=%s total=%d（+%d/-%d）",
            lst.asof if lst else "?",
            len(after),
            len(added),
            len(removed),
        )
        return {
            "success": True,
            "data": {
                "first_import": before is None,
                "added": len(added),
                "removed": len(removed),
                "added_sample": added[:8],
                "removed_sample": removed[:8],
                "meta": lst.meta() if lst else None,
            },
        }
    finally:
        _refresh_lock.release()
