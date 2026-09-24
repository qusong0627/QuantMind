"""决策名册（P2.9）的**配置面**：读现状 → 校验 → 落盘 → 回读复验 → 清理残留。

名册的语义（谁跑哪些模型、身份怎么算、错误怎么报）在
:mod:`backend.shared.decision_llm_client` 里，本模块**不复制任何一条**：校验走
``resolve_roster`` 本身，而不是另写一份「我以为的规则」——两套规则必然漂移，而漂移的
校验会放进一份跑不起来的名册。

**为什么这个端点必须挂在 trade 服务**
--------------------------------------
``resolve_roster`` 读的是 ``os.environ``（不是 ``get_secret``），而 ``set_secret``
写文件的同时也写**本进程**的 ``os.environ``。所以：

* 由 **trade 进程自己**写 → 下一轮 tick 调 ``default_roster()`` 时就读到新名册，
  **不用重启**（决策轮每 ``POLL_S`` 醒一次，名册是每轮取一次）；
* 由 api 进程写 → trade 进程的 ``os.environ`` 不会变，得等它重启时才从
  ``runtime.env`` 读回来 ⇒「点了保存、界面显示已生效、实际还在跑旧的」。

这正是本模块的写入口只能有一个的原因。名册**不碰** :data:`ENV_FLAG`
（``QM_DECISION_ROUND_ENABLED``）：那个开关 worker 只在**启动时**判一次
（``decision_round_runner`` 一旦判否就直接 return），写它而不重启是**撒谎**，
写了再重启又是「静默开始跑真钱」——故只读展示，不提供写。

密钥纪律
--------
名册串会被打进日志、状态键、``docker inspect`` 与工单，**key 不能出现在里面**：
UI 填的 key 写进 ``runtime.env`` 的一个生成变量名（:func:`key_var_name`），名册里
只引用变量名（``api_key_env``）；读接口只报「配没配」（``api_key_set``）与变量名，
**永不回传值**（同 ``runtime_secrets.mask_secret`` 的取向：不展示胜过展示脱敏）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.shared.decision_llm_client import (
    AGENT_LIMIT,
    ENV_ROSTER,
    LLMNotConfigured,
    looks_like_placeholder,
    resolve_roster,
)
from backend.shared.order_contract import normalize_agent
from backend.shared.runtime_secrets import (
    delete_secret,
    read_runtime_env,
    runtime_env_path,
    set_secret,
)

logger = logging.getLogger(__name__)

#: 决策轮开关（只读展示；写它需要重启，见模块 docstring）。
ENV_FLAG = "QM_DECISION_ROUND_ENABLED"

#: 生成的 API key 变量名前缀。带这个前缀、又不在生效名册里的变量，视为历史残留并在
#: 保存时清掉（否则删掉一家模型后，它的 key 会永远留在 runtime.env 里没人知道）。
KEY_VAR_PREFIX = "QM_DECISION_LLM_KEY_"

#: 变量名里 agent 段的合法字符（``runtime_secrets._KEY_PATTERN`` 只认大写/数字/下划线）。
_VAR_UNSAFE = re.compile(r"[^A-Z0-9]+")
_VAR_AGENT_MAX = 48

#: 逐家可覆盖的调参键（与 ``resolve_roster`` 认识的键一致）。
_TUNING_KEYS = ("timeout", "max_tokens", "temperature")


def _src(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def key_var_name(agent: str) -> str:
    """agent → 生成 key 的变量名（``deepseek-v4-pro`` → ``QM_DECISION_LLM_KEY_DEEPSEEK_V4_PRO``）。

    变量名由**身份**推出来，不按序号：序号会随名册顺序变，一改顺序 key 就得重填。
    """
    safe = _VAR_UNSAFE.sub("_", normalize_agent(agent).upper()).strip("_")
    return f"{KEY_VAR_PREFIX}{(safe or 'AGENT')[:_VAR_AGENT_MAX]}"


def _raw_entries(src: Mapping[str, str]) -> list[Any]:
    """当前名册串 → 原始项列表；没配/不是数组/坏 JSON → ``[]``（读接口不因此报错）。"""
    raw = str(src.get(ENV_ROSTER, "") or "").strip()
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except ValueError:
        return []
    return list(doc) if isinstance(doc, list) else []


def _refs_of(entries: Sequence[Any]) -> dict[str, dict[str, str]]:
    """原始项列表 → ``{agent: {"base_url_env": …, "api_key_env": …}}``（只取变量名）。

    用途有二：保存时知道「这家原来引用的是哪个变量」（用户没重填 key 就沿用它），
    以及算出哪些生成变量已无人引用（可清理）。
    """
    out: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        agent = normalize_agent(entry.get("model") or "")
        if not agent:
            continue
        out[agent] = {
            field: str(entry.get(f"{field}_env") or "").strip()
            for field in ("base_url", "api_key")
        }
    return out


def _resolve_one(src: Mapping[str, str], entry: Any) -> tuple[Any, str]:
    """**单家**解析（把这一项单独当整段名册喂给 ``resolve_roster``）。

    → ``(config, "")`` 或 ``(None, 报错原文)``。逐家解析而不是整段解析一次，是为了让
    「第 2 项缺 key」能报到第 2 项头上——整段解析会在第一处错误就抛，界面上只剩一句
    话，用户得自己去数字段。
    """
    try:
        one = {**src, ENV_ROSTER: json.dumps([entry], ensure_ascii=False)}
        return resolve_roster(one)[0], ""
    except LLMNotConfigured as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - 读接口不因单家异常整页失败
        return None, f"{type(exc).__name__}: {exc}"


def read_round_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """决策轮开关的**只读**状态（严读法，仅 ``"true"`` 算开）。

    判据走 ``env_flags.normalize_env_flag``（全项目唯一口径），**不另写一份**：
    「1/yes/on 算不算开」在本仓已经分叉过一次（P4 迁移），不能再来第二套。
    """
    from backend.shared.env_flags import normalize_env_flag

    return {
        "enabled": normalize_env_flag(_src(env).get(ENV_FLAG)),
        "env": ENV_FLAG,
        "note": (
            "开关只在 trade 服务启动时判一次，改了必须重启该服务才生效——"
            "本页只展示，不代写（写了不重启是假生效，写了重启是真钱开跑）"
        ),
    }


def read_agent_status(
    *, native: Any | None = None, last_key: str = "trade:decision-round:last"
) -> dict[str, Any]:
    """逐家状态镜像：``last``（最后完成的那一家）与 ``last:{agent}``。

    键的读写口径属调度层（``decision_round_io.write_status``），这里只读；Redis 拿不到
    就返回空 dict 并在 ``error`` 里说明——**不假绿**（面板显示「无状态镜像」是对的，
    显示「一切正常」才是错的）。
    """
    try:
        if native is None:
            from backend.services.trade.services.decision_round_io import (
                native_redis_client,
            )

            native = native_redis_client()
        client = native
        out: dict[str, Any] = {"last": _load_json(client.get(last_key)), "agents": {}}
        for key in client.scan_iter(match=f"{last_key}:*", count=100):
            agent = str(key).split(f"{last_key}:", 1)[-1]
            out["agents"][agent] = _load_json(client.get(key))
        out["error"] = ""
        return out
    except Exception as exc:  # noqa: BLE001 - 状态读不到不该让配置页失败
        return {"last": None, "agents": {}, "error": f"{type(exc).__name__}: {exc}"}


def _load_json(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def describe(
    *,
    env: Mapping[str, str] | None = None,
    status_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """名册现状（**永不抛**）：逐家声明的端点/变量名/key 有没有配 + 逐家解析结果。

    读接口不因「名册坏了」而 500——名册坏的时候正是最需要看这个页面的时候。
    """
    src = _src(env)
    raw_entries = _raw_entries(src)
    roster_configured = bool(str(src.get(ENV_ROSTER, "") or "").strip())
    # 「永不抛」是这一层的契约（名册坏掉时正是最需要看这一页的时候），所以状态镜像
    # 这一侧也要在这里兜住：注入点（含测试替身）抛异常或返回非 dict 都不该整页失败。
    try:
        status = (status_reader or read_agent_status)()
    except Exception as exc:  # noqa: BLE001
        status = {"last": None, "agents": {}, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(status, Mapping):
        status = {"last": None, "agents": {}, "error": "状态镜像返回了非对象"}

    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries):
        model = str(entry.get("model") or "").strip() if isinstance(entry, Mapping) else ""
        agent = normalize_agent(model)
        config, error = _resolve_one(src, entry)
        key_env = (
            str(entry.get("api_key_env") or "").strip()
            if isinstance(entry, Mapping)
            else ""
        )
        key_literal = (
            str(entry.get("api_key") or "").strip()
            if isinstance(entry, Mapping)
            else ""
        )
        entries.append(
            {
                "index": index + 1,
                "model": model,
                "agent": agent,
                "ok": config is not None,
                "error": error,
                # 端点只报**声明**（变量名或字面值）与解析值；key 只报「配没配」
                "base_url": (config.base_url if config else ""),
                "base_url_env": (
                    str(entry.get("base_url_env") or "").strip()
                    if isinstance(entry, Mapping)
                    else ""
                ),
                "api_key_env": key_env,
                # 字面值直接写在名册串里：能跑，但名册会进日志/状态键/工单 ⇒ 只报事实，
                # 界面上据此提示「建议改存变量」（见 key_var_name 的用法）
                "api_key_inline": bool(key_literal),
                "api_key_set": bool(
                    key_literal
                    or (key_env and str(src.get(key_env, "")).strip())
                    or (config and config.api_key)
                ),
                "timeout": config.timeout if config else None,
                "max_tokens": config.max_tokens if config else None,
                "temperature": config.temperature if config else None,
                "status": status.get("agents", {}).get(agent),
            }
        )

    single_ok, single_error = _single_status(src)
    return {
        "roster_configured": roster_configured,
        # 不配名册 = 单家三件套（逐字旧行为）；配了但坏了 → 一家都跑不动
        "source": "roster" if roster_configured else ("single" if single_ok else "none"),
        "entries": entries,
        "limit": AGENT_LIMIT,
        "env": ENV_ROSTER,
        "runtime_env_path": str(runtime_env_path()),
        "round": read_round_status(src),
        "last": status.get("last"),
        "status_error": status.get("error") or "",
        "single": {"ok": single_ok, "error": single_error},
        "error": _roster_error(src) if roster_configured else single_error,
    }


def _single_status(src: Mapping[str, str]) -> tuple[bool, str]:
    """全局三件套（单家路径）能不能解析——名册不配时它就是实际在跑的那套。"""
    from backend.shared.decision_llm_client import resolve_config

    try:
        resolve_config(src)
        return True, ""
    except LLMNotConfigured as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _roster_error(src: Mapping[str, str]) -> str:
    """整段名册的错误原文（能解析 → 空串）。"""
    try:
        resolve_roster(src)
        return ""
    except LLMNotConfigured as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _clean_entry(entry: Any, index: int) -> tuple[dict[str, Any] | None, str]:
    """一项 UI 载荷 → 名册项（校验：model 必填、数字键可解析）；坏则给一句人话。"""
    if not isinstance(entry, Mapping):
        return None, f"第 {index + 1} 项不是对象"
    model = str(entry.get("model") or "").strip()
    if not model or looks_like_placeholder(model):
        return None, f"第 {index + 1} 项缺 model（模型名必填，它同时是这个 agent 的身份）"
    out: dict[str, Any] = {"model": model}
    base_url = str(entry.get("base_url") or "").strip()
    base_url_env = str(entry.get("base_url_env") or "").strip()
    if base_url_env:
        out["base_url_env"] = base_url_env
    elif base_url:
        out["base_url"] = base_url
    for key in _TUNING_KEYS:
        raw = entry.get(key)
        if raw is None or raw == "":
            continue
        cast = int if key == "max_tokens" else float
        try:
            out[key] = cast(raw)
        except (TypeError, ValueError):
            return None, f"第 {index + 1} 项（{model}）的 {key}={raw!r} 不是数字"
    return out, ""


def apply_roster(
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    set_secret_fn: Callable[[str, str], Any] = set_secret,
    delete_secret_fn: Callable[[str], Any] = delete_secret,
) -> dict[str, Any]:
    """保存名册（**先校验、再落盘、后回读复验**；复验失败自动回滚到旧名册）。

    载荷：``{"entries": [{"model", "base_url", "api_key", "api_key_env", "timeout",
    "max_tokens", "temperature"}]}``。key 的三态：

    * 填了 ``api_key`` → 写进 ``runtime.env`` 的 :func:`key_var_name` 变量并引用它；
    * 没填（空/缺） → **沿用**这家原来引用的变量（用户只改端点时不会把 key 改没）；
    * ``clear_api_key`` → 删掉生成变量、不再引用（回落到全局三件套）。

    落盘顺序是「**先 key 后名册**」：名册串一旦写入就立刻生效，不能让它引用一个还没
    写下去的变量（那是「保存成功但下一轮报未配置」）。
    """
    src = _src(env)
    raw_in = payload.get("entries")
    if not isinstance(raw_in, list) or not raw_in:
        return {"ok": False, "errors": ["名册至少要有一家；要停决策轮请关开关，不要清空名册"]}
    if len(raw_in) > AGENT_LIMIT:
        return {
            "ok": False,
            "errors": [
                f"配了 {len(raw_in)} 家、超过上限 {AGENT_LIMIT} 家：一轮里逐家串行跑，"
                "排在后面的家会跑不完（表现为那家当天不决策）"
            ],
        }

    previous_refs = _refs_of(_raw_entries(src))
    cleaned: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_agent: dict[str, int] = {}
    seen_var: dict[str, int] = {}
    for index, entry in enumerate(raw_in):
        item, err = _clean_entry(entry, index)
        if err:
            errors.append(err)
            continue
        assert item is not None  # err 为空时 _clean_entry 必回填 item
        agent = normalize_agent(item["model"])
        if agent in seen_agent:
            errors.append(
                f"第 {index + 1} 项（{item['model']}）与第 {seen_agent[agent]} 项归一后重名"
                f"（agent={agent}）：agent 进分账账本段/幂等键/审计行，同名会把两家并成一本账"
            )
            continue
        seen_agent[agent] = index + 1
        var = key_var_name(agent)
        if var in seen_var:
            errors.append(
                f"第 {index + 1} 项（{item['model']}）与第 {seen_var[var]} 项的 key 变量名"
                f"撞了（{var}）：请改模型名，否则两家的 key 会互相覆盖"
            )
            continue
        seen_var[var] = index + 1

        if bool(entry.get("clear_api_key")):
            item["_drop_key_var"] = var
            item["_delete_key_var"] = var
        else:
            literal = str(entry.get("api_key") or "").strip()
            explicit_env = str(entry.get("api_key_env") or "").strip()
            if literal:
                item["api_key_env"] = var
                item["_write_key_var"] = (var, literal)
            elif explicit_env:
                item["api_key_env"] = explicit_env
            elif previous_refs.get(agent, {}).get("api_key"):
                item["api_key_env"] = previous_refs[agent]["api_key"]
        cleaned.append(item)

    if errors:
        return {"ok": False, "errors": errors}

    # 预览校验：整段串出来先让真解析器过一遍，不合法就不落盘（错误原文带项号/模型/变量名）。
    # **预览环境要把「待写入的 key」先放进去**：pre-check 跑在落盘之前，新填的 key 此刻
    # 还不在环境里——不预置的话，第一次保存一把新 key 会被判「未配置」而永远存不进去。
    candidate = [
        {k: v for k, v in item.items() if not k.startswith("_")} for item in cleaned
    ]
    preview = dict(src)
    for item in cleaned:
        write = item.get("_write_key_var")
        if write:
            preview[write[0]] = write[1]
    try:
        resolve_roster({**preview, ENV_ROSTER: json.dumps(candidate, ensure_ascii=False)})
    except LLMNotConfigured as exc:
        return {"ok": False, "errors": [str(exc)]}

    previous_raw = str(src.get(ENV_ROSTER, "") or "")
    written_keys: list[str] = []
    deleted_keys: list[str] = []
    for item in cleaned:
        write = item.get("_write_key_var")
        if write:
            var, value = write
            try:
                set_secret_fn(var, value)
            except Exception as exc:  # noqa: BLE001 - 变量名/取值非法：如实报，不半途而废
                return {"ok": False, "errors": [f"写入 {var} 失败：{exc}"]}
            written_keys.append(var)
    roster_text = json.dumps(candidate, ensure_ascii=False)
    try:
        set_secret_fn(ENV_ROSTER, roster_text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [f"写入 {ENV_ROSTER} 失败：{exc}"]}

    # 回读复验：真解析器读**落盘后**的环境。失败就回滚并如实报（不留下一个「显示已保存、
    # 实际一轮都跑不动」的名册）。
    verify_src = os.environ if env is None else env
    try:
        resolved = resolve_roster(verify_src)
    except Exception as exc:  # noqa: BLE001
        _rollback(previous_raw, set_secret_fn)
        return {
            "ok": False,
            "errors": [f"回读复验失败、已回滚到上一版名册：{exc}"],
            "rolled_back": True,
        }

    referenced = {item["api_key_env"] for item in cleaned if item.get("api_key_env")}
    referenced |= {
        item["base_url_env"] for item in cleaned if item.get("base_url_env")
    }
    for name in sorted(_stale_key_vars(verify_src, referenced)):
        try:
            delete_secret_fn(name)
            deleted_keys.append(name)
        except Exception as exc:  # noqa: BLE001 - 清理失败只记，不影响已生效的名册
            logger.warning("[decision-roster] 清理残留变量 %s 失败: %s", name, exc)

    dropped = [
        item["_delete_key_var"] for item in cleaned if item.get("_delete_key_var")
    ]
    for name in dropped:
        try:
            delete_secret_fn(name)
            deleted_keys.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[decision-roster] 删除变量 %s 失败: %s", name, exc)

    return {
        "ok": True,
        "errors": [],
        "agents": [normalize_agent(item["model"]) for item in cleaned],
        "models": [c.model for c in resolved],
        "written_key_vars": written_keys,
        "deleted_key_vars": sorted(set(deleted_keys)),
        "note": "已生效（trade 进程即时读到；下一轮 tick 用新名册）。无需重启。",
    }


def _rollback(previous_raw: str, set_secret_fn: Callable[[str, str], Any]) -> None:
    """复验失败时把名册串写回上一版（空串 = 改名册为不配，回单家三件套）。"""
    try:
        set_secret_fn(ENV_ROSTER, previous_raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("[decision-roster] 回滚 %s 失败: %s", ENV_ROSTER, exc)


def _stale_key_vars(src: Mapping[str, str], referenced: set[str]) -> set[str]:
    """``runtime.env`` 里带本模块前缀、但已无人引用的变量（历史残留）。

    只看 ``runtime.env`` 的**文件内容**：进程环境里同名的真实变量不归本模块管，
    删它越权（那是运维在 compose 里显式注入的）。
    """
    try:
        on_disk = read_runtime_env()
    except Exception:  # noqa: BLE001
        return set()
    return {
        name
        for name in on_disk
        if name.startswith(KEY_VAR_PREFIX) and name not in referenced
    }


def clear_roster(
    *,
    env: Mapping[str, str] | None = None,
    set_secret_fn: Callable[[str, str], Any] = set_secret,
    delete_secret_fn: Callable[[str], Any] = delete_secret,
) -> dict[str, Any]:
    """清掉名册（回单家三件套）并删掉本模块生成的 key 变量。**不碰全局三件套。**"""
    src = _src(env)
    previous_raw = str(src.get(ENV_ROSTER, "") or "")
    try:
        set_secret_fn(ENV_ROSTER, "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [f"清空 {ENV_ROSTER} 失败：{exc}"]}
    try:
        resolve_roster(_src(env))
    except Exception as exc:  # noqa: BLE001 - 清完连单家都不可用：回滚，别把能跑的搞停
        _rollback(previous_raw, set_secret_fn)
        return {
            "ok": False,
            "errors": [
                f"已回滚：清空后连单家三件套也不可用（{exc}）。"
                "先把 QM_DECISION_LLM_BASE_URL / QM_DECISION_LLM_API_KEY / "
                "QM_DECISION_LLM_MODEL 配上再清空——否则清空即静默停机"
                "（每轮都跑不成，而心跳照写）"
            ],
            "rolled_back": True,
        }
    deleted: list[str] = []
    for name in sorted(_stale_key_vars(_src(env), set())):
        try:
            delete_secret_fn(name)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[decision-roster] 删除变量 %s 失败: %s", name, exc)
    return {
        "ok": True,
        "errors": [],
        "deleted_key_vars": deleted,
        "note": "已回到单家三件套（QM_DECISION_LLM_BASE_URL / _API_KEY / _MODEL）。",
    }
