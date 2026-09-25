"""全局股票池 - 文件层（v2）。

职责：
1. **成员 TXT**（唯一事实源）：`<pool_dir>/<name>.txt`，前缀式一行一个
   （`SH600036`），支持 `#` 注释行；原子写（tmp + os.replace），
   人可用文本编辑器直接改，其他模块可直接读；
2. **Qlib instruments 物化**：回测引擎需要 `sh600036\\tSTART\\tEND` 格式时
   由 `materialize_snapshot` 生成，消费方共用，避免各自实现。

不引入 engine 依赖：本模块只做「符号列表 ↔ 文件」的纯函数式转换。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from collections.abc import Iterable, Sequence

from .constants import INSTRUMENT_FILE_PREFIX
from .normalize import (
    normalize_market,
    normalize_symbols,
    normalize_to_qlib,
    to_api_symbol,
)

logger = logging.getLogger(__name__)


def pool_dir() -> Path:
    return Path(os.getenv("QM_STOCK_POOL_TXT_DIR", "/data/stock_pool"))


def qlib_data_dir() -> Path:
    """Qlib 数据根目录（与 engine 读取端同源：统一走 qlib_paths 解析）。

    QLIB_PROVIDER_URI 显式覆盖优先；否则由 qlib_paths 按固定目录
    /data/qlib/cn_data 优先解析，避免股票池 instruments 落到旧 db/qlib_data
    造成「物化写 A、回测读 B」的分裂缓存。
    """
    env_val = os.getenv("QLIB_PROVIDER_URI", "").strip()
    if env_val:
        return Path(env_val)
    from backend.shared.qlib_paths import resolve_qlib_data_dir

    return Path(resolve_qlib_data_dir("CN"))


# ---------------------------------------------------------------------------
# 成员 TXT
# ---------------------------------------------------------------------------
def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(text))


def pool_subdir(
    scope: str, *, tenant_id: str | None = None, owner_user_id: str | None = None
) -> str:
    """用户 / 租户池的子目录名；global 返回空串（根目录扁平存放）。

    隔离靠目录：`/data/stock_pool/u00000001/<code>.txt`，
    同用户同名天然唯一，不同用户同名天然互不可见。
    """
    if scope == "user" and owner_user_id:
        return f"u{_safe_name(owner_user_id)}"
    if scope == "tenant" and tenant_id:
        return f"t{_safe_name(tenant_id)}"
    return ""


def _join_pool_dir(*parts: str) -> str:
    """拼接池目录路径，并钳制在 pool_dir 之内（防路径穿越）。"""
    base = pool_dir().resolve()
    target = base.joinpath(*(p for p in parts if p)).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"股票池路径越界: {target}")
    return str(target)


def pool_txt_name(
    scope: str,
    code: str,
    *,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """TXT 文件名（不含目录）。隔离由子目录承担，文件名只做安全清洗。"""
    return f"{_safe_name(code)}.txt"


def legacy_pool_txt_name(
    scope: str,
    code: str,
    *,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """旧版扁平文件名（仅读兼容）：根目录 `u<uid>_<code>.txt`。"""
    name = _safe_name(code)
    if scope == "user" and owner_user_id:
        return f"u{_safe_name(owner_user_id)}_{name}.txt"
    if scope == "tenant" and tenant_id:
        return f"t{_safe_name(tenant_id)}_{name}.txt"
    return f"{name}.txt"


def pool_txt_path(
    scope: str,
    code: str,
    *,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """新写入位置：global 根目录；user/tenant 进各自隔离子目录。"""
    return _join_pool_dir(
        pool_subdir(scope, tenant_id=tenant_id, owner_user_id=owner_user_id),
        pool_txt_name(scope, code, tenant_id=tenant_id, owner_user_id=owner_user_id),
    )


def legacy_pool_txt_path(
    scope: str,
    code: str,
    *,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """旧版扁平位置（仅读兼容，不再写入）。"""
    return _join_pool_dir(
        "",
        legacy_pool_txt_name(
            scope, code, tenant_id=tenant_id, owner_user_id=owner_user_id
        ),
    )


def resolve_pool_txt(
    scope: str,
    code: str,
    *,
    file_path: str | None = None,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """读侧路径解析（优先级从高到低）：
    DB file_path（存在）→ 新隔离子目录路径（存在）→ 旧扁平路径（存在）
    → 新隔离子目录路径（默认写入位）。
    旧路径命中时读兼容，下一次保存自动迁移到新位置。
    """
    if file_path and Path(file_path).exists():
        return str(file_path)
    new_path = pool_txt_path(
        scope, code, tenant_id=tenant_id, owner_user_id=owner_user_id
    )
    if Path(new_path).exists():
        return new_path
    legacy_path = legacy_pool_txt_path(
        scope, code, tenant_id=tenant_id, owner_user_id=owner_user_id
    )
    if legacy_path != new_path and Path(legacy_path).exists():
        logger.info("股票池命中旧扁平路径（读兼容，下次保存迁移）: %s", legacy_path)
        return legacy_path
    return file_path or new_path


def write_pool_txt(path: str | Path, api_symbols: Iterable[str], *, header: str = "") -> int:
    """把成员（前缀式）写成 TXT。原子替换。返回行数。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    lines: list[str] = []
    for sym in api_symbols or []:
        s = str(sym or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        lines.append(s)

    body = [f"# {header}" if header else "# stock pool members (prefix symbols, one per line)"]
    body.extend(lines)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("\n".join(body) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    logger.info("股票池 TXT 已写入 %s: %d symbols", target, len(lines))
    return len(lines)


def read_pool_txt(path: str | Path) -> list[str]:
    """读成员 TXT（返回前缀式列表）。文件不存在返回空列表（不抛）。"""
    fp = Path(path)
    if not fp.exists():
        return []
    out: list[str] = []
    try:
        text = fp.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        logger.warning("股票池 TXT 读取失败 %s: %s", fp, exc)
        return []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # 容错：带逗号的行（用户手改）取第一个单元格
        s = s.split(",")[0].split(";")[0].split("\t")[0].strip()
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Qlib instruments 物化（回测引擎消费格式）
# ---------------------------------------------------------------------------
def instrument_file(pool_code: str) -> Path:
    return qlib_data_dir() / "instruments" / f"{INSTRUMENT_FILE_PREFIX}{pool_code}.txt"


def write_instruments(
    pool_code: str,
    symbols: Sequence[str],
    market: str = "CN",
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> str | None:
    """写 Qlib instruments 文件，格式 `sh600036\\tSTART\\tEND`。

    symbols 为任意口径（前缀/后缀均可）；写文件时转 Qlib 小写前缀口径。
    """
    if not symbols:
        logger.warning("股票池 %s 成员为空，跳过 instruments 物化", pool_code)
        return None

    mk = normalize_market(market)
    start = str(start_date or "1990-01-01")
    end = str(end_date or "2100-01-01")

    target = instrument_file(pool_code)
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        qs = normalize_to_qlib(sym, mk)
        if not qs or qs in seen:
            continue
        seen.add(qs)
        lines.append(f"{qs}\t{start}\t{end}")

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "股票池 instruments 已物化 %s: %d symbols -> %s",
        pool_code,
        len(lines),
        target,
    )
    return str(target)


def read_instruments(pool_code: str) -> list[str]:
    """读回 instruments 文件（返回 Qlib 口径符号）。"""
    fp = instrument_file(pool_code)
    if not fp.exists():
        return []
    return read_instruments_file(fp)


def read_instruments_file(path: str | Path) -> list[str]:
    """Qlib instruments 文件兼容解析（消费侧统一入口）。

    标准格式为 `sym\\tSTART\\tEND`（qlib 的 D.instruments 只认市场短名，
    不认任意文件路径；且整行不能直接当代码用）。只取首列，跳过 `#`
    注释与空行；成员 TXT（前缀式一行一个）同样兼容。
    """
    fp = Path(path)
    if not fp.exists():
        return []
    out: list[str] = []
    for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        first = s.split("\t")[0].split(",")[0].strip()
        if first:
            out.append(first)
    return out


def materialize_snapshot(
    snapshot,
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> str | None:
    """把 `PoolSnapshot` 物化成 Qlib instruments 文件，返回绝对路径。

    - `unfiltered`（对应旧 `universe='all'`）→ 返回 None，表示**不过滤**；
    - 空池 → 同样返回 None（**调用方须自行决定是否报错**，不要静默当成不过滤）；
    - 正常池 → 写 `instruments/pool_<code>.txt` 并返回路径。

    回测 / 训练 / 推理等消费方共用，避免各自实现物化逻辑。
    """
    if snapshot is None or snapshot.unfiltered or snapshot.is_empty:
        return None
    safe_code = _safe_name(getattr(snapshot, "code", None) or snapshot.pool_id or "pool")
    return write_instruments(
        safe_code,
        list(snapshot.api_symbols or snapshot.symbols),
        getattr(snapshot, "market", "CN"),
        start_date=start_date,
        end_date=end_date,
    )


def normalize_member_input(raw: Iterable[str], market: str = "CN") -> list[str]:
    """任意来源的成员输入 → 后缀式去重列表（保序）。"""
    return normalize_symbols(list(raw or []), market)


def to_api_members(storage_symbols: Sequence[str], market: str) -> list[str]:
    """后缀式 → 前缀式（写 TXT 用）。"""
    return [to_api_symbol(s, market) for s in storage_symbols]
