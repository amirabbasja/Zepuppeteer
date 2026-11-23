from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zepuppeteer.models import NeedsAttention, Profile, TaskContext
from zepuppeteer.tasks.colab.open_notebook import run
from zepuppeteer.websites.colab import DEFAULT_NOTEBOOK_URL, validate_notebook_url
from zepuppeteer.workflows import load_workflow, resolve_tasks

TASK = "zepuppeteer.tasks.colab.open_notebook"

class ColabConfigurationTests(unittest.TestCase):
    def test_valid_urls_preserve_paths(self):
        for url in (DEFAULT_NOTEBOOK_URL, "https://colab.research.google.com/drive/id?authuser=1#cell"):
            self.assertEqual(validate_notebook_url(url), url)
    def test_invalid_urls(self):
        for url in (None, "", "http://colab.research.google.com/", "https://colab.research.google.com.evil.test/",
                    "https://u:p@colab.research.google.com/", "https://colab.research.google.com:444/", "https://[broken"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_notebook_url(url)
    def test_workflow_resolves(self):
        self.assertIs(resolve_tasks(load_workflow("config/workflows/colab.toml"))["colab.open_notebook"], run)

class ColabTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.manifest = self.root / "profile.toml"
        self.manifest.write_text('[profile.platforms.colab]\nlogin_username="user@example.test"\n'
            'notebook_url="https://colab.research.google.com/drive/id"\nnotebook_name="Example"\nauto_connect=true\n')
        self.page = SimpleNamespace(close=AsyncMock(), title=AsyncMock(return_value="Example - Colab"),
                                    click_target=AsyncMock(), click_text=AsyncMock(), screenshot=AsyncMock())
        self.ctx = TaskContext(SimpleNamespace(new_page=AsyncMock(return_value=self.page)),
            Profile("one", self.root / "browser", manifest_path=self.manifest), {}, {}, self.root, "run", "open")
        self.site = SimpleNamespace(open_notebook=AsyncMock(return_value={}),
            run_all_and_wait_for_last_cell=AsyncMock(return_value="last-visible-code-cell"))
        for name, value in (("ColabSite", lambda page: self.site), ("check_login", AsyncMock(return_value="logged_in"))):
            patcher = patch(f"{TASK}.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_new_connection_runs_once_and_closes(self):
        with patch(f"{TASK}.check_runtime_connection", AsyncMock(side_effect=["disconnected", "connected"])):
            result = await run(self.ctx)
        self.assertTrue(result.outputs["job_started"])
        self.site.run_all_and_wait_for_last_cell.assert_awaited_once()
        self.assertEqual([call.args[0] for call in self.page.click_target.await_args_list], ["colab_connection_options", "colab_connect_hosted"])
        self.page.close.assert_awaited_once()

    async def test_existing_runtime_updates_without_restarting_or_exiting(self):
        with patch(f"{TASK}.check_runtime_connection", AsyncMock(return_value="connected")), \
                patch(f"{TASK}.keepAlive", AsyncMock()) as keep:
            result = await run(self.ctx)
        self.assertEqual(result.status, "success")
        keep.assert_awaited_once_with(self.page, task_number=5)
        self.site.run_all_and_wait_for_last_cell.assert_not_awaited()

    async def test_wrong_account_and_notebook_preserve_page(self):
        with patch(f"{TASK}.check_login", AsyncMock(return_value="wrong_account")):
            with self.assertRaisesRegex(NeedsAttention, "wrong_account"):
                await run(self.ctx)
        self.page.close.assert_not_awaited()
        self.page.title.return_value = "Different - Colab"
        with self.assertRaisesRegex(NeedsAttention, "name mismatch"):
            await run(self.ctx)

    async def test_invalid_configuration_fails_before_tab(self):
        self.manifest.write_text('[profile.platforms.colab]\nlogin_username="user@example.test"\nnotebook_url="https://wrong.test"')
        with self.assertRaises(ValueError):
            await run(self.ctx)
        self.ctx.browser.new_page.assert_not_awaited()

    async def test_missing_name_requires_attention_before_tab(self):
        self.manifest.write_text('[profile.platforms.colab]\nlogin_username="user@example.test"')
        with self.assertRaisesRegex(NeedsAttention, "notebook_name"):
            await run(self.ctx)
        self.ctx.browser.new_page.assert_not_awaited()

    async def test_invalid_runtime_options_fail_before_tab(self):
        original = self.manifest.read_text()
        for extra in ('auto_connect="false"', 'job_start_timeout_seconds=0'):
            self.manifest.write_text(original.replace('auto_connect=true', extra))
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                await run(self.ctx)
        self.ctx.browser.new_page.assert_not_awaited()
