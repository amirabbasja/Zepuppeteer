from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zepuppeteer.models import NeedsAttention
from zepuppeteer.tasks.colab.open_notebook import _parse_runtime_resources, getRemainingQuota

SAMPLE = "You currently have zero compute units available. At your current usage level, this runtime may last up to 1 hour 40 minutes."

class QuotaTests(unittest.IsolatedAsyncioTestCase):
    def test_quota_and_duration_parsing(self):
        result = _parse_runtime_resources(SAMPLE)
        self.assertEqual(result["remaining_quota"], 0)
        self.assertEqual(result["time_remaining"], 6000)
        self.assertEqual(datetime.fromisoformat(result["end_time"]).utcoffset(), timedelta(hours=3, minutes=30))
        for duration, seconds in (("1 day 2 hours 3 minutes 4 seconds", 93784), ("1.5 hours", 5400), ("45 minutes", 2700)):
            result = _parse_runtime_resources(f"You currently have 1,234.5 compute units available. This runtime may last up to {duration}.")
            self.assertEqual(result["remaining_quota"], 1234.5)
            self.assertEqual(result["time_remaining"], seconds)
    async def test_missing_region_prevents_unscoped_notebook_read(self):
        page = SimpleNamespace(browser=SimpleNamespace(targets={}), read_text=AsyncMock())
        with self.assertRaises(NeedsAttention):
            await getRemainingQuota(page)
        page.read_text.assert_not_awaited()
    async def test_reads_only_resources_region(self):
        region = [0.7, 0.3, 0.3, 0.7]
        page = SimpleNamespace(browser=SimpleNamespace(targets={"colab_resources": {"region": region}}),
                               read_text=AsyncMock(return_value=SAMPLE))
        with patch("zepuppeteer.tasks.colab.open_notebook.keepAlive", AsyncMock()):
            result = await getRemainingQuota(page)
        self.assertEqual(result["time_remaining"], 6000)
        page.read_text.assert_awaited_once_with(region)
