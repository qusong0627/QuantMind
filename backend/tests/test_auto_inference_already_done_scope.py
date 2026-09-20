"""auto_inference_if_needed 的「当日已完成」探测 SQL 必须带模型维度。

背景（实测 2026-09）：`qm_model_inference_dispatch_logs` 里凡是
`qm_model_inference_settings` 开了两个以上模型的交易日，记录永远是

    ... mdl_A success  celery_auto_inference_if_needed
    ... mdl_B skipped  ALREADY_DONE

因为探测只按 (trade_date, tenant_id, user_id) 查 `engine_feature_runs`，
第一个模型跑成后同用户其余模型全被误判为「今天已经出过信号了」。

而落库侧本来就是按模型分桶并存的：`InferenceScriptRunner._persist_and_publish`
以 `feature_version = script_v1_<模型桶>` 分别覆盖写入，同一天不同模型互不干扰。
两处口径必须一致，否则被启用/被设为默认的模型会静默地永远轮不到。
"""

from __future__ import annotations

import pytest

from backend.services.engine.tasks.celery_tasks import _already_done_probe_sql


def _where_clause(sql: str) -> str:
    return sql.split("WHERE", 1)[1]


class TestAlreadyDoneProbeSql:
    def test_scopes_to_model_when_model_known(self):
        # Arrange / Act
        sql = _already_done_probe_sql("mdl_cn_train_20260919_lightgbm_deadbeef")

        # Assert
        assert "model_name = :mid" in _where_clause(sql)

    def test_keeps_date_and_owner_filters(self):
        sql = _already_done_probe_sql("mdl_x")

        where = _where_clause(sql)
        assert "trade_date = :d" in where
        assert "status = 'signal_ready'" in where
        assert "tenant_id = :tid" in where
        assert "user_id = :uid" in where

    def test_without_model_falls_back_to_owner_wide_scope(self):
        # 解析不出模型时宁可少跑（沿用旧口径），也不能退化成「谁都算已完成」
        sql = _already_done_probe_sql(None)

        assert "model_name" not in sql
        assert "tenant_id = :tid" in sql

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_model_id_is_treated_as_unknown(self, empty):
        assert "model_name" not in _already_done_probe_sql(empty)

    def test_model_id_is_bound_not_interpolated(self):
        # 模型 id 是绑定参数而非拼进 SQL：既避免注入，也让同一条探测语句服务所有模型
        sql = _already_done_probe_sql("mdl_cn_train_20260919_x")

        assert ":mid" in sql
        assert "mdl_cn_train_20260919_x" not in sql
