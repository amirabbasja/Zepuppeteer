"""Screen readiness checks do not inspect the DOM or browser responses."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zepuppeteer.models import NeedsAttention
from zepuppeteer.screen import TargetNotFound
from zepuppeteer.websites.colab import ColabSite

class ColabSiteTests(unittest.IsolatedAsyncioTestCase):
    def page(self, url="https://colab.research.google.com/drive/id"):
        return SimpleNamespace(goto=AsyncMock(), wait_for_text=AsyncMock(), read_text=AsyncMock(return_value="File Runtime"),
                               current_url=AsyncMock(return_value=url), url=url, title=AsyncMock(return_value="Notebook - Colab"))
    async def test_editor_requires_visible_runtime_menu_and_title(self):
        page = self.page()
        result = await ColabSite(page).open_notebook(page.url)
        self.assertEqual(result["title"], "Notebook - Colab")
        page.wait_for_text.assert_awaited_once()
    async def test_login_redirect_requires_manual_attention(self):
        page = self.page("https://accounts.google.com/signin")
        page.wait_for_text.side_effect = TargetNotFound("missing")
        with self.assertRaisesRegex(NeedsAttention, "sign-in"):
            await ColabSite(page).open_notebook("https://colab.research.google.com/drive/id")
    async def test_permission_and_unexpected_redirect(self):
        page = self.page()
        page.read_text.return_value = "You need access"
        with self.assertRaises(NeedsAttention):
            await ColabSite(page).open_notebook(page.url)
        page.current_url.return_value = "https://unexpected.test"
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            await ColabSite(page).open_notebook(page.url)
