"""普通 pickle 产物（sklearn MLP / Ridge）加载回归测试。

背景：训练端 numpy(>=2) 写出的 sklearn pickle，在推理环境 numpy(1.26) 上
`pickle.load` 直接抛 ValueError（`<class MT19937> is not a known BitGenerator module.`
或 MT19937 state 校验失败）——**这是跨版本序列化不兼容，不是产物损坏**。
表现是 `torch.load` 报 `Invalid magic number`（mlp 走的是 pytorch 框架分支，
落盘物却是普通 pickle）或 numpy 反序列化中途炸，模型在界面上显示正常但加载失败。

本文件钉住三件事：
1. 两类新式 pickle 形态（ctor 收到类对象 / state 是新式 dict）在兼容层下**必须能加载**，
   且严格路径确实会拒绝它们（证明兼容层不是恒真空转）；
2. 引擎加载器按 `framework` 分派后仍能落到普通 pickle（sklearn 分支与 pytorch 回退分支）；
3. 三份推理脚本（parquet 模板 / ensemble 模板 / 引擎加载器）都必须带兼容层，
   且**必须显式改写 dispatch 表** —— 只重写 `load_build` 是静默失效（C 版表不可改，
   纯 Python 版的表里存的是函数对象，不走属性查找）。这条只能靠源码守卫。

真实产物断言：现存模型目录里任何严格路径读不了的 `model.pkl`，都必须能被兼容层读回。
"""

from __future__ import annotations

import importlib.util
import json
import pickle
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 模型产物在容器内是 /app/models，宿主是 <repo>/models；测试里两个都试
MODELS_CANDIDATES = [Path("/app/models/users"), REPO_ROOT / "models" / "users"]

SCAN_GUARD_PATHS = {
    "parquet 模板": "backend/services/engine/inference/templates/inference_parquet.py",
    "ensemble 模板": "backend/services/engine/inference/templates/inference_ensemble_src.py",
    "引擎加载器": "backend/services/engine/inference/model_loader.py",
}


def _load_module_by_path(rel_path: str, name: str):
    """按文件路径加载模块（templates/ 与 scripts/ 不在包路径里）。"""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def loader_mod():
    return _load_module_by_path(
        "backend/services/engine/inference/model_loader.py", "model_loader_under_test"
    )


# ── 1. 新式 numpy pickle 的两类形态 ────────────────────────────────────
#
# 形态用 `__reduce__` 合成，与 numpy>=2 实际写出的流同构：
#   a) `__bit_generator_ctor(<class MT19937>)` —— 传类对象而非类名字符串；
#   b) ctor 正常（类名字符串）+ BUILD 阶段套用新式 state dict。
# 两者在 numpy 1.26 上都以 ValueError 拒绝，且报错文本含 BitGenerator / MT19937
# （兼容层的判定条件就是这个文本，故必须与真实报错一致）。


def _write_reduce_pickle(path: Path, reduce_fn) -> None:
    class _Synthetic:
        __reduce__ = staticmethod(reduce_fn)

    path.write_bytes(pickle.dumps(_Synthetic(), protocol=5))


def _shape_ctor_class_object():
    import numpy.random._pickle as np_pickle

    return (np_pickle.__bit_generator_ctor, (np.random.MT19937,), None)


def _shape_new_style_state_dict():
    import numpy.random._pickle as np_pickle

    return (
        np_pickle.__bit_generator_ctor,
        ("MT19937",),
        {
            "key": np.ones(624, dtype=np.uint32),
            "pos": 5,
            "has_gauss": 0,
            "cached_gaussian": 0.0,
        },
    )


_NEW_STYLE_SHAPES = {
    "ctor 收到类对象": _shape_ctor_class_object,
    "ctor 正常但 state 是新式 dict": _shape_new_style_state_dict,
}


@pytest.mark.parametrize("shape_name", sorted(_NEW_STYLE_SHAPES))
def test_strict_pickle_rejects_new_style_shape(tmp_path, shape_name):
    """先证明严格路径确实拒绝 —— 否则下面的兼容断言可能是恒真。"""
    pkl = tmp_path / "model.pkl"
    _write_reduce_pickle(pkl, _NEW_STYLE_SHAPES[shape_name])

    with pytest.raises(ValueError) as excinfo:
        with open(pkl, "rb") as f:
            pickle.load(f)

    message = str(excinfo.value)
    assert "BitGenerator" in message or "MT19937" in message


@pytest.mark.parametrize("shape_name", sorted(_NEW_STYLE_SHAPES))
def test_engine_loader_compat_reads_new_style_shape(tmp_path, loader_mod, shape_name):
    """兼容层必须把两类形态都读回来（RNG state 丢弃，对象其余部分完好）。"""
    pkl = tmp_path / "model.pkl"
    _write_reduce_pickle(pkl, _NEW_STYLE_SHAPES[shape_name])

    obj = loader_mod._load_plain_pickle(pkl)

    assert isinstance(obj, np.random.MT19937)


def _raise_unrelated_value_error():
    """模块级函数（局部函数不可 pickle）。"""
    raise ValueError("这是别的错误，不该被 RNG 兼容层吞掉")


