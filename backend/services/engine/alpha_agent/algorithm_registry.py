"""挖掘算法注册表（移植 P2）。

算法 = 一种产出因子的方式：``rdagent``（LLM 多轮演化）或 ``statistical``
（经典量价候选 → 截面 IC/RankIC/ICIR 检验 → |RankIC| Top-K 入库）。本模块是
算法清单的**唯一装载点**与提交路径的**唯一闸门**：

- manifest 落在 ``algorithms/*.yaml``（随代码进 git 的资产；``key`` 必须等于
  文件名——名实一致，禁止 manifest 自封身份）；
- ``list_algorithms`` 供目录接口：单个坏 manifest 如实报 ``valid=False`` +
  原因，不拖垮列表、也不静默消失；
- ``resolve_algorithm`` 是 evolve / mining/batch 的共用校验口：未知 / 停用 /
  市场不支持 / 频率不支持 / 不消费数据集选择的算法在派发前显式报错（路由转
  400）——绝不"按默认算法跑出一份不是用户要的东西"。

``runner`` 为仓根相对路径且装载时校验文件存在：声明了却跑不起来的算法不该
出现在可选目录里。能力面（``markets`` / ``freqs`` / ``uses_datasets``）是
闸门的唯一依据，不许在路由里散写算法名 if。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_FREQS: tuple[str, ...] = ("day",)


def normalize_freq(value: str | None) -> str:
    """Normalize the only frequency currently implemented by QuantMind mining."""
    freq = str(value or "").strip().lower() or "day"
    if freq not in SUPPORTED_FREQS:
        raise ValueError(f"不支持的挖掘频率 {value!r}（可选：day）")
    return freq


logger = logging.getLogger(__name__)

#: 内建 manifest 目录（随代码进 git；内建优先唯一来源，无用户目录——P2 口径）
ALGORITHMS_DIR = Path(__file__).resolve().parent / "algorithms"

#: 仓根：runner 存在性校验的基准（容器内 = /app，与子进程落点一致）
REPO_ROOT = Path(__file__).resolve().parents[4]

#: 未指定算法时的默认值 = 迁移前唯一路径（老前端零改动、行为逐位一致）
DEFAULT_ALGORITHM_KEY = "rdagent"

#: key 安全字符集：纯小写（大小写混用会造出 Statistical/statistical 两个身份）。
#: 装载前必须过此闸再拼任何路径（防路径穿越，同 recipe_registry 纪律）。
_SAFE_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

#: 进度契约取值（见 MiningAlgorithm.progress_kind）：
#: workdir = RD-Agent 工作目录扫描（迁移前唯一形状，缺省值）；
#: jsonl = runner 追加写 $LOG_DIR/progress.jsonl。
PROGRESS_KIND_WORKDIR = "workdir"
PROGRESS_KIND_JSONL = "jsonl"
PROGRESS_KINDS: tuple[str, ...] = (PROGRESS_KIND_WORKDIR, PROGRESS_KIND_JSONL)


class AlgorithmError(ValueError):
    """manifest 非法 / 请求的算法组合不可派发。路由捕获后转 400。"""


@dataclass(frozen=True)
class Stage:
    """算法的流程阶段（UI 进度展示用）：``key`` 稳定、``label`` 中文展示。"""

    key: str
    label: str


@dataclass(frozen=True)
class MiningAlgorithm:
    """一个已装载并经校验的挖掘算法（冻结资产，KISS）。

    ``uses_llm`` / ``requires_gpu`` 是**运行时能力面**（默认 True，与迁移前
    唯一路径一致）：前者决定是否走 LLM 相关链路（提示词池注入、LLM env、
    ``--loop-n``/``--direction`` 参数），后者决定是否跑 GPU 硬件锁——统计派
    纯 CPU，占用的是 CPU 不是显卡，拿 GPU 锁拦它是错的。launcher 按这两个
    字段分支，不在代码里散写算法名 if。

    ``progress_kind`` 是 runner 的**进度契约**（显式声明，不靠嗅探文件猜）：
    ``workdir`` = RD-Agent 工作目录扫描（Loop_* 目录 + 阶段 pkl，迁移前唯一
    形状）；``jsonl`` = runner 往 ``$LOG_DIR/progress.jsonl`` 追加事件行
    （阶段 key 与本 manifest ``stages`` 对应）。
    """

    key: str
    name: str
    version: str
    desc: str
    vendor: str
    enabled: bool
    tags: tuple[str, ...]
    markets: tuple[str, ...]
    freqs: tuple[str, ...]
    uses_datasets: bool
    uses_llm: bool
    requires_gpu: bool
    progress_kind: str
    stages: tuple[Stage, ...]
    runner: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AlgorithmError(message)


def _parse_str_list(
    algorithm_key: str, field: str, raw: Any, *, required: bool
) -> tuple[str, ...]:
    """字符串列表字段：元素必须是非空 str、去重保序。"""
    if raw is None:
        _require(not required, f"算法 {algorithm_key}: {field} 必填")
        return ()
    _require(
        isinstance(raw, list) and (bool(raw) or not required),
        f"算法 {algorithm_key}: {field} 必须是非空列表",
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        _require(
            isinstance(item, str) and bool(item.strip()),
            f"算法 {algorithm_key}: {field} 含非法项 {item!r}（须为非空字符串）",
        )
        value = item.strip()
        _require(value not in seen, f"算法 {algorithm_key}: {field} 重复项 {value}")
        out.append(value)
        seen.add(value)
    return tuple(out)


def _parse_stages(algorithm_key: str, raw: Any) -> tuple[Stage, ...]:
    _require(
        isinstance(raw, list) and bool(raw),
        f"算法 {algorithm_key}: stages 必须是非空列表",
    )
    stages: list[Stage] = []
    seen: set[str] = set()
    for item in raw:
        _require(isinstance(item, dict), f"算法 {algorithm_key}: stages 项必须是对象")
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        _require(
            bool(_SAFE_KEY_RE.fullmatch(key)),
            f"算法 {algorithm_key}: 阶段 key 非法 {item.get('key')!r}",
        )
        _require(key not in seen, f"算法 {algorithm_key}: 阶段 key 重复 {key}")
        _require(bool(label), f"算法 {algorithm_key}: 阶段 {key} 缺 label")
        stages.append(Stage(key=key, label=label))
        seen.add(key)
    return tuple(stages)


def _parse_runner(algorithm_key: str, raw: Any, repo_root: Path) -> str:
    runner = str(raw or "").strip()
    _require(bool(runner), f"算法 {algorithm_key}: runner 必填")
    _require(
        not Path(runner).is_absolute(),
        f"算法 {algorithm_key}: runner 必须是仓根相对路径",
    )
    _require(
        ".." not in Path(runner).parts,
        f"算法 {algorithm_key}: runner 不得含 ..（{runner}）",
    )
    _require(
        (repo_root / runner).is_file(),
        f"算法 {algorithm_key}: runner 不存在 {runner}（基准 {repo_root}）",
    )
    return runner


def validate_manifest(
    data: Any, *, expected_key: str | None = None, repo_root: Path | None = None
) -> MiningAlgorithm:
    """结构校验 + 规范化（测试可直接喂 dict）。"""
    _require(isinstance(data, dict), "算法 manifest 必须是 YAML 对象")
    key = str(data.get("key") or "").strip()
    _require(
        bool(_SAFE_KEY_RE.fullmatch(key)),
        f"算法 manifest key 非法（仅小写字母/数字/._-，≤64）: {data.get('key')!r}",
    )
    if expected_key is not None:
        _require(
            key == expected_key, f"算法文件名与 key 不一致: {expected_key} != {key}"
        )

    name = str(data.get("name") or "").strip()
    _require(bool(name), f"算法 {key}: name 必填")
    version = str(data.get("version") or "").strip()
    _require(bool(version), f"算法 {key}: version 必填（任务行溯源的版本锚）")
    desc = str(data.get("desc") or "").strip()
    _require(bool(desc), f"算法 {key}: desc 必填")

    enabled = data.get("enabled", True)
    _require(isinstance(enabled, bool), f"算法 {key}: enabled 必须是布尔")
    uses_datasets = data.get("uses_datasets", True)
    _require(isinstance(uses_datasets, bool), f"算法 {key}: uses_datasets 必须是布尔")
    # 运行时能力面：缺省 True = 迁移前唯一路径（RD-Agent 演化）的行为
    uses_llm = data.get("uses_llm", True)
    _require(isinstance(uses_llm, bool), f"算法 {key}: uses_llm 必须是布尔")
    requires_gpu = data.get("requires_gpu", True)
    _require(isinstance(requires_gpu, bool), f"算法 {key}: requires_gpu 必须是布尔")
    # 进度契约：缺省 workdir = 迁移前唯一形状（RD-Agent 工作目录扫描）
    progress_kind = str(data.get("progress_kind") or PROGRESS_KIND_WORKDIR).strip()
    _require(
        progress_kind in PROGRESS_KINDS,
        f"算法 {key}: progress_kind {progress_kind!r} 不在 {PROGRESS_KINDS}",
    )

    markets = _parse_str_list(key, "markets", data.get("markets"), required=True)
    freqs = _parse_str_list(key, "freqs", data.get("freqs"), required=True)
    for freq in freqs:
        _require(
            freq in SUPPORTED_FREQS,
            f"算法 {key}: freq {freq!r} 不在支持集 {SUPPORTED_FREQS}（mining_config 单一出处）",
        )

    return MiningAlgorithm(
        key=key,
        name=name,
        version=version,
        desc=desc,
        vendor=str(data.get("vendor") or "").strip(),
        enabled=enabled,
        tags=_parse_str_list(key, "tags", data.get("tags"), required=False),
        markets=markets,
        freqs=freqs,
        uses_datasets=uses_datasets,
        uses_llm=uses_llm,
        requires_gpu=requires_gpu,
        progress_kind=progress_kind,
        stages=_parse_stages(key, data.get("stages")),
        runner=_parse_runner(key, data.get("runner"), repo_root or REPO_ROOT),
    )


def _load_file(path: Path, *, repo_root: Path | None) -> MiningAlgorithm:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError, yaml.YAMLError) as exc:
        # YAMLError 不 ⊂ ValueError；RecursionError = 深层嵌套 YAML。坏文件必须
        # 收敛成 AlgorithmError——list_algorithms 依赖「单个坏文件不拖垮列表」。
        raise AlgorithmError(f"算法 {path.stem}: 读取/解析失败: {exc}") from exc
    return validate_manifest(data, expected_key=path.stem, repo_root=repo_root)


def _registry_dir(registry_dir: Path | None) -> Path:
    return ALGORITHMS_DIR if registry_dir is None else Path(registry_dir)


def load_algorithm(
    algorithm_key: str,
    *,
    registry_dir: Path | None = None,
    repo_root: Path | None = None,
) -> MiningAlgorithm:
    """按 key 装载算法（key 即文件名；非法/不存在都报「不存在」不细说）。"""
    key = str(algorithm_key or "").strip().lower()
    _require(bool(_SAFE_KEY_RE.fullmatch(key)), f"挖掘算法不存在: {algorithm_key!r}")
    directory = _registry_dir(registry_dir)
    path = directory / f"{key}.yaml"
    if not path.is_file():
        available = (
            sorted(p.stem for p in directory.glob("*.yaml"))
            if directory.is_dir()
            else []
        )
        hint = "、".join(available) if available else "（注册表目录为空）"
        raise AlgorithmError(f"挖掘算法不存在: {key}（可选: {hint}）")
    return _load_file(path, repo_root=repo_root)


def normalize_algorithm(value: str | None) -> str:
    """算法 key 归一：空 = 默认（迁移前唯一路径）；非法字符在此就拒。"""
    key = str(value or "").strip().lower()
    if not key:
        return DEFAULT_ALGORITHM_KEY
    _require(bool(_SAFE_KEY_RE.fullmatch(key)), f"挖掘算法 key 非法: {value!r}")
    return key


def algorithm_summary(algo: MiningAlgorithm) -> dict[str, Any]:
    """目录接口 / 任务行快照共用的展示形状。"""
    return {
        "key": algo.key,
        "name": algo.name,
        "version": algo.version,
        "desc": algo.desc,
        "vendor": algo.vendor,
        "enabled": algo.enabled,
        "tags": list(algo.tags),
        "markets": list(algo.markets),
        "freqs": list(algo.freqs),
        "uses_datasets": algo.uses_datasets,
        "uses_llm": algo.uses_llm,
        "requires_gpu": algo.requires_gpu,
        "progress_kind": algo.progress_kind,
        "stages": [{"key": s.key, "label": s.label} for s in algo.stages],
        "runner": algo.runner,
    }


def list_algorithms(
    *, registry_dir: Path | None = None, repo_root: Path | None = None
) -> list[dict[str, Any]]:
    """全部 manifest 摘要（按 key 排序，确定性顺序）；坏文件如实报告不拖垮列表。"""
    directory = _registry_dir(registry_dir)
    if not directory.is_dir():
        # 目录缺失是打包事故（manifest 随代码进 git）——如实记录，别假装没有算法
        logger.error("[algorithm-registry] manifest 目录不存在: %s", directory)
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            algo = _load_file(path, repo_root=repo_root)
        except AlgorithmError as exc:
            logger.warning("[algorithm-registry] manifest 不可用: %s", exc)
            out.append({"key": path.stem, "valid": False, "error": str(exc)})
            continue
        out.append({**algorithm_summary(algo), "valid": True})
    return out


def resolve_algorithm(
    algorithm_key: str | None,
    *,
    market: str | None,
    freq: str | None,
    datasets: list[str] | None = None,
    registry_dir: Path | None = None,
    repo_root: Path | None = None,
) -> MiningAlgorithm:
    """提交路径的唯一算法闸门（evolve / mining/batch / 目录接口共用）。

    校验顺序固定（先来先报，测试钉住）：存在 → 启用 → 市场 → 频率 → 数据集。

    ``datasets`` 语义同 P1：None/空 = 未做选择（全选，不算缩窄）；非空 = 用户
    显式缩窄——对 ``uses_datasets: false`` 的算法（统计派不喂 LLM）显式拒绝，
    而不是收下选择再沉默丢弃。
    """
    algo = load_algorithm(
        normalize_algorithm(algorithm_key),
        registry_dir=registry_dir,
        repo_root=repo_root,
    )
    if not algo.enabled:
        raise AlgorithmError(f"挖掘算法已停用: {algo.key}（{algo.name}）")

    market_value = str(market or "").strip().lower()
    if market_value and market_value not in algo.markets:
        raise AlgorithmError(
            f"挖掘算法 {algo.key} 不支持市场 {market}（支持: {'、'.join(algo.markets)}）"
        )

    freq_value = normalize_freq(freq)
    if freq_value not in algo.freqs:
        raise AlgorithmError(
            f"挖掘算法 {algo.key} 不支持频率 {freq_value}（支持: {'、'.join(algo.freqs)}）"
        )

    picked = [str(d).strip() for d in (datasets or []) if str(d or "").strip()]
    if picked and not algo.uses_datasets:
        raise AlgorithmError(
            f"挖掘算法 {algo.key} 不消费数据集选择（收到 {len(picked)} 项）；"
            "请清除数据集选择或改用其他算法"
        )
    return algo
