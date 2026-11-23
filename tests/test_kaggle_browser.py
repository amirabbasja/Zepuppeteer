from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zepuppeteer.models import NeedsAttention
from zepuppeteer.websites.kaggle import KaggleSite
from zepuppeteer.tasks.kaggle.open_notebook import check_login, check_runtime_connection

class KaggleSiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_notebook_opens_editor_without_copying(self):
        url = "https://www.kaggle.com/code/user/notebook?x=1#cell"
        page = SimpleNamespace(goto=AsyncMock(), wait_for_text=AsyncMock(), title=AsyncMock(return_value="Notebook | Kaggle"),
                               current_url=AsyncMock(return_value=url), url=url)
        await KaggleSite(page).open_notebook(url)
        page.goto.assert_awaited_once_with("https://www.kaggle.com/code/user/notebook/edit?x=1#cell")
        page.wait_for_text.assert_awaited_once()
    async def test_account_check_reads_menu_profile_url(self):
        page = SimpleNamespace(current_url=AsyncMock(side_effect=["https://www.kaggle.com/code/author/notebook", "https://www.kaggle.com/my-user"]),
                               click_target=AsyncMock(), click_text=AsyncMock(), hotkey=AsyncMock(), press=AsyncMock(), wait_for_text=AsyncMock(),
                               title=AsyncMock(side_effect=["Notebook | Kaggle", "Tester | Kaggle"]))
        self.assertEqual(await check_login(page, "my-user"), "logged_in")
        self.assertEqual([call.args[0] for call in page.click_target.await_args_list], ["kaggle_account", "kaggle_profile"])
        page.hotkey.assert_awaited_once_with("alt", "left")
    async def test_runtime_states(self):
        for text, state in (("Stop Session", "connected"), ("Starting RAM", "connecting"),
                            ("Start Session", "disconnected"), ("Unknown", "unknown")):
            page = SimpleNamespace(browser=SimpleNamespace(targets={}), read_text=AsyncMock(return_value=text))
            self.assertEqual(await check_runtime_connection(page, timeout=0), state)
