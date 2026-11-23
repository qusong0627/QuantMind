"""提交跨库 T+5 模型训练（lightgbm/xgboost/catboost/nativetft/linear/random_forest/mlp），
并对训练 config.yaml 做回读断言。

唯一事实源：`data/quantdb/factor_research/screening/training_selection_300.json`
（`backend/scripts/select_training_features.py` 的产物；`train_plan` + `features_payload`）。

对比训练（同一套因子/切分换模型类型）：`--model-type xgboost`（job/display 名按
模型 token 自动改写，LGB↔XGB↔CAT），并建议 `--pin-versions-from <基准 run 的
config.yaml>` 钉扎逐库目录版本——版本不一致即拒绝提交，保证对比吃同一份因子快照。

流程：
1. admin 登录（口令取环境变量 QM_PASSWORD）；
2. 对 `libraries_used` 逐库拉 `GET /models/feature-catalog`，取 published 版本号
   与该库已启用特征的 ``key -> source_column`` 映射，对 300 条 features 做逐条预校验
   （锚库裸名 ∈ 锚库键；``"库:列"`` ∈ 该库键）——**任何一条不命中即列全量缺失并退出，
   绝不带着缺特征提交**；
3. 组装载荷：``factor_selection.method=""``（训练不做二次筛选；需要训练端筛选时
   用 ``--factor-selection-method ic_icir --n-top N`` 显式开启）；
   ``factor_source`` = 锚库 + 全副库 ``factor_catalog_versions`` 版本 map；
   ``pause_others=false``（不动并行会话的非 quantmind 容器）；
4. ``POST /models/run-training``；
5. 轮询 ``data/training_jobs/<run_id>/config.yaml`` 断言：特征数=300、
   ``factor_field_sources`` 逐条来源一致、label.horizon=5、筛选 method 与提交一致、
   版本 map 逐字一致、切分窗口逐字一致。

用法：
    QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py --dry-run
    QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py
    # 对比训练：同因子/窗口换模型类型 + 钉扎库版本
    QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py \\
        --model-type xgboost --pin-versions-from data/training_jobs/<base_run>/config.yaml
    # GPU TFT（NativeTFT，dl_params 走内置配方；训练容器自动探测 GPU，无卡回落 CPU）
    QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py \\
        --model-type nativetft --pin-versions-from data/training_jobs/<base_run>/config.yaml
    # 无 admin 口令时走内部调用（secret 取 env INTERNAL_CALL_SECRET，fail-closed）
    INTERNAL_CALL_SECRET=... python3 backend/scripts/submit_crosslib_training.py \\
        --internal-call --internal-user-id 10000001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SELECTION = (
    _ROOT / "data/quantdb/factor_research/screening/training_selection_300.json"
)
_JOBS_DIR = _ROOT / "data/training_jobs"

_MARKET = "CN"
_CONFIG_POLL_INTERVAL_S = 5.0

# 模型类型 → job/display 名 token 与模型产物文件名（train.py `_save_model` 分派）。
# nativetft 的落盘权重是固定名 model.pth（trainers_dl 写出，单模型流程不改名；
# metadata.model_file 记 model_nativetft.pth 属已知对不上，加载侧有 *.pth 兜底）。
_MODEL_TYPE_INFO: dict[str, dict[str, str]] = {
    "lightgbm": {"token": "LGB", "label": "LightGBM", "file": "model.lgb"},
    "xgboost": {"token": "XGB", "label": "XGBoost", "file": "model.xgb"},
    "catboost": {"token": "CAT", "label": "CatBoost", "file": "model.cbm"},
    "nativetft": {"token": "TFT", "label": "NativeTFT", "file": "model.pth"},
    # sklearn 系模型（train.py `_save_model` 统一 pickle 落盘 model.pkl）
    "linear": {"token": "LIN", "label": "Linear", "file": "model.pkl"},
    "random_forest": {"token": "RF", "label": "RandomForest", "file": "model.pkl"},
    "mlp": {"token": "MLP", "label": "MLP", "file": "model.pkl"},
    # Qlib DL 族：单模型流程由 trainers_dl 固定写 model.pth（`_save_model` 返回
    # model_<type>.pth 属已知对不上，注册侧按实际文件记 model.pth，加载侧有兜底）
    "gru": {"token": "GRU", "label": "GRU", "file": "model.pth"},
    "lstm": {"token": "LSTM", "label": "LSTM", "file": "model.pth"},
    "alstm": {"token": "ALSTM", "label": "ALSTM", "file": "model.pth"},
    "transformer": {"token": "TRF", "label": "Transformer", "file": "model.pth"},
    "tabnet": {"token": "TAB", "label": "TabNet", "file": "model.pth"},
    "tcn": {"token": "TCN", "label": "TCN", "file": "model.pth"},
}

# NativeTFT 配方（两轮全窗前例一致：2026-09-17 批 / 2026-09-23 批）。
# n_epochs=8 必须显式传（框架默认 200）；实测 best epoch=1~2、第 7 轮早停。
# 口径注意：训练日志的 valid_ic 是 Pearson，result.json by_split 才是平台 rank-IC。
_TFT_DL_PARAMS: dict[str, int] = {
    "n_epochs": 8,
    "step_len": 20,
    "batch_size": 4000,
    "early_stopping_rounds": 5,
}

# MLP（sklearn MLPRegressor）配方：batch_size 必须显式传——默认 200 在 650 万行
# 上是每轮 3.2 万次微型更新（实测 28 分钟跑不完首轮）；n_epochs=8 与 2026-09-17
# 历史 MLP 批次一致（trainer 把 n_epochs 映射成 max_iter）。
_MLP_DL_PARAMS: dict[str, int] = {
    "n_epochs": 8,
    "batch_size": 4000,
}

# Qlib DL 族配方（gru/lstm/alstm/transformer/tabnet/tcn）：与 2026-09-17/18
# 历史批一致。n_epochs 框架默认 200，必须显式收敛到 8；模型专属超参
# （lr/hidden_size/dropout 等）走 trainers_dl.py 各分支默认。
_QLIB_DL_TYPES = frozenset(
    {"gru", "lstm", "alstm", "transformer", "tabnet", "tcn"}
)
_QLIB_DL_PARAMS: dict[str, int] = {
    "n_epochs": 8,
    "step_len": 20,
    "batch_size": 4000,
    "early_stopping_rounds": 5,
}


def _dl_params_for(model_type: str) -> dict[str, int]:
    if model_type == "nativetft":
        return dict(_TFT_DL_PARAMS)
    if model_type == "mlp":
        return dict(_MLP_DL_PARAMS)
    if model_type in _QLIB_DL_TYPES:
        return dict(_QLIB_DL_PARAMS)
    return {}


def _derive_run_names(
    job_name: str, display_name: str, model_type: str
) -> tuple[str, str]:
    """把基准名里的模型 token 改写成目标模型；原名不含任何已知 token 时兜底追加。"""
    info = _MODEL_TYPE_INFO[model_type]
    for other in _MODEL_TYPE_INFO.values():
        if other["token"] != info["token"]:
            job_name = job_name.replace(f"_{other['token']}", f"_{info['token']}")
            display_name = display_name.replace(other["label"], info["label"])
    if info["token"] not in job_name:
        job_name = f"{job_name}_{info['token']}"
    if info["label"] not in display_name:
        display_name = f"{display_name} {info['label']}"
    return job_name, display_name


def _fail(msg: str) -> None:
    raise SystemExit(f"[submit] {msg}")


def _load_selection(path: Path) -> dict[str, Any]:
    if not path.is_file():
        _fail(f"选型产物不存在：{path}（先跑 select_training_features.py）")
    with path.open("r", encoding="utf-8") as fh:
        sel = json.load(fh)
    for key in ("train_plan", "features_payload", "libraries_used", "funnel"):
        if key not in sel:
            _fail(f"选型产物缺键 {key!r}：{path}")
    tp = sel["train_plan"]
    feats = [str(x).strip() for x in sel["features_payload"] if str(x).strip()]
    if not feats:
        _fail("features_payload 为空")
    if len(feats) != len(set(feats)):
        _fail("features_payload 有重复条目")
    outs = [x.split(":", 1)[1] if ":" in x else x for x in feats]
    dup = {x for x in outs if outs.count(x) > 1}
    if dup:
        _fail(f"输出列名跨库重名（提交必 422）：{sorted(dup)}")
    if tp.get("anchor_lib") not in sel["libraries_used"]:
        _fail("train_plan.anchor_lib 不在 libraries_used 中")
    return {
        "path": path,
        "train_plan": tp,
        "features": feats,
        "libraries": list(sel["libraries_used"]),
        "funnel_final": (sel.get("funnel") or {}).get("final"),
    }


async def _login(client, password: str) -> str:
    r = await client.post(
        "/auth/login",
        json={"username": "admin", "password": password, "tenant_id": "default"},
    )
    body = (
        r.json()
        if r.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    tok = body.get("access_token") or (body.get("data") or {}).get("access_token") or ""
    if r.status_code != 200 or not tok:
        _fail(f"登录失败 {r.status_code}: {str(r.text)[:200]}")
    return str(tok)


def _build_internal_headers(user_id: str, tenant_id: str) -> dict[str, str]:
    """内部调用身份头：secret 只从 env 读，缺失即拒绝（fail-closed）。"""
    secret = os.getenv("INTERNAL_CALL_SECRET", "").strip()
    if not secret:
        _fail("内部调用需 env INTERNAL_CALL_SECRET（fail-closed，不落默认值）")
    if not user_id:
        _fail("内部调用需 --internal-user-id（模型产物按用户目录落盘）")
    return {
        "X-Internal-Call": secret,
        "X-User-Id": user_id,
        "X-Tenant-Id": tenant_id or "default",
    }


async def _fetch_catalog(client, headers: dict[str, str], lib: str) -> dict[str, Any]:
    """返回 {version_id, columns: key -> source_column（仅 enabled 项）}；不 ready 即退出。"""
    r = await client.get(
        "/models/feature-catalog",
        params={"market": _MARKET, "factor_source": lib},
        headers=headers,
    )
    if r.status_code != 200:
        _fail(f"拉取目录失败 {lib}: {r.status_code} {str(r.text)[:200]}")
    data = r.json()
    status = str(data.get("catalog_status") or "")
    version_id = str(data.get("version_id") or "")
    if status != "ready" or not version_id:
        _fail(f"目录未就绪 {lib}: catalog_status={status!r} version_id={version_id!r}")
    mapping: dict[str, str] = {}
    for cat in data.get("categories") or []:
        for feat in cat.get("features") or []:
            key = str(feat.get("key") or "").strip()
            if not key or feat.get("enabled") is False:
                continue
            col = str(feat.get("source_column") or "").strip() or key
            mapping[key] = col
    if not mapping:
        _fail(f"目录 {lib} 没有任何 enabled 特征")
    return {"version_id": version_id, "columns": mapping}


def _validate_features(
    features: list[str], anchor: str, catalogs: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """逐条预校验，返回输出列名 -> factor_field_sources 值（与 resolver 同构）。"""
    expected: dict[str, str] = {}
    missing: list[str] = []
    for item in features:
        if ":" in item:
            lib, key = item.split(":", 1)
            cat = catalogs.get(lib)
            if cat is None or key not in cat["columns"]:
                missing.append(item)
                continue
            expected[key] = f"{lib}:{cat['columns'][key]}"
        else:
            cat = catalogs.get(anchor)
            if cat is None or item not in cat["columns"]:
                missing.append(item)
                continue
            expected[item] = str(cat["columns"][item])
    if missing:
        _fail(
            f"{len(missing)}/{len(features)} 条特征不在所属库的已发布目录中：{missing}"
        )
    if len(expected) != len(features):
        _fail(f"输出列名折叠：期望 {len(features)} 实得 {len(expected)}")
    return expected


def _build_payload(
    sel: dict[str, Any],
    versions: dict[str, str],
    fs_method: str = "",
    fs_n_top: int = 0,
    fs_pfs: bool = True,
    fs_dh: bool = True,
    dl_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tp = sel["train_plan"]
    anchor = tp["anchor_lib"]
    if anchor not in versions:
        _fail(f"锚库 {anchor} 缺版本号")
    feats = sel["features"]
    method = str(fs_method or "").strip().lower()
    n_top = int(fs_n_top or 0) or len(feats)
    dl_params = _dl_params_for(str(tp["model_type"]))
    if dl_override:
        dl_params.update(dl_override)
    payload: dict[str, Any] = {
        "job_name": tp["job_name"],
        "display_name": tp["display_name"],
        "model_type": tp["model_type"],
        "features": feats,
        "target_horizon_days": int(tp["label"]["target_horizon_days"]),
        "target_mode": str(tp["label"]["target_mode"]),
        "label_formula": "",
        "effective_trade_date": "",
        "training_window": "",
        "num_boost_round": int(tp["num_boost_round"]),
        "early_stopping_rounds": int(tp["early_stopping_rounds"]),
        "lgb_params": {},  # 走平台默认（LR/叶子数/正则等）
        # nativetft 显式传配方 dl_params（n_epochs=8 必须传，框架默认 200）；
        # 树模型保持 {}（走各自默认）；--dl-params 可覆盖/追加。
        "dl_params": dl_params,
        "max_time_minutes": int(tp["max_time_minutes"]),
        "context": dict(tp["context"]),
        "explain": {
            "enable_shap": True,
            "shap_split": "valid",
            "shap_sample_rows": 30000,
        },
        "ensemble": "none",
        "deploy_to_production": False,
        # method=""（默认）：训练不做 IC/ICIR 二次筛选；显式传键，杜绝编排器默认注入
        # method=ic_icir / n_top=80。需要训练端筛选时经 --factor-selection-method 显式开启。
        "factor_selection": {
            "method": method,
            "n_top": n_top,
            "ic_threshold": 0.01,
            "icir_threshold": 0.15,
            "correlation_threshold": 0.9,
            "pfs_enabled": bool(fs_pfs),
            "dh_enabled": bool(fs_dh),
        },
        "auto_feature_filter": "false",
        # 显式 false：pause_others 会 stop 非 quantmind 前缀容器（并行会话的其他栈），
        # 而本机非 quantmind 容器合计仅 ~0.6G，收益可忽略、误伤风险实存。
        "pause_others": False,
        "required_artifacts": [
            _MODEL_TYPE_INFO[str(tp["model_type"])]["file"],
            "pred.parquet",
            "pred.pkl",
            "metadata.json",
            "config.yaml",
            "result.json",
        ],
        "factor_source": anchor,
        "factor_catalog_version": versions[anchor],
        "factor_catalog_versions": {
            lib: vid for lib, vid in versions.items() if lib != anchor
        },
    }
    split = tp["split"]
    for key in (
        "train_start",
        "train_end",
        "valid_start",
        "valid_end",
        "test_start",
        "test_end",
    ):
        payload[key] = str(split[key])
    return payload


def _artifact_names(raw: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                for k in ("name", "path", "file"):
                    if item.get(k):
                        names.add(str(item[k]))
    return names


def _assert_config(
    cfg: dict[str, Any],
    sel: dict[str, Any],
    versions: dict[str, str],
    expected_sources: dict[str, str],
    fs_method: str = "",
    fs_n_top: int = 0,
    dl_expected: dict[str, Any] | None = None,
) -> list[str]:
    """返回失败清单（空=全部通过）。"""
    fails: list[str] = []
    data = cfg.get("data") or {}
    label = cfg.get("label") or {}
    fsel = cfg.get("factor_selection") or {}
    model = cfg.get("model") or {}
    split = cfg.get("split") or {}
    context = cfg.get("context") or {}
    tp = sel["train_plan"]

    cfg_feats = [str(x) for x in (data.get("features") or [])]
    want_feats = list(expected_sources.keys())
    if len(cfg_feats) != len(want_feats):
        fails.append(f"特征数不符：config {len(cfg_feats)} != 期望 {len(want_feats)}")
    elif set(cfg_feats) != set(want_feats):
        diff = sorted(set(cfg_feats) ^ set(want_feats))
        fails.append(f"特征集合不符（前 10 差异）：{diff[:10]}")

    sources = data.get("factor_field_sources") or {}
    if len(sources) != len(expected_sources):
        fails.append(
            f"factor_field_sources 数不符：{len(sources)} != {len(expected_sources)}"
        )
    src_mismatch = [
        f"{k}: {sources.get(k)!r} != {v!r}"
        for k, v in list(expected_sources.items())
        if sources.get(k) != v
    ]
    if src_mismatch:
        fails.append(f"factor_field_sources 不一致（前 5）：{src_mismatch[:5]}")

    if int(label.get("target_horizon_days") or -1) != int(
        tp["label"]["target_horizon_days"]
    ):
        fails.append(
            f"label.target_horizon_days={label.get('target_horizon_days')!r} != "
            f"{tp['label']['target_horizon_days']}"
        )
    if str(label.get("target_mode") or "") != str(tp["label"]["target_mode"]):
        fails.append(f"label.target_mode={label.get('target_mode')!r}")

    want_method = str(fs_method or "").strip().lower()
    if str(fsel.get("method") or "").strip().lower() != want_method:
        fails.append(
            f"factor_selection.method={fsel.get('method')!r}（应为 {want_method!r}）"
        )
    if want_method and int(fsel.get("n_top") or 0) != int(fs_n_top or 0):
        fails.append(
            f"factor_selection.n_top={fsel.get('n_top')!r}（应为 {int(fs_n_top or 0)}）"
        )

    if str(model.get("type") or "") != str(tp["model_type"]):
        fails.append(f"model.type={model.get('type')!r} != {tp['model_type']!r}")
    expected_dl = _dl_params_for(str(tp["model_type"]))
    if dl_expected:
        expected_dl.update(dl_expected)
    if str(tp["model_type"]) in ("lightgbm", "xgboost", "catboost"):
        if int(model.get("num_boost_round") or -1) != int(tp["num_boost_round"]):
            fails.append(f"model.num_boost_round={model.get('num_boost_round')!r}")
        if int(model.get("early_stopping_rounds") or -1) != int(
            tp["early_stopping_rounds"]
        ):
            fails.append(
                f"model.early_stopping_rounds={model.get('early_stopping_rounds')!r}"
            )
    elif expected_dl:
        got_dl = model.get("dl_params") or {}
        if {k: got_dl.get(k) for k in expected_dl} != expected_dl:
            fails.append(
                f"model.dl_params 缺项/不符：{got_dl!r}（期望含 {expected_dl!r}）"
            )

    if str(context.get("market") or "") != str(tp["context"]["market"]):
        fails.append(f"context.market={context.get('market')!r}")

    for seg in ("train", "valid", "test"):
        got = [str(x) for x in (split.get(seg) or [])]
        want = [str(tp["split"][f"{seg}_start"]), str(tp["split"][f"{seg}_end"])]
        if got != want:
            fails.append(f"split.{seg}={got} != {want}")

    cfg_versions = {
        str(k): str(v) for k, v in (data.get("factor_catalog_versions") or {}).items()
    }
    if cfg_versions != versions:
        fails.append(
            f"factor_catalog_versions 不一致：config={cfg_versions} want={versions}"
        )
    if str(data.get("factor_source") or "") != tp["anchor_lib"]:
        fails.append(f"data.factor_source={data.get('factor_source')!r}")

    want_art = {
        _MODEL_TYPE_INFO[str(tp["model_type"])]["file"],
        "pred.parquet",
        "pred.pkl",
        "metadata.json",
        "config.yaml",
        "result.json",
    }
    got_art = _artifact_names((cfg.get("output") or {}).get("required_artifacts"))
    if not want_art.issubset(got_art):
        fails.append(
            f"required_artifacts 缺项：{sorted(want_art - got_art)}（实得 {sorted(got_art)}）"
        )
    return fails


def _load_pinned_versions(path: Path) -> dict[str, str]:
    """从基准 run 的 config.yaml 读 data.factor_catalog_versions（含锚库的全量 map）。"""
    if not path.is_file():
        _fail(f"--pin-versions-from 路径不存在：{path}")
    import yaml

    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    pinned = (cfg.get("data") or {}).get("factor_catalog_versions") or {}
    if not pinned:
        _fail(f"{path} 未含 data.factor_catalog_versions，无法钉扎")
    return {str(k): str(v) for k, v in pinned.items()}


async def _poll_config(run_id: str, timeout_s: float) -> Path:
    path = _JOBS_DIR / run_id / "config.yaml"
    waited = 0.0
    while waited <= timeout_s:
        if path.is_file():
            return path
        await asyncio.sleep(_CONFIG_POLL_INTERVAL_S)
        waited += _CONFIG_POLL_INTERVAL_S
    _fail(f"config.yaml 在 {timeout_s:.0f}s 内未出现：{path}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selection-json", type=Path, default=_DEFAULT_SELECTION)
    ap.add_argument("--api-base", default="http://localhost:8000/api/v1")
    ap.add_argument("--password", default=os.getenv("QM_PASSWORD", ""))
    ap.add_argument(
        "--internal-call",
        action="store_true",
        help="走内部调用身份头（X-Internal-Call），免 admin 口令",
    )
    ap.add_argument(
        "--internal-user-id",
        default="",
        help="内部调用时代表哪个用户（模型产物落该用户目录，如 10000001）",
    )
    ap.add_argument("--internal-tenant-id", default="default")
    ap.add_argument("--dry-run", action="store_true", help="只校验 + 打印载荷，不提交")
    ap.add_argument(
        "--wait-config", type=float, default=180.0, help="config.yaml 等待秒数"
    )
    ap.add_argument(
        "--model-type",
        choices=sorted(_MODEL_TYPE_INFO),
        default=None,
        help="覆盖 train_plan.model_type（对比训练用；job/display 名按 token 自动改写）",
    )
    ap.add_argument(
        "--job-name", default="", help="覆盖任务机器名（默认按 model-type 改写）"
    )
    ap.add_argument(
        "--display-name", default="", help="覆盖任务展示名（默认按 model-type 改写）"
    )
    ap.add_argument(
        "--pin-versions-from",
        type=Path,
        default=None,
        help="基准 run 的 config.yaml——逐库目录版本必须与其一致，否则拒绝提交（对比训练同快照）",
    )
    ap.add_argument(
        "--factor-selection-method",
        choices=["", "ic_icir", "combined"],
        default="",
        help=(
            "训练端因子筛选：''（默认，不筛）/ ic_icir（IC/ICIR 初筛+相关性去冗余+PFS/DH）。"
            "开启后模型实际入模特征由训练段筛选决定，config 里仍是提交全量清单。"
        ),
    )
    ap.add_argument(
        "--n-top",
        type=int,
        default=0,
        help="训练端筛选保留特征数（0=提交特征数；仅 method 非空时生效）",
    )
    ap.add_argument(
        "--no-pfs", action="store_true", help="关闭 PFS 扰动保真度闸门（默认开）"
    )
    ap.add_argument(
        "--no-dh", action="store_true", help="关闭 DH 多样性增益闸门（默认开）"
    )
    ap.add_argument(
        "--dl-params",
        default="",
        help=(
            "JSON 对象，覆盖/追加 dl_params（仅 DL 模型；例："
            "'{\"n_epochs\":10,\"early_stopping_rounds\":100,\"lr\":0.0001}'）"
        ),
    )
    args = ap.parse_args()

    sel = _load_selection(args.selection_json)
    tp = sel["train_plan"]
    fs_method = str(args.factor_selection_method or "").strip().lower()
    fs_n_top = int(args.n_top or 0) or len(sel["features"])
    fs_pfs = bool(fs_method) and not args.no_pfs
    fs_dh = bool(fs_method) and not args.no_dh
    dl_override: dict[str, Any] = {}
    if args.dl_params:
        try:
            parsed = json.loads(args.dl_params)
        except json.JSONDecodeError as exc:
            _fail(f"--dl-params 不是合法 JSON：{exc}")
        if not isinstance(parsed, dict):
            _fail("--dl-params 必须是 JSON 对象")
        dl_override = parsed
    model_type = str(args.model_type or tp.get("model_type") or "lightgbm")
    if model_type not in _MODEL_TYPE_INFO:
        _fail(f"不支持的 model_type={model_type!r}（可选：{sorted(_MODEL_TYPE_INFO)}）")
    job_name, display_name = _derive_run_names(
        tp["job_name"], tp["display_name"], model_type
    )
    if args.job_name:
        job_name = args.job_name
    if args.display_name:
        display_name = args.display_name
    sel = {
        **sel,
        "train_plan": {
            **tp,
            "model_type": model_type,
            "job_name": job_name,
            "display_name": display_name,
        },
    }
    tp = sel["train_plan"]
    print(f"[submit] 选型产物：{sel['path']}")
    print(
        f"[submit] features_payload={len(sel['features'])} 库={len(sel['libraries'])} "
        f"funnel.final={sel['funnel_final']}"
    )
    print(
        f"[submit] job_name={tp['job_name']!r} display={tp['display_name']!r} "
        f"model={tp['model_type']} horizon={tp['label']['target_horizon_days']} 窗="
        f"{tp['split']['train_start']}..{tp['split']['test_end']}"
    )

    import httpx
    import yaml

    async with httpx.AsyncClient(base_url=args.api_base, timeout=120) as client:
        if args.internal_call:
            auth_headers = _build_internal_headers(
                args.internal_user_id, args.internal_tenant_id
            )
            print("[submit] 认证方式：内部调用身份头（X-Internal-Call）")
        else:
            if not args.password:
                _fail("未提供 admin 口令（env QM_PASSWORD 或 --password）")
            token = await _login(client, args.password)
            auth_headers = {"Authorization": f"Bearer {token}"}
        catalogs: dict[str, dict[str, Any]] = {}
        for lib in sel["libraries"]:
            catalogs[lib] = await _fetch_catalog(client, auth_headers, lib)
            print(
                f"  · {lib:22s} {catalogs[lib]['version_id']}  enabled={len(catalogs[lib]['columns'])}"
            )
        if args.pin_versions_from:
            versions_now = {
                lib: str(cat["version_id"]) for lib, cat in catalogs.items()
            }
            pinned_all = _load_pinned_versions(args.pin_versions_from)
            # 只钉本 run 实际使用的库：选型子集（如第二梯队 300）不覆盖基准的全部库时，
            # 未使用的库版本与本 run 无关（config 也只写用到的库）。
            missing_pin = sorted(set(versions_now) - set(pinned_all))
            if missing_pin:
                _fail(f"钉扎基准缺库 {missing_pin}：无法保证同因子快照")
            pinned = {lib: pinned_all[lib] for lib in versions_now}
            if pinned != versions_now:
                diffs = [
                    f"{k}: now={versions_now.get(k)!r} pinned={pinned.get(k)!r}"
                    for k in sorted(versions_now)
                    if pinned.get(k) != versions_now.get(k)
                ]
                _fail(
                    "库目录版本与钉扎基准不一致（对比训练必须同一因子快照）："
                    + "；".join(diffs)
                )
            print(
                f"[submit] 库版本钉扎一致（{args.pin_versions_from}）：{len(pinned)} 库"
            )
        expected_sources = _validate_features(
            sel["features"], tp["anchor_lib"], catalogs
        )
        versions = {lib: str(cat["version_id"]) for lib, cat in catalogs.items()}

        payload = _build_payload(
            sel, versions, fs_method, fs_n_top, fs_pfs, fs_dh, dl_override
        )
        print(f"[submit] 预校验通过：{len(expected_sources)} 条特征全部命中已发布目录")
        print(
            f"[submit] 载荷摘要：features={len(payload['features'])} "
            f"method={payload['factor_selection']['method']!r} "
            f"n_top={payload['factor_selection']['n_top']} "
            f"pfs={payload['factor_selection']['pfs_enabled']} "
            f"dh={payload['factor_selection']['dh_enabled']} "
            f"pause_others={payload['pause_others']} "
            f"versions={len(payload['factor_catalog_versions']) + 1} 库"
        )
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print("[submit][dry-run] 未提交")
            return

        r = await client.post(
            "/models/run-training",
            json=payload,
            headers=auth_headers,
        )
        if r.status_code not in (200, 201, 202):
            _fail(f"提交失败 {r.status_code}: {str(r.text)[:600]}")
        body = r.json()
        run_id = str(body.get("runId") or body.get("run_id") or "")
        if not run_id:
            _fail(f"响应缺 runId：{str(body)[:400]}")
        print(
            f"[submit] 已提交 run_id={run_id} status={body.get('status')!r} "
            f"validFeatureCount={body.get('validFeatureCount')} "
            f"missingFeatureCount={body.get('missingFeatureCount')}"
        )
        if body.get("missingFeatureCount"):
            _fail(
                f"服务端报告缺失特征 {body.get('missingFeatureCount')} 个："
                f"{str(body.get('missingFeatures'))[:300]}"
            )

        cfg_path = await _poll_config(run_id, args.wait_config)
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        fails = _assert_config(
            cfg, sel, versions, expected_sources, fs_method, fs_n_top, dl_override
        )
        print(f"[submit] config.yaml 回读：{cfg_path}")
        if fails:
            for f in fails:
                print(f"  ✗ {f}")
            _fail(f"回读断言 {len(fails)} 条未通过")
        print(
            f"[submit] 回读断言全部通过：{len(expected_sources)} 特征 / "
            f"T+{tp['label']['target_horizon_days']} / method={fs_method!r} / "
            f"跨库来源逐条一致 / 窗口与版本钉扎一致"
        )
        print(f"[submit] run_id={run_id}，训练监控：data/training_jobs/{run_id}/")


if __name__ == "__main__":
    asyncio.run(main())
