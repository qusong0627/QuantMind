"""因子值库目录（T-MV-06）——``config/factor_libraries.yaml`` 的唯一读取口。

职责边界（维护性原则 1「单源清单」）：

- **策展在 YAML**：名称 / 性质（kind）/ 说明 / 市场归属 / 治理标记
  （``excluded``——标签/泄露库显式收录但不开放为挖掘方向）。
- **事实在磁盘**：每库的实际列数与日期范围由 :func:`library_disk_facts`
  现场轻量读取（单分区 schema + 分区目录名），**绝不在 YAML 复制**——
  数字写死必漂移（``get_data_summary`` 的旧硬编码是实证）。
- **机器可发现性不在这里**：后台「刷新字段」走
  ``quantdb_factor_reader.discover()`` / ``FACTOR_SOURCE_DIRS``，那是另一张
  面；本目录是人工策展 + 治理面，两者互补不互抄。

schema 契约由金样钉住：``backend/tests/fixtures/factor_libraries_golden.json``
是 :func:`load_factor_libraries` 输出的逐位快照——改 YAML 必须同步金样
（``test_factor_libraries.py`` 红/绿）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: kind 词表（YAML 校验白名单；新增词表项必须先想清楚消费者语义）
KINDS: tuple[str, ...] = (
    "training_features",
    "microstructure",
    "alpha_zoo",
    "mined",
    "manifest",
    "auxiliary",
    "labels",
)

#: 市场代码白名单（与 quantdb_factor_reader.MARKET_FACTOR_SOURCES 同口径）
MARKETS: tuple[str, ...] = ("CN", "HK", "US", "CRYPTO", "FUTURES", "CUSTOM")

#: 目录文件相对仓库根的位置
CONFIG_REL = Path("config") / "factor_libraries.yaml"

SUPPORTED_VERSION = 1

#: 库 id 只允许小写下划线——它进磁盘路径拼接，必须拒绝路径穿越字符
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class FactorLibraryError(ValueError):
    """目录文件缺失 / YAML 损坏 / 违反 schema。调用方决定降级还是炸。"""


def _repo_root() -> Path:
    # backend/services/engine/mining_plugins/factor_libraries.py → parents[4] = 仓库根
    return Path(__file__).resolve().parents[4]


def load_factor_libraries(config_path: Path | None = None) -> dict:
    """读取并校验目录 YAML → 归一化结构。

    Returns:
        ``{"version": 1, "checked_at": str, "libraries": [entry, ...]}``，
        entry 键：``id/name/kind/description/excluded/markets``（顺序 = YAML
        书写顺序，即设置页展示顺序）。

    Raises:
        FactorLibraryError: 文件缺失、YAML 解析失败、或任何条目违反 schema
        （未知 kind / 重复 id / 空 markets / labels 未标 excluded / …）。
        校验是**响亮失败**：目录是展示面的上游，坏一半比全坏更危险。
    """
    path = config_path or (_repo_root() / CONFIG_REL)
    if not path.is_file():
        raise FactorLibraryError(f"factor libraries config not found: {path}")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 运行镜像必带 pyyaml
        raise FactorLibraryError(f"pyyaml unavailable: {exc}") from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FactorLibraryError(f"invalid yaml in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FactorLibraryError(f"{path}: top level must be a mapping")

    version = raw.get("version")
    if version != SUPPORTED_VERSION:
        raise FactorLibraryError(
            f"{path}: unsupported version {version!r}; expected {SUPPORTED_VERSION}"
        )
    raw_libraries = raw.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise FactorLibraryError(f"{path}: 'libraries' must be a non-empty list")

    libraries: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_libraries):
        libraries.append(_normalize_entry(path, index, item, seen_ids))
    return {
        "version": SUPPORTED_VERSION,
        "checked_at": str(raw.get("checked_at") or ""),
        "libraries": libraries,
    }


def _normalize_entry(path: Path, index: int, item: object, seen_ids: set[str]) -> dict:
    where = f"{path}: libraries[{index}]"
    if not isinstance(item, dict):
        raise FactorLibraryError(f"{where}: must be a mapping")

    lib_id = str(item.get("id") or "").strip()
    if not lib_id:
        raise FactorLibraryError(f"{where}: missing id")
    where = f"{where} (id={lib_id})"
    if not _ID_RE.fullmatch(lib_id):
        raise FactorLibraryError(
            f"{where}: id must match {_ID_RE.pattern} (it is joined into disk paths)"
        )
    if lib_id in seen_ids:
        raise FactorLibraryError(f"{where}: duplicate id")
    seen_ids.add(lib_id)

    name = str(item.get("name") or "").strip()
    if not name:
        raise FactorLibraryError(f"{where}: missing name")

    kind = str(item.get("kind") or "").strip()
    if kind not in KINDS:
        raise FactorLibraryError(
            f"{where}: unknown kind {kind!r}; expected one of {KINDS}"
        )

    description = str(item.get("description") or "").strip()
    if not description:
        raise FactorLibraryError(f"{where}: missing description")

    excluded_raw = item.get("excluded", False)
    if not isinstance(excluded_raw, bool):
        raise FactorLibraryError(f"{where}: 'excluded' must be a boolean")
    excluded = excluded_raw
    # 安全不变量：标签/泄露库绝不可开放为挖掘方向——漏标 excluded 直接炸，
    # 而不是让它以可选方向的身份溜进设置页。
    if kind == "labels" and not excluded:
        raise FactorLibraryError(
            f"{where}: kind='labels' must set excluded: true (leakage safety)"
        )

    raw_markets = item.get("markets")
    if not isinstance(raw_markets, list) or not raw_markets:
        raise FactorLibraryError(f"{where}: 'markets' must be a non-empty list")
    markets: list[str] = []
    for market in raw_markets:
        code = str(market or "").strip().upper()
        if code not in MARKETS:
            raise FactorLibraryError(
                f"{where}: unknown market {market!r}; expected one of {MARKETS}"
            )
        if code in markets:
            raise FactorLibraryError(f"{where}: duplicate market {code}")
        markets.append(code)

    return {
        "id": lib_id,
        "name": name,
        "kind": kind,
        "description": description,
        "excluded": excluded,
        "markets": markets,
    }


def _format_dt(value: str) -> str:
    """分区名 ``YYYYMMDD`` → ``YYYY-MM-DD``；非该形态原样返回（不猜）。"""
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value


def library_disk_facts(market: str, library_id: str) -> dict | None:
    """单库磁盘事实：``{"columns", "start", "end"}``；目录缺失 → ``None``。

    刻意不用 ``QuantDBFactorReader.describe()``——describe 是全量扫描（含
    min/max 数据值），设置页 GET 扛不动；这里只读一个 parquet 的 footer
    （schema）+ 分区目录名（日期范围），与数据本身无关。

    ``columns`` 读不出（pyarrow 缺失/文件损坏）时为 ``None`` 而不是丢整个
    事实——日期范围仍是真的；调用方按「无列数」展示。
    """
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir,
    )

    root = market_data_dir(market) / "6_ml_datasets" / library_id
    if not root.is_dir():
        return None

    dts = sorted(
        p.name[3:] for p in root.iterdir() if p.is_dir() and p.name.startswith("dt=")
    )
    probe: Path | None = None
    if dts:
        first = root / f"dt={dts[0]}"
        candidate = first / "data.parquet"
        if candidate.is_file():
            probe = candidate
        else:
            files = sorted(first.glob("*.parquet"))
            probe = files[0] if files else None
    else:
        files = sorted(root.rglob("*.parquet"))
        probe = files[0] if files else None
    if probe is None:
        return None

    columns: int | None = None
    try:
        import pyarrow.parquet as pq

        columns = len(pq.read_schema(probe).names)
    except Exception as exc:  # noqa: BLE001 - 事实层尽力而为，端点会兜底
        logger.warning(
            "factor library %s@%s schema unreadable (%s)", library_id, market, exc
        )

    return {
        "columns": columns,
        "start": _format_dt(dts[0]) if dts else None,
        "end": _format_dt(dts[-1]) if dts else None,
    }


def factor_libraries_payload(config_path: Path | None = None) -> dict:
    """端点就绪载荷：策展条目 + 逐市场磁盘事实。

    ``markets`` 从 YAML 的列表形态展开为 ``{市场: facts|None}``——声明了但
    本机没挂载/缺失的市场保留键、值为 ``None``（「声明存在、事实缺席」本身
    就是值得展示的运维信息，不静默抹掉）。

    单库事实读取失败只降级该库（``None``），不拖垮整份目录。
    """
    directory = load_factor_libraries(config_path)
    libraries: list[dict] = []
    for lib in directory["libraries"]:
        markets: dict[str, dict | None] = {}
        for market in lib["markets"]:
            try:
                markets[market] = library_disk_facts(market, lib["id"])
            except Exception as exc:  # noqa: BLE001 - 事实层失败不拦目录
                logger.warning(
                    "factor library facts failed for %s@%s: %s",
                    lib["id"],
                    market,
                    exc,
                )
                markets[market] = None
        libraries.append({**lib, "markets": markets})
    return {
        "version": directory["version"],
        "checked_at": directory["checked_at"],
        "libraries": libraries,
    }
