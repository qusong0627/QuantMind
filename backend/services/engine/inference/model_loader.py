"""
Model Loader - 支持多框架模型加载 (LightGBM, PyTorch/TFT, ONNX)
"""

import hashlib
import json
import logging
import pickle
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 模型目录里与权重同放的训练产物：它们不是模型，盲搜扩展名时必须排除。
# 训练端 metadata 记的名字（model_gru.pth）与产物落盘名（model.pth）曾对不上，
# 兜底 glob 会把 pred.pkl（预测结果 DataFrame）当成 sklearn 模型加载，
# 直到调用 predict() 才崩 —— 见同目录 templates/inference_parquet.py 的同类修复。
_ARTIFACT_STEMS = frozenset({
    "pred", "result", "metadata", "config", "inference",
    "shap_summary", "feature_importance", "training_log",
})


def _is_model_weight(path: Path) -> bool:
    """该文件是否可能是模型权重（排除预测产物、配置脚本等训练副产品）。"""
    stem = path.stem.lower()
    return stem not in _ARTIFACT_STEMS and not stem.startswith("pred_")


def _load_plain_pickle(path: Path) -> Any:
    """读普通 pickle 产物（sklearn MLP / Ridge 等），容忍新式 numpy 的 RNG state。

    训练端 numpy(>=2) 与推理环境 numpy(1.26) 对 RandomState 的 pickle 口径不同：
    新版把 BitGenerator **类对象**传给 `__bit_generator_ctor`（旧版只认类名字符串），
    `__randomstate_ctor` 收到的也是实例，且 state 的 key 是 ndarray；旧版 cython
    校验直接抛 `... is not a known BitGenerator module.` 或
    `state is not a legacy MT19937 state`。该 state 只在 fit/sample 抽样时用到，
    **推理不抽样** → 丢弃它是安全的（对象退化为默认确定性的 RandomState，
    权重复原不受影响）。与 `templates/inference_parquet.py::_load_plain_pickle`
    同口径（模板自包含、不 import 本模块，故两处各留一份）。
    """
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except ValueError as exc:
        if "BitGenerator" not in str(exc) and "MT19937" not in str(exc):
            raise
        logger.warning("模型 pickle 含本环境读不了的新式 RNG state，按兼容模式加载: %s", exc)
        return _load_pickle_with_rng_state_dropped(path)


class _NumpyCompatUnpickler(pickle._Unpickler):  # type: ignore[attr-defined]
    """兼容洗牌：折算新旧 numpy 的 RandomState 构造口径，读不了的 state 丢弃。

    必须用纯 Python 的 `pickle._Unpickler` 并显式改写 dispatch 表：子类只重写
    `load_build` 不会生效——dispatch 里存的是函数对象，不走实例属性查找
    （`find_class` 走属性查找，所以只重写它才有效）。
    """

    def find_class(self, module: str, name: str) -> Any:
        resolved = super().find_class(module, name)
        if module == "numpy.random._pickle" and name == "__bit_generator_ctor":
            def _ctor(bit_generator: Any = "MT19937", *args: Any, **kwargs: Any) -> Any:
                if isinstance(bit_generator, type):  # numpy>=2 传的是类对象
                    bit_generator = bit_generator.__name__
                return resolved(bit_generator, *args, **kwargs)
            return _ctor
        if module == "numpy.random._pickle" and name == "__randomstate_ctor":
            def _rs_ctor(bit_generator: Any = "MT19937", *args: Any, **kwargs: Any) -> Any:
                import numpy as np

                if isinstance(bit_generator, np.random.BitGenerator):  # numpy>=2 传实例
                    return np.random.RandomState(bit_generator)
                return resolved(bit_generator, *args, **kwargs)
            return _rs_ctor
        return resolved

    def load_build(self) -> None:
        try:
            return super().load_build()
        except ValueError as exc:
            if "MT19937" not in str(exc) and "BitGenerator" not in str(exc):
                raise
            # state 已被 super() 弹出，直接返回 = 丢弃该 state（见上方说明）
            return


