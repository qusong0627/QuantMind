import lightgbm as lgb
import numpy as np
import pandas as pd
from qlib.model.base import Model
from qlib.data.dataset import DatasetH
from qlib.data.dataset.handler import DataHandlerLP


class LightGBMDirect(Model):
    def __init__(self, **kwargs):
        self.params = kwargs
        self.model = None

    @staticmethod
    def _sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
        def sanitize_name(name):
            return "".join(c if c.isalnum() or c == "_" else "_" for c in str(name))

        df.columns = [sanitize_name(c) for c in df.columns]
        return df

    def fit(self, dataset: DatasetH, **kwargs):
        df_train, df_valid = dataset.prepare(
            ["train", "valid"],
            col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        if df_train.empty or df_valid.empty:
            raise ValueError("Empty data for training or validation")

        x_train, y_train = df_train["feature"], df_train["label"]
        x_valid, y_valid = df_valid["feature"], df_valid["label"]

        x_train = self._sanitize_columns(x_train)
        x_valid = self._sanitize_columns(x_valid)

        y_train = np.squeeze(y_train.values)
        y_valid = np.squeeze(y_valid.values)

        dtrain = lgb.Dataset(x_train, label=y_train)
        dvalid = lgb.Dataset(x_valid, label=y_valid)

        early_stopping_rounds = self.params.pop("early_stopping_rounds", 100)
        num_boost_round = self.params.pop("num_boost_round", 2000)

        callbacks = [
            lgb.early_stopping(early_stopping_rounds),
            lgb.log_evaluation(period=100),
        ]

        self.model = lgb.train(
            self.params,
            dtrain,
            valid_sets=[dtrain, dvalid],
            valid_names=["train", "valid"],
            num_boost_round=num_boost_round,
            callbacks=callbacks,
            **kwargs,
        )

    def predict(self, dataset: DatasetH):
        if self.model is None:
            raise ValueError("model is not fitted yet")

        x_test = dataset.prepare("test", col_set="feature", data_key=DataHandlerLP.DK_I)
        x_test = self._sanitize_columns(x_test)
        predict_kwargs = {}
        num_threads = int(self.params.get("num_threads") or 0) if isinstance(self.params, dict) else 0
        if num_threads > 0:
            predict_kwargs["num_threads"] = num_threads
        return pd.Series(self.model.predict(x_test, **predict_kwargs), index=x_test.index)
