"""Kronos 运行时适配：延迟导入官方仓库，避免污染主环境。

Kronos 仓库不随本项目分发。设置 ``KRONOS_HOME`` 或传 ``--kronos-home``
指向 clone 后的仓库根目录（其中包含 ``model/kronos.py``）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd


class KronosNotAvailable(RuntimeError):
    """Kronos 仓库或依赖不可用。"""


def resolve_kronos_home(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.getenv("KRONOS_HOME"),
        str(Path.home() / "Kronos"),
        "/data/kronos/Kronos",
    ]
    for item in candidates:
        if not item:
            continue
        home = Path(item).expanduser()
        if (home / "model" / "kronos.py").is_file():
            return home
    raise KronosNotAvailable(
        "Kronos repo not found. Clone https://github.com/shiyu-coder/Kronos and set "
        "KRONOS_HOME or pass --kronos-home."
    )


class KronosRunner:
    """加载 tokenizer + predictor，提供批量预测。"""

    def __init__(
        self,
        *,
        kronos_home: str | None = None,
        model_name: str = "NeoQuasar/Kronos-small",
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        device: str = "cpu",
        max_context: int = 512,
    ) -> None:
        home = resolve_kronos_home(kronos_home)
        if str(home) not in sys.path:
            sys.path.insert(0, str(home))
        try:
            from model import Kronos, KronosPredictor, KronosTokenizer
        except Exception as exc:  # noqa: BLE001
            raise KronosNotAvailable(
                f"failed to import Kronos from {home}: {exc}. "
                "Install its requirements (pip install -r requirements.txt)."
            ) from exc

        self.device = device
        self.max_context = max_context
        self.tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
        self.model = Kronos.from_pretrained(model_name)
        self.predictor = KronosPredictor(
            self.model, self.tokenizer, device=device, max_context=max_context
        )

    def predict_batch(
        self,
        frames: list[pd.DataFrame],
        x_timestamps: list[pd.Series],
        y_timestamps: list[pd.Series],
        *,
        pred_len: int,
        temperature: float = 0.6,
        top_p: float = 0.9,
        sample_count: int = 10,
    ) -> list[pd.DataFrame]:
        if not frames:
            return []
        return list(
            self.predictor.predict_batch(
                df_list=frames,
                x_timestamp_list=x_timestamps,
                y_timestamp_list=y_timestamps,
                pred_len=pred_len,
                T=temperature,
                top_p=top_p,
                sample_count=sample_count,
                verbose=False,
            )
        )
