"""Notebook screen flows wait for real UI changes and preserve verification evidence."""
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from zepuppeteer.models import Profile, TaskContext
from zepuppeteer.screen import Box, Word, TargetNotFound
from zepuppeteer.tasks.colab.open_notebook import check_login as colab_login
from zepuppeteer.tasks.kaggle.open_notebook import check_login as kaggle_login, check_runtime_connection
from zepuppeteer.websites.notebooks import save_notebook_evidence, wait_for_title


def email(value, y):
    return Word(value, Box(600, y, 240, 30), (1, y, 1))


class NotebookScreenTests(unittest.IsolatedAsyncioTestCase):
    def google_page(self):
        return SimpleNamespace(browser=SimpleNamespace(targets={}), current_url=AsyncMock(return_value="https://colab.research.google.com/drive/id"),
            find_text=AsyncMock(return_value=[]), click_target=AsyncMock(), words=AsyncMock(), press=AsyncMock())

    async def test_colab_waits_for_account_popup(self):
        page = self.google_page()
        page.words.side_effect = [[], [email("tester@example.com", 150)]]
        self.assertEqual(await colab_login(page, "tester@example.com"), "logged_in")
        self.assertEqual(page.words.await_count, 2)
        page.press.assert_awaited_once_with("esc")

    async def test_secondary_google_account_does_not_pass(self):
        page = self.google_page()
        page.words.return_value = [email("tester@example.com", 350), email("other@example.com", 180)]
        self.assertEqual(await colab_login(page, "tester@example.com"), "wrong_account")

    async def test_unreadable_google_menu_returns_unknown_and_closes(self):
        page = self.google_page()
        page.words.return_value = []
        self.assertEqual(await colab_login(page, "tester@example.com", timeout=0), "unknown")
        page.press.assert_awaited_once_with("esc")

    async def test_kaggle_waits_for_profile_navigation_and_returns_to_editor(self):
        notebook = "https://www.kaggle.com/code/author/notebook/edit"
        page = SimpleNamespace(current_url=AsyncMock(side_effect=[notebook, notebook, "https://www.kaggle.com/tester"]),
            click_target=AsyncMock(), hotkey=AsyncMock(), press=AsyncMock(), wait_for_text=AsyncMock(),
            title=AsyncMock(side_effect=["Notebook | Kaggle", "Tester | Kaggle"]))
        self.assertEqual(await kaggle_login(page, "tester"), "logged_in")
        self.assertEqual(page.current_url.await_count, 3)
        page.hotkey.assert_awaited_once_with("alt", "left")

    async def test_kaggle_failed_menu_does_not_go_back(self):
        page = SimpleNamespace(current_url=AsyncMock(return_value="https://www.kaggle.com/code/user/notebook"),
            click_target=AsyncMock(side_effect=[None, TargetNotFound("missing")]), hotkey=AsyncMock(), press=AsyncMock(),
            title=AsyncMock(return_value="Notebook | Kaggle"))
        with self.assertRaises(TargetNotFound):
            await kaggle_login(page, "tester")
        page.hotkey.assert_not_awaited()
        page.press.assert_awaited_once_with("esc")

    async def test_kaggle_opens_session_options_only_to_read_state(self):
        page = SimpleNamespace(browser=SimpleNamespace(targets={"kaggle_session_options": {"text": "Session options"}}),
            read_text=AsyncMock(side_effect=["", "Stop Session"]), click_target=AsyncMock(), press=AsyncMock())
        self.assertEqual(await check_runtime_connection(page), "connected")
        self.assertEqual(page.click_target.await_args.args, ("kaggle_session_options",))
        page.press.assert_awaited_once_with("esc")

    async def test_title_waits_for_loaded_notebook(self):
        page = SimpleNamespace(title=AsyncMock(side_effect=["Loading", "Example - Colab"]))
        self.assertEqual(await wait_for_title(page, re.compile(r" - Colab$"), timeout=1), "Example - Colab")

    async def test_success_saves_screen_and_verification_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            async def screenshot(*, path):
                Path(path).write_bytes(b"PNG fixture")
            page = SimpleNamespace(screenshot=screenshot, title=AsyncMock(return_value="Notebook - Colab"))
            ctx = TaskContext(None, Profile("test", root), {}, {}, root, "run", "step")
            outputs = await save_notebook_evidence(ctx, page, "colab", {"notebook_url": "https://colab.research.google.com/drive/id"},
                "https://colab.research.google.com/drive/id", runtime_status="connected")
            self.assertTrue(Path(outputs["screenshot_path"]).is_file())
            self.assertEqual(json.loads(Path(outputs["metadata_path"]).read_text()), outputs)
            self.assertTrue(outputs["account_verified"])

class RuntimeImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_icon_state_has_precedence_over_ocr(self):
        from zepuppeteer.websites.notebooks import visual_runtime_state
        page = SimpleNamespace(browser=SimpleNamespace(targets={
            "colab_runtime_connecting": {"image": "connecting.png", "region": [0.5, 0.1, 0.5, 0.3]}, "colab_runtime_connected": {"image": "connected.png", "region": [0.5, 0.1, 0.5, 0.3]}}),
            wait_for_target=AsyncMock())
        self.assertEqual(await visual_runtime_state(page, "colab", ("connecting", "connected")), "connecting")
        page.wait_for_target.assert_awaited_once_with("colab_runtime_connecting", timeout=0)

    async def test_missing_state_image_falls_through(self):
        from zepuppeteer.websites.notebooks import visual_runtime_state
        page = SimpleNamespace(browser=SimpleNamespace(targets={"kaggle_runtime_connected": {"image": "connected.png", "region": [0.5, 0.1, 0.5, 0.3]}}),
            wait_for_target=AsyncMock(side_effect=TargetNotFound("missing")))
        self.assertIsNone(await visual_runtime_state(page, "kaggle", ("connecting", "connected")))

    async def test_state_image_requires_runtime_region(self):
        from zepuppeteer.websites.notebooks import visual_runtime_state
        page = SimpleNamespace(browser=SimpleNamespace(targets={"kaggle_runtime_connected": {"image": "icon.png"}}))
        with self.assertRaisesRegex(ValueError, "region"):
            await visual_runtime_state(page, "kaggle", ("connected",))
