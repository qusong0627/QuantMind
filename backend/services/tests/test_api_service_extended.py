from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from backend.services.api.main import app

    # Mock lifespan to avoid DB connection issues during simple unit tests
    with patch("backend.services.api.main.lifespan", MagicMock()):
        with TestClient(app) as client:
            yield client


class TestAPIExtendedIntegration:
    """验证 Consolidated API 中各集成应用的响应性"""

    def test_admin_dashboard_integration(self, client):
        """验证管理后台 Dashboard 路由是否可达"""
        from backend.services.api.main import app
        from backend.services.api.user_app.middleware.auth import require_admin

        app.state.started_at = datetime.now(timezone.utc) - timedelta(days=2, hours=6)
        # 覆盖权限检查依赖
        app.dependency_overrides[require_admin] = lambda: {
            "id": "admin",
            "role": "admin",
        }

        async def _mock_get(self, url, *args, **kwargs):
            payload = {"status": "healthy", "service": url.rsplit("/", 1)[0].split("//", 1)[-1]}
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        with patch("httpx.AsyncClient.get", _mock_get):
            try:
                response = client.get("/api/v1/admin/dashboard/metrics")
                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                assert "users" in data["data"]
                assert data["data"]["system"]["health_score"] == 100
                assert data["data"]["system"]["uptime_days"] >= 2
                assert data["data"]["system"]["status"] == "healthy"
                assert len(data["data"]["system"]["services"]) == 4
            finally:
                # 清理覆盖，避免影响其他测试
                app.dependency_overrides.clear()

    def test_admin_models_data_status_integration(self, client):
        """验证管理端数据状态路由可达并返回结构化字段"""
        from backend.services.api.main import app
        from backend.services.api.user_app.middleware.auth import require_admin

        app.dependency_overrides[require_admin] = lambda: {
            "id": "admin",
            "role": "admin",
        }

        try:
            response = client.get("/api/v1/admin/models/data-status")
            assert response.status_code == 200
            payload = response.json()
            assert "checked_at" in payload
            assert "trade_date" in payload
            assert "qlib_data" in payload
            assert "market_data_daily" in payload
            assert "latest_date_coverage" in payload["qlib_data"]
        finally:
            app.dependency_overrides.clear()

    def test_stock_query_integration(self, client):
        """验证选股查询路由集成"""
        # Mock StockSearchService
        mock_results = [{"symbol": "000001.SZ", "name": "平安银行"}]
        with patch("backend.services.api.stock_query_app.routes.StockSearchService") as MockSearch:
            instance = MockSearch.return_value
            instance.search_stocks = AsyncMock(return_value=mock_results)

            response = client.get("/api/v1/stocks/search?q=平安")
            assert response.status_code == 200
            data = response.json()
            assert "results" in data
            assert data["results"] == mock_results

    def test_stock_detail_integration(self, client):
        """验证股票详情路由集成"""
        mock_info = {"symbol": "000001.SZ", "name": "平安银行", "price": 10.5}
        with patch("backend.services.api.stock_query_app.routes.StockQueryService") as MockQuery:
            instance = MockQuery.return_value
            instance.get_stock_info = AsyncMock(return_value=mock_info)

            response = client.get("/api/v1/stocks/000001.SZ")
            assert response.status_code == 200
            assert response.json()["symbol"] == "000001.SZ"

    def test_research_overview_integration(self, client):
        """验证投研聚合路由返回模型、批次和候选结构"""
        from backend.services.api.main import app
        from backend.services.api.routers import research as research_router_module
        from backend.services.api.user_app.middleware.auth import get_current_user

        class _FakeMappingsResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def first(self):
                return self._rows[0] if self._rows else None

        class _FakeScalarResult:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _FakeResult:
            def __init__(self, rows, *, mapping=True):
                self._rows = rows
                self._mapping = mapping

            def mappings(self):
                return _FakeMappingsResult(self._rows)

            def fetchall(self):
                return self._rows

        class _FakeSession:
            overview_sql = ""

            async def execute(self, statement, params=None):
                sql = str(statement)
                if "GROUP BY model_id" in sql:
                    return _FakeResult(
                        [
                            {
                                "model_id": "alpha158",
                                "run_count": 2,
                                "latest_prediction_trade_date": datetime(2026, 4, 27, tzinfo=timezone.utc).date(),
                                "last_updated_at": datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
                            }
                        ]
                    )
                if "GROUP BY run_id, model_id" in sql:
                    return _FakeResult(
                        [
                            {
                                "run_id": "run_demo",
                                "model_id": "alpha158",
                                "inference_date": datetime(2026, 4, 24, tzinfo=timezone.utc).date(),
                                "prediction_trade_date": datetime(2026, 4, 27, tzinfo=timezone.utc).date(),
                                "universe_label": "沪深全市场 · 生产批次",
                                "stock_count": 3,
                                "avg_score": 0.71,
                                "last_updated_at": datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
                            }
                        ]
                    )
                if "COUNT(*) AS total_count" in sql:
                    return _FakeResult(
                        [
                            {
                                "total_count": 3,
                                "avg_score": 0.71,
                                "high_confidence_count": 1,
                                "strong_count": 1,
                                "last_updated_at": datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
                            }
                        ]
                    )
                if "GROUP BY industry" in sql:
                    return _FakeResult([("银行",), ("券商",)], mapping=False)
                if "jsonb_array_elements_text" in sql:
                    return _FakeResult([("金融科技",), ("中字头",)], mapping=False)
                if "WITH snap_page AS" in sql:
                    self.overview_sql = sql
                return _FakeResult(
                    [
                        {
                            "run_id": "run_demo",
                            "model_id": "alpha158",
                            "symbol": "000001",
                            "stock_name": "平安银行",
                            "score_rank": 1,
                            "fusion_score": 0.88,
                            "signal_side": "BUY",
                            "latest_change_pct": 2.36,
                            "consecutive_limit_up_days": 2,
                            "volume_trend_3d": True,
                            "volume_trend_5d": True,
                            "turnover_rate": 4.2,
                            "amount": 123456789.0,
                            "total_mv": 2500000.0,
                            "industry": "银行",
                            "concept_tags": ["金融科技", "中字头"],
                            "confidence_level": "high",
                            "hit_reasons": ["模型高分", "3日量能递增"],
                            "risk_flags": ["近10日回撤较大"],
                            "close_price": 12.34,
                            "return_1d": 0.0236,
                            "return_3d": 0.0512,
                            "thesis_summary": "适合作为投研观察样本",
                            "updated_at": datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
                        }
                    ]
                )

        session_ref = {"value": None}

        @asynccontextmanager
        async def _fake_get_session(read_only=True):
            session = _FakeSession()
            session_ref["value"] = session
            yield session

        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "u1",
            "tenant_id": "default",
            "is_admin": False,
        }

        with patch.object(research_router_module, "get_session", _fake_get_session):
            try:
                response = client.get("/api/v1/research/overview")
                assert response.status_code == 200
                payload = response.json()["data"]
                assert payload["activeModelId"] == "alpha158"
                assert payload["activeRunId"] == "run_demo"
                assert len(payload["models"]) == 1
                assert len(payload["runs"]) == 1
                assert payload["summary"]["total"] == 3
                assert payload["filters"]["sectors"] == ["银行", "券商"]
                assert payload["filters"]["concepts"] == ["金融科技", "中字头"]
                assert payload["items"][0]["code"] == "000001"
                assert payload["items"][0]["concept"] == "金融科技 / 中字头"
                assert payload["items"][0]["nextDayReturn"] == pytest.approx(2.36)
                assert payload["items"][0]["day3Return"] == pytest.approx(5.12)
                assert payload["pagination"]["limit"] == 80
                assert payload["pagination"]["offset"] == 0
                assert payload["pagination"]["returned"] == 1
                assert payload["pagination"]["total"] == 3
                assert payload["pagination"]["hasMore"] is True
                assert "LEAD(sdl.close, 1)" in session_ref["value"].overview_sql
                assert "sdl_run.trade_date = snap.data_trade_date" in session_ref["value"].overview_sql
            finally:
                app.dependency_overrides.clear()

    def test_community_integration(self, client):
        """验证社区路由集成"""
        response = client.get("/api/v1/community/posts?page=1&pageSize=10")
        # 即使没有数据或没权限，也不应该是 404
        assert response.status_code != 404

    def test_notifications_integration(self, client):
        """验证通知路由集成"""
        response = client.get("/api/v1/notifications")
        assert response.status_code != 404

    def test_model_training_user_feature_catalog_integration(self, client):
        """验证用户态模型训练特征字典路由可达"""
        from backend.services.api.main import app
        from backend.services.api.user_app.middleware.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "u1",
            "tenant_id": "t1",
            "is_admin": False,
        }

        with patch("backend.services.api.routers.model_training._load_feature_catalog_from_db", AsyncMock(return_value=None)):
            with patch(
                "backend.services.api.routers.model_training._load_feature_catalog_from_file",
                MagicMock(return_value={"version_id": "v1", "version_name": "v1", "feature_count": 1, "categories": [], "source": "file"}),
            ):
                with patch(
                    "backend.services.api.routers.model_training._enrich_feature_catalog_with_data_coverage",
                    MagicMock(side_effect=lambda payload: payload),
                ):
                    try:
                        response = client.get("/api/v1/models/feature-catalog")
                        assert response.status_code == 200
                        data = response.json()
                        assert data["version_id"] == "v1"
                    finally:
                        app.dependency_overrides.clear()

    def test_model_training_user_run_and_status_integration(self, client):
        """验证用户态模型训练启动/状态接口可达并返回结构化数据"""
        from backend.services.api.main import app
        from backend.services.api.user_app.middleware.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "u1",
            "tenant_id": "t1",
            "is_admin": False,
        }

        with patch(
            "backend.services.api.routers.model_training.submit_training_job",
            AsyncMock(return_value={"runId": "r1", "status": "pending", "payload": {"foo": "bar"}}),
        ):
            with patch(
                "backend.services.api.routers.model_training.get_training_run_for_owner",
                AsyncMock(return_value={"runId": "r1", "status": "running", "progress": 30, "logs": "", "result": {}, "isCompleted": False}),
            ):
                try:
                    response = client.post("/api/v1/models/run-training", json={"features": [], "lgb_params": {}})
                    assert response.status_code == 200
                    assert response.json()["runId"] == "r1"

                    status_resp = client.get("/api/v1/models/training-runs/r1")
                    assert status_resp.status_code == 200
                    assert status_resp.json()["status"] == "running"
                finally:
                    app.dependency_overrides.clear()

    def test_model_training_admin_alias_route_integration(self, client):
        """验证 admin 兼容别名路由仍可访问"""
        from backend.services.api.main import app
        from backend.services.api.user_app.middleware.auth import require_admin

        app.dependency_overrides[require_admin] = lambda: {
            "user_id": "admin",
            "tenant_id": "default",
            "is_admin": True,
        }

        with patch(
            "backend.services.api.routers.admin.admin_training.submit_training_job",
            AsyncMock(return_value={"runId": "admin_r1", "status": "pending", "payload": {}}),
        ):
            try:
                response = client.post("/api/v1/admin/models/run-training", json={"features": [], "lgb_params": {}})
                assert response.status_code == 200
                assert response.json()["runId"] == "admin_r1"
            finally:
                app.dependency_overrides.clear()