_NumpyCompatUnpickler.dispatch = pickle._Unpickler.dispatch.copy()  # type: ignore[attr-defined]
_NumpyCompatUnpickler.dispatch[pickle.BUILD[0]] = _NumpyCompatUnpickler.load_build


def _load_pickle_with_rng_state_dropped(path: Path) -> Any:
    with open(path, "rb") as f:
        return _NumpyCompatUnpickler(f).load()

# 默认最大缓存模型数
DEFAULT_MAX_MODELS = 5


class EnsembleBooster:
    """多个 LightGBM Boosters 的集成包装器"""

    def __init__(self, boosters: list):
        self.boosters = boosters

    def predict(self, data, **kwargs):
        import numpy as np

        preds = [b.predict(data, **kwargs) for b in self.boosters]
        return np.mean(preds, axis=0)

    def feature_name(self):
        return self.boosters[0].feature_name()


class StackingEnsemble:
    """Stacking 集成推理：多基模型预测 → 元学习器融合"""

    def __init__(self, base_models: dict[str, Any], meta_model: Any, model_types: list[str],
                 fill_values: dict[str, dict], features: list[str]):
        self.base_models = base_models
        self.meta_model = meta_model
        self.model_types = model_types
        self.fill_values = fill_values
        self.features = features

    def predict(self, X: np.ndarray, **kwargs) -> np.ndarray:
        """对特征矩阵执行 Stacking 推理：各基模型预测 → 拼接 → 元学习器输出"""
        base_preds = []
        for mt in self.model_types:
            model = self.base_models[mt]
            fv = self.fill_values.get(mt, {})
            if mt == "lightgbm":
                pred = model.predict(X, **kwargs)
            elif mt == "xgboost":
                import xgboost as xgb
                pred = model.predict(xgb.DMatrix(X))
            elif mt == "catboost":
                from catboost import Pool
                pred = model.predict(Pool(X))[0].flatten()
            else:
                pred = model.predict(X).flatten()
            base_preds.append(pred)

        meta_X = np.column_stack(base_preds)
        return self.meta_model.predict(meta_X)

    def feature_name(self):
        return self.features