def test_unrelated_value_error_is_not_swallowed(tmp_path, loader_mod):
    """兼容层只兜 BitGenerator/MT19937 文本；其它 ValueError 必须原样抛出。"""
    pkl = tmp_path / "model.pkl"

    class _Broken:
        def __reduce__(self):
            return (_raise_unrelated_value_error, ())

    pkl.write_bytes(pickle.dumps(_Broken(), protocol=5))
    with pytest.raises(ValueError, match="别的错误"):
        loader_mod._load_plain_pickle(pkl)


# ── 2. 引擎加载器：framework 分派后两种落点 ─────────────────────────────


def _make_model_dir(base: Path, metadata: dict, payload: object) -> Path:
    model_dir = base / "mdl_test_0001"
    model_dir.mkdir(parents=True)
    (model_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    with open(model_dir / "model.pkl", "wb") as f:
        pickle.dump(payload, f)
    return model_dir


def test_engine_loader_reads_sklearn_framework_pickle(tmp_path, loader_mod):
    payload = {"marker": "sklearn-artifact"}
    model_dir = _make_model_dir(
        tmp_path, {"framework": "sklearn", "model_file": "model.pkl"}, payload
    )

    loaded = loader_mod.ModelLoader(tmp_path).load_model("mdl_test_0001", model_dir=model_dir)

    assert loaded == payload


def test_engine_loader_falls_back_to_pickle_for_pytorch_framework(tmp_path, loader_mod):
    """mlp 的真实形态：framework=pytorch（训练端 `_get_model_framework` 的口径），
    落盘物却是普通 pickle → torch.load 报 Invalid magic number，必须回退到兼容层。"""
    payload = {"marker": "mlp-artifact"}
    model_dir = _make_model_dir(
        tmp_path,
        {"framework": "pytorch", "model_type": "mlp", "model_file": "model.pkl"},
        payload,
    )

    loaded = loader_mod.ModelLoader(tmp_path).load_model("mdl_test_0001", model_dir=model_dir)

    assert loaded == payload


# ── 3. 源码守卫：三份脚本都必须带兼容层 + dispatch 补丁 ──────────────────


@pytest.mark.parametrize("label", sorted(SCAN_GUARD_PATHS))
def test_all_inference_scripts_carry_pickle_compat_layer(label):
    src = (REPO_ROOT / SCAN_GUARD_PATHS[label]).read_text(encoding="utf-8")

    assert "def _load_plain_pickle" in src, f"{label} 缺普通 pickle 兼容加载函数"
    assert "class _NumpyCompatUnpickler(pickle._Unpickler)" in src, (
        f"{label} 的兼容 Unpickler 必须基于纯 Python 的 pickle._Unpickler"
    )
    # 这两行是静默失效的开关：注释掉它们，load_build 重写就完全不起作用，
    # 而所有「能读回对象」的测试仍可能通过（因为 find_class 走属性查找仍生效）——
    # 触发不了坏 state 的场景时完全无感。
    assert "_NumpyCompatUnpickler.dispatch = pickle._Unpickler.dispatch.copy()" in src, (
        f"{label} 未复制 dispatch 表 → load_build 重写不生效"
    )
    assert "_NumpyCompatUnpickler.dispatch[pickle.BUILD[0]]" in src, (
        f"{label} 未把 load_build 挂回 dispatch 表"
    )


def test_template_pkl_branches_route_through_compat_loader():
    """模板的 .pkl 分支不得再裸调 pickle.load（那正是跨版本失败的入口）。"""
    parquet = (
        REPO_ROOT / SCAN_GUARD_PATHS["parquet 模板"]
    ).read_text(encoding="utf-8")
    assert "model = _load_plain_pickle(model_path)" in parquet

    ensemble = (
        REPO_ROOT / SCAN_GUARD_PATHS["ensemble 模板"]
    ).read_text(encoding="utf-8")
    assert "return _load_plain_pickle(model_path)" in ensemble, "集成模板基模型 .pkl 分支未走兼容层"
    assert "meta_data = _load_plain_pickle(meta_model_path)" in ensemble, (
        "集成模板 meta_model.pkl 未走兼容层"
    )


# ── 4. 真实产物断言：现有模型里读不了的 .pkl 必须能读回 ──────────────────


def _iter_real_pickles():
    """遍历真实模型目录里的权重 pickle。

    目录层级有两种：`<tenant>/<user>/mdl_*`（A 股为主）与
    `<tenant>/<user>/<market>/mdl_*`（HK/US 等多市场），两种都要扫。
    """
    for root in MODELS_CANDIDATES:
        if not root.exists():
            continue
        model_dirs = sorted(root.glob("*/*/mdl_*")) + sorted(root.glob("*/*/*/mdl_*"))
        for model_dir in model_dirs:
            # 权重文件名随算法而变（model.pkl / model_mlp.pkl …），排除 pred_* 预测产物
            for pkl in sorted(model_dir.glob("*.pkl")):
                if not pkl.stem.lower().startswith("pred"):
                    yield pkl
        return


def test_real_artifacts_unreadable_by_strict_pickle_still_load(loader_mod):
    unreadable = []
    for pkl in _iter_real_pickles():
        try:
            with open(pkl, "rb") as f:
                pickle.load(f)
        except ValueError as exc:
            if "BitGenerator" in str(exc) or "MT19937" in str(exc):
                unreadable.append(pkl)

    if not unreadable:
        pytest.skip("当前模型目录里没有需要兼容层的 .pkl 产物")

    for pkl in unreadable:
        assert loader_mod._load_plain_pickle(pkl) is not None, f"{pkl} 兼容层读不回"
