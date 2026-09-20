"""评估长序列侧车（``data/eval_series/<object_type>/<object_id>.json``，设计 §1.6）。

**为什么单独落盘**：模型的逐日 IC、因子的逐日 IC / 分位线是上千点的长序列。
把它们放进 ``eval_scores.dimensions`` 会让 ``GET /api/v1/eval/scores`` 变重——
评估中心每次刷新都要拖着几十条序列走。列表只留标量 detail，长序列按需取
（``GET /api/v1/eval/series``），选中对象时才读这一个文件。

**两条纪律**：

1. **读不到要说清是哪一种读不到**（``missing`` / ``corrupt`` / ``version_mismatch``
   / ``unsafe_object_id``）。返回空序列是最坏的选择——前端会把它画成一条平线，
   等于伪造证据；
2. **object_id 进路径段**，直接来自 URL：``..``/``/`` 一律拒（路径穿越），
   并且 ``object_type`` 只认六类白名单；含冒号的账户 id（``10000001:CN``）经
   ``series_filename`` 编码成单段文件名，读写走同一映射。

写侧原子（tmp + replace）：进程被杀时留下的半截 JSON 不许被读成有效侧车。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SERIES_VERSION = 1
SERIES_DIR_ENV = "QM_EVAL_SERIES_DIR"
# 容器内挂载点（./data:/data，与 l05_store 同约定）
DEFAULT_SERIES_DIR = "/data/eval_series"

# 仓库根（backend/shared/eval_series.py → 仓库根；便携包 $ROOT、Docker /app）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# object_type 进路径段：只认与 eval_scores 写入侧一致的六类
_ALLOWED_TYPES = frozenset(
    {"factor", "model", "strategy", "account", "daily_selection", "strategy_health"}
)
# 原样形态：模型带下划线、因子带点号、日期带横线、账户为数字。首字符不许是点
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# 编码形态（quote 之后）：只多出 `%`，且首字符不许是点（隐藏文件）
_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9%][A-Za-z0-9._%~-]{0,255}$")
# 原样形态的上限；编码形态会变长，所以在编码前先卡原长
_MAX_ID_LEN = 128


class UnsafeObjectId(ValueError):
    """object_type / object_id 不安全（路径穿越或不认识的类型）。"""


def safe_object_id(object_id: Any) -> bool:
    """object_id 是否可**原样**作为文件名（写侧用，不抛异常）。

    注意这比「可落盘」更严：账户 id ``10000001:CN`` 含冒号，原样形态为假、
    但经 :func:`series_filename` 编码后可以落盘。判「能不能读」请用
    :func:`is_series_id`。
    """
    return bool(_ID_PATTERN.match(str(object_id or "")))


def series_filename(object_id: Any) -> str:
    """object_id → 落盘文件名（读写两侧唯一映射）。

    真实 id 里有 Windows 文件名非法字符：账户卡是 ``用户:市场``（``10000001:CN``），
    策略卡的 object_id 在 CLI 直跑时甚至可能是结果文件路径。所以这里两段式：

    - **原样形态**（``_ID_PATTERN``）→ 原样返回，既有因子/模型侧车文件名不变；
    - 其余 → 百分号编码（``:`` → ``%3A``、``/`` → ``%2F``），编码结果必然是
      单个路径段；仍须落在 ``_SLUG_PATTERN`` 里，否则拒绝。

    两段不会撞名：原样形态永不含 ``%``（含 ``%`` 的输入直接拒），而编码结果
    只要发生编码就必然含 ``%``。

    三类输入**直接拒、不编码**：路径分隔符（``/`` ``\\``——含分隔符的不是 id 是路径）、
    ``%``（已编码形态再编码一次会把穿越尝试洗成无辜文件名）、首字符为点（隐藏文件）。
    """
    raw = str(object_id or "")
    if _ID_PATTERN.match(raw):
        return raw
    if (
        not raw.strip()
        or "%" in raw
        or "/" in raw
        or "\\" in raw
        or len(raw) > _MAX_ID_LEN
    ):
        raise UnsafeObjectId(f"object_id 含非法字符（禁止路径穿越）: {object_id!r}")
    slug = quote(raw, safe="")
    if not _SLUG_PATTERN.match(slug):
        raise UnsafeObjectId(f"object_id 编码后仍不安全: {object_id!r}")
    return slug


def is_series_id(object_id: Any) -> bool:
    """object_id 能否落盘/读取（非抛错闸门，给路由用）。"""
    try:
        series_filename(object_id)
    except UnsafeObjectId:
        return False
    return True


def _check_type(object_type: Any) -> str:
    ot = str(object_type or "").strip()
    if ot not in _ALLOWED_TYPES:
        raise UnsafeObjectId(f"object_type 非法（只认六类评分卡）: {object_type!r}")
    return ot


def resolve_series_dir(root: Path | str | None = None) -> Path:
    """侧车根目录：显式 ``root`` > 环境变量 > 容器挂载点 > 仓库 ``data/``。"""
    if root is not None:
        return Path(root)
    env_val = os.getenv(SERIES_DIR_ENV, "").strip()
    if env_val:
        return Path(env_val)
    mounted = Path(DEFAULT_SERIES_DIR)
    if mounted.parent.is_dir():
        return mounted
    return _PROJECT_ROOT / "data" / "eval_series"


def series_path(
    object_type: str, object_id: str, *, root: Path | str | None = None
) -> Path:
    """侧车文件路径。不检查存在性，由调用方决定如何降级。

    非法 ``object_type`` / ``object_id`` 抛 :class:`UnsafeObjectId`——写侧应当
    在拿到 URL 参数时就撞上这堵墙，而不是把文件写到目录外面。
    """
    ot = _check_type(object_type)
    return resolve_series_dir(root) / ot / f"{series_filename(object_id)}.json"


def save_series(
    object_type: str,
    object_id: str,
    payload: dict[str, Any],
    *,
    root: Path | str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """原子写侧车，返回 ``{path, bytes, written, note}``。

    写失败**不抛给评分主流程**（序列是附加证据，不应让整张卡失败），但必须
    把失败原因带回去让调用方写进 note——静默丢失序列等于前端永远缺图。
    """
    target = series_path(object_type, object_id, root=root)
    body = {
        "version": SERIES_VERSION,
        "object_type": str(object_type),
        "object_id": str(object_id),
        # 3.10 兼容：容器内是 py3.10，没有 datetime.UTC（3.11 才加）
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        **(payload or {}),
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        logger.warning("评估序列侧车写入失败 %s: %s", target, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "path": str(target),
            "bytes": 0,
            "written": False,
            "note": f"序列侧车写入失败：{type(exc).__name__}: {exc}",
        }
    return {"path": str(target), "bytes": len(text), "written": True, "note": None}


def load_series(
    object_type: str, object_id: str, *, root: Path | str | None = None
) -> dict[str, Any]:
    """读侧车，永远返回信封（不抛异常、不静默给空序列）。

    ``{"available": bool, "reason": str | None, "note": str, "data": dict | None,
    "generated_at": str | None, "version": int | None, "path": str}``
    """
    try:
        target = series_path(object_type, object_id, root=root)
    except UnsafeObjectId as exc:
        return {
            "available": False,
            "reason": "unsafe_object_id",
            "note": f"非法对象标识，拒绝读取：{exc}",
            "data": None,
            "generated_at": None,
            "version": None,
            "path": "",
        }

    envelope: dict[str, Any] = {
        "available": False,
        "reason": None,
        "note": "",
        "data": None,
        "generated_at": None,
        "version": None,
        "path": str(target),
    }
    if not target.is_file():
        envelope["reason"] = "missing"
        envelope["note"] = (
            f"序列侧车不存在（{target}）——该对象尚未产出长序列，不是「算出来是空的」"
        )
        return envelope
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        envelope["reason"] = "corrupt"
        envelope["note"] = f"序列侧车不可读：{type(exc).__name__}: {exc}"
        return envelope
    if not isinstance(raw, dict):
        envelope["reason"] = "corrupt"
        envelope["note"] = (
            f"序列侧车结构非法（顶层应为对象，实际 {type(raw).__name__}）"
        )
        return envelope
    if int(raw.get("version") or 0) != SERIES_VERSION:
        envelope["reason"] = "version_mismatch"
        envelope["note"] = (
            f"序列侧车版本 {raw.get('version')} ≠ 当前口径 {SERIES_VERSION}"
            "——旧口径字段含义可能已变，拒绝下发（重跑评分卡即可重建）"
        )
        return envelope

    envelope.update(
        {
            "available": True,
            "note": "",
            "data": raw,
            "generated_at": raw.get("generated_at"),
            "version": int(raw["version"]),
        }
    )
    return envelope