class ModelLoader:
    """多框架模型管理器"""

    def __init__(self, production_dir: Path, max_models: int = DEFAULT_MAX_MODELS):
        self.production_dir = production_dir
        self.max_models = max_models
        self._loaded_models: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _build_cache_key(model_id: str, cache_key: str | None) -> str:
        return str(cache_key or model_id)

    def load_model(self, model_id: str, *, model_dir: Path | None = None, cache_key: str | None = None) -> Any:
        """加载模型，自动识别框架"""
        cache_id = self._build_cache_key(model_id, cache_key)
        with self._lock:
            if cache_id in self._loaded_models:
                self._loaded_models.move_to_end(cache_id)
                return self._loaded_models[cache_id]

        resolved_model_dir = model_dir or self._resolve_model_dir(model_id)
        if not resolved_model_dir.exists():
            raise FileNotFoundError(f"Model directory {model_id} not found at {self.production_dir}")

        # 读取元数据以确定框架
        metadata = self._get_metadata(resolved_model_dir)
        framework = metadata.get("framework", "lightgbm").lower()

        try:
            if framework == "lightgbm":
                model = self._load_lightgbm(resolved_model_dir, metadata)
            elif framework == "xgboost":
                model = self._load_xgboost(resolved_model_dir, metadata)
            elif framework == "catboost":
                model = self._load_catboost(resolved_model_dir, metadata)
            elif framework == "sklearn":
                model = self._load_sklearn(resolved_model_dir, metadata)
            elif framework == "pytorch" or framework == "tft":
                model = self._load_pytorch(resolved_model_dir, metadata)
            elif framework == "onnx":
                model = self._load_onnx(resolved_model_dir, metadata)
            else:
                raise ValueError(f"Unsupported framework: {framework}")

            # Stacking 集成：加载基模型 + 元学习器
            if metadata.get("is_ensemble") and metadata.get("ensemble_method") == "stacking":
                model = self._load_stacking(resolved_model_dir, metadata, model)

            with self._lock:
                self._evict_lru()
                self._loaded_models[cache_id] = model

            logger.info(f"✅ Successfully loaded {framework} model: {model_id}")
            return model
        except Exception as e:
            logger.error(f"❌ Failed to load model {model_id} ({framework}): {e}")
            raise

    def _load_lightgbm(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 LightGBM 模型 (支持单模型和集成)"""
        import lightgbm as lgb

        is_ensemble = metadata.get("is_ensemble", False) or any(model_dir.glob("seed_*.txt"))

        if is_ensemble:
            seed_files = sorted(list(model_dir.glob("seed_*.txt")))
            if not seed_files:
                # 兼容性检查：如果 metadata 说集成但没 seed 文件，看有没有 model.txt
                main_file = model_dir / "model.txt"
                if main_file.exists():
                    seed_files = [main_file]

            if not seed_files:
                raise FileNotFoundError("Ensemble seed files missing")
            boosters = [lgb.Booster(model_file=str(f)) for f in seed_files]
            return EnsembleBooster(boosters)
        else:
            model_file = model_dir / metadata.get("model_file", "model.lgb")
            if not model_file.exists():
                # 优先找 .lgb（训练流水线产物），再 .txt，最后 .pkl
                candidates = (
                    list(model_dir.glob("*.lgb"))
                    + list(model_dir.glob("*.txt"))
                    + list(model_dir.glob("*.pkl"))
                )
                if not candidates:
                    raise FileNotFoundError(f"No LightGBM model file found in {model_dir}")
                model_file = candidates[0]

            if model_file.suffix == ".pkl":
                with open(model_file, "rb") as f:
                    return pickle.load(f)
            return lgb.Booster(model_file=str(model_file))

    def _load_xgboost(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 XGBoost 模型"""
        import xgboost as xgb
        model_file = model_dir / metadata.get("model_file", "model.xgb")
        if not model_file.exists():
            candidates = list(model_dir.glob("*.xgb"))
            if not candidates:
                raise FileNotFoundError(f"No XGBoost model file found in {model_dir}")
            model_file = candidates[0]
        model = xgb.Booster()
        model.load_model(str(model_file))
        return model

    def _load_catboost(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 CatBoost 模型"""
        from catboost import CatBoost
        model_file = model_dir / metadata.get("model_file", "model.cbm")
        if not model_file.exists():
            candidates = list(model_dir.glob("*.cbm"))
            if not candidates:
                raise FileNotFoundError(f"No CatBoost model file found in {model_dir}")
            model_file = candidates[0]
        model = CatBoost()
        model.load_model(str(model_file), format="cbm")
        return model

    def _load_sklearn(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 sklearn 模型 (pickle)"""
        model_file = model_dir / metadata.get("model_file", "model.pkl")
        if not model_file.exists():
            candidates = [p for p in sorted(model_dir.glob("*.pkl")) if _is_model_weight(p)]
            if not candidates:
                raise FileNotFoundError(f"No sklearn model file found in {model_dir}")
            model_file = candidates[0]
        return _load_plain_pickle(model_file)

    def _load_pytorch(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 PyTorch / TFT / Qlib DL 模型"""
        try:
            import torch
        except ImportError:
            logger.error("torch is required for pytorch models. Please install it.")
            raise

        model_file = model_dir / metadata.get("model_file", "model.pth")
        if not model_file.exists():
            candidates = list(model_dir.glob("*.pth")) + list(model_dir.glob("*.pt"))
            if not candidates:
                raise FileNotFoundError(f"No PyTorch model file found in {model_dir}")
            model_file = candidates[0]

        # Qlib DL 模型: 通过 model_class_name + model_params 重建
        model_class_name = metadata.get("model_class_name")
        if model_class_name:
            return self._load_qlib_dl_model(model_file, metadata, model_class_name)

        # 针对 TFT (Temporal Fusion Transformer) 的特殊处理
        model_type = str(metadata.get("model_type", ""))
        if model_type == "TFT":
            try:
                from pytorch_forecasting import TemporalFusionTransformer

                # 注意：TFT 通常使用 load_from_checkpoint
                return TemporalFusionTransformer.load_from_checkpoint(str(model_file))
            except ImportError:
                logger.warning("pytorch_forecasting not installed, loading as raw torch model")

        if "nativetft" in model_type.lower():
            from .native_tft_model import load_native_tft_state_dict

            return load_native_tft_state_dict(str(model_file), metadata)

        # mlp 等「非 Qlib 类」的落盘对象：训练端 `train.py::_save_model` 对 mlp/linear
        # 走 `pickle.dump`（普通 pickle，**不是** torch 序列化）。weights_only=False 是
        # PyTorch>=2.6 读整体对象的前提（默认 True 直接拒读）。
        try:
            return torch.load(str(model_file), map_location="cpu", weights_only=False)
        except (RuntimeError, ValueError) as exc:
            # torch.load 误读普通 pickle 的两种表现：
            #  - 反序列化成功、比对 magic number 失败 → "Invalid magic number; corrupt file?"
            #  - 含新式 numpy RNG state → 反序列化中途报 BitGenerator 相关 ValueError
            message = str(exc)
            if (
                "Invalid magic number" not in message
                and "BitGenerator" not in message
                and "MT19937" not in message
            ):
                raise
            logger.info(
                "%-30s 不是 torch 序列化产物，改按普通 pickle 加载", model_file.name
            )
            return _load_plain_pickle(model_file)

    @staticmethod
    def _load_qlib_dl_model(model_file: Path, metadata: dict[str, Any], model_class_name: str) -> Any:
        """加载 Qlib DL 模型: 从 state_dict + model_params 重建模型。"""
        import importlib
        import torch

        # 键必须与训练端写进 metadata 的 model_class_name 一致
        # （`backend/shared/model_algorithm_meta.ALGO_CLASS_NAMES`）；
        # 曾用 "Transformer"/"TabNet" 短名，与写入值 "TransformerModel"/"TabnetModel"
        # 对不上 → ValueError: Unknown Qlib model class。与推理模板
        # `inference/templates/inference_parquet.py::_QLIB_DL_MAP` 保持同表。
        _QLIB_MODEL_MAP = {
            "GRU":              ("qlib.contrib.model.pytorch_gru_ts",         "GRU"),
            "LSTM":             ("qlib.contrib.model.pytorch_lstm_ts",        "LSTM"),
            "ALSTM":            ("qlib.contrib.model.pytorch_alstm_ts",       "ALSTM"),
            "TransformerModel": ("qlib.contrib.model.pytorch_transformer_ts", "TransformerModel"),
            "TCN":              ("qlib.contrib.model.pytorch_tcn_ts",         "TCN"),
            "TabnetModel":      ("qlib.contrib.model.pytorch_tabnet",         "TabnetModel"),
        }

        if model_class_name not in _QLIB_MODEL_MAP:
            raise ValueError(f"Unknown Qlib model class: {model_class_name}")

        mod_path, cls_name = _QLIB_MODEL_MAP[model_class_name]
        mod = importlib.import_module(mod_path)
        ModelCls = getattr(mod, cls_name)

        model_params = dict(metadata.get("model_params", {}))
        model_params["GPU"] = -1  # CPU for inference

        model_obj = ModelCls(**model_params)

        state_dict = torch.load(str(model_file), map_location="cpu")
        # 找到内部 PyTorch 模型并加载权重
        inner_model = getattr(model_obj, "model", None)
        if inner_model is None:
            for attr_name in ("gru_model", "lstm_model", "alstm_model", "transformer_model", "tcn_model", "tabnet_model"):
                inner_model = getattr(model_obj, attr_name, None)
                if inner_model is not None:
                    break
        if inner_model is not None and state_dict is not None:
            inner_model.load_state_dict(state_dict)
            inner_model.eval()
        model_obj.fitted = True
        logger.info("Loaded Qlib DL model: %s from %s", model_class_name, model_file)
        return model_obj

    def _load_onnx(self, model_dir: Path, metadata: dict[str, Any]) -> Any:
        """加载 ONNX 模型 (预留)"""
        import onnxruntime as ort

        model_file = model_dir / metadata.get("model_file", "model.onnx")
        return ort.InferenceSession(str(model_file))

    def _load_stacking(self, model_dir: Path, metadata: dict[str, Any], primary_model: Any) -> StackingEnsemble:
        """加载 Stacking 集成：基模型 + Ridge 元学习器"""
        model_types = metadata.get("model_types", [])
        saved_models = metadata.get("saved_models", {})
        fill_values = metadata.get("fill_values", {})
        features = metadata.get("feature_columns") or metadata.get("features", [])

        # 加载各基模型
        base_models: dict[str, Any] = {}
        for mt in model_types:
            model_file = saved_models.get(mt, "")
            if not model_file:
                continue
            model_path = model_dir / model_file
            if not model_path.exists():
                logger.warning("Stacking base model file missing: %s", model_path)
                continue
            if mt == "lightgbm":
                import lightgbm as lgb
                base_models[mt] = lgb.Booster(model_file=str(model_path))
            elif mt == "xgboost":
                import xgboost as xgb
                booster = xgb.Booster()
                booster.load_model(str(model_path))
                base_models[mt] = booster
            elif mt == "catboost":
                from catboost import CatBoost
                cb = CatBoost()
                cb.load_model(str(model_path), format="cbm")
                base_models[mt] = cb

        # 加载元学习器
        meta_model_path = model_dir / metadata.get("meta_model_file", "meta_model.pkl")
        if not meta_model_path.exists():
            raise FileNotFoundError(f"Stacking meta_model not found: {meta_model_path}")
        with open(meta_model_path, "rb") as f:
            meta_data = pickle.load(f)
        meta_model = meta_data["model"] if isinstance(meta_data, dict) else meta_data

        # 基模型填充值 — 优先使用 per-model fill_values，回退到全局 fill_values
        base_fv = metadata.get("base_model_fill_values", {})
        global_fv = metadata.get("fill_values", {})
        base_fill_values: dict[str, dict] = {}
        for mt in model_types:
            per_model_fv = base_fv.get(mt, {}) if isinstance(base_fv, dict) else {}
            base_fill_values[mt] = per_model_fv if per_model_fv else (global_fv if isinstance(global_fv, dict) else {})

        logger.info("Loaded Stacking ensemble: %d base models + Ridge meta-learner", len(base_models))
        return StackingEnsemble(
            base_models=base_models,
            meta_model=meta_model,
            model_types=model_types,
            fill_values=base_fill_values,
            features=features,
        )

    def _get_metadata(self, model_dir: Path) -> dict[str, Any]:
        """获取元数据，不存在则返回空字典"""
        merged: dict[str, Any] = {}
        meta_path = model_dir / "metadata.json"
        if meta_path.exists():
            with open(meta_path) as f:
                merged.update(json.load(f))

        # Optional runtime metadata (non-breaking extension).
        inf_meta_path = model_dir / "inference_metadata.json"
        if inf_meta_path.exists():
            with open(inf_meta_path) as f:
                inf_meta = json.load(f)
            for k, v in inf_meta.items():
                if k not in merged:
                    merged[k] = v

        return merged

    def _resolve_model_dir(self, model_id: str) -> Path:
        """支持版本解析 (e.g. model_v2)"""
        exact = self.production_dir / model_id
        if exact.exists():
            return exact
        candidates = sorted(self.production_dir.glob(f"{model_id}_v*"), key=lambda p: p.name, reverse=True)
        return candidates[0] if candidates else exact

    def _evict_lru(self):
        """LRU 淘汰"""
        while len(self._loaded_models) >= self.max_models:
            self._loaded_models.popitem(last=False)

    def get_model(self, model_id: str, *, cache_key: str | None = None) -> Any | None:
        cache_id = self._build_cache_key(model_id, cache_key)
        with self._lock:
            if cache_id in self._loaded_models:
                self._loaded_models.move_to_end(cache_id)
                return self._loaded_models[cache_id]
        return None

    def get_model_metadata(self, model_id: str, *, model_dir: Path | None = None) -> dict[str, Any] | None:
        resolved_model_dir = model_dir or self._resolve_model_dir(model_id)
        return self._get_metadata(resolved_model_dir) or None

    @property
    def loaded_models(self) -> list[str]:
        return list(self._loaded_models.keys())
