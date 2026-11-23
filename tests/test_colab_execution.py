from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zepuppeteer.models import NeedsAttention
from zepuppeteer.screen import TargetNotFound
from zepuppeteer.websites.colab import ColabSite

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def page(self, targets=None):
        if targets:
            targets = {name: {"image": "icon.png", "region": [0.5, 0.5, 0.5, 0.5]}
                       for name in ("colab_last_cell_running", "colab_last_cell_anchor", "colab_notebook_idle")}
        return SimpleNamespace(browser=SimpleNamespace(targets=targets or {}), hotkey=AsyncMock(),
                               click_target=AsyncMock(), wait_for_target=AsyncMock())
    async def test_missing_running_target_never_starts_work(self):
        page = self.page()
        with self.assertRaises(NeedsAttention):
            await ColabSite(page).run_all_and_wait_for_last_cell()
        page.click_target.assert_not_awaited()
    async def test_already_running_never_restarts(self):
        page = self.page({"colab_last_cell_running": {"image": "icon.png"}})
        with self.assertRaisesRegex(NeedsAttention, "already executing"):
            await ColabSite(page).run_all_and_wait_for_last_cell()
        page.click_target.assert_not_awaited()
    async def test_clicks_once_then_requires_last_cell_visual_evidence(self):
        page = self.page({"colab_last_cell_running": {"image": "icon.png"}})
        page.wait_for_target.side_effect = [object(), object(), TargetNotFound("idle"), object(), object()]
        self.assertEqual(await ColabSite(page).run_all_and_wait_for_last_cell(), "last-visible-code-cell")
        page.click_target.assert_awaited_once()
        self.assertEqual(page.wait_for_target.await_count, 5)
    async def test_invalid_timeout(self):
        for timeout in (0, -1, True, "300", float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await ColabSite(self.page()).run_all_and_wait_for_last_cell(timeout_seconds=timeout)
