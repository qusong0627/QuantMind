"""
StrategyStorageService 单元测试
测试 PG + COS 统一存储服务的核心功能（使用 mock 隔离外部依赖）
"""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


class TestStrategyStorageService(unittest.IsolatedAsyncioTestCase):
    """strategy_storage.StrategyStorageService 单元测试"""

    def _make_service(self, cos_available: bool = True):
        """构造注入了 mock 依赖的 StrategyStorageService 实例。"""
        from backend.shared.strategy_storage import StrategyStorageService

        svc = StrategyStorageService.__new__(StrategyStorageService)
        svc._has_cos_key_col = True  # 跳过 __init__：补列探测缓存（现行实现会读它）
        if cos_available:
            mock_cos = MagicMock()
            mock_cos.client = MagicMock()
            mock_cos.bucket_name = "test-bucket"
            mock_cos.base_url = "https://test-bucket.cos.ap-beijing.myqcloud.com"
            mock_cos.upload_file.return_value = {
                "success": True,
                "url": "https://test.cos/test.py",
            }
            mock_cos.get_presigned_url.return_value = "https://test.cos/test.py?sign=xxx"
            svc._cos = mock_cos
        else:
            svc._cos = None
        return svc

    # ------------------------------------------------------------------
    # _code_hash
    # ------------------------------------------------------------------

    def test_code_hash_deterministic(self):
        from backend.shared.strategy_storage import _code_hash

        code = "print('hello')"
        self.assertEqual(_code_hash(code), _code_hash(code))
        self.assertNotEqual(_code_hash(code), _code_hash("print('world')"))

    # ------------------------------------------------------------------
    # _make_cos_key
    # ------------------------------------------------------------------

    def test_make_cos_key_format(self):
        from backend.shared.strategy_storage import _make_cos_key

        key = _make_cos_key("user123", "strat-abc")
        self.assertTrue(key.startswith("user_strategies/user123/"))
        self.assertTrue(key.endswith(".py"))

    # ------------------------------------------------------------------
    # _parse_tags
    # ------------------------------------------------------------------

    def test_parse_tags_list(self):
        from backend.shared.strategy_storage import _parse_tags

        self.assertEqual(_parse_tags(["AI", "qlib"]), ["AI", "qlib"])

    def test_parse_tags_pg_array_string(self):
        from backend.shared.strategy_storage import _parse_tags

        self.assertEqual(_parse_tags("{AI,qlib}"), ["AI", "qlib"])

    def test_parse_tags_json_string(self):
        from backend.shared.strategy_storage import _parse_tags

        self.assertEqual(_parse_tags('["AI","qlib"]'), ["AI", "qlib"])

    def test_parse_tags_none(self):
        from backend.shared.strategy_storage import _parse_tags

        self.assertEqual(_parse_tags(None), [])

    # ------------------------------------------------------------------
    # _local_mode property
    # ------------------------------------------------------------------

    def test_local_mode_when_cos_none(self):
        svc = self._make_service(cos_available=False)
        self.assertTrue(svc._local_mode)

    def test_local_mode_false_when_cos_available(self):
        svc = self._make_service(cos_available=True)
        self.assertFalse(svc._local_mode)

    # ------------------------------------------------------------------
    # save()
    # ------------------------------------------------------------------

    async def test_save_calls_cos_and_db_returns_id(self):
        svc = self._make_service(cos_available=True)

        def fake_upsert(*args, **kw):
            # 位置：user_id, strategy_id, name, code, ...（实现按位置调用）
            assert args[3] == "import qlib\nprint('hello')"
            return "42"

        svc._db_upsert = fake_upsert

        with patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1):
            result = await svc.save(
                user_id="1",
                name="Test Strategy",
                code="import qlib\nprint('hello')",
                metadata={"tags": ["AI"]},
            )

        self.assertEqual(result["id"], "42")
        self.assertIn("cos_url", result)
        self.assertIn("cos_key", result)
        self.assertIn("code_hash", result)
        self.assertIsNotNone(result["code_hash"])

    async def test_save_without_cos_still_writes_db(self):
        """COS 不可用时，save 应仍然写入 PG（code 字段保存代码）。"""
        svc = self._make_service(cos_available=False)

        def fake_upsert(*args, **kw):
            # 确认 code 非空（位置参数第 4 位）
            self.assertIsNotNone(args[3])
            return "99"

        svc._db_upsert = fake_upsert

        with patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1):
            result = await svc.save(
                user_id="1",
                name="Local Strategy",
                code="print('fallback')",
            )

        self.assertEqual(result["id"], "99")
        self.assertIsNone(result["cos_url"])

    # ------------------------------------------------------------------
    # list()
    # ------------------------------------------------------------------

    def test_list_returns_cos_url_from_presign(self):
        svc = self._make_service(cos_available=True)

        fake_row = (
            1,  # id
            "My Strategy",  # name
            "desc",  # description
            "ACTIVE",  # status（存量词表，list 原样返回）
            "https://old.cos/key.py",  # cos_url
            "user_strategies/1/2024/01/abc.py",  # cos_key
            "deadbeef",  # code_hash
            '["AI"]',  # tags
            False,  # is_verified
            '{}',  # execution_config
            datetime(2024, 1, 1, tzinfo=timezone.utc),  # created_at
            datetime(2024, 1, 2, tzinfo=timezone.utc),  # updated_at
            '{}',  # parameters
            '{}',  # config
        )

        mock_session = MagicMock()
        mock_session.execute.return_value.fetchall.return_value = [fake_row]

        from contextlib import contextmanager

        @contextmanager
        def fake_db_ctx():
            yield mock_session

        with (
            patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1),
            patch("backend.shared.strategy_storage.get_db", new=fake_db_ctx),
        ):
            results = svc.list(user_id="1")

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item["id"], "1")
        self.assertEqual(item["name"], "My Strategy")
        # cos_url 直接来自列（现行实现对列表不逐行预签名）
        self.assertEqual(item["cos_url"], "https://old.cos/key.py")

    def test_list_without_user_id_returns_empty(self):
        """user_id 无法解析时返回空列表而不抛异常。"""
        svc = self._make_service(cos_available=True)
        with patch(
            "backend.shared.strategy_storage._ensure_int_user_id",
            side_effect=ValueError("bad"),
        ):
            results = svc.list(user_id="bad_user")
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # delete()
    # ------------------------------------------------------------------

    def test_delete_returns_true_on_success(self):
        svc = self._make_service(cos_available=False)

        mock_session = MagicMock()
        select_result = MagicMock()
        select_result.fetchone.return_value = ("DRAFT", None)  # status, cos_key
        delete_result = MagicMock()
        delete_result.rowcount = 1
        mock_session.execute.side_effect = [select_result, delete_result]

        from contextlib import contextmanager

        @contextmanager
        def fake_db_ctx():
            yield mock_session

        with (
            patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1),
            patch("backend.shared.strategy_storage.get_db", new=fake_db_ctx),
        ):
            result = asyncio.run(svc.delete(strategy_id=1, user_id="1"))

        self.assertTrue(result)

    def test_delete_returns_false_when_not_found(self):
        svc = self._make_service(cos_available=False)

        mock_session = MagicMock()
        select_result = MagicMock()
        select_result.fetchone.return_value = None  # 行不存在
        mock_session.execute.side_effect = [select_result]

        from contextlib import contextmanager

        @contextmanager
        def fake_db_ctx():
            yield mock_session

        with (
            patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1),
            patch("backend.shared.strategy_storage.get_db", new=fake_db_ctx),
        ):
            result = asyncio.run(svc.delete(strategy_id=999, user_id="1"))

        self.assertFalse(result)

    def test_delete_refuses_running_strategy(self):
        """T-P3-02：运行中（SIM/LIVE）策略拒绝删除（防悬空引用），ValueError 直出话术。"""
        svc = self._make_service(cos_available=False)

        mock_session = MagicMock()
        select_result = MagicMock()
        select_result.fetchone.return_value = ("SIM", None)
        mock_session.execute.side_effect = [select_result]

        from contextlib import contextmanager

        @contextmanager
        def fake_db_ctx():
            yield mock_session

        with (
            patch("backend.shared.strategy_storage._ensure_int_user_id", return_value=1),
            patch("backend.shared.strategy_storage.get_db", new=fake_db_ctx),
            self.assertRaises(ValueError) as ctx,
        ):
            asyncio.run(svc.delete(strategy_id=7, user_id="1"))
        self.assertIn("运行", str(ctx.exception))

    # ------------------------------------------------------------------
    # 用户隔离
    # ------------------------------------------------------------------

    def test_list_user_isolation(self):
        """不同 user_id 的调用生成不同的查询 uid 参数，确保隔离。"""
        svc = self._make_service(cos_available=False)

        executed_params = []

        def mock_execute(stmt, params=None):
            executed_params.append(params)
            return MagicMock(fetchall=lambda: [])

        mock_session_a = MagicMock()
        mock_session_a.execute = mock_execute
        mock_session_b = MagicMock()
        mock_session_b.execute = mock_execute

        from contextlib import contextmanager

        sessions = [mock_session_a, mock_session_b]
        call_count = [0]

        @contextmanager
        def fake_db_ctx():
            idx = call_count[0] % len(sessions)
            call_count[0] += 1
            yield sessions[idx]

        with patch("backend.shared.strategy_storage.get_db", new=fake_db_ctx):
            with patch(
                "backend.shared.strategy_storage._ensure_int_user_id",
                side_effect=[1, 2],
            ):
                svc.list(user_id="user_a")
                svc.list(user_id="user_b")

        uids = [p["uid"] for p in executed_params if p and "uid" in p]
        self.assertEqual(uids, [1, 2], "两次调用应使用不同的 user_id 参数")


if __name__ == "__main__":
    unittest.main()
