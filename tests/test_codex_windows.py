"""Codex can place a weekly limit in either rate-limit window slot."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.providers.codex import CodexProvider


class CodexWindowTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, rate_limit):
        provider = CodexProvider()
        provider._usage_with_refresh = AsyncMock(return_value=SimpleNamespace(
            status_code=200,
            json=lambda: {"rate_limit": rate_limit},
        ))
        return await provider.fetch({"access_token": "test-token"})

    async def test_weekly_limit_in_primary_slot(self):
        result = await self._fetch({
            "primary_window": {"limit_window_seconds": 604800, "used_percent": 42},
            "secondary_window": None,
        })
        self.assertIsNone(result.error)
        self.assertEqual([(m.key, m.label, m.utilization) for m in result.metrics],
                         [("primary", "Weekly", 42.0)])

    async def test_session_and_weekly_limits_in_usual_slots(self):
        result = await self._fetch({
            "primary_window": {"limit_window_seconds": 18000, "used_percent": 10},
            "secondary_window": {"limit_window_seconds": 604800, "used_percent": 30},
        })
        self.assertEqual([(m.key, m.label) for m in result.metrics],
                         [("primary", "Session (5h)"), ("secondary", "Weekly")])


if __name__ == "__main__":
    unittest.main()
